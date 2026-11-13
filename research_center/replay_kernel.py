"""JSON-safe incremental state machine for the bounded reference replay.

The kernel uses the same event transition for batch and resumed runs. It is a
local research primitive; its state is integrity-checked for accidental drift,
not authenticated as a security boundary.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from strategy.base import Bar, Context


STATE_SCHEMA_VERSION = 1
ENGINE_ID = "research_center.incremental.minute.v1"
_STATE_FIELDS = {
    "schema_version",
    "engine",
    "mode",
    "paper_admission",
    "config_sha256",
    "state_sha256",
    "rows_processed",
    "cash",
    "quantity",
    "average_cost",
    "entry_date",
    "pending_order_id",
    "last_close",
    "last_source_datetime",
    "last_bar_start_datetime",
    "strategy_history",
    "orders",
    "trades",
    "equity",
    "bars",
    "fee_total",
    "finalized",
    "cancelled",
}


def _normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    # Lazy import avoids an engine <-> kernel import cycle.
    from .engine import validate_config

    return validate_config(config)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("state/bars must contain finite JSON-compatible values") from exc


def _config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _state_digest(state: dict[str, Any]) -> str:
    without_digest = {key: value for key, value in state.items() if key != "state_sha256"}
    return hashlib.sha256(_canonical_json(without_digest).encode("utf-8")).hexdigest()


def _seal(state: dict[str, Any]) -> dict[str, Any]:
    state["state_sha256"] = _state_digest(state)
    return state


def _finite_tree(value: Any, path: str = "state") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path} contains a non-string key")
        for key, item in value.items():
            _finite_tree(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _stamp(value: Any, field: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field} 必须是有效的无时区分钟时间戳") from exc
    if pd.isna(result) or result.tzinfo is not None:
        raise ValueError(f"{field} 必须是有效的无时区分钟时间戳")
    if result.second != 0 or result.microsecond != 0 or result.nanosecond != 0:
        raise ValueError(f"{field} 必须精确到整分钟")
    return result


def _stamp_text(value: Any, field: str) -> str:
    return _stamp(value, field).isoformat()


def _iso_date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期或 null") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期或 null")
    return parsed


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    return number


def _validate_state(config: dict[str, Any], state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("state 必须是 JSON 字典")
    if set(state) != _STATE_FIELDS:
        raise ValueError("state schema fields 不匹配")
    if not isinstance(state.get("state_sha256"), str) or state["state_sha256"] != _state_digest(state):
        raise ValueError("state_sha256 校验失败；state 可能已被意外修改")
    _finite_tree(state)
    if not _is_int(state["schema_version"]) or state["schema_version"] != STATE_SCHEMA_VERSION:
        raise ValueError("不支持的 replay state schema_version")
    if state["engine"] != ENGINE_ID:
        raise ValueError("state engine 不匹配")
    if state["mode"] != "RESEARCH_ONLY" or state["paper_admission"] is not False:
        raise ValueError("replay state 必须保持 RESEARCH_ONLY 且 paper_admission=false")
    if state["config_sha256"] != _config_digest(config):
        raise ValueError("state 与当前 config 不匹配")

    rows = state["rows_processed"]
    from .engine import MAX_ENGINE_ROWS, LOT_SIZE

    if not _is_int(rows) or not 0 <= rows <= MAX_ENGINE_ROWS:
        raise ValueError("rows_processed 超过 state 上限或格式无效")
    _number(state["cash"], "cash", minimum=0.0)
    quantity = state["quantity"]
    if not _is_int(quantity) or quantity < 0 or quantity % LOT_SIZE:
        raise ValueError("quantity 必须为非负整手数量")
    average_cost = _number(state["average_cost"], "average_cost", minimum=0.0)
    fee_total = _number(state["fee_total"], "fee_total", minimum=0.0)
    if quantity == 0 and (average_cost != 0.0 or state["entry_date"] is not None):
        raise ValueError("空仓 state 的 average_cost 和 entry_date 必须为零/null")
    if quantity > 0 and (average_cost <= 0.0 or state["entry_date"] is None):
        raise ValueError("有仓 state 必须包含正的 average_cost 和 entry_date")
    entry_date = _iso_date(state["entry_date"], "entry_date")
    last_close = state["last_close"]
    if last_close is not None:
        _number(last_close, "last_close", minimum=0.0)
        if last_close == 0:
            raise ValueError("last_close 必须大于 0 或为 null")
    source_text = state["last_source_datetime"]
    start_text = state["last_bar_start_datetime"]
    if rows == 0:
        if any(value is not None for value in (source_text, start_text, last_close)):
            raise ValueError("空 state 的 last timestamps/close 必须为 null")
    else:
        if source_text is None or start_text is None or last_close is None:
            raise ValueError("非空 state 必须包含 last timestamps 和 close")
        last_source = _stamp(source_text, "last_source_datetime")
        last_start = _stamp(start_text, "last_bar_start_datetime")
        if (last_source - last_start).total_seconds() != 60:
            raise ValueError("last bar start 必须比 source end 早一分钟")
        if entry_date is not None and entry_date > last_source.date():
            raise ValueError("entry_date 不可晚于 state 最后一根 bar")

    if not isinstance(state["finalized"], bool) or not isinstance(state["cancelled"], bool):
        raise ValueError("finalized/cancelled 必须是布尔值")
    for name in ("strategy_history", "orders", "trades", "equity", "bars"):
        if not isinstance(state[name], list):
            raise ValueError(f"{name} 必须是数组")
    if len(state["bars"]) != rows or len(state["equity"]) != rows + 1:
        raise ValueError("state rows、bars、equity 数量不一致")
    if len(state["orders"]) > rows or len(state["trades"]) > rows:
        raise ValueError("state history 超出每根 bar 一次事件的上限")
    slow = config["params"]["slow"]
    if len(state["strategy_history"]) > slow + 1:
        raise ValueError("strategy_history 超过策略所需窗口")
    if state["strategy_history"] != state["bars"][-(slow + 1) :]:
        raise ValueError("strategy_history 与累计 bars 尾部不一致")
    if rows:
        if state["bars"][-1]["source_datetime"] != state["last_source_datetime"]:
            raise ValueError("last_source_datetime 与最后一根 bar 不一致")
        if state["bars"][-1]["bar_start_datetime"] != state["last_bar_start_datetime"]:
            raise ValueError("last_bar_start_datetime 与最后一根 bar 不一致")
        if state["equity"][-1]["source_datetime"] != state["last_source_datetime"]:
            raise ValueError("最后一条 equity 与最后一根 bar 不一致")
    if state["orders"]:
        expected_ids = [f"R{index:06d}" for index in range(1, len(state["orders"]) + 1)]
        if [order.get("order_id") for order in state["orders"]] != expected_ids:
            raise ValueError("order IDs 必须连续且按提交顺序排列")
    unresolved = [
        order
        for order in state["orders"]
        if order.get("status") in {"pending", "partial_filled"}
    ]
    pending_id = state["pending_order_id"]
    if pending_id is not None and not isinstance(pending_id, str):
        raise ValueError("pending_order_id 必须是字符串或 null")
    if len(unresolved) > 1:
        raise ValueError("state 不能包含多个未完成订单")
    if (unresolved[0]["order_id"] if unresolved else None) != pending_id:
        raise ValueError("pending_order_id 与 orders 不一致")
    for order in state["orders"]:
        if order.get("symbol") != config["symbol"] or order.get("side") not in {"buy", "sell"}:
            raise ValueError("state order symbol/side 无效")
        requested = order.get("requested_quantity")
        filled = order.get("filled_quantity")
        remaining = order.get("remaining_quantity")
        if not all(_is_int(item) for item in (requested, filled, remaining)):
            raise ValueError("order quantities 必须是整数")
        if requested <= 0 or requested % LOT_SIZE or filled < 0 or remaining < 0:
            raise ValueError("order quantities 必须为合法整手数量")
        if filled + remaining != requested:
            raise ValueError("order filled/remaining 数量不守恒")
        if order.get("status") not in {"pending", "partial_filled", "filled", "rejected", "partially_cancelled"}:
            raise ValueError("order status 无效")
    if state["finalized"] and pending_id is not None:
        # The legacy result keeps this order's status and reason unchanged; the
        # finalized flag makes it impossible to submit another fill attempt.
        if not unresolved:
            raise ValueError("finalized state 中的 pending_order_id 必须指向未完成订单")

    # Ensure nested state has no NaN or non-JSON values. This also catches bool
    # substitutions in fields that otherwise pass Python's int checks.
    _canonical_json(state)
    return state


def _canonical_bar(config: dict[str, Any], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("bar payload 必须是字典")
    if payload.get("symbol") != config["symbol"]:
        raise ValueError("bar symbol 与 config.symbol 不一致")
    source_time = _stamp(payload.get("source_datetime"), "source_datetime")
    present_starts = [
        payload[name]
        for name in ("datetime", "bar_start_datetime", "start")
        if name in payload
    ]
    if not present_starts:
        raise ValueError("bar 必须包含 datetime、bar_start_datetime 或 start")
    start_times = [_stamp(value, "bar_start_datetime") for value in present_starts]
    expected_start = source_time - pd.Timedelta(minutes=1)
    if any(value != expected_start for value in start_times):
        raise ValueError("bar start/datetime 必须等于 source_datetime - 1 分钟")
    start_day = pd.Timestamp(config["start"]).date()
    end_day = pd.Timestamp(config["end"]).date()
    if not start_day <= source_time.date() <= end_day:
        raise ValueError("bar source_datetime 超出 config 请求区间")
    label = source_time.strftime("%H:%M")
    if not ("09:31" <= label <= "11:30" or "13:01" <= label <= "15:00"):
        raise ValueError("bar source_datetime 不在 A 股连续交易时段")

    numbers = {
        name: _number(payload.get(name), name, minimum=0.0 if name in {"volume", "turnover"} else 0.01)
        for name in ("open", "high", "low", "close", "volume", "turnover")
    }
    if numbers["high"] < max(numbers["open"], numbers["close"], numbers["low"]):
        raise ValueError("bar high 低于 OHLC 其他价格")
    if numbers["low"] > min(numbers["open"], numbers["close"], numbers["high"]):
        raise ValueError("bar low 高于 OHLC 其他价格")
    return {
        "datetime": expected_start.isoformat(),
        "bar_start_datetime": expected_start.isoformat(),
        "source_datetime": source_time.isoformat(),
        "symbol": config["symbol"],
        **numbers,
    }


def _pending_order(state: dict[str, Any]) -> dict[str, Any] | None:
    order_id = state["pending_order_id"]
    if order_id is None:
        return None
    for order in reversed(state["orders"]):
        if order["order_id"] == order_id:
            return order
    raise ValueError("pending order 不存在于 orders 中")


def _advance_one(
    config: dict[str, Any],
    state: dict[str, Any],
    payload: dict[str, Any],
    strategy: Any,
) -> None:
    from .engine import LOT_SIZE, _reject, _round_adverse_price

    symbol = config["symbol"]
    source_time = pd.Timestamp(payload["source_datetime"])
    bar_start = pd.Timestamp(payload["bar_start_datetime"])
    trade_day = source_time.date()
    row_open = float(payload["open"])
    row_high = float(payload["high"])
    row_low = float(payload["low"])
    row_close = float(payload["close"])
    row_volume = float(payload["volume"])
    row_turnover = float(payload["turnover"])
    state["bars"].append(payload)
    state["rows_processed"] += 1
    state["last_close"] = row_close
    state["last_source_datetime"] = source_time.isoformat()
    state["last_bar_start_datetime"] = bar_start.isoformat()
    state["strategy_history"].append(payload)
    history_size = config["params"]["slow"] + 1
    if len(state["strategy_history"]) > history_size:
        del state["strategy_history"][: len(state["strategy_history"]) - history_size]

    sale_filled_this_bar = False
    pending = _pending_order(state)
    if pending:
        gap_minutes = (
            source_time - pd.Timestamp(pending["last_attempt_datetime"])
        ).total_seconds() / 60.0
        if gap_minutes != 1.0:
            _reject(pending, "next_source_minute_not_contiguous")
            state["pending_order_id"] = None
            pending = None
        elif row_volume <= 0:
            _reject(pending, "zero_volume_bar")
            state["pending_order_id"] = None
            pending = None
        elif row_high <= row_low:
            _reject(pending, "one_price_bar")
            state["pending_order_id"] = None
            pending = None
        elif pending["side"] == "sell" and (
            state["entry_date"] is None or trade_day <= date.fromisoformat(state["entry_date"])
        ):
            _reject(pending, "T+1_sell_restriction")
            state["pending_order_id"] = None
            pending = None
        else:
            side = pending["side"]
            reference = row_high if side == "buy" else row_low
            fill_price = _round_adverse_price(reference, side, config["slippage_bps"])
            participation = config["params"]["max_turnover_participation"]
            if row_turnover > 0:
                capacity = int((row_turnover * participation / fill_price) // LOT_SIZE) * LOT_SIZE
            else:
                capacity = 0
            fill_quantity = min(int(pending["remaining_quantity"]), capacity)
            fill_quantity = (fill_quantity // LOT_SIZE) * LOT_SIZE

            if side == "buy":
                while fill_quantity > 0:
                    notional = fill_quantity * fill_price
                    commission = max(
                        notional * config["commission_rate"], config["min_commission"]
                    )
                    if notional + commission <= state["cash"] + 1e-9:
                        break
                    fill_quantity -= LOT_SIZE
            else:
                fill_quantity = min(fill_quantity, int(state["quantity"]))
                fill_quantity = (fill_quantity // LOT_SIZE) * LOT_SIZE
                commission = 0.0

            if fill_quantity <= 0:
                pending["reason"] = "no_cash_or_turnover_capacity"
                # Preserve the legacy status value even if this order had a
                # prior partial fill and this attempt found no new capacity.
                pending["status"] = "pending"
                pending["last_attempt_datetime"] = source_time.isoformat()
            else:
                sell_basis = float(state["average_cost"])
                notional = fill_quantity * fill_price
                commission = max(
                    notional * config["commission_rate"], config["min_commission"]
                )
                stamp_duty = (
                    notional * config["stamp_duty_rate"] if side == "sell" else 0.0
                )
                slippage_cost = abs(fill_price - reference) * fill_quantity
                adverse_range_cost = (
                    max(0.0, fill_price - row_open) * fill_quantity
                    if side == "buy"
                    else max(0.0, row_open - fill_price) * fill_quantity
                )
                if side == "buy":
                    old_quantity = state["quantity"]
                    state["cash"] -= notional + commission
                    total_cost = state["average_cost"] * old_quantity + notional + commission
                    state["quantity"] += fill_quantity
                    state["average_cost"] = total_cost / state["quantity"]
                    if old_quantity == 0:
                        state["entry_date"] = trade_day.isoformat()
                else:
                    state["cash"] += notional - commission - stamp_duty
                    state["quantity"] -= fill_quantity
                    if state["quantity"] == 0:
                        state["average_cost"] = 0.0
                        state["entry_date"] = None
                    sale_filled_this_bar = True
                if state["cash"] < -1e-7:
                    raise RuntimeError("cash invariant violated by execution model")
                state["cash"] = max(0.0, state["cash"])
                state["fee_total"] += commission + stamp_duty
                state["trades"].append(
                    {
                        "order_id": pending["order_id"],
                        "symbol": symbol,
                        "side": side,
                        "signal_datetime": pending["signal_datetime"],
                        "fill_datetime": source_time.isoformat(),
                        "datetime": source_time.isoformat(),
                        "quantity": fill_quantity,
                        "price": fill_price,
                        "notional": notional,
                        "commission": commission,
                        "stamp_duty": stamp_duty,
                        "slippage_cost": slippage_cost,
                        "adverse_range_cost_vs_open": adverse_range_cost,
                        "turnover_participation": notional / row_turnover if row_turnover > 0 else None,
                        "realized_pnl": (
                            notional - commission - stamp_duty - sell_basis * fill_quantity
                            if side == "sell"
                            else None
                        ),
                    }
                )
                pending["filled_quantity"] += fill_quantity
                pending["remaining_quantity"] -= fill_quantity
                pending["commission_total"] += commission
                pending["stamp_duty_total"] += stamp_duty
                pending["fill_notional_total"] += notional
                pending["average_fill_price"] = (
                    pending["fill_notional_total"] / pending["filled_quantity"]
                )
                pending["reason"] = None
                if pending["remaining_quantity"] <= 0:
                    pending["remaining_quantity"] = 0
                    pending["status"] = "filled"
                    state["pending_order_id"] = None
                    pending = None
                else:
                    pending["status"] = "partial_filled"
                    pending["reason"] = "turnover_or_cash_capacity_remainder"
                    pending["last_attempt_datetime"] = source_time.isoformat()

    position_payload = (
        {
            "quantity": state["quantity"],
            "avg_cost": state["average_cost"],
            "available_to_sell": (
                state["quantity"]
                if state["entry_date"] is not None
                and trade_day > date.fromisoformat(state["entry_date"])
                else 0
            ),
        }
        if state["quantity"] > 0
        else {}
    )
    # Pandas 3 infers Arrow-backed string columns/labels from a list of dicts.
    # This installed macOS Arrow build segfaulted across HTTP request threads.
    # Strategy histories need numerical OHLCV and explicit datetime values;
    # object-typed column labels avoid implicit Arrow string construction.
    fields = ("open", "high", "low", "close", "volume", "turnover")
    history_frame = pd.DataFrame(
        np.asarray([[item[key] for key in fields] for item in state["strategy_history"]], dtype=float),
        columns=pd.Index(fields, dtype=object),
        index=pd.DatetimeIndex([pd.Timestamp(item["source_datetime"]) for item in state["strategy_history"]], name="source_datetime"),
    )
    history_frame["source_datetime"] = history_frame.index
    history_frame["datetime"] = pd.DatetimeIndex([pd.Timestamp(item["datetime"]) for item in state["strategy_history"]])
    history = {symbol: history_frame}
    context_params = {
        "commission_rate": config["commission_rate"],
        "min_commission": config["min_commission"],
        "slippage_bps": config["slippage_bps"],
        "stamp_duty_rate": config["stamp_duty_rate"],
        "target_fraction": config["params"]["target_fraction"],
        "max_turnover_participation": config["params"]["max_turnover_participation"],
        "fast": config["params"]["fast"],
        "slow": config["params"]["slow"],
        "_pending_side": pending["side"] if pending else None,
        "_sale_filled_this_bar": sale_filled_this_bar,
    }
    context = Context(
        datetime=source_time,
        cash=state["cash"],
        positions={symbol: position_payload} if position_payload else {},
        history=history,
        params=context_params,
    )
    bar = Bar(
        datetime=source_time,
        ohlcv={
            symbol: {
                "open": row_open,
                "high": row_high,
                "low": row_low,
                "close": row_close,
                "volume": row_volume,
                "turnover": row_turnover,
            }
        },
    )
    if pending is None:
        for request in strategy.on_bar(bar, context) or []:
            if request.symbol != symbol or request.side not in {"buy", "sell"}:
                continue
            requested_quantity = int(float(request.quantity) // LOT_SIZE) * LOT_SIZE
            if requested_quantity <= 0:
                continue
            order_id = f"R{len(state['orders']) + 1:06d}"
            order = {
                "order_id": order_id,
                "symbol": symbol,
                "side": request.side,
                "signal_datetime": source_time.isoformat(),
                "signal_bar_start": bar_start.isoformat(),
                "requested_quantity": requested_quantity,
                "filled_quantity": 0,
                "remaining_quantity": requested_quantity,
                "average_fill_price": None,
                "fill_notional_total": 0.0,
                "commission_total": 0.0,
                "stamp_duty_total": 0.0,
                "status": "pending",
                "reason": "awaiting_next_contiguous_minute",
                "last_attempt_datetime": source_time.isoformat(),
            }
            state["orders"].append(order)
            state["pending_order_id"] = order_id
            break

    equity_value = state["cash"] + state["quantity"] * row_close
    state["equity"].append(
        {
            "datetime": source_time.isoformat(),
            "source_datetime": source_time.isoformat(),
            "bar_start_datetime": bar_start.isoformat(),
            "cash": state["cash"],
            "quantity": state["quantity"],
            "equity": equity_value,
        }
    )


def initial_state(config: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-serializable empty replay state bound to normalized config."""
    normalized = _normalized_config(config)
    cash = float(normalized["initial_cash"])
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine": ENGINE_ID,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "config_sha256": _config_digest(normalized),
        "state_sha256": "",
        "rows_processed": 0,
        "cash": cash,
        "quantity": 0,
        "average_cost": 0.0,
        "entry_date": None,
        "pending_order_id": None,
        "last_close": None,
        "last_source_datetime": None,
        "last_bar_start_datetime": None,
        "strategy_history": [],
        "orders": [],
        "trades": [],
        "equity": [
            {
                "datetime": f"{normalized['start']} 00:00:00",
                "source_datetime": None,
                "cash": cash,
                "quantity": 0,
                "equity": cash,
            }
        ],
        "bars": [],
        "fee_total": 0.0,
        "finalized": False,
        "cancelled": False,
    }
    _seal(state)
    _validate_state(normalized, state)
    return state


