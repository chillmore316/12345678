"""Portfolio admission, cancellation and recovery using synthetic job records."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from research_center.portfolio_workflow import PortfolioSourceChanged
from research_center.workflow_jobs import WorkflowJobs, _run_job


def spec():
    return {"config": {"symbols": ["000001.SZSE", "000002.SZSE"], "start": "2024-01-02",
        "end": "2024-01-05", "strategy": "buy_hold", "initial_cash": 100000}}


class PortfolioWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.store = SimpleNamespace(root=self.root)
        self.jobs = WorkflowJobs(self.store)
        self.snapshot = patch("research_center.portfolio_workflow.snapshot_sources", return_value={"source_version": "f" * 64})
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def admit(self, payload=None, request_id=None):
        with patch("research_center.workflow_jobs.subprocess.Popen", return_value=SimpleNamespace(pid=999999, terminate=lambda: None, wait=lambda **_: None)):
            return self.jobs.submit("portfolio", payload or spec(), request_id or uuid.uuid4().hex)

    def claim(self, record):
        with self.jobs._connect() as connection:
            token = connection.execute("SELECT launch_token FROM jobs WHERE job_id=?", (record["job_id"],)).fetchone()[0]
        return self.jobs._claim_job(record["job_id"], token)

    def test_preview_reads_no_prices_and_creates_no_store(self):
        with patch("research_center.portfolio_data.load_aligned_frames", side_effect=AssertionError("must not read prices")):
            plan = self.jobs.preview("portfolio", spec())
        self.assertTrue(plan["plan_only"])
        self.assertTrue(plan["scope"]["portfolio_aggregation"])
        self.assertEqual(plan["source_version"], "f" * 64)
        self.assertEqual(plan["limits"]["wall_seconds"], 300)
        self.assertEqual(plan["case_count"], 1)
        self.assertFalse(self.root.exists())

    def test_unsafe_fields_holdout_and_stale_source_fail_before_admission(self):
        for field in ("data_root", "backend", "command", "env"):
            bad = spec()
            bad["config"][field] = "not-allowed"
            with self.assertRaises(ValueError):
                self.jobs.preview("portfolio", bad)
        bad = spec()
        bad["config"]["start"] = "2026-01-02"
        bad["config"]["end"] = "2026-01-05"
        with self.assertRaises(ValueError):
            self.jobs.preview("portfolio", bad)
        bad = spec()
        bad["expected_source_version"] = "0" * 64
        with self.assertRaises(PortfolioSourceChanged):
            self.admit(bad)
        self.assertFalse(self.root.exists())

    def test_exact_retry_survives_source_change_without_new_spawn(self):
        request = uuid.uuid4().hex
        first = self.admit(request_id=request)
        with patch("research_center.workflow_jobs._plan_for", side_effect=AssertionError("must not replan")), patch(
                "research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                "research_center.workflow_jobs.subprocess.Popen") as spawn:
            retry = self.jobs.submit("portfolio", spec(), request)
        self.assertEqual(first["job_id"], retry["job_id"])
        self.assertEqual(retry["status"], "interrupted")
        spawn.assert_not_called()

    def test_queued_cancel_is_idempotent_and_does_not_start_calculation(self):
        record = self.admit()
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=True):
            first = self.jobs.request_cancel(record["job_id"])
            second = self.jobs.request_cancel(record["job_id"])
        self.assertTrue(first["cancel_requested"])
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["cancel_requested_at"], second["cancel_requested_at"])
        self.assertTrue(self.jobs.cancel_path(record["job_id"]).is_file())
        self.assertIsNone(self.claim(record))
        self.assertEqual(self.jobs.get(record["job_id"])["status"], "cancelled")

    def test_cancel_marker_symlink_does_not_write_outside_store(self):
        record = self.admit()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.jobs.cancel_path(record["job_id"]).parent.symlink_to(outside, target_is_directory=True)
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=True):
            with self.assertRaises(OSError):
                self.jobs.request_cancel(record["job_id"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_unknown_or_alive_inner_execution_keeps_active_and_blocks_new_admission(self):
        record = self.admit()
        for value in (True, None):
            with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                    "research_center.workflow_jobs._portfolio_execution_is_alive", return_value=value):
                self.assertEqual(self.jobs.get(record["job_id"])["status"], "queued")
                with self.assertRaisesRegex(ValueError, "已有研究实验运行"):
                    self.admit()

    def test_worker_uses_bounded_executor_and_safe_report_link(self):
        record = self.claim(self.admit())
        def execute(config, root, **options):
            self.assertEqual(options["run_id"], record["job_id"])
            self.assertEqual(options["expected_source_version"], "f" * 64)
            self.assertGreater(options["timeout_seconds"], 0.1)
            self.assertLessEqual(options["timeout_seconds"], 300)
            self.assertEqual(options["cancel_path"], self.jobs.cancel_path(record["job_id"]))
            self.assertEqual(Path(root), self.root.parent / "research_portfolios")
            return {"run_id": record["job_id"], "status": "completed", "cleanup_confirmed": True,
                    "code_version": "f" * 64, "frames": 4, "private_token": "PRIVATE-TOKEN-SENTINEL"}
        with patch("research_center.portfolio_cli.run_bounded", side_effect=execute, create=True):
            _run_job(self.store, record, self.jobs)
        final = self.jobs.get(record["job_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["receipt"]["cases"][0]["report_url"], "/portfolio#" + record["job_id"])
        self.assertNotIn("PRIVATE-TOKEN-SENTINEL", json.dumps(final))

    def test_unconfirmed_cleanup_remains_active_until_child_is_definitely_gone(self):
        record = self.claim(self.admit())
        with patch("research_center.portfolio_cli.run_bounded", return_value={"status": "interrupted", "cleanup_confirmed": False}, create=True):
            _run_job(self.store, record, self.jobs)
        self.assertEqual(self.jobs._find_job(record["job_id"])["status"], "running")
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                "research_center.workflow_jobs._portfolio_execution_is_alive", return_value=None):
            self.assertEqual(self.jobs.get(record["job_id"])["status"], "running")
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                "research_center.workflow_jobs._portfolio_execution_is_alive", return_value=False):
            self.assertEqual(self.jobs.get(record["job_id"])["status"], "interrupted")

    def test_completed_archive_recovers_job_without_reexecution(self):
        record = self.admit()
        detail = {"receipt": {"status": "completed", "code_version": "f" * 64},
                  "result": {"config_sha256": record["plan"]["config_sha256"]}}
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                "research_center.workflow_jobs._portfolio_execution_is_alive", return_value=False), patch(
                "research_center.portfolio_archive.PortfolioArchive.get", return_value=detail), patch(
                "research_center.workflow_jobs.subprocess.Popen") as spawn:
            recovered = self.jobs.get(record["job_id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(recovered["receipt"]["recovered_from_completed_archive"])
        spawn.assert_not_called()

    def test_dead_controller_and_child_reconcile_only_matching_unfinished_receipt(self):
        record = self.admit()
        directory = self.root.parent / "research_portfolios" / record["job_id"]
        directory.mkdir(parents=True)
        receipt = {"run_id": record["job_id"], "mode": "RESEARCH_ONLY", "paper_admission": False, "status": "running"}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        sentinel = directory / "state.json"
        sentinel.write_text("PRIVATE-STATE-SENTINEL")
        with patch("research_center.workflow_jobs._worker_is_alive", return_value=False), patch(
                "research_center.workflow_jobs._portfolio_execution_is_alive", return_value=False):
            recovered = self.jobs.get(record["job_id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(json.loads((directory / "receipt.json").read_text())["status"], "interrupted")
        self.assertEqual(sentinel.read_text(), "PRIVATE-STATE-SENTINEL")


if __name__ == "__main__":
    unittest.main()
