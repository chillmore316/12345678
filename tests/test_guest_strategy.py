"""Synthetic guest-bridge checks; these tests never launch the real VM."""

from __future__ import annotations

import builtins
from copy import deepcopy
import hashlib
import json
import math
import sys
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

from research_center import guest_strategy as guest
from research_center.vm_strategy_session import (
    DEFAULT_INSTANCE,
    DEFAULT_LIMA_HOME,
    VMRuntimeSpec,
    VMStrategyError,
    VMStrategyUnavailable,
)
from strategy.base import Bar, Context, Order


SYMBOL = "600000.SH"
_MISSING = object()


def _runtime() -> VMRuntimeSpec:
    return VMRuntimeSpec(
        expected_instance_yaml_sha256="a" * 64,
        expected_resolved_config_sha256="b" * 64,
        immutable_image_ref=f"quant-strategy-worker@sha256:{'c' * 64}",
        worker_sha256="d" * 64,
        lima_home=DEFAULT_LIMA_HOME,
        instance=DEFAULT_INSTANCE,
    )


def _context(
    *,
    stamp: str = "2024-01-02T09:31:00",
    history: pd.DataFrame | None = None,
    params: dict | None = None,
) -> tuple[Bar, Context]:
    current = pd.Timestamp(stamp)
    bar = Bar(
        datetime=current,
        ohlcv={
            SYMBOL: {
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume": 1000.0,
                "turnover": 10200.0,
            }
        },
    )
    if history is None:
        history = _history(["2024-01-02T09:30:00", stamp])
    context = Context(
        datetime=current,
        cash=100_000.0,
        positions={SYMBOL: {"quantity": 0, "avg_cost": 0.0, "available_to_sell": 0}},
        history={SYMBOL: history},
        params=params or {},
    )
    return bar, context


def _history(stamps: list[str], *, optional: bool = False) -> pd.DataFrame:
    columns = ["open", "high", "low", "close", "volume", "turnover"]
    rows = [
        [10.0 + i, 10.5 + i, 9.9 + i, 10.2 + i, 1000.0 + i, 10200.0 + i]
        for i in range(len(stamps))
    ]
    if optional:
        columns += ["source_datetime", "datetime", "end", "barstart"]
        for index, stamp in enumerate(stamps):
            end = pd.Timestamp(stamp)
            start = end - pd.Timedelta(minutes=1)
            rows[index] += [end, start, end, start]
    return pd.DataFrame(
        rows,
        columns=pd.Index(columns, dtype=object),
        index=pd.DatetimeIndex(stamps, name="source_datetime"),
    )


class _MemoryGuestSession:
    """In-memory interpreter for the trusted driver and benign fixtures only."""

    def __init__(self, source: bytes, runtime: VMRuntimeSpec) -> None:
        if source != guest.TRUSTED_DRIVER_SOURCE or type(runtime) is not VMRuntimeSpec:
            raise AssertionError("unexpected fake-session startup")
        self.original_modules = {
            name: sys.modules.get(name, _MISSING)
            for name in ("strategy", "strategy.base", "_guest_user_strategy")
        }
        self.namespace: dict[str, object] = {"__name__": "_guest_driver_test"}
        # Only the fixed trusted driver is evaluated here. Strategy fixtures
        # below are known benign and run only through its guest-side handle().
        exec(compile(source, "<trusted-driver-test>", "exec", dont_inherit=True), self.namespace)
        self.closed = False
        self.close_error: BaseException | None = None
        self.exchange_count = 0
        self.active = 0
        self.max_active = 0
        self._mutex = threading.Lock()

    def exchange(self, payload):  # type: ignore[no-untyped-def]
        if self.closed:
            raise VMStrategyUnavailable("session_closed")
        with self._mutex:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.exchange_count += 1
        try:
            if payload.get("op") == "on_bar":
                time.sleep(0.01)
            try:
                result = self.namespace["handle"](payload)  # type: ignore[operator]
                wire = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                if len(wire.encode("utf-8")) > 64 * 1024:
                    raise ValueError("response limit")
                return json.loads(wire)
            except BaseException as exc:
                raise VMStrategyError("callback_failed") from exc
        finally:
            with self._mutex:
                self.active -= 1

    def close(self) -> None:
        self.closed = True
        for name, original in self.original_modules.items():
            if original is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        if self.close_error is not None:
            raise self.close_error


