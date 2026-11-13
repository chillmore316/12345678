"""Bounded, local-only minute research runner for one Shanghai/Shenzhen stock.

The engine is intentionally research-only. It reads only the requested
2024-2025 monthly JoinQuant batches, reuses the existing data canonicalizer,
and never writes to the raw data lake.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import math
from pathlib import Path
import re
import stat
from typing import Any, Callable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from data.joinquant_minute import (
    REQUIRED_SOURCE_COLUMNS,
    canonicalize_frame,
    inspect_monthly_source,
    jq_to_vt,
    vt_to_jq,
)
from .strategies import STRATEGIES, BuyHoldStrategy, SmaCrossStrategy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "闲鱼商品_A股1分钟数据_聚宽版" / "01_数据文件"
TRAIN_START = date(2024, 1, 1)
TRAIN_END = date(2025, 12, 31)
MAX_CALENDAR_DAYS = 31
LOT_SIZE = 100
MAX_ENGINE_ROWS = MAX_CALENDAR_DAYS * 240 + 240
TRAINING_SELL_STAMP_DUTY_RATE = 0.0005


def _parse_date(value: Any, field: str) -> date:
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
    if pd.isna(parsed) or parsed.tzinfo is not None:
        raise ValueError(f"{field} 必须是无时区日期")
    result = parsed.date()
    return result


def _normalize_stock_symbol(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol 必须是带交易所后缀的单个 A 股代码")
    try:
        jq_code = vt_to_jq(value)
        vt_code = jq_to_vt(jq_code)
    except ValueError as exc:
        raise ValueError("symbol 后缀须为 .XSHE/.XSHG 或 .SZSE/.SSE") from exc
    code, exchange = vt_code.split(".", 1)
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("symbol 代码必须为 6 位数字")
    # Only main-board A-share prefixes are in scope. Do not guess ETF, B-share,
    # ChiNext or STAR eligibility from a six-digit code.
    if exchange == "SZSE" and not code.startswith(("000", "001", "002", "003")):
        raise ValueError("目前仅支持深市主板代码前缀 000/001/002/003")
    if exchange == "SSE" and not code.startswith(("600", "601", "603", "605")):
        raise ValueError("目前仅支持沪市主板代码前缀 600/601/603/605")
    return vt_code


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a single-stock, one-minute, frozen-train request."""
    if not isinstance(config, dict):
        raise ValueError("config 必须是字典")
    symbol = _normalize_stock_symbol(config.get("symbol"))
    start = _parse_date(config.get("start"), "start")
    end = _parse_date(config.get("end"), "end")
    if end < start:
        raise ValueError("end 必须不早于 start")
    if (end - start).days + 1 > MAX_CALENDAR_DAYS:
        raise ValueError("日期范围最多 31 个自然日")
    if start < TRAIN_START or end > TRAIN_END:
        raise ValueError("仅允许 2024-2025 冻结训练期；不能读取 2026 验证期")
    interval = str(config.get("interval", "1m")).lower()
    if interval != "1m":
        raise ValueError("interval 目前只支持 '1m'")
    strategy_name = str(config.get("strategy", "buy_hold")).lower()
    if strategy_name not in STRATEGIES:
        raise ValueError("strategy 只能是 buy_hold 或 sma_cross")

    initial_cash = float(config.get("initial_cash", 100_000.0))
    commission_rate = float(config.get("commission_rate", 0.00025))
    min_commission = float(config.get("min_commission", 5.0))
    slippage_bps = float(config.get("slippage_bps", 5.0))
    for name, value in (
        ("initial_cash", initial_cash),
        ("commission_rate", commission_rate),
        ("min_commission", min_commission),
        ("slippage_bps", slippage_bps),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} 必须是有限数值")
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须大于 0")
    if commission_rate < 0 or min_commission < 0 or slippage_bps < 0:
        raise ValueError("commission_rate、min_commission 和 slippage_bps 不能为负数")

    params = config.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params 必须是字典")
    allowed_params = {
        "target_fraction",
        "max_turnover_participation",
        "fast",
        "slow",
        "fast_window",
        "slow_window",
    }
    unknown = set(params) - allowed_params
    if unknown:
        raise ValueError(f"params 包含不支持的字段：{sorted(unknown)}")
    if "fast" in params and "fast_window" in params and params["fast"] != params["fast_window"]:
        raise ValueError("params.fast 与 params.fast_window 取值不一致")
    if "slow" in params and "slow_window" in params and params["slow"] != params["slow_window"]:
        raise ValueError("params.slow 与 params.slow_window 取值不一致")
    raw_fast = params.get("fast", params.get("fast_window", 5))
    raw_slow = params.get("slow", params.get("slow_window", 20))
    if isinstance(raw_fast, bool) or isinstance(raw_slow, bool):
        raise ValueError("SMA 的 fast/slow 必须是整数")
    if int(raw_fast) != float(raw_fast) or int(raw_slow) != float(raw_slow):
        raise ValueError("SMA 的 fast/slow 必须是整数")
    normalized_params = {
        "target_fraction": float(params.get("target_fraction", 0.5)),
        "max_turnover_participation": float(params.get("max_turnover_participation", 0.01)),
        "fast": int(raw_fast),
        "slow": int(raw_slow),
    }
    target_fraction = normalized_params["target_fraction"]
    participation = normalized_params["max_turnover_participation"]
    if not math.isfinite(target_fraction) or not 0 < target_fraction <= 1:
        raise ValueError("params.target_fraction 必须在 (0, 1] 内")
    if not math.isfinite(participation) or not 0 < participation <= 0.10:
        raise ValueError("params.max_turnover_participation 必须在 (0, 0.10] 内")
    if normalized_params["fast"] <= 0 or normalized_params["slow"] <= 1:
        raise ValueError("params.fast 必须大于 0，params.slow 必须大于 1")
    if normalized_params["fast"] >= normalized_params["slow"]:
        raise ValueError("params.fast 必须小于 params.slow")
    if normalized_params["slow"] > MAX_ENGINE_ROWS - 1:
        raise ValueError("params.slow 超过 31 日分钟窗口上限")

    return {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": interval,
        "strategy": strategy_name,
        "initial_cash": initial_cash,
        "commission_rate": commission_rate,
        "min_commission": min_commission,
        "slippage_bps": slippage_bps,
        "stamp_duty_rate": TRAINING_SELL_STAMP_DUTY_RATE,
        "params": normalized_params,
        "data_root": str(Path(config.get("data_root") or DEFAULT_DATA_ROOT).expanduser().resolve()),
    }


