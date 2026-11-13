"""Bounded, immutable registration for isolated research data candidates.

This module deliberately does not load candidate data into a backtest or a
paper account.  It validates a small JSON projection and stores its original
bytes for later, read-only inspection.
"""
from __future__ import annotations

import datetime as _datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import uuid


MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_ROWS_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10_000
MAX_LABEL_LENGTH = 120
MAX_PREVIEW_ROWS = 50
MAX_LIST_RECORDS = 100
MAX_DIRECTORY_ENTRIES = 1000
MAX_RECORD_BYTES = 256 * 1024

_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL_RE = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z")
_DATE_RE = re.compile(r"[0-9]{8}\Z")
_REQUIRED_ROW_FIELDS = frozenset(
    {"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"}
)
_OPTIONAL_ROW_FIELDS = frozenset({"change", "pct_chg"})
_RECORD_FIELDS = frozenset(
    {
        "candidate_id",
        "label",
        "created_at",
        "row_count",
        "symbols",
        "start",
        "end",
        "manifest_sha256",
        "rows_sha256",
        "status",
        "mode",
        "paper_admission",
        "candidate_promoted_to_runtime",
        "source_evidence_copied",
        "message",
    }
)
_STANDARD_MESSAGE = "仅作结构性候选登记；哈希只证明字节一致性，不是来源认证。候选不进入回测或实时模拟。"
_LIST_MESSAGE = "列表仅展示登记摘要，未逐项校验冻结数据；打开详情会重新校验。"
_CORRUPT_MESSAGE = "登记记录损坏或不可信，需核查；详情不可用。"


class CandidateIntegrityError(ValueError):
    """Raised when a candidate cannot be safely validated."""


def _reject_constant(value: str):
    raise ValueError("JSON 不允许 NaN 或 Infinity")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象含重复字段")
        result[key] = value
    return result


def _parse_json(raw: bytes, description: str):
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
        raise CandidateIntegrityError(f"{description} 不是有效的 UTF-8 JSON") from None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _content_id(manifest_bytes: bytes, rows_bytes: bytes) -> str:
    return hashlib.sha256(manifest_bytes + b"\0" + rows_bytes).hexdigest()[:32]


def _stat_signature(info):
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
    )


