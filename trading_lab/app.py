"""Same-origin FastAPI application for the local, research-only Trading Lab."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .journal import JournalStore
from .ledger import (
    AlreadyAdvancedError,
    IdempotencyConflict,
    Ledger,
    LedgerError,
    NotFoundError,
    RiskLimitError,
)
from .market import MarketService


APP_NAME = "tradinglab"
APP_DIR = Path.home() / "Library" / "Application Support" / "TradingLab"
PRIVATE_ENV = APP_DIR / ".env"
PROFILE_PATH = APP_DIR / "profile.json"
DEFAULT_DB_PATH = APP_DIR / "trading_lab.sqlite3"
FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
PRIVATE_ENV_KEYS = {
    "TUSHARE_HTTP_URL", "TUSHARE_TOKEN", "TUSHARE_BACKUP_TOKEN",
    "TUSHARE_PRIVILEGED_APIS", "TRADING_LAB_TRUST_ENV",
}
MAX_JSON_BODY = 1 * 1024 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_BACKUP_BYTES = 64 * 1024 * 1024
TRADE_SYMBOLS = {"603283.SH", "515880.SH"}
ALL_SYMBOLS = {"603283.SH", "515880.SH", "000300.SH"}
ADVANCE_SYMBOLS = ("603283.SH", "515880.SH")
BACKUP_TABLES = (
    "accounts", "orders", "cycles", "fills", "lots", "marks", "equity_curve", "idempotency",
    "market_datasets", "market_current", "market_bars", "market_calendar",
    "market_provider_state", "market_request_clock", "market_sync_state",
    "lab_account_market", "lab_reviews", "lab_review_history", "lab_plans",
    "lab_plan_history", "lab_strategy", "lab_observations", "lab_profile_import_state",
    "lab_account_creates",
)
OPTIONAL_BACKUP_TABLES = {"lab_account_creates"}
INSERT_ORDER = (
    "accounts", "market_datasets", "market_calendar", "market_provider_state",
    "market_request_clock", "market_sync_state", "market_bars", "market_current",
    "lab_reviews", "lab_review_history", "lab_plans", "lab_plan_history", "lab_strategy",
    "lab_observations", "lab_profile_import_state", "lab_account_creates", "lab_account_market",
    "orders", "cycles", "fills", "lots", "marks", "equity_curve", "idempotency",
)
LEDGER_NUMERIC_KEYS = {
    "initial_cash", "cash", "equity", "market_value", "realized_pnl", "unrealized_pnl",
    "drawdown", "quantity", "sellable", "next_session_sellable", "avg_cost", "mark",
    "filled_quantity", "remaining_quantity", "limit_price", "stop_price", "price", "fill_price",
    "notional", "commission", "transfer_fee", "stamp_duty", "fees", "total_fees",
    "realized_pnl", "net_pnl", "r_multiple", "initial_risk", "total_buy_quantity",
    "total_sell_quantity", "buy_notional", "sell_notional", "buy_fees", "sell_fees",
    "holding_days", "market_value", "avg_win", "avg_loss", "payoff_ratio", "profit_factor",
    "avg_r", "cumulative_net", "max_drawdown", "max_drawdown_amount", "avg_holding_days",
    "win_rate", "commission_rate", "minimum_commission", "stock_stamp_duty_rate",
    "stock_transfer_fee_rate", "etf_stamp_duty_rate", "etf_transfer_fee_rate",
    "participation_rate", "risk_pct", "planned_risk", "post_cash", "post_equity",
    "reserved_cash", "available_cash",
    "closed_cycle_net", "month_end_equity", "equity_change", "return",
}
LEDGER_INTEGER_KEYS = {
    "quantity", "sellable", "next_session_sellable", "filled_quantity", "remaining_quantity",
    "total_buy_quantity", "total_sell_quantity", "holding_days", "count", "wins", "total_fills",
    "total_closed", "totalfills", "totalclosed", "current_length", "max_win", "max_loss",
}


def load_private_env(path: str | os.PathLike[str] = PRIVATE_ENV) -> None:
    """Load only the approved key=value entries, preserving process env precedence."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in PRIVATE_ENV_KEYS or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError("日期格式应为 YYYY-MM-DD") from None


def _json_numbers(value: Any, key: str | None = None) -> Any:
    """Convert only known numeric fields from Ledger payloads; IDs/text stay intact."""
    if isinstance(value, dict):
        return {name: _json_numbers(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item, key) for item in value]
    if key not in LEDGER_NUMERIC_KEYS or not isinstance(value, str):
        return value
    try:
        number = Decimal(value)
        if not number.is_finite():
            return None
        if key in LEDGER_INTEGER_KEYS and number == number.to_integral_value():
            return int(number)
        return float(number)
    except (InvalidOperation, ValueError):
        return value


def _account_payload(ledger: Ledger, account_id: int) -> dict[str, Any]:
    payload = _json_numbers(ledger.account(account_id))
    for fill in payload.get("fills", []):
        snapshot = fill.get("plan_snapshot")
        if isinstance(snapshot, dict):
            fill["plan_id"] = snapshot.get("plan_id")
    for order in payload.get("orders", []):
        order["created_at"] = order.get("created_date")
    return payload


