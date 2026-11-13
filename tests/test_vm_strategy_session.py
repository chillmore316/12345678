"""Synthetic host-controller tests; never launch Lima or access real data."""

from __future__ import annotations

import errno
import builtins
import contextlib
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from research_center import vm_strategy_session as session_module
from research_center.vm_strategy_session import (
    DEFAULT_INSTANCE,
    MAX_SOURCE_BYTES,
    OWNER_LABEL,
    VMRuntimeSpec,
    VMStrategyError,
    VMStrategySession,
    VMStrategyUnavailable,
    _CommandResult,
    _CONTAINER_PROBE,
    _GUEST_NIC_PROBE,
    _IMAGE_REF_RE,
    _SubprocessTransport,
    _VMStrategySessionCore,
    _canonical_resolved_config_bytes,
    _image_inspect_argument,
    _parse_image_inspect,
    _parse_lima_instance,
    _parse_worker_response,
    _ProtocolFailure,
    _resolved_config_sha256,
    _strict_json_bytes,
    _validate_lima_config,
    _validate_container_probe,
    _validate_guest_probe,
)


WORKER_SHA = "a" * 64
INDEX_SHA = "b" * 64
CONFIG_SHA = "c" * 64
IMAGE_REF = f"quant-strategy-worker@sha256:{INDEX_SHA}"


def _safe_yaml() -> bytes:
    return (
        "vmType: vz\n"
        "arch: aarch64\n"
        "mounts: []\n"
        "additionalDisks: []\n"
        "ssh:\n"
        "  loadDotSSHPubKeys: false\n"
        "  forwardAgent: false\n"
        "  forwardX11: false\n"
        "  forwardX11Trusted: false\n"
        "  overVsock: true\n"
        "networks: []\n"
        "portForwards:\n"
        "  - guestIP: 0.0.0.0\n"
        "    guestIPMustBeZero: false\n"
        "    guestPortRange: [1, 65535]\n"
        "    proto: any\n"
        "    ignore: true\n"
        "propagateProxyEnv: false\n"
        "env: {}\n"
        "hostResolver:\n"
        "  enabled: false\n"
    ).encode("ascii")


def _guest_probe(*, state: str = "down", default_route: bool = False) -> bytes:
    return json.dumps(
        {
            "interfaces": [{"name": "eth0", "operstate": state}],
            "default_route": default_route,
        },
        separators=(",", ":"),
    ).encode("ascii") + b"\n"


