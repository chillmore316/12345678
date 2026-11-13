from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest import mock

import pandas as pd

from research_center import engine, replay_kernel


def config(**overrides):
    value = {
        "symbol": "000001.SZSE",
        "start": "2024-01-04",
        "end": "2024-01-05",
        "interval": "1m",
        "strategy": "buy_hold",
        "initial_cash": 100_000,
        "commission_rate": 0,
        "min_commission": 0,
        "slippage_bps": 0,
        "params": {"target_fraction": 0.5, "max_turnover_participation": 0.01},
    }
    value.update(overrides)
    return value


def synthetic_bars(source_times, closes, *, flat_indices=(), zero_volume_indices=()):
    rows = []
    for index, (source_time, close) in enumerate(zip(source_times, closes)):
        source_time = pd.Timestamp(source_time)
        close = float(close)
        flat = index in flat_indices
        rows.append(
            {
                "datetime": source_time - pd.Timedelta(minutes=1),
                "source_datetime": source_time,
                "symbol": "000001.SZSE",
                "exchange": "SZSE",
                "vt_symbol": "000001.SZSE",
                "open": close,
                "high": close if flat else close + 0.2,
                "low": close if flat else close - 0.2,
                "close": close,
                "volume": 0.0 if index in zero_volume_indices else 1_000_000.0,
                "turnover": 1_000_000_000.0,
                "source": "synthetic_test",
            }
        )
    return pd.DataFrame(rows)


def source_manifest():
    return {"read_only": True, "research_only": True, "used_source_files": []}


