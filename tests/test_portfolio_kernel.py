from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import unittest

import pandas as pd

from research_center import portfolio_kernel as kernel


SYMBOLS = ["000001.SZSE", "000002.SZSE"]


def config(**overrides):
    value = {
        "symbols": list(SYMBOLS),
        "start": "2024-01-02",
        "end": "2024-01-05",
        "interval": "1m",
        "strategy": "buy_hold",
        "initial_cash": 10_000.0,
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "slippage_bps": 0.0,
        "params": {"target_fraction": 1.0, "max_turnover_participation": 0.1, "fast": 1, "slow": 2},
    }
    value.update(overrides)
    return value


def frame(source_time, closes=(10.0, 10.0), *, turnovers=None, flat=False):
    source = pd.Timestamp(source_time)
    turnovers = turnovers or (1_000_000.0, 1_000_000.0)
    rows = {}
    for index, symbol in enumerate(SYMBOLS):
        close = float(closes[index])
        rows[symbol] = {
            "symbol": symbol,
            "datetime": (source - pd.Timedelta(minutes=1)).isoformat(),
            "bar_start_datetime": (source - pd.Timedelta(minutes=1)).isoformat(),
            "source_datetime": source.isoformat(),
            "open": close,
            "high": close if flat else close + 0.2,
            "low": close if flat else close - 0.2,
            "close": close,
            "volume": 1_000_000.0,
            "turnover": float(turnovers[index]),
        }
    return rows


class PortfolioConfigTest(unittest.TestCase):
    def test_equal_weight_default_and_normalized_config_round_trip(self):
        raw = config(params={"max_turnover_participation": 0.1, "fast": 1, "slow": 2})
        normalized = kernel.normalize_portfolio(raw)
        self.assertEqual(normalized["allocation_weights"], {SYMBOLS[0]: 0.25, SYMBOLS[1]: 0.25})
        self.assertEqual([item["symbol"] for item in normalized["symbol_configs"]], SYMBOLS)
        self.assertEqual(kernel.normalize_portfolio(json.loads(json.dumps(normalized))), normalized)

    def test_explicit_weights_must_fit_shared_budget_and_strategy_is_allowlisted(self):
        bad_weights = config(allocation_weights={SYMBOLS[0]: 0.6, SYMBOLS[1]: 0.5})
        with self.assertRaises(ValueError):
            kernel.normalize_portfolio(bad_weights)
        with self.assertRaisesRegex(ValueError, "buy_hold 或 sma_cross"):
            kernel.normalize_portfolio(config(strategy="unknown"))


