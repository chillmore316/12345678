"""Persistent isolated accounts for historical custom-Python replays.

The source is only transported to the reviewed guest adapter.  This module
never compiles or imports user source and never opens the market-data lake.  An
account advances only over the bars frozen in a completed custom-Python run.
"""
from __future__ import annotations

import copy
import csv
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import custom_kernel, replay_kernel
from .custom_engine import GuestStrategyAdapter, _prepare, configured_runtime
from .custom_source_store import CustomSourceStore
from .store import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAMESPACE_NAME = "python_replays"
DATABASE_NAME = "accounts.sqlite3"
PROTOCOL_VERSION = "historical-python-replay-v1"
ACCOUNT_SCHEMA_VERSION = 1
MAX_SOURCE_RESULT_BYTES = 64 * 1024 * 1024
MAX_PROVENANCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024
MAX_API_SOURCE_BYTES = 32 * 1024
MAX_STEP_BARS = 240
MAX_ACCOUNT_JSON_BYTES = 64 * 1024 * 1024
MAX_ACCOUNT_SOURCE_JSON_BYTES = 4 * 1024 * 1024
_UUID_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SOURCE_ID = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_EXPORTS = frozenset({"equity", "orders", "trades", "positions", "events"})
_CODE_PATHS = {
    "custom_engine": "research_center/custom_engine.py",
    "guest_adapter": "research_center/guest_strategy.py",
    "custom_kernel": "research_center/custom_kernel.py",
    "engine": "research_center/engine.py",
    "strategies": "research_center/strategies.py",
    "replay_accounts": "research_center/replay_accounts.py",
    "replay_kernel": "research_center/replay_kernel.py",
    "custom_source_store": "research_center/custom_source_store.py",
    "minute_adapter": "data/joinquant_minute.py",
    "strategy_base": "strategy/base.py",
    "vm_session": "research_center/vm_strategy_session.py",
    "vm_worker": "research_center/vm_strategy_worker.py",
}
_HASH_FIELDS = (
    "config_sha256", "execution_config_sha256", "request_config_sha256",
    "source_json_sha256", "source_sha256", "api_source_sha256", "bars_sha256",
    "manifest_sha256", "readiness_sha256", "identity_sha256", "component_hashes_sha256",
    "module_sha256", "engine_code_sha256", "state_sha256", "result_sha256",
)


class PythonReplayAccountError(ValueError):
    """A request, frozen source, or persistent account violates its contract."""


class PythonReplayRevisionConflict(PythonReplayAccountError):
    """The submitted account revision is stale."""


class PythonReplaySourceChanged(PythonReplayAccountError):
    """The pinned strategy source or trusted runtime code has changed."""


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise PythonReplayAccountError("account data must be finite JSON") from exc


def _json_bytes(value: Any, maximum: int, field: str) -> bytes:
    raw = _json_dump(value).encode("utf-8", "strict")
    if len(raw) > maximum:
        raise PythonReplayAccountError(f"{field} exceeds the {maximum}-byte limit")
    return raw


def _json_copy(value: Any, maximum: int = MAX_ACCOUNT_JSON_BYTES, field: str = "account data") -> Any:
    raw = _json_bytes(value, maximum, field)
    return json.loads(raw.decode("utf-8", "strict"))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_json(value: Any, maximum: int = MAX_ACCOUNT_JSON_BYTES, field: str = "account data") -> str:
    return _sha256(_json_bytes(value, maximum, field))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_uuid_hex(value: Any, field: str) -> str:
    if type(value) is not str or _UUID_HEX.fullmatch(value) is None:
        raise PythonReplayAccountError(f"{field} must be a 32-character lowercase UUID hex")
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise PythonReplayAccountError(f"{field} must be a valid UUID") from None
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_ctime_ns))


def _private_directory(path: Path, *, create: bool = False) -> os.stat_result | None:
    if create:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError:
            raise PythonReplayAccountError("historical Python replay store is unavailable") from None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise PythonReplayAccountError("historical Python replay store is unavailable") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.getuid()):
        raise PythonReplayAccountError("historical Python replay namespace must be a private directory")
    return info


def _safe_read_file(path: Path, maximum: int, *, allow_missing: bool = False) -> bytes | None:
    """Stable no-follow, nonblocking bounded read of one regular file."""
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise PythonReplayAccountError(f"required frozen file is missing: {path.name}") from None
    except OSError:
        raise PythonReplayAccountError("frozen file cannot be safely accessed") from None
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise PythonReplayAccountError("frozen file parent must be a regular directory")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(parent, dir_flags)
    except OSError:
        raise PythonReplayAccountError("frozen file parent cannot be safely opened") from None
    fd = None
    try:
        if _stat_identity(os.fstat(dir_fd)) != _stat_identity(parent_stat):
            raise PythonReplayAccountError("frozen file parent changed before it was opened")
        try:
            before = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise PythonReplayAccountError(f"required frozen file is missing: {path.name}") from None
        except OSError:
            raise PythonReplayAccountError("frozen file cannot be safely inspected") from None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PythonReplayAccountError("frozen file must be a regular non-symlink file")
        if before.st_size > maximum:
            raise PythonReplayAccountError(f"frozen file exceeds the {maximum}-byte limit")
        try:
            fd = os.open(path.name, flags, dir_fd=dir_fd)
        except OSError:
            raise PythonReplayAccountError("frozen file cannot be safely opened") from None
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before)
                or opened.st_size > maximum):
            raise PythonReplayAccountError("frozen file changed before it was read")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise PythonReplayAccountError(f"frozen file exceeds the {maximum}-byte limit")
        after_fd = os.fstat(fd)
        after_path = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        if (_stat_identity(after_fd) != _stat_identity(opened)
                or _stat_identity(after_path) != _stat_identity(opened)
                or stat.S_ISLNK(after_path.st_mode)):
            raise PythonReplayAccountError("frozen file changed while it was read")
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(dir_fd)


def _read_json_file(path: Path, maximum: int) -> tuple[bytes, Any]:
    raw = _safe_read_file(path, maximum)
    assert raw is not None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PythonReplayAccountError(f"frozen file is not valid JSON: {path.name}") from None
    return raw, value


def _current_trusted_code_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, relative in _CODE_PATHS.items():
        raw = _safe_read_file(PROJECT_ROOT / relative, 4 * 1024 * 1024)
        assert raw is not None
        hashes[name] = _sha256(raw)
    return hashes


def _current_module_sha256() -> str:
    raw = _safe_read_file(Path(__file__), 4 * 1024 * 1024)
    assert raw is not None
    return _sha256(raw)


def _module_code_digest(module_sha256: str, trusted_hashes: dict[str, str]) -> str:
    return _sha256_json({"module_sha256": module_sha256,
                         "trusted_code_sha256": trusted_hashes,
                         "protocol_version": PROTOCOL_VERSION})


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    result = _json_copy(config)
    result.pop("data_root", None)
    execution = result.get("execution_config")
    if isinstance(execution, dict):
        execution.pop("data_root", None)
    return result