class _ScriptedSession:
    """Fake transport for host-only validation and lifecycle tests."""

    def __init__(self, source: bytes, runtime: VMRuntimeSpec, *, result=None, close_error=None):
        if source != guest.TRUSTED_DRIVER_SOURCE or type(runtime) is not VMRuntimeSpec:
            raise AssertionError("unexpected fake-session startup")
        self.result = result
        self.close_error = close_error
        self.closed = False
        self.exchanges: list[dict] = []
        self.on_bar_calls = 0
        self.active = 0
        self.max_active = 0
        self._mutex = threading.Lock()

    def exchange(self, payload):  # type: ignore[no-untyped-def]
        with self._mutex:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.exchanges.append(deepcopy(payload))
            if payload.get("op") == "on_bar":
                self.on_bar_calls += 1
        try:
            if payload.get("op") == "on_bar":
                time.sleep(0.01)
            if payload.get("op") == "init":
                return {
                    "metadata": {
                        "api_version": guest.API_VERSION,
                        "symbol": payload["symbol"],
                        "source_sha256": payload["source_sha256"],
                        "api_source_sha256": payload["api_source_sha256"],
                    },
                    "state": {},
                }
            if callable(self.result):
                return self.result(payload)
            if self.result is not None:
                return deepcopy(self.result)
            return {"orders": [], "state": {}}
        finally:
            with self._mutex:
                self.active -= 1

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _make_adapter(session_cls, **kwargs):  # type: ignore[no-untyped-def]
    source = kwargs.pop("source", b"# harmless fixture")
    params = kwargs.pop("params", {})
    with patch.object(guest, "VMStrategySession", session_cls):
        adapter = guest.GuestStrategyAdapter(
            source=source,
            symbol=SYMBOL,
            params=params,
            runtime=_runtime(),
            **kwargs,
        )
    return adapter


