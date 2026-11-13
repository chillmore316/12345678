"""Versioned host-side callback kernel for custom research strategies.

This module is execution plumbing around ``replay_kernel._advance_one``.  It
does not implement a second fill engine: the existing v1 transition remains the
only source of cash, commission, stamp duty, slippage, and fill calculations.
The internal SMA-shaped config supplies only the legacy transition's bounded
history window and order sizing controls; it is never used to generate a user
strategy signal.

The adapter is caller-owned.  A returned state is provisional until the caller
has also closed its adapter successfully (some adapters report late worker
errors during close).  This module never closes it and never admits a paper or
live account.  It reads no market data and executes no user source itself.

Typical host usage::

    config = make_execution_config(request, history_limit=60)
    state = initial_state(config, trusted_identity, user_state=adapter.state)
    state = advance(config, state, bars, adapter, final=True)
    adapter.close()  # commit ``state`` only if this succeeds
    result = result_from_state(config, state, data_manifest)

``trusted_identity`` is host-supplied provenance (source/API/driver/runtime
hashes and algorithm parameter hashes).  It is bound into state, not taken
from a strategy callback response.  Hashing detects accidental drift; it is
not an authentication boundary.
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

from strategy.base import Bar, Context, Order

from . import replay_kernel


STATE_SCHEMA_VERSION = 2
ENGINE_ID = "research_center.custom_execution.v2"
MODE = "RESEARCH_ONLY"
PAPER_ADMISSION = False
MAX_HISTORY_LIMIT = 241
MAX_USER_STATE_BYTES = 16 * 1024
MAX_IDENTITY_BYTES = 16 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
_OUTER_FIELDS = {
    "schema_version",
    "engine",
    "mode",
    "paper_admission",
    "config_sha256",
    "identity",
    "identity_sha256",
    "history_limit",
    "user_state",
    "inner_v1",
    "lots",
    "state_sha256",
}
_REJECTION_REASONS = {
    "multiple_orders_per_bar",
    "pending_order_exists",
    "unsupported_symbol",
    "unsupported_side",
    "unsupported_order_type",
    "invalid_order_quantity",
    "sell_exceeds_t_plus_one_available",
}
_LOT_SIZE = 100


def _canonical_json(
    value: Any,
    *,
    maximum: int | None = None,
    max_nodes: int | None = MAX_JSON_NODES,
    max_depth: int | None = MAX_JSON_DEPTH,
    field: str = "value",
) -> str:
    """Validate bounded JSON input without accepting numpy/pandas objects."""
    nodes = 0
    ancestors: set[int] = set()

    def visit(item: Any, depth: int, path: str) -> None:
        nonlocal nodes
        nodes += 1
        if max_nodes is not None and nodes > max_nodes:
            raise ValueError(f"{field} exceeds the JSON node limit")
        if max_depth is not None and depth > max_depth:
            raise ValueError(f"{field} exceeds the JSON nesting limit")
        if item is None or type(item) in (bool, int, str):
            if isinstance(item, str):
                try:
                    item.encode("utf-8", "strict")
                except UnicodeError as exc:
                    raise ValueError(f"{path} is not valid UTF-8 text") from exc
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return
        if type(item) not in (list, dict):
            raise ValueError(f"{path} contains a non-JSON value")
        identity = id(item)
        if identity in ancestors:
            raise ValueError(f"{path} contains a JSON cycle")
        ancestors.add(identity)
        try:
            if type(item) is list:
                for index, child in enumerate(item):
                    visit(child, depth + 1, f"{path}[{index}]")
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError(f"{path} contains a non-string key")
                    visit(key, depth + 1, f"{path}.<key>")
                    visit(child, depth + 1, f"{path}.{key}")
        finally:
            ancestors.remove(identity)

    visit(value, 0, field)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError(f"{field} must be finite JSON-compatible data") from exc
    if maximum is not None and len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its {maximum}-byte limit")
    return encoded


def _json_copy(value: Any, *, maximum: int, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{field} must be a JSON object")
    encoded = _canonical_json(value, maximum=maximum, field=field)
    return json.loads(encoded)


def _sha256_json(value: Any, *, field: str) -> str:
    return hashlib.sha256(_canonical_json(value, field=field).encode("utf-8")).hexdigest()


def _outer_digest(state: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in state.items() if key != "state_sha256"}
    return hashlib.sha256(
        _canonical_json(
            unsigned,
            max_nodes=None,
            max_depth=128,
            field="custom state",
        ).encode("utf-8")
    ).hexdigest()


def _seal_outer(state: dict[str, Any]) -> dict[str, Any]:
    state["state_sha256"] = _outer_digest(state)
    return state


def _require_history_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_HISTORY_LIMIT:
        raise ValueError(f"history_limit 必须是 1..{MAX_HISTORY_LIMIT} 的整数")
    return value


def _normalize_execution_config(config: dict[str, Any], history_limit: int) -> dict[str, Any]:
    normalized = replay_kernel._normalized_config(config)
    expected_slow = max(2, history_limit - 1)
    params = normalized["params"]
    if (
        normalized["strategy"] != "sma_cross"
        or params["fast"] != 1
        or params["slow"] != expected_slow
    ):
        raise ValueError("execution config 必须使用本内核生成的 legacy history-window shim")
    return normalized


def make_execution_config(base_request: dict[str, Any], history_limit: int = 60) -> dict[str, Any]:
    """Build the validated v1 execution shim for a custom callback run.

    Only stock, date range, cash, fees, slippage, data-root provenance,
    ``target_fraction`` and ``max_turnover_participation`` survive.  Custom
    algorithm parameters belong in the adapter and in the separately supplied
    trusted identity, never in this internal legacy config.
    """
    limit = _require_history_limit(history_limit)
    if type(base_request) is not dict:
        raise ValueError("base_request 必须是字典")
    allowed = {
        "symbol",
        "start",
        "end",
        "interval",
        "strategy",
        "initial_cash",
        "commission_rate",
        "min_commission",
        "slippage_bps",
        "data_root",
        "params",
    }
    extra = set(base_request) - allowed
    if extra:
        raise ValueError(f"base_request 包含非执行字段：{sorted(extra)}")
    raw_params = base_request.get("params", {})
    if type(raw_params) is not dict:
        raise ValueError("base_request.params 必须是字典")
    execution_params = {
        "target_fraction": raw_params.get("target_fraction", 0.5),
        "max_turnover_participation": raw_params.get("max_turnover_participation", 0.01),
        # These are required only because replay_kernel's v1 schema validates
        # an SMA-shaped window. No SMA strategy instance generates signals.
        "fast": 1,
        "slow": max(2, limit - 1),
    }
    shim = {
        name: base_request[name]
        for name in (
            "symbol",
            "start",
            "end",
            "interval",
            "initial_cash",
            "commission_rate",
            "min_commission",
            "slippage_bps",
            "data_root",
        )
        if name in base_request
    }
    shim["strategy"] = "sma_cross"
    shim["params"] = execution_params
    normalized = replay_kernel._normalized_config(shim)
    _normalize_execution_config(normalized, limit)
    return normalized


def _apply_trade_to_lots(lots: list[dict[str, Any]], trade: dict[str, Any]) -> None:
    side = trade.get("side")
    fill_stamp = replay_kernel._stamp(trade.get("fill_datetime"), "trade.fill_datetime")
    quantity = trade.get("quantity")
    if type(quantity) is not int or quantity <= 0 or quantity % _LOT_SIZE:
        raise ValueError("inner trade quantity must be a positive board lot")
    day = fill_stamp.date()
    if side == "buy":
        if lots and lots[-1]["date"] == day.isoformat():
            lots[-1]["quantity"] += quantity
        else:
            lots.append({"date": day.isoformat(), "quantity": quantity})
        return
    if side != "sell":
        raise ValueError("inner trade side must be buy or sell")

    eligible = sum(
        lot["quantity"]
        for lot in lots
        if date.fromisoformat(lot["date"]) < day
    )
    if quantity > eligible:
        raise ValueError("inner sell trade exceeds T+1 eligible lots")
    remaining = quantity
    updated: list[dict[str, Any]] = []
    for lot in lots:
        acquired = date.fromisoformat(lot["date"])
        available = lot["quantity"] if acquired < day else 0
        consumed = min(available, remaining)
        kept = lot["quantity"] - consumed
        remaining -= consumed
        if kept:
            updated.append({"date": lot["date"], "quantity": kept})
    if remaining:
        raise ValueError("inner sell trade cannot be reconciled to eligible lots")
    lots[:] = updated


def _rebuild_lots(inner: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    lots: list[dict[str, Any]] = []
    prior_fill: pd.Timestamp | None = None
    known_bars = {item["source_datetime"] for item in inner["bars"]}
    for trade in inner["trades"]:
        if type(trade) is not dict or trade.get("symbol") != symbol:
            raise ValueError("inner trade symbol/shape is invalid")
        fill_stamp = replay_kernel._stamp(trade.get("fill_datetime"), "trade.fill_datetime")
        if prior_fill is not None and fill_stamp < prior_fill:
            raise ValueError("inner trade fill timestamps must be monotonic")
        if fill_stamp.isoformat() not in known_bars:
            raise ValueError("inner trade fill time must match a processed source bar")
        prior_fill = fill_stamp
        _apply_trade_to_lots(lots, trade)
    if sum(item["quantity"] for item in lots) != inner["quantity"]:
        raise ValueError("outer lots do not reconcile to inner quantity")
    expected_entry = lots[0]["date"] if lots else None
    if inner["entry_date"] != expected_entry:
        raise ValueError("inner entry_date must equal the oldest remaining lot date")
    return lots


def _sync_trades(
    outer: dict[str, Any], inner: dict[str, Any], trade_cursor: int, symbol: str
) -> int:
    trades = inner["trades"]
    if trade_cursor > len(trades):
        raise ValueError("trade cursor moved past the inner ledger")
    lots = outer["lots"]
    for trade in trades[trade_cursor:]:
        if type(trade) is not dict or trade.get("symbol") != symbol:
            raise ValueError("new inner trade symbol/shape is invalid")
        _apply_trade_to_lots(lots, trade)
    if sum(item["quantity"] for item in lots) != inner["quantity"]:
        raise ValueError("new fills do not reconcile to the outer lot ledger")
    inner["entry_date"] = lots[0]["date"] if lots else None
    if not lots:
        inner["average_cost"] = 0.0
    return len(trades)


def _eligible_quantity(lots: list[dict[str, Any]], trade_day: date) -> int:
    return sum(
        item["quantity"]
        for item in lots
        if date.fromisoformat(item["date"]) < trade_day
    )


def _history_frame(
    rows: list[dict[str, Any]], symbol: str, history_limit: int
) -> pd.DataFrame:
    visible = rows[-history_limit:]
    fields = ("open", "high", "low", "close", "volume", "turnover")
    frame = pd.DataFrame(
        np.asarray([[item[key] for key in fields] for item in visible], dtype=float),
        columns=pd.Index(fields, dtype=object),
        index=pd.DatetimeIndex(
            [pd.Timestamp(item["source_datetime"]) for item in visible],
            name="source_datetime",
        ),
    )
    frame["source_datetime"] = frame.index
    frame["datetime"] = pd.DatetimeIndex(
        [pd.Timestamp(item["datetime"]) for item in visible]
    )
    return frame


def _context_for_bar(
    config: dict[str, Any],
    inner: dict[str, Any],
    outer: dict[str, Any],
    payload: dict[str, Any],
    history_limit: int,
    *,
    sale_filled_this_bar: bool,
) -> tuple[Bar, Context]:
    symbol = config["symbol"]
    source_time = pd.Timestamp(payload["source_datetime"])
    pending = replay_kernel._pending_order(inner)
    position = {}
    if inner["quantity"] > 0:
        position = {
            "quantity": inner["quantity"],
            "avg_cost": inner["average_cost"],
            "available_to_sell": _eligible_quantity(outer["lots"], source_time.date()),
        }
    params = {
        "commission_rate": config["commission_rate"],
        "min_commission": config["min_commission"],
        "slippage_bps": config["slippage_bps"],
        "stamp_duty_rate": config["stamp_duty_rate"],
        "target_fraction": config["params"]["target_fraction"],
        "max_turnover_participation": config["params"]["max_turnover_participation"],
        # fast/slow are explicitly implementation-only window plumbing.
        "fast": config["params"]["fast"],
        "slow": config["params"]["slow"],
        "_pending_side": pending["side"] if pending else None,
        "_sale_filled_this_bar": sale_filled_this_bar,
    }
    bar = Bar(
        datetime=source_time,
        ohlcv={
            symbol: {
                key: float(payload[key])
                for key in ("open", "high", "low", "close", "volume", "turnover")
            }
        },
    )
    context = Context(
        datetime=source_time,
        cash=float(inner["cash"]),
        positions={symbol: position} if position else {},
        history={symbol: _history_frame(inner["strategy_history"], symbol, history_limit)},
        params=params,
    )
    return bar, context


def _append_rejection(
    inner: dict[str, Any],
    config: dict[str, Any],
    payload: dict[str, Any],
    request: Any,
    reason: str,
) -> None:
    if reason not in _REJECTION_REASONS:
        raise ValueError("unsupported fixed rejection reason")
    invalid = _request_rejection_reason(request, config["symbol"], 10**9)
    if invalid not in (None, "sell_exceeds_t_plus_one_available"):
        raise ValueError("invalid_custom_order_intent:" + invalid)
    requested_quantity = int(request.quantity)
    order = {
        "order_id": f"R{len(inner['orders']) + 1:06d}",
        "symbol": request.symbol,
        "side": request.side,
        "signal_datetime": payload["source_datetime"],
        "signal_bar_start": payload["bar_start_datetime"],
        "requested_quantity": requested_quantity,
        "filled_quantity": 0,
        "remaining_quantity": requested_quantity,
        "average_fill_price": None,
        "fill_notional_total": 0.0,
        "commission_total": 0.0,
        "stamp_duty_total": 0.0,
        "status": "rejected",
        "reason": reason,
        "last_attempt_datetime": payload["source_datetime"],
    }
    inner["orders"].append(order)


def _request_rejection_reason(
    request: Any, symbol: str, eligible_to_sell: int, *, pending_order: bool = False
) -> str | None:
    if not isinstance(request, Order):
        raise ValueError("adapter must return strategy.base.Order objects")
    if not isinstance(request.symbol, str) or request.symbol != symbol:
        return "unsupported_symbol"
    if not isinstance(request.side, str) or request.side not in {"buy", "sell"}:
        return "unsupported_side"
    if request.order_type != "market" or request.limit_price is not None:
        return "unsupported_order_type"
    quantity = request.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        return "invalid_order_quantity"
    try:
        number = float(quantity)
    except (OverflowError, ValueError):
        return "invalid_order_quantity"
    if not math.isfinite(number) or not 0 < number <= 10**9 or not number.is_integer() or int(number) % _LOT_SIZE:
        return "invalid_order_quantity"
    if pending_order:
        return "pending_order_exists"
    if request.side == "sell" and int(number) > eligible_to_sell:
        return "sell_exceeds_t_plus_one_available"
    return None


def _snapshot_adapter(adapter: Any) -> dict[str, Any]:
    try:
        current = _json_copy(
            adapter.state,
            maximum=MAX_USER_STATE_BYTES,
            field="adapter.state",
        )
    except AttributeError as exc:
        raise ValueError("adapter must expose a JSON-object state property") from exc
    # A callback may intentionally update state. The caller's original state is
    # still detached; only this working copy advances.
    return current


class _CallbackProxy:
    """Adapt the caller's stateful callback to the legacy _advance_one hook."""

    def __init__(
        self,
        adapter: Any,
        config: dict[str, Any],
        outer: dict[str, Any],
        inner: dict[str, Any],
        history_limit: int,
        trade_cursor: int,
        bar_trade_start: int,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.outer = outer
        self.inner = inner
        self.history_limit = history_limit
        self.trade_cursor = trade_cursor
        self.bar_trade_start = bar_trade_start
        self.called = False

    def _invoke(
        self,
        bar: Bar,
        context: Context,
        payload: dict[str, Any],
        *,
        pending_order: bool,
    ) -> list[Order]:
        if self.called:
            raise RuntimeError("custom callback invoked more than once for one bar")
        self.called = True
        self.trade_cursor = _sync_trades(
            self.outer, self.inner, self.trade_cursor, self.config["symbol"]
        )
        source_time = pd.Timestamp(payload["source_datetime"])
        position = context.positions.get(self.config["symbol"])
        if position is not None:
            position["available_to_sell"] = _eligible_quantity(
                self.outer["lots"], source_time.date()
            )
        # The v1 context uses slow+1 rows. Keep the custom callback's declared
        # history contract, including the one-row edge case.
        frame = context.history.get(self.config["symbol"])
        if frame is not None:
            context.history[self.config["symbol"]] = frame.tail(self.history_limit).copy()
        requests = self.adapter.on_bar(bar, context)
        self.outer["user_state"] = _snapshot_adapter(self.adapter)
        if requests is None:
            requests = []
        if type(requests) is not list:
            raise ValueError("adapter.on_bar must return a list of Order objects")
        if not requests:
            return []
        eligible = _eligible_quantity(self.outer["lots"], source_time.date())
        request = requests[0]
        if len(requests) > 1:
            _append_rejection(
                self.inner,
                self.config,
                payload,
                request,
                "multiple_orders_per_bar",
            )
            return []
        reason = _request_rejection_reason(
            request,
            self.config["symbol"],
            eligible,
            pending_order=pending_order,
        )
        if reason:
            if reason in {"unsupported_symbol", "unsupported_side", "unsupported_order_type", "invalid_order_quantity"}:
                # Malformed protocol intents cannot be faithfully represented by
                # the v1 ledger. Fail the callback instead of inventing a valid
                # symbol, side or whole-lot quantity for their rejection rows.
                raise ValueError("invalid_custom_order_intent:" + reason)
            _append_rejection(self.inner, self.config, payload, request, reason)
            return []
        return [request]

    def on_bar(self, bar: Bar, context: Context) -> list[Order]:
        # Called from _advance_one only after pending fill processing.
        if not isinstance(bar, Bar) or not isinstance(context, Context):
            raise ValueError("legacy callback supplied an invalid Bar/Context")
        if not context.history.get(self.config["symbol"], pd.DataFrame()).empty:
            row = context.history[self.config["symbol"]].iloc[-1]
            payload = {
                "source_datetime": pd.Timestamp(context.datetime).isoformat(),
                "bar_start_datetime": (
                    pd.Timestamp(context.datetime) - pd.Timedelta(minutes=1)
                ).isoformat(),
                "datetime": (
                    pd.Timestamp(context.datetime) - pd.Timedelta(minutes=1)
                ).isoformat(),
                "symbol": self.config["symbol"],
                **{name: float(row[name]) for name in ("open", "high", "low", "close", "volume", "turnover")},
            }
        else:
            raise ValueError("legacy callback history must include the current closed bar")
        return self._invoke(bar, context, payload, pending_order=False)


def _manual_callback(
    proxy: _CallbackProxy,
    payload: dict[str, Any],
    *,
    pending_order: bool,
) -> None:
    bar, context = _context_for_bar(
        proxy.config,
        proxy.inner,
        proxy.outer,
        payload,
        proxy.history_limit,
        sale_filled_this_bar=any(
            trade.get("side") == "sell"
            for trade in proxy.inner["trades"][proxy.bar_trade_start :]
        ),
    )
    proxy._invoke(bar, context, payload, pending_order=pending_order)


def _validate_outer(config: dict[str, Any], state: Any) -> dict[str, Any]:
    if type(state) is not dict:
        raise ValueError("custom state must be a JSON object")
    if set(state) != _OUTER_FIELDS:
        raise ValueError("custom state schema fields do not match version 2")
    if type(state.get("state_sha256")) is not str or state["state_sha256"] != _outer_digest(state):
        raise ValueError("custom state_sha256 validation failed")
    _canonical_json(state, max_nodes=None, max_depth=128, field="custom state")
    if type(state["schema_version"]) is not int or state["schema_version"] != STATE_SCHEMA_VERSION:
        raise ValueError("unsupported custom state schema version")
    if state["engine"] != ENGINE_ID or state["mode"] != MODE or state["paper_admission"] is not False:
        raise ValueError("custom state must remain RESEARCH_ONLY with paper_admission=false")
    if state["config_sha256"] != replay_kernel._config_digest(config):
        raise ValueError("custom state does not match execution config")
    limit = _require_history_limit(state["history_limit"])
    _normalize_execution_config(config, limit)
    identity = _json_copy(
        state["identity"], maximum=MAX_IDENTITY_BYTES, field="strategy identity"
    )
    if state["identity_sha256"] != _sha256_json(identity, field="strategy identity"):
        raise ValueError("strategy identity hash does not match bound identity")
    user_state = _json_copy(
        state["user_state"], maximum=MAX_USER_STATE_BYTES, field="user_state"
    )
    if user_state != state["user_state"]:
        raise ValueError("user_state is not in canonical JSON form")
    inner = state["inner_v1"]
    replay_kernel._validate_state(config, inner)
    if type(state["lots"]) is not list:
        raise ValueError("lots must be a list")
    rebuilt = _rebuild_lots(inner, config["symbol"])
    lots = state["lots"]
    for index, lot in enumerate(lots):
        if type(lot) is not dict or set(lot) != {"date", "quantity"}:
            raise ValueError(f"lots[{index}] has an invalid shape")
        acquired = replay_kernel._iso_date(lot["date"], f"lots[{index}].date")
        if acquired is None or type(lot["quantity"]) is not int or lot["quantity"] <= 0 or lot["quantity"] % _LOT_SIZE:
            raise ValueError(f"lots[{index}] must contain a date and positive board-lot quantity")
    if lots != rebuilt:
        raise ValueError("outer lots do not match the inner trade ledger")
    return state


def initial_state(
    execution_config: dict[str, Any],
    identity: dict[str, Any],
    *,
    user_state: dict[str, Any],
    history_limit: int = 60,
) -> dict[str, Any]:
    """Create a detached v2 state bound to config, identity and guest state."""
    limit = _require_history_limit(history_limit)
    normalized = _normalize_execution_config(execution_config, limit)
    trusted_identity = _json_copy(
        identity, maximum=MAX_IDENTITY_BYTES, field="strategy identity"
    )
    caller_state = _json_copy(
        user_state, maximum=MAX_USER_STATE_BYTES, field="user_state"
    )
    inner = replay_kernel.initial_state(normalized)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine": ENGINE_ID,
        "mode": MODE,
        "paper_admission": PAPER_ADMISSION,
        "config_sha256": replay_kernel._config_digest(normalized),
        "identity": trusted_identity,
        "identity_sha256": _sha256_json(trusted_identity, field="strategy identity"),
        "history_limit": limit,
        "user_state": caller_state,
        "inner_v1": inner,
        "lots": [],
        "state_sha256": "",
    }
    _seal_outer(state)
    _validate_outer(normalized, state)
    return state