class ResearchReplayKernelTest(unittest.TestCase):
    def _batch(self, cfg, frame):
        with mock.patch.object(
            engine, "_load_training_bars", return_value=(frame, source_manifest())
        ):
            return engine.run_backtest(cfg)

    def test_incremental_json_restart_matches_batch_and_preserves_legacy_metrics(self):
        cfg = config()
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
            [10, 10.1, 10.2],
        )
        batch = self._batch(cfg, frame)
        payloads = batch["bars"]
        initial = replay_kernel.initial_state(cfg)
        initial_before = deepcopy(initial)

        state = replay_kernel.advance(cfg, initial, payloads[:1])
        self.assertEqual(initial, initial_before)
        self.assertEqual(state["pending_order_id"], "R000001")
        self.assertEqual(state["orders"][0]["status"], "pending")

        restarted = json.loads(json.dumps(state, allow_nan=False))
        state = replay_kernel.advance(cfg, restarted, payloads[1:], final=True)
        self.assertTrue(state["finalized"])
        result = replay_kernel.result_from_state(cfg, state, source_manifest())
        self.assertEqual(result, batch)
        self.assertEqual(result["trades"][0]["fill_datetime"], "2024-01-04T09:32:00")
        self.assertEqual(result["trades"][0]["price"], 10.3)
        self.assertEqual(result["metrics"]["ending_equity"], 99_500)
        self.assertFalse(result["metrics"]["negative_cash"])
        with self.assertRaisesRegex(ValueError, "finalized"):
            replay_kernel.advance(cfg, state, [])

    def test_partial_fill_survives_pause_and_json_restart_then_completes(self):
        cfg = config()
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
            [10, 10.1, 10.2],
        )
        frame.loc[1, "turnover"] = 500_000.0
        bars = self._batch(cfg, frame)["bars"]

        state = replay_kernel.advance(cfg, replay_kernel.initial_state(cfg), bars[:2])
        self.assertEqual(state["orders"][0]["status"], "partial_filled")
        self.assertEqual(state["orders"][0]["filled_quantity"], 400)
        before_pause = deepcopy(state)
        state = replay_kernel.advance(cfg, state, [], final=False)
        self.assertEqual(state, before_pause)
        self.assertEqual(state["pending_order_id"], "R000001")

        restarted = json.loads(json.dumps(state, allow_nan=False))
        state = replay_kernel.advance(cfg, restarted, bars[2:], final=True)
        result = replay_kernel.result_from_state(cfg, state, source_manifest())
        self.assertEqual(result["orders"][0]["status"], "filled")
        self.assertEqual([trade["quantity"] for trade in result["trades"]], [400, 4_600])
        self.assertEqual(sum(trade["quantity"] for trade in result["trades"]), 5_000)
        self.assertGreaterEqual(result["metrics"]["minimum_cash"], 0)

    def test_gap_zero_volume_and_one_price_bar_keep_reject_rules(self):
        cases = (
            (
                ["2024-01-04 09:31", "2024-01-04 09:33"],
                [10, 10.1],
                (),
                (),
                "next_source_minute_not_contiguous",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32"],
                [10, 10.1],
                (),
                (1,),
                "zero_volume_bar",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32"],
                [10, 10.1],
                (1,),
                (),
                "one_price_bar",
            ),
        )
        for times, closes, flat, zero, reason in cases:
            with self.subTest(reason=reason):
                cfg = config()
                frame = synthetic_bars(
                    times,
                    closes,
                    flat_indices=flat,
                    zero_volume_indices=zero,
                )
                result = self._batch(cfg, frame)
                rejected = [order for order in result["orders"] if order["status"] == "rejected"]
                self.assertTrue(rejected)
                self.assertEqual(rejected[0]["reason"], reason)

    def test_sma_sell_obeys_t1_and_does_not_buy_on_sale_fill_bar(self):
        cfg = config(
            strategy="sma_cross",
            params={
                "fast": 1,
                "slow": 2,
                "target_fraction": 0.5,
                "max_turnover_participation": 0.01,
            },
        )
        frame = synthetic_bars(
            [
                "2024-01-04 09:31",
                "2024-01-04 09:32",
                "2024-01-04 09:33",
                "2024-01-04 09:34",
                "2024-01-04 09:35",
                "2024-01-04 09:36",
                "2024-01-05 09:31",
                "2024-01-05 09:32",
                "2024-01-05 09:33",
            ],
            [10, 8, 9, 11, 7, 6, 5, 20, 20],
        )
        batch = self._batch(cfg, frame)
        state = replay_kernel.initial_state(cfg)
        for offset in range(0, len(batch["bars"]), 2):
            state = replay_kernel.advance(
                cfg,
                json.loads(json.dumps(state, allow_nan=False)),
                batch["bars"][offset : offset + 2],
                final=offset + 2 >= len(batch["bars"]),
            )
        result = replay_kernel.result_from_state(cfg, state, source_manifest())
        self.assertEqual(result, batch)
        buys = [trade for trade in result["trades"] if trade["side"] == "buy"]
        sells = [trade for trade in result["trades"] if trade["side"] == "sell"]
        self.assertTrue(buys)
        self.assertTrue(sells)
        self.assertEqual(buys[0]["fill_datetime"][:10], "2024-01-04")
        self.assertEqual(sells[0]["fill_datetime"][:10], "2024-01-05")
        self.assertEqual(len(result["orders"]), 2)

    def test_cash_stays_nonnegative_when_fill_capacity_exceeds_affordability(self):
        cfg = config(
            initial_cash=10_000,
            params={"target_fraction": 1, "max_turnover_participation": 0.01},
        )
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"], [10, 10]
        )
        result = self._batch(cfg, frame)
        self.assertEqual(result["trades"][0]["quantity"], 900)
        self.assertAlmostEqual(result["metrics"]["minimum_cash"], 820)
        self.assertFalse(result["metrics"]["negative_cash"])

    def test_each_buy_fill_pays_minimum_commission(self):
        cfg = config(
            commission_rate=0,
            min_commission=5,
            params={"target_fraction": 0.1, "max_turnover_participation": 0.01},
        )
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"], [10, 10]
        )
        result = self._batch(cfg, frame)
        self.assertEqual(result["trades"][0]["commission"], 5)
        self.assertEqual(result["metrics"]["fees_paid"], 5)

    def test_run_backtest_cancellation_pauses_without_finalizing_pending_order(self):
        cfg = config()
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
            [10, 10.1, 10.2],
        )
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 3

        with mock.patch.object(
            engine, "_load_training_bars", return_value=(frame, source_manifest())
        ):
            result = engine.run_backtest(cfg, cancelled=cancelled)
        self.assertTrue(result["metrics"]["cancelled"])
        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(result["orders"][0]["status"], "pending")
        self.assertEqual(
            result["orders"][0]["reason"],
            "run_cancelled_before_remaining_quantity_filled",
        )

    def test_repeated_or_out_of_order_bars_and_tampered_inputs_are_rejected(self):
        cfg = config()
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"], [10, 10.1]
        )
        bars = self._batch(cfg, frame)["bars"]
        initial = replay_kernel.initial_state(cfg)
        with self.assertRaisesRegex(ValueError, "递增"):
            replay_kernel.advance(cfg, initial, [bars[1], bars[0]])

        state = replay_kernel.advance(cfg, initial, bars[:1])
        original = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "递增"):
            replay_kernel.advance(cfg, state, [bars[0]])
        self.assertEqual(state, original)

        altered_config = config(initial_cash=200_000)
        with self.assertRaisesRegex(ValueError, "config"):
            replay_kernel.advance(altered_config, initial, [])

        tampered_state = deepcopy(state)
        tampered_state["cash"] = 1.0
        with self.assertRaisesRegex(ValueError, "state_sha256"):
            replay_kernel.advance(cfg, tampered_state, [])

    def test_bar_start_contract_and_terminal_pending_status_are_preserved(self):
        cfg = config()
        frame = synthetic_bars(["2024-01-04 09:31"], [10])
        bars = self._batch(cfg, frame)["bars"]
        malformed = deepcopy(bars[0])
        malformed["bar_start_datetime"] = "2024-01-04T09:31:00"
        with self.assertRaisesRegex(ValueError, "source_datetime - 1"):
            replay_kernel.advance(cfg, replay_kernel.initial_state(cfg), [malformed])

        state = replay_kernel.advance(
            cfg, replay_kernel.initial_state(cfg), bars, final=True
        )
        self.assertTrue(state["finalized"])
        self.assertEqual(state["orders"][0]["status"], "pending")
        self.assertEqual(
            state["orders"][0]["reason"], "awaiting_next_contiguous_minute"
        )

    def test_http_thread_history_avoids_implicit_arrow_string_allocations(self):
        from concurrent.futures import ThreadPoolExecutor
        from pandas.core.arrays.string_arrow import ArrowStringArray
        cfg=config(strategy='sma_cross',params={'fast':1,'slow':2,'target_fraction':.5})
        frame=synthetic_bars(['2024-01-04 09:31','2024-01-04 09:32','2024-01-04 09:33'],[10,9,11])
        bars=self._batch(cfg,frame)['bars']
        state=replay_kernel.initial_state(cfg)
        with mock.patch.object(ArrowStringArray,'_from_sequence',side_effect=AssertionError('implicit Arrow string allocation')):
            for bar in bars:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    state=pool.submit(replay_kernel.advance,cfg,state,[bar]).result()
        self.assertEqual(state['rows_processed'],3)


if __name__ == "__main__":
    unittest.main()
