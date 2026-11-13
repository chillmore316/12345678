from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from research_center.custom_source_store import CustomSourceStore
from research_center.symbol_comparison import execute_symbols, plan_symbols


def comparison_spec() -> dict:
    return {
        "base": {
            "strategy": "sma_cross",
            "backend": "reference",
            "start": "2024-01-02",
            "end": "2024-01-05",
            "initial_cash": 10_000,
            "commission_rate": 0.00025,
            "min_commission": 5,
            "slippage_bps": 5,
            "params": {
                "fast": 3,
                "slow": 12,
                "target_fraction": 0.4,
                "max_turnover_participation": 0.02,
            },
        },
        "symbols": ["000001.XSHE", "000002.SZSE"],
    }


class ResearchSymbolComparisonPlanTest(unittest.TestCase):
    def test_plan_normalizes_symbols_and_keeps_one_fixed_research_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_root = Path(temp_dir) / "run-store-not-created"
            plan = plan_symbols(SimpleNamespace(root=store_root), comparison_spec())

        self.assertTrue(plan["valid"])
        self.assertTrue(plan["plan_only"])
        self.assertEqual(plan["mode"], "RESEARCH_ONLY")
        self.assertFalse(plan["paper_admission"])
        self.assertEqual(plan["scope"], {
            "comparison_scope": "fixed_strategy_per_symbol_research_comparison",
            "portfolio_aggregation": False,
            "automatic_selection": False,
            "untouched_oos": False,
            "symbol_count": 2,
            "symbols": ["000001.SZSE", "000002.SZSE"],
        })
        self.assertEqual(plan["case_count"], 2)
        self.assertEqual([case["label"] for case in plan["cases"]],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual([case["symbol"] for case in plan["cases"]],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual({case["config"]["experiment_id"] for case in plan["cases"]},
                         {plan["experiment_id"]})
        self.assertEqual([case["config"]["label"] for case in plan["cases"]],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual([case["config"]["start"] for case in plan["cases"]],
                         ["2024-01-02", "2024-01-02"])
        self.assertEqual([case["config"]["end"] for case in plan["cases"]],
                         ["2024-01-05", "2024-01-05"])
        self.assertEqual({case["config"]["initial_cash"] for case in plan["cases"]}, {10_000.0})
        self.assertEqual({case["config"]["commission_rate"] for case in plan["cases"]}, {0.00025})
        self.assertEqual({case["config"]["min_commission"] for case in plan["cases"]}, {5.0})
        self.assertEqual({case["config"]["slippage_bps"] for case in plan["cases"]}, {5.0})
        self.assertEqual({case["config"]["params"]["fast"] for case in plan["cases"]}, {3})
        self.assertFalse(store_root.exists(), "plan-only must not initialize the run ledger")

    def test_rejects_unknown_fields_path_overrides_and_unsupported_builtin_fields(self) -> None:
        invalid_specs = []

        unknown_top = comparison_spec()
        unknown_top["retry"] = True
        invalid_specs.append(unknown_top)

        symbol_in_base = comparison_spec()
        symbol_in_base["base"]["symbol"] = "000001.SZSE"
        invalid_specs.append(symbol_in_base)

        path_override = comparison_spec()
        path_override["base"]["data_root"] = "/tmp/other-data"
        invalid_specs.append(path_override)

        source_path_override = comparison_spec()
        source_path_override["base"]["source_path"] = "/tmp/strategy.py"
        invalid_specs.append(source_path_override)

        ignored_builtin_field = comparison_spec()
        ignored_builtin_field["base"]["source_id"] = "not-used"
        invalid_specs.append(ignored_builtin_field)

        for spec in invalid_specs:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                plan_symbols(SimpleNamespace(root=Path("/tmp/unused-symbol-store")), spec)

    def test_rejects_wrong_symbol_count_types_and_duplicate_aliases(self) -> None:
        invalid_specs = []
        invalid_specs.append({"base": comparison_spec()["base"], "symbols": ["000001.SZSE"]})
        invalid_specs.append({"base": comparison_spec()["base"], "symbols": [
            "000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE", "000005.SZSE",
        ]})
        invalid_specs.append({"base": comparison_spec()["base"], "symbols": [
            "000001.SZSE", 2,
        ]})
        invalid_specs.append({"base": comparison_spec()["base"], "symbols": [
            "000001.XSHE", "000001.SZSE",
        ]})

        for spec in invalid_specs:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                plan_symbols(SimpleNamespace(root=Path("/tmp/unused-symbol-store")), spec)

    def test_registry_rejects_non_mainboard_and_invalid_shared_ranges(self) -> None:
        invalid_specs = []
        non_mainboard = comparison_spec()
        non_mainboard["symbols"][1] = "300001.SZSE"
        invalid_specs.append(non_mainboard)

        too_long = comparison_spec()
        too_long["base"].update(start="2024-01-02", end="2024-02-02")
        invalid_specs.append(too_long)

        outside_period = comparison_spec()
        outside_period["base"].update(start="2026-01-02", end="2026-01-05")
        invalid_specs.append(outside_period)

        for spec in invalid_specs:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                plan_symbols(SimpleNamespace(root=Path("/tmp/unused-symbol-store")), spec)

    def test_custom_strategy_keeps_symbol_versions_and_matches_shared_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_store = CustomSourceStore(root / "custom_sources")
            registered = source_store.register(
                b"def build_strategy(api, params):\n    return object()\n",
                name="synthetic comparison strategy",
                default_params={"threshold": 1},
            )
            spec = comparison_spec()
            spec["base"] = {
                "strategy": "custom_python",
                "backend": "isolated_python",
                "source_id": registered["registration_id"],
                "start": "2024-01-02",
                "end": "2024-01-05",
                "initial_cash": 10_000,
                "custom_params": {"threshold": 2},
                "history_limit": 30,
            }
            plan = plan_symbols(SimpleNamespace(root=root), spec)

        versions = {case["config"]["strategy_version"] for case in plan["cases"]}
        self.assertEqual(len(versions), 2, "custom versions bind the per-symbol execution config")
        self.assertEqual({case["strategy_version"] for case in plan["cases"]}, versions)
        self.assertEqual({case["config"]["source_id"] for case in plan["cases"]},
                         {registered["registration_id"]})
        self.assertEqual(len({case["config"]["source_sha256"] for case in plan["cases"]}), 1)
        self.assertEqual({case["config"]["custom_params"]["threshold"] for case in plan["cases"]},
                         {2})
        self.assertEqual([case["config"]["execution_config"]["symbol"] for case in plan["cases"]],
                         ["000001.SZSE", "000002.SZSE"])


class ResearchSymbolComparisonExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SimpleNamespace(
            root=Path("/tmp/unused-symbol-store"),
            get=lambda run_id: {"run_id": run_id, "status": "queued"},
            list=lambda: [],
        )

    def test_execution_is_sequential_and_enriches_events_and_receipt(self) -> None:
        events: list[dict] = []
        submitted: list[dict] = []

        def submit(_store, config):
            submitted.append(config)
            return {"run_id": f"run-{len(submitted)}", "status": "queued"}

        def refresh(_store, record):
            return {**record, "status": "completed"}

        with patch("research_center.runner.submit", side_effect=submit), \
             patch("research_center.runner.refresh_status", side_effect=refresh), \
             patch("research_center.batch.time.sleep"):
            receipt = execute_symbols(self.store, comparison_spec(), on_event=events.append)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["mode"], "RESEARCH_ONLY")
        self.assertFalse(receipt["paper_admission"])
        self.assertFalse(receipt["scope"]["portfolio_aggregation"])
        self.assertFalse(receipt["scope"]["automatic_selection"])
        self.assertFalse(receipt["scope"]["untouched_oos"])
        self.assertEqual(receipt["scope"]["symbols"], ["000001.SZSE", "000002.SZSE"])
        self.assertEqual([case["status"] for case in receipt["cases"]], ["completed", "completed"])
        self.assertEqual([case["symbol"] for case in receipt["cases"]],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual([case["run_id"] for case in receipt["cases"]], ["run-1", "run-2"])
        self.assertEqual([config["symbol"] for config in submitted],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual([config["label"] for config in submitted],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual({config["experiment_id"] for config in submitted},
                         {receipt["experiment_id"]})
        self.assertEqual(events[0]["type"], "planned")
        self.assertEqual(events[0]["scope"], receipt["scope"])
        self.assertEqual([case["symbol"] for case in events[0]["cases"]],
                         ["000001.SZSE", "000002.SZSE"])
        submitted_events = [event for event in events if event["type"] == "submitted"]
        self.assertEqual([event["case"]["symbol"] for event in submitted_events],
                         ["000001.SZSE", "000002.SZSE"])
        self.assertEqual(events[-1]["type"], "completed")
        self.assertEqual(events[-1]["receipt"]["scope"], receipt["scope"])

    def test_invalid_later_symbol_prevents_all_submissions(self) -> None:
        spec = comparison_spec()
        spec["symbols"][1] = "300001.SZSE"
        with patch("research_center.runner.submit") as submit:
            with self.assertRaisesRegex(ValueError, "前缀"):
                execute_symbols(self.store, spec)
        submit.assert_not_called()

    def test_failed_case_stops_later_symbols_and_retains_symbol_receipt(self) -> None:
        submitted: list[str] = []

        def submit(_store, config):
            submitted.append(config["symbol"])
            return {"run_id": "a" * 32, "status": "failed"}

        with patch("research_center.runner.submit", side_effect=submit):
            receipt = execute_symbols(self.store, comparison_spec())

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(submitted, ["000001.SZSE"])
        self.assertEqual(receipt["cases"][0]["run_id"], "a" * 32)
        self.assertEqual(receipt["cases"][0]["symbol"], "000001.SZSE")
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        self.assertEqual(receipt["cases"][1]["symbol"], "000002.SZSE")

    def test_interrupt_after_submission_keeps_symbol_and_run_id(self) -> None:
        def stop_after_submit(event):
            if event["type"] == "submitted":
                raise KeyboardInterrupt

        with patch("research_center.runner.submit", return_value={
                "run_id": "b" * 32, "status": "queued",
        }):
            receipt = execute_symbols(
                self.store,
                comparison_spec(),
                on_event=stop_after_submit,
            )

        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["run_id"], "b" * 32)
        self.assertEqual(receipt["cases"][0]["symbol"], "000001.SZSE")
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        self.assertEqual(receipt["cases"][1]["symbol"], "000002.SZSE")


if __name__ == "__main__":
    unittest.main()
