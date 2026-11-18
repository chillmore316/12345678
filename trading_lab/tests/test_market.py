from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_lab import market


def envelope(fields, items, code=0):
    return {"code": code, "msg": "provider detail", "data": {"fields": fields, "items": items}}


STOCK_FIELDS = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
CAL_FIELDS = ["exchange", "cal_date", "is_open"]
LIMIT_FIELDS = ["ts_code", "trade_date", "up_limit", "down_limit"]


class FakeClock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def time(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "market.sqlite3"
        self.clock = FakeClock()
        self.service = market.MarketService(self.db_path, clock=self.clock.time, sleeper=self.clock.sleep)
        self.env = patch.dict(os.environ, {
            "TUSHARE_TOKEN": "main-test-secret",
            "TUSHARE_BACKUP_TOKEN": "backup-test-secret",
            "TUSHARE_HTTP_URL": "http://relay.invalid/tu_share/",
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def payloads(self, symbol="603283.SH", dates=("20260105", "20260106"), precloses=(10.0, 10.0)):
        daily = envelope(STOCK_FIELDS, [
            [symbol, dates[0], 10.0, 10.2, 9.8, 10.0, precloses[0], 2.0, 3.5],
            [symbol, dates[1], 10.0, 10.3, 9.9, 10.1, precloses[1], 3.0, 4.5],
        ])
        calendar = envelope(CAL_FIELDS, [
            ["SSE", dates[0], 1], ["SSE", dates[1], 1],
        ])
        limits = envelope(LIMIT_FIELDS, [
            [symbol, dates[0], 11.0, 9.0], [symbol, dates[1], 11.11, 9.09],
        ])
        return {"daily": daily, "fund_daily": daily, "index_daily": daily,
                "trade_cal": calendar, "stk_limit": limits}

    def serve(self, payloads, captured=None):
        def opener(*handlers):
            if captured is not None:
                captured["handlers"] = handlers

            class FixtureOpener:
                def open(self, request, timeout):
                    body = json.loads(request.data.decode("utf-8"))
                    if captured is not None:
                        captured.setdefault("requests", []).append(body)
                        captured.setdefault("request_objects", []).append(request)
                    value = payloads[body["api_name"]]
                    if callable(value):
                        value = value(body)
                    if isinstance(value, Exception):
                        raise value
                    return FakeResponse(json.dumps(value, allow_nan=True).encode("utf-8"))
            return FixtureOpener()

        return patch("trading_lab.market.urllib.request.build_opener", side_effect=opener)

    def test_symbols_and_tushare_units_calendar_and_source_lineage(self):
        captured = {}
        with self.serve(self.payloads(), captured), patch.dict(os.environ, {
            "TUSHARE_PRIVILEGED_APIS": "stk_limit",
        }, clear=False):
            result = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(result["status"], "ok")
        bars = self.service.bars("603283.SH")
        self.assertEqual([row["volume"] for row in bars["rows"]], [200, 300])
        self.assertEqual(bars["rows"][0]["amount"], 3500.0)
        self.assertEqual(bars["rows"][0]["source"], "tushare")
        self.assertEqual(bars["adjustment"], "none")
        self.assertEqual(self.service.calendar("20260105", "20260106"), ["2026-01-05", "2026-01-06"])
        self.assertTrue(bars["rows"][0]["limits_verified"])
        calls = captured["requests"]
        self.assertEqual([item["token"] for item in calls], [
            "backup-test-secret", "backup-test-secret", "main-test-secret",
        ])
        self.assertTrue(any(isinstance(h, __import__("urllib.request").request.ProxyHandler)
                            and h.proxies == {} for h in captured["handlers"]))
        self.assertEqual(len(self.service.symbols()), 3)

    def test_tushare_daily_and_limit_rows_require_exact_codes(self):
        valid = self.payloads()
        daily_records = valid["daily"]["data"]["items"]
        for invalid_code in (None, "", "000300.SH"):
            rows = [list(row) for row in daily_records]
            rows[0][0] = invalid_code
            with self.subTest(daily_code=invalid_code):
                with self.serve({**valid, "daily": envelope(STOCK_FIELDS, rows)}):
                    result = self.service.sync("603283.SH", "20260105", "20260106")
                self.assertEqual(result["status"], "invalid_data")
                self.assertEqual(self.service.bars("603283.SH")["rows"], [])

        for invalid_code in (None, "", "000300.SH"):
            with self.subTest(limit_code=invalid_code):
                with self.assertRaises(ValueError):
                    market.MarketService._limit_rows("603283.SH", [{
                        "ts_code": invalid_code, "trade_date": "20260105",
                        "up_limit": 11.0, "down_limit": 9.0,
                    }], "2026-01-05", "2026-01-05")

    def test_calendar_requires_sse_unique_full_interval_and_keeps_cache_on_bad_reply(self):
        with self.serve(self.payloads()):
            first = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(first["status"], "ok")
        before_bars = self.service.bars("603283.SH")["rows"]
        before_calendar = self.service.calendar("20260105", "20260106")

        daily = self.payloads()["daily"]["data"]["items"]
        daily.append(["603283.SH", "20260107", 10.0, 10.3, 9.9, 10.2, 10.1, 4.0, 5.5])
        limits = self.payloads()["stk_limit"]["data"]["items"]
        limits.append(["603283.SH", "20260107", 11.22, 9.18])
        variants = {
            "partial": envelope(CAL_FIELDS, [["SSE", "20260105", 1], ["SSE", "20260106", 1]]),
            "foreign": envelope(CAL_FIELDS, [
                ["SSE", "20260105", 1], ["SSE", "20260106", 1], ["SZSE", "20260107", 1],
            ]),
            "duplicate": envelope(CAL_FIELDS, [
                ["SSE", "20260105", 1], ["SSE", "20260106", 1],
                ["SSE", "20260107", 1], ["SSE", "20260107", 1],
            ]),
        }
        for label, calendar in variants.items():
            with self.subTest(calendar=label):
                payloads = self.payloads()
                payloads["daily"]["data"]["items"] = [list(row) for row in daily]
                payloads["stk_limit"]["data"]["items"] = [list(row) for row in limits]
                payloads["trade_cal"] = calendar
                with self.serve(payloads):
                    result = self.service.sync("603283.SH", "20260105", "20260107")
                self.assertEqual(result["status"], "invalid_data")
                self.assertTrue(result["cache_retained"])
                self.assertFalse(result["calendar_verified"])
                self.assertEqual(self.service.bars("603283.SH")["rows"], before_bars)
                self.assertEqual(self.service.calendar("20260105", "20260106"), before_calendar)
                self.assertEqual(self.service.execution_bar("603283.SH", "20260107")["status"], "data_missing")

    def test_calendar_rows_require_exact_sse_and_all_dates(self):
        valid = [{"exchange": "SSE", "cal_date": "2026-01-05", "is_open": 1},
                 {"exchange": "SSE", "cal_date": "2026-01-06", "is_open": 0}]
        self.assertEqual(market.MarketService._calendar_rows(valid, "2026-01-05", "2026-01-06"),
                         [("2026-01-05", 1), ("2026-01-06", 0)])
        for rows in (
            [valid[0]],
            [valid[0], {**valid[1], "exchange": "SZSE"}],
            [*valid, dict(valid[1])],
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    market.MarketService._calendar_rows(rows, "2026-01-05", "2026-01-06")

    def test_malformed_nan_and_invalid_ohlc_leave_cache_unchanged(self):
        good = self.payloads()
        with self.serve(good):
            self.service.sync("603283.SH", "20260105", "20260106")
        before = self.service.bars("603283.SH")["rows"]
        bad = self.payloads()
        bad["daily"] = envelope(STOCK_FIELDS, [["603283.SH", "20260105", float("nan"), 10.2, 9.8, 10.0, 10, 1, 1]])
        with self.serve(bad):
            result = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(result["status"], "invalid_data")
        self.assertEqual(self.service.bars("603283.SH")["rows"], before)

        bad = self.payloads()
        bad["daily"] = envelope(STOCK_FIELDS, [["603283.SH", "20260105", 10.0, 9.9, 9.8, 10.0, 10, 1, 1]])
        with self.serve(bad):
            result = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(result["status"], "invalid_data")
        self.assertEqual(self.service.bars("603283.SH")["rows"], before)

    def test_duplicate_rows_dedupe_only_when_identical(self):
        fixture = self.payloads()
        first = fixture["daily"]["data"]["items"][0]
        fixture["daily"]["data"]["items"].insert(1, list(first))
        with self.serve(fixture):
            result = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"], 2)

        bad = self.payloads()
        bad["daily"]["data"]["items"].insert(1, ["603283.SH", "20260105", 10, 10.3, 9.8, 10, 10, 2, 3.5])
        with self.serve(bad):
            result = self.service.sync("603283.SH", "20260105", "20260106")
        self.assertEqual(result["status"], "invalid_data")
        self.assertEqual(len(self.service.bars("603283.SH")["rows"]), 2)

    def test_provider_failure_retains_cache_and_backoff_is_persistent(self):
        with self.serve(self.payloads()):
            self.service.sync("603283.SH", "20260105", "20260106")
        before = self.service.bars("603283.SH")["rows"]
        self.clock.value += 61
        failed = self.payloads()
        failed["daily"] = envelope([], [], code=401)
        with self.serve(failed) as mocked:
            result = self.service.sync("603283.SH", "20260105", "20260106")
            n_calls = mocked.call_count
            again = self.service.sync("603283.SH", "20260105", "20260106")
            self.assertEqual(mocked.call_count, n_calls)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["cache_retained"])
        self.assertEqual(self.service.bars("603283.SH")["rows"], before)
        self.assertEqual(again["status"], "error")
        self.assertGreater(again["retry_after"], 0)
        combined = repr(result) + repr(again)
        self.assertNotIn("main-test-secret", combined)
        self.assertNotIn("backup-test-secret", combined)

    def test_request_spacing_is_at_least_eight_tenths(self):
        captured = {}
        with self.serve(self.payloads(), captured):
            self.service.sync("603283.SH", "20260105", "20260106")
        self.assertGreaterEqual(self.clock.value - 1_800_000_000.0, 1.59)

    def test_explicit_akshare_normalizes_units_without_calendar_or_stock_limits(self):
        records = [{
            "日期": "2026-01-05", "开盘": 0.124, "最高": 0.13, "最低": 0.12,
            "收盘": 0.124, "成交量": 10, "成交额": 500, "涨跌额": 0.0005,
        }]
        class FakeAkshare:
            def fund_etf_hist_em(self, symbol, period, start_date, end_date, adjust):
                self.args = (symbol, period, start_date, end_date, adjust)
                return records
        fake_akshare = FakeAkshare()
        with patch("trading_lab.market.importlib.import_module", return_value=fake_akshare):
            result = self.service.sync("515880.SH", "20260105", "20260105", provider="akshare")
        self.assertEqual(fake_akshare.args, ("515880", "daily", "20260105", "20260105", ""))
        self.assertEqual(result["source"], "akshare:fund_etf_hist_em")
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["calendar_verified"])
        row = self.service.bars("515880.SH")["rows"][0]
        self.assertEqual(row["volume"], 1000)
        self.assertEqual(row["amount"], 500)
        self.assertAlmostEqual(row["pre_close"], 0.1235)
        self.assertEqual(row["limits_source"], market.ETF_LIMIT_SOURCE)
        self.assertEqual(row["up_limit"], 0.136)
        self.assertFalse(self.service.execution_bar("515880.SH", "20260105")["execution_allowed"])
        self.assertEqual(self.service.bars("603283.SH")["source"], None)

    def test_515880_fixed_etf_rule_rounding_and_missing_preclose_fail_closed(self):
        self.assertEqual(market._derive_515880_limits(0.1235), (0.136, 0.111))
        self.assertIsNone(market._derive_515880_limits(None))
        payloads = self.payloads("515880.SH", precloses=(0.1235, 0.1235))
        payloads["fund_daily"]["data"]["items"][1][6] = None
        with self.serve(payloads):
            result = self.service.sync("515880.SH", "20260105", "20260106")
        self.assertEqual(result["limits_rule"]["as_of"], market.ETF_LIMIT_RULE_DATE)
        rows = self.service.bars("515880.SH")["rows"]
        self.assertEqual(rows[0]["up_limit"], 0.136)
        self.assertEqual(rows[0]["down_limit"], 0.111)
        self.assertEqual(rows[0]["limits_source"], market.ETF_LIMIT_SOURCE)
        self.assertIsNone(rows[1]["up_limit"])
        self.assertEqual(self.service.execution_bar("515880.SH", "20260106")["status"], "limits_unverified")

    def test_execution_requires_calendar_limits_and_preclose_continuity(self):
        with self.serve(self.payloads()):
            self.service.sync("603283.SH", "20260105", "20260106")
        bar = self.service.execution_bar("603283.SH", "20260106")
        self.assertTrue(bar["execution_allowed"])
        self.assertTrue(bar["corporate_action_verified"])
        changed = self.payloads(precloses=(10.0, 9.5))
        with self.serve(changed):
            self.service.sync("603283.SH", "20260105", "20260106")
        bar = self.service.execution_bar("603283.SH", "20260106")
        self.assertFalse(bar["execution_allowed"])
        self.assertEqual(bar["status"], "corporate_action_unverified")
        self.assertIn("除权除息", bar["warning"])

    def test_execution_rejects_ohlc_outside_verified_price_limits(self):
        payloads = self.payloads()
        payloads["daily"]["data"]["items"][1][3] = 11.2
        payloads["daily"]["data"]["items"][1][5] = 11.2
        with self.serve(payloads):
            self.service.sync("603283.SH", "20260105", "20260106")
        bar = self.service.execution_bar("603283.SH", "20260106")
        self.assertFalse(bar["execution_allowed"])
        self.assertEqual(bar["status"], "invalid_price_limits")
        self.assertFalse(bar["price_limits_consistent"])
        self.assertIn("OHLC", bar["warning"])

    def test_qfq_import_is_isolated_and_never_execution_ready(self):
        path = Path(self.temp.name) / "qfq.csv"
        path.write_text(
            "date,open,high,low,close,volume,amount,pre_close\n"
            "2026-01-05,10,10.2,9.8,10,200,3500,10\n", encoding="utf-8",
        )
        result = self.service.import_file("603283.SH", path, "本地 CSV", adjustment="qfq")
        bars = self.service.bars("603283.SH")
        self.assertEqual(result["adjustment"], "qfq")
        self.assertEqual(bars["rows"][0]["adjustment"], "qfq")
        self.assertEqual(bars["source"], "本地 CSV")
        self.assertIn("不可用于执行", bars["warning"])
        self.assertEqual(self.service.execution_bar("603283.SH", "20260105")["status"], "adjusted_history_not_executable")

    def test_unknown_provider_does_not_fallback_and_import_rejects_urls(self):
        result = self.service.sync("603283.SH", "20260105", "20260106", provider="automatic")
        self.assertEqual(result["status"], "unsupported")
        with self.assertRaises(ValueError):
            self.service.import_file("603283.SH", "https://example.invalid/a.csv", "akshare")


if __name__ == "__main__":
    unittest.main()
