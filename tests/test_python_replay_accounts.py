from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from research_center import custom_engine, custom_kernel
from research_center.store import RunStore
from strategy.base import Order
import research_center.python_replay_accounts as accounts_module


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_json(value) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8"))


def _bars():
    points = [
        ("2024-01-02", "09:31", 10.0),
        ("2024-01-02", "09:32", 10.0),
        ("2024-01-03", "09:31", 11.0),
        ("2024-01-03", "09:32", 11.0),
    ]
    result = []
    for day, end, price in points:
        hour, minute = (int(part) for part in end.split(":"))
        start_minute = hour * 60 + minute - 1
        start = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
        stamp = f"{day}T{start}:00"
        result.append({
            "datetime": stamp,
            "bar_start_datetime": stamp,
            "source_datetime": f"{day}T{end}:00",
            "symbol": "000001.SZSE",
            "open": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "close": price,
            "volume": 10000.0,
            "turnover": price * 10000.0,
        })
    return result


class FakeSourceStore:
    def __init__(self, record, source):
        self._record = copy.deepcopy(record)
        self._source = source

    def show(self, source_id):
        if source_id != self._record["registration_id"]:
            raise FileNotFoundError("source not found")
        return copy.deepcopy(self._record)

    def read_source(self, source_id):
        if source_id != self._record["registration_id"]:
            raise FileNotFoundError("source not found")
        return self._source


class FakeStatefulAdapter:
    def __init__(self, owner, source, symbol, params, runtime, state=None,
                 history_limit=60, api_source=None):
        self.owner = owner
        self.symbol = symbol
        self._state = copy.deepcopy(state if state is not None else {"callbacks": 0})
        self.fail_callback = owner.fail_callback
        self.fail_close = owner.fail_close
        owner.instances += 1
        owner.received_sources.append(source)
        owner.received_api_sources.append(api_source)

    @property
    def state(self):
        return copy.deepcopy(self._state)

    def on_bar(self, bar, ctx):
        self._state["callbacks"] += 1
        position = ctx.positions.get(self.symbol, {})
        self.owner.available_to_sell.append(int(position.get("available_to_sell", 0)))
        if self.fail_callback:
            raise RuntimeError("fake callback failure")
        if self._state["callbacks"] == 1:
            return [Order(symbol=self.symbol, side="buy", quantity=100)]
        if self._state["callbacks"] == 3:
            return [Order(symbol=self.symbol, side="sell", quantity=100)]
        return []

    def close(self):
        self.owner.closes += 1
        if self.fail_close:
            raise RuntimeError("fake close failure")


class PythonReplayAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temporary.name) / "runs")
        self.accounts = accounts_module.PythonReplayAccounts(self.store)
        self.runtime_sentinel = object()
        self.fake_adapter = SimpleNamespace(
            instances=0, closes=0, fail_callback=False, fail_close=False,
            received_sources=[], received_api_sources=[], available_to_sell=[],
        )

        self.source = b"def build_strategy(symbol, params):\n    raise RuntimeError('guest-only')\n"
        self.api_source = b"# frozen strategy API fixture\n"
        self.source_id = _hash(self.source)
        risk_params = {"target_fraction": 0.5, "max_turnover_participation": 0.1}
        request_base = {
            "symbol": "000001.SZSE", "start": "2024-01-02", "end": "2024-01-03",
            "interval": "1m", "strategy": "custom_python", "initial_cash": 25000,
            "commission_rate": 0.0, "min_commission": 0.0, "slippage_bps": 0.0,
            "data_root": str(Path(self.temporary.name) / "synthetic-only"),
            "params": risk_params,
        }
        execution_config = custom_kernel.make_execution_config(request_base, history_limit=5)
        self.strategy_version = "a" * 64
        trusted_hashes = accounts_module._current_trusted_code_hashes()
        legacy_hashes = {
            name: trusted_hashes[key]
            for name, key in (("engine", "engine"), ("strategies", "strategies"),
                              ("replay_accounts", "replay_accounts"),
                              ("replay_kernel", "replay_kernel"),
                              ("strategy_base", "strategy_base"))
        }
        runtime_pins = {
            **custom_engine._RUNTIME_PINS,
            "lima_home": str(custom_engine._RUNTIME_HOME),
            "instance": custom_engine._RUNTIME_INSTANCE,
        }
        generic_driver_hash = _hash(custom_engine.TRUSTED_DRIVER_SOURCE)
        self.component_hashes = {
            "api_source_sha256": _hash(self.api_source),
            "sdk_source_sha256": _hash(self.api_source),
            "generic_driver_sha256": generic_driver_hash,
            "trusted_code_sha256": trusted_hashes,
            "legacy_source_sha256": legacy_hashes,
            "runtime_pins": runtime_pins,
        }
        self.identity = {
            "schema": "research-center-custom-python-bridge-v1",
            "strategy_version": self.strategy_version,
            "source_id": self.source_id,
            "source_sha256": _hash(self.source),
            "api_source_sha256": _hash(self.api_source),
            "sdk_source_sha256": _hash(self.api_source),
            "generic_driver_sha256": generic_driver_hash,
            "trusted_code_sha256": copy.deepcopy(trusted_hashes),
            "legacy_source_sha256": copy.deepcopy(legacy_hashes),
            "runtime_pins": copy.deepcopy(runtime_pins),
        }
        self.normalized = {
            "strategy": "custom_python", "backend": "isolated_python",
            "source_id": self.source_id, "name": "synthetic strategy",
            "description": "synthetic fixture", "source_sha256": _hash(self.source),
            "code_version": _hash(self.source), "interface_contract": "strategy-base-json-state-v1",
            "source_metadata": {
                "registration_id": self.source_id, "name": "synthetic strategy",
                "description": "synthetic fixture",
                "interface_contract": "strategy-base-json-state-v1",
                "source_sha256": _hash(self.source), "code_version": _hash(self.source),
            },
            "execution_config": copy.deepcopy(execution_config),
            "custom_params": {"counter_fixture": True}, "history_limit": 5,
            **{name: execution_config[name] for name in (
                "symbol", "start", "end", "interval", "initial_cash", "commission_rate",
                "min_commission", "slippage_bps", "data_root",
            )},
            "params": risk_params,
            "strategy_version": self.strategy_version,
        }
        self.prepared = SimpleNamespace(
            normalized=copy.deepcopy(self.normalized), execution_config=copy.deepcopy(execution_config),
            record={
                "schema_version": 1, "registration_id": self.source_id,
                "created_at": "2026-11-03T00:00:00Z",
                "interface_contract": "strategy-base-json-state-v1",
                "name": "synthetic strategy", "description": "synthetic fixture",
                "source_sha256": _hash(self.source), "code_version": _hash(self.source),
                "declared_import_roots": [],
                "import_declaration_status": "DECLARED_NOT_VERIFIED",
                "runtime_interface_verified": False, "dependencies_verified": False,
                "mode": "RESEARCH_ONLY", "paper_admission": False,
                "default_params": {},
            },
            source=self.source, api_source=self.api_source,
            component_hashes=copy.deepcopy(self.component_hashes),
            identity=copy.deepcopy(self.identity), strategy_version=self.strategy_version,
        )
        self.source_record = copy.deepcopy(self.prepared.record)
        self.source_store = FakeSourceStore(self.source_record, self.source)

        source_config = copy.deepcopy(self.normalized)
        source_config.update(label="source run", experiment_id="fixture", parent_run_id=None)
        self.source_run = self.store.create(source_config)
        self.store.update(self.source_run["run_id"], status="completed", progress=100,
                          backend="isolated_python", strategy_version=self.strategy_version,
                          mode="RESEARCH_ONLY", paper_admission=False)
        self.source_manifest = {
            "schema_version": self.identity["schema"],
            "mode": "RESEARCH_ONLY", "paper_admission": False,
            "strategy_version": self.strategy_version, "source_id": self.source_id,
            "source_sha256": _hash(self.source), "api_source_sha256": _hash(self.api_source),
            "sdk_source_sha256": _hash(self.api_source),
            "generic_driver_sha256": generic_driver_hash,
            "trusted_code_sha256": copy.deepcopy(trusted_hashes),
            "legacy_source_sha256": copy.deepcopy(legacy_hashes),
            "runtime_pins": copy.deepcopy(runtime_pins),
            "runtime_interface_verified": True,
            "registration": {
                key: copy.deepcopy(self.prepared.record[key])
                for key in (
                    "schema_version", "registration_id", "created_at", "interface_contract",
                    "name", "description", "code_version", "declared_import_roots",
                    "import_declaration_status", "runtime_interface_verified",
                    "dependencies_verified", "mode", "paper_admission",
                )
            },
        }
        self.data_manifest = {
            "read_only": True,
            "used_source_files": [{"path": "fixture.parquet", "sha256": "c" * 64}],
            "source_price_adjustment": "unresolved",
        }
        self.readiness = {
            "status": "mismatch", "paper_admission": False,
            "checks": [{"id": "fixture", "status": "mismatch"}],
        }
        self.source_result = {
            "strategy": "custom_python", "backend": "isolated_python",
            "strategy_version": self.strategy_version,
            "custom_config": {key: copy.deepcopy(value) for key, value in self.normalized.items()
                               if key != "execution_config"},
            "source_manifest": copy.deepcopy(self.source_manifest),
            "custom_execution": {
                "mode": "RESEARCH_ONLY", "paper_admission": False,
                "strategy_identity": copy.deepcopy(self.identity),
                "identity_sha256": _sha_json(self.identity),
            },
            "data_manifest": copy.deepcopy(self.data_manifest),
            "data_readiness": copy.deepcopy(self.readiness),
            "bars": _bars(), "metrics": {"initial_cash": 25000},
            "equity": [], "orders": [], "trades": [], "positions": [],
        }
        provenance_hashes = {accounts_module._CODE_PATHS[key]: value
                             for key, value in trusted_hashes.items()}
        self.provenance = {
            "engine": "research_center.custom_execution.v2",
            "backend": "isolated_python", "strategy_version": self.strategy_version,
            "mode": "RESEARCH_ONLY", "source_hashes": provenance_hashes,
        }
        run_dir = self.store.directory(self.source_run["run_id"])
        self.store.save_result(self.source_run["run_id"], self.source_result)
        (run_dir / "custom_strategy.py").write_bytes(self.source)
        (run_dir / "provenance.json").write_text(json.dumps(self.provenance), encoding="utf-8")

        self.patches = [
            patch.object(accounts_module, "_prepare", side_effect=self._prepare),
            patch.object(accounts_module.PythonReplayAccounts, "_source_store",
                         return_value=self.source_store),
            patch.object(accounts_module, "configured_runtime", return_value=self.runtime_sentinel),
            patch.object(accounts_module, "GuestStrategyAdapter", side_effect=self._new_fake_adapter),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temporary.cleanup)

    def _prepare(self, config, source_store):
        return copy.deepcopy(self.prepared)

    def _new_fake_adapter(self, *args, **kwargs):
        return FakeStatefulAdapter(self.fake_adapter, *args, **kwargs)

    def create_account(self):
        return self.accounts.create(self.source_run["run_id"], label="synthetic historical practice")

    def test_missing_namespace_gets_are_read_only(self):
        root = self.accounts.root
        self.assertFalse(root.exists())
        self.assertEqual(self.accounts.list(), [])
        self.assertFalse(root.exists())
        reopened = accounts_module.PythonReplayAccounts(self.store)
        self.assertEqual(reopened.list(), [])
        self.assertFalse(root.exists())

    def test_create_close_failure_and_get_render_from_saved_result_only(self):
        self.fake_adapter.fail_close = True
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.create_account()
        self.assertFalse(self.accounts.root.exists())
        self.fake_adapter.fail_close = False

        created = self.create_account()
        account_id = created["record"]["account_id"]
        with patch.object(accounts_module, "_prepare", side_effect=AssertionError("GET prepared source")):
            with patch.object(custom_kernel, "result_from_state",
                              side_effect=AssertionError("GET rerendered account")):
                self.assertEqual(self.accounts.detail(account_id)["result"], created["result"])
                self.assertEqual(len(self.accounts.list()), 1)

    def test_create_rejects_partial_or_unverified_source_manifest_before_guest_init(self):
        run_dir = self.store.directory(self.source_run["run_id"])
        result_path = run_dir / "result.json"
        original_result = result_path.read_bytes()

        def mismatch(field, value):
            def change(manifest):
                manifest[field] = value
            return change

        def legacy_mismatch(manifest):
            manifest["legacy_source_sha256"]["engine"] = "f" * 64

        def pins_mismatch(manifest):
            manifest["runtime_pins"]["instance"] = "forged-instance"

        def registration_mismatch(manifest):
            manifest["registration"]["code_version"] = "f" * 64

        changes = (
            ("generic driver", mismatch("generic_driver_sha256", "f" * 64)),
            ("legacy source hashes", legacy_mismatch),
            ("runtime pins", pins_mismatch),
            ("interface false", mismatch("runtime_interface_verified", False)),
            ("interface nonbool", mismatch("runtime_interface_verified", "true")),
            ("producer schema", mismatch("schema_version", "unknown-producer")),
            ("registration code version", registration_mismatch),
        )
        try:
            for label, change in changes:
                with self.subTest(field=label):
                    forged = copy.deepcopy(self.source_result)
                    change(forged["source_manifest"])
                    result_path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(accounts_module.PythonReplayAccountError):
                        self.create_account()
                    self.assertFalse(self.accounts.root.exists())
                    self.assertEqual(self.fake_adapter.instances, 0)
        finally:
            result_path.write_bytes(original_result)

    def test_stored_manifest_tampering_fails_even_after_resealing_json_and_row(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        fields = (
            "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
            "total_bars", "created_at", "updated_at", "finalized", *accounts_module._HASH_FIELDS,
        )
        with sqlite3.connect(self.accounts.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            source = json.loads(row["source_json"])
            result = json.loads(row["result_json"])
            source_manifest = source["source_manifest"]
            source_manifest["legacy_source_sha256"]["engine"] = "f" * 64
            result["source_manifest"] = copy.deepcopy(source_manifest)
            source_json = accounts_module._json_dump(source)
            result_json = accounts_module._json_dump(result)
            values = {name: row[name] for name in fields}
            values["source_json_sha256"] = _hash(source_json.encode("utf-8"))
            values["result_sha256"] = _hash(result_json.encode("utf-8"))
            checksum = accounts_module._row_checksum(values)
            connection.execute("""UPDATE accounts SET source_json=?,result_json=?,source_json_sha256=?,
                result_sha256=?,row_checksum=? WHERE account_id=?""",
                (source_json, result_json, values["source_json_sha256"],
                 values["result_sha256"], checksum, account_id))
        with self.assertRaises(accounts_module.PythonReplayAccountError) as caught:
            self.accounts.detail(account_id)
        self.assertEqual(str(caught.exception),
                         "historical Python replay source manifest does not match its components")

    def test_create_pause_resume_step_reopens_with_counter_and_pending_fills(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        self.assertEqual(created["record"]["status"], "paused")
        self.assertEqual(created["record"]["cursor"], 0)
        self.assertEqual(created["record"]["mode"], "RESEARCH_ONLY")
        self.assertFalse(created["record"]["paper_admission"])
        self.assertEqual(created["data_readiness"]["status"], "mismatch")
        self.assertEqual(created["bars"], [])
        self.assertNotIn("user_source", created)
        self.assertNotIn("api_source", created)
        self.assertNotIn("data_root", created["config"])
        self.assertNotIn("data_root", created["result"]["custom_config"])
        self.assertEqual(self.fake_adapter.closes, 1)

        running = self.accounts.resume(account_id, 0)
        first = self.accounts.step(account_id, 1, 1, "counter-1")
        self.assertEqual(first["record"]["cursor"], 1)
        self.assertEqual(first["result"]["orders"][0]["side"], "buy")
        paused = self.accounts.pause(account_id, 2)
        self.assertEqual(paused["record"]["status"], "paused")
        reopened = accounts_module.PythonReplayAccounts(
            RunStore(Path(self.temporary.name) / "runs")
        )
        reopened.resume(account_id, 3)
        second = reopened.step(account_id, 1, 4, "counter-2")
        self.assertEqual(second["record"]["cursor"], 2)
        self.assertEqual(len(second["trades"]), 1)
        self.assertEqual(second["trades"][0]["side"], "buy")
        third = reopened.step(account_id, 2, 5, "counter-3")
        self.assertEqual(third["record"]["status"], "completed")
        self.assertEqual(third["record"]["cursor"], 4)
        self.assertEqual([trade["side"] for trade in third["trades"]], ["buy", "sell"])
        self.assertEqual(third["trades"][0]["fill_datetime"], "2024-01-02T09:32:00")
        self.assertEqual(third["trades"][1]["fill_datetime"], "2024-01-03T09:32:00")
        self.assertEqual([trade["price"] for trade in third["trades"]], [10.1, 10.9])
        self.assertAlmostEqual(third["result"]["metrics"]["ending_equity"], 25079.455, places=6)
        self.assertAlmostEqual(third["result"]["data_manifest"]["total_fees_paid"], 0.545, places=6)
        self.assertEqual(self.fake_adapter.available_to_sell, [0, 0, 100, 0])
        self.assertEqual(third["result"]["custom_execution"]["open_lots"], [])
        self.assertEqual(self.fake_adapter.instances, 4)
        self.assertEqual(self.fake_adapter.closes, 4)

    def test_duplicate_request_replays_cached_ack_without_callback_and_revision_cas(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)
        first = self.accounts.step(account_id, 1, 1, "idempotent")
        event_count = len(first["events"])
        callback_instances = self.fake_adapter.instances
        replayed = self.accounts.step(account_id, 1, 1, "idempotent")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["record"]["cursor"], 1)
        self.assertEqual(len(replayed["events"]), event_count)
        self.assertEqual(self.fake_adapter.instances, callback_instances)
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.accounts.step(account_id, 2, 1, "idempotent")
        with self.assertRaises(accounts_module.PythonReplayRevisionConflict):
            self.accounts.step(account_id, 1, 0, "stale-revision")
        for invalid in (0, 241, True, 1.0, "1"):
            with self.subTest(steps=invalid), self.assertRaises(accounts_module.PythonReplayAccountError):
                self.accounts.step(account_id, invalid, 2, "invalid-" + str(invalid))
        self.assertEqual(self.accounts.detail(account_id)["record"]["cursor"], 1)

    def test_callback_close_result_and_event_failures_leave_checkpoint_and_requests_unchanged(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        self.accounts.resume(account_id, 0)

        def snapshot():
            with sqlite3.connect(self.accounts.database) as connection:
                account = connection.execute(
                    "SELECT revision,cursor,state_json,result_json,row_checksum FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                events = connection.execute("SELECT COUNT(*) FROM events WHERE account_id=?", (account_id,)).fetchone()[0]
                requests = connection.execute("SELECT COUNT(*) FROM requests WHERE account_id=?", (account_id,)).fetchone()[0]
            return account, events, requests

        before = snapshot()
        self.fake_adapter.fail_callback = True
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.accounts.step(account_id, 1, 1, "callback-fail")
        self.assertEqual(snapshot(), before)

        self.fake_adapter.fail_callback = False
        self.fake_adapter.fail_close = True
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.accounts.step(account_id, 1, 1, "close-fail")
        self.assertEqual(snapshot(), before)

        self.fake_adapter.fail_close = False
        with patch.object(custom_kernel, "result_from_state", side_effect=RuntimeError("render fail")):
            with self.assertRaises(accounts_module.PythonReplayAccountError):
                self.accounts.step(account_id, 1, 1, "render-fail")
        self.assertEqual(snapshot(), before)

        with patch.object(self.accounts, "_insert_event", side_effect=sqlite3.OperationalError("synthetic db failure")):
            with self.assertRaises(accounts_module.PythonReplayAccountError):
                self.accounts.step(account_id, 1, 1, "db-fail")
        self.assertEqual(snapshot(), before)

    def test_source_code_bar_and_checkpoint_tampering_fail_closed(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        with sqlite3.connect(self.accounts.database) as connection:
            connection.execute("UPDATE accounts SET user_source=? WHERE account_id=?",
                               (sqlite3.Binary(b"tampered"), account_id))
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.accounts.detail(account_id)

        fresh = self.create_account()
        fresh_id = fresh["record"]["account_id"]
        with sqlite3.connect(self.accounts.database) as connection:
            bars = json.loads(connection.execute(
                "SELECT bars_json FROM accounts WHERE account_id=?", (fresh_id,)
            ).fetchone()[0])
            bars[0]["close"] += 0.1
            connection.execute("UPDATE accounts SET bars_json=? WHERE account_id=?",
                               (json.dumps(bars), fresh_id))
        with self.assertRaises(accounts_module.PythonReplayAccountError):
            self.accounts.resume(fresh_id, 0)

    def test_code_hash_relations_reject_tampering_even_with_resealed_row(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        with sqlite3.connect(self.accounts.database) as connection:
            connection.execute("UPDATE accounts SET code_hash=? WHERE account_id=?",
                               ("f" * 64, account_id))
        with self.assertRaises(accounts_module.PythonReplayAccountError) as caught:
            self.accounts.detail(account_id)
        self.assertEqual(str(caught.exception),
                         "historical Python replay code hash does not match its components")

        fresh = self.create_account()
        fresh_id = fresh["record"]["account_id"]
        fields = (
            "protocol_version", "account_id", "run_id", "label", "status", "revision", "cursor",
            "total_bars", "created_at", "updated_at", "finalized", *accounts_module._HASH_FIELDS,
        )
        with sqlite3.connect(self.accounts.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM accounts WHERE account_id=?", (fresh_id,)).fetchone()
            values = {name: row[name] for name in fields}
            values["engine_code_sha256"] = "e" * 64
            checksum = accounts_module._row_checksum(values)
            connection.execute(
                "UPDATE accounts SET engine_code_sha256=?,row_checksum=? WHERE account_id=?",
                (values["engine_code_sha256"], checksum, fresh_id),
            )
        with self.assertRaises(accounts_module.PythonReplayAccountError) as caught:
            self.accounts.detail(fresh_id)
        self.assertEqual(str(caught.exception),
                         "historical Python replay engine code hash does not match its components")

    def test_current_source_pin_checked_before_resume_and_permission_scope(self):
        created = self.create_account()
        account_id = created["record"]["account_id"]
        stale_prepared = copy.deepcopy(self.prepared)
        stale_prepared.strategy_version = "d" * 64
        with patch.object(accounts_module, "_prepare", return_value=stale_prepared):
            with self.assertRaises(accounts_module.PythonReplaySourceChanged):
                self.accounts.resume(account_id, 0)
        after = self.accounts.detail(account_id)
        self.assertEqual(after["record"]["revision"], 0)
        self.assertEqual(after["record"]["status"], "paused")
        self.assertEqual(len(after["events"]), 1)
        namespace_stat = self.accounts.root.stat()
        database_stat = self.accounts.database.stat()
        self.assertEqual(stat.S_IMODE(namespace_stat.st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(database_stat.st_mode), 0o600)
        self.assertEqual(self.accounts.csv_bytes(account_id, "equity")[:3], b"\xef\xbb\xbf")


if __name__ == "__main__":
    unittest.main()
