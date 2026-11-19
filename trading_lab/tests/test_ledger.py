import tempfile
import unittest
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from trading_lab.ledger import (
    AlreadyAdvancedError,
    IdempotencyConflict,
    Ledger,
    LedgerError,
    RiskLimitError,
)


STOCK = "603283.SH"
ETF = "515880.SH"


def bar(
    symbol=STOCK,
    *,
    open_price="10.00",
    close="10.00",
    volume=1000,
    capacity_volume=1000,
    capacity_date="2026-08-31",
    adjustment="none",
    corporate_action_verified=True,
    execution_allowed=True,
    limits_verified=True,
):
    if symbol == ETF:
        upper, lower = "2.400", "1.800"
    else:
        upper, lower = "11.00", "9.00"
    return {
        "is_open": True,
        "calendar_verified": True,
        "corporate_action_verified": corporate_action_verified,
        "execution_allowed": execution_allowed,
        "limits_verified": limits_verified,
        "adjustment": adjustment,
        "open": open_price,
        "close": close,
        "up_limit": upper,
        "down_limit": lower,
        "volume": volume,
        "capacity_volume": capacity_volume,
        "capacity_date": capacity_date,
    }


def advance(ledger, account_id, day, bars, request_id):
    previous = ledger.account(account_id)["cursor_date"]
    dated = {}
    for symbol, values in bars.items():
        if isinstance(values, dict):
            values.setdefault("date", day)
            values.setdefault("previous_session_date", previous)
        dated[symbol] = values
    return ledger.advance(account_id, day, dated, request_id)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trading_lab.sqlite3"
        self.ledger = Ledger(self.db_path)
        self.account = self.ledger.create_account("sandbox")
        self.account_id = self.account["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def make_buy(self, *, qty=100, price="10.00", stop="9.80", **kwargs):
        return self.ledger.order(
            self.account_id, STOCK, "BUY", qty, price, stop_price=stop, **kwargs
        )

    def test_new_account_is_empty_and_has_day_zero_equity(self):
        account = self.account
        self.assertEqual(account["cursor_date"], "2026-09-01")
        self.assertEqual(account["cash"], "20000.00")
        self.assertEqual(account["equity"], "20000.00")
        self.assertEqual(account["positions"], [])
        self.assertEqual(len(account["equity_curve"]), 1)
        self.assertEqual(account["equity_curve"][0]["date"], "2026-09-01")
        with self.assertRaises(LedgerError):
            self.ledger.order(self.account_id, "000001.SH", "BUY", 100, "10.00", "9.00")

    def test_order_is_pending_and_idempotent_before_any_fill(self):
        order = self.make_buy(request_id="buy-1", plan_snapshot={"strategy_name": "swing"})
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.ledger.account(self.account_id)["fills"], [])
        replay = self.make_buy(request_id="buy-1", plan_snapshot={"strategy_name": "swing"})
        self.assertEqual(replay["id"], order["id"])
        with self.assertRaises(IdempotencyConflict):
            self.ledger.order(self.account_id, STOCK, "BUY", 100, "10.00",
                              stop_price="9.80", request_id="buy-1", risk_pct=2)
        state = self.ledger.account(self.account_id)
        self.assertEqual(state["reserved_cash"], "1005.01")
        self.assertEqual(state["available_cash"], "18994.99")

    def test_buy_orders_require_matching_strategy_and_version(self):
        metadata = {"strategy_name": "swing", "strategy_version": "v1"}
        self.make_buy(request_id="swing-a", plan_id="plan-a", plan_snapshot=metadata)
        # A different plan ID is allowed when strategy name and version match.
        self.make_buy(request_id="swing-b", plan_id="plan-b", plan_snapshot=metadata)
        with self.assertRaisesRegex(LedgerError, "strategy name/version"):
            self.make_buy(request_id="mean-reversion",
                          plan_snapshot={"strategy_name": "mean_revert", "strategy_version": "v1"})
        with self.assertRaisesRegex(LedgerError, "strategy name/version"):
            self.make_buy(request_id="unassigned", plan_snapshot={})

    def test_open_cycle_rejects_other_strategy_version(self):
        meta = {"strategy_name": "swing", "strategy_version": "v1"}
        self.make_buy(request_id="open-cycle", plan_id="plan-a", plan_snapshot=meta)
        advance(self.ledger, self.account_id, "2026-09-02",
                {STOCK: bar(capacity_volume=2000, capacity_date="2026-09-01")}, "open-cycle-fill")
        self.assertEqual(self.ledger.account(self.account_id)["cycles"][0]["strategy"], "swing")
        self.make_buy(request_id="same-cycle-add", plan_id="plan-b", plan_snapshot=meta)
        with self.assertRaisesRegex(LedgerError, "strategy name/version"):
            self.make_buy(request_id="other-cycle-add",
                          plan_snapshot={"strategy_name": "swing", "strategy_version": "v2"})

    def test_fill_time_strategy_mismatch_expires_order_without_rolling_back_valid_fill(self):
        first = self.make_buy(
            request_id="fill-guard-a", plan_id="p1",
            plan_snapshot={"strategy_name": "swing", "strategy_version": "v1"},
        )
        second = self.make_buy(
            request_id="fill-guard-b", plan_id="p2",
            plan_snapshot={"strategy_name": "swing", "strategy_version": "v1"},
        )
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT plan_snapshot_json FROM orders WHERE id = ?",
                               (second["id"],)).fetchone()
            snapshot = json.loads(row[0])
            snapshot["strategy_name"] = "mean_revert"
            conn.execute("UPDATE orders SET plan_snapshot_json = ? WHERE id = ?",
                         (json.dumps(snapshot), second["id"]))
        result = advance(
            self.ledger, self.account_id, "2026-09-02",
            {STOCK: bar(capacity_volume=2000, capacity_date="2026-09-01")},
            "fill-guard",
        )
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["fills"][0]["order_id"], first["id"])
        self.assertEqual(result["blocked_orders"], [
            {"order_id": second["id"], "reason": "strategy_version_mismatch_at_fill"}
        ])
        state = self.ledger.account(self.account_id)
        self.assertEqual(state["cursor_date"], "2026-09-02")
        self.assertEqual(state["positions"][0]["quantity"], 100)
        self.assertEqual(state["orders"][1]["status"], "expired")

    def test_buy_lot_tick_and_risk_budget_are_enforced(self):
        with self.assertRaises(LedgerError):
            self.make_buy(qty=99)
        with self.assertRaises(LedgerError):
            self.make_buy(price="10.001")
        with self.assertRaises(RiskLimitError):
            self.make_buy(qty=1000, price="10.00", stop="9.00")
        # risk_pct is in percentage points and is adjustable by the caller.
        order = self.make_buy(qty=1000, price="10.00", stop="9.00", risk_pct=10)
        self.assertEqual(order["status"], "pending")

    def test_t1_partial_fills_weighted_cost_fees_cycles_and_stats(self):
        metadata = {
            "strategy_name": "mean_revert",
            "strategy_version": "v2",
            "discipline": "followed",
        }
        order = self.make_buy(qty=100, request_id="entry-1", plan_id="plan-1",
                              plan_snapshot=metadata)

        day1 = bar(open_price="10.00", close="10.10", capacity_date="2026-09-01")
        result1 = advance(self.ledger, self.account_id, "2026-09-02", {STOCK: day1}, "d1")
        self.assertEqual(len(result1["fills"]), 1)
        self.assertEqual(result1["fills"][0]["quantity"], 100)
        self.assertEqual(result1["fills"][0]["commission"], "5.00")
        self.assertEqual(result1["fills"][0]["transfer_fee"], "0.01")
        self.assertEqual(self.ledger.account(self.account_id)["orders"][0]["status"], "filled")
        position = self.ledger.account(self.account_id)["positions"][0]
        self.assertEqual(position["sellable"], 0)
        self.assertEqual(position["next_session_sellable"], 100)

        self.make_buy(qty=100, price="9.99", stop="9.80", request_id="entry-2",
                      plan_id="plan-1", plan_snapshot=metadata)
        day2 = bar(open_price="9.99", close="10.00", capacity_date="2026-09-02")
        result2 = advance(self.ledger, self.account_id, "2026-09-03", {STOCK: day2}, "d2")
        self.assertEqual(result2["fills"][0]["quantity"], 100)
        self.assertEqual(result2["fills"][0]["commission"], "5.00")
        position = self.ledger.account(self.account_id)["positions"][0]
        self.assertEqual(position["quantity"], 200)
        self.assertEqual(position["sellable"], 100)
        self.assertEqual(position["avg_cost"], "10.045100")
        self.assertEqual(Decimal(position["market_value"]), Decimal("2000.00"))
        self.assertEqual(Decimal(position["unrealized_pnl"]), Decimal("-9.02"))
        self.assertEqual(Decimal(self.ledger.account(self.account_id)["equity"]),
                         Decimal(self.ledger.account(self.account_id)["cash"]) + Decimal("2000.00"))
        self.assertEqual(Decimal(self.ledger.account(self.account_id)["equity"]),
                         Decimal("20000.00") + Decimal(position["unrealized_pnl"]))
        cycle_before_sale = self.ledger.account(self.account_id)["cycles"][0]
        self.assertGreater(Decimal(cycle_before_sale["initial_risk"]), Decimal("30.00"))
        self.assertEqual(cycle_before_sale["total_buy_quantity"], 200)
        self.assertEqual(cycle_before_sale["strategy"], "mean_revert")
        self.assertEqual(cycle_before_sale["strategy_version"], "v2")
        self.assertEqual(cycle_before_sale["discipline"], "followed")

        sell = self.ledger.order(self.account_id, STOCK, "SELL", 200, "10.00",
                                 request_id="exit")
        self.assertEqual(sell["status"], "pending")
        day3 = bar(open_price="10.10", close="10.15", capacity_date="2026-09-03")
        result3 = advance(self.ledger, self.account_id, "2026-09-04", {STOCK: day3}, "d3")
        self.assertEqual(result3["fills"][0]["quantity"], 100)
        self.assertEqual(result3["fills"][0]["stamp_duty"], "0.51")
        self.assertEqual(self.ledger.account(self.account_id)["orders"][2]["status"],
                         "partially_filled_expired")
        self.ledger.order(self.account_id, STOCK, "SELL", 100, "10.00",
                          request_id="exit-remainder")
        day4 = bar(open_price="10.20", close="10.20", capacity_date="2026-09-04")
        result4 = advance(self.ledger, self.account_id, "2026-09-07", {STOCK: day4}, "d4")
        self.assertEqual(result4["fills"][0]["quantity"], 100)
        final = self.ledger.account(self.account_id)
        cycle = final["cycles"][0]
        self.assertEqual(cycle["status"], "closed")
        self.assertEqual(cycle["total_buy_quantity"], 200)
        self.assertEqual(cycle["total_sell_quantity"], 200)
        self.assertEqual(final["positions"], [])
        self.assertEqual(len(final["fills"]), 4)
        self.assertEqual(cycle["net_pnl"], "9.94")
        self.assertEqual(final["realized_pnl"], cycle["net_pnl"])
        self.assertEqual(Decimal(final["equity"]) - Decimal(final["initial_cash"]),
                         Decimal(final["realized_pnl"]))
        stats = self.ledger.stats(self.account_id)
        self.assertEqual(stats["total_fills"], 4)
        self.assertEqual(stats["total_closed"], 1)
        self.assertEqual(stats["win_rate"], "1")
        self.assertIsNone(stats["avg_loss"])
        self.assertIsNone(stats["profit_factor"])
        self.assertEqual(stats["by_strategy"]["mean_revert@v2"]["count"], 1)
        self.assertEqual(stats["by_discipline"]["followed"]["count"], 1)
        self.assertTrue(stats["insufficient_samples"])

    def test_etf_tick_and_no_stock_stamp_or_transfer_fees(self):
        order = self.ledger.order(
            self.account_id, ETF, "BUY", 100, "2.125", stop_price="2.100",
            request_id="etf-buy",
        )
        result = advance(
            self.ledger,
            self.account_id, "2026-09-02",
            {ETF: bar(ETF, open_price="2.123", close="2.130", capacity_date="2026-09-01")},
            "etf-d1",
        )
        fill = result["fills"][0]
        self.assertEqual(fill["price"], "2.123")
        self.assertEqual(fill["commission"], "5.00")
        self.assertEqual(fill["stamp_duty"], "0.00")
        self.assertEqual(fill["transfer_fee"], "0.00")
        self.assertEqual(order["status"], "pending")
        self.ledger.order(self.account_id, ETF, "SELL", 100, "2.120",
                          request_id="etf-sell")
        sold = advance(
            self.ledger, self.account_id, "2026-09-03",
            {ETF: bar(ETF, open_price="2.130", close="2.130",
                      capacity_date="2026-09-02")},
            "etf-d2",
        )["fills"][0]
        self.assertEqual(sold["side"], "SELL")
        self.assertEqual(sold["stamp_duty"], "0.00")
        self.assertEqual(sold["transfer_fee"], "0.00")
        with self.assertRaises(LedgerError):
            self.ledger.order(self.account_id, ETF, "BUY", 100, "2.1234", stop_price="2.100")

    def test_same_day_sell_submission_queues_for_next_t1_session(self):
        self.make_buy(request_id="same-day-entry")
        advance(self.ledger, self.account_id, "2026-09-02",
                {STOCK: bar(capacity_date="2026-09-01")}, "same-day-buy-fill")
        position = self.ledger.account(self.account_id)["positions"][0]
        self.assertEqual(position["sellable"], 0)
        self.assertEqual(position["next_session_sellable"], 100)
        sell = self.ledger.order(self.account_id, STOCK, "SELL", 100, "10.00",
                                 request_id="same-day-sell")
        self.assertEqual(sell["status"], "pending")
        next_day = advance(
            self.ledger, self.account_id, "2026-09-03",
            {STOCK: bar(open_price="10.10", capacity_date="2026-09-02")},
            "next-session-sell",
        )
        self.assertEqual(len(next_day["fills"]), 1)
        self.assertEqual(next_day["fills"][0]["side"], "SELL")
        self.assertEqual(next_day["fills"][0]["date"], "2026-09-03")
        self.assertEqual(self.ledger.account(self.account_id)["positions"], [])

    def test_sell_at_lower_limit_is_not_filled_and_expires(self):
        self.make_buy(request_id="lower-lock-entry")
        advance(self.ledger, self.account_id, "2026-09-02",
                {STOCK: bar(capacity_date="2026-09-01")}, "lower-lock-buy")
        sell = self.ledger.order(self.account_id, STOCK, "SELL", 100, "9.00",
                                 request_id="lower-lock-sell")
        result = advance(
            self.ledger, self.account_id, "2026-09-03",
            {STOCK: bar(open_price="9.00", close="9.00", capacity_date="2026-09-02")},
            "lower-lock",
        )
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["blocked_orders"][0]["reason"],
                         "locked_at_lower_limit_no_queue_fill")
        state = self.ledger.account(self.account_id)
        self.assertEqual(state["orders"][1]["id"], sell["id"])
        self.assertEqual(state["orders"][1]["status"], "expired")
        self.assertEqual(state["positions"][0]["quantity"], 100)

    def test_buy_open_below_stop_is_rejected_to_preserve_risk_budget(self):
        self.make_buy(request_id="gap-below-stop")
        result = advance(
            self.ledger, self.account_id, "2026-09-02",
            {STOCK: bar(open_price="9.70", close="9.70", capacity_date="2026-09-01")},
            "gap-below-stop-day",
        )
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["blocked_orders"][0]["reason"],
                         "open_at_or_below_stop_invalidates_risk")
        self.assertEqual(self.ledger.account(self.account_id)["positions"], [])

    def test_sell_odd_lot_must_liquidate_whole_remainder(self):
        self.make_buy(qty=200, request_id="odd-entry")
        advance(
            self.ledger, self.account_id, "2026-09-02",
            {STOCK: bar(capacity_volume=2000, capacity_date="2026-09-01")}, "odd-buy-fill",
        )
        with self.assertRaises(LedgerError):
            self.ledger.order(self.account_id, STOCK, "SELL", 50, "10.00",
                              request_id="bad-odd-sell")
        sell = self.ledger.order(self.account_id, STOCK, "SELL", 200, "10.00",
                                 request_id="partial-sell")
        result = advance(
            self.ledger, self.account_id, "2026-09-03",
            {STOCK: bar(open_price="10.10", capacity_volume=1500,
                        capacity_date="2026-09-02")}, "partial-sell-fill",
        )
        self.assertEqual(result["fills"][0]["quantity"], 150)
        self.assertEqual(self.ledger.account(self.account_id)["positions"][0]["quantity"], 50)
        self.assertEqual(self.ledger.cancel(self.account_id, sell["id"])["status"],
                         "partially_filled_expired")
        residual_sell = self.ledger.order(self.account_id, STOCK, "SELL", 50, "10.00",
                                          request_id="sell-odd-residual")
        self.assertEqual(residual_sell["quantity"], 50)
        final = advance(
            self.ledger, self.account_id, "2026-09-04",
            {STOCK: bar(open_price="10.10", capacity_volume=1500,
                        capacity_date="2026-09-03")}, "sell-odd-residual-fill",
        )
        self.assertEqual(final["fills"][0]["quantity"], 50)
        self.assertEqual(self.ledger.account(self.account_id)["positions"], [])

    def test_future_day_volume_does_not_change_fill_capacity(self):
        quantities = []
        for current_volume in (1, 10_000_000):
            account = self.ledger.create_account(f"vol-{current_volume}")
            self.ledger.order(account["id"], STOCK, "BUY", 200, "10.00",
                              stop_price="9.80", request_id=f"buy-{current_volume}")
            result = advance(
                self.ledger,
                account["id"], "2026-09-02",
                {STOCK: bar(volume=current_volume, capacity_volume=1000,
                            capacity_date="2026-09-01")},
                f"advance-{current_volume}",
            )
            quantities.append(sum(fill["quantity"] for fill in result["fills"]))
        self.assertEqual(quantities, [100, 100])

    def test_limit_up_and_bad_unheld_bar_expire_day_order_with_reason(self):
        order = self.make_buy()
        locked = bar(open_price="11.00", close="11.00", capacity_date="2026-09-01")
        result = advance(self.ledger, self.account_id, "2026-09-02", {STOCK: locked}, "locked")
        self.assertEqual(result["fills"], [])
        self.assertIn("locked_at_upper_limit", result["blocked_orders"][0]["reason"])
        self.assertEqual(self.ledger.account(self.account_id)["orders"][0]["status"], "expired")
        self.make_buy(request_id="adjusted-order")
        adjusted = bar(adjustment="qfq", capacity_date="2026-09-02")
        result = advance(self.ledger, self.account_id, "2026-09-03", {STOCK: adjusted}, "adjusted")
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["blocked_orders"][0]["reason"], "adjusted_bar_rejected")

    def test_bar_date_and_open_price_must_match_verified_session(self):
        self.make_buy(request_id="date-guard")
        mismatched = bar(capacity_date="2026-09-01")
        mismatched["date"] = "2026-09-03"
        with self.assertRaisesRegex(LedgerError, "does not match advance date"):
            advance(self.ledger, self.account_id, "2026-09-02",
                    {STOCK: mismatched}, "date-mismatch")
        self.assertEqual(self.ledger.account(self.account_id)["cursor_date"], "2026-09-01")
        out_of_band = bar(open_price="8.99", capacity_date="2026-09-02")
        result = advance(self.ledger, self.account_id, "2026-09-02",
                         {STOCK: out_of_band}, "out-of-band")
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["blocked_orders"][0]["reason"],
                         "open_outside_verified_price_limits")

    def test_previous_session_proof_rejects_skipped_account_session(self):
        self.make_buy(request_id="skip-session")
        skipped = bar(capacity_date="2026-09-04")
        skipped["date"] = "2026-09-07"
        skipped["previous_session_date"] = "2026-09-04"
        with self.assertRaisesRegex(LedgerError, "previous_session_date.*2026-09-01"):
            self.ledger.advance(self.account_id, "2026-09-07", {STOCK: skipped}, "skip-day")
        self.assertEqual(self.ledger.account(self.account_id)["cursor_date"], "2026-09-01")
        self.assertEqual(self.ledger.account(self.account_id)["orders"][0]["status"], "pending")

    def test_missing_held_bar_rolls_back_entire_advance(self):
        self.make_buy(request_id="held")
        advance(self.ledger, self.account_id, "2026-09-02",
                            {STOCK: bar(capacity_date="2026-09-01")}, "open")
        before = self.ledger.account(self.account_id)
        with self.assertRaises(LedgerError):
            advance(self.ledger, self.account_id, "2026-09-03", {}, "bad-roll")
        after = self.ledger.account(self.account_id)
        self.assertEqual(after["cursor_date"], before["cursor_date"])
        self.assertEqual(after["equity_curve"], before["equity_curve"])
        self.assertEqual(after["cash"], before["cash"])

    def test_bad_company_action_bar_rolls_back_held_account(self):
        self.make_buy(request_id="held-action")
        advance(self.ledger, self.account_id, "2026-09-02",
                            {STOCK: bar(capacity_date="2026-09-01")}, "open-action")
        with self.assertRaisesRegex(LedgerError, "corporate_action_unverified"):
            advance(
                self.ledger,
                self.account_id, "2026-09-03",
                {STOCK: bar(corporate_action_verified=False,
                            capacity_date="2026-09-02", close="9.00")}, "bad-action",
            )
        self.assertEqual(self.ledger.account(self.account_id)["cursor_date"], "2026-09-02")

    def test_invalid_held_close_rolls_back_before_mark_or_cursor_update(self):
        self.make_buy(request_id="bad-close")
        advance(self.ledger, self.account_id, "2026-09-02",
                {STOCK: bar(capacity_date="2026-09-01")}, "valid-open")
        before = self.ledger.account(self.account_id)
        invalid = bar(close="11.50", capacity_date="2026-09-02")
        with self.assertRaisesRegex(LedgerError, "bar_price_outside_verified_limits"):
            advance(self.ledger, self.account_id, "2026-09-03", {STOCK: invalid}, "bad-close-roll")
        after = self.ledger.account(self.account_id)
        self.assertEqual(after["cursor_date"], before["cursor_date"])
        self.assertEqual(after["equity_curve"], before["equity_curve"])
        self.assertEqual(after["positions"][0]["mark"], before["positions"][0]["mark"])

    def test_missing_mark_carries_forward_stale_and_ignores_later_override(self):
        self.make_buy(request_id="mark")
        advance(self.ledger, self.account_id, "2026-09-02",
                            {STOCK: bar(close="10.30", capacity_date="2026-09-01")}, "mark1")
        original = self.ledger.account(self.account_id)["positions"][0]["mark"]
        advance(self.ledger, self.account_id, "2026-09-03",
                            {STOCK: bar(close=None, capacity_date="2026-09-02")}, "mark2")
        carried = self.ledger.account(self.account_id, marks={STOCK: "20.00"})
        self.assertEqual(carried["positions"][0]["mark"], original)
        self.assertTrue(carried["positions"][0]["mark_stale"])
        self.assertIn(f"stale_mark:{STOCK}", carried["warnings"])

    def test_suspended_and_zero_volume_marks_carry_forward_as_stale(self):
        self.make_buy(request_id="suspension-mark")
        advance(self.ledger, self.account_id, "2026-09-02",
                {STOCK: bar(close="10.00", capacity_date="2026-09-01")}, "mark-before-halt")
        suspended = bar(close="9.50", volume=100, capacity_date="2026-09-02",
                        execution_allowed=False)
        suspended["status"] = "suspended"
        advance(self.ledger, self.account_id, "2026-09-03",
                {STOCK: suspended}, "suspended-session")
        zero_volume = bar(close="9.40", volume=0, capacity_date="2026-09-03")
        advance(self.ledger, self.account_id, "2026-09-04",
                {STOCK: zero_volume}, "zero-volume-session")
        state = self.ledger.account(self.account_id)
        self.assertEqual(state["cursor_date"], "2026-09-04")
        self.assertEqual(state["positions"][0]["mark"], "10.00")
        self.assertTrue(state["positions"][0]["mark_stale"])
        self.assertIn(f"stale_mark:{STOCK}", state["warnings"])
        self.assertEqual(state["equity_curve"][-1]["marks"][STOCK], "10.00")

    def test_drawdown_warning_and_optional_training_pause(self):
        self.make_buy(qty=1000, request_id="drawdown", stop="9.90")
        advance(self.ledger, self.account_id, "2026-09-02",
                            {STOCK: bar(open_price="10.00", close="10.00",
                        capacity_volume=10000, capacity_date="2026-09-01")}, "buy-dd")
        advance(self.ledger, self.account_id, "2026-09-03",
                            {STOCK: bar(open_price="9.00", close="9.00",
                        capacity_volume=10000, capacity_date="2026-09-02")}, "mark-dd")
        account = self.ledger.account(self.account_id)
        self.assertGreaterEqual(Decimal(account["drawdown"]), Decimal("0.05"))
        warned = self.make_buy(request_id="warned", price="9.00", stop="8.80")
        self.assertIn("drawdown_at_or_above_5_percent", warned["warnings"])

        paused = Ledger(Path(self.tmp.name) / "paused.sqlite3")
        paused_account = paused.create_account("pause", fees={"pause_on_drawdown": True})
        paused.order(paused_account["id"], STOCK, "BUY", 1000, "10.00", "9.90",
                     request_id="pause-buy")
        advance(paused, paused_account["id"], "2026-09-02",
                       {STOCK: bar(close="10.00", capacity_volume=10000,
                                   capacity_date="2026-09-01")}, "pause-d1")
        advance(paused, paused_account["id"], "2026-09-03",
                       {STOCK: bar(close="9.00", capacity_volume=10000,
                                   capacity_date="2026-09-02")}, "pause-d2")
        with self.assertRaises(RiskLimitError):
            paused.order(paused_account["id"], STOCK, "BUY", 100, "9.00", "8.80",
                         request_id="paused-order")

    def test_idempotent_advance_and_strict_cursor(self):
        self.make_buy(request_id="dedupe-order")
        bars = {STOCK: bar(capacity_date="2026-09-01")}
        first = advance(self.ledger, self.account_id, "2026-09-02", bars, "same-request")
        replay = advance(self.ledger, self.account_id, "2026-09-02", bars, "same-request")
        self.assertEqual(first, replay)
        changed = {STOCK: bar(open_price="10.01", capacity_date="2026-09-01")}
        with self.assertRaises(IdempotencyConflict):
            advance(self.ledger, self.account_id, "2026-09-02", changed, "same-request")
        with self.assertRaises(AlreadyAdvancedError):
            advance(self.ledger, self.account_id, "2026-09-02", bars, "different-request")

    def test_cancel_releases_pending_order_and_is_repeatable(self):
        order = self.make_buy(request_id="cancel-me")
        cancelled = self.ledger.cancel(self.account_id, order["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.ledger.cancel(self.account_id, order["id"])["status"], "cancelled")
        self.assertEqual(self.ledger.account(self.account_id)["fills"], [])

    def test_zero_denominators_return_none(self):
        stats = self.ledger.stats(self.account_id)
        for key in ("win_rate", "avg_win", "avg_loss", "payoff_ratio", "profit_factor", "avg_r"):
            self.assertIsNone(stats[key])
        self.assertTrue(stats["insufficient_samples"])


if __name__ == "__main__":
    unittest.main()
