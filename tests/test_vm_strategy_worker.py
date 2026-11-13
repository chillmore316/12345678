"""Synthetic NDJSON tests for the guest-only strategy worker.

Protocol tests call the private serving helper with in-memory streams and benign
synthetic strategies.  They do not launch the public worker on a Linux host:
Linux alone does not establish a VM/container boundary.  On macOS the public
CLI test verifies it refuses before reading stdin.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PROJECT_ROOT / "research_center" / "vm_strategy_worker.py"
PYTHON_PATH = Path(sys.executable)
_WORKER = runpy.run_path(str(WORKER_PATH))
_serve = _WORKER["_serve"]
MAX_SOURCE_BYTES = _WORKER["MAX_SOURCE_BYTES"]
MAX_INPUT_LINE_BYTES = _WORKER["MAX_INPUT_LINE_BYTES"]
MAX_RESPONSE_BYTES = _WORKER["MAX_RESPONSE_BYTES"]
MAX_STDERR_BYTES = _WORKER["MAX_STDERR_BYTES"]


def _frame(request_id: str, source: str) -> bytes:
    return json.dumps(
        {"request_id": request_id, "source": source},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _exchange(initial: bytes, *frames: bytes) -> tuple[int, list[Any], bytes, bytes]:
    stdin = io.BytesIO(initial + b"".join(frames))
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    status = _serve(stdin, stdout, stderr)
    output = stdout.getvalue()
    parsed = [json.loads(line) for line in output.splitlines()]
    return status, parsed, output, stderr.getvalue()


class WorkerProtocolTests(unittest.TestCase):
    def test_ready_stateful_callbacks_and_prints_keep_stdout_protocol_only(self) -> None:
        source = (
            "print('startup log')\n"
            "count = 0\n"
            "def handle(payload):\n"
            "    global count\n"
            "    count += 1\n"
            "    print('callback log', count)\n"
            "    return {'count': count, 'payload': payload}\n"
        )
        status, frames, output, stderr = _exchange(
            _frame("start-1", source),
            b'{"request_id":"call-1","payload":{"x":1}}\n',
            b'{"request_id":"call-2","payload":["y",2]}\n',
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            frames,
            [
                {"request_id": "start-1", "result": {"ready": True}},
                {"request_id": "call-1", "result": {"count": 1, "payload": {"x": 1}}},
                {"request_id": "call-2", "result": {"count": 2, "payload": ["y", 2]}},
            ],
        )
        self.assertEqual(output.count(b"\n"), 3)
        self.assertIn(b"startup log", stderr)
        self.assertIn(b"callback log", stderr)
        self.assertLessEqual(len(stderr), MAX_STDERR_BYTES)

    def test_exception_is_redacted_and_uses_fixed_error_code(self) -> None:
        source = "def handle(payload):\n    raise RuntimeError('PRIVATE_TRACEBACK_MARKER')\n"
        status, frames, output, stderr = _exchange(
            _frame("start", source),
            b'{"request_id":"req","payload":null}\n',
        )
        self.assertEqual(status, 0)
        self.assertEqual(frames, [
            {"request_id": "start", "result": {"ready": True}},
            {"request_id": "req", "error": "callback_failed"},
        ])
        self.assertNotIn(b"PRIVATE_TRACEBACK_MARKER", output + stderr)
        self.assertNotIn(b"Traceback", output + stderr)

    def test_startup_syntax_error_returns_only_fixed_error(self) -> None:
        status, frames, output, stderr = _exchange(
            _frame("start", "def handle(:\n    return None\n")
        )
        self.assertEqual(status, 1)
        self.assertEqual(frames, [{"request_id": "start", "error": "startup_failed"}])
        self.assertNotIn(b"def handle", output + stderr)
        self.assertNotIn(b"SyntaxError", output + stderr)

    def test_strict_parser_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        source = "def handle(payload):\n    return payload\n"
        invalid_callback_frames = (
            b'{"request_id":"req","request_id":"req","payload":1}\n',
            b'{"request_id":"req","payload":{"x":1,"x":2}}\n',
            b'{"request_id":"req","payload":NaN}\n',
            b'{"request_id":"req","payload":Infinity}\n',
            b'{"request_id":"req","payload":1e999}\n',
        )
        for invalid in invalid_callback_frames:
            with self.subTest(frame=invalid[:80]):
                status, frames, output, _stderr = _exchange(_frame("start", source), invalid)
                self.assertEqual(status, 2)
                self.assertEqual(frames, [{"request_id": "start", "result": {"ready": True}}])
                self.assertNotIn(b"Infinity", output)
                self.assertNotIn(b"duplicate", output)

        status, frames, output, _stderr = _exchange(
            b'{"request_id":"start","source":"def handle(payload):\\n return payload\\n","extra":1}\n'
        )
        self.assertEqual(status, 2)
        self.assertEqual(frames, [])
        self.assertEqual(output, b"")

    def test_worker_rejects_invalid_request_id_format(self) -> None:
        source = "def handle(payload):\n    return payload\n"
        invalid_ids = ("", "!bad", "x" * 65, "non-ascii-中文")
        for index, request_id in enumerate(invalid_ids):
            with self.subTest(request_id=request_id):
                encoded_id = json.dumps(request_id, ensure_ascii=False).encode("utf-8")
                callback = b'{"request_id":' + encoded_id + b',"payload":null}\n'
                status, frames, output, _stderr = _exchange(
                    _frame(f"start-{index}", source), callback
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    frames,
                    [{"request_id": f"start-{index}", "result": {"ready": True}}],
                )
                self.assertNotIn(encoded_id, output)

    def test_depth_and_node_limits_terminate_without_echoing_bad_frame(self) -> None:
        source = "def handle(payload):\n    return payload\n"
        too_deep = "[" * 34 + "null" + "]" * 34
        too_many_nodes = "[" + ",".join(["null"] * 10_000) + "]"
        self.assertLess(len(too_many_nodes.encode("ascii")), MAX_INPUT_LINE_BYTES)
        for payload in (too_deep, too_many_nodes):
            with self.subTest(payload_length=len(payload)):
                callback = (
                    '{"request_id":"req","payload":' + payload + "}\n"
                ).encode("ascii")
                status, frames, output, _stderr = _exchange(_frame("start", source), callback)
                self.assertEqual(status, 2)
                self.assertEqual(frames, [{"request_id": "start", "result": {"ready": True}}])
                self.assertNotIn(payload.encode("ascii"), output)

    def test_oversize_line_and_source_are_rejected_without_echo(self) -> None:
        source = "def handle(payload):\n    return payload\n"
        oversized_line = b"{" + (b"x" * MAX_INPUT_LINE_BYTES) + b"}\n"
        self.assertGreater(len(oversized_line), MAX_INPUT_LINE_BYTES)
        status, frames, output, _stderr = _exchange(oversized_line)
        self.assertEqual(status, 2)
        self.assertEqual(frames, [])
        self.assertEqual(output, b"")

        too_large_source = "#" + ("s" * MAX_SOURCE_BYTES)
        self.assertEqual(len(too_large_source.encode("utf-8")), MAX_SOURCE_BYTES + 1)
        oversized_source_frame = _frame("start", too_large_source)
        self.assertLess(len(oversized_source_frame), MAX_INPUT_LINE_BYTES)
        status, frames, output, _stderr = _exchange(oversized_source_frame)
        self.assertEqual(status, 2)
        self.assertEqual(frames, [])
        self.assertEqual(output, b"")
        self.assertNotIn(b"ssss", output)

    def test_nonfinite_and_oversize_callback_results_return_safe_codes(self) -> None:
        for return_expression, expected_error in (
            ("float('nan')", "result_invalid"),
            ("'x' * 65536", "result_too_large"),
        ):
            source = f"def handle(payload):\n    return {return_expression}\n"
            status, frames, output, _stderr = _exchange(
                _frame("start", source),
                b'{"request_id":"req","payload":null}\n',
            )
            self.assertEqual(status, 0)
            self.assertEqual(
                frames,
                [
                    {"request_id": "start", "result": {"ready": True}},
                    {"request_id": "req", "error": expected_error},
                ],
            )
            self.assertTrue(
                all(len(line) <= MAX_RESPONSE_BYTES for line in output.splitlines(keepends=True))
            )

    def test_stderr_cap_applies_across_startup_and_callbacks(self) -> None:
        source = (
            "print('s' * 5000)\n"
            "def handle(payload):\n"
            "    print('c' * 5000)\n"
            "    return True\n"
        )
        status, frames, _output, stderr = _exchange(
            _frame("start", source),
            b'{"request_id":"req-1","payload":null}\n',
            b'{"request_id":"req-2","payload":null}\n',
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(frames), 3)
        self.assertEqual(len(stderr), MAX_STDERR_BYTES)

    @unittest.skipUnless(os.sys.platform == "darwin", "public CLI guard is checked on macOS")
    def test_public_cli_refuses_before_reading_stdin_on_macos(self) -> None:
        self.assertTrue(PYTHON_PATH.is_file())
        payload = _frame("start", "raise RuntimeError('MUST_NOT_EXECUTE')\n")
        with tempfile.TemporaryDirectory(prefix="vm_worker_cli_guard_") as temporary:
            completed = subprocess.run(
                [str(PYTHON_PATH), "-I", "-S", "-B", "-u", str(WORKER_PATH)],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temporary,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                timeout=5,
                check=False,
            )
        self.assertEqual(completed.returncode, 78)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"vm_strategy_worker is only enabled on Linux\n")
        self.assertNotIn(b"MUST_NOT_EXECUTE", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