def _accounts_payload(ledger: Ledger) -> list[dict[str, Any]]:
    return [_json_numbers(item) for item in ledger.accounts()]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _profile_observations(path: Path | None, symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    names = {item["symbol"]: item["name"] for item in symbols}
    result = []
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        return result
    for item in payload["observations"]:
        if not isinstance(item, dict) or item.get("symbol") not in names:
            continue
        quantity = _as_float(item.get("quantity"))
        cost = _as_float(item.get("reference_cost"))
        if quantity is None or quantity <= 0 or cost is None or cost < 0:
            continue
        result.append({
            "symbol": item["symbol"], "name": names[item["symbol"]],
            "quantity": quantity, "reference_cost": cost,
            "note": item.get("note", "") if isinstance(item.get("note", ""), str) else "",
        })
    return result


def _ledger_error_message(exc: LedgerError) -> tuple[int, str]:
    if isinstance(exc, NotFoundError):
        return 404, "账户、委托或计划不存在。"
    if isinstance(exc, IdempotencyConflict):
        return 409, "请求编号已用于不同操作，请重新提交。"
    if isinstance(exc, AlreadyAdvancedError):
        return 409, "账户无法重复推进到已处理的日期。"
    if isinstance(exc, RiskLimitError):
        return 422, "委托超过训练风险或资金约束，请检查数量、限价和止损价。"
    message = str(exc).casefold()
    if "t+1" in message:
        return 422, "卖出数量超过当前可卖数量，受 T+1 规则限制。"
    if "tick" in message or "price" in message or "limit" in message:
        return 422, "价格或涨跌停条件不符合该标的训练规则。"
    if "risk" in message or "cash" in message or "budget" in message:
        return 422, "委托超过训练风险或可用资金约束。"
    if "already" in message or "advance date" in message:
        return 409, "账户日期不能重复或向后跳过交易日。"
    if "symbol" in message or "side" in message or "quantity" in message:
        return 422, "标的、方向或数量不符合模拟规则。"
    return 422, "账本拒绝了这项操作，请检查账户状态和输入。"


def _safe_value_error(exc: ValueError) -> str:
    message = str(exc)
    if any("\u4e00" <= char <= "\u9fff" for char in message):
        return message[:240]
    return "请求内容无效，请检查后重试。"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class AccountCreate(InputModel):
    name: str = Field(min_length=1, max_length=80)
    initial_cash: float = Field(gt=0, le=1_000_000_000_000)
    start_date: str
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    commission_rate: float | None = Field(default=None, ge=0, le=1)
    minimum_commission: float | None = Field(default=None, ge=0, le=1_000_000)
    stock_stamp_duty_rate: float | None = Field(default=None, ge=0, le=1)
    stock_transfer_fee_rate: float | None = Field(default=None, ge=0, le=1)

    @field_validator("start_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        return _date(value)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("账户名称不能为空")
        return value.strip()

    @field_validator("request_id")
    @classmethod
    def nonblank_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("请求编号不能为空")
        return value.strip()


class OrderInput(InputModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=100_000_000)
    limit_price: float = Field(gt=0, le=1_000_000)
    stop_price: float | None = Field(default=None, gt=0, le=1_000_000)
    risk_pct: float = Field(default=1, gt=0, le=100)
    plan_id: int | str | None = None
    request_id: str = Field(min_length=1, max_length=128)


class AdvanceInput(InputModel):
    request_id: str = Field(min_length=1, max_length=128)


class MarketSyncInput(InputModel):
    symbol: str
    start: str
    end: str
    provider: Literal["tushare", "akshare"]

    @field_validator("start", "end")
    @classmethod
    def valid_date(cls, value: str) -> str:
        return _date(value)

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class ReviewInput(InputModel):
    answers: list[str] = Field(min_length=7, max_length=7)
    tags: list[str] = Field(default_factory=list, max_length=20)
    completed: bool

    @field_validator("answers")
    @classmethod
    def answer_bounds(cls, value: list[str]) -> list[str]:
        if any(len(item) > 4000 for item in value):
            raise ValueError("单条复盘答案不能超过 4000 字符")
        return value

    @field_validator("tags")
    @classmethod
    def tag_bounds(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 48 for item in value):
            raise ValueError("复盘标签格式无效")
        return list(dict.fromkeys(item.strip() for item in value))


class PlanInput(InputModel):
    symbol: str
    strategy: str = Field(default="", max_length=128)
    market_trend: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=4000)
    entry_condition: str = Field(default="", max_length=4000)
    entry_price: float | None = Field(default=None, gt=0, le=1_000_000)
    stop_price: float | None = Field(default=None, gt=0, le=1_000_000)
    target_price: float | None = Field(default=None, gt=0, le=1_000_000)
    quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    discipline: str = Field(default="", max_length=4000)
    status: Literal["planned", "active", "completed", "canceled", "cancelled"] = "planned"

    @field_validator("symbol")
    @classmethod
    def supported_symbol(cls, value: str) -> str:
        if value not in ALL_SYMBOLS:
            raise ValueError("不支持的标的代码")
        return value

    @field_validator("reason", "entry_condition")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("计划理由和入场条件不能为空")
        return value.strip()

    @field_validator("quantity")
    @classmethod
    def whole_quantity(cls, value: int | None) -> int | None:
        if value is not None and value % 100:
            raise ValueError("计划数量须为 100 股或份的整数倍")
        return value


class RestoreInput(InputModel):
    backup: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _sqlite(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_app_schema(db_path: str | Path) -> None:
    conn = _sqlite(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lab_account_market (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
                created_at TEXT NOT NULL, start_date TEXT NOT NULL, max_date TEXT NOT NULL,
                checksum TEXT NOT NULL, snapshot_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lab_account_creates (
                request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
                created_at TEXT NOT NULL
            )
        """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lab_observations (
                symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL,
                reference_cost TEXT NOT NULL, note TEXT NOT NULL,
                source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lab_profile_import_state (
                id INTEGER PRIMARY KEY CHECK(id=1), seeded_at TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _seed_profile_observations(
    db_path: str | Path, profile_path: Path | None, symbols: list[dict[str, Any]],
) -> None:
    if profile_path is None:
        return
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(profile, dict) or not isinstance(profile.get("observations"), list):
        return
    observations = _profile_observations(profile_path, symbols)
    conn = _sqlite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        seeded = conn.execute("SELECT 1 FROM lab_profile_import_state WHERE id=1").fetchone()
        if seeded is None:
            now = _now_iso()
            for item in observations:
                conn.execute(
                    "INSERT OR IGNORE INTO lab_observations(symbol,quantity,reference_cost,note,source,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item["symbol"], str(item["quantity"]), str(item["reference_cost"]),
                     item["note"], "private_profile_seed", now),
                )
            conn.execute("INSERT INTO lab_profile_import_state(id,seeded_at) VALUES(1,?)", (now,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _db_observations(db_path: str | Path, symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {item["symbol"]: item["name"] for item in symbols}
    conn = _sqlite(db_path)
    try:
        rows = conn.execute(
            "SELECT symbol,quantity,reference_cost,note FROM lab_observations ORDER BY symbol"
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        if row["symbol"] not in names:
            continue
        quantity = _as_float(row["quantity"])
        cost = _as_float(row["reference_cost"])
        if quantity is None or cost is None:
            continue
        result.append({"symbol": row["symbol"], "name": names[row["symbol"]],
                       "quantity": quantity, "reference_cost": cost, "note": row["note"]})
    return result


def _read_calendar(db_path: str | Path, start: str, end: str) -> list[dict[str, Any]]:
    conn = _sqlite(db_path)
    try:
        rows = conn.execute(
            "SELECT cal_date,is_open,source,verified_at FROM market_calendar "
            "WHERE exchange='SSE' AND cal_date>=? AND cal_date<=? ORDER BY cal_date",
            (start, end),
        ).fetchall()
        return [{"date": row["cal_date"], "is_open": bool(row["is_open"]),
                 "source": row["source"], "verified_at": row["verified_at"]} for row in rows]
    finally:
        conn.close()


def _freeze_market(market: MarketService, db_path: str | Path, start: str) -> dict[str, Any]:
    all_bars: dict[str, dict[str, Any]] = {}
    date_sets: list[set[str]] = []
    for symbol in ("603283.SH", "515880.SH", "000300.SH"):
        data = market.bars(symbol)
        if data["adjustment"] != "none" or not data["rows"]:
            raise ValueError("创建账户需要三项标的均有原始日线缓存")
        all_bars[symbol] = data
        date_sets.append({row["date"] for row in data["rows"]})
    common_dates = set.intersection(*date_sets)
    if not common_dates:
        raise ValueError("三项标的没有共同的可用交易日")
    calendar_all = _read_calendar(db_path, start, max(common_dates))
    calendar_by_date = {item["date"]: item for item in calendar_all}
    if start not in calendar_by_date or not calendar_by_date[start]["is_open"]:
        raise ValueError("起始日必须是已验证的开市交易日")
    common_open = sorted(day for day in common_dates if day in calendar_by_date and calendar_by_date[day]["is_open"])
    if start not in common_open:
        raise ValueError("起始日缺少三项标的的目标日线")
    future_common = [day for day in common_open if day >= start]
    max_date = future_common[-1]
    if max_date <= start:
        raise ValueError("所选起始日之后没有可推进的历史日线")
    calendar_rows = _read_calendar(db_path, start, max_date)
    expected_days = (date.fromisoformat(max_date) - date.fromisoformat(start)).days + 1
    if len(calendar_rows) != expected_days or [r["date"] for r in calendar_rows] != [
        (date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(expected_days)
    ]:
        raise ValueError("起始日至缓存末日之间的交易日历覆盖不完整")
    if any(not row.get("source") or not row.get("verified_at") for row in calendar_rows):
        raise ValueError("交易日历来源未通过核验")

    frozen_bars: dict[str, dict[str, Any]] = {}
    execution: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, data in all_bars.items():
        selected = [dict(row) for row in data["rows"] if row["date"] <= max_date]
        frozen = {key: value for key, value in data.items() if key != "rows"}
        frozen["rows"] = selected
        frozen_bars[symbol] = frozen
        rows_by_day = {row["date"]: row for row in selected}
        execution[symbol] = {}
        open_dates = [row["date"] for row in calendar_rows if row["is_open"]]
        for calendar_row in calendar_rows:
            if not calendar_row["is_open"]:
                continue
            day = calendar_row["date"]
            bar = market.execution_bar(symbol, day)
            prior_sessions = [item for item in open_dates if item < day]
            bar["previous_session_date"] = prior_sessions[-1] if prior_sessions else None
            if symbol in ADVANCE_SYMBOLS:
                prior = [row for row in data["rows"] if row["date"] < day]
                prior_row = prior[-1] if prior else None
                bar["capacity_date"] = prior_row["date"] if prior_row else None
                bar["capacity_volume"] = prior_row["volume"] if prior_row else 0
            execution[symbol][day] = bar
    return {
        "version": 1, "created_at": _now_iso(), "start_date": start,
        "max_date": max_date, "bars": frozen_bars,
        "execution_bars": execution, "calendar_days": calendar_rows,
    }


def _snapshot_for_account(db_path: str | Path, account_id: int) -> dict[str, Any] | None:
    conn = _sqlite(db_path)
    try:
        row = conn.execute(
            "SELECT snapshot_json,checksum FROM lab_account_market WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        raw = row["snapshot_json"]
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["checksum"]:
            raise ValueError("账户行情快照校验失败")
        return json.loads(raw)
    finally:
        conn.close()


def _store_account_creation(
    conn: sqlite3.Connection,
    account_id: int,
    snapshot: dict[str, Any],
    request_id: str | None,
    fingerprint: str,
) -> None:
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO lab_account_market(account_id,created_at,start_date,max_date,checksum,snapshot_json) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, snapshot["created_at"], snapshot["start_date"], snapshot["max_date"], digest, raw),
    )
    if request_id is not None:
        conn.execute(
            "INSERT INTO lab_account_creates(request_id,fingerprint,account_id,created_at) "
            "VALUES(?,?,?,?)",
            (request_id, fingerprint, account_id, _now_iso()),
        )


def _account_create_fingerprint(body: AccountCreate) -> str:
    payload = body.model_dump(mode="json", exclude={"request_id"}, exclude_none=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _account_create_request(db_path: str | Path, request_id: str) -> dict[str, Any] | None:
    conn = _sqlite(db_path)
    try:
        row = conn.execute(
            "SELECT fingerprint,account_id FROM lab_account_creates WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _clip_market(snapshot: dict[str, Any], symbol: str, end: str) -> dict[str, Any]:
    payload = snapshot["bars"].get(symbol)
    if payload is None:
        raise HTTPException(status_code=404, detail="该账户未冻结此标的的行情。")
    result = deepcopy(payload)
    result["rows"] = [row for row in result["rows"] if row["date"] <= end]
    result["warning"] = result.get("warning")
    return result


def _incomplete_error() -> str:
    return "账户冻结行情缺少下一已验证交易日，进度保持不变。"


def _to_ledger_float(payload: dict[str, Any]) -> dict[str, Any]:
    return _json_numbers(payload)


def _same_numeric(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def _insert_history_after_fills(journal: JournalStore, account: dict[str, Any], fills: list[dict[str, Any]]) -> None:
    affected: set[int] = set()
    for fill in fills:
        snapshot = fill.get("plan_snapshot")
        raw_id = snapshot.get("plan_id") if isinstance(snapshot, dict) else None
        if raw_id is not None:
            try:
                affected.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        cycle_id = fill.get("cycle_id")
        for cycle in account.get("cycles", []):
            if cycle.get("id") == cycle_id and cycle.get("plan_id") is not None:
                try:
                    affected.add(int(cycle["plan_id"]))
                except (TypeError, ValueError):
                    pass
    for plan_id in affected:
        plan = journal.plan(plan_id)
        if plan is None:
            continue
        related = [cycle for cycle in account.get("cycles", []) if str(cycle.get("plan_id")) == str(plan_id)]
        if any(cycle.get("status") == "open" for cycle in related):
            status = "active"
        elif any(cycle.get("status") == "closed" for cycle in related):
            status = "completed"
        else:
            continue
        if plan.get("status") != status:
            journal.set_plan_status(plan_id, status, "实际模拟成交后自动更新；不代表计划纪律已验证")


def _csv_download(account: dict[str, Any], account_id: int) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["fill_id", "order_id", "cycle_id", "plan_id", "date", "symbol", "side",
                     "quantity", "price", "notional", "fees", "realized_pnl"])
    for fill in account.get("fills", []):
        snapshot = fill.get("plan_snapshot") if isinstance(fill.get("plan_snapshot"), dict) else {}
        writer.writerow([
            fill.get("id"), fill.get("order_id"), fill.get("cycle_id"),
            fill.get("plan_id", snapshot.get("plan_id")), fill.get("date"), fill.get("symbol"),
            fill.get("side"), fill.get("quantity"), fill.get("price"), fill.get("notional"),
            fill.get("fees"), fill.get("realized_pnl"),
        ])
    filename = f"trading-lab-account-{account_id}-fills.csv"
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def create_app(
    db_path: str | os.PathLike[str] | None = None,
    profile_path: str | os.PathLike[str] | None = None,
) -> FastAPI:
    explicit_db = db_path is not None
    if explicit_db:
        resolved_db = Path(db_path).expanduser()
        resolved_profile = Path(profile_path).expanduser() if profile_path is not None else None
    else:
        load_private_env()
        resolved_db = Path(os.environ.get("TRADING_LAB_DB", str(DEFAULT_DB_PATH))).expanduser()
        resolved_profile = Path(profile_path).expanduser() if profile_path is not None else PROFILE_PATH
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(resolved_db)
    market = MarketService(resolved_db)
    journal = JournalStore(resolved_db)
    _ensure_app_schema(resolved_db)
    _seed_profile_observations(resolved_db, resolved_profile, market.symbols())

    app = FastAPI(title="Trading Lab", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.db_path = resolved_db
    app.state.ledger = ledger
    app.state.market = market
    app.state.journal = journal
    app.state.mutation_lock = __import__("threading").RLock()
    app.state.profile_path = resolved_profile

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            host = request.headers.get("host", "").lower()
            origin = request.headers.get("origin", "")
            parsed = urlsplit(origin)
            if (not host or not origin or parsed.scheme != request.url.scheme
                    or parsed.netloc.lower() != host or parsed.path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                return JSONResponse(status_code=403, content={"detail": "本地请求来源校验失败。"})
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return JSONResponse(status_code=403, content={"detail": "仅允许本机页面提交。"})
            raw_length = request.headers.get("content-length")
            no_body_route = request.method == "DELETE" or request.url.path.endswith("/cancel")
            if raw_length is None and not no_body_route:
                return JSONResponse(status_code=411, content={"detail": "请求体长度无效。"})
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "请求体长度无效。"})
                limit = MAX_UPLOAD_BYTES + 1024 * 1024 if request.url.path == "/api/market/import" else (
                    MAX_BACKUP_BYTES + 1024 * 1024 if request.url.path == "/api/restore" else MAX_JSON_BODY
                )
                if length < 0 or length > limit:
                    return JSONResponse(status_code=413, content={"detail": "请求内容超过大小限制。"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "提交内容格式不正确，请检查后重试。"})

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "请求无法完成。"
        if not any("\u4e00" <= char <= "\u9fff" for char in detail):
            detail = "请求资源不存在或不可用。" if exc.status_code == 404 else "请求无法完成，请检查后重试。"
        return JSONResponse(status_code=exc.status_code, content={"detail": detail[:240]})

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(request: Request, exc: LedgerError):
        status, message = _ledger_error_message(exc)
        return JSONResponse(status_code=status, content={"detail": message})

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": _safe_value_error(exc)})

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": "本地服务暂时无法完成请求，请稍后重试。"})

    @app.get("/api/health")
    async def health():
        return {"service": APP_NAME, "status": "ok", "mode": "RESEARCH_ONLY"}

    @app.get("/api/bootstrap")
    async def bootstrap():
        symbols = market.symbols()
        return {
            "symbols": symbols,
            "observations": _db_observations(resolved_db, symbols),
            "accounts": _accounts_payload(ledger),
            "reviews": journal.reviews(),
            "plans": journal.plans(),
            "strategy": journal.strategy(),
            "today": _today(),
        }

    @app.get("/api/market/{symbol}")
    async def get_market(
        symbol: str,
        end: str | None = None,
        account_id: int | None = None,
    ):
        day = _date(end) if end else (_today() if account_id is None else None)
        if account_id is None:
            return market.bars(symbol, end=day)
        account = ledger.account(account_id)
        frozen = _snapshot_for_account(resolved_db, account_id)
        if frozen is None:
            raise HTTPException(status_code=404, detail="该账户没有已冻结的行情快照。")
        clipped_end = min(day or account["cursor_date"], account["cursor_date"])
        return _clip_market(frozen, symbol, clipped_end)

    @app.post("/api/market/sync")
    def sync_market(body: MarketSyncInput):
        with app.state.mutation_lock:
            summary = market.sync(body.symbol, body.start, body.end, provider=body.provider)
            payload = market.bars(body.symbol, body.start, body.end)
        payload["sync_status"] = summary
        if summary.get("warning"):
            payload["warning"] = summary["warning"]
        return payload

    @app.post("/api/market/import")
    def import_market(
        file: UploadFile = File(...),
        symbol: str = Form(...),
        source: str = Form(...),
        adjustment: str = Form("none"),
    ):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".csv", ".parquet"}:
            raise HTTPException(status_code=422, detail="仅支持 CSV 或 Parquet 文件。")
        temp_path = None
        size = 0
        try:
            with tempfile.NamedTemporaryFile(prefix="trading-lab-import-", suffix=suffix, delete=False) as temp:
                temp_path = Path(temp.name)
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="导入文件超过 64 MiB 限制。")
                    temp.write(chunk)
            with app.state.mutation_lock:
                result = market.import_file(symbol, temp_path, source, adjustment)
                payload = market.bars(symbol)
            payload["import_status"] = result
            if result.get("warning"):
                payload["warning"] = result["warning"]
            return payload
        finally:
            file.file.close()
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @app.get("/api/accounts")
    async def list_accounts():
        return _accounts_payload(ledger)

    @app.post("/api/accounts")
    def create_account(body: AccountCreate):
        start = body.start_date
        if start > _today():
            raise HTTPException(status_code=422, detail="开始日期不能晚于今天。")

        def existing_response(account_id: int) -> dict[str, Any]:
            result = _account_payload(ledger, account_id)
            frozen = _snapshot_for_account(resolved_db, account_id)
            if frozen is None:
                raise HTTPException(status_code=500, detail="账户创建记录缺少冻结行情快照。")
            result["market_snapshot"] = {
                "start_date": frozen["start_date"], "max_date": frozen["max_date"],
            }
            return result

        with app.state.mutation_lock:
            fingerprint = _account_create_fingerprint(body)
            if body.request_id is not None:
                previous = _account_create_request(resolved_db, body.request_id)
                if previous is not None:
                    if previous["fingerprint"] != fingerprint:
                        raise HTTPException(status_code=409, detail="请求编号已用于不同的账户创建内容。")
                    return existing_response(int(previous["account_id"]))
            snapshot = _freeze_market(market, resolved_db, start)
            fees = {key: getattr(body, key) for key in (
                "commission_rate", "minimum_commission", "stock_stamp_duty_rate", "stock_transfer_fee_rate",
            ) if getattr(body, key) is not None}
            try:
                created = ledger.create_account(
                    name=body.name, initial_cash=body.initial_cash, start_date=start,
                    mode="historical", fees=fees or None,
                    on_created=lambda conn, account_id: _store_account_creation(
                        conn, account_id, snapshot, body.request_id, fingerprint,
                    ),
                )
            except sqlite3.IntegrityError:
                # A second service process may have committed this request after our read.
                if body.request_id is None:
                    raise
                previous = _account_create_request(resolved_db, body.request_id)
                if previous is None:
                    raise
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(status_code=409, detail="请求编号已用于不同的账户创建内容。") from None
                return existing_response(int(previous["account_id"]))
            account_id = int(created["id"])
            result = _account_payload(ledger, account_id)
            result["market_snapshot"] = {"start_date": snapshot["start_date"], "max_date": snapshot["max_date"]}
            return result

    @app.get("/api/accounts/{account_id}")
    async def account_detail(account_id: int):
        return _account_payload(ledger, account_id)

    @app.post("/api/accounts/{account_id}/orders")
    async def place_order(account_id: int, body: OrderInput):
        with app.state.mutation_lock:
            ledger.account(account_id)
            conn = _sqlite(resolved_db)
            try:
                previous = conn.execute(
                    "SELECT symbol,side,quantity,limit_price,stop_price,plan_id,plan_snapshot_json "
                    "FROM orders WHERE account_id=? AND request_id=?",
                    (account_id, body.request_id),
                ).fetchone()
            finally:
                conn.close()
            if previous is not None:
                saved = json.loads(previous["plan_snapshot_json"])
                submitted_plan_id = str(body.plan_id) if body.plan_id is not None else None
                saved_risk = saved.get("risk_pct", "1")
                same_request = (
                    previous["symbol"] == body.symbol and previous["side"] == body.side
                    and int(previous["quantity"]) == body.quantity
                    and _same_numeric(previous["limit_price"], body.limit_price)
                    and _same_numeric(previous["stop_price"], body.stop_price)
                    and (str(previous["plan_id"]) if previous["plan_id"] is not None else None) == submitted_plan_id
                    and _same_numeric(saved_risk, body.risk_pct)
                )
                if not same_request:
                    raise HTTPException(status_code=409, detail="请求编号已用于不同的委托内容。")
                return _account_payload(ledger, account_id)
            plan_snapshot = None
            plan_id = None
            if body.plan_id is not None:
                try:
                    plan_id = int(body.plan_id)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422, detail="计划编号无效。") from None
                plan = journal.plan(plan_id)
                if plan is None:
                    raise HTTPException(status_code=404, detail="关联的计划不存在。")
                if plan["symbol"] != body.symbol:
                    raise HTTPException(status_code=422, detail="委托标的与关联计划不一致。")
                strategy = journal.strategy()
                plan_snapshot = {
                    "plan_revision": plan["revision"], "plan": plan,
                    "strategy": plan.get("strategy", ""),
                    "strategy_version": strategy.get("version", "1.0"),
                    "planned_discipline": plan.get("discipline", ""),
                    "discipline": "unknown", "actual_plan_adherence": "unknown",
                }
            ledger.order(
                account_id=account_id, symbol=body.symbol, side=body.side,
                quantity=body.quantity, limit_price=body.limit_price, stop_price=body.stop_price,
                plan_id=str(plan_id) if plan_id is not None else None,
                request_id=body.request_id, risk_pct=body.risk_pct, plan_snapshot=plan_snapshot,
            )
            return _account_payload(ledger, account_id)

    @app.post("/api/accounts/{account_id}/orders/{order_id}/cancel")
    async def cancel_order(account_id: int, order_id: int):
        with app.state.mutation_lock:
            ledger.cancel(account_id, order_id)
            return _account_payload(ledger, account_id)

    @app.post("/api/accounts/{account_id}/advance")
    async def advance_account(account_id: int, body: AdvanceInput):
        with app.state.mutation_lock:
            ledger.account(account_id)
            conn = _sqlite(resolved_db)
            try:
                prior = conn.execute(
                    "SELECT result_json FROM idempotency WHERE account_id=? AND method='advance' AND request_id=?",
                    (account_id, body.request_id),
                ).fetchone()
            finally:
                conn.close()
            if prior is not None:
                return _account_payload(ledger, account_id)
            frozen = _snapshot_for_account(resolved_db, account_id)
            if frozen is None:
                raise HTTPException(status_code=422, detail="账户没有可推进的冻结行情。")
            account = ledger.account(account_id)
            cursor = account["cursor_date"]
            if cursor < frozen["start_date"] or cursor >= frozen["max_date"]:
                raise HTTPException(status_code=422, detail="账户已到冻结历史数据末日，无法继续推进。")
            days = frozen["calendar_days"]
            calendar_map = {row["date"]: row for row in days}
            expected = (date.fromisoformat(frozen["max_date"]) - date.fromisoformat(frozen["start_date"])).days + 1
            if len(days) != expected:
                raise HTTPException(status_code=422, detail="冻结交易日历覆盖不完整，账户未推进。")
            next_day = next((row["date"] for row in days if row["date"] > cursor and row["is_open"]), None)
            if next_day is None or next_day not in calendar_map:
                raise HTTPException(status_code=422, detail=_incomplete_error())
            bars: dict[str, dict[str, Any]] = {}
            for symbol in ADVANCE_SYMBOLS:
                bar = frozen.get("execution_bars", {}).get(symbol, {}).get(next_day)
                if not isinstance(bar, dict) or bar.get("date") != next_day or bar.get("calendar_verified") is not True:
                    raise HTTPException(status_code=422, detail=_incomplete_error())
                if bar.get("previous_session_date") != cursor:
                    raise HTTPException(status_code=422, detail="冻结行情缺少紧邻上一交易日，账户未推进。")
                held = any(pos.get("symbol") == symbol and (pos.get("quantity") or 0) > 0
                           for pos in account.get("positions", []))
                if held and bar.get("price_limits_consistent") is False:
                    raise HTTPException(status_code=422, detail="持仓标的 OHLC 超出涨跌停范围，账户未推进。")
                bars[symbol] = bar
            result = ledger.advance(account_id, next_day, bars, request_id=body.request_id)
            updated = _account_payload(ledger, account_id)
            _insert_history_after_fills(journal, updated, result.get("fills", []))
            return _account_payload(ledger, account_id)

    @app.get("/api/accounts/{account_id}/stats")
    async def account_stats(account_id: int):
        stats = _json_numbers(ledger.stats(account_id))
        stats["totalfills"] = stats.get("total_fills", 0)
        stats["totalclosed"] = stats.get("total_closed", 0)
        if stats.get("avg_loss") is not None:
            stats["avg_loss"] = abs(stats["avg_loss"])
        for field, key in (("by_symbol", "symbol"), ("by_strategy", "strategy"),
                           ("by_discipline", "discipline"), ("by_plan_link", "plan_link"),
                           ("monthly", "month")):
            values = stats.get(field, {})
            if isinstance(values, dict):
                rows = []
                for name, item in sorted(values.items()):
                    row = {key: name, **(item if isinstance(item, dict) else {"value": item})}
                    if field == "monthly" and isinstance(item, dict):
                        row.setdefault("net_pnl", item.get("closed_cycle_net"))
                    rows.append(row)
                stats[field] = rows
        return stats

    @app.get("/api/reviews")
    async def list_reviews(search: str = Query(default="", max_length=200)):
        return journal.reviews(search)

    @app.put("/api/reviews/{review_date}")
    async def save_review(review_date: str, body: ReviewInput):
        day = _date(review_date)
        if day > _today():
            raise HTTPException(status_code=422, detail="不能保存未来日期的复盘。")
        if body.completed and any(not answer.strip() for answer in body.answers):
            raise HTTPException(status_code=422, detail="填写完 7 项内容后才能标记复盘完成。")
        return journal.save_review(day, body.answers, body.tags, body.completed)

    @app.delete("/api/reviews/{review_date}", status_code=204)
    async def delete_review(review_date: str):
        result = journal.delete_review(_date(review_date))
        if result is None:
            raise HTTPException(status_code=404, detail="复盘记录不存在。")
        return Response(status_code=204)

    @app.get("/api/reviews/{review_date}/history")
    async def review_history(review_date: str):
        return journal.review_history(_date(review_date))

    @app.get("/api/plans")
    async def list_plans():
        return journal.plans()

    @app.post("/api/plans")
    async def create_plan(body: PlanInput):
        return journal.create_plan(body.model_dump())

    @app.put("/api/plans/{plan_id}")
    async def update_plan(plan_id: int, body: PlanInput):
        updated = journal.update_plan(plan_id, body.model_dump())
        if updated is None:
            raise HTTPException(status_code=404, detail="交易计划不存在。")
        return updated

    @app.delete("/api/plans/{plan_id}", status_code=204)
    async def delete_plan(plan_id: int):
        if journal.delete_plan(plan_id) is None:
            raise HTTPException(status_code=404, detail="交易计划不存在。")
        return Response(status_code=204)

    @app.get("/api/plans/{plan_id}/history")
    async def plan_history(plan_id: int):
        if journal.plan(plan_id, include_deleted=True) is None:
            raise HTTPException(status_code=404, detail="交易计划不存在。")
        return journal.plan_history(plan_id)

    @app.get("/api/backup")
    async def export_backup():
        backup = _make_backup(resolved_db)
        body = json.dumps(backup, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_BACKUP_BYTES:
            raise HTTPException(status_code=413, detail="备份超过 64 MiB，暂无法导出。")
        filename = f"trading-lab-backup-{_today()}.json"
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })

    @app.post("/api/restore")
    async def restore_backup(body: RestoreInput):
        return _restore_to_new_database(resolved_db, body.backup)

    @app.get("/api/export/fills.csv")
    async def export_fills(account_id: int = Query(..., gt=0)):
        return _csv_download(_account_payload(ledger, account_id), account_id)

    if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def frontend_not_built():
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'><title>交易实验室</title>"
                "<p>本地界面尚未构建。请先运行 launch.command --setup。</p>", status_code=503,
            )
    return app


def _make_backup(db_path: str | Path) -> dict[str, Any]:
    conn = _sqlite(db_path)
    try:
        conn.execute("BEGIN")
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables: dict[str, Any] = {}
        for table in BACKUP_TABLES:
            if table not in available:
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            records = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            tables[table] = {"columns": columns, "rows": records}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"format": "trading-lab-backup", "version": 1, "created_at": _now_iso(), "tables": tables}


def _validate_backup(backup: dict[str, Any]) -> dict[str, Any]:
    if backup.get("format") != "trading-lab-backup" or backup.get("version") != 1:
        raise ValueError("backup format")
    tables = backup.get("tables")
    required_tables = set(BACKUP_TABLES) - OPTIONAL_BACKUP_TABLES
    if (not isinstance(tables, dict) or not required_tables.issubset(tables)
            or set(tables) - set(BACKUP_TABLES)):
        raise ValueError("backup tables")
    normalized: dict[str, Any] = {}
    total_rows = 0
    for table, payload in tables.items():
        if not isinstance(payload, dict) or not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
            raise ValueError("backup shape")
        columns = payload["columns"]
        if not all(isinstance(column, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", column) for column in columns):
            raise ValueError("backup columns")
        if len(columns) != len(set(columns)) or len(payload["rows"]) > 1_000_000:
            raise ValueError("backup row limit")
        rows = []
        for row in payload["rows"]:
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError("backup row shape")
            # Reject nested objects and non-finite numbers before any database writes.
            for value in row.values():
                if isinstance(value, (dict, list)) or (isinstance(value, float) and not math.isfinite(value)):
                    raise ValueError("backup value")
            rows.append(row)
        total_rows += len(rows)
        normalized[table] = {"columns": columns, "rows": rows}
    if total_rows > 2_000_000:
        raise ValueError("backup total rows")
    return normalized


def _check_restored_db(path: Path) -> None:
    conn = _sqlite(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("integrity")
        for row in conn.execute("SELECT id,initial_cash,cash,cursor_date FROM accounts"):
            account_id = int(row[0])
            initial = Decimal(row[1]); cash = Decimal(row[2])
            if not initial.is_finite() or not cash.is_finite() or initial <= 0 or cash < 0:
                raise ValueError("account cash")
            date.fromisoformat(row[3])
            expected_cash = initial
            net_shares: dict[str, int] = {}
            for fill in conn.execute(
                "SELECT side,symbol,quantity,notional,total_fees FROM fills WHERE account_id=?", (account_id,)
            ):
                quantity = int(fill["quantity"])
                notional = Decimal(fill["notional"])
                fees = Decimal(fill["total_fees"])
                if quantity <= 0 or notional <= 0 or fees < 0 or notional.is_finite() is False or fees.is_finite() is False:
                    raise ValueError("fill amount")
                if fill["side"] == "BUY":
                    expected_cash -= notional + fees
                    net_shares[fill["symbol"]] = net_shares.get(fill["symbol"], 0) + quantity
                elif fill["side"] == "SELL":
                    expected_cash += notional - fees
                    net_shares[fill["symbol"]] = net_shares.get(fill["symbol"], 0) - quantity
                else:
                    raise ValueError("fill side")
            if expected_cash.quantize(Decimal("0.01")) != cash.quantize(Decimal("0.01")):
                raise ValueError("cash reconciliation")
            lots = {row[0]: int(row[1] or 0) for row in conn.execute(
                "SELECT symbol,SUM(remaining_quantity) FROM lots WHERE account_id=? GROUP BY symbol",
                (account_id,),
            )}
            for symbol in set(net_shares) | set(lots):
                if net_shares.get(symbol, 0) != lots.get(symbol, 0) or lots.get(symbol, 0) < 0:
                    raise ValueError("position reconciliation")
        for row in conn.execute("SELECT quantity,filled_quantity FROM orders"):
            if int(row[0]) <= 0 or int(row[1]) < 0 or int(row[1]) > int(row[0]):
                raise ValueError("order quantity")
        for row in conn.execute("SELECT quantity,remaining_quantity FROM lots"):
            if int(row[0]) < 0 or int(row[1]) < 0 or int(row[1]) > int(row[0]):
                raise ValueError("lot quantity")
        mismatch = conn.execute(
            "SELECT o.id FROM orders o LEFT JOIN (SELECT order_id,SUM(quantity) AS q FROM fills GROUP BY order_id) f "
            "ON f.order_id=o.id WHERE COALESCE(f.q,0)<>o.filled_quantity LIMIT 1"
        ).fetchone()
        if mismatch:
            raise ValueError("fill reconciliation")
        for row in conn.execute("SELECT checksum,snapshot_json FROM lab_account_market"):
            raw = row["snapshot_json"]
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["checksum"]:
                raise ValueError("market snapshot checksum")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict) \
                    or not isinstance(payload.get("calendar_days"), list):
                raise ValueError("market snapshot shape")
        for row in conn.execute(
            "SELECT c.request_id,c.fingerprint,c.account_id,s.account_id AS snapshot_account_id "
            "FROM lab_account_creates c LEFT JOIN lab_account_market s ON s.account_id=c.account_id"
        ):
            if (not isinstance(row["request_id"], str) or not 1 <= len(row["request_id"]) <= 128
                    or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"])
                    or row["snapshot_account_id"] != row["account_id"]):
                raise ValueError("account create request")
    finally:
        conn.close()
    Ledger(path).accounts()


def _restore_to_new_database(active_db: str | Path, backup: dict[str, Any]) -> dict[str, Any]:
    try:
        tables = _validate_backup(backup)
    except Exception:
        raise HTTPException(status_code=422, detail="备份结构无效，当前数据库未更改。") from None
    destination = Path(active_db).expanduser().resolve().parent / f"trading-lab-restored-{uuid.uuid4().hex[:12]}.sqlite3"
    try:
        Ledger(destination)
        MarketService(destination)
        JournalStore(destination)
        _ensure_app_schema(destination)
        conn = _sqlite(destination)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            for table in reversed(INSERT_ORDER):
                if table in tables:
                    conn.execute(f"DELETE FROM {table}")
            for table in INSERT_ORDER:
                if table not in tables:
                    continue
                payload = tables[table]
                actual = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                if set(payload["columns"]) != set(actual):
                    raise ValueError("schema mismatch")
                columns = payload["columns"]
                placeholders = ",".join("?" for _ in columns)
                names = ",".join(f'"{column}"' for column in columns)
                sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
                conn.executemany(sql, [[row[column] for column in columns] for row in payload["rows"]])
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _check_restored_db(destination)
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=422, detail="备份校验或账本核对未通过，原数据库未更改。") from None
    return {
        "restored": False,
        "message": f"备份已校验并另存为 {destination.name}。当前数据库未替换；如需使用，请重启前将 TRADING_LAB_DB 指向该文件。",
    }

