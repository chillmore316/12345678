from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from research_center import portfolio_kernel as kernel
from research_center import portfolio_replay_accounts as replay_accounts
from research_center.portfolio_archive import archive_root
from research_center.portfolio_provenance import snapshot_sources
from research_center.store import RunStore


SYMBOLS = ["000001.SZSE", "000002.SZSE"]
SOURCE_RUN_ID = "a" * 32
SENTINEL_DATA_ROOT = "/private/synthetic-minute-lake-do-not-return"


def _config(*, end="2024-01-02"):
    return kernel.normalize_portfolio({
        "symbols": list(SYMBOLS),
        "start": "2024-01-02",
        "end": end,
        "interval": "1m",
        "strategy": "buy_hold",
        "initial_cash": 10_000.0,
        "commission_rate": 0.001,
        "min_commission": 1.0,
        "slippage_bps": 0.0,
        "data_root": SENTINEL_DATA_ROOT,
        "params": {
            "target_fraction": 1.0,
            "max_turnover_participation": 0.1,
            "fast": 1,
            "slow": 2,
        },
    })


def _frames(count=6):
    result = []
    for index in range(count):
        source_time = pd.Timestamp("2024-01-02 09:31") + pd.Timedelta(minutes=index)
        rows = {}
        for symbol_index, symbol in enumerate(SYMBOLS):
            price = 10.0 + (symbol_index * 2) + index * 0.1
            rows[symbol] = {
                "symbol": symbol,
                "datetime": (source_time - pd.Timedelta(minutes=1)).isoformat(),
                "bar_start_datetime": (source_time - pd.Timedelta(minutes=1)).isoformat(),
                "source_datetime": source_time.isoformat(),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 1_000_000.0,
                "turnover": 10_000_000.0,
            }
        result.append(rows)
    return result


def _write_json(path: Path, value) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


def make_synthetic_completed_portfolio(root: Path, *, run_id: str = SOURCE_RUN_ID,
                                       end: str = "2024-01-02", count: int = 6):
    """Create a hash-verified synthetic source archive for local tests only."""
    root = Path(root)
    store = RunStore(root / "runs")
    config = _config(end=end)
    frames = _frames(count)
    state = kernel.advance(config, kernel.initial_state(config), frames, final=True)
    result = kernel.result_from_state(config, state)
    readiness = {
        "schema_version": 1,
        "status": "mismatch",
        "checks": [{"name": "synthetic_test_only", "status": "mismatch"}],
        "paper_admission": False,
    }
    manifest = {
        "schema_version": 1,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "symbols": list(SYMBOLS),
        "start": config["start"],
        "end": config["end"],
        "frames": len(frames),
        "alignment": "exact_bar_start_and_source_datetime",
        "sources": {
            symbol: {"source_id": f"synthetic-{index}", "sha256": hashlib.sha256(symbol.encode()).hexdigest()}
            for index, symbol in enumerate(SYMBOLS)
        },
    }
    snapshot = snapshot_sources(replay_accounts.PROJECT_ROOT)
    result["data_readiness"] = readiness
    result["provenance"] = {
        "source_version": snapshot["source_version"],
        "runtime": snapshot["runtime"],
        "source_snapshot": "source_snapshot.json",
        "scope": "current_sources_match_archived_snapshot_before_and_after_execution",
    }
    directory = archive_root(store) / run_id
    directory.mkdir(parents=True)
    artifacts = {
        "config.json": config,
        "data_manifest.json": manifest,
        "state.json": state,
        "result.json": result,
        "source_snapshot.json": snapshot,
    }
    hashes = {}
    for name, value in artifacts.items():
        raw = _write_json(directory / name, value)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    receipt = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "completed",
        "created_at": "2024-01-02T00:00:00+00:00",
        "finished_at": "2024-01-02T00:01:00+00:00",
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "frames": len(frames),
        "code_version": snapshot["source_version"],
        "artifact_sha256": hashes,
    }
    _write_json(directory / "receipt.json", receipt)
    return store, run_id, config, frames, state, result


class PortfolioReplayAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store, self.run_id, self.config, self.frames, self.full_state, self.full_result = (
            make_synthetic_completed_portfolio(self.root)
        )
        self.full_kernel_result = kernel.result_from_state(self.config, self.full_state)
        self.accounts = replay_accounts.PortfolioReplayAccounts(self.store)

    def _create(self, request_id="create-one"):
        return self.accounts.create(self.run_id, "synthetic", request_id=request_id)

    def test_create_is_idempotent_isolated_and_has_no_fake_partial_metrics(self):
        detail = self._create()
        self.assertEqual(detail["record"]["status"], "paused")
        self.assertEqual(detail["record"]["revision"], 0)
        self.assertEqual(detail["record"]["cursor"], 0)
        self.assertEqual(detail["record"]["total_frames"], len(self.frames))
        self.assertEqual(detail["record"]["creation_request_id"], "create-one")
        self.assertFalse(detail["record"]["paper_admission"])
        self.assertNotIn("metrics", detail["result"])
        self.assertNotIn("bars", detail["result"])
        self.assertEqual(detail["result"]["current"]["equity"], self.config["initial_cash"])
        self.assertNotIn("data_root", detail["config"])
        self.assertNotIn(SENTINEL_DATA_ROOT, json.dumps(detail, ensure_ascii=False))
        self.assertEqual(self.accounts.root, self.store.root.parent / (self.store.root.name + "_portfolio_replays"))
        self.assertNotEqual(self.accounts.root, self.store.root)

        retry = self._create()
        self.assertEqual(retry["record"]["account_id"], detail["record"]["account_id"])
        self.assertTrue(retry["creation_replayed"])
        with self.assertRaises(replay_accounts._CreateRequestConflict):
            self.accounts.create(self.run_id, "different label", request_id="create-one")
        self.assertEqual(len(self.accounts.list_accounts()), 1)

    def test_manual_resume_steps_pause_restart_retry_and_final_kernel_equivalence(self):
        created = self._create()
        account_id = created["record"]["account_id"]
        with self.assertRaises(replay_accounts.PortfolioReplayAccountError):
            self.accounts.step(account_id, 1, 0, "step-before-start")

        started = self.accounts.start(account_id, 0)
        first = self.accounts.step(account_id, 2, started["record"]["revision"], "first-batch")
        self.assertEqual(first["record"]["cursor"], 2)
        self.assertEqual(first["record"]["status"], "running")
        self.assertEqual(len(first["equity"]), 3)
        self.assertNotIn("metrics", first["result"])
        self.assertNotIn("bars", first["result"])

        # Advance further, then retry the first request with its original
        # revision. It must return the stable first acknowledgement and leave
        # the later account state intact.
        second = self.accounts.step(account_id, 2, first["record"]["revision"], "second-batch")
        retry = self.accounts.step(account_id, 2, started["record"]["revision"], "first-batch")
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["applied_cursor"], 2)
        self.assertEqual(retry["applied_revision"], first["record"]["revision"])
        self.assertEqual(retry["record"]["cursor"], second["record"]["cursor"])
        with self.assertRaises(replay_accounts.PortfolioReplayAccountError):
            self.accounts.step(account_id, 3, started["record"]["revision"], "first-batch")
        with self.assertRaises(replay_accounts.PortfolioReplayRevisionConflict) as caught:
            self.accounts.step(account_id, 1, 1, "stale-new-request")
        self.assertTrue(caught.exception.rollback_confirmed)
        after_conflict = self.accounts.detail(account_id)
        self.assertEqual(after_conflict["record"]["revision"], second["record"]["revision"])
        self.assertEqual(after_conflict["record"]["cursor"], 4)

        paused = self.accounts.pause(account_id, second["record"]["revision"])
        resumed = self.accounts.resume(account_id, paused["record"]["revision"])
        final = self.accounts.step(account_id, 240, resumed["record"]["revision"], "final-batch")
        self.assertEqual(final["record"]["status"], "completed")
        self.assertEqual(final["record"]["remaining_frames"], 0)
        self.assertTrue(final["record"]["finalized"])
        self.assertEqual(final["result"], self.full_kernel_result)
        self.assertEqual(final["result"]["equity"], self.full_kernel_result["equity"])
        self.assertEqual(final["result"]["orders"], self.full_kernel_result["orders"])
        self.assertEqual(final["result"]["trades"], self.full_kernel_result["trades"])
        self.assertEqual(final["result"]["positions"], self.full_kernel_result["positions"])
        self.assertAlmostEqual(final["result"]["metrics"]["fees_paid"], self.full_state["fee_total"])
        self.assertFalse(final["result"]["paper_admission"])
        self.assertFalse(final["data_readiness"]["paper_admission"])

    def test_json_state_restores_after_restart_mid_replay(self):
        detail = self._create()
        account_id = detail["record"]["account_id"]
        started = self.accounts.start(account_id, 0)
        partial = self.accounts.step(account_id, 3, started["record"]["revision"], "restart-partial")
        reopened_store = RunStore(self.store.root)
        reopened = replay_accounts.PortfolioReplayAccounts(reopened_store)
        restored = reopened.detail(account_id)
        self.assertEqual(restored["record"]["cursor"], 3)
        self.assertEqual(restored["equity"], partial["equity"])
        self.assertEqual(restored["orders"], partial["orders"])
        final = reopened.step(account_id, 3, partial["record"]["revision"], "restart-final")
        self.assertEqual(final["result"], self.full_kernel_result)

    def test_frozen_frame_tampering_rolls_back_cursor_and_revision(self):
        detail = self._create()
        account_id = detail["record"]["account_id"]
        started = self.accounts.start(account_id, 0)
        with self.accounts._transaction() as connection:
            row = connection.execute("SELECT frozen_bars_json FROM accounts WHERE account_id=?",
                                     (account_id,)).fetchone()
            tampered = json.loads(row["frozen_bars_json"])
            tampered[-1][SYMBOLS[0]]["close"] += 1.0
            connection.execute("UPDATE accounts SET frozen_bars_json=? WHERE account_id=?",
                               (json.dumps(tampered), account_id))
        before = self.accounts.detail(account_id)["record"]
        with self.assertRaisesRegex(replay_accounts.PortfolioReplayAccountError, "分钟帧哈希"):
            self.accounts.step(account_id, 2, started["record"]["revision"], "tampered-frames")
        after = self.accounts.detail(account_id)["record"]
        self.assertEqual((after["revision"], after["cursor"]), (before["revision"], before["cursor"]))

    def test_failed_kernel_call_rolls_back_account_state(self):
        detail = self._create()
        account_id = detail["record"]["account_id"]
        started = self.accounts.start(account_id, 0)
        before = self.accounts.detail(account_id)["record"]
        with patch.object(kernel, "advance", side_effect=RuntimeError("synthetic kernel failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic kernel failure"):
                self.accounts.step(account_id, 1, started["record"]["revision"], "failing-step")
        after = self.accounts.detail(account_id)["record"]
        self.assertEqual((after["revision"], after["cursor"]), (before["revision"], before["cursor"]))

    def test_source_drift_keeps_history_readable_and_blocks_continuation(self):
        detail = self._create()
        account_id = detail["record"]["account_id"]
        with patch.object(replay_accounts, "_source_version", return_value="f" * 64):
            readable = self.accounts.detail(account_id)
            self.assertFalse(readable["record"]["code_current"])
            self.assertFalse(readable["record"]["continuation_allowed"])
            with self.assertRaises(replay_accounts.PortfolioReplaySourceChanged):
                self.accounts.start(account_id, 0)
        self.assertEqual(self.accounts.detail(account_id)["record"]["status"], "paused")

    def test_archive_tampering_and_oversized_date_scope_are_rejected(self):
        result_path = archive_root(self.store) / self.run_id / "result.json"
        result_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.accounts.create(self.run_id, request_id="tampered-source")

        other_root = self.root / "wide"
        wide_store, wide_run, *_ = make_synthetic_completed_portfolio(
            other_root, run_id="b" * 32, count=6
        )
        wide_directory = archive_root(wide_store) / wide_run
        wide_config = json.loads((wide_directory / "config.json").read_text(encoding="utf-8"))
        wide_config["end"] = "2026-01-02"
        config_raw = _write_json(wide_directory / "config.json", wide_config)
        wide_receipt = json.loads((wide_directory / "receipt.json").read_text(encoding="utf-8"))
        wide_receipt["artifact_sha256"]["config.json"] = hashlib.sha256(config_raw).hexdigest()
        _write_json(wide_directory / "receipt.json", wide_receipt)
        wide_accounts = replay_accounts.PortfolioReplayAccounts(wide_store)
        with self.assertRaises(replay_accounts.PortfolioReplayAccountError):
            wide_accounts.create(wide_run, request_id="too-wide")

    def test_database_dangling_symlink_and_protected_roots_are_rejected(self):
        other_root = self.root / "db-link"
        store = RunStore(other_root / "runs")
        account_root = store.root.parent / (store.root.name + "_portfolio_replays")
        account_root.mkdir(parents=True)
        database = account_root / "accounts.sqlite3"
        database.symlink_to(account_root / "missing-target.sqlite3")
        with self.assertRaisesRegex(replay_accounts.PortfolioReplayAccountError, "non-symlink"):
            replay_accounts.PortfolioReplayAccounts(store)
        with self.assertRaisesRegex(replay_accounts.PortfolioReplayAccountError, "QuantLivePaper"):
            replay_accounts.PortfolioReplayAccounts._validate_root(
                Path(self.root) / "QuantLivePaper" / "replays"
            )

    def test_csv_exports_are_bounded_to_account_history(self):
        detail = self._create()
        account_id = detail["record"]["account_id"]
        started = self.accounts.start(account_id, 0)
        stepped = self.accounts.step(account_id, 2, started["record"]["revision"], "csv-step")
        csv_data = self.accounts.csv_bytes(account_id, "equity")
        self.assertTrue(csv_data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(csv_data, self.accounts.export_csv(account_id, "equity"))
        self.assertEqual(len(stepped["equity"]), 3)
        self.assertNotIn(SENTINEL_DATA_ROOT.encode(), csv_data)


if __name__ == "__main__":
    unittest.main()
