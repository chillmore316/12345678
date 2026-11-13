"""Explicit, fail-closed preparation for the reviewed local research VM.

This module is intentionally not an upload, backtest, account, or trading
entrypoint.  The public API accepts no runtime paths, instance names, images,
commands, source, or environment overrides.  Tests use the private runner seam
with temporary directories and synthetic command results only.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import os
import pwd
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable


MODE = "RESEARCH_ONLY"
PAPER_ADMISSION = False
DEFAULT_INSTANCE = "strategy"
DEFAULT_LIMA_HOME = Path.home() / "Library/Application Support/QuantSandbox/lima"
LIMACTL_PATH = "/opt/homebrew/bin/limactl"
LIMACTL_VERSION = "2.2.1"

# These pins are the reviewed values returned by
# research_center.custom_engine.configured_runtime().  They are repeated here
# as a guard against a changed factory or an accidental runtime substitution.
_EXPECTED_RUNTIME = {
    "expected_instance_yaml_sha256": "1d0523837d0aafaac3b06661997ea97ad4e25dd976e13fca1d5d7a189be95abd",
    "expected_resolved_config_sha256": "f00339bb1c8af41e457608772c30c6b1d751a1abb78d79651b3d9bc7f5495bf3",
    "immutable_image_ref": "quant-strategy-worker@sha256:a5972e8f6704cfdd3cd53426edec57c2624e20de842639678cdcbbce9a2c425e",
    "worker_sha256": "1b9c2dd77827d41653963ce37d5ed21fbfaaaae72647754a5c0ca12aee731fa2",
    "expected_image_config_sha256": "4eb865cdac8520ff8ed42a68531979755560180100b20674b831e4ffa674aa88",
}
_EXPECTED_SESSION_SOURCE_SHA256 = "79992b159b946f9c71e9ad35aa34cc853f51100068d573079b12e519ca9fdfd8"
_EXPECTED_CUSTOM_ENGINE_SOURCE_SHA256 = "e33d698e0eddce0fecd09fde67e0d5bb6ee671bbbc1c6f46f1fd12548763d80c"

_MAX_OPERATION_SECONDS = 120.0
_MAX_BOOT_SECONDS = 60.0
_MAX_PROBE_SECONDS = 30.0
_CLEANUP_RESERVE_SECONDS = 24.0
_COMMAND_SECONDS = 8.0
_MAX_STDOUT = 256 * 1024
_MAX_STDERR = 8 * 1024
_MAX_OWNER_QUERY_STDOUT = 64 * 1024
_MAX_LEASE_BYTES = 256
_MAX_INSPECT_BYTES = 256 * 1024
_LEASE_RE = re.compile(rb"active:([0-9a-f]{32})\n\Z")
_CONTAINER_NAME_RE = re.compile(r"qs-[0-9a-f]{24}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRUSTED_PROBE_SOURCE = (
    b"def handle(payload):\n"
    b"    return {'runtime_probe': True}\n"
)
_CONTAINER_HOLDER_SOURCE = "import time; time.sleep(190)"
_GUEST_USER = pwd.getpwuid(os.getuid()).pw_name
_GUEST_HOME = "/home/" + _GUEST_USER + ".guest"
_OFFLINE_DROPIN_PATH = _GUEST_HOME + "/.config/systemd/user/containerd.service.d/20-quant-offline.conf"
_GUEST_DROPIN_PROBE = r'''import os, pwd, stat, sys
home = __QUANT_GUEST_HOME__
relative = ".config/systemd/user/containerd.service.d/20-quant-offline.conf"
expected = b"[Service]\nEnvironment=CONTAINERD_ROOTLESS_ROOTLESSKIT_NET=none\nEnvironment=CONTAINERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=none\n"
try:
    # Lima uses the host login name and a separate guest home directory.
    account = pwd.getpwnam(__QUANT_GUEST_USER__)
    current = home
    info = os.lstat(current)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid:
        raise ValueError
    for component in relative.split("/")[:-1]:
        current = os.path.join(current, component)
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid:
            raise ValueError
        if info.st_mode & 0o022:
            raise ValueError
    path = os.path.join(home, relative)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != account.pw_uid or stat.S_IMODE(before.st_mode) != 0o644:
        raise ValueError
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or after.st_uid != account.pw_uid or after.st_ino != before.st_ino or after.st_size > 512:
            raise ValueError
        data = os.read(fd, 513)
    finally:
        os.close(fd)
    if data != expected:
        raise ValueError
except Exception:
    print("invalid")
    sys.exit(1)
print("valid")
'''.replace("__QUANT_GUEST_HOME__", repr(_GUEST_HOME)).replace("__QUANT_GUEST_USER__", repr(_GUEST_USER))


class RuntimeManagerError(RuntimeError):
    """Safe public failure carrying a fixed code and fixed Chinese summary."""

    _MESSAGES = {
        "unsupported_platform": "当前平台不支持此研究运行环境",
        "runtime_source_untrusted": "研究运行组件与已审阅版本不一致",
        "runtime_spec_untrusted": "研究运行配置与固定配置不一致",
        "runtime_home_invalid": "研究运行目录不可验证",
        "instance_dir_invalid": "研究虚拟机目录不可验证",
        "runtime_busy": "研究运行环境正被其他控制器使用",
        "runtime_quarantined": "研究运行环境处于隔离状态，未执行清理",
        "runtime_lease_invalid": "研究运行隔离标记不可验证",
        "runtime_lease_unrecoverable": "研究运行隔离标记缺少可验证的容器归属",
        "runtime_command_failed": "研究运行环境命令未通过验证",
        "runtime_command_timeout": "研究运行环境命令超时",
        "runtime_command_output_invalid": "研究运行环境命令输出不可验证",
        "limactl_version_unverified": "Lima 版本与固定版本不一致",
        "instance_missing": "指定研究虚拟机不存在",
        "instance_state_unknown": "研究虚拟机状态不可验证",
        "instance_config_unverified": "研究虚拟机配置与固定配置不一致",
        "instance_start_failed": "研究虚拟机未能安全启动",
        "guest_offline_unverified": "研究虚拟机网络隔离未通过验证",
        "guest_policy_unverified": "研究虚拟机离线服务配置未通过验证",
        "recovery_owner_not_found": "未找到隔离标记对应的容器，保留隔离状态",
        "recovery_owner_ambiguous": "隔离标记对应多个容器，保留隔离状态",
        "recovery_container_unverified": "隔离标记对应容器未通过所有权或配置验证",
        "recovery_cleanup_failed": "隔离容器未能安全清理，保留隔离状态",
        "runtime_probe_failed": "固定研究运行探针未通过",
        "runtime_probe_close_failed": "固定研究运行探针未能完成清理",
        "runtime_unavailable": "研究运行环境当前不可用",
    }

    def __init__(self, code: str) -> None:
        if code not in self._MESSAGES:
            code = "runtime_unavailable"
        self.code = code
        self.message = self._MESSAGES[code]
        super().__init__(self.message)


def _metadata(
    *, prepared: bool, status: str, error_code: str | None = None,
    recovered_owned_job: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": MODE,
        "paper_admission": PAPER_ADMISSION,
        "prepared": bool(prepared),
        "instance": DEFAULT_INSTANCE,
        "status": status,
        "verification_scope": "pinned_vm_offline_controls_and_trusted_probe",
        "recovered_owned_job": bool(recovered_owned_job),
    }
    if error_code is not None:
        result["error_code"] = error_code
    return result


def prepare_runtime() -> dict[str, Any]:
    """Prepare and verify the fixed research VM using trusted control code.

    This explicit action has no caller-controlled options.  Failure is returned
    as a small metadata object containing only a fixed error code.
    """
    if sys.platform != "darwin":
        return _metadata(prepared=False, status="unavailable", error_code="unsupported_platform")
    try:
        sdk, runtime = _load_trusted_runtime()
        return _prepare_runtime(
            runtime,
            transport=sdk._SubprocessTransport(),
            sdk=sdk,
            session_factory=sdk.VMStrategySession,
            platform=sys.platform,
        )
    except RuntimeManagerError as exc:
        return _metadata(prepared=False, status="unavailable", error_code=exc.code)
    except Exception:
        return _metadata(prepared=False, status="unavailable", error_code="runtime_unavailable")


def _load_trusted_runtime() -> tuple[Any, Any]:
    project_root = Path(__file__).resolve().parents[1]
    session_path = project_root / "research_center" / "vm_strategy_session.py"
    worker_path = project_root / "research_center" / "vm_strategy_worker.py"
    custom_engine_path = project_root / "research_center" / "custom_engine.py"
    if _hash_owned_source(session_path) != _EXPECTED_SESSION_SOURCE_SHA256:
        raise RuntimeManagerError("runtime_source_untrusted")
    if _hash_owned_source(worker_path) != _EXPECTED_RUNTIME["worker_sha256"]:
        raise RuntimeManagerError("runtime_source_untrusted")
    if _hash_owned_source(custom_engine_path) != _EXPECTED_CUSTOM_ENGINE_SOURCE_SHA256:
        raise RuntimeManagerError("runtime_source_untrusted")

    # Import only after verifying both source files.  The factory and type are
    # checked again against literal reviewed pins below.
    sdk = importlib.import_module("research_center.vm_strategy_session")
    custom_engine = importlib.import_module("research_center.custom_engine")
    runtime = custom_engine.configured_runtime()
    if type(runtime) is not sdk.VMRuntimeSpec:
        raise RuntimeManagerError("runtime_spec_untrusted")
    if (
        dict(getattr(custom_engine, "_RUNTIME_PINS", {})) != _EXPECTED_RUNTIME
        or getattr(custom_engine, "_RUNTIME_HOME", None) != DEFAULT_LIMA_HOME
        or getattr(custom_engine, "_RUNTIME_INSTANCE", None) != DEFAULT_INSTANCE
        or runtime.lima_home != DEFAULT_LIMA_HOME
        or runtime.instance != DEFAULT_INSTANCE
        or _runtime_values(runtime) != _EXPECTED_RUNTIME
    ):
        raise RuntimeManagerError("runtime_spec_untrusted")
    return sdk, runtime


def _hash_owned_source(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 4 * 1024 * 1024:
            raise RuntimeManagerError("runtime_source_untrusted")
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or info.st_size > 4 * 1024 * 1024
            ):
                raise RuntimeManagerError("runtime_source_untrusted")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, min(65536, 4 * 1024 * 1024 + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > 4 * 1024 * 1024:
                    raise RuntimeManagerError("runtime_source_untrusted")
                chunks.append(block)
            if total != info.st_size:
                raise RuntimeManagerError("runtime_source_untrusted")
            return hashlib.sha256(b"".join(chunks)).hexdigest()
        finally:
            os.close(fd)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError("runtime_source_untrusted") from exc


def _runtime_values(runtime: Any) -> dict[str, str]:
    return {
        "expected_instance_yaml_sha256": runtime.expected_instance_yaml_sha256,
        "expected_resolved_config_sha256": runtime.expected_resolved_config_sha256,
        "immutable_image_ref": runtime.immutable_image_ref,
        "worker_sha256": runtime.worker_sha256,
        "expected_image_config_sha256": runtime.expected_image_config_sha256,
    }


def _prepare_runtime(
    runtime: Any,
    *,
    transport: Any,
    sdk: Any,
    session_factory: Callable[..., Any],
    platform: str,
) -> dict[str, Any]:
    """Private, injectable implementation used only by synthetic tests."""
    if platform != "darwin":
        return _metadata(prepared=False, status="unavailable", error_code="unsupported_platform")
    try:
        _validate_runtime(runtime, sdk)
        deadline = time.monotonic() + _MAX_OPERATION_SECONDS
        lock_fd = _acquire_runtime_lock(Path(runtime.lima_home), Path(runtime.lima_home) / runtime.instance)
        recovered = False
        started = False
        try:
            home = Path(runtime.lima_home)
            instance_dir = home / runtime.instance
            marker = _read_quarantine_marker(home)
            _verify_instance_files(home, instance_dir, runtime, sdk)
            limactl = LIMACTL_PATH
            _verify_limactl_version(transport, limactl, home, deadline)
            state = _read_instance_state(transport, sdk, limactl, runtime, home, deadline)
            if state == "Stopped":
                _run_required(
                    transport,
                    [limactl, "start", runtime.instance, "--tty=false"],
                    home,
                    deadline,
                    timeout=_MAX_BOOT_SECONDS,
                    code="instance_start_failed",
                )
                started = True
                state = _read_instance_state(transport, sdk, limactl, runtime, home, deadline)
            if state != "Running":
                raise RuntimeManagerError("instance_state_unknown")

            _disable_guest_network(transport, sdk, limactl, runtime, home, deadline)
            _verify_guest_policy(transport, limactl, runtime, home, deadline)
            if marker is not None:
                token = marker
                _recover_owned_container(
                    transport, sdk, limactl, runtime, token, home, deadline
                )
                recovered = True
            _verify_guest_network_offline(transport, sdk, limactl, runtime, home, deadline)
        finally:
            _release_runtime_lock(lock_fd)

        remaining = max(0.0, deadline - time.monotonic())
        probe_timeout = min(_MAX_PROBE_SECONDS, remaining - _CLEANUP_RESERVE_SECONDS)
        if probe_timeout < 1.0:
            raise RuntimeManagerError("runtime_probe_failed")
        _run_trusted_probe(session_factory, runtime, probe_timeout)
        status = "recovered_and_prepared" if recovered else (
            "started_and_prepared" if started else "prepared"
        )
        return _metadata(
            prepared=True,
            status=status,
            recovered_owned_job=recovered,
        )
    except RuntimeManagerError as exc:
        return _metadata(prepared=False, status="unavailable", error_code=exc.code)
    except Exception:
        return _metadata(prepared=False, status="unavailable", error_code="runtime_unavailable")


def _validate_runtime(runtime: Any, sdk: Any) -> None:
    if type(runtime) is not sdk.VMRuntimeSpec:
        raise RuntimeManagerError("runtime_spec_untrusted")
    if (
        runtime.instance != DEFAULT_INSTANCE
        or not isinstance(runtime.lima_home, Path)
        or not runtime.lima_home.is_absolute()
        or not _runtime_values_well_formed(runtime)
    ):
        raise RuntimeManagerError("runtime_spec_untrusted")


def _runtime_values_well_formed(runtime: Any) -> bool:
    return (
        type(runtime.expected_instance_yaml_sha256) is str
        and _SHA256_RE.fullmatch(runtime.expected_instance_yaml_sha256) is not None
        and type(runtime.expected_resolved_config_sha256) is str
        and _SHA256_RE.fullmatch(runtime.expected_resolved_config_sha256) is not None
        and runtime.immutable_image_ref == _EXPECTED_RUNTIME["immutable_image_ref"]
        and runtime.worker_sha256 == _EXPECTED_RUNTIME["worker_sha256"]
        and runtime.expected_image_config_sha256 == _EXPECTED_RUNTIME["expected_image_config_sha256"]
    )


def _acquire_runtime_lock(home: Path, instance_dir: Path) -> int:
    _verify_private_directory(home, "runtime_home_invalid")
    _verify_directory(instance_dir, os.getuid(), "instance_dir_invalid")
    path = home / ".strategy-session.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeManagerError("runtime_home_invalid") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeManagerError("runtime_home_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeManagerError("runtime_busy") from exc
    except RuntimeManagerError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise RuntimeManagerError("runtime_home_invalid") from exc
    return fd


def _verify_private_directory(path: Path, code: str) -> None:
    try:
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or path.resolve(strict=True) != path
        ):
            raise RuntimeManagerError(code)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError(code) from exc


def _verify_directory(path: Path, owner_uid: int, code: str) -> None:
    try:
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or path.resolve(strict=True) != path
        ):
            raise RuntimeManagerError(code)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError(code) from exc


def _release_runtime_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_quarantine_marker(home: Path) -> str | None:
    path = home / ".strategy-session-quarantine"
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeManagerError("runtime_lease_invalid") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_mode & 0o077
        or before.st_size > _MAX_LEASE_BYTES
    ):
        raise RuntimeManagerError("runtime_lease_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or info.st_size > _MAX_LEASE_BYTES
            ):
                raise RuntimeManagerError("runtime_lease_invalid")
            raw = os.read(fd, _MAX_LEASE_BYTES + 1)
        finally:
            os.close(fd)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError("runtime_lease_invalid") from exc
    if len(raw) > _MAX_LEASE_BYTES:
        raise RuntimeManagerError("runtime_lease_invalid")
    match = _LEASE_RE.fullmatch(raw)
    if match is None:
        if raw == b"cleanup_unverified\n":
            raise RuntimeManagerError("runtime_lease_unrecoverable")
        raise RuntimeManagerError("runtime_lease_invalid")
    return match.group(1).decode("ascii")


def _verify_instance_files(home: Path, instance_dir: Path, runtime: Any, sdk: Any) -> None:
    try:
        sdk._reject_override_files(home, instance_dir)
        raw = _read_owned_instance_yaml(
            instance_dir / "lima.yaml", sdk.MAX_INSTANCE_YAML_BYTES
        )
    except Exception as exc:
        raise RuntimeManagerError("instance_config_unverified") from exc
    if hashlib.sha256(raw).hexdigest() != runtime.expected_instance_yaml_sha256:
        raise RuntimeManagerError("instance_config_unverified")


def _read_owned_instance_yaml(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_size > maximum
        ):
            raise RuntimeManagerError("instance_config_unverified")
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or info.st_size > maximum
            ):
                raise RuntimeManagerError("instance_config_unverified")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise RuntimeManagerError("instance_config_unverified")
                chunks.append(chunk)
            if total != info.st_size:
                raise RuntimeManagerError("instance_config_unverified")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError("instance_config_unverified") from exc


def _run_command(
    transport: Any,
    argv: list[str],
    home: Path,
    deadline: float,
    *,
    timeout: float = _COMMAND_SECONDS,
    stdout_limit: int = _MAX_STDOUT,
    stderr_limit: int = _MAX_STDERR,
) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeManagerError("runtime_command_timeout")
    bounded_timeout = min(timeout, remaining)
    try:
        result = transport.run(
            argv,
            lima_home=home,
            timeout=bounded_timeout,
            cwd=home,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "command_timeout":
            raise RuntimeManagerError("runtime_command_timeout") from exc
        raise RuntimeManagerError("runtime_command_failed") from exc
    if (
        type(getattr(result, "returncode", None)) is not int
        or type(getattr(result, "stdout", None)) is not bytes
        or type(getattr(result, "stderr", None)) is not bytes
        or len(result.stdout) > stdout_limit
        or len(result.stderr) > stderr_limit
    ):
        raise RuntimeManagerError("runtime_command_output_invalid")
    return result


def _run_required(
    transport: Any,
    argv: list[str],
    home: Path,
    deadline: float,
    *,
    timeout: float = _COMMAND_SECONDS,
    stdout_limit: int = _MAX_STDOUT,
    code: str = "runtime_command_failed",
) -> bytes:
    result = _run_command(
        transport, argv, home, deadline, timeout=timeout, stdout_limit=stdout_limit
    )
    if result.returncode != 0:
        raise RuntimeManagerError(code)
    return result.stdout


def _verify_limactl_version(transport: Any, limactl: str, home: Path, deadline: float) -> None:
    raw = _run_required(
        transport, [limactl, "--version"], home, deadline,
        code="limactl_version_unverified",
    )
    try:
        value = raw.decode("ascii", "strict").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeManagerError("limactl_version_unverified") from exc
    if value not in {f"limactl version {LIMACTL_VERSION}", LIMACTL_VERSION}:
        raise RuntimeManagerError("limactl_version_unverified")


def _read_instance_state(
    transport: Any, sdk: Any, limactl: str, runtime: Any, home: Path, deadline: float
) -> str:
    raw = _run_required(
        transport,
        [limactl, "list", "--json", runtime.instance],
        home,
        deadline,
        code="instance_config_unverified",
    )
    try:
        value = sdk._strict_json_loads(raw)
        if type(value) is list:
            if not value:
                raise RuntimeManagerError("instance_missing")
            if len(value) != 1:
                raise ValueError
            value = value[0]
        if type(value) is not dict or value.get("name") != runtime.instance:
            raise ValueError
        status = value.get("status")
        if status not in {"Running", "Stopped"}:
            raise RuntimeManagerError("instance_state_unknown")
        if value.get("vmType") != "vz":
            raise ValueError
        instance_dir = home / runtime.instance
        expected_paths = {
            "dir": str(instance_dir),
            "LimaHome": str(home),
            "sshConfigFile": str(instance_dir / "ssh.config"),
            "IdentityFile": str(home / "_config" / "user"),
        }
        for key, path in expected_paths.items():
            if type(value.get(key)) is not str or value[key] != path:
                raise ValueError
        if value.get("HostOS") != "darwin" or value.get("HostArch") not in {"aarch64", "arm64"}:
            raise ValueError
        if status == "Running":
            port = value.get("sshLocalPort")
            if type(port) is not int or not (1 <= port <= 65535) or value.get("sshAddress") != "127.0.0.1":
                raise ValueError
        config = value.get("config")
        if sdk._resolved_config_sha256(config) != runtime.expected_resolved_config_sha256:
            raise ValueError
        sdk._validate_lima_config(config)
        return status
    except RuntimeManagerError:
        raise
    except Exception as exc:
        raise RuntimeManagerError("instance_config_unverified") from exc


def _guest_base(limactl: str, instance: str) -> list[str]:
    return [limactl, "shell", "--workdir", "/", instance]


def _disable_guest_network(
    transport: Any, sdk: Any, limactl: str, runtime: Any, home: Path, deadline: float
) -> None:
    # This is the first guest command after boot.  It only disables the fixed
    # Lima guest NIC; the manager never raises or restarts guest networking.
    result = _run_command(
        transport,
        _guest_base(limactl, runtime.instance) + ["sudo", "-n", "ip", "link", "set", "dev", "eth0", "down"],
        home,
        deadline,
    )
    if result.returncode != 0:
        raise RuntimeManagerError("guest_offline_unverified")
    _verify_guest_network_offline(transport, sdk, limactl, runtime, home, deadline)


def _verify_guest_network_offline(
    transport: Any, sdk: Any, limactl: str, runtime: Any, home: Path, deadline: float
) -> None:
    raw = _run_required(
        transport,
        _guest_base(limactl, runtime.instance)
        + ["python3", "-I", "-B", "-c", sdk._GUEST_NIC_PROBE],
        home,
        deadline,
        code="guest_offline_unverified",
    )
    try:
        sdk._validate_guest_probe(raw)
    except Exception as exc:
        raise RuntimeManagerError("guest_offline_unverified") from exc


def _guest_policy_command(
    transport: Any,
    limactl: str,
    runtime: Any,
    home: Path,
    deadline: float,
    *argv: str,
    timeout: float = _COMMAND_SECONDS,
    stdout_limit: int = _MAX_STDOUT,
) -> bytes:
    return _run_required(
        transport,
        _guest_base(limactl, runtime.instance) + list(argv),
        home,
        deadline,
        timeout=timeout,
        stdout_limit=stdout_limit,
        code="guest_policy_unverified",
    )


def _verify_guest_policy(
    transport: Any, limactl: str, runtime: Any, home: Path, deadline: float
) -> None:
    identity = _guest_policy_command(transport, limactl, runtime, home, deadline, "id", "-un", stdout_limit=128)
    if identity != (_GUEST_USER + "\n").encode("utf-8"):
        raise RuntimeManagerError("guest_policy_unverified")
    result = _run_command(
        transport,
        _guest_base(limactl, runtime.instance)
        + ["sudo", "-n", "python3", "-I", "-B", "-c", _GUEST_DROPIN_PROBE],
        home,
        deadline,
        stdout_limit=128,
    )
    if result.returncode != 0 or result.stdout != b"valid\n":
        raise RuntimeManagerError("guest_policy_unverified")
    dropin_paths = _guest_policy_command(
        transport, limactl, runtime, home, deadline,
        "systemctl", "--user", "show", "--property=DropInPaths", "--value", "containerd.service",
        stdout_limit=2048,
    )
    try:
        paths = dropin_paths.decode("utf-8", "strict").strip().split()
    except UnicodeDecodeError as exc:
        raise RuntimeManagerError("guest_policy_unverified") from exc
    if _OFFLINE_DROPIN_PATH not in paths:
        raise RuntimeManagerError("guest_policy_unverified")
    environment = _guest_policy_command(
        transport, limactl, runtime, home, deadline,
        "systemctl", "--user", "show", "--property=Environment", "--value", "containerd.service",
        stdout_limit=4096,
    )
    try:
        env_items = environment.decode("utf-8", "strict").strip().split()
    except UnicodeDecodeError as exc:
        raise RuntimeManagerError("guest_policy_unverified") from exc
    if env_items.count("CONTAINERD_ROOTLESS_ROOTLESSKIT_NET=none") != 1 or env_items.count(
        "CONTAINERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=none"
    ) != 1:
        raise RuntimeManagerError("guest_policy_unverified")
    active = _guest_policy_command(
        transport, limactl, runtime, home, deadline,
        "systemctl", "--user", "show", "--property=ActiveState", "--value", "containerd.service",
        stdout_limit=128,
    )
    if active != b"active\n":
        raise RuntimeManagerError("guest_policy_unverified")
    _guest_policy_command(
        transport, limactl, runtime, home, deadline,
        "systemctl", "--user", "stop", "default-buildkit.service", stdout_limit=128,
    )
    buildkit = _guest_policy_command(
        transport, limactl, runtime, home, deadline,
        "systemctl", "--user", "show", "--property=ActiveState", "--value", "default-buildkit.service",
        stdout_limit=128,
    )
    if buildkit == b"failed\n":
        _guest_policy_command(
            transport, limactl, runtime, home, deadline,
            "systemctl", "--user", "reset-failed", "default-buildkit.service", stdout_limit=128,
        )
        buildkit = _guest_policy_command(
            transport, limactl, runtime, home, deadline,
            "systemctl", "--user", "show", "--property=ActiveState", "--value", "default-buildkit.service",
            stdout_limit=128,
        )
    if buildkit != b"inactive\n":
        raise RuntimeManagerError("guest_policy_unverified")


def _read_owner_filtered_names(
    transport: Any,
    limactl: str,
    instance: str,
    token: str,
    home: Path,
    deadline: float,
) -> list[str]:
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise RuntimeManagerError("runtime_lease_invalid")
    command = _guest_base(limactl, instance) + [
        "nerdctl", "ps", "--all", "--filter", f"label=org.quant.strategy-session.owner={token}",
        "--format", "{{.Names}}",
    ]
    result = _run_command(
        transport, command, home, deadline,
        stdout_limit=_MAX_OWNER_QUERY_STDOUT,
    )
    if result.returncode != 0:
        raise RuntimeManagerError("runtime_command_failed")
    if not result.stdout:
        return []
    try:
        text = result.stdout.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise RuntimeManagerError("runtime_command_output_invalid") from exc
    if not text.endswith("\n") or "\x00" in text:
        raise RuntimeManagerError("runtime_command_output_invalid")
    names = text[:-1].split("\n")
    if not names or any(_CONTAINER_NAME_RE.fullmatch(name) is None for name in names):
        raise RuntimeManagerError("runtime_command_output_invalid")
    return names


def _json_object(raw: bytes, sdk: Any, code: str) -> dict[str, Any]:
    try:
        value = sdk._strict_json_loads(raw)
        if type(value) is list:
            if not value:
                raise ValueError
            if len(value) != 1:
                raise ValueError
            value = value[0]
        if type(value) is not dict:
            raise ValueError
        return value
    except Exception as exc:
        raise RuntimeManagerError(code) from exc


def _recover_owned_container(
    transport: Any,
    sdk: Any,
    limactl: str,
    runtime: Any,
    token: str,
    home: Path,
    deadline: float,
) -> None:
    # Re-verify the pinned image before trusting any container metadata.
    image = _run_required(
        transport,
        _guest_base(limactl, runtime.instance)
        + ["nerdctl", "image", "inspect", sdk._image_inspect_argument(runtime.immutable_image_ref)],
        home,
        deadline,
        code="recovery_container_unverified",
    )
    try:
        sdk._parse_image_inspect(
            image,
            runtime.immutable_image_ref,
            runtime.worker_sha256,
            runtime.expected_image_config_sha256,
        )
    except Exception as exc:
        raise RuntimeManagerError("recovery_container_unverified") from exc
    image_value = _json_object(image, sdk, "recovery_container_unverified")

    names = _read_owner_filtered_names(transport, limactl, runtime.instance, token, home, deadline)
    if not names:
        # The marker does not prove that an ambiguous create request has
        # completed.  An empty filtered view is not enough to clear quarantine.
        raise RuntimeManagerError("recovery_owner_not_found")
    if len(names) != 1:
        raise RuntimeManagerError("recovery_owner_ambiguous")
    name = names[0]
    inspect = _run_command(
        transport,
        _guest_base(limactl, runtime.instance)
        + ["nerdctl", "container", "inspect", "--format", "{{json .}}", name],
        home,
        deadline,
        stdout_limit=_MAX_INSPECT_BYTES,
    )
    if inspect.returncode != 0:
        raise RuntimeManagerError("recovery_container_unverified")
    _validate_owned_container(inspect.stdout, name, token, runtime, image_value, sdk)

    removed = _run_command(
        transport,
        _guest_base(limactl, runtime.instance) + ["nerdctl", "rm", "--force", name],
        home,
        deadline,
    )
    if removed.returncode != 0:
        raise RuntimeManagerError("recovery_cleanup_failed")
    exact = _run_command(
        transport,
        _guest_base(limactl, runtime.instance)
        + ["nerdctl", "container", "inspect", "--format", "{{json .Name}}", name],
        home,
        deadline,
        stdout_limit=4096,
    )
    if exact.returncode == 0 or not sdk._VMStrategySessionCore._known_not_found(
        sdk._CommandResult(exact.returncode, exact.stdout, exact.stderr)
    ):
        raise RuntimeManagerError("recovery_cleanup_failed")
    names_after = _read_owner_filtered_names(
        transport, limactl, runtime.instance, token, home, deadline
    )
    if names_after:
        raise RuntimeManagerError("recovery_cleanup_failed")
    _clear_owned_marker(home, token)


def _validate_owned_container(
    raw: bytes,
    name: str,
    token: str,
    runtime: Any,
    image_value: dict[str, Any],
    sdk: Any,
) -> None:
    value = _json_object(raw, sdk, "recovery_container_unverified")
    expected_top_level = {
        "Id", "Created", "Path", "Args", "State", "Image", "ResolvConfPath",
        "HostnamePath", "HostsPath", "LogPath", "Name", "RestartCount", "Driver",
        "Platform", "AppArmorProfile", "HostConfig", "Mounts", "Config", "NetworkSettings",
    }
    if set(value) != expected_top_level:
        raise RuntimeManagerError("recovery_container_unverified")
    container_id = value.get("Id")
    if type(container_id) is not str or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise RuntimeManagerError("recovery_container_unverified")
    image_digest = runtime.immutable_image_ref.rsplit("@", 1)[1]
    manifest_id = image_digest if image_digest.startswith("sha256:") else f"sha256:{image_digest}"
    image_id = f"sha256:{runtime.expected_image_config_sha256}"
    image_config = image_value.get("Config")
    if type(image_config) is not dict:
        raise RuntimeManagerError("recovery_container_unverified")
    image_env = image_config.get("Env")
    image_labels = image_config.get("Labels")
    repo_digests = image_value.get("RepoDigests")
    repo_tags = image_value.get("RepoTags")
    if (
        image_value.get("Id") != image_id
        or type(image_env) is not list
        or any(type(item) is not str or "=" not in item for item in image_env)
        or type(image_labels) is not dict
        or type(repo_digests) is not list
        or any(type(item) is not str or "@" not in item for item in repo_digests)
        or not any(item.rsplit("@", 1)[-1] == image_digest for item in repo_digests)
        or type(repo_tags) is not list
        or not repo_tags
        or any(type(item) is not str or not item or "\x00" in item for item in repo_tags)
    ):
        raise RuntimeManagerError("recovery_container_unverified")
    if any(
        key == "org.quant.strategy-session.owner"
        or key.startswith(("nerdctl/", "containerd.io/"))
        for key in image_labels
    ):
        raise RuntimeManagerError("recovery_container_unverified")

    allowed_config_images = {
        runtime.immutable_image_ref,
        image_digest,
        image_digest.removeprefix("sha256:"),
        manifest_id,
    }
    for tag in repo_tags:
        allowed_config_images.add(tag)
        image_name = tag.split(":", 1)[0]
        if "/" not in image_name and "@" not in tag:
            allowed_config_images.add(f"docker.io/library/{tag}")

    config = value.get("Config")
    host = value.get("HostConfig")
    if type(config) is not dict or set(config) != {
        "Hostname", "User", "AttachStdin", "Env", "Image", "Labels"
    } or type(host) is not dict:
        raise RuntimeManagerError("recovery_container_unverified")
    hostname = container_id[:12]
    if (
        value.get("Name") != name
        or value.get("Image") != manifest_id
        or value.get("Path") != "python"
        or value.get("Args") != ["-I", "-B", "-c", _CONTAINER_HOLDER_SOURCE]
        or config.get("Hostname") != hostname
        or config.get("Image") not in allowed_config_images
        or config.get("User") != "65532:65532"
        or config.get("AttachStdin") is not False
        or config.get("Env") != image_env + [f"HOSTNAME={hostname}"]
    ):
        raise RuntimeManagerError("recovery_container_unverified")
    labels = config.get("Labels")
    if type(labels) is not dict:
        raise RuntimeManagerError("recovery_container_unverified")

    state_dir = labels.get("nerdctl/state-dir")
    state_dir_match = (
        re.fullmatch(
            r"(" + re.escape(_GUEST_HOME) + r"/\.local/share/nerdctl/[0-9a-f]{8})/containers/default/"
            + re.escape(container_id),
            state_dir,
        )
        if type(state_dir) is str
        else None
    )
    if state_dir_match is None:
        raise RuntimeManagerError("recovery_container_unverified")
    state_root = state_dir_match.group(1)
    expected_mount = {
        "Type": "tmpfs",
        "Source": "tmpfs",
        "Destination": "/tmp",
        "Mode": "rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "RW": True,
        "Propagation": "",
    }
    expected_labels = dict(image_labels)
    expected_labels.update({
        "containerd.io/restart.explicitly-stopped": "false",
        "io.containerd.image.config.stop-signal": "SIGTERM",
        "nerdctl/auto-remove": "true",
        "nerdctl/dns": '{"DNSServers":null,"DNSResolvConfOptions":null,"DNSSearchDomains":null}',
        "nerdctl/extraHosts": "[]",
        "nerdctl/host-config": '{"BlkioWeight":0,"CidFile":"","Devices":null}',
        "nerdctl/hostname": hostname,
        "nerdctl/image-digest": manifest_id,
        "nerdctl/ipc": '{"mode":"private"}',
        "nerdctl/log-config": '{"driver":"","address":""}',
        "nerdctl/log-uri": "none",
        "nerdctl/mounts.0": '{"Type":"tmpfs","Source":"tmpfs","Destination":"/tmp","Mode":"rw,noexec,nosuid,nodev,size=16m,mode=1777","RW":true,"Propagation":""}',
        "nerdctl/name": name,
        "nerdctl/namespace": "default",
        "nerdctl/networks": '["none"]',
        "nerdctl/platform": "linux/arm64/v8",
        "nerdctl/state-dir": state_dir,
        "nerdctl/user": "65532:65532",
        "org.quant.strategy-session.owner": token,
    })
    if labels != expected_labels:
        raise RuntimeManagerError("recovery_container_unverified")

    host_expected = {
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
    }
    host_cap_drop = host.get("CapDrop")
    expected_cap_drop = host_expected.pop("CapDrop")
    host_without_cap_drop = dict(host)
    host_without_cap_drop.pop("CapDrop", None)
    if (
        type(host_cap_drop) is not list
        or any(type(capability) is not str for capability in host_cap_drop)
        or len(host_cap_drop) != len(expected_cap_drop)
        or set(host_cap_drop) != set(expected_cap_drop)
        or host_without_cap_drop != host_expected
    ):
        raise RuntimeManagerError("recovery_container_unverified")

    network_settings = value.get("NetworkSettings")
    if network_settings != {
        "Ports": {},
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "IPAddress": "",
        "IPPrefixLen": 0,
        "MacAddress": "",
        "Networks": {},
    }:
        raise RuntimeManagerError("recovery_container_unverified")
    if value.get("Mounts") != [expected_mount]:
        raise RuntimeManagerError("recovery_container_unverified")
    if (
        value.get("ResolvConfPath") != f"{state_dir}/resolv.conf"
        or value.get("HostnamePath") != f"{state_dir}/hostname"
        or value.get("HostsPath") != f"{state_root}/etchosts/default/{container_id}/hosts"
        or value.get("LogPath") != ""
        or value.get("RestartCount") != 0
        or value.get("Driver") != "overlayfs"
        or value.get("Platform") != "linux"
        or value.get("AppArmorProfile") != ""
    ):
        raise RuntimeManagerError("recovery_container_unverified")
    state = value.get("State")
    if type(state) is not dict or set(state) != {
        "Status", "Running", "Paused", "Restarting", "Pid", "ExitCode", "Error", "StartedAt", "FinishedAt"
    }:
        raise RuntimeManagerError("recovery_container_unverified")
    if (
        state.get("Status") not in {"running", "exited", "created"}
        or type(state.get("Running")) is not bool
        or type(state.get("Paused")) is not bool
        or type(state.get("Restarting")) is not bool
        or type(state.get("Pid")) is not int
        or type(state.get("ExitCode")) is not int
        or type(state.get("Error")) is not str
        or type(state.get("StartedAt")) is not str
        or type(state.get("FinishedAt")) is not str
    ):
        raise RuntimeManagerError("recovery_container_unverified")


def _clear_owned_marker(home: Path, token: str) -> None:
    path = home / ".strategy-session-quarantine"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise RuntimeManagerError("runtime_lease_invalid")
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            raw = os.read(fd, _MAX_LEASE_BYTES + 1)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or info.st_size > _MAX_LEASE_BYTES
                or raw != f"active:{token}\n".encode("ascii")
            ):
                raise RuntimeManagerError("runtime_lease_invalid")
        finally:
            os.close(fd)
        directory_fd = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeManagerError("runtime_lease_invalid")
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except RuntimeManagerError:
        raise
    except OSError as exc:
        raise RuntimeManagerError("runtime_lease_invalid") from exc


def _run_trusted_probe(session_factory: Callable[..., Any], runtime: Any, timeout: float) -> None:
    if _TRUSTED_PROBE_SOURCE != b"def handle(payload):\n    return {'runtime_probe': True}\n":
        raise RuntimeManagerError("runtime_probe_failed")
    try:
        session = session_factory(
            _TRUSTED_PROBE_SOURCE,
            runtime=runtime,
            session_timeout=timeout,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "runtime_busy":
            raise RuntimeManagerError("runtime_busy") from exc
        raise RuntimeManagerError("runtime_probe_failed") from exc
    exchange_ok = False
    close_ok = False
    try:
        exchange_ok = session.exchange({}) == {"runtime_probe": True}
    except Exception:
        exchange_ok = False
    finally:
        try:
            session.close()
            close_ok = True
        except Exception:
            close_ok = False
    if not exchange_ok:
        raise RuntimeManagerError("runtime_probe_failed")
    if not close_ok:
        raise RuntimeManagerError("runtime_probe_close_failed")

