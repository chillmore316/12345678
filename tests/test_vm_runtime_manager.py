"""Synthetic tests for the trusted research VM prepare/recovery manager.

These tests use only an owned temporary directory, an in-memory fake Limactl
transport, and a fake trusted-probe session.  They never inspect or start Lima.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_center import vm_runtime_manager as manager
from research_center import vm_strategy_session as sdk


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


def _image_inspect(runtime: sdk.VMRuntimeSpec) -> bytes:
    env = [
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
    ]
    image_labels = {
        "org.opencontainers.image.created": "2026-10-27T00:23:49.223Z",
        "org.opencontainers.image.description": "An extremely fast Python package and project manager, written in Rust.",
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.revision": "329bf9448afd9f9088d14a4d57fbc61eb454f117",
        "org.opencontainers.image.source": "https://github.com/astral-sh/uv",
        "org.opencontainers.image.title": "uv",
        "org.opencontainers.image.url": "https://github.com/astral-sh/uv",
        "org.opencontainers.image.version": "0.13.6-python3.11-trixie-slim",
        "org.quant.worker.protocol": "ndjson-v1",
        "org.quant.worker.sha256": runtime.worker_sha256,
    }
    return json.dumps(
        [{
            "Id": f"sha256:{runtime.expected_image_config_sha256}",
            "Os": "linux",
            "Architecture": "arm64",
            "RepoDigests": [runtime.immutable_image_ref],
            "RepoTags": ["quant-strategy-worker:phase12"],
            "Config": {
                "User": "65532:65532",
                "Entrypoint": ["python", "-I", "-B", "-u", "/opt/quant/worker.py"],
                "Cmd": None,
                "WorkingDir": "/tmp",
                "Env": env,
                "Labels": image_labels,
            },
        }],
        separators=(",", ":"),
    ).encode("ascii")


def _container_inspect(runtime: sdk.VMRuntimeSpec, name: str, token: str) -> bytes:
    image = json.loads(_image_inspect(runtime))[0]
    env = image["Config"]["Env"]
    container_id = "a" * 64
    hostname = container_id[:12]
    state_root = manager._GUEST_HOME + "/.local/share/nerdctl/1935db59"
    state_dir = f"{state_root}/containers/default/{container_id}"
    mount = {
        "Type": "tmpfs",
        "Source": "tmpfs",
        "Destination": "/tmp",
        "Mode": "rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "RW": True,
        "Propagation": "",
    }
    labels = dict(image["Config"]["Labels"])
    labels.update({
        "containerd.io/restart.explicitly-stopped": "false",
        "io.containerd.image.config.stop-signal": "SIGTERM",
        "nerdctl/auto-remove": "true",
        "nerdctl/dns": '{"DNSServers":null,"DNSResolvConfOptions":null,"DNSSearchDomains":null}',
        "nerdctl/extraHosts": "[]",
        "nerdctl/host-config": '{"BlkioWeight":0,"CidFile":"","Devices":null}',
        "nerdctl/hostname": hostname,
        "nerdctl/image-digest": runtime.immutable_image_ref.rsplit("@", 1)[1],
        "nerdctl/ipc": '{"mode":"private"}',
        "nerdctl/log-config": '{"driver":"","address":""}',
        "nerdctl/log-uri": "none",
        "nerdctl/mounts.0": json.dumps(mount, separators=(",", ":")),
        "nerdctl/name": name,
        "nerdctl/namespace": "default",
        "nerdctl/networks": '["none"]',
        "nerdctl/platform": "linux/arm64/v8",
        "nerdctl/state-dir": state_dir,
        "nerdctl/user": "65532:65532",
        "org.quant.strategy-session.owner": token,
    })
    value = {
        "Id": container_id,
        "Created": "2026-11-03T03:15:17.880657609Z",
        "Path": "python",
        "Args": ["-I", "-B", "-c", "import time; time.sleep(190)"],
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Pid": 18784,
            "ExitCode": 0,
            "Error": "",
            "StartedAt": "2026-11-03T03:15:18.049120443Z",
            "FinishedAt": "",
        },
        "Image": runtime.immutable_image_ref.rsplit("@", 1)[1],
        "ResolvConfPath": f"{state_dir}/resolv.conf",
        "HostnamePath": f"{state_dir}/hostname",
        "HostsPath": f"{state_root}/etchosts/default/{container_id}/hosts",
        "LogPath": "",
        "Name": name,
        "RestartCount": 0,
        "Driver": "overlayfs",
        "Platform": "linux",
        "AppArmorProfile": "",
        "Config": {
            "Hostname": hostname,
            "Image": "docker.io/library/quant-strategy-worker:phase12",
            "User": "65532:65532",
            "AttachStdin": False,
            "Env": [*env, f"HOSTNAME={hostname}"],
            "Labels": labels,
        },
        "HostConfig": {
            "ContainerIDFile": "",
            "LogConfig": {"driver": "", "address": ""},
            "NetworkMode": "none",
            "PortBindings": {},
            "RestartPolicy": {"Name": "", "MaximumRetryCount": 0},
            "AutoRemove": True,
            "CapAdd": [],
            "CapDrop": [
                "CAP_SETFCAP", "CAP_SYS_CHROOT", "CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP",
                "CAP_NET_BIND_SERVICE", "CAP_KILL", "CAP_AUDIT_WRITE", "CAP_CHOWN",
                "CAP_DAC_OVERRIDE", "CAP_FSETID", "CAP_FOWNER", "CAP_MKNOD", "CAP_NET_RAW",
            ],
            "CgroupnsMode": "private",
            "Dns": None,
            "DnsOptions": None,
            "DnsSearch": None,
            "ExtraHosts": [],
            "GroupAdd": ["65532"],
            "IpcMode": "private",
            "OomScoreAdj": 0,
            "PidMode": "",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=1777"},
            "UTSMode": "",
            "ShmSize": 0,
            "Sysctls": {"net.ipv4.ip_unprivileged_port_start": "0"},
            "Runtime": "io.containerd.runc.v2",
            "CpusetMems": "",
            "CpusetCpus": "",
            "CpuQuota": 100000,
            "CpuShares": 0,
            "CpuPeriod": 100000,
            "CpuRealtimePeriod": 0,
            "CpuRealtimeRuntime": 0,
            "Memory": 256 * 1024 * 1024,
            "MemorySwap": 256 * 1024 * 1024,
            "MemoryReservation": 0,
            "MemorySwappiness": None,
            "PidsLimit": 32,
            "Ulimits": [{"Name": "nofile", "Soft": 64, "Hard": 64}],
            "OomKillDisable": False,
            "Devices": None,
            "BlkioWeight": 0,
            "BlkioWeightDevice": [],
            "BlkioDeviceReadBps": [],
            "BlkioDeviceWriteBps": [],
            "BlkioDeviceReadIOps": [],
            "BlkioDeviceWriteIOps": [],
        },
        "Mounts": [mount],
        "NetworkSettings": {
            "Ports": {},
            "GlobalIPv6Address": "",
            "GlobalIPv6PrefixLen": 0,
            "IPAddress": "",
            "IPPrefixLen": 0,
            "MacAddress": "",
            "Networks": {},
        },
    }
    return json.dumps(value, separators=(",", ":")).encode("ascii")


class _FakeResult:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeTransport:
    def __init__(
        self,
        root: Path,
        runtime: sdk.VMRuntimeSpec,
        *,
        state: str = "Running",
        config: dict[str, object] | None = None,
        buildkit_state: str = "active",
        owner_names: list[str] | None = None,
        wrong_container_owner: bool = False,
        rm_error: bool = False,
        missing_instance: bool = False,
        inspect_payload: bytes | None = None,
    ) -> None:
        self.root = root
        self.runtime = runtime
        self.state = state
        self.config = config or _safe_lima_config()
        self.buildkit_state = buildkit_state
        self.owner_names = list(owner_names or [])
        self.wrong_container_owner = wrong_container_owner
        self.rm_error = rm_error
        self.missing_instance = missing_instance
        self.inspect_payload = inspect_payload
        self.container_present = bool(self.owner_names)
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.network_down = False
        self.buildkit_active = True
        self.started = False
        self.removed_names: list[str] = []
        self._inspect_name = self.owner_names[0] if len(self.owner_names) == 1 else ""

    def run(self, argv: list[str], **kwargs: object) -> _FakeResult:
        command = list(argv)
        self.calls.append(command)
        self.timeouts.append(float(kwargs["timeout"]))
        if kwargs["lima_home"] != self.root or kwargs["cwd"] != self.root:
            raise AssertionError("manager must pin child cwd and LIMA_HOME to the test-owned root")
        if command == [manager.LIMACTL_PATH, "--version"]:
            return _FakeResult(0, b"limactl version 2.2.1\n")
        if command == [manager.LIMACTL_PATH, "list", "--json", sdk.DEFAULT_INSTANCE]:
            if self.missing_instance:
                return _FakeResult(0, b"[]")
            status = self.state
            item = {
                "name": sdk.DEFAULT_INSTANCE,
                "status": status,
                "vmType": "vz",
                "dir": str(self.root / sdk.DEFAULT_INSTANCE),
                "LimaHome": str(self.root),
                "sshConfigFile": str(self.root / sdk.DEFAULT_INSTANCE / "ssh.config"),
                "IdentityFile": str(self.root / "_config" / "user"),
                "HostOS": "darwin",
                "HostArch": "aarch64",
                "sshLocalPort": 52344 if status == "Running" else 0,
                "sshAddress": "127.0.0.1" if status == "Running" else "",
                "config": self.config,
            }
            return _FakeResult(0, json.dumps([item], separators=(",", ":")).encode())
        if command == [manager.LIMACTL_PATH, "start", sdk.DEFAULT_INSTANCE, "--tty=false"]:
            self.started = True
            self.state = "Running"
            return _FakeResult(0)
        prefix = [manager.LIMACTL_PATH, "shell", "--workdir", "/", sdk.DEFAULT_INSTANCE]
        if command[:5] != prefix:
            raise AssertionError(f"unexpected command prefix: {command!r}")
        guest = command[5:]
        if guest == ["sudo", "-n", "ip", "link", "set", "dev", "eth0", "down"]:
            self.network_down = True
            return _FakeResult(0)
        if guest[:4] == ["python3", "-I", "-B", "-c"] and guest[4] == sdk._GUEST_NIC_PROBE:
            if not self.network_down:
                return _FakeResult(0, b'{"interfaces":[{"name":"eth0","operstate":"up"}],"default_route":true}\n')
            return _FakeResult(0, b'{"interfaces":[{"name":"eth0","operstate":"down"}],"default_route":false}\n')
        if guest == ["id", "-un"]:
            return _FakeResult(0, (manager._GUEST_USER + "\n").encode())
        if guest[:5] == ["sudo", "-n", "python3", "-I", "-B"] and guest[5:7] == ["-c", manager._GUEST_DROPIN_PROBE]:
            return _FakeResult(0, b"valid\n")
        if guest[:5] == ["systemctl", "--user", "show", "--property=DropInPaths", "--value"]:
            return _FakeResult(0, (manager._OFFLINE_DROPIN_PATH + "\n").encode())
        if guest[:5] == ["systemctl", "--user", "show", "--property=Environment", "--value"]:
            return _FakeResult(
                0,
                b"CONTAINERD_ROOTLESS_ROOTLESSKIT_NET=none CONTAINERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=none\n",
            )
        if guest[:5] == ["systemctl", "--user", "show", "--property=ActiveState", "--value"]:
            if guest[5] == "containerd.service":
                return _FakeResult(0, b"active\n")
            if guest[5] == "default-buildkit.service":
                return _FakeResult(0, (self.buildkit_state + "\n").encode("ascii"))
        if guest == ["systemctl", "--user", "stop", "default-buildkit.service"]:
            if self.buildkit_state == "active":
                self.buildkit_state = "inactive"
            return _FakeResult(0)
        if guest == ["systemctl", "--user", "reset-failed", "default-buildkit.service"]:
            if self.buildkit_state == "failed":
                self.buildkit_state = "inactive"
            return _FakeResult(0)
        if guest[:3] == ["nerdctl", "image", "inspect"]:
            return _FakeResult(0, _image_inspect(self.runtime))
        if guest[:3] == ["nerdctl", "ps", "--all"]:
            if len(self.owner_names) == 0:
                return _FakeResult(0, b"")
            return _FakeResult(0, ("\n".join(self.owner_names) + "\n").encode("ascii"))
        if guest[:3] == ["nerdctl", "container", "inspect"]:
            if len(guest) >= 6 and guest[3:5] == ["--format", "{{json .}}"]:
                name = guest[5]
                if self.container_present and name == self._inspect_name:
                    if self.inspect_payload is not None:
                        return _FakeResult(0, self.inspect_payload)
                    token = "0" * 32 if self.wrong_container_owner else self._lease_token()
                    return _FakeResult(0, _container_inspect(self.runtime, name, token))
                return _FakeResult(1, b"", b"no such container\n")
            if len(guest) >= 6 and guest[3:5] == ["--format", "{{json .Name}}"]:
                return _FakeResult(1, b"", b"no such container\n")
        if guest[:3] == ["nerdctl", "rm", "--force"]:
            name = guest[3]
            if self.rm_error:
                return _FakeResult(1, b"", b"synthetic failure\n")
            self.removed_names.append(name)
            self.owner_names = []
            self.container_present = False
            return _FakeResult(0, (name + "\n").encode())
        raise AssertionError(f"unexpected synthetic guest command: {guest!r}")

    def set_lease_token(self, token: str) -> None:
        self._token = token

    def _lease_token(self) -> str:
        return getattr(self, "_token", "f" * 32)


class _FakeSession:
    def __init__(self, *, response_ok: bool = True, close_ok: bool = True) -> None:
        self.response_ok = response_ok
        self.close_ok = close_ok
        self.source: bytes | None = None
        self.runtime: sdk.VMRuntimeSpec | None = None
        self.timeout: float | None = None
        self.exchanged = False
        self.closed = False
        self.lock_available = False

    def __call__(self, source: bytes, *, runtime: sdk.VMRuntimeSpec, session_timeout: float) -> "_FakeSession":
        self.source = source
        self.runtime = runtime
        self.timeout = session_timeout
        lock_fd = os.open(Path(runtime.lima_home) / ".strategy-session.lock", os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_available = True
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except BlockingIOError:
            self.lock_available = False
        finally:
            os.close(lock_fd)
        return self

    def exchange(self, payload: object) -> dict[str, bool]:
        if payload != {}:
            raise AssertionError("only the empty trusted probe payload is allowed")
        self.exchanged = True
        if not self.response_ok:
            return {"runtime_probe": False}
        return {"runtime_probe": True}

    def close(self) -> None:
        self.closed = True
        if not self.close_ok:
            raise RuntimeError("synthetic close failure")


class RuntimeManagerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.instance_dir = self.home / sdk.DEFAULT_INSTANCE
        self.instance_dir.mkdir()
        yaml = _safe_yaml()
        (self.instance_dir / "lima.yaml").write_bytes(yaml)
        self.runtime = sdk.VMRuntimeSpec(
            expected_instance_yaml_sha256=hashlib.sha256(yaml).hexdigest(),
            expected_resolved_config_sha256=sdk._resolved_config_sha256(_safe_lima_config()),
            immutable_image_ref=manager._EXPECTED_RUNTIME["immutable_image_ref"],
            worker_sha256=manager._EXPECTED_RUNTIME["worker_sha256"],
            expected_image_config_sha256=manager._EXPECTED_RUNTIME["expected_image_config_sha256"],
            lima_home=self.home,
            instance=sdk.DEFAULT_INSTANCE,
        )
        self.fake_session = _FakeSession()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_manager(self, fake: _FakeTransport | None = None, *, session: _FakeSession | None = None) -> dict[str, object]:
        fake = fake or _FakeTransport(self.home, self.runtime)
        session = session or self.fake_session
        return manager._prepare_runtime(
            self.runtime,
            transport=fake,
            sdk=sdk,
            session_factory=session,
            platform="darwin",
        )

    def test_stopped_instance_starts_then_is_locked_down_before_probe(self) -> None:
        fake = _FakeTransport(self.home, self.runtime, state="Stopped")
        result = self.run_manager(fake)
        self.assertTrue(result["prepared"])
        self.assertEqual(result["mode"], "RESEARCH_ONLY")
        self.assertIs(result["paper_admission"], False)
        self.assertEqual(result["status"], "started_and_prepared")
        self.assertEqual(fake.calls[0], [manager.LIMACTL_PATH, "--version"])
        start_index = fake.calls.index([manager.LIMACTL_PATH, "start", "strategy", "--tty=false"])
        first_shell_index = next(i for i, call in enumerate(fake.calls) if call[1:2] == ["shell"])
        self.assertLess(start_index, first_shell_index)
        self.assertLessEqual(fake.timeouts[start_index], manager._MAX_BOOT_SECONDS)
        self.assertEqual(
            fake.calls[first_shell_index][5:],
            ["sudo", "-n", "ip", "link", "set", "dev", "eth0", "down"],
        )
        self.assertEqual(fake.calls.count([manager.LIMACTL_PATH, "start", "strategy", "--tty=false"]), 1)
        self.assertTrue(fake.network_down)
        self.assertFalse(any("create" in call or "pull" in call or "build" in call for call in fake.calls))
        self.assertEqual(self.fake_session.source, manager._TRUSTED_PROBE_SOURCE)
        self.assertEqual(self.fake_session.timeout, manager._MAX_PROBE_SECONDS)
        self.assertTrue(self.fake_session.lock_available)
        self.assertTrue(self.fake_session.exchanged and self.fake_session.closed)
        self.assertTrue(all(timeout <= manager._MAX_OPERATION_SECONDS for timeout in fake.timeouts))

    def test_running_instance_is_not_recreated_or_started(self) -> None:
        fake = _FakeTransport(self.home, self.runtime, state="Running")
        result = self.run_manager(fake)
        self.assertTrue(result["prepared"])
        self.assertEqual(result["status"], "prepared")
        self.assertFalse(fake.started)
        self.assertFalse(any("start" in call for call in fake.calls))
        self.assertFalse(any("create" in call or "pull" in call for call in fake.calls))

    def test_failed_buildkit_unit_is_reset_and_rechecked_after_stop(self) -> None:
        fake = _FakeTransport(self.home, self.runtime, buildkit_state="failed")
        result = self.run_manager(fake)
        self.assertTrue(result["prepared"])
        commands = [call[5:] for call in fake.calls if call[1:2] == ["shell"]]
        stop = commands.index(["systemctl", "--user", "stop", "default-buildkit.service"])
        reset = commands.index(["systemctl", "--user", "reset-failed", "default-buildkit.service"])
        active_state_queries = [
            index for index, command in enumerate(commands)
            if command == [
                "systemctl", "--user", "show", "--property=ActiveState", "--value",
                "default-buildkit.service",
            ]
        ]
        self.assertLess(stop, active_state_queries[0])
        self.assertLess(active_state_queries[0], reset)
        self.assertLess(reset, active_state_queries[1])
        self.assertEqual(fake.buildkit_state, "inactive")

    def test_missing_and_unexpected_instance_states_are_refused(self) -> None:
        missing = _FakeTransport(self.home, self.runtime, state="Stopped", missing_instance=True)
        result = self.run_manager(missing)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "instance_missing")
        self.assertFalse(missing.started)
        self.assertFalse(any(call[1:2] == ["shell"] for call in missing.calls))

        unknown = _FakeTransport(self.home, self.runtime, state="Suspended")
        result = self.run_manager(unknown)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "instance_state_unknown")
        self.assertFalse(unknown.started)
        self.assertFalse(any(call[1:2] == ["shell"] for call in unknown.calls))

    def test_yaml_override_and_resolved_config_mismatch_refuse_before_guest_or_start(self) -> None:
        for case in ("yaml", "override", "resolved"):
            with self.subTest(case=case):
                # Each subcase gets a fresh root to avoid carrying an override
                # or modified config into another validation path.
                with tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary).resolve()
                    instance = home / "strategy"
                    instance.mkdir()
                    yaml = _safe_yaml()
                    (instance / "lima.yaml").write_bytes(yaml)
                    runtime = sdk.VMRuntimeSpec(
                        expected_instance_yaml_sha256=hashlib.sha256(yaml).hexdigest(),
                        expected_resolved_config_sha256=sdk._resolved_config_sha256(_safe_lima_config()),
                        immutable_image_ref=self.runtime.immutable_image_ref,
                        worker_sha256=self.runtime.worker_sha256,
                        expected_image_config_sha256=self.runtime.expected_image_config_sha256,
                        lima_home=home,
                        instance="strategy",
                    )
                    fake = _FakeTransport(home, runtime, state="Stopped")
                    harness = object.__new__(RuntimeManagerHarness)
                    harness.instance_dir = instance
                    if case == "yaml":
                        (instance / "lima.yaml").write_bytes(_safe_yaml() + b"# drift\n")
                    elif case == "override":
                        override = home / "_config" / "override.yaml"
                        override.parent.mkdir()
                        override.write_text("ignored: true\n", encoding="utf-8")
                    else:
                        fake.config.update({"networks": [{"lima": "shared"}]})
                    result = manager._prepare_runtime(
                        runtime,
                        transport=fake,
                        sdk=sdk,
                        session_factory=_FakeSession(),
                        platform="darwin",
                    )
                    self.assertFalse(result["prepared"])
                    self.assertFalse(fake.started)
                    self.assertFalse(any(call[1:2] == ["shell"] for call in fake.calls))

    def test_busy_runtime_flock_prevents_commands_and_marker_changes(self) -> None:
        lock_path = self.home / ".strategy-session.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake = _FakeTransport(self.home, self.runtime)
        try:
            result = self.run_manager(fake)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "runtime_busy")
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.home / ".strategy-session-quarantine").exists())

    def test_marker_without_owner_token_and_malformed_marker_fail_closed(self) -> None:
        marker = self.home / ".strategy-session-quarantine"
        marker.write_bytes(b"cleanup_unverified\n")
        os.chmod(marker, 0o600)
        fake = _FakeTransport(self.home, self.runtime)
        result = self.run_manager(fake)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "runtime_lease_unrecoverable")
        self.assertTrue(marker.exists())
        self.assertEqual(fake.calls, [])

        marker.write_bytes(b"active:not-a-valid-token\n")
        fake = _FakeTransport(self.home, self.runtime)
        result = self.run_manager(fake)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "runtime_lease_invalid")
        self.assertTrue(marker.exists())
        self.assertEqual(fake.calls, [])

    def test_active_owner_no_match_keeps_quarantine_for_uncertain_create(self) -> None:
        token = "a" * 32
        marker = self.home / ".strategy-session-quarantine"
        marker.write_bytes(f"active:{token}\n".encode())
        os.chmod(marker, 0o600)
        fake = _FakeTransport(self.home, self.runtime, owner_names=[])
        fake.set_lease_token(token)
        result = self.run_manager(fake)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "recovery_owner_not_found")
        self.assertTrue(marker.exists())
        owner_query = next(call for call in fake.calls if call[5:7] == ["nerdctl", "ps"])
        self.assertEqual(
            owner_query[5:],
            [
                "nerdctl", "ps", "--all", "--filter",
                f"label=org.quant.strategy-session.owner={token}",
                "--format", "{{.Names}}",
            ],
        )
        self.assertEqual(fake.removed_names, [])
        self.assertFalse(any("list" in call for call in fake.calls if "nerdctl" in call))

    def test_single_verified_owned_container_is_removed_and_only_its_marker_cleared(self) -> None:
        token = "b" * 32
        name = "qs-0123456789abcdef01234567"
        marker = self.home / ".strategy-session-quarantine"
        marker.write_bytes(f"active:{token}\n".encode())
        os.chmod(marker, 0o600)
        fake = _FakeTransport(self.home, self.runtime, owner_names=[name])
        fake.set_lease_token(token)
        result = self.run_manager(fake)
        self.assertTrue(result["prepared"])
        self.assertEqual(result["status"], "recovered_and_prepared")
        self.assertTrue(result["recovered_owned_job"])
        self.assertEqual(fake.removed_names, [name])
        self.assertFalse(marker.exists())
        self.assertTrue(any(call[5:] == ["nerdctl", "rm", "--force", name] for call in fake.calls))
        self.assertTrue(any(call[5:7] == ["nerdctl", "container"] for call in fake.calls))
        self.assertFalse(any(call[5:7] == ["nerdctl", "build"] for call in fake.calls))

    def test_multiple_owner_matches_and_mismatched_inspect_never_remove_or_clear(self) -> None:
        token = "c" * 32
        marker = self.home / ".strategy-session-quarantine"
        marker.write_bytes(f"active:{token}\n".encode())
        os.chmod(marker, 0o600)
        multiple = _FakeTransport(
            self.home,
            self.runtime,
            owner_names=["qs-0123456789abcdef01234567", "qs-1123456789abcdef01234567"],
        )
        multiple.set_lease_token(token)
        result = self.run_manager(multiple)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "recovery_owner_ambiguous")
        self.assertEqual(multiple.removed_names, [])
        self.assertTrue(marker.exists())

        single = _FakeTransport(self.home, self.runtime, owner_names=["qs-0123456789abcdef01234567"], wrong_container_owner=True)
        single.set_lease_token(token)
        result = self.run_manager(single)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "recovery_container_unverified")
        self.assertEqual(single.removed_names, [])
        self.assertTrue(marker.exists())

    def test_wrong_image_or_host_attachment_fails_inspection_before_removal(self) -> None:
        token = "e" * 32
        name = "qs-0123456789abcdef01234567"
        marker = self.home / ".strategy-session-quarantine"
        for mutation in ("image", "image_ref", "bind", "memory", "mount", "network", "guest_home", "command"):
            with self.subTest(mutation=mutation):
                marker.write_bytes(f"active:{token}\n".encode())
                os.chmod(marker, 0o600)
                inspect = json.loads(_container_inspect(self.runtime, name, token))
                if mutation == "image":
                    inspect["Image"] = "sha256:" + "0" * 64
                elif mutation == "image_ref":
                    inspect["Config"]["Image"] = "docker.io/library/untrusted:latest"
                elif mutation == "bind":
                    inspect["HostConfig"]["Binds"] = ["/home:/guest"]
                elif mutation == "memory":
                    inspect["HostConfig"]["Memory"] += 1
                elif mutation == "mount":
                    inspect["Mounts"].append({
                        "Type": "bind", "Source": manager._GUEST_HOME, "Destination": "/guest",
                        "Mode": "rw", "RW": True, "Propagation": "rprivate",
                    })
                elif mutation == "guest_home":
                    inspect["Config"]["Labels"]["nerdctl/state-dir"] = "/home/other.guest/.local/share/nerdctl/1935db59/containers/default/" + inspect["Id"]
                elif mutation == "network":
                    inspect["NetworkSettings"]["Networks"]["default"] = {"IPAddress": "10.0.0.2"}
                else:
                    inspect["Args"][-1] = "import os; os.system('id')"
                fake = _FakeTransport(
                    self.home,
                    self.runtime,
                    owner_names=[name],
                    inspect_payload=json.dumps(inspect, separators=(",", ":")).encode("ascii"),
                )
                fake.set_lease_token(token)
                result = self.run_manager(fake)
                self.assertFalse(result["prepared"])
                self.assertEqual(result["error_code"], "recovery_container_unverified")
                self.assertEqual(fake.removed_names, [])
                self.assertTrue(marker.exists())

    def test_remove_failure_and_invalid_probe_keep_prepared_false(self) -> None:
        token = "d" * 32
        name = "qs-0123456789abcdef01234567"
        marker = self.home / ".strategy-session-quarantine"
        marker.write_bytes(f"active:{token}\n".encode())
        os.chmod(marker, 0o600)
        fake = _FakeTransport(self.home, self.runtime, owner_names=[name], rm_error=True)
        fake.set_lease_token(token)
        result = self.run_manager(fake)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "recovery_cleanup_failed")
        self.assertTrue(marker.exists())

        marker.unlink()
        session = _FakeSession(response_ok=False)
        result = self.run_manager(_FakeTransport(self.home, self.runtime), session=session)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "runtime_probe_failed")
        self.assertTrue(session.closed)

        session = _FakeSession(close_ok=False)
        result = self.run_manager(_FakeTransport(self.home, self.runtime), session=session)
        self.assertFalse(result["prepared"])
        self.assertEqual(result["error_code"], "runtime_probe_close_failed")

    def test_no_public_runtime_or_source_override_parameters(self) -> None:
        self.assertEqual(tuple(__import__("inspect").signature(manager.prepare_runtime).parameters), ())
        with patch("research_center.vm_runtime_manager.sys.platform", "linux"):
            result = manager.prepare_runtime()
        self.assertFalse(result["prepared"])
        self.assertIs(result["paper_admission"], False)
        self.assertEqual(result["error_code"], "unsupported_platform")

    def test_subprocess_environment_excludes_proxy_and_ssh_agent_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/trusted-home",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HTTP_PROXY": "secret-proxy",
                "HTTPS_PROXY": "secret-proxy",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
            },
            clear=True,
        ):
            env = sdk._SubprocessTransport._child_env(self.home)
        self.assertEqual(
            env,
            {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": "/tmp/trusted-home",
                "LIMA_HOME": str(self.home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
