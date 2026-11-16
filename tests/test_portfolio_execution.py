"""Lifecycle and synthetic-data tests for bounded portfolio execution."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

from research_center.portfolio_cli import (
    PortfolioExecutionBusy,
    _acquire_store_lock,
    _spawn_worker,
    execution_is_alive,
    run_bounded,
    _read_bytes, _terminate_owned_group, _assert_no_unreaped_runs, _process_identity,
)
from research_center.portfolio_archive import PortfolioArchive
from research_center.portfolio_provenance import snapshot_sources
from research_center.store import ROOT


def _portfolio_config(data_root: Path):
    return {
        "symbols": ["000001.SZSE", "000002.SZSE"],
        "start": "2024-01-04",
        "end": "2024-01-04",
        "strategy": "buy_hold",
        "initial_cash": 100_000,
        "commission_rate": 0,
        "min_commission": 0,
        "slippage_bps": 0,
        "data_root": str(data_root),
        "params": {"target_fraction": 0.5, "max_turnover_participation": 0.01},
    }


def _write_synthetic_parquet(data_root: Path, *, bars_per_symbol: int = 3):
    data_root.mkdir(parents=True, exist_ok=True)
    times = [
        pd.Timestamp("2024-01-04 09:31") + pd.Timedelta(minutes=index)
        for index in range(bars_per_symbol)
    ]
    rows = []
    for symbol_index, code in enumerate(("000001.XSHE", "000002.XSHE")):
        for index, timestamp in enumerate(times):
            close = 10.0 + symbol_index + index * 0.01
            rows.append({
                "time": timestamp,
                "code": code,
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 100_000.0,
                "money": close * 100_000.0,
            })
    path = data_root / "stock_1m_2024-01_batch000.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _sleeping_process(seconds: float, *, on_finish: Path | None = None):
    script = "import time;time.sleep(" + repr(seconds) + ")"
    if on_finish is not None:
        script += ";open(" + repr(str(on_finish)) + ", 'w').write('late')"
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class PortfolioExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "synthetic-data"
        _write_synthetic_parquet(self.data_root)
        self.store = self.root / "portfolio-store"
        self.config = _portfolio_config(self.data_root)

    def test_subprocess_success_publishes_only_the_five_hashed_schema2_artifacts(self):
        expected_version = snapshot_sources(ROOT)["source_version"]
        receipt = run_bounded(
            self.config,
            self.store,
            run_id="1" * 32,
            timeout_seconds=10,
            expected_source_version=expected_version,
        )
        directory = self.store / ("1" * 32)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["code_version"], expected_version)
        self.assertGreater(receipt["frames"], 0)
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertEqual(set(receipt["artifact_sha256"]), {
            "config.json", "data_manifest.json", "state.json", "result.json", "source_snapshot.json",
        })
        self.assertEqual(
            {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
             for name in receipt["artifact_sha256"]},
            receipt["artifact_sha256"],
        )
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        snapshot = json.loads((directory / "source_snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(result["provenance"]["source_version"], expected_version)
        self.assertEqual(result["provenance"]["runtime"], snapshot["runtime"])
        self.assertFalse(result["paper_admission"])
        self.assertFalse(receipt["paper_admission"])
        self.assertFalse(any(path.name.endswith(".json") for path in (directory / ".control").glob("*")))
        self.assertIs(execution_is_alive(self.store, "1" * 32), False)
        archived = PortfolioArchive(self.store).get("1" * 32)
        self.assertEqual(archived["receipt"]["source_snapshot_status"], "archived_and_verified")
        self.assertEqual(archived["result"]["provenance"]["source_version"], expected_version)

    def test_distinct_runs_receive_new_ids_and_run_directory_is_never_reused(self):
        first = run_bounded(self.config, self.store, timeout_seconds=10)
        second = run_bounded(self.config, self.store, timeout_seconds=10)
        self.assertNotEqual(first["run_id"], second["run_id"])
        with self.assertRaises(FileExistsError):
            run_bounded(self.config, self.store, run_id=first["run_id"], timeout_seconds=10)

    def test_archive_root_lock_is_nonblocking_and_refuses_concurrent_execution(self):
        lock_descriptor = _acquire_store_lock(self.store)
        try:
            with self.assertRaises(PortfolioExecutionBusy):
                run_bounded(self.config, self.store, run_id="2" * 32, timeout_seconds=1)
        finally:
            os.close(lock_descriptor)
        self.assertFalse((self.store / ("2" * 32)).exists())

    def test_fifo_and_oversized_inventory_fail_without_unbounded_read(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            _read_bytes(fifo, 1024)
        self.store.mkdir()
        for index in range(4):
            (self.store / str(index)).touch()
        with patch("research_center.portfolio_cli._MAX_ARCHIVE_ENTRIES", 3):
            with self.assertRaises(PortfolioExecutionBusy):
                _assert_no_unreaped_runs(self.store)

    def test_reaped_group_is_never_signalled(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait(timeout=3)
        with patch("research_center.portfolio_cli.os.killpg", side_effect=ProcessLookupError) as kill:
            self.assertTrue(_terminate_owned_group(process))
            kill.assert_called_once_with(process.pid, 0)

    def test_missing_owner_never_converts_unconfirmed_cleanup_into_dead(self):
        self.store.mkdir()
        run_id = "a" * 32
        directory = self.store / run_id
        directory.mkdir()
        receipt = {"run_id": run_id, "mode": "RESEARCH_ONLY", "paper_admission": False,
                   "status": "timed_out", "cleanup_confirmed": False}
        for value in (False, None, "true"):
            receipt["cleanup_confirmed"] = value
            (directory / "receipt.json").write_text(json.dumps(receipt))
            self.assertIsNone(execution_is_alive(self.store, run_id))
            with self.assertRaises(PortfolioExecutionBusy):
                _assert_no_unreaped_runs(self.store)
        receipt["cleanup_confirmed"] = True
        (directory / "receipt.json").write_text(json.dumps(receipt))
        self.assertIs(execution_is_alive(self.store, run_id), False)
        del receipt["cleanup_confirmed"]
        (directory / "receipt.json").write_text(json.dumps(receipt))
        self.assertIs(execution_is_alive(self.store, run_id), False)
        (directory / ".control").mkdir()
        self.assertIsNone(execution_is_alive(self.store, run_id))
        (directory / ".control").rmdir()
        receipt["schema_version"] = 2
        (directory / "receipt.json").write_text(json.dumps(receipt))
        self.assertIsNone(execution_is_alive(self.store, run_id))
        snapshot = snapshot_sources(ROOT)
        receipt["code_version"] = snapshot["source_version"]
        (directory / "receipt.json").write_text(json.dumps(receipt))
        (directory / "source_snapshot.json").write_text(json.dumps(snapshot))
        self.assertIsNone(execution_is_alive(self.store, run_id))
        hashes = {path: entry["sha256"] for path, entry in snapshot["files"].items()}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with patch("research_center.portfolio_cli._LEGACY_SYNCHRONOUS_SOURCE_MAPS", {digest}):
            self.assertIs(execution_is_alive(self.store, run_id), False)
            receipt["code_version"] = "0" * 64
            (directory / "receipt.json").write_text(json.dumps(receipt))
            self.assertIsNone(execution_is_alive(self.store, run_id))
        receipt["schema_version"] = 3
        (directory / "receipt.json").write_text(json.dumps(receipt))
        self.assertIsNone(execution_is_alive(self.store, run_id))
        (directory / ".control").symlink_to(directory / "missing")
        self.assertIsNone(execution_is_alive(self.store, run_id))
        (directory / ".control").unlink()
        receipt["status"] = "running"
        (directory / "receipt.json").write_text(json.dumps(receipt))
        self.assertIsNone(execution_is_alive(self.store, run_id))

    def test_changed_bootstrap_with_same_run_is_unknown_not_dead(self):
        expected = ["/python", "-I", "-c", "new bootstrap", "project-hex", "store-hex", "a" * 32, "5.0"]
        old = expected.copy()
        old[3] = "older bootstrap"
        fake = type("Process", (), {"returncode": 0, "stdout": "123 123 S " + " ".join(old)})()
        with patch("research_center.portfolio_cli.subprocess.run", return_value=fake):
            self.assertIsNone(_process_identity(123, 123, expected))

    def test_timeout_kills_and_reaps_only_the_spawned_group_and_ignores_late_completion(self):
        late_marker = self.root / "late-completion"
        process_holder = []

        def spawn(root, run_id, timeout_seconds, token):
            process = _sleeping_process(2, on_finish=late_marker)
            process_holder.append(process)
            return process

        with patch("research_center.portfolio_cli._spawn_worker", side_effect=spawn):
            receipt = run_bounded(self.config, self.store, timeout_seconds=0.1)
        self.assertEqual(receipt["status"], "timed_out")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertIsNotNone(process_holder[0].poll())
        time.sleep(0.1)
        self.assertFalse(late_marker.exists())
        self.assertEqual(json.loads((self.store / receipt["run_id"] / "receipt.json").read_text())["status"],
                         "timed_out")
        self.assertIs(execution_is_alive(self.store, receipt["run_id"]), False)

    def test_cancel_marker_is_read_only_and_child_is_reaped(self):
        cancel_path = self.root / "cancel.marker"
        process_holder = []

        def spawn(root, run_id, timeout_seconds, token):
            process = _sleeping_process(2)
            process_holder.append(process)
            return process

        timer = threading.Timer(0.1, cancel_path.touch)
        timer.start()
        self.addCleanup(timer.cancel)
        with patch("research_center.portfolio_cli._spawn_worker", side_effect=spawn):
            receipt = run_bounded(self.config, self.store, timeout_seconds=5, cancel_path=cancel_path)
        self.assertEqual(receipt["status"], "cancelled")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertTrue(cancel_path.exists())
        self.assertIsNotNone(process_holder[0].poll())
        self.assertIs(execution_is_alive(self.store, receipt["run_id"]), False)

    def test_nonzero_worker_exit_is_terminal_failed_without_partial_success(self):
        process_holder = []

        def spawn(root, run_id, timeout_seconds, token):
            process = subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(7)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True,
            )
            process_holder.append(process)
            return process

        with patch("research_center.portfolio_cli._spawn_worker", side_effect=spawn):
            receipt = run_bounded(self.config, self.store, timeout_seconds=3)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertIsNotNone(process_holder[0].poll())
        directory = self.store / receipt["run_id"]
        self.assertFalse((directory / "result.json").exists())
        self.assertNotIn("artifact_sha256", receipt)

    def test_source_drift_after_child_run_prevents_publication(self):
        with patch("research_center.portfolio_cli._verify_source_snapshot",
                   side_effect=[None, RuntimeError("synthetic source drift")]) as verify:
            receipt = run_bounded(self.config, self.store, timeout_seconds=10)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertNotIn("artifact_sha256", receipt)
        directory = self.store / receipt["run_id"]
        self.assertFalse((directory / "result.json").exists())

    def test_unconfirmed_cleanup_keeps_private_pid_and_blocks_another_execution(self):
        process_holder = []

        def pause_after_bootstrap(root, run_id, timeout_seconds, token):
            process = _spawn_worker(root, run_id, timeout_seconds, token)
            process_holder.append(process)
            started_path = root / run_id / ".control" / "started"
            limit = time.monotonic() + 3
            while not started_path.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            if started_path.exists():
                os.killpg(process.pid, signal.SIGSTOP)
            return process

        with patch("research_center.portfolio_cli._spawn_worker", side_effect=pause_after_bootstrap), \
                patch("research_center.portfolio_cli._terminate_owned_group", return_value=False):
            receipt = run_bounded(self.config, self.store, timeout_seconds=0.1)
        process = process_holder[0]
        try:
            self.assertEqual(receipt["status"], "timed_out")
            self.assertFalse(receipt["cleanup_confirmed"])
            self.assertIs(execution_is_alive(self.store, receipt["run_id"]), True)
            with self.assertRaises(PortfolioExecutionBusy):
                run_bounded(self.config, self.store, timeout_seconds=1)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        self.assertIs(execution_is_alive(self.store, receipt["run_id"]), False)

    def test_execution_is_alive_checks_exact_bootstrap_and_copied_store_cannot_claim_it(self):
        entered = threading.Event()
        process_holder = []
        result_holder = []
        error_holder = []
        cancel_path = self.root / "alive-cancel.marker"

        def pause_after_bootstrap(root, run_id, timeout_seconds, token):
            process = _spawn_worker(root, run_id, timeout_seconds, token)
            process_holder.append(process)
            started_path = root / run_id / ".control" / "started"
            limit = time.monotonic() + 3
            while not started_path.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            if started_path.exists():
                os.killpg(process.pid, signal.SIGSTOP)
            entered.set()
            return process

        def execute():
            try:
                result_holder.append(run_bounded(
                    self.config, self.store, timeout_seconds=5, cancel_path=cancel_path,
                ))
            except BaseException as error:  # surfaced to the owning test thread
                error_holder.append(error)

        with patch("research_center.portfolio_cli._spawn_worker", side_effect=pause_after_bootstrap):
            worker = threading.Thread(target=execute, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                run_id = next(name for name in os.listdir(self.store) if len(name) == 32)
                self.assertIs(execution_is_alive(self.store, run_id), True)

                copied = self.root / "copied-store"
                shutil.copytree(self.store, copied)
                self.assertIs(execution_is_alive(copied, run_id), False)
            finally:
                cancel_path.touch(exist_ok=True)
                worker.join(5)
                if worker.is_alive() and process_holder:
                    try:
                        os.killpg(process_holder[0].pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    try:
                        os.killpg(process_holder[0].pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    worker.join(3)
        self.assertFalse(error_holder, error_holder)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder[0]["status"], "cancelled")
        self.assertTrue(result_holder[0]["cleanup_confirmed"])
        self.assertIsNotNone(process_holder[0].poll())


if __name__ == "__main__":
    unittest.main()
