from __future__ import annotations

import stat
from pathlib import Path
import unittest
from unittest import mock

import pandas as pd

from research_center import engine
from research_center import vnpy_bridge
from data.joinquant_minute import canonicalize_frame


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_VNTRADER = PROJECT_ROOT / ".vntrader"


def synthetic_bars(
    source_times, closes, *, volumes=None, turnovers=None, flat_indices=()
):
    rows = []
    volumes = volumes or [1_000_000.0] * len(source_times)
    turnovers = turnovers or [None] * len(source_times)
    for index, (source_time, close, volume) in enumerate(
        zip(source_times, closes, volumes)
    ):
        source_time = pd.Timestamp(source_time)
        close = float(close)
        turnover = turnovers[index]
        if turnover is None:
            turnover = float(volume) * close
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
                "volume": float(volume),
                "turnover": float(turnover),
                "source": "synthetic_test",
            }
        )
    return pd.DataFrame(rows)


def config(**overrides):
    value = {
        "backend": "vnpy",
        "symbol": "000001.SZSE",
        "start": "2024-01-04",
        "end": "2024-01-04",
        "interval": "1m",
        "strategy": "buy_hold",
        "initial_cash": 100_000,
        "commission_rate": 0.0,
        "min_commission": 5.0,
        "slippage_bps": 0.0,
        "params": {
            "target_fraction": 0.5,
            "max_turnover_participation": 0.01,
        },
    }
    value.update(overrides)
    return value


def source_manifest():
    return {
        "read_only": True,
        "research_only": True,
        "used_source_files": [],
        "timestamp_contract": {
            "source_time": "bar-end label is availability time",
            "canonical_datetime": "bar-start",
        },
        "volume_semantics": "synthetic test input",
    }


