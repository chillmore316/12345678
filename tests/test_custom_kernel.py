from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from strategy.base import Bar, Context, Order
from research_center import custom_kernel, replay_kernel
from research_center.strategies import SmaCrossStrategy


IDENTITY = {
    "source_sha256": "a" * 64,
    "api_sha256": "b" * 64,
    "driver_sha256": "c" * 64,
    "algorithm_params_sha256": "d" * 64,
}


def bars_for_days(days: list[str], closes: list[float] | None = None, per_day: int = 12):
    if closes is None:
        closes = [10.0] * (len(days) * per_day)
    result = []
    index = 0
    for day in days:
        for minute in range(per_day):
            stamp = datetime.fromisoformat(f"{day} 09:31:00") + timedelta(minutes=minute)
            close = float(closes[index])
            result.append(
                {
                    "symbol": "600000.SSE",
                    "source_datetime": stamp.isoformat(),
                    "datetime": (stamp - timedelta(minutes=1)).isoformat(),
                    "open": close,
                    "high": close + 0.02,
                    "low": close - 0.02,
                    "close": close,
                    "volume": 1_000_000.0,
                    "turnover": 100_000_000.0,
                }
            )
            index += 1
    return result


def compact_bars(spec: list[tuple[str, str, float]], *, turnover: float = 1_000_000.0):
    result = []
    for day, hm, close in spec:
        stamp = datetime.fromisoformat(f"{day} {hm}:00")
        result.append(
            {
                "symbol": "600000.SSE",
                "source_datetime": stamp.isoformat(),
                "datetime": (stamp - timedelta(minutes=1)).isoformat(),
                "open": float(close),
                "high": float(close) + 0.02,
                "low": float(close) - 0.02,
                "close": float(close),
                "volume": 1_000_000.0,
                "turnover": turnover,
            }
        )
    return result


def config(*, start="2024-01-02", end="2024-01-05", history_limit=6, **overrides):
    request = {
        "symbol": "600000.SSE",
        "start": start,
        "end": end,
        "interval": "1m",
        "initial_cash": 100_000.0,
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "slippage_bps": 5.0,
        "params": {
            "target_fraction": 0.5,
            "max_turnover_participation": 0.1,
            # Algorithm parameters are intentionally discarded by the shim.
            "user_lookback": 17,
        },
    }
    request.update(overrides)
    return custom_kernel.make_execution_config(request, history_limit=history_limit)


class FakeAdapter:
    def __init__(self, state=None, *, symbol="600000.SSE", callback=None):
        self.symbol = symbol
        self._state = deepcopy(state if state is not None else {"calls": 0})
        self.callback = callback

    @property
    def state(self):
        return deepcopy(self._state)

    def on_bar(self, bar: Bar, ctx: Context):
        current = deepcopy(self._state)
        index = current.get("calls", 0)
        current["calls"] = index + 1
        self._state = current
        if self.callback:
            return self.callback(index, bar, ctx)
        return []


class BuiltinSmaAdapter(FakeAdapter):
    def __init__(self, execution_config, state=None):
        super().__init__(state)
        params = execution_config["params"]
        self.strategy = SmaCrossStrategy(
            execution_config["symbol"], params["fast"], params["slow"]
        )

    def on_bar(self, bar, ctx):
        self._state["calls"] = self._state.get("calls", 0) + 1
        return self.strategy.on_bar(bar, ctx)


def new_state(cfg, history_limit=6):
    return custom_kernel.initial_state(
        cfg, IDENTITY, user_state={"calls": 0}, history_limit=history_limit
    )


