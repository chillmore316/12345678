"""Guest-only adapter for the existing ``strategy.base`` strategy contract.

User source is transported as data and is compiled only by the trusted driver
inside the verified VM worker.  This module never imports, compiles, or executes
user source on the host.  It is not wired to the replay engine or UI.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import math
import numbers
from pathlib import Path
import re
import threading
from typing import Any

import numpy as np
import pandas as pd

from strategy.base import Bar, Context, Order
from research_center.vm_strategy_session import (
    MAX_SOURCE_BYTES,
    VMRuntimeSpec,
    VMStrategyError,
    VMStrategySession,
    VMStrategyUnavailable,
    _bounded_json_preflight,
)


API_VERSION = "strategy-base-json-state-v1"
MAX_PARAMS_BYTES = 16 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_API_SOURCE_BYTES = 32 * 1024
MAX_HISTORY_LIMIT = 241  # 240 closed bars plus the kernel's current completed bar.
DEFAULT_HISTORY_LIMIT = 60
_SYMBOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "turnover")
_TIME_FIELDS = ("source_datetime", "end", "datetime", "barstart")
_ALLOWED_HISTORY_FIELDS = frozenset((*_NUMERIC_FIELDS, *_TIME_FIELDS))
_SYSTEM_PARAMS = frozenset(
    {
        "commission_rate",
        "min_commission",
        "slippage_bps",
        "stamp_duty_rate",
        "target_fraction",
        "max_turnover_participation",
        "_pending_side",
        "_sale_filled_this_bar",
    }
)
_DEFAULT_API_SOURCE_PATH = Path(__file__).resolve().parent.parent / "strategy" / "base.py"


class GuestStrategyError(VMStrategyError):
    """Fixed-code failure raised by the host-side bridge."""


# This is trusted adapter code, not strategy source.  It is deliberately small
# enough to fit the worker's startup-source limit; user and API source travel in
# the separately bounded initialization payload.
TRUSTED_DRIVER_SOURCE = b'''\
import json
import math
import sys
import types

_API = "strategy-base-json-state-v1"
_STATE_LIMIT = 16 * 1024
_PARAM_LIMIT = 16 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 10000
_NUMERIC = ("open", "high", "low", "close", "volume", "turnover")
_TIME = ("source_datetime", "end", "datetime", "barstart")
_strategy = None
_base = None
_symbol = None
_metadata = None


def _canonical(value, maximum):
    nodes = 0
    ancestors = set()

    def visit(item, depth):
        nonlocal nodes
        if depth > _MAX_DEPTH:
            raise ValueError("json_depth")
        nodes += 1
        if nodes > _MAX_NODES:
            raise ValueError("json_nodes")
        kind = type(item)
        if item is None or kind in (bool, int):
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("json_nonfinite")
            return
        if kind is str:
            item.encode("utf-8", "strict")
            return
        if kind not in (list, dict):
            raise ValueError("json_type")
        identity = id(item)
        if identity in ancestors:
            raise ValueError("json_cycle")
        ancestors.add(identity)
        try:
            if kind is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError("json_key")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
        finally:
            ancestors.remove(identity)

    visit(value, 0)
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", "strict")
    if len(encoded) > maximum:
        raise ValueError("json_size")
    return encoded


def _require_state(value):
    if type(value) is not dict:
        raise ValueError("state_shape")
    _canonical(value, _STATE_LIMIT)
    return value


def _timestamp(value):
    import pandas as pd
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is not None:
        raise ValueError("timestamp")
    return result


def _install_modules(api_source):
    global _base
    package = types.ModuleType("strategy")
    package.__package__ = "strategy"
    package.__path__ = []
    base = types.ModuleType("strategy.base")
    base.__package__ = "strategy"
    base.__file__ = "<guest-frozen-strategy-base>"
    # Register both modules before executing base.py: dataclass resolves the
    # class module through sys.modules during decoration.
    sys.modules["strategy"] = package
    sys.modules["strategy.base"] = base
    code = compile(api_source, "<guest-frozen-strategy-base>", "exec", dont_inherit=True)
    exec(code, base.__dict__)
    package.base = base
    _base = base


def _init(payload):
    global _strategy, _symbol, _metadata
    expected = {
        "op", "api_version", "source_sha256", "api_source_sha256", "symbol",
        "params", "user_source", "api_source", "restore", "state",
    }
    if type(payload) is not dict or set(payload) != expected or payload["op"] != "init":
        raise ValueError("init_shape")
    if payload["api_version"] != _API or type(payload["symbol"]) is not str:
        raise ValueError("init_api")
    if type(payload["source_sha256"]) is not str or type(payload["api_source_sha256"]) is not str:
        raise ValueError("init_hash")
    params = payload["params"]
    if type(params) is not dict:
        raise ValueError("params_shape")
    _canonical(params, _PARAM_LIMIT)
    user_source = payload["user_source"]
    api_source = payload["api_source"]
    if type(user_source) is not str or type(api_source) is not str:
        raise ValueError("source_shape")
    _install_modules(api_source)
    user_module = types.ModuleType("_guest_user_strategy")
    user_module.__package__ = ""
    user_module.__file__ = "<guest-user-strategy>"
    sys.modules["_guest_user_strategy"] = user_module
    user_code = compile(user_source, "<guest-user-strategy>", "exec", dont_inherit=True)
    exec(user_code, user_module.__dict__)
    factory = getattr(user_module, "build_strategy", None)
    if not callable(factory):
        raise ValueError("factory_missing")
    instance = factory(payload["symbol"], params)
    if not isinstance(instance, _base.Strategy):
        raise ValueError("strategy_type")
    export_state = getattr(instance, "export_state", None)
    restore_state = getattr(instance, "restore_state", None)
    if not callable(export_state) or not callable(restore_state):
        raise ValueError("state_hooks")
    _symbol = payload["symbol"]
    if type(payload["restore"]) is not bool:
        raise ValueError("restore_flag")
    if payload["restore"]:
        original = _require_state(payload["state"])
        original_json = _canonical(original, _STATE_LIMIT)
        restore_state(original)
        restored = _require_state(export_state())
        if _canonical(restored, _STATE_LIMIT) != original_json:
            raise ValueError("state_restore_mismatch")
    elif payload["state"] is not None:
        raise ValueError("unexpected_state")
    _strategy = instance
    initial_state = _require_state(export_state())
    _metadata = {
        "api_version": _API,
        "symbol": _symbol,
        "source_sha256": payload["source_sha256"],
        "api_source_sha256": payload["api_source_sha256"],
    }
    return {"metadata": _metadata, "state": initial_state}


def _bar_call(payload):
    import pandas as pd
    global _strategy
    expected = {"op", "symbol", "datetime", "ohlcv", "history", "cash", "positions", "params"}
    if type(payload) is not dict or set(payload) != expected or payload["op"] != "on_bar":
        raise ValueError("bar_shape")
    if _strategy is None or payload["symbol"] != _symbol:
        raise ValueError("strategy_uninitialized")
    stamp = _timestamp(payload["datetime"])
    ohlcv = payload["ohlcv"]
    if type(ohlcv) is not dict or set(ohlcv) != set(_NUMERIC):
        raise ValueError("ohlcv_shape")
    frame_payload = payload["history"]
    if type(frame_payload) is not dict or set(frame_payload) != {"columns", "index", "rows"}:
        raise ValueError("history_shape")
    columns = frame_payload["columns"]
    index = frame_payload["index"]
    rows = frame_payload["rows"]
    if type(columns) is not list or type(index) is not list or type(rows) is not list:
        raise ValueError("history_shape")
    if len(index) != len(rows) or len(rows) > 241:
        raise ValueError("history_length")
    if not set(_NUMERIC).issubset(columns) or any(
        type(name) is not str or name not in set(_NUMERIC + _TIME) for name in columns
    ) or len(set(columns)) != len(columns):
        raise ValueError("history_columns")
    stamps = [_timestamp(value) for value in index]
    if any(value > stamp for value in stamps):
        raise ValueError("history_future")
    if any(type(row) is not list or len(row) != len(columns) for row in rows):
        raise ValueError("history_rows")
    data = pd.DataFrame(rows, columns=pd.Index(columns, dtype=object),
                        index=pd.DatetimeIndex(stamps, name="source_datetime"))
    for name in columns:
        if name in _TIME:
            values = data[name].tolist()
            parsed = [None if value is None else _timestamp(value) for value in values]
            if any(value is not None and value > stamp for value in parsed):
                raise ValueError("history_column_future")
            data[name] = parsed
    history = {_symbol: data}
    positions = payload["positions"]
    params = payload["params"]
    if type(positions) is not dict or any(key != _symbol for key in positions):
        raise ValueError("positions_shape")
    if type(params) is not dict:
        raise ValueError("params_shape")
    _canonical(params, _PARAM_LIMIT)
    bar = _base.Bar(datetime=stamp, ohlcv={_symbol: ohlcv})
    context = _base.Context(
        datetime=stamp,
        cash=payload["cash"],
        positions=positions,
        history=history,
        params=params,
    )
    orders = _strategy.on_bar(bar, context)
    if type(orders) is not list:
        raise ValueError("orders_shape")
    normalized = []
    import numpy as np
    for order in orders:
        if not isinstance(order, _base.Order):
            raise ValueError("order_type")
        quantity = order.quantity
        if isinstance(quantity, (bool, np.bool_)):
            normalized_quantity = quantity
        elif isinstance(quantity, (int, np.integer)):
            normalized_quantity = int(quantity)
        elif isinstance(quantity, (float, np.floating)):
            number = float(quantity)
            normalized_quantity = int(number) if math.isfinite(number) and number.is_integer() else number
        else:
            normalized_quantity = quantity
        normalized.append({
            "symbol": order.symbol,
            "side": order.side,
            "quantity": normalized_quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
        })
    state = _require_state(_strategy.export_state())
    return {"orders": normalized, "state": state}


def handle(payload):
    if type(payload) is not dict or type(payload.get("op")) is not str:
        raise ValueError("operation")
    if payload["op"] == "init":
        if _strategy is not None:
            raise ValueError("already_initialized")
        return _init(payload)
    if payload["op"] == "on_bar":
        return _bar_call(payload)
    raise ValueError("operation")
'''


def _canonical_json(value: Any, *, maximum: int, code: str) -> bytes:
    try:
        # The SDK preflight bounds strings/nodes before the encoder touches
        # them.  The extra byte is for its newline reservation; canonical JSON
        # itself has no trailing newline.
        _bounded_json_preflight(value, maximum=maximum + 1)
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output = bytearray()
        for text_chunk in encoder.iterencode(value):
            if len(text_chunk) + len(output) > maximum:
                raise ValueError("json_size")
            encoded_chunk = text_chunk.encode("utf-8", "strict")
            if len(encoded_chunk) + len(output) > maximum:
                raise ValueError("json_size")
            output.extend(encoded_chunk)
        encoded = bytes(output)
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise GuestStrategyError(code) from exc
    if len(encoded) > maximum:
        raise GuestStrategyError(code)
    return encoded


def _copy_json_object(value: Any, *, maximum: int, code: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise GuestStrategyError(code)
    _canonical_json(value, maximum=maximum, code=code)
    return deepcopy(value)


def _timestamp(value: Any, code: str) -> pd.Timestamp:
    if isinstance(value, (bool, numbers.Real)) or not isinstance(value, (str, date, datetime)):
        raise GuestStrategyError(code)
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GuestStrategyError(code) from exc
    if pd.isna(stamp) or stamp.tzinfo is not None:
        raise GuestStrategyError(code)
    if stamp.second or stamp.microsecond or stamp.nanosecond:
        raise GuestStrategyError(code)
    return stamp


def _number(value: Any, code: str) -> int | float:
    if isinstance(value, (bool, np.bool_)):
        raise GuestStrategyError(code)
    if type(value) is int or isinstance(value, np.integer):
        return int(value)
    if type(value) is not float and not isinstance(value, np.floating):
        raise GuestStrategyError(code)
    result = float(value)
    if not math.isfinite(result):
        raise GuestStrategyError(code)
    return result


def _timestamp_value(value: Any, current: pd.Timestamp, code: str) -> str | None:
    if value is None or value is pd.NaT:
        return None
    stamp = _timestamp(value, code)
    if stamp > current:
        raise GuestStrategyError("history_future_timestamp")
    return stamp.isoformat()


def _default_api_source() -> bytes:
    try:
        return _DEFAULT_API_SOURCE_PATH.read_bytes()
    except OSError as exc:
        raise GuestStrategyError("api_source_unavailable") from exc


class GuestStrategyAdapter:
    """One persistent VM session adapting a Strategy subclass to the replay API.

    ``history_limit`` accepts 1..241 rows.  The upper bound allows the replay
    kernel's ``slow + 1`` completed-bar window when ``slow`` is at its normal
    240-bar bridge limit.  All input timestamps must be closed and no later than
    ``bar.datetime`` / ``ctx.datetime``.
    """

    def __init__(
        self,
        source: bytes,
        symbol: str,
        params: dict[str, Any],
        runtime: VMRuntimeSpec | None,
        state: dict[str, Any] | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        api_source: bytes | None = None,
    ) -> None:
        # This gate intentionally precedes even a type/length check on user source.
        if runtime is None:
            raise VMStrategyUnavailable("runtime_not_verified")
        if type(runtime) is not VMRuntimeSpec:
            raise VMStrategyUnavailable("runtime_spec_invalid")
        if type(source) is not bytes:
            raise GuestStrategyError("source_type")
        if len(source) > MAX_SOURCE_BYTES:
            raise GuestStrategyError("source_too_large")
        try:
            user_source = source.decode("utf-8-sig", "strict")
        except UnicodeDecodeError as exc:
            raise GuestStrategyError("source_encoding") from exc
        if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
            raise GuestStrategyError("symbol_invalid")
        if type(history_limit) is not int or not 1 <= history_limit <= MAX_HISTORY_LIMIT:
            raise GuestStrategyError("history_limit_invalid")
        user_params = _copy_json_object(params, maximum=MAX_PARAMS_BYTES, code="params_invalid")
        initial_state = None
        if state is not None:
            initial_state = _copy_json_object(state, maximum=MAX_STATE_BYTES, code="state_invalid")
        frozen_api_source = _default_api_source() if api_source is None else api_source
        if type(frozen_api_source) is not bytes or len(frozen_api_source) > MAX_API_SOURCE_BYTES:
            raise GuestStrategyError("api_source_invalid")
        try:
            api_text = frozen_api_source.decode("utf-8-sig", "strict")
        except UnicodeDecodeError as exc:
            raise GuestStrategyError("api_source_encoding") from exc

        self.symbol = symbol
        self.source_sha256 = hashlib.sha256(source).hexdigest()
        self.api_source_sha256 = hashlib.sha256(frozen_api_source).hexdigest()
        self.history_limit = history_limit
        self._user_params = user_params
        self._lock = threading.RLock()
        self._closed = False
        self._close_error: VMStrategyError | None = None
        self._state: dict[str, Any] | None = None
        self._metadata: dict[str, Any] | None = None
        self._session: VMStrategySession | None = None

        try:
            self._session = VMStrategySession(TRUSTED_DRIVER_SOURCE, runtime)
            response = self._session.exchange(
                {
                    "op": "init",
                    "api_version": API_VERSION,
                    "source_sha256": self.source_sha256,
                    "api_source_sha256": self.api_source_sha256,
                    "symbol": symbol,
                    "params": user_params,
                    "user_source": user_source,
                    "api_source": api_text,
                    "restore": initial_state is not None,
                    "state": initial_state,
                }
            )
            metadata, current_state = self._validate_initial_response(response)
            self._metadata = metadata
            self._state = current_state
        except BaseException as exc:
            self._abort(exc)
            raise

    @property
    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if self._metadata is None:
                raise GuestStrategyError("session_uninitialized")
            return deepcopy(self._metadata)

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                raise GuestStrategyError("session_uninitialized")
            return deepcopy(self._state)

    def on_bar(self, bar: Bar, ctx: Context) -> list[Order]:
        with self._lock:
            if self._closed or self._session is None:
                raise GuestStrategyError("session_closed")
            try:
                payload = self._bar_payload(bar, ctx)
                response = self._session.exchange(payload)
                orders, current_state = self._validate_bar_response(response)
                self._state = current_state
                return orders
            except BaseException as exc:
                self._abort(exc)
                raise

    def _bar_payload(self, bar: Bar, ctx: Context) -> dict[str, Any]:
        if type(bar) is not Bar or type(ctx) is not Context:
            raise GuestStrategyError("bar_context_type")
        bar_time = _timestamp(bar.datetime, "bar_datetime_invalid")
        context_time = _timestamp(ctx.datetime, "context_datetime_invalid")
        if bar_time != context_time:
            raise GuestStrategyError("bar_context_time_mismatch")

        if type(bar.ohlcv) is not dict or set(bar.ohlcv) != {self.symbol}:
            raise GuestStrategyError("bar_symbol_invalid")
        event = bar.ohlcv[self.symbol]
        if type(event) is not dict or not set(_NUMERIC_FIELDS).issubset(event):
            raise GuestStrategyError("bar_ohlcv_invalid")
        if set(event) - _ALLOWED_HISTORY_FIELDS:
            raise GuestStrategyError("bar_ohlcv_invalid")
        event_values: dict[str, Any] = {}
        for name in _NUMERIC_FIELDS:
            if name not in event:
                raise GuestStrategyError("bar_ohlcv_invalid")
            event_values[name] = _number(event[name], "bar_ohlcv_invalid")
        for name in _TIME_FIELDS:
            if name in event:
                # Optional endpoint/start metadata is validated here, while
                # Bar.datetime and the history columns carry time in the guest.
                _timestamp_value(event[name], bar_time, "bar_ohlcv_invalid")

        if type(ctx.history) is not dict or set(ctx.history) - {self.symbol}:
            raise GuestStrategyError("history_symbol_invalid")
        frame = ctx.history.get(self.symbol)
        if not isinstance(frame, pd.DataFrame):
            raise GuestStrategyError("history_invalid")
        if len(frame) > self.history_limit:
            raise GuestStrategyError("history_limit_exceeded")
        columns = list(frame.columns)
        if any(type(name) is not str or name not in _ALLOWED_HISTORY_FIELDS for name in columns):
            raise GuestStrategyError("history_columns_invalid")
        if len(set(columns)) != len(columns) or not set(_NUMERIC_FIELDS).issubset(columns):
            raise GuestStrategyError("history_columns_invalid")
        indices = [_timestamp(value, "history_timestamp_invalid") for value in frame.index]
        if any(value > context_time for value in indices):
            raise GuestStrategyError("history_future_timestamp")
        if any(left >= right for left, right in zip(indices, indices[1:])):
            raise GuestStrategyError("history_order_invalid")
        if not indices or indices[-1] != context_time:
            raise GuestStrategyError("history_current_bar_missing")
        rows: list[list[Any]] = []
        for row_index in range(len(frame)):
            row: list[Any] = []
            for name in columns:
                value = frame.iloc[row_index][name]
                if name in _NUMERIC_FIELDS:
                    row.append(_number(value, "history_value_invalid"))
                else:
                    row.append(_timestamp_value(value, context_time, "history_timestamp_invalid"))
            rows.append(row)

        if type(ctx.positions) is not dict or set(ctx.positions) - {self.symbol}:
            raise GuestStrategyError("positions_invalid")
        position: dict[str, Any] = {}
        raw_position = ctx.positions.get(self.symbol, {})
        if type(raw_position) is not dict or set(raw_position) - {"quantity", "avg_cost", "available_to_sell"}:
            raise GuestStrategyError("positions_invalid")
        for name, value in raw_position.items():
            position[name] = _number(value, "positions_invalid")
        positions = {self.symbol: position} if position else {}

        cash = _number(ctx.cash, "cash_invalid")
        context_params = _copy_json_object(
            ctx.params, maximum=MAX_PARAMS_BYTES, code="context_params_invalid"
        )
        effective_params = dict(context_params)
        effective_params.update(self._user_params)
        # User strategy parameters may override generic context keys such as
        # fast/slow, but current costs, risk sizing and execution flags remain
        # sourced from the engine context.
        for name in _SYSTEM_PARAMS:
            if name in context_params:
                effective_params[name] = context_params[name]
            else:
                effective_params.pop(name, None)
        _canonical_json(effective_params, maximum=MAX_PARAMS_BYTES, code="params_invalid")
        return {
            "op": "on_bar",
            "symbol": self.symbol,
            "datetime": context_time.isoformat(),
            "ohlcv": event_values,
            "history": {
                "columns": columns,
                "index": [value.isoformat() for value in indices],
                "rows": rows,
            },
            "cash": cash,
            "positions": positions,
            "params": effective_params,
        }

    def _validate_initial_response(self, response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if type(response) is not dict or set(response) != {"metadata", "state"}:
            raise GuestStrategyError("guest_init_result_invalid")
        metadata = response["metadata"]
        expected_metadata = {
            "api_version": API_VERSION,
            "symbol": self.symbol,
            "source_sha256": self.source_sha256,
            "api_source_sha256": self.api_source_sha256,
        }
        if type(metadata) is not dict or set(metadata) != set(expected_metadata):
            raise GuestStrategyError("guest_metadata_invalid")
        if metadata != expected_metadata:
            raise GuestStrategyError("guest_metadata_mismatch")
        state = _copy_json_object(response["state"], maximum=MAX_STATE_BYTES, code="guest_state_invalid")
        return deepcopy(metadata), state

    def _validate_bar_response(self, response: Any) -> tuple[list[Order], dict[str, Any]]:
        if type(response) is not dict or set(response) != {"orders", "state"}:
            raise GuestStrategyError("guest_result_invalid")
        raw_orders = response["orders"]
        if type(raw_orders) is not list or len(raw_orders) > 1:
            raise GuestStrategyError("orders_invalid")
        orders: list[Order] = []
        expected_keys = {"symbol", "side", "quantity", "order_type", "limit_price"}
        for item in raw_orders:
            if type(item) is not dict or set(item) != expected_keys:
                raise GuestStrategyError("order_fields_invalid")
            if item["symbol"] != self.symbol or type(item["symbol"]) is not str:
                raise GuestStrategyError("order_symbol_invalid")
            if type(item["side"]) is not str or item["side"] not in {"buy", "sell"}:
                raise GuestStrategyError("order_side_invalid")
            quantity = item["quantity"]
            if type(quantity) is not int or quantity <= 0 or quantity > 1_000_000_000 or quantity % 100:
                raise GuestStrategyError("order_quantity_invalid")
            if type(item["order_type"]) is not str or item["order_type"] != "market":
                raise GuestStrategyError("order_type_invalid")
            if item["limit_price"] is not None:
                raise GuestStrategyError("order_limit_unsupported")
            orders.append(Order(symbol=self.symbol, side=item["side"], quantity=quantity))
        state = _copy_json_object(response["state"], maximum=MAX_STATE_BYTES, code="guest_state_invalid")
        return orders, state

    def _abort(self, cause: BaseException) -> None:
        session = self._session
        if session is None:
            return
        if self._close_error is not None:
            raise self._close_error from cause
        if self._closed:
            return
        self._closed = True
        try:
            session.close()
        except BaseException as close_error:
            if isinstance(close_error, VMStrategyError):
                self._close_error = close_error
            else:
                self._close_error = GuestStrategyError("session_close_failed")
            raise self._close_error from cause

    def close(self) -> None:
        with self._lock:
            if self._closed:
                if self._close_error is not None:
                    raise self._close_error
                return
            self._closed = True
            session = self._session
            if session is not None:
                try:
                    session.close()
                except VMStrategyError as exc:
                    self._close_error = exc
                    raise
                except BaseException as exc:
                    self._close_error = GuestStrategyError("session_close_failed")
                    raise self._close_error from exc

    def __enter__(self) -> "GuestStrategyAdapter":
        with self._lock:
            if self._closed:
                raise GuestStrategyError("session_closed")
            return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

