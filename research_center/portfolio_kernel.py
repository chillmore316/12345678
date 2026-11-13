"""Incremental, shared-cash replay for a bounded portfolio of main-board stocks.

The state is JSON serializable and checksummed to catch accidental edits.  This
is a research primitive, not an authenticated ledger or a paper-trading engine.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any

import numpy as np
import pandas as pd

from strategy.base import Bar, Context
from . import replay_kernel
from .engine import LOT_SIZE, MAX_ENGINE_ROWS, _reject, _round_adverse_price
from .strategies import BuyHoldStrategy, SmaCrossStrategy


STATE_SCHEMA_VERSION = 1
ENGINE_ID = "research_center.portfolio.incremental.minute.v1"
MAX_SYMBOLS = 4
MIN_SYMBOLS = 2
_STATE_FIELDS = {
    "schema_version", "engine", "mode", "paper_admission", "config_sha256",
    "state_sha256", "rows_processed", "cash", "positions", "pending_order_ids",
    "last_source_datetime", "last_bar_start_datetime", "strategy_history",
    "orders", "trades", "equity", "bars", "fee_total", "finalized", "cancelled",
}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("state/config/frames 必须是有限 JSON 数据") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _state_digest(state: dict[str, Any]) -> str:
    return _digest({key: value for key, value in state.items() if key != "state_sha256"})


def _seal(state: dict[str, Any]) -> dict[str, Any]:
    state["state_sha256"] = _state_digest(state)
    return state


def _finite_tree(value: Any, path: str = "state") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 含非有限数值")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path} 含非字符串键")
        for key, item in value.items():
            _finite_tree(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} 含非 JSON 类型")


def normalize_portfolio(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the portfolio and return a canonical config.

    ``params.target_fraction`` is the portfolio-wide allocation ceiling.  It
    defaults to the single-stock engine's 0.5.  ``allocation_weights`` values
    are fractions of total cash and may sum to less than that ceiling.
    """
    from .engine import _normalize_stock_symbol, validate_config

    if not isinstance(config, dict):
        raise ValueError("config 必须是字典")
    raw_symbols = config.get("symbols")
    if not isinstance(raw_symbols, list) or not MIN_SYMBOLS <= len(raw_symbols) <= MAX_SYMBOLS:
        raise ValueError("组合需要 2 至 4 个 symbols")
    symbols = [_normalize_stock_symbol(item) for item in raw_symbols]
    if len(set(symbols)) != len(symbols):
        raise ValueError("组合 symbols 不能重复")
    strategy_name = str(config.get("strategy", "buy_hold")).lower()
    if strategy_name not in {"buy_hold", "sma_cross"}:
        raise ValueError("组合 strategy 只能是 buy_hold 或 sma_cross")

    raw_params = config.get("params", {})
    if not isinstance(raw_params, dict):
        raise ValueError("params 必须是字典")
    # Let the established validator own the allowed strategy/cost/date fields.
    # It also normalizes legacy fast_window/slow_window aliases.
    first_raw = {key: value for key, value in config.items() if key not in {"symbols", "allocation_weights", "symbol_configs"}}
    first_raw["symbol"] = symbols[0]
    base = validate_config(first_raw)
    budget = float(base["params"]["target_fraction"])

    raw_weights = config.get("allocation_weights")
    if raw_weights is None:
        weights = {symbol: budget / len(symbols) for symbol in symbols}
    else:
        if not isinstance(raw_weights, dict):
            raise ValueError("allocation_weights 必须是 symbol 到比例的字典")
        normalized_weights: dict[str, float] = {}
        for raw_symbol, raw_weight in raw_weights.items():
            symbol = _normalize_stock_symbol(raw_symbol)
            if symbol in normalized_weights:
                raise ValueError("allocation_weights 含重复规范化 symbol")
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
                raise ValueError(f"allocation_weights[{symbol}] 必须是有限数值")
            weight = float(raw_weight)
            if not math.isfinite(weight) or weight <= 0 or weight > 1:
                raise ValueError(f"allocation_weights[{symbol}] 必须在 (0, 1] 内")
            normalized_weights[symbol] = weight
        if set(normalized_weights) != set(symbols):
            raise ValueError("allocation_weights 必须恰好覆盖全部 symbols")
        if sum(normalized_weights.values()) > budget + 1e-12:
            raise ValueError("allocation_weights 合计不能超过 params.target_fraction 总预算")
        weights = {symbol: normalized_weights[symbol] for symbol in symbols}

    symbol_configs = []
    for symbol in symbols:
        one = dict(first_raw)
        one["symbol"] = symbol
        one_params = dict(base["params"])
        one_params["target_fraction"] = weights[symbol]
        one["params"] = one_params
        symbol_configs.append(validate_config(one))

    normalized = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine": ENGINE_ID,
        "symbols": symbols,
        "symbol_configs": symbol_configs,
        "start": base["start"],
        "end": base["end"],
        "interval": base["interval"],
        "strategy": base["strategy"],
        "initial_cash": base["initial_cash"],
        "commission_rate": base["commission_rate"],
        "min_commission": base["min_commission"],
        "slippage_bps": base["slippage_bps"],
        "stamp_duty_rate": base["stamp_duty_rate"],
        "data_root": base["data_root"],
        "params": dict(base["params"]),
        "allocation_weights": weights,
        "allocation_order": list(symbols),
    }
    _canonical_json(normalized)
    return normalized


