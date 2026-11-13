"""A tightly bounded buy-and-hold adapter over VeighNa's native minute engine.

This module intentionally keeps VeighNa imports lazy.  Its database package is
part of BacktestingEngine's import graph, while the research center's parquet
loader remains the only source of market data.  Native imports are isolated
from the user's existing ``.vntrader`` directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.metadata
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Callable

import pandas as pd

from strategy.base import Bar, Context

from . import engine as research_engine
from .strategies import BuyHoldStrategy


LOT_SIZE = 100
PRICE_TICK = 0.01
_NATIVE_IMPORT_LOCK = threading.Lock()
_NATIVE_IMPORT_TEMP: tempfile.TemporaryDirectory[str] | None = None
_NATIVE_RUN_DIR: Path | None = None
_NATIVE_TRADER_DIR: Path | None = None
_NATIVE_API: dict[str, Any] | None = None


class _NativeCancellation(BaseException):
    """Escape VeighNa's per-bar Exception handler without a fake completion."""


def _isolated_native_api() -> dict[str, Any]:
    """Import VeighNa with a temporary, empty settings directory.

    ``vnpy.trader.utility`` selects ``.vntrader`` from the process cwd at import
    time and ``setting.py`` loads ``vt_setting.json``.  Importing while briefly
    inside a fresh directory keeps account settings and existing database files
    out of the import path.  The temporary directory is retained for the worker
    process lifetime because VeighNa stores its resolved paths in module globals.
    """
    global _NATIVE_API, _NATIVE_IMPORT_TEMP, _NATIVE_RUN_DIR, _NATIVE_TRADER_DIR

    with _NATIVE_IMPORT_LOCK:
        if _NATIVE_API is not None:
            return _NATIVE_API

        try:
            versions = {
                "vnpy": importlib.metadata.version("vnpy"),
                "vnpy_portfoliostrategy": importlib.metadata.version(
                    "vnpy_portfoliostrategy"
                ),
            }
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError("VeighNa native dependencies are not installed") from exc
        if versions != {"vnpy": "4.4.0", "vnpy_portfoliostrategy": "1.3.0"}:
            raise RuntimeError(
                "VeighNa adapter requires vnpy 4.4.0 and "
                f"vnpy_portfoliostrategy 1.3.0; found {versions}"
            )

        utility = sys.modules.get("vnpy.trader.utility")
        preloaded_native = any(
            name in sys.modules
            for name in (
                "vnpy.trader.setting",
                "vnpy.trader.database",
                "vnpy_portfoliostrategy.backtesting",
            )
        )
        if utility is not None and _NATIVE_TRADER_DIR is None:
            raise RuntimeError(
                "VeighNa utility was imported before the isolated bridge; "
                "refusing to reuse an unknown .vntrader configuration"
            )
        if preloaded_native and _NATIVE_TRADER_DIR is None:
            raise RuntimeError(
                "VeighNa settings/database modules were imported before the "
                "isolated bridge; refusing to reuse their state"
            )

        if _NATIVE_IMPORT_TEMP is None:
            _NATIVE_IMPORT_TEMP = tempfile.TemporaryDirectory(
                prefix="research-center-vnpy-"
            )
            _NATIVE_RUN_DIR = Path(_NATIVE_IMPORT_TEMP.name) / "run"
            _NATIVE_RUN_DIR.mkdir()
            _NATIVE_TRADER_DIR = _NATIVE_RUN_DIR / ".vntrader"
            _NATIVE_TRADER_DIR.mkdir()
            # The empty config is the only VeighNa settings file this adapter
            # allows the native import to see.  It is private to the temp worker.
            (_NATIVE_TRADER_DIR / "vt_setting.json").write_text(
                "{}", encoding="utf-8"
            )

        if _NATIVE_RUN_DIR is None or _NATIVE_TRADER_DIR is None:
            raise RuntimeError("isolated VeighNa import directory is unavailable")

        prior_cwd = Path.cwd()
        prior_sys_path = list(sys.path)
        try:
            os.chdir(_NATIVE_RUN_DIR)
            from vnpy_portfoliostrategy.backtesting import BacktestingEngine
            from vnpy_portfoliostrategy.template import StrategyTemplate
            from vnpy.trader.constant import Direction, Exchange, Interval, Status
            from vnpy.trader.object import BarData
            from vnpy.trader.utility import round_to
            from vnpy.trader import database as database_module
            from vnpy.trader import utility as utility_module
        except Exception as exc:
            raise RuntimeError(
                "could not import the isolated VeighNa native backtesting API"
            ) from exc
        finally:
            os.chdir(prior_cwd)
            # utility.py adds the isolated cwd to sys.path during import.  The
            # package modules are already loaded; don't leave a stale temp path.
            sys.path[:] = prior_sys_path

        utility_temp = Path(utility_module.TEMP_DIR).resolve()
        if utility_temp != _NATIVE_TRADER_DIR.resolve():
            raise RuntimeError(
                "VeighNa resolved a non-isolated settings directory; refusing to continue"
            )
        if getattr(database_module, "database", None) is not None:
            raise RuntimeError(
                "a VeighNa database instance exists; refusing to reuse it"
            )

        settings_file = _NATIVE_TRADER_DIR / "vt_setting.json"
        if not settings_file.is_file() or settings_file.stat().st_size != 2:
            raise RuntimeError(
                "isolated VeighNa settings changed during import; refusing to continue"
            )

        _NATIVE_API = {
            "BacktestingEngine": BacktestingEngine,
            "StrategyTemplate": StrategyTemplate,
            "BarData": BarData,
            "Direction": Direction,
            "Exchange": Exchange,
            "Interval": Interval,
            "Status": Status,
            "round_to": round_to,
            "database_module": database_module,
            "versions": versions,
        }
        return _NATIVE_API


