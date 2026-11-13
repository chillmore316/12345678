"""Research-only bridge for immutable custom Python strategies.

User source is transported to :class:`GuestStrategyAdapter` as bytes and is
executed only by the separately pinned guest runtime.  This module does not
compile, import, or execute user source on the host.  Market data and account
transitions remain owned by the existing local research engine/kernel.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

import pandas as pd

from research_center import custom_kernel, engine
from research_center.custom_source_store import (
    DEFAULT_ROOT as DEFAULT_SOURCE_ROOT,
    MAX_PARAMS_BYTES,
    CustomSourceStore,
    _validate_params,
)
from research_center.guest_strategy import (
    MAX_HISTORY_LIMIT,
    GuestStrategyAdapter,
    TRUSTED_DRIVER_SOURCE,
    _default_api_source,
)
from research_center.vm_strategy_session import (
    DEFAULT_INSTANCE,
    DEFAULT_LIMA_HOME,
    VMRuntimeSpec,
)


STRATEGY_ID = "custom_python"
BACKEND_ID = "isolated_python"
MODE = "RESEARCH_ONLY"
PAPER_ADMISSION = False
DEFAULT_HISTORY_LIMIT = 60
_SOURCE_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_SCHEMA = "research-center-custom-python-bridge-v1"
_MAX_CODE_FILE_BYTES = 4 * 1024 * 1024

# These are literal pins from the reviewed local runtime specification.  The
# live Lima configuration is still checked by VMStrategySession at each run;
# this factory never discovers a runtime, starts Lima, edits networking, or
# falls back to a different runtime.
_RUNTIME_PINS = {
    "expected_instance_yaml_sha256": "1d0523837d0aafaac3b06661997ea97ad4e25dd976e13fca1d5d7a189be95abd",
    "expected_resolved_config_sha256": "f00339bb1c8af41e457608772c30c6b1d751a1abb78d79651b3d9bc7f5495bf3",
    "immutable_image_ref": "quant-strategy-worker@sha256:a5972e8f6704cfdd3cd53426edec57c2624e20de842639678cdcbbce9a2c425e",
    "worker_sha256": "1b9c2dd77827d41653963ce37d5ed21fbfaaaae72647754a5c0ca12aee731fa2",
    "expected_image_config_sha256": "4eb865cdac8520ff8ed42a68531979755560180100b20674b831e4ffa674aa88",
}
_RUNTIME_HOME = DEFAULT_LIMA_HOME
_RUNTIME_INSTANCE = DEFAULT_INSTANCE

_EXECUTION_FIELDS = (
    "symbol",
    "start",
    "end",
    "interval",
    "initial_cash",
    "commission_rate",
    "min_commission",
    "slippage_bps",
    "data_root",
)
_RISK_PARAM_FIELDS = frozenset({"target_fraction", "max_turnover_participation"})
_IGNORED_METADATA_FIELDS = frozenset({"experiment_id", "parent_run_id", "label"})
_SOURCE_METADATA_FIELDS = frozenset(
    {"registration_id", "name", "description", "interface_contract", "source_sha256", "code_version"}
)
_ALLOWED_CONFIG_FIELDS = frozenset(
    {
        *_EXECUTION_FIELDS,
        "strategy",
        "backend",
        "source_id",
        "custom_params",
        "params",
        "history_limit",
        "strategy_version",
        "execution_config",
        "source_metadata",
        *_SOURCE_METADATA_FIELDS,
        *_IGNORED_METADATA_FIELDS,
    }
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PATHS = {
    "custom_engine": Path(__file__),
    "guest_adapter": _PROJECT_ROOT / "research_center" / "guest_strategy.py",
    "custom_kernel": _PROJECT_ROOT / "research_center" / "custom_kernel.py",
    "engine": _PROJECT_ROOT / "research_center" / "engine.py",
    "strategies": _PROJECT_ROOT / "research_center" / "strategies.py",
    "replay_accounts": _PROJECT_ROOT / "research_center" / "replay_accounts.py",
    "replay_kernel": _PROJECT_ROOT / "research_center" / "replay_kernel.py",
    "custom_source_store": _PROJECT_ROOT / "research_center" / "custom_source_store.py",
    "minute_adapter": _PROJECT_ROOT / "data" / "joinquant_minute.py",
    "strategy_base": _PROJECT_ROOT / "strategy" / "base.py",
    "vm_session": _PROJECT_ROOT / "research_center" / "vm_strategy_session.py",
    "vm_worker": _PROJECT_ROOT / "research_center" / "vm_strategy_worker.py",
}


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("custom strategy identity must be bounded finite JSON") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


def configured_runtime() -> VMRuntimeSpec:
    """Return the reviewed project runtime pins without runtime discovery."""
    return VMRuntimeSpec(
        **_RUNTIME_PINS,
        lima_home=_RUNTIME_HOME,
        instance=_RUNTIME_INSTANCE,
    )


def _resolve_store(source_store: Any | None) -> Any:
    if source_store is None:
        return CustomSourceStore(DEFAULT_SOURCE_ROOT)
    if not callable(getattr(source_store, "show", None)) or not callable(
        getattr(source_store, "read_source", None)
    ):
        raise ValueError("source_store must provide read-only show/read_source methods")
    return source_store


def _validate_param_object(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{field} must be a JSON object")
    try:
        encoded = _validate_params(value)
        copied = json.loads(encoded.decode("utf-8", "strict"))
    except Exception as exc:
        # The archive helper's error text is fixed and contains no user data.
        raise ValueError(f"{field} is outside the bounded JSON parameter policy") from exc
    if len(encoded) > MAX_PARAMS_BYTES:
        raise ValueError(f"{field} exceeds the 16 KiB parameter limit")
    return copied


def _read_source_bundle(source_store: Any, source_id: Any) -> tuple[dict[str, Any], bytes]:
    if type(source_id) is not str or _SOURCE_ID_RE.fullmatch(source_id) is None:
        raise ValueError("source_id must be a lowercase SHA-256 registration ID")
    record = source_store.show(source_id)
    source = source_store.read_source(source_id)
    if type(record) is not dict or type(source) is not bytes:
        raise ValueError("custom source archive returned an invalid record")
    source_sha = _sha256(source)
    if (
        record.get("registration_id") != source_id
        or record.get("source_sha256") != source_sha
        or record.get("code_version") != source_sha
        or record.get("interface_contract") != "strategy-base-json-state-v1"
        or record.get("mode") != MODE
        or record.get("paper_admission") is not False
        or record.get("runtime_interface_verified") is not False
        or type(record.get("default_params")) is not dict
    ):
        raise ValueError("custom source archive integrity or policy mismatch")
    _validate_param_object(record["default_params"], "registered default_params")
    name = record.get("name")
    if type(name) is not str or not name:
        raise ValueError("custom source archive name is invalid")
    # read_source() performs the archive's own no-follow, immutable-record and
    # source digest checks.  Verify the returned bytes again at this boundary.
    if len(source) > 64 * 1024:
        raise ValueError("custom source exceeds the 64 KiB archive limit")
    return record, source


def _module_source_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, path in _SOURCE_PATHS.items():
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("source file is not a regular project file")
            if path.stat().st_size > _MAX_CODE_FILE_BYTES:
                raise ValueError("source file exceeds code identity bound")
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ValueError(f"trusted bridge component unavailable: {name}") from exc
        hashes[name] = _sha256(raw)
    return hashes


def _component_hashes(api_source: bytes) -> dict[str, Any]:
    if type(api_source) is not bytes or len(api_source) > 32 * 1024:
        raise ValueError("frozen strategy API source is invalid")
    code_hashes = _module_source_hashes()
    api_sha = _sha256(api_source)
    if api_sha != code_hashes["strategy_base"]:
        raise ValueError("frozen strategy API source changed while building its identity")
    return {
        "api_source_sha256": api_sha,
        "sdk_source_sha256": code_hashes["strategy_base"],
        "generic_driver_sha256": _sha256(TRUSTED_DRIVER_SOURCE),
        "trusted_code_sha256": code_hashes,
        "legacy_source_sha256": {
            "engine": code_hashes["engine"],
            "strategies": code_hashes["strategies"],
            "replay_accounts": code_hashes["replay_accounts"],
            "replay_kernel": code_hashes["replay_kernel"],
            "strategy_base": code_hashes["strategy_base"],
        },
        "runtime_pins": {
            **_RUNTIME_PINS,
            "lima_home": str(_RUNTIME_HOME),
            "instance": _RUNTIME_INSTANCE,
        },
    }


def _canonical_request_fields(config: dict[str, Any], history_limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
    params = config.get("params", {})
    if type(params) is not dict or any(type(key) is not str for key in params):
        raise ValueError("params must contain only execution-risk keys")
    extra_params = set(params) - _RISK_PARAM_FIELDS
    if extra_params:
        raise ValueError(f"params contains unsupported execution-risk fields: {sorted(extra_params)}")

    base_request = {name: config[name] for name in _EXECUTION_FIELDS if name in config}
    base_request["strategy"] = "custom_python"
    base_request["params"] = deepcopy(params)
    execution_config = custom_kernel.make_execution_config(base_request, history_limit=history_limit)
    normalized = {
        name: execution_config[name]
        for name in _EXECUTION_FIELDS
    }
    normalized["params"] = {
        name: execution_config["params"][name]
        for name in sorted(_RISK_PARAM_FIELDS)
    }
    return normalized, execution_config


@dataclass(frozen=True)
class _PreparedConfig:
    normalized: dict[str, Any]
    execution_config: dict[str, Any]
    record: dict[str, Any]
    source: bytes
    api_source: bytes
    component_hashes: dict[str, Any]
    identity: dict[str, Any]
    strategy_version: str


def _prepare(config: Any, source_store: Any | None) -> _PreparedConfig:
    if type(config) is not dict:
        raise ValueError("config must be a JSON object")
    unknown = set(config) - _ALLOWED_CONFIG_FIELDS
    if unknown:
        raise ValueError(f"config contains unsupported fields: {sorted(unknown)}")
    strategy = config.get("strategy", STRATEGY_ID)
    backend = config.get("backend", BACKEND_ID)
    if strategy != STRATEGY_ID:
        raise ValueError("strategy must be custom_python")
    if backend != BACKEND_ID:
        raise ValueError("backend must be isolated_python")

    history_limit = config.get("history_limit", DEFAULT_HISTORY_LIMIT)
    if type(history_limit) is not int or not 1 <= history_limit <= MAX_HISTORY_LIMIT:
        raise ValueError("history_limit must be an integer from 1 to 241")

    store = _resolve_store(source_store)
    record, source = _read_source_bundle(store, config.get("source_id"))
    supplied_custom_params = _validate_param_object(
        config.get("custom_params", {}), "custom_params"
    )
    merged_custom_params = deepcopy(record["default_params"])
    merged_custom_params.update(supplied_custom_params)
    merged_custom_params = _validate_param_object(merged_custom_params, "merged custom_params")

    normalized_fields, execution_config = _canonical_request_fields(config, history_limit)
    expected_source_metadata = {
        "registration_id": record["registration_id"],
        "name": record["name"],
        "description": record.get("description", ""),
        "interface_contract": record["interface_contract"],
        "source_sha256": _sha256(source),
        "code_version": record["code_version"],
    }
    for field in _SOURCE_METADATA_FIELDS:
        if field in config and config[field] != expected_source_metadata[field]:
            raise ValueError(f"submitted source metadata is stale: {field}")
    if "source_metadata" in config:
        supplied_metadata = config["source_metadata"]
        if type(supplied_metadata) is not dict or set(supplied_metadata) - _SOURCE_METADATA_FIELDS:
            raise ValueError("source_metadata contains unsupported fields")
        if any(
            supplied_metadata[field] != expected_source_metadata[field]
            for field in supplied_metadata
        ):
            raise ValueError("submitted source_metadata is stale")
    if "execution_config" in config:
        supplied_execution = config["execution_config"]
        if type(supplied_execution) is not dict or _canonical_json(supplied_execution) != _canonical_json(
            execution_config
        ):
            raise ValueError("submitted execution_config is stale or does not match normalized fields")

    api_source = _default_api_source()
    component_hashes = _component_hashes(api_source)
    source_sha = _sha256(source)
    version_material = {
        "schema": _VERSION_SCHEMA,
        "source_id": record["registration_id"],
        "source_sha256": source_sha,
        "api_source_sha256": component_hashes["api_source_sha256"],
        "sdk_source_sha256": component_hashes["sdk_source_sha256"],
        "generic_driver_sha256": component_hashes["generic_driver_sha256"],
        "trusted_code_sha256": component_hashes["trusted_code_sha256"],
        "legacy_source_sha256": component_hashes["legacy_source_sha256"],
        "runtime_pins": component_hashes["runtime_pins"],
        "custom_params": merged_custom_params,
        "history_limit": history_limit,
        "execution_config": execution_config,
    }
    strategy_version = _sha256_json(version_material)
    identity = {
        "schema": _VERSION_SCHEMA,
        "strategy_version": strategy_version,
        "source_id": record["registration_id"],
        "source_sha256": source_sha,
        "api_source_sha256": component_hashes["api_source_sha256"],
        "sdk_source_sha256": component_hashes["sdk_source_sha256"],
        "generic_driver_sha256": component_hashes["generic_driver_sha256"],
        "trusted_code_sha256": component_hashes["trusted_code_sha256"],
        "legacy_source_sha256": component_hashes["legacy_source_sha256"],
        "runtime_pins": component_hashes["runtime_pins"],
        "custom_params_sha256": _sha256_json(merged_custom_params),
        "history_limit": history_limit,
        "execution_config_sha256": _sha256_json(execution_config),
    }
    # The kernel's state schema deliberately bounds the complete identity.
    custom_kernel._json_copy(
        identity,
        maximum=custom_kernel.MAX_IDENTITY_BYTES,
        field="strategy identity",
    )

    if "strategy_version" in config:
        claimed_version = config["strategy_version"]
        if type(claimed_version) is not str or _VERSION_RE.fullmatch(claimed_version) is None:
            raise ValueError("strategy_version must be a lowercase SHA-256 digest")
        if claimed_version != strategy_version:
            raise ValueError("strategy_version is stale for the current source/configuration")

    normalized = {
        "strategy": STRATEGY_ID,
        "backend": BACKEND_ID,
        "source_id": record["registration_id"],
        "name": record["name"],
        "description": record.get("description", ""),
        "source_sha256": source_sha,
        "code_version": record["code_version"],
        "interface_contract": record["interface_contract"],
        "source_metadata": expected_source_metadata,
        "execution_config": deepcopy(execution_config),
        "custom_params": merged_custom_params,
        "history_limit": history_limit,
        **normalized_fields,
        "strategy_version": strategy_version,
    }
    return _PreparedConfig(
        normalized=normalized,
        execution_config=execution_config,
        record=record,
        source=source,
        api_source=api_source,
        component_hashes=component_hashes,
        identity=identity,
        strategy_version=strategy_version,
    )


def normalize_config(config: dict[str, Any], source_store: Any | None = None) -> dict[str, Any]:
    """Validate a custom-strategy request and bind it to current source/code.

    Only the standard engine fields, bounded execution-risk parameters and
    bounded ``custom_params`` are accepted.  Source/runtime paths and source
    code are not request fields; the archive and pinned runtime are injected by
    trusted caller code.
    """
    prepared = _prepare(config, source_store)
    return deepcopy(prepared.normalized)


def strategy_version(config: dict[str, Any], source_store: Any | None = None) -> str:
    """Return the deterministic current composite identity for this request."""
    return _prepare(config, source_store).strategy_version


def _payloads(frame: Any, symbol: str) -> list[dict[str, Any]]:
    return [
        {
            "datetime": pd.Timestamp(row.datetime).isoformat(),
            "bar_start_datetime": pd.Timestamp(row.datetime).isoformat(),
            "source_datetime": pd.Timestamp(row.source_datetime).isoformat(),
            "symbol": symbol,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "turnover": float(row.turnover),
        }
        for row in frame.itertuples(index=False)
    ]


def _source_manifest(prepared: _PreparedConfig, *, runtime_interface_verified: bool) -> dict[str, Any]:
    record = prepared.record
    registration = {
        key: deepcopy(record[key])
        for key in (
            "schema_version",
            "registration_id",
            "created_at",
            "interface_contract",
            "name",
            "description",
            "code_version",
            "declared_import_roots",
            "import_declaration_status",
            "runtime_interface_verified",
            "dependencies_verified",
            "mode",
            "paper_admission",
        )
        if key in record
    }
    return {
        "schema_version": _VERSION_SCHEMA,
        "mode": MODE,
        "paper_admission": PAPER_ADMISSION,
        "strategy_version": prepared.strategy_version,
        "source_id": record["registration_id"],
        "source_sha256": _sha256(prepared.source),
        "registration": registration,
        "api_source_sha256": prepared.component_hashes["api_source_sha256"],
        "sdk_source_sha256": prepared.component_hashes["sdk_source_sha256"],
        "generic_driver_sha256": prepared.component_hashes["generic_driver_sha256"],
        "trusted_code_sha256": deepcopy(prepared.component_hashes["trusted_code_sha256"]),
        "legacy_source_sha256": deepcopy(prepared.component_hashes["legacy_source_sha256"]),
        "runtime_pins": deepcopy(prepared.component_hashes["runtime_pins"]),
        "runtime_interface_verified": runtime_interface_verified,
        "verification_scope": (
            "This run initialized the pinned guest adapter and closed it successfully; the flag "
            "covers only this run's adapter interface. Dependencies, source licensing, data quality, "
            "runtime persistence, and paper admission are not inferred."
        ),
    }


def run_backtest(
    config: dict[str, Any],
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    *,
    source_store: Any | None = None,
    runtime: VMRuntimeSpec | None = None,
) -> dict[str, Any]:
    """Replay one bounded training-period request with a pinned guest strategy.

    ``runtime`` is a trusted internal test/integration hook and must never be
    populated from a public request.  The public runner should omit it and use
    :func:`configured_runtime`.
    """
    if type(config) is not dict or "strategy_version" not in config:
        raise ValueError("config must be bound to a current strategy_version")
    if cancelled is not None and not callable(cancelled):
        raise ValueError("cancelled must be callable or None")
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable or None")
    prepared = _prepare(config, source_store)
    if config.get("strategy_version") != prepared.strategy_version:
        raise ValueError("config must be bound to the current strategy_version")
    if cancelled is not None and bool(cancelled()):
        raise InterruptedError("custom strategy run cancelled before guest initialization")

    active_runtime = configured_runtime() if runtime is None else runtime
    adapter = GuestStrategyAdapter(
        prepared.source,
        prepared.normalized["symbol"],
        deepcopy(prepared.normalized["custom_params"]),
        active_runtime,
        state=None,
        history_limit=prepared.normalized["history_limit"],
        api_source=prepared.api_source,
    )

    try:
        if progress:
            progress(0.0, "正在读取指定训练期分钟数据")
        # Fail an invalid guest interface before opening any market-data files.
        state = custom_kernel.initial_state(
            prepared.execution_config,
            prepared.identity,
            user_state=adapter.state,
            history_limit=prepared.normalized["history_limit"],
        )
        frame, data_manifest = engine._load_training_bars(prepared.execution_config)
        payloads = _payloads(frame, prepared.normalized["symbol"])

        last_progress_bucket = -1

        def report_replay_progress(processed: int, total: int) -> None:
            nonlocal last_progress_bucket
            if not progress or total <= 0:
                return
            bucket = min(99, int(100 * processed / total))
            if processed == total or bucket > last_progress_bucket:
                last_progress_bucket = bucket
                progress(float(bucket), f"已回放 {processed}/{total} 根分钟线")

        state = custom_kernel.advance(
            prepared.execution_config,
            state,
            payloads,
            adapter,
            final=True,
            cancelled=cancelled,
            progress=report_replay_progress,
        )
    except BaseException:
        # An unreturned provisional state is discarded.  Close errors remain
        # visible and cannot be converted into a result.
        adapter.close()
        raise

    # This is the commit boundary: close may surface late guest stderr or
    # worker errors, so no result or 100% progress is emitted before it passes.
    adapter.close()
    result = custom_kernel.result_from_state(
        prepared.execution_config,
        state,
        data_manifest,
    )
    result["strategy"] = STRATEGY_ID
    result["backend"] = BACKEND_ID
    result["name"] = prepared.record["name"]
    result["strategy_version"] = prepared.strategy_version
    result["custom_config"] = deepcopy(prepared.normalized)
    result["custom_config"].pop("execution_config", None)
    result["source_manifest"] = _source_manifest(
        prepared,
        runtime_interface_verified=True,
    )
    if progress:
        progress(100.0, "回测完成")
    return result

