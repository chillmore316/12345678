from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from research_center import engine


def synthetic_bars(source_times, closes, *, flat_indices=(), zero_volume_indices=()):
    rows = []
    for index, (source_time, close) in enumerate(zip(source_times, closes)):
        source_time = pd.Timestamp(source_time)
        close = float(close)
        flat = index in flat_indices
        high = close if flat else close + 0.2
        low = close if flat else close - 0.2
        rows.append(
            {
                "datetime": source_time - pd.Timedelta(minutes=1),
                "source_datetime": source_time,
                "symbol": "000001.SZSE",
                "exchange": "SZSE",
                "vt_symbol": "000001.SZSE",
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": 0.0 if index in zero_volume_indices else 1_000_000.0,
                "turnover": 1_000_000_000.0,
                "source": "synthetic_test",
            }
        )
    return pd.DataFrame(rows)


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


def manifest():
    return {"read_only": True, "research_only": True, "used_source_files": []}


class ResearchEngineConfigTest(unittest.TestCase):
    def test_normalizes_training_config_and_default_target_fraction(self):
        normalized = engine.validate_config(
            {
                "symbol": "000001.XSHE",
                "start": "2024-01-04",
                "end": "2024-01-04",
            }
        )
        self.assertEqual(normalized["symbol"], "000001.SZSE")
        self.assertEqual(normalized["params"]["target_fraction"], 0.5)
        aliases = engine.validate_config(
            {
                "symbol": "000001.SZSE",
                "start": "2024-01-04",
                "end": "2024-01-04",
                "strategy": "sma_cross",
                "params": {"fast_window": 2, "slow_window": 8},
            }
        )
        self.assertEqual((aliases["params"]["fast"], aliases["params"]["slow"]), (2, 8))
        self.assertNotIn("fast_window", aliases["params"])

    def test_rejects_out_of_period_long_range_etf_and_star_codes(self):
        for invalid in (
            config(start="2026-01-01", end="2026-01-01"),
            config(start="2024-01-01", end="2024-02-01"),
            config(symbol="510300.SSE"),
            config(symbol="688981.SSE"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    engine.validate_config(invalid)


class ResearchEngineReplayTest(unittest.TestCase):
    def _run(self, cfg, frame):
        with mock.patch.object(engine, "_load_training_bars", return_value=(frame, manifest())):
            return engine.run_backtest(cfg)

    def test_buy_hold_fills_next_contiguous_bar_and_starts_equity_at_cash(self):
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
            [10, 10.1, 10.2],
        )
        result = self._run(config(), frame)
        self.assertEqual(result["equity"][0]["equity"], 100_000)
        self.assertEqual(result["orders"][0]["status"], "filled")
        self.assertEqual(result["trades"][0]["fill_datetime"], "2024-01-04T09:32:00")
        self.assertEqual(result["trades"][0]["datetime"], result["trades"][0]["fill_datetime"])
        self.assertEqual(result["trades"][0]["price"], 10.3)
        self.assertEqual(result["equity"][2]["datetime"], "2024-01-04T09:32:00")
        self.assertEqual(result["equity"][2]["bar_start_datetime"], "2024-01-04T09:31:00")
        self.assertIsNone(result["metrics"]["annual_return"])
        self.assertIsNone(result["metrics"]["sharpe"])
        self.assertEqual(result["metrics"]["total_trades"], 1)
        self.assertTrue(result["positions"])
        self.assertFalse(result["metrics"]["negative_cash"])

    def test_gap_zero_volume_and_one_price_bars_reject_pending_orders(self):
        cases = (
            (
                ["2024-01-04 09:31", "2024-01-04 09:33", "2024-01-04 09:34"],
                [10, 10.1, 10.2],
                (),
                (),
                "next_source_minute_not_contiguous",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
                [10, 10.1, 10.2],
                (),
                (1,),
                "zero_volume_bar",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32", "2024-01-04 09:33"],
                [10, 10.1, 10.2],
                (1,),
                (),
                "one_price_bar",
            ),
        )
        for times, closes, flat, zero, reason in cases:
            with self.subTest(reason=reason):
                result = self._run(
                    config(),
                    synthetic_bars(
                        times,
                        closes,
                        flat_indices=flat,
                        zero_volume_indices=zero,
                    ),
                )
                rejected = [order for order in result["orders"] if order["status"] == "rejected"]
                self.assertTrue(rejected)
                self.assertEqual(rejected[0]["reason"], reason)

    def test_sma_sell_obeys_t_plus_one_and_sharpe_uses_daily_returns(self):
        times = [
            "2024-01-04 09:31",
            "2024-01-04 09:32",
            "2024-01-04 09:33",
            "2024-01-04 09:34",
            "2024-01-04 09:35",
            "2024-01-04 09:36",
            "2024-01-05 09:31",
            "2024-01-05 09:32",
            "2024-01-05 09:33",
        ]
        frame = synthetic_bars(times, [10, 8, 9, 11, 7, 6, 5, 4, 4.2])
        result = self._run(
            config(
                strategy="sma_cross",
                params={
                    "fast": 1,
                    "slow": 2,
                    "target_fraction": 0.5,
                    "max_turnover_participation": 0.01,
                },
            ),
            frame,
        )
        buys = [trade for trade in result["trades"] if trade["side"] == "buy"]
        sells = [trade for trade in result["trades"] if trade["side"] == "sell"]
        self.assertTrue(buys)
        self.assertTrue(sells)
        self.assertEqual(buys[0]["fill_datetime"][:10], "2024-01-04")
        self.assertEqual(sells[0]["fill_datetime"][:10], "2024-01-05")
        self.assertAlmostEqual(sells[0]["stamp_duty"], sells[0]["notional"] * 0.0005)
        self.assertEqual(result["metrics"]["daily_return_observations"], 2)
        self.assertIn("sharpe", result["metrics"])

    def test_partial_fill_advances_attempt_time_and_cancels_remainder_on_gap(self):
        frame = synthetic_bars(
            [
                "2024-01-04 09:31",
                "2024-01-04 09:32",
                "2024-01-04 09:33",
                "2024-01-04 09:35",
            ],
            [10, 10.1, 10.2, 10.3],
        )
        frame["turnover"] = 1_000_000.0
        result = self._run(config(), frame)
        order = result["orders"][0]
        self.assertEqual(order["status"], "partially_cancelled")
        self.assertGreater(order["filled_quantity"], 0)
        self.assertEqual(
            order["remaining_quantity"],
            order["requested_quantity"] - order["filled_quantity"],
        )
        self.assertEqual(order["reason"], "next_source_minute_not_contiguous")

    def test_cancelled_before_read_returns_initial_capital(self):
        with mock.patch.object(engine, "_load_training_bars") as load:
            result = engine.run_backtest(config(), cancelled=lambda: True)
        load.assert_not_called()
        self.assertTrue(result["metrics"]["cancelled"])
        self.assertEqual(result["metrics"]["ending_equity"], 100_000)


if __name__ == "__main__":
    unittest.main()

