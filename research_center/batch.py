"""Bounded, sequential parameter batches for the local research runner.

Batch planning is read-only. Execution validates every case before it admits
the first individual run, then delegates each case to the existing runner so
the usual immutable per-run artifacts and lifecycle controls remain intact.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import time
from typing import Any, Callable
import uuid


_SPEC_FIELDS = frozenset({"base", "cases"})
_BASE_FIELDS = frozenset({
    "strategy", "backend", "symbol", "start", "end", "interval", "initial_cash",
    "params", "custom_params", "commission_rate", "min_commission", "slippage_bps",
    "history_limit", "source_id",
})
_CASE_FIELDS = frozenset({
    "label", "params", "custom_params", "commission_rate", "min_commission", "slippage_bps",
})
_MAP_FIELDS = frozenset({"params", "custom_params"})
_COST_FIELDS = frozenset({"commission_rate", "min_commission", "slippage_bps"})
_COMMON_FIELDS = (
    "strategy", "backend", "symbol", "start", "end", "interval", "initial_cash",
    "history_limit", "source_id", "source_sha256", "data_root",
)
_MAX_CASES = 8
_MIN_CASES = 2
_MAX_WAIT_SECONDS_PER_CASE = 300.0


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


def _effective_config(config: dict[str, Any]) -> dict[str, Any]:
    """Drop presentation metadata and parameters unused by buy-and-hold."""
    effective = deepcopy(config)
    for key in ("label", "experiment_id", "parent_run_id"):
        effective.pop(key, None)
    if effective.get("strategy") == "buy_hold":
        params = effective.get("params")
        if isinstance(params, dict):
            params.pop("fast", None)
            params.pop("slow", None)
    return effective


def plan_batch(store: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a 2–8 case batch without creating run records.

    ``store`` is only read through its resolved ``root`` attribute. A lightweight
    object such as ``SimpleNamespace(root=Path(...))`` is sufficient, so plan
    validation does not have to initialize ``RunStore`` or its SQLite ledger.
    """
    spec = _object(spec, "spec")
    _reject_unknown(spec, _SPEC_FIELDS, "spec")
    if set(spec) != _SPEC_FIELDS:
        raise ValueError("spec must contain exactly base and cases")

    base = _object(spec["base"], "base")
    _reject_unknown(base, _BASE_FIELDS, "base")
    strategy = base.get("strategy", "buy_hold")
    if strategy != "custom_python" and "custom_params" in base:
        raise ValueError("custom_params is only supported for custom_python")
    cases = spec["cases"]
    if type(cases) is not list or not _MIN_CASES <= len(cases) <= _MAX_CASES:
        raise ValueError("cases must contain between 2 and 8 entries")

    experiment_id = uuid.uuid4().hex
    normalized_cases: list[dict[str, Any]] = []
    labels: set[str] = set()
    seen_effective: dict[str, str] = {}

    from .registry import normalize_config

    for index, case_value in enumerate(cases, start=1):
        case = _object(case_value, f"cases[{index - 1}]")
        _reject_unknown(case, _CASE_FIELDS, f"cases[{index - 1}]")
        if strategy != "custom_python" and "custom_params" in case:
            raise ValueError("custom_params is only supported for custom_python")
        label = case.get("label")
        if type(label) is not str or not label.strip() or len(label) > 80:
            raise ValueError(f"cases[{index - 1}].label must be a non-empty string up to 80 characters")
        if label in labels:
            raise ValueError(f"duplicate case label: {label}")
        labels.add(label)

        request = deepcopy(base)
        for key in _MAP_FIELDS:
            if key in case:
                base_map = request.get(key, {})
                case_map = case[key]
                if type(base_map) is not dict or type(case_map) is not dict:
                    raise ValueError(f"{key} must be an object in base and case overrides")
                request[key] = {**base_map, **deepcopy(case_map)}
        for key in _COST_FIELDS:
            if key in case:
                request[key] = case[key]
        request.update(experiment_id=experiment_id, label=label)

        # Normalize every case now, before execute_batch can submit anything.
        normalized = normalize_config(request, store)
        effective_key = _canonical(_effective_config(normalized))
        if effective_key in seen_effective:
            raise ValueError(
                f"duplicate effective cases: {seen_effective[effective_key]} and {label}"
            )
        seen_effective[effective_key] = label
        normalized_cases.append({"index": index, "label": label, "config": normalized})

    first_config = normalized_cases[0]["config"]
    common = {key: deepcopy(first_config[key]) for key in _COMMON_FIELDS if key in first_config}
    for case in normalized_cases[1:]:
        config = case["config"]
        candidate = {key: config[key] for key in _COMMON_FIELDS if key in config}
        if _canonical(candidate) != _canonical(common):
            # Case schema prevents changing these fields; retain this guard if
            # runner normalization grows aliases or derived common fields.
            raise ValueError("all cases must use the same symbol, dates, backend, cash, history, and source")

    return {
        "schema_version": 1,
        "valid": True,
        "plan_only": True,
        "experiment_id": experiment_id,
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "common": common,
        "case_count": len(normalized_cases),
        "cases": normalized_cases,
    }