def _container_probe(**changes: object) -> bytes:
    value: dict[str, object] = {
        "memory_max": "268435456",
        "swap_max": "0",
        "cpu_max": "100000 100000",
        "pids_max": "32",
        "uid": 65532,
        "euid": 65532,
        "no_new_privs": 1,
        "cap_eff": "0000000000000000",
        "root_readonly": True,
        "interfaces": ["lo"],
        "vsock_errno": errno.EPERM,
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _image_inspect(*, repo_digests: list[str] | None = None, labels: dict[str, str] | None = None) -> bytes:
    return json.dumps(
        [
            {
                "Id": f"sha256:{CONFIG_SHA}",
                "Os": "linux",
                "Architecture": "arm64",
                "RepoDigests": repo_digests if repo_digests is not None else [IMAGE_REF],
                "Config": {
                    "User": "65532:65532",
                    "Entrypoint": ["python", "-I", "-B", "-u", "/opt/quant/worker.py"],
                    "Cmd": None,
                    "WorkingDir": "/tmp",
                    "Env": [
                        "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "LANG=C.UTF-8",
                        "GPG_KEY=synthetic-public-key-id",
                        "PYTHON_VERSION=3.11.17",
                        "PYTHON_SHA256=synthetic-image-hash",
                        "UV_TOOL_BIN_DIR=/usr/local/bin",
                        "OPENBLAS_NUM_THREADS=1",
                        "OMP_NUM_THREADS=1",
                        "MKL_NUM_THREADS=1",
                        "NUMEXPR_MAX_THREADS=1",
                    ],
                    "Labels": labels if labels is not None else {"org.quant.worker.sha256": WORKER_SHA}
                },
            }
        ],
        separators=(",", ":"),
    ).encode("ascii")


def _safe_lima_config() -> dict[str, object]:
    return {
        "vmType": "vz",
        "mountInotify": False,
        "mounts": None,
        "additionalDisks": [],
        "ssh": {
            "localPort": 0,
            "loadDotSSHPubKeys": False,
            "forwardAgent": False,
            "forwardX11": False,
            "forwardX11Trusted": False,
            "overVsock": True,
        },
        "portForwards": [{
            "guestIPMustBeZero": False,
            "guestIP": "0.0.0.0",
            "guestPortRange": [1, 65535],
            "hostIP": "127.0.0.1",
            "hostPortRange": [1, 65535],
            "proto": "any",
            "ignore": True,
        }],
        "propagateProxyEnv": False,
        "env": {},
        "hostResolver": {"enabled": False, "ipv6": False},
        "networks": [],
    }


_FAKE_WORKER = r"""
import json, os, sys, time
marker, mode = sys.argv[1], sys.argv[2]
initial = json.loads(sys.stdin.buffer.readline())
with open(marker, 'wb') as handle:
    handle.write(b'initial_frame_received')
def emit(value):
    sys.stdout.buffer.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
    sys.stdout.buffer.flush()
if mode == 'overflow_startup':
    sys.stdout.buffer.write(b'X' * 65537)
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == 'multi_startup':
    ready = {'request_id': initial['request_id'], 'result': {'ready': True}}
    emit(ready)
    emit(ready)
    time.sleep(30)
elif mode == 'wrong_nonce':
    emit({'request_id': 'wrong-nonce', 'result': {'ready': True}})
    time.sleep(30)
elif mode == 'ready_number':
    emit({'request_id': initial['request_id'], 'result': {'ready': 1}})
    time.sleep(30)
else:
    emit({'request_id': initial['request_id'], 'result': {'ready': True}})
    for line in sys.stdin.buffer:
        frame = json.loads(line)
        if mode == 'stderr_reply_burst':
            response = {'request_id': frame['request_id'], 'result': frame['payload']}
            os.write(2, b'E' * 8193)
            os.write(1, json.dumps(response, separators=(',', ':')).encode() + b'\n')
            time.sleep(30)
            continue
        if mode == 'late_stderr_after_reply':
            emit({'request_id': frame['request_id'], 'result': frame['payload']})
            time.sleep(0.1)
            os.write(2, b'L' * 8193)
            with open(marker + '.late_stderr', 'wb') as handle:
                handle.write(b'late_stderr_written')
            time.sleep(30)
            continue
        if mode == 'stderr_flood':
            sys.stderr.buffer.write(b'E' * 8193)
            sys.stderr.buffer.flush()
            time.sleep(30)
        if mode == 'hang_callback':
            with open(marker + '.callback', 'wb') as handle:
                handle.write(b'callback_received')
            time.sleep(30)
        emit({'request_id': frame['request_id'], 'result': frame['payload']})
"""


class _FakeTransport:
    def __init__(
        self,
        root: Path,
        *,
        guest: bytes | None = None,
        container: bytes | None = None,
        worker_mode: str = "echo",
        wrong_owner: bool = False,
        oversized_utility: bool = False,
        readiness_false_polls: int = 1,
        start_error: bool = False,
        create_mode: str = "ok",
    ) -> None:
        self.root = root
        self.guest = guest or _guest_probe()
        self.container = container or _container_probe()
        self.worker_mode = worker_mode
        self.wrong_owner = wrong_owner
        self.oversized_utility = oversized_utility
        self.readiness_false_polls = readiness_false_polls
        self.readiness_polls = 0
        self.start_error = start_error
        self.create_mode = create_mode
        self.calls: list[list[str]] = []
        self.command_timeouts: list[float] = []
        self.binary_lookups: list[str] = []
        self.command_envs: list[dict[str, str]] = []
        self.container_command: list[str] | None = None
        self.start_command: list[str] | None = None
        self.worker_exec_command: list[str] | None = None
        self.worker: subprocess.Popen[bytes] | None = None
        self.container_exists = False
        self.container_running = False
        self.container_attached = False
        self.container_removed = False
        self.owner_value = ""
        self.container_name = ""
        self.event_path = root / "initial-frame-received"
        self.exec_probe_saw_no_source = False

    def run(self, argv: list[str], **kwargs: object) -> _CommandResult:
        command = list(argv)
        self.calls.append(command)
        timeout = kwargs.get("timeout")
        assert type(timeout) in (int, float)
        self.command_timeouts.append(float(timeout))
        self.command_envs.append(_SubprocessTransport._child_env(self.root))
        guest_nerdctl_prefix = [
            "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE, "nerdctl"
        ]
        if "nerdctl" in command:
            assert command[:len(guest_nerdctl_prefix)] == guest_nerdctl_prefix
        if self.oversized_utility and "list" in command:
            return _CommandResult(0, b"x" * (session_module.MAX_UTILITY_STDOUT + 1), b"")
        if len(command) >= 3 and command[1:3] == ["list", "--json"]:
            instance_dir = self.root / DEFAULT_INSTANCE
            data = {
                "name": DEFAULT_INSTANCE,
                "status": "Running",
                "vmType": "vz",
                "dir": str(instance_dir),
                "LimaHome": str(self.root),
                "sshConfigFile": str(instance_dir / "ssh.config"),
                "IdentityFile": str(self.root / "_config" / "user"),
                "sshLocalPort": 57919,
                "sshAddress": "127.0.0.1",
                "HostOS": "darwin",
                "HostArch": "aarch64",
                "config": _safe_lima_config(),
            }
            return _CommandResult(0, json.dumps(data).encode("utf-8"), b"")
        if command[1:3] == ["shell", DEFAULT_INSTANCE] and command[3] == "python3":
            return _CommandResult(0, self.guest, b"")
        guest_args = command[len(guest_nerdctl_prefix):] if command[:6] == guest_nerdctl_prefix else []
        if guest_args[:2] == ["image", "inspect"]:
            return _CommandResult(0, _image_inspect(), b"")
        if guest_args[:1] == ["create"]:
            self.container_command = command
            self.container_exists = self.create_mode not in {
                "ambiguous_absent", "ambiguous_exception"
            }
            self.container_running = False
            self.container_removed = False
            label_index = guest_args.index("--label") + 1
            self.owner_value = guest_args[label_index].split("=", 1)[1]
            self.container_name = guest_args[guest_args.index("--name") + 1]
            if self.create_mode == "ambiguous_exception":
                raise OSError("synthetic lost create acknowledgement")
            if self.create_mode.startswith("ambiguous"):
                return _CommandResult(1, b"", b"synthetic ambiguous create result\n")
            if self.create_mode == "malformed_id":
                return _CommandResult(0, b"not-a-container-id\n", b"")
            return _CommandResult(0, b"e" * 64 + b"\n", b"")
        if guest_args[:2] == ["container", "inspect"]:
            format_index = guest_args.index("--format") + 1
            if guest_args[format_index] == "{{json .State.Running}}":
                self.readiness_polls += 1
                if not self.container_exists or self.container_removed:
                    return _CommandResult(1, b"", b"no such container\n")
                if self.readiness_polls <= self.readiness_false_polls:
                    return _CommandResult(0, b"false\n", b"")
                return _CommandResult(
                    0, (b"true\n" if self.container_running else b"false\n"), b""
                )
            if not self.container_exists or self.container_removed:
                return _CommandResult(1, b"", b"no such container\n")
            owner = "not-owned" if self.wrong_owner and self.container_attached else self.owner_value
            labels = {OWNER_LABEL: owner}
            return _CommandResult(0, json.dumps(labels).encode("ascii"), b"")
        if guest_args[:1] == ["exec"]:
            self.exec_probe_saw_no_source = not self.event_path.exists()
            return _CommandResult(0, self.container, b"")
        if guest_args[:1] == ["start"]:
            self.start_command = command
            if self.start_error:
                return _CommandResult(1, b"", b"synthetic start failure\n")
            self.container_running = True
            return _CommandResult(0, b"", b"")
        if guest_args[:2] == ["rm", "--force"]:
            self.container_removed = True
            self.container_exists = False
            return _CommandResult(0, b"", b"")
        raise AssertionError(f"unexpected synthetic utility command: {command[:8]!r}")

    def start(self, argv: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        self.worker_exec_command = list(argv)
        expected_prefix = [
            "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE,
            "nerdctl", "exec", "--interactive", "--user", "65532:65532",
        ]
        assert argv[:len(expected_prefix)] == expected_prefix
        assert self.container_exists
        assert self.readiness_polls > 0
        assert self.exec_probe_saw_no_source
        assert argv[len(expected_prefix)] == self.container_name
        assert argv[len(expected_prefix) + 1:] == [
            "python", "-I", "-B", "-u", "/opt/quant/worker.py"
        ]
        self.container_attached = True
        safe_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "LANG": "C.UTF-8",
            "LIMA_HOME": str(self.root),
        }
        self.command_envs.append(dict(safe_env))
        self.worker = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", _FAKE_WORKER, str(self.event_path), self.worker_mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root,
            env=safe_env,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
            shell=False,
        )
        assert self.worker.stdin is not None
        assert self.worker.stdout is not None
        assert self.worker.stderr is not None
        for stream in (self.worker.stdin, self.worker.stdout, self.worker.stderr):
            os.set_blocking(stream.fileno(), False)
        return self.worker


