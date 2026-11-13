"""Durable, single-flight orchestration for bounded research workflows.

The HTTP layer may submit jobs, but only this module's fixed worker entrypoint
executes a previously normalized batch or rolling plan. Job state lives beside
the supplied research store; reads reconcile dead workers and never relaunch.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import uuid
from typing import Any, Iterator


_KINDS = frozenset({"batch", "rolling", "symbols"})
_ACTIVE = frozenset({"queued", "running"})
_TERMINAL = frozenset({"completed", "failed", "timed_out", "interrupted", "cancelled"})
_MAX_SPEC_BYTES = 64 * 1024
_MAX_CASES = 8  # 8 batch cases or 4 rolling folds x 2 windows.
_WAIT_SECONDS_PER_CASE = 300.0
_POLL_INTERVAL = 0.25
_MAX_WORKFLOW_SECONDS = int(_MAX_CASES * _WAIT_SECONDS_PER_CASE + 30)
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_json_value(value: Any, path: str = "spec") -> None:
    """Reject Python-only values before canonical hashing or persistence."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_json_value(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} object keys must be strings")
            _require_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain only JSON values")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow request must be valid JSON") from exc


def _load_record(raw: str) -> dict[str, Any]:
    record = json.loads(raw)
    if type(record) is not dict:
        raise RuntimeError("workflow job record is not an object")
    return record


def _request_id(value: Any) -> str:
    if type(value) is not str or _REQUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("request_id must be a lowercase UUIDv4 in 32-hex form")
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError as exc:
        raise ValueError("request_id must be a lowercase UUIDv4 in 32-hex form") from exc
    if parsed.version != 4 or parsed.hex != value:
        raise ValueError("request_id must be a lowercase UUIDv4 in 32-hex form")
    return value


def _job_id(value: Any) -> str:
    if type(value) is not str or _JOB_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid workflow job_id")
    return value


def _plan_for(root: Path, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("kind must be batch, rolling or symbols")
    if type(spec) is not dict:
        raise ValueError("spec must be a JSON object")
    _require_json_value(spec)
    if kind == "batch":
        from .batch import plan_batch

        plan = plan_batch(_RootOnlyStore(root), deepcopy(spec))
    elif kind == "rolling":
        from .rolling import plan_rolling

        plan = plan_rolling(_RootOnlyStore(root), deepcopy(spec))
    else:
        from .symbol_comparison import plan_symbols

        plan = plan_symbols(_RootOnlyStore(root), deepcopy(spec))
    # Keep the public result and later durable record within strict JSON bounds.
    encoded = _canonical(plan).encode("utf-8")
    if len(encoded) > _MAX_SPEC_BYTES * 8:
        raise ValueError("normalized workflow plan is too large")
    return plan


class _RootOnlyStore:
    """Plan adapters may inspect the root but must not initialize the run DB."""

    def __init__(self, root: Path) -> None:
        self.root = root


def _fix_experiment_identity(plan: dict[str, Any], job_id: str) -> dict[str, Any]:
    normalized = deepcopy(plan)
    normalized["experiment_id"] = job_id
    for case in normalized["cases"]:
        config = case.get("config")
        if type(config) is not dict:
            raise ValueError("normalized workflow case has no config object")
        config["experiment_id"] = job_id
    return normalized


def _initial_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "queued",
        "experiment_id": plan["experiment_id"],
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "wait_seconds_per_case": _WAIT_SECONDS_PER_CASE,
        "cases": [],
    }
    for case in plan["cases"]:
        entry = {
            "index": case["index"],
            "label": case["label"],
            "status": "not_started",
            "run_status": None,
            "run_id": None,
        }
        for key in ("fold_index", "role", "window"):
            if key in case:
                entry[key] = deepcopy(case[key])
        receipt["cases"].append(entry)
    if "validation_scope" in plan:
        receipt.update(
            validation_scope=plan["validation_scope"],
            untouched_oos=plan.get("untouched_oos", False),
            automatic_selection=plan.get("automatic_selection", False),
            fold_count=plan.get("fold_count"),
            folds=deepcopy(plan.get("folds", [])),
        )
    if "scope" in plan:
        receipt["scope"] = deepcopy(plan["scope"])
    return receipt


