"""Synthetic tests for the isolated persistent strategy process."""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from research_center import strategy_sandbox as sandbox_module
from research_center.strategy_sandbox import (
    MAX_MESSAGE_BYTES,
    MAX_SOURCE_BYTES,
    PythonSandbox,
    SandboxError,
    SandboxUnavailable,
    _StrictJSONError,
    _TrustedNativeProbeProcess,
    _build_profile,
    _parse_response,
    _strict_json_loads,
    _validate_json_value,
)


DARWIN_SANDBOX_AVAILABLE = (
    os.sys.platform == "darwin" and sandbox_module.SANDBOX_EXEC_PATH.is_file()
)


class SourceValidationTests(unittest.TestCase):
    def test_invalid_utf8_syntax_and_size_fail_before_launch(self) -> None:
        with self.assertRaises(SandboxError) as invalid_utf8:
            _TrustedNativeProbeProcess(b"\xff")
        self.assertEqual(invalid_utf8.exception.code, "source_invalid_utf8")

        with self.assertRaises(SandboxError) as syntax_error:
            _TrustedNativeProbeProcess(b"def handle(:\n    return None\n")
        self.assertEqual(syntax_error.exception.code, "source_syntax")

        with self.assertRaises(SandboxError) as too_large:
            _TrustedNativeProbeProcess(b"#" * (MAX_SOURCE_BYTES + 1))
        self.assertEqual(too_large.exception.code, "source_too_large")

    def test_non_darwin_platform_fails_closed(self) -> None:
        instance = _TrustedNativeProbeProcess(b"def handle(payload):\n    return payload\n")
        with mock.patch.object(sandbox_module.sys, "platform", "linux"):
            with self.assertRaises(SandboxError) as unavailable:
                instance.__enter__()
        self.assertEqual(unavailable.exception.code, "sandbox_unavailable")
        self.assertTrue(instance._closed)

    def test_sampled_memory_and_timeout_configuration_is_validated(self) -> None:
        with self.assertRaises(SandboxError) as invalid_memory:
            _TrustedNativeProbeProcess(b"def handle(payload):\n    return payload\n", memory_limit_mib=0)
        self.assertEqual(invalid_memory.exception.code, "invalid_memory_limit")

        with self.assertRaises(SandboxError) as invalid_session:
            _TrustedNativeProbeProcess(b"def handle(payload):\n    return payload\n", session_timeout=float("inf"))
        self.assertEqual(invalid_session.exception.code, "invalid_session_timeout")

    def test_public_constructor_refuses_before_examining_source(self) -> None:
        with self.assertRaises(SandboxUnavailable) as unavailable:
            PythonSandbox(object())  # type: ignore[arg-type]
        self.assertEqual(
            unavailable.exception.code,
            "native_process_environment_not_isolated",
        )


class StrictProtocolTests(unittest.TestCase):
    def test_strict_json_rejects_nonfinite_duplicate_keys_and_overflow(self) -> None:
        for raw in (b"NaN", b"Infinity", b"1e999", b'{"x":1,"x":2}'):
            with self.subTest(raw=raw), self.assertRaises(_StrictJSONError):
                _strict_json_loads(raw)

    def test_strict_json_rejects_depth_and_node_overflow(self) -> None:
        nested = None
        for _ in range(33):
            nested = [nested]
        with self.assertRaises(_StrictJSONError):
            _validate_json_value(nested)

        with self.assertRaises(_StrictJSONError):
            _validate_json_value([None] * 10_000)

    def test_response_requires_one_line_exact_shape_and_matching_nonce(self) -> None:
        self.assertEqual(
            _parse_response(b'{"request_id":"abc","result":{"n":3}}\n', "abc"),
            {"n": 3},
        )
        invalid_responses = (
            b'{"request_id":"abc","request_id":"abc","result":3}\n',
            b'{"request_id":"abc","result":NaN}\n',
            b'{"request_id":"abc","result":1e999}\n',
            b'{"request_id":"wrong","result":3}\n',
            b'{"request_id":"abc","result":3,"extra":1}\n',
            b'{"request_id":"abc","result":3}\n{"request_id":"abc","result":4}\n',
            b'{"request_id":"abc","result":3}',
            b"x" * (MAX_MESSAGE_BYTES + 1),
        )
        for raw in invalid_responses:
            with self.subTest(raw=raw[:60]), self.assertRaises(_StrictJSONError):
                _parse_response(raw, "abc")

    def test_profile_is_deny_default_and_does_not_grant_broad_system_reads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsbx_profile_test_") as temporary:
            numpy_path = Path(temporary) / "site-packages" / "numpy"
            numpy_path.mkdir(parents=True)
            profile = _build_profile(
                Path("/runtime/python"),
                Path("/runtime"),
                Path("/private/tmp/owned"),
                [numpy_path],
            )
            self.assertIn("(deny default)", profile)
            self.assertIn('(literal "/")', profile)
            self.assertIn('(subpath "/private/tmp/owned")', profile)
            self.assertIn(f'(subpath "{numpy_path}")', profile)
            self.assertNotIn('(subpath "/System")', profile)
            self.assertNotIn("network-outbound", profile)
            self.assertNotIn("process-fork", profile)