class GuestDriverTests(unittest.TestCase):
    def test_driver_source_stays_bounded_and_strategy_state_roundtrips(self) -> None:
        user_source = b'''\
import numpy as np
from strategy.base import Order, Strategy

class Counter(Strategy):
    def __init__(self, symbol):
        self.symbol = symbol
        self.count = 0
    def on_bar(self, bar, ctx):
        self.count += 1
        return [Order(self.symbol, "buy", np.int64(100))] if self.count == 1 else []
    def export_state(self):
        return {"count": self.count}
    def restore_state(self, state):
        self.count = state["count"]

def build_strategy(symbol, params):
    return Counter(symbol)
'''
        self.assertLessEqual(len(guest.TRUSTED_DRIVER_SOURCE), 64 * 1024)
        sessions: list[_MemoryGuestSession] = []

        class CaptureSession(_MemoryGuestSession):
            def __init__(self, source, runtime):
                super().__init__(source, runtime)
                sessions.append(self)

        with patch.object(guest, "VMStrategySession", CaptureSession):
            with guest.GuestStrategyAdapter(user_source, SYMBOL, {}, _runtime()) as adapter:
                self.assertEqual(adapter.metadata["api_version"], guest.API_VERSION)
                bar, ctx = _context()
                orders = adapter.on_bar(bar, ctx)
                self.assertEqual(orders, [Order(SYMBOL, "buy", 100)])
                self.assertEqual(adapter.state, {"count": 1})
                snapshot = adapter.state
                snapshot["count"] = 999
                self.assertEqual(adapter.state, {"count": 1})
                self.assertEqual(adapter.on_bar(bar, ctx), [])
                self.assertEqual(adapter.state, {"count": 2})

        with patch.object(guest, "VMStrategySession", CaptureSession):
            with guest.GuestStrategyAdapter(
                user_source, SYMBOL, {}, _runtime(), state={"count": 1}
            ) as restored:
                self.assertEqual(restored.state, {"count": 1})
                bar, ctx = _context()
                self.assertEqual(restored.on_bar(bar, ctx), [])
                self.assertEqual(restored.state, {"count": 2})
        self.assertTrue(all(session.closed for session in sessions))

    def test_guest_rebuilds_closed_history_with_object_column_labels_and_protected_params(self) -> None:
        source = b'''\
from strategy.base import Strategy

class Inspect(Strategy):
    def __init__(self, symbol):
        self.symbol = symbol
        self.observed = {}
    def on_bar(self, bar, ctx):
        history = ctx.history[self.symbol]
        self.observed = {
            "bar_time": bar.datetime.isoformat(),
            "context_time": ctx.datetime.isoformat(),
            "event_fields": list(bar.ohlcv[self.symbol]),
            "index": [value.isoformat() for value in history.index],
            "columns": [str(value) for value in history.columns],
            "object_columns": history.columns.dtype == object,
            "closes": [float(value) for value in history["close"].tolist()],
            "source_times": [value.isoformat() for value in history["source_datetime"].tolist()],
            "barstarts": [value.isoformat() for value in history["barstart"].tolist()],
            "cash": ctx.cash,
            "position": ctx.positions[self.symbol]["quantity"],
            "target": ctx.params["target_fraction"],
            "fast": ctx.params["fast"],
            "slow": ctx.params["slow"],
            "commission": ctx.params["commission_rate"],
            "participation": ctx.params["max_turnover_participation"],
            "pending": ctx.params["_pending_side"],
            "sale_filled": ctx.params["_sale_filled_this_bar"],
            "custom": ctx.params["custom"],
        }
        return []
    def export_state(self):
        return self.observed
    def restore_state(self, state):
        self.observed = state

def build_strategy(symbol, params):
    return Inspect(symbol)
'''
        history = _history(["2024-01-02T09:30:00", "2024-01-02T09:31:00"], optional=True)
        params = {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "slippage_bps": 5.0,
            "stamp_duty_rate": 0.0005,
            "target_fraction": 0.4,
            "max_turnover_participation": 0.01,
            "fast": 1,
            "slow": 60,
            "_pending_side": "buy",
            "_sale_filled_this_bar": False,
        }
        bar, ctx = _context(history=history, params=params)
        bar.ohlcv[SYMBOL].update(
            {
                "end": bar.datetime,
                "barstart": bar.datetime - pd.Timedelta(minutes=1),
            }
        )
        with patch.object(guest, "VMStrategySession", _MemoryGuestSession):
            with guest.GuestStrategyAdapter(
                source,
                SYMBOL,
                {
                    "target_fraction": 0.99,
                    "fast": 5,
                    "slow": 20,
                    "commission_rate": 9.0,
                    "max_turnover_participation": 0.9,
                    "_pending_side": "sell",
                    "_sale_filled_this_bar": True,
                    "custom": "from-user",
                },
                _runtime(),
            ) as adapter:
                self.assertEqual(adapter.on_bar(bar, ctx), [])
                self.assertEqual(
                    adapter.state,
                    {
                        "bar_time": "2024-01-02T09:31:00",
                        "context_time": "2024-01-02T09:31:00",
                        "event_fields": ["open", "high", "low", "close", "volume", "turnover"],
                        "index": ["2024-01-02T09:30:00", "2024-01-02T09:31:00"],
                        "columns": [
                            "open", "high", "low", "close", "volume", "turnover",
                            "source_datetime", "datetime", "end", "barstart",
                        ],
                        "object_columns": True,
                        "closes": [10.2, 11.2],
                        "source_times": ["2024-01-02T09:30:00", "2024-01-02T09:31:00"],
                        "barstarts": ["2024-01-02T09:29:00", "2024-01-02T09:30:00"],
                        "cash": 100000.0,
                        "position": 0,
                        "target": 0.4,
                        "fast": 5,
                        "slow": 20,
                        "commission": 0.00025,
                        "participation": 0.01,
                        "pending": "buy",
                        "sale_filled": False,
                        "custom": "from-user",
                    },
                )


