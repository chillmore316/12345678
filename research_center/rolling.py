"""Bounded chronological rolling validation with one fixed configuration.

Planning is read-only.  Every train and validation window is normalized up
front, then execution reuses the existing sequential research runner.  Each
window is an independent run with a fresh initial cash balance and positions.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import re
from typing import Any, Callable
import uuid

from .batch import _BASE_FIELDS, _run_normalized_plan
from .registry import normalize_config


_SPEC_FIELDS = frozenset({"base", "folds"})
_FOLD_FIELDS = frozenset({"train_start", "train_end", "validation_start", "validation_end"})
_ROLLING_BASE_FIELDS = _BASE_FIELDS - frozenset({"start", "end"})
_BUILTIN_IGNORED_FIELDS = frozenset({"source_id", "history_limit", "custom_params"})
_COMMON_CONFIG_FIELDS = frozenset({
    "strategy", "backend", "symbol", "interval", "initial_cash", "commission_rate",
    "min_commission", "slippage_bps", "stamp_duty_rate", "params", "data_root",
    "source_id", "source_sha256", "code_version", "interface_contract",
    "source_metadata", "custom_params", "history_limit",
})
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_VALIDATION_SCOPE = "fixed_configuration_research_period_validation"


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {sorted(unknown)}")


def _iso_date(value: Any, field: str) -> date:
    if type(value) is not str or _DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an exact YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an exact YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an exact YYYY-MM-DD date")
    return parsed


def _label(fold_index: int, role: str) -> str:
    role_label = "训练段" if role == "train" else "验证段"
    return f"第 {fold_index} 折 · {role_label}"


def plan_rolling(store: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a fixed-configuration 2–4 fold rolling plan.

    The store is read only through ``root`` by the existing config normalizer;
    a lightweight object with a ``root`` attribute is sufficient for planning.
    """
    spec = _object(spec, "spec")
    _reject_unknown(spec, _SPEC_FIELDS, "spec")
    if set(spec) != _SPEC_FIELDS:
        raise ValueError("spec must contain exactly base and folds")

    base = _object(spec["base"], "base")
    _reject_unknown(base, _ROLLING_BASE_FIELDS, "base")
    strategy = base.get("strategy", "buy_hold")
    if strategy != "custom_python" and _BUILTIN_IGNORED_FIELDS.intersection(base):
        unsupported = sorted(_BUILTIN_IGNORED_FIELDS.intersection(base))
        raise ValueError(f"{unsupported} is only supported for custom_python")
    folds = spec["folds"]
    if type(folds) is not list or not 2 <= len(folds) <= 4:
        raise ValueError("folds must contain between 2 and 4 entries")

    parsed_folds: list[dict[str, Any]] = []
    previous_validation_end: date | None = None
    previous_train_start: date | None = None
    previous_train_end: date | None = None

    for index, value in enumerate(folds, start=1):
        fold = _object(value, f"folds[{index - 1}]")
        _reject_unknown(fold, _FOLD_FIELDS, f"folds[{index - 1}]")
        if set(fold) != _FOLD_FIELDS:
            raise ValueError(f"folds[{index - 1}] must contain exactly the four window dates")

        train_start = _iso_date(fold["train_start"], f"folds[{index - 1}].train_start")
        train_end = _iso_date(fold["train_end"], f"folds[{index - 1}].train_end")
        validation_start = _iso_date(
            fold["validation_start"], f"folds[{index - 1}].validation_start"
        )
        validation_end = _iso_date(
            fold["validation_end"], f"folds[{index - 1}].validation_end"
        )

        if train_end < train_start:
            raise ValueError(f"folds[{index - 1}] train_end must be on or after train_start")
        if validation_end < validation_start:
            raise ValueError(
                f"folds[{index - 1}] validation_end must be on or after validation_start"
            )
        if train_end >= validation_start:
            raise ValueError(f"folds[{index - 1}] training must end before validation starts")
        if previous_validation_end is not None and validation_start <= previous_validation_end:
            raise ValueError("validation windows must be disjoint and chronological")
        if previous_train_start is not None and train_start < previous_train_start:
            raise ValueError("train_start values must be nondecreasing across folds")
        if previous_train_end is not None and train_end <= previous_train_end:
            raise ValueError("train_end values must be strictly increasing across folds")

        parsed_folds.append({
            "fold_index": index,
            "train_start": train_start.isoformat(),
            "train_end": train_end.isoformat(),
            "validation_start": validation_start.isoformat(),
            "validation_end": validation_end.isoformat(),
        })
        previous_validation_end = validation_end
        previous_train_start = train_start
        previous_train_end = train_end

    experiment_id = uuid.uuid4().hex
    normalized_cases: list[dict[str, Any]] = []
    plan_folds: list[dict[str, Any]] = []
    common: dict[str, Any] | None = None
    common_config_fields = _COMMON_CONFIG_FIELDS
    if strategy != "custom_python":
        # Built-in strategy versions are independent of window dates, so also
        # compare them across normalizations to catch source drift mid-plan.
        common_config_fields = common_config_fields | frozenset({"strategy_version"})

    # Normalize every role/window before returning a plan.  execute_rolling
    # does not admit any run until this full loop has succeeded.
    for fold in parsed_folds:
        fold_index = fold["fold_index"]
        fold_plan: dict[str, Any] = {"fold_index": fold_index}
        for role, start_key, end_key in (
            ("train", "train_start", "train_end"),
            ("validation", "validation_start", "validation_end"),
        ):
            index = len(normalized_cases) + 1
            label = _label(fold_index, role)
            window = {"start": fold[start_key], "end": fold[end_key]}
            request = deepcopy(base)
            request.update(
                start=window["start"],
                end=window["end"],
                experiment_id=experiment_id,
                label=label,
            )
            config = normalize_config(request, store)
            config.pop("parent_run_id", None)
            normalized_common = {
                key: deepcopy(config[key])
                for key in common_config_fields
                if key in config
            }
            if common is None:
                common = normalized_common
            elif normalized_common != common:
                raise ValueError("all windows must use the same normalized fixed configuration and source")
            normalized_cases.append({
                "index": index,
                "label": label,
                "fold_index": fold_index,
                "role": role,
                "window": window,
                "config": config,
            })
            fold_plan[role] = {
                **window,
                "case_index": index,
                "label": label,
            }
        plan_folds.append(fold_plan)

    if common is None:
        # The fold count check above makes this unreachable, but keep the return
        # shape explicit if the bounds change in a future schema revision.
        common = {}

    return {
        "schema_version": 1,
        "valid": True,
        "plan_only": True,
        "experiment_id": experiment_id,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "validation_scope": _VALIDATION_SCOPE,
        "untouched_oos": False,
        "automatic_selection": False,
        "common": common,
        "fold_count": len(plan_folds),
        "case_count": len(normalized_cases),
        "folds": plan_folds,
        "cases": normalized_cases,
    }