def vntrader_snapshot(path: Path):
    """Capture names and metadata only; never read settings or database bytes."""
    if not path.exists():
        return None
    entries = [path, *sorted(path.rglob("*"))]
    result = []
    for entry in entries:
        info = entry.stat()
        result.append(
            (
                "." if entry == path else entry.relative_to(path).as_posix(),
                stat.S_IFMT(info.st_mode),
                info.st_size,
                info.st_ino,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    return tuple(result)


class ResearchVeighNaBridgeTest(unittest.TestCase):
    def _run(self, cfg, frame):
        with mock.patch.object(
            engine, "_load_training_bars", return_value=(frame, source_manifest())
        ):
            return vnpy_bridge.run_backtest(cfg)

    def test_real_canonicalizer_schema_with_bare_symbol_and_exchange(self):
        raw = pd.DataFrame({'time': pd.to_datetime(['2024-01-04 09:31', '2024-01-04 09:32']),
                            'code': ['000001.XSHE'] * 2, 'open': [10., 10.1],
                            'high': [10.2, 10.3], 'low': [9.8, 9.9], 'close': [10., 10.1],
                            'volume': [1_000_000.] * 2, 'money': [10_000_000.] * 2})
        frame = canonicalize_frame(raw)
        self.assertEqual(frame.iloc[0]['symbol'], '000001')
        self.assertEqual(frame.iloc[0]['vt_symbol'], '000001.SZSE')
        result = self._run(config(), frame)
        self.assertEqual(len(result['trades']), 1)
        invalid = frame.copy()
        invalid['exchange'] = 'SSE'
        with self.assertRaisesRegex(ValueError, 'exchange'):
            self._run(config(), invalid)

    def test_00_native_minute_order_and_trade_ids_are_preserved_and_fee_is_booked(self):
        before = vntrader_snapshot(USER_VNTRADER)
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"],
            [10.0, 10.1],
        )
        result = self._run(config(), frame)
        after = vntrader_snapshot(USER_VNTRADER)

        self.assertEqual(before, after)
        self.assertEqual(result["data_manifest"]["backend"], "vnpy")
        self.assertEqual(
            result["data_manifest"]["native_engine_versions"],
            {"vnpy": "4.4.0", "vnpy_portfoliostrategy": "1.3.0"},
        )
        self.assertEqual(
            result["data_manifest"]["native_engine"],
            "vnpy_portfoliostrategy.backtesting.BacktestingEngine",
        )
        self.assertFalse(result["data_manifest"]["database_instance_created"])
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(len(result["trades"]), 1)
        order = result["orders"][0]
        trade = result["trades"][0]
        self.assertEqual(order["native_status"], "ALLTRADED")
        self.assertEqual(order["status"], "filled")
        self.assertTrue(order["native_order_id"].startswith("BACKTESTING."))
        self.assertTrue(trade["native_trade_id"].startswith("BACKTESTING."))
        self.assertEqual(order["native_order_id"], trade["native_order_id"])
        self.assertEqual(order["native_order_raw_id"], trade["native_order_raw_id"])
        self.assertEqual(trade["fill_datetime"], "2024-01-04T09:32:00")
        self.assertEqual(trade["commission"], 5.0)
        self.assertEqual(trade["stamp_duty"], 0.0)
        self.assertEqual(result["metrics"]["fees_paid"], 5.0)
        self.assertEqual(result["equity"][2]["cash"], 49_995.0)
        self.assertFalse(result["metrics"]["negative_cash"])
        self.assertIn("not_asserted", result["data_manifest"]["reference_matcher_equivalence"])
        self.assertFalse(result["positions"][0]["t_plus_one_verified"])

    def test_capacity_rejection_is_all_or_none_then_native_fill_can_retry(self):
        frame = synthetic_bars(
            [
                "2024-01-04 09:31",
                "2024-01-04 09:32",
                "2024-01-04 09:33",
            ],
            [10.0, 10.1, 10.2],
            volumes=[1_000_000.0, 1_000_000.0, 1_000_000.0],
            turnovers=[10_000_000.0, 100_000.0, 10_000_000.0],
        )
        result = self._run(config(), frame)

        self.assertEqual(
            result["orders"][0]["reason"], "all_or_none_turnover_capacity_exceeded"
        )
        self.assertEqual(result["orders"][0]["status"], "rejected")
        self.assertEqual(result["orders"][1]["status"], "filled")
        self.assertEqual(result["trades"][0]["native_order_id"], result["orders"][1]["native_order_id"])
        self.assertEqual(
            result["data_manifest"]["all_or_none_turnover_capacity_rejections"], 1
        )

    def test_slippage_is_a_separate_cash_debit_and_budget_reduces_by_one_lot(self):
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"],
            [10.0, 10.1],
        )
        result = self._run(
            config(
                initial_cash=2_030,
                slippage_bps=100,
                params={
                    "target_fraction": 1.0,
                    "max_turnover_participation": 0.01,
                },
            ),
            frame,
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["quantity"], 100)
        self.assertEqual(trade["price"], 10.1)
        self.assertAlmostEqual(trade["slippage_cost"], 10.1)
        self.assertEqual(result["metrics"]["slippage_paid"], 10.1)
        self.assertEqual(
            result["data_manifest"]["slippage_cost_accounting"],
            "separate_cash_debit",
        )
        self.assertAlmostEqual(result["equity"][2]["cash"], 1_004.9)
        self.assertFalse(result["metrics"]["negative_cash"])

    def test_native_internal_exception_cannot_return_a_partial_success(self):
        frame = synthetic_bars(
            ["2024-01-04 09:31", "2024-01-04 09:32"],
            [10.0, 10.1],
        )
        with mock.patch.object(
            vnpy_bridge.BuyHoldStrategy,
            "on_bar",
            side_effect=RuntimeError("synthetic strategy failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "stopped before processing every supplied minute bar"
            ):
                self._run(config(), frame)

    def test_gap_zero_volume_and_one_price_next_bars_reject_native_order(self):
        cases = (
            (
                ["2024-01-04 09:31", "2024-01-04 09:33"],
                [10.0, 10.1],
                [1_000_000.0, 1_000_000.0],
                (),
                "next_source_minute_not_contiguous",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32"],
                [10.0, 10.1],
                [1_000_000.0, 0.0],
                (),
                "zero_volume_bar",
            ),
            (
                ["2024-01-04 09:31", "2024-01-04 09:32"],
                [10.0, 10.1],
                [1_000_000.0, 1_000_000.0],
                (1,),
                "one_price_bar",
            ),
        )
        for times, closes, volumes, flat, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                frame = synthetic_bars(
                    times, closes, volumes=volumes, flat_indices=flat
                )
                result = self._run(config(), frame)
                rejected = [
                    order
                    for order in result["orders"]
                    if order["status"] == "rejected"
                ]
                self.assertTrue(rejected)
                self.assertEqual(rejected[0]["reason"], expected_reason)
                self.assertFalse(result["trades"])

    def test_only_buy_hold_is_supported_by_native_backend(self):
        with self.assertRaisesRegex(ValueError, "buy_hold only"):
            vnpy_bridge.run_backtest(config(strategy="sma_cross"))


if __name__ == "__main__":
    unittest.main()
