from __future__ import annotations

import copy
import math
import unittest

from research_center.analytics import build_analytics


def bar(stamp: str, close: float) -> dict:
    return {
        "source_datetime": stamp,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
    }


def equity(stamp: str | None, cash: float, quantity: float, value: float) -> dict:
    return {
        "source_datetime": stamp,
        "cash": cash,
        "quantity": quantity,
        "equity": value,
    }


class ResearchAnalyticsTest(unittest.TestCase):
    def test_isolated_python_requires_shared_execution_contract(self):
        result = {'equity': [equity(None, 5000, 0, 5000)], 'trades': [], 'bars': [],
                  'backend': 'isolated_python', 'data_manifest': {}}
        config = {'initial_cash': 5000, 'backend': 'isolated_python'}
        with self.assertRaisesRegex(ValueError, '共享撮合契约'):
            build_analytics(config, result)
        result['custom_execution'] = {'engine': 'research_center.custom_execution.v2',
                                      'mode': 'RESEARCH_ONLY', 'paper_admission': False}
        actual = build_analytics(config, result)
        reference = build_analytics({'initial_cash': 5000}, {**result, 'backend': 'reference'})
        self.assertEqual(actual['metrics'], reference['metrics'])
        result['data_manifest']['slippage_cost_accounting'] = 'separate_cash_debit'
        with self.assertRaisesRegex(ValueError, '共享撮合契约'):
            build_analytics(config, result)

    def test_fifo_partial_fills_fees_calendar_periods_and_price_benchmark(self):
        config = {"initial_cash": 1_000.0, "backend": "reference"}
        trades = [
            {
                "side": "buy",
                "quantity": 4,
                "price": 10,
                "notional": 40,
                "commission": 2,
                "stamp_duty": 0,
                "slippage_cost": 0.8,
                "datetime": "2024-12-31T15:00:00",
            },
            {
                "side": "buy",
                "quantity": 6,
                "price": 12,
                "notional": 72,
                "commission": 3,
                "stamp_duty": 0,
                "slippage_cost": 1.2,
                "datetime": "2025-01-01T15:00:00",
            },
            {
                "side": "sell",
                "quantity": 5,
                "price": 15,
                "notional": 75,
                "commission": 2,
                "stamp_duty": 3,
                "slippage_cost": 1.5,
                "datetime": "2025-02-01T15:00:00",
            },
            {
                "side": "sell",
                "quantity": 5,
                "price": 11,
                "notional": 55,
                "commission": 2,
                "stamp_duty": 2.75,
                "slippage_cost": 1.1,
                "datetime": "2025-03-01T15:00:00",
            },
        ]
        result = {
            "equity": [
                equity(None, 1_000, 0, 1_000),
                equity("2024-12-31T15:00:00", 958, 4, 998),
                equity("2025-01-01T15:00:00", 883, 10, 1_003),
                equity("2025-02-01T15:00:00", 953, 5, 1_028),
                equity("2025-03-01T15:00:00", 1_003.25, 0, 1_003.25),
            ],
            "trades": trades,
            "bars": [
                bar("2024-12-31T15:00:00", 10),
                bar("2025-01-01T15:00:00", 11),
                bar("2025-02-01T15:00:00", 12),
                bar("2025-03-01T15:00:00", 13),
            ],
            "data_manifest": {"backend": "reference"},
        }

        analytics = build_analytics(config, result)
        metrics = analytics["metrics"]

        self.assertEqual(analytics["status"], "available")
        self.assertEqual(metrics["closed_pair_count"], 3)
        self.assertEqual(len(analytics["closed_trades"]), 3)
        self.assertAlmostEqual(analytics["closed_trades"][0]["quantity"], 4)
        self.assertAlmostEqual(analytics["closed_trades"][1]["quantity"], 1)
        self.assertAlmostEqual(metrics["net_realized_pnl"], 3.25)
        self.assertAlmostEqual(metrics["commission"], 9)
        self.assertAlmostEqual(metrics["stamp_duty"], 5.75)
        self.assertAlmostEqual(metrics["slippage_cost"], 4.6)
        self.assertEqual(metrics["slippage_cash_debit"], 0)
        self.assertEqual(metrics["slippage_accounting"], "reference_price_embedded")
        self.assertEqual(metrics["winning_pair_count"], 2)
        self.assertEqual(metrics["losing_pair_count"], 1)
        self.assertAlmostEqual(metrics["profit_factor"], 15.5 / 12.25)
        self.assertAlmostEqual(metrics["profit_loss_ratio"], 7.75 / 12.25)
        self.assertAlmostEqual(analytics["daily_returns"][0]["return"], -0.002)
        self.assertEqual(analytics["daily_returns"][0]["date"], "2024-12-31")
        self.assertEqual([row["period"] for row in analytics["monthly_returns"]], [
            "2024-12", "2025-01", "2025-02", "2025-03"
        ])
        self.assertTrue(all(row["partial"] for row in analytics["monthly_returns"]))
        self.assertEqual([row["period"] for row in analytics["yearly_returns"]], [
            "2024", "2025"
        ])
        self.assertTrue(all(row["partial"] for row in analytics["yearly_returns"]))
        self.assertEqual(analytics["benchmark"]["points"][0]["value"], 1_000)
        self.assertEqual(
            analytics["benchmark"]["points"][0]["datetime"],
            "2024-12-31T15:00:00",
        )
        self.assertAlmostEqual(analytics["benchmark"]["total_return"], 0.3)
        self.assertFalse(analytics["benchmark"]["executable"])
        self.assertIsNone(metrics["annual_return"])
        self.assertIsNone(metrics["calmar"])

    def test_native_and_reference_slippage_have_distinct_cash_contracts(self):
        initial_cash = 10_000.0
        native_result = {
            "equity": [
                equity(None, initial_cash, 0, initial_cash),
                equity("2024-06-03T09:32:00", 8_985, 100, 9_985),
                equity("2024-06-03T09:33:00", 10_068, 0, 10_068),
            ],
            "trades": [
                {
                    "side": "buy", "quantity": 100, "price": 10,
                    "notional": 1_000, "commission": 5, "stamp_duty": 0,
                    "slippage_cost": 10, "datetime": "2024-06-03T09:32:00",
                },
                {
                    "side": "sell", "quantity": 100, "price": 11,
                    "notional": 1_100, "commission": 5, "stamp_duty": 1,
                    "slippage_cost": 11, "datetime": "2024-06-03T09:33:00",
                },
            ],
            "bars": [
                bar("2024-06-03T09:32:00", 10),
                bar("2024-06-03T09:33:00", 11),
            ],
            "data_manifest": {
                "backend": "vnpy",
                "slippage_cost_accounting": "separate_cash_debit",
            },
        }
        native = build_analytics(
            {"initial_cash": initial_cash, "backend": "vnpy"}, native_result
        )
        self.assertAlmostEqual(native["metrics"]["slippage_cash_debit"], 21)
        self.assertAlmostEqual(native["closed_trades"][0]["buy_cost_including_allocated_fees"], 1_015)
        self.assertAlmostEqual(native["closed_trades"][0]["sell_proceeds_after_allocated_fees"], 1_083)
        self.assertAlmostEqual(native["closed_trades"][0]["net_realized_pnl"], 68)

        reference_result = copy.deepcopy(native_result)
        reference_result["data_manifest"] = {"backend": "reference"}
        reference_result["trades"][0].update(price=10.1, notional=1_010)
        reference_result["trades"][1].update(price=10.9, notional=1_090)
        reference_result["equity"][1].update(cash=8_985, equity=9_985)
        reference_result["equity"][2].update(cash=10_069, equity=10_069)
        reference = build_analytics(
            {"initial_cash": initial_cash, "backend": "reference"}, reference_result
        )
        self.assertAlmostEqual(reference["metrics"]["slippage_cost"], 21)
        self.assertEqual(reference["metrics"]["slippage_cash_debit"], 0)
        self.assertAlmostEqual(reference["closed_trades"][0]["net_realized_pnl"], 69)

    def test_empty_trading_state_uses_zero_counts_and_null_undefined_metrics(self):
        analytics = build_analytics(
            {"initial_cash": 5_000},
            {
                "equity": [equity(None, 5_000, 0, 5_000)],
                "trades": [],
                "bars": [],
                "data_manifest": {"backend": "reference"},
            },
        )
        metrics = analytics["metrics"]
        self.assertEqual(metrics["total_return"], 0)
        self.assertEqual(metrics["closed_pair_count"], 0)
        self.assertEqual(metrics["turnover"], 0)
        self.assertIsNone(metrics["win_rate"])
        self.assertIsNone(metrics["profit_factor"])
        self.assertIsNone(metrics["profit_loss_ratio"])
        self.assertIsNone(metrics["sortino"])
        self.assertIsNone(metrics["annual_return"])
        self.assertIn("sortino", metrics["metric_notes"])
        self.assertEqual(analytics["benchmark"]["status"], "unavailable")
        self.assertEqual(analytics["benchmark"]["points"], [])

    def test_rejects_oversell_and_nonfinite_inputs(self):
        base = {
            "equity": [
                equity(None, 1_000, 0, 1_000),
                equity("2025-01-01T15:00:00", 1_000, 0, 1_000),
            ],
            "trades": [
                {
                    "side": "sell", "quantity": 1, "price": 10,
                    "notional": 10, "commission": 0, "stamp_duty": 0,
                    "slippage_cost": 0, "datetime": "2025-01-01T15:00:00",
                }
            ],
            "bars": [bar("2025-01-01T15:00:00", 10)],
            "data_manifest": {"backend": "reference"},
        }
        with self.assertRaisesRegex(ValueError, "超卖"):
            build_analytics({"initial_cash": 1_000}, base)

        nonfinite = {
            "equity": [equity(None, 1_000, 0, 1_000)],
            "trades": [],
            "bars": [bar("2025-01-01T15:00:00", math.inf)],
            "data_manifest": {"backend": "reference"},
        }
        with self.assertRaisesRegex(ValueError, "有限数值"):
            build_analytics({"initial_cash": 1_000}, nonfinite)


if __name__ == "__main__":
    unittest.main()
