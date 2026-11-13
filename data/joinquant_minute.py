"""JoinQuant-style A-share minute data adapter for research and VeighNa.

The raw vendor files are treated as immutable.  This module discovers the
monthly parquet batches, selects the most complete revision for each month and
batch, and converts JoinQuant's bar-end timestamps (09:31..15:00) to VeighNa's
bar-start convention (09:30..14:59).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Iterable, Sequence

import pandas as pd
import pyarrow.parquet as pq


MONTHLY_NAME = re.compile(
    r"^stock_1m_(?P<year>\d{4})-(?P<month>\d{2})_batch"
    r"(?P<batch>\d{3})(?:\((?P<revision>\d+)\))?\.parquet$"
)
REQUIRED_SOURCE_COLUMNS = {
    "time",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "money",
}
PRICE_COLUMNS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class MonthlySource:
    path: Path
    month: str
    batch: int
    revision: int
    rows: int
    min_time: pd.Timestamp
    max_time: pd.Timestamp
    min_code: str
    max_code: str

    @property
    def priority(self) -> tuple[pd.Timestamp, int, int, int]:
        """Prefer the widest/latest snapshot, then explicit revision/mtime."""
        return (self.max_time, self.rows, self.revision, self.path.stat().st_mtime_ns)


def jq_to_vt(code: str) -> str:
    """Convert ``000001.XSHE`` to ``000001.SZSE``."""
    value = str(code).strip().upper()
    if value.endswith(".XSHE"):
        return value[:-5] + ".SZSE"
    if value.endswith(".XSHG"):
        return value[:-5] + ".SSE"
    if value.endswith((".SZSE", ".SSE")):
        return value
    raise ValueError(f"unsupported JoinQuant code: {code!r}")


def vt_to_jq(code: str) -> str:
    """Convert a VeighNa symbol to the JoinQuant suffix convention."""
    value = str(code).strip().upper()
    if value.endswith(".SZSE"):
        return value[:-5] + ".XSHE"
    if value.endswith(".SSE"):
        return value[:-4] + ".XSHG"
    if value.endswith((".XSHE", ".XSHG")):
        return value
    if re.fullmatch(r"\d{6}", value):
        if value.startswith(("0", "1", "2", "3")):
            return value + ".XSHE"
        if value.startswith(("5", "6", "9")):
            return value + ".XSHG"
    raise ValueError(f"exchange is ambiguous for symbol: {code!r}")


def _column_statistics(metadata, column_name: str) -> tuple[object, object]:
    names = metadata.schema.names
    try:
        column_index = names.index(column_name)
    except ValueError as exc:
        raise ValueError(f"missing parquet column {column_name!r}") from exc

    minimum = None
    maximum = None
    for row_group_index in range(metadata.num_row_groups):
        stats = metadata.row_group(row_group_index).column(column_index).statistics
        if stats is None or not stats.has_min_max:
            continue
        minimum = stats.min if minimum is None else min(minimum, stats.min)
        maximum = stats.max if maximum is None else max(maximum, stats.max)
    if minimum is None or maximum is None:
        raise ValueError(f"parquet column {column_name!r} has no min/max statistics")
    return minimum, maximum


def inspect_monthly_source(path: Path) -> MonthlySource:
    match = MONTHLY_NAME.match(path.name)
    if not match:
        raise ValueError(f"not a monthly JoinQuant parquet: {path}")
    parquet = pq.ParquetFile(path)
    schema = set(parquet.schema_arrow.names)
    missing = REQUIRED_SOURCE_COLUMNS - schema
    if missing:
        raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
    min_time, max_time = _column_statistics(parquet.metadata, "time")
    min_code, max_code = _column_statistics(parquet.metadata, "code")
    return MonthlySource(
        path=path,
        month=f"{match.group('year')}-{match.group('month')}",
        batch=int(match.group("batch")),
        revision=int(match.group("revision") or 0),
        rows=parquet.metadata.num_rows,
        min_time=pd.Timestamp(min_time),
        max_time=pd.Timestamp(max_time),
        min_code=str(min_code),
        max_code=str(max_code),
    )


def discover_monthly_sources(
    root: Path,
    start: str | datetime | pd.Timestamp | None = None,
    end: str | datetime | pd.Timestamp | None = None,
) -> tuple[list[MonthlySource], list[MonthlySource]]:
    """Return canonical sources and shadowed older revisions."""
    root = Path(root)
    start_month = pd.Timestamp(start).strftime("%Y-%m") if start is not None else None
    end_month = pd.Timestamp(end).strftime("%Y-%m") if end is not None else None
    grouped: dict[tuple[str, int], list[MonthlySource]] = {}
    for path in sorted(root.glob("stock_1m_*-*_batch*.parquet")):
        if not MONTHLY_NAME.match(path.name):
            continue
        source = inspect_monthly_source(path)
        if start_month and source.month < start_month:
            continue
        if end_month and source.month > end_month:
            continue
        grouped.setdefault((source.month, source.batch), []).append(source)

    canonical: list[MonthlySource] = []
    shadowed: list[MonthlySource] = []
    for key in sorted(grouped):
        candidates = sorted(grouped[key], key=lambda item: item.priority, reverse=True)
        canonical.append(candidates[0])
        shadowed.extend(candidates[1:])
    return canonical, shadowed


def canonicalize_frame(
    frame: pd.DataFrame,
    *,
    timestamp_convention: str = "bar_end",
    source: str = "joinquant_parquet",
) -> pd.DataFrame:
    """Validate and convert raw JoinQuant bars to a VeighNa-ready frame."""
    missing = REQUIRED_SOURCE_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    data = frame[sorted(REQUIRED_SOURCE_COLUMNS)].copy()
    data = data.rename(columns={"time": "source_datetime", "money": "turnover"})
    data["source_datetime"] = pd.to_datetime(data["source_datetime"], errors="raise")
    data["datetime"] = data["source_datetime"]
    if timestamp_convention == "bar_end":
        data["datetime"] = data["datetime"] - pd.Timedelta(minutes=1)
    elif timestamp_convention != "bar_start":
        raise ValueError("timestamp_convention must be 'bar_end' or 'bar_start'")

    for column in PRICE_COLUMNS + ["volume", "turnover"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    # JoinQuant materializes the full monthly security grid.  Pre-listing,
    # delisted and fully unavailable minutes are represented by an all-NaN row.
    # Absence of a VeighNa bar is the conservative representation: it prevents
    # both trading and accidental forward-filling through a suspension.
    value_columns = PRICE_COLUMNS + ["volume", "turnover"]
    unavailable = data[value_columns].isna().all(axis=1)
    data = data.loc[~unavailable].copy()
    data["vt_symbol"] = data["code"].map(jq_to_vt)
    data["symbol"] = data["vt_symbol"].str.slice(0, 6)
    data["exchange"] = data["vt_symbol"].str.rsplit(".", n=1).str[-1]
    data["source"] = source

    null_required = data[value_columns].isna().any(axis=1)
    bad_price = (data[PRICE_COLUMNS] <= 0).any(axis=1)
    bad_ohlc = (data["high"] < data[PRICE_COLUMNS].max(axis=1)) | (
        data["low"] > data[PRICE_COLUMNS].min(axis=1)
    )
    bad_flow = (data["volume"] < 0) | (data["turnover"] < 0)
    if null_required.any() or bad_price.any() or bad_ohlc.any() or bad_flow.any():
        raise ValueError(
            "invalid bars: "
            f"null={int(null_required.sum())}, price={int(bad_price.sum())}, "
            f"ohlc={int(bad_ohlc.sum())}, flow={int(bad_flow.sum())}"
        )

    data = data.sort_values(["vt_symbol", "datetime", "source_datetime"])
    duplicates = data.duplicated(["vt_symbol", "datetime"], keep=False)
    if duplicates.any():
        conflicting = (
            data.loc[duplicates]
            .groupby(["vt_symbol", "datetime"])[PRICE_COLUMNS + ["volume", "turnover"]]
            .nunique(dropna=False)
            .gt(1)
            .any(axis=1)
        )
        if conflicting.any():
            raise ValueError(f"conflicting duplicate bars: {int(conflicting.sum())}")
        data = data.drop_duplicates(["vt_symbol", "datetime"], keep="last")

    columns = [
        "datetime",
        "source_datetime",
        "symbol",
        "exchange",
        "vt_symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "source",
    ]
    return data[columns].reset_index(drop=True)


def load_monthly_bars(
    root: Path,
    symbols: Sequence[str],
    start: str | datetime | pd.Timestamp,
    end: str | datetime | pd.Timestamp,
) -> tuple[pd.DataFrame, dict]:
    """Load selected symbols without mutating or copying the raw dataset."""
    if not symbols:
        raise ValueError("at least one symbol is required")
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if end_ts < start_ts:
        raise ValueError("end precedes start")
    jq_symbols = sorted({vt_to_jq(symbol) for symbol in symbols})
    canonical, shadowed = discover_monthly_sources(root, start_ts, end_ts)
    frames: list[pd.DataFrame] = []
    used: list[str] = []
    for source in canonical:
        candidates = [
            code for code in jq_symbols if source.min_code <= code <= source.max_code
        ]
        if not candidates:
            continue
        table = pq.read_table(
            source.path,
            columns=sorted(REQUIRED_SOURCE_COLUMNS),
            filters=[
                ("code", "in", candidates),
                ("time", ">=", start_ts.to_pydatetime()),
                ("time", "<", (end_ts + pd.Timedelta(days=1)).to_pydatetime()),
            ],
        )
        if table.num_rows:
            frames.append(table.to_pandas())
            used.append(str(source.path))

    if not frames:
        raise FileNotFoundError(
            f"no bars found for {jq_symbols} between {start_ts.date()} and {end_ts.date()}"
        )
    result = canonicalize_frame(pd.concat(frames, ignore_index=True))
    raw_rows = sum(len(item) for item in frames)
    result = result[
        (result["datetime"] >= start_ts)
        & (result["datetime"] < end_ts + pd.Timedelta(days=1))
    ].reset_index(drop=True)
    manifest = {
        "symbols_requested": jq_symbols,
        "start": str(start_ts),
        "end": str(end_ts),
        "rows": len(result),
        "raw_rows": raw_rows,
        "dropped_all_nan_unavailable_rows": raw_rows - len(result),
        "used_files": used,
        "shadowed_files": [str(item.path) for item in shadowed],
        "timestamp_source": "bar_end",
        "timestamp_output": "bar_start",
        "timestamp_shift_minutes": -1,
        "research_only": True,
        "price_adjustment": "vendor_qfq_vintage_unresolved",
        "volume_semantics": "adjustment-scaled_units_not_verified_raw_shares",
        "volume_capacity_usable": False,
        "turnover_capacity_proxy_usable": True,
    }
    return result, manifest


def iter_vnpy_bars(frame: pd.DataFrame) -> Iterable[object]:
    """Yield VeighNa ``BarData`` objects from a canonical frame."""
    from zoneinfo import ZoneInfo

    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData

    timezone = ZoneInfo("Asia/Shanghai")
    for row in frame.itertuples(index=False):
        value = pd.Timestamp(row.datetime).to_pydatetime()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        yield BarData(
            symbol=row.symbol,
            exchange=Exchange(row.exchange),
            datetime=value,
            interval=Interval.MINUTE,
            open_price=float(row.open),
            high_price=float(row.high),
            low_price=float(row.low),
            close_price=float(row.close),
            volume=float(row.volume),
            turnover=float(row.turnover),
            gateway_name="JOINQUANT_PARQUET",
        )
