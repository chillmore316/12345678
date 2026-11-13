from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research_center.engine import validate_config
from research_center.registry import strategy_version
from research_center.store import RunStore
import research_center.replay_accounts as replay_accounts


def synthetic_bars(count: int = 4) -> list[dict]:
    times = ["09:31", "09:32", "09:33", "09:34"][:count]
    rows = []
    for index, time in enumerate(times):
        start_minutes = int(time[:2]) * 60 + int(time[3:]) - 1
        bar_start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        rows.append({
            "datetime": f"2024-01-04T{bar_start}:00",
            "bar_start_datetime": f"2024-01-04T{bar_start}:00",
            "source_datetime": f"2024-01-04T{time}:00",
            "symbol": "000001.SZSE",
            "open": 10.0 + index,
            "high": 10.2 + index,
            "low": 9.8 + index,
            "close": 10.0 + index,
            "volume": 10000.0,
            "turnover": 1000000.0,
        })
    return rows


class FakeKernel:
    """Deterministic JSON-only test double; never opens source data."""

    __file__ = str(Path(replay_accounts.PROJECT_ROOT) / "research_center" / "replay_kernel.py")

    def __init__(self):
        self.fail_result = False
        self.advance_calls = 0

    @staticmethod
    def initial_state(config):
        initial = float(config["initial_cash"])
        return {
            "rows_processed": 0,
            "finalized": False,
            "cash": initial,
            "bars": [],
            "equity": [{
                "datetime": f"{config['start']} 00:00:00",
                "source_datetime": None,
                "cash": initial,
                "quantity": 0,
                "equity": initial,
            }],
            "orders": [],
            "trades": [],
            "positions": [],
        }

    def advance(self, config, state, bars, *, final=False):
        self.advance_calls += 1
        next_state = copy.deepcopy(state)
        next_state["bars"].extend(copy.deepcopy(bars))
        next_state["rows_processed"] += len(bars)
        next_state["finalized"] = final
        for bar in bars:
            next_state["equity"].append({
                "datetime": bar["source_datetime"],
                "source_datetime": bar["source_datetime"],
                "cash": next_state["cash"],
                "quantity": 0,
                "equity": next_state["cash"],
            })
        return next_state

    def result_from_state(self, config, state, manifest):
        if self.fail_result:
            raise RuntimeError("synthetic result failure")
        return {
            "metrics": {
                "initial_cash": config["initial_cash"],
                "ending_equity": state["equity"][-1]["equity"],
                "total_trades": len(state["trades"]),
                "negative_cash": False,
            },
            "equity": copy.deepcopy(state["equity"]),
            "orders": copy.deepcopy(state["orders"]),
            "trades": copy.deepcopy(state["trades"]),
            "positions": copy.deepcopy(state["positions"]),
            "bars": copy.deepcopy(state["bars"]),
            "data_manifest": copy.deepcopy(manifest),
            "limitations": ["RESEARCH_ONLY"],
        }


class ReplayAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temporary.name) / "runs")
        self.accounts = replay_accounts.ReplayAccounts(self.store)
        self.kernel = FakeKernel()
        self.kernel_patch = patch.object(replay_accounts, "_kernel_api", return_value=self.kernel)
        self.kernel_patch.start()
        self.addCleanup(self.kernel_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.run_id = self.make_source_run()

    def make_source_run(self, *, backend="reference", status="completed", version=None,
                        config_overrides=None):
        current = strategy_version("buy_hold")
        configured_version = current if version is None else version
        config = validate_config({
            "symbol": "000001.SZSE",
            "start": "2024-01-04",
            "end": "2024-01-04",
            "strategy": "buy_hold",
            "initial_cash": 25000,
            "commission_rate": 0,
            "min_commission": 0,
            "slippage_bps": 0,
            "data_root": str(Path(self.temporary.name) / "synthetic-only"),
        })
        config.update(backend=backend, strategy_version=configured_version)
        config.update(config_overrides or {})
        record = self.store.create(config)
        self.store.update(record["run_id"], status=status, backend=backend,
                          strategy_version=configured_version)
        source_result = {
            "backend": backend,
            "strategy_version": configured_version,
            "metrics": {"initial_cash": 25000, "ending_equity": 26000},
            "bars": synthetic_bars(),
            "orders": [{"order_id": "SOURCE_ONLY"}],
            "trades": [{"order_id": "SOURCE_ONLY"}],
            "equity": [{"equity": 26000}],
            "positions": [],
            "data_manifest": {
                "read_only": True,
                "used_source_files": [{"path": "synthetic.parquet", "sha256": "a" * 64}],
                "source_price_adjustment": "unresolved",
            },
            "data_readiness": {
                "status": "mismatch",
                "paper_admission": False,
                "checks": [{"id": "minute_price_conflict", "status": "mismatch",
                            "message": "synthetic price conflict"}],
            },
        }
        self.store.save_result(record["run_id"], source_result)
        provenance = {
            "backend": backend,
            "strategy_version": configured_version,
            "engine": "bounded-minute-research-v1",
            "source_hashes": {"research_center/engine.py": "e" * 64},
        }
        (self.store.directory(record["run_id"]) / "provenance.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )
        return record["run_id"]

    def new_account(self):
        return self.accounts.create(self.run_id, label="synthetic replay")

    def test_create_is_paused_freezes_hashes_and_preserves_readiness_without_future_bars(self):
        created = self.new_account()
        self.assertEqual(created["record"]["status"], "paused")
        self.assertEqual(created["record"]["cursor"], 0)
        self.assertEqual(created["bars"], [])
        self.assertEqual(created["result"]["orders"], [])
        self.assertEqual(created["record"]["mode"], "RESEARCH_ONLY")
        self.assertFalse(created["record"]["paper_admission"])
        self.assertEqual(created["data_readiness"]["checks"][0]["status"], "mismatch")
        self.assertFalse(created["data_readiness"]["paper_admission"])
        self.assertEqual(created["source"]["dataset_hashes"][0]["sha256"], "a" * 64)
        self.assertNotIn("frozen_bars", created)
        with self.assertRaises(replay_accounts.ReplayAccountError):
            self.accounts.step(created["record"]["account_id"], 1, 0, "paused-step")

    def test_duplicate_step_is_exactly_once_and_payload_changes_are_rejected(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        first = self.accounts.step(account_id, 2, 1, "request-a")
        self.assertEqual(first["record"]["cursor"], 2)
        self.assertEqual(len(first["bars"]), 2)
        with sqlite3.connect(self.accounts.database) as connection:
            stored_ack = connection.execute(
                "SELECT response_json FROM requests WHERE account_id=? AND request_id=?",
                (account_id, "request-a"),
            ).fetchone()[0]
        ack = json.loads(stored_ack)
        self.assertLess(len(stored_ack), 500)
        self.assertNotIn("bars", ack)
        self.assertNotIn("events", ack)
        repeated = self.accounts.step(account_id, 2, 1, "request-a")
        self.assertTrue(repeated["replayed"])
        self.assertEqual(repeated["record"]["cursor"], 2)
        self.assertEqual(self.kernel.advance_calls, 1)
        self.assertEqual(len(self.accounts.events(account_id)), len(first["events"]))
        second = self.accounts.step(account_id, 1, 2, "request-b")
        self.assertEqual(second["record"]["cursor"], 3)
        repeated_after_later_step = self.accounts.step(account_id, 2, 1, "request-a")
        self.assertTrue(repeated_after_later_step["replayed"])
        self.assertEqual(repeated_after_later_step["record"]["cursor"], 3)
        self.assertEqual(len(repeated_after_later_step["bars"]), 3)
        self.assertEqual(repeated_after_later_step["original_revision"], 1)
        self.assertEqual(repeated_after_later_step["original_cursor"], 0)
        self.assertEqual(repeated_after_later_step["applied_revision"], 2)
        self.assertEqual(repeated_after_later_step["applied_cursor"], 2)
        self.assertEqual(self.kernel.advance_calls, 2)
        self.assertEqual(len(repeated_after_later_step["events"]), len(second["events"]))
        with self.assertRaises(replay_accounts.ReplayAccountError):
            self.accounts.step(account_id, 1, 1, "request-a")
        self.assertEqual(self.accounts.detail(account_id)["record"]["cursor"], 3)

    def test_stale_revision_and_invalid_steps_do_not_advance(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        with self.assertRaises(replay_accounts.RevisionConflict):
            self.accounts.step(account_id, 1, 0, "stale")
        for value in (0, 241, True, 1.0, "1"):
            with self.subTest(steps=value), self.assertRaises(replay_accounts.ReplayAccountError):
                self.accounts.step(account_id, value, 1, "invalid-" + str(value))
        self.assertEqual(self.accounts.detail(account_id)["record"]["cursor"], 0)
        self.assertEqual(self.kernel.advance_calls, 0)

    def test_pause_restart_resume_preserves_state_and_paused_accounts_cannot_step(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        first = self.accounts.step(account_id, 1, 1, "one")
        reopened = replay_accounts.ReplayAccounts(RunStore(Path(self.temporary.name) / "runs"))
        restored = reopened.detail(account_id)
        self.assertEqual(restored["record"]["cursor"], 1)
        self.assertEqual(restored["bars"], first["bars"])
        paused = reopened.pause(account_id, restored["record"]["revision"])
        self.assertEqual(paused["record"]["status"], "paused")
        with self.assertRaises(replay_accounts.ReplayAccountError):
            reopened.step(account_id, 1, paused["record"]["revision"], "blocked")
        resumed = reopened.resume(account_id, paused["record"]["revision"])
        self.assertEqual(resumed["record"]["status"], "running")
        self.assertEqual(resumed["record"]["cursor"], 1)

    def test_final_step_completes_atomically_and_future_steps_are_refused(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        completed = self.accounts.step(account_id, 240, 1, "final")
        self.assertEqual(completed["record"]["status"], "completed")
        self.assertTrue(completed["record"]["finalized"])
        self.assertEqual(completed["record"]["cursor"], 4)
        self.assertEqual(len(completed["bars"]), 4)
        self.assertEqual([item["event_type"] for item in completed["events"]][-2:], ["step", "complete"])
        replayed = self.accounts.step(account_id, 240, 1, "final")
        self.assertTrue(replayed["replayed"])
        with self.assertRaises(replay_accounts.ReplayAccountError):
            self.accounts.step(account_id, 1, completed["record"]["revision"], "after-end")

    def test_failure_rolls_back_account_and_request_then_can_retry(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        before = self.accounts.detail(account_id)
        self.kernel.fail_result = True
        with self.assertRaisesRegex(RuntimeError, "synthetic result failure"):
            self.accounts.step(account_id, 1, 1, "retryable")
        failed = self.accounts.detail(account_id)
        self.assertEqual(failed["record"]["cursor"], before["record"]["cursor"])
        self.assertEqual(failed["record"]["revision"], before["record"]["revision"])
        self.assertEqual(failed["bars"], before["bars"])
        self.assertEqual(failed["events"][-1]["event_type"], "error")
        self.kernel.fail_result = False
        retried = self.accounts.step(account_id, 1, 1, "retryable")
        self.assertEqual(retried["record"]["cursor"], 1)

    def test_source_changes_after_create_do_not_change_frozen_replay_input(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        result_path = self.store.directory(self.run_id) / "result.json"
        result_path.write_text("{}", encoding="utf-8")
        provenance_path = self.store.directory(self.run_id) / "provenance.json"
        provenance_path.write_text("{}", encoding="utf-8")
        self.accounts.resume(account_id, 0)
        stepped = self.accounts.step(account_id, 1, 1, "frozen")
        self.assertEqual(stepped["bars"], synthetic_bars()[:1])
        self.assertEqual(stepped["record"]["source_hashes"], created["record"]["source_hashes"])
        self.assertNotEqual(stepped["bars"], synthetic_bars())

    def test_rejects_invalid_ids_incomplete_native_and_old_version_sources(self):
        for bad_id in ("../bad", "A" * 32, "not-a-uuid"):
            with self.subTest(run_id=bad_id), self.assertRaises(replay_accounts.ReplayAccountError):
                self.accounts.create(bad_id)
        incomplete = self.make_source_run(status="running")
        with self.assertRaisesRegex(replay_accounts.ReplayAccountError, "completed"):
            self.accounts.create(incomplete)
        native = self.make_source_run(backend="vnpy")
        with self.assertRaises(replay_accounts.ReplayAccountError):
            self.accounts.create(native)
        old_version = self.make_source_run(version="core-old-version")
        with self.assertRaisesRegex(replay_accounts.ReplayAccountError, "strategy version"):
            self.accounts.create(old_version)
        missing_version = self.make_source_run(version="")
        with self.assertRaisesRegex(replay_accounts.ReplayAccountError, "no frozen strategy version"):
            self.accounts.create(missing_version)

    def test_config_bounds_and_root_guards_fail_closed(self):
        outside_period = self.make_source_run(config_overrides={"start": "2026-01-01", "end": "2026-01-01"})
        with self.assertRaisesRegex(ValueError, "2024-2025"):
            self.accounts.create(outside_period)
        too_long = self.make_source_run(config_overrides={"start": "2024-01-01", "end": "2024-02-01"})
        with self.assertRaisesRegex(ValueError, "31"):
            self.accounts.create(too_long)
        star_board = self.make_source_run(config_overrides={"symbol": "688981.SSE"})
        with self.assertRaisesRegex(ValueError, "主板"):
            self.accounts.create(star_board)
        with self.assertRaisesRegex(ValueError, "data root"):
            replay_accounts.ReplayAccounts._validate_root(replay_accounts.PROJECT_ROOT / "data" / "replays")
        with self.assertRaisesRegex(ValueError, "QuantLivePaper"):
            replay_accounts.ReplayAccounts._validate_root(Path("/tmp/QuantLivePaper/replay_accounts"))

    def test_real_kernel_recomputes_orders_from_initial_cash(self):
        from research_center import replay_kernel

        with patch.object(replay_accounts, "_kernel_api", return_value=replay_kernel):
            created = self.accounts.create(self.run_id)
            account_id = created["record"]["account_id"]
            self.assertEqual(created["equity"][0]["equity"], 25000)
            self.assertEqual(created["orders"], [])
            self.assertFalse(created["record"]["paper_admission"])
            self.accounts.resume(account_id, created["record"]["revision"])
            first = self.accounts.step(account_id, 1, 1, "kernel-first")
            self.assertEqual(first["record"]["cursor"], 1)
            self.assertEqual(len(first["bars"]), 1)
            self.assertEqual(first["orders"][0]["order_id"], "R000001")
            self.assertNotEqual(first["orders"][0]["order_id"], "SOURCE_ONLY")
            self.assertEqual(first["record"]["status"], "running")

    def test_exports_are_processed_rows_only(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        self.accounts.step(account_id, 1, 1, "export")
        self.assertIn(b"source_datetime", self.accounts.csv_bytes(account_id, "equity"))
        self.assertIn(b"event_type", self.accounts.csv_bytes(account_id, "events"))
        self.assertNotIn(b"09:32:00", self.accounts.csv_bytes(account_id, "equity"))
        with self.assertRaises(replay_accounts.ReplayAccountError):
            self.accounts.csv_bytes(account_id, "bars")

    def test_code_drift_blocks_continuation_but_old_account_stays_readable(self):
        created = self.new_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        with patch.object(replay_accounts, "_code_identity", return_value={"research_center/engine.py": "changed"}):
            with self.assertRaises(replay_accounts.SourceCodeChanged):
                self.accounts.step(account_id, 1, 1, "source-drift")
            readable = self.accounts.detail(account_id)
            self.assertEqual(readable["record"]["cursor"], 0)
            self.assertEqual(readable["bars"], [])
            self.assertIn(b"event_type", self.accounts.csv_bytes(account_id, "events"))

    def test_invalid_source_bar_window_session_and_prices_rejected_at_creation(self):
        source=self.store.directory(self.run_id)/'result.json'
        original=json.loads(source.read_text())
        for field,value in (('source_datetime','2026-01-04T09:31:00'),('open',0)):
            invalid=copy.deepcopy(original)
            invalid['bars'][0][field]=value
            if field=='source_datetime':
                invalid['bars'][0]['datetime']='2026-01-04T09:30:00'
                invalid['bars'][0]['bar_start_datetime']='2026-01-04T09:30:00'
            source.write_text(json.dumps(invalid))
            with self.assertRaises(replay_accounts.ReplayAccountError):
                self.accounts.create(self.run_id)
        source.write_text(json.dumps(original))

    def test_current_fees_do_not_copy_future_source_run_fees(self):
        from research_center import replay_kernel
        path=self.store.directory(self.run_id)/'result.json'
        source=json.loads(path.read_text());source['data_manifest']['total_fees_paid']=99999
        path.write_text(json.dumps(source))
        with patch.object(replay_accounts,'_kernel_api',return_value=replay_kernel):
            created=self.accounts.create(self.run_id)
            self.assertEqual(created['result']['data_manifest']['total_fees_paid'],0)
            self.assertTrue(created['record']['code_current'])
            with patch.object(replay_accounts,'_code_digest',return_value='changed'):
                frozen=self.accounts.detail(created['record']['account_id'])
                self.assertFalse(frozen['record']['continuation_allowed'])

    def test_process_killed_between_account_update_and_commit_recovers_exactly_once(self):
        import subprocess,sys
        from research_center import replay_kernel
        with patch.object(replay_accounts,'_kernel_api',return_value=replay_kernel):
            created=self.accounts.create(self.run_id);aid=created['record']['account_id']
            self.accounts.resume(aid,0)
            code="""
import os,sys
from research_center.replay_accounts import ReplayAccounts
from research_center.store import RunStore
s=ReplayAccounts(RunStore(sys.argv[1])); original=s._insert_event
def killed(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
s._insert_event=killed
s.step(sys.argv[2],2,1,'kill-before-commit')
"""
            child=subprocess.run([sys.executable,'-c',code,str(self.store.root),aid],capture_output=True,text=True,timeout=20)
            self.assertEqual(child.returncode,23,child.stderr)
            restored=replay_accounts.ReplayAccounts(self.store);d=restored.detail(aid)
            self.assertEqual(d['record']['cursor'],0);self.assertEqual(d['record']['revision'],1)
            self.assertEqual(d['trades'],[])
            self.assertFalse(any(event['event_type']=='step' for event in d['events']))
            d=restored.step(aid,2,1,'kill-before-commit');self.assertEqual(d['record']['cursor'],2)
            self.assertEqual(len(d['trades']),1)
            duplicate=restored.step(aid,2,1,'kill-before-commit')
            self.assertTrue(duplicate['replayed']);self.assertEqual(len(duplicate['trades']),1)


if __name__ == "__main__":
    unittest.main()
