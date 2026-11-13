"""Host-side NDJSON controller for a separately verified Lima/container guest.

This module does not make native host Python safe.  The public constructor is
available only on macOS with an explicit trusted :class:`VMRuntimeSpec`; it
validates the Lima instance, immutable worker image, guest network state, and
live container controls before sending source bytes to the guest worker.

Runtime/image facts are checked at construction, and the source is never
compiled, imported, or executed in the host process.  This is a component only:
it is not wired to a registry, upload endpoint, account, market data, or live
trading path.  The project remains RESEARCH_ONLY / SHADOW_ONLY.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from research_center.vm_strategy_worker import (
    MAX_INPUT_LINE_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_STDERR_BYTES,
    _ProtocolError,
    _strict_json_loads,
    _validate_json_value,
)


DEFAULT_LIMA_HOME = Path.home() / "Library/Application Support/QuantSandbox/lima"
DEFAULT_INSTANCE = "strategy"
WORKER_LABEL = "org.quant.worker.sha256"
OWNER_LABEL = "org.quant.strategy-session.owner"
MAX_UTILITY_STDOUT = 256 * 1024
MAX_UTILITY_STDERR = 8 * 1024
MAX_INSTANCE_YAML_BYTES = 1024 * 1024
DEFAULT_SESSION_TIMEOUT = 180.0
DEFAULT_EXCHANGE_TIMEOUT = 3.0
CLEANUP_UTILITY_TIMEOUT = 8.0
UTILITY_TIMEOUT = 8.0
STARTUP_TIMEOUT = 12.0
CONTAINER_READINESS_TIMEOUT = 3.0
CONTAINER_READINESS_INTERVAL = 0.05
CONTAINER_HOLDER_SECONDS = 190
_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_IMAGE_REPOSITORY = rf"(?:(?:[a-z0-9.-]+(?::[0-9]+)?)/)?{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
_IMAGE_REF_RE = re.compile(rf"(?:{_IMAGE_REPOSITORY}@)?sha256:[0-9a-f]{{64}}\Z")
_INSTANCE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
_CONTAINER_NAME_RE = re.compile(r"qs-[0-9a-f]{24}\Z")
_RESPONSE_ERRORS = {
    "startup_failed",
    "callback_failed",
    "result_invalid",
    "result_too_large",
}
_WORKER_ENTRYPOINT = ["python", "-I", "-B", "-u", "/opt/quant/worker.py"]
_CONTAINER_HOLDER_SOURCE = f"import time; time.sleep({CONTAINER_HOLDER_SECONDS})"
_WORKER_IMAGE_ENV_KEYS = {
    "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "UV_TOOL_BIN_DIR",
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_MAX_THREADS",
}


class VMStrategyError(RuntimeError):
    """Safe public error carrying only a fixed error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VMStrategyUnavailable(VMStrategyError):
    """The requested guest runtime could not be verified or controlled."""


class _ProtocolFailure(VMStrategyError):
    """An invalid or over-limit host/guest NDJSON exchange."""


@dataclass(frozen=True)
class VMRuntimeSpec:
    """Frozen trusted configuration for one reviewed Lima worker runtime.

    ``lima_home`` and ``instance`` are checked again by the public constructor
    against the project-fixed QuantSandbox location and instance name.  Tests
    exercise the private controller with synthetic temporary roots instead.
    """

    expected_instance_yaml_sha256: str
    expected_resolved_config_sha256: str
    immutable_image_ref: str
    worker_sha256: str
    expected_image_config_sha256: str | None = None
    lima_home: Path = DEFAULT_LIMA_HOME
    instance: str = DEFAULT_INSTANCE

    def __post_init__(self) -> None:
        if not isinstance(self.lima_home, Path) or not self.lima_home.is_absolute():
            raise ValueError("runtime_path")
        if not isinstance(self.instance, str) or _INSTANCE_RE.fullmatch(self.instance) is None:
            raise ValueError("runtime_instance")
        if not isinstance(self.expected_instance_yaml_sha256, str) or not _SHA256_RE.fullmatch(
            self.expected_instance_yaml_sha256
        ):
            raise ValueError("runtime_yaml_hash")
        if not isinstance(self.expected_resolved_config_sha256, str) or not _SHA256_RE.fullmatch(
            self.expected_resolved_config_sha256
        ):
            raise ValueError("runtime_resolved_config_hash")
        if not isinstance(self.worker_sha256, str) or not _SHA256_RE.fullmatch(self.worker_sha256):
            raise ValueError("runtime_worker_hash")
        if not isinstance(self.immutable_image_ref, str) or not _IMAGE_REF_RE.fullmatch(
            self.immutable_image_ref
        ):
            raise ValueError("runtime_image_ref")
        if self.expected_image_config_sha256 is not None and (
            not isinstance(self.expected_image_config_sha256, str)
            or not _SHA256_RE.fullmatch(self.expected_image_config_sha256)
        ):
            raise ValueError("runtime_image_config_hash")


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _ProbeResult:
    memory_max: str
    swap_max: str
    cpu_max: str
    pids_max: str
    uid: int
    euid: int
    no_new_privs: int
    cap_eff: str
    root_readonly: bool
    interfaces: tuple[str, ...]
    vsock_errno: int


@dataclass(frozen=True)
class _GuestProbeResult:
    interfaces: tuple[tuple[str, str], ...]
    default_route: bool


_GUEST_NIC_PROBE = r"""import json, os
items = []
for name in sorted(os.listdir('/sys/class/net')):
    if name == 'lo':
        continue
    try:
        state = open('/sys/class/net/' + name + '/operstate', 'r', encoding='ascii').read().strip()
    except OSError:
        state = 'unreadable'
    items.append({'name': name, 'operstate': state})
default_route = False
try:
    with open('/proc/net/route', 'r', encoding='ascii') as handle:
        next(handle, None)
        for line in handle:
            fields = line.split()
            if len(fields) >= 4 and fields[0] != 'lo' and fields[1] == '00000000':
                flags = int(fields[3], 16)
                if (flags & 1) and not (flags & 0x200):
                    default_route = True
except (OSError, ValueError):
    default_route = True
try:
    with open('/proc/net/ipv6_route', 'r', encoding='ascii') as handle:
        for line in handle:
            fields = line.split()
            # Fields end with flags, device. Ignore loopback/reject placeholders.
            if len(fields) >= 10 and fields[0] == ('0' * 32) and fields[1] == '00':
                flags = int(fields[8], 16)
                interface = fields[9]
                if interface != 'lo' and not (flags & 0x200):
                    default_route = True
except (OSError, ValueError):
    default_route = True
print(json.dumps({'interfaces': items, 'default_route': default_route}, separators=(',', ':')))
"""


_CONTAINER_PROBE = r"""import errno, json, os, socket, stat
def read(path):
    with open(path, 'r', encoding='ascii') as handle:
        return handle.read().strip()
status = {}
with open('/proc/self/status', 'r', encoding='ascii') as handle:
    for line in handle:
        if line.startswith('NoNewPrivs:'):
            status['no_new_privs'] = int(line.split()[1])
        elif line.startswith('CapEff:'):
            status['cap_eff'] = line.split()[1]
try:
    vsock = getattr(socket, 'AF_VSOCK')
except AttributeError:
    vsock_errno = -1
else:
    try:
        sock = socket.socket(vsock, socket.SOCK_STREAM, 0)
    except OSError as exc:
        vsock_errno = exc.errno or 0
    else:
        sock.close()
        vsock_errno = 0
root_flags = os.statvfs('/').f_flag
print(json.dumps({
    'memory_max': read('/sys/fs/cgroup/memory.max'),
    'swap_max': read('/sys/fs/cgroup/memory.swap.max'),
    'cpu_max': read('/sys/fs/cgroup/cpu.max'),
    'pids_max': read('/sys/fs/cgroup/pids.max'),
    'uid': os.getuid(), 'euid': os.geteuid(),
    'no_new_privs': status.get('no_new_privs', -1),
    'cap_eff': status.get('cap_eff', ''),
    'root_readonly': bool(root_flags & os.ST_RDONLY),
    'interfaces': sorted(os.listdir('/sys/class/net')),
    'vsock_errno': vsock_errno,
}, separators=(',', ':')))
"""