def _validate_bars(value: Any, execution_config: dict[str, Any]) -> list[dict[str, Any]]:
    from .engine import MAX_ENGINE_ROWS

    if type(value) is not list or not value:
        raise PythonReplayAccountError("completed source run has no replayable saved bars")
    if len(value) > MAX_ENGINE_ROWS:
        raise PythonReplayAccountError(f"saved bars exceed the {MAX_ENGINE_ROWS}-row replay limit")
    result: list[dict[str, Any]] = []
    previous: str | None = None
    for index, raw in enumerate(value):
        if type(raw) is not dict:
            raise PythonReplayAccountError(f"saved bar {index} has an invalid schema")
        try:
            canonical = replay_kernel._canonical_bar(execution_config, raw)
        except Exception:
            raise PythonReplayAccountError(f"saved bar {index} violates the canonical bar contract") from None
        # Preserve the saved result's canonical row exactly; refuse rows that
        # would need normalization, enrichment, or fields to be discarded.
        if raw != canonical:
            raise PythonReplayAccountError(f"saved bar {index} is not in canonical frozen form")
        source_time = canonical["source_datetime"]
        try:
            parsed = datetime.fromisoformat(source_time)
            start = datetime.fromisoformat(canonical["bar_start_datetime"])
            timestamp = datetime.fromisoformat(canonical["datetime"])
        except (TypeError, ValueError):
            raise PythonReplayAccountError(f"saved bar {index} has invalid timestamps") from None
        if (parsed.tzinfo is not None or start.tzinfo is not None or timestamp.tzinfo is not None
                or parsed.second or parsed.microsecond
                or parsed - start != timedelta(minutes=1) or timestamp != start):
            raise PythonReplayAccountError(f"saved bar {index} violates the source-end/bar-start contract")
        if previous is not None and source_time <= previous:
            raise PythonReplayAccountError("saved bars must be strictly ordered by source_datetime")
        previous = source_time
        for name in ("open", "high", "low", "close", "volume", "turnover"):
            number = canonical[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
                raise PythonReplayAccountError(f"saved bar {index} has invalid numeric field {name}")
        result.append(copy.deepcopy(canonical))
    return result


def _expected_source_fields(prepared: Any) -> dict[str, Any]:
    components = prepared.component_hashes
    return {
        "schema_version": prepared.identity.get("schema"),
        "strategy_version": prepared.strategy_version,
        "source_id": prepared.record["registration_id"],
        "source_sha256": _sha256(prepared.source),
        "api_source_sha256": _sha256(prepared.api_source),
        "sdk_source_sha256": components["sdk_source_sha256"],
        "generic_driver_sha256": components["generic_driver_sha256"],
        "trusted_code_sha256": components["trusted_code_sha256"],
        "legacy_source_sha256": components["legacy_source_sha256"],
        "runtime_pins": components["runtime_pins"],
    }


def _validate_source_run(run_record: dict[str, Any], source_result: Any,
                         provenance: Any, prepared: Any, source_bytes: bytes,
                         frozen_source: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if type(source_result) is not dict or type(provenance) is not dict:
        raise PythonReplayAccountError("source result or provenance is invalid")
    if (run_record.get("status") != "completed"
            or run_record.get("strategy") != "custom_python"
            or run_record.get("backend") != "isolated_python"):
        raise PythonReplayAccountError("source must be a completed isolated custom-Python run")
    if run_record.get("mode") != "RESEARCH_ONLY" or run_record.get("paper_admission") is not False:
        raise PythonReplayAccountError("source run must remain RESEARCH_ONLY")
    expected_version = prepared.strategy_version
    if any(run_record.get(key) != expected_version for key in ("strategy_version",)):
        raise PythonReplaySourceChanged("source strategy version is old or does not match current code")
    run_config = run_record.get("config")
    if type(run_config) is not dict or run_config.get("strategy_version") != expected_version:
        raise PythonReplaySourceChanged("source run config has a stale strategy version")
    if source_result.get("backend") != "isolated_python" or source_result.get("strategy") != "custom_python":
        raise PythonReplayAccountError("source result is not from the custom-Python backend")
    if source_result.get("strategy_version") != expected_version:
        raise PythonReplaySourceChanged("saved source result has a stale strategy version")
    if (provenance.get("backend") != "isolated_python"
            or provenance.get("strategy_version") != expected_version
            or provenance.get("mode") != "RESEARCH_ONLY"):
        raise PythonReplaySourceChanged("source provenance does not match the current strategy version")
    expected = _expected_source_fields(prepared)
    source_manifest = source_result.get("source_manifest")
    execution = source_result.get("custom_execution")
    if type(source_manifest) is not dict or type(execution) is not dict:
        raise PythonReplayAccountError("saved custom source identity is missing")
    if (source_manifest.get("schema_version") != expected["schema_version"]
            or source_manifest.get("mode") != "RESEARCH_ONLY"
            or source_manifest.get("paper_admission") is not False
            or source_manifest.get("runtime_interface_verified") is not True
            or execution.get("mode") != "RESEARCH_ONLY"
            or execution.get("paper_admission") is not False):
        raise PythonReplayAccountError("saved custom source identity violates research-only policy")
    for name in ("strategy_version", "source_id", "source_sha256", "api_source_sha256",
                 "sdk_source_sha256", "generic_driver_sha256", "trusted_code_sha256",
                 "legacy_source_sha256", "runtime_pins"):
        if source_manifest.get(name) != expected[name]:
            raise PythonReplaySourceChanged(f"saved source manifest does not match current {name}")
    registration = source_manifest.get("registration")
    if type(registration) is not dict:
        raise PythonReplayAccountError("saved source registration metadata is missing")
    expected_registration = {
        "registration_id": prepared.record.get("registration_id"),
        "code_version": prepared.record.get("code_version"),
        "interface_contract": prepared.record.get("interface_contract"),
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "runtime_interface_verified": False,
        "dependencies_verified": False,
        "import_declaration_status": "DECLARED_NOT_VERIFIED",
    }
    if any(registration.get(name) != value for name, value in expected_registration.items()):
        raise PythonReplaySourceChanged("saved source registration metadata does not match the archive")
    if (registration.get("runtime_interface_verified") is not False
            or registration.get("dependencies_verified") is not False):
        raise PythonReplayAccountError("source registration must retain its unverified admission flags")
    if (registration.get("registration_id") != expected["source_id"]
            or registration.get("code_version") != expected["source_sha256"]):
        raise PythonReplaySourceChanged("saved source registration ID/version does not match source bytes")
    identity = prepared.identity
    if (execution.get("strategy_identity") != identity
            or execution.get("identity_sha256") != _sha256_json(identity)):
        raise PythonReplaySourceChanged("saved strategy identity does not match current source")
    if _sha256(source_bytes) != expected["source_sha256"] or frozen_source != source_bytes:
        raise PythonReplaySourceChanged("frozen source bytes do not match the registered source")
    custom_config = source_result.get("custom_config")
    expected_config = _json_copy(prepared.normalized)
    expected_config.pop("execution_config", None)
    if custom_config != expected_config:
        raise PythonReplaySourceChanged("saved normalized config does not match the current source config")
    if run_config.get("source_id") != expected["source_id"]:
        raise PythonReplaySourceChanged("source run config points to a different custom source")
    if any(run_config.get(key) != expected_version for key in ("strategy_version",)):
        raise PythonReplaySourceChanged("source run strategy version changed")

    trusted_hashes = prepared.component_hashes.get("trusted_code_sha256")
    if type(trusted_hashes) is not dict or _current_trusted_code_hashes() != trusted_hashes:
        raise PythonReplaySourceChanged("trusted custom replay code changed since source execution")
    provenance_hashes = provenance.get("source_hashes")
    if type(provenance_hashes) is not dict:
        raise PythonReplayAccountError("source provenance has no code snapshot hashes")
    for key, relative in _CODE_PATHS.items():
        if provenance_hashes.get(relative) != trusted_hashes.get(key):
            raise PythonReplaySourceChanged("source provenance code snapshot does not match current code")

    execution_config = prepared.execution_config
    bars = _validate_bars(source_result.get("bars"), execution_config)
    manifest = source_result.get("data_manifest")
    if type(manifest) is not dict:
        raise PythonReplayAccountError("saved source result has no frozen data manifest")
    readiness = source_result.get("data_readiness")
    if type(readiness) is not dict:
        readiness = {
            "status": "unverified", "reason": "source_run_did_not_save_data_readiness",
            "checks": [], "paper_admission": False, "origin": "source_run_snapshot",
        }
    else:
        readiness = _json_copy(readiness, MAX_ACCOUNT_SOURCE_JSON_BYTES, "data_readiness")
        readiness["paper_admission"] = False
    return (_json_copy(identity), bars,
            _json_copy(manifest, MAX_ACCOUNT_SOURCE_JSON_BYTES, "data_manifest"), readiness)


def _row_checksum(values: dict[str, Any]) -> str:
    material = {
        "protocol_version": values["protocol_version"],
        "account_id": values["account_id"], "run_id": values["run_id"],
        "label": values["label"], "status": values["status"],
        "revision": int(values["revision"]), "cursor": int(values["cursor"]),
        "total_bars": int(values["total_bars"]),
        "created_at": values["created_at"], "updated_at": values["updated_at"],
        "finalized": int(values["finalized"]),
        **{name: values[name] for name in _HASH_FIELDS},
    }
    return _sha256_json(material)


class PythonReplayAccounts:
    """SQLite-backed custom Python replay accounts under ``root/python_replays``.

    Construction and GET methods are read-only.  The namespace and database
    are created only after a successful guest factory/close during ``create``.
    """

    def __init__(self, run_store: RunStore):
        if not isinstance(run_store, RunStore):
            raise TypeError("run_store must be a research_center.store.RunStore")
        self.run_store = run_store
        self.root = Path(run_store.root).resolve() / NAMESPACE_NAME
        self.database = self.root / DATABASE_NAME
        self._validate_location()

    def _validate_location(self) -> None:
        parts = {part.casefold() for part in self.root.parts}
        if "quantlivepaper" in parts:
            raise PythonReplayAccountError("Python replay accounts cannot be inside QuantLivePaper")
        from .engine import DEFAULT_DATA_ROOT
        normalized = self.root.resolve()
        blocked = (PROJECT_ROOT / "data", Path(DEFAULT_DATA_ROOT).expanduser().resolve())
        if any(_is_within(normalized, path.expanduser().resolve()) for path in blocked):
            raise PythonReplayAccountError("Python replay accounts cannot be inside a market-data root")
        # Check existing paths without creating the namespace or database.
        _private_directory(self.root)
        self._database_stat()

    def _database_stat(self) -> os.stat_result | None:
        info = _private_directory(self.root)
        if info is None:
            return None
        try:
            database_stat = self.database.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise PythonReplayAccountError("historical Python replay database is unavailable") from None
        if (stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode)
                or database_stat.st_mode & 0o077 or (database_stat.st_mode & 0o600) != 0o600
                or database_stat.st_nlink != 1 or database_stat.st_uid != os.getuid()):
            raise PythonReplayAccountError("historical Python replay database must be a private regular file")
        return database_stat

    def _ensure_database(self) -> None:
        root_parent = self.root.parent
        try:
            parent_stat = root_parent.lstat()
        except OSError:
            raise PythonReplayAccountError("RunStore root is unavailable") from None
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise PythonReplayAccountError("RunStore root must be a regular directory")
        _private_directory(self.root, create=True)
        if self._database_stat() is None:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.database, flags, 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise PythonReplayAccountError("historical Python replay database cannot be created") from None
            else:
                os.close(fd)
        self._initialize()

    def _connect_readonly(self) -> sqlite3.Connection:
        if self._database_stat() is None:
            raise FileNotFoundError("historical Python replay database not found")
        try:
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True,
                                         timeout=15, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay database cannot be read") from None

    def _connect_write(self) -> sqlite3.Connection:
        if self._database_stat() is None:
            raise PythonReplayAccountError("historical Python replay database is unavailable")
        try:
            connection = sqlite3.connect(self.database, timeout=20, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            return connection
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay database cannot be opened") from None

    def _initialize(self) -> None:
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, ACCOUNT_SCHEMA_VERSION):
                raise PythonReplayAccountError("historical Python replay database schema is unsupported")
            connection.execute("""CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('paused','running','completed')),
                revision INTEGER NOT NULL,
                cursor INTEGER NOT NULL,
                total_bars INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                config_json TEXT NOT NULL,
                execution_config_json TEXT NOT NULL,
                request_config_json TEXT NOT NULL,
                source_json TEXT NOT NULL,
                bars_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                readiness_json TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                component_hashes_json TEXT NOT NULL,
                user_source BLOB NOT NULL,
                api_source BLOB NOT NULL,
                state_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                execution_config_sha256 TEXT NOT NULL,
                request_config_sha256 TEXT NOT NULL,
                source_json_sha256 TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                api_source_sha256 TEXT NOT NULL,
                bars_sha256 TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                readiness_sha256 TEXT NOT NULL,
                identity_sha256 TEXT NOT NULL,
                component_hashes_sha256 TEXT NOT NULL,
                module_sha256 TEXT NOT NULL,
                engine_code_sha256 TEXT NOT NULL,
                state_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                finalized INTEGER NOT NULL CHECK(finalized IN (0,1)),
                row_checksum TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS python_replays_created_idx ON accounts(created_at DESC,account_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                details_json TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS python_replays_events_idx ON events(account_id,event_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS requests (
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                request_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(account_id,request_id)
            )""")
            connection.execute(f"PRAGMA user_version={ACCOUNT_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.database, 0o600)
        except OSError:
            raise PythonReplayAccountError("historical Python replay store permissions could not be secured") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._ensure_database()
            connection = self._connect_write()
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay database is unavailable") from None
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as exc:
                connection.rollback()
                if isinstance(exc, sqlite3.Error):
                    raise PythonReplayAccountError("historical Python replay database transaction failed") from None
                raise
        finally:
            connection.close()

    def _insert_event(self, connection: sqlite3.Connection, account_id: str,
                      event_type: str, revision: int, details: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO events(account_id,event_type,created_at,revision,details_json) VALUES(?,?,?,?,?)",
            (account_id, event_type, _now(), revision,
             _json_dump(details or {})),
        )

    @staticmethod
    def _validate_label(label: Any) -> str:
        if type(label) is not str or len(label) > 80:
            raise PythonReplayAccountError("label must be a string of at most 80 characters")
        return label

    def _source_store(self) -> CustomSourceStore:
        return CustomSourceStore(self.run_store.root / "custom_sources")

    def _current_prepared(self, request_config: dict[str, Any], expected: dict[str, Any],
                          source_store: Any) -> Any:
        try:
            prepared = _prepare(request_config, source_store)
            current_module_hash = _current_module_sha256()
            current_trusted_hashes = _current_trusted_code_hashes()
        except BaseException:
            raise PythonReplaySourceChanged("custom source or trusted replay code is unavailable") from None
        if (prepared.strategy_version != expected["strategy_version"]
                or prepared.identity != expected["identity"]
                or prepared.source != expected["user_source"]
                or prepared.api_source != expected["api_source"]
                or prepared.component_hashes != expected["component_hashes"]
                or current_module_hash != expected["module_sha256"]
                or current_trusted_hashes != prepared.component_hashes.get("trusted_code_sha256")):
            raise PythonReplaySourceChanged("custom source or trusted replay code changed; account cannot continue")
        return prepared

    def create(self, run_id: str, label: str = "") -> dict[str, Any]:
        run_id = _valid_uuid_hex(run_id, "run_id")
        label = self._validate_label(label)
        try:
            run_record = self.run_store.get(run_id)
        except (FileNotFoundError, ValueError):
            raise PythonReplayAccountError("completed source run was not found") from None
        if (run_record.get("status") != "completed" or run_record.get("strategy") != "custom_python"
                or run_record.get("backend") != "isolated_python"):
            raise PythonReplayAccountError("source must be a completed isolated custom-Python run")

        run_directory = self.run_store.directory(run_id)
        try:
            directory_info = run_directory.lstat()
        except OSError:
            raise PythonReplayAccountError("source run directory is unavailable") from None
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise PythonReplayAccountError("source run directory must be a regular directory")
        root = Path(self.run_store.root).resolve()
        try:
            if run_directory.resolve(strict=True).parent != root:
                raise PythonReplayAccountError("source run directory must remain inside its RunStore")
        except OSError:
            raise PythonReplayAccountError("source run directory is unavailable") from None

        result_raw, source_result = _read_json_file(run_directory / "result.json", MAX_SOURCE_RESULT_BYTES)
        provenance_raw, provenance = _read_json_file(run_directory / "provenance.json", MAX_PROVENANCE_BYTES)
        if type(source_result) is not dict or type(provenance) is not dict:
            raise PythonReplayAccountError("source result and provenance must be JSON objects")
        if len(provenance_raw) > MAX_PROVENANCE_BYTES:
            raise PythonReplayAccountError("source provenance exceeds its size limit")
        source_store = self._source_store()
        try:
            prepared = _prepare(run_record.get("config"), source_store)
            archive_record = source_store.show(prepared.record["registration_id"])
            archive_source = source_store.read_source(prepared.record["registration_id"])
        except BaseException:
            raise PythonReplaySourceChanged("current custom source could not be verified") from None
        if (type(archive_record) is not dict or type(archive_source) is not bytes
                or prepared.source != archive_source
                or prepared.record != archive_record):
            raise PythonReplaySourceChanged("custom source archive changed during verification")
        frozen_source = _safe_read_file(run_directory / "custom_strategy.py", MAX_SOURCE_BYTES)
        if frozen_source is None:
            raise PythonReplayAccountError("frozen custom strategy source is missing")
        identity, bars, manifest, readiness = _validate_source_run(
            run_record, source_result, provenance, prepared, archive_source, frozen_source
        )

        normalized = _json_copy(prepared.normalized, MAX_ACCOUNT_SOURCE_JSON_BYTES, "normalized config")
        execution_config = _json_copy(prepared.execution_config, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                      "execution config")
        request_config = _json_copy(run_record["config"], MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                    "source request config")
        module_sha = _current_module_sha256()
        trusted_hashes = _json_copy(prepared.component_hashes["trusted_code_sha256"])
        component_hashes = _json_copy(prepared.component_hashes, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                      "component hashes")
        self._current_prepared(
            run_record["config"],
            {"strategy_version": prepared.strategy_version, "identity": identity,
             "user_source": prepared.source, "api_source": prepared.api_source,
             "component_hashes": component_hashes, "module_sha256": module_sha},
            source_store,
        )

        # The only user-code call during create is inside this guest adapter.
        # The returned state is committed only after close succeeds.
        try:
            adapter = GuestStrategyAdapter(
                prepared.source, prepared.normalized["symbol"],
                copy.deepcopy(prepared.normalized["custom_params"]),
                configured_runtime(), state=None,
                history_limit=prepared.normalized["history_limit"],
                api_source=prepared.api_source,
            )
        except BaseException:
            raise PythonReplayAccountError("guest strategy initialization failed") from None
        initial_user_state = None
        try:
            initial_user_state = _json_copy(adapter.state, 16 * 1024, "initial user state")
        except BaseException:
            try:
                adapter.close()
            except BaseException:
                pass
            raise PythonReplayAccountError("guest strategy returned an invalid initial state") from None
        try:
            adapter.close()
        except BaseException:
            raise PythonReplayAccountError("guest strategy close failed during account creation") from None

        try:
            state = custom_kernel.initial_state(
                execution_config, identity, user_state=initial_user_state,
                history_limit=prepared.normalized["history_limit"],
            )
            result = self._make_result(
                prepared, state, manifest, readiness,
                source_result.get("source_manifest"),
            )
            state_json = _json_dump(state)
            result_json = _json_dump(result)
            if len(state_json.encode("utf-8")) > MAX_ACCOUNT_JSON_BYTES:
                raise PythonReplayAccountError("initial state exceeds account size limit")
            if len(result_json.encode("utf-8")) > MAX_ACCOUNT_JSON_BYTES:
                raise PythonReplayAccountError("initial result exceeds account size limit")
            self._current_prepared(
                run_record["config"],
                {"strategy_version": prepared.strategy_version, "identity": identity,
                 "user_source": prepared.source, "api_source": prepared.api_source,
                 "component_hashes": component_hashes, "module_sha256": module_sha},
                source_store,
            )
        except PythonReplayAccountError:
            raise
        except BaseException:
            raise PythonReplayAccountError("custom replay initial checkpoint could not be created") from None

        account_id = uuid.uuid4().hex
        now = _now()
        hash_values = {
            "config_sha256": _sha256(_json_bytes(normalized, MAX_ACCOUNT_SOURCE_JSON_BYTES, "config")),
            "execution_config_sha256": _sha256(_json_bytes(execution_config, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                              "execution config")),
            "request_config_sha256": _sha256(_json_bytes(request_config, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                           "source request config")),
            "source_json_sha256": "",
            "source_sha256": _sha256(prepared.source),
            "api_source_sha256": _sha256(prepared.api_source),
            "bars_sha256": _sha256(_json_bytes(bars, MAX_SOURCE_RESULT_BYTES, "frozen bars")),
            "manifest_sha256": _sha256(_json_bytes(manifest, MAX_ACCOUNT_SOURCE_JSON_BYTES, "manifest")),
            "readiness_sha256": _sha256(_json_bytes(readiness, MAX_ACCOUNT_SOURCE_JSON_BYTES, "data_readiness")),
            "identity_sha256": _sha256(_json_bytes(identity, 64 * 1024, "identity")),
            "component_hashes_sha256": _sha256(_json_bytes(component_hashes, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                              "component hashes")),
            "module_sha256": module_sha,
            "engine_code_sha256": _sha256_json(trusted_hashes),
            "state_sha256": _sha256(state_json.encode("utf-8")),
            "result_sha256": _sha256(result_json.encode("utf-8")),
        }
        code_hash = _module_code_digest(module_sha, trusted_hashes)
        source_hashes = {
            "source_result_sha256": _sha256(result_raw),
            "source_config_sha256": _sha256(_json_bytes(run_record["config"], MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                         "source config")),
            "source_provenance_sha256": _sha256(provenance_raw),
            "source_bars_sha256": _sha256(_json_bytes(bars, MAX_SOURCE_RESULT_BYTES, "source bars")),
            "source_sha256": _sha256(prepared.source),
            "api_source_sha256": _sha256(prepared.api_source),
            "trusted_code_sha256": _sha256_json(trusted_hashes),
            "python_replay_module_sha256": module_sha,
        }
        source_public = {
            "run_id": run_id, "strategy": "custom_python", "backend": "isolated_python",
            "strategy_version": prepared.strategy_version,
            "source_id": prepared.record["registration_id"],
            "hashes": source_hashes,
            "dataset_hashes": _json_copy(manifest.get("used_source_files", []),
                                           MAX_ACCOUNT_SOURCE_JSON_BYTES, "dataset hashes"),
            "source_manifest": _json_copy(source_result["source_manifest"],
                                            MAX_ACCOUNT_SOURCE_JSON_BYTES, "source manifest"),
            "provenance": _json_copy(provenance, MAX_PROVENANCE_BYTES, "source provenance"),
            "data_readiness": _json_copy(readiness, MAX_ACCOUNT_SOURCE_JSON_BYTES, "data_readiness"),
            "custom_config": _public_config(normalized),
            "request_config": {"run_id": run_id, "label": label},
        }
        hash_values["source_json_sha256"] = _sha256(_json_bytes(
            source_public, MAX_ACCOUNT_SOURCE_JSON_BYTES, "source metadata"
        ))
        bars_json = _json_dump(bars)
        manifest_json = _json_dump(manifest)
        readiness_json = _json_dump(readiness)
        identity_json = _json_dump(identity)
        component_hashes_json = _json_dump(component_hashes)
        source_json = _json_dump(source_public)
        values = {
            "account_id": account_id, "run_id": run_id, "label": label,
            "status": "paused", "revision": 0, "cursor": 0, "total_bars": len(bars),
            "created_at": now, "updated_at": now, "protocol_version": PROTOCOL_VERSION,
            "config_sha256": hash_values["config_sha256"],
            "execution_config_sha256": hash_values["execution_config_sha256"],
            "request_config_sha256": hash_values["request_config_sha256"],
            "source_json_sha256": hash_values["source_json_sha256"],
            "source_sha256": hash_values["source_sha256"],
            "api_source_sha256": hash_values["api_source_sha256"],
            "bars_sha256": hash_values["bars_sha256"],
            "manifest_sha256": hash_values["manifest_sha256"],
            "readiness_sha256": hash_values["readiness_sha256"],
            "identity_sha256": hash_values["identity_sha256"],
            "component_hashes_sha256": hash_values["component_hashes_sha256"],
            "module_sha256": hash_values["module_sha256"],
            "engine_code_sha256": hash_values["engine_code_sha256"],
            "state_sha256": hash_values["state_sha256"],
            "result_sha256": hash_values["result_sha256"],
            "finalized": 0,
        }
        values["row_checksum"] = _row_checksum(values)
        try:
            with self._transaction() as connection:
                columns = [
                    "account_id", "run_id", "label", "status", "revision", "cursor", "total_bars",
                    "created_at", "updated_at", "protocol_version", "config_json",
                    "execution_config_json", "request_config_json", "source_json", "bars_json",
                    "manifest_json", "readiness_json", "identity_json", "component_hashes_json",
                    "user_source", "api_source", "state_json", "result_json", *_HASH_FIELDS,
                    "code_hash", "finalized", "row_checksum",
                ]
                insert_values = [
                    account_id, run_id, label, "paused", 0, 0, len(bars), now, now,
                    PROTOCOL_VERSION, _json_dump(normalized), _json_dump(execution_config),
                    _json_dump(request_config), source_json, bars_json, manifest_json, readiness_json,
                    identity_json, component_hashes_json, sqlite3.Binary(prepared.source),
                    sqlite3.Binary(prepared.api_source), state_json, result_json,
                    *(values[name] for name in _HASH_FIELDS), code_hash, 0, values["row_checksum"],
                ]
                placeholders = ",".join("?" for _ in columns)
                connection.execute(
                    f"INSERT INTO accounts({','.join(columns)}) VALUES({placeholders})",
                    insert_values,
                )
                self._insert_event(connection, account_id, "create", 0, {
                    "source_run_id": run_id,
                    "source_result_sha256": source_hashes["source_result_sha256"],
                    "total_bars": len(bars), "status": "paused",
                    "mode": "RESEARCH_ONLY", "kind": "historical_python_replay",
                })
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay account could not be committed") from None
        return self.detail(account_id)

    @staticmethod
    def _make_result(prepared: Any, state: dict[str, Any], manifest: dict[str, Any],
                     readiness: dict[str, Any], source_manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            result = custom_kernel.result_from_state(prepared.execution_config, state, manifest)
            result = _json_copy(result)
        except BaseException:
            raise PythonReplayAccountError("custom replay result could not be rendered") from None
        result["strategy"] = "custom_python"
        result["backend"] = "isolated_python"
        result["strategy_version"] = prepared.strategy_version
        result["custom_config"] = _public_config(prepared.normalized)
        result["custom_config"].pop("execution_config", None)
        result["source_manifest"] = _json_copy(source_manifest)
        result["data_readiness"] = _json_copy(readiness)
        result["mode"] = "RESEARCH_ONLY"
        result["paper_admission"] = False
        return result

    def _verify_row(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["protocol_version"] != PROTOCOL_VERSION:
            raise PythonReplayAccountError("historical Python replay protocol changed")
        values = {name: row[name] for name in (
            "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
            "total_bars", "created_at", "updated_at", "finalized", *_HASH_FIELDS,
        )}
        if row["row_checksum"] != _row_checksum(values):
            raise PythonReplayAccountError("historical Python replay row checksum failed")
        bounds = {
            "config_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "execution_config_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "request_config_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "source_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "bars_json": MAX_SOURCE_RESULT_BYTES,
            "manifest_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "readiness_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "identity_json": 64 * 1024,
            "component_hashes_json": MAX_ACCOUNT_SOURCE_JSON_BYTES,
            "state_json": MAX_ACCOUNT_JSON_BYTES,
            "result_json": MAX_ACCOUNT_JSON_BYTES,
        }
        try:
            for name, maximum in bounds.items():
                if len(row[name].encode("utf-8")) > maximum:
                    raise PythonReplayAccountError(f"stored {name} exceeds its size limit")
            if len(bytes(row["user_source"])) > MAX_SOURCE_BYTES:
                raise PythonReplayAccountError("stored user source exceeds its size limit")
            if len(bytes(row["api_source"])) > MAX_API_SOURCE_BYTES:
                raise PythonReplayAccountError("stored API source exceeds its size limit")
        except (AttributeError, TypeError):
            raise PythonReplayAccountError("historical Python replay checkpoint is corrupt") from None
        try:
            config = json.loads(row["config_json"])
            execution_config = json.loads(row["execution_config_json"])
            request_config = json.loads(row["request_config_json"])
            source = json.loads(row["source_json"])
            bars = json.loads(row["bars_json"])
            manifest = json.loads(row["manifest_json"])
            readiness = json.loads(row["readiness_json"])
            identity = json.loads(row["identity_json"])
            components = json.loads(row["component_hashes_json"])
            state = json.loads(row["state_json"])
            result = json.loads(row["result_json"])
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise PythonReplayAccountError("historical Python replay checkpoint is corrupt") from None
        dictionary_fields = (config, execution_config, request_config, source, manifest,
                             readiness, identity, components, state, result)
        if any(type(value) is not dict for value in dictionary_fields) or type(bars) is not list:
            raise PythonReplayAccountError("historical Python replay checkpoint schema is invalid")
        trusted_hashes = components.get("trusted_code_sha256")
        if (type(trusted_hashes) is not dict
                or any(type(key) is not str or type(value) is not str
                       or _SOURCE_ID.fullmatch(value) is None
                       for key, value in trusted_hashes.items())
                or type(row["module_sha256"]) is not str
                or _SOURCE_ID.fullmatch(row["module_sha256"]) is None):
            raise PythonReplayAccountError("historical Python replay code identity is corrupt")
        if row["engine_code_sha256"] != _sha256_json(trusted_hashes):
            raise PythonReplayAccountError("historical Python replay engine code hash does not match its components")
        if row["code_hash"] != _module_code_digest(row["module_sha256"], trusted_hashes):
            raise PythonReplayAccountError("historical Python replay code hash does not match its components")
        checks = (
            ("config_sha256", _sha256(_json_bytes(config, MAX_ACCOUNT_SOURCE_JSON_BYTES, "config"))),
            ("execution_config_sha256", _sha256(_json_bytes(execution_config, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                              "execution config"))),
            ("request_config_sha256", _sha256(_json_bytes(request_config, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                           "source request config"))),
            ("source_json_sha256", _sha256(_json_bytes(source, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                        "source metadata"))),
            ("source_sha256", _sha256(bytes(row["user_source"]))),
            ("api_source_sha256", _sha256(bytes(row["api_source"]))),
            ("bars_sha256", _sha256(_json_bytes(bars, MAX_SOURCE_RESULT_BYTES, "frozen bars"))),
            ("manifest_sha256", _sha256(_json_bytes(manifest, MAX_ACCOUNT_SOURCE_JSON_BYTES, "manifest"))),
            ("readiness_sha256", _sha256(_json_bytes(readiness, MAX_ACCOUNT_SOURCE_JSON_BYTES, "data_readiness"))),
            ("identity_sha256", _sha256(_json_bytes(identity, 64 * 1024, "identity"))),
            ("component_hashes_sha256", _sha256(_json_bytes(components, MAX_ACCOUNT_SOURCE_JSON_BYTES,
                                                               "component hashes"))),
            ("module_sha256", row["module_sha256"]),
            ("state_sha256", _sha256(_json_dump(state).encode("utf-8"))),
            ("result_sha256", _sha256(_json_dump(result).encode("utf-8"))),
        )
        if any(row[name] != digest for name, digest in checks):
            raise PythonReplayAccountError("historical Python replay checkpoint hash failed")
        inner = state.get("inner_v1")
        if type(inner) is not dict or type(state.get("identity")) is not dict:
            raise PythonReplayAccountError("historical Python replay state schema is invalid")
        if type(inner.get("rows_processed")) is not int or type(inner.get("finalized")) is not bool:
            raise PythonReplayAccountError("historical Python replay cursor state is invalid")
        if int(row["cursor"]) != inner["rows_processed"]:
            raise PythonReplayAccountError("historical Python replay cursor does not match its state")
        if int(row["total_bars"]) != len(bars):
            raise PythonReplayAccountError("historical Python replay bar count changed")
        if bool(row["finalized"]) != inner["finalized"]:
            raise PythonReplayAccountError("historical Python replay finalization changed")
        source_manifest = source.get("source_manifest")
        source_hashes = source.get("hashes")
        custom_execution = result.get("custom_execution")
        if type(source_manifest) is not dict or type(source_hashes) is not dict or type(custom_execution) is not dict:
            raise PythonReplayAccountError("historical Python replay source identity is invalid")
        identity_sha256 = _sha256_json(identity)
        if (row["identity_sha256"] != identity_sha256
                or state.get("identity") != identity
                or state.get("identity_sha256") != identity_sha256
                or custom_execution.get("strategy_identity") != identity
                or custom_execution.get("identity_sha256") != identity_sha256):
            raise PythonReplayAccountError("historical Python replay identity does not match its checkpoints")
        strategy_version = source.get("strategy_version")
        if (type(strategy_version) is not str
                or source_manifest.get("strategy_version") != strategy_version
                or identity.get("strategy_version") != strategy_version
                or result.get("strategy_version") != strategy_version):
            raise PythonReplayAccountError("historical Python replay strategy identity is inconsistent")
        if source_manifest != result.get("source_manifest"):
            raise PythonReplayAccountError("historical Python replay source manifests do not match")
        manifest_expectations = {
            "schema_version": identity.get("schema"),
            "strategy_version": strategy_version,
            "source_id": identity.get("source_id"),
            "source_sha256": identity.get("source_sha256"),
            "api_source_sha256": identity.get("api_source_sha256"),
            "sdk_source_sha256": identity.get("sdk_source_sha256"),
            "generic_driver_sha256": identity.get("generic_driver_sha256"),
            "trusted_code_sha256": identity.get("trusted_code_sha256"),
            "legacy_source_sha256": identity.get("legacy_source_sha256"),
            "runtime_pins": identity.get("runtime_pins"),
        }
        if (source_manifest.get("mode") != "RESEARCH_ONLY"
                or source_manifest.get("paper_admission") is not False
                or source_manifest.get("runtime_interface_verified") is not True
                or any(source_manifest.get(key) != value
                       for key, value in manifest_expectations.items())
                or source_manifest.get("api_source_sha256") != components.get("api_source_sha256")
                or source_manifest.get("sdk_source_sha256") != components.get("sdk_source_sha256")
                or source_manifest.get("generic_driver_sha256") != components.get("generic_driver_sha256")
                or source_manifest.get("trusted_code_sha256") != trusted_hashes
                or source_manifest.get("legacy_source_sha256") != components.get("legacy_source_sha256")
                or source_manifest.get("runtime_pins") != components.get("runtime_pins")):
            raise PythonReplayAccountError("historical Python replay source manifest does not match its components")
        registration = source_manifest.get("registration")
        custom_config = source.get("custom_config")
        source_metadata = custom_config.get("source_metadata") if type(custom_config) is dict else None
        if type(registration) is not dict or type(source_metadata) is not dict:
            raise PythonReplayAccountError("historical Python replay registration metadata is invalid")
        registration_expectations = {
            "registration_id": identity.get("source_id"),
            "code_version": identity.get("source_sha256"),
            "interface_contract": source_metadata.get("interface_contract"),
            "name": source_metadata.get("name"),
            "description": source_metadata.get("description"),
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
            "runtime_interface_verified": False,
            "dependencies_verified": False,
            "import_declaration_status": "DECLARED_NOT_VERIFIED",
        }
        if (type(registration.get("schema_version")) is not int
                or registration.get("schema_version") < 1
                or registration.get("runtime_interface_verified") is not False
                or registration.get("dependencies_verified") is not False
                or any(registration.get(key) != value
                       for key, value in registration_expectations.items())):
            raise PythonReplayAccountError("historical Python replay registration metadata is inconsistent")
        metadata_expectations = {
            "registration_id": identity.get("source_id"),
            "name": registration.get("name"),
            "description": registration.get("description"),
            "interface_contract": registration.get("interface_contract"),
            "source_sha256": identity.get("source_sha256"),
            "code_version": identity.get("source_sha256"),
        }
        if any(source_metadata.get(key) != value for key, value in metadata_expectations.items()):
            raise PythonReplayAccountError("historical Python replay source metadata is inconsistent")
        if (source_hashes.get("source_sha256") != _sha256(bytes(row["user_source"]))
                or source_hashes.get("api_source_sha256") != _sha256(bytes(row["api_source"]))
                or source_hashes.get("trusted_code_sha256") != row["engine_code_sha256"]
                or source_hashes.get("python_replay_module_sha256") != row["module_sha256"]
                or source_manifest.get("source_sha256") != _sha256(bytes(row["user_source"]))
                or source_manifest.get("api_source_sha256") != _sha256(bytes(row["api_source"]))):
            raise PythonReplayAccountError("historical Python replay source hashes are inconsistent")
        return {"config": config, "execution_config": execution_config, "source": source,
                "bars": bars, "manifest": manifest, "readiness": readiness,
                "identity": identity, "components": components, "state": state, "result": result}

    def _code_current(self, row: sqlite3.Row, verified: dict[str, Any]) -> bool:
        try:
            return (_current_module_sha256() == row["module_sha256"]
                    and _current_trusted_code_hashes() == verified["components"].get("trusted_code_sha256"))
        except (OSError, PythonReplayAccountError):
            return False

    def _record_from_row(self, row: sqlite3.Row, verified: dict[str, Any]) -> dict[str, Any]:
        result = verified["result"]
        source = verified["source"]
        code_current = self._code_current(row, verified)
        cursor = int(row["cursor"])
        total = int(row["total_bars"])
        finalized = bool(row["finalized"])
        return {
            "continuation_allowed": code_current and not finalized,
            "code_current": code_current,
            "account_id": row["account_id"], "run_id": row["run_id"],
            "label": row["label"], "status": row["status"],
            "revision": int(row["revision"]), "cursor": cursor,
            "total_bars": total, "remaining_bars": max(0, total - cursor),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "strategy": "custom_python", "strategy_version": source["strategy_version"],
            "source_hashes": _json_copy(source["hashes"]),
            "dataset_hashes": _json_copy(source["dataset_hashes"]),
            "code_hash": row["code_hash"], "finalized": finalized,
            "current_equity": (result.get("equity") or [{}])[-1].get("equity"),
            "mode": "RESEARCH_ONLY", "paper_admission": False,
            "kind": "historical_python_replay",
        }

    def _detail_from_connection(self, connection: sqlite3.Connection, account_id: str,
                                *, include_events: bool = True) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("historical Python replay account not found")
        verified = self._verify_row(row)
        if include_events:
            events = [{
                "event_id": int(item["event_id"]), "event_type": item["event_type"],
                "created_at": item["created_at"], "revision": int(item["revision"]),
                "details": json.loads(item["details_json"]),
            } for item in connection.execute(
                "SELECT * FROM events WHERE account_id=? ORDER BY event_id", (account_id,)
            )]
        else:
            events = []
        result = verified["result"]
        return {
            "record": self._record_from_row(row, verified),
            "config": _public_config(verified["config"]),
            "source": verified["source"],
            "data_readiness": _json_copy(verified["readiness"]),
            "result": _json_copy(result),
            "equity": _json_copy(result.get("equity", [])),
            "orders": _json_copy(result.get("orders", [])),
            "trades": _json_copy(result.get("trades", [])),
            "positions": _json_copy(result.get("positions", [])),
            "bars": _json_copy(result.get("bars", [])),
            "events": events,
        }

    def list(self) -> list[dict[str, Any]]:
        if self._database_stat() is None:
            return []
        try:
            connection = self._connect_readonly()
            try:
                rows = connection.execute("SELECT * FROM accounts ORDER BY created_at DESC,account_id").fetchall()
                return [self._record_from_row(row, self._verify_row(row)) for row in rows]
            finally:
                connection.close()
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay accounts cannot be listed") from None

    def detail(self, account_id: str) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        if self._database_stat() is None:
            raise FileNotFoundError("historical Python replay account not found")
        try:
            connection = self._connect_readonly()
            try:
                connection.execute("BEGIN")
                return self._detail_from_connection(connection, account_id)
            finally:
                connection.close()
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay account cannot be read") from None

    @staticmethod
    def _check_expected_revision(expected_revision: Any, actual: int) -> int:
        if type(expected_revision) is not int or expected_revision < 0:
            raise PythonReplayAccountError("expected_revision must be a non-negative integer")
        if expected_revision != actual:
            raise PythonReplayRevisionConflict(f"stale revision: expected {expected_revision}, current {actual}")
        return expected_revision

    def _assert_current(self, row: sqlite3.Row, verified: dict[str, Any]) -> Any:
        expected = {
            "strategy_version": verified["source"]["strategy_version"],
            "identity": verified["identity"],
            "user_source": bytes(row["user_source"]),
            "api_source": bytes(row["api_source"]),
            "component_hashes": verified["components"],
            "module_sha256": row["module_sha256"],
        }
        prepared = self._current_prepared(
            json.loads(row["request_config_json"]), expected, self._source_store()
        )
        if (_json_dump(prepared.normalized) != _json_dump(verified["config"])
                or _json_dump(prepared.execution_config) != _json_dump(verified["execution_config"])):
            raise PythonReplaySourceChanged("normalized account config changed; account cannot continue")
        return prepared

    def resume(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("historical Python replay account not found")
            verified = self._verify_row(row)
            self._check_expected_revision(expected_revision, int(row["revision"]))
            if row["status"] != "paused":
                raise PythonReplayAccountError("only a paused, unfinished account can resume")
            if bool(row["finalized"]):
                raise PythonReplayAccountError("finished account cannot resume")
            self._assert_current(row, verified)
            revision = int(row["revision"]) + 1
            now = _now()
            values = {key: row[key] for key in (
                "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
                "total_bars", "created_at", "updated_at", "finalized", *_HASH_FIELDS,
            )}
            values.update(status="running", revision=revision, updated_at=now)
            checksum = _row_checksum(values)
            connection.execute("UPDATE accounts SET status='running',revision=?,updated_at=?,row_checksum=? WHERE account_id=?",
                               (revision, now, checksum, account_id))
            self._insert_event(connection, account_id, "resume", revision, {})
            return self._detail_from_connection(connection, account_id)

    def pause(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("historical Python replay account not found")
            verified = self._verify_row(row)
            self._check_expected_revision(expected_revision, int(row["revision"]))
            if row["status"] != "running":
                raise PythonReplayAccountError("only a running account can pause")
            revision = int(row["revision"]) + 1
            now = _now()
            values = {key: row[key] for key in (
                "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
                "total_bars", "created_at", "updated_at", "finalized", *_HASH_FIELDS,
            )}
            values.update(status="paused", revision=revision, updated_at=now)
            checksum = _row_checksum(values)
            connection.execute("UPDATE accounts SET status='paused',revision=?,updated_at=?,row_checksum=? WHERE account_id=?",
                               (revision, now, checksum, account_id))
            self._insert_event(connection, account_id, "pause", revision, {})
            return self._detail_from_connection(connection, account_id)

    def _new_adapter(self, row: sqlite3.Row, verified: dict[str, Any], prepared: Any) -> Any:
        try:
            return GuestStrategyAdapter(
                bytes(row["user_source"]), prepared.normalized["symbol"],
                copy.deepcopy(prepared.normalized["custom_params"]), configured_runtime(),
                state=copy.deepcopy(verified["state"]["user_state"]),
                history_limit=prepared.normalized["history_limit"],
                api_source=bytes(row["api_source"]),
            )
        except BaseException:
            raise PythonReplayAccountError("guest strategy could not be restored from its checkpoint") from None

    @staticmethod
    def _advance_with_close(prepared: Any, state: dict[str, Any], batch: list[dict[str, Any]],
                            adapter: Any, *, final: bool) -> dict[str, Any]:
        provisional = None
        advance_failed = False
        try:
            provisional = custom_kernel.advance(
                prepared.execution_config, state, batch, adapter, final=final,
            )
        except BaseException:
            advance_failed = True
        close_failed = False
        try:
            adapter.close()
        except BaseException:
            close_failed = True
        if close_failed:
            raise PythonReplayAccountError("guest strategy close failed; checkpoint was not committed") from None
        if advance_failed:
            raise PythonReplayAccountError("guest strategy callback failed; checkpoint was not committed") from None
        if type(provisional) is not dict:
            raise PythonReplayAccountError("custom replay kernel returned an invalid checkpoint")
        return provisional

    def step(self, account_id: str, steps: int, expected_revision: int,
             request_id: str) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        if type(steps) is not int or not 1 <= steps <= MAX_STEP_BARS:
            raise PythonReplayAccountError(f"steps must be an integer from 1 to {MAX_STEP_BARS}")
        if type(expected_revision) is not int or expected_revision < 0:
            raise PythonReplayAccountError("expected_revision must be a non-negative integer")
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise PythonReplayAccountError("request_id must be 1-128 ASCII letters, digits, '.', '_', ':', or '-'")
        payload = {"steps": steps, "expected_revision": expected_revision}
        payload_json = _json_dump(payload)
        try:
            with self._transaction() as connection:
                prior = connection.execute(
                    "SELECT payload_json,response_json FROM requests WHERE account_id=? AND request_id=?",
                    (account_id, request_id),
                ).fetchone()
                if prior is not None:
                    if prior["payload_json"] != payload_json:
                        raise PythonReplayAccountError("request_id was already used with a different payload")
                    response = self._detail_from_connection(connection, account_id)
                    response.update(json.loads(prior["response_json"]))
                    response["replayed"] = True
                    return response

                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("historical Python replay account not found")
                verified = self._verify_row(row)
                self._check_expected_revision(expected_revision, int(row["revision"]))
                if row["status"] != "running":
                    raise PythonReplayAccountError("account must be resumed before stepping")
                if bool(row["finalized"]):
                    raise PythonReplayAccountError("source range is complete; no more bars can be processed")
                prepared = self._assert_current(row, verified)
                cursor = int(row["cursor"])
                total = int(row["total_bars"])
                batch = verified["bars"][cursor:min(total, cursor + steps)]
                if not batch:
                    raise PythonReplayAccountError("no saved source bars remain to process")
                new_cursor = cursor + len(batch)
                final = new_cursor == total
                adapter = self._new_adapter(row, verified, prepared)
                next_state = self._advance_with_close(
                    prepared, verified["state"], batch, adapter, final=final,
                )
                if (int(next_state.get("inner_v1", {}).get("rows_processed", -1)) != new_cursor
                        or bool(next_state.get("inner_v1", {}).get("finalized")) != final
                        or next_state.get("user_state") != adapter.state):
                    raise PythonReplayAccountError("custom replay checkpoint does not match the submitted bars")
                result = self._make_result(
                    prepared, next_state, verified["manifest"], verified["readiness"],
                    verified["source"]["source_manifest"],
                )
                self._assert_current(row, verified)
                state_json = _json_dump(next_state)
                result_json = _json_dump(result)
                if len(state_json.encode("utf-8")) > MAX_ACCOUNT_JSON_BYTES:
                    raise PythonReplayAccountError("custom replay state exceeds account size limit")
                if len(result_json.encode("utf-8")) > MAX_ACCOUNT_JSON_BYTES:
                    raise PythonReplayAccountError("custom replay result exceeds account size limit")

                ending_status = "completed" if final else "running"
                revision = int(row["revision"]) + 1
                now = _now()
                state_hash = _sha256(state_json.encode("utf-8"))
                result_hash = _sha256(result_json.encode("utf-8"))
                values = {key: row[key] for key in (
                    "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
                    "total_bars", "created_at", "updated_at", "finalized", *_HASH_FIELDS,
                )}
                values.update(status=ending_status, revision=revision, cursor=new_cursor,
                              updated_at=now, state_sha256=state_hash, result_sha256=result_hash,
                              finalized=int(final))
                checksum = _row_checksum(values)
                connection.execute("""UPDATE accounts SET status=?,revision=?,cursor=?,updated_at=?,
                    state_json=?,result_json=?,state_sha256=?,result_sha256=?,finalized=?,row_checksum=?
                    WHERE account_id=?""",
                    (ending_status, revision, new_cursor, now, state_json, result_json,
                     state_hash, result_hash, int(final), checksum, account_id))
                self._insert_event(connection, account_id, "step", revision, {
                    "request_id": request_id, "steps_requested": steps,
                    "bars_processed": len(batch), "cursor_before": cursor,
                    "cursor_after": new_cursor, "final": final,
                })
                if final:
                    self._insert_event(connection, account_id, "complete", revision,
                                       {"cursor": new_cursor, "total_bars": total})
                ack = {
                    "request_id": request_id,
                    "original_revision": int(row["revision"]),
                    "original_cursor": cursor,
                    "applied_revision": revision,
                    "applied_cursor": new_cursor,
                    "bars_processed": len(batch),
                    "status_at_apply": ending_status,
                }
                response = self._detail_from_connection(connection, account_id)
                response.update(ack)
                response["replayed"] = False
                connection.execute(
                    "INSERT INTO requests(account_id,request_id,payload_json,response_json,created_at) VALUES(?,?,?,?,?)",
                    (account_id, request_id, payload_json, _json_dump(ack), now),
                )
                return response
        except sqlite3.Error:
            raise PythonReplayAccountError("historical Python replay step could not be committed") from None

    def events(self, account_id: str) -> list[dict[str, Any]]:
        return self.detail(account_id)["events"]

    def csv_bytes(self, account_id: str, kind: str) -> bytes:
        if kind not in _EXPORTS:
            raise PythonReplayAccountError("supported exports are equity, orders, trades, positions, and events")
        detail = self.detail(account_id)
        rows = detail["events"] if kind == "events" else detail[kind]
        if not rows:
            return b""
        fields = list(dict.fromkeys(key for row in rows for key in row))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")

    export_csv = csv_bytes


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
