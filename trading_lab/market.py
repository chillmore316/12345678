"""Isolated daily market-data cache for the local TradingLab MVP.

This module does not connect to a broker and never fetches data during service
construction or reads. Network access occurs only through ``sync`` and uses a
Tushare-shaped POST relay. Cached bars are immutable snapshots with row-level
source and adjustment lineage.
"""

from __future__ import annotations

import csv
import importlib
import inspect
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_HTTP_URL = "http://tus.sxdata.top/tu_share/"
MODE = "收盘历史数据"
ETF_LIMIT_RULE_DATE = "2026-11-17"
ETF_LIMIT_RULE_SOURCE_URL = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/"
    "c/c_20260424_10816492.shtml"
)
ETF_LIMIT_RULE_SYMBOL = "515880.SH"
ETF_LIMIT_SOURCE = "515880普通股票ETF规则推导(10%，非供应商逐日涨跌停)"
_API_ALLOWLIST = {"daily", "fund_daily", "index_daily", "trade_cal", "stk_limit"}
_API_FIELDS = {
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "fund_daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "index_daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "trade_cal": "exchange,cal_date,is_open",
    "stk_limit": "ts_code,trade_date,up_limit,down_limit",
}
_SYMBOLS: dict[str, dict[str, Any]] = {
    "603283.SH": {"name": "赛腾股份", "kind": "stock", "tick": 0.01, "lot": 100},
    "515880.SH": {"name": "通信ETF", "kind": "etf", "tick": 0.001, "lot": 100},
    "000300.SH": {"name": "沪深300", "kind": "index", "tick": 0.01, "lot": 0},
}
_NETWORK_LOCK = threading.Lock()
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_IMPORT_BYTES = 64 * 1024 * 1024
_MAX_IMPORT_ROWS = 500_000


