"""CLI compatibility and parent-side portfolio execution contracts."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from research_center.portfolio_cli import main, run_bounded
from research_center.store import ROOT


class PortfolioCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "config.json"
        self.file.write_text("{}", encoding="utf-8")
        self.store = self.root / "output"

    def _kernel(self):
        kernel = ModuleType("research_center.portfolio_kernel")
        kernel.normalize_portfolio = lambda value: {"symbol_configs": [], "source": value}
        return kernel

    def test_plan_reads_config_without_creating_run_store_or_loading_data(self):
        output = io.StringIO()
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": self._kernel()}), \
                patch("research_center.portfolio_cli.load_aligned_frames") as load, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["--store", str(self.store), "plan", "--file", str(self.file)]), 0)
        load.assert_not_called()
        self.assertFalse(self.store.exists())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "RESEARCH_ONLY")
        self.assertFalse(payload["paper_admission"])

    def test_cli_run_uses_bounded_api_and_timeout_option(self):
        expected = {"run_id": "a" * 32, "status": "failed", "cleanup_confirmed": True}
        output = io.StringIO()
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": self._kernel()}), \
                patch("research_center.portfolio_cli.run_bounded", return_value=expected) as bounded, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["--store", str(self.store), "run", "--file", str(self.file),
                                   "--timeout-seconds", "0.5"]), 1)
        self.assertEqual(bounded.call_args.args[0]["source"], {})
        self.assertEqual(bounded.call_args.args[1], self.store)
        self.assertEqual(bounded.call_args.kwargs["timeout_seconds"], 0.5)
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertFalse(self.store.exists())

    def test_cli_show_keeps_receipt_lookup(self):
        run_id = "b" * 32
        directory = self.store / run_id
        directory.mkdir(parents=True)
        receipt = {"run_id": run_id, "status": "timed_out", "cleanup_confirmed": True}
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--store", str(self.store), "show", run_id]), 0)
        self.assertEqual(json.loads(output.getvalue()), receipt)

    def test_run_bounded_has_no_stdout_and_records_setup_failure_terminally(self):
        kernel = self._kernel()
        output = io.StringIO()
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": kernel}), \
                patch("research_center.portfolio_cli._source_snapshot", side_effect=OSError("private path")), \
                contextlib.redirect_stdout(output):
            receipt = run_bounded({}, self.store, run_id="c" * 32)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertNotIn("private path", receipt.get("error", ""))
        self.assertTrue((self.store / ("c" * 32) / "receipt.json").exists())
        self.assertFalse((self.store / ("c" * 32) / "result.json").exists())

    def test_expected_source_version_mismatch_fails_before_spawn(self):
        kernel = self._kernel()
        fake_snapshot = {
            "schema_version": 1,
            "source_version": "f" * 64,
            "runtime": {},
            "files": {},
        }
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": kernel}), \
                patch("research_center.portfolio_cli._source_snapshot", return_value=fake_snapshot), \
                patch("research_center.portfolio_cli._verify_source_snapshot"), \
                patch("research_center.portfolio_cli._spawn_worker") as spawn:
            receipt = run_bounded({}, self.store, run_id="d" * 32,
                                  expected_source_version="0" * 64)
        spawn.assert_not_called()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["code_version"], "f" * 64)
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertFalse((self.store / ("d" * 32) / "result.json").exists())

    def test_existing_protected_research_and_market_paths_remain_rejected(self):
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": self._kernel()}):
            for protected in (ROOT / "data" / "blocked", ROOT / "results" / "research_center" / "blocked"):
                with self.subTest(protected=protected), self.assertRaisesRegex(ValueError, "独立的研究目录"):
                    run_bounded({}, protected, run_id="e" * 32)
        self.assertFalse((ROOT / "data" / "blocked" / ("e" * 32)).exists())


if __name__ == "__main__":
    unittest.main()