def _case_metadata(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "fold_index": plan["fold_index"],
        "role": plan["role"],
        "window": deepcopy(plan["window"]),
    }


def _annotate_event(event: dict[str, Any], case_by_label: dict[str, dict[str, Any]],
                    folds: list[dict[str, Any]]) -> dict[str, Any]:
    event = deepcopy(event)
    if event.get("type") == "planned":
        for case in event.get("cases", []):
            case.update(case_by_label.get(case.get("label"), {}))
    if isinstance(event.get("case"), dict):
        event["case"].update(case_by_label.get(event["case"].get("label"), {}))
    receipt = event.get("receipt")
    if isinstance(receipt, dict):
        receipt["folds"] = deepcopy(folds)
        for case in receipt.get("cases", []):
            case.update(case_by_label.get(case.get("label"), {}))
    return event


def execute_rolling(
    store: Any,
    spec: dict[str, Any],
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate all windows, then execute their fixed configs sequentially."""
    plan = plan_rolling(store, spec)
    case_by_label = {
        case["label"]: _case_metadata(case)
        for case in plan["cases"]
    }

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(_annotate_event(event, case_by_label, plan["folds"]))

    receipt = _run_normalized_plan(
        store,
        plan,
        wait_seconds=300.0,
        poll_interval=0.25,
        on_event=emit if on_event is not None else None,
    )
    receipt.update(
        validation_scope=_VALIDATION_SCOPE,
        untouched_oos=False,
        automatic_selection=False,
        fold_count=plan["fold_count"],
        folds=deepcopy(plan["folds"]),
    )
    for case in receipt["cases"]:
        case.update(case_by_label.get(case.get("label"), {}))
    return receipt

