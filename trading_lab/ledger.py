"""Isolated, deterministic paper ledger for Trading Lab.

This module deliberately has no market-data or broker integration. ``advance``
only consumes the verified, raw bars supplied by its caller; orders are always
created pending and get one next-session opening attempt (DAY time in force).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date as Date
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


CENT = Decimal("0.01")
TICK = Decimal("0.01")
ETF_TICK = Decimal("0.001")
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
SUPPORTED_SYMBOLS = frozenset({"603283.SH", "515880.SH"})
MINIMUM_SAMPLE_SIZE = 30


class LedgerError(ValueError):
    """Base class for a rejected or invalid ledger operation."""


class NotFoundError(LedgerError):
    """An account or order does not exist in this ledger."""


class IdempotencyConflict(LedgerError):
    """A request ID was reused with a different payload."""


class AlreadyAdvancedError(LedgerError):
    """The ledger has already advanced through the requested date."""


class RiskLimitError(LedgerError):
    """A requested order exceeds the configured training risk guard."""


DEFAULT_FEES: dict[str, Any] = {
    "commission_rate": "0.00025",
    "minimum_commission": "5.00",
    "stock_stamp_duty_rate": "0.0005",
    "stock_transfer_fee_rate": "0.00001",
    "etf_stamp_duty_rate": "0",
    "etf_transfer_fee_rate": "0",
    "participation_rate": "0.10",
    "pause_on_drawdown": False,
}


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise LedgerError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError(f"{field} must be a finite number") from None
    if not result.is_finite():
        raise LedgerError(f"{field} must be a finite number")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _date(value: str | Date, field: str = "date") -> str:
    try:
        parsed = value if isinstance(value, Date) else Date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise LedgerError(f"{field} must be an ISO date (YYYY-MM-DD)") from None
    return parsed.isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Date):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"request payload is not JSON serializable: {exc}") from None


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _is_etf(symbol: str) -> bool:
    return symbol.startswith(("15", "50", "51", "52", "56", "58"))


class Ledger:
    """SQLite ledger isolated from existing paper accounts and services.

    Money, prices, fees, and P&L are stored as Decimal strings. Values returned
    to callers are JSON-safe strings for decimals and integers for share counts.
    ``participation_rate`` is a fraction of the supplied bar volume (10% by
    default); buy fills are rounded down to whole 100-share lots.
    """

    def __init__(self, db_path: str | Path):
        raw_path = str(db_path)
        self._uri = raw_path == ":memory:"
        if self._uri:
            self._db = f"file:trading_lab_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self._db, uri=True, check_same_thread=False,
                                           isolation_level=None)
        else:
            path = Path(db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = str(path)
            self._keeper = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, uri=self._uri, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            if not self._uri:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    cursor_date TEXT NOT NULL,
                    initial_cash TEXT NOT NULL,
                    cash TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    limit_price TEXT NOT NULL,
                    stop_price TEXT,
                    plan_id TEXT,
                    plan_snapshot_json TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pending_reason TEXT,
                    cumulative_notional TEXT NOT NULL DEFAULT '0',
                    commission_charged TEXT NOT NULL DEFAULT '0',
                    UNIQUE(account_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS orders_pending
                    ON orders(account_id, status, id);
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    cycle_id INTEGER REFERENCES cycles(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    notional TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    transfer_fee TEXT NOT NULL,
                    stamp_duty TEXT NOT NULL,
                    total_fees TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    plan_snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    plan_id TEXT,
                    strategy TEXT NOT NULL,
                    strategy_version TEXT,
                    discipline TEXT NOT NULL DEFAULT 'unknown',
                    open_date TEXT NOT NULL,
                    close_date TEXT,
                    status TEXT NOT NULL,
                    initial_risk TEXT,
                    total_buy_quantity INTEGER NOT NULL DEFAULT 0,
                    total_sell_quantity INTEGER NOT NULL DEFAULT 0,
                    buy_notional TEXT NOT NULL DEFAULT '0',
                    sell_notional TEXT NOT NULL DEFAULT '0',
                    buy_fees TEXT NOT NULL DEFAULT '0',
                    sell_fees TEXT NOT NULL DEFAULT '0',
                    net_pnl TEXT,
                    r_multiple TEXT,
                    holding_days INTEGER
                );
                CREATE INDEX IF NOT EXISTS cycles_account_status
                    ON cycles(account_id, status, symbol);
                CREATE TABLE IF NOT EXISTS lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
                    buy_fill_id INTEGER NOT NULL REFERENCES fills(id),
                    acquired_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    remaining_quantity INTEGER NOT NULL,
                    remaining_cost TEXT NOT NULL,
                    risk_per_share TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lots_position
                    ON lots(account_id, symbol, acquired_date, id);
                CREATE TABLE IF NOT EXISTS marks (
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    mark_date TEXT NOT NULL,
                    price TEXT NOT NULL,
                    stale INTEGER NOT NULL,
                    PRIMARY KEY(account_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS equity_curve (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    trade_date TEXT NOT NULL,
                    cash TEXT NOT NULL,
                    market_value TEXT,
                    equity TEXT,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT,
                    marks_json TEXT NOT NULL,
                    stale_symbols_json TEXT NOT NULL,
                    UNIQUE(account_id, trade_date)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    method TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY(account_id, method, request_id)
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cycles)")}
            if "strategy_version" not in columns:
                conn.execute("ALTER TABLE cycles ADD COLUMN strategy_version TEXT")
            if "discipline" not in columns:
                conn.execute("ALTER TABLE cycles ADD COLUMN discipline TEXT NOT NULL DEFAULT 'unknown'")
        finally:
            conn.close()

    @staticmethod
    def _settings(fees: Mapping[str, Any] | None) -> dict[str, Any]:
        settings = dict(DEFAULT_FEES)
        if fees:
            unknown = set(fees) - set(settings)
            if unknown:
                raise LedgerError(f"unknown fee/settings keys: {', '.join(sorted(unknown))}")
            settings.update(fees)
        for key in (
            "commission_rate", "minimum_commission", "stock_stamp_duty_rate",
            "stock_transfer_fee_rate", "etf_stamp_duty_rate",
            "etf_transfer_fee_rate", "participation_rate",
        ):
            value = _decimal(settings[key], key)
            if value < ZERO:
                raise LedgerError(f"{key} cannot be negative")
            settings[key] = format(value, "f")
        participation = _decimal(settings["participation_rate"], "participation_rate")
        if participation <= ZERO or participation > Decimal("1"):
            raise LedgerError("participation_rate must be greater than 0 and at most 1")
        if not isinstance(settings["pause_on_drawdown"], bool):
            raise LedgerError("pause_on_drawdown must be a boolean")
        return settings

    @staticmethod
    def _check_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"account {account_id} not found")
        return row

    @staticmethod
    def _check_symbol(symbol: str) -> str:
        result = str(symbol).strip().upper()
        if result not in SUPPORTED_SYMBOLS:
            raise LedgerError(f"unsupported or non-tradable symbol: {result}")
        return result

    @staticmethod
    def _strategy_identity(snapshot: Mapping[str, Any]) -> tuple[str | None, str | None]:
        name = snapshot.get("strategy_name") or snapshot.get("strategy")
        version = snapshot.get("strategy_version")
        return (
            str(name).strip() or None if name is not None else None,
            str(version).strip() or None if version is not None else None,
        )

    @classmethod
    def _buy_strategy_mismatch(
        cls,
        conn: sqlite3.Connection,
        account_id: int,
        symbol: str,
        incoming_snapshot: Mapping[str, Any],
        check_pending: bool = True,
    ) -> bool:
        incoming = cls._strategy_identity(incoming_snapshot)
        cycle = conn.execute(
            "SELECT strategy, strategy_version FROM cycles WHERE account_id = ? AND symbol = ? "
            "AND status = 'open' ORDER BY id DESC LIMIT 1", (account_id, symbol)
        ).fetchone()
        if cycle is not None:
            existing = (None if cycle["strategy"] == "unassigned" else cycle["strategy"],
                        cycle["strategy_version"])
            if incoming != existing:
                return True
        if not check_pending:
            return False
        pending = conn.execute(
            "SELECT plan_snapshot_json FROM orders WHERE account_id = ? AND symbol = ? "
            "AND side = 'BUY' AND status IN ('pending', 'partially_filled') ORDER BY id",
            (account_id, symbol),
        ).fetchall()
        for row in pending:
            try:
                existing = cls._strategy_identity(json.loads(row["plan_snapshot_json"]))
            except (TypeError, ValueError):
                return True
            if incoming != existing:
                return True
        return False

    @staticmethod
    def _check_price(price: Any, field: str, symbol: str | None = None) -> Decimal:
        result = _decimal(price, field)
        tick = ETF_TICK if symbol is not None and _is_etf(symbol) else TICK
        if result <= ZERO or result % tick != ZERO:
            raise LedgerError(f"{field} must be positive and aligned to the {tick} price tick")
        return result

    @staticmethod
    def _fees_for_notional(
        symbol: str,
        notional: Decimal,
        side: str,
        settings: Mapping[str, Any],
        previous_notional: Decimal = ZERO,
        commission_already_charged: Decimal = ZERO,
    ) -> tuple[Decimal, Decimal, Decimal]:
        rate = _decimal(settings["commission_rate"], "commission_rate")
        minimum = _decimal(settings["minimum_commission"], "minimum_commission")
        cumulative = previous_notional + notional
        commission_target = _money(max(minimum, cumulative * rate)) if cumulative > ZERO else ZERO
        commission = _money(max(ZERO, commission_target - commission_already_charged))
        etf = _is_etf(symbol)
        stamp_key = "etf_stamp_duty_rate" if etf else "stock_stamp_duty_rate"
        transfer_key = "etf_transfer_fee_rate" if etf else "stock_transfer_fee_rate"
        stamp = (_money(notional * _decimal(settings[stamp_key], stamp_key))
                 if side == "SELL" else _money(ZERO))
        transfer = _money(notional * _decimal(settings[transfer_key], transfer_key))
        return commission, transfer, stamp

    @classmethod
    def _estimate_fees(
        cls, symbol: str, notional: Decimal, side: str, settings: Mapping[str, Any]
    ) -> Decimal:
        commission, transfer, stamp = cls._fees_for_notional(symbol, notional, side, settings)
        return commission + transfer + stamp

    @staticmethod
    def _idempotent_result(
        conn: sqlite3.Connection,
        account_id: int,
        method: str,
        request_id: str | None,
        payload: Any,
    ) -> dict[str, Any] | None:
        if not request_id:
            return None
        row = conn.execute(
            "SELECT payload_hash, result_json FROM idempotency "
            "WHERE account_id = ? AND method = ? AND request_id = ?",
            (account_id, method, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != _fingerprint(payload):
            raise IdempotencyConflict(f"request_id {request_id!r} reused with different payload")
        return json.loads(row["result_json"])

    @staticmethod
    def _save_idempotent(
        conn: sqlite3.Connection,
        account_id: int,
        method: str,
        request_id: str | None,
        payload: Any,
        result: Mapping[str, Any],
    ) -> None:
        if request_id:
            conn.execute(
                "INSERT INTO idempotency(account_id, method, request_id, payload_hash, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, method, request_id, _fingerprint(payload), _canonical(result)),
            )

    def create_account(
        self,
        name: str,
        initial_cash: Any = "20000",
        start_date: str | Date | None = "2026-09-01",
        mode: str = "historical",
        fees: Mapping[str, Any] | None = None,
        on_created: Callable[[sqlite3.Connection, int], None] | None = None,
    ) -> dict[str, Any]:
        """Create an empty account; optionally run a trusted callback before commit."""
        display_name = str(name).strip()
        if not display_name:
            raise LedgerError("account name cannot be blank")
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"historical", "training", "research"}:
            raise LedgerError("ledger accounts are limited to historical/training/research modes")
        cash = _money(_decimal(initial_cash, "initial_cash"))
        if cash <= ZERO:
            raise LedgerError("initial_cash must be positive")
        cursor = _date(start_date or Date.today())
        settings = self._settings(fees)
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO accounts(name, mode, cursor_date, initial_cash, cash, settings_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (display_name, normalized_mode, cursor, _text(cash), _text(cash),
                 _canonical(settings), Date.today().isoformat()),
            )
            account_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO equity_curve(account_id, trade_date, cash, market_value, equity, "
                "realized_pnl, unrealized_pnl, marks_json, stale_symbols_json) "
                "VALUES (?, ?, ?, '0.00', ?, '0.00', '0.00', '{}', '[]')",
                (account_id, cursor, _text(cash), _text(cash)),
            )
            if on_created is not None:
                on_created(conn, account_id)
            return self._account_summary(conn, account_id)

    def accounts(self) -> list[dict[str, Any]]:
        with self._reading() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM accounts ORDER BY id")]
            return [self._account_summary(conn, int(account_id)) for account_id in ids]

    def account(
        self,
        account_id: int | None = None,
        marks: Mapping[str, Any] | None = None,
        *,
        id: int | None = None,
    ) -> dict[str, Any]:
        account_id = account_id if account_id is not None else id
        if account_id is None:
            raise LedgerError("account id is required")
        with self._reading() as conn:
            self._check_account(conn, account_id)
            return self._account_summary(conn, account_id, marks)

    def _account_summary(
        self,
        conn: sqlite3.Connection,
        account_id: int,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = self._check_account(conn, account_id)
        settings = json.loads(account["settings_json"])
        cash = _decimal(account["cash"], "cash")
        realized = self._realized_pnl(conn, account_id)
        rows = conn.execute(
            "SELECT symbol, SUM(remaining_quantity) AS quantity FROM lots "
            "WHERE account_id = ? GROUP BY symbol HAVING SUM(remaining_quantity) > 0 ORDER BY symbol",
            (account_id,),
        ).fetchall()
        # Use per-lot Decimal arithmetic below; SQLite REAL is deliberately not
        # used as the source of monetary totals.
        positions: list[dict[str, Any]] = []
        values: dict[str, Decimal | None] = {}
        stale_symbols: list[str] = []
        override_prices: dict[str, Decimal] = {}
        for sym, raw_price in (overrides or {}).items():
            symbol = self._check_symbol(str(sym))
            override_prices[symbol] = self._check_price(raw_price, f"marks[{symbol}]", symbol)
        for row in rows:
            symbol = row["symbol"]
            lots = conn.execute(
                "SELECT remaining_quantity, remaining_cost, acquired_date FROM lots "
                "WHERE account_id = ? AND symbol = ? AND remaining_quantity > 0 ORDER BY id",
                (account_id, symbol),
            ).fetchall()
            quantity = sum(int(lot["remaining_quantity"]) for lot in lots)
            cost = sum((_decimal(lot["remaining_cost"], "lot cost") for lot in lots), ZERO)
            cost = _money(cost)
            sellable = sum(
                int(lot["remaining_quantity"])
                for lot in lots if lot["acquired_date"] < account["cursor_date"]
            )
            next_session_sellable = sum(
                int(lot["remaining_quantity"])
                for lot in lots if lot["acquired_date"] <= account["cursor_date"]
            )
            mark_row = conn.execute(
                "SELECT price, stale FROM marks WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
            stale = True
            mark: Decimal | None = None
            # Stored marks are frozen to the account cursor. A caller-provided
            # quote cannot replace a mark already persisted for that symbol.
            if mark_row is not None:
                mark = _decimal(mark_row["price"], "mark")
                stale = bool(mark_row["stale"])
            elif symbol in override_prices:
                mark = override_prices[symbol]
                stale = False
            if stale:
                stale_symbols.append(symbol)
            market_value = _money(mark * quantity) if mark is not None else None
            unrealized = _money(market_value - cost) if market_value is not None else None
            values[symbol] = market_value
            positions.append({
                "symbol": symbol,
                "quantity": quantity,
                "sellable": sellable,
                "next_session_sellable": next_session_sellable,
                "avg_cost": _text((cost / quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
                if quantity else None,
                "market_value": _text(market_value),
                "unrealized_pnl": _text(unrealized),
                "mark": _text(mark),
                "mark_stale": stale,
            })
        all_marked = all(v is not None for v in values.values())
        market_value_total = _money(sum((v for v in values.values() if v is not None), ZERO))
        equity = _money(cash + market_value_total) if all_marked else None
        reserved_cash = self._reserved_cash(conn, account_id)
        available_cash = max(ZERO, _money(cash - reserved_cash))
        unrealized_total = _money(sum(
            (_decimal(position["unrealized_pnl"], "position unrealized pnl")
             for position in positions if position["unrealized_pnl"] is not None), ZERO
        )) if all_marked else None
        latest = conn.execute(
            "SELECT equity FROM equity_curve WHERE account_id = ? ORDER BY trade_date DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        peak = self._peak_equity(conn, account_id)
        current_for_dd = equity if equity is not None else (
            _decimal(latest["equity"], "equity") if latest and latest["equity"] is not None else None
        )
        drawdown = self._drawdown(current_for_dd, peak)
        warnings = [f"stale_mark:{symbol}" for symbol in sorted(stale_symbols)]
        if drawdown is not None and drawdown >= Decimal("0.05"):
            warnings.append("drawdown_at_or_above_5_percent")
        orders = [self._order_record(row) for row in conn.execute(
            "SELECT * FROM orders WHERE account_id = ? ORDER BY id", (account_id,)
        )]
        fills = [self._fill_record(row) for row in conn.execute(
            "SELECT * FROM fills WHERE account_id = ? ORDER BY id", (account_id,)
        )]
        cycles = [self._cycle_record(row) for row in conn.execute(
            "SELECT * FROM cycles WHERE account_id = ? ORDER BY id", (account_id,)
        )]
        curve = [self._curve_record(row) for row in conn.execute(
            "SELECT * FROM equity_curve WHERE account_id = ? ORDER BY trade_date", (account_id,)
        )]
        return {
            "id": int(account_id),
            "name": account["name"],
            "mode": account["mode"],
            "cursor_date": account["cursor_date"],
            "initial_cash": account["initial_cash"],
            "cash": account["cash"],
            "reserved_cash": _text(reserved_cash),
            "available_cash": _text(available_cash),
            "equity": _text(equity),
            "market_value": _text(market_value_total) if all_marked else None,
            "realized_pnl": _text(realized),
            "unrealized_pnl": _text(unrealized_total),
            "drawdown": _text(drawdown),
            "warnings": warnings,
            "settings": settings,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "cycles": cycles,
            "equity_curve": curve,
        }

    def order(
        self,
        account_id: int,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: Any,
        stop_price: Any | None = None,
        plan_id: str | None = None,
        request_id: str | None = None,
        risk_pct: Any = 1,
        plan_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue one next-session opening DAY order; 1 ``risk_pct`` means 1%."""
        symbol = self._check_symbol(symbol)
        side = str(side).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise LedgerError("side must be BUY or SELL")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise LedgerError("quantity must be a positive integer share count")
        if side == "BUY" and quantity % 100:
            raise LedgerError("buy quantity must be a multiple of 100 shares")
        limit = self._check_price(limit_price, "limit_price", symbol)
        stop = None if stop_price is None else self._check_price(stop_price, "stop_price", symbol)
        if side == "BUY" and (stop is None or stop >= limit):
            raise LedgerError("BUY orders require stop_price > 0 and stop_price < limit_price")
        risk = _decimal(risk_pct, "risk_pct")
        if risk <= ZERO or risk > ONE_HUNDRED:
            raise LedgerError("risk_pct is expressed in percentage points and must be in (0, 100]")
        request_id = str(request_id).strip() if request_id else None
        if request_id == "":
            request_id = None
        if request_id is None:
            request_id = uuid.uuid4().hex
        plan_id = str(plan_id).strip() if plan_id is not None else None
        if plan_snapshot is not None and not isinstance(plan_snapshot, Mapping):
            raise LedgerError("plan_snapshot must be a mapping when provided")
        plan_meta = dict(plan_snapshot or {})
        if plan_id is None and plan_meta.get("plan_id") is not None:
            plan_id = str(plan_meta["plan_id"]).strip() or None
        payload = {
            "symbol": symbol, "side": side, "quantity": quantity,
            "limit_price": format(limit, "f"), "stop_price": _text(stop),
            "plan_id": plan_id, "risk_pct": format(risk, "f"),
            "plan_snapshot": plan_meta,
        }
        with self._transaction() as conn:
            account = self._check_account(conn, account_id)
            existing = self._idempotent_result(conn, account_id, "order", request_id, payload)
            if existing is not None:
                return existing
            settings = json.loads(account["settings_json"])
            current_dd = self._current_drawdown(conn, account_id)
            if side == "BUY" and current_dd is not None and current_dd >= Decimal("0.05"):
                if settings["pause_on_drawdown"]:
                    raise RiskLimitError("new BUY blocked by configured 5% drawdown pause")
            if side == "BUY" and self._buy_strategy_mismatch(
                conn, account_id, symbol, plan_meta
            ):
                raise LedgerError(
                    "BUY strategy name/version must match the open symbol cycle and pending BUY orders"
                )
            if side == "BUY":
                equity_row = conn.execute(
                    "SELECT equity FROM equity_curve WHERE account_id = ? ORDER BY trade_date DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                cash = _decimal(account["cash"], "cash")
                equity = _decimal(equity_row["equity"], "equity") if equity_row and equity_row["equity"] else cash
                budget_basis = min(cash, equity)
                budget = _money(budget_basis * risk / ONE_HUNDRED)
                risk_per_share = limit - stop
                entry_notional = _money(limit * quantity)
                exit_notional = _money(stop * quantity)
                estimated_fees = (
                    self._estimate_fees(symbol, entry_notional, "BUY", settings)
                    + self._estimate_fees(symbol, exit_notional, "SELL", settings)
                )
                planned_risk = _money(risk_per_share * quantity + estimated_fees)
                if planned_risk > budget:
                    raise RiskLimitError(
                        f"planned risk {planned_risk} exceeds {risk}% budget {budget}; "
                        "quantity is never increased to meet a budget"
                    )
                reserve = self._buy_reserve(quantity, limit, symbol, settings)
                reserved = self._reserved_cash(conn, account_id)
                if reserve + reserved > cash:
                    raise RiskLimitError("BUY order would reserve more cash than the account holds")
            else:
                sellable = self._sellable_quantity(
                    conn, account_id, symbol, account["cursor_date"], next_session=True
                )
                reserved_sell = self._reserved_sell_quantity(conn, account_id, symbol)
                if quantity + reserved_sell > sellable:
                    raise RiskLimitError(
                        f"SELL quantity exceeds T+1 sellable shares ({max(0, sellable - reserved_sell)})"
                    )
                if quantity % 100 and quantity + reserved_sell != sellable:
                    raise LedgerError(
                        "an odd-lot SELL must liquidate the entire unreserved T+1 remainder"
                    )
            snapshot = {
                **plan_meta,
                "plan_id": plan_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "limit_price": format(limit, "f"),
                "stop_price": _text(stop),
                "risk_pct": format(risk, "f"),
                "created_date": account["cursor_date"],
                "planned_risk": _text(planned_risk) if side == "BUY" else None,
            }
            cur = conn.execute(
                "INSERT INTO orders(account_id, symbol, side, quantity, filled_quantity, limit_price, "
                "stop_price, plan_id, plan_snapshot_json, request_id, created_date, status, pending_reason) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'pending', 'awaiting_next_advance')",
                (account_id, symbol, side, quantity, _text(limit), _text(stop), plan_id,
                 _canonical(snapshot), request_id, account["cursor_date"]),
            )
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()
            result = self._order_record(row)
            if side == "BUY" and current_dd is not None and current_dd >= Decimal("0.05"):
                result["warnings"] = ["drawdown_at_or_above_5_percent"]
            self._save_idempotent(conn, account_id, "order", request_id, payload, result)
            return result

    @staticmethod
    def _buy_reserve(quantity: int, limit: Decimal, symbol: str,
                     settings: Mapping[str, Any]) -> Decimal:
        notional = _money(limit * quantity)
        fees = Ledger._estimate_fees(symbol, notional, "BUY", settings)
        return _money(notional + fees)

    @classmethod
    def _reserved_cash(cls, conn: sqlite3.Connection, account_id: int,
                       except_order_id: int | None = None) -> Decimal:
        rows = conn.execute(
            "SELECT * FROM orders WHERE account_id = ? AND side = 'BUY' "
            "AND status IN ('pending', 'partially_filled') ORDER BY id", (account_id,)
        ).fetchall()
        total = ZERO
        account_row = conn.execute(
            "SELECT settings_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        settings = json.loads(account_row["settings_json"])
        for row in rows:
            if except_order_id is not None and int(row["id"]) == except_order_id:
                continue
            remaining = int(row["quantity"]) - int(row["filled_quantity"])
            if remaining <= 0:
                continue
            limit = _decimal(row["limit_price"], "limit price")
            notional = _money(limit * remaining)
            commission, transfer, stamp = cls._fees_for_notional(
                row["symbol"], notional, "BUY", settings,
                previous_notional=_decimal(row["cumulative_notional"], "order notional"),
                commission_already_charged=_decimal(row["commission_charged"], "order commission"),
            )
            total += notional + commission + transfer + stamp
        return _money(total)

    @staticmethod
    def _sellable_quantity(conn: sqlite3.Connection, account_id: int,
                           symbol: str, on_date: str, next_session: bool = False) -> int:
        comparison = "<=" if next_session else "<"
        row = conn.execute(
            "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity FROM lots "
            f"WHERE account_id = ? AND symbol = ? AND remaining_quantity > 0 AND acquired_date {comparison} ?",
            (account_id, symbol, on_date),
        ).fetchone()
        return int(row["quantity"])

    @staticmethod
    def _reserved_sell_quantity(conn: sqlite3.Connection, account_id: int, symbol: str,
                                except_order_id: int | None = None) -> int:
        rows = conn.execute(
            "SELECT id, quantity, filled_quantity FROM orders WHERE account_id = ? AND symbol = ? "
            "AND side = 'SELL' AND status IN ('pending', 'partially_filled')",
            (account_id, symbol),
        ).fetchall()
        return sum(max(0, int(row["quantity"]) - int(row["filled_quantity"]))
                   for row in rows if except_order_id is None or int(row["id"]) != except_order_id)

    def cancel(self, account_id: int, order_id: int) -> dict[str, Any]:
        with self._transaction() as conn:
            self._check_account(conn, account_id)
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ? AND account_id = ?", (order_id, account_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"order {order_id} not found")
            if row["status"] in {"pending", "partially_filled"}:
                conn.execute(
                    "UPDATE orders SET status = 'cancelled', pending_reason = 'cancelled_by_user' "
                    "WHERE id = ?", (order_id,)
                )
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return self._order_record(row)

    @staticmethod
    def _bar_reason(bar: Mapping[str, Any] | None, symbol: str, trade_date: str) -> str | None:
        if bar is None:
            return "missing_market_bar"
        if not isinstance(bar, Mapping):
            return "invalid_market_bar"
        try:
            if _date(bar.get("date"), "bar date") != trade_date:
                return "bar_date_mismatch"
        except LedgerError:
            return "bar_date_missing_or_invalid"
        if bar.get("is_open", bar.get("calendar_is_open", True)) is not True:
            return "market_closed_or_calendar_unverified"
        if bar.get("calendar_verified") is not True:
            return "calendar_unverified"
        if bar.get("corporate_action_verified") is not True:
            return "corporate_action_unverified"
        if bar.get("execution_allowed") is not True:
            return "execution_not_allowed"
        if bar.get("limits_verified", bar.get("price_limits_verified")) is not True:
            return "price_limits_unverified"
        if bar.get("adjustment") != "none":
            return "adjusted_bar_rejected"
        if "raw_adjustment" in bar and bar["raw_adjustment"] != "none":
            return "adjusted_bar_rejected"
        try:
            volume = _decimal(bar.get("volume"), "bar volume")
            if volume <= ZERO:
                return "zero_or_missing_volume"
            open_price = Ledger._check_price(bar.get("open"), "bar open", symbol)
            upper = Ledger._check_price(bar.get("up_limit"), "bar up_limit", symbol)
            lower = Ledger._check_price(bar.get("down_limit"), "bar down_limit", symbol)
            if lower >= upper or open_price < lower or open_price > upper:
                return "open_outside_verified_price_limits"
        except LedgerError:
            return "invalid_or_incomplete_execution_bar"
        return None

    @staticmethod
    def _held_bar_reason(bar: Mapping[str, Any] | None, trade_date: str,
                         symbol: str) -> str | None:
        if bar is None:
            return "missing_held_symbol_bar"
        if not isinstance(bar, Mapping):
            return "invalid_held_symbol_bar"
        try:
            if _date(bar.get("date"), "bar date") != trade_date:
                return "bar_date_mismatch"
        except LedgerError:
            return "bar_date_missing_or_invalid"
        if bar.get("calendar_verified") is not True:
            return "calendar_unverified"
        if bar.get("corporate_action_verified") is not True:
            return "corporate_action_unverified"
        if bar.get("adjustment") != "none":
            return "adjusted_bar_rejected"
        if "raw_adjustment" in bar and bar["raw_adjustment"] != "none":
            return "adjusted_bar_rejected"
        status = str(bar.get("status", "")).strip().lower()
        if status.startswith("invalid_price_limits"):
            return "invalid_price_limits"
        if bar.get("close") is not None:
            try:
                reason = Ledger._mark_validation_reason(bar, symbol)
            except LedgerError:
                return "invalid_held_mark_bar"
            if reason:
                return reason
        return None

    @staticmethod
    def _mark_validation_reason(bar: Mapping[str, Any], symbol: str | None) -> str | None:
        """Validate a present close and any accompanying OHLC/limit fields."""
        if bar.get("close") is None:
            return None
        close = Ledger._check_price(bar["close"], "bar close", symbol)
        known: dict[str, Decimal] = {"close": close}
        for field in ("open", "high", "low"):
            if bar.get(field) is not None:
                known[field] = Ledger._check_price(bar[field], f"bar {field}", symbol)
        if "high" in known and "low" in known and known["low"] > known["high"]:
            return "invalid_ohlc_relationship"
        if "high" in known and any(
            known["high"] < value for key, value in known.items() if key != "high"
        ):
            return "invalid_ohlc_relationship"
        if "low" in known and any(
            known["low"] > value for key, value in known.items() if key != "low"
        ):
            return "invalid_ohlc_relationship"
        if bar.get("up_limit") is not None and bar.get("down_limit") is not None:
            upper = Ledger._check_price(bar["up_limit"], "bar up_limit", symbol)
            lower = Ledger._check_price(bar["down_limit"], "bar down_limit", symbol)
            if lower >= upper:
                return "invalid_price_limits"
            if any(value < lower or value > upper for value in known.values()):
                return "bar_price_outside_verified_limits"
        return None

    @staticmethod
    def _volume_capacity(bar: Mapping[str, Any], rate: Decimal, trade_date: str) -> int:
        """Use only a prior day's visible volume, never current-day full volume."""
        try:
            capacity_date = _date(bar.get("capacity_date"), "capacity_date")
            capacity_volume = _decimal(bar.get("capacity_volume"), "capacity_volume")
        except LedgerError:
            return 0
        if capacity_date >= trade_date or capacity_volume <= ZERO:
            return 0
        return int((capacity_volume * rate).to_integral_value(rounding=ROUND_DOWN))

    @staticmethod
    def _buy_quantity_affordable(
        max_quantity: int,
        price: Decimal,
        cash_available: Decimal,
        order_row: sqlite3.Row,
        settings: Mapping[str, Any],
    ) -> int:
        quantity = max_quantity - (max_quantity % 100)
        previous_notional = _decimal(order_row["cumulative_notional"], "order notional")
        commission_so_far = _decimal(order_row["commission_charged"], "order commission")
        while quantity > 0:
            notional = _money(price * quantity)
            commission, transfer, stamp = Ledger._fees_for_notional(
                order_row["symbol"], notional, "BUY", settings,
                previous_notional=previous_notional,
                commission_already_charged=commission_so_far,
            )
            if notional + commission + transfer + stamp <= cash_available:
                return quantity
            quantity -= 100
        return 0

    def advance(
        self,
        account_id: int,
        date: str | Date,
        bars: Mapping[str, Mapping[str, Any]],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        day = _date(date)
        if not isinstance(bars, Mapping):
            raise LedgerError("bars must map symbols to raw execution bars")
        normalized_bars: dict[str, Mapping[str, Any]] = {}
        for symbol, bar in bars.items():
            normalized_bars[self._check_symbol(str(symbol))] = bar
        request_id = str(request_id).strip() if request_id else None
        if request_id == "":
            request_id = None
        payload = {"date": day, "bars": normalized_bars}
        with self._transaction() as conn:
            account = self._check_account(conn, account_id)
            existing = self._idempotent_result(conn, account_id, "advance", request_id, payload)
            if existing is not None:
                return existing
            previous_cursor = account["cursor_date"]
            if day <= previous_cursor:
                raise AlreadyAdvancedError(
                    f"advance date {day} must be strictly after cursor {previous_cursor}"
                )
            for symbol, bar in normalized_bars.items():
                if not isinstance(bar, Mapping):
                    raise LedgerError(f"bar for {symbol} must be a mapping")
                try:
                    bar_date = _date(bar.get("date"), f"{symbol}.date")
                    previous_session = _date(
                        bar.get("previous_session_date"), f"{symbol}.previous_session_date"
                    )
                except LedgerError as exc:
                    raise LedgerError(str(exc)) from None
                if bar_date != day:
                    raise LedgerError(
                        f"bar date for {symbol} ({bar_date}) does not match advance date {day}"
                    )
                if previous_session != previous_cursor:
                    raise LedgerError(
                        f"previous_session_date for {symbol} must match account cursor "
                        f"{previous_cursor}, got {previous_session}"
                    )
            settings = json.loads(account["settings_json"])
            # Do not roll an existing position across a missing, adjusted,
            # calendar-unverified, or corporate-action-unverified bar.
            held_symbols = [row[0] for row in conn.execute(
                "SELECT symbol FROM lots WHERE account_id = ? GROUP BY symbol "
                "HAVING SUM(remaining_quantity) > 0", (account_id,)
            )]
            for held_symbol in held_symbols:
                held_bar = normalized_bars.get(held_symbol)
                held_reason = self._held_bar_reason(held_bar, day, held_symbol)
                if held_reason:
                    raise LedgerError(f"cannot advance held symbol {held_symbol}: {held_reason}")
            capacity_by_symbol: dict[str, int] = {}
            capacity_valid_by_symbol: dict[str, bool] = {}
            reasons: dict[int, str] = {}
            fills_today: list[dict[str, Any]] = []
            pending = conn.execute(
                "SELECT * FROM orders WHERE account_id = ? "
                "AND status IN ('pending', 'partially_filled') ORDER BY id", (account_id,)
            ).fetchall()
            for order_row in pending:
                order_id = int(order_row["id"])
                if order_row["created_date"] != previous_cursor:
                    reason = "order_created_after_previous_cursor"
                    reasons[order_id] = reason
                    conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                 (reason, order_id))
                    continue
                bar = normalized_bars.get(order_row["symbol"])
                reason = self._bar_reason(bar, order_row["symbol"], day)
                if reason:
                    reasons[order_id] = reason
                    conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                 (reason, order_id))
                    continue
                assert bar is not None
                open_price = self._check_price(bar["open"], "bar open", order_row["symbol"])
                upper = self._check_price(bar["up_limit"], "bar up_limit", order_row["symbol"])
                lower = self._check_price(bar["down_limit"], "bar down_limit", order_row["symbol"])
                side = order_row["side"]
                if side == "BUY" and open_price >= upper:
                    reason = "locked_at_upper_limit_no_queue_fill"
                elif side == "SELL" and open_price <= lower:
                    reason = "locked_at_lower_limit_no_queue_fill"
                elif side == "BUY" and open_price > _decimal(order_row["limit_price"], "limit price"):
                    reason = "limit_not_reached"
                elif side == "BUY" and open_price <= _decimal(order_row["stop_price"], "stop price"):
                    reason = "open_at_or_below_stop_invalidates_risk"
                elif side == "SELL" and open_price < _decimal(order_row["limit_price"], "limit price"):
                    reason = "limit_not_reached"
                else:
                    reason = ""
                if reason:
                    reasons[order_id] = reason
                    conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                 (reason, order_id))
                    continue
                symbol = order_row["symbol"]
                if symbol not in capacity_by_symbol:
                    capacity_by_symbol[symbol] = self._volume_capacity(
                        bar, _decimal(settings["participation_rate"], "participation_rate"), day
                    )
                    capacity_valid_by_symbol[symbol] = capacity_by_symbol[symbol] > 0
                max_capacity = capacity_by_symbol[symbol]
                if max_capacity <= 0:
                    reason = ("participation_capacity_exhausted" if capacity_valid_by_symbol[symbol]
                              else "missing_or_nonprior_lagged_capacity")
                    reasons[order_id] = reason
                    conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                 (reason, order_id))
                    continue
                remaining = int(order_row["quantity"]) - int(order_row["filled_quantity"])
                if side == "BUY":
                    max_fill = min(remaining, max_capacity)
                    other_reservations = self._reserved_cash(conn, account_id, except_order_id=order_id)
                    cash = _decimal(account["cash"], "cash")
                    available_cash = max(ZERO, cash - other_reservations)
                    max_fill = self._buy_quantity_affordable(
                        max_fill, open_price, available_cash, order_row, settings
                    )
                    if max_fill <= 0:
                        reason = "insufficient_cash_or_participation_capacity"
                        reasons[order_id] = reason
                        conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                     (reason, order_id))
                        continue
                else:
                    sellable = self._sellable_quantity(conn, account_id, symbol, day)
                    max_fill = min(remaining, max_capacity, sellable)
                    if max_fill <= 0:
                        reason = "no_t_plus_1_sellable_shares_or_capacity"
                        reasons[order_id] = reason
                        conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                     (reason, order_id))
                        continue
                if side == "BUY" and self._buy_strategy_mismatch(
                    conn, account_id, symbol, json.loads(order_row["plan_snapshot_json"]),
                    check_pending=False,
                ):
                    reason = "strategy_version_mismatch_at_fill"
                    reasons[order_id] = reason
                    conn.execute("UPDATE orders SET pending_reason = ? WHERE id = ?",
                                 (reason, order_id))
                    continue
                fill = self._execute_fill(
                    conn, account_id, order_row, day, open_price, max_fill, settings
                )
                fills_today.append(fill)
                capacity_by_symbol[symbol] = max(0, max_capacity - max_fill)
                if fill["quantity"] < remaining:
                    reasons[order_id] = "partial_fill_remainder_expired_at_day_end"
                account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            # Mainland A-share limit orders are DAY orders in this MVP: a
            # next-session opening attempt expires at that session's close.
            # A held-symbol integrity failure above aborts before this point.
            expiring = conn.execute(
                "SELECT id, filled_quantity FROM orders WHERE account_id = ? "
                "AND status IN ('pending', 'partially_filled') AND created_date <= ? ORDER BY id",
                (account_id, previous_cursor),
            ).fetchall()
            for expiring_order in expiring:
                order_id = int(expiring_order["id"])
                filled_quantity = int(expiring_order["filled_quantity"])
                if filled_quantity:
                    status = "partially_filled_expired"
                    reason = "partial_fill_remainder_expired_at_day_end"
                else:
                    status = "expired"
                    old_reason = conn.execute(
                        "SELECT pending_reason FROM orders WHERE id = ?", (order_id,)
                    ).fetchone()[0]
                    reason = old_reason or "not_filled_at_next_session_open"
                conn.execute(
                    "UPDATE orders SET status = ?, pending_reason = ? WHERE id = ?",
                    (status, reason, order_id),
                )
                if reason:
                    reasons[order_id] = reason
            conn.execute("UPDATE accounts SET cursor_date = ? WHERE id = ?", (day, account_id))
            stale_symbols = self._update_marks_and_curve(conn, account_id, day, normalized_bars,
                                                         fills_today)
            summary = self._account_summary(conn, account_id)
            drawdown = _decimal(summary["drawdown"], "drawdown") if summary["drawdown"] else None
            warnings = [f"stale_mark:{symbol}" for symbol in stale_symbols]
            if drawdown is not None and drawdown >= Decimal("0.05"):
                warnings.append("drawdown_at_or_above_5_percent")
            result = {
                "account_id": int(account_id),
                "date": day,
                "previous_cursor": previous_cursor,
                "fills": fills_today,
                "blocked_orders": [
                    {"order_id": order_id, "reason": reason} for order_id, reason in sorted(reasons.items())
                ],
                "cash": summary["cash"],
                "equity": summary["equity"],
                "warnings": warnings,
            }
            self._save_idempotent(conn, account_id, "advance", request_id, payload, result)
            return result

    def _execute_fill(
        self,
        conn: sqlite3.Connection,
        account_id: int,
        order_row: sqlite3.Row,
        trade_date: str,
        price: Decimal,
        quantity: int,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        order_id = int(order_row["id"])
        symbol = order_row["symbol"]
        side = order_row["side"]
        notional = _money(price * quantity)
        previous_notional = _decimal(order_row["cumulative_notional"], "order notional")
        commission_before = _decimal(order_row["commission_charged"], "order commission")
        commission, transfer, stamp = self._fees_for_notional(
            symbol, notional, side, settings,
            previous_notional=previous_notional,
            commission_already_charged=commission_before,
        )
        fees = commission + transfer + stamp
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        cash = _decimal(account["cash"], "cash")
        plan_snapshot = order_row["plan_snapshot_json"]
        realized_pnl = ZERO
        cycle_id: int | None = None
        cost_basis = ZERO
        if side == "BUY":
            cash_change = notional + fees
            if cash_change > cash:
                raise LedgerError("atomic fill would create negative cash")
            open_cycle = conn.execute(
                "SELECT * FROM cycles WHERE account_id = ? AND symbol = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1", (account_id, symbol)
            ).fetchone()
            if open_cycle is None:
                snapshot = json.loads(plan_snapshot)
                strategy = (snapshot.get("strategy_name") or snapshot.get("strategy")
                            or "unassigned")
                strategy_version = snapshot.get("strategy_version")
                discipline = snapshot.get("discipline") or snapshot.get("adherence") or "unknown"
                cur = conn.execute(
                    "INSERT INTO cycles(account_id, symbol, plan_id, strategy, strategy_version, discipline, "
                    "open_date, status, initial_risk) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '0.00')",
                    (account_id, symbol, order_row["plan_id"], str(strategy),
                     str(strategy_version) if strategy_version is not None else None,
                     str(discipline), trade_date),
                )
                cycle_id = int(cur.lastrowid)
            else:
                cycle_id = int(open_cycle["id"])
            stop = _decimal(order_row["stop_price"], "stop price")
            risk_per_share = _money(max(ZERO, price - stop))
            cur = conn.execute(
                "INSERT INTO fills(account_id, order_id, cycle_id, symbol, side, trade_date, quantity, "
                "price, notional, commission, transfer_fee, stamp_duty, total_fees, realized_pnl, "
                "plan_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0.00', ?)",
                (account_id, order_id, cycle_id, symbol, side, trade_date, quantity, _text(price),
                 _text(notional), _text(commission), _text(transfer), _text(stamp), _text(fees), plan_snapshot),
            )
            fill_id = int(cur.lastrowid)
            total_cost = _money(notional + fees)
            conn.execute(
                "INSERT INTO lots(account_id, symbol, cycle_id, buy_fill_id, acquired_date, quantity, "
                "remaining_quantity, remaining_cost, risk_per_share) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, symbol, cycle_id, fill_id, trade_date, quantity, quantity,
                 _text(total_cost), _text(risk_per_share)),
            )
            cycle_risk = _decimal(
                self._cycle_text(conn, cycle_id, "initial_risk"), "initial risk"
            )
            exit_fees_at_stop = self._estimate_fees(
                symbol, _money(stop * quantity), "SELL", settings
            )
            risk_addition = _money(max(ZERO, price - stop) * quantity + fees + exit_fees_at_stop)
            conn.execute(
                "UPDATE cycles SET total_buy_quantity = total_buy_quantity + ?, "
                "buy_notional = ?, buy_fees = ?, initial_risk = ? WHERE id = ?",
                (quantity,
                 _text(_money(_decimal(self._cycle_text(conn, cycle_id, "buy_notional"), "buy notional") + notional)),
                 _text(_money(_decimal(self._cycle_text(conn, cycle_id, "buy_fees"), "buy fees") + fees)),
                 _text(_money(cycle_risk + risk_addition)),
                 cycle_id),
            )
            cash = _money(cash - cash_change)
        else:
            cash_change = notional - fees
            sellable_lots = conn.execute(
                "SELECT * FROM lots WHERE account_id = ? AND symbol = ? AND remaining_quantity > 0 "
                "AND acquired_date < ? ORDER BY acquired_date, id",
                (account_id, symbol, trade_date),
            ).fetchall()
            shares_left = quantity
            for lot in sellable_lots:
                if shares_left <= 0:
                    break
                lot_qty = int(lot["remaining_quantity"])
                take = min(shares_left, lot_qty)
                remaining_cost = _decimal(lot["remaining_cost"], "lot cost")
                allocation = remaining_cost if take == lot_qty else _money(remaining_cost * take / lot_qty)
                remaining_cost = _money(remaining_cost - allocation)
                conn.execute(
                    "UPDATE lots SET remaining_quantity = ?, remaining_cost = ? WHERE id = ?",
                    (lot_qty - take, _text(remaining_cost), lot["id"]),
                )
                cost_basis += allocation
                shares_left -= take
            if shares_left:
                raise LedgerError("atomic fill would sell more than T+1 sellable quantity")
            if cash_change < ZERO:
                raise LedgerError("sell fees exceed sale proceeds")
            open_cycle = conn.execute(
                "SELECT id FROM cycles WHERE account_id = ? AND symbol = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1", (account_id, symbol)
            ).fetchone()
            cycle_id = int(open_cycle["id"]) if open_cycle else None
            realized_pnl = _money(notional - fees - cost_basis)
            cur = conn.execute(
                "INSERT INTO fills(account_id, order_id, cycle_id, symbol, side, trade_date, quantity, "
                "price, notional, commission, transfer_fee, stamp_duty, total_fees, realized_pnl, "
                "plan_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, order_id, cycle_id, symbol, side, trade_date, quantity, _text(price),
                 _text(notional), _text(commission), _text(transfer), _text(stamp), _text(fees),
                 _text(realized_pnl), plan_snapshot),
            )
            if cycle_id is not None:
                conn.execute(
                    "UPDATE cycles SET total_sell_quantity = total_sell_quantity + ?, "
                    "sell_notional = ?, sell_fees = ? WHERE id = ?",
                    (quantity,
                     _text(_money(_decimal(self._cycle_text(conn, cycle_id, "sell_notional"), "sell notional") + notional)),
                     _text(_money(_decimal(self._cycle_text(conn, cycle_id, "sell_fees"), "sell fees") + fees)),
                     cycle_id),
                )
            cash = _money(cash + cash_change)
            remaining = conn.execute(
                "SELECT COALESCE(SUM(remaining_quantity), 0) FROM lots "
                "WHERE account_id = ? AND symbol = ?", (account_id, symbol)
            ).fetchone()[0]
            if int(remaining) == 0 and cycle_id is not None:
                cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
                net = _money(
                    _decimal(cycle["sell_notional"], "cycle sell notional")
                    - _decimal(cycle["buy_notional"], "cycle buy notional")
                    - _decimal(cycle["buy_fees"], "cycle buy fees")
                    - _decimal(cycle["sell_fees"], "cycle sell fees")
                )
                initial_risk = _decimal(cycle["initial_risk"], "cycle initial risk") if cycle["initial_risk"] else ZERO
                r_multiple = net / initial_risk if initial_risk > ZERO else None
                hold_days = (Date.fromisoformat(trade_date) - Date.fromisoformat(cycle["open_date"])).days
                conn.execute(
                    "UPDATE cycles SET close_date = ?, status = 'closed', net_pnl = ?, r_multiple = ?, "
                    "holding_days = ? WHERE id = ?",
                    (trade_date, _text(net), _text(r_multiple), hold_days, cycle_id),
                )
        conn.execute("UPDATE accounts SET cash = ? WHERE id = ?", (_text(cash), account_id))
        filled_after = int(order_row["filled_quantity"]) + quantity
        status = "filled" if filled_after >= int(order_row["quantity"]) else "partially_filled"
        cumulative_notional = _money(previous_notional + notional)
        commission_after = _money(commission_before + commission)
        conn.execute(
            "UPDATE orders SET filled_quantity = ?, status = ?, pending_reason = ?, "
            "cumulative_notional = ?, commission_charged = ? WHERE id = ?",
            (filled_after, status, None if status == "filled" else "partial_fill_remainder_pending",
             _text(cumulative_notional), _text(commission_after), order_id),
        )
        fill_row = conn.execute("SELECT * FROM fills WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._fill_record(fill_row)

    @staticmethod
    def _cycle_text(conn: sqlite3.Connection, cycle_id: int, field: str) -> str:
        if field not in {"buy_notional", "buy_fees", "sell_notional", "sell_fees", "initial_risk"}:
            raise LedgerError("invalid cycle amount field")
        row = conn.execute(f"SELECT {field} FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        return row[field]

    def _update_marks_and_curve(
        self,
        conn: sqlite3.Connection,
        account_id: int,
        trade_date: str,
        bars: Mapping[str, Mapping[str, Any]],
        fills_today: list[dict[str, Any]],
    ) -> list[str]:
        positions = conn.execute(
            "SELECT symbol, SUM(remaining_quantity) quantity FROM lots WHERE account_id = ? "
            "GROUP BY symbol HAVING SUM(remaining_quantity) > 0", (account_id,)
        ).fetchall()
        stale_symbols: list[str] = []
        marks_used: dict[str, str] = {}
        market_value = ZERO
        unrealized = ZERO
        all_marked = True
        for pos in positions:
            symbol = pos["symbol"]
            quantity = int(pos["quantity"])
            bar = bars.get(symbol)
            new_mark: Decimal | None = None
            if isinstance(bar, Mapping):
                adjustment = bar.get("adjustment")
                raw_adjustment_ok = "raw_adjustment" not in bar or bar["raw_adjustment"] == "none"
                status = str(bar.get("status", "")).strip().lower()
                try:
                    positive_volume = _decimal(bar.get("volume"), "bar volume") > ZERO
                except LedgerError:
                    positive_volume = False
                suspended = status.startswith("suspended")
                if (adjustment == "none" and raw_adjustment_ok
                        and not status.startswith("invalid_price_limits")
                        and not suspended and positive_volume
                        and bar.get("close") is not None):
                    try:
                        if self._mark_validation_reason(bar, symbol) is None:
                            new_mark = self._check_price(bar["close"], "bar close", symbol)
                    except LedgerError:
                        new_mark = None
            stale = False
            mark_row = conn.execute(
                "SELECT price FROM marks WHERE account_id = ? AND symbol = ?", (account_id, symbol)
            ).fetchone()
            if new_mark is None:
                stale = True
                if mark_row is not None:
                    new_mark = _decimal(mark_row["price"], "carried mark")
                else:
                    todays_fill = next((fill for fill in reversed(fills_today)
                                        if fill["symbol"] == symbol), None)
                    if todays_fill is not None:
                        new_mark = _decimal(todays_fill["price"], "execution mark")
            if new_mark is None:
                all_marked = False
                stale_symbols.append(symbol)
                continue
            conn.execute(
                "INSERT INTO marks(account_id, symbol, mark_date, price, stale) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, symbol) DO UPDATE SET mark_date=excluded.mark_date, "
                "price=excluded.price, stale=excluded.stale",
                (account_id, symbol, trade_date, _text(new_mark), int(stale)),
            )
            if stale:
                stale_symbols.append(symbol)
            marks_used[symbol] = _text(new_mark) or "0"
            market_value += _money(new_mark * quantity)
            lots = conn.execute(
                "SELECT remaining_cost FROM lots WHERE account_id = ? AND symbol = ? "
                "AND remaining_quantity > 0", (account_id, symbol)
            ).fetchall()
            cost = sum((_decimal(lot["remaining_cost"], "remaining cost") for lot in lots), ZERO)
            unrealized += _money(_money(new_mark * quantity) - cost)
        account = conn.execute("SELECT cash FROM accounts WHERE id = ?", (account_id,)).fetchone()
        cash = _decimal(account["cash"], "cash")
        market_value = _money(market_value)
        equity = _money(cash + market_value) if all_marked else None
        realized = self._realized_pnl(conn, account_id)
        conn.execute(
            "INSERT INTO equity_curve(account_id, trade_date, cash, market_value, equity, realized_pnl, "
            "unrealized_pnl, marks_json, stale_symbols_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, trade_date, _text(cash), _text(market_value) if all_marked else None,
             _text(equity), _text(realized), _text(_money(unrealized)) if all_marked else None,
             _canonical(marks_used), _canonical(sorted(stale_symbols))),
        )
        return sorted(stale_symbols)

    @staticmethod
    def _realized_pnl(conn: sqlite3.Connection, account_id: int) -> Decimal:
        rows = conn.execute(
            "SELECT realized_pnl FROM fills WHERE account_id = ? AND side = 'SELL'", (account_id,)
        ).fetchall()
        return _money(sum((_decimal(row["realized_pnl"], "fill realized pnl") for row in rows), ZERO))

    @staticmethod
    def _peak_equity(conn: sqlite3.Connection, account_id: int) -> Decimal | None:
        values = [
            _decimal(row[0], "equity") for row in conn.execute(
                "SELECT equity FROM equity_curve WHERE account_id = ? AND equity IS NOT NULL "
                "ORDER BY trade_date", (account_id,)
            )
        ]
        return max(values) if values else None

    @staticmethod
    def _drawdown(equity: Decimal | None, peak: Decimal | None) -> Decimal | None:
        if equity is None or peak is None or peak <= ZERO:
            return None
        return max(ZERO, (peak - equity) / peak)

    def _current_drawdown(self, conn: sqlite3.Connection, account_id: int) -> Decimal | None:
        latest = conn.execute(
            "SELECT equity FROM equity_curve WHERE account_id = ? ORDER BY trade_date DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        equity = _decimal(latest["equity"], "equity") if latest and latest["equity"] else None
        return self._drawdown(equity, self._peak_equity(conn, account_id))

    @staticmethod
    def _order_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": int(row["quantity"]),
            "filled_quantity": int(row["filled_quantity"]),
            "remaining_quantity": int(row["quantity"]) - int(row["filled_quantity"]),
            "limit_price": row["limit_price"],
            "stop_price": row["stop_price"],
            "plan_id": row["plan_id"],
            "plan_snapshot": json.loads(row["plan_snapshot_json"]),
            "request_id": row["request_id"],
            "created_date": row["created_date"],
            "time_in_force": "NEXT_SESSION_OPEN_DAY",
            "status": row["status"],
            "pending_reason": row["pending_reason"],
        }

    @staticmethod
    def _fill_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "order_id": int(row["order_id"]),
            "cycle_id": int(row["cycle_id"]) if row["cycle_id"] is not None else None,
            "symbol": row["symbol"],
            "side": row["side"],
            "date": row["trade_date"],
            "quantity": int(row["quantity"]),
            "price": row["price"],
            "notional": row["notional"],
            "commission": row["commission"],
            "transfer_fee": row["transfer_fee"],
            "stamp_duty": row["stamp_duty"],
            "fees": row["total_fees"],
            "realized_pnl": row["realized_pnl"],
            "plan_snapshot": json.loads(row["plan_snapshot_json"]),
        }

    @staticmethod
    def _cycle_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "symbol": row["symbol"],
            "plan_id": row["plan_id"],
            "strategy": row["strategy"],
            "strategy_version": row["strategy_version"],
            "discipline": row["discipline"],
            "open_date": row["open_date"],
            "close_date": row["close_date"],
            "status": row["status"],
            "initial_risk": row["initial_risk"],
            "total_buy_quantity": int(row["total_buy_quantity"]),
            "total_sell_quantity": int(row["total_sell_quantity"]),
            "buy_notional": row["buy_notional"],
            "sell_notional": row["sell_notional"],
            "buy_fees": row["buy_fees"],
            "sell_fees": row["sell_fees"],
            "net_pnl": row["net_pnl"],
            "r_multiple": row["r_multiple"],
            "holding_days": row["holding_days"],
        }

    @staticmethod
    def _curve_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "date": row["trade_date"],
            "cash": row["cash"],
            "market_value": row["market_value"],
            "equity": row["equity"],
            "realized_pnl": row["realized_pnl"],
            "unrealized_pnl": row["unrealized_pnl"],
            "marks": json.loads(row["marks_json"]),
            "stale_symbols": json.loads(row["stale_symbols_json"]),
        }

    def stats(self, account_id: int) -> dict[str, Any]:
        with self._reading() as conn:
            self._check_account(conn, account_id)
            fills = conn.execute(
                "SELECT COUNT(*) FROM fills WHERE account_id = ?", (account_id,)
            ).fetchone()[0]
            closed = [row for row in conn.execute(
                "SELECT * FROM cycles WHERE account_id = ? AND status = 'closed' "
                "ORDER BY close_date, id", (account_id,)
            )]
            pnl_values = [_decimal(row["net_pnl"], "cycle pnl") for row in closed]
            wins = [value for value in pnl_values if value > ZERO]
            losses = [value for value in pnl_values if value < ZERO]
            avg_win = _money(sum(wins, ZERO) / len(wins)) if wins else None
            avg_loss = _money(sum(losses, ZERO) / len(losses)) if losses else None
            gross_profit = sum(wins, ZERO)
            gross_loss = abs(sum(losses, ZERO))
            profit_factor = gross_profit / gross_loss if gross_loss > ZERO else None
            payoff = avg_win / abs(avg_loss) if avg_win is not None and avg_loss not in (None, ZERO) else None
            valid_r = [_decimal(row["r_multiple"], "R multiple") for row in closed
                       if row["r_multiple"] is not None]
            holding = [int(row["holding_days"]) for row in closed if row["holding_days"] is not None]
            equity_rows = conn.execute(
                "SELECT trade_date, equity FROM equity_curve WHERE account_id = ? "
                "ORDER BY trade_date", (account_id,)
            ).fetchall()
            equity_curve = [self._curve_record(row) for row in conn.execute(
                "SELECT * FROM equity_curve WHERE account_id = ? ORDER BY trade_date", (account_id,)
            )]
            peak: Decimal | None = None
            max_dd = ZERO
            max_dd_amount = ZERO
            for row in equity_rows:
                if row["equity"] is None:
                    continue
                equity = _decimal(row["equity"], "equity")
                peak = equity if peak is None else max(peak, equity)
                if peak > ZERO:
                    dd = (peak - equity) / peak
                    max_dd = max(max_dd, dd)
                    max_dd_amount = max(max_dd_amount, peak - equity)
            streaks = self._streaks(pnl_values)
            by_symbol = self._group_stats(closed, lambda row: row["symbol"])
            by_strategy = self._group_stats(
                closed,
                lambda row: (f"{row['strategy']}@{row['strategy_version']}"
                             if row["strategy_version"] else row["strategy"] or "unassigned"),
            )
            by_discipline = self._group_stats(closed, lambda row: row["discipline"] or "unknown")
            by_plan_link = self._group_stats(
                closed, lambda row: "linked" if row["plan_id"] else "unlinked"
            )
            monthly = self._monthly(conn, account_id, closed)
            summary = self._account_summary(conn, account_id)
            cumulative_net = (_decimal(summary["equity"], "equity")
                              - _decimal(summary["initial_cash"], "initial cash")
                              if summary["equity"] is not None else None)
            return {
                "account_id": account_id,
                "total_fills": int(fills),
                "total_closed": len(closed),
                "totalfills": int(fills),
                "totalclosed": len(closed),
                "win_rate": self._ratio(len(wins), len(closed)),
                "avg_win": _text(avg_win),
                "avg_loss": _text(avg_loss),
                "payoff_ratio": _text(payoff),
                "profit_factor": _text(profit_factor),
                "avg_r": _text(sum(valid_r, ZERO) / len(valid_r)) if valid_r else None,
                "cumulative_net": _text(_money(cumulative_net)) if cumulative_net is not None else None,
                "max_drawdown": _text(max_dd),
                "max_drawdown_amount": _text(_money(max_dd_amount)),
                "streaks": streaks,
                "avg_holding_days": _text(Decimal(sum(holding)) / len(holding)) if holding else None,
                "by_symbol": by_symbol,
                "by_strategy": by_strategy,
                "by_discipline": by_discipline,
                "by_plan_link": by_plan_link,
                "monthly": monthly,
                "equity_curve": equity_curve,
                "insufficient_samples": len(closed) < MINIMUM_SAMPLE_SIZE,
                "minimum_sample_size": MINIMUM_SAMPLE_SIZE,
            }

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> str | None:
        if denominator == 0:
            return None
        return _text(Decimal(numerator) / Decimal(denominator))

    @staticmethod
    def _streaks(pnls: list[Decimal]) -> dict[str, int]:
        current_type = "flat"
        current = 0
        max_win = 0
        max_loss = 0
        for pnl in pnls:
            kind = "win" if pnl > ZERO else "loss" if pnl < ZERO else "flat"
            if kind == current_type:
                current += 1
            else:
                current_type = kind
                current = 1
            if kind == "win":
                max_win = max(max_win, current)
            elif kind == "loss":
                max_loss = max(max_loss, current)
        return {
            "current_type": current_type if pnls else "none",
            "current_length": current if pnls else 0,
            "max_win": max_win,
            "max_loss": max_loss,
        }

    @classmethod
    def _group_stats(cls, cycles: list[sqlite3.Row], key_fn: Any) -> dict[str, Any]:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in cycles:
            groups.setdefault(str(key_fn(row)), []).append(row)
        result: dict[str, Any] = {}
        for key, rows in sorted(groups.items()):
            pnls = [_decimal(row["net_pnl"], "cycle pnl") for row in rows]
            wins = [pnl for pnl in pnls if pnl > ZERO]
            losses = [pnl for pnl in pnls if pnl < ZERO]
            gross_loss = abs(sum(losses, ZERO))
            rs = [_decimal(row["r_multiple"], "r multiple") for row in rows
                  if row["r_multiple"] is not None]
            result[key] = {
                "count": len(rows),
                "wins": len(wins),
                "net_pnl": _text(_money(sum(pnls, ZERO))),
                "win_rate": cls._ratio(len(wins), len(rows)),
                "profit_factor": _text(sum(wins, ZERO) / gross_loss) if gross_loss > ZERO else None,
                "avg_r": _text(sum(rs, ZERO) / len(rs)) if rs else None,
            }
        return result

    @classmethod
    def _monthly(cls, conn: sqlite3.Connection, account_id: int,
                 closed: list[sqlite3.Row]) -> dict[str, Any]:
        cycle_pnl: dict[str, Decimal] = {}
        for row in closed:
            month = row["close_date"][:7]
            cycle_pnl[month] = cycle_pnl.get(month, ZERO) + _decimal(row["net_pnl"], "cycle pnl")
        rows = conn.execute(
            "SELECT trade_date, equity FROM equity_curve WHERE account_id = ? "
            "ORDER BY trade_date", (account_id,)
        ).fetchall()
        month_end: dict[str, Decimal] = {}
        for row in rows:
            if row["equity"] is not None:
                month_end[row["trade_date"][:7]] = _decimal(row["equity"], "equity")
        result: dict[str, Any] = {}
        prev_equity: Decimal | None = None
        for month in sorted(set(cycle_pnl) | set(month_end)):
            equity = month_end.get(month)
            result[month] = {
                "closed_cycle_net": _text(_money(cycle_pnl.get(month, ZERO))),
                "month_end_equity": _text(equity),
                "equity_change": _text(_money(equity - prev_equity))
                if equity is not None and prev_equity is not None else None,
                "return": _text((equity - prev_equity) / prev_equity)
                if equity is not None and prev_equity is not None and prev_equity > ZERO else None,
            }
            if equity is not None:
                prev_equity = equity
        return result

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

