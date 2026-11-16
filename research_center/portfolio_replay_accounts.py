"""Isolated, manually stepped accounts for frozen historical portfolios.

Accounts are created only from completed, hash-verified portfolio archives.
They keep a private copy of the validated configuration and observed frames,
never reopen the minute-data lake, and never run in the background.  They are
research records, not paper or live trading accounts.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterator

from .store import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_CONFIG_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_SOURCE_STATE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_RESULT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FRAMES = 31 * 240
MAX_STEP_FRAMES = 240
_ID = re.compile(r"^[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EXPORTS = frozenset({"equity", "orders", "trades", "positions", "events"})
_CODE_FILES = (
    "research_center/portfolio_replay_accounts.py",
    "research_center/portfolio_kernel.py",
    "research_center/replay_kernel.py",
    "research_center/portfolio_data.py",
    "research_center/portfolio_provenance.py",
    "research_center/engine.py",
    "research_center/strategies.py",
    "strategy/base.py",
)
_ARTIFACT_LIMITS = {
    "config.json": MAX_CONFIG_BYTES,
    "data_manifest.json": MAX_MANIFEST_BYTES,
    "state.json": MAX_SOURCE_STATE_BYTES,
    "result.json": MAX_SOURCE_RESULT_BYTES,
    "source_snapshot.json": MAX_SOURCE_SNAPSHOT_BYTES,
}
_READ_LIMITS = {**_ARTIFACT_LIMITS, "receipt.json": 32 * 1024}
_PRIVATE_KEYS = frozenset({
    "data_root", "artifact_directory", "source_snapshot", "source_text", "path",
    "paths", "directory", "directories", "file_path", "absolute_path", "root",
})


class PortfolioReplayAccountError(ValueError):
    """An account request violates its frozen-source or replay contract."""

    rollback_confirmed = False


class PortfolioReplayRevisionConflict(PortfolioReplayAccountError):
    """The caller's expected revision is stale; its state transaction rolled back."""

    rollback_confirmed = True


class PortfolioReplaySourceChanged(PortfolioReplayAccountError):
    """Code mismatch rejected before commit; its state transaction rolled back."""

    rollback_confirmed = True


class _CreateRequestConflict(PortfolioReplayAccountError):
    """A create idempotency key was reused with a different payload."""


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PortfolioReplayAccountError("组合回放记录含非 JSON 或非有限数值") from exc


def _parse_json(raw: bytes, name: str) -> Any:
    def reject_constant(_value):
        raise ValueError("组合归档含非有限数值")
    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PortfolioReplayAccountError(f"组合归档文件无效：{name}") from None


