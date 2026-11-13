"""Read-only, bounded access to completed portfolio CLI artifacts.

Hashes detect accidental file changes; they do not authenticate a ledger or
certify market data. Reading this archive never runs a strategy or creates files.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import stat

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_LIMITS = {"receipt.json": 32 * 1024, "config.json": 128 * 1024,
           "data_manifest.json": 8 * 1024**2, "state.json": 128 * 1024**2,
           "result.json": 64 * 1024**2}
_ARTIFACTS = frozenset(_LIMITS) - {"receipt.json"}
_CONFIG_FIELDS = frozenset({"symbols", "strategy", "start", "end", "interval",
    "initial_cash", "commission_rate", "min_commission", "slippage_bps",
    "params", "allocation_weights", "allocation_order"})
_RECEIPT_FIELDS = frozenset({"run_id", "status", "created_at", "finished_at", "frames", "error"})


class PortfolioArtifactChanged(ValueError):
    pass


def archive_root(store):
    return store.root.parent / (store.root.name + "_portfolios")


def _json(raw):
    def invalid(_):
        raise ValueError("组合记录含非有限数值")
    value = json.loads(raw, parse_constant=invalid)
    if type(value) is not dict:
        raise ValueError("组合记录格式无效")
    return value


def _read(directory_fd, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMITS[name]:
            raise ValueError("组合记录文件类型或大小无效")
        raw = stream.read(_LIMITS[name] + 1)
    if len(raw) > _LIMITS[name]:
        raise ValueError("组合记录过大")
    return raw


class PortfolioArchive:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def get(self, run_id, *, summary=False):
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            raise ValueError("组合运行编号无效")
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            receipt = _json(_read(directory_fd, "receipt.json"))
            if (receipt.get("run_id") != run_id or receipt.get("mode") != "RESEARCH_ONLY"
                    or receipt.get("paper_admission") is not False
                    or receipt.get("status") not in {"queued", "running", "completed", "failed", "interrupted", "timed_out", "cancelled"}):
                raise ValueError("组合回执身份或模式无效")
            config_raw = _read(directory_fd, "config.json")
            config = _json(config_raw)
            public_config = {key: value for key, value in config.items() if key in _CONFIG_FIELDS}
            public_receipt = {key: value for key, value in receipt.items() if key in _RECEIPT_FIELDS}
            # Execution status is historical. Inventory does not read the large
            # report/state files and must never imply their hashes were checked.
            public_receipt["artifact_integrity"] = "not_checked"
            output = {"receipt": public_receipt, "config": public_config, "result": None, "data_manifest": None}
            if receipt["status"] != "completed":
                return output
            hashes = receipt.get("artifact_sha256")
            if (type(hashes) is not dict or set(hashes) != _ARTIFACTS
                    or any(type(value) is not str or _SHA.fullmatch(value) is None for value in hashes.values())):
                raise PortfolioArtifactChanged("组合产物哈希清单缺失或不完整，请核查原运行目录")
            if hashlib.sha256(config_raw).hexdigest() != hashes["config.json"]:
                raise PortfolioArtifactChanged("组合配置已变化，停止展示该报告")
            if summary:
                return output
            parsed = {}
            for name in ("data_manifest.json", "state.json", "result.json"):
                raw = _read(directory_fd, name)
                if hashlib.sha256(raw).hexdigest() != hashes[name]:
                    raise PortfolioArtifactChanged("组合产物已变化，停止展示该报告")
                # The state may be large. Only its byte hash is needed for a
                # report view; kernel financial reconciliation is independent.
                if name != "state.json":
                    parsed[name] = _json(raw)
            result = parsed["result.json"]
            if result.get("mode") != "RESEARCH_ONLY" or result.get("paper_admission") is not False:
                raise ValueError("组合结果模式无效")
            public_receipt["artifact_integrity"] = "verified"
            output.update(result=result, data_manifest=parsed["data_manifest.json"])
            return output
        finally:
            os.close(directory_fd)

    def list(self):
        output = {"runs": [], "mode": "RESEARCH_ONLY", "paper_admission": False, "truncated": False}
        if not self.root.exists():
            return output
        if self.root.is_symlink():
            raise ValueError("组合归档目录不能是链接")
        with os.scandir(self.root) as entries:
            entries = list(itertools.islice(entries, 1001))
        output["truncated"] = len(entries) > 1000
        for entry in entries[:1000]:
            if _ID.fullmatch(entry.name) is None:
                continue
            try:
                record = self.get(entry.name, summary=True)
                output["runs"].append({"receipt": record["receipt"], "config": record["config"]})
            except (OSError, ValueError, TypeError, KeyError):
                output["runs"].append({"receipt": {"run_id": entry.name, "status": "invalid", "error": "组合记录缺失或变化，请核查原运行目录"}, "config": {}})
        output["runs"].sort(key=lambda row: str(row["receipt"].get("created_at", "")), reverse=True)
        if len(output["runs"]) > 100:
            output["truncated"] = True
        output["runs"] = output["runs"][:100]
        return output