class CustomKernelTests(unittest.TestCase):
    def test_internal_config_is_a_window_shim_and_excludes_user_params(self):
        cfg = config(history_limit=1)
        self.assertEqual(cfg["strategy"], "sma_cross")
        self.assertEqual(cfg["params"], {
            "target_fraction": 0.5,
            "max_turnover_participation": 0.1,
            "fast": 1,
            "slow": 2,
        })
        self.assertNotIn("user_lookback", cfg["params"])
        with self.assertRaises(ValueError):
            custom_kernel.make_execution_config(
                {"symbol": "600000.SSE", "start": "2024-01-02", "end": "2024-01-02", "unsafe": True}
            )
        for invalid in (0, 242, True, 2.5):
            with self.subTest(history_limit=invalid), self.assertRaises(ValueError):
                custom_kernel.make_execution_config(
                    {"symbol": "600000.SSE", "start": "2024-01-02", "end": "2024-01-02"},
                    history_limit=invalid,
                )

    def test_exact_inner_parity_with_v1_sma_transition(self):
        cfg = config(history_limit=6)
        close_pattern = [
            10, 9, 8, 9, 10, 12, 11, 9, 8, 7, 9, 11,
            12, 10, 8, 6, 7, 9, 12, 13, 10, 8, 7, 11,
        ]
        source_bars = bars_for_days(
            ["2024-01-02", "2024-01-03"], close_pattern, per_day=12
        )
        custom = new_state(cfg, history_limit=6)
        adapter = BuiltinSmaAdapter(cfg, custom["user_state"])
        actual = custom_kernel.advance(cfg, custom, source_bars, adapter, final=True)
        legacy = replay_kernel.advance(
            cfg, replay_kernel.initial_state(cfg), source_bars, final=True
        )
        self.assertEqual(actual["inner_v1"], legacy)
        self.assertGreater(len(legacy["trades"]), 0)
        self.assertEqual(actual["mode"], "RESEARCH_ONLY")
        self.assertIs(actual["paper_admission"], False)

    def test_stateful_counter_resumes_across_chunks(self):
        cfg = config(history_limit=3)
        source_bars = bars_for_days(["2024-01-02"], per_day=6)
        first = new_state(cfg, history_limit=3)
        adapter1 = FakeAdapter(first["user_state"])
        paused = custom_kernel.advance(cfg, first, source_bars[:3], adapter1)
        self.assertEqual(paused["user_state"]["calls"], 3)
        self.assertFalse(paused["inner_v1"]["finalized"])

        resumed_adapter = FakeAdapter(paused["user_state"])
        resumed = custom_kernel.advance(
            cfg, paused, source_bars[3:], resumed_adapter, final=True
        )
        one_shot = custom_kernel.advance(
            cfg,
            first,
            source_bars,
            FakeAdapter(first["user_state"]),
            final=True,
        )
        self.assertEqual(resumed["user_state"]["calls"], 6)
        self.assertEqual(resumed["inner_v1"], one_shot["inner_v1"])
        self.assertEqual(resumed["user_state"], one_shot["user_state"])

    def test_outer_seal_supports_bounded_multi_day_legacy_bar_count(self):
        cfg = config(
            start="2024-01-02", end="2024-01-08", history_limit=60
        )
        days = [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-08",
        ]
        source_bars = bars_for_days(days, per_day=120)
        state = new_state(cfg, history_limit=60)
        adapter = FakeAdapter(state["user_state"])
        advanced = custom_kernel.advance(cfg, state, source_bars, adapter)
        self.assertEqual(advanced["inner_v1"]["rows_processed"], 600)
        self.assertEqual(advanced["user_state"]["calls"], 600)

    def test_t_plus_one_lots_scale_in_oversell_reject_fifo_and_next_day_sale(self):
        cfg = config(start="2024-01-02", end="2024-01-04", history_limit=4)
        source_bars = compact_bars(
            [
                ("2024-01-02", "09:31", 10),  # buy lot 1 signal
                ("2024-01-02", "09:32", 10),  # buy lot 1 fill
                ("2024-01-03", "09:31", 10),  # buy lot 2 signal
                ("2024-01-03", "09:32", 10),  # buy lot 2 fill; oversell attempt
                ("2024-01-03", "09:33", 10),  # sell old lot signal
                ("2024-01-03", "09:34", 10),  # sell old lot fill
                ("2024-01-03", "09:35", 10),  # today's new lot still locked
                ("2024-01-04", "09:31", 10),  # next-day sale allowed
                ("2024-01-04", "09:32", 10),  # next-day sale fill
            ]
        )
        plan = {
            0: [Order("600000.SSE", "buy", 100)],
            2: [Order("600000.SSE", "buy", 100)],
            3: [Order("600000.SSE", "sell", 200)],
            4: [Order("600000.SSE", "sell", 100)],
            6: [Order("600000.SSE", "sell", 100)],
            7: [Order("600000.SSE", "sell", 100)],
        }
        state = new_state(cfg, history_limit=4)
        adapter = FakeAdapter(
            state["user_state"], callback=lambda index, _bar, _ctx: plan.get(index, [])
        )
        scaled_in = custom_kernel.advance(cfg, state, source_bars[:4], adapter)
        self.assertEqual(
            scaled_in["lots"],
            [
                {"date": "2024-01-02", "quantity": 100},
                {"date": "2024-01-03", "quantity": 100},
            ],
        )
        result = custom_kernel.result_from_state(cfg, scaled_in, {})
        self.assertEqual(result["positions"][0]["quantity"], 200)
        self.assertEqual(result["positions"][0]["available_to_sell"], 100)
        self.assertEqual(scaled_in["inner_v1"]["entry_date"], "2024-01-02")

        finished = custom_kernel.advance(
            cfg, scaled_in, source_bars[4:], adapter, final=True
        )
        self.assertEqual(finished["user_state"]["calls"], len(source_bars))
        orders = finished["inner_v1"]["orders"]
        self.assertEqual(orders[2]["status"], "rejected")
        self.assertEqual(orders[2]["reason"], "sell_exceeds_t_plus_one_available")
        self.assertEqual(orders[3]["status"], "filled")
        self.assertEqual(orders[4]["status"], "rejected")
        self.assertEqual(orders[4]["reason"], "sell_exceeds_t_plus_one_available")
        self.assertEqual(orders[5]["status"], "filled")
        self.assertEqual(finished["inner_v1"]["quantity"], 0)
        self.assertEqual(finished["lots"], [])
        self.assertTrue(all(item["cash"] >= 0 for item in finished["inner_v1"]["equity"]))

    def test_every_partial_fill_bar_calls_callback_and_core_charges_each_fill_once(self):
        cfg = config(
            start="2024-01-02",
            end="2024-01-02",
            history_limit=2,
            initial_cash=10_000.0,
            params={"target_fraction": 0.5, "max_turnover_participation": 0.1},
        )
        source_bars = compact_bars(
            [("2024-01-02", f"09:{minute:02d}", 10) for minute in range(31, 36)],
            turnover=12_000.0,
        )
        state = new_state(cfg, history_limit=2)

        def place_once(index, _bar, _ctx):
            if index in {0, 1, 2}:
                return [Order("600000.SSE", "buy", 300 if index == 0 else 100)]
            return []

        adapter = FakeAdapter(state["user_state"], callback=place_once)
        result = custom_kernel.advance(cfg, state, source_bars, adapter, final=True)
        inner = result["inner_v1"]
        self.assertEqual(result["user_state"]["calls"], 5)
        self.assertEqual([trade["quantity"] for trade in inner["trades"]], [100, 100, 100])
        self.assertEqual(inner["orders"][0]["status"], "filled")
        self.assertEqual(
            [order["reason"] for order in inner["orders"][1:]],
            ["pending_order_exists", "pending_order_exists"],
        )
        self.assertAlmostEqual(inner["fee_total"], 15.0)
        self.assertAlmostEqual(sum(t["commission"] + t["stamp_duty"] for t in inner["trades"]), 15.0)
        self.assertTrue(all(row["cash"] >= 0 for row in inner["equity"]))

    def test_invalid_orders_fail_atomically_without_fabricated_rejection_fields(self):
        cfg = config(start="2024-01-02", end="2024-01-02", history_limit=3)
        source_bar = compact_bars([("2024-01-02", "09:31", 10)])[0]
        cases = [
            (Order("600000.SSE", "short", 100), "unsupported_side"),
            (Order("600000.SSE", "buy", 100, order_type="limit", limit_price=10), "unsupported_order_type"),
            (Order("600000.SSE", "buy", 150), "invalid_order_quantity"),
            (Order("000001.SZSE", "buy", 100), "unsupported_symbol"),
        ]
        for request, reason in cases:
            with self.subTest(reason=reason):
                state = new_state(cfg, history_limit=3)
                adapter = FakeAdapter(
                    state["user_state"], callback=lambda _i, _b, _c, order=request: [order]
                )
                original = deepcopy(state)
                with self.assertRaisesRegex(ValueError, '^invalid_custom_order_intent:' + reason + '$'):
                    custom_kernel.advance(cfg, state, [source_bar], adapter)
                self.assertEqual(state, original)
                self.assertEqual(state["inner_v1"]["orders"], [])
                self.assertEqual(state["inner_v1"]["trades"], [])

    def test_outer_identity_config_mode_and_input_bars_are_validated_atomically(self):
        cfg = config(start="2024-01-02", end="2024-01-02", history_limit=3)
        state = new_state(cfg, history_limit=3)
        source_bar = compact_bars([("2024-01-02", "09:31", 10)])[0]

        identity_tamper = deepcopy(state)
        identity_tamper["identity"]["source_sha256"] = "f" * 64
        custom_kernel._seal_outer(identity_tamper)
        with self.assertRaisesRegex(ValueError, "identity hash"):
            custom_kernel.advance(
                cfg, identity_tamper, [], FakeAdapter(identity_tamper["user_state"])
            )

        mode_tamper = deepcopy(state)
        mode_tamper["mode"] = "LIVE"
        custom_kernel._seal_outer(mode_tamper)
        with self.assertRaisesRegex(ValueError, "RESEARCH_ONLY"):
            custom_kernel.advance(cfg, mode_tamper, [], FakeAdapter(state["user_state"]))

        changed_config = config(
            start="2024-01-02", end="2024-01-02", history_limit=3, initial_cash=99_999
        )
        adapter = FakeAdapter(state["user_state"])
        original = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "does not match"):
            custom_kernel.advance(changed_config, state, [], adapter)
        self.assertEqual(state, original)
        self.assertEqual(adapter.state, original["user_state"])

        future = compact_bars([("2024-01-03", "09:31", 10)])[0]
        adapter = FakeAdapter(state["user_state"])
        with self.assertRaisesRegex(ValueError, "超出 config 请求区间"):
            custom_kernel.advance(cfg, state, [future], adapter)
        self.assertEqual(state, original)
        self.assertEqual(adapter.state, original["user_state"])

        reversed_pair = compact_bars(
            [("2024-01-02", "09:32", 10), ("2024-01-02", "09:31", 10)]
        )
        with self.assertRaisesRegex(ValueError, "strictly increase"):
            custom_kernel.advance(cfg, state, reversed_pair, FakeAdapter(state["user_state"]))
        self.assertEqual(state, original)

    def test_callback_exception_does_not_mutate_caller_state(self):
        cfg = config(start="2024-01-02", end="2024-01-02", history_limit=3)
        state = new_state(cfg, history_limit=3)
        original = deepcopy(state)
        source_bar = compact_bars([("2024-01-02", "09:31", 10)])[0]

        class BrokenAdapter(FakeAdapter):
            def on_bar(self, bar, ctx):
                self._state["calls"] += 1
                raise RuntimeError("synthetic callback failure")

        adapter = BrokenAdapter(state["user_state"])
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            custom_kernel.advance(cfg, state, [source_bar], adapter)
        self.assertEqual(state, original)
        self.assertEqual(state["inner_v1"]["rows_processed"], 0)

    def test_cancelled_pause_retains_pending_and_resume_finalizes(self):
        cfg = config(start="2024-01-02", end="2024-01-02", history_limit=3)
        source_bars = compact_bars(
            [("2024-01-02", "09:31", 10), ("2024-01-02", "09:32", 10)]
        )
        state = new_state(cfg, history_limit=3)
        adapter = FakeAdapter(
            state["user_state"],
            callback=lambda index, _bar, _ctx: [Order("600000.SSE", "buy", 100)] if index == 0 else [],
        )
        pending = custom_kernel.advance(cfg, state, source_bars[:1], adapter)
        self.assertIsNotNone(pending["inner_v1"]["pending_order_id"])
        paused = custom_kernel.advance(
            cfg,
            pending,
            source_bars[1:],
            FakeAdapter(pending["user_state"]),
            final=True,
            cancelled=lambda: True,
        )
        self.assertTrue(paused["inner_v1"]["cancelled"])
        self.assertFalse(paused["inner_v1"]["finalized"])
        self.assertEqual(
            paused["inner_v1"]["orders"][0]["reason"],
            "run_cancelled_before_remaining_quantity_filled",
        )

        resumed = custom_kernel.advance(
            cfg,
            paused,
            source_bars[1:],
            FakeAdapter(paused["user_state"]),
            final=True,
            cancelled=lambda: False,
        )
        self.assertTrue(resumed["inner_v1"]["finalized"])
        self.assertFalse(resumed["inner_v1"]["cancelled"])
        self.assertEqual(resumed["inner_v1"]["orders"][0]["status"], "filled")


if __name__ == "__main__":
    unittest.main()

