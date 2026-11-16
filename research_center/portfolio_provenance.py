"""Bounded, read-only source and runtime provenance for portfolio runs.

The snapshot deliberately covers a fixed source allowlist. It is an integrity
record for accidental drift, not an authenticated signature or a sandbox for
executing archived Python.
"""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
from typing import Any


SCHEMA_VERSION = 1
SOURCE_FILES = (
    "research_center/__init__.py",
    "research_center/portfolio_kernel.py",
    "research_center/portfolio_data.py",
    "research_center/portfolio_cli.py",
    "research_center/portfolio_provenance.py",
    "research_center/engine.py",
    "research_center/replay_kernel.py",
    "research_center/strategies.py",
    "research_center/data_evidence.py",
    "research_center/store.py",
    "data/__init__.py",
    "data/joinquant_minute.py",
    "strategy/__init__.py",
    "strategy/base.py",
)

MAX_SOURCE_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SERIALIZED_SNAPSHOT_BYTES = 8 * 1024 * 1024

_RUNTIME_FIELDS = (
    "python_implementation",
    "python_version",
    "pandas_version",
    "numpy_version",
    "pyarrow_version",
    "platform_system",
    "platform_architecture",
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}$")
_PYTHON_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+!-]{0,32})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PortfolioProvenanceError(ValueError):
    """Fixed-message error for invalid or unavailable provenance inputs."""


def _fail(message: str) -> None:
    raise PortfolioProvenanceError(message)


def _coerce_root(root: Path) -> Path:
    if not isinstance(root, (str, os.PathLike)):
        _fail("源码目录无效")
    try:
        return Path(root)
    except (OSError, TypeError, ValueError):
        _fail("源码目录无效")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        _fail("源码快照格式无效")


def _serialized_snapshot(snapshot: dict[str, Any]) -> bytes:
    """Measure the exact indented JSON representation written by the CLI."""
    try:
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        _fail("源码快照格式无效")


def _runtime_metadata() -> dict[str, str]:
    """Return only generic runtime versions; never include host paths or IDs."""
    try:
        return {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "pandas_version": metadata.version("pandas"),
            "numpy_version": metadata.version("numpy"),
            "pyarrow_version": metadata.version("pyarrow"),
            "platform_system": platform.system(),
            "platform_architecture": platform.machine(),
        }
    except Exception:
        _fail("运行环境信息不可用")


def _validate_runtime(runtime: Any) -> None:
    if type(runtime) is not dict or set(runtime) != set(_RUNTIME_FIELDS):
        _fail("运行环境信息无效")
    for field in _RUNTIME_FIELDS:
        value = runtime[field]
        if type(value) is not str or not value or len(value) > 64:
            _fail("运行环境信息无效")
        if not _SAFE_TOKEN.fullmatch(value):
            _fail("运行环境信息无效")
    if not _PYTHON_VERSION.fullmatch(runtime["python_version"]):
        _fail("运行环境信息无效")