class _UninspectableSource:
    def __getattribute__(self, _name: str) -> object:
        raise AssertionError("constructor inspected source before platform/runtime gate")


class PublicGateTests(unittest.TestCase):
    def test_missing_runtime_rejects_before_touching_source(self) -> None:
        with self.assertRaises(VMStrategyUnavailable) as raised:
            VMStrategySession(_UninspectableSource(), runtime=None)  # type: ignore[arg-type]
        self.assertEqual(raised.exception.code, "runtime_not_verified")

    def test_non_darwin_rejects_before_touching_source(self) -> None:
        runtime = VMRuntimeSpec(
            expected_instance_yaml_sha256="d" * 64,
            expected_resolved_config_sha256=_resolved_config_sha256(_safe_lima_config()),
            immutable_image_ref=IMAGE_REF,
            worker_sha256=WORKER_SHA,
        )
        with patch.object(session_module.sys, "platform", "linux"):
            with self.assertRaises(VMStrategyUnavailable) as raised:
                VMStrategySession(_UninspectableSource(), runtime=runtime)  # type: ignore[arg-type]
        self.assertEqual(raised.exception.code, "unsupported_platform")

    def test_runtime_spec_requires_fixed_instance_digest_shapes(self) -> None:
        with self.assertRaises(ValueError):
            VMRuntimeSpec(
                expected_instance_yaml_sha256="d" * 64,
                expected_resolved_config_sha256="e" * 64,
                immutable_image_ref="registry/worker:latest",
                worker_sha256=WORKER_SHA,
            )
        self.assertTrue(_IMAGE_REF_RE.fullmatch(f"sha256:{INDEX_SHA}"))
        self.assertFalse(_IMAGE_REF_RE.fullmatch("registry/worker:latest"))


