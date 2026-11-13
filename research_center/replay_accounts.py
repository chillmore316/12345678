"""Persistent, isolated accounts for manually stepped historical replays.

This module never opens the minute-data lake and never starts a worker.  It
freezes bars from a completed reference-backend research run, then feeds only
new bars to the shared reference replay kernel inside SQLite transactions.
"""
from __future__ import annotations

import copy
import csv
from contextlib import contextmanager
import hashlib
import importlib
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

from .store import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_RESULT_BYTES = 64 * 1024 * 1024
MAX_PROVENANCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BARS = 31 * 240 + 240
MAX_STEP_BARS = 240
ACCOUNT_SCHEMA_VERSION = 1
_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EXPORTS = frozenset({"equity", "orders", "trades", "positions", "events"})


class ReplayAccountError(ValueError):
    """A replay request violates its frozen-source or account contract."""


class RevisionConflict(ReplayAccountError):
    """The caller's optimistic revision is stale."""


class SourceCodeChanged(ReplayAccountError):
    """The engine/strategy code changed after the account was created."""


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _json_copy(value: Any) -> Any:
    return json.loads(_json_dump(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_uuid_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID_HEX.fullmatch(value):
        raise ReplayAccountError(f"{field} must be a 32-character lowercase UUID hex")
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ReplayAccountError(f"{field} must be a valid UUID") from exc
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stable_read_json(path: Path, max_bytes: int) -> tuple[bytes, Any]:
    """Read one regular file without following a symlink or accepting races."""
    try:
        before_path = path.lstat()
    except FileNotFoundError as exc:
        raise ReplayAccountError(f"required source file is missing: {path.name}") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ReplayAccountError(f"source file must be a regular non-symlink file: {path.name}")
    if before_path.st_size > max_bytes:
        raise ReplayAccountError(f"source file exceeds the {max_bytes}-byte limit: {path.name}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReplayAccountError(f"cannot safely open source file: {path.name}") from exc
    try:
        opened_before = os.fstat(fd)
        if not stat.S_ISREG(opened_before.st_mode):
            raise ReplayAccountError(f"source file is not regular: {path.name}")
        identity = _stat_identity(opened_before)
        if identity != _stat_identity(before_path):
            raise ReplayAccountError(f"source file changed before read: {path.name}")
        if opened_before.st_size > max_bytes:
            raise ReplayAccountError(f"source file exceeds the {max_bytes}-byte limit: {path.name}")
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(fd, min(1024 * 1024, max_bytes + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > max_bytes:
                raise ReplayAccountError(f"source file exceeds the {max_bytes}-byte limit: {path.name}")
        after_fd = os.fstat(fd)
        after_path = path.lstat()
        if (_stat_identity(after_fd) != identity or _stat_identity(after_path) != identity
                or stat.S_ISLNK(after_path.st_mode)):
            raise ReplayAccountError(f"source file changed during read: {path.name}")
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayAccountError(f"source file is not valid JSON: {path.name}") from exc
    return raw, value


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_ctime_ns))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _kernel_api():
    return importlib.import_module(".replay_kernel", __package__)


def _code_identity() -> dict[str, str]:
    """Hash the exact strategy, execution engine, and shared kernel sources."""
    kernel_path = Path(_kernel_api().__file__).resolve()
    paths = (
        PROJECT_ROOT / "research_center" / "engine.py",
        PROJECT_ROOT / "research_center" / "strategies.py",
        PROJECT_ROOT / "research_center" / "replay_accounts.py",
        PROJECT_ROOT / "strategy" / "base.py",
        kernel_path,
    )
    hashes: dict[str, str] = {}
    for path in paths:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(PROJECT_ROOT).as_posix()
        except ValueError as exc:
            raise ReplayAccountError("strategy/engine source must remain inside the project") from exc
        if path.is_symlink() or not resolved.is_file():
            raise ReplayAccountError(f"strategy/engine source is not a regular file: {relative}")
        hashes[relative] = _sha256(resolved.read_bytes())
    return hashes


def _code_digest(source_hashes: dict[str, str]) -> str:
    return _sha256(_json_dump(source_hashes).encode("utf-8"))


def _validate_bars(value: Any, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReplayAccountError("completed reference run has no replayable source bars")
    if len(value) > MAX_SOURCE_BARS:
        raise ReplayAccountError(f"source bars exceed the {MAX_SOURCE_BARS}-row replay limit")
    required = {"datetime", "bar_start_datetime", "source_datetime", "symbol", "open", "high",
                "low", "close", "volume", "turnover"}
    result: list[dict[str, Any]] = []
    previous: str | None = None
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ReplayAccountError(f"source bar {index} has an invalid schema")
        bar = _json_copy(raw)
        for key in ("datetime", "bar_start_datetime", "source_datetime", "symbol"):
            if not isinstance(bar.get(key), str) or not bar[key]:
                raise ReplayAccountError(f"source bar {index} has invalid {key}")
        if bar["symbol"] != symbol:
            raise ReplayAccountError(f"source bar {index} does not match the frozen symbol")
        try:
            source_time = datetime.fromisoformat(bar["source_datetime"])
            bar_start = datetime.fromisoformat(bar["bar_start_datetime"])
            datetime_value = datetime.fromisoformat(bar["datetime"])
        except ValueError as exc:
            raise ReplayAccountError(f"source bar {index} has invalid timestamps") from exc
        if (source_time.tzinfo is not None or bar_start.tzinfo is not None or datetime_value.tzinfo is not None
                or any(item.second or item.microsecond for item in (source_time, bar_start, datetime_value))
                or source_time - bar_start != timedelta(minutes=1)
                or datetime_value != bar_start):
            raise ReplayAccountError(f"source bar {index} violates the bar-end/bar-start timestamp contract")
        if not start <= source_time.date().isoformat() <= end:
            raise ReplayAccountError(f"source bar {index} is outside the frozen configured date range")
        session_time = source_time.strftime("%H:%M")
        if not ("09:31" <= session_time <= "11:30" or "13:01" <= session_time <= "15:00"):
            raise ReplayAccountError(f"source bar {index} is outside the continuous session")
        if previous is not None and bar["source_datetime"] <= previous:
            raise ReplayAccountError("source bars must be strictly ordered by source_datetime")
        previous = bar["source_datetime"]
        for key in ("open", "high", "low", "close", "volume", "turnover"):
            item = bar.get(key)
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
                raise ReplayAccountError(f"source bar {index} has invalid numeric field {key}")
        if (any(float(bar[key]) <= 0 for key in ("open", "high", "low", "close"))
                or float(bar["volume"]) < 0 or float(bar["turnover"]) < 0
                or float(bar["high"]) < max(float(bar["open"]), float(bar["close"]), float(bar["low"]))
                or float(bar["low"]) > min(float(bar["open"]), float(bar["close"]), float(bar["high"]))):
            raise ReplayAccountError(f"source bar {index} has inconsistent OHLC/volume values")
        result.append(bar)
    return result


class ReplayAccounts:
    """SQLite-backed manually advanced accounts isolated under a RunStore.

    There is intentionally no root argument.  A source RunStore's
    ``root/replay_accounts/accounts.sqlite3`` is the only accepted location.
    """

    def __init__(self, run_store: RunStore):
        if not isinstance(run_store, RunStore):
            raise TypeError("run_store must be a research_center.store.RunStore")
        self.run_store = run_store
        self.root = Path(run_store.root).resolve() / "replay_accounts"
        self._validate_root(self.root)
        if self.root.exists() and self.root.is_symlink():
            raise ReplayAccountError("replay account root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_root(self.root.resolve())
        root_stat = self.root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ReplayAccountError("replay account root must be a regular directory")
        self.root = self.root.resolve()
        self.database = self.root / "accounts.sqlite3"
        self._initialize()

    @staticmethod
    def _validate_root(root: Path) -> None:
        normalized = root.expanduser().resolve()
        parts = {part.casefold() for part in normalized.parts}
        if "quantlivepaper" in parts:
            raise ReplayAccountError("replay account root cannot be inside QuantLivePaper")

        # Reject locations in the project's source-data tree and the engine's
        # original JoinQuant minute archive.  The check is path-only and does
        # not open or enumerate either data location.
        from .engine import DEFAULT_DATA_ROOT, PROJECT_ROOT as ENGINE_PROJECT_ROOT

        blocked = (
            ENGINE_PROJECT_ROOT / "data",
            Path(DEFAULT_DATA_ROOT),
        )
        for forbidden in blocked:
            forbidden_path = forbidden.expanduser().resolve()
            if _is_within(normalized, forbidden_path):
                raise ReplayAccountError("replay account root cannot be inside an original data root")

    def _connect(self) -> sqlite3.Connection:
        if self.database.exists():
            db_stat = self.database.lstat()
            if stat.S_ISLNK(db_stat.st_mode) or not stat.S_ISREG(db_stat.st_mode):
                raise ReplayAccountError("replay account database must be a regular non-symlink file")
        connection = sqlite3.connect(self.database, timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        if self.database.exists():
            db_stat = self.database.lstat()
            if stat.S_ISLNK(db_stat.st_mode) or not stat.S_ISREG(db_stat.st_mode):
                raise ReplayAccountError("replay account database must be a regular non-symlink file")
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('paused','running','completed')),
                    revision INTEGER NOT NULL,
                    cursor INTEGER NOT NULL,
                    total_bars INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    frozen_bars_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    code_hashes_json TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    finalized INTEGER NOT NULL CHECK(finalized IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS accounts_created_idx ON accounts(created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_account_idx ON events(account_id,event_id);
                CREATE TABLE IF NOT EXISTS requests (
                    account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,request_id)
                );
                """
            )

    @contextmanager
    def _transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_event(self, connection: sqlite3.Connection, account_id: str, event_type: str,
                      revision: int, details: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO events(account_id,event_type,created_at,revision,details_json) VALUES(?,?,?,?,?)",
            (account_id, event_type, _now(), revision, _json_dump(details or {})),
        )

    def _log_error(self, account_id: str, action: str, exc: BaseException) -> None:
        """Record a failed action after its state transaction has rolled back."""
        try:
            with self._transaction() as connection:
                exists = connection.execute(
                    "SELECT revision FROM accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                if exists:
                    self._insert_event(connection, account_id, "error", int(exists["revision"]), {
                        "action": action,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:1200],
                    })
        except Exception:
            # The original failure is authoritative; logging must not disguise it.
            pass

    @staticmethod
    def _validate_label(label: Any) -> str:
        if not isinstance(label, str) or len(label) > 80:
            raise ReplayAccountError("label must be a string of at most 80 characters")
        return label

    def create(self, run_id: str, label: str = "") -> dict[str, Any]:
        run_id = _valid_uuid_hex(run_id, "run_id")
        label = self._validate_label(label)
        record = self.run_store.get(run_id)
        if record.get("status") != "completed":
            raise ReplayAccountError("source run must be completed")
        if record.get("backend") != "reference":
            raise ReplayAccountError("only completed reference-backend source runs are supported")

        source_config = record.get("config")
        if not isinstance(source_config, dict):
            raise ReplayAccountError("source run config is missing or invalid")
        if source_config.get("backend", "reference") != "reference":
            raise ReplayAccountError("native source runs are not supported")
        from .engine import validate_config
        from .registry import strategy_version

        config = validate_config(source_config)
        from .registry import STRATEGIES
        if config["strategy"] not in STRATEGIES:
            raise ReplayAccountError("source strategy is not allowlisted")
        current_strategy_version = strategy_version(config["strategy"])
        version_values = (
            record.get("strategy_version"), source_config.get("strategy_version"),
        )
        if any(not isinstance(value, str) or not value for value in version_values):
            raise ReplayAccountError("source run has no frozen strategy version")

        run_directory = self.run_store.directory(run_id)
        if run_directory.is_symlink() or not _is_within(run_directory.resolve(), Path(self.run_store.root).resolve()):
            raise ReplayAccountError("source run directory must remain inside its RunStore")
        result_raw, source_result = _stable_read_json(run_directory / "result.json", MAX_SOURCE_RESULT_BYTES)
        provenance_raw, provenance = _stable_read_json(run_directory / "provenance.json", MAX_PROVENANCE_BYTES)
        if not isinstance(source_result, dict) or not isinstance(provenance, dict):
            raise ReplayAccountError("source result and provenance must be JSON objects")
        if source_result.get("backend") != "reference":
            raise ReplayAccountError("source result is not from the reference backend")
        if any(value != current_strategy_version for value in version_values):
            raise ReplayAccountError("source strategy version is old or does not match current code")
        if source_result.get("strategy_version") != current_strategy_version:
            raise ReplayAccountError("source result strategy version is missing or outdated")
        if provenance.get("backend") != "reference" or provenance.get("strategy_version") != current_strategy_version:
            raise ReplayAccountError("source provenance backend/version is missing or outdated")

        bars = _validate_bars(source_result.get("bars"), config["symbol"], config["start"], config["end"])
        source_readiness = source_result.get("data_readiness")
        if not isinstance(source_readiness, dict):
            source_readiness = {
                "status": "unverified",
                "reason": "source_run_did_not_save_data_readiness",
                "checks": [],
                "paper_admission": False,
                "origin": "source_run_snapshot",
            }
        else:
            source_readiness = _json_copy(source_readiness)
            # Historical readiness details remain intact, while this account
            # can never be represented as paper admission.
            source_readiness["paper_admission"] = False
        source_manifest = source_result.get("data_manifest")
        if not isinstance(source_manifest, dict):
            raise ReplayAccountError("source result has no frozen data manifest")
        dataset_hashes = _json_copy(source_manifest.get("used_source_files", []))
        if not isinstance(dataset_hashes, list):
            raise ReplayAccountError("source data manifest has invalid dataset hash entries")
        source_config_hash = _sha256(_json_dump(source_config).encode("utf-8"))
        hashes = {
            "source_result_sha256": _sha256(result_raw),
            "source_config_sha256": source_config_hash,
            "source_provenance_sha256": _sha256(provenance_raw),
            "source_bars_sha256": _sha256(_json_dump(bars).encode("utf-8")),
        }
        source = {
            "run_id": run_id,
            "backend": "reference",
            "strategy": config["strategy"],
            "strategy_version": current_strategy_version,
            "hashes": hashes,
            "dataset_hashes": dataset_hashes,
            "source_config": _json_copy(source_config),
            "provenance": _json_copy(provenance),
            "data_readiness": source_readiness,
            "request_config": {"run_id": run_id, "label": label},
        }
        manifest = _json_copy(source_manifest)
        manifest["replay_account"] = {
            "schema_version": ACCOUNT_SCHEMA_VERSION,
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
            "source_run_id": run_id,
            "source_result_sha256": hashes["source_result_sha256"],
            "source_config_sha256": hashes["source_config_sha256"],
            "source_provenance_sha256": hashes["source_provenance_sha256"],
            "source_bars_sha256": hashes["source_bars_sha256"],
            "recomputed_from_initial_cash": True,
        }
        code_hashes = _code_identity()
        code_hash = _code_digest(code_hashes)
        kernel = _kernel_api()
        state = kernel.initial_state(config)
        if not isinstance(state, dict):
            raise ReplayAccountError("replay kernel returned an invalid initial state")
        state_json = _json_dump(state)
        result = self._make_result(config, state, manifest, source_readiness)
        result_json = _json_dump(result)
        # Detect a source edit racing with account initialization.
        if _code_digest(_code_identity()) != code_hash:
            raise SourceCodeChanged("strategy/engine code changed while creating the account")

        now = _now()
        account_id = uuid.uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO accounts(
                    account_id,run_id,label,status,revision,cursor,total_bars,created_at,updated_at,
                    config_json,source_json,frozen_bars_json,manifest_json,state_json,result_json,
                    code_hashes_json,code_hash,finalized
                ) VALUES(?,?,?,'paused',0,0,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (account_id, run_id, label, len(bars), now, now, _json_dump(config), _json_dump(source),
                 _json_dump(bars), _json_dump(manifest), state_json, result_json,
                 _json_dump(code_hashes), code_hash),
            )
            self._insert_event(connection, account_id, "create", 0, {
                "source_run_id": run_id,
                "source_result_sha256": hashes["source_result_sha256"],
                "total_bars": len(bars),
                "status": "paused",
                "mode": "RESEARCH_ONLY",
            })
        return self.detail(account_id)

    @staticmethod
    def _make_result(config: dict[str, Any], state: dict[str, Any], manifest: dict[str, Any],
                     data_readiness: dict[str, Any]) -> dict[str, Any]:
        result = _kernel_api().result_from_state(config, state, manifest)
        if not isinstance(result, dict):
            raise ReplayAccountError("replay kernel returned an invalid result")
        result = _json_copy(result)
        result["bars"] = _json_copy(state.get("bars", []))
        # Preserve kernel-derived current fees, not the source run's final fees.
        result["data_readiness"] = _json_copy(data_readiness)
        result["mode"] = "RESEARCH_ONLY"
        result["paper_admission"] = False
        return result

    def _record_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        source = json.loads(row["source_json"])
        try:
            code_current = _code_digest(_code_identity()) == row["code_hash"]
        except (OSError, ValueError):
            code_current = False
        return {
            "continuation_allowed": code_current and not bool(row["finalized"]),
            "code_current": code_current,
            "account_id": row["account_id"],
            "run_id": row["run_id"],
            "label": row["label"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "cursor": int(row["cursor"]),
            "total_bars": int(row["total_bars"]),
            "remaining_bars": max(0, int(row["total_bars"]) - int(row["cursor"])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "strategy": source["strategy"],
            "strategy_version": source["strategy_version"],
            "source_hashes": _json_copy(source["hashes"]),
            "dataset_hashes": _json_copy(source["dataset_hashes"]),
            "code_hash": row["code_hash"],
            "finalized": bool(row["finalized"]),
            "current_equity": (result.get("equity") or [{}])[-1].get("equity"),
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
        }

    def _detail_from_connection(self, connection: sqlite3.Connection, account_id: str,
                                *, include_events: bool = True) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("replay account not found")
        config = json.loads(row["config_json"])
        source = json.loads(row["source_json"])
        result = json.loads(row["result_json"])
        events = []
        if include_events:
            events = [
                {"event_id": int(item["event_id"]), "event_type": item["event_type"],
                 "created_at": item["created_at"], "revision": int(item["revision"]),
                 "details": json.loads(item["details_json"])}
                for item in connection.execute(
                    "SELECT * FROM events WHERE account_id=? ORDER BY event_id", (account_id,)
                )
            ]
        record = self._record_from_row(row)
        return {
            "record": record,
            "config": config,
            "source": source,
            "data_readiness": _json_copy(source["data_readiness"]),
            "result": result,
            "equity": _json_copy(result.get("equity", [])),
            "orders": _json_copy(result.get("orders", [])),
            "trades": _json_copy(result.get("trades", [])),
            "positions": _json_copy(result.get("positions", [])),
            "bars": _json_copy(result.get("bars", [])),
            "events": events,
        }

    def list(self) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY created_at DESC,account_id").fetchall()
            return [self._record_from_row(row) for row in rows]

    def detail(self, account_id: str) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        with self._transaction(immediate=False) as connection:
            return self._detail_from_connection(connection, account_id)

    def _check_expected_revision(self, expected_revision: Any, actual: int) -> int:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ReplayAccountError("expected_revision must be a non-negative integer")
        if expected_revision != actual:
            raise RevisionConflict(f"stale revision: expected {expected_revision}, current {actual}")
        return expected_revision

    def _assert_code_unchanged(self, row: sqlite3.Row) -> None:
        actual_hashes = _code_identity()
        if _code_digest(actual_hashes) != row["code_hash"]:
            raise SourceCodeChanged("strategy/engine source changed; this account is readable but cannot continue")

    def resume(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        try:
            with self._transaction() as connection:
                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("replay account not found")
                self._check_expected_revision(expected_revision, int(row["revision"]))
                if row["status"] != "paused":
                    raise ReplayAccountError("only a paused, unfinished account can resume")
                if bool(row["finalized"]):
                    raise ReplayAccountError("finished account cannot resume")
                self._assert_code_unchanged(row)
                revision = int(row["revision"]) + 1
                now = _now()
                connection.execute("UPDATE accounts SET status='running',revision=?,updated_at=? WHERE account_id=?",
                                   (revision, now, account_id))
                self._insert_event(connection, account_id, "resume", revision, {})
                return self._detail_from_connection(connection, account_id)
        except Exception as exc:
            self._log_error(account_id, "resume", exc)
            raise

    def pause(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        try:
            with self._transaction() as connection:
                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("replay account not found")
                self._check_expected_revision(expected_revision, int(row["revision"]))
                if row["status"] != "running":
                    raise ReplayAccountError("only a running account can pause")
                revision = int(row["revision"]) + 1
                now = _now()
                connection.execute("UPDATE accounts SET status='paused',revision=?,updated_at=? WHERE account_id=?",
                                   (revision, now, account_id))
                self._insert_event(connection, account_id, "pause", revision, {})
                return self._detail_from_connection(connection, account_id)
        except Exception as exc:
            self._log_error(account_id, "pause", exc)
            raise

    def step(self, account_id: str, steps: int, expected_revision: int,
             request_id: str) -> dict[str, Any]:
        account_id = _valid_uuid_hex(account_id, "account_id")
        try:
            if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEP_BARS:
                raise ReplayAccountError(f"steps must be an integer from 1 to {MAX_STEP_BARS}")
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise ReplayAccountError("request_id must be 1-128 ASCII letters, digits, '.', '_', ':', or '-'")
            request_payload = {"steps": steps, "expected_revision": expected_revision}
            payload_json = _json_dump(request_payload)
            with self._transaction() as connection:
                prior = connection.execute(
                    "SELECT payload_json,response_json FROM requests WHERE account_id=? AND request_id=?",
                    (account_id, request_id),
                ).fetchone()
                if prior is not None:
                    if prior["payload_json"] != payload_json:
                        raise ReplayAccountError("request_id was already used with a different payload")
                    ack = json.loads(prior["response_json"])
                    response = self._detail_from_connection(connection, account_id)
                    response.update(ack)
                    response["replayed"] = True
                    return response

                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("replay account not found")
                self._check_expected_revision(expected_revision, int(row["revision"]))
                if row["status"] != "running":
                    raise ReplayAccountError("account must be resumed before stepping")
                if bool(row["finalized"]):
                    raise ReplayAccountError("source range is complete; no more bars can be processed")
                self._assert_code_unchanged(row)

                cursor = int(row["cursor"])
                total = int(row["total_bars"])
                frozen_bars = json.loads(row["frozen_bars_json"])
                batch = frozen_bars[cursor:min(total, cursor + steps)]
                if not batch:
                    raise ReplayAccountError("no source bars remain to process")
                new_cursor = cursor + len(batch)
                final = new_cursor == total
                config = json.loads(row["config_json"])
                state = json.loads(row["state_json"])
                next_state = _kernel_api().advance(config, state, batch, final=final)
                if not isinstance(next_state, dict):
                    raise ReplayAccountError("replay kernel returned an invalid state")
                if int(next_state.get("rows_processed", -1)) != new_cursor:
                    raise ReplayAccountError("replay kernel cursor does not match the submitted bars")
                if bool(next_state.get("finalized")) != final:
                    raise ReplayAccountError("replay kernel finalization does not match source exhaustion")
                state_json = _json_dump(next_state)
                manifest = json.loads(row["manifest_json"])
                source = json.loads(row["source_json"])
                result = self._make_result(config, next_state, manifest, source["data_readiness"])
                result_json = _json_dump(result)
                # Include source exhaustion in the same atomic account commit.
                if final:
                    ending_status = "completed"
                else:
                    ending_status = "running"
                revision = int(row["revision"]) + 1
                now = _now()
                connection.execute(
                    """UPDATE accounts SET status=?,revision=?,cursor=?,updated_at=?,state_json=?,
                       result_json=?,finalized=? WHERE account_id=?""",
                    (ending_status, revision, new_cursor, now, state_json, result_json,
                     int(final), account_id),
                )
                self._insert_event(connection, account_id, "step", revision, {
                    "request_id": request_id,
                    "steps_requested": steps,
                    "bars_processed": len(batch),
                    "cursor_before": cursor,
                    "cursor_after": new_cursor,
                    "final": final,
                })
                if final:
                    self._insert_event(connection, account_id, "complete", revision, {
                        "cursor": new_cursor,
                        "total_bars": total,
                    })
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
        except Exception as exc:
            self._log_error(account_id, "step", exc)
            raise

    def events(self, account_id: str) -> list[dict[str, Any]]:
        return self.detail(account_id)["events"]

    def csv_bytes(self, account_id: str, name: str) -> bytes:
        if name not in _EXPORTS:
            raise ReplayAccountError("supported exports are equity, orders, trades, positions, and events")
        detail = self.detail(account_id)
        rows = detail["events"] if name == "events" else detail[name]
        if not rows:
            return b""
        fields = list(dict.fromkeys(key for row in rows for key in row))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")

    export_csv = csv_bytes