def _source_version(file_hashes: dict[str, str], runtime: dict[str, str]) -> str:
    payload = {
        "files": {path: file_hashes[path] for path in sorted(file_hashes)},
        "runtime": runtime,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _open_root(root: Path) -> int:
    try:
        raw_path = os.fspath(root)
        if not isinstance(raw_path, str) or not raw_path:
            _fail("源码目录无效")
        if ".." in Path(raw_path).parts:
            _fail("源码目录无效")
        absolute = os.path.abspath(raw_path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None or not hasattr(os, "O_DIRECTORY"):
            _fail("当前平台不支持安全源码读取")
        fd = os.open(absolute, flags | nofollow)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            _fail("源码目录无效")
        return fd
    except PortfolioProvenanceError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("源码目录无效")


def _read_source(root_fd: int, relative_path: str) -> bytes:
    """Read one allowlisted file using no-follow directory-relative opens."""
    directory_fd = -1
    try:
        directory_fd = os.dup(root_fd)
        parts = relative_path.split("/")
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                _fail("源码文件类型无效")
            if file_stat.st_size > MAX_SOURCE_FILE_BYTES:
                _fail("源码文件大小超限")
            with os.fdopen(file_fd, "rb", closefd=True) as source_file:
                file_fd = -1
                data = source_file.read(MAX_SOURCE_FILE_BYTES + 1)
            if len(data) > MAX_SOURCE_FILE_BYTES:
                _fail("源码文件大小超限")
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _fail("源码文件编码无效")
            return data
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    except PortfolioProvenanceError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("源码文件缺失或不可读")
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _snapshot_sources(root: Path, runtime: dict[str, str]) -> dict[str, Any]:
    _validate_runtime(runtime)
    try:
        root_fd = _open_root(root)
    except (OSError, TypeError):
        _fail("源码目录无效")
    files: dict[str, dict[str, str]] = {}
    file_hashes: dict[str, str] = {}
    total_size = 0
    try:
        for relative_path in SOURCE_FILES:
            content = _read_source(root_fd, relative_path)
            total_size += len(content)
            if total_size > MAX_TOTAL_SOURCE_BYTES:
                _fail("源码总大小超限")
            try:
                source_text = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _fail("源码文件编码无效")
            digest = hashlib.sha256(content).hexdigest()
            files[relative_path] = {"sha256": digest, "source": source_text}
            file_hashes[relative_path] = digest
    finally:
        os.close(root_fd)

    result = {
        "schema_version": SCHEMA_VERSION,
        "source_version": _source_version(file_hashes, runtime),
        "runtime": dict(runtime),
        "files": files,
    }
    # Keep the public return value restricted to strict JSON values.
    if len(_serialized_snapshot(result)) > MAX_SERIALIZED_SNAPSHOT_BYTES:
        _fail("源码快照序列化大小超限")
    return result


def snapshot_sources(root: Path) -> dict[str, Any]:
    """Capture the fixed source allowlist and generic runtime metadata in memory."""
    runtime = _runtime_metadata()
    return _snapshot_sources(_coerce_root(root), runtime)


def validate_snapshot(snapshot: Any) -> None:
    """Reject malformed, modified, oversized, or non-allowlisted snapshots."""
    if type(snapshot) is not dict or set(snapshot) != {
        "schema_version",
        "source_version",
        "runtime",
        "files",
    }:
        _fail("源码快照格式无效")
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != SCHEMA_VERSION:
        _fail("源码快照版本无效")
    source_version = snapshot["source_version"]
    if type(source_version) is not str or not _SHA256.fullmatch(source_version):
        _fail("源码快照版本无效")
    runtime = snapshot["runtime"]
    _validate_runtime(runtime)

    files = snapshot["files"]
    if type(files) is not dict or set(files) != set(SOURCE_FILES):
        _fail("源码快照文件清单无效")
    file_hashes: dict[str, str] = {}
    total_size = 0
    for relative_path in SOURCE_FILES:
        entry = files[relative_path]
        if type(entry) is not dict or set(entry) != {"sha256", "source"}:
            _fail("源码快照文件记录无效")
        digest = entry["sha256"]
        source_text = entry["source"]
        if type(digest) is not str or not _SHA256.fullmatch(digest):
            _fail("源码快照哈希无效")
        if type(source_text) is not str:
            _fail("源码快照正文无效")
        try:
            source_bytes = source_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _fail("源码快照正文编码无效")
        if len(source_bytes) > MAX_SOURCE_FILE_BYTES:
            _fail("源码文件大小超限")
        total_size += len(source_bytes)
        if total_size > MAX_TOTAL_SOURCE_BYTES:
            _fail("源码总大小超限")
        actual_digest = hashlib.sha256(source_bytes).hexdigest()
        if actual_digest != digest:
            _fail("源码快照正文与哈希不一致")
        file_hashes[relative_path] = digest

    if _source_version(file_hashes, runtime) != source_version:
        _fail("源码快照版本校验失败")
    if len(_serialized_snapshot(snapshot)) > MAX_SERIALIZED_SNAPSHOT_BYTES:
        _fail("源码快照序列化大小超限")


def verify_sources(root: Path, snapshot: Any, check_runtime: bool = True) -> None:
    """Verify current allowlisted files against a validated in-memory snapshot."""
    if type(check_runtime) is not bool:
        _fail("运行环境校验选项无效")
    validate_snapshot(snapshot)
    current_runtime = snapshot["runtime"]
    if check_runtime:
        current_runtime = _runtime_metadata()
        _validate_runtime(current_runtime)

    current = _snapshot_sources(_coerce_root(root), current_runtime)
    for relative_path in SOURCE_FILES:
        if current["files"][relative_path]["sha256"] != snapshot["files"][relative_path]["sha256"]:
            _fail("当前源码与快照不一致")
    if check_runtime and current["source_version"] != snapshot["source_version"]:
        _fail("当前运行环境与快照不一致")