def _stamp(value: Any, field: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field} 必须是有效的无时区分钟时间戳") from exc
    if pd.isna(result) or result.tzinfo is not None or result.second or result.microsecond or result.nanosecond:
        raise ValueError(f"{field} 必须是有效的无时区整分钟时间戳")
    return result


def _canonical_frames(config: dict[str, Any], frames: Any, previous: str | None) -> list[dict[str, dict[str, Any]]]:
    if not isinstance(frames, list):
        raise ValueError("frames 必须是完整组合分钟帧数组")
    if len(frames) > MAX_ENGINE_ROWS:
        raise ValueError(f"组合分钟帧超过上限 {MAX_ENGINE_ROWS}")
    symbols = config["symbols"]
    symbol_configs = {item["symbol"]: item for item in config["symbol_configs"]}
    prior_time = _stamp(previous, "state.last_source_datetime") if previous is not None else None
    result = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"frames[{index}] 必须是 symbol 到 bar 的映射")
        rows = frame.get("bars") if "bars" in frame else frame
        if not isinstance(rows, dict) or set(rows) != set(symbols):
            raise ValueError(f"frames[{index}] 必须且只能包含全部配置 symbols")
        normalized: dict[str, dict[str, Any]] = {}
        axis: tuple[pd.Timestamp, pd.Timestamp] | None = None
        for symbol in symbols:
            payload = rows[symbol]
            if not isinstance(payload, dict):
                raise ValueError(f"frames[{index}][{symbol}] 必须是 bar 字典")
            canonical = replay_kernel._canonical_bar(symbol_configs[symbol], payload)
            current = (_stamp(canonical["bar_start_datetime"], "bar_start_datetime"),
                       _stamp(canonical["source_datetime"], "source_datetime"))
            if axis is None:
                axis = current
            elif current != axis:
                raise ValueError(f"frames[{index}] 标的 bar_start/source_time 不一致")
            normalized[symbol] = canonical
        assert axis is not None
        if prior_time is not None and axis[1] <= prior_time:
            raise ValueError("frames 必须严格晚于 state 且按 source_datetime 递增；不允许重复或乱序")
        prior_time = axis[1]
        result.append(normalized)
    return result


def _position_view(position: dict[str, Any], trade_day: date) -> dict[str, Any]:
    quantity = int(position["quantity"])
    sellable = sum(
        int(lot["quantity"])
        for lot in position["lots"]
        if date.fromisoformat(lot["entry_date"]) < trade_day
    )
    return {
        "quantity": quantity,
        "avg_cost": float(position["average_cost"]),
        "available_to_sell": sellable,
    } if quantity else {}


def _recompute_average_cost(position: dict[str, Any]) -> None:
    quantity = sum(int(lot["quantity"]) for lot in position["lots"])
    position["quantity"] = quantity
    position["average_cost"] = (
        sum(float(lot["unit_cost"]) * int(lot["quantity"]) for lot in position["lots"]) / quantity
        if quantity else 0.0
    )


def _sell_lots(position: dict[str, Any], quantity: int, trade_day: date) -> float:
    remaining = quantity
    basis = 0.0
    kept = []
    for lot in position["lots"]:
        lot_quantity = int(lot["quantity"])
        if remaining and date.fromisoformat(lot["entry_date"]) < trade_day:
            sold = min(lot_quantity, remaining)
            basis += sold * float(lot["unit_cost"])
            lot_quantity -= sold
            remaining -= sold
        if lot_quantity:
            kept.append({**lot, "quantity": lot_quantity})
    if remaining:
        raise RuntimeError("sellable lot invariant violated")
    position["lots"] = kept
    _recompute_average_cost(position)
    return basis


