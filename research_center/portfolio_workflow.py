"""Plan a single shared-cash run without reading prices or admitting work."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from .engine import MAX_ENGINE_ROWS
from .portfolio_kernel import normalize_portfolio
from .portfolio_provenance import snapshot_sources
from .store import ROOT

_CONFIG_FIELDS = frozenset({"symbols", "start", "end", "interval", "strategy", "initial_cash",
    "commission_rate", "min_commission", "slippage_bps", "params", "allocation_weights"})
_PARAM_FIELDS = frozenset({"fast", "slow", "fast_window", "slow_window", "target_fraction", "max_turnover_participation"})
_SHA = re.compile(r"[a-f0-9]{64}\Z")


class PortfolioSourceChanged(ValueError):
    pass


def plan_portfolio(store, spec):
    if type(spec) is not dict or not {"config"} <= set(spec) <= {"config", "expected_source_version"}:
        raise ValueError("组合预检需要配置，且不能包含额外控制字段")
    config = spec["config"]
    if type(config) is not dict or set(config) - _CONFIG_FIELDS:
        raise ValueError("组合配置含不支持字段，网页不能更改行情目录或执行环境")
    params = config.get("params", {})
    if type(params) is not dict or set(params) - _PARAM_FIELDS:
        raise ValueError("组合策略参数含不支持字段")
    normalized = normalize_portfolio(config)
    expected = spec.get("expected_source_version")
    if expected is not None and (type(expected) is not str or _SHA.fullmatch(expected) is None):
        raise ValueError("组合执行版本格式无效")
    version = snapshot_sources(ROOT)["source_version"]
    if expected is not None and expected != version:
        raise PortfolioSourceChanged("执行源码已变化，请重新预检后提交新任务")
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return {
        "valid": True, "plan_only": True, "mode": "RESEARCH_ONLY", "paper_admission": False,
        "experiment_id": uuid.uuid4().hex, "case_count": 1,
        "source_version": version, "config_sha256": hashlib.sha256(encoded).hexdigest(),
        "limits": {"wall_seconds": 300, "max_symbols": 4, "max_rows_per_symbol": MAX_ENGINE_ROWS},
        "scope": {"comparison_scope": "shared_cash_portfolio", "portfolio_aggregation": True,
                  "automatic_selection": False, "symbols": list(normalized["symbols"])},
        "cases": [{"index": 1, "label": "共享资金组合", "config": normalized}],
    }