def _snapshot_file(path: Path, limit: int, description: str, forbidden_root: Path | None) -> bytes:
    """Read one stable, bounded regular-file snapshot without following links."""
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
        before_path = os.lstat(absolute)
    except (OSError, TypeError, ValueError):
        raise CandidateIntegrityError(f"{description} 不可读取") from None
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise CandidateIntegrityError(f"{description} 必须是普通文件，且不能是符号链接")
    if before_path.st_size > limit:
        raise CandidateIntegrityError(f"{description} 超过大小上限")
    if forbidden_root is not None:
        try:
            resolved = absolute.resolve(strict=True)
            resolved.relative_to(forbidden_root)
        except ValueError:
            pass
        except OSError:
            raise CandidateIntegrityError(f"{description} 不可读取") from None
        else:
            raise CandidateIntegrityError("不能从候选库自身导入，避免递归登记")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(absolute, flags)
    except OSError:
        raise CandidateIntegrityError(f"{description} 不可安全打开") from None
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or _stat_signature(before_fd) != _stat_signature(before_path):
            raise CandidateIntegrityError(f"{description} 在读取前发生变化")
        if before_fd.st_size > limit:
            raise CandidateIntegrityError(f"{description} 超过大小上限")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise CandidateIntegrityError(f"{description} 超过大小上限")
        after_fd = os.fstat(fd)
        try:
            after_path = os.lstat(absolute)
        except OSError:
            raise CandidateIntegrityError(f"{description} 在读取期间被替换") from None
        signature = _stat_signature(before_fd)
        if (
            signature != _stat_signature(after_fd)
            or signature != _stat_signature(after_path)
            or total != before_fd.st_size
        ):
            raise CandidateIntegrityError(f"{description} 在读取期间发生变化")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_date(value) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        return _datetime.datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") == value
    except ValueError:
        return False


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_candidate(manifest_bytes: bytes, rows_bytes: bytes):
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise CandidateIntegrityError("manifest.json 超过大小上限")
    if len(rows_bytes) > MAX_ROWS_BYTES:
        raise CandidateIntegrityError("rows.json 超过大小上限")

    manifest = _parse_json(manifest_bytes, "manifest.json")
    rows = _parse_json(rows_bytes, "rows.json")
    if not isinstance(manifest, dict):
        raise CandidateIntegrityError("manifest.json 顶层必须是对象")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise CandidateIntegrityError("rows.json 必须含 1 至 10000 行")

    if manifest.get("mode") != "RESEARCH_ONLY":
        raise CandidateIntegrityError("manifest.mode 必须为 RESEARCH_ONLY")
    if manifest.get("status") != "ISOLATED_CANDIDATE_NOT_ADMITTED":
        raise CandidateIntegrityError("manifest.status 不符合隔离候选格式")
    row_count = manifest.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count != len(rows):
        raise CandidateIntegrityError("manifest.row_count 与实际行数不一致")
    if manifest.get("rows_sha256") != _sha256(rows_bytes):
        raise CandidateIntegrityError("manifest.rows_sha256 与 rows.json 不一致")

    # These controls are security gates, so false must be the JSON boolean false.
    for key in ("paper_admission", "candidate_promoted_to_runtime"):
        if manifest.get(key) is not False:
            raise CandidateIntegrityError(f"manifest.{key} 必须是布尔 false")
    for key, value in manifest.items():
        lowered = key.casefold() if isinstance(key, str) else ""
        if any(token in lowered for token in ("admission", "promot")) and value is not False:
            raise CandidateIntegrityError("manifest 中的准入或 promotion 字段必须是布尔 false")

    declared_symbols = manifest.get("symbols")
    if not isinstance(declared_symbols, list) or not declared_symbols:
        raise CandidateIntegrityError("manifest.symbols 必须是非空证券列表")
    if any(not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol) for symbol in declared_symbols):
        raise CandidateIntegrityError("manifest.symbols 含无效证券代码")
    if len(set(declared_symbols)) != len(declared_symbols):
        raise CandidateIntegrityError("manifest.symbols 不能重复")

    seen = set()
    row_symbols = set()
    dates = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CandidateIntegrityError(f"第 {index + 1} 行必须是对象")
        keys = set(row)
        if not _REQUIRED_ROW_FIELDS.issubset(keys) or keys - _REQUIRED_ROW_FIELDS - _OPTIONAL_ROW_FIELDS:
            raise CandidateIntegrityError(f"第 {index + 1} 行字段不符合允许的九字段投影")
        symbol = row.get("ts_code")
        trade_date = row.get("trade_date")
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            raise CandidateIntegrityError(f"第 {index + 1} 行证券代码无效")
        if not _is_date(trade_date):
            raise CandidateIntegrityError(f"第 {index + 1} 行 trade_date 不是有效 YYYYMMDD 日期")
        key = (symbol, trade_date)
        if key in seen:
            raise CandidateIntegrityError("rows.json 含重复证券日期")
        seen.add(key)
        row_symbols.add(symbol)
        dates.append(trade_date)

        for field in ("open", "high", "low", "close", "pre_close", "vol", "amount"):
            if not _finite_number(row[field]):
                raise CandidateIntegrityError(f"第 {index + 1} 行 {field} 必须是有限数值")
        for field in ("change", "pct_chg"):
            if field in row and not _finite_number(row[field]):
                raise CandidateIntegrityError(f"第 {index + 1} 行 {field} 必须是有限数值")
        if any(row[field] <= 0 for field in ("open", "high", "low", "close", "pre_close")):
            raise CandidateIntegrityError(f"第 {index + 1} 行价格必须大于零")
        if row["vol"] < 0 or row["amount"] < 0:
            raise CandidateIntegrityError(f"第 {index + 1} 行 vol 和 amount 不能为负数")
        if row["vol"] == 0 and row["amount"] != 0:
            raise CandidateIntegrityError(f"第 {index + 1} 行零成交量必须对应零成交额")
        if row["high"] < max(row["open"], row["low"], row["close"]):
            raise CandidateIntegrityError(f"第 {index + 1} 行 high 关系无效")
        if row["low"] > min(row["open"], row["high"], row["close"]):
            raise CandidateIntegrityError(f"第 {index + 1} 行 low 关系无效")

    if row_symbols != set(declared_symbols):
        raise CandidateIntegrityError("manifest.symbols 与 rows.json 证券集合不一致")

    return {
        "row_count": len(rows),
        "symbols": sorted(row_symbols),
        "start": min(dates),
        "end": max(dates),
        "rows": rows,
    }