def _emit(on_event: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        on_event(deepcopy(event))


def _find_admitted_run(store: Any, experiment_id: str, label: str) -> dict[str, Any] | None:
    """Recover a runner-created record if submit raised after creating it."""
    try:
        records = store.list()
    except Exception:
        return None
    for record in records:
        if record.get("experiment_id") == experiment_id and record.get("label") == label:
            return record
    return None


def _case_receipt(case: dict[str, Any]) -> dict[str, Any]:
    return {"index": case["index"], "label": case["label"], "status": "not_started",
            "run_status": None, "run_id": None}


def execute_batch(
    store: Any,
    spec: dict[str, Any],
    *,
    wait_seconds: float = 300.0,
    poll_interval: float = 0.25,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate all cases, then submit and wait for them sequentially.

    The wait bound applies separately to each admitted case and cannot exceed
    the runner's 240 second worker alarm by more than a 60 second exit margin.
    A timed-out or interrupted run is left untouched and remains individually
    inspectable/cancellable by its returned ``run_id``.
    """
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        raise ValueError("wait_seconds must be a finite number from 0 to 300")
    wait_seconds = float(wait_seconds)
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= _MAX_WAIT_SECONDS_PER_CASE:
        raise ValueError("wait_seconds must be a finite number from 0 to 300")
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)):
        raise ValueError("poll_interval must be a finite number greater than 0 and at most 5")
    poll_interval = float(poll_interval)
    if not math.isfinite(poll_interval) or not 0 < poll_interval <= 5:
        raise ValueError("poll_interval must be a finite number greater than 0 and at most 5")

    # This must complete for the entire batch before the first runner.submit.
    plan = plan_batch(store, spec)
    return _run_normalized_plan(store, plan, wait_seconds, poll_interval, on_event)


def _run_normalized_plan(store, plan, wait_seconds, poll_interval, on_event):
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "experiment_id": plan["experiment_id"],
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "wait_seconds_per_case": wait_seconds,
        "cases": [_case_receipt(case) for case in plan["cases"]],
    }
    try:
        return _execute_planned(store, plan, receipt, wait_seconds, poll_interval, on_event)
    except KeyboardInterrupt:
        # An interrupt may arrive while publishing progress, after a run has
        # already been admitted. Keep the known IDs and do not emit again.
        if receipt["status"] == "running":
            unrecorded = next((case for case in receipt["cases"] if case["status"] == "not_started"), None)
            if unrecorded is not None:
                recovered = _find_admitted_run(store, receipt["experiment_id"], unrecorded["label"])
                if recovered is not None:
                    unrecorded.update(run_id=recovered.get("run_id"), run_status=recovered.get("status"),
                                      status="interrupted")
            receipt["status"] = "interrupted"
            receipt["stop_reason"] = "batch interrupted; submitted runs remain separately controllable"
        return receipt


def _execute_planned(store, plan, receipt, wait_seconds, poll_interval, on_event):
    _emit(on_event, {
        "type": "planned",
        "status": "running",
        "experiment_id": receipt["experiment_id"],
        "mode": receipt["mode"],
        "paper_admission": False,
        "common": deepcopy(plan["common"]),
        "cases": [{"index": case["index"], "label": case["label"]} for case in plan["cases"]],
    })

    from . import runner

    for case_plan, case_receipt in zip(plan["cases"], receipt["cases"]):
        label = case_plan["label"]
        run_record: dict[str, Any] | None = None
        try:
            run_record = runner.submit(store, case_plan["config"])
        except KeyboardInterrupt:
            # Submit may have durably created the run just before interruption.
            recovered = _find_admitted_run(store, receipt["experiment_id"], label)
            if recovered is not None:
                case_receipt.update(run_id=recovered.get("run_id"),
                                    run_status=recovered.get("status"), status="interrupted")
            else:
                case_receipt.update(status="interrupted")
            receipt["status"] = "interrupted"
            receipt["stop_reason"] = "interrupted during submission; no later cases were submitted"
            _emit(on_event, {"type": "interrupted", "receipt": receipt})
            return receipt
        except Exception as exc:
            recovered = _find_admitted_run(store, receipt["experiment_id"], label)
            case_receipt.update(status="failed", error=str(exc))
            if recovered is not None:
                case_receipt.update(run_id=recovered.get("run_id"), run_status=recovered.get("status"))
            receipt["status"] = "failed"
            receipt["stop_reason"] = f"submission failed for case {label}"
            _emit(on_event, {"type": "terminal", "receipt": receipt})
            return receipt

        case_receipt.update(run_id=run_record["run_id"],
                            run_status=run_record.get("status", "queued"),
                            status=run_record.get("status", "queued"))
        last_emitted_run_status = case_receipt["run_status"]
        _emit(on_event, {"type": "submitted", "experiment_id": receipt["experiment_id"],
                         "case": deepcopy(case_receipt)})

        deadline = time.monotonic() + wait_seconds
        while case_receipt["run_status"] in runner.ACTIVE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                case_receipt["status"] = "timed_out"
                receipt["status"] = "timed_out"
                receipt["stop_reason"] = (
                    f"case {label} exceeded its wait bound; run remains separately controllable"
                )
                _emit(on_event, {"type": "timed_out", "receipt": receipt})
                return receipt
            try:
                time.sleep(min(poll_interval, remaining))
                run_record = runner.refresh_status(store, store.get(case_receipt["run_id"]))
            except KeyboardInterrupt:
                try:
                    run_record = runner.refresh_status(store, store.get(case_receipt["run_id"]))
                    case_receipt["run_status"] = run_record.get("status")
                except Exception:
                    pass
                case_receipt["status"] = "interrupted"
                receipt["status"] = "interrupted"
                receipt["stop_reason"] = (
                    f"interrupted while waiting for case {label}; run remains separately controllable"
                )
                _emit(on_event, {"type": "interrupted", "receipt": receipt})
                return receipt
            except Exception as exc:
                case_receipt.update(status="failed", error=str(exc))
                receipt["status"] = "failed"
                receipt["stop_reason"] = f"status refresh failed for case {label}"
                _emit(on_event, {"type": "terminal", "receipt": receipt})
                return receipt

            case_receipt["run_status"] = run_record.get("status")
            case_receipt["status"] = run_record.get("status")
            if case_receipt["run_status"] != last_emitted_run_status:
                _emit(on_event, {"type": "updated", "experiment_id": receipt["experiment_id"],
                                 "case": deepcopy(case_receipt)})
                last_emitted_run_status = case_receipt["run_status"]

        if case_receipt["run_status"] != "completed":
            terminal_status = case_receipt["run_status"] or "failed"
            case_receipt["status"] = terminal_status
            receipt["status"] = terminal_status if terminal_status in {
                "failed", "cancelled", "interrupted", "timed_out"
            } else "failed"
            receipt["stop_reason"] = f"case {label} ended with status {terminal_status}; later cases were not submitted"
            _emit(on_event, {"type": "terminal", "receipt": receipt})
            return receipt

    receipt["status"] = "completed"
    receipt["stop_reason"] = None
    _emit(on_event, {"type": "completed", "receipt": receipt})
    return receipt