def _strict_load_json_object(raw: bytes, *, expected_keys: set[str] | None = None) -> dict[str, Any]:
    try:
        value = _strict_json_loads(raw)
    except _ProtocolError as exc:
        raise _ProtocolFailure("invalid_json") from exc
    if type(value) is not dict:
        raise _ProtocolFailure("invalid_json_shape")
    if expected_keys is not None and set(value) != expected_keys:
        raise _ProtocolFailure("invalid_json_shape")
    return value


def _strict_json_bytes(value: Any, *, maximum: int) -> bytes:
    try:
        _bounded_json_preflight(value, maximum=maximum)
    except _ProtocolError as exc:
        if str(exc) == "frame_too_large":
            raise _ProtocolFailure("frame_too_large") from exc
        raise _ProtocolFailure("invalid_json_value") from exc
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        output = bytearray()
        for text_chunk in encoder.iterencode(value):
            # The preflight bounds every scalar's character length. Check the
            # cheap lower bound before UTF-8 encoding each emitted chunk.
            if len(text_chunk) + len(output) + 1 > maximum:
                raise _ProtocolFailure("frame_too_large")
            encoded_chunk = text_chunk.encode("utf-8", "strict")
            if len(encoded_chunk) + len(output) + 1 > maximum:
                raise _ProtocolFailure("frame_too_large")
            output.extend(encoded_chunk)
        output.append(0x0A)
        return bytes(output)
    except _ProtocolFailure:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError, _ProtocolError) as exc:
        raise _ProtocolFailure("invalid_json_value") from exc


def _bounded_json_preflight(value: Any, *, maximum: int) -> None:
    """Validate JSON shape and lower-bound output size without copying scalars."""
    if type(maximum) is not int or maximum < 1:
        raise _ProtocolError("frame_too_large")
    char_budget = maximum - 1  # reserve the terminating newline
    nodes = 0
    minimum_chars = 0
    ancestors: set[int] = set()

    def add_minimum_chars(count: int) -> None:
        nonlocal minimum_chars
        if count < 0 or count > char_budget - minimum_chars:
            raise _ProtocolError("frame_too_large")
        minimum_chars += count

    def count_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise _ProtocolError("nodes")

    def validate_string(item: str) -> None:
        add_minimum_chars(len(item))
        # String length is already bounded before scanning code points.
        for character in item:
            codepoint = ord(character)
            if 0xD800 <= codepoint <= 0xDFFF:
                raise _ProtocolError("invalid_unicode")

    def visit(item: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise _ProtocolError("depth")
        count_node()
        item_type = type(item)
        if item is None:
            add_minimum_chars(4)
            return
        if item_type is bool:
            add_minimum_chars(5 if item is False else 4)
            return
        if item_type is int:
            bits = item.bit_length()
            # 0.301029 is strictly below log10(2), so this is a safe lower
            # bound on decimal digits without converting a huge integer.
            digits = ((bits - 1) * 301029) // 1_000_000 + 1 if bits else 1
            add_minimum_chars(digits + (1 if item < 0 else 0))
            return
        if item_type is float:
            if not math.isfinite(item):
                raise _ProtocolError("nonfinite")
            add_minimum_chars(len(repr(item)))
            return
        if item_type is str:
            validate_string(item)
            return
        if item_type not in (list, dict):
            raise _ProtocolError("unsupported_type")

        identity = id(item)
        if identity in ancestors:
            raise _ProtocolError("cycle")
        if item_type is list and len(item) > MAX_JSON_NODES - nodes:
            raise _ProtocolError("nodes")
        if item_type is dict and len(item) > (MAX_JSON_NODES - nodes) // 2:
            raise _ProtocolError("nodes")
        ancestors.add(identity)
        try:
            if item_type is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise _ProtocolError("non_string_key")
                    count_node()  # keys count toward the shared node limit
                    validate_string(key)
                    visit(child, depth + 1)
        finally:
            ancestors.remove(identity)

    visit(value, 0)


def _canonical_resolved_config_bytes(value: Any) -> bytes:
    """Canonical JSON for the entire resolved Lima config object.

    Canonical form is UTF-8 JSON with recursively sorted object keys, compact
    separators, non-ASCII characters preserved, and non-finite values rejected.
    The expected digest is part of the trusted frozen runtime specification.
    """
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError, _ProtocolError) as exc:
        raise VMStrategyUnavailable("lima_config_canonicalization_failed") from exc
    return encoded


def _resolved_config_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_resolved_config_bytes(value)).hexdigest()