def _month_keys(start: date, end: date) -> list[str]:
    current = pd.Timestamp(start).to_period("M")
    final = pd.Timestamp(end).to_period("M")
    months: list[str] = []
    while current <= final:
        months.append(str(current))
        current += 1
    return months


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    value = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise ValueError(f"minute source must be a regular non-symlink file: {path}")
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _sha256_consistent(path: Path, expected_identity: tuple[int, int, int, int, int, int]) -> str:
    before = _file_identity(path)
    if before != expected_identity:
        raise RuntimeError(f"minute source changed after inspection/read: {path}")
    digest = _sha256(path)
    after = _file_identity(path)
    if after != before:
        raise RuntimeError(f"minute source changed while hashing: {path}")
    return digest


def _load_training_bars(config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read only requested-month metadata and matching-symbol row groups."""
    root = Path(config["data_root"])
    if not root.is_dir():
        raise FileNotFoundError(f"本地分钟数据目录不存在：{root}")
    start = pd.Timestamp(config["start"])
    end = pd.Timestamp(config["end"])
    end_exclusive = end + pd.Timedelta(days=1)
    jq_code = vt_to_jq(config["symbol"])

    grouped: dict[tuple[str, int], list[Any]] = defaultdict(list)
    inspected_paths: list[str] = []
    inspected_identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    for month in _month_keys(start.date(), end.date()):
        # This filename filter is deliberately before Parquet footer inspection:
        # no held-out month metadata is opened by this loader.
        for path in sorted(root.glob(f"stock_1m_{month}_batch*.parquet")):
            identity_before = _file_identity(path)
            source = inspect_monthly_source(path)
            identity_after = _file_identity(path)
            if identity_after != identity_before:
                raise RuntimeError(f"minute source changed while reading metadata: {path}")
            grouped[(source.month, source.batch)].append(source)
            resolved = str(path.resolve())
            inspected_paths.append(resolved)
            inspected_identities[resolved] = identity_after

    canonical = []
    shadowed = []
    for key in sorted(grouped):
        candidates = sorted(grouped[key], key=lambda item: item.priority, reverse=True)
        canonical.append(candidates[0])
        shadowed.extend(candidates[1:])

    frames: list[pd.DataFrame] = []
    used_paths: list[Path] = []
    raw_rows = 0
    all_nan_rows = 0
    for source in canonical:
        if not (source.min_code <= jq_code <= source.max_code):
            continue
        source_path = source.path.resolve()
        identity = inspected_identities[str(source_path)]
        if _file_identity(source_path) != identity:
            raise RuntimeError(f"minute source changed after footer inspection: {source_path}")
        table = pq.read_table(
            source.path,
            columns=sorted(REQUIRED_SOURCE_COLUMNS),
            filters=[
                ("code", "=", jq_code),
                ("time", ">=", start.to_pydatetime()),
                ("time", "<", end_exclusive.to_pydatetime()),
            ],
        )
        if not table.num_rows:
            continue
        if _file_identity(source_path) != identity:
            raise RuntimeError(f"minute source changed while reading selected rows: {source_path}")
        frame = table.to_pandas()
        raw_rows += len(frame)
        unavailable = frame[["open", "high", "low", "close", "volume", "money"]].isna().all(axis=1)
        all_nan_rows += int(unavailable.sum())
        frames.append(frame)
        used_paths.append(source_path)

    if not frames:
        raise FileNotFoundError(
            f"本地分钟数据中未找到 {jq_code} 在 {start.date()} 至 {end.date()} 的记录"
        )
    raw = pd.concat(frames, ignore_index=True)
    canonical_frame = canonicalize_frame(raw)
    canonical_frame = canonical_frame.loc[
        (canonical_frame["datetime"] >= start)
        & (canonical_frame["datetime"] < end_exclusive)
    ].sort_values(["datetime", "source_datetime"]).reset_index(drop=True)
    if canonical_frame.empty:
        raise FileNotFoundError(
            f"{jq_code} 在 {start.date()} 至 {end.date()} 的分钟记录均不可用"
        )
    if len(canonical_frame) > MAX_ENGINE_ROWS:
        raise ValueError(
            f"requested data returned {len(canonical_frame)} bars; bounded limit is {MAX_ENGINE_ROWS}"
        )
    numeric_columns = ["open", "high", "low", "close", "volume", "turnover"]
    numeric_values = canonical_frame[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(numeric_values).all():
        raise ValueError("minute data contains non-finite OHLC/volume/turnover values")
    source_times = canonical_frame["source_datetime"]
    if source_times.dt.tz is not None or source_times.dt.second.ne(0).any() or source_times.dt.microsecond.ne(0).any():
        raise ValueError("source timestamps must be timezone-naive whole minutes")
    labels = source_times.dt.strftime("%H:%M")
    valid_session = (
        labels.between("09:31", "11:30")
        | labels.between("13:01", "15:00")
    )
    if not valid_session.all():
        raise ValueError("minute data contains a timestamp outside the A-share continuous sessions")

    source_hashes = [
        {
            "path": str(path),
            "sha256": _sha256_consistent(path, inspected_identities[str(path)]),
            "identity_verified": True,
        }
        for path in sorted(set(used_paths))
    ]
    manifest = {
        "read_only": True,
        "research_only": True,
        "data_root": str(root),
        "requested_symbol": config["symbol"],
        "joinquant_symbol": jq_code,
        "requested_range_inclusive": [config["start"], config["end"]],
        "requested_months_only": _month_keys(start.date(), end.date()),
        "metadata_inspected_file_count": len(inspected_paths),
        "metadata_inspected_paths": inspected_paths,
        "canonical_files_considered": len(canonical),
        "shadowed_revisions": [str(item.path.resolve()) for item in shadowed],
        "used_source_files": source_hashes,
        "raw_selected_rows": raw_rows,
        "all_nan_unavailable_rows": all_nan_rows,
        "canonical_rows": len(canonical_frame),
        "valid_dates": int(canonical_frame["source_datetime"].dt.normalize().nunique()),
        "timestamp_contract": {
            "source_time": "JoinQuant bar-end timestamp; used as signal availability time",
            "canonical_datetime": "bar-start after adapter shift of -1 minute",
            "timezone": "Asia/Shanghai naive source timestamps",
        },
        "price_adjustment": "vendor_qfq_vintage_unresolved",
        "volume_semantics": "adjustment-scaled units not verified as raw shares; used only to reject zero-volume fill bars",
        "turnover_capacity_semantics": "turnover field used only as a conservative research proxy; units/vintage are not certified",
        "stamp_duty_rate": TRAINING_SELL_STAMP_DUTY_RATE,
        "source_identity_check": "device/inode/mode/size/mtime_ns/ctime_ns stable across footer, selected read, and SHA256",
    }
    return canonical_frame, manifest


def _round_adverse_price(reference: float, side: str, slippage_bps: float) -> float:
    raw = reference * (1.0 + (1.0 if side == "buy" else -1.0) * slippage_bps / 10_000.0)
    ticks = raw / 0.01
    if side == "buy":
        return max(0.01, math.ceil(ticks - 1e-12) * 0.01)
    return max(0.01, math.floor(ticks + 1e-12) * 0.01)


def _reject(order: dict[str, Any], reason: str) -> None:
    order["remaining_quantity"] = max(
        0, int(order["requested_quantity"]) - int(order["filled_quantity"])
    )
    order["status"] = "partially_cancelled" if order["filled_quantity"] > 0 else "rejected"
    order["reason"] = reason


def _make_strategy(config: dict[str, Any]):
    if config["strategy"] == "buy_hold":
        return BuyHoldStrategy(config["symbol"])
    params = config["params"]
    return SmaCrossStrategy(
        config["symbol"], params["fast"], params["slow"]
    )


def _daily_returns(equity: list[dict[str, Any]], initial_cash: float) -> list[float]:
    daily_last: dict[str, float] = {}
    for point in equity:
        if point.get("source_datetime") is None:
            continue
        day = point["source_datetime"][:10]
        daily_last[day] = float(point["equity"])
    returns = []
    previous = initial_cash
    for day in sorted(daily_last):
        current = daily_last[day]
        returns.append(current / previous - 1.0)
        previous = current
    return returns


def _result(
    config: dict[str, Any],
    equity: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    final_position: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    data_manifest: dict[str, Any],
    *,
    cancelled: bool = False,
) -> dict[str, Any]:
    initial_cash = config["initial_cash"]
    values = [float(item["equity"]) for item in equity] or [initial_cash]
    ending_equity = values[-1]
    total_return = ending_equity / initial_cash - 1.0
    peak = initial_cash
    drawdowns = []
    for value in values:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1.0 if peak else 0.0)
    if bars:
        first = pd.Timestamp(bars[0]["source_datetime"])
        last = pd.Timestamp(bars[-1]["source_datetime"])
        elapsed_days = max((last - first).total_seconds() / 86_400.0, 0.0)
    else:
        elapsed_days = 0.0
    annual_return = (
        (ending_equity / initial_cash) ** (365.2425 / elapsed_days) - 1.0
        if ending_equity > 0 and elapsed_days >= 365.2425
        else None
    )
    daily = _daily_returns(equity, initial_cash)
    daily_std = pd.Series(daily, dtype=float).std(ddof=1) if len(daily) > 1 else float("nan")
    sharpe = (
        float(pd.Series(daily, dtype=float).mean() / daily_std * math.sqrt(252.0))
        if len(daily) > 1 and math.isfinite(daily_std) and daily_std > 0
        else None
    )
    metrics = {
        "initial_cash": initial_cash,
        "ending_equity": ending_equity,
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_return_note": "omitted for ranges shorter than one calendar year",
        "max_drawdown": min(drawdowns) if drawdowns else 0.0,
        "sharpe": sharpe,
        "sharpe_frequency": "daily",
        "daily_return_observations": len(daily),
        "total_trades": len(trades),
        "total_fills": len(trades),
        "filled_orders": sum(item["status"] == "filled" for item in orders),
        "partial_or_pending_orders": sum(
            item["status"] in {"partial_filled", "pending", "partially_cancelled"} for item in orders
        ),
        "rejected_orders": sum(item["status"] == "rejected" for item in orders),
        "minimum_cash": min((float(item["cash"]) for item in equity), default=initial_cash),
        "negative_cash": any(float(item["cash"]) < -1e-8 for item in equity),
        "fees_paid": sum(
            float(item.get("commission", 0.0)) + float(item.get("stamp_duty", 0.0))
            for item in trades
        ),
        "cancelled": cancelled,
    }
    limitations = [
        "RESEARCH_ONLY; no paper or live admission is implied.",
        "The frozen workflow has no selected route and validation_allowed=false; this engine only supports bounded training-period probes.",
        "Minute price adjustment vintage is unresolved; results are not a certified qfq replay.",
        "The source does not provide point-in-time ST status; this engine cannot guarantee historical ST exclusion.",
        "Share-volume units and turnover scale are uncertified; turnover is only a conservative proxy cap, not formal capacity evidence.",
        "The supported code prefixes do not establish full A-share listing, corporate-action, limit-price, or delisting eligibility.",
        "Training-period sell stamp duty is fixed at 0.05%; exchange fees, borrow costs, and broker-specific fills are not modeled.",
        "A one-minute source-time gap, zero-volume bar, or one-price bar rejects the pending order; no forward fill or delayed backfill is used.",
    ]
    return {
        "metrics": metrics,
        "equity": equity,
        "orders": orders,
        "trades": trades,
        "positions": [final_position] if final_position and final_position["quantity"] > 0 else [],
        "bars": bars,
        "data_manifest": data_manifest,
        "limitations": limitations,
    }


def run_backtest(
    config: dict[str, Any],
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a bounded one-symbol event replay with next-contiguous-minute fills."""
    normalized = validate_config(config)
    if cancelled and cancelled():
        from . import replay_kernel

        empty_manifest = {
            "read_only": True,
            "research_only": True,
            "requested_range_inclusive": [normalized["start"], normalized["end"]],
            "requested_months_only": [],
            "used_source_files": [],
        }
        state = replay_kernel._mark_cancelled_before_read(
            normalized, replay_kernel.initial_state(normalized)
        )
        return replay_kernel.result_from_state(normalized, state, empty_manifest)
    if progress:
        progress(0.0, "正在读取指定训练期分钟数据")

    frame, manifest = _load_training_bars(normalized)
    from . import replay_kernel

    payloads = [
        {
            "datetime": pd.Timestamp(row.datetime).isoformat(),
            "bar_start_datetime": pd.Timestamp(row.datetime).isoformat(),
            "source_datetime": pd.Timestamp(row.source_datetime).isoformat(),
            "symbol": normalized["symbol"],
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "turnover": float(row.turnover),
        }
        for row in frame.itertuples(index=False)
    ]
    state = replay_kernel._advance_loaded_bars(
        normalized,
        replay_kernel.initial_state(normalized),
        payloads,
        cancelled=cancelled,
        progress=(
            lambda processed, total: progress(
                100.0 * processed / total,
                f"已回放 {processed}/{total} 根分钟线",
            )
            if progress
            and (
                processed == total
                or processed % max(1, total // 100) == 0
            )
            else None
        ),
    )
    return replay_kernel.result_from_state(normalized, state, manifest)