def _advance_validated(
    normalized: dict[str, Any],
    working: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    final: bool,
    stop_before: Callable[[int, int], bool] | None = None,
    after_bar: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    from .engine import MAX_ENGINE_ROWS, _make_strategy

    if working["finalized"]:
        raise ValueError("finalized replay state 不可继续 advance")
    start_rows = working["rows_processed"]
    if start_rows + len(bars) > MAX_ENGINE_ROWS:
        raise ValueError(f"requested data exceeds bounded limit of {MAX_ENGINE_ROWS} bars")
    prior = (
        pd.Timestamp(working["last_source_datetime"])
        if working["last_source_datetime"] is not None
        else None
    )
    normalized_bars = []
    for payload in bars:
        canonical = _canonical_bar(normalized, payload)
        current = pd.Timestamp(canonical["source_datetime"])
        if prior is not None and current <= prior:
            raise ValueError("bars 必须严格晚于 state last_source_datetime 且彼此递增")
        normalized_bars.append(canonical)
        prior = current

    processed = 0
    # The trusted reference strategies are stateless; their only rolling input
    # is strategy_history, which is persisted in JSON state.
    strategy = _make_strategy(normalized)
    for index, payload in enumerate(normalized_bars):
        if stop_before and stop_before(index, len(normalized_bars)):
            pending = _pending_order(working)
            if pending:
                pending["reason"] = "run_cancelled_before_remaining_quantity_filled"
            break
        _advance_one(normalized, working, payload, strategy)
        processed += 1
        if after_bar:
            after_bar(start_rows + processed, start_rows + len(normalized_bars))

    stopped = processed < len(normalized_bars)
    if final and not stopped:
        pending = _pending_order(working)
        if pending and pending["status"] in {"pending", "partial_filled"}:
            pending["reason"] = pending["reason"] or "no_later_bar_before_range_end"
        working["finalized"] = True
    _seal(working)
    _validate_state(normalized, working)
    return working


def advance(
    config: dict[str, Any],
    state: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    final: bool = False,
) -> dict[str, Any]:
    """Apply newly appended canonical bars and return a detached JSON state.

    ``final=False`` is a resumable pause and keeps any outstanding order. Set
    ``final=True`` only when the source range is exhausted; the result retains
    legacy order status/reason fields while ``finalized`` prevents later fills.
    """
    normalized = _normalized_config(config)
    _validate_state(normalized, state)
    if not isinstance(final, bool):
        raise ValueError("final 必须是布尔值")
    if not isinstance(bars, list):
        raise ValueError("bars 必须是仅含新增 canonical payload 的数组")
    working = deepcopy(state)
    return _advance_validated(normalized, working, bars, final=final)


def _advance_loaded_bars(
    config: dict[str, Any],
    state: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    cancelled: Callable[[], bool] | None,
    progress: Callable[[int, int], None] | None,
) -> dict[str, Any]:
    """Internal batch adapter retaining run_backtest's per-bar callbacks."""
    normalized = _normalized_config(config)
    _validate_state(normalized, state)
    working = deepcopy(state)

    def stop_before(_index: int, _count: int) -> bool:
        return bool(cancelled and cancelled())

    def after_bar(processed_total: int, bars_total: int) -> None:
        if progress:
            progress(processed_total, bars_total)

    # With no cancellation callback, exhaustion is definitive. When a callback
    # interrupts the loop, preserve the outstanding order for later resumption.
    will_check_cancel = cancelled is not None
    if will_check_cancel:
        # `_advance_validated` only knows whether a bar was skipped; with zero
        # remaining bars it cannot stop. The final callback value remains the
        # legacy result's cancellation signal, matching run_backtest semantics.
        working = _advance_validated(
            normalized,
            working,
            bars,
            final=True,
            stop_before=stop_before,
            after_bar=after_bar,
        )
        if working["rows_processed"] < state["rows_processed"] + len(bars):
            # A callback cancellation is a pause, not a completed data range.
            working["finalized"] = False
            _seal(working)
            _validate_state(normalized, working)
        working["cancelled"] = bool(cancelled and cancelled())
        _seal(working)
        _validate_state(normalized, working)
        return working

    return _advance_validated(
        normalized,
        working,
        bars,
        final=True,
        after_bar=after_bar,
    )


def _mark_cancelled_before_read(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_config(config)
    _validate_state(normalized, state)
    working = deepcopy(state)
    working["finalized"] = True
    working["cancelled"] = True
    _seal(working)
    _validate_state(normalized, working)
    return working


def result_from_state(
    config: dict[str, Any], state: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Render the legacy engine result shape from a validated incremental state."""
    normalized = _normalized_config(config)
    _validate_state(normalized, state)
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是字典")
    from . import engine

    data_manifest = deepcopy(manifest)
    data_manifest["total_fees_paid"] = float(state["fee_total"])
    data_manifest["source_price_adjustment"] = "unresolved"
    final_position = None
    if state["quantity"] > 0:
        entry_date = date.fromisoformat(state["entry_date"])
        last_source = pd.Timestamp(state["last_source_datetime"])
        last_close = float(state["last_close"] or 0.0)
        final_position = {
            "symbol": normalized["symbol"],
            "quantity": state["quantity"],
            "avg_cost": float(state["average_cost"]),
            "market_value": state["quantity"] * last_close,
            "unrealized_pnl": state["quantity"] * (last_close - state["average_cost"]),
            "available_to_sell": (
                state["quantity"] if last_source.date() > entry_date else 0
            ),
            "entry_date": entry_date.isoformat(),
        }
    return engine._result(
        normalized,
        deepcopy(state["equity"]),
        deepcopy(state["orders"]),
        deepcopy(state["trades"]),
        final_position,
        deepcopy(state["bars"]),
        data_manifest,
        cancelled=state["cancelled"],
    )