def _json_copy(value: Any) -> Any:
    return json.loads(_json_dump(value))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_id(value: Any, field: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise PortfolioReplayAccountError(f"{field} 必须是 32 位小写十六进制编号")
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise PortfolioReplayAccountError(f"{field} 编号无效") from None
    return value


def _valid_request_id(value: Any) -> str:
    if type(value) is not str or _REQUEST_ID.fullmatch(value) is None:
        raise PortfolioReplayAccountError("request_id 必须是 1 至 128 位 ASCII 字母、数字或 . _ : -")
    return value


def _safe_read_at(directory: Path, name: str, limit: int) -> bytes:
    """Read a regular child file with no symlink traversal or read race."""
    if name not in _READ_LIMITS or limit != _READ_LIMITS[name]:
        raise PortfolioReplayAccountError("组合归档文件名无效")
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise PortfolioReplayAccountError(f"组合归档文件类型或大小无效：{name}")
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        identity = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns)
        original = (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity != original:
            raise PortfolioReplayAccountError(f"组合归档文件在读取前变化：{name}")
        chunks = []
        total = 0
        while True:
            block = os.read(file_fd, min(1024 * 1024, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > limit:
                raise PortfolioReplayAccountError(f"组合归档文件超过大小上限：{name}")
        after_fd = os.fstat(file_fd)
        after_path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        after_identity = (after_fd.st_dev, after_fd.st_ino, after_fd.st_mode, after_fd.st_size,
                          after_fd.st_mtime_ns, after_fd.st_ctime_ns)
        after_path_identity = (after_path.st_dev, after_path.st_ino, after_path.st_mode,
                               after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns)
        if identity != after_identity or identity != after_path_identity:
            raise PortfolioReplayAccountError(f"组合归档文件在读取期间变化：{name}")
        return b"".join(chunks)
    except PortfolioReplayAccountError:
        raise
    except OSError:
        raise PortfolioReplayAccountError(f"无法安全读取组合归档文件：{name}") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _safe_project_read(root: Path, relative_path: str, limit: int = 1024 * 1024) -> bytes:
    """Read one project source without following symlinks in any component."""
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = relative_path.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        details = os.fstat(file_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise PortfolioReplayAccountError("组合回放源码文件类型或大小无效")
        content = bytearray()
        while len(content) <= limit:
            block = os.read(file_fd, min(128 * 1024, limit + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > limit:
            raise PortfolioReplayAccountError("组合回放源码文件超过大小上限")
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise PortfolioReplayAccountError("组合回放源码文件类型无效")
        return bytes(content)
    except PortfolioReplayAccountError:
        raise
    except OSError:
        raise PortfolioReplayAccountError("组合回放源码缺失或不可安全读取") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _code_identity() -> dict[str, str]:
    return {name: _sha256(_safe_project_read(PROJECT_ROOT, name)) for name in _CODE_FILES}


def _code_digest(identity: dict[str, str]) -> str:
    return _sha256(_json_dump(identity).encode("utf-8"))


def _source_version() -> str:
    from .portfolio_provenance import snapshot_sources
    snapshot = snapshot_sources(PROJECT_ROOT)
    return snapshot["source_version"]


def _public_value(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON data while omitting machine-local paths and source text."""
    if depth > 20:
        return None
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if (normalized in _PRIVATE_KEYS or normalized.endswith("_path")
                    or normalized.endswith("_directory") or normalized.endswith("_root")):
                continue
            output[key] = _public_value(item, depth=depth + 1)
        return output
    if isinstance(value, list):
        return [_public_value(item, depth=depth + 1) for item in value]
    return value


def _source_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    hashes: set[str] = set()
    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif "sha256" in key.casefold() and type(value) is str and _SHA.fullmatch(value):
            hashes.add(value)
    visit(manifest)
    keep = ("schema_version", "mode", "paper_admission", "symbols", "start", "end",
            "frames", "alignment")
    return {
        **{key: manifest[key] for key in keep if key in manifest},
        "source_file_hashes": sorted(hashes),
    }


def _validate_source(store: RunStore, run_id: str) -> dict[str, Any]:
    """Load and reconcile a completed portfolio's immutable archive artifacts."""
    from .portfolio_archive import PortfolioArchive, archive_root
    from .portfolio_kernel import normalize_portfolio, result_from_state, _validate_state, _canonical_frames
    from .portfolio_provenance import validate_snapshot

    run_id = _valid_id(run_id, "run_id")
    archive = PortfolioArchive(archive_root(store))
    # This performs the archive's established identity, mode, five-hash, and
    # archived 14-file provenance checks before any account is created.
    archived = archive.get(run_id)
    directory = Path(archive.root) / run_id
    raw = {name: _safe_read_at(directory, name, limit)
           for name, limit in _ARTIFACT_LIMITS.items()}
    receipt_raw = _safe_read_at(directory, "receipt.json", _READ_LIMITS["receipt.json"])
    receipt = _parse_json(receipt_raw, "receipt.json")
    expected_names = set(_ARTIFACT_LIMITS)
    hashes = receipt.get("artifact_sha256") if type(receipt) is dict else None
    if (type(receipt) is not dict or receipt.get("run_id") != run_id
            or receipt.get("status") != "completed" or receipt.get("mode") != "RESEARCH_ONLY"
            or receipt.get("paper_admission") is not False or receipt.get("schema_version") != 2
            or type(hashes) is not dict or set(hashes) != expected_names
            or any(type(value) is not str or _SHA.fullmatch(value) is None for value in hashes.values())):
        raise PortfolioReplayAccountError("只能基于完整的 schema-2 RESEARCH_ONLY 组合归档创建历史账户")
    for name, content in raw.items():
        if _sha256(content) != hashes[name]:
            raise PortfolioReplayAccountError("组合归档产物哈希校验失败，未创建历史账户")

    config = _parse_json(raw["config.json"], "config.json")
    manifest = _parse_json(raw["data_manifest.json"], "data_manifest.json")
    source_state = _parse_json(raw["state.json"], "state.json")
    source_result = _parse_json(raw["result.json"], "result.json")
    snapshot = _parse_json(raw["source_snapshot.json"], "source_snapshot.json")
    if not all(type(item) is dict for item in (config, manifest, source_state, source_result, snapshot)):
        raise PortfolioReplayAccountError("组合归档配置、清单、状态或结果格式无效")
    validate_snapshot(snapshot)
    if (receipt.get("code_version") != snapshot.get("source_version")
            or archived["receipt"].get("source_snapshot_status") != "archived_and_verified"
            or snapshot.get("source_version") != _source_version()):
        raise PortfolioReplaySourceChanged("组合归档源码版本与当前源码不一致，拒绝创建历史账户")

    try:
        normalized = normalize_portfolio(config)
    except Exception:
        raise PortfolioReplayAccountError("组合归档配置未通过当前 portfolio kernel 校验") from None
    if normalized != config:
        raise PortfolioReplayAccountError("组合归档配置不是当前 kernel 的规范配置")
    start = date.fromisoformat(normalized["start"])
    end = date.fromisoformat(normalized["end"])
    if (start < date(2024, 1, 1) or end > date(2025, 12, 31)
            or (end - start).days + 1 > 31):
        raise PortfolioReplayAccountError("历史账户仅接受 2024 至 2025 年且不超过 31 个日历日的归档")

    try:
        _validate_state(normalized, source_state)
    except Exception:
        raise PortfolioReplayAccountError("组合归档状态未通过当前 kernel 的财务一致性校验") from None
    if source_state.get("finalized") is not True or source_state.get("cancelled") is not False:
        raise PortfolioReplayAccountError("组合源运行必须已正常完成并封存")
    bars = source_result.get("bars")
    if type(bars) is not list or not bars or len(bars) > MAX_SOURCE_FRAMES:
        raise PortfolioReplayAccountError(f"组合源帧必须为 1 至 {MAX_SOURCE_FRAMES} 帧")
    try:
        canonical_bars = _canonical_frames(normalized, bars, None)
    except Exception:
        raise PortfolioReplayAccountError("组合源结果分钟帧未通过时间轴与行情字段校验") from None
    if canonical_bars != source_state.get("bars") or bars != source_state.get("bars"):
        raise PortfolioReplayAccountError("组合源结果帧与完成状态不一致")
    expected_frames = len(bars)
    if (receipt.get("frames") != expected_frames or source_state.get("rows_processed") != expected_frames
            or manifest.get("frames") != expected_frames or manifest.get("symbols") != normalized["symbols"]
            or manifest.get("mode") != "RESEARCH_ONLY" or manifest.get("paper_admission") is not False
            or source_result.get("mode") != "RESEARCH_ONLY"
            or source_result.get("paper_admission") is not False):
        raise PortfolioReplayAccountError("组合回执、数据清单和结果帧数或模式不匹配")
    expected_result = result_from_state(normalized, source_state)
    if any(source_result.get(key) != value for key, value in expected_result.items()):
        raise PortfolioReplayAccountError("组合源结果与完成状态的财务视图不一致")
    readiness = source_result.get("data_readiness")
    provenance = source_result.get("provenance")
    if (type(readiness) is not dict or readiness.get("paper_admission") is not False
            or type(provenance) is not dict
            or provenance.get("source_version") != snapshot.get("source_version")
            or provenance.get("runtime") != snapshot.get("runtime")):
        raise PortfolioReplayAccountError("组合源结果缺少兼容的研究就绪度或源码溯源信息")

    code_identity = _code_identity()
    source_hashes = {
        "source_config_sha256": _sha256(raw["config.json"]),
        "source_manifest_sha256": _sha256(raw["data_manifest.json"]),
        "source_state_sha256": _sha256(raw["state.json"]),
        "source_result_sha256": _sha256(raw["result.json"]),
        "source_snapshot_sha256": _sha256(raw["source_snapshot.json"]),
        "source_bars_sha256": _sha256(_json_dump(bars).encode("utf-8")),
    }
    if code_identity != _code_identity() or snapshot.get("source_version") != _source_version():
        raise PortfolioReplaySourceChanged("创建期间组合源码发生变化，历史账户未创建")
    source = {
        "run_id": run_id,
        "receipt_created_at": receipt.get("created_at"),
        "source_code_version": snapshot["source_version"],
        "source_final_state_sha256": source_state["state_sha256"],
        "source_runtime": snapshot["runtime"],
        "hashes": source_hashes,
        "source_result_snapshot": {
            "sha256": source_hashes["source_result_sha256"],
            "config_sha256": expected_result["config_sha256"],
            "metrics": _json_copy(expected_result["metrics"]),
            "bars_sha256": source_hashes["source_bars_sha256"],
            "data_readiness": _public_value(readiness),
            "provenance": {
                "source_version": provenance["source_version"],
                "runtime": _json_copy(provenance["runtime"]),
                "scope": provenance.get("scope"),
            },
        },
        "data_readiness": _public_value(readiness),
        "manifest_summary": _source_manifest_summary(manifest),
        "source_snapshot_status": "archived_and_current_at_account_creation",
        "source_artifact_integrity": "verified_and_reconciled",
    }
    return {
        "config": normalized,
        "bars": bars,
        "manifest": manifest,
        "source": source,
        "source_state_sha256": source_hashes["source_state_sha256"],
        "source_final_state_sha256": source_state["state_sha256"],
        "code_identity": code_identity,
    }


class PortfolioReplayAccounts:
    """SQLite-backed manually advanced accounts isolated beside a RunStore."""

    def __init__(self, run_store: RunStore):
        if not isinstance(run_store, RunStore):
            raise TypeError("run_store must be a research_center.store.RunStore")
        self.run_store = run_store
        self.root = Path(run_store.root).parent / (Path(run_store.root).name + "_portfolio_replays")
        self._validate_root(self.root, Path(run_store.root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_root(self.root, Path(run_store.root))
        self.database = self.root / "accounts.sqlite3"
        self._initialize()

    @staticmethod
    def _validate_root(root: Path, run_root: Path | None = None) -> None:
        try:
            raw = Path(root).expanduser().absolute()
            normalized = raw.resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise PortfolioReplayAccountError("portfolio replay account root is invalid") from None
        folded_parts = {part.casefold() for part in normalized.parts}
        if "quantlivepaper" in folded_parts:
            raise PortfolioReplayAccountError("portfolio replay accounts cannot be inside QuantLivePaper")
        if raw != normalized:
            raise PortfolioReplayAccountError("portfolio replay account root cannot traverse symlinks")
        if normalized.exists():
            info = normalized.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PortfolioReplayAccountError("portfolio replay account root must be a regular directory")
        if run_root is not None:
            source_root = Path(run_root).resolve(strict=False)
            try:
                if normalized == source_root or normalized.is_relative_to(source_root):
                    raise PortfolioReplayAccountError("portfolio replay accounts must be isolated from the RunStore")
            except AttributeError:  # Python runtimes older than Path.is_relative_to.
                if normalized == source_root or source_root in normalized.parents:
                    raise PortfolioReplayAccountError("portfolio replay accounts must be isolated from the RunStore")
        from .engine import DEFAULT_DATA_ROOT, PROJECT_ROOT as ENGINE_ROOT
        from .store import ROOT as STORE_ROOT
        blocked = (ENGINE_ROOT / "data", Path(DEFAULT_DATA_ROOT), STORE_ROOT / "data")
        for candidate in blocked:
            protected = Path(candidate).expanduser().resolve(strict=False)
            try:
                if normalized == protected or normalized.is_relative_to(protected):
                    raise PortfolioReplayAccountError("portfolio replay accounts cannot be inside a data root")
            except AttributeError:
                if normalized == protected or protected in normalized.parents:
                    raise PortfolioReplayAccountError("portfolio replay accounts cannot be inside a data root")

    def _connect(self) -> sqlite3.Connection:
        self._assert_database_path()
        connection = sqlite3.connect(self.database, timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        self._assert_database_path()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    creation_request_id TEXT NOT NULL UNIQUE,
                    creation_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('paused','running','completed')),
                    revision INTEGER NOT NULL,
                    cursor INTEGER NOT NULL,
                    total_frames INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    frozen_bars_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    code_identity_json TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    source_state_sha256 TEXT NOT NULL,
                    finalized INTEGER NOT NULL CHECK(finalized IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS portfolio_replay_accounts_created_idx
                    ON accounts(created_at DESC,account_id);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS portfolio_replay_events_account_idx
                    ON events(account_id,event_id);
                CREATE TABLE IF NOT EXISTS step_requests (
                    account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,request_id)
                );
                """
            )
        try:
            os.chmod(self.database, 0o600, follow_symlinks=False)
        except OSError:
            pass

    def _assert_database_path(self) -> None:
        try:
            details = self.database.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise PortfolioReplayAccountError("portfolio replay database path is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise PortfolioReplayAccountError("portfolio replay database must be a regular non-symlink file")

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

    def _insert_event(self, connection: sqlite3.Connection, account_id: str,
                      event_type: str, revision: int, details: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO events(account_id,event_type,created_at,revision,details_json) VALUES(?,?,?,?,?)",
            (account_id, event_type, _now(), revision, _json_dump(_public_value(details or {}))),
        )

    def _log_error(self, account_id: str, action: str, exc: BaseException) -> None:
        """Log a failed action in a new transaction after account rollback."""
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT revision FROM accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                if row is not None:
                    self._insert_event(connection, account_id, "error", int(row["revision"]), {
                        "action": action,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    })
        except Exception:
            pass

    @staticmethod
    def _validate_label(label: Any) -> str:
        if type(label) is not str or len(label) > 80:
            raise PortfolioReplayAccountError("label 必须是至多 80 个字符的字符串")
        return label

    def _find_create_request(self, connection: sqlite3.Connection, request_id: str,
                             payload_json: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT account_id,creation_payload_json FROM accounts WHERE creation_request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["creation_payload_json"] != payload_json:
            raise _CreateRequestConflict("creation request_id 已用于不同的 source run 或 label")
        detail = self._detail_from_connection(connection, row["account_id"])
        detail["creation_replayed"] = True
        return detail

    def create(self, run_id: str, label: str = "", request_id: str | None = None) -> dict[str, Any]:
        run_id = _valid_id(run_id, "run_id")
        label = self._validate_label(label)
        request_id = _valid_request_id(request_id)
        payload_json = _json_dump({"run_id": run_id, "label": label})
        # Fast-path lost responses before touching source archives again.
        with self._transaction() as connection:
            prior = self._find_create_request(connection, request_id, payload_json)
            if prior is not None:
                return prior

        source = _validate_source(self.run_store, run_id)
        from .portfolio_kernel import initial_state
        config = source["config"]
        bars = source["bars"]
        state = initial_state(config)
        result = self._result_for_state(config, state, len(bars))
        source_json = _json_dump(source["source"])
        config_json = _json_dump(config)
        bars_json = _json_dump(bars)
        manifest_json = _json_dump(source["manifest"])
        state_json = _json_dump(state)
        result_json = _json_dump(result)
        code_identity = source["code_identity"]
        code_hash = _code_digest(code_identity)
        if code_identity != _code_identity() or source["source"]["source_code_version"] != _source_version():
            raise PortfolioReplaySourceChanged("创建期间组合源码发生变化，历史账户未创建")

        account_id = uuid.uuid4().hex
        now = _now()
        with self._transaction() as connection:
            # A simultaneous retry may have committed while source artifacts were checked.
            prior = self._find_create_request(connection, request_id, payload_json)
            if prior is not None:
                return prior
            connection.execute(
                """INSERT INTO accounts(
                    account_id,run_id,label,creation_request_id,creation_payload_json,
                    status,revision,cursor,total_frames,created_at,updated_at,
                    config_json,source_json,frozen_bars_json,manifest_json,state_json,result_json,
                    code_identity_json,code_hash,source_state_sha256,finalized
                ) VALUES(?,?,?,?,?,'paused',0,0,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (account_id, run_id, label, request_id, payload_json, len(bars), now, now,
                 config_json, source_json, bars_json, manifest_json, state_json, result_json,
                 _json_dump(code_identity), code_hash, source["source_state_sha256"]),
            )
            self._insert_event(connection, account_id, "create", 0, {
                "source_run_id": run_id,
                "source_result_sha256": source["source"]["hashes"]["source_result_sha256"],
                "total_frames": len(bars),
                "status": "paused",
                "mode": "RESEARCH_ONLY",
            })
            detail = self._detail_from_connection(connection, account_id)
            detail["creation_replayed"] = False
            return detail

    @staticmethod
    def _positions_from_state(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        from . import portfolio_kernel as kernel
        final_day = (kernel._stamp(state["last_source_datetime"], "last_source_datetime").date()
                     if state["last_source_datetime"] else None)
        current_frame = state["bars"][-1] if state["bars"] else {}
        positions: dict[str, Any] = {}
        for symbol in config["symbols"]:
            position = state["positions"][symbol]
            view = kernel._position_view(position, final_day) if final_day else {}
            if not view:
                view = {"quantity": 0, "avg_cost": 0.0, "available_to_sell": 0}
            view.update({
                "market_value": int(position["quantity"]) * float(current_frame.get(symbol, {}).get("close", 0.0)),
                "lots": _json_copy(position["lots"]),
            })
            positions[symbol] = view
        return positions

    @classmethod
    def _result_for_state(cls, config: dict[str, Any], state: dict[str, Any], total_frames: int) -> dict[str, Any]:
        from . import portfolio_kernel as kernel
        kernel._validate_state(config, state)
        if state["finalized"]:
            return _json_copy(kernel.result_from_state(config, state))
        # Deliberately expose only observed, consumed frames while unfinished.
        # There are no synthetic final metrics, forced cancellations, or future bars.
        current_point = _json_copy(state["equity"][-1])
        return {
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
            "config_sha256": state["config_sha256"],
            "progress": {
                "processed_frames": int(state["rows_processed"]),
                "total_frames": int(total_frames),
                "finalized": False,
            },
            "current": current_point,
            "equity": _json_copy(state["equity"]),
            "positions": cls._positions_from_state(config, state),
            "orders": _json_copy(state["orders"]),
            "trades": _json_copy(state["trades"]),
        }

    @staticmethod
    def _check_revision(expected_revision: Any, actual: int) -> int:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise PortfolioReplayAccountError("expected_revision 必须是非负整数")
        if expected_revision != actual:
            raise PortfolioReplayRevisionConflict(
                f"revision 已变化：expected={expected_revision}, current={actual}"
            )
        return expected_revision

    @staticmethod
    def _validate_account_id(account_id: Any) -> str:
        return _valid_id(account_id, "account_id")

    def _code_current(self, row: sqlite3.Row) -> bool:
        try:
            identity = _code_identity()
            return (_code_digest(identity) == row["code_hash"]
                    and _source_version() == json.loads(row["source_json"])["source_code_version"])
        except Exception:
            return False

    def _assert_code_unchanged(self, row: sqlite3.Row) -> None:
        if not self._code_current(row):
            raise PortfolioReplaySourceChanged(
                "组合源码或固定 14 文件执行版本已变化；历史账户仍可读取，但不能继续推进"
            )

    @staticmethod
    def _record_from_row(row: sqlite3.Row, code_current: bool) -> dict[str, Any]:
        source = json.loads(row["source_json"])
        result = json.loads(row["result_json"])
        finalized = bool(row["finalized"])
        cursor = int(row["cursor"])
        total = int(row["total_frames"])
        current_equity = (result.get("metrics", {}).get("ending_equity")
                          if finalized else result.get("current", {}).get("equity"))
        return {
            "account_id": row["account_id"],
            "run_id": row["run_id"],
            "label": row["label"],
            "creation_request_id": row["creation_request_id"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "cursor": cursor,
            "total_frames": total,
            "remaining_frames": max(0, total - cursor),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source_code_version": source["source_code_version"],
            "source_hashes": _json_copy(source["hashes"]),
            "code_hash": row["code_hash"],
            "code_current": bool(code_current),
            "continuation_allowed": bool(code_current and not finalized),
            "finalized": finalized,
            "current_equity": current_equity,
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
        }

    def _detail_from_connection(self, connection: sqlite3.Connection, account_id: str,
                                *, code_current: bool | None = None) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("portfolio replay account not found")
        if code_current is None:
            code_current = self._code_current(row)
        config = json.loads(row["config_json"])
        source = json.loads(row["source_json"])
        result = json.loads(row["result_json"])
        state = json.loads(row["state_json"])
        events = [
            {"event_id": int(item["event_id"]), "event_type": item["event_type"],
             "created_at": item["created_at"], "revision": int(item["revision"]),
             "details": json.loads(item["details_json"])}
            for item in connection.execute(
                "SELECT * FROM events WHERE account_id=? ORDER BY event_id", (account_id,)
            )
        ]
        return {
            "record": self._record_from_row(row, bool(code_current)),
            "config": _public_value(config),
            "source": _public_value(source),
            "data_readiness": _public_value(source["data_readiness"]),
            "result": _public_value(result),
            "equity": _json_copy(result.get("equity", [])),
            "orders": _json_copy(result.get("orders", [])),
            "trades": _json_copy(result.get("trades", [])),
            "positions": _json_copy(result.get("positions", {})),
            "events": events,
        }

    def list_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise PortfolioReplayAccountError("limit 必须是 1 至 100 的整数")
        try:
            code_ok = _code_identity()
            source_ok = _source_version()
            identity_hash = _code_digest(code_ok)
        except Exception:
            identity_hash = ""
            source_ok = ""
        with self._transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY created_at DESC,account_id LIMIT ?", (limit,)
            ).fetchall()
            output = []
            for row in rows:
                try:
                    source_version = json.loads(row["source_json"])["source_code_version"]
                    code_current = identity_hash == row["code_hash"] and source_ok == source_version
                except Exception:
                    code_current = False
                output.append(self._record_from_row(row, code_current))
            return output

    def detail(self, account_id: str) -> dict[str, Any]:
        account_id = self._validate_account_id(account_id)
        with self._transaction(immediate=False) as connection:
            return self._detail_from_connection(connection, account_id)

    def _mutate_status(self, account_id: str, expected_revision: int, target: str,
                       event_type: str) -> dict[str, Any]:
        account_id = self._validate_account_id(account_id)
        try:
            with self._transaction() as connection:
                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("portfolio replay account not found")
                self._check_revision(expected_revision, int(row["revision"]))
                if target == "running":
                    if row["status"] != "paused" or bool(row["finalized"]):
                        raise PortfolioReplayAccountError("只能启动未完成且已暂停的历史账户")
                    self._assert_code_unchanged(row)
                elif row["status"] != "running":
                    raise PortfolioReplayAccountError("只能暂停正在运行状态的历史账户")
                revision = int(row["revision"]) + 1
                now = _now()
                connection.execute(
                    "UPDATE accounts SET status=?,revision=?,updated_at=? WHERE account_id=?",
                    (target, revision, now, account_id),
                )
                self._insert_event(connection, account_id, event_type, revision, {})
                return self._detail_from_connection(connection, account_id)
        except Exception as exc:
            self._log_error(account_id, event_type, exc)
            raise

    def start(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        return self._mutate_status(account_id, expected_revision, "running", "start")

    # HTTP integrations that already call historical replay actions "resume"
    # can use this exact same guarded transition.
    resume = start

    def pause(self, account_id: str, expected_revision: int) -> dict[str, Any]:
        return self._mutate_status(account_id, expected_revision, "paused", "pause")

    def step(self, account_id: str, steps: int, expected_revision: int,
             request_id: str) -> dict[str, Any]:
        account_id = self._validate_account_id(account_id)
        action = "step"
        try:
            if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEP_FRAMES:
                raise PortfolioReplayAccountError(f"steps 必须是 1 至 {MAX_STEP_FRAMES} 的整数")
            request_id = _valid_request_id(request_id)
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
                raise PortfolioReplayAccountError("expected_revision 必须是非负整数")
            payload_json = _json_dump({"steps": steps, "expected_revision": expected_revision})
            with self._transaction() as connection:
                prior = connection.execute(
                    "SELECT payload_json,response_json FROM step_requests WHERE account_id=? AND request_id=?",
                    (account_id, request_id),
                ).fetchone()
                if prior is not None:
                    if prior["payload_json"] != payload_json:
                        raise PortfolioReplayAccountError("step request_id 已用于不同请求载荷")
                    response = self._detail_from_connection(connection, account_id)
                    response.update(json.loads(prior["response_json"]))
                    response["replayed"] = True
                    return response

                row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError("portfolio replay account not found")
                self._check_revision(expected_revision, int(row["revision"]))
                if row["status"] != "running":
                    raise PortfolioReplayAccountError("历史账户必须先显式启动才能逐帧推进")
                if bool(row["finalized"]):
                    raise PortfolioReplayAccountError("归档历史范围已完成，没有剩余分钟帧")
                self._assert_code_unchanged(row)

                cursor = int(row["cursor"])
                total = int(row["total_frames"])
                bars = json.loads(row["frozen_bars_json"])
                source = json.loads(row["source_json"])
                state = json.loads(row["state_json"])
                if (type(bars) is not list or len(bars) != total or not 0 <= cursor <= total
                        or _sha256(_json_dump(bars).encode("utf-8"))
                        != source.get("hashes", {}).get("source_bars_sha256")):
                    raise PortfolioReplayAccountError("冻结组合分钟帧哈希或帧数校验失败")
                if (type(state) is not dict or state.get("rows_processed") != cursor
                        or len(state.get("bars", [])) != cursor
                        or len(state.get("equity", [])) != cursor + 1):
                    raise PortfolioReplayAccountError("账户状态与已消费组合分钟帧游标不一致")
                batch = bars[cursor:min(total, cursor + steps)]
                if not batch:
                    raise PortfolioReplayAccountError("冻结组合数据已耗尽")
                new_cursor = cursor + len(batch)
                final = new_cursor == total
                config = json.loads(row["config_json"])
                from .portfolio_kernel import advance
                next_state = advance(config, state, batch, final=final)
                if (type(next_state) is not dict or next_state.get("rows_processed") != new_cursor
                        or next_state.get("finalized") is not final):
                    raise PortfolioReplayAccountError("portfolio kernel 返回的游标或完成状态不匹配")
                result = self._result_for_state(config, next_state, total)
                if final:
                    if next_state.get("state_sha256") != source.get("source_final_state_sha256"):
                        raise PortfolioReplayAccountError("完整回放终态与已核验源 state 不一致")
                    source_result_snapshot = source.get("source_result_snapshot", {})
                    if result.get("metrics") != source_result_snapshot.get("metrics"):
                        raise PortfolioReplayAccountError("完整回放指标与已核验源 result 不一致")
                state_json = _json_dump(next_state)
                result_json = _json_dump(result)
                # Check again before the SQLite commit, so code drift during the
                # kernel call rolls back cursor, ledger, and revision together.
                self._assert_code_unchanged(row)
                revision = int(row["revision"]) + 1
                now = _now()
                ending_status = "completed" if final else "running"
                connection.execute(
                    """UPDATE accounts SET status=?,revision=?,cursor=?,updated_at=?,state_json=?,
                       result_json=?,finalized=? WHERE account_id=?""",
                    (ending_status, revision, new_cursor, now, state_json, result_json,
                     int(final), account_id),
                )
                self._insert_event(connection, account_id, "step", revision, {
                    "request_id": request_id,
                    "steps_requested": steps,
                    "frames_processed": len(batch),
                    "cursor_before": cursor,
                    "cursor_after": new_cursor,
                    "final": final,
                })
                if final:
                    self._insert_event(connection, account_id, "complete", revision, {
                        "cursor": new_cursor,
                        "total_frames": total,
                    })
                ack = {
                    "request_id": request_id,
                    "original_revision": int(row["revision"]),
                    "original_cursor": cursor,
                    "applied_revision": revision,
                    "applied_cursor": new_cursor,
                    "frames_processed": len(batch),
                    "status_at_apply": ending_status,
                }
                response = self._detail_from_connection(connection, account_id)
                response.update(ack)
                response["replayed"] = False
                connection.execute(
                    "INSERT INTO step_requests(account_id,request_id,payload_json,response_json,created_at) VALUES(?,?,?,?,?)",
                    (account_id, request_id, payload_json, _json_dump(ack), now),
                )
                return response
        except Exception as exc:
            self._log_error(account_id, action, exc)
            raise

    def events(self, account_id: str) -> list[dict[str, Any]]:
        return self.detail(account_id)["events"]

    def csv_bytes(self, account_id: str, kind: str) -> bytes:
        if kind not in _EXPORTS:
            raise PortfolioReplayAccountError("CSV 类型须为 equity、orders、trades、positions 或 events")
        detail = self.detail(account_id)
        rows = detail["events"] if kind == "events" else detail[kind]
        if kind == "positions" and isinstance(rows, dict):
            rows = [{"symbol": symbol, **value} for symbol, value in rows.items()]
        if not rows:
            return b""
        fields = list(dict.fromkeys(key for row in rows for key in row))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")

    export_csv = csv_bytes


# Compatibility aliases for callers migrating from the single-symbol API.
PortfolioReplayError = PortfolioReplayAccountError
RevisionConflict = PortfolioReplayRevisionConflict
SourceCodeChanged = PortfolioReplaySourceChanged