def _pending(state: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    order_id = state["pending_order_ids"][symbol]
    if order_id is None:
        return None
    for order in reversed(state["orders"]):
        if order["order_id"] == order_id:
            return order
    raise ValueError("pending order 不存在于 orders")


def _reject_pending(state: dict[str, Any], symbol: str, order: dict[str, Any], reason: str) -> None:
    _reject(order, reason)
    state["pending_order_ids"][symbol] = None


def _capacity_quantity(turnover: float, participation: float, price: float) -> int:
    """Return whole-lot turnover capacity without binary-float boundary loss."""
    if turnover <= 0 or price <= 0:
        return 0
    # _round_adverse_price can return e.g. 10.200000000000001 for an intended
    # cent tick. Normalize that representation before evaluating the boundary.
    cent_price = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raw_shares = Decimal(str(turnover)) * Decimal(str(participation)) / cent_price
    lots = (raw_shares / Decimal(LOT_SIZE)).to_integral_value(rounding=ROUND_FLOOR)
    return int(lots) * LOT_SIZE


def _attempt_fill(config: dict[str, Any], state: dict[str, Any], symbol: str,
                  bar: dict[str, Any], sale_filled: dict[str, bool]) -> None:
    order = _pending(state, symbol)
    if order is None:
        return
    source_time = pd.Timestamp(bar["source_datetime"])
    trade_day = source_time.date()
    position = state["positions"][symbol]
    gap = (source_time - pd.Timestamp(order["last_attempt_datetime"])).total_seconds() / 60.0
    if gap != 1.0:
        _reject_pending(state, symbol, order, "next_source_minute_not_contiguous")
        return
    if bar["volume"] <= 0:
        _reject_pending(state, symbol, order, "zero_volume_bar")
        return
    if bar["high"] <= bar["low"]:
        _reject_pending(state, symbol, order, "one_price_bar")
        return
    sellable = sum(int(lot["quantity"]) for lot in position["lots"]
                   if date.fromisoformat(lot["entry_date"]) < trade_day)
    if order["side"] == "sell" and sellable <= 0:
        _reject_pending(state, symbol, order, "T+1_sell_restriction")
        return

    side = order["side"]
    reference = bar["high"] if side == "buy" else bar["low"]
    fill_price = float(Decimal(str(_round_adverse_price(reference, side, config["slippage_bps"])))
                       .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    participation = config["params"]["max_turnover_participation"]
    capacity = _capacity_quantity(float(bar["turnover"]), float(participation), float(fill_price))
    fill_quantity = min(int(order["remaining_quantity"]), capacity)
    fill_quantity = (fill_quantity // LOT_SIZE) * LOT_SIZE
    if side == "sell":
        fill_quantity = min(fill_quantity, sellable)
        fill_quantity = (fill_quantity // LOT_SIZE) * LOT_SIZE
    else:
        while fill_quantity > 0:
            notional = fill_quantity * fill_price
            commission = max(notional * config["commission_rate"], config["min_commission"])
            if notional + commission <= state["cash"] + 1e-9:
                break
            fill_quantity -= LOT_SIZE

    if fill_quantity <= 0:
        order["reason"] = "no_cash_or_turnover_capacity"
        order["status"] = "pending"
        order["last_attempt_datetime"] = source_time.isoformat()
        return

    notional = fill_quantity * fill_price
    commission = max(notional * config["commission_rate"], config["min_commission"])
    stamp_duty = notional * config["stamp_duty_rate"] if side == "sell" else 0.0
    slippage_cost = abs(fill_price - reference) * fill_quantity
    adverse_range_cost = (max(0.0, fill_price - bar["open"]) if side == "buy"
                          else max(0.0, bar["open"] - fill_price)) * fill_quantity
    realized_basis = None
    if side == "buy":
        state["cash"] -= notional + commission
        position["lots"].append({
            "quantity": fill_quantity,
            "entry_date": trade_day.isoformat(),
            "unit_cost": (notional + commission) / fill_quantity,
        })
        _recompute_average_cost(position)
    else:
        realized_basis = _sell_lots(position, fill_quantity, trade_day)
        state["cash"] += notional - commission - stamp_duty
        sale_filled[symbol] = True
    if state["cash"] < -1e-7:
        raise RuntimeError("shared cash invariant violated")
    state["cash"] = max(0.0, state["cash"])
    state["fee_total"] += commission + stamp_duty
    state["trades"].append({
        "order_id": order["order_id"], "symbol": symbol, "side": side,
        "signal_datetime": order["signal_datetime"],
        "fill_datetime": source_time.isoformat(), "datetime": source_time.isoformat(),
        "quantity": fill_quantity, "price": fill_price, "notional": notional,
        "commission": commission, "stamp_duty": stamp_duty,
        "slippage_cost": slippage_cost,
        "adverse_range_cost_vs_open": adverse_range_cost,
        "turnover_participation": notional / bar["turnover"] if bar["turnover"] > 0 else None,
        "realized_pnl": notional - commission - stamp_duty - realized_basis if side == "sell" else None,
    })
    order["filled_quantity"] += fill_quantity
    order["remaining_quantity"] -= fill_quantity
    order["commission_total"] += commission
    order["stamp_duty_total"] += stamp_duty
    order["fill_notional_total"] += notional
    order["average_fill_price"] = order["fill_notional_total"] / order["filled_quantity"]
    order["reason"] = None
    if order["remaining_quantity"] == 0:
        order["status"] = "filled"
        state["pending_order_ids"][symbol] = None
    else:
        order["status"] = "partial_filled"
        order["reason"] = "turnover_or_cash_capacity_remainder"
        order["last_attempt_datetime"] = source_time.isoformat()


def _history_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    fields = ("open", "high", "low", "close", "volume", "turnover")
    result = pd.DataFrame(
        np.asarray([[item[key] for key in fields] for item in rows], dtype=float),
        columns=pd.Index(fields, dtype=object),
        index=pd.DatetimeIndex([pd.Timestamp(item["source_datetime"]) for item in rows], name="source_datetime"),
    )
    result["source_datetime"] = result.index
    result["datetime"] = pd.DatetimeIndex([pd.Timestamp(item["datetime"]) for item in rows])
    return result


def _advance_frame(config: dict[str, Any], state: dict[str, Any], frame: dict[str, dict[str, Any]]) -> None:
    symbols = config["symbols"]
    source_time = pd.Timestamp(frame[symbols[0]]["source_datetime"])
    bar_start = pd.Timestamp(frame[symbols[0]]["bar_start_datetime"])
    state["rows_processed"] += 1
    state["last_source_datetime"] = source_time.isoformat()
    state["last_bar_start_datetime"] = bar_start.isoformat()
    state["bars"].append(frame)
    history_limit = config["params"]["slow"] + 1
    for symbol in symbols:
        history = state["strategy_history"][symbol]
        history.append(frame[symbol])
        if len(history) > history_limit:
            del history[:len(history) - history_limit]

    sale_filled = {symbol: False for symbol in symbols}
    # Stable config order is the explicit same-minute cash allocation priority.
    for symbol in symbols:
        _attempt_fill(config, state, symbol, frame[symbol], sale_filled)

    # All strategies see the same post-matching cash and position snapshot.
    cash_snapshot = float(state["cash"])
    trade_day = source_time.date()
    position_snapshot = {
        symbol: _position_view(state["positions"][symbol], trade_day)
        for symbol in symbols
    }
    history_snapshot = {
        symbol: _history_frame(state["strategy_history"][symbol])
        for symbol in symbols if state["strategy_history"][symbol]
    }
    params_base = {
        "commission_rate": config["commission_rate"],
        "min_commission": config["min_commission"],
        "slippage_bps": config["slippage_bps"],
        "stamp_duty_rate": config["stamp_duty_rate"],
        "max_turnover_participation": config["params"]["max_turnover_participation"],
        "fast": config["params"]["fast"],
        "slow": config["params"]["slow"],
    }
    for symbol in symbols:
        if state["pending_order_ids"][symbol] is not None:
            continue
        params = {
            **params_base,
            "target_fraction": config["allocation_weights"][symbol],
            "_pending_side": None,
            "_sale_filled_this_bar": sale_filled[symbol],
        }
        context = Context(
            datetime=source_time,
            cash=cash_snapshot,
            positions=deepcopy(position_snapshot),
            history=history_snapshot,
            params=params,
        )
        bar_payload = frame[symbol]
        bar = Bar(
            datetime=source_time,
            ohlcv={symbol: {key: bar_payload[key] for key in ("open", "high", "low", "close", "volume", "turnover")}},
        )
        if config["strategy"] == "buy_hold":
            strategy = BuyHoldStrategy(symbol)
        else:
            strategy = SmaCrossStrategy(symbol, config["params"]["fast"], config["params"]["slow"])
        for request in strategy.on_bar(bar, context) or []:
            if getattr(request, "symbol", None) != symbol or getattr(request, "side", None) not in {"buy", "sell"}:
                continue
            raw_quantity = getattr(request, "quantity", 0)
            try:
                numeric_quantity = float(raw_quantity)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric_quantity) or numeric_quantity <= 0:
                continue
            quantity = int(numeric_quantity // LOT_SIZE) * LOT_SIZE
            if quantity <= 0:
                continue
            order_id = f"P{len(state['orders']) + 1:06d}"
            state["orders"].append({
                "order_id": order_id, "symbol": symbol, "side": request.side,
                "signal_datetime": source_time.isoformat(),
                "signal_bar_start": bar_start.isoformat(),
                "requested_quantity": quantity, "filled_quantity": 0,
                "remaining_quantity": quantity, "average_fill_price": None,
                "fill_notional_total": 0.0, "commission_total": 0.0,
                "stamp_duty_total": 0.0, "status": "pending",
                "reason": "awaiting_next_contiguous_minute",
                "last_attempt_datetime": source_time.isoformat(),
            })
            state["pending_order_ids"][symbol] = order_id
            break

    total_market_value = sum(state["positions"][symbol]["quantity"] * frame[symbol]["close"] for symbol in symbols)
    equity = state["cash"] + total_market_value
    state["equity"].append({
        "datetime": source_time.isoformat(), "source_datetime": source_time.isoformat(),
        "bar_start_datetime": bar_start.isoformat(), "cash": state["cash"],
        "positions": {symbol: state["positions"][symbol]["quantity"] for symbol in symbols},
        "market_value": total_market_value, "equity": equity,
    })


def initial_state(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_portfolio(config)
    cash = float(normalized["initial_cash"])
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine": ENGINE_ID,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "config_sha256": _digest(normalized),
        "state_sha256": "",
        "rows_processed": 0,
        "cash": cash,
        "positions": {symbol: {"quantity": 0, "average_cost": 0.0, "lots": []} for symbol in normalized["symbols"]},
        "pending_order_ids": {symbol: None for symbol in normalized["symbols"]},
        "last_source_datetime": None,
        "last_bar_start_datetime": None,
        "strategy_history": {symbol: [] for symbol in normalized["symbols"]},
        "orders": [],
        "trades": [],
        "equity": [{
            "datetime": f"{normalized['start']}T00:00:00", "source_datetime": None,
            "bar_start_datetime": None, "cash": cash,
            "positions": {symbol: 0 for symbol in normalized["symbols"]},
            "market_value": 0.0, "equity": cash,
        }],
        "bars": [],
        "fee_total": 0.0,
        "finalized": False,
        "cancelled": False,
    }
    _seal(state)
    _validate_state(normalized, state)
    return state


def _validate_state(config: dict[str, Any], state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
        raise ValueError("portfolio state schema fields 不匹配")
    if not isinstance(state.get("state_sha256"), str) or state["state_sha256"] != _state_digest(state):
        raise ValueError("state_sha256 校验失败；state 可能已被意外修改")
    _finite_tree(state)
    if (isinstance(state["schema_version"], bool) or not isinstance(state["schema_version"], int)
            or state["schema_version"] != STATE_SCHEMA_VERSION or state["engine"] != ENGINE_ID):
        raise ValueError("不支持的 portfolio state 版本")
    if state["mode"] != "RESEARCH_ONLY" or state["paper_admission"] is not False:
        raise ValueError("portfolio state 必须保持 RESEARCH_ONLY 且 paper_admission=false")
    if state["config_sha256"] != _digest(config):
        raise ValueError("state 与当前组合 config 不匹配")
    rows = state["rows_processed"]
    if isinstance(rows, bool) or not isinstance(rows, int) or not 0 <= rows <= MAX_ENGINE_ROWS:
        raise ValueError("rows_processed 超过上限或格式无效")
    cash = state["cash"]
    if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(float(cash)) or cash < 0:
        raise ValueError("cash 必须是有限非负数")
    symbols = config["symbols"]
    if set(state["positions"]) != set(symbols) or set(state["pending_order_ids"]) != set(symbols):
        raise ValueError("state positions/pending symbols 与 config 不匹配")
    if set(state["strategy_history"]) != set(symbols):
        raise ValueError("strategy_history symbols 与 config 不匹配")
    for symbol in symbols:
        position = state["positions"][symbol]
        if set(position) != {"quantity", "average_cost", "lots"} or not isinstance(position["lots"], list):
            raise ValueError(f"{symbol} position schema 无效")
        quantity = position["quantity"]
        avg_cost = position["average_cost"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0 or quantity % LOT_SIZE:
            raise ValueError(f"{symbol} quantity 必须是非负整手")
        if isinstance(avg_cost, bool) or not isinstance(avg_cost, (int, float)) or not math.isfinite(float(avg_cost)) or avg_cost < 0:
            raise ValueError(f"{symbol} average_cost 必须是有限非负数")
        lot_total = 0
        weighted_cost = 0.0
        for lot in position["lots"]:
            if set(lot) != {"quantity", "entry_date", "unit_cost"}:
                raise ValueError(f"{symbol} lot schema 无效")
            lot_quantity = lot["quantity"]
            unit_cost = lot["unit_cost"]
            try:
                entry_day = date.fromisoformat(lot["entry_date"])
            except (TypeError, ValueError):
                raise ValueError(f"{symbol} lot entry_date 无效") from None
            if (isinstance(lot_quantity, bool) or not isinstance(lot_quantity, int) or lot_quantity <= 0
                    or lot_quantity % LOT_SIZE or isinstance(unit_cost, bool)
                    or not isinstance(unit_cost, (int, float)) or not math.isfinite(float(unit_cost)) or unit_cost <= 0):
                raise ValueError(f"{symbol} lot quantity/unit_cost 无效")
            if state["last_source_datetime"] is not None and entry_day > _stamp(state["last_source_datetime"], "last_source_datetime").date():
                raise ValueError(f"{symbol} lot entry_date 晚于 state 时间")
            lot_total += lot_quantity
            weighted_cost += lot_quantity * float(unit_cost)
        expected_cost = weighted_cost / lot_total if lot_total else 0.0
        if quantity != lot_total or not math.isclose(float(avg_cost), expected_cost, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError(f"{symbol} quantity/average_cost 与批次不一致")
        if (quantity == 0) != (not position["lots"]):
            raise ValueError(f"{symbol} 空仓与批次不一致")
    if not isinstance(state["finalized"], bool) or not isinstance(state["cancelled"], bool):
        raise ValueError("finalized/cancelled 必须是布尔值")
    if not isinstance(state["orders"], list) or not isinstance(state["trades"], list) or not isinstance(state["equity"], list) or not isinstance(state["bars"], list):
        raise ValueError("orders/trades/equity/bars 必须是数组")
    if len(state["bars"]) != rows or len(state["equity"]) != rows + 1:
        raise ValueError("state rows、bars、equity 数量不一致")
    if len(state["orders"]) > rows * len(symbols) or len(state["trades"]) > rows * len(symbols):
        raise ValueError("state history 超过组合逐帧事件上限")
    if set(state["strategy_history"]) != set(symbols):
        raise ValueError("strategy_history symbols 无效")
    max_history = config["params"]["slow"] + 1
    for symbol in symbols:
        history = state["strategy_history"][symbol]
        if not isinstance(history, list) or len(history) > max_history:
            raise ValueError(f"{symbol} strategy_history 超过策略窗口")
        expected = [frame[symbol] for frame in state["bars"][-max_history:]]
        if history != expected:
            raise ValueError(f"{symbol} strategy_history 与 bars 尾部不一致")
    if rows == 0:
        if state["last_source_datetime"] is not None or state["last_bar_start_datetime"] is not None:
            raise ValueError("空 state 时间轴必须为 null")
    else:
        source = _stamp(state["last_source_datetime"], "last_source_datetime")
        start = _stamp(state["last_bar_start_datetime"], "last_bar_start_datetime")
        if (source - start).total_seconds() != 60:
            raise ValueError("最后 source_datetime 必须比 bar_start 晚一分钟")
        if state["bars"][-1][symbols[0]]["source_datetime"] != source.isoformat():
            raise ValueError("last_source_datetime 与最后一帧不一致")
        if state["bars"][-1][symbols[0]]["bar_start_datetime"] != start.isoformat():
            raise ValueError("last_bar_start_datetime 与最后一帧不一致")
    if not isinstance(state["pending_order_ids"], dict):
        raise ValueError("pending_order_ids 必须是字典")
    expected_ids = [f"P{index:06d}" for index in range(1, len(state["orders"]) + 1)]
    if [order.get("order_id") for order in state["orders"]] != expected_ids:
        raise ValueError("order IDs 必须连续且按提交顺序排列")
    for symbol in symbols:
        unresolved = []
        for order in state["orders"]:
            if order.get("symbol") != symbol:
                continue
            if order.get("side") not in {"buy", "sell"}:
                raise ValueError("order side 无效")
            requested, filled, remaining = (order.get("requested_quantity"), order.get("filled_quantity"), order.get("remaining_quantity"))
            if any(isinstance(item, bool) or not isinstance(item, int) for item in (requested, filled, remaining)):
                raise ValueError("order quantities 必须是整数")
            if requested <= 0 or requested % LOT_SIZE or filled < 0 or remaining < 0 or filled + remaining != requested:
                raise ValueError("order quantities 不守恒或非整手")
            if order.get("status") not in {"pending", "partial_filled", "filled", "rejected", "partially_cancelled"}:
                raise ValueError("order status 无效")
            if order["status"] in {"pending", "partial_filled"}:
                unresolved.append(order)
        if len(unresolved) > 1:
            raise ValueError(f"{symbol} 同时存在多个未完成订单")
        pending_id = state["pending_order_ids"][symbol]
        if (unresolved[0]["order_id"] if unresolved else None) != pending_id:
            raise ValueError(f"{symbol} pending id 与 orders 不一致")
    fee_total = state["fee_total"]
    if isinstance(fee_total, bool) or not isinstance(fee_total, (int, float)) or not math.isfinite(float(fee_total)) or fee_total < 0:
        raise ValueError("fee_total 必须是有限非负数")

    # Reconcile every ledger view independently. The checksum detects casual
    # edits; these equations also reject a state that was edited and resealed.
    close = lambda left, right: math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-7)
    if len(state["trades"]) > rows * len(symbols):
        raise ValueError("trades 超过组合逐帧成交上限")
    cash_from_trades = float(config["initial_cash"])
    fees_from_trades = 0.0
    quantity_from_trades = {symbol: 0 for symbol in symbols}
    basis_from_trades = {symbol: 0.0 for symbol in symbols}
    trade_by_order: dict[str, list[dict[str, Any]]] = {}
    prior_fill_time = None
    for trade in state["trades"]:
        symbol = trade.get("symbol")
        side = trade.get("side")
        if symbol not in symbols or side not in {"buy", "sell"}:
            raise ValueError("trade symbol/side 无效")
        quantity = trade.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or quantity % LOT_SIZE:
            raise ValueError("trade quantity 必须是正整手")
        fill_time = _stamp(trade.get("fill_datetime"), "trade.fill_datetime")
        if prior_fill_time is not None and fill_time < prior_fill_time:
            raise ValueError("trades 必须按成交时间递增")
        prior_fill_time = fill_time
        price, notional = trade.get("price"), trade.get("notional")
        commission, stamp_duty = trade.get("commission"), trade.get("stamp_duty")
        for name, value in (("price", price), ("notional", notional), ("commission", commission), ("stamp_duty", stamp_duty)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"trade.{name} 必须是有限非负数")
        if price <= 0 or commission < 0 or stamp_duty < 0 or not close(notional, quantity * price):
            raise ValueError("trade price/notional 不一致")
        order_id = trade.get("order_id")
        if not isinstance(order_id, str):
            raise ValueError("trade order_id 无效")
        trade_by_order.setdefault(order_id, []).append(trade)
        fees_from_trades += float(commission) + float(stamp_duty)
        if side == "buy":
            if stamp_duty != 0:
                raise ValueError("buy trade 不得计印花税")
            cash_from_trades -= float(notional) + float(commission)
            quantity_from_trades[symbol] += quantity
            basis_from_trades[symbol] += float(notional) + float(commission)
        else:
            realized = trade.get("realized_pnl")
            if isinstance(realized, bool) or not isinstance(realized, (int, float)) or not math.isfinite(float(realized)):
                raise ValueError("sell trade.realized_pnl 必须是有限数值")
            basis_sold = float(notional) - float(commission) - float(stamp_duty) - float(realized)
            if basis_sold < -1e-7:
                raise ValueError("sell trade realized PnL 隐含负成本")
            cash_from_trades += float(notional) - float(commission) - float(stamp_duty)
            quantity_from_trades[symbol] -= quantity
            basis_from_trades[symbol] -= max(0.0, basis_sold)
    if not close(float(state["fee_total"]), fees_from_trades):
        raise ValueError("fee_total 与成交手续费合计不一致")
    if not close(float(state["cash"]), cash_from_trades):
        raise ValueError("cash 与初始资金及成交现金流不守恒")
    for symbol in symbols:
        position = state["positions"][symbol]
        if quantity_from_trades[symbol] != position["quantity"]:
            raise ValueError(f"{symbol} position quantity 与成交记录不守恒")
        position_basis = float(position["average_cost"]) * int(position["quantity"])
        if not close(position_basis, basis_from_trades[symbol]):
            raise ValueError(f"{symbol} 持仓成本与成交记录不守恒")

    if not state["equity"] or not isinstance(state["equity"][0], dict):
        raise ValueError("equity 缺少初始记录")
    first_equity = state["equity"][0]
    if (first_equity.get("source_datetime") is not None
            or not close(first_equity.get("cash", -1), config["initial_cash"])
            or not close(first_equity.get("market_value", -1), 0.0)
            or not close(first_equity.get("equity", -1), config["initial_cash"])):
        raise ValueError("equity 初始记录与初始资金不一致")
    trades_by_time: dict[str, list[dict[str, Any]]] = {}
    for trade in state["trades"]:
        trades_by_time.setdefault(trade["fill_datetime"], []).append(trade)
    running_cash = float(config["initial_cash"])
    running_quantity = {symbol: 0 for symbol in symbols}
    observed_sources = set()
    for index, frame in enumerate(state["bars"]):
        source = frame[symbols[0]]["source_datetime"]
        observed_sources.add(source)
        point = state["equity"][index + 1]
        if point.get("source_datetime") != source or point.get("bar_start_datetime") != frame[symbols[0]]["bar_start_datetime"]:
            raise ValueError("equity 时间轴与完整 bar 帧不一致")
        for trade in trades_by_time.get(source, []):
            symbol, side, quantity = trade["symbol"], trade["side"], trade["quantity"]
            if side == "buy":
                running_cash -= trade["notional"] + trade["commission"]
                running_quantity[symbol] += quantity
            else:
                running_cash += trade["notional"] - trade["commission"] - trade["stamp_duty"]
                running_quantity[symbol] -= quantity
        expected_market_value = sum(running_quantity[symbol] * frame[symbol]["close"] for symbol in symbols)
        if (not close(point.get("cash", -1), running_cash)
                or point.get("positions") != running_quantity
                or not close(point.get("market_value", -1), expected_market_value)
                or not close(point.get("equity", -1), running_cash + expected_market_value)):
            raise ValueError("equity/cash/positions 与逐帧成交及收盘估值不一致")
    if set(trades_by_time) - observed_sources:
        raise ValueError("trade fill_datetime 不存在于 state bars 时间轴")
    if (not close(running_cash, state["cash"])
            or running_quantity != {symbol: state["positions"][symbol]["quantity"] for symbol in symbols}):
        raise ValueError("最终 cash/positions 与逐帧成交记录不一致")

    order_ids = {order.get("order_id") for order in state["orders"]}
    if set(trade_by_order) - order_ids:
        raise ValueError("trade 引用了不存在的 order")
    for order in state["orders"]:
        linked = trade_by_order.get(order["order_id"], [])
        if (sum(item["quantity"] for item in linked) != order["filled_quantity"]
                or not close(sum(float(item["notional"]) for item in linked), order["fill_notional_total"])
                or not close(sum(float(item["commission"]) for item in linked), order["commission_total"])
                or not close(sum(float(item["stamp_duty"]) for item in linked), order["stamp_duty_total"])):
            raise ValueError("order 累计成交与 trades 不一致")
    _canonical_json(state)
    return state


def advance(config: dict[str, Any], state: dict[str, Any], frames: list[dict[str, Any]], *, final: bool = False) -> dict[str, Any]:
    """Apply aligned complete frames and return a detached JSON-safe state."""
    normalized = normalize_portfolio(config)
    _validate_state(normalized, state)
    if not isinstance(final, bool):
        raise ValueError("final 必须是布尔值")
    if state["finalized"]:
        raise ValueError("finalized portfolio state 不可继续 advance")
    if state["rows_processed"] + (len(frames) if isinstance(frames, list) else 0) > MAX_ENGINE_ROWS:
        raise ValueError(f"requested frames exceed bounded limit of {MAX_ENGINE_ROWS}")
    canonical = _canonical_frames(normalized, frames, state["last_source_datetime"])
    working = deepcopy(state)
    for frame in canonical:
        _advance_frame(normalized, working, frame)
    if final:
        for symbol in normalized["symbols"]:
            order = _pending(working, symbol)
            if order is not None and order["status"] in {"pending", "partial_filled"}:
                order["reason"] = order["reason"] or "no_later_bar_before_range_end"
        working["finalized"] = True
    _seal(working)
    _validate_state(normalized, working)
    return working


def result_from_state(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_portfolio(config)
    _validate_state(normalized, state)
    if not state["finalized"]:
        raise ValueError("result 只能从 final=True 的组合 state 生成")
    final_day = (_stamp(state["last_source_datetime"], "last_source_datetime").date()
                 if state["last_source_datetime"] else None)
    positions = {}
    for symbol in normalized["symbols"]:
        position = state["positions"][symbol]
        view = _position_view(position, final_day) if final_day else {}
        if not view:
            view = {"quantity": 0, "avg_cost": 0.0, "available_to_sell": 0}
        view.update({
            "market_value": position["quantity"] * (state["bars"][-1][symbol]["close"] if state["bars"] else 0.0),
            "lots": deepcopy(position["lots"]),
        })
        positions[symbol] = view
    values = [float(item["equity"]) for item in state["equity"]]
    ending_equity = values[-1] if values else float(normalized["initial_cash"])
    peak = float(normalized["initial_cash"])
    drawdowns = []
    for value in values:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1.0 if peak else 0.0)
    daily_last: dict[str, float] = {}
    for item in state["equity"]:
        if item["source_datetime"]:
            daily_last[item["source_datetime"][:10]] = float(item["equity"])
    daily = []
    previous = float(normalized["initial_cash"])
    for day in sorted(daily_last):
        daily.append(daily_last[day] / previous - 1.0)
        previous = daily_last[day]
    daily_std = pd.Series(daily, dtype=float).std(ddof=1) if len(daily) > 1 else float("nan")
    sharpe = (float(pd.Series(daily, dtype=float).mean() / daily_std * math.sqrt(252.0))
              if len(daily) > 1 and math.isfinite(daily_std) and daily_std > 0 else None)
    metrics = {
        "initial_cash": normalized["initial_cash"], "ending_equity": ending_equity,
        "total_return": ending_equity / normalized["initial_cash"] - 1.0,
        "max_drawdown": min(drawdowns) if drawdowns else 0.0,
        "sharpe": sharpe, "sharpe_frequency": "daily",
        "daily_return_observations": len(daily), "total_trades": len(state["trades"]),
        "total_fills": len(state["trades"]),
        "filled_orders": sum(item["status"] == "filled" for item in state["orders"]),
        "partial_or_pending_orders": sum(item["status"] in {"partial_filled", "pending", "partially_cancelled"} for item in state["orders"]),
        "rejected_orders": sum(item["status"] == "rejected" for item in state["orders"]),
        "minimum_cash": min(float(item["cash"]) for item in state["equity"]),
        "negative_cash": any(float(item["cash"]) < -1e-8 for item in state["equity"]),
        "fees_paid": float(state["fee_total"]),
    }
    limitations = [
        "RESEARCH_ONLY; paper_admission=false; no paper or live admission is implied.",
        "Only 2-4 supported main-board symbols and built-in buy_hold/sma_cross strategies are accepted.",
        "Target weights are bounded by the shared portfolio cash budget; same-minute order fills follow config symbols order.",
        "Aligned observed timestamps do not certify price adjustment vintage, historical tradability, or full market coverage.",
        "Turnover is an uncertified conservative capacity proxy; exchange fees, broker fills, corporate actions, and limit-price eligibility are not modeled.",
        "Pending orders require the next observed source minute to be exactly contiguous; gaps reject without forward-fill or delayed backfill.",
        "Each buy lot is tracked separately for T+1 sellability; sellable shares are limited to prior-date lots.",
    ]
    return {
        "mode": "RESEARCH_ONLY", "paper_admission": False,
        "config_sha256": state["config_sha256"], "metrics": metrics,
        "equity": deepcopy(state["equity"]), "positions": positions,
        "orders": deepcopy(state["orders"]), "trades": deepcopy(state["trades"]),
        "bars": deepcopy(state["bars"]), "limitations": limitations,
    }