class PortfolioReplayTest(unittest.TestCase):
    def test_same_minute_signals_share_cash_snapshot_and_compete_in_symbol_order(self):
        cfg = config(
            params={"target_fraction": 1.0, "max_turnover_participation": 0.1, "fast": 1, "slow": 2},
            allocation_weights={SYMBOLS[0]: 0.6, SYMBOLS[1]: 0.4},
        )
        frames = [frame("2024-01-02 09:31"), frame("2024-01-02 09:32"), frame("2024-01-02 09:33")]
        result_state = kernel.advance(cfg, kernel.initial_state(cfg), frames, final=True)
        orders = result_state["orders"]
        self.assertEqual([(item["symbol"], item["requested_quantity"]) for item in orders],
                         [(SYMBOLS[0], 600), (SYMBOLS[1], 400)])
        # The first symbol consumes available cash first. The second is reduced
        # by whole lots, and shared cash never crosses below zero.
        trades = result_state["trades"]
        self.assertEqual([(item["symbol"], item["quantity"]) for item in trades],
                         [(SYMBOLS[0], 600), (SYMBOLS[1], 300)])
        self.assertGreaterEqual(result_state["cash"], 0)
        result = kernel.result_from_state(cfg, result_state)
        self.assertFalse(result["metrics"]["negative_cash"])
        self.assertEqual(result["mode"], "RESEARCH_ONLY")
        self.assertFalse(result["paper_admission"])

    def test_costs_and_turnover_capacity_create_partial_fills(self):
        cfg = config(
            initial_cash=20_000.0,
            commission_rate=0.001,
            min_commission=5.0,
            params={"target_fraction": 1.0, "max_turnover_participation": 0.01, "fast": 1, "slow": 2},
        )
        limited = frame("2024-01-02 09:32", turnovers=(102_000.0, 102_000.0))
        state = kernel.advance(cfg, kernel.initial_state(cfg), [frame("2024-01-02 09:31"), limited], final=True)
        self.assertEqual([trade["quantity"] for trade in state["trades"]], [100, 100])
        self.assertEqual([trade["commission"] for trade in state["trades"]], [5.0, 5.0])
        self.assertEqual([order["status"] for order in state["orders"]], ["partial_filled", "partial_filled"])
        self.assertAlmostEqual(state["cash"], 17_950.0)
        self.assertEqual(state["fee_total"], 10.0)

    def test_old_lot_remains_sellable_when_same_day_lot_is_locked(self):
        mixed = {
            "quantity": 400,
            "average_cost": 10.0,
            "lots": [
                {"quantity": 300, "entry_date": "2024-01-02", "unit_cost": 10.0},
                {"quantity": 100, "entry_date": "2024-01-03", "unit_cost": 10.0},
            ],
        }
        view = kernel._position_view(mixed, date(2024, 1, 3))
        self.assertEqual(view["quantity"], 400)
        self.assertEqual(view["available_to_sell"], 300)
        basis = kernel._sell_lots(mixed, 300, date(2024, 1, 3))
        self.assertEqual(basis, 3_000.0)
        self.assertEqual(mixed["quantity"], 100)
        self.assertEqual(mixed["lots"], [
            {"quantity": 100, "entry_date": "2024-01-03", "unit_cost": 10.0}
        ])

    def test_cross_lunch_gap_is_accepted_but_pending_order_is_rejected(self):
        cfg = config()
        state = kernel.advance(
            cfg,
            kernel.initial_state(cfg),
            [frame("2024-01-02 11:30"), frame("2024-01-02 13:01")],
            final=True,
        )
        self.assertGreaterEqual(state["rows_processed"], 2)
        self.assertEqual(state["orders"][0]["status"], "rejected")
        self.assertEqual(state["orders"][0]["reason"], "next_source_minute_not_contiguous")

    def test_missing_symbol_frame_fails_without_mutating_input_state(self):
        cfg = config()
        state = kernel.advance(cfg, kernel.initial_state(cfg), [frame("2024-01-02 09:31")])
        original = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "全部配置 symbols"):
            kernel.advance(cfg, state, [{SYMBOLS[0]: frame("2024-01-02 09:32")[SYMBOLS[0]]}])
        self.assertEqual(state, original)

    def test_state_tampering_fails_before_copy_on_write(self):
        cfg = config()
        state = kernel.advance(cfg, kernel.initial_state(cfg), [frame("2024-01-02 09:31")])
        original = deepcopy(state)
        tampered = deepcopy(state)
        tampered["cash"] += 1.0
        with self.assertRaisesRegex(ValueError, "state_sha256"):
            kernel.advance(cfg, tampered, [frame("2024-01-02 09:32")])
        self.assertEqual(state, original)

    def test_resealed_cash_and_fee_tampering_fail_ledger_reconciliation(self):
        cfg = config()
        state = kernel.advance(
            cfg,
            kernel.initial_state(cfg),
            [frame("2024-01-02 09:31"), frame("2024-01-02 09:32")],
            final=True,
        )
        tampered_cash = deepcopy(state)
        tampered_cash["cash"] += 1.0
        kernel._seal(tampered_cash)
        with self.assertRaisesRegex(ValueError, "cash 与初始资金"):
            kernel.advance(cfg, tampered_cash, [])

        tampered_fees = deepcopy(state)
        tampered_fees["fee_total"] += 1.0
        kernel._seal(tampered_fees)
        with self.assertRaisesRegex(ValueError, "fee_total 与成交手续费"):
            kernel.advance(cfg, tampered_fees, [])

    def test_segmented_json_restore_matches_single_pass(self):
        cfg = config()
        frames = [
            frame("2024-01-02 09:31"),
            frame("2024-01-02 09:32"),
            frame("2024-01-02 09:33"),
            frame("2024-01-02 09:34"),
        ]
        full = kernel.advance(cfg, kernel.initial_state(cfg), frames, final=True)
        partial = kernel.advance(cfg, kernel.initial_state(cfg), frames[:2])
        restored = json.loads(json.dumps(partial, allow_nan=False))
        resumed = kernel.advance(cfg, restored, frames[2:], final=True)
        self.assertEqual(resumed, full)
        self.assertEqual(kernel.result_from_state(cfg, resumed), kernel.result_from_state(cfg, full))


if __name__ == "__main__":
    unittest.main()
