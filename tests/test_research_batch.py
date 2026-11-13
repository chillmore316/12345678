from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from research_center.batch import execute_batch, plan_batch
from research_center.custom_source_store import CustomSourceStore
from research_center.registry import normalize_config


def spec_with_two_cases() -> dict:
    return {
        "base": {
            "strategy": "sma_cross",
            "backend": "reference",
            "symbol": "000001.SZSE",
            "start": "2024-01-02",
            "end": "2024-01-05",
            "initial_cash": 10_000,
            "params": {"fast": 3, "slow": 12},
        },
        "cases": [
            {"label": "short-fast", "params": {"fast": 4}},
            {"label": "long-fast", "params": {"fast": 5}},
        ],
    }


class ResearchBatchPlanTest(unittest.TestCase):
    def test_plan_normalizes_all_cases_without_initializing_store(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root) / "not-created"
            plan = plan_batch(SimpleNamespace(root=root_path), spec_with_two_cases())

            self.assertTrue(plan["valid"])
            self.assertTrue(plan["plan_only"])
            self.assertEqual(plan["mode"], "RESEARCH_ONLY")
            self.assertFalse(plan["paper_admission"])
            self.assertEqual(plan["case_count"], 2)
            self.assertEqual([row["label"] for row in plan["cases"]], ["short-fast", "long-fast"])
            self.assertEqual({row["config"]["experiment_id"] for row in plan["cases"]},
                             {plan["experiment_id"]})
            self.assertEqual([row["config"]["params"]["fast"] for row in plan["cases"]], [4, 5])
            self.assertEqual(plan["common"]["backend"], "reference")
            self.assertFalse(root_path.exists(), "plan-only must not initialize the run ledger")

    def test_unknown_fields_and_duplicate_effective_cases_are_rejected(self) -> None:
        for location in ("spec", "base", "case"):
            spec = spec_with_two_cases()
            if location == "spec":
                spec["retry"] = True
            elif location == "base":
                spec["base"]["data_source_override"] = "/tmp/other"
            else:
                spec["cases"][0]["backend"] = "vnpy"
            with self.subTest(location=location), self.assertRaises(ValueError):
                plan_batch(SimpleNamespace(root=Path("/tmp/unused-batch-store")), spec)

        duplicate = spec_with_two_cases()
        duplicate["cases"][1]["params"] = {"fast": 4}
        with self.assertRaisesRegex(ValueError, "duplicate effective cases"):
            plan_batch(SimpleNamespace(root=Path("/tmp/unused-batch-store")), duplicate)

        ignored_input = spec_with_two_cases()
        ignored_input["base"]["custom_params"] = {"ignored": 1}
        with self.assertRaisesRegex(ValueError, "only supported for custom_python"):
            plan_batch(SimpleNamespace(root=Path("/tmp/unused-batch-store")), ignored_input)

        custom_source_override = spec_with_two_cases()
        custom_source_override["base"]["data_root"] = "/tmp/other-source"
        with self.assertRaises(ValueError):
            plan_batch(SimpleNamespace(root=Path("/tmp/unused-batch-store")), custom_source_override)

    def test_invalid_later_case_prevents_every_submission(self) -> None:
        store = SimpleNamespace(root=Path("/tmp/unused-batch-store"))
        spec = spec_with_two_cases()
        spec["cases"][1]["params"] = {"fast": 12}
        with patch("research_center.runner.submit") as submit:
            with self.assertRaises(ValueError):
                execute_batch(store, spec)
        submit.assert_not_called()

    def test_custom_config_remains_idempotent_through_runner_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_store = CustomSourceStore(root_path / "custom_sources")
            registered = source_store.register(
                b"def build_strategy(api, params):\n    return object()\n",
                name="synthetic batch strategy",
                default_params={"threshold": 1},
            )
            store = SimpleNamespace(root=root_path)
            spec = {
                "base": {
                    "strategy": "custom_python",
                    "backend": "isolated_python",
                    "source_id": registered["registration_id"],
                    "symbol": "000001.SZSE",
                    "start": "2024-01-02",
                    "end": "2024-01-05",
                    "initial_cash": 10_000,
                    "custom_params": {"threshold": 2},
                },
                "cases": [
                    {"label": "threshold-3", "custom_params": {"threshold": 3}},
                    {"label": "threshold-4", "custom_params": {"threshold": 4}},
                ],
            }
            plan = plan_batch(store, spec)

            for case in plan["cases"]:
                with self.subTest(label=case["label"]):
                    self.assertEqual(normalize_config(case["config"], store), case["config"])


class ResearchBatchExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SimpleNamespace(
            root=Path("/tmp/unused-batch-store"),
            get=lambda run_id: {"run_id": run_id, "status": "queued"},
            list=lambda: [],
        )

    def test_execution_is_sequential_and_emits_experiment_before_submit(self) -> None:
        events: list[dict] = []
        order: list[str] = []

        def submit(store, config):
            self.assertEqual(events[0]["type"], "planned")
            order.append("submit:" + config["label"])
            return {"run_id": config["label"], "status": "queued"}

        def refresh(store, record):
            order.append("complete:" + record["run_id"])
            return {"run_id": record["run_id"], "status": "completed"}

        with patch("research_center.runner.submit", side_effect=submit), \
             patch("research_center.runner.refresh_status", side_effect=refresh), \
             patch("research_center.batch.time.sleep"):
            receipt = execute_batch(self.store, spec_with_two_cases(), on_event=events.append)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["mode"], "RESEARCH_ONLY")
        self.assertFalse(receipt["paper_admission"])
        self.assertEqual(order, ["submit:short-fast", "complete:short-fast",
                                 "submit:long-fast", "complete:long-fast"])
        self.assertEqual([case["status"] for case in receipt["cases"]], ["completed", "completed"])
        self.assertEqual(events[0]["experiment_id"], receipt["experiment_id"])
        self.assertEqual(events[-1]["type"], "completed")

    def test_failure_stops_later_cases_and_preserves_submitted_run_id(self) -> None:
        submissions: list[str] = []

        def submit(store, config):
            submissions.append(config["label"])
            return {"run_id": "a" * 32, "status": "failed"}

        with patch("research_center.runner.submit", side_effect=submit):
            receipt = execute_batch(self.store, spec_with_two_cases())

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(submissions, ["short-fast"])
        self.assertEqual(receipt["cases"][0]["run_id"], "a" * 32)
        self.assertEqual(receipt["cases"][1]["status"], "not_started")

    def test_update_events_are_emitted_only_when_run_status_changes(self) -> None:
        events: list[dict] = []
        statuses = iter(("running", "running", "completed", "running", "running", "completed"))

        def submit(_store, config):
            return {"run_id": config["label"], "status": "queued"}

        def refresh(_store, record):
            return {"run_id": record["run_id"], "status": next(statuses)}

        with patch("research_center.runner.submit", side_effect=submit), \
             patch("research_center.runner.refresh_status", side_effect=refresh), \
             patch("research_center.batch.time.sleep"):
            receipt = execute_batch(self.store, spec_with_two_cases(), on_event=events.append)

        updates = [event for event in events if event["type"] == "updated"]
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(updates), 4)
        self.assertEqual([event["case"]["run_status"] for event in updates],
                         ["running", "completed", "running", "completed"])

    def test_timeout_returns_controllable_run_and_does_not_submit_next_case(self) -> None:
        submitted: list[str] = []
        with patch("research_center.runner.submit", side_effect=lambda _s, config: (
                submitted.append(config["label"]) or {"run_id": "b" * 32, "status": "queued"})), \
             patch("research_center.runner.refresh_status") as refresh, \
             patch("research_center.runner.cancel") as cancel:
            receipt = execute_batch(self.store, spec_with_two_cases(), wait_seconds=0)

        self.assertEqual(receipt["status"], "timed_out")
        self.assertEqual(receipt["cases"][0]["status"], "timed_out")
        self.assertEqual(receipt["cases"][0]["run_status"], "queued")
        self.assertEqual(receipt["cases"][0]["run_id"], "b" * 32)
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        self.assertEqual(submitted, ["short-fast"])
        refresh.assert_not_called()
        cancel.assert_not_called()

    def test_keyboard_interrupt_stops_batch_and_retains_run_reference(self) -> None:
        submitted: list[str] = []

        def submit(store, config):
            submitted.append(config["label"])
            return {"run_id": "c" * 32, "status": "queued"}

        with patch("research_center.runner.submit", side_effect=submit), \
             patch("research_center.runner.refresh_status", return_value={
                 "run_id": "c" * 32, "status": "running",
             }), \
             patch("research_center.batch.time.sleep", side_effect=KeyboardInterrupt):
            receipt = execute_batch(self.store, spec_with_two_cases())

        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["run_status"], "running")
        self.assertEqual(receipt["cases"][0]["run_id"], "c" * 32)
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        self.assertEqual(submitted, ["short-fast"])

    def test_interrupt_during_progress_keeps_receipt_and_stops_admission(self) -> None:
        for interrupted_event in ("planned", "submitted", "updated", "completed"):
            with self.subTest(event=interrupted_event):
                def emit(event):
                    if event["type"] == interrupted_event:
                        raise KeyboardInterrupt

                def submit(_store, config):
                    return {"run_id": config["label"], "status": "queued"}

                def refresh(_store, record):
                    return {**record, "status": "completed"}

                with patch("research_center.runner.submit", side_effect=submit) as admission, \
                     patch("research_center.runner.refresh_status", side_effect=refresh), \
                     patch("research_center.batch.time.sleep"):
                    receipt = execute_batch(self.store, spec_with_two_cases(), on_event=emit)

                if interrupted_event == "completed":
                    self.assertEqual(receipt["status"], "completed")
                    self.assertEqual(admission.call_count, 2)
                else:
                    self.assertEqual(receipt["status"], "interrupted")
                    self.assertEqual(receipt["cases"][1]["status"], "not_started")
                    self.assertEqual(admission.call_count, int(interrupted_event != "planned"))
                if interrupted_event != "planned":
                    self.assertEqual(receipt["cases"][0]["run_id"], "short-fast")

    def test_interrupt_after_admission_recovers_id_from_saved_record(self) -> None:
        persisted = []
        self.store.list = lambda: persisted

        class InterruptedRunRecord(dict):
            def __getitem__(self, key):
                if key == "run_id":
                    raise KeyboardInterrupt
                return super().__getitem__(key)

        def submit(_store, config):
            row = {"run_id": "d" * 32, "status": "queued", "label": config["label"],
                   "experiment_id": config["experiment_id"]}
            persisted.append(row)
            return InterruptedRunRecord(row)

        with patch("research_center.runner.submit", side_effect=submit) as admission:
            receipt = execute_batch(self.store, spec_with_two_cases())

        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["run_id"], "d" * 32)
        self.assertEqual(receipt["cases"][0]["run_status"], "queued")
        self.assertEqual(receipt["cases"][1]["status"], "not_started")
        admission.assert_called_once()


if __name__ == "__main__":
    unittest.main()
