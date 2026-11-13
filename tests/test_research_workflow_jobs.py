from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import patch

from research_center.workflow_jobs import WorkflowJobs, _run_job


def batch_spec() -> dict:
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
                "train_start": "2024-01-01",
                "train_end": "2024-01-20",
                "validation_start": "2024-01-21",
                "validation_end": "2024-01-31",
            },
        ],
    }


class ResearchWorkflowJobsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "research-store"
        self.store = SimpleNamespace(root=self.root)
        self.jobs = WorkflowJobs(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def request_id() -> str:
        return uuid.uuid4().hex

    def fake_popen(self, pid: int = 999_999):
        return patch("research_center.workflow_jobs.subprocess.Popen",
                     return_value=SimpleNamespace(pid=pid, terminate=lambda: None,
                                                   wait=lambda **_kwargs: None))

    def test_preview_is_plan_only_and_does_not_create_store(self) -> None:
        plan = self.jobs.preview("batch", batch_spec())

        self.assertTrue(plan["valid"])
        self.assertTrue(plan["plan_only"])
        self.assertEqual(plan["mode"], "RESEARCH_ONLY")
        self.assertFalse(plan["paper_admission"])
        self.assertEqual(plan["case_count"], 2)
        self.assertFalse(self.root.exists())

    def test_submit_persists_fixed_identity_and_initial_receipt_before_spawn(self) -> None:
        request_id = self.request_id()
        observed: list[dict] = []

        def spawn(arguments, **_kwargs):
            # The durable job record and complete case receipt precede Popen.
            observed.append(self.jobs._find_request(request_id))
            self.assertIn("research_center.workflow_jobs", arguments)
            return SimpleNamespace(pid=999_998, terminate=lambda: None, wait=lambda **_kwargs: None)

        with patch("research_center.workflow_jobs.subprocess.Popen", side_effect=spawn):
            record = self.jobs.submit("batch", batch_spec(), request_id)

        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["mode"], "RESEARCH_ONLY")
        self.assertFalse(record["paper_admission"])
        self.assertEqual(record["experiment_id"], record["job_id"])
        self.assertEqual(record["plan"]["experiment_id"], record["job_id"])
        self.assertEqual({case["config"]["experiment_id"] for case in record["plan"]["cases"]},
                         {record["job_id"]})
        self.assertEqual(len(record["receipt"]["cases"]), 2)
        self.assertTrue(all(case["status"] == "not_started" for case in record["receipt"]["cases"]))
        self.assertEqual(observed[0]["job_id"], record["job_id"])
        json.dumps(record, ensure_ascii=False, allow_nan=False)

    def test_exact_idempotent_retry_is_returned_without_replanning_or_respawn(self) -> None:
        request_id = self.request_id()
        with self.fake_popen() as popen:
            first = self.jobs.submit("batch", batch_spec(), request_id)

        with patch("research_center.workflow_jobs._plan_for", side_effect=AssertionError("must not replan")), \
             patch("research_center.workflow_jobs._worker_is_alive", return_value=False), \
             patch("research_center.workflow_jobs.subprocess.Popen") as second_spawn:
            retry = self.jobs.submit("batch", batch_spec(), request_id)

        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertEqual(retry["status"], "interrupted")
        popen.assert_called_once()
        second_spawn.assert_not_called()

    def test_same_id_with_changed_payload_is_rejected(self) -> None:
        request_id = self.request_id()
        with self.fake_popen():
            self.jobs.submit("batch", batch_spec(), request_id)
        changed = batch_spec()
        changed["cases"][0]["params"]["fast"] = 6

        with self.assertRaisesRegex(ValueError, "different workflow content"):
            self.jobs.submit("batch", changed, request_id)

    def test_request_id_must_be_lowercase_uuid4_hex(self) -> None:
        for invalid in ("not-a-uuid", str(uuid.uuid4()), "0" * 32):
            with self.subTest(request_id=invalid), self.assertRaises(ValueError):
                self.jobs.submit("batch", batch_spec(), invalid)
        self.assertFalse(self.root.exists())

    def test_only_one_active_workflow_can_be_admitted_per_store(self) -> None:
        with self.fake_popen(pid=999_997):
            first = self.jobs.submit("batch", batch_spec(), self.request_id())
        self.assertEqual(first["status"], "queued")

        with patch("research_center.workflow_jobs._worker_is_alive", return_value=True), \
             self.fake_popen(pid=999_996) as second_spawn:
            with self.assertRaisesRegex(ValueError, "已有研究实验运行"):
                self.jobs.submit("rolling", rolling_spec(), self.request_id())
        second_spawn.assert_not_called()

    def test_post_admission_spawn_failure_returns_durable_failed_job(self) -> None:
        request_id = self.request_id()
        with patch("research_center.workflow_jobs.subprocess.Popen", side_effect=OSError("no process")):
            record = self.jobs.submit("batch", batch_spec(), request_id)

        self.assertEqual(record["status"], "failed")
        self.assertIn("no process", record["error"])
        with patch("research_center.workflow_jobs._plan_for", side_effect=AssertionError("must not replan")):
            retry = self.jobs.submit("batch", batch_spec(), request_id)
        self.assertEqual(retry["job_id"], record["job_id"])
        self.assertEqual(retry["status"], "failed")

    def test_get_and_list_reconcile_dead_worker_without_launching_one(self) -> None:
        with self.fake_popen():
            record = self.jobs.submit("batch", batch_spec(), self.request_id())
        partial = record["receipt"]
        partial["cases"][0].update(status="running", run_status="running", run_id="a" * 32)
        record.update(status="running", receipt=partial, started_at="2026-11-09T00:00:00+00:00")
        self.jobs._save_record(record)

        with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), \
             patch("research_center.workflow_jobs.subprocess.Popen") as spawn:
            recovered = self.jobs.get(record["job_id"])
            listed = self.jobs.list()

        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["receipt"]["cases"][0]["run_id"], "a" * 32)
        self.assertEqual(recovered["receipt"]["status"], "interrupted")
        self.assertEqual(listed["jobs"][0]["status"], "interrupted")
        self.assertNotIn("plan", listed["jobs"][0])
        self.assertNotIn("receipt", listed["jobs"][0])
        spawn.assert_not_called()

    def test_copied_job_does_not_belong_to_worker_in_original_store(self) -> None:
        with self.fake_popen():
            record = self.jobs.submit("batch", batch_spec(), self.request_id())
        restored = self.root.with_name("restored store")
        shutil.copytree(self.root, restored)
        restored_jobs = WorkflowJobs(SimpleNamespace(root=restored))
        command = (f"S python -m research_center.workflow_jobs --store {self.jobs.root} "
                   f"--job-id {record['job_id']} --token {'a' * 32}")
        process = SimpleNamespace(stdout=command, returncode=0)
        with patch("research_center.workflow_jobs.os.waitpid", side_effect=ChildProcessError), \
             patch("research_center.workflow_jobs.os.kill"), \
             patch("research_center.workflow_jobs.subprocess.run", return_value=process):
            self.assertEqual(self.jobs.get(record["job_id"])["status"], "queued")
            self.assertEqual(restored_jobs.get(record["job_id"])["status"], "interrupted")
            self.assertEqual(self.jobs.get(record["job_id"])["status"], "queued")

    def test_unknown_process_probe_preserves_active_admission(self) -> None:
        with self.fake_popen():
            record = self.jobs.submit("batch", batch_spec(), self.request_id())
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=None), \
             patch("research_center.workflow_jobs.subprocess.Popen") as spawn:
            self.assertEqual(self.jobs.get(record["job_id"])["status"], "queued")
            with self.assertRaisesRegex(ValueError, "已有"):
                self.jobs.submit("batch", batch_spec(), self.request_id())
        spawn.assert_not_called()

    def test_worker_reuses_persisted_plan_and_saves_progress_and_terminal_receipt(self) -> None:
        request_id = self.request_id()
        with self.fake_popen():
            queued = self.jobs.submit("batch", batch_spec(), request_id)
        with self.jobs._connect() as connection:
            token = connection.execute(
                "SELECT launch_token FROM jobs WHERE job_id=?", (queued["job_id"],)
            ).fetchone()[0]
        claimed = self.jobs._claim_job(queued["job_id"], token)
        self.assertEqual(claimed["status"], "running")

        final_receipt = {
            "schema_version": 1,
            "status": "completed",
            "experiment_id": queued["job_id"],
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
            "cases": [
                {"index": 1, "label": "short-fast", "status": "completed",
                 "run_status": "completed", "run_id": "b" * 32},
                {"index": 2, "label": "long-fast", "status": "completed",
                 "run_status": "completed", "run_id": "c" * 32},
            ],
        }

        def run_plan(store, plan, *, wait_seconds, poll_interval, on_event):
            self.assertEqual(store.root, self.root)
            self.assertEqual(plan["experiment_id"], queued["job_id"])
            self.assertEqual(wait_seconds, 300.0)
            on_event({"type": "submitted", "case": final_receipt["cases"][0]})
            with patch("research_center.workflow_jobs._worker_is_alive", return_value=True):
                partial = self.jobs.get(queued["job_id"])["receipt"]
            self.assertEqual(partial["cases"][0]["run_id"], "b" * 32)
            return final_receipt

        with patch("research_center.batch._run_normalized_plan", side_effect=run_plan), \
             patch("research_center.workflow_jobs._worker_is_alive", return_value=True):
            _run_job(self.store, claimed, self.jobs)

        completed = self.jobs.get(queued["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["receipt"]["cases"][1]["run_id"], "c" * 32)
        self.assertEqual(completed["experiment_id"], queued["job_id"])

    def test_rolling_jobs_keep_fold_metadata_in_the_plan_and_receipt(self) -> None:
        with self.fake_popen():
            record = self.jobs.submit("rolling", rolling_spec(), self.request_id())

        self.assertEqual(record["plan"]["fold_count"], 2)
        self.assertEqual(record["receipt"]["fold_count"], 2)
        self.assertEqual(len(record["receipt"]["cases"]), 4)
        self.assertEqual(record["receipt"]["cases"][0]["role"], "train")
        self.assertEqual(record["receipt"]["cases"][1]["role"], "validation")

    def test_symbol_jobs_keep_nonportfolio_scope_in_saved_record(self) -> None:
        base = batch_spec()["base"]
        del base["symbol"]
        spec = {"base": base, "symbols": ["000001.SZSE", "000002.SZSE"]}
        preview = self.jobs.preview("symbols", spec)
        self.assertFalse(self.jobs.db_path.exists())
        self.assertEqual([case["config"]["symbol"] for case in preview["cases"]], spec["symbols"])
        with self.fake_popen():
            record = self.jobs.submit("symbols", spec, self.request_id())
        self.assertEqual(record["plan"]["scope"], record["receipt"]["scope"])
        self.assertFalse(record["receipt"]["scope"]["portfolio_aggregation"])
        self.assertFalse(record["paper_admission"])

    def test_list_has_stable_object_schema(self) -> None:
        self.jobs.root.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.jobs.list(), {"jobs": []})
        self.assertFalse(self.jobs.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