class RuntimeProbeTests(unittest.TestCase):
    def test_resolved_config_accepts_only_offline_host_attachment_policy(self) -> None:
        _validate_lima_config(_safe_lima_config())
        failures = (
            {"mounts": [{"location": "/Users/example"}]},
            {"additionalDisks": ["host-disk"]},
            {"ssh": {**_safe_lima_config()["ssh"], "overVsock": False}},
            {"ssh": {**_safe_lima_config()["ssh"], "loadDotSSHPubKeys": True}},
            {"ssh": {**_safe_lima_config()["ssh"], "forwardAgent": True}},
            {"ssh": {**_safe_lima_config()["ssh"], "localPort": True}},
            {"ssh": {**_safe_lima_config()["ssh"], "forwardX11": True}},
            {"propagateProxyEnv": True},
            {"env": {"API_TOKEN": "synthetic-only"}},
            {"hostResolver": {"enabled": True, "ipv6": False}},
            {"networks": [{"lima": "shared"}]},
            {"portForwards": [{**_safe_lima_config()["portForwards"][0], "ignore": False}]},
            {"portForwards": _safe_lima_config()["portForwards"] * 2},
            {"hostAgent": {"socket": "/tmp/host.sock"}},
        )
        for changes in failures:
            config = {**_safe_lima_config(), **changes}
            with self.subTest(changes=changes), self.assertRaises(VMStrategyUnavailable):
                _validate_lima_config(config)

    def test_lima_list_requires_single_running_vz_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance_dir = root / DEFAULT_INSTANCE
            instance_dir.mkdir()
            good = json.dumps([{
                "name": DEFAULT_INSTANCE,
                "status": "Running",
                "vmType": "vz",
                "dir": str(instance_dir),
                "LimaHome": str(root),
                "sshConfigFile": str(instance_dir / "ssh.config"),
                "IdentityFile": str(root / "_config" / "user"),
                "sshLocalPort": 57919,
                "sshAddress": "127.0.0.1",
                "HostOS": "darwin",
                "HostArch": "aarch64",
                "config": _safe_lima_config(),
            }]).encode()
            expected_config_hash = _resolved_config_sha256(_safe_lima_config())
            _parse_lima_instance(good, DEFAULT_INSTANCE, instance_dir, root, expected_config_hash)
            for bad in (
                json.dumps([{"name": DEFAULT_INSTANCE, "status": "Stopped", "vmType": "vz"}]).encode(),
                json.dumps([{"name": "other", "status": "Running", "vmType": "vz"}]).encode(),
                json.dumps([
                    {"name": DEFAULT_INSTANCE, "status": "Running", "vmType": "vz"},
                    {"name": DEFAULT_INSTANCE, "status": "Running", "vmType": "vz"},
                ]).encode(),
            ):
                with self.subTest(raw=bad), self.assertRaises(VMStrategyUnavailable):
                    _parse_lima_instance(
                        bad, DEFAULT_INSTANCE, instance_dir, root, expected_config_hash
                    )

    def test_resolved_config_digest_rejects_unknown_host_socket_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            instance_dir = root / DEFAULT_INSTANCE
            instance_dir.mkdir()
            base_config = _safe_lima_config()
            expected_hash = _resolved_config_sha256(base_config)
            changed_config = {**base_config, "hostSocket": "/tmp/synthetic.sock"}
            entry = {
                "name": DEFAULT_INSTANCE,
                "status": "Running",
                "vmType": "vz",
                "dir": str(instance_dir),
                "LimaHome": str(root),
                "sshConfigFile": str(instance_dir / "ssh.config"),
                "IdentityFile": str(root / "_config" / "user"),
                "sshLocalPort": 57919,
                "sshAddress": "127.0.0.1",
                "HostOS": "darwin",
                "HostArch": "aarch64",
                "config": changed_config,
            }
            with self.assertRaises(VMStrategyUnavailable) as raised:
                _parse_lima_instance(
                    json.dumps(entry).encode(), DEFAULT_INSTANCE, instance_dir, root, expected_hash
                )
            self.assertEqual(raised.exception.code, "lima_config_hash_mismatch")

    def test_rehashed_resolved_config_still_rejects_unknown_host_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            instance_dir = root / DEFAULT_INSTANCE
            instance_dir.mkdir()
            changed_config = {**_safe_lima_config(), "hostSocket": "/tmp/synthetic.sock"}
            entry = {
                "name": DEFAULT_INSTANCE,
                "status": "Running",
                "vmType": "vz",
                "dir": str(instance_dir),
                "LimaHome": str(root),
                "sshConfigFile": str(instance_dir / "ssh.config"),
                "IdentityFile": str(root / "_config" / "user"),
                "sshLocalPort": 57919,
                "sshAddress": "127.0.0.1",
                "HostOS": "darwin",
                "HostArch": "aarch64",
                "config": changed_config,
            }
            with self.assertRaises(VMStrategyUnavailable) as raised:
                _parse_lima_instance(
                    json.dumps(entry).encode(),
                    DEFAULT_INSTANCE,
                    instance_dir,
                    root,
                    _resolved_config_sha256(changed_config),
                )
            self.assertEqual(
                raised.exception.code, "lima_config_unrecognized_security_field"
            )

    def test_resolved_config_hash_uses_sorted_compact_utf8_json(self) -> None:
        self.assertEqual(
            _canonical_resolved_config_bytes({"z": 1, "a": "离线"}),
            _canonical_resolved_config_bytes({"a": "离线", "z": 1}),
        )
        self.assertEqual(
            _canonical_resolved_config_bytes({"z": 1, "a": "离线"}),
            '{"a":"离线","z":1}'.encode("utf-8"),
        )

    def test_guest_network_probe_rejects_up_interface_or_default_route(self) -> None:
        result = _validate_guest_probe(_guest_probe())
        self.assertEqual(result.interfaces, (("eth0", "down"),))
        with self.assertRaises(VMStrategyUnavailable):
            _validate_guest_probe(_guest_probe(state="up"))
        with self.assertRaises(VMStrategyUnavailable):
            _validate_guest_probe(_guest_probe(state="unknown"))
        with self.assertRaises(VMStrategyUnavailable):
            _validate_guest_probe(_guest_probe(default_route=True))
        self.assertIn("/proc/net/ipv6_route", _GUEST_NIC_PROBE)
        self.assertIn("interface != 'lo'", _GUEST_NIC_PROBE)
        self.assertIn("fields[8]", _GUEST_NIC_PROBE)
        self.assertIn("fields[9]", _GUEST_NIC_PROBE)
        self.assertIn("0x200", _GUEST_NIC_PROBE)

    def test_container_probe_checks_resource_privilege_network_and_vsock(self) -> None:
        result = _validate_container_probe(_container_probe())
        self.assertEqual(result.interfaces, ("lo",))
        invalid = (
            {"memory_max": "268435457"},
            {"swap_max": "max"},
            {"cpu_max": "max 100000"},
            {"cpu_max": "50000 100000"},
            {"pids_max": "33"},
            {"uid": 0, "euid": 0},
            {"no_new_privs": 0},
            {"cap_eff": "0000000000000001"},
            {"root_readonly": False},
            {"interfaces": ["eth0", "lo"]},
            {"vsock_errno": 0},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(VMStrategyUnavailable):
                _validate_container_probe(_container_probe(**fields))

    def test_exact_trusted_container_probe_executes_with_fake_os_and_socket(self) -> None:
        real_stat = importlib.import_module("stat")
        self.assertFalse(hasattr(real_stat, "ST_RDONLY"))
        values = {
            "/proc/self/status": "NoNewPrivs:\t1\nCapEff:\t0000000000000000\n",
            "/sys/fs/cgroup/memory.max": "268435456\n",
            "/sys/fs/cgroup/memory.swap.max": "0\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
            "/sys/fs/cgroup/pids.max": "32\n",
        }

        def fake_open(path: str, *args: object, **kwargs: object) -> io.StringIO:
            if path not in values:
                raise AssertionError("probe attempted an unapproved synthetic path")
            return io.StringIO(values[path])

        fake_os = types.SimpleNamespace(
            ST_RDONLY=1,
            statvfs=lambda _path: types.SimpleNamespace(f_flag=1),
            getuid=lambda: 65532,
            geteuid=lambda: 65532,
            listdir=lambda _path: ["lo"],
        )

        def denied_vsock(*_args: object) -> object:
            raise PermissionError(errno.EPERM, "synthetic denied")

        fake_socket = types.SimpleNamespace(AF_VSOCK=40, SOCK_STREAM=1, socket=denied_vsock)
        modules = {
            "errno": errno,
            "json": json,
            "os": fake_os,
            "socket": fake_socket,
            "stat": real_stat,
        }
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name in modules:
                return modules[name]
            return real_import(name, *args, **kwargs)

        controlled_builtins = dict(vars(builtins))
        controlled_builtins["open"] = fake_open
        controlled_builtins["__import__"] = fake_import
        namespace: dict[str, object] = {"__builtins__": controlled_builtins}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exec(compile(_CONTAINER_PROBE, "<trusted-container-probe-test>", "exec"), namespace)
        output = json.loads(captured.getvalue())
        self.assertTrue(output["root_readonly"])
        self.assertEqual(output["vsock_errno"], errno.EPERM)
        self.assertEqual(output["interfaces"], ["lo"])
        self.assertTrue(_validate_container_probe(captured.getvalue().encode("utf-8")))

    def test_image_identity_separates_index_digest_from_config_digest(self) -> None:
        self.assertEqual(_image_inspect_argument(IMAGE_REF), INDEX_SHA)
        _parse_image_inspect(_image_inspect(), IMAGE_REF, WORKER_SHA)
        with self.assertRaises(VMStrategyUnavailable):
            _parse_image_inspect(
                _image_inspect(repo_digests=[]), IMAGE_REF, WORKER_SHA
            )
        _parse_image_inspect(
            _image_inspect(repo_digests=[]), IMAGE_REF, WORKER_SHA, CONFIG_SHA
        )
        with self.assertRaises(VMStrategyUnavailable):
            _parse_image_inspect(
                _image_inspect(labels={"org.quant.worker.sha256": "0" * 64}),
                IMAGE_REF,
                WORKER_SHA,
            )
        with self.assertRaises(VMStrategyUnavailable):
            _parse_image_inspect(_image_inspect(), IMAGE_REF, WORKER_SHA, "0" * 64)
        bad_entrypoint = json.loads(_image_inspect())
        bad_entrypoint[0]["Config"]["Entrypoint"] = ["sh", "-c", "python /opt/quant/worker.py"]
        with self.assertRaises(VMStrategyUnavailable):
            _parse_image_inspect(json.dumps(bad_entrypoint).encode(), IMAGE_REF, WORKER_SHA)

    def test_worker_response_requires_exact_nonce_shape_and_strict_json(self) -> None:
        self.assertEqual(
            _parse_worker_response(b'{"request_id":"n","result":{"ready":true}}\n', "n"),
            (True, {"ready": True}),
        )
        bad = (
            b'{"request_id":"other","result":null}\n',
            b'{"request_id":"n","result":null,"error":"callback_failed"}\n',
            b'{"request_id":"n","error":"private traceback"}\n',
            b'{"request_id":"n","result":NaN}\n',
            b'{"request_id":"n","result":1e999}\n',
            b'{"request_id":"n","request_id":"n","result":null}\n',
            b'{"request_id":"n","result":null,"extra":1}\n',
            b'{"request_id":"n","result":null}\n{"request_id":"n","result":null}\n',
        )
        for line in bad:
            with self.subTest(line=line[:72]), self.assertRaises(VMStrategyError):
                _parse_worker_response(line, "n")

    def test_boolean_scalar_frame_exact_byte_boundaries(self) -> None:
        for value, expected in ((True, b"true\n"), (False, b"false\n")):
            with self.subTest(value=value):
                self.assertEqual(_strict_json_bytes(value, maximum=len(expected)), expected)
                with self.assertRaises(_ProtocolFailure):
                    _strict_json_bytes(value, maximum=len(expected) - 1)

    def test_host_request_encoder_enforces_json_nodes_depth_finiteness_and_frame_cap(self) -> None:
        bad_payloads = (
            {"value": float("inf")},
            {"value": "\ud800"},
            {"value": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[None]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]},
            {"value": [0] * 10_000},
            {"value": "x" * (128 * 1024)},
        )
        for payload in bad_payloads:
            with self.subTest(payload_type=type(payload["value"]).__name__), self.assertRaises(VMStrategyError):
                _strict_json_bytes({"request_id": "n", "payload": payload}, maximum=128 * 1024)

    def test_oversized_scalars_are_rejected_before_json_encoder_or_validator(self) -> None:
        payloads = (
            {"value": "x" * (1024 * 1024)},
            {"value": ["y" * 40_000] * 4},
            {"value": 1 << 1_000_000},
        )
        with (
            patch.object(
                session_module, "_validate_json_value",
                side_effect=AssertionError("legacy validator must not process request values"),
            ),
            patch.object(
                session_module.json.JSONEncoder, "iterencode",
                side_effect=AssertionError("oversized values must be rejected before encoding"),
            ),
        ):
            for payload in payloads:
                with self.subTest(value_type=type(payload["value"]).__name__):
                    with self.assertRaises(_ProtocolFailure) as raised:
                        _strict_json_bytes(
                            {"request_id": "n", "payload": payload}, maximum=128 * 1024
                        )
                    self.assertEqual(raised.exception.code, "frame_too_large")

    def test_bounded_encoder_accepts_exact_multibyte_and_escaped_frame_boundary(self) -> None:
        value = {"request_id": "n", "payload": {"text": "交易/\\\"\n😀"}}
        expected = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        self.assertEqual(_strict_json_bytes(value, maximum=len(expected)), expected)
        with self.assertRaises(_ProtocolFailure) as raised:
            _strict_json_bytes(value, maximum=len(expected) - 1)
        self.assertEqual(raised.exception.code, "frame_too_large")