@unittest.skipUnless(DARWIN_SANDBOX_AVAILABLE, "requires macOS sandbox-exec")
class RealSandboxTests(unittest.TestCase):
    def test_persistent_json_round_trips_keep_strategy_state(self) -> None:
        source = (
            b"count = 0\n"
            b"def handle(payload):\n"
            b"    global count\n"
            b"    count += 1\n"
            b"    return {'count': count, 'payload': payload}\n"
        )
        with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
            self.assertEqual(
                sandbox.exchange({"bar": 1}),
                {"count": 1, "payload": {"bar": 1}},
            )
            self.assertEqual(
                sandbox.exchange(["bar", 2]),
                {"count": 2, "payload": ["bar", 2]},
            )

    def test_numpy_pandas_imports_and_synthetic_calculation(self) -> None:
        source = (
            b"import numpy as np\n"
            b"import pandas as pd\n"
            b"def handle(payload):\n"
            b"    values = np.asarray(payload, dtype=float)\n"
            b"    rolling = pd.Series(values).rolling(2).mean().iloc[-1]\n"
            b"    return {'sum': float(values.sum()), 'rolling': float(rolling)}\n"
        )
        with _TrustedNativeProbeProcess(source, memory_limit_mib=256, session_timeout=30) as sandbox:
            result = sandbox.exchange([1.0, 2.0, 3.0], timeout=15)
        self.assertEqual(result, {"sum": 6.0, "rolling": 2.5})

    def test_os_profile_denies_outside_reads_writes_symlink_escape_network_and_fork(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsbx_synthetic_canary_") as temporary:
            outside = Path(temporary)
            canary = outside / "read-canary.txt"
            canary.write_bytes(b"synthetic canary only")
            blocked_write = outside / "blocked-write.txt"
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.15)
            port = listener.getsockname()[1]
            source = (
                b"import os, socket\n"
                b"def attempt(fn):\n"
                b"    try:\n"
                b"        fn()\n"
                b"        return 'allowed'\n"
                b"    except OSError:\n"
                b"        return 'denied'\n"
                b"def handle(payload):\n"
                b"    cwd_path = os.path.join(os.getcwd(), 'blocked-private-write.txt')\n"
                b"    read = attempt(lambda: open(payload['canary'], 'rb').read())\n"
                b"    symlink = attempt(lambda: open(payload['escape'], 'rb').read())\n"
                b"    outside_write = attempt(lambda: open(payload['write'], 'wb').write(b'x'))\n"
                b"    private_write = attempt(lambda: open(cwd_path, 'wb').write(b'x'))\n"
                b"    def connect():\n"
                b"        with socket.create_connection(('127.0.0.1', payload['port']), timeout=0.2):\n"
                b"            pass\n"
                b"    network = attempt(connect)\n"
                b"    def fork():\n"
                b"        pid = os.fork()\n"
                b"        if pid == 0:\n"
                b"            os._exit(0)\n"
                b"        os.waitpid(pid, 0)\n"
                b"    fork_result = attempt(fork)\n"
                b"    return {'read': read, 'symlink': symlink, 'outside_write': outside_write, 'private_write': private_write, 'network': network, 'fork': fork_result}\n"
            )
            try:
                with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
                    escape = sandbox._workspace / "escape-link.txt"
                    escape.symlink_to(canary)
                    result = sandbox.exchange(
                        {
                            "canary": str(canary),
                            "escape": str(escape),
                            "write": str(blocked_write),
                            "port": port,
                        }
                    )
                self.assertEqual(
                    result,
                    {
                        "read": "denied",
                        "symlink": "denied",
                        "outside_write": "denied",
                        "private_write": "denied",
                        "network": "denied",
                        "fork": "denied",
                    },
                )
                self.assertFalse(blocked_write.exists())
                with self.assertRaises(socket.timeout):
                    listener.accept()
            finally:
                listener.close()

    def test_child_environment_excludes_parent_canary(self) -> None:
        source = b"import os\ndef handle(payload):\n    return os.environ.get('QSBX_PARENT_CANARY')\n"
        with mock.patch.dict(os.environ, {"QSBX_PARENT_CANARY": "synthetic-only"}):
            with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
                result = sandbox.exchange(None)
        self.assertIsNone(result)

    def test_randomized_synthetic_victim_environment_leak_is_measured_and_public_entry_is_blocked(self) -> None:
        """Measure the known native leak; a passing probe is not a security pass."""
        nonce = secrets.token_hex(24)
        nonce_digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        victim_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "QUANT_SANDBOX_VICTIM_CANARY": nonce,
        }
        victim = subprocess.Popen(
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-c",
                "import time; time.sleep(15)",
            ],
            env=victim_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        source = (
            b"import os, types\n"
            b"os.uname = lambda: types.SimpleNamespace(release='99.0', sysname='Darwin', machine='arm64', version='probe', nodename='probe')\n"
            b"import ctypes, hashlib\n"
            b"def handle(payload):\n"
            b"    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)\n"
            b"    mib = (ctypes.c_int * 3)(1, 49, int(payload['pid']))\n"
            b"    buffer = ctypes.create_string_buffer(65536)\n"
            b"    length = ctypes.c_size_t(len(buffer))\n"
            b"    result = lib.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0)\n"
            b"    entries = [part for part in buffer.raw[:length.value].split(b'\\0') if part.startswith(b'QUANT_SANDBOX_VICTIM_CANARY=')] if result == 0 else []\n"
            b"    matched = any(hashlib.sha256(item.split(b'=', 1)[1]).hexdigest() == payload['nonce_digest'] for item in entries)\n"
            b"    return {'returncode': result, 'errno': ctypes.get_errno(), 'matched_nonce_digest': matched, 'canary_entries': len(entries)}\n"
        )
        try:
            time.sleep(0.1)
            with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
                result = sandbox.exchange(
                    {"pid": victim.pid, "nonce_digest": nonce_digest}, timeout=5
                )
            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["matched_nonce_digest"])
            self.assertEqual(result["canary_entries"], 1)
            with self.assertRaises(SandboxUnavailable) as unavailable:
                PythonSandbox(source, session_timeout=20)
            self.assertEqual(
                unavailable.exception.code,
                "native_process_environment_not_isolated",
            )
        finally:
            if victim.poll() is None:
                victim.terminate()
            try:
                victim.wait(timeout=2)
            except subprocess.TimeoutExpired:
                victim.kill()
                victim.wait(timeout=2)

    def test_rss_observation_failure_fails_closed(self) -> None:
        source = b"def handle(payload):\n    return payload\n"
        with mock.patch.object(sandbox_module.subprocess, "run", side_effect=OSError):
            sandbox = _TrustedNativeProbeProcess(source, session_timeout=20)
            sandbox.__enter__()
            with self.assertRaises(SandboxError) as failed:
                sandbox.exchange(None)
        self.assertEqual(failed.exception.code, "rss_unavailable")
        self.assertTrue(sandbox._closed)

    def test_nonfinite_strategy_result_fails_and_cleans_session(self) -> None:
        source = b"def handle(payload):\n    return float('inf')\n"
        sandbox = _TrustedNativeProbeProcess(source, session_timeout=20)
        sandbox.__enter__()
        workspace = sandbox._workspace
        pid = sandbox._process.pid
        with self.assertRaises(SandboxError) as failed:
            sandbox.exchange(None)
        self.assertIn(failed.exception.code, {"worker_exit", "protocol_error"})
        self.assertTrue(sandbox._closed)
        self.assertFalse(workspace.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_multiple_json_frames_on_the_ipc_channel_abort_the_session(self) -> None:
        source = (
            b"import json, os, sys\n"
            b"def handle(payload):\n"
            b"    frame = sys._getframe().f_back\n"
            b"    while frame is not None and 'protocol_fd' not in frame.f_locals:\n"
            b"        frame = frame.f_back\n"
            b"    request = frame.f_locals['request']\n"
            b"    response = json.dumps({'request_id': request['request_id'], 'result': 1}, separators=(',', ':')).encode() + b'\\n'\n"
            b"    os.write(frame.f_locals['protocol_fd'], response + response)\n"
            b"    return None\n"
        )
        with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
            with self.assertRaises(SandboxError) as failed:
                sandbox.exchange(None)
        self.assertEqual(failed.exception.code, "protocol_error")

    def test_stdout_and_stderr_floods_are_bounded_and_not_returned(self) -> None:
        cases = (
            (
                "stdout_limit",
                b"import os\ndef handle(payload):\n    os.write(1, b'x' * 131072)\n    return None\n",
            ),
            (
                "stderr_limit",
                b"import os\ndef handle(payload):\n    os.write(2, b'x' * 16384)\n    return None\n",
            ),
        )
        for expected, source in cases:
            with self.subTest(expected=expected):
                with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
                    with self.assertRaises(SandboxError) as failed:
                        sandbox.exchange(None, timeout=3)
                self.assertEqual(failed.exception.code, expected)

    def test_stderr_limit_applies_to_the_whole_session(self) -> None:
        source = (
            b"import os\n"
            b"def handle(payload):\n"
            b"    os.write(2, b'x' * 5000)\n"
            b"    return payload\n"
        )
        with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
            self.assertEqual(sandbox.exchange(1), 1)
            with self.assertRaises(SandboxError) as failed:
                sandbox.exchange(2)
        self.assertEqual(failed.exception.code, "stderr_limit")

    def test_startup_that_never_reads_stdin_is_killed_by_wall_timeout(self) -> None:
        source = b"while True:\n    pass\ndef handle(payload):\n    return payload\n"
        sandbox = _TrustedNativeProbeProcess(source, session_timeout=10)
        sandbox.__enter__()
        workspace = sandbox._workspace
        with self.assertRaises(SandboxError) as failed:
            sandbox.exchange({"synthetic": True}, timeout=0.35)
        self.assertEqual(failed.exception.code, "timeout")
        self.assertFalse(workspace.exists())

    def test_hung_handler_is_killed_by_cancellation(self) -> None:
        source = b"def handle(payload):\n    while True:\n        pass\n"
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        sandbox = _TrustedNativeProbeProcess(source, session_timeout=10)
        sandbox.__enter__()
        workspace = sandbox._workspace
        timer.start()
        try:
            with self.assertRaises(SandboxError) as failed:
                sandbox.exchange(None, timeout=3, cancelled=cancel_event.is_set)
        finally:
            timer.cancel()
        self.assertEqual(failed.exception.code, "cancelled")
        self.assertFalse(workspace.exists())

    def test_sampled_rss_ceiling_kills_a_synthetic_allocation(self) -> None:
        source = (
            b"import time\n"
            b"blocks = []\n"
            b"for _ in range(24):\n"
            b"    block = bytearray(8 * 1024 * 1024)\n"
            b"    block[::4096] = b'x' * len(block[::4096])\n"
            b"    blocks.append(block)\n"
            b"    time.sleep(0.025)\n"
            b"def handle(payload):\n"
            b"    return len(blocks)\n"
        )
        sandbox = _TrustedNativeProbeProcess(source, memory_limit_mib=64, session_timeout=20)
        sandbox.__enter__()
        workspace = sandbox._workspace
        time.sleep(0.4)
        with self.assertRaises(SandboxError) as failed:
            sandbox.exchange(None, timeout=10)
        self.assertEqual(failed.exception.code, "memory_limit_exceeded")
        self.assertFalse(workspace.exists())

    def test_source_print_is_suppressed_and_ipc_remains_valid(self) -> None:
        source = b"def handle(payload):\n    print('synthetic log')\n    return payload\n"
        with _TrustedNativeProbeProcess(source, session_timeout=20) as sandbox:
            self.assertEqual(sandbox.exchange({"ok": True}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
