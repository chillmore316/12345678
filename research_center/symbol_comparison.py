"""Bounded fixed-strategy comparisons across explicit main-board symbols.

Planning is read-only.  Each symbol is normalized independently before any
execution starts, then the resulting single-symbol runs are submitted in the
user's explicit order.  The runs share one experiment id and fixed inputs; this
module does not combine them into a portfolio or select a preferred symbol.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable
import uuid

from .batch import _BASE_FIELDS, _run_normalized_plan
from .registry import normalize_config


_SPEC_FIELDS = frozenset({"base", "symbols"})
_SYMBOL_BASE_FIELDS = _BASE_FIELDS - frozenset({"symbol"})
_BUILTIN_ONLY_UNSUPPORTED = frozenset({"source_id", "history_limit", "custom_params"})
_MIN_SYMBOLS = 2
_MAX_SYMBOLS = 4
_COMPARISON_SCOPE = "fixed_strategy_per_symbol_research_comparison"


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {sorted(unknown)}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def _common_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return normalized identity shared across runs, excluding per-symbol fields.

    A custom strategy's version binds its execution configuration, including
    symbol, so each case must retain its own version.  Its source metadata,
    source hash, custom parameters and the symbol-independent execution config
    are still compared here to prove that the requested strategy is fixed.
    """
    identity = deepcopy(config)
    for key in ("symbol", "label", "experiment_id", "parent_run_id"):
        identity.pop(key, None)
    if config.get("strategy") == "custom_python":
        identity.pop("strategy_version", None)
        execution_config = identity.get("execution_config")
        if type(execution_config) is not dict:
            raise ValueError("custom_python normalized execution_config is missing")
        execution_config.pop("symbol", None)
    return identity


def _scope(symbols: list[str]) -> dict[str, Any]:
    return {
        "comparison_scope": _COMPARISON_SCOPE,
        "portfolio_aggregation": False,
        "automatic_selection": False,
        "untouched_oos": False,
        "symbol_count": len(symbols),
        "symbols": list(symbols),
    }


def plan_symbols(store: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a fixed 2–4 symbol plan without creating runs.

    ``store`` is only passed to the existing registry normalizer.  A lightweight
    object exposing ``root`` is sufficient; this function never initializes the
    run ledger or submits a run.
    """
    spec = _object(spec, "spec")
    _reject_unknown(spec, _SPEC_FIELDS, "spec")
    if set(spec) != _SPEC_FIELDS:
        raise ValueError("spec must contain exactly base and symbols")

    base = _object(spec["base"], "base")
    _reject_unknown(base, _SYMBOL_BASE_FIELDS, "base")
    strategy = base.get("strategy", "buy_hold")
    if strategy != "custom_python" and _BUILTIN_ONLY_UNSUPPORTED.intersection(base):
        unsupported = sorted(_BUILTIN_ONLY_UNSUPPORTED.intersection(base))
        raise ValueError(f"{unsupported} is only supported for custom_python")

    requested_symbols = spec["symbols"]
    if type(requested_symbols) is not list or not _MIN_SYMBOLS <= len(requested_symbols) <= _MAX_SYMBOLS:
        raise ValueError("symbols must contain between 2 and 4 entries")
    if any(type(symbol) is not str for symbol in requested_symbols):
        raise ValueError("each symbols entry must be a string")

    experiment_id = uuid.uuid4().hex
    normalized_cases: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    common: dict[str, Any] | None = None

    # Normalize each full case through the registry before an execution plan is
    # returned.  execute_symbols therefore cannot submit an early case when a
    # later symbol is unsupported or has a stale custom-strategy identity.
    for index, requested_symbol in enumerate(requested_symbols, start=1):
        request = deepcopy(base)
        request.update(
            symbol=requested_symbol,
            experiment_id=experiment_id,
            label="pending",
        )
        config = normalize_config(request, store)
        symbol = config["symbol"]
        if symbol in seen_symbols:
            raise ValueError(f"duplicate normalized symbol: {symbol}")
        seen_symbols.add(symbol)
        label = symbol
        config["label"] = label

        normalized_identity = _common_identity(config)
        if common is None:
            common = normalized_identity
        elif _canonical(normalized_identity) != _canonical(common):
            raise ValueError(
                "all symbols must use the same normalized strategy, source, parameters, dates, cash, and costs"
            )

        normalized_cases.append({
            "index": index,
            "label": label,
            "symbol": symbol,
            "strategy_version": config.get("strategy_version"),
            "config": config,
        })

    if common is None:  # The count check above makes this unreachable.
        common = {}

    normalized_symbols = [case["symbol"] for case in normalized_cases]
    return {
        "schema_version": 1,
        "valid": True,
        "plan_only": True,
        "experiment_id": experiment_id,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "scope": _scope(normalized_symbols),
        "common": common,
        "case_count": len(normalized_cases),
        "cases": normalized_cases,
    }


def _case_metadata(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": case["symbol"],
        "strategy_version": case.get("strategy_version"),
    }


def _annotate_event(event: dict[str, Any], case_by_label: dict[str, dict[str, Any]],
                    scope: dict[str, Any]) -> dict[str, Any]:
    event = deepcopy(event)
    event["scope"] = deepcopy(scope)
    if event.get("type") == "planned":
        for case in event.get("cases", []):
            case.update(case_by_label.get(case.get("label"), {}))
    if isinstance(event.get("case"), dict):
        event["case"].update(case_by_label.get(event["case"].get("label"), {}))
    receipt = event.get("receipt")
    if isinstance(receipt, dict):
        receipt["scope"] = deepcopy(scope)
        for case in receipt.get("cases", []):
            case.update(case_by_label.get(case.get("label"), {}))
    return event


def execute_symbols(
    store: Any,
    spec: dict[str, Any],
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate all symbols, then execute their independent runs sequentially."""
    plan = plan_symbols(store, spec)
    case_by_label = {
        case["label"]: _case_metadata(case)
        for case in plan["cases"]
    }
    scope = deepcopy(plan["scope"])

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(_annotate_event(event, case_by_label, scope))

    receipt = _run_normalized_plan(
        store,
        plan,
        wait_seconds=300.0,
        poll_interval=0.25,
        on_event=emit if on_event is not None else None,
    )
    receipt["scope"] = scope
    for case in receipt["cases"]:
        case.update(case_by_label.get(case.get("label"), {}))
    return receipt