class GuestHostValidationTests(unittest.TestCase):
    def test_host_rejects_future_history_and_mismatched_context_time_before_exchange(self) -> None:
        invalid_histories = [
            (["2024-01-02T09:31:00", "2024-01-02T09:32:00"], "history_future_timestamp"),
            (["2024-01-02T09:30:00", "2024-01-02T09:31:00", "2024-01-02T09:31:00"], "history_order_invalid"),
            (["2024-01-02T09:31:00", "2024-01-02T09:30:00"], "history_order_invalid"),
            (["2024-01-02T09:30:00"], "history_current_bar_missing"),
            ([], "history_current_bar_missing"),
        ]
        for stamps, expected_code in invalid_histories:
            with self.subTest(stamps=stamps):
                sessions = []

                class Capture(_ScriptedSession):
                    def __init__(self, source, runtime):
                        super().__init__(source, runtime)
                        sessions.append(self)

                with patch.object(guest, "VMStrategySession", Capture):
                    adapter = guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime())
                bar, ctx = _context(history=_history(stamps))
                with self.assertRaises(guest.GuestStrategyError) as caught:
                    adapter.on_bar(bar, ctx)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(sessions[0].closed)
                self.assertEqual(sessions[0].on_bar_calls, 0)

        with patch.object(guest, "VMStrategySession", _ScriptedSession):
            adapter = guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime())
        self.addCleanup(adapter.close)
        bar, ctx = _context()
        ctx.datetime += pd.Timedelta(minutes=1)
        with self.assertRaises(guest.GuestStrategyError) as mismatch:
            adapter.on_bar(bar, ctx)
        self.assertEqual(mismatch.exception.code, "bar_context_time_mismatch")

    def test_host_independently_rejects_all_invalid_order_shapes(self) -> None:
        invalid_orders = [
            ([{"symbol": "000001.SZ", "side": "buy", "quantity": 100, "order_type": "market", "limit_price": None}], "order_symbol_invalid"),
            ([{"symbol": SYMBOL, "side": "hold", "quantity": 100, "order_type": "market", "limit_price": None}], "order_side_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100.0, "order_type": "market", "limit_price": None}], "order_quantity_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": True, "order_type": "market", "limit_price": None}], "order_quantity_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 101, "order_type": "market", "limit_price": None}], "order_quantity_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 1_000_000_100, "order_type": "market", "limit_price": None}], "order_quantity_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100, "order_type": "limit", "limit_price": 10.0}], "order_type_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100, "order_type": "market", "limit_price": 10.0}], "order_limit_unsupported"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100, "order_type": "market", "limit_price": None, "extra": 1}], "order_fields_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100, "order_type": "market", "limit_price": None}] * 2, "orders_invalid"),
            ([{"symbol": SYMBOL, "side": "buy", "quantity": 100, "order_type": "market", "limit_price": math.nan}], "order_limit_unsupported"),
        ]
        for orders, expected_code in invalid_orders:
            with self.subTest(expected_code=expected_code, orders=orders):
                with patch.object(
                    guest,
                    "VMStrategySession",
                    lambda source, runtime, result={"orders": orders, "state": {}}: _ScriptedSession(
                        source, runtime, result=result
                    ),
                ):
                    adapter = guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime())
                bar, ctx = _context()
                with self.assertRaises(guest.GuestStrategyError) as caught:
                    adapter.on_bar(bar, ctx)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(adapter._closed)

    def test_valid_market_order_and_guest_state_are_accepted(self) -> None:
        with patch.object(
            guest,
            "VMStrategySession",
            lambda source, runtime: _ScriptedSession(
                source,
                runtime,
                result={
                    "orders": [{
                        "symbol": SYMBOL,
                        "side": "sell",
                        "quantity": 200,
                        "order_type": "market",
                        "limit_price": None,
                    }],
                    "state": {"ticks": 3},
                },
            ),
        ):
            with guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime()) as adapter:
                bar, ctx = _context()
                self.assertEqual(adapter.on_bar(bar, ctx), [Order(SYMBOL, "sell", 200)])
                self.assertEqual(adapter.state, {"ticks": 3})

    def test_caps_validate_params_and_state_on_host(self) -> None:
        with patch.object(guest, "VMStrategySession", _ScriptedSession):
            with self.assertRaises(guest.GuestStrategyError) as state_error:
                guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime(), state={"x": "a" * 17_000})
            self.assertEqual(state_error.exception.code, "state_invalid")
            with self.assertRaises(guest.GuestStrategyError) as params_error:
                guest.GuestStrategyAdapter(b"x", SYMBOL, {"x": "a" * 17_000}, _runtime())
            self.assertEqual(params_error.exception.code, "params_invalid")
            with self.assertRaises(guest.GuestStrategyError) as limit_error:
                guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime(), history_limit=242)
            self.assertEqual(limit_error.exception.code, "history_limit_invalid")

    def test_guest_state_restore_mismatch_and_guest_state_overflow_fail_closed(self) -> None:
        mismatch_source = b'''\
from strategy.base import Strategy
class BadRestore(Strategy):
    def on_bar(self, bar, ctx): return []
    def export_state(self): return {"count": 0}
    def restore_state(self, state): pass
def build_strategy(symbol, params): return BadRestore()
'''
        with patch.object(guest, "VMStrategySession", _MemoryGuestSession):
            with self.assertRaises(VMStrategyError) as mismatch:
                guest.GuestStrategyAdapter(mismatch_source, SYMBOL, {}, _runtime(), state={"count": 1})
        self.assertEqual(mismatch.exception.code, "callback_failed")

        oversized_source = b'''\
from strategy.base import Strategy
class TooLarge(Strategy):
    def on_bar(self, bar, ctx): return []
    def export_state(self): return {"blob": "x" * (17 * 1024)}
    def restore_state(self, state): pass
def build_strategy(symbol, params): return TooLarge()
'''
        with patch.object(guest, "VMStrategySession", _MemoryGuestSession):
            with self.assertRaises(VMStrategyError) as too_large:
                guest.GuestStrategyAdapter(oversized_source, SYMBOL, {}, _runtime())
        self.assertEqual(too_large.exception.code, "callback_failed")

        missing_hooks_source = b'''\
from strategy.base import Strategy
class MissingHooks(Strategy):
    def on_bar(self, bar, ctx): return []
def build_strategy(symbol, params): return MissingHooks()
'''
        with patch.object(guest, "VMStrategySession", _MemoryGuestSession):
            with self.assertRaises(VMStrategyError) as missing_hooks:
                guest.GuestStrategyAdapter(missing_hooks_source, SYMBOL, {}, _runtime())
        self.assertEqual(missing_hooks.exception.code, "callback_failed")

    def test_missing_runtime_fails_before_inspecting_source(self) -> None:
        class PoisonSource:
            def __bytes__(self):
                raise AssertionError("source was inspected")

            def __len__(self):
                raise AssertionError("source was inspected")

        with self.assertRaises(VMStrategyUnavailable) as caught:
            guest.GuestStrategyAdapter(PoisonSource(), SYMBOL, {}, None)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "runtime_not_verified")

    def test_poison_fixture_is_only_transported_and_host_never_compiles_it(self) -> None:
        poison = b"\xef\xbb\xbfraise RuntimeError('MUST_NOT_RUN_ON_HOST')"
        captured = None

        class Capture(_ScriptedSession):
            def exchange(self, payload):  # type: ignore[no-untyped-def]
                nonlocal captured
                captured = deepcopy(payload)
                return super().exchange(payload)

        with patch.object(guest, "VMStrategySession", Capture), patch.object(
            builtins,
            "compile",
            side_effect=AssertionError("host attempted to compile user source"),
        ):
            with guest.GuestStrategyAdapter(poison, SYMBOL, {}, _runtime()) as adapter:
                self.assertEqual(adapter.source_sha256, hashlib.sha256(poison).hexdigest())
        self.assertEqual(captured["user_source"], poison.decode("utf-8-sig"))
        self.assertEqual(captured["source_sha256"], adapter.source_sha256)

    def test_close_error_propagates_and_calls_are_serialized(self) -> None:
        instances: list[_ScriptedSession] = []

        class Capture(_ScriptedSession):
            def __init__(self, source, runtime):
                super().__init__(source, runtime)
                instances.append(self)

        with patch.object(guest, "VMStrategySession", Capture):
            adapter = guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime())
        session = instances[-1]
        bar, ctx = _context()
        errors: list[BaseException] = []

        def call() -> None:
            try:
                adapter.on_bar(bar, ctx)
            except BaseException as exc:  # surfaced in the main test thread
                errors.append(exc)

        workers = [threading.Thread(target=call) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertFalse(errors)
        self.assertEqual(session.max_active, 1)
        self.assertEqual(session.on_bar_calls, 4)

        session.close_error = VMStrategyUnavailable("worker_stderr_limit")
        with self.assertRaises(VMStrategyUnavailable) as close_error:
            adapter.close()
        self.assertEqual(close_error.exception.code, "worker_stderr_limit")
        with self.assertRaises(VMStrategyUnavailable) as repeated_close_error:
            adapter.close()
        self.assertEqual(repeated_close_error.exception.code, "worker_stderr_limit")

        with patch.object(guest, "VMStrategySession", Capture):
            failed = guest.GuestStrategyAdapter(b"x", SYMBOL, {}, _runtime())
        failed_session = instances[-1]
        failed_session.result = {"unexpected": True}
        failed_session.close_error = VMStrategyUnavailable("worker_stderr_limit")
        with self.assertRaises(VMStrategyUnavailable) as abort_close_error:
            failed.on_bar(bar, ctx)
        self.assertEqual(abort_close_error.exception.code, "worker_stderr_limit")
        with self.assertRaises(VMStrategyUnavailable) as sticky_abort_error:
            failed.close()
        self.assertEqual(sticky_abort_error.exception.code, "worker_stderr_limit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