class _ControllerHarness(unittest.TestCase):
    def make_runtime(self, root: Path) -> VMRuntimeSpec:
        root = root.resolve()
        instance = root / DEFAULT_INSTANCE
        instance.mkdir(parents=True)
        raw_yaml = _safe_yaml()
        (instance / "lima.yaml").write_bytes(raw_yaml)
        return VMRuntimeSpec(
            expected_instance_yaml_sha256=hashlib.sha256(raw_yaml).hexdigest(),
            expected_resolved_config_sha256=_resolved_config_sha256(_safe_lima_config()),
            immutable_image_ref=IMAGE_REF,
            worker_sha256=WORKER_SHA,
            lima_home=root,
            instance=DEFAULT_INSTANCE,
        )

    def make_core(
        self,
        root: Path,
        *,
        source: object = b"# SYNTHETIC_SOURCE_MUST_NOT_EXECUTE\n",
        transport: _FakeTransport | None = None,
        session_timeout: float = 30,
    ) -> tuple[_VMStrategySessionCore, _FakeTransport]:
        root = root.resolve()
        runtime = self.make_runtime(root)
        fake = transport or _FakeTransport(root)
        fake.root = root
        fake.event_path = root / "initial-frame-received"
        def which(name: str, path: str | None = None) -> str:
            fake.binary_lookups.append(name)
            return f"/fake/bin/{name}"

        lookup = patch.object(session_module.shutil, "which", side_effect=which)
        lookup.start()
        self.addCleanup(lookup.stop)
        core = _VMStrategySessionCore(
            source,
            runtime,
            transport=fake,
            platform="darwin",
            session_timeout=session_timeout,
        )
        self.addCleanup(lambda: core.close() if not core._closed else None)
        return core, fake


