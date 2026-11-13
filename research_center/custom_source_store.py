"""Immutable, syntax-only storage for user supplied strategy source.

Registration parses source with :func:`ast.parse` only. It never compiles,
imports, evaluates, or executes user code. A successful registration proves
only UTF-8 validity, Python syntax, and the presence of one top-level
``build_strategy`` function. Runtime interface and dependency checks belong to
the isolated strategy runner and remain explicitly unverified here.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import unicodedata
from typing import Any, Iterator
import uuid


SCHEMA_VERSION = 1
INTERFACE_CONTRACT = "strategy-base-json-state-v1"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "results" / "research_center" / "custom_sources"

MAX_SOURCE_BYTES = 64 * 1024
MAX_PARAMS_BYTES = 16 * 1024
MAX_PARAMS_DEPTH = 8
MAX_PARAMS_NODES = 512
MAX_PARAM_STRING_CHARS = 4096
MAX_PARAM_KEY_CHARS = 256
MAX_NAME_CHARS = 120
MAX_NAME_BYTES = 256
MAX_DESCRIPTION_CHARS = 1000
MAX_DESCRIPTION_BYTES = 4096
MAX_IMPORT_ROOTS = 512
MAX_IMPORT_ROOT_CHARS = 8192
MAX_RECORD_BYTES = 64 * 1024
MAX_DIRECTORY_ENTRIES = 1000
MAX_LIST_RECORDS = 100

_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_CREDENTIAL_PARAM_KEYS = frozenset(
    {"password", "passwd", "secret", "api_key", "api_token", "access_token",
     "refresh_token", "auth_token", "client_secret", "private_key", "authorization"}
)
_CREDENTIAL_KEY_FORMS = frozenset(
    ''.join(character for character in key.casefold() if character.isalnum())
    for key in _CREDENTIAL_PARAM_KEYS
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "registration_id",
        "created_at",
        "interface_contract",
        "name",
        "description",
        "default_params",
        "parent_id",
        "source_sha256",
        "code_version",
        "declared_import_roots",
        "import_declaration_status",
        "runtime_interface_verified",
        "dependencies_verified",
        "mode",
        "paper_admission",
        "record_sha256",
    }
)

_ERROR_MESSAGES = {
    "source_type": "source 必须是 bytes。",
    "source_too_large": "source 超过 64 KiB 上限。",
    "source_encoding": "source 必须是严格 UTF-8 编码。",
    "source_syntax": "source 存在 Python 语法错误。",
    "source_entrypoint": "source 必须且只能声明一个顶层同步 build_strategy 函数。",
    "source_analysis_limit": "source 的静态导入清单超过上限。",
    "metadata_invalid": "name 或 description 不符合元数据限制。",
    "params_invalid": "default_params 必须是有界的内置 JSON 数据。",
    "params_too_large": "default_params 超过 JSON 大小上限。",
    "credential_param": "default_params 不得包含凭据字段。",
    "invalid_id": "source ID 格式无效。",
    "parent_not_found": "parent_id 指向的 source 不存在或不可验证。",
    "record_not_found": "source 记录不存在。",
    "record_corrupt": "source 记录损坏或不可信。",
    "store_unavailable": "source 存储不可安全访问。",
}


class CustomSourceError(ValueError):
    """Fixed-code error that never includes user supplied source text."""

    def __init__(self, code: str, *, lineno: int | None = None, offset: int | None = None):
        self.code = code
        self.lineno = lineno if type(lineno) is int and lineno > 0 else None
        self.offset = offset if type(offset) is int and offset > 0 else None
        super().__init__(_ERROR_MESSAGES.get(code, _ERROR_MESSAGES["record_corrupt"]))


class _ParamsValidationError(Exception):
    def __init__(self, code: str):
        self.code = code


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")


def _validate_plain_text(value: Any, *, max_chars: int, max_bytes: int) -> bool:
    if type(value) is not str or not value or len(value) > max_chars:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    if len(encoded) > max_bytes:
        return False
    return not any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _validate_metadata(name: Any, description: Any) -> None:
    if not _validate_plain_text(name, max_chars=MAX_NAME_CHARS, max_bytes=MAX_NAME_BYTES):
        raise CustomSourceError("metadata_invalid") from None
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise CustomSourceError("metadata_invalid") from None
    if type(description) is not str or len(description) > MAX_DESCRIPTION_CHARS:
        raise CustomSourceError("metadata_invalid") from None
    try:
        encoded = description.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise CustomSourceError("metadata_invalid") from None
    if len(encoded) > MAX_DESCRIPTION_BYTES or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in description
    ):
        raise CustomSourceError("metadata_invalid") from None
    if description and (Path(description).is_absolute() or PureWindowsPath(description).is_absolute()):
        raise CustomSourceError("metadata_invalid") from None


def _validate_params(value: Any) -> bytes:
    if type(value) is not dict:
        raise _ParamsValidationError("params_invalid")

    nodes = 0
    total_string_chars = 0
    seen_containers: set[int] = set()
    pending: list[tuple[Any, int]] = [(value, 0)]

    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_PARAMS_NODES:
            raise _ParamsValidationError("params_invalid")
        if depth > MAX_PARAMS_DEPTH:
            raise _ParamsValidationError("params_invalid")

        item_type = type(item)
        if item is None or item_type is bool:
            continue
        if item_type is int:
            if item.bit_length() > 4096:
                raise _ParamsValidationError("params_too_large")
            continue
        if item_type is float:
            if not math.isfinite(item):
                raise _ParamsValidationError("params_invalid")
            continue
        if item_type is str:
            if len(item) > MAX_PARAM_STRING_CHARS:
                raise _ParamsValidationError("params_too_large")
            total_string_chars += len(item)
            if total_string_chars > MAX_PARAMS_BYTES:
                raise _ParamsValidationError("params_too_large")
            try:
                item.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise _ParamsValidationError("params_invalid") from None
            continue
        if item_type not in (dict, list):
            raise _ParamsValidationError("params_invalid")

        identity = id(item)
        if identity in seen_containers:
            raise _ParamsValidationError("params_invalid")
        seen_containers.add(identity)

        if item_type is list:
            if nodes + len(item) > MAX_PARAMS_NODES:
                raise _ParamsValidationError("params_invalid")
            pending.extend((child, depth + 1) for child in reversed(item))
            continue

        if nodes + 2 * len(item) > MAX_PARAMS_NODES:
            raise _ParamsValidationError("params_invalid")
        children: list[tuple[Any, int]] = []
        for key, child in item.items():
            if type(key) is not str or len(key) > MAX_PARAM_KEY_CHARS:
                raise _ParamsValidationError("params_invalid")
            folded_key = ''.join(character for character in key.casefold() if character.isalnum())
            if folded_key in _CREDENTIAL_KEY_FORMS:
                raise _ParamsValidationError("credential_param")
            total_string_chars += len(key)
            if total_string_chars > MAX_PARAMS_BYTES:
                raise _ParamsValidationError("params_too_large")
            try:
                key.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise _ParamsValidationError("params_invalid") from None
            nodes += 1  # Count mapping keys as JSON nodes as well.
            children.append((child, depth + 1))
        pending.extend(reversed(children))

    try:
        encoded = _canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise _ParamsValidationError("params_invalid") from None
    if len(encoded) > MAX_PARAMS_BYTES:
        raise _ParamsValidationError("params_too_large")
    return encoded


def _analyze_source(source: bytes) -> tuple[str, list[str]]:
    if type(source) is not bytes:
        raise CustomSourceError("source_type") from None
    if len(source) > MAX_SOURCE_BYTES:
        raise CustomSourceError("source_too_large") from None
    try:
        text = source.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        raise CustomSourceError("source_encoding") from None

    parse_failed = False
    syntax_lineno: int | None = None
    syntax_offset: int | None = None
    try:
        tree = ast.parse(text, filename="<custom-strategy>", mode="exec")
    except SyntaxError as exc:
        parse_failed = True
        syntax_lineno, syntax_offset = exc.lineno, exc.offset
    except (ValueError, TypeError, RecursionError, MemoryError):
        parse_failed = True
    if parse_failed:
        # Raise after leaving the SyntaxError handler so its source excerpt is
        # not retained as this public exception's context.
        raise CustomSourceError("source_syntax", lineno=syntax_lineno, offset=syntax_offset) from None

    entrypoint_count = sum(
        1
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == "build_strategy"
    )
    if entrypoint_count != 1:
        raise CustomSourceError("source_entrypoint") from None

    import_roots: set[str] = set()
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_roots.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    import_roots.add(node.module.split(".", 1)[0])
                elif node.level:
                    import_roots.add(".")
            if len(import_roots) > MAX_IMPORT_ROOTS:
                raise CustomSourceError("source_analysis_limit")
    except (RecursionError, MemoryError):
        raise CustomSourceError("source_analysis_limit") from None

    roots = sorted(import_roots)
    if sum(len(root) for root in roots) > MAX_IMPORT_ROOT_CHARS:
        raise CustomSourceError("source_analysis_limit")
    return text, roots


def _identity_payload(
    *,
    name: str,
    description: str,
    default_params: dict[str, Any],
    parent_id: str | None,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "interface_contract": INTERFACE_CONTRACT,
        "name": name,
        "description": description,
        "default_params": default_params,
        "parent_id": parent_id,
        "source_sha256": source_sha256,
    }


def _record_bytes(record: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise CustomSourceError("record_corrupt") from None
    if len(payload) > MAX_RECORD_BYTES:
        raise CustomSourceError("source_analysis_limit") from None
    return payload


def _reject_json_constant(_value: str):
    raise ValueError("invalid constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_record(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError, OverflowError, MemoryError):
        raise CustomSourceError("record_corrupt") from None
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise CustomSourceError("record_corrupt") from None
    return value


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
    )


def _check_private_stat(info: os.stat_result, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected_type or stat.S_IMODE(info.st_mode) & 0o077:
        raise CustomSourceError("record_corrupt") from None
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise CustomSourceError("record_corrupt") from None


def _read_fd_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(16 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise CustomSourceError("record_corrupt") from None
    return b"".join(chunks)


def _read_private_file(directory_fd: int, name: str, limit: int) -> bytes:
    # A FIFO must not block open() before fstat can reject its file type.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise CustomSourceError("record_corrupt") from None
    try:
        before = os.fstat(fd)
        _check_private_stat(before, directory=False)
        if before.st_size > limit:
            raise CustomSourceError("record_corrupt") from None
        raw = _read_fd_bounded(fd, limit)
        after = os.fstat(fd)
        if _stat_signature(before) != _stat_signature(after) or len(raw) != before.st_size:
            raise CustomSourceError("record_corrupt") from None
        return raw
    except OSError:
        raise CustomSourceError("record_corrupt") from None
    finally:
        os.close(fd)


def _open_private_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise CustomSourceError("record_not_found") from None
    except OSError:
        raise CustomSourceError("record_corrupt") from None
    try:
        _check_private_stat(os.fstat(fd), directory=True)
        return fd
    except Exception:
        os.close(fd)
        raise


class CustomSourceStore:
    """Immutable source archive with bounded, no-follow reads.

    Construction and all read methods are read-only. The root is created only
    by :meth:`register`; an absent root therefore lists as a known empty store
    without creating directories or files.
    """

    def __init__(self, root: os.PathLike[str] | str = DEFAULT_ROOT):
        try:
            self.root = Path(os.path.abspath(os.fspath(root)))
        except (TypeError, ValueError, OSError):
            raise CustomSourceError("store_unavailable") from None

    def _open_root(self, *, create: bool) -> int | None:
        if create:
            try:
                if not os.path.lexists(self.root):
                    self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            except OSError:
                raise CustomSourceError("store_unavailable") from None

        try:
            before_path = os.lstat(self.root)
        except FileNotFoundError:
            if create:
                raise CustomSourceError("store_unavailable") from None
            return None
        except OSError:
            raise CustomSourceError("store_unavailable") from None

        if stat.S_ISLNK(before_path.st_mode):
            raise CustomSourceError("store_unavailable") from None
        try:
            _check_private_stat(before_path, directory=True)
        except CustomSourceError:
            raise CustomSourceError("store_unavailable") from None

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.root, flags)
            after_fd = os.fstat(fd)
        except OSError:
            raise CustomSourceError("store_unavailable") from None
        if _stat_signature(before_path) != _stat_signature(after_fd):
            os.close(fd)
            raise CustomSourceError("store_unavailable") from None
        return fd

    @contextmanager
    def _locked_root(self) -> Iterator[int]:
        root_fd = self._open_root(create=True)
        assert root_fd is not None
        lock_fd: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                lock_fd = os.open(".lock", flags, 0o600, dir_fd=root_fd)
                _check_private_stat(os.fstat(lock_fd), directory=False)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError:
                raise CustomSourceError("store_unavailable") from None
            yield root_fd
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(root_fd)

    @staticmethod
    def _validate_id(value: Any) -> str:
        if type(value) is not str or not _ID_RE.fullmatch(value):
            raise CustomSourceError("invalid_id") from None
        return value

    @staticmethod
    def _load_entry(root_fd: int, registration_id: str) -> tuple[dict[str, Any], bytes]:
        registration_id = CustomSourceStore._validate_id(registration_id)
        entry_fd = _open_private_directory(root_fd, registration_id)
        try:
            raw_record = _read_private_file(entry_fd, "record.json", MAX_RECORD_BYTES)
            source = _read_private_file(entry_fd, "source.py", MAX_SOURCE_BYTES)
        finally:
            os.close(entry_fd)

        record = _parse_record(raw_record)
        try:
            if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
                raise ValueError("schema")
            sealed = {key: value for key, value in record.items() if key != "record_sha256"}
            if record["record_sha256"] != _sha256(_canonical_json_bytes(sealed)):
                raise ValueError("record integrity")
            if record["registration_id"] != registration_id:
                raise ValueError("id")
            if record["interface_contract"] != INTERFACE_CONTRACT:
                raise ValueError("contract")
            if type(record["name"]) is not str or type(record["description"]) is not str:
                raise ValueError("metadata")
            _validate_metadata(record["name"], record["description"])
            if type(record["default_params"]) is not dict:
                raise ValueError("params")
            _validate_params(record["default_params"])
            parent_id = record["parent_id"]
            if parent_id is not None and (type(parent_id) is not str or not _ID_RE.fullmatch(parent_id)):
                raise ValueError("parent")
            source_sha256 = _sha256(source)
            if record["source_sha256"] != source_sha256 or record["code_version"] != source_sha256:
                raise ValueError("source hash")
            identity = _identity_payload(
                name=record["name"],
                description=record["description"],
                default_params=record["default_params"],
                parent_id=parent_id,
                source_sha256=source_sha256,
            )
            if _sha256(_canonical_json_bytes(identity)) != registration_id:
                raise ValueError("identity")
            if record["runtime_interface_verified"] is not False:
                raise ValueError("runtime verification")
            if record["dependencies_verified"] is not False:
                raise ValueError("dependency verification")
            if record["paper_admission"] is not False or record["mode"] != "RESEARCH_ONLY":
                raise ValueError("mode")
            if record["import_declaration_status"] != "DECLARED_NOT_VERIFIED":
                raise ValueError("import status")
            roots = record["declared_import_roots"]
            if type(roots) is not list or any(type(root) is not str for root in roots):
                raise ValueError("imports")
            if roots != sorted(set(roots)) or len(roots) > MAX_IMPORT_ROOTS:
                raise ValueError("imports")
            if sum(len(root) for root in roots) > MAX_IMPORT_ROOT_CHARS:
                raise ValueError("imports")
            created_at = record["created_at"]
            if type(created_at) is not str or len(created_at) > 40:
                raise ValueError("timestamp")
            parsed_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                raise ValueError("timestamp")
        except CustomSourceError:
            raise CustomSourceError("record_corrupt") from None
        except (_ParamsValidationError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            raise CustomSourceError("record_corrupt") from None

        try:
            _text, declared_import_roots = _analyze_source(source)
        except CustomSourceError:
            raise CustomSourceError("record_corrupt") from None
        if declared_import_roots != record["declared_import_roots"]:
            raise CustomSourceError("record_corrupt") from None
        return record, source

    def register(
        self,
        source: bytes,
        *,
        name: str,
        description: str = "",
        default_params: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        _text, import_roots = _analyze_source(source)
        _validate_metadata(name, description)
        if default_params is None:
            normalized_params: dict[str, Any] = {}
        else:
            normalized_params = default_params
        try:
            encoded_params = _validate_params(normalized_params)
            # Validation has bounded this JSON payload to 16 KiB; parse that
            # snapshot back so later caller mutation cannot change the record.
            normalized_params = json.loads(encoded_params.decode("utf-8", "strict"))
        except _ParamsValidationError as exc:
            raise CustomSourceError(exc.code) from None
        except (UnicodeError, ValueError, RecursionError, OverflowError):
            raise CustomSourceError("params_invalid") from None

        if parent_id is not None:
            self._validate_id(parent_id)

        source_sha256 = _sha256(source)
        identity = _identity_payload(
            name=name,
            description=description,
            default_params=normalized_params,
            parent_id=parent_id,
            source_sha256=source_sha256,
        )
        registration_id = _sha256(_canonical_json_bytes(identity))

        with self._locked_root() as root_fd:
            if parent_id is not None:
                try:
                    self._load_entry(root_fd, parent_id)
                except CustomSourceError:
                    raise CustomSourceError("parent_not_found") from None

            try:
                existing_stat = os.stat(registration_id, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing_stat = None
            except OSError:
                raise CustomSourceError("record_corrupt") from None

            if existing_stat is not None:
                existing, _existing_source = self._load_entry(root_fd, registration_id)
                if existing != self._manifest_for(
                    registration_id,
                    source_sha256,
                    name,
                    description,
                    normalized_params,
                    parent_id,
                    import_roots,
                    created_at=existing["created_at"],
                ):
                    raise CustomSourceError("record_corrupt") from None
                return existing

            created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            record = self._manifest_for(
                registration_id,
                source_sha256,
                name,
                description,
                normalized_params,
                parent_id,
                import_roots,
                created_at=created_at,
            )
            record_bytes = _record_bytes(record)
            self._publish(root_fd, registration_id, record_bytes, source)
            return record

    @staticmethod
    def _manifest_for(
        registration_id: str,
        source_sha256: str,
        name: str,
        description: str,
        default_params: dict[str, Any],
        parent_id: str | None,
        import_roots: list[str],
        *,
        created_at: str,
    ) -> dict[str, Any]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "registration_id": registration_id,
            "created_at": created_at,
            "interface_contract": INTERFACE_CONTRACT,
            "name": name,
            "description": description,
            "default_params": default_params,
            "parent_id": parent_id,
            "source_sha256": source_sha256,
            "code_version": source_sha256,
            "declared_import_roots": import_roots,
            "import_declaration_status": "DECLARED_NOT_VERIFIED",
            "runtime_interface_verified": False,
            "dependencies_verified": False,
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
        }
        record["record_sha256"] = _sha256(_canonical_json_bytes(record))
        return record

    @staticmethod
    def _write_private_file(directory_fd: int, name: str, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _publish(self, root_fd: int, registration_id: str, record: bytes, source: bytes) -> None:
        temporary_name = f".tmp-{uuid.uuid4().hex}"
        temporary_fd: int | None = None
        try:
            os.mkdir(temporary_name, 0o700, dir_fd=root_fd)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            temporary_fd = os.open(temporary_name, flags, dir_fd=root_fd)
            _check_private_stat(os.fstat(temporary_fd), directory=True)
            self._write_private_file(temporary_fd, "record.json", record)
            self._write_private_file(temporary_fd, "source.py", source)
            os.fsync(temporary_fd)

            # The root's exclusive flock serializes cooperating writers. The
            # rename publishes the complete directory atomically; no file in
            # an existing immutable record is opened for writing.
            try:
                os.stat(registration_id, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise CustomSourceError("record_corrupt") from None
            os.rename(temporary_name, registration_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        except CustomSourceError:
            raise
        except OSError:
            raise CustomSourceError("store_unavailable") from None
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                temp_fd = _open_private_directory(root_fd, temporary_name)
            except CustomSourceError:
                temp_fd = None
            if temp_fd is not None:
                try:
                    for filename in ("record.json", "source.py"):
                        try:
                            os.unlink(filename, dir_fd=temp_fd)
                        except FileNotFoundError:
                            pass
                    os.close(temp_fd)
                    os.rmdir(temporary_name, dir_fd=root_fd)
                except OSError:
                    try:
                        os.close(temp_fd)
                    except OSError:
                        pass

    def list(self) -> dict[str, Any]:
        root_fd = self._open_root(create=False)
        if root_fd is None:
            return {
                "sources": [],
                "status": "empty",
                "complete": True,
                "empty": True,
                "scanned_entries": 0,
                "scanned_valid_count": 0,
                "corrupt_count": 0,
                "ignored_count": 0,
                "returned_count": 0,
                "scan_limit_reached": False,
                "records_truncated": False,
            }

        retained: list[dict[str, Any]] = []
        scanned_entries = 0
        scanned_valid_count = 0
        corrupt_count = 0
        ignored_count = 0
        scan_limit_reached = False
        try:
            try:
                entries = os.scandir(root_fd)
            except OSError:
                raise CustomSourceError("store_unavailable") from None
            with entries:
                for entry in itertools.islice(entries, MAX_DIRECTORY_ENTRIES):
                    scanned_entries += 1
                    if entry.name == ".lock":
                        continue
                    if not _ID_RE.fullmatch(entry.name):
                        ignored_count += 1
                        continue
                    try:
                        record, _source = self._load_entry(root_fd, entry.name)
                    except CustomSourceError:
                        corrupt_count += 1
                        continue
                    scanned_valid_count += 1
                    retained.append(record)
                    if len(retained) > MAX_LIST_RECORDS:
                        retained.sort(key=lambda item: (item["created_at"], item["registration_id"]), reverse=True)
                        retained.pop()
            scan_limit_reached = scanned_entries >= MAX_DIRECTORY_ENTRIES
        except OSError:
            raise CustomSourceError("store_unavailable") from None
        finally:
            os.close(root_fd)

        records_truncated = scanned_valid_count > len(retained)
        complete = not (scan_limit_reached or records_truncated or corrupt_count or ignored_count)
        status = "truncated" if scan_limit_reached or records_truncated else (
            "partial" if corrupt_count or ignored_count else ("empty" if scanned_valid_count == 0 else "ok")
        )
        sources = sorted(
            retained,
            key=lambda item: (item["created_at"], item["registration_id"]),
            reverse=True,
        )
        return {
            "sources": sources,
            "status": status,
            "complete": complete,
            "empty": scanned_valid_count == 0 and complete,
            "scanned_entries": scanned_entries,
            "scanned_valid_count": scanned_valid_count,
            "corrupt_count": corrupt_count,
            "ignored_count": ignored_count,
            "returned_count": len(sources),
            "scan_limit_reached": scan_limit_reached,
            "records_truncated": records_truncated,
        }

    def show(self, registration_id: str) -> dict[str, Any]:
        registration_id = self._validate_id(registration_id)
        root_fd = self._open_root(create=False)
        if root_fd is None:
            raise CustomSourceError("record_not_found") from None
        try:
            record, _source = self._load_entry(root_fd, registration_id)
            return record
        finally:
            os.close(root_fd)

    def read_source(self, registration_id: str) -> bytes:
        registration_id = self._validate_id(registration_id)
        root_fd = self._open_root(create=False)
        if root_fd is None:
            raise CustomSourceError("record_not_found") from None
        try:
            _record, source = self._load_entry(root_fd, registration_id)
            return source
        finally:
            os.close(root_fd)