class _ProviderFailure(Exception):
    def __init__(self, kind: str, retry_after: int | None = None):
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(kind)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _safe_date(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    fmt = "%Y%m%d" if re.fullmatch(r"\d{8}", text) else "%Y-%m-%d"
    try:
        return datetime.strptime(text, fmt).date().isoformat()
    except (TypeError, ValueError):
        raise ValueError("日期须为 YYYY-MM-DD 或 YYYYMMDD") from None


def _compact_date(iso_date: str) -> str:
    return iso_date.replace("-", "")


def _decimal(value: Any, field: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        raise ValueError(f"{field}缺失")
    if isinstance(value, bool):
        raise ValueError(f"{field}格式错误")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field}格式错误") from None
    if not result.is_finite():
        raise ValueError(f"{field}必须为有限数字")
    return result


def _normalise_bar(
    symbol: str,
    record: dict[str, Any],
    *,
    volume_scale: Decimal = Decimal("1"),
    amount_scale: Decimal = Decimal("1"),
    raw_tushare: bool = False,
) -> dict[str, Any]:
    code = record.get("ts_code", record.get("symbol"))
    if code not in (None, "", symbol):
        raise ValueError("文件含有不匹配的标的代码")
    day_value = record.get("date", record.get("trade_date"))
    day = _safe_date(day_value)
    prices: dict[str, Decimal] = {}
    for key in ("open", "high", "low", "close"):
        price = _decimal(record.get(key), key)
        assert price is not None
        if price <= 0:
            raise ValueError("价格必须大于零")
        prices[key] = price
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
        raise ValueError("最高价低于其他 OHLC 价格")
    if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
        raise ValueError("最低价高于其他 OHLC 价格")

    volume_key = "vol" if raw_tushare else "volume"
    volume = _decimal(record.get(volume_key), "成交量")
    amount = _decimal(record.get("amount"), "成交额")
    assert volume is not None and amount is not None
    volume *= volume_scale
    amount *= amount_scale
    if volume < 0 or amount < 0:
        raise ValueError("成交量和成交额不能为负数")
    if volume != volume.to_integral_value():
        raise ValueError("成交量换算后不是整数股")
    if volume == 0 and amount != 0:
        raise ValueError("零成交量记录的成交额必须为零")

    pre_close = _decimal(record.get("pre_close"), "昨收", allow_none=True)
    if pre_close is not None and pre_close <= 0:
        raise ValueError("昨收必须大于零")
    up = _decimal(record.get("up_limit"), "涨停价", allow_none=True)
    down = _decimal(record.get("down_limit"), "跌停价", allow_none=True)
    if (up is None) != (down is None):
        raise ValueError("涨停价和跌停价必须同时提供")
    if up is not None and (up <= 0 or down is None or down <= 0 or up < down):
        raise ValueError("涨跌停价无效")
    return {
        "date": day,
        "open": float(prices["open"]),
        "high": float(prices["high"]),
        "low": float(prices["low"]),
        "close": float(prices["close"]),
        "volume": int(volume),
        "amount": float(amount),
        "pre_close": float(pre_close) if pre_close is not None else None,
        "up_limit": float(up) if up is not None else None,
        "down_limit": float(down) if down is not None else None,
    }


def _derive_515880_limits(pre_close: float | None) -> tuple[float, float] | None:
    if pre_close is None:
        return None
    base = _decimal(pre_close, "昨收")
    assert base is not None
    if base <= 0:
        return None
    tick = Decimal("0.001")
    upper = (base * Decimal("1.10")).quantize(tick, rounding=ROUND_HALF_UP)
    lower = (base * Decimal("0.90")).quantize(tick, rounding=ROUND_HALF_UP)
    return float(upper), float(lower)


def _identical(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(key) == b.get(key) for key in (
        "open", "high", "low", "close", "volume", "amount", "pre_close",
        "up_limit", "down_limit",
    ))


class MarketService:
    """Local SQLite daily-bar service; sync is the only network entry point."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep
        Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS market_datasets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    mode TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS market_datasets_lookup
                    ON market_datasets(symbol, source, adjustment, id);
                CREATE TABLE IF NOT EXISTS market_current (
                    symbol TEXT PRIMARY KEY,
                    dataset_id INTEGER NOT NULL REFERENCES market_datasets(id)
                );
                CREATE TABLE IF NOT EXISTS market_bars (
                    dataset_id INTEGER NOT NULL REFERENCES market_datasets(id),
                    date TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, volume INTEGER NOT NULL, amount REAL NOT NULL,
                    pre_close REAL, up_limit REAL, down_limit REAL,
                    limits_source TEXT, source TEXT NOT NULL, adjustment TEXT NOT NULL,
                    mode TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, date)
                );
                CREATE TABLE IF NOT EXISTS market_calendar (
                    exchange TEXT NOT NULL, cal_date TEXT NOT NULL,
                    is_open INTEGER NOT NULL, source TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    PRIMARY KEY(exchange, cal_date)
                );
                CREATE TABLE IF NOT EXISTS market_provider_state (
                    scope TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                    backoff_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS market_request_clock (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_request_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_sync_state (
                    symbol TEXT PRIMARY KEY, last_status TEXT NOT NULL,
                    last_warning TEXT, retry_after INTEGER,
                    updated_at TEXT NOT NULL
                );
            """)

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _symbol(symbol: str) -> str:
        code = str(symbol).strip().upper()
        if code not in _SYMBOLS:
            raise ValueError("不支持的标的代码")
        return code

    def symbols(self) -> list[dict[str, Any]]:
        return [{"symbol": key, **value} for key, value in _SYMBOLS.items()]

    def _credential(self, api_name: str) -> tuple[str, str]:
        main = os.environ.get("TUSHARE_TOKEN", "").strip()
        backup = os.environ.get("TUSHARE_BACKUP_TOKEN", "").strip()
        privileged = {
            item.strip() for item in os.environ.get("TUSHARE_PRIVILEGED_APIS", "").split(",")
            if item.strip() in _API_ALLOWLIST
        }
        if api_name in privileged:
            return (main, "main") if main else ("", "main")
        if backup:
            return backup, "backup"
        return main, "main"

    @staticmethod
    def _endpoint() -> str:
        endpoint = os.environ.get("TUSHARE_HTTP_URL", DEFAULT_HTTP_URL).strip()
        parts = urllib.parse.urlsplit(endpoint)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise _ProviderFailure("endpoint")
        return endpoint

    def _request_rows(self, api_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if api_name not in _API_ALLOWLIST:
            raise _ProviderFailure("api")
        token, slot = self._credential(api_name)
        if not token:
            raise _ProviderFailure("credentials")
        endpoint = self._endpoint()
        scope = f"{slot}:{api_name}"
        with _NETWORK_LOCK:
            now = self._clock()
            with self._db() as db:
                state = db.execute(
                    "SELECT failures,backoff_until FROM market_provider_state WHERE scope=?", (scope,)
                ).fetchone()
                if state and state["backoff_until"] > now:
                    raise _ProviderFailure("backoff", max(1, math.ceil(state["backoff_until"] - now)))
                pacing = db.execute("SELECT last_request_at FROM market_request_clock WHERE id=1").fetchone()
                last = pacing[0] if pacing else None
                delay = max(0.0, 0.8 - (now - last)) if last is not None else 0.0
                if delay:
                    self._sleep(delay)
                    now = self._clock()
                db.execute(
                    "INSERT INTO market_request_clock(id,last_request_at) VALUES(1,?) "
                    "ON CONFLICT(id) DO UPDATE SET last_request_at=excluded.last_request_at", (now,)
                )

            body = json.dumps({
                "api_name": api_name,
                "token": token,
                "params": params,
                "fields": _API_FIELDS[api_name],
            }, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                endpoint, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                trust_env = os.environ.get("TRADING_LAB_TRUST_ENV", "false").strip().lower() in {
                    "1", "true", "yes", "on",
                }
                if trust_env:
                    opener = urllib.request.build_opener(_NoRedirect())
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
                with opener.open(request, timeout=15) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError("large response")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("code") != 0:
                    raise ValueError("provider response")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("provider data")
                fields, items = data.get("fields"), data.get("items")
                if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
                    raise ValueError("provider fields")
                if len(set(fields)) != len(fields) or not isinstance(items, list) or len(items) > 200_000:
                    raise ValueError("provider rows")
                records: list[dict[str, Any]] = []
                for item in items:
                    if isinstance(item, dict):
                        records.append(item)
                    elif isinstance(item, list) and len(item) == len(fields):
                        records.append(dict(zip(fields, item)))
                    else:
                        raise ValueError("provider row")
            except _ProviderFailure:
                raise
            except Exception:
                with self._db() as db:
                    prior = db.execute(
                        "SELECT failures FROM market_provider_state WHERE scope=?", (scope,)
                    ).fetchone()
                    failures = (prior[0] if prior else 0) + 1
                    backoff = min(60 * (2 ** min(failures - 1, 6)), 3600)
                    db.execute(
                        "INSERT INTO market_provider_state(scope,failures,backoff_until) VALUES(?,?,?) "
                        "ON CONFLICT(scope) DO UPDATE SET failures=excluded.failures,"
                        "backoff_until=excluded.backoff_until", (scope, failures, self._clock() + backoff)
                    )
                raise _ProviderFailure("request", backoff) from None
            with self._db() as db:
                db.execute(
                    "INSERT INTO market_provider_state(scope,failures,backoff_until) VALUES(?,0,0) "
                    "ON CONFLICT(scope) DO UPDATE SET failures=0,backoff_until=0", (scope,)
                )
            return records

    def _request_akshare_frame(self, symbol: str, start: str, end: str) -> tuple[Any, str]:
        kind = _SYMBOLS[symbol]["kind"]
        api_name = {"stock": "stock_zh_a_hist", "etf": "fund_etf_hist_em",
                    "index": "index_zh_a_hist"}[kind]
        scope = f"akshare:{symbol}"
        with _NETWORK_LOCK:
            now = self._clock()
            with self._db() as db:
                state = db.execute(
                    "SELECT failures,backoff_until FROM market_provider_state WHERE scope=?", (scope,)
                ).fetchone()
                if state and state["backoff_until"] > now:
                    raise _ProviderFailure("backoff", max(1, math.ceil(state["backoff_until"] - now)))
                pacing = db.execute("SELECT last_request_at FROM market_request_clock WHERE id=1").fetchone()
                last = pacing[0] if pacing else None
                delay = max(0.0, 0.8 - (now - last)) if last is not None else 0.0
                if delay:
                    self._sleep(delay)
                    now = self._clock()
                db.execute(
                    "INSERT INTO market_request_clock(id,last_request_at) VALUES(1,?) "
                    "ON CONFLICT(id) DO UPDATE SET last_request_at=excluded.last_request_at", (now,)
                )
            try:
                ak = importlib.import_module("akshare")
                function = getattr(ak, api_name, None)
                if function is None:
                    raise _ProviderFailure("akshare_api")
                signature = inspect.signature(function)
                expected = {"symbol", "period", "start_date", "end_date"}
                if kind != "index":
                    expected.add("adjust")
                if not expected.issubset(signature.parameters):
                    raise _ProviderFailure("akshare_api")
                kwargs = {
                    "symbol": symbol.split(".", 1)[0], "period": "daily",
                    "start_date": _compact_date(start), "end_date": _compact_date(end),
                }
                if kind != "index":
                    kwargs["adjust"] = ""
                frame = function(**kwargs)
                if frame is None or getattr(frame, "empty", False):
                    raise _ProviderFailure("no_data")
            except _ProviderFailure:
                raise
            except Exception:
                with self._db() as db:
                    prior = db.execute(
                        "SELECT failures FROM market_provider_state WHERE scope=?", (scope,)
                    ).fetchone()
                    failures = (prior[0] if prior else 0) + 1
                    backoff = min(60 * (2 ** min(failures - 1, 6)), 3600)
                    db.execute(
                        "INSERT INTO market_provider_state(scope,failures,backoff_until) VALUES(?,?,?) "
                        "ON CONFLICT(scope) DO UPDATE SET failures=excluded.failures,"
                        "backoff_until=excluded.backoff_until", (scope, failures, self._clock() + backoff)
                    )
                raise _ProviderFailure("request", backoff) from None
            with self._db() as db:
                db.execute(
                    "INSERT INTO market_provider_state(scope,failures,backoff_until) VALUES(?,0,0) "
                    "ON CONFLICT(scope) DO UPDATE SET failures=0,backoff_until=0", (scope,)
                )
            return frame, api_name

    @staticmethod
    def _akshare_bars(
        symbol: str, frame: Any, start: str, end: str,
    ) -> list[dict[str, Any]]:
        if isinstance(frame, list):
            records = frame
        elif hasattr(frame, "to_dict"):
            records = frame.to_dict(orient="records")
        else:
            raise ValueError("AKShare 日线结构无效")
        if not records or len(records) > 200_000:
            raise ValueError("AKShare 日线为空或行数超限")
        def pick(record: dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if key in record:
                    return record[key]
            return None
        result: dict[str, dict[str, Any]] = {}
        for record in records:
            close = pick(record, "close", "收盘")
            pre_close = pick(record, "pre_close", "昨收")
            change = pick(record, "change", "涨跌额")
            if pre_close in (None, "") and change not in (None, ""):
                close_value = _decimal(close, "收盘")
                change_value = _decimal(change, "涨跌额", allow_none=True)
                if close_value is not None and change_value is not None:
                    pre_close = close_value - change_value
            canonical = {
                "date": pick(record, "date", "trade_date", "日期"),
                "open": pick(record, "open", "开盘"), "high": pick(record, "high", "最高"),
                "low": pick(record, "low", "最低"), "close": close,
                "volume": pick(record, "volume", "成交量"),
                "amount": pick(record, "amount", "成交额"), "pre_close": pre_close,
                "up_limit": pick(record, "up_limit", "涨停价"),
                "down_limit": pick(record, "down_limit", "跌停价"),
            }
            row = _normalise_bar(
                symbol, canonical, volume_scale=Decimal("100"), amount_scale=Decimal("1"),
            )
            if not start <= row["date"] <= end:
                raise ValueError("AKShare 日线日期超出请求范围")
            previous = result.get(row["date"])
            if previous and not _identical(previous, row):
                raise ValueError("AKShare 日线含有冲突重复日期")
            result[row["date"]] = row
        return [result[day] for day in sorted(result)]

    @staticmethod
    def _tushare_bars(symbol: str, records: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("ts_code") != symbol:
                raise ValueError("日线数据含有缺失或不匹配的标的代码")
            row = _normalise_bar(
                symbol, record, volume_scale=Decimal("100"), amount_scale=Decimal("1000"), raw_tushare=True,
            )
            if not start <= row["date"] <= end:
                raise ValueError("数据日期超出请求范围")
            old = result.get(row["date"])
            if old and not _identical(old, row):
                raise ValueError("数据含有冲突重复日期")
            result[row["date"]] = row
        return [result[key] for key in sorted(result)]

    @staticmethod
    def _calendar_rows(records: list[dict[str, Any]], start: str, end: str) -> list[tuple[str, int]]:
        result: dict[str, int] = {}
        for record in records:
            if record.get("exchange") != "SSE":
                raise ValueError("交易日历交易所缺失或不是 SSE")
            day = _safe_date(record.get("cal_date"))
            value = record.get("is_open")
            if str(value) not in {"0", "1", "False", "True"}:
                raise ValueError("交易日历字段无效")
            is_open = int(str(value) in {"1", "True"})
            if not start <= day <= end:
                raise ValueError("日历日期超出请求范围")
            if day in result:
                raise ValueError("交易日历日期重复")
            result[day] = is_open
        expected_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        expected = {
            (date.fromisoformat(start) + timedelta(days=offset)).isoformat()
            for offset in range(expected_days)
        }
        if set(result) != expected:
            raise ValueError("交易日历未完整覆盖请求区间")
        return sorted(result.items())

    @staticmethod
    def _limit_rows(symbol: str, records: list[dict[str, Any]], start: str, end: str) -> dict[str, tuple[float, float]]:
        result: dict[str, tuple[float, float]] = {}
        for record in records:
            if record.get("ts_code") != symbol:
                raise ValueError("涨跌停数据含有缺失或不匹配的标的代码")
            day = _safe_date(record.get("trade_date"))
            if not start <= day <= end:
                raise ValueError("涨跌停日期超出请求范围")
            up = _decimal(record.get("up_limit"), "涨停价")
            down = _decimal(record.get("down_limit"), "跌停价")
            if up is None or down is None or up <= 0 or down <= 0 or up < down:
                raise ValueError("供应商涨跌停价无效")
            value = (float(up), float(down))
            if day in result and result[day] != value:
                raise ValueError("涨跌停数据含有冲突重复日期")
            result[day] = value
        return result

    def _current_count(self, symbol: str) -> int:
        with self._db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM market_bars b JOIN market_current c ON b.dataset_id=c.dataset_id "
                "WHERE c.symbol=?", (symbol,),
            ).fetchone()
            return int(row[0])

    def _record_sync(self, symbol: str, status: str, warning: str | None, retry_after: int | None) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO market_sync_state(symbol,last_status,last_warning,retry_after,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET last_status=excluded.last_status,"
                "last_warning=excluded.last_warning,retry_after=excluded.retry_after,updated_at=excluded.updated_at",
                (symbol, status, warning, retry_after, self._now_iso()),
            )

    def _snapshot(
        self, symbol: str, source: str, adjustment: str, rows: list[dict[str, Any]],
        *, limits: dict[str, tuple[float, float]] | None = None, limits_source: str | None = None,
        allow_etf_limit_derivation: bool = False,
    ) -> int:
        updated = self._now_iso()
        limits = limits or {}
        with self._db() as db:
            prior = db.execute(
                "SELECT id FROM market_datasets WHERE symbol=? AND source=? AND adjustment=? ORDER BY id DESC LIMIT 1",
                (symbol, source, adjustment),
            ).fetchone()
            db.execute(
                "INSERT INTO market_datasets(symbol,source,adjustment,updated_at,mode) VALUES(?,?,?,?,?)",
                (symbol, source, adjustment, updated, MODE),
            )
            dataset_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            if prior:
                db.execute(
                    "INSERT INTO market_bars SELECT ?,date,open,high,low,close,volume,amount,pre_close,"
                    "up_limit,down_limit,limits_source,source,adjustment,mode,updated_at "
                    "FROM market_bars WHERE dataset_id=?", (dataset_id, prior["id"]),
                )
            for row in rows:
                existing = db.execute(
                    "SELECT up_limit,down_limit,limits_source FROM market_bars WHERE dataset_id=? AND date=?",
                    (dataset_id, row["date"]),
                ).fetchone()
                up, down = row.get("up_limit"), row.get("down_limit")
                row_limit_source = limits_source if up is not None else None
                if row["date"] in limits:
                    up, down = limits[row["date"]]
                    row_limit_source = "tushare_stk_limit" if source == "tushare" else limits_source
                elif (allow_etf_limit_derivation and symbol == ETF_LIMIT_RULE_SYMBOL
                      and adjustment == "none"):
                    derived = _derive_515880_limits(row.get("pre_close"))
                    if derived:
                        up, down = derived
                        row_limit_source = ETF_LIMIT_SOURCE
                if up is None and existing and existing[0] is not None:
                    up, down, row_limit_source = existing[0], existing[1], existing[2]
                db.execute(
                    "INSERT OR REPLACE INTO market_bars(dataset_id,date,open,high,low,close,volume,amount,"
                    "pre_close,up_limit,down_limit,limits_source,source,adjustment,mode,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dataset_id, row["date"], row["open"], row["high"], row["low"], row["close"],
                     row["volume"], row["amount"], row.get("pre_close"), up, down, row_limit_source,
                     source, adjustment, MODE, updated),
                )
            db.execute(
                "INSERT INTO market_current(symbol,dataset_id) VALUES(?,?) ON CONFLICT(symbol) "
                "DO UPDATE SET dataset_id=excluded.dataset_id", (symbol, dataset_id),
            )
        return len(rows)

    @staticmethod
    def _warning_for_failure(kind: str, api_name: str) -> str:
        if kind == "credentials":
            return "行情凭证未配置，已保留本地缓存"
        if kind == "backoff":
            return "数据源处于退避等待期，已保留本地缓存"
        if api_name == "daily" or api_name == "fund_daily" or api_name == "index_daily":
            return "日线请求失败，已保留本地缓存"
        if api_name == "trade_cal":
            return "交易日历请求失败，执行状态保持关闭"
        return "涨跌停数据请求失败，执行状态保持关闭"

    def sync(self, symbol: str, start: str, end: str, provider: str = "tushare") -> dict[str, Any]:
        code = self._symbol(symbol)
        try:
            first, last = _safe_date(start), _safe_date(end)
            if first > last:
                raise ValueError("开始日期不能晚于结束日期")
        except ValueError as exc:
            return {"status": "invalid", "symbol": code, "rows": self._current_count(code), "warning": str(exc)}
        if provider == "akshare":
            try:
                frame, api_name = self._request_akshare_frame(code, first, last)
                ak_rows = self._akshare_bars(code, frame, first, last)
                if not ak_rows:
                    raise _ProviderFailure("no_data")
            except _ProviderFailure as exc:
                warning = "AKShare 未返回日线，已保留本地缓存" if exc.kind == "no_data" else "AKShare 请求失败或处于退避期，已保留本地缓存"
                self._record_sync(code, "error", warning, exc.retry_after)
                return {"status": "error", "symbol": code, "rows": self._current_count(code),
                        "source": f"akshare:{_SYMBOLS[code]['kind']}", "adjustment": "none", "mode": MODE,
                        "cache_retained": True, "retry_after": exc.retry_after, "warning": warning}
            except (ValueError, TypeError, OverflowError):
                warning = "AKShare 日线校验失败，已保留本地缓存"
                self._record_sync(code, "invalid_data", warning, None)
                return {"status": "invalid_data", "symbol": code, "rows": self._current_count(code),
                        "cache_retained": True, "retry_after": None, "warning": warning}
            source = f"akshare:{api_name}"
            count = self._snapshot(code, source, "none", ak_rows, allow_etf_limit_derivation=True)
            dates = {row["date"] for row in ak_rows}
            with self._db() as db:
                known = {row[0] for row in db.execute(
                    "SELECT cal_date FROM market_calendar WHERE exchange='SSE' AND cal_date IN ("
                    + ",".join("?" for _ in dates) + ")", tuple(sorted(dates)),
                ).fetchall()} if dates else set()
            warnings = []
            if known != dates:
                warnings.append("未取得完整的本地已验证交易日历，执行状态保持关闭")
            if _SYMBOLS[code]["kind"] == "stock":
                warnings.append("AKShare 不提供已核验的逐日涨跌停价，执行状态保持关闭")
            if code == ETF_LIMIT_RULE_SYMBOL and any(row["pre_close"] is None for row in ak_rows):
                warnings.append("昨收缺失，无法推导 515880 涨跌停价")
            warning = "；".join(warnings) or None
            status = "partial" if warning else "ok"
            self._record_sync(code, status, warning, None)
            return {"status": status, "symbol": code, "rows": count, "source": source,
                    "adjustment": "none", "mode": MODE, "cache_retained": False,
                    "retry_after": None, "warning": warning, "calendar_verified": known == dates,
                    "limits_rule": ({"symbol": ETF_LIMIT_RULE_SYMBOL, "as_of": ETF_LIMIT_RULE_DATE,
                                     "source_url": ETF_LIMIT_RULE_SOURCE_URL}
                                    if code == ETF_LIMIT_RULE_SYMBOL else None)}
        if provider != "tushare":
            warning = "仅支持显式 Tushare 同步；其他来源请通过本地 CSV/Parquet 导入"
            self._record_sync(code, "unsupported", warning, None)
            return {"status": "unsupported", "symbol": code, "rows": self._current_count(code), "warning": warning}

        kind = _SYMBOLS[code]["kind"]
        daily_api = {"stock": "daily", "etf": "fund_daily", "index": "index_daily"}[kind]
        params = {"ts_code": code, "start_date": _compact_date(first), "end_date": _compact_date(last)}
        try:
            daily_records = self._request_rows(daily_api, params)
            daily_rows = self._tushare_bars(code, daily_records, first, last)
            if not daily_rows:
                raise _ProviderFailure("no_data")
        except _ProviderFailure as exc:
            warning = "该区间未返回有效日线，已保留本地缓存" if exc.kind == "no_data" else self._warning_for_failure(exc.kind, daily_api)
            status = "no_data" if exc.kind == "no_data" else "error"
            self._record_sync(code, status, warning, exc.retry_after)
            return {"status": status, "symbol": code, "rows": self._current_count(code), "source": "tushare",
                    "adjustment": "none", "mode": MODE, "cache_retained": True,
                    "retry_after": exc.retry_after, "warning": warning}
        except (ValueError, TypeError, OverflowError):
            warning = "日线数据校验失败，已保留本地缓存"
            self._record_sync(code, "invalid_data", warning, None)
            return {"status": "invalid_data", "symbol": code, "rows": self._current_count(code),
                    "source": "tushare", "adjustment": "none", "mode": MODE,
                    "cache_retained": True, "retry_after": None, "warning": warning}

        warnings: list[str] = []
        calendar_rows: list[tuple[str, int]] | None = None
        try:
            cal_records = self._request_rows("trade_cal", {
                "exchange": "SSE", "start_date": _compact_date(first), "end_date": _compact_date(last),
            })
            calendar_rows = self._calendar_rows(cal_records, first, last)
            with self._db() as db:
                verified_at = self._now_iso()
                db.executemany(
                    "INSERT INTO market_calendar(exchange,cal_date,is_open,source,verified_at) VALUES('SSE',?,?,?,?) "
                    "ON CONFLICT(exchange,cal_date) DO UPDATE SET is_open=excluded.is_open,"
                    "source=excluded.source,verified_at=excluded.verified_at",
                    [(day, opened, "tushare_trade_cal", verified_at) for day, opened in calendar_rows],
                )
        except _ProviderFailure as exc:
            warnings.append(self._warning_for_failure(exc.kind, "trade_cal"))
        except (ValueError, TypeError):
            warning = "交易日历数据校验失败，已保留本地缓存"
            self._record_sync(code, "invalid_data", warning, None)
            return {"status": "invalid_data", "symbol": code, "rows": self._current_count(code),
                    "source": "tushare", "adjustment": "none", "mode": MODE,
                    "cache_retained": True, "retry_after": None, "warning": warning,
                    "calendar_verified": False}

        provider_limits: dict[str, tuple[float, float]] = {}
        if kind == "stock":
            try:
                limit_records = self._request_rows("stk_limit", params)
                provider_limits = self._limit_rows(code, limit_records, first, last)
                if not provider_limits:
                    warnings.append("供应商未返回涨跌停价，执行状态保持关闭")
            except _ProviderFailure as exc:
                warnings.append(self._warning_for_failure(exc.kind, "stk_limit"))
            except (ValueError, TypeError):
                warnings.append("涨跌停数据校验失败，执行状态保持关闭")

        count = self._snapshot(
            code, "tushare", "none", daily_rows, limits=provider_limits,
            limits_source="tushare_stk_limit" if provider_limits else None,
            allow_etf_limit_derivation=True,
        )
        warning = "；".join(dict.fromkeys(warnings)) or None
        status = "partial" if warning else "ok"
        self._record_sync(code, status, warning, None)
        return {"status": status, "symbol": code, "rows": count, "source": "tushare",
                "adjustment": "none", "mode": MODE, "cache_retained": False,
                "retry_after": None, "warning": warning,
                "calendar_verified": calendar_rows is not None,
                "limits_verified_rows": len(provider_limits),
                "limits_rule": ({"symbol": ETF_LIMIT_RULE_SYMBOL, "as_of": ETF_LIMIT_RULE_DATE,
                                 "source_url": ETF_LIMIT_RULE_SOURCE_URL}
                                if code == ETF_LIMIT_RULE_SYMBOL else None)}

    def import_file(
        self, symbol: str, path: str | os.PathLike[str], source: str, adjustment: str = "none",
    ) -> dict[str, Any]:
        code = self._symbol(symbol)
        label = str(source).strip()
        if not re.fullmatch(r"[\w .:()\-]{1,64}", label, flags=re.UNICODE) or "://" in label:
            raise ValueError("source 可包含中英文、数字、空格、点、下划线、冒号、括号或短横线")
        adj = str(adjustment).strip().lower()
        if adj not in {"none", "qfq", "hfq"}:
            raise ValueError("adjustment 仅支持 none、qfq 或 hfq")
        raw_path = os.fspath(path)
        if "\x00" in raw_path or "://" in raw_path or raw_path.startswith(("//", "\\\\")):
            raise ValueError("仅接受本机 CSV 或 Parquet 文件路径")
        file_path = Path(raw_path).expanduser()
        if file_path.suffix.lower() not in {".csv", ".parquet"}:
            raise ValueError("仅支持 CSV 或 Parquet")
        try:
            resolved = file_path.resolve(strict=True)
            if not resolved.is_file() or resolved.stat().st_size > _MAX_IMPORT_BYTES:
                raise ValueError("导入文件不存在或超过大小限制")
            if resolved.suffix.lower() == ".csv":
                with resolved.open("r", newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                        raise ValueError("CSV 表头无效")
                    records = list(reader)
            else:
                import pandas as pd
                records = pd.read_parquet(resolved).to_dict(orient="records")
        except ValueError:
            raise
        except Exception:
            raise ValueError("导入文件读取失败") from None
        if not records or len(records) > _MAX_IMPORT_ROWS:
            raise ValueError("导入文件无数据或行数超限")
        raw_tushare = label.lower().startswith("tushare") and "vol" in records[0] and "volume" not in records[0]
        if not raw_tushare and "volume" not in records[0]:
            raise ValueError("导入文件须提供以股为单位的 volume")
        rows_by_date: dict[str, dict[str, Any]] = {}
        for record in records:
            row = _normalise_bar(
                code, record,
                volume_scale=Decimal("100") if raw_tushare else Decimal("1"),
                amount_scale=Decimal("1000") if raw_tushare else Decimal("1"),
                raw_tushare=raw_tushare,
            )
            previous = rows_by_date.get(row["date"])
            if previous and not _identical(previous, row):
                raise ValueError("导入文件含有冲突重复日期")
            rows_by_date[row["date"]] = row
        rows = [rows_by_date[day] for day in sorted(rows_by_date)]
        self._snapshot(code, label, adj, rows, limits_source=f"imported:{label}")
        warning = "复权数据仅供图表研究，不可用于执行" if adj != "none" else None
        self._record_sync(code, "imported", warning, None)
        return {"status": "ok", "symbol": code, "rows": len(rows), "source": label,
                "adjustment": adj, "mode": MODE, "updated_at": self._now_iso(), "warning": warning}

    def bars(self, symbol: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        code = self._symbol(symbol)
        first = _safe_date(start) if start is not None else None
        last = _safe_date(end) if end is not None else None
        if first and last and first > last:
            raise ValueError("开始日期不能晚于结束日期")
        where = ["c.symbol=?"]
        args: list[Any] = [code]
        if first:
            where.append("b.date>=?"); args.append(first)
        if last:
            where.append("b.date<=?"); args.append(last)
        with self._db() as db:
            dataset = db.execute(
                "SELECT d.id AS dataset_id,d.source,d.adjustment,d.updated_at,d.mode FROM market_current c "
                "JOIN market_datasets d ON d.id=c.dataset_id WHERE c.symbol=?", (code,),
            ).fetchone()
            rows = db.execute(
                "SELECT b.* FROM market_bars b JOIN market_current c ON c.dataset_id=b.dataset_id WHERE "
                + " AND ".join(where) + " ORDER BY b.date", args,
            ).fetchall()
            sync = db.execute("SELECT last_warning FROM market_sync_state WHERE symbol=?", (code,)).fetchone()
        return {
            "symbol": code, "name": _SYMBOLS[code]["name"],
            "rows": [{"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
                      "close": r["close"], "volume": r["volume"], "amount": r["amount"],
                      "pre_close": r["pre_close"], "up_limit": r["up_limit"], "down_limit": r["down_limit"],
                      "limits_source": r["limits_source"], "limits_verified": self._verified_limits(r),
                      "source": r["source"], "adjustment": r["adjustment"],
                      "mode": r["mode"], "updated_at": r["updated_at"]}
                     for r in rows],
            "source": dataset["source"] if dataset else None,
            "dataset_id": dataset["dataset_id"] if dataset else None,
            "adjustment": dataset["adjustment"] if dataset else None,
            "updated_at": dataset["updated_at"] if dataset else None,
            "mode": dataset["mode"] if dataset else MODE,
            "limits_rule": ({"symbol": ETF_LIMIT_RULE_SYMBOL, "as_of": ETF_LIMIT_RULE_DATE,
                             "source_url": ETF_LIMIT_RULE_SOURCE_URL}
                            if code == ETF_LIMIT_RULE_SYMBOL else None),
            "warning": sync["last_warning"] if sync else None,
        }

    @staticmethod
    def _verified_limits(row: sqlite3.Row) -> bool:
        source = row["limits_source"]
        return source == "tushare_stk_limit" or source == ETF_LIMIT_SOURCE

    def calendar(self, start: str, end: str) -> list[str]:
        first, last = _safe_date(start), _safe_date(end)
        if first > last:
            raise ValueError("开始日期不能晚于结束日期")
        with self._db() as db:
            rows = db.execute(
                "SELECT cal_date FROM market_calendar WHERE exchange='SSE' AND is_open=1 "
                "AND cal_date>=? AND cal_date<=? ORDER BY cal_date", (first, last),
            ).fetchall()
        return [row[0] for row in rows]

    def execution_bar(self, symbol: str, day: str) -> dict[str, Any]:
        code = self._symbol(symbol)
        iso_day = _safe_date(day)
        dataset = self.bars(code, iso_day, iso_day)
        row = dataset["rows"][0] if dataset["rows"] else None
        with self._db() as db:
            cal = db.execute(
                "SELECT is_open FROM market_calendar WHERE exchange='SSE' AND cal_date=?", (iso_day,),
            ).fetchone()
            prev_cal = db.execute(
                "SELECT cal_date FROM market_calendar WHERE exchange='SSE' AND cal_date<? AND is_open=1 "
                "ORDER BY cal_date DESC LIMIT 1", (iso_day,),
            ).fetchone()
            prev_bar = None
            if prev_cal:
                prev_bar = db.execute(
                    "SELECT b.close FROM market_bars b JOIN market_current c ON b.dataset_id=c.dataset_id "
                    "WHERE c.symbol=? AND b.date=?", (code, prev_cal[0]),
                ).fetchone()
        is_calendar_verified = cal is not None
        is_open = bool(cal[0]) if cal else False
        suspended = bool(row and row["volume"] == 0)
        limits_verified = bool(row and row["limits_verified"])
        warning = dataset["warning"]
        price_limits_consistent: bool | None = None
        if row and limits_verified:
            lower = Decimal(str(row["down_limit"]))
            upper = Decimal(str(row["up_limit"]))
            half_tick = Decimal(str(_SYMBOLS[code]["tick"])) / Decimal("2")
            price_limits_consistent = all(
                lower - half_tick <= Decimal(str(row[key])) <= upper + half_tick
                for key in ("open", "high", "low", "close")
            )
            if not price_limits_consistent:
                warning = "日线 OHLC 超出涨跌停范围，数据不可执行"
        corp_verified = False
        if row and row.get("pre_close") is not None and prev_bar:
            tolerance = float(_SYMBOLS[code]["tick"]) / 2
            corp_verified = abs(float(row["pre_close"]) - float(prev_bar[0])) <= tolerance
            if not corp_verified:
                corporate_warning = "昨收与前一交易日收盘价不一致，可能涉及除权除息，尚未核验"
                warning = f"{warning}；{corporate_warning}" if warning else corporate_warning
        elif row:
            continuity_warning = "前一交易日收盘价或昨收缺失，复权连续性未核验"
            warning = f"{warning}；{continuity_warning}" if warning else continuity_warning
        status = "ready"
        if row is None:
            status = "data_missing"
        elif dataset["adjustment"] != "none":
            status = "adjusted_history_not_executable"
        elif not is_calendar_verified:
            status = "calendar_unverified"
        elif not is_open:
            status = "market_closed"
        elif _SYMBOLS[code]["kind"] == "index":
            status = "index_not_tradable"
        elif suspended:
            status = "suspended"
        elif not limits_verified:
            status = "limits_unverified"
        elif price_limits_consistent is False:
            status = "invalid_price_limits"
        elif not corp_verified:
            status = "corporate_action_unverified"
        result = dict(row) if row else {"symbol": code, "date": iso_day}
        result.update({
            "symbol": code, "name": _SYMBOLS[code]["name"], "is_open": is_open,
            "calendar_verified": is_calendar_verified, "limits_verified": limits_verified,
            "price_limits_consistent": price_limits_consistent,
            "corporate_action_verified": corp_verified, "suspended": suspended,
            "execution_allowed": status == "ready", "status": status, "warning": warning,
            "mode": MODE,
            "limits_rule": ({"symbol": ETF_LIMIT_RULE_SYMBOL, "as_of": ETF_LIMIT_RULE_DATE,
                             "source_url": ETF_LIMIT_RULE_SOURCE_URL}
                            if code == ETF_LIMIT_RULE_SYMBOL else None),
        })
        return result

