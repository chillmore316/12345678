"""Bounded, read-only loading of aligned training bars for a shared-cash replay."""
from __future__ import annotations

from typing import Any
import pandas as pd
from .engine import _load_training_bars, validate_config


def load_aligned_frames(configurations: list[dict[str, Any]]) -> tuple[list[dict[str, dict[str, Any]]], dict[str, Any]]:
    """Load every requested symbol and require identical observed minute axes."""
    if type(configurations) is not list or not 2 <= len(configurations) <= 4:
        raise ValueError("组合需要 2 至 4 个单标的配置")
    configs = [validate_config(config) for config in configurations]
    symbols = [config["symbol"] for config in configs]
    if len(set(symbols)) != len(symbols):
        raise ValueError("组合标的不能重复")
    shared = ("start", "end", "interval", "data_root")
    if any(any(config[key] != configs[0][key] for key in shared) for config in configs[1:]):
        raise ValueError("组合标的必须使用相同日期、周期和数据目录")
    frames: list[dict[str, dict[str, Any]]] = []
    axis = None
    manifests = {}
    for config in configs:
        table, manifest = _load_training_bars(config)
        observed = [(pd.Timestamp(row.datetime).isoformat(), pd.Timestamp(row.source_datetime).isoformat())
                    for row in table.itertuples(index=False)]
        if not observed or len(set(observed)) != len(observed) or observed != sorted(observed):
            raise ValueError("组合行情时间标签为空、重复或乱序")
        if axis is None:
            axis = observed
            frames = [{} for _ in observed]
        elif observed != axis:
            raise ValueError("组合标的分钟时间轴不一致，请核查缺失分钟和可交易状态")
        for index, row in enumerate(table.itertuples(index=False)):
            start, source = observed[index]
            frames[index][config["symbol"]] = {
                "symbol": config["symbol"], "datetime": start, "bar_start_datetime": start,
                "source_datetime": source,
                **{key: float(getattr(row, key)) for key in ("open", "high", "low", "close", "volume", "turnover")},
            }
        manifests[config["symbol"]] = manifest
    return frames, {
        "schema_version": 1, "mode": "RESEARCH_ONLY", "paper_admission": False,
        "symbols": symbols, "start": configs[0]["start"], "end": configs[0]["end"],
        "frames": len(frames), "alignment": "exact_bar_start_and_source_datetime",
        "sources": manifests,
        "limitations": ["时间轴一致只描述已读取的分钟；价格、日历完整性和可交易状态须独立认证。"],
    }
