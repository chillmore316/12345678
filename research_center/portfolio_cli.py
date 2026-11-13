"""Explicit CLI for bounded shared-cash historical research."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
import uuid

from .store import ROOT
from .portfolio_data import load_aligned_frames
from .engine import DEFAULT_DATA_ROOT

DEFAULT_STORE = ROOT / "results/research_center_portfolios"


def write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode()
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="共享资金组合历史研究")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "run"):
        command = actions.add_parser(name)
        command.add_argument("--file", type=Path, required=True)
    show = actions.add_parser("show")
    show.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.action == "show":
        if not re.fullmatch(r"[a-f0-9]{32}", args.run_id):
            parser.error("运行编号无效")
        print((args.store / args.run_id / "receipt.json").read_text())
        return 0
    with args.file.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        parser.error("配置文件最多 64 KiB")
    from .portfolio_kernel import normalize_portfolio, initial_state, advance, result_from_state
    config = normalize_portfolio(json.loads(raw))
    if args.action == "plan":
        print(json.dumps({"config": config, "mode": "RESEARCH_ONLY", "paper_admission": False}, ensure_ascii=False))
        return 0
    run_id = uuid.uuid4().hex
    root = args.store.expanduser().resolve()
    blocked = (ROOT / "data", ROOT / "results/research_center", Path(DEFAULT_DATA_ROOT).resolve())
    if "quantlivepaper" in {part.casefold() for part in root.parts} or any(root.is_relative_to(path) for path in blocked):
        parser.error("组合输出必须使用独立的研究目录")
    directory = root / run_id
    directory.mkdir(parents=True, mode=0o700)
    receipt = dict(schema_version=2, run_id=run_id, status="running", created_at=datetime.now(timezone.utc).isoformat(),
                   mode="RESEARCH_ONLY", paper_admission=False, artifact_directory=str(directory))
    write_json(directory / "receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    try:
        write_json(directory / "config.json", config)
        from .portfolio_provenance import snapshot_sources, verify_sources
        snapshot = snapshot_sources(ROOT)
        write_json(directory / "source_snapshot.json", snapshot)
        receipt["code_version"] = snapshot["source_version"]
        write_json(directory / "receipt.json", receipt)
        verify_sources(ROOT, snapshot)
        single_configs = config["symbol_configs"]
        frames, manifest = load_aligned_frames(single_configs)
        state = advance(config, initial_state(config), frames, final=True)
        result = result_from_state(config, state)
        from .data_evidence import assess_data
        readiness = {}
        for single in single_configs:
            symbol = single["symbol"]
            readiness[symbol] = assess_data(single, {
                "bars": [frame[symbol] for frame in frames],
                "data_manifest": manifest["sources"][symbol],
            })
        result["data_readiness"] = {
            "status": "mismatch" if any(item["status"] == "mismatch" for item in readiness.values()) else "unverified",
            "by_symbol": readiness, "paper_admission": False,
        }
        verify_sources(ROOT, snapshot)
        result["provenance"] = {
            "source_version": snapshot["source_version"], "runtime": snapshot["runtime"],
            "source_snapshot": "source_snapshot.json",
            "scope": "current_sources_match_archived_snapshot_before_and_after_execution",
        }
        write_json(directory / "data_manifest.json", manifest)
        write_json(directory / "state.json", state)
        write_json(directory / "result.json", result)
        hashes = {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                  for file in directory.iterdir() if file.suffix == ".json" and file.name != "receipt.json"}
        receipt.update(status="completed", frames=len(frames), artifact_sha256=hashes)
    except Exception as error:
        receipt.update(status="failed", error=str(error)[:1000])
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(directory / "receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return 0 if receipt["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
