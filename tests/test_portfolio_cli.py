"""Verify CLI publication and scope without executing a portfolio strategy."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from research_center.portfolio_cli import main


class PortfolioCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "config.json"
        self.file.write_text("{}")
        self.store = self.root / "output"
        self.kernel = ModuleType("research_center.portfolio_kernel")
        self.kernel.normalize_portfolio = lambda value: {"symbol_configs": []}
        self.kernel.initial_state = lambda config: {}
        self.kernel.advance = lambda config, state, frames, final: {"frames": len(frames)}
        self.kernel.result_from_state = lambda config, state: state
        self.provenance = ModuleType("research_center.portfolio_provenance")
        self.provenance.snapshot_sources = lambda root: {"schema_version": 1, "source_version": "f" * 64, "files": {}, "runtime": {"python": "synthetic"}}
        self.provenance.verify_sources = lambda root, snapshot: None

    def call(self, *args):
        with patch.dict("sys.modules", {"research_center.portfolio_kernel": self.kernel,
                "research_center.portfolio_provenance": self.provenance}), contextlib.redirect_stdout(io.StringIO()):
            return main(["--store", str(self.store), *args])

    def test_plan_has_no_output_directory_or_data_read(self):
        with patch("research_center.portfolio_cli.load_aligned_frames") as load:
            self.assertEqual(self.call("plan", "--file", str(self.file)), 0)
            load.assert_not_called()
        self.assertFalse(self.store.exists())

    def test_load_failure_has_terminal_receipt_and_no_success_result(self):
        with patch("research_center.portfolio_cli.load_aligned_frames", side_effect=ValueError("unaligned")):
            self.assertEqual(self.call("run", "--file", str(self.file)), 1)
        directory = next(self.store.iterdir())
        receipt = json.loads((directory / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse((directory / "result.json").exists())
        self.assertEqual(self.call("show", directory.name), 0)

    def test_success_publishes_hashes_and_unique_runs(self):
        with patch("research_center.portfolio_cli.load_aligned_frames", return_value=([{}], {})):
            for _ in range(2):
                self.assertEqual(self.call("run", "--file", str(self.file)), 0)
        self.assertEqual(len(list(self.store.iterdir())), 2)
        for directory in self.store.iterdir():
            receipt = json.loads((directory / "receipt.json").read_text())
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(set(receipt["artifact_sha256"]), {"config.json", "data_manifest.json", "state.json", "result.json", "source_snapshot.json"})
            self.assertEqual(receipt["code_version"], "f" * 64)
            result = json.loads((directory / "result.json").read_text())
            self.assertEqual(result["provenance"]["source_version"], receipt["code_version"])
            self.assertFalse(receipt["paper_admission"])

    def test_changed_sources_fail_before_success_result_publication(self):
        with patch.object(self.provenance, "verify_sources", side_effect=[None, ValueError("执行源码已变化")]), patch(
                "research_center.portfolio_cli.load_aligned_frames", return_value=([{}], {})):
            self.assertEqual(self.call("run", "--file", str(self.file)), 1)
        directory = next(self.store.iterdir())
        self.assertEqual(json.loads((directory / "receipt.json").read_text())["status"], "failed")
        self.assertTrue((directory / "source_snapshot.json").exists())
        self.assertFalse((directory / "result.json").exists())
