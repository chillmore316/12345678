from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from research_center.custom_source_store import CustomSourceStore
from research_center.rolling import execute_rolling, plan_rolling


def rolling_spec() -> dict:
    return {
        "base": {
            "strategy": "sma_cross",
            "backend": "reference",
            "symbol": "000001.SZSE",
            "initial_cash": 10_000,
            "params": {"fast": 3, "slow": 12},
        },
        "folds": [
            {
                "train_start": "2024-01-01",
                "train_end": "2024-01-10",
                "validation_start": "2024-01-15",
                "validation_end": "2024-01-20",
            },
            {
                # The later training window may include the earlier validation window.
                "train_start": "2024-01-01",
                "train_end": "2024-01-20",
                "validation_start": "2024-01-21",
                "validation_end": "2024-01-31",
            },
        ],
    }


class ResearchRollingPlanTest(unittest.TestCase):
    def test_plan_normalizes_fixed_config_windows_without_creating_store(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store_root = Path(root) / "not-created"
            plan = plan_rolling(SimpleNamespace(root=store_root), rolling_spec())

            self.assertTrue(plan["valid"])
            self.assertTrue(plan["plan_only"])
            self.assertEqual(plan["mode"], "RESEARCH_ONLY")
            self.assertFalse(plan["paper_admission"])
            self.assertFalse(plan["untouched_oos"])
            self.assertFalse(plan["automatic_selection"])
            self.assertIn("strategy_version", plan["common"])
            self.assertEqual(plan["fold_count"], 2)
            self.assertEqual(plan["case_count"], 4)
            self.assertEqual(
                [case["label"] for case in plan["cases"]],
                ["第 1 折 · 训练段", "第 1 折 · 验证段", "第 2 折 · 训练段", "第 2 折 · 验证段"],
            )
            self.assertEqual(
                [(case["fold_index"], case["role"]) for case in plan["cases"]],
                [(1, "train"), (1, "validation"), (2, "train"), (2, "validation")],
            )
            self.assertEqual(
                [case["config"]["start"] for case in plan["cases"]],
                ["2024-01-01", "2024-01-15", "2024-01-01", "2024-01-21"],
            )
            self.assertEqual(
                [case["config"]["end"] for case in plan["cases"]],
                ["2024-01-10", "2024-01-20", "2024-01-20", "2024-01-31"],
            )
            self.assertEqual({case["config"]["initial_cash"] for case in plan["cases"]}, {10_000.0})
            self.assertTrue(all(case["config"]["params"] == plan["cases"][0]["config"]["params"]
                                for case in plan["cases"]))
            self.assertEqual({case["config"]["experiment_id"] for case in plan["cases"]},
                             {plan["experiment_id"]})
            self.assertTrue(all("parent_run_id" not in case["config"] for case in plan["cases"]))
            self.assertEqual(plan["folds"][1]["train"]["end"], "2024-01-20")
            self.assertFalse(store_root.exists(), "plan-only must not initialize the run store")

    def test_rejects_schema_and_noncanonical_dates(self) -> None:
        cases = []
        extra_top = rolling_spec()
        extra_top["retry"] = True
        cases.append(extra_top)
        base_date = rolling_spec()
        base_date["base"]["start"] = "2024-01-01"
        cases.append(base_date)
        bad_fold = rolling_spec()
        bad_fold["folds"][0]["unexpected"] = 1
        cases.append(bad_fold)
        noncanonical = rolling_spec()
        noncanonical["folds"][0]["train_start"] = "2024-1-01"
        cases.append(noncanonical)
        invalid_day = rolling_spec()
        invalid_day["folds"][0]["train_start"] = "2024-02-30"
        cases.append(invalid_day)

        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                plan_rolling(SimpleNamespace(root=Path("/tmp/unused-rolling-store")), spec)

    def test_requires_ordered_disjoint_validation_and_increasing_train_windows(self) -> None:
        invalid_specs = []

        overlap = rolling_spec()
        overlap["folds"][1].update(
            train_start="2024-01-05",
            train_end="2024-01-18",
            validation_start="2024-01-20",
            validation_end="2024-01-31",
        )
        invalid_specs.append(overlap)

        reverse_validation = rolling_spec()
        reverse_validation["folds"][1].update(
            train_start="2024-01-05",
            train_end="2024-01-18",
            validation_start="2024-01-19",
            validation_end="2024-01-20",
        )
        invalid_specs.append(reverse_validation)

        backward_train_start = rolling_spec()
        backward_train_start["folds"][1]["train_start"] = "2023-12-31"
        invalid_specs.append(backward_train_start)

        nonincreasing_train_end = rolling_spec()
        nonincreasing_train_end["folds"][1]["train_end"] = "2024-01-10"
        invalid_specs.append(nonincreasing_train_end)

        train_crosses_validation = rolling_spec()
        train_crosses_validation["folds"][0]["train_end"] = "2024-01-15"
        invalid_specs.append(train_crosses_validation)

        expected_errors = (
            "validation windows must be disjoint and chronological",
            "validation windows must be disjoint and chronological",
            "train_start values must be nondecreasing",
            "train_end values must be strictly increasing",
            "training must end before validation starts",
        )
        for spec, message in zip(invalid_specs, expected_errors):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, message):
                plan_rolling(SimpleNamespace(root=Path("/tmp/unused-rolling-store")), spec)

    def test_normalizer_rejects_long_or_out_of_period_windows(self) -> None:
        too_long = rolling_spec()
        too_long["folds"][0].update(
            train_end="2024-02-05",
            validation_start="2024-02-06",
            validation_end="2024-02-10",
        )
        too_long["folds"][1].update(
            train_start="2024-02-01",
            train_end="2024-02-11",
            validation_start="2024-02-12",
            validation_end="2024-02-20",
        )
        with self.assertRaisesRegex(ValueError, "31"):
            plan_rolling(SimpleNamespace(root=Path("/tmp/unused-rolling-store")), too_long)

        outside_period = rolling_spec()
        outside_period["folds"][1].update(
            validation_start="2025-12-31",
            validation_end="2026-01-02",
        )
        with self.assertRaisesRegex(ValueError, "2024-2025"):
            plan_rolling(SimpleNamespace(root=Path("/tmp/unused-rolling-store")), outside_period)

    def test_custom_per_window_identity_is_not_misreported_as_common(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_store = CustomSourceStore(root_path / "custom_sources")
            registered = source_store.register(
                b"def build_strategy(api, params):\n    return object()\n",
                name="synthetic rolling strategy",
                default_params={"threshold": 1},
            )
            spec = rolling_spec()
            spec["base"] = {
                "strategy": "custom_python",
                "backend": "isolated_python",
                "symbol": "000001.SZSE",
                "initial_cash": 10_000,
                "source_id": registered["registration_id"],
                "custom_params": {"threshold": 2},
            }
            plan = plan_rolling(SimpleNamespace(root=root_path), spec)

        self.assertEqual(plan["common"]["source_id"], registered["registration_id"])
        self.assertIn("source_sha256", plan["common"])
        self.assertIn("code_version", plan["common"])
        self.assertIn("data_root", plan["common"])
        self.assertNotIn("start", plan["common"])
        self.assertNotIn("end", plan["common"])
        self.assertNotIn("strategy_version", plan["common"])
        versions = {case["config"]["strategy_version"] for case in plan["cases"]}
        self.assertEqual(len(versions), 4, "each window retains its full execution identity")
        self.assertEqual(
            {case["config"]["execution_config"]["start"] for case in plan["cases"]},
            {"2024-01-01", "2024-01-15", "2024-01-21"},
        )

    def test_rejects_builtin_fields_ignored_by_the_runner(self) -> None:
        for field, value in (("source_id", "ignored"), ("history_limit", 60),
                             ("custom_params", {"x": 1})):
            spec = rolling_spec()
            spec["base"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "custom_python"):
                plan_rolling(SimpleNamespace(root=Path("/tmp/unused-rolling-store")), spec)


class ResearchRollingExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SimpleNamespace(
            root=Path("/tmp/unused-rolling-store"),
            get=lambda run_id: {"run_id": run_id, "status": "queued"},
            list=lambda: [],
        )

    def test_execution_uses_independent_sequential_runs_and_enriches_receipt(self) -> None:
        events: list[dict] = []
        submissions: list[dict] = []

        def submit(_store, config):
            submissions.append(config)
            return {"run_id": f"run-{len(submissions)}", "status": "queued"}

        def refresh(_store, record):
            return {**record, "status": "completed"}

        with patch("research_center.runner.submit", side_effect=submit), \
             patch("research_center.runner.refresh_status", side_effect=refresh), \
             patch("research_center.batch.time.sleep"):
            receipt = execute_rolling(self.store, rolling_spec(), on_event=events.append)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["mode"], "RESEARCH_ONLY")
        self.assertFalse(receipt["paper_admission"])
        self.assertFalse(receipt["untouched_oos"])
        self.assertFalse(receipt["automatic_selection"])
        self.assertEqual(receipt["fold_count"], 2)
        self.assertEqual([case["status"] for case in receipt["cases"]], ["completed"] * 4)
        self.assertEqual(
            [(case["fold_index"], case["role"]) for case in receipt["cases"]],
            [(1, "train"), (1, "validation"), (2, "train"), (2, "validation")],
        )
        self.assertEqual([case["run_id"] for case in receipt["cases"]],
                         ["run-1", "run-2", "run-3", "run-4"])
        self.assertEqual([config["label"] for config in submissions],
                         ["第 1 折 · 训练段", "第 1 折 · 验证段", "第 2 折 · 训练段", "第 2 折 · 验证段"])
        self.assertEqual({config["initial_cash"] for config in submissions}, {10_000.0})
        self.assertTrue(all(config.get("parent_run_id") is None for config in submissions))
        self.assertEqual(events[0]["type"], "planned")
        self.assertEqual(
            [(case["fold_index"], case["role"]) for case in events[0]["cases"]],
            [(1, "train"), (1, "validation"), (2, "train"), (2, "validation")],
        )
        self.assertEqual(events[-1]["type"], "completed")
        self.assertEqual(len(events[-1]["receipt"]["folds"]), 2)

    def test_late_invalid_window_prevents_all_submissions(self) -> None:
        spec = rolling_spec()
        spec["folds"][1].update(
            validation_start="2025-12-31",
            validation_end="2026-01-02",
        )
        with patch("research_center.runner.submit") as submit:
            with self.assertRaisesRegex(ValueError, "2024-2025"):
                execute_rolling(self.store, spec)
        submit.assert_not_called()

    def test_failure_stops_later_windows_and_keeps_role_and_run_id(self) -> None:
        submitted: list[str] = []

        def submit(_store, config):
            submitted.append(config["label"])
            return {"run_id": "a" * 32, "status": "failed"}

        with patch("research_center.runner.submit", side_effect=submit):
            receipt = execute_rolling(self.store, rolling_spec())

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(submitted, ["第 1 折 · 训练段"])
        self.assertEqual(receipt["cases"][0]["run_id"], "a" * 32)
        self.assertEqual((receipt["cases"][0]["fold_index"], receipt["cases"][0]["role"]),
                         (1, "train"))
        self.assertEqual(receipt["cases"][1]["status"], "not_started")

    def test_interrupt_keeps_admitted_run_and_rolling_case_metadata(self) -> None:
        submitted_events: list[dict] = []

        def interrupt_after_submit(event):
            if event["type"] == "submitted":
                submitted_events.append(event)
                raise KeyboardInterrupt

        with patch("research_center.runner.submit", return_value={
                "run_id": "b" * 32, "status": "queued",
        }):
            receipt = execute_rolling(
                self.store,
                rolling_spec(),
                on_event=interrupt_after_submit,
            )

        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["run_id"], "b" * 32)
        self.assertEqual((receipt["cases"][0]["fold_index"], receipt["cases"][0]["role"]),
                         (1, "train"))
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        self.assertEqual((submitted_events[0]["case"]["fold_index"],
                          submitted_events[0]["case"]["role"]), (1, "train"))


if __name__ == "__main__":
    unittest.main()

