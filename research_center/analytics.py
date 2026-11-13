"""Deterministic, read-only analytics for a saved research result.

This module derives report fields from the result's actual equity marks, fills,
and bars. It never rebuilds an equity curve or fabricates orders.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
import math
from typing import Any


_BENCHMARK_NAME = "标的价格指数（不含交易成本）"
_YEAR_DAYS = 365.2425
_TRADING_DAYS = 252.0
_TOL = 1e-8


def _number(value: Any, name: str, *, default: float | None = None) -> float:
    if value is None and default is not None:
        value = default
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    return number


def _timestamp(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} 不是有效的 ISO 日期时间") from exc
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat()


def _same_timezone_mode(timestamps: list[datetime]) -> None:
    modes = {item.utcoffset() is not None for item in timestamps}
    if len(modes) > 1:
        raise ValueError("输入日期时间不能混用带时区和不带时区的值")


def _close_enough(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-7 * max(1.0, abs(left), abs(right))


def _require_finite_tree(value: Any, path: str = "analytics") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} 产生非有限数值，拒绝输出")
    if isinstance(value, dict):
        for key, item in value.items():
            _require_finite_tree(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_finite_tree(item, f"{path}[{index}]")


def _normalize_equity(
    result: dict[str, Any], initial_cash: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows = result.get("equity")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("result.equity 缺失；不能从初始资金重建实际净值")

    dated: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise ValueError(f"equity[{index}] 必须是对象")
        cash = _number(raw.get("cash"), f"equity[{index}].cash")
        quantity = _number(
            raw.get("quantity", raw.get("qty")), f"equity[{index}].quantity"
        )
        value = _number(raw.get("equity"), f"equity[{index}].equity")
        if cash < -_TOL or quantity < -_TOL or value < -_TOL:
            raise ValueError("不支持负现金、负持仓或负权益")
        if quantity < 0:
            quantity = 0.0

        # An explicit null source_datetime marks the engine's opening ledger
        # row, even though that row also carries a display-only datetime.
        if raw.get("availability_datetime") is not None:
            stamp_value = raw["availability_datetime"]
        elif "source_datetime" in raw:
            stamp_value = raw.get("source_datetime")
        else:
            stamp_value = raw.get("datetime")
        row = {
            "cash": cash,
            "quantity": quantity,
            "equity": value,
            "_index": index,
        }
        if stamp_value is None:
            undated.append(row)
            continue
        stamp = _timestamp(stamp_value, f"equity[{index}].availability_datetime")
        if stamp in seen:
            raise ValueError("equity 中存在重复的可用时间")
        seen.add(stamp)
        row["_datetime"] = stamp
        dated.append(row)

    if len(undated) > 1:
        raise ValueError("equity 中存在多个无时间起始行")
    if undated:
        baseline = undated[0]
        if baseline["_index"] != 0:
            raise ValueError("无时间起始权益行必须位于equity首位")
        if not _close_enough(baseline["cash"], initial_cash) or not _close_enough(
            baseline["equity"], initial_cash
        ) or not _close_enough(baseline["quantity"], 0.0):
            raise ValueError("不支持非初始现金或非空仓的起始权益行")

    dated.sort(key=lambda row: (row["_datetime"], row["_index"]))
    return dated, undated


def _normalize_trades(
    result: dict[str, Any], accounting: str
) -> list[dict[str, Any]]:
    raw_rows = result.get("trades", [])
    if not isinstance(raw_rows, list):
        raise ValueError("result.trades 必须是列表")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise ValueError(f"trades[{index}] 必须是对象")
        side = str(raw.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"trades[{index}].side 仅支持 buy/sell；拒绝空头")
        quantity = _number(
            raw.get("quantity", raw.get("qty")), f"trades[{index}].quantity"
        )
        price = _number(raw.get("price"), f"trades[{index}].price")
        notional = _number(raw.get("notional"), f"trades[{index}].notional")
        for required_fee in ("commission", "stamp_duty", "slippage_cost"):
            if required_fee not in raw:
                raise ValueError(f"trades[{index}] 缺少{required_fee}，不能假设费用为零")
        commission = _number(
            raw.get("commission", 0.0), f"trades[{index}].commission"
        )
        stamp_duty = _number(
            raw.get("stamp_duty", 0.0), f"trades[{index}].stamp_duty"
        )
        slippage = _number(
            raw.get("slippage_cost", 0.0), f"trades[{index}].slippage_cost"
        )
        if quantity <= 0 or price <= 0 or notional <= 0:
            raise ValueError("成交数量、价格和成交额必须大于零")
        if commission < 0 or stamp_duty < 0 or slippage < 0:
            raise ValueError("成交费用不能为负数")
        if not _close_enough(notional, quantity * price):
            raise ValueError(f"trades[{index}] 的notional与quantity*price不一致")
        dt_value = raw.get("datetime", raw.get("fill_datetime"))
        if dt_value is None:
            raise ValueError(f"trades[{index}] 缺少成交时间")
        stamp = _timestamp(dt_value, f"trades[{index}].datetime")
        rows.append(
            {
                "side": side,
                "quantity": quantity,
                "price": price,
                "notional": notional,
                "commission": commission,
                "stamp_duty": stamp_duty,
                "slippage_cost": slippage,
                "datetime": stamp,
                "index": index,
                "source": raw,
                "cash_slippage": slippage if accounting == "separate_cash_debit" else 0.0,
            }
        )
    rows.sort(key=lambda row: (row["datetime"], row["index"]))
    return rows


def _normalize_bars(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = result.get("bars", [])
    if not isinstance(raw_rows, list):
        raise ValueError("result.bars 必须是列表")
    rows: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise ValueError(f"bars[{index}] 必须是对象")
        stamp_value = raw.get("source_datetime")
        if stamp_value is None:
            raise ValueError(f"bars[{index}] 缺少source_datetime")
        stamp = _timestamp(stamp_value, f"bars[{index}].source_datetime")
        if stamp in seen:
            raise ValueError("bars 中存在重复的source_datetime")
        seen.add(stamp)
        ohlc = {
            field: _number(raw.get(field), f"bars[{index}].{field}")
            for field in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in ohlc.values()):
            raise ValueError("benchmark OHLC必须为正数")
        if (
            ohlc["low"] > ohlc["high"]
            or ohlc["low"] > min(ohlc["open"], ohlc["close"])
            or ohlc["high"] < max(ohlc["open"], ohlc["close"])
        ):
            raise ValueError(f"bars[{index}] OHLC关系不成立")
        rows.append({"datetime": stamp, "close": ohlc["close"], "index": index})
    rows.sort(key=lambda row: (row["datetime"], row["index"]))
    return rows


def _reconcile_ledger(
    equity_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    initial_cash: float,
    accounting: str,
) -> None:
    """Reject unexplained initial holdings, oversells, or cash/quantity drift."""
    cash = initial_cash
    quantity = 0.0
    trade_index = 0
    for point in equity_rows:
        stamp = point["_datetime"]
        while trade_index < len(trades) and trades[trade_index]["datetime"] <= stamp:
            trade = trades[trade_index]
            quantity_before = quantity
            q = trade["quantity"]
            if trade["side"] == "buy":
                cash -= (
                    trade["notional"]
                    + trade["commission"]
                    + trade["stamp_duty"]
                    + trade["cash_slippage"]
                )
                quantity += q
            else:
                if q > quantity_before + 1e-7 * max(1.0, quantity_before):
                    raise ValueError("成交记录出现超卖；不支持空头或初始持仓")
                cash += (
                    trade["notional"]
                    - trade["commission"]
                    - trade["stamp_duty"]
                    - trade["cash_slippage"]
                )
                quantity -= q
                if abs(quantity) <= 1e-7:
                    quantity = 0.0
            if cash < -1e-7:
                raise ValueError("成交现金流导致负现金；拒绝分析")
            trade_index += 1
        if not _close_enough(point["cash"], cash) or not _close_enough(
            point["quantity"], quantity
        ):
            raise ValueError("equity现金/持仓与成交账本不一致")
    if equity_rows and trade_index != len(trades):
        raise ValueError("成交时间晚于最后一条实际权益记录")


def _fifo_closed_trades(
    trades: list[dict[str, Any]], accounting: str
) -> tuple[list[dict[str, Any]], float]:
    lots: deque[dict[str, Any]] = deque()
    closed: list[dict[str, Any]] = []
    realized_total = 0.0
    for trade in trades:
        q = trade["quantity"]
        slip = trade["cash_slippage"]
        if trade["side"] == "buy":
            lots.append(
                {
                    "datetime": trade["datetime"],
                    "quantity": q,
                    "remaining": q,
                    "price": trade["price"],
                    "notional_remaining": trade["notional"],
                    "commission_remaining": trade["commission"],
                    "stamp_duty_remaining": trade["stamp_duty"],
                    "slippage_remaining": slip,
                }
            )
            continue

        available = math.fsum(lot["remaining"] for lot in lots)
        if q > available + 1e-7 * max(1.0, available):
            raise ValueError("成交记录出现超卖；无法进行FIFO配对")
        sell_net_per_share = (
            trade["notional"]
            - trade["commission"]
            - trade["stamp_duty"]
            - slip
        ) / q
        remaining_to_match = q
        while remaining_to_match > 1e-9:
            if not lots:
                raise ValueError("成交记录出现超卖；无法进行FIFO配对")
            lot = lots[0]
            matched = min(remaining_to_match, lot["remaining"])
            fraction = matched / lot["remaining"]
            buy_notional = lot["notional_remaining"] * fraction
            buy_commission = lot["commission_remaining"] * fraction
            buy_stamp = lot["stamp_duty_remaining"] * fraction
            buy_slippage = lot["slippage_remaining"] * fraction
            buy_cost = buy_notional + buy_commission + buy_stamp + buy_slippage
            sell_notional = trade["notional"] * matched / q
            sell_commission = trade["commission"] * matched / q
            sell_stamp = trade["stamp_duty"] * matched / q
            sell_slippage = slip * matched / q
            sell_proceeds = sell_net_per_share * matched
            pnl = sell_proceeds - buy_cost
            holding_seconds = (
                trade["datetime"] - lot["datetime"]
            ).total_seconds()
            if holding_seconds < 0:
                raise ValueError("FIFO成交时间倒序")
            closed.append(
                {
                    "buy_datetime": _iso(lot["datetime"]),
                    "sell_datetime": _iso(trade["datetime"]),
                    "quantity": matched,
                    "buy_price": lot["price"],
                    "sell_price": trade["price"],
                    "buy_notional": buy_notional,
                    "sell_notional": sell_notional,
                    "buy_commission": buy_commission,
                    "buy_stamp_duty": buy_stamp,
                    "buy_slippage_cost": buy_slippage,
                    "sell_commission": sell_commission,
                    "sell_stamp_duty": sell_stamp,
                    "sell_slippage_cost": sell_slippage,
                    "buy_cost_including_allocated_fees": buy_cost,
                    "sell_proceeds_after_allocated_fees": sell_proceeds,
                    "net_realized_pnl": pnl,
                    "return_on_cost": pnl / buy_cost if buy_cost > 0 else None,
                    "holding_minutes": holding_seconds / 60.0,
                    "holding_days": holding_seconds / 86_400.0,
                }
            )
            realized_total += pnl
            lot["remaining"] -= matched
            lot["notional_remaining"] -= buy_notional
            lot["commission_remaining"] -= buy_commission
            lot["stamp_duty_remaining"] -= buy_stamp
            lot["slippage_remaining"] -= buy_slippage
            remaining_to_match -= matched
            if lot["remaining"] <= 1e-9:
                lots.popleft()
    open_quantity = math.fsum(lot["remaining"] for lot in lots)
    return closed, open_quantity


def _period_rows(daily: list[dict[str, Any]], key_format: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        day = date.fromisoformat(row["date"])
        period = day.strftime(key_format)
        grouped[period].append(row)
    output = []
    for period, rows in sorted(grouped.items()):
        growth = 1.0
        for row in rows:
            growth *= 1.0 + row["return"]
        output.append(
            {
                "period": period,
                "return": growth - 1.0,
                # The vendor calendar/coverage has not been certified, so even
                # a period at the dataset boundary cannot be called complete.
                "partial": True,
                "observed_days": len(rows),
                "start": rows[0]["date"],
                "end": rows[-1]["date"],
            }
        )
    return output


def _benchmark(bars: list[dict[str, Any]], initial_cash: float) -> dict[str, Any]:
    if not bars:
        return {
            "kind": "security_price_index",
            "name": _BENCHMARK_NAME,
            "status": "unavailable",
            "points": [],
            "total_return": None,
            "reason": "result.bars 未提供可用的 source_datetime OHLC 数据",
            "price_basis_confirmed": False,
            "costs_included": False,
            "executable": False,
        }
    first_close = bars[0]["close"]
    points = [
        {
            "datetime": _iso(row["datetime"]),
            "value": initial_cash * row["close"] / first_close,
        }
        for row in bars
    ]
    return {
        "kind": "security_price_index",
        "name": _BENCHMARK_NAME,
        "status": "available",
        "points": points,
        "total_return": points[-1]["value"] / initial_cash - 1.0,
        "reason": None,
        "price_basis_confirmed": False,
        "price_basis": "来源复权/价格基准未认证",
        "costs_included": False,
        "executable": False,
    }


def build_analytics(config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Build JSON-safe report analytics without modifying ``result``.

    ``closed_trades`` counts FIFO buy-lot/sell-fill matched quantity slices,
    not orders or fill records. Reference slippage is already in fill prices;
    only an explicit VeighNa ``separate_cash_debit`` manifest adds it to costs.
    """
    if not isinstance(config, dict) or not isinstance(result, dict):
        raise ValueError("config 和 result 必须是对象")
    result_metrics = result.get("metrics", {})
    if not isinstance(result_metrics, dict):
        result_metrics = {}
    initial_cash = _number(
        config.get("initial_cash", result_metrics.get("initial_cash")),
        "initial_cash",
    )
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须大于零")
    manifest = result.get("data_manifest") or {}
    if not isinstance(manifest, dict):
        raise ValueError("result.data_manifest 必须是对象")
    accounting_tag = manifest.get("slippage_cost_accounting")
    if accounting_tag not in (None, "separate_cash_debit"):
        raise ValueError("不支持的slippage_cost_accounting契约")
    backend = manifest.get("backend") or result.get("backend") or config.get(
        "backend", "reference"
    )
    if backend not in {"reference", "vnpy", "isolated_python"}:
        raise ValueError("不支持的回测backend")
    if backend == "isolated_python":
        custom = result.get("custom_execution", {})
        if (custom.get("engine") != "research_center.custom_execution.v2"
                or custom.get("mode") != "RESEARCH_ONLY" or custom.get("paper_admission") is not False
                or accounting_tag is not None):
            raise ValueError("隔离 Python 结果缺少共享撮合契约")
    if backend == "vnpy" and accounting_tag != "separate_cash_debit":
        raise ValueError("VeighNa结果缺少独立滑点现金扣款契约，拒绝推断")
    accounting = (
        "separate_cash_debit"
        if accounting_tag == "separate_cash_debit"
        else "reference_price_embedded"
    )

    equity_rows, _undated = _normalize_equity(result, initial_cash)
    trades = _normalize_trades(result, accounting)
    bars = _normalize_bars(result)
    all_timestamps = [
        row["_datetime"] for row in equity_rows
    ] + [row["datetime"] for row in trades] + [row["datetime"] for row in bars]
    _same_timezone_mode(all_timestamps)
    _reconcile_ledger(equity_rows, trades, initial_cash, accounting)
    if trades and not equity_rows:
        raise ValueError("存在成交但没有带时间的实际权益记录，无法核对账本")
    closed, open_quantity = _fifo_closed_trades(trades, accounting)
    if equity_rows and not _close_enough(equity_rows[-1]["quantity"], open_quantity):
        raise ValueError("期末FIFO未平仓数量与实际权益持仓不一致")

    ending_equity = (
        equity_rows[-1]["equity"] if equity_rows else initial_cash
    )
    total_return = ending_equity / initial_cash - 1.0
    peak = initial_cash
    drawdowns_by_stamp: dict[datetime, float] = {}
    for point in equity_rows:
        peak = max(peak, point["equity"])
        drawdowns_by_stamp[point["_datetime"]] = (
            point["equity"] / peak - 1.0 if peak > 0 else None
        )
    max_drawdown = min(drawdowns_by_stamp.values(), default=0.0)

    daily_last: dict[date, dict[str, Any]] = {}
    for point in equity_rows:
        day = point["_datetime"].date()
        daily_last[day] = point
    daily_returns: list[dict[str, Any]] = []
    prior_equity = initial_cash
    daily_observations: list[float] = []
    for day in sorted(daily_last):
        point = daily_last[day]
        if prior_equity <= 0:
            raise ValueError("前一日权益非正，无法计算简单日收益")
        daily_return = point["equity"] / prior_equity - 1.0
        daily_observations.append(daily_return)
        daily_returns.append(
            {
                "date": day.isoformat(),
                "equity": point["equity"],
                "cash": point["cash"],
                "quantity": point["quantity"],
                "return": daily_return,
                "drawdown": drawdowns_by_stamp[point["_datetime"]],
            }
        )
        prior_equity = point["equity"]

    downside_rms = None
    sortino = None
    if len(daily_observations) > 1:
        downside_rms = math.sqrt(
            math.fsum(min(value, 0.0) ** 2 for value in daily_observations)
            / len(daily_observations)
        )
        if downside_rms > 0:
            sortino = (
                math.fsum(daily_observations)
                / len(daily_observations)
                / downside_rms
                * math.sqrt(_TRADING_DAYS)
            )

    observed_times = [point["_datetime"] for point in equity_rows]
    elapsed_days = (
        (observed_times[-1] - observed_times[0]).total_seconds() / 86_400.0
        if len(observed_times) > 1
        else 0.0
    )
    annual_return = None
    if elapsed_days >= _YEAR_DAYS and ending_equity > 0:
        annual_return = (ending_equity / initial_cash) ** (_YEAR_DAYS / elapsed_days) - 1.0
    calmar = (
        annual_return / abs(max_drawdown)
        if annual_return is not None and max_drawdown < 0
        else None
    )

    average_daily_equity = (
        math.fsum(row["equity"] for row in daily_returns) / len(daily_returns)
        if daily_returns
        else None
    )
    gross_notional = math.fsum(row["notional"] for row in trades)
    turnover = (
        0.0
        if gross_notional == 0
        else (
            gross_notional / average_daily_equity
            if average_daily_equity is not None and average_daily_equity > 0
            else None
        )
    )
    commission = math.fsum(row["commission"] for row in trades)
    stamp_duty = math.fsum(row["stamp_duty"] for row in trades)
    slippage_cost = math.fsum(row["slippage_cost"] for row in trades)
    slippage_cash_debit = (
        slippage_cost if accounting == "separate_cash_debit" else 0.0
    )

    pnls = [row["net_realized_pnl"] for row in closed]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    zeroes = len(pnls) - len(winners) - len(losers)
    gross_profit = math.fsum(winners)
    gross_loss = abs(math.fsum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    win_loss_ratio = (
        (math.fsum(winners) / len(winners))
        / abs(math.fsum(losers) / len(losers))
        if winners and losers
        else None
    )
    win_rate = len(winners) / len(closed) if closed else None
    average_holding_minutes = (
        math.fsum(row["holding_minutes"] for row in closed) / len(closed)
        if closed
        else None
    )
    fees_paid = commission + stamp_duty

    metric_notes: dict[str, str] = {}
    if sortino is None:
        metric_notes["sortino"] = (
            "Sortino需要至少两个日收益观察且downside RMS大于0；否则未定义"
        )
    if annual_return is None:
        metric_notes["annual_return"] = "可用实际权益观察跨度不足365.2425天或期末权益非正"
    if calmar is None:
        metric_notes["calmar"] = "需要可用年化收益且最大回撤绝对值大于0"
    if win_rate is None:
        metric_notes["win_rate"] = "没有FIFO已平仓配对"
    if profit_factor is None:
        metric_notes["profit_factor"] = "没有亏损配对作为分母；不以无穷大代替未定义"
    if win_loss_ratio is None:
        metric_notes["profit_loss_ratio"] = "缺少盈利或亏损FIFO配对，无法计算平均盈亏比"
    if turnover is None:
        metric_notes["turnover"] = "存在成交额但没有正的平均每日实际权益"
    if average_holding_minutes is None:
        metric_notes["average_holding_minutes"] = "没有FIFO已平仓配对"

    metrics = {
        "initial_cash": initial_cash,
        "ending_equity": ending_equity,
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_return_note": "观察跨度不足365.2425天时不年化",
        "elapsed_calendar_days": elapsed_days,
        "max_drawdown": max_drawdown,
        "sortino": sortino,
        "sortino_mar_daily": 0.0,
        "sortino_downside_rms": downside_rms,
        "sortino_frequency": "daily",
        "sortino_annualization_factor": math.sqrt(_TRADING_DAYS),
        "daily_return_observations": len(daily_observations),
        "calmar": calmar,
        "gross_traded_notional": gross_notional,
        "average_daily_equity": average_daily_equity,
        "turnover": turnover,
        "turnover_annualized": False,
        "turnover_sides": "买入与卖出成交额均计入",
        "commission": commission,
        "stamp_duty": stamp_duty,
        "fees_paid": fees_paid,
        "slippage_cost": slippage_cost,
        "slippage_cash_debit": slippage_cash_debit,
        "slippage_accounting": accounting,
        "closed_pair_count": len(closed),
        "winning_pair_count": len(winners),
        "losing_pair_count": len(losers),
        "zero_pnl_pair_count": zeroes,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "profit_loss_ratio": win_loss_ratio,
        "gross_realized_profit": gross_profit,
        "gross_realized_loss": gross_loss,
        "net_realized_pnl": math.fsum(pnls),
        "average_closed_pair_pnl": math.fsum(pnls) / len(pnls) if pnls else None,
        "average_holding_minutes": average_holding_minutes,
        "average_holding_days": (
            average_holding_minutes / 1_440.0
            if average_holding_minutes is not None
            else None
        ),
        "open_quantity": open_quantity,
        "trade_count_basis": "FIFO买入批次与卖出成交的数量配对，不是委托数或成交单数",
        "metric_notes": metric_notes,
    }

    limitations = list(result.get("limitations", [])) if isinstance(
        result.get("limitations", []), list
    ) else []
    limitations.extend(
        [
            "月度和年度收益按所选窗口内可见日收益复合；交易日历/数据覆盖未经认证，所有期间均标记partial=true。",
            "benchmark仅由同份bar的close构成价格指数；价格复权基准未认证，不含交易成本，也不是可成交买入并持有订单。",
            "reference后端的slippage_cost是已反映在填单价格内的估值，不再次扣减权益或FIFO现金流。",
            "交易统计按FIFO平仓数量配对；开仓未平数量不计胜负，也不等同于委托数或成交单数。",
        ]
    )
    unique_limitations = list(dict.fromkeys(str(item) for item in limitations))

    analytics = {
        "schema_version": 1,
        "status": "available",
        "metrics": metrics,
        "daily_returns": daily_returns,
        "monthly_returns": _period_rows(daily_returns, "%Y-%m"),
        "yearly_returns": _period_rows(daily_returns, "%Y"),
        "closed_trades": closed,
        "benchmark": _benchmark(bars, initial_cash),
        "methodology": {
            "equity_source": "直接使用result.equity的实际cash/quantity/equity，不重建净值曲线",
            "availability_time": "优先availability_datetime，其次source_datetime；仅无source_datetime字段时才使用datetime",
            "daily_return": "每日最后一条实际equity相对前日最后权益；首个观察日相对initial_cash，因此包含首日成本",
            "drawdown": "以initial_cash及其后的每条实际权益记录更新历史峰值计算",
            "monthly_yearly_return": "对所选窗口内对应期间的日收益连乘；因交易日历未认证统一partial=true",
            "sortino": "MAR=0；downside RMS=sqrt(mean(min(daily_return,0)^2))，分母平均包括所有日收益观察；按sqrt(252)年化",
            "annual_return_and_calmar": "仅当实际权益首末观察跨度至少365.2425个日历日时计算年化；否则为None",
            "turnover": "所有买卖成交notional之和 / 平均每日最后权益；双边、不年化",
            "closed_trade_pairing": "空头和初始持仓不支持；卖出按FIFO消耗买入批次，部分费用按匹配股数分摊",
            "slippage": "reference只展示内含于价格的slippage_cost估值；manifest声明separate_cash_debit时独立计入现金/FIFO成本",
            "benchmark": "同份bars按source_datetime排序，以首个close归一化至initial_cash；只比较价格，不模拟订单",
            "undefined_metrics": "未定义数值为JSON null，并在metrics.metric_notes说明原因",
        },
        "limitations": unique_limitations,
    }
    _require_finite_tree(analytics)
    return analytics