def advance(
    execution_config: dict[str, Any],
    state: dict[str, Any],
    bars: list[dict[str, Any]],
    adapter: Any,
    *,
    final: bool = False,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Advance a detached v2 state through newly appended closed bars.

    The adapter must expose ``symbol``, JSON-object ``state`` and
    ``on_bar(Bar, Context) -> list[Order]``.  It remains owned by the caller;
    close it outside this function and commit the returned state only when
    close succeeds. Any validation/callback exception leaves ``state`` itself
    unchanged and returns no partial account state.
    """
    normalized = replay_kernel._normalized_config(execution_config)
    _validate_outer(normalized, state)
    if state["inner_v1"]["finalized"]:
        raise ValueError("finalized custom replay state cannot advance")
    if type(final) is not bool:
        raise ValueError("final must be boolean")
    if type(bars) is not list:
        raise ValueError("bars must be a list of newly appended canonical payloads")
    if cancelled is not None and not callable(cancelled):
        raise ValueError("cancelled must be callable or None")
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable or None")
    if getattr(adapter, "symbol", None) != normalized["symbol"]:
        raise ValueError("adapter symbol must match the execution config")
    adapter_state = _json_copy(
        adapter.state, maximum=MAX_USER_STATE_BYTES, field="adapter.state"
    )
    if adapter_state != state["user_state"]:
        raise ValueError("adapter must be restored from the state's user_state before advance")

    working = deepcopy(state)
    inner = working["inner_v1"]
    if inner["finalized"]:
        raise ValueError("finalized custom replay state cannot advance")
    start_rows = inner["rows_processed"]
    from .engine import MAX_ENGINE_ROWS

    if start_rows + len(bars) > MAX_ENGINE_ROWS:
        raise ValueError(f"requested data exceeds bounded limit of {MAX_ENGINE_ROWS} bars")
    prior = (
        pd.Timestamp(inner["last_source_datetime"])
        if inner["last_source_datetime"] is not None
        else None
    )
    canonical_bars = []
    for raw in bars:
        canonical = replay_kernel._canonical_bar(normalized, raw)
        current = pd.Timestamp(canonical["source_datetime"])
        if prior is not None and current <= prior:
            raise ValueError("bars must strictly increase after state last_source_datetime")
        canonical_bars.append(canonical)
        prior = current

    limit = working["history_limit"]
    trade_cursor = len(inner["trades"])
    stopped = False
    processed = 0
    for payload in canonical_bars:
        if cancelled is not None and bool(cancelled()):
            pending = replay_kernel._pending_order(inner)
            if pending:
                pending["reason"] = "run_cancelled_before_remaining_quantity_filled"
            stopped = True
            break
        bar_trade_start = len(inner["trades"])
        proxy = _CallbackProxy(
            adapter,
            normalized,
            working,
            inner,
            limit,
            trade_cursor,
            bar_trade_start,
        )
        replay_kernel._advance_one(normalized, inner, payload, proxy)
        trade_cursor = _sync_trades(working, inner, proxy.trade_cursor, normalized["symbol"])
        if not proxy.called:
            # The pending-order path does not call the proxy inside v1.  Its
            # fills have just been synchronized above, so the manual callback
            # starts after those fills rather than applying them twice.
            proxy.trade_cursor = trade_cursor
            pending = replay_kernel._pending_order(inner)
            # The legacy v1 transition deliberately skips strategy calls while
            # an order remains pending. The custom callback contract does not:
            # deliver every bar and reject any overlapping new intent.
            _manual_callback(proxy, payload, pending_order=pending is not None)
            trade_cursor = proxy.trade_cursor
        processed += 1
        if progress:
            progress(start_rows + processed, start_rows + len(canonical_bars))

    ending_cancelled = bool(cancelled and cancelled()) if cancelled is not None else False
    was_cancelled = stopped or ending_cancelled
    if was_cancelled:
        pending = replay_kernel._pending_order(inner)
        if pending:
            pending["reason"] = "run_cancelled_before_remaining_quantity_filled"
        inner["cancelled"] = True
        inner["finalized"] = False
    else:
        if processed or cancelled is not None or final:
            inner["cancelled"] = False
        if final and processed == len(canonical_bars):
            pending = replay_kernel._pending_order(inner)
            if pending and pending["status"] in {"pending", "partial_filled"}:
                pending["reason"] = pending["reason"] or "no_later_bar_before_range_end"
            inner["finalized"] = True

    # Seal both schemas independently; the nested v1 payload remains a valid
    # v1 state and is never rewritten with v2 fields.
    replay_kernel._seal(inner)
    replay_kernel._validate_state(normalized, inner)
    _seal_outer(working)
    _validate_outer(normalized, working)
    return working


def result_from_state(
    execution_config: dict[str, Any],
    state: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Render the v1 account result plus v2 strategy identity/provenance."""
    normalized = replay_kernel._normalized_config(execution_config)
    _validate_outer(normalized, state)
    if type(manifest) is not dict:
        raise ValueError("manifest must be a dictionary")
    result = replay_kernel.result_from_state(normalized, state["inner_v1"], manifest)
    if result["positions"]:
        last_day = (
            pd.Timestamp(state["inner_v1"]["last_source_datetime"]).date()
            if state["inner_v1"]["last_source_datetime"] is not None
            else date.min
        )
        result["positions"][0]["available_to_sell"] = _eligible_quantity(
            state["lots"], last_day
        )
    result["custom_execution"] = {
        "engine": ENGINE_ID,
        "schema_version": STATE_SCHEMA_VERSION,
        "mode": MODE,
        "paper_admission": False,
        "strategy_identity": deepcopy(state["identity"]),
        "identity_sha256": state["identity_sha256"],
        "history_limit": state["history_limit"],
        "open_lots": deepcopy(state["lots"]),
        "callback": "adapter.on_bar(strategy.base.Bar, strategy.base.Context)",
        "execution_plumbing": (
            "Cash, fills and costs were delegated to replay_kernel._advance_one. "
            "The internal SMA-shaped config only provides v1 schema/window plumbing; "
            "no SMA-cross signal was generated by this custom kernel."
        ),
        "adapter_close_required_before_commit": True,
    }
    return result