def _as_timestamp(value: Any, field: str) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"minute data has invalid {field}") from exc
    if pd.isna(parsed) or parsed.tzinfo is not None:
        raise ValueError(f"minute data {field} must be timezone-naive")
    if parsed.second or parsed.microsecond or parsed.nanosecond:
        raise ValueError(f"minute data {field} must be on whole minutes")
    return parsed


def _prepare_frame(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    required = {
        "datetime",
        "source_datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"native bridge input is missing columns: {sorted(missing)}")
    if frame.empty:
        raise ValueError("native bridge requires at least one available minute bar")

    prepared = frame.copy()
    prepared["datetime"] = prepared["datetime"].map(
        lambda value: _as_timestamp(value, "canonical datetime")
    )
    prepared["source_datetime"] = prepared["source_datetime"].map(
        lambda value: _as_timestamp(value, "source datetime")
    )
    prepared = prepared.sort_values("source_datetime").reset_index(drop=True)
    if prepared["source_datetime"].duplicated().any():
        raise ValueError("native bridge input has duplicate source minute labels")

    starts = prepared["datetime"].tolist()
    labels = prepared["source_datetime"].tolist()
    if any(label - start != pd.Timedelta(minutes=1) for start, label in zip(starts, labels)):
        raise ValueError(
            "JoinQuant source end labels must be exactly one minute after canonical bar starts"
        )

    numeric_columns = ["open", "high", "low", "close", "volume", "turnover"]
    numeric = prepared[numeric_columns].apply(pd.to_numeric, errors="coerce")
    if not all(math.isfinite(float(value)) for value in numeric.to_numpy().flat):
        raise ValueError("native bridge input contains non-finite bar fields")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("native bridge requires positive OHLC prices")
    if (numeric[["volume", "turnover"]] < 0).any().any():
        raise ValueError("native bridge requires non-negative volume and turnover")
    prepared[numeric_columns] = numeric
    # The existing canonicalizer separates bare symbol and exchange, while
    # vt_symbol carries the fully qualified identity. Accept that real schema.
    code, exchange = symbol.split('.', 1)
    if 'vt_symbol' in prepared:
        if not prepared['vt_symbol'].astype(str).eq(symbol).all():
            raise ValueError('native bridge vt_symbol differs from validated config')
    elif 'symbol' not in prepared:
        raise ValueError('native bridge input lacks security identity')
    if 'symbol' in prepared:
        if not prepared['symbol'].astype(str).isin([symbol, code]).all():
            raise ValueError('native bridge data symbol differs from validated config')
        if 'vt_symbol' not in prepared and prepared['symbol'].astype(str).eq(code).any() and 'exchange' not in prepared:
            raise ValueError('bare symbol requires an explicit exchange')
    if 'exchange' in prepared and not prepared['exchange'].astype(str).eq(exchange).all():
        raise ValueError('native bridge exchange differs from validated config')
    prepared['symbol'] = symbol
    return prepared


def _floor_price_to_tick(price: float, tick: float) -> float:
    if not math.isfinite(price) or price <= 0 or tick <= 0:
        raise ValueError("native limit price must be positive and finite")
    return max(tick, math.floor(price / tick + 1e-10) * tick)


def _status_name(native_status: Any, status_enum: Any) -> str:
    if native_status == status_enum.ALLTRADED:
        return "filled"
    if native_status == status_enum.PARTTRADED:
        return "partial_filled"
    if native_status == status_enum.CANCELLED:
        return "cancelled"
    if native_status == status_enum.REJECTED:
        return "rejected"
    return "pending"


def _native_run(
    normalized: dict[str, Any],
    frame: pd.DataFrame,
    data_manifest: dict[str, Any],
    progress: Callable[[float, str], None] | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    api = _isolated_native_api()
    BacktestingEngine = api["BacktestingEngine"]
    StrategyTemplate = api["StrategyTemplate"]
    BarData = api["BarData"]
    Direction = api["Direction"]
    Exchange = api["Exchange"]
    Interval = api["Interval"]
    Status = api["Status"]
    round_to = api["round_to"]

    symbol = normalized["symbol"]
    code, exchange_text = symbol.split(".", 1)
    exchange = Exchange.SSE if exchange_text == "SSE" else Exchange.SZSE
    prepared = _prepare_frame(frame, symbol)
    row_by_dt: dict[datetime, Any] = {}
    native_bars = {}
    for row in prepared.itertuples(index=False):
        source_dt = pd.Timestamp(row.source_datetime).to_pydatetime()
        row_by_dt[source_dt] = row
        native_bars[source_dt] = BarData(
            gateway_name="RESEARCH",
            symbol=code,
            exchange=exchange,
            datetime=source_dt,
            interval=Interval.MINUTE,
            volume=float(row.volume),
            turnover=float(row.turnover),
            open_price=float(row.open),
            high_price=float(row.high),
            low_price=float(row.low),
            close_price=float(row.close),
        )

    signal_meta: dict[str, dict[str, Any]] = {}
    status_reasons: dict[str, str] = {}
    processed_datetimes: list[datetime] = []
    native_cancelled = False
    fee_by_order: dict[str, float] = {}
    stamp_duty_by_order: dict[str, float] = {}
    slippage_by_order: dict[str, float] = {}

    class BoundedBacktestingEngine(BacktestingEngine):
        """Keep VeighNa matching; add only data, capacity, and cancellation gates."""

        def output(self, msg: str) -> None:
            self.logs.append(str(msg))

        def new_bars(self, dt: datetime) -> None:
            nonlocal native_cancelled
            if cancelled and cancelled():
                native_cancelled = True
                raise _NativeCancellation()
            super().new_bars(dt)
            if self.strategy and self.strategy.inited:
                processed_datetimes.append(dt)
                if progress:
                    done = len(processed_datetimes)
                    total = max(1, len(self.dts))
                    step = max(1, total // 100)
                    if done == total or done % step == 0:
                        progress(
                            5.0 + 90.0 * done / total,
                            f"VeighNa 原生分钟回放 {done}/{total}",
                        )

        def cross_limit_order(self) -> None:
            for vt_orderid, order in list(self.active_limit_orders.items()):
                bar = self.bars.get(order.vt_symbol)
                if bar is None:
                    continue
                signal_dt = order.datetime
                if signal_dt is None or self.datetime - signal_dt != timedelta(minutes=1):
                    reason = "next_source_minute_not_contiguous"
                elif float(bar.volume) <= 0:
                    reason = "zero_volume_bar"
                elif float(bar.high_price) <= float(bar.low_price):
                    reason = "one_price_bar"
                elif self.datetime not in row_by_dt:
                    reason = "missing_native_source_bar"
                elif int(order.volume) % LOT_SIZE:
                    reason = "non_round_lot_quantity"
                elif order.direction != Direction.LONG:
                    reason = "unsupported_native_direction"
                elif float(order.price) <= 0:
                    reason = "invalid_native_limit_price"
                else:
                    participation = normalized["params"]["max_turnover_participation"]
                    conservative_limit_price = float(order.price)
                    capacity = (
                        math.floor(
                            float(bar.turnover)
                            * participation
                            / conservative_limit_price
                            / LOT_SIZE
                        )
                        * LOT_SIZE
                    )
                    reason = (
                        "all_or_none_turnover_capacity_exceeded"
                        if float(order.volume) > capacity
                        else ""
                    )
                if not reason:
                    continue
                status_reasons[vt_orderid] = reason
                self.active_limit_orders.pop(vt_orderid, None)
                order.status = Status.REJECTED
                self.strategy.update_order(order)
            super().cross_limit_order()

    engine = BoundedBacktestingEngine()
    datetimes = sorted(native_bars)
    first_dt = datetimes[0]
    last_dt = datetimes[-1]
    engine.set_parameters(
        vt_symbols=[symbol],
        interval=Interval.MINUTE,
        start=first_dt,
        end=last_dt,
        rates={symbol: normalized["commission_rate"]},
        slippages={symbol: 0.0},
        sizes={symbol: 1.0},
        priceticks={symbol: PRICE_TICK},
        capital=normalized["initial_cash"],
        annual_days=252,
    )
    engine.days = 0
    # VeighNa's warmup loop compares day-of-month values.  Set a prior-day
    # sentinel so its first warmup call breaks at index zero for every date.
    engine.datetime = first_dt - timedelta(days=1)
    engine.history_data = {
        (dt, symbol): bar for dt, bar in native_bars.items()
    }
    engine.dts = set(datetimes)

    class NativeBuyHoldAdapter(StrategyTemplate):
        def __init__(self, strategy_engine: Any, strategy_name: str, vt_symbols: list[str], setting: dict) -> None:
            super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
            self.symbol = setting["symbol"]
            self.core = BuyHoldStrategy(self.symbol)
            self.cash = float(setting["initial_cash"])
            self.params = setting["params"]

        def on_init(self) -> None:
            return

        def update_trade(self, trade: Any) -> None:
            super().update_trade(trade)
            if trade.direction == Direction.LONG:
                notional = float(trade.price) * float(trade.volume)
                commission = max(
                    notional * normalized["commission_rate"],
                    normalized["min_commission"],
                )
                stamp_duty = (
                    notional * normalized["stamp_duty_rate"]
                    if trade.direction == Direction.SHORT
                    else 0.0
                )
                slippage_cost = (
                    abs(notional) * normalized["slippage_bps"] / 10_000.0
                )
                self.cash -= notional + commission + stamp_duty + slippage_cost
                if self.cash < -1e-7:
                    raise RuntimeError("native fill exceeded the reserved cash budget")
                fee_by_order[trade.vt_orderid] = (
                    fee_by_order.get(trade.vt_orderid, 0.0) + commission
                )
                stamp_duty_by_order[trade.vt_orderid] = (
                    stamp_duty_by_order.get(trade.vt_orderid, 0.0) + stamp_duty
                )
                slippage_by_order[trade.vt_orderid] = (
                    slippage_by_order.get(trade.vt_orderid, 0.0) + slippage_cost
                )
            else:
                raise RuntimeError("buy_hold adapter produced an unexpected sell trade")

        def on_bars(self, bars: dict[str, Any]) -> None:
            if not self.trading:
                return
            native_bar = bars.get(self.symbol)
            if native_bar is None:
                return
            source_dt = native_bar.datetime
            row = row_by_dt[source_dt]
            pending_side = (
                "buy"
                if any(
                    item.vt_symbol == self.symbol
                    for item in engine.active_limit_orders.values()
                )
                else None
            )
            context = Context(
                datetime=pd.Timestamp(source_dt),
                cash=self.cash,
                positions=(
                    {self.symbol: {"quantity": self.get_pos(self.symbol), "available_to_sell": 0}}
                    if self.get_pos(self.symbol) > 0
                    else {}
                ),
                history={
                    self.symbol: pd.DataFrame(
                        [
                            {
                                "source_datetime": pd.Timestamp(source_dt),
                                "open": float(row.open),
                                "high": float(row.high),
                                "low": float(row.low),
                                "close": float(row.close),
                                "volume": float(row.volume),
                                "turnover": float(row.turnover),
                            }
                        ]
                    ).set_index("source_datetime", drop=False)
                },
                params={
                    "commission_rate": normalized["commission_rate"],
                    "min_commission": normalized["min_commission"],
                    "slippage_bps": normalized["slippage_bps"],
                    "target_fraction": self.params["target_fraction"],
                    "max_turnover_participation": self.params[
                        "max_turnover_participation"
                    ],
                    "_pending_side": pending_side,
                },
            )
            signal_bar = Bar(
                datetime=pd.Timestamp(source_dt),
                ohlcv={
                    self.symbol: {
                        "open": float(row.open),
                        "high": float(row.high),
                        "low": float(row.low),
                        "close": float(row.close),
                        "volume": float(row.volume),
                        "turnover": float(row.turnover),
                    }
                },
            )
            requests = self.core.on_bar(signal_bar, context) or []
            if not requests:
                return
            request = requests[0]
            requested_quantity = float(request.quantity)
            if (
                request.side != "buy"
                or request.symbol != self.symbol
                or not requested_quantity.is_integer()
                or int(requested_quantity) % LOT_SIZE
            ):
                raise RuntimeError("BuyHoldStrategy returned an unsupported native order")
            raw_limit = float(row.close) * (
                1.0 + normalized["slippage_bps"] / 10_000.0
            )
            limit_price = _floor_price_to_tick(raw_limit, PRICE_TICK)
            quantity = int(requested_quantity)
            slippage_rate = normalized["slippage_bps"] / 10_000.0
            while quantity > 0:
                max_notional = quantity * limit_price
                max_commission = max(
                    max_notional * normalized["commission_rate"],
                    normalized["min_commission"],
                )
                max_slippage = abs(max_notional) * slippage_rate
                if max_notional + max_commission + max_slippage <= self.cash + 1e-8:
                    break
                quantity -= LOT_SIZE
            if quantity <= 0:
                return
            order_ids = self.buy(
                self.symbol,
                round_to(limit_price, PRICE_TICK),
                quantity,
            )
            for order_id in order_ids:
                signal_meta[order_id] = {
                    "signal_datetime": pd.Timestamp(source_dt).isoformat(),
                    "signal_bar_start": pd.Timestamp(row.datetime).isoformat(),
                    "reference_price": float(row.close),
                    "quantity": quantity,
                }

    engine.add_strategy(
        NativeBuyHoldAdapter,
        {
            "symbol": symbol,
            "initial_cash": normalized["initial_cash"],
            "params": normalized["params"],
        },
    )

    if progress:
        progress(5.0, "正在使用 VeighNa 原生 BacktestingEngine 回放")
    isolated_before = _isolated_trader_snapshot()
    try:
        try:
            engine.run_backtesting()
        except _NativeCancellation:
            native_cancelled = True
        if native_cancelled:
            for vt_orderid in list(engine.active_limit_orders):
                status_reasons[vt_orderid] = "run_cancelled_before_next_contiguous_bar"
                engine.cancel_order(engine.strategy, vt_orderid)
        elif processed_datetimes != datetimes:
            raise RuntimeError(
                "VeighNa native replay stopped before processing every supplied minute bar"
            )
        native_daily = engine.calculate_result()
    finally:
        isolated_after = _isolated_trader_snapshot()
        if isolated_before != isolated_after:
            raise RuntimeError(
                "VeighNa changed files in its isolated .vntrader directory; refusing result"
            )
    if getattr(api["database_module"], "database", None) is not None:
        raise RuntimeError("VeighNa instantiated a database during native replay")

    for vt_orderid in list(engine.active_limit_orders):
        status_reasons[vt_orderid] = "no_later_bar_before_range_end"
        engine.cancel_order(engine.strategy, vt_orderid)

    native_orders = engine.get_all_orders()
    native_trades = engine.get_all_trades()
    processed_set = set(processed_datetimes)
    if any(trade.datetime not in processed_set for trade in native_trades):
        raise RuntimeError("VeighNa returned a fill outside the processed minute set")
    order_by_raw_id = {order.orderid: order for order in native_orders}
    trade_by_raw_order: dict[str, list[Any]] = {}
    for trade in native_trades:
        trade_by_raw_order.setdefault(trade.orderid, []).append(trade)
    if len({trade.vt_tradeid for trade in native_trades}) != len(native_trades):
        raise RuntimeError("VeighNa returned duplicate native trade identities")

    order_rows: list[dict[str, Any]] = []
    for order in sorted(native_orders, key=lambda item: int(item.orderid)):
        related = trade_by_raw_order.get(order.orderid, [])
        filled = sum(float(item.volume) for item in related)
        if abs(filled - float(order.traded)) > 1e-8:
            raise RuntimeError(
                f"native order/trade volume mismatch for {order.vt_orderid}"
            )
        meta = signal_meta.get(order.vt_orderid, {})
        weighted_notional = sum(float(item.price) * float(item.volume) for item in related)
        order_rows.append(
            {
                "order_id": order.vt_orderid,
                "native_order_id": order.vt_orderid,
                "native_order_raw_id": order.orderid,
                "native_status": order.status.name,
                "symbol": order.vt_symbol,
                "side": "buy" if order.direction == Direction.LONG else "sell",
                "signal_datetime": meta.get(
                    "signal_datetime",
                    order.datetime.isoformat() if order.datetime else None,
                ),
                "signal_bar_start": meta.get("signal_bar_start"),
                "limit_price": float(order.price),
                "requested_quantity": int(order.volume),
                "filled_quantity": int(filled),
                "remaining_quantity": int(max(0.0, float(order.volume) - filled)),
                "average_fill_price": weighted_notional / filled if filled else None,
                "fill_notional_total": weighted_notional,
                "commission_total": fee_by_order.get(order.vt_orderid, 0.0),
                "slippage_cost_total": slippage_by_order.get(order.vt_orderid, 0.0),
                "stamp_duty_total": stamp_duty_by_order.get(order.vt_orderid, 0.0),
                "status": _status_name(order.status, Status),
                "reason": status_reasons.get(order.vt_orderid),
            }
        )

    trades_rows: list[dict[str, Any]] = []
    for trade in sorted(native_trades, key=lambda item: (item.datetime, item.vt_tradeid)):
        order = order_by_raw_id.get(trade.orderid)
        if order is None:
            raise RuntimeError(f"native trade {trade.vt_tradeid} has no native order")
        meta = signal_meta.get(order.vt_orderid, {})
        notional = float(trade.price) * float(trade.volume)
        commission = max(
            notional * normalized["commission_rate"], normalized["min_commission"]
        )
        stamp_duty = (
            notional * normalized["stamp_duty_rate"]
            if trade.direction == Direction.SHORT
            else 0.0
        )
        slippage_cost = (
            abs(notional) * normalized["slippage_bps"] / 10_000.0
        )
        trades_rows.append(
            {
                "order_id": order.vt_orderid,
                "native_order_id": order.vt_orderid,
                "native_order_raw_id": order.orderid,
                "native_trade_id": trade.vt_tradeid,
                "symbol": trade.vt_symbol,
                "side": "buy" if trade.direction == Direction.LONG else "sell",
                "signal_datetime": meta.get("signal_datetime"),
                "fill_datetime": trade.datetime.isoformat(),
                "datetime": trade.datetime.isoformat(),
                "quantity": int(trade.volume),
                "price": float(trade.price),
                "notional": notional,
                "commission": commission,
                "stamp_duty": stamp_duty,
                "slippage_cost": slippage_cost,
                "reference_price": float(meta.get("reference_price", trade.price)),
            }
        )

    # Reconstruct only the marked cash/position ledger from native fills.  The
    # fills themselves remain the raw VeighNa trades above; no reference matcher
    # is used to create or alter them.
    prepared = prepared.loc[
        prepared["source_datetime"].map(lambda value: value.to_pydatetime()).isin(
            set(processed_datetimes)
        )
    ].reset_index(drop=True)
    trades_by_time: dict[datetime, list[dict[str, Any]]] = {}
    for trade in trades_rows:
        trade_dt = pd.Timestamp(trade["fill_datetime"]).to_pydatetime()
        trades_by_time.setdefault(trade_dt, []).append(trade)

    cash = float(normalized["initial_cash"])
    quantity = 0
    cost_basis = 0.0
    equity_rows: list[dict[str, Any]] = [
        {
            "datetime": f"{normalized['start']} 00:00:00",
            "source_datetime": None,
            "cash": cash,
            "quantity": 0,
            "equity": cash,
        }
    ]
    bars_rows: list[dict[str, Any]] = []
    last_close: float | None = None
    entry_date = None
    for row in prepared.itertuples(index=False):
        source_dt = pd.Timestamp(row.source_datetime)
        bar_start = pd.Timestamp(row.datetime)
        last_close = float(row.close)
        for trade in trades_by_time.get(source_dt.to_pydatetime(), []):
            if trade["side"] != "buy":
                raise RuntimeError("native buy_hold unexpectedly produced a sell fill")
            debit = (
                float(trade["notional"])
                + float(trade["commission"])
                + float(trade["stamp_duty"])
                + float(trade["slippage_cost"])
            )
            old_cost = cost_basis
            cash -= debit
            quantity += int(trade["quantity"])
            cost_basis = old_cost + debit
            entry_date = source_dt.date()
        if cash < -1e-7:
            raise RuntimeError("native buy_hold cash guard was violated")
        cash = max(0.0, cash)
        bars_rows.append(
            {
                "datetime": bar_start.isoformat(),
                "bar_start_datetime": bar_start.isoformat(),
                "source_datetime": source_dt.isoformat(),
                "symbol": symbol,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "turnover": float(row.turnover),
            }
        )
        equity_rows.append(
            {
                "datetime": source_dt.isoformat(),
                "source_datetime": source_dt.isoformat(),
                "bar_start_datetime": bar_start.isoformat(),
                "cash": cash,
                "quantity": quantity,
                "equity": cash + quantity * last_close,
            }
        )

    final_position = None
    if quantity > 0 and last_close is not None:
        final_position = {
            "symbol": symbol,
            "quantity": quantity,
            "avg_cost": cost_basis / quantity,
            "market_value": quantity * last_close,
            "unrealized_pnl": quantity * last_close - cost_basis,
            "available_to_sell": None,
            "entry_date": entry_date.isoformat() if entry_date else None,
            "t_plus_one_verified": False,
        }

    data_manifest = dict(data_manifest)
    data_manifest.update(
        {
            "backend": "vnpy",
            "native_engine": "vnpy_portfoliostrategy.backtesting.BacktestingEngine",
            "native_engine_versions": api["versions"],
            "native_history_data_rows": len(engine.history_data),
            "native_dts_count": len(engine.dts),
            "native_order_ids": [order["native_order_id"] for order in order_rows],
            "native_trade_ids": [trade["native_trade_id"] for trade in trades_rows],
            "data_injection": "in-memory history_data/dts; BacktestingEngine.load_data not called",
            "database_instance_created": False,
            "settings_isolation": "temporary empty .vntrader/vt_setting.json; existing user settings not loaded",
            "execution_contract": "native VeighNa limit matcher after next-minute, gap, zero-volume, one-price, turnover-capacity, cash and round-lot guards",
            "capacity_proxy": "all-or-none cap = vendor bar turnover * max_turnover_participation / native limit price, rounded down to 100 shares; turnover units unverified",
            "native_daily_result_rows": len(native_daily) if native_daily is not None else 0,
            "native_order_trade_identity_verified": True,
            "reference_matcher_equivalence": "not_asserted",
            "all_or_none_turnover_capacity_rejections": sum(
                1
                for order in order_rows
                if order["reason"] == "all_or_none_turnover_capacity_exceeded"
            ),
            "slippage_cost_accounting": "separate_cash_debit",
            "cancelled": bool(native_cancelled),
        }
    )
    data_manifest["minimum_commission_applied_to_native_fills"] = True
    data_manifest["stamp_duty_rate"] = normalized["stamp_duty_rate"]

    result = research_engine._result(
        normalized,
        equity_rows,
        order_rows,
        trades_rows,
        final_position,
        bars_rows,
        data_manifest,
        cancelled=bool(native_cancelled),
    )
    result["limitations"] = [
        "RESEARCH_ONLY; no paper or live admission is implied.",
        "This backend is restricted to buy_hold, one main-board stock, 1-minute bars, 2024-2025 training data, and at most 31 calendar days.",
        "VeighNa native limit-order fills are reported from its actual BacktestingEngine orders and trades; no equivalence with the local reference matcher is claimed.",
        "Buy limits are capped at signal close plus configured slippage, rounded down to the 0.01 price tick; VeighNa may fill at a better native open price.",
        "A next source-time gap, zero-volume bar, one-price bar, non-round-lot order, or turnover-proxy capacity excess rejects the entire native order before VeighNa matching.",
        "Vendor turnover units are unverified; the all-or-none participation check is only a conservative research proxy, not certified market capacity. Volume is used only to reject zero-volume fill bars.",
        "The native raw fill price is retained. Minimum commission and abs(native notional) * slippage_bps / 10000 are separately debited in the marked cash/equity ledger; buy_hold makes no sale, so sell stamp duty is reported but not charged.",
        "A-share T+1 sell eligibility is unverified because buy_hold does not sell.",
        "Minute price adjustment vintage, share-volume semantics, point-in-time ST status, limit-price/suspension state, and corporate-action completeness remain unverified.",
        "Exchange fees, transfer fees, borrow costs, broker routing, and live order behavior are not modeled.",
    ]
    result["metrics"].update(
        {
            "native_order_count": len(order_rows),
            "native_trade_count": len(trades_rows),
            "native_engine": data_manifest["native_engine"],
            "slippage_paid": sum(
                float(trade["slippage_cost"]) for trade in trades_rows
            ),
        }
    )
    if progress:
        progress(
            100.0 if not native_cancelled else 5.0 + 90.0 * len(processed_datetimes) / max(1, len(datetimes)),
            "VeighNa 原生回放已取消" if native_cancelled else "VeighNa 原生回放完成",
        )
    return result


def _isolated_trader_snapshot() -> tuple[tuple[str, int, int, int, int], ...]:
    """Stat only isolated VeighNa files; never read their contents."""
    if _NATIVE_TRADER_DIR is None:
        return ()
    entries = []
    for path in sorted(_NATIVE_TRADER_DIR.rglob("*")):
        stat_result = path.stat()
        entries.append(
            (
                path.relative_to(_NATIVE_TRADER_DIR).as_posix(),
                stat_result.st_mode,
                stat_result.st_size,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
            )
        )
    return tuple(entries)


def run_backtest(
    config: dict[str, Any],
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one bounded buy-and-hold study using VeighNa's native minute engine."""
    normalized = research_engine.validate_config(config)
    if normalized["strategy"] != "buy_hold":
        raise ValueError("the VeighNa backend currently supports buy_hold only")
    if cancelled and cancelled():
        manifest = {
            "read_only": True,
            "research_only": True,
            "backend": "vnpy",
            "requested_range_inclusive": [normalized["start"], normalized["end"]],
            "requested_months_only": [],
            "used_source_files": [],
            "native_engine": "not_started",
        }
        initial = {
            "datetime": f"{normalized['start']} 00:00:00",
            "source_datetime": None,
            "cash": normalized["initial_cash"],
            "quantity": 0,
            "equity": normalized["initial_cash"],
        }
        return research_engine._result(
            normalized,
            [initial],
            [],
            [],
            None,
            [],
            manifest,
            cancelled=True,
        )
    if progress:
        progress(0.0, "正在读取指定训练期分钟数据")
    frame, manifest = research_engine._load_training_bars(normalized)
    if cancelled and cancelled():
        initial = {
            "datetime": f"{normalized['start']} 00:00:00",
            "source_datetime": None,
            "cash": normalized["initial_cash"],
            "quantity": 0,
            "equity": normalized["initial_cash"],
        }
        manifest = dict(manifest)
        manifest.update({"backend": "vnpy", "native_engine": "not_started"})
        return research_engine._result(
            normalized,
            [initial],
            [],
            [],
            None,
            [],
            manifest,
            cancelled=True,
        )
    return _native_run(normalized, frame, manifest, progress, cancelled)