class ControllerLifecycleTests(_ControllerHarness):
    def test_preflight_utility_timeouts_follow_session_and_cleanup_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core, fake = self.make_core(root, session_timeout=1.0)
            preflight_timeouts = list(fake.command_timeouts)
            self.assertTrue(preflight_timeouts)
            self.assertTrue(all(0 < timeout <= 1.0 for timeout in preflight_timeouts))
            core.close()
            cleanup_timeouts = fake.command_timeouts[len(preflight_timeouts):]
            self.assertEqual(
                cleanup_timeouts,
                [session_module.CLEANUP_UTILITY_TIMEOUT] * 3,
            )

    def test_preflight_precedes_source_and_persistent_exchange_reaps_owned_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _FakeTransport(root)
            core, fake = self.make_core(
                root,
                source=b"raise RuntimeError('HOST_OR_WORKER_MUST_NOT_EXECUTE_TEST_SOURCE')\n",
                transport=fake,
            )
            self.assertTrue(fake.exec_probe_saw_no_source)
            self.assertTrue(fake.event_path.exists())
            self.assertNotEqual(core._startup_request_id, "")
            self.assertEqual(core.exchange({"n": 1}), {"n": 1})
            self.assertEqual(core.exchange(["two", 2]), ["two", 2])
            command = fake.container_command
            self.assertIsNotNone(command)
            assert command is not None
            self.assertEqual(command[:7], [
                "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE, "nerdctl", "create"
            ])
            self.assertIn("--network", command)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--log-driver", command)
            self.assertEqual(command[command.index("--log-driver") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertEqual(command[command.index("--user") + 1], "65532:65532")
            self.assertEqual(command[command.index(INDEX_SHA) + 1:], [
                "-I", "-B", "-c", "import time; time.sleep(190)"
            ])
            self.assertEqual(command[command.index("--entrypoint") + 1], "python")
            self.assertEqual(fake.readiness_polls, 2)
            self.assertEqual(fake.start_command[:7], [
                "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE,
                "nerdctl", "start",
            ])
            self.assertEqual(fake.start_command[-1], fake.container_name)
            self.assertNotIn("--attach", fake.start_command)
            self.assertEqual(fake.worker_exec_command[:10], [
                "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE,
                "nerdctl", "exec", "--interactive", "--user", "65532:65532",
            ])
            self.assertFalse(any("run" in recorded for recorded in [*fake.calls, command]))
            self.assertEqual(fake.binary_lookups, ["limactl"])
            guest_prefix = [
                "/fake/bin/limactl", "shell", "--workdir", "/", DEFAULT_INSTANCE, "nerdctl"
            ]
            for recorded in [*fake.calls, command, fake.start_command, fake.worker_exec_command]:
                if recorded is None:
                    continue
                if "nerdctl" in recorded:
                    self.assertEqual(recorded[:6], guest_prefix)
            self.assertNotIn("nerdctl", fake.binary_lookups)
            self.assertFalse(any(item in {"-v", "--volume", "--mount", "--env", "-e"} for item in command))
            self.assertFalse(any("HOST_OR_WORKER_MUST_NOT_EXECUTE" in item for item in command))
            core.close()
            self.assertIsNotNone(fake.worker)
            assert fake.worker is not None
            self.assertIsNotNone(fake.worker.poll())
            self.assertTrue(fake.container_removed)
            self.assertFalse((root / ".strategy-session-quarantine").exists())

    def test_created_container_is_removed_when_synchronous_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake = _FakeTransport(root, start_error=True)
            with self.assertRaises(VMStrategyUnavailable) as raised:
                self.make_core(root, transport=fake)
            self.assertEqual(raised.exception.code, "container_start_failed")
            self.assertIsNotNone(fake.container_command)
            self.assertIsNotNone(fake.start_command)
            self.assertIsNone(fake.worker_exec_command)
            self.assertTrue(fake.container_removed)
            self.assertFalse(fake.event_path.exists())
            self.assertFalse((root / ".strategy-session-quarantine").exists())
            self.assertFalse(any("exec" in command for command in fake.calls))

    def test_uncertain_create_quarantines_even_when_immediate_inspect_is_absent(self) -> None:
        for mode, expected in (
            ("ambiguous_absent", "container_create_failed"),
            ("ambiguous_exception", "runtime_command_failed"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                fake = _FakeTransport(root, create_mode=mode)
                with self.subTest(mode=mode), self.assertRaises(VMStrategyUnavailable) as raised:
                    self.make_core(root, transport=fake)
                self.assertEqual(raised.exception.code, expected)
                self.assertIsNotNone(fake.container_command)
                self.assertFalse(fake.container_exists)
                self.assertFalse(fake.event_path.exists())
                self.assertFalse(any("exec" in command for command in fake.calls))
                self.assertTrue((root / ".strategy-session-quarantine").is_file())

    def test_profile_has_no_inherited_credentials_or_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _FakeTransport(root)
            with patch.dict(
                os.environ,
                {
                    "HOME": "/synthetic/home",
                    "PATH": "/synthetic/path",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "HTTP_PROXY": "synthetic-proxy-value",
                    "HTTPS_PROXY": "synthetic-proxy-value",
                    "SSH_AUTH_SOCK": "/synthetic/agent.sock",
                    "API_TOKEN": "synthetic-token",
                },
                clear=True,
            ):
                env = _SubprocessTransport._child_env(root)
            self.assertEqual(env["HOME"], "/synthetic/home")
            self.assertEqual(env["LIMA_HOME"], str(root))
            self.assertEqual(env["PATH"], session_module._SAFE_PATH)
            self.assertEqual(set(env), {"PATH", "HOME", "LIMA_HOME", "LANG", "LC_ALL"})
            core, fake = self.make_core(root, transport=fake)
            for recorded in fake.command_envs:
                self.assertNotIn("HTTP_PROXY", recorded)
                self.assertNotIn("HTTPS_PROXY", recorded)
                self.assertNotIn("SSH_AUTH_SOCK", recorded)
                self.assertNotIn("API_TOKEN", recorded)
            core.close()

    def test_guest_network_failure_stops_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _FakeTransport(root, guest=_guest_probe(state="up"))
            with self.assertRaises(VMStrategyUnavailable) as raised:
                self.make_core(root, transport=fake)
            self.assertEqual(raised.exception.code, "guest_network_not_offline")
            self.assertIsNone(fake.worker)
            self.assertFalse(fake.event_path.exists())

    def test_live_container_probe_failure_never_sends_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _FakeTransport(root, container=_container_probe(vsock_errno=0))
            with self.assertRaises(VMStrategyUnavailable) as raised:
                self.make_core(root, transport=fake)
            self.assertEqual(raised.exception.code, "container_controls_unverified")
            self.assertTrue(fake.exec_probe_saw_no_source)
            self.assertFalse(fake.event_path.exists())
            self.assertTrue(fake.container_removed)
            self.assertFalse((root / ".strategy-session-quarantine").exists())

    def test_source_encoding_and_size_are_checked_after_probes_before_delivery(self) -> None:
        for source, expected in ((b"\xff", "source_encoding_invalid"),
                                 (b"#" * (MAX_SOURCE_BYTES + 1), "source_too_large"),
                                 (b"\x00" * 22000, "source_frame_too_large")):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = _FakeTransport(root)
                with self.subTest(expected=expected), self.assertRaises(VMStrategyUnavailable) as raised:
                    self.make_core(root, source=source, transport=fake)
                self.assertEqual(raised.exception.code, expected)
                self.assertTrue(fake.exec_probe_saw_no_source)
                self.assertFalse(fake.event_path.exists())
                self.assertTrue(fake.container_removed)

    def test_yaml_hash_and_override_metadata_fail_before_any_runtime_command(self) -> None:
        for case in ("hash", "override"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                runtime = self.make_runtime(root)
                if case == "hash":
                    (root / DEFAULT_INSTANCE / "lima.yaml").write_bytes(_safe_yaml() + b"# drift\n")
                else:
                    override_dir = root / "_config"
                    override_dir.mkdir()
                    (override_dir / "override.yaml").write_text("metadata only\n", encoding="utf-8")
                fake = _FakeTransport(root)
                with patch.object(session_module.shutil, "which", side_effect=lambda name, path=None: f"/fake/bin/{name}"):
                    with self.subTest(case=case), self.assertRaises(VMStrategyUnavailable) as raised:
                        _VMStrategySessionCore(
                            b"# never sent", runtime, transport=fake, platform="darwin", session_timeout=30,
                        )
                self.assertEqual(
                    raised.exception.code,
                    "instance_config_hash_mismatch" if case == "hash" else "override_file_present",
                )
                self.assertEqual(fake.calls, [])
                self.assertIsNone(fake.worker)

    def test_runtime_flock_blocks_second_controller_until_first_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core, fake = self.make_core(root)
            blocked = _FakeTransport(root)
            with self.assertRaises(VMStrategyUnavailable) as raised:
                _VMStrategySessionCore(
                    b"# not sent", core._runtime, transport=blocked, platform="darwin", session_timeout=30,
                )
            self.assertEqual(raised.exception.code, "runtime_busy")
            self.assertEqual(blocked.calls, [])
            core.close()
            self.assertTrue(fake.container_removed)

    def test_idle_session_deadline_cleans_up_without_another_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            core, fake = self.make_core(root, session_timeout=1.0)
            time.sleep(1.1)
            self.assertTrue(core._closed)
            self.assertTrue(fake.container_removed)
            assert fake.worker is not None
            self.assertIsNotNone(fake.worker.poll())
            self.assertFalse((root / ".strategy-session-quarantine").exists())

    def test_startup_overflow_multiple_frames_and_wrong_nonce_fail_closed(self) -> None:
        for mode, expected in (
            ("overflow_startup", "response_frame_too_large"),
            ("multi_startup", "multiple_or_trailing_responses"),
            ("wrong_nonce", "response_nonce_mismatch"),
            ("ready_number", "worker_ready_invalid"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = _FakeTransport(root, worker_mode=mode)
                with self.subTest(mode=mode), self.assertRaises(VMStrategyError) as raised:
                    self.make_core(root, transport=fake)
                self.assertEqual(raised.exception.code, expected)
                self.assertTrue(fake.container_removed)
                self.assertFalse((root / ".strategy-session-quarantine").exists())

    def test_cancel_timeout_and_stderr_overflow_kill_only_owned_session(self) -> None:
        for mode, invoke, expected in (
            ("echo", lambda core: core.exchange({"x": 1}, cancelled=lambda: True), "cancelled"),
            ("echo", lambda core: core.exchange({"x": 1}, cancelled=lambda: 1), "cancel_callback_invalid"),
            ("hang_callback", lambda core: core.exchange({"x": 1}, timeout=0.05), "exchange_timeout"),
            ("stderr_flood", lambda core: core.exchange({"x": 1}, timeout=1), "worker_stderr_limit"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = _FakeTransport(root, worker_mode=mode)
                core, fake = self.make_core(root, transport=fake)
                with self.subTest(mode=mode), self.assertRaises(VMStrategyError) as raised:
                    invoke(core)
                self.assertEqual(raised.exception.code, expected)
                self.assertTrue(fake.container_removed)
                self.assertFalse((root / ".strategy-session-quarantine").exists())
                assert fake.worker is not None
                self.assertIsNotNone(fake.worker.poll())

    def test_stdout_reply_and_stderr_overflow_in_same_burst_fails_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake = _FakeTransport(root, worker_mode="stderr_reply_burst")
            core, fake = self.make_core(root, transport=fake)
            with self.assertRaises(VMStrategyUnavailable) as raised:
                core.exchange({"burst": True}, timeout=2.0)
            self.assertEqual(raised.exception.code, "worker_stderr_limit")
            self.assertTrue(fake.container_removed)
            self.assertFalse((root / ".strategy-session-quarantine").exists())
            assert fake.worker is not None
            self.assertIsNotNone(fake.worker.poll())

    def test_late_stderr_is_reported_by_final_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake = _FakeTransport(root, worker_mode="late_stderr_after_reply")
            core, fake = self.make_core(root, transport=fake)
            self.assertEqual(core.exchange({"delayed": True}), {"delayed": True})
            marker = Path(str(fake.event_path) + ".late_stderr")
            deadline = time.monotonic() + 2.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            with self.assertRaises(VMStrategyUnavailable) as raised:
                core.close()
            self.assertEqual(raised.exception.code, "worker_stderr_limit")
            self.assertTrue(fake.container_removed)
            self.assertFalse((root / ".strategy-session-quarantine").exists())
            assert fake.worker is not None
            self.assertIsNotNone(fake.worker.poll())

    def test_watchdog_close_race_returns_fixed_session_closed_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake = _FakeTransport(root, worker_mode="hang_callback")
            core, _fake = self.make_core(root, transport=fake)
            failures: list[BaseException] = []

            def exchange() -> None:
                try:
                    core.exchange({"block": True}, timeout=2.0)
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=exchange)
            thread.start()
            callback_marker = Path(str(fake.event_path) + ".callback")
            deadline = time.monotonic() + 2.0
            while not callback_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(callback_marker.exists())
            watchdog = threading.Thread(target=core._expire_session)
            watchdog.start()
            watchdog.join(timeout=2.0)
            self.assertFalse(watchdog.is_alive())
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], VMStrategyUnavailable)
            self.assertEqual(getattr(failures[0], "code", None), "session_closed")

    def test_failed_cleanup_keeps_quarantine_and_refuses_future_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _FakeTransport(root, wrong_owner=True)
            core, _fake = self.make_core(root, transport=fake)
            with self.assertRaises(VMStrategyUnavailable) as raised:
                core.close()
            self.assertEqual(raised.exception.code, "cleanup_unverified")
            marker = root / ".strategy-session-quarantine"
            self.assertTrue(marker.is_file())
            blocked_fake = _FakeTransport(root.resolve())
            with self.assertRaises(VMStrategyUnavailable) as next_raised:
                _VMStrategySessionCore(
                    b"# not sent",
                    core._runtime,
                    transport=blocked_fake,
                    platform="darwin",
                    session_timeout=30,
                )
            self.assertEqual(next_raised.exception.code, "runtime_quarantined")
            self.assertFalse(fake.container_removed)
            self.assertEqual(blocked_fake.calls, [])


class BoundedUtilityTests(unittest.TestCase):
    def test_utility_output_and_wall_limits_reap_owned_synthetic_process(self) -> None:
        runner = _SubprocessTransport()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = (
                ([sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*200)"], 0.1, 16),
                ([sys.executable, "-I", "-c", "import time;time.sleep(5)"], 0.05, 1024),
            )
            expected = ("command_output_limit", "command_timeout")
            for (argv, timeout, limit), code in zip(commands, expected):
                with self.subTest(code=code), self.assertRaises(VMStrategyUnavailable) as raised:
                    runner.run(
                        argv,
                        lima_home=root,
                        cwd=root,
                        timeout=timeout,
                        stdout_limit=limit,
                        stderr_limit=64,
                    )
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