def _utc_now():
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _write_fsynced(path: Path, data: bytes):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CandidateStore:
    """Store frozen data candidates beneath a RunStore root."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.candidates_root = self.root / "data_candidates"

    def _storage(self, create=False):
        try:
            info = os.lstat(self.candidates_root)
        except FileNotFoundError:
            if not create:
                return None
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.candidates_root, 0o700)
            except FileExistsError:
                pass
            try:
                info = os.lstat(self.candidates_root)
            except OSError:
                raise CandidateIntegrityError("候选库目录不可用") from None
        except OSError:
            raise CandidateIntegrityError("候选库目录不可用") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CandidateIntegrityError("候选库路径必须是普通目录")
        return self.candidates_root

    def _read_child(self, directory: Path, name: str, limit: int, description: str):
        return _snapshot_file(directory / name, limit, description, None)

    def _load_record(self, candidate_id: str):
        root = self._storage(create=False)
        if root is None:
            raise FileNotFoundError("candidate not found")
        directory = root / candidate_id
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            raise FileNotFoundError("candidate not found") from None
        except OSError:
            raise CandidateIntegrityError("候选登记目录不可用") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CandidateIntegrityError("候选登记目录不可信")
        raw = self._read_child(directory, "record.json", MAX_RECORD_BYTES, "record.json")
        value = _parse_json(raw, "record.json")
        if not self._record_valid(value, candidate_id):
            raise CandidateIntegrityError("record.json 字段损坏或不可信")
        return value, directory

    @staticmethod
    def _record_valid(record, candidate_id):
        if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
            return False
        if record.get("candidate_id") != candidate_id or not _ID_RE.fullmatch(candidate_id):
            return False
        if not isinstance(record.get("label"), str) or len(record["label"]) > MAX_LABEL_LENGTH:
            return False
        created_at = record.get("created_at")
        if not isinstance(created_at, str):
            return False
        try:
            timestamp = _datetime.datetime.fromisoformat(created_at)
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            return False
        row_count = record.get("row_count")
        if isinstance(row_count, bool) or not isinstance(row_count, int) or not (1 <= row_count <= MAX_ROWS):
            return False
        symbols = record.get("symbols")
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(not isinstance(item, str) or not _SYMBOL_RE.fullmatch(item) for item in symbols)
            or symbols != sorted(set(symbols))
        ):
            return False
        if not _is_date(record.get("start")) or not _is_date(record.get("end")):
            return False
        if record["start"] > record["end"]:
            return False
        if not isinstance(record.get("manifest_sha256"), str) or not _SHA_RE.fullmatch(record["manifest_sha256"]):
            return False
        if not isinstance(record.get("rows_sha256"), str) or not _SHA_RE.fullmatch(record["rows_sha256"]):
            return False
        if record.get("status") != "CANDIDATE_ONLY" or record.get("mode") != "RESEARCH_ONLY":
            return False
        for field in ("paper_admission", "candidate_promoted_to_runtime", "source_evidence_copied"):
            if record.get(field) is not False:
                return False
        if record.get("message") != _STANDARD_MESSAGE:
            return False
        return True

    def _verified(self, candidate_id: str):
        record, directory = self._load_record(candidate_id)
        manifest_bytes = self._read_child(directory, "manifest.json", MAX_MANIFEST_BYTES, "manifest.json")
        rows_bytes = self._read_child(directory, "rows.json", MAX_ROWS_BYTES, "rows.json")
        if _sha256(manifest_bytes) != record["manifest_sha256"]:
            raise CandidateIntegrityError("冻结 manifest.json 的 SHA-256 不匹配")
        if _sha256(rows_bytes) != record["rows_sha256"]:
            raise CandidateIntegrityError("冻结 rows.json 的 SHA-256 不匹配")
        if _content_id(manifest_bytes, rows_bytes) != candidate_id:
            raise CandidateIntegrityError("候选内容 ID 不匹配")
        derived = _validate_candidate(manifest_bytes, rows_bytes)
        expected = (derived["row_count"], derived["symbols"], derived["start"], derived["end"])
        recorded = (record["row_count"], record["symbols"], record["start"], record["end"])
        if expected != recorded:
            raise CandidateIntegrityError("record.json 摘要与冻结候选内容不一致")
        return record, manifest_bytes, rows_bytes, derived

    @staticmethod
    def _detail_result(record, derived):
        validation = {
            "ok": True,
            "manifest_sha256_matches": True,
            "rows_sha256_matches": True,
            "content_id_matches": True,
            "schema_valid": True,
            "checked_row_count": derived["row_count"],
            "checked_symbol_count": len(derived["symbols"]),
            "source_certification_independently_verified": False,
            "paper_admission": False,
            "candidate_promoted_to_runtime": False,
            "message": "结构与冻结字节校验通过；来源、单位、复权、授权及独立数据认证未验证。候选不进入回测或实时模拟。",
        }
        return {
            "record": dict(record),
            "preview_rows": [dict(row) for row in derived["rows"][:MAX_PREVIEW_ROWS]],
            "validation": validation,
        }

    def register(self, manifest_path, rows_path, label=""):
        if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
            raise CandidateIntegrityError("label 必须是长度不超过 120 的文本")
        manifest_path = Path(os.fsdecode(os.fspath(manifest_path)))
        rows_path = Path(os.fsdecode(os.fspath(rows_path)))
        manifest_bytes = _snapshot_file(manifest_path, MAX_MANIFEST_BYTES, "manifest.json", self.root)
        rows_bytes = _snapshot_file(rows_path, MAX_ROWS_BYTES, "rows.json", self.root)
        derived = _validate_candidate(manifest_bytes, rows_bytes)
        manifest_hash = _sha256(manifest_bytes)
        rows_hash = _sha256(rows_bytes)
        candidate_id = _content_id(manifest_bytes, rows_bytes)

        root = self._storage(create=True)
        stage = root / (".pending-" + uuid.uuid4().hex)
        os.mkdir(stage, 0o700)
        renamed = False
        try:
            _write_fsynced(stage / "manifest.json", manifest_bytes)
            _write_fsynced(stage / "rows.json", rows_bytes)
            record = {
                "candidate_id": candidate_id,
                "label": label,
                "created_at": _utc_now(),
                "row_count": derived["row_count"],
                "symbols": derived["symbols"],
                "start": derived["start"],
                "end": derived["end"],
                "manifest_sha256": manifest_hash,
                "rows_sha256": rows_hash,
                "status": "CANDIDATE_ONLY",
                "mode": "RESEARCH_ONLY",
                "paper_admission": False,
                "candidate_promoted_to_runtime": False,
                "source_evidence_copied": False,
                "message": _STANDARD_MESSAGE,
            }
            record_bytes = json.dumps(
                record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            _write_fsynced(stage / "record.json", record_bytes)
            _fsync_directory(stage)

            lock_path = root / ".register.lock"
            lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            lock_flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                lock_fd = os.open(lock_path, lock_flags, 0o600)
            except OSError:
                raise CandidateIntegrityError("候选库登记锁不可用") from None
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise CandidateIntegrityError("候选库登记锁不可信")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                final = root / candidate_id
                if os.path.lexists(final):
                    existing_record, existing_manifest, existing_rows, _ = self._verified(candidate_id)
                    if (
                        _sha256(existing_manifest) != manifest_hash
                        or _sha256(existing_rows) != rows_hash
                    ):
                        raise CandidateIntegrityError("候选 ID 碰撞，拒绝覆盖现有登记")
                    return self._detail_result(
                        existing_record,
                        _validate_candidate(existing_manifest, existing_rows),
                    )
                try:
                    os.rename(stage, final)
                    renamed = True
                    _fsync_directory(root)
                except FileExistsError:
                    existing_record, existing_manifest, existing_rows, _ = self._verified(candidate_id)
                    if (
                        _sha256(existing_manifest) != manifest_hash
                        or _sha256(existing_rows) != rows_hash
                    ):
                        raise CandidateIntegrityError("候选 ID 碰撞，拒绝覆盖现有登记")
                    return self._detail_result(
                        existing_record,
                        _validate_candidate(existing_manifest, existing_rows),
                    )
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            if not renamed and os.path.lexists(stage):
                shutil.rmtree(stage)
        return self.detail(candidate_id)

    def list(self):
        root = self._storage(create=False)
        if root is None:
            return {"candidates": [], "truncated": False}
        candidates = []
        names = []
        truncated = False
        try:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORY_ENTRIES:
                        truncated = True
                        break
                    if _ID_RE.fullmatch(entry.name):
                        if len(names) >= MAX_LIST_RECORDS:
                            truncated = True
                            break
                        names.append(entry.name)
        except OSError:
            raise CandidateIntegrityError("候选库目录不可读取") from None
        for candidate_id in names:
            try:
                record, _ = self._load_record(candidate_id)
                summary = dict(record)
                summary["symbol_count"] = len(record["symbols"])
                summary["symbols"] = record["symbols"][:3]
                summary["message"] = _LIST_MESSAGE
                summary["summary_validation"] = "NOT_CHECKED"
                candidates.append(summary)
            except (CandidateIntegrityError, FileNotFoundError, OSError):
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "label": "",
                        "created_at": "",
                        "row_count": 0,
                        "symbols": [],
                        "start": "",
                        "end": "",
                        "manifest_sha256": "",
                        "rows_sha256": "",
                        "status": "CHECK_REQUIRED",
                        "mode": "RESEARCH_ONLY",
                        "paper_admission": False,
                        "candidate_promoted_to_runtime": False,
                        "source_evidence_copied": False,
                        "message": _CORRUPT_MESSAGE,
                        "summary_validation": "RECORD_INVALID",
                    }
                )
        candidates.sort(key=lambda record: (record.get("created_at", ""), record["candidate_id"]), reverse=True)
        return {"candidates": candidates, "truncated": truncated}

    def detail(self, candidate_id):
        if not isinstance(candidate_id, str) or not _ID_RE.fullmatch(candidate_id):
            raise CandidateIntegrityError("candidate_id 必须是 32 位小写十六进制")
        record, _, _, derived = self._verified(candidate_id)
        return self._detail_result(record, derived)

    def rows_bytes(self, candidate_id):
        if not isinstance(candidate_id, str) or not _ID_RE.fullmatch(candidate_id):
            raise CandidateIntegrityError("candidate_id 必须是 32 位小写十六进制")
        _, _, rows_bytes, _ = self._verified(candidate_id)
        return rows_bytes