def _path_is_regular_nosymlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _read_instance_yaml(path: Path) -> bytes:
    if not _path_is_regular_nosymlink(path):
        raise VMStrategyUnavailable("instance_config_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INSTANCE_YAML_BYTES:
                raise VMStrategyUnavailable("instance_config_invalid")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, min(65536, MAX_INSTANCE_YAML_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > MAX_INSTANCE_YAML_BYTES:
                    raise VMStrategyUnavailable("instance_config_invalid")
                chunks.append(block)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except VMStrategyError:
        raise
    except OSError as exc:
        raise VMStrategyUnavailable("instance_config_unavailable") from exc


def _validate_lima_config(config: Any) -> None:
    """Validate Lima's resolved instance config from ``limactl list --json``.

    The source YAML is hash-pinned separately.  Lima's resolved config is the
    authority for inherited/builtin defaults, so this deliberately does not
    try to interpret YAML or infer safety from absent source keys.
    """
    if type(config) is not dict:
        raise VMStrategyUnavailable("lima_config_unavailable")
    if config.get("vmType") != "vz":
        raise VMStrategyUnavailable("vm_type_unverified")
    if config.get("mounts") not in (None, []):
        raise VMStrategyUnavailable("host_mount_configured")
    if config.get("additionalDisks", []) != []:
        raise VMStrategyUnavailable("host_disk_configured")
    if "mountInotify" in config and config["mountInotify"] is not False:
        raise VMStrategyUnavailable("host_mount_configured")

    ssh = config.get("ssh")
    if type(ssh) is not dict:
        raise VMStrategyUnavailable("ssh_config_unverified")
    if set(ssh) != {
        "localPort", "loadDotSSHPubKeys", "forwardAgent", "forwardX11",
        "forwardX11Trusted", "overVsock",
    }:
        raise VMStrategyUnavailable("ssh_config_unverified")
    if (
        type(ssh.get("localPort")) is not int
        or ssh.get("localPort") != 0
        or ssh.get("overVsock") is not True
        or ssh.get("forwardAgent") is not False
        or ssh.get("loadDotSSHPubKeys") is not False
        or ssh.get("forwardX11") is not False
        or ssh.get("forwardX11Trusted") is not False
    ):
        raise VMStrategyUnavailable("ssh_config_unverified")

    if config.get("propagateProxyEnv") is not False:
        raise VMStrategyUnavailable("proxy_config_unverified")
    if type(config.get("env", {})) is not dict or config.get("env", {}) != {}:
        raise VMStrategyUnavailable("guest_environment_configured")
    host_resolver = config.get("hostResolver")
    if type(host_resolver) is not dict or host_resolver.get("enabled") is not False:
        raise VMStrategyUnavailable("host_resolver_unverified")
    if set(host_resolver) != {"enabled", "ipv6"} or host_resolver.get("ipv6") is not False:
        raise VMStrategyUnavailable("host_resolver_unverified")
    if config.get("networks", []) not in (None, []):
        raise VMStrategyUnavailable("host_network_configured")

    forwards = config.get("portForwards")
    if type(forwards) is not list or len(forwards) != 1:
        raise VMStrategyUnavailable("port_forward_config_unverified")
    rule = forwards[0]
    required_rule = {
        "guestIP": "0.0.0.0",
        "guestIPMustBeZero": False,
        "guestPortRange": [1, 65535],
        "hostIP": "127.0.0.1",
        "hostPortRange": [1, 65535],
        "proto": "any",
        "ignore": True,
    }
    if type(rule) is not dict or rule != required_rule:
        raise VMStrategyUnavailable("port_forward_config_unverified")
    if type(rule["guestIPMustBeZero"]) is not bool or rule["guestIPMustBeZero"] is not False:
        raise VMStrategyUnavailable("port_forward_config_unverified")
    if type(rule["ignore"]) is not bool or rule["ignore"] is not True:
        raise VMStrategyUnavailable("port_forward_config_unverified")
    if any(type(item) is not int for item in rule["guestPortRange"] + rule["hostPortRange"]):
        raise VMStrategyUnavailable("port_forward_config_unverified")

    # Fail closed if Lima adds a host attachment, proxy, environment, socket,
    # SSH, or network field anywhere in the resolved config that this schema
    # did not review.
    allowed_sensitive_paths = {
        ("mounts",), ("additionaldisks",), ("mountinotify",), ("disk",),
        ("propagateproxyenv",), ("ssh",), ("ssh", "localport"),
        ("ssh", "loaddotsshpubkeys"), ("ssh", "forwardagent"),
        ("ssh", "forwardx11"), ("ssh", "forwardx11trusted"),
        ("ssh", "overvsock"), ("portforwards",), ("hostresolver",),
        ("hostresolver", "enabled"), ("hostresolver", "ipv6"),
        ("env",), ("networks",),
    }
    forbidden_fragments = (
        "mount", "disk", "proxy", "env", "sshagent", "pubkey", "forwardx11",
        "hostagent", "network", "vsock", "socket",
    )

    def check_keys(value: Any, path: tuple[str, ...] = ()) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise VMStrategyUnavailable("lima_config_unavailable")
                child_path = path + (key.lower(),)
                if any(fragment in key.lower() for fragment in forbidden_fragments):
                    if child_path not in allowed_sensitive_paths:
                        raise VMStrategyUnavailable("lima_config_unrecognized_security_field")
                check_keys(child, child_path)
        elif type(value) is list:
            for child in value:
                check_keys(child, path)

    check_keys(config)


def _reject_override_files(lima_home: Path, instance_dir: Path) -> None:
    candidates = (
        lima_home / "_config" / "override.yaml",
        lima_home / "_config" / "override.yml",
        lima_home / "_config" / "defaults.yaml",
        lima_home / "_config" / "defaults.yml",
        lima_home / "_config" / "default.yaml",
        lima_home / "_config" / "default.yml",
        instance_dir / "override.yaml",
        instance_dir / "override.yml",
        instance_dir / "default.yaml",
        instance_dir / "default.yml",
    )
    for path in candidates:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VMStrategyUnavailable("override_metadata_unavailable") from exc
        raise VMStrategyUnavailable("override_file_present")


def _parse_lima_instance(
    raw: bytes,
    instance: str,
    instance_dir: Path,
    lima_home: Path,
    expected_resolved_config_sha256: str,
) -> None:
    try:
        value = _strict_json_loads(raw)
    except _ProtocolError as exc:
        raise VMStrategyUnavailable("lima_list_invalid") from exc
    if type(value) is list:
        if len(value) != 1:
            raise VMStrategyUnavailable("lima_instance_ambiguous")
        value = value[0]
    if type(value) is not dict:
        raise VMStrategyUnavailable("lima_list_invalid")
    if value.get("name") != instance or value.get("status") != "Running":
        raise VMStrategyUnavailable("lima_instance_not_running")
    if value.get("vmType") != "vz":
        raise VMStrategyUnavailable("vm_type_unverified")
    expected_paths = {
        "dir": str(instance_dir),
        "LimaHome": str(lima_home),
        "sshConfigFile": str(instance_dir / "ssh.config"),
        # Compare only the metadata path. Never open or read the key file.
        "IdentityFile": str(lima_home / "_config" / "user"),
    }
    for key, expected_path in expected_paths.items():
        if type(value.get(key)) is not str or value[key] != expected_path:
            raise VMStrategyUnavailable("lima_instance_path_mismatch")
    ssh_local_port = value.get("sshLocalPort")
    if (
        type(ssh_local_port) is not int
        or not (1 <= ssh_local_port <= 65535)
        or value.get("sshAddress") != "127.0.0.1"
    ):
        raise VMStrategyUnavailable("lima_ssh_endpoint_unverified")
    if value.get("HostOS") != "darwin":
        raise VMStrategyUnavailable("lima_host_unverified")
    if value.get("HostArch") not in {"aarch64", "arm64"}:
        raise VMStrategyUnavailable("lima_host_unverified")
    config = value.get("config")
    if _resolved_config_sha256(config) != expected_resolved_config_sha256:
        raise VMStrategyUnavailable("lima_config_hash_mismatch")
    _validate_lima_config(config)


def _parse_guest_probe(raw: bytes) -> _GuestProbeResult:
    value = _strict_load_json_object(raw.strip(), expected_keys={"interfaces", "default_route"})
    if type(value["interfaces"]) is not list or type(value["default_route"]) is not bool:
        raise _ProtocolFailure("guest_probe_invalid")
    interfaces: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in value["interfaces"]:
        if type(item) is not dict or set(item) != {"name", "operstate"}:
            raise _ProtocolFailure("guest_probe_invalid")
        name, state = item["name"], item["operstate"]
        if type(name) is not str or type(state) is not str or not name or name in names:
            raise _ProtocolFailure("guest_probe_invalid")
        names.add(name)
        interfaces.append((name, state.lower()))
    return _GuestProbeResult(tuple(interfaces), value["default_route"])


def _validate_guest_probe(raw: bytes) -> _GuestProbeResult:
    result = _parse_guest_probe(raw)
    offline_states = {"down", "lowerlayerdown", "dormant", "notpresent"}
    if result.default_route or any(state not in offline_states for _name, state in result.interfaces):
        raise VMStrategyUnavailable("guest_network_not_offline")
    return result


def _parse_container_probe(raw: bytes) -> _ProbeResult:
    keys = {
        "memory_max", "swap_max", "cpu_max", "pids_max", "uid", "euid",
        "no_new_privs", "cap_eff", "root_readonly", "interfaces", "vsock_errno",
    }
    value = _strict_load_json_object(raw.strip(), expected_keys=keys)
    strings = ("memory_max", "swap_max", "cpu_max", "pids_max", "cap_eff")
    if any(type(value[key]) is not str for key in strings):
        raise _ProtocolFailure("container_probe_invalid")
    integers = ("uid", "euid", "no_new_privs", "vsock_errno")
    if any(type(value[key]) is not int for key in integers):
        raise _ProtocolFailure("container_probe_invalid")
    if type(value["root_readonly"]) is not bool or type(value["interfaces"]) is not list:
        raise _ProtocolFailure("container_probe_invalid")
    if any(type(item) is not str for item in value["interfaces"]):
        raise _ProtocolFailure("container_probe_invalid")
    return _ProbeResult(
        value["memory_max"], value["swap_max"], value["cpu_max"], value["pids_max"],
        value["uid"], value["euid"], value["no_new_privs"], value["cap_eff"],
        value["root_readonly"], tuple(value["interfaces"]), value["vsock_errno"],
    )


def _validate_container_probe(raw: bytes) -> _ProbeResult:
    probe = _parse_container_probe(raw)
    cpu = re.fullmatch(r"([0-9]{1,12}) ([0-9]{1,12})", probe.cpu_max)
    if cpu is None or int(cpu.group(2)) <= 0 or int(cpu.group(1)) != int(cpu.group(2)):
        raise VMStrategyUnavailable("container_cpu_limit_unverified")
    if (
        probe.memory_max != "268435456"
        or probe.swap_max != "0"
        or probe.pids_max != "32"
        or probe.uid != 65532
        or probe.euid != 65532
        or probe.no_new_privs != 1
        or re.fullmatch(r"0+", probe.cap_eff) is None
        or not probe.root_readonly
        or probe.interfaces != ("lo",)
        or probe.vsock_errno not in {errno.EPERM, errno.EAFNOSUPPORT}
    ):
        raise VMStrategyUnavailable("container_controls_unverified")
    return probe


def _image_inspect_argument(image_ref: str) -> str:
    if not _IMAGE_REF_RE.fullmatch(image_ref):
        raise VMStrategyUnavailable("runtime_image_ref")
    return image_ref.rsplit("@", 1)[-1].removeprefix("sha256:")


def _parse_image_inspect(
    raw: bytes,
    image_ref: str,
    worker_sha256: str,
    expected_config_sha256: str | None = None,
) -> None:
    try:
        value = _strict_json_loads(raw)
    except _ProtocolError as exc:
        raise VMStrategyUnavailable("image_inspect_invalid") from exc
    if type(value) is list:
        if len(value) != 1:
            raise VMStrategyUnavailable("image_ref_ambiguous")
        value = value[0]
    if type(value) is not dict:
        raise VMStrategyUnavailable("image_inspect_invalid")
    repo_digests = value.get("RepoDigests")
    if repo_digests is None:
        repo_digests = []
    config = value.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if type(repo_digests) is not list or any(type(item) is not str for item in repo_digests):
        raise VMStrategyUnavailable("image_inspect_invalid")
    if type(labels) is not dict or labels.get(WORKER_LABEL) != worker_sha256:
        raise VMStrategyUnavailable("image_identity_unverified")
    if config.get("User") != "65532:65532":
        raise VMStrategyUnavailable("image_user_unverified")
    if config.get("Entrypoint") != _WORKER_ENTRYPOINT or config.get("Cmd") not in (None, []):
        raise VMStrategyUnavailable("image_entrypoint_unverified")
    if config.get("WorkingDir") != "/tmp":
        raise VMStrategyUnavailable("image_workdir_unverified")
    image_env = config.get("Env")
    if type(image_env) is not list or len(image_env) != len(_WORKER_IMAGE_ENV_KEYS):
        raise VMStrategyUnavailable("image_environment_unverified")
    env_names: list[str] = []
    for item in image_env:
        if type(item) is not str or len(item) > 4096 or "=" not in item:
            raise VMStrategyUnavailable("image_environment_unverified")
        env_names.append(item.split("=", 1)[0])
    if len(set(env_names)) != len(env_names) or set(env_names) != _WORKER_IMAGE_ENV_KEYS:
        raise VMStrategyUnavailable("image_environment_unverified")
    image_id = value.get("Id")
    if type(image_id) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise VMStrategyUnavailable("image_identity_unverified")
    if value.get("Os") != "linux" or value.get("Architecture") not in {"arm64", "aarch64"}:
        raise VMStrategyUnavailable("image_platform_unverified")
    requested_digest = image_ref.rsplit("@", 1)[-1]
    digest_match = any(item.rsplit("@", 1)[-1] == requested_digest for item in repo_digests)
    # nerdctl's inspect Id is a config digest, not necessarily the OCI
    # index/manifest digest named by the immutable reference.
    if not digest_match and expected_config_sha256 is None:
        raise VMStrategyUnavailable("image_identity_unverified")
    if expected_config_sha256 is not None and image_id != f"sha256:{expected_config_sha256}":
        raise VMStrategyUnavailable("image_config_identity_mismatch")


def _parse_worker_response(line: bytes, expected_request_id: str) -> tuple[bool, Any]:
    if not line.endswith(b"\n") or len(line) > MAX_RESPONSE_BYTES:
        raise _ProtocolFailure("response_frame_invalid")
    response = _strict_load_json_object(line[:-1])
    if type(response.get("request_id")) is not str or response["request_id"] != expected_request_id:
        raise _ProtocolFailure("response_nonce_mismatch")
    keys = set(response)
    if keys == {"request_id", "result"}:
        return True, response["result"]
    if keys == {"request_id", "error"} and type(response["error"]) is str:
        if response["error"] not in _RESPONSE_ERRORS:
            raise _ProtocolFailure("response_error_invalid")
        return False, response["error"]
    raise _ProtocolFailure("response_shape_invalid")


class _SubprocessTransport:
    """Bounded command runner that owns and reaps every process it starts."""

    @staticmethod
    def _child_env(lima_home: Path) -> dict[str, str]:
        inherited_home = os.environ.get("HOME")
        if not inherited_home:
            raise VMStrategyUnavailable("host_home_unavailable")
        env = {"PATH": _SAFE_PATH, "HOME": inherited_home, "LIMA_HOME": str(lima_home)}
        for key in ("LANG", "LC_ALL"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> list[str]:
        if type(argv) not in (list, tuple) or not argv:
            raise VMStrategyUnavailable("command_invalid")
        normalized: list[str] = []
        for value in argv:
            if type(value) is not str or not value or "\x00" in value:
                raise VMStrategyUnavailable("command_invalid")
            normalized.append(value)
        return normalized

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=0.4)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            raise VMStrategyUnavailable("child_reap_failed") from exc

    def run(
        self,
        argv: Sequence[str],
        *,
        lima_home: Path,
        timeout: float = UTILITY_TIMEOUT,
        cwd: Path | None = None,
        stdout_limit: int = MAX_UTILITY_STDOUT,
        stderr_limit: int = MAX_UTILITY_STDERR,
    ) -> _CommandResult:
        command = self._validate_argv(argv)
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or type(stdout_limit) is not int
            or type(stderr_limit) is not int
            or stdout_limit < 0
            or stderr_limit < 0
        ):
            raise VMStrategyUnavailable("command_limits_invalid")
        env = self._child_env(lima_home)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd or lima_home),
                env=env,
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        except (OSError, ValueError) as exc:
            raise VMStrategyUnavailable("command_start_failed") from exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        streams = {process.stdout.fileno(): (process.stdout, stdout, stdout_limit),
                   process.stderr.fileno(): (process.stderr, stderr, stderr_limit)}
        try:
            for fd, (stream, _buffer, _limit) in streams.items():
                os.set_blocking(fd, False)
                selector.register(stream, selectors.EVENT_READ, fd)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VMStrategyUnavailable("command_timeout")
                for key, _mask in selector.select(min(remaining, 0.1)):
                    fd = key.data
                    stream, target, limit = streams[fd]
                    try:
                        chunk = os.read(fd, min(65536, limit + 1 - len(target)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    target.extend(chunk)
                    if len(target) > limit:
                        raise VMStrategyUnavailable("command_output_limit")
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            return _CommandResult(returncode, bytes(stdout), bytes(stderr))
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_group(process)
            raise VMStrategyUnavailable("command_timeout") from exc
        except BaseException:
            self._terminate_process_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def start(
        self,
        argv: Sequence[str],
        *,
        lima_home: Path,
        cwd: Path,
    ) -> subprocess.Popen[bytes]:
        command = self._validate_argv(argv)
        env = self._child_env(lima_home)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                env=env,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
                shell=False,
            )
        except (OSError, ValueError) as exc:
            raise VMStrategyUnavailable("container_start_failed") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._terminate_process_group(process)
            raise VMStrategyUnavailable("container_pipes_unavailable")
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        return process


class _VMStrategySessionCore:
    """Private controller; injected transports are used only by synthetic tests."""

    def __init__(
        self,
        source: Any,
        runtime: Any,
        *,
        transport: Any = None,
        platform: str | None = None,
        session_timeout: float = DEFAULT_SESSION_TIMEOUT,
    ) -> None:
        self._runtime = runtime
        self._transport = transport or _SubprocessTransport()
        self._platform = platform if platform is not None else sys.platform
        self._process: subprocess.Popen[bytes] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._container_name = f"qs-{uuid.uuid4().hex[:24]}"
        self._owner_token = uuid.uuid4().hex
        self._container_create_attempted = False
        self._container_created = False
        self._container_create_uncertain = False
        self._closed = False
        self._lock_fd: int | None = None
        self._lease_marker_owned = False
        self._close_lock = threading.RLock()
        self._watchdog_timer: threading.Timer | None = None
        self._close_error_code: str | None = None
        self._stderr_bytes = 0
        self._stderr_overflow = False
        self._stderr_io_error = False
        self._stderr_lock = threading.Lock()
        self._stdout_buffer = bytearray()
        self._session_timeout = session_timeout
        self._session_deadline = 0.0
        self._instance_dir = Path(runtime.lima_home) / runtime.instance
        self._lima_home = Path(runtime.lima_home)
        self._limactl_path: str | None = None
        self._container_command: list[str] = []
        self._container_start_command: list[str] = []
        self._worker_exec_command: list[str] = []
        self._source = source
        try:
            self._validate_controller_config()
            self._session_deadline = time.monotonic() + session_timeout
            self._preflight_and_start()
            self._send_startup_source()
            self._arm_session_watchdog()
        except BaseException:
            try:
                self.close()
            except VMStrategyError:
                pass
            raise

    def _validate_controller_config(self) -> None:
        if self._platform != "darwin":
            raise VMStrategyUnavailable("unsupported_platform")
        if (
            type(self._session_timeout) not in (int, float)
            or not math.isfinite(self._session_timeout)
            or not (1.0 <= self._session_timeout <= DEFAULT_SESSION_TIMEOUT)
        ):
            raise VMStrategyUnavailable("session_timeout_invalid")
        if not isinstance(self._lima_home, Path) or not self._lima_home.is_absolute():
            raise VMStrategyUnavailable("runtime_path")
        if not isinstance(self._runtime.instance, str) or _INSTANCE_RE.fullmatch(
            self._runtime.instance
        ) is None:
            raise VMStrategyUnavailable("runtime_instance")
        if not _SHA256_RE.fullmatch(str(self._runtime.expected_instance_yaml_sha256)):
            raise VMStrategyUnavailable("runtime_yaml_hash")
        if not _SHA256_RE.fullmatch(str(self._runtime.expected_resolved_config_sha256)):
            raise VMStrategyUnavailable("runtime_resolved_config_hash")
        if not _SHA256_RE.fullmatch(str(self._runtime.worker_sha256)):
            raise VMStrategyUnavailable("runtime_worker_hash")
        if not _IMAGE_REF_RE.fullmatch(str(self._runtime.immutable_image_ref)):
            raise VMStrategyUnavailable("runtime_image_ref")
        expected_config = getattr(self._runtime, "expected_image_config_sha256", None)
        if expected_config is not None and not _SHA256_RE.fullmatch(str(expected_config)):
            raise VMStrategyUnavailable("runtime_image_config_hash")

    def _acquire_runtime_lock(self) -> None:
        try:
            root_info = self._lima_home.lstat()
        except OSError as exc:
            raise VMStrategyUnavailable("runtime_home_unavailable") from exc
        if not stat.S_ISDIR(root_info.st_mode):
            raise VMStrategyUnavailable("runtime_home_invalid")
        try:
            resolved = self._lima_home.resolve(strict=True)
            if resolved != self._lima_home:
                raise VMStrategyUnavailable("runtime_home_invalid")
        except OSError as exc:
            raise VMStrategyUnavailable("runtime_home_invalid") from exc
        try:
            instance_info = self._instance_dir.lstat()
        except OSError as exc:
            raise VMStrategyUnavailable("instance_dir_unavailable") from exc
        if not stat.S_ISDIR(instance_info.st_mode):
            raise VMStrategyUnavailable("instance_dir_invalid")
        try:
            if self._instance_dir.resolve(strict=True) != self._lima_home / self._runtime.instance:
                raise VMStrategyUnavailable("instance_dir_invalid")
        except OSError as exc:
            raise VMStrategyUnavailable("instance_dir_invalid") from exc
        lock_path = self._lima_home / ".strategy-session.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                os.close(descriptor)
                raise VMStrategyUnavailable("runtime_lock_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise VMStrategyUnavailable("runtime_busy") from exc
            self._lock_fd = descriptor
        except VMStrategyError:
            raise
        except OSError as exc:
            raise VMStrategyUnavailable("runtime_lock_failed") from exc
        marker = self._lima_home / ".strategy-session-quarantine"
        try:
            marker.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise VMStrategyUnavailable("runtime_quarantined") from exc
        else:
            raise VMStrategyUnavailable("runtime_quarantined")

    def _binary(self, name: str) -> str:
        path = shutil.which(name, path=_SAFE_PATH)
        if path is None:
            raise VMStrategyUnavailable("runtime_command_missing")
        return path

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = CLEANUP_UTILITY_TIMEOUT,
        session_bounded: bool = False,
    ) -> _CommandResult:
        """Run a utility; preflight honors session time, cleanup keeps a grace window."""
        if session_bounded:
            timeout = min(timeout, self._remaining_session())
        try:
            result = self._transport.run(
                argv,
                lima_home=self._lima_home,
                timeout=timeout,
                cwd=self._lima_home,
                stdout_limit=MAX_UTILITY_STDOUT,
                stderr_limit=MAX_UTILITY_STDERR,
            )
            if (
                type(result) is not _CommandResult
                or type(result.returncode) is not int
                or type(result.stdout) is not bytes
                or type(result.stderr) is not bytes
            ):
                raise VMStrategyUnavailable("runtime_command_result_invalid")
            if len(result.stdout) > MAX_UTILITY_STDOUT or len(result.stderr) > MAX_UTILITY_STDERR:
                raise VMStrategyUnavailable("command_output_limit")
            return result
        except VMStrategyError:
            raise
        except BaseException as exc:
            raise VMStrategyUnavailable("runtime_command_failed") from exc

    def _require_success(self, result: _CommandResult, code: str) -> bytes:
        if type(result.returncode) is not int or result.returncode != 0:
            raise VMStrategyUnavailable(code)
        if len(result.stdout) > MAX_UTILITY_STDOUT or len(result.stderr) > MAX_UTILITY_STDERR:
            raise VMStrategyUnavailable("command_output_limit")
        return result.stdout

    def _preflight_and_start(self) -> None:
        self._acquire_runtime_lock()
        instance_yaml = self._instance_dir / "lima.yaml"
        _reject_override_files(self._lima_home, self._instance_dir)
        raw_yaml = _read_instance_yaml(instance_yaml)
        actual_yaml_hash = hashlib.sha256(raw_yaml).hexdigest()
        if actual_yaml_hash != self._runtime.expected_instance_yaml_sha256:
            raise VMStrategyUnavailable("instance_config_hash_mismatch")

        limactl = self._binary("limactl")
        self._limactl_path = limactl
        listed = self._run(
            [limactl, "list", "--json", self._runtime.instance], session_bounded=True
        )
        _parse_lima_instance(
            self._require_success(listed, "lima_list_failed"),
            self._runtime.instance,
            self._instance_dir,
            self._lima_home,
            self._runtime.expected_resolved_config_sha256,
        )

        guest_probe = self._run(
            [limactl, "shell", self._runtime.instance, "python3", "-I", "-B", "-c", _GUEST_NIC_PROBE],
            session_bounded=True,
        )
        try:
            _validate_guest_probe(self._require_success(guest_probe, "guest_probe_failed"))
        except _ProtocolFailure as exc:
            raise VMStrategyUnavailable("guest_probe_invalid") from exc

        image_inspect = self._run(
            self._guest_nerdctl(
                "image", "inspect", _image_inspect_argument(self._runtime.immutable_image_ref)
            ),
            session_bounded=True,
        )
        _parse_image_inspect(
            self._require_success(image_inspect, "image_inspect_failed"),
            self._runtime.immutable_image_ref,
            self._runtime.worker_sha256,
            getattr(self._runtime, "expected_image_config_sha256", None),
        )

        self._container_command = self._build_container_command()
        self._assert_name_available(session_bounded=True)
        self._create_quarantine_lease()
        # A create timeout/error is ambiguous: the guest daemon may still have
        # created the named container. Keep the runtime quarantined in that case.
        self._container_create_attempted = True
        self._container_create_uncertain = True
        created = self._run(self._container_command, session_bounded=True)
        created_id = self._require_success(created, "container_create_failed")
        if created_id.endswith(b"\n"):
            created_id = created_id[:-1]
        try:
            decoded_id = created_id.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise VMStrategyUnavailable("container_create_unverified") from exc
        if _SHA256_RE.fullmatch(decoded_id) is None:
            raise VMStrategyUnavailable("container_create_unverified")
        self._container_created = True
        self._container_create_uncertain = False

        exists, labels = self._inspect_container(session_bounded=True)
        if not exists or labels is None or labels.get(OWNER_LABEL) != self._owner_token:
            raise VMStrategyUnavailable("container_create_unverified")

        self._container_start_command = self._build_container_start_command()
        started = self._run(self._container_start_command, session_bounded=True)
        self._require_success(started, "container_start_failed")

        self._wait_for_container_running()

        probe = self._run(
            self._guest_nerdctl(
                "exec", self._container_name, "python3", "-I", "-B", "-c", _CONTAINER_PROBE
            ),
            session_bounded=True,
        )
        try:
            _validate_container_probe(self._require_success(probe, "container_probe_failed"))
        except _ProtocolFailure as exc:
            raise VMStrategyUnavailable("container_probe_invalid") from exc

        self._worker_exec_command = self._build_worker_exec_command()
        try:
            self._process = self._transport.start(
                self._worker_exec_command,
                lima_home=self._lima_home,
                cwd=self._lima_home,
            )
        except VMStrategyError:
            raise
        except BaseException as exc:
            raise VMStrategyUnavailable("worker_exec_start_failed") from exc
        self._selector = selectors.DefaultSelector()
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._selector.register(self._process.stdin, selectors.EVENT_WRITE, "stdin")
        self._selector.register(self._process.stdout, selectors.EVENT_READ, "stdout")
        self._selector.register(self._process.stderr, selectors.EVENT_READ, "stderr")

    def _guest_nerdctl(self, *args: str) -> list[str]:
        """Run nerdctl only inside the explicitly selected Lima instance."""
        limactl = self._limactl_path
        if not isinstance(limactl, str) or not limactl.startswith("/"):
            raise VMStrategyUnavailable("limactl_path_unavailable")
        if any(type(arg) is not str or not arg or "\x00" in arg for arg in args):
            raise VMStrategyUnavailable("guest_command_invalid")
        return [
            limactl, "shell", "--workdir", "/", self._runtime.instance, "nerdctl", *args
        ]

    def _build_container_command(self) -> list[str]:
        name = self._container_name
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise VMStrategyUnavailable("container_name_invalid")
        owner = self._owner_token
        return [
            *self._guest_nerdctl("create"), "--interactive", "--rm",
            "--pull=never",
            "--log-driver", "none",
            "--name", name,
            "--label", f"{OWNER_LABEL}={owner}",
            "--network", "none",
            "--read-only",
            "--user", "65532:65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", "1",
            "--memory", "256m",
            "--memory-swap", "256m",
            "--pids-limit", "32",
            "--cgroupns", "private",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--ulimit", "nofile=64:64",
            "--entrypoint", "python",
            _image_inspect_argument(self._runtime.immutable_image_ref),
            "-I", "-B", "-c", _CONTAINER_HOLDER_SOURCE,
        ]

    def _build_container_start_command(self) -> list[str]:
        # PID 1 is a fixed trusted 190-second holder for the ordinary/orphan
        # path, not a hard TTL against same-UID untrusted code. The worker runs
        # separately via exec inside this same container.
        return self._guest_nerdctl("start", self._container_name)

    def _build_worker_exec_command(self) -> list[str]:
        return self._guest_nerdctl(
            "exec", "--interactive", "--user", "65532:65532", self._container_name,
            "python", "-I", "-B", "-u", "/opt/quant/worker.py",
        )

    def _wait_for_container_running(self) -> None:
        deadline = time.monotonic() + min(
            CONTAINER_READINESS_TIMEOUT, self._remaining_session()
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VMStrategyUnavailable("container_readiness_timeout")
            result = self._run(
                self._guest_nerdctl(
                    "container", "inspect", "--format", "{{json .State.Running}}",
                    self._container_name,
                ),
                timeout=min(UTILITY_TIMEOUT, remaining),
                session_bounded=True,
            )
            if result.returncode != 0:
                raise VMStrategyUnavailable("container_readiness_failed")
            try:
                running = _strict_json_loads(result.stdout.strip())
            except _ProtocolError as exc:
                raise VMStrategyUnavailable("container_readiness_invalid") from exc
            if type(running) is not bool:
                raise VMStrategyUnavailable("container_readiness_invalid")
            if running:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VMStrategyUnavailable("container_readiness_timeout")
            time.sleep(min(CONTAINER_READINESS_INTERVAL, remaining))

    def _inspect_container(
        self, *, session_bounded: bool = False
    ) -> tuple[bool, dict[str, Any] | None]:
        result = self._run(
            self._guest_nerdctl(
                "container", "inspect", "--format", "{{json .Config.Labels}}", self._container_name
            ),
            session_bounded=session_bounded,
        )
        if result.returncode == 0:
            try:
                labels = _strict_json_loads(result.stdout.strip())
            except _ProtocolError as exc:
                raise VMStrategyUnavailable("container_inspect_invalid") from exc
            if type(labels) is not dict:
                raise VMStrategyUnavailable("container_inspect_invalid")
            return True, labels
        if self._known_not_found(result):
            return False, None
        raise VMStrategyUnavailable("container_inspect_failed")

    @staticmethod
    def _known_not_found(result: _CommandResult) -> bool:
        if result.returncode == 0:
            return False
        # Only fixed, non-sensitive diagnostic phrases are inspected.  The
        # raw text is never included in errors, logs, or caller-visible output.
        try:
            message = result.stderr.decode("utf-8", "strict").lower()
        except UnicodeDecodeError:
            return False
        return any(phrase in message for phrase in ("no such container", "container not found"))

    def _assert_name_available(self, *, session_bounded: bool = False) -> None:
        exists, _labels = self._inspect_container(session_bounded=session_bounded)
        if exists:
            raise VMStrategyUnavailable("container_name_collision")

    def _create_quarantine_lease(self) -> None:
        marker = self._lima_home / ".strategy-session-quarantine"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        payload = ("active:" + self._owner_token + "\n").encode("ascii")
        try:
            descriptor = os.open(marker, flags, 0o600)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._lease_marker_owned = True
        except FileExistsError as exc:
            raise VMStrategyUnavailable("runtime_quarantined") from exc
        except OSError as exc:
            raise VMStrategyUnavailable("runtime_lease_failed") from exc

    def _source_frame(self) -> bytes:
        if type(self._source) is not bytes:
            raise VMStrategyUnavailable("source_type_invalid")
        if len(self._source) > MAX_SOURCE_BYTES:
            raise VMStrategyUnavailable("source_too_large")
        try:
            source_text = self._source.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise VMStrategyUnavailable("source_encoding_invalid") from exc
        request_id = uuid.uuid4().hex
        try:
            frame = _strict_json_bytes(
                {"request_id": request_id, "source": source_text},
                maximum=MAX_INPUT_LINE_BYTES,
            )
        except _ProtocolFailure as exc:
            code = "source_frame_too_large" if exc.code == "frame_too_large" else "source_frame_invalid"
            raise VMStrategyUnavailable(code) from exc
        self._startup_request_id = request_id
        return frame

    def _send_startup_source(self) -> None:
        frame = self._source_frame()
        try:
            line = self._roundtrip(frame, timeout=min(STARTUP_TIMEOUT, self._remaining_session()))
        finally:
            self._source = b""
            del frame
        success, response = _parse_worker_response(line, self._startup_request_id)
        if not success:
            raise VMStrategyUnavailable("worker_startup_rejected")
        if (
            type(response) is not dict
            or set(response) != {"ready"}
            or type(response["ready"]) is not bool
            or response["ready"] is not True
        ):
            raise _ProtocolFailure("worker_ready_invalid")

    def _remaining_session(self) -> float:
        remaining = self._session_deadline - time.monotonic()
        if remaining <= 0:
            raise VMStrategyUnavailable("session_timeout")
        return remaining

    def _arm_session_watchdog(self) -> None:
        remaining = self._remaining_session()
        timer = threading.Timer(remaining, self._expire_session)
        timer.daemon = True
        self._watchdog_timer = timer
        timer.start()

    def _expire_session(self) -> None:
        try:
            self.close()
        except VMStrategyError:
            # The close error is retained on the session and quarantine marker.
            pass

    def _check_cancelled(self, cancelled: Callable[[], bool] | None) -> None:
        if cancelled is None:
            return
        if not callable(cancelled):
            raise VMStrategyUnavailable("cancel_callback_invalid")
        try:
            stopped = cancelled()
        except BaseException as exc:
            raise VMStrategyUnavailable("cancel_callback_failed") from exc
        if type(stopped) is not bool:
            raise VMStrategyUnavailable("cancel_callback_invalid")
        if stopped:
            raise VMStrategyUnavailable("cancelled")

    def _roundtrip(
        self,
        frame: bytes,
        *,
        timeout: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> bytes:
        # Keep local references for the exchange. The watchdog may close and
        # clear the session fields concurrently; that must become a fixed
        # session_closed error rather than leaking raw descriptor exceptions.
        process = self._process
        selector = self._selector
        if self._closed or process is None or selector is None:
            raise VMStrategyUnavailable("session_closed")
        stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
        if stdin is None or stdout is None or stderr is None:
            raise VMStrategyUnavailable("session_closed")
        try:
            return self._roundtrip_with_resources(
                frame, timeout=timeout, cancelled=cancelled,
                process=process, selector=selector, stdin=stdin, stdout=stdout, stderr=stderr,
            )
        except VMStrategyError as exc:
            if self._stderr_overflow:
                raise VMStrategyUnavailable("worker_stderr_limit") from exc
            if self._stderr_io_error:
                raise VMStrategyUnavailable("worker_stderr_unverified") from exc
            if self._closed:
                raise VMStrategyUnavailable("session_closed") from exc
            raise
        except (OSError, ValueError, AttributeError, KeyError) as exc:
            if self._stderr_overflow:
                raise VMStrategyUnavailable("worker_stderr_limit") from exc
            if self._stderr_io_error:
                raise VMStrategyUnavailable("worker_stderr_unverified") from exc
            if self._closed:
                raise VMStrategyUnavailable("session_closed") from exc
            raise VMStrategyUnavailable("session_io_failed") from exc

    def _drain_stderr_fd(self, fd: int) -> bool:
        """Drain at most the remaining session budget plus one byte.

        Returns true at EOF. The lock makes this the only serialized reader
        shared by exchange polling and final close-time draining.
        """
        with self._stderr_lock:
            if self._stderr_overflow or self._stderr_io_error:
                return False
            while True:
                remaining = MAX_STDERR_BYTES + 1 - self._stderr_bytes
                if remaining <= 0:
                    self._stderr_overflow = True
                    return False
                try:
                    chunk = os.read(fd, min(4096, remaining))
                except BlockingIOError:
                    return False
                if not chunk:
                    return True
                self._stderr_bytes += len(chunk)
                if self._stderr_bytes > MAX_STDERR_BYTES:
                    self._stderr_overflow = True
                    return False

    def _roundtrip_with_resources(
        self,
        frame: bytes,
        *,
        timeout: float,
        cancelled: Callable[[], bool] | None,
        process: subprocess.Popen[bytes],
        selector: selectors.BaseSelector,
        stdin: Any,
        stdout: Any,
        stderr: Any,
    ) -> bytes:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise VMStrategyUnavailable("exchange_timeout_invalid")
        deadline = min(time.monotonic() + timeout, self._session_deadline)
        try:
            selector.get_key(stdin)
        except KeyError:
            selector.register(stdin, selectors.EVENT_WRITE, "stdin")
        stdin_fd = stdin.fileno()
        stderr_fd = stderr.fileno()
        written = 0
        response_line: bytes | None = None
        while response_line is None:
            self._check_cancelled(cancelled)
            if process.poll() is not None:
                raise VMStrategyUnavailable("worker_exited")
            remaining = min(deadline - time.monotonic(), self._remaining_session())
            if remaining <= 0:
                raise VMStrategyUnavailable("exchange_timeout")
            events = selector.select(min(remaining, 0.05))
            for key, _mask in events:
                stream_kind = key.data
                fd = key.fd
                if stream_kind == "stdin":
                    try:
                        count = os.write(fd, frame[written:written + 16384])
                    except (BrokenPipeError, OSError) as exc:
                        raise VMStrategyUnavailable("worker_stdin_failed") from exc
                    if count <= 0:
                        raise VMStrategyUnavailable("worker_stdin_failed")
                    written += count
                    if written == len(frame):
                        selector.unregister(key.fileobj)
                elif stream_kind == "stdout":
                    if response_line is not None:
                        raise _ProtocolFailure("multiple_or_trailing_responses")
                    try:
                        chunk = os.read(fd, MAX_RESPONSE_BYTES + 1)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise VMStrategyUnavailable("worker_stdout_closed")
                    self._stdout_buffer.extend(chunk)
                    if len(self._stdout_buffer) > MAX_RESPONSE_BYTES:
                        raise _ProtocolFailure("response_frame_too_large")
                    newline = self._stdout_buffer.find(b"\n")
                    if newline >= 0:
                        if newline != len(self._stdout_buffer) - 1:
                            raise _ProtocolFailure("multiple_or_trailing_responses")
                        response_line = bytes(self._stdout_buffer)
                        self._stdout_buffer.clear()
                elif stream_kind == "stderr":
                    if self._drain_stderr_fd(fd):
                        try:
                            selector.unregister(key.fileobj)
                        except KeyError:
                            pass
            # Do not return on stdout until all ready stderr bytes in this
            # event batch and any newly queued bytes have been checked.
            if self._drain_stderr_fd(stderr_fd):
                try:
                    selector.unregister(stderr)
                except KeyError:
                    pass
            if self._stderr_overflow:
                raise VMStrategyUnavailable("worker_stderr_limit")
            if self._stderr_io_error:
                raise VMStrategyUnavailable("worker_stderr_unverified")
        if process.poll() is not None:
            raise VMStrategyUnavailable("worker_exited")
        # The request stream stays open for the next callback.
        return response_line

    def exchange(
        self,
        payload: Any,
        *,
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT,
        cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        try:
            self._remaining_session()
            self._check_cancelled(cancelled)
            request_id = uuid.uuid4().hex
            frame = _strict_json_bytes(
                {"request_id": request_id, "payload": payload},
                maximum=MAX_INPUT_LINE_BYTES,
            )
            response_line = self._roundtrip(frame, timeout=timeout, cancelled=cancelled)
            success, response = _parse_worker_response(response_line, request_id)
            if not success:
                raise VMStrategyError(response)
            return response
        except BaseException:
            try:
                self.close()
            except VMStrategyError:
                pass
            raise

    def _write_quarantine(self) -> bool:
        marker = self._lima_home / ".strategy-session-quarantine"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
            try:
                os.write(descriptor, b"cleanup_unverified\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return True
        except FileExistsError:
            try:
                marker.lstat()
                return True
            except OSError:
                return False
        except OSError:
            try:
                marker.lstat()
                return True
            except OSError:
                return False

    def _remove_owned_lease(self) -> bool:
        if not self._lease_marker_owned:
            return True
        marker = self._lima_home / ".strategy-session-quarantine"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    return False
                content = os.read(descriptor, 256)
            finally:
                os.close(descriptor)
            expected = ("active:" + self._owner_token + "\n").encode("ascii")
            if content != expected:
                return False
            marker.unlink()
            self._lease_marker_owned = False
            return True
        except OSError:
            return False

    def _reap_host_process(self) -> subprocess.Popen[bytes] | None:
        process = self._process
        if process is None:
            return None
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired as exc:
                    raise VMStrategyUnavailable("child_reap_failed") from exc
        self._process = None
        return process

    def _cleanup_container(self) -> bool:
        if not self._container_create_attempted:
            return True
        try:
            exists, labels = self._inspect_container()
            if exists:
                assert labels is not None
                if labels.get(OWNER_LABEL) != self._owner_token:
                    return False
                removed = self._run(self._guest_nerdctl("rm", "--force", self._container_name))
                if removed.returncode != 0:
                    return False
                exists, _labels = self._inspect_container()
                if exists:
                    return False
            # Even a currently absent name is not proof that an unacknowledged
            # create request cannot complete after this inspection.
            return not self._container_create_uncertain
        except BaseException:
            return False

    def _release_lock(self) -> None:
        descriptor = self._lock_fd
        self._lock_fd = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                if self._close_error_code is not None:
                    raise VMStrategyUnavailable(self._close_error_code)
                return
            self._closed = True
            timer = self._watchdog_timer
            self._watchdog_timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if self._selector is not None:
                self._selector.close()
                self._selector = None
            reap_ok = True
            process = self._process
            try:
                reaped = self._reap_host_process()
                if reaped is not None:
                    process = reaped
            except VMStrategyError:
                reap_ok = False
            # Session deadlines do not cancel container removal/verification;
            # those cleanup utilities retain their separate bounded grace.
            cleanup_ok = self._cleanup_container()
            if process is not None:
                try:
                    if process.stderr is not None:
                        self._drain_stderr_fd(process.stderr.fileno())
                except (OSError, ValueError, AttributeError):
                    with self._stderr_lock:
                        self._stderr_io_error = True
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                    self._process = None
            lease_ok = self._remove_owned_lease() if reap_ok and cleanup_ok else False
            cleanup_required = self._container_create_attempted or self._lease_marker_owned
            if cleanup_required and (not reap_ok or not cleanup_ok or not lease_ok):
                quarantine_ok = self._write_quarantine()
                self._close_error_code = "cleanup_unverified"
                if quarantine_ok:
                    self._release_lock()
                # If the quarantine marker cannot be persisted, intentionally
                # retain the flock descriptor so this host process cannot start
                # another session while cleanup remains uncertain.
                raise VMStrategyUnavailable(self._close_error_code)
            if self._stderr_io_error:
                self._close_error_code = "worker_stderr_unverified"
                self._release_lock()
                raise VMStrategyUnavailable(self._close_error_code)
            if self._stderr_overflow:
                self._close_error_code = "worker_stderr_limit"
                self._release_lock()
                raise VMStrategyUnavailable(self._close_error_code)
            self._release_lock()

    def __enter__(self) -> "_VMStrategySessionCore":
        if self._closed:
            raise VMStrategyUnavailable("session_closed")
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class VMStrategySession:
    """Persistent strategy session backed only by the verified Lima runtime.

    The public constructor intentionally rejects a missing runtime or a
    non-macOS host before reading or validating ``source``.  It has no
    host-execution option and performs no runtime auto-discovery.
    """

    def __init__(
        self,
        source: bytes,
        runtime: VMRuntimeSpec | None = None,
        *,
        session_timeout: float = DEFAULT_SESSION_TIMEOUT,
    ) -> None:
        if runtime is None:
            raise VMStrategyUnavailable("runtime_not_verified")
        if sys.platform != "darwin":
            raise VMStrategyUnavailable("unsupported_platform")
        if type(runtime) is not VMRuntimeSpec:
            raise VMStrategyUnavailable("runtime_spec_invalid")
        if runtime.lima_home != DEFAULT_LIMA_HOME or runtime.instance != DEFAULT_INSTANCE:
            raise VMStrategyUnavailable("runtime_spec_invalid")
        self._core = _VMStrategySessionCore(
            source,
            runtime,
            session_timeout=session_timeout,
        )

    def exchange(
        self,
        payload: Any,
        *,
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT,
        cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        return self._core.exchange(payload, timeout=timeout, cancelled=cancelled)

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "VMStrategySession":
        if self._core._closed:
            raise VMStrategyUnavailable("session_closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._core.__exit__(exc_type, exc, traceback)
