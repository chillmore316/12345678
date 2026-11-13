"""Read-only, fail-closed data evidence checks for local research runs.

This module inspects the bars and data manifest already saved by a run.  It
does not load minute data, alter execution inputs, or grant paper admission.
Fixed local daily caches are bounded comparison references; matching hashes do
not certify their price basis, completeness, or research provenance.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DAILY_PROBE_ROOT = (
    PROJECT_ROOT / "research" / "stock_strategy_v2" / "raw_daily_probe_20261004"
)
_RAW_DAILY_BATCH50_ROOT = (
    PROJECT_ROOT / "research" / "stock_strategy_v2" / "raw_daily_batch50_20261004"
)
_RAW_DAILY_BATCH100_ROOT = (
    PROJECT_ROOT / "research" / "stock_strategy_v2" / "raw_daily_batch100_20261004"
)
_RAW_SYMBOLS = {
    "000001": "000001.SZ",
    "000002": "000002.SZ",
    "600000": "600000.SH",
}
_RAW_DAILY_BATCH_REFERENCES = (
    (
        "raw_daily_batch50_20261004",
        _RAW_DAILY_BATCH50_ROOT,
        "c57da75b481f0b13e430ea975f4c87a24271baeabe1620e316dae15e22ab0f97",
        50,
    ),
    (
        "raw_daily_batch100_20261004",
        _RAW_DAILY_BATCH100_ROOT,
        "39f226127fe42c80c66d171859de28cff223bf36fd0b7ba923c2f66943026539",
        100,
    ),
)
_SYMBOL_ALIASES = {
    "SZ": {"SZ", "SZSE", "XSHE"},
    "SH": {"SH", "SSE", "XSHG"},
}
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_PARQUET_BYTES = 16 * 1024 * 1024
_PRICE_MISMATCH_THRESHOLD = 0.011
_TRAIN_START = date(2024, 1, 1)
_TRAIN_END = date(2025, 12, 31)
_MAX_CALENDAR_DAYS = 31
_BATCH_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
_BATCH_DAILY_CHECKS = (
    "amount_numeric_finite_nonnegative",
    "non_empty",
    "ohlc_boundaries",
    "ohlc_numeric_finite_positive",
    "required_columns",
    "row_count_below_6000",
    "symbol_matches",
    "trade_date_in_range",
    "trade_date_yyyymmdd",
    "unique_key",
    "vol_numeric_finite_nonnegative",
)
_BATCH_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SZ|SH|BJ)$")


class _CacheIssue(Exception):
    """Safe, bounded cache error with JSON-ready evidence."""

    def __init__(self, status: str, issue: str, **evidence: Any) -> None:
        super().__init__(issue)
        self.status = status
        self.issue = issue
        self.evidence = evidence


def _check(
    check_id: str,
    title: str,
    status: str,
    message: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "message": message,
        "evidence": dict(evidence or {}),
    }


def _raw_symbol(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    parts = value.upper().split(".")
    if len(parts) != 2 or not re.fullmatch(r"\d{6}", parts[0]):
        return None
    code = parts[0]
    if code in _RAW_SYMBOLS:
        expected_market = "SH" if code.startswith("6") else "SZ"
    elif code.startswith(("000", "001", "002", "003")):
        expected_market = "SZ"
    elif code.startswith(("600", "601", "603", "605")):
        expected_market = "SH"
    else:
        return None
    suffix = parts[1]
    if suffix not in _SYMBOL_ALIASES[expected_market]:
        return None
    return code, _RAW_SYMBOLS.get(code, f"{code}.{expected_market}")


def _parse_config_scope(config: Mapping[str, Any]) -> tuple[date, date, str, str]:
    start_text = config.get("start")
    end_text = config.get("end")
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise ValueError("config_dates_missing")
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError("config_dates_invalid") from exc
    if start.isoformat() != start_text or end.isoformat() != end_text:
        raise ValueError("config_dates_not_iso")
    if end < start or (end - start).days + 1 > _MAX_CALENDAR_DAYS:
        raise ValueError("config_window_out_of_bounds")
    if start < _TRAIN_START or end > _TRAIN_END:
        raise ValueError("config_outside_frozen_training_window")
    symbol = _raw_symbol(config.get("symbol"))
    if symbol is None:
        return start, end, "", ""
    return start, end, symbol[0], symbol[1]


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _stable_read(path: Path, max_bytes: int) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    try:
        path_before = path.lstat()
    except FileNotFoundError as exc:
        raise _CacheIssue("unverified", "cache_file_missing", file=path.name) from exc
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        raise _CacheIssue("mismatch", "cache_file_not_regular", file=path.name)
    if path_before.st_size > max_bytes:
        raise _CacheIssue(
            "mismatch", "cache_file_over_size_limit", file=path.name,
            size_bytes=int(path_before.st_size), max_bytes=max_bytes,
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _CacheIssue("unverified", "cache_file_missing", file=path.name) from exc
    except OSError as exc:
        raise _CacheIssue(
            "mismatch", "cache_file_open_failed", file=path.name,
            error_type=type(exc).__name__,
        ) from exc

    try:
        opened_before = os.fstat(fd)
        if not stat.S_ISREG(opened_before.st_mode):
            raise _CacheIssue("mismatch", "cache_file_not_regular", file=path.name)
        identity_before = _identity(opened_before)
        if identity_before != _identity(path_before):
            raise _CacheIssue("mismatch", "cache_file_changed_before_read", file=path.name)
        if opened_before.st_size > max_bytes:
            raise _CacheIssue(
                "mismatch", "cache_file_over_size_limit", file=path.name,
                size_bytes=int(opened_before.st_size), max_bytes=max_bytes,
            )
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while True:
                chunk = handle.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise _CacheIssue(
                        "mismatch", "cache_file_over_size_limit", file=path.name,
                        size_bytes=total, max_bytes=max_bytes,
                    )
        opened_after = os.fstat(fd)
        try:
            path_after = path.lstat()
        except FileNotFoundError as exc:
            raise _CacheIssue("mismatch", "cache_file_removed_during_read", file=path.name) from exc
        if (
            _identity(opened_after) != identity_before
            or _identity(path_after) != identity_before
            or stat.S_ISLNK(path_after.st_mode)
        ):
            raise _CacheIssue("mismatch", "cache_file_changed_during_read", file=path.name)
        return b"".join(chunks), identity_before
    finally:
        os.close(fd)


def _cache_directory(cache_root: Path = RAW_DAILY_PROBE_ROOT) -> None:
    cache_root = Path(cache_root)
    try:
        relative = cache_root.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise _CacheIssue("mismatch", "cache_directory_outside_project_root") from exc
    current = PROJECT_ROOT
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise _CacheIssue("unverified", "cache_directory_missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _CacheIssue("mismatch", "cache_directory_not_regular_directory")


def _decode_daily_closes(
    raw_bytes: bytes,
    target_symbol: str,
    start: date,
    end: date,
    issue_prefix: str,
) -> dict[str, float]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise _CacheIssue("unverified", "pyarrow_unavailable") from exc

    filename = f"{target_symbol}.parquet"
    try:
        parquet = pq.ParquetFile(io.BytesIO(raw_bytes))
        required_columns = {"ts_code", "trade_date", "close"}
        if not required_columns.issubset(set(parquet.schema_arrow.names)):
            raise _CacheIssue("mismatch", f"{issue_prefix}_schema_invalid", file=filename)
        table = pq.read_table(
            io.BytesIO(raw_bytes),
            columns=["ts_code", "trade_date", "close"],
            filters=[
                ("ts_code", "=", target_symbol),
                ("trade_date", ">=", start.strftime("%Y%m%d")),
                ("trade_date", "<=", end.strftime("%Y%m%d")),
            ],
        )
        rows = table.to_pylist()
    except _CacheIssue:
        raise
    except Exception as exc:
        raise _CacheIssue(
            "mismatch", f"{issue_prefix}_parquet_invalid", file=filename,
            error_type=type(exc).__name__,
        ) from exc

    daily: dict[str, float] = {}
    for row in rows:
        trade_date = row.get("trade_date")
        close = row.get("close")
        symbol = row.get("ts_code")
        if not isinstance(trade_date, str) or not re.fullmatch(r"\d{8}", trade_date):
            raise _CacheIssue("mismatch", f"{issue_prefix}_trade_date_invalid", symbol=target_symbol)
        if symbol != target_symbol:
            raise _CacheIssue("mismatch", f"{issue_prefix}_symbol_mismatch", symbol=target_symbol)
        try:
            parsed_date = datetime.strptime(trade_date, "%Y%m%d").date()
        except ValueError as exc:
            raise _CacheIssue("mismatch", f"{issue_prefix}_trade_date_invalid", symbol=target_symbol) from exc
        if not (start <= parsed_date <= end):
            cache_prefix = issue_prefix.split("_", 1)[0]
            raise _CacheIssue(
                "mismatch", f"{cache_prefix}_filter_returned_out_of_window_row", symbol=target_symbol,
            )
        if (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(float(close))
            or float(close) <= 0
        ):
            raise _CacheIssue(
                "mismatch", f"{issue_prefix}_close_invalid", symbol=target_symbol, trade_date=trade_date,
            )
        if trade_date in daily:
            raise _CacheIssue(
                "mismatch", f"{issue_prefix}_duplicate_date", symbol=target_symbol, trade_date=trade_date,
            )
        daily[trade_date] = float(close)
    return daily


def _read_cache(
    code: str, start: date, end: date
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _cache_directory(RAW_DAILY_PROBE_ROOT)
    manifest_path = RAW_DAILY_PROBE_ROOT / "manifest.json"
    manifest_bytes, _ = _stable_read(manifest_path, _MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CacheIssue("mismatch", "cache_manifest_invalid_json") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise _CacheIssue("mismatch", "cache_manifest_schema_invalid")

    expected_symbols = manifest.get("expected_symbols")
    symbol_rows = manifest.get("symbols")
    if (
        not isinstance(expected_symbols, list)
        or not all(isinstance(symbol, str) for symbol in expected_symbols)
        or set(expected_symbols) != set(_RAW_SYMBOLS.values())
        or len(expected_symbols) != len(_RAW_SYMBOLS)
        or not isinstance(symbol_rows, list)
        or len(symbol_rows) != len(_RAW_SYMBOLS)
    ):
        raise _CacheIssue("mismatch", "cache_manifest_symbol_inventory_invalid")

    request_window = manifest.get("request_window")
    if not isinstance(request_window, Mapping):
        raise _CacheIssue("mismatch", "cache_manifest_window_invalid")
    try:
        declared_start = datetime.strptime(request_window.get("start_date", ""), "%Y%m%d").date()
        declared_end = datetime.strptime(request_window.get("end_date", ""), "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise _CacheIssue("mismatch", "cache_manifest_window_invalid") from exc
    if declared_end < declared_start or declared_start > start or declared_end < end:
        raise _CacheIssue("mismatch", "cache_manifest_window_does_not_cover_run")

    entries: dict[str, Mapping[str, Any]] = {}
    for item in symbol_rows:
        if not isinstance(item, Mapping):
            raise _CacheIssue("mismatch", "cache_manifest_symbol_entry_invalid")
        symbol = item.get("symbol")
        if symbol not in _RAW_SYMBOLS.values() or symbol in entries:
            raise _CacheIssue("mismatch", "cache_manifest_symbol_entry_invalid")
        expected_filename = f"{symbol}.parquet"
        if item.get("parquet_file") != expected_filename:
            raise _CacheIssue(
                "mismatch", "cache_manifest_filename_invalid", symbol=symbol,
            )
        declared_hash = item.get("sha256")
        if not isinstance(declared_hash, str) or not _HEX_256.fullmatch(declared_hash):
            raise _CacheIssue(
                "mismatch", "cache_manifest_hash_invalid", symbol=symbol,
            )
        if item.get("status") != "passed":
            raise _CacheIssue(
                "mismatch", "cache_manifest_file_not_passed", symbol=symbol,
            )
        entries[symbol] = item
    if set(entries) != set(_RAW_SYMBOLS.values()):
        raise _CacheIssue("mismatch", "cache_manifest_symbol_inventory_invalid")

    target_symbol = _RAW_SYMBOLS[code]
    target_bytes: bytes | None = None
    file_evidence: list[dict[str, Any]] = []
    for symbol in _RAW_SYMBOLS.values():
        filename = f"{symbol}.parquet"
        path = RAW_DAILY_PROBE_ROOT / filename
        raw_bytes, identity = _stable_read(path, _MAX_PARQUET_BYTES)
        actual_hash = hashlib.sha256(raw_bytes).hexdigest()
        declared_hash = entries[symbol]["sha256"]
        if actual_hash != declared_hash:
            raise _CacheIssue(
                "mismatch", "cache_file_sha256_mismatch", file=filename,
                expected_sha256=declared_hash, actual_sha256=actual_hash,
            )
        file_evidence.append({
            "symbol": symbol,
            "file": filename,
            "size_bytes": len(raw_bytes),
            "sha256": actual_hash,
            "identity_stable": True,
        })
        if symbol == target_symbol:
            target_bytes = raw_bytes

    if target_bytes is None:
        raise _CacheIssue("mismatch", "cache_target_file_missing", file=f"{target_symbol}.parquet")
    daily = _decode_daily_closes(target_bytes, target_symbol, start, end, "cache_target")

    cache_evidence = {
        "manifest_status_declared": manifest.get("status"),
        "provider_declared": manifest.get("provider"),
        "price_basis_declared": manifest.get("price_basis"),
        "price_basis_verified_declared": manifest.get("price_basis_verified"),
        "declared_request_window": {
            "start_date": request_window.get("start_date"),
            "end_date": request_window.get("end_date"),
        },
        "cache_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "hashes_verified_against_local_manifest": True,
        "files": file_evidence,
        "target_symbol": target_symbol,
        "target_rows_in_configured_window": len(daily),
        "target_daily_closes": {key: daily[key] for key in sorted(daily)},
        "filter_window": [start.isoformat(), end.isoformat()],
    }
    return cache_evidence, [{"trade_date": key, "close": value} for key, value in sorted(daily.items())]


def _read_batch_manifest(
    cache_name: str,
    cache_root: Path,
    pinned_sha256: str,
    expected_symbol_count: int,
    start: date,
    end: date,
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]], dict[str, Any]]:
    _cache_directory(cache_root)
    manifest_bytes, _ = _stable_read(cache_root / "manifest.json", _MAX_MANIFEST_BYTES)
    actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_hash != pinned_sha256:
        raise _CacheIssue(
            "mismatch", "batch_manifest_sha256_mismatch", cache=cache_name,
            expected_sha256=pinned_sha256, actual_sha256=actual_manifest_hash,
        )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CacheIssue("mismatch", "batch_manifest_invalid_json", cache=cache_name) from exc
    if (
        not isinstance(manifest, Mapping)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "passed"
        or manifest.get("mode") != "RESEARCH_ONLY"
        or manifest.get("provider") != "official_tushare"
        or manifest.get("price_basis") != "unadjusted"
        or manifest.get("price_basis_verified") is not False
        or manifest.get("vol_unit") != "手"
        or manifest.get("amount_unit") != "千元"
        or manifest.get("fields") != _BATCH_DAILY_FIELDS
        or manifest.get("calendar_completeness_verified") is not False
        or manifest.get("corporate_actions_verified") is not False
        or manifest.get("paper_admission") is not False
    ):
        raise _CacheIssue("mismatch", "batch_manifest_metadata_invalid", cache=cache_name)

    request_window = manifest.get("request_window")
    expected_window = {"start_date": "20230101", "end_date": "20251231"}
    if not isinstance(request_window, Mapping) or dict(request_window) != expected_window:
        raise _CacheIssue("mismatch", "batch_manifest_window_invalid", cache=cache_name)
    try:
        declared_start = datetime.strptime(request_window["start_date"], "%Y%m%d").date()
        declared_end = datetime.strptime(request_window["end_date"], "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise _CacheIssue("mismatch", "batch_manifest_window_invalid", cache=cache_name) from exc
    if declared_start > start or declared_end < end:
        raise _CacheIssue("mismatch", "batch_manifest_window_does_not_cover_run", cache=cache_name)

    expected_symbols = manifest.get("expected_symbols")
    symbol_rows = manifest.get("symbols")
    if (
        not isinstance(expected_symbols, list)
        or len(expected_symbols) != expected_symbol_count
        or not all(isinstance(symbol, str) and _BATCH_SYMBOL_RE.fullmatch(symbol) for symbol in expected_symbols)
        or len(set(expected_symbols)) != expected_symbol_count
        or not isinstance(symbol_rows, list)
        or len(symbol_rows) != expected_symbol_count
    ):
        raise _CacheIssue("mismatch", "batch_manifest_symbol_inventory_invalid", cache=cache_name)

    required_entry_keys = {
        "checks", "error_type", "parquet_file", "raw_rows", "request", "sha256",
        "sha256_verified", "status", "symbol", "validation_errors",
    }
    entries: dict[str, Mapping[str, Any]] = {}
    for item in symbol_rows:
        if not isinstance(item, Mapping) or set(item) != required_entry_keys:
            raise _CacheIssue("mismatch", "batch_manifest_entry_metadata_invalid", cache=cache_name)
        symbol = item.get("symbol")
        if (
            not isinstance(symbol, str)
            or not _BATCH_SYMBOL_RE.fullmatch(symbol)
            or symbol in entries
        ):
            raise _CacheIssue("mismatch", "batch_manifest_entry_symbol_invalid", cache=cache_name)
        filename = item.get("parquet_file")
        expected_filename = f"{symbol}.parquet"
        if (
            not isinstance(filename, str)
            or filename != expected_filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise _CacheIssue(
                "mismatch", "batch_manifest_filename_invalid", cache=cache_name, symbol=symbol,
            )
        declared_hash = item.get("sha256")
        if not isinstance(declared_hash, str) or not _HEX_256.fullmatch(declared_hash):
            raise _CacheIssue(
                "mismatch", "batch_manifest_entry_hash_invalid", cache=cache_name, symbol=symbol,
            )
        if (
            item.get("status") != "passed"
            or item.get("sha256_verified") is not True
            or item.get("error_type") is not None
            or item.get("validation_errors") != []
        ):
            raise _CacheIssue(
                "mismatch", "batch_manifest_entry_status_invalid", cache=cache_name, symbol=symbol,
            )
        raw_rows = item.get("raw_rows")
        if isinstance(raw_rows, bool) or not isinstance(raw_rows, int) or not (1 <= raw_rows < 6000):
            raise _CacheIssue(
                "mismatch", "batch_manifest_entry_row_count_invalid", cache=cache_name, symbol=symbol,
            )
        checks = item.get("checks")
        if (
            not isinstance(checks, Mapping)
            or set(checks) != set(_BATCH_DAILY_CHECKS)
            or any(checks.get(name) is not True for name in _BATCH_DAILY_CHECKS)
        ):
            raise _CacheIssue(
                "mismatch", "batch_manifest_entry_checks_invalid", cache=cache_name, symbol=symbol,
            )
        request = item.get("request")
        expected_request = {
            "api_name": "daily",
            "fields": _BATCH_DAILY_FIELDS,
            "parameters": {
                "end_date": "20251231",
                "start_date": "20230101",
                "ts_code": symbol,
            },
        }
        if not isinstance(request, Mapping) or dict(request) != expected_request:
            raise _CacheIssue(
                "mismatch", "batch_manifest_entry_request_invalid", cache=cache_name, symbol=symbol,
            )
        entries[symbol] = item

    if set(entries) != set(expected_symbols):
        raise _CacheIssue("mismatch", "batch_manifest_symbol_inventory_invalid", cache=cache_name)
    return manifest, entries, {
        "cache": cache_name,
        "manifest_sha256": actual_manifest_hash,
        "manifest_sha256_pinned": True,
        "expected_symbol_count": expected_symbol_count,
        "manifest_symbols_unique": True,
    }


def _read_batch_target(
    cache_name: str,
    cache_root: Path,
    manifest: Mapping[str, Any],
    manifest_evidence: Mapping[str, Any],
    entry: Mapping[str, Any],
    target_symbol: str,
    start: date,
    end: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    filename = entry["parquet_file"]
    raw_bytes, _ = _stable_read(cache_root / filename, _MAX_PARQUET_BYTES)
    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    declared_hash = entry["sha256"]
    if actual_hash != declared_hash:
        raise _CacheIssue(
            "mismatch", "batch_target_sha256_mismatch", cache=cache_name, file=filename,
            expected_sha256=declared_hash, actual_sha256=actual_hash,
        )
    daily = _decode_daily_closes(raw_bytes, target_symbol, start, end, "batch_target")

    cache_evidence = {
        "reference_cache": cache_name,
        "manifest_sha256": manifest_evidence["manifest_sha256"],
        "manifest_sha256_pinned": True,
        "manifest_provider_declared": manifest.get("provider"),
        "price_basis_declared": manifest.get("price_basis"),
        "price_basis_verified_declared": manifest.get("price_basis_verified"),
        "declared_request_window": dict(manifest["request_window"]),
        "vol_unit_declared": manifest.get("vol_unit"),
        "amount_unit_declared": manifest.get("amount_unit"),
        "target_file": filename,
        "target_file_sha256": actual_hash,
        "target_file_sha256_verified": True,
        "target_file_identity_stable": True,
        "target_rows_in_configured_window": len(daily),
        "target_daily_closes": {key: daily[key] for key in sorted(daily)},
        "filter_window": [start.isoformat(), end.isoformat()],
        "other_manifest_catalog_issues": [],
    }
    return cache_evidence, [{"trade_date": key, "close": value} for key, value in sorted(daily.items())]


def _read_batch_reference(
    raw_symbol: str, start: date, end: date
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    """Read only pinned metadata and, on membership, one bounded target file."""
    manifests: list[tuple[str, Path, dict[str, Any], dict[str, Mapping[str, Any]], dict[str, Any]]] = []
    catalog_issues: list[dict[str, str]] = []
    for cache_name, cache_root, pinned_sha256, expected_count in _RAW_DAILY_BATCH_REFERENCES:
        try:
            manifest, entries, evidence = _read_batch_manifest(
                cache_name, Path(cache_root), pinned_sha256, expected_count, start, end,
            )
        except _CacheIssue as exc:
            catalog_issues.append({"cache": cache_name, "issue": exc.issue, "status": exc.status})
            continue
        manifests.append((cache_name, Path(cache_root), manifest, entries, evidence))

    matching = [item for item in manifests if raw_symbol in item[3]]
    if len(matching) > 1:
        raise _CacheIssue(
            "mismatch", "batch_target_membership_ambiguous", symbol=raw_symbol,
            caches=[item[0] for item in matching],
        )
    if not matching:
        if catalog_issues:
            first_issue = catalog_issues[0]
            raise _CacheIssue(
                first_issue["status"], first_issue["issue"], cache=first_issue["cache"],
            )
        return None, [], {
            "manifest_read_performed": True,
            "target_file_read_performed": False,
            "target_symbol": raw_symbol,
            "membership": "not_found_in_pinned_manifests",
            "manifests_checked": [item[4] for item in manifests],
        }

    cache_name, cache_root, manifest, entries, manifest_evidence = matching[0]
    cache_evidence, daily_rows = _read_batch_target(
        cache_name, cache_root, manifest, manifest_evidence, entries[raw_symbol],
        raw_symbol, start, end,
    )
    cache_evidence["other_manifest_catalog_issues"] = catalog_issues
    return cache_evidence, daily_rows, {
        "manifest_read_performed": True,
        "target_file_read_performed": True,
        "target_symbol": raw_symbol,
        "membership": "matched",
        "selected_cache": cache_name,
        "manifests_checked": [item[4] for item in manifests],
        "manifest_catalog_issues": catalog_issues,
    }


def _source_uncertainty_checks(manifest: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    manifest = manifest if isinstance(manifest, Mapping) else {}
    source_price_adjustment = manifest.get("source_price_adjustment")
    price_adjustment = manifest.get("price_adjustment")
    volume_semantics = manifest.get("volume_semantics")
    turnover_semantics = manifest.get("turnover_capacity_semantics")
    reference_equivalence = manifest.get("reference_matcher_equivalence")
    daily_completeness = manifest.get("calendar_completeness_verified")
    actions_verified = manifest.get("corporate_actions_verified")
    return [
        _check(
            "minute_price_basis", "分钟价格复权口径", "unverified",
            "分钟价格复权口径及历史版本未认证。",
            {"source_price_adjustment": source_price_adjustment, "price_adjustment": price_adjustment},
        ),
        _check(
            "volume_units", "成交量单位", "unverified",
            "成交量是否为原始股数未认证。",
            {"volume_semantics": volume_semantics},
        ),
        _check(
            "turnover_units", "成交额单位与容量语义", "unverified",
            "成交额单位与容量代理未认证。",
            {"turnover_capacity_semantics": turnover_semantics},
        ),
        _check(
            "pit_st_state", "点时 ST 状态", "unverified",
            "未验证每个回测时点的 ST 状态。",
        ),
        _check(
            "limit_suspend_state", "涨跌停与停牌状态", "unverified",
            "未验证历史涨跌停价、停牌及可交易状态。",
        ),
        _check(
            "trading_calendar", "交易日历完整性", "unverified",
            "交易日历完整性未认证。",
            {"calendar_completeness_verified": daily_completeness},
        ),
        _check(
            "corporate_actions", "公司行动完整性", "unverified",
            "公司行动及其历史生效时间未认证。",
            {"corporate_actions_verified": actions_verified},
        ),
        _check(
            "execution_parity", "执行模型一致性", "unverified",
            "与其他回测撮合器或真实执行链路的等价性未认证。",
            {"reference_matcher_equivalence": reference_equivalence},
        ),
    ]


def _overall_status(checks: list[dict[str, Any]]) -> str:
    return "mismatch" if any(item["status"] == "mismatch" for item in checks) else "unverified"


def assess_data(config: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Assess only existing result bars and a tiny fixed daily cache.

    The function is read-only.  It does not re-open the minute lake, modify the
    result, alter costs or fills, or make a paper-admission decision.
    """
    checks: list[dict[str, Any]] = []
    manifest_value = result.get("data_manifest") if isinstance(result, Mapping) else None
    run_manifest = manifest_value if isinstance(manifest_value, Mapping) else None
    checks.extend(_source_uncertainty_checks(run_manifest))

    if not isinstance(config, Mapping) or not isinstance(result, Mapping):
        checks.append(_check(
            "input_scope", "输入范围", "mismatch", "无法核验回测配置或已保存结果。",
            {"error_type": "invalid_input"},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    try:
        start, end, code, raw_symbol = _parse_config_scope(config)
    except ValueError as exc:
        issue = str(exc)
        checks.append(_check(
            "input_scope", "输入范围", "mismatch", "回测范围不符合本模块的冻结训练窗口。",
            {"issue": issue},
        ))
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "未读取日线缓存；配置范围不满足受限核查条件。",
            {"read_performed": False},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    requested_range = run_manifest.get("requested_range_inclusive") if run_manifest else None
    manifest_symbol = run_manifest.get("requested_symbol") if run_manifest else None
    date_scope_matches = (
        isinstance(requested_range, list)
        and len(requested_range) == 2
        and requested_range == [start.isoformat(), end.isoformat()]
    )
    symbol_scope_matches = (
        _raw_symbol(manifest_symbol) == (code, raw_symbol)
        if code
        else manifest_symbol == config.get("symbol")
    )
    manifest_scope_matches = date_scope_matches and symbol_scope_matches
    if run_manifest is not None and not manifest_scope_matches:
        checks.append(_check(
            "run_scope", "回测结果范围", "mismatch",
            "结果数据清单与请求配置范围或代码不一致，停止价格比较。",
            {"requested_range_inclusive": requested_range, "requested_symbol": manifest_symbol},
        ))
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "未读取日线缓存；回测结果范围未匹配。",
            {"read_performed": False},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    checks.append(_check(
        "run_scope", "回测结果范围", "observed",
        "配置处于冻结训练窗口内，且结果清单范围匹配。" if run_manifest else
        "配置处于冻结训练窗口内；结果清单缺失，未作范围交叉核验。",
        {
            "symbol": config.get("symbol"),
            "date_range_inclusive": [start.isoformat(), end.isoformat()],
            "calendar_days": (end - start).days + 1,
            "result_manifest_present": run_manifest is not None,
            "result_bar_count": len(result.get("bars", [])) if isinstance(result.get("bars", []), list) else None,
        },
    ))

    if not code:
        checks.append(_check(
            "raw_daily_cache_integrity", "固定原始日线缓存完整性", "unverified",
            "固定缓存没有该代码的对照文件；未访问其他数据源。",
            {"read_performed": False, "configured_symbol": config.get("symbol")},
        ))
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "没有匹配的缓存代码，价格对照未知。",
            {"read_performed": False},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    batch_membership_evidence: dict[str, Any] | None = None
    try:
        if code in _RAW_SYMBOLS:
            # Keep the original three-code probe first and independent. A failure
            # here must never fall through to a broader optional reference.
            cache_evidence, daily_rows = _read_cache(code, start, end)
        else:
            cache_evidence, daily_rows, batch_membership_evidence = _read_batch_reference(
                raw_symbol, start, end,
            )
    except _CacheIssue as exc:
        checks.append(_check(
            "raw_daily_cache_integrity", "固定原始日线缓存完整性", exc.status,
            "固定原始日线缓存未能通过受限完整性核验。",
            {"issue": exc.issue, **exc.evidence},
        ))
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "缓存未能完成完整性核验，未作价格比较。",
            {"read_performed": False},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    if cache_evidence is None:
        membership_evidence = batch_membership_evidence or {
            "manifest_read_performed": False,
            "target_file_read_performed": False,
            "target_symbol": raw_symbol,
            "membership": "unknown",
        }
        checks.append(_check(
            "raw_daily_cache_integrity", "固定原始日线缓存完整性", "unverified",
            "固定批次清单中未找到该代码；未读取或解码目标价格文件。",
            {"read_performed": True, **membership_evidence},
        ))
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "该代码不属于已验证的固定批次清单，价格对照未知。",
            {"read_performed": False, **membership_evidence},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    checks.append(_check(
        "raw_daily_cache_integrity", "固定原始日线缓存完整性", "observed",
        (
            "固定批次清单的 SHA256 与固定值一致，目标日线文件与清单哈希一致，且读取期间文件身份稳定。"
            if cache_evidence.get("reference_cache")
            else "三份固定日线缓存文件的 SHA256 与本地清单一致，且读取期间文件身份稳定。"
        ),
        cache_evidence,
    ))
    checks.append(_check(
        "daily_cache_price_basis", "日线缓存价格口径", "unverified",
        "缓存清单虽标称未复权，但 price_basis_verified 为 false；该声明不能认证价格口径。",
        {
            "provider_declared": cache_evidence.get(
                "provider_declared", cache_evidence.get("manifest_provider_declared"),
            ),
            "price_basis_declared": cache_evidence["price_basis_declared"],
            "price_basis_verified_declared": cache_evidence["price_basis_verified_declared"],
            "manifest_status_declared": cache_evidence.get(
                "manifest_status_declared", "passed",
            ),
        },
    ))

    bars = result.get("bars")
    if not isinstance(bars, list):
        checks.append(_check(
            "daily_reference", "原始日线对照", "unverified",
            "已保存结果没有可读取的 bars 列表。",
            {"read_performed": True, "bar_list_present": False},
        ))
        return {
            "schema_version": 1,
            "status": _overall_status(checks),
            "checks": checks,
            "paper_admission": False,
            "limitations": _limitations(),
        }

    run_15h: dict[str, float] = {}
    duplicate_run_dates: list[str] = []
    unusable_15h_rows = 0
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        bar_symbol = _raw_symbol(bar.get("symbol", config.get("symbol")))
        if bar_symbol != (code, raw_symbol):
            continue
        source_text = bar.get("source_datetime")
        if not isinstance(source_text, str):
            continue
        try:
            source_time = datetime.fromisoformat(source_text)
        except ValueError:
            continue
        if source_time.tzinfo is not None or source_time.date() < start or source_time.date() > end:
            continue
        if (source_time.hour, source_time.minute, source_time.second, source_time.microsecond) != (15, 0, 0, 0):
            continue
        trade_date = source_time.date().strftime("%Y%m%d")
        close = bar.get("close")
        if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(float(close)) or float(close) <= 0:
            unusable_15h_rows += 1
            continue
        if trade_date in run_15h:
            duplicate_run_dates.append(trade_date)
        run_15h[trade_date] = float(close)

    daily_by_date = {row["trade_date"]: row["close"] for row in daily_rows}
    paired_dates = sorted(set(run_15h) & set(daily_by_date))
    comparisons = [
        {
            "trade_date": trade_date,
            "source_datetime": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00",
            "run_15h_close": run_15h[trade_date],
            "daily_cache_close": daily_by_date[trade_date],
            "absolute_difference": abs(run_15h[trade_date] - daily_by_date[trade_date]),
            "mismatch_threshold": _PRICE_MISMATCH_THRESHOLD,
        }
        for trade_date in paired_dates
    ]
    mismatched = [item for item in comparisons if item["absolute_difference"] > _PRICE_MISMATCH_THRESHOLD]
    if duplicate_run_dates:
        reference_status = "mismatch"
        reference_message = "存在重复的 15:00 bar，无法建立唯一的日末对照。"
    elif mismatched:
        reference_status = "mismatch"
        reference_message = "至少一个 15:00 bar close 与缓存同日 close 的差异超过 0.011。"
    elif comparisons and not (
        set(daily_by_date) - set(run_15h)
        or set(run_15h) - set(daily_by_date)
        or unusable_15h_rows
    ):
        reference_status = "observed"
        reference_message = "已逐日比较相同日期的 15:00 bar close 与缓存日线 close；该观察不认证价格口径。"
    elif comparisons:
        reference_status = "unverified"
        reference_message = "窗口内只有部分日期可配对；未把缺少的日末价格视作通过。"
    else:
        reference_status = "unverified"
        reference_message = "没有可配对的 15:00 bar 与同日缓存 close；未用盘中 close 替代。"
    checks.append(_check(
        "daily_reference", "原始日线对照", reference_status, reference_message,
        {
            "cache_symbol": raw_symbol,
            "daily_rows_in_configured_window": len(daily_rows),
            "run_15h_days": len(run_15h),
            "paired_days": len(comparisons),
            "mismatch_days": len(mismatched),
            "missing_15h_for_unpaired_cache_days": sorted(set(daily_by_date) - set(run_15h)),
            "missing_cache_for_unpaired_15h_days": sorted(set(run_15h) - set(daily_by_date)),
            "duplicate_run_15h_dates": sorted(set(duplicate_run_dates)),
            "unusable_15h_rows": unusable_15h_rows,
            "comparisons": comparisons,
            "close_ratio_inversion_used": False,
        },
    ))

    return {
        "schema_version": 1,
        "status": _overall_status(checks),
        "checks": checks,
        "paper_admission": False,
        "limitations": _limitations(),
    }


def _limitations() -> list[str]:
    return [
        "所有结果保持 RESEARCH_ONLY；paper_admission 固定为 false。",
        "比较只使用已保存 bars 与固定日线参考文件的配置日期；不重读分钟库。",
        "缓存标称未复权但自身 price_basis_verified=false，SHA256 一致只证明缓存文件与清单一致。",
        "有限日期的收盘价匹配不能认证分钟价格复权口径，也不能由 close ratio 反推分钟原价。",
        "成交量单位、成交额容量语义、点时 ST、涨跌停与停牌状态、日历完整性、公司行动和执行一致性均未认证。",
    ]
