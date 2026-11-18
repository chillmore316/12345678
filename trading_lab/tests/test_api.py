from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading_lab.app import create_app
from trading_lab.market import MarketService


ORIGIN = "http://127.0.0.1:8090"
DAYS = ("20260105", "20260106", "20260107")


class TradingLabAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.sqlite3"
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps({
            "observations": [{
                "symbol": "603283.SH", "quantity": 300, "reference_cost": 40.25,
                "note": "用户提供的观察，不作为模拟成交。",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        self.app = create_app(db_path=self.db_path, profile_path=self.profile_path)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.headers.update({"Origin": ORIGIN})
        self.market: MarketService = self.app.state.market
        self._seeded = False

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _fixture_rows(api_name, params):
        if api_name in {"daily", "fund_daily", "index_daily"}:
            symbol = params["ts_code"]
            records = []
            for index, day in enumerate(DAYS):
                records.append({
                    "ts_code": symbol, "trade_date": day,
                    "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                    "pre_close": 10.0, "vol": 1000.0 + index * 100,
                    "amount": 100.0 + index,
                })
            return records
        if api_name == "trade_cal":
            return [{"exchange": "SSE", "cal_date": day, "is_open": 1} for day in DAYS]
        if api_name == "stk_limit":
            return [{"ts_code": params["ts_code"], "trade_date": day,
                     "up_limit": 11.0, "down_limit": 9.0} for day in DAYS]
        raise AssertionError(f"Unexpected synthetic endpoint: {api_name}")

    def seed_market(self):
        if self._seeded:
            return
        self.market._request_rows = self._fixture_rows
        for symbol in ("603283.SH", "515880.SH", "000300.SH"):
            result = self.market.sync(symbol, "20260105", "20260107")
            self.assertIn(result["status"], {"ok", "partial"})
        self._seeded = True

    def create_account(self, start="2026-01-05", cash=20_000):
        self.seed_market()
        response = self.client.post("/api/accounts", json={
            "name": "合成测试账户", "initial_cash": cash, "start_date": start,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def replace_frozen_snapshot(self, account_id, snapshot):
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE lab_account_market SET snapshot_json=?,checksum=? WHERE account_id=?",
                (raw, digest, account_id),
            )

    @staticmethod
    def plan_payload(**overrides):
        data = {
            "symbol": "603283.SH", "strategy": "趋势回踩策略V1.0",
            "market_trend": "震荡", "reason": "测试理由", "entry_condition": "收盘确认回踩",
            "entry_price": 10.1, "stop_price": 9.0, "target_price": 12.0,
            "quantity": 100, "discipline": "按条件执行", "status": "planned",
        }
        data.update(overrides)
        return data

    def test_bootstrap_uses_seeded_observations_without_market_requests(self):
        self.app.state.market._request_rows = lambda *args, **kwargs: self.fail("startup/read attempted network")
        response = self.client.get("/api/bootstrap")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual({item["symbol"] for item in body["symbols"]},
                         {"603283.SH", "515880.SH", "000300.SH"})
        self.assertEqual(len(body["observations"]), 1)
        self.assertEqual(body["observations"][0]["note"], "用户提供的观察，不作为模拟成交。")
        self.assertEqual(body["accounts"], [])
        self.assertEqual(body["reviews"], [])
        self.assertEqual(body["plans"], [])
        self.assertEqual(body["strategy"]["name"], "趋势回踩策略V1.0")
        self.assertRegex(body["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue((self.root / "test.sqlite3").is_file())

    def test_mutations_require_exact_same_origin(self):
        body = {"answers": [""] * 7, "tags": [], "completed": False}
        denied = self.client.put("/api/reviews/2026-01-05", json=body, headers={"Origin": ""})
        self.assertEqual(denied.status_code, 403)
        self.assertIn("本地", denied.json()["detail"])
        denied = self.client.put("/api/reviews/2026-01-05", json=body,
                                 headers={"Origin": "http://evil.example:8090"})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.put("/api/reviews/2026-01-05", json=body, headers={"Origin": ORIGIN})
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_review_revisions_delete_tombstone_and_preserve_text(self):
        answers = ["大盘震荡", "未填写板块", "只记录事实", "按计划观察", "没有成交", "暂未发现", "明日继续观察"]
        first = self.client.put("/api/reviews/2026-01-05", json={
            "answers": answers, "tags": ["空仓观察"], "completed": False,
        }).json()
        changed = list(answers); changed[4] = "文字 123，不解释为金额"
        second = self.client.put("/api/reviews/2026-01-05", json={
            "answers": changed, "tags": ["等待确认"], "completed": True,
        })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["revision"], first["revision"] + 1)
        self.assertEqual(second.json()["answers"][4], "文字 123，不解释为金额")
        history = self.client.get("/api/reviews/2026-01-05/history").json()
        self.assertEqual([row["revision"] for row in history], [2, 1])
        self.assertEqual(history[0]["answers"][4], "文字 123，不解释为金额")
        deleted = self.client.delete("/api/reviews/2026-01-05")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/reviews?search=观察").json(), [])
        history = self.client.get("/api/reviews/2026-01-05/history").json()
        self.assertEqual(history[0]["event"], "deleted")
        self.assertTrue(history[0]["deleted"])
        self.assertEqual(len(history), 3)

    def test_plan_history_and_order_idempotency_keep_original_snapshot(self):
        account = self.create_account()
        created = self.client.post("/api/plans", json=self.plan_payload())
        self.assertEqual(created.status_code, 200, created.text)
        plan = created.json()
        edited = self.client.put(f"/api/plans/{plan['id']}", json=self.plan_payload(reason="修订后的计划"))
        self.assertEqual(edited.status_code, 200, edited.text)
        plan_id = plan["id"]
        history = self.client.get(f"/api/plans/{plan_id}/history").json()
        self.assertEqual(len(history), 2)
        self.assertEqual(len({row["id"] for row in history}), 2)
        self.assertEqual(history[0]["plan_id"], plan_id)
        self.assertEqual(history[0]["snapshot"]["reason"], "修订后的计划")

        order_body = {
            "symbol": "603283.SH", "side": "BUY", "quantity": 100,
            "limit_price": 10.1, "stop_price": 9.0, "risk_pct": 1,
            "plan_id": plan_id, "request_id": "same-order-uuid",
        }
        placed = self.client.post(f"/api/accounts/{account['id']}/orders", json=order_body)
        self.assertEqual(placed.status_code, 200, placed.text)
        detail = placed.json()
        saved = detail["orders"][0]
        self.assertEqual(saved["plan_snapshot"]["plan_revision"], 2)
        self.assertEqual(saved["plan_snapshot"]["plan"]["reason"], "修订后的计划")
        self.assertEqual(saved["plan_snapshot"]["strategy"], "趋势回踩策略V1.0")
        self.assertEqual(saved["plan_snapshot"]["discipline"], "unknown")
        self.assertEqual(saved["created_at"], "2026-01-05")

        self.client.put(f"/api/plans/{plan_id}", json=self.plan_payload(reason="再一次修订"))
        retried = self.client.post(f"/api/accounts/{account['id']}/orders", json=order_body)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(len(retried.json()["orders"]), 1)
        changed = dict(order_body, quantity=200)
        conflict = self.client.post(f"/api/accounts/{account['id']}/orders", json=changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("请求编号", conflict.json()["detail"])

    def test_account_freezes_market_and_get_is_clipped_after_refresh(self):
        account = self.create_account(start="2026-01-06")
        snapshot = self.client.get("/api/market/603283.SH?end=2026-01-07&account_id=" + str(account["id"]))
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        frozen = snapshot.json()
        self.assertEqual(frozen["rows"][-1]["date"], "2026-01-06")
        self.assertEqual(frozen["rows"][0]["date"], "2026-01-05")
        dataset_id = frozen["dataset_id"]
        source = frozen["source"]

        csv_path = self.root / "replacement.csv"
        csv_path.write_text(
            "date,open,high,low,close,volume,amount,pre_close\n"
            "2026-01-06,10,10.5,9.5,10.4,100000,10000,10\n", encoding="utf-8",
        )
        self.market.import_file("603283.SH", csv_path, "alternate local source")
        live = self.client.get("/api/market/603283.SH?end=2026-01-07").json()
        self.assertEqual(live["source"], "alternate local source")
        frozen_again = self.client.get(
            f"/api/market/603283.SH?end=2026-01-07&account_id={account['id']}"
        ).json()
        self.assertEqual(frozen_again["source"], source)
        self.assertEqual(frozen_again["dataset_id"], dataset_id)
        self.assertEqual(frozen_again["rows"][-1]["close"], 10.0)

    def test_account_create_request_retries_original_account_and_snapshot(self):
        self.seed_market()
        body = {
            "name": "幂等账户", "initial_cash": 20_000, "start_date": "2026-01-05",
            "request_id": "create-account-once",
        }
        first = self.client.post("/api/accounts", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        accepted = first.json()
        account_id = accepted["id"]
        frozen_before = self.client.get(
            f"/api/market/603283.SH?end=2026-01-07&account_id={account_id}"
        ).json()

        csv_path = self.root / "refreshed.csv"
        csv_path.write_text(
            "date,open,high,low,close,volume,amount,pre_close\n"
            "2026-01-05,11,11.5,10.5,11,100000,10000,10\n", encoding="utf-8",
        )
        self.market.import_file("603283.SH", csv_path, "refreshed local source")
        self.assertEqual(
            self.client.get("/api/market/603283.SH?end=2026-01-07").json()["source"],
            "refreshed local source",
        )

        retried = self.client.post("/api/accounts", json=body)
        self.assertEqual(retried.status_code, 200, retried.text)
        accepted_again = retried.json()
        self.assertEqual(accepted_again["id"], account_id)
        self.assertEqual(accepted_again["market_snapshot"], accepted["market_snapshot"])
        self.assertEqual(len(self.client.get("/api/accounts").json()), 1)
        frozen_after = self.client.get(
            f"/api/market/603283.SH?end=2026-01-07&account_id={account_id}"
        ).json()
        self.assertEqual(frozen_after["dataset_id"], frozen_before["dataset_id"])
        self.assertEqual(frozen_after["source"], frozen_before["source"])
        self.assertEqual(frozen_after["rows"], frozen_before["rows"])

    def test_account_create_request_id_rejects_changed_payload(self):
        self.seed_market()
        body = {
            "name": "幂等账户", "initial_cash": 20_000, "start_date": "2026-01-05",
            "request_id": "create-account-conflict",
        }
        first = self.client.post("/api/accounts", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        changed = self.client.post("/api/accounts", json={**body, "initial_cash": 30_000})
        self.assertEqual(changed.status_code, 409)
        self.assertIn("请求编号", changed.json()["detail"])
        self.assertEqual(len(self.client.get("/api/accounts").json()), 1)

    def test_account_creation_callback_failure_rolls_back_all_rows(self):
        import importlib

        app_module = importlib.import_module("trading_lab.app")
        self.seed_market()
        store = app_module._store_account_creation

        def store_then_fail(*args, **kwargs):
            store(*args, **kwargs)
            raise RuntimeError("synthetic atomic callback failure")

        body = {
            "name": "不会留下的账户", "initial_cash": 20_000, "start_date": "2026-01-05",
            "request_id": "create-account-rollback",
        }
        with patch.object(app_module, "_store_account_creation", side_effect=store_then_fail):
            with TestClient(self.app, base_url=ORIGIN, raise_server_exceptions=False) as client:
                client.headers.update({"Origin": ORIGIN})
                response = client.post("/api/accounts", json=body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.client.get("/api/accounts").json(), [])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_account_market").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_account_creates").fetchone()[0], 0)

    def test_advance_pregated_failure_is_422_and_same_request_can_retry(self):
        account = self.create_account()
        account_id = account["id"]
        db_path = self.app.state.db_path
        from trading_lab.app import _snapshot_for_account

        saved = _snapshot_for_account(db_path, account_id)
        incomplete = json.loads(json.dumps(saved))
        incomplete["execution_bars"]["603283.SH"].pop("2026-01-06")
        self.replace_frozen_snapshot(account_id, incomplete)
        request = {"request_id": "advance-after-precondition"}
        before = self.client.get(f"/api/accounts/{account_id}").json()
        rejected = self.client.post(f"/api/accounts/{account_id}/advance", json=request)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("进度保持不变", rejected.json()["detail"])
        after_rejection = self.client.get(f"/api/accounts/{account_id}").json()
        self.assertEqual(after_rejection, before)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM idempotency WHERE account_id=? AND method='advance' "
                "AND request_id=?", (account_id, request["request_id"]),
            ).fetchone()[0], 0)

        self.replace_frozen_snapshot(account_id, saved)
        retried = self.client.post(f"/api/accounts/{account_id}/advance", json=request)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["cursor_date"], "2026-01-06")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM idempotency WHERE account_id=? AND method='advance' "
                "AND request_id=?", (account_id, request["request_id"]),
            ).fetchone()[0], 1)

    def test_advance_terminal_date_pregate_is_422_without_ledger_change(self):
        account = self.create_account(start="2026-01-06")
        account_id = account["id"]
        advanced = self.client.post(f"/api/accounts/{account_id}/advance", json={"request_id": "to-last-day"})
        self.assertEqual(advanced.status_code, 200, advanced.text)
        before = self.client.get(f"/api/accounts/{account_id}").json()
        request = {"request_id": "after-terminal-date"}
        rejected = self.client.post(f"/api/accounts/{account_id}/advance", json=request)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("冻结历史数据末日", rejected.json()["detail"])
        repeated = self.client.post(f"/api/accounts/{account_id}/advance", json=request)
        self.assertEqual(repeated.status_code, 422, repeated.text)
        after = self.client.get(f"/api/accounts/{account_id}").json()
        self.assertEqual(after, before)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM idempotency WHERE account_id=? AND method='advance' "
                "AND request_id=?", (account_id, request["request_id"]),
            ).fetchone()[0], 0)

    def test_advance_uses_frozen_next_day_and_duplicate_uuid_returns_account(self):
        account = self.create_account()
        order = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "BUY", "quantity": 100,
            "limit_price": 10.1, "stop_price": 9.0, "request_id": "fill-once",
        })
        self.assertEqual(order.status_code, 200, order.text)
        first = self.client.post(f"/api/accounts/{account['id']}/advance", json={"request_id": "advance-once"})
        self.assertEqual(first.status_code, 200, first.text)
        payload = first.json()
        self.assertEqual(payload["cursor_date"], "2026-01-06")
        self.assertEqual(payload["fills"][0]["plan_id"], None)
        self.assertEqual(payload["fills"][0]["quantity"], 100)
        retry = self.client.post(f"/api/accounts/{account['id']}/advance", json={"request_id": "advance-once"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["cursor_date"], "2026-01-06")
        self.assertEqual(len(retry.json()["fills"]), 1)
        stats = self.client.get(f"/api/accounts/{account['id']}/stats").json()
        self.assertEqual(stats["totalfills"], 1)
        self.assertIsInstance(stats["monthly"], list)

    def test_unfilled_expired_order_does_not_complete_plan(self):
        account = self.create_account()
        plan = self.client.post("/api/plans", json=self.plan_payload()).json()
        order = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "BUY", "quantity": 100,
            "limit_price": 9.9, "stop_price": 9.0, "plan_id": plan["id"],
            "request_id": "expires-unfilled",
        })
        self.assertEqual(order.status_code, 200, order.text)
        advanced = self.client.post(f"/api/accounts/{account['id']}/advance", json={"request_id": "advance-no-fill"})
        self.assertEqual(advanced.status_code, 200, advanced.text)
        self.assertEqual(advanced.json()["fills"], [])
        order_record = advanced.json()["orders"][0]
        self.assertIn(order_record["status"], {"expired", "partially_filled_expired"})
        self.assertEqual(self.client.get("/api/plans").json()[0]["status"], "planned")

    def test_monthly_net_pnl_is_numeric_and_equals_closed_cycle_total(self):
        account = self.create_account()
        buy = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "BUY", "quantity": 100,
            "limit_price": 10.1, "stop_price": 9.0, "request_id": "monthly-buy",
        })
        self.assertEqual(buy.status_code, 200, buy.text)
        first = self.client.post(f"/api/accounts/{account['id']}/advance", json={
            "request_id": "monthly-buy-advance",
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["fills"][0]["side"], "BUY")

        sell = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "SELL", "quantity": 100,
            "limit_price": 9.9, "request_id": "monthly-sell",
        })
        self.assertEqual(sell.status_code, 200, sell.text)
        second = self.client.post(f"/api/accounts/{account['id']}/advance", json={
            "request_id": "monthly-sell-advance",
        })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["fills"][-1]["side"], "SELL")

        closed_cycles = [cycle for cycle in second.json()["cycles"] if cycle["status"] == "closed"]
        self.assertEqual(len(closed_cycles), 1)
        expected = sum(float(cycle["net_pnl"]) for cycle in closed_cycles)
        stats = self.client.get(f"/api/accounts/{account['id']}/stats").json()
        self.assertEqual(len(stats["monthly"]), 1)
        monthly = stats["monthly"][0]
        self.assertIsInstance(monthly["net_pnl"], float)
        self.assertIsInstance(monthly["closed_cycle_net"], float)
        self.assertIsInstance(monthly["month_end_equity"], float)
        self.assertAlmostEqual(monthly["net_pnl"], expected, places=2)

    def test_market_sync_api_returns_rows_array_and_import_accepts_ui_source(self):
        self.market._request_rows = self._fixture_rows
        response = self.client.post("/api/market/sync", json={
            "symbol": "603283.SH", "start": "2026-01-05", "end": "2026-01-07", "provider": "tushare",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json()["rows"], list)
        self.assertEqual(len(response.json()["rows"]), 3)
        self.assertIsInstance(response.json()["sync_status"]["rows"], int)
        upload = self.client.post("/api/market/import", data={
            "symbol": "603283.SH", "source": "本地 CSV", "adjustment": "none",
        }, files={"file": ("bars.csv", "date,open,high,low,close,volume,amount,pre_close\n"
                                 "2026-01-05,10,10.5,9.5,10,10000,100000,10\n", "text/csv")})
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertIsInstance(upload.json()["rows"], list)
        self.assertEqual(upload.json()["source"], "本地 CSV")

    def test_backup_restore_is_separate_whitelisted_and_financially_checked(self):
        self.seed_market()
        created = self.client.post("/api/accounts", json={
            "name": "备份测试账户", "initial_cash": 20_000, "start_date": "2026-01-05",
            "request_id": "backup-account-request",
        })
        self.assertEqual(created.status_code, 200, created.text)
        self.client.put("/api/reviews/2026-01-05", json={
            "answers": ["记录"] * 7, "tags": ["等待确认"], "completed": False,
        })
        self.client.post("/api/plans", json=self.plan_payload())
        with patch.dict(os.environ, {"TUSHARE_TOKEN": "synthetic-token-never-export"}):
            backup_response = self.client.get("/api/backup")
        self.assertEqual(backup_response.status_code, 200)
        backup = backup_response.json()
        self.assertEqual(backup["format"], "trading-lab-backup")
        self.assertIn("lab_observations", backup["tables"])
        self.assertIn("lab_account_market", backup["tables"])
        self.assertIn("lab_account_creates", backup["tables"])
        self.assertNotIn("synthetic-token-never-export", backup_response.text)
        restored = self.client.post("/api/restore", json={"backup": backup})
        self.assertEqual(restored.status_code, 200, restored.text)
        result = restored.json()
        self.assertFalse(result["restored"])
        filename = re.search(r"另存为 ([^。]+)\。", result["message"]).group(1)
        self.assertEqual(Path(filename).name, filename)
        restored_path = self.root / filename
        self.assertTrue(restored_path.is_file())
        self.assertEqual(len(self.client.get("/api/accounts").json()), 1)
        with sqlite3.connect(restored_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT request_id,account_id FROM lab_account_creates"
            ).fetchall(), [("backup-account-request", created.json()["id"])])

        legacy_backup = json.loads(json.dumps(backup))
        del legacy_backup["tables"]["lab_account_creates"]
        legacy_restore = self.client.post("/api/restore", json={"backup": legacy_backup})
        self.assertEqual(legacy_restore.status_code, 200, legacy_restore.text)

        tampered = json.loads(json.dumps(backup))
        row = tampered["tables"]["accounts"]["rows"][0]
        row["cash"] = str(float(row["cash"]) + 1.0)
        rejected = self.client.post("/api/restore", json={"backup": tampered})
        self.assertEqual(rejected.status_code, 422)
        self.assertIn("账本核对", rejected.json()["detail"])
        self.assertEqual(len(self.client.get("/api/accounts").json()), 1)

    def test_csv_export_includes_plan_link_and_json_errors_are_chinese(self):
        account = self.create_account()
        plan = self.client.post("/api/plans", json=self.plan_payload()).json()
        placed = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "BUY", "quantity": 100,
            "limit_price": 10.1, "stop_price": 9.0, "plan_id": plan["id"],
            "request_id": "csv-fill",
        })
        self.assertEqual(placed.status_code, 200, placed.text)
        self.client.post(f"/api/accounts/{account['id']}/advance", json={"request_id": "csv-advance"})
        csv_response = self.client.get(f"/api/export/fills.csv?account_id={account['id']}")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("plan_id", csv_response.text)
        detail = self.client.get(f"/api/accounts/{account['id']}").json()
        fill = detail["fills"][0]
        self.assertEqual(fill["plan_id"], str(plan["id"]))
        self.assertEqual(fill["quantity"], 100)
        response = self.client.post(f"/api/accounts/{account['id']}/orders", json={
            "symbol": "603283.SH", "side": "BUY", "quantity": 1000000,
            "limit_price": 10.1, "stop_price": 9.0, "request_id": "risk-reject",
        })
        self.assertEqual(response.status_code, 422)
        self.assertRegex(response.json()["detail"], r"[\u4e00-\u9fff]")


if __name__ == "__main__":
    unittest.main()