def _event_receipt(previous: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    receipt = deepcopy(previous)
    event_type = event.get("type")
    if event_type == "planned":
        receipt["status"] = "running"
    full = event.get("receipt")
    if type(full) is dict:
        rolling_fields = {
            key: deepcopy(receipt[key])
            for key in ("validation_scope", "untouched_oos", "automatic_selection", "fold_count", "folds")
            if key in receipt
        }
        receipt = deepcopy(full)
        receipt.update(rolling_fields)
    case = event.get("case")
    if type(case) is dict:
        index = case.get("index")
        if type(index) is int and 1 <= index <= len(receipt.get("cases", [])):
            receipt["cases"][index - 1].update(deepcopy(case))
    return receipt


class WorkflowJobs:
    """Persist and execute one bounded research workflow at a time per store root."""

    def __init__(self, store: Any) -> None:
        try:
            self.root = Path(store.root).resolve()
        except (AttributeError, TypeError, OSError) as exc:
            raise ValueError("store must provide a filesystem root") from exc
        self.db_path = self.root / "workflow_jobs.sqlite3"
        self.lock_path = self.root / "workflow_jobs.lock"

    def preview(self, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Validate and return a plan without initializing or writing the store."""
        plan = _plan_for(self.root, kind, spec)
        return deepcopy(plan)

    def submit(self, kind: str, spec: dict[str, Any], request_id: str) -> dict[str, Any]:
        request_id = _request_id(request_id)
        if type(kind) is not str or kind not in _KINDS:
            raise ValueError("kind must be batch, rolling or symbols")
        if type(spec) is not dict:
            raise ValueError("spec must be a JSON object")
        _require_json_value(spec)
        digest_payload = {"kind": kind, "spec": spec}
        canonical = _canonical(digest_payload)
        if len(canonical.encode("utf-8")) > _MAX_SPEC_BYTES:
            raise ValueError("workflow spec exceeds 64 KiB")
        content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        # Exact retries remain retrievable even if strategy configuration or
        # installed code changed after the original job was admitted.
        with self._root_lock(create=False):
            existing = self._find_request(request_id)
            if existing is not None:
                return self._same_request(existing, content_sha256)

        plan = _plan_for(self.root, kind, spec)
        job_id = uuid.uuid4().hex
        launch_token = uuid.uuid4().hex
        plan = _fix_experiment_identity(plan, job_id)
        now = _utc_now()
        record = {
            "schema_version": 1,
            "job_id": job_id,
            "experiment_id": job_id,
            "request_id": request_id,
            "kind": kind,
            "status": "queued",
            "mode": "RESEARCH_ONLY",
            "paper_admission": False,
            "content_sha256": content_sha256,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "plan": plan,
            "receipt": _initial_receipt(plan),
            "last_event": None,
            "error": None,
        }

        # Root flock serializes idempotency, active-job admission, and the
        # short Popen/pid-recording window across HTTP and worker processes.
        with self._root_lock(create=True):
            existing = self._find_request(request_id)
            if existing is not None:
                return self._same_request(existing, content_sha256)
            self._reconcile_active_locked()
            active = self._active_record()
            if active is not None:
                raise ValueError("已有研究实验运行，请等待其终止后再提交")
            self._ensure_database()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO jobs(job_id, request_id, content_sha256, launch_token, record) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job_id, request_id, content_sha256, launch_token, _canonical(record)),
                )
                connection.commit()

            try:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "research_center.workflow_jobs",
                        "--store",
                        str(self.root),
                        "--job-id",
                        job_id,
                        "--token",
                        launch_token,
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env={
                        **os.environ,
                        "OMP_NUM_THREADS": "1",
                        "OPENBLAS_NUM_THREADS": "1",
                        "MKL_NUM_THREADS": "1",
                        "NUMEXPR_NUM_THREADS": "1",
                    },
                )
            except Exception as exc:
                record.update(status="failed", finished_at=_utc_now(), error=_safe_error(exc))
                self._save_record(record)
                return deepcopy(record)

            try:
                record["pid"] = worker.pid
                self._save_record(record)
            except Exception as exc:
                # The worker is blocked on the same root lock until this block
                # exits, so it cannot claim the queued row before termination.
                try:
                    worker.terminate()
                    worker.wait(timeout=2)
                except Exception:
                    pass
                record.update(status="failed", finished_at=_utc_now(), error=_safe_error(exc))
                try:
                    self._save_record(record)
                except Exception:
                    # The committed queued row remains idempotently discoverable;
                    # its next read will fail closed as interrupted.
                    pass
            return deepcopy(record)

    def get(self, job_id: str) -> dict[str, Any]:
        job_id = _job_id(job_id)
        with self._root_lock(create=False):
            record = self._find_job(job_id)
            if record is None:
                raise FileNotFoundError("workflow job not found")
            return deepcopy(self._reconcile_record_locked(record))

    def list(self) -> dict[str, Any]:
        with self._root_lock(create=False):
            if not self.db_path.exists():
                return {"jobs": []}
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT record FROM jobs ORDER BY rowid DESC LIMIT 100"
                ).fetchall()
            records = [_load_record(row[0]) for row in rows]
            summary_fields = ("job_id", "experiment_id", "request_id", "kind", "status", "created_at",
                              "mode", "paper_admission")
            jobs = [{key: record[key] for key in summary_fields}
                    for record in (self._reconcile_record_locked(row) for row in records)]
            return {"jobs": jobs}

    @contextmanager
    def _root_lock(self, *, create: bool) -> Iterator[None]:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if not create and not self.lock_path.exists() and not self.db_path.exists():
            yield
            return
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.lock_path, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _ensure_database(self) -> None:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.db_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, "
                "content_sha256 TEXT NOT NULL, launch_token TEXT NOT NULL, record TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _find_job(self, job_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT record FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return _load_record(row[0]) if row else None

    def _find_request(self, request_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
        return _load_record(row[0]) if row else None

    def _same_request(self, record: dict[str, Any], digest: str) -> dict[str, Any]:
        if record.get("content_sha256") != digest:
            raise ValueError("request_id was already used with different workflow content")
        return deepcopy(self._reconcile_record_locked(record))

    def _save_record(self, record: dict[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET record=? WHERE job_id=?",
                (_canonical(record), record["job_id"]),
            )
            connection.commit()

    def _active_record(self) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record FROM jobs WHERE json_extract(record, '$.status') IN ('queued', 'running') "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchall()
        for row in rows:
            record = _load_record(row[0])
            if record.get("status") in _ACTIVE:
                return record
        return None

    def _reconcile_active_locked(self) -> None:
        active = self._active_record()
        if active is not None:
            self._reconcile_record_locked(active)

    def _reconcile_record_locked(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("status") not in _ACTIVE:
            return record
        pid = record.get("pid")
        if type(pid) is int and pid > 0 and _worker_is_alive(pid, record["job_id"], self.root) is not False:
            return record
        record = deepcopy(record)
        record.update(
            status="interrupted",
            finished_at=_utc_now(),
            error="no active worker for this store and job; workflow was not re-admitted",
        )
        receipt = record.get("receipt")
        if type(receipt) is dict:
            receipt["status"] = "interrupted"
            receipt["stop_reason"] = "no active workflow worker for this store; submitted runs remain separately inspectable"
        self._save_record(record)
        return record

    def _claim_job(self, job_id: str, token: str) -> dict[str, Any] | None:
        with self._root_lock(create=False):
            if not self.db_path.exists():
                return None
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT launch_token, record FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None or row[0] != token:
                    connection.rollback()
                    return None
                record = _load_record(row[1])
                if record.get("status") != "queued":
                    connection.rollback()
                    return None
                record.update(status="running", pid=os.getpid(), started_at=_utc_now())
                if isinstance(record.get("receipt"), dict):
                    record["receipt"]["status"] = "running"
                connection.execute(
                    "UPDATE jobs SET record=? WHERE job_id=?",
                    (_canonical(record), job_id),
                )
                connection.commit()
                return record

    def _persist_progress(self, job_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        with self._root_lock(create=False):
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT record FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise RuntimeError("workflow record is missing")
                record = _load_record(row[0])
                if record.get("status") != "running":
                    connection.rollback()
                    raise RuntimeError("workflow is no longer running")
                previous = record.get("receipt") or {}
                receipt = _event_receipt(previous, event)
                record.update(receipt=receipt, last_event=deepcopy(event))
                connection.execute(
                    "UPDATE jobs SET record=? WHERE job_id=?",
                    (_canonical(record), job_id),
                )
                connection.commit()
                return receipt

    def _finish_worker(
        self,
        job_id: str,
        status: str,
        *,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in _TERMINAL:
            status = "failed"
        with self._root_lock(create=False):
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT record FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    return
                record = _load_record(row[0])
                if record.get("status") != "running":
                    connection.rollback()
                    return
                final_receipt = deepcopy(receipt if receipt is not None else record.get("receipt"))
                if type(final_receipt) is dict:
                    final_receipt["status"] = status
                record.update(
                    status=status,
                    finished_at=_utc_now(),
                    receipt=final_receipt,
                    error=error,
                )
                connection.execute(
                    "UPDATE jobs SET record=? WHERE job_id=?",
                    (_canonical(record), job_id),
                )
                connection.commit()


def _safe_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = type(exc).__name__
    return message[:1000]


def _worker_is_alive(pid: int, job_id: str, store_root: Path) -> bool | None:
    """Return None when process identity cannot be determined safely."""
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    try:
        completed = subprocess.run(
            ["/bin/ps", "-ww", "-o", "stat=", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = completed.stdout.strip().split(maxsplit=1)
    if completed.returncode != 0 or len(fields) != 2:
        return None
    state, command = fields
    if state.startswith("Z"):
        return False
    worker, separator, token = command.rpartition(" --token ")
    expected = f" -m research_center.workflow_jobs --store {store_root} --job-id {job_id}"
    return bool(separator and worker.endswith(expected) and _JOB_ID_RE.fullmatch(token))


def _run_job(store: Any, record: dict[str, Any], jobs: WorkflowJobs) -> None:
    from .batch import _run_normalized_plan

    plan = record["plan"]
    case_metadata = {
        case["label"]: {
            key: deepcopy(case[key])
            for key in ("fold_index", "role", "window")
            if key in case
        }
        for case in plan["cases"]
    }

    def progress(event: dict[str, Any]) -> None:
        annotated = deepcopy(event)
        case = annotated.get("case")
        if type(case) is dict:
            case.update(case_metadata.get(case.get("label"), {}))
        for item in annotated.get("cases", []) if type(annotated.get("cases")) is list else []:
            item.update(case_metadata.get(item.get("label"), {}))
        receipt = annotated.get("receipt")
        if type(receipt) is dict:
            if "scope" in plan:
                receipt["scope"] = deepcopy(plan["scope"])
            for item in receipt.get("cases", []):
                item.update(case_metadata.get(item.get("label"), {}))
        jobs._persist_progress(record["job_id"], annotated)

    previous_alarm = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("workflow exceeded its total time bound")))
    signal.alarm(_MAX_WORKFLOW_SECONDS)
    try:
        receipt = _run_normalized_plan(
            store,
            plan,
            wait_seconds=_WAIT_SECONDS_PER_CASE,
            poll_interval=_POLL_INTERVAL,
            on_event=progress,
        )
        if record["kind"] == "rolling":
            receipt.update(
                validation_scope=plan["validation_scope"],
                untouched_oos=plan["untouched_oos"],
                automatic_selection=plan["automatic_selection"],
                fold_count=plan["fold_count"],
                folds=deepcopy(plan["folds"]),
            )
            for case in receipt["cases"]:
                case.update(case_metadata.get(case.get("label"), {}))
        elif record["kind"] == "symbols":
            receipt["scope"] = deepcopy(plan["scope"])
        status = receipt.get("status")
        if status not in _TERMINAL:
            status = "failed"
        jobs._finish_worker(record["job_id"], status, receipt=receipt)
    except TimeoutError as exc:
        latest = jobs.get(record["job_id"]).get("receipt")
        jobs._finish_worker(record["job_id"], "timed_out", receipt=latest, error=_safe_error(exc))
    except KeyboardInterrupt as exc:
        latest = jobs.get(record["job_id"]).get("receipt")
        jobs._finish_worker(record["job_id"], "interrupted", receipt=latest, error=_safe_error(exc))
    except Exception as exc:
        latest = jobs.get(record["job_id"]).get("receipt")
        jobs._finish_worker(record["job_id"], "failed", receipt=latest, error=_safe_error(exc))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)


def _worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="private bounded research workflow worker")
    parser.add_argument("--store", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    try:
        job_id = _job_id(args.job_id)
        store_root = Path(args.store).resolve()
        jobs = WorkflowJobs(_RootOnlyStore(store_root))
        record = jobs._claim_job(job_id, args.token)
        if record is None:
            return 0
        from .store import RunStore

        # Always provide the explicit persisted root; never use DEFAULT_STORE.
        store = RunStore(store_root)
        _run_job(store, record, jobs)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker_main())
