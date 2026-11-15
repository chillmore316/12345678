"""Bounded, supervised CLI for shared-cash historical portfolio research."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
import uuid

from .store import ROOT
from .portfolio_data import load_aligned_frames
from .engine import DEFAULT_DATA_ROOT

DEFAULT_STORE = ROOT / "results/research_center_portfolios"
_RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_ARTIFACT_NAMES = frozenset({
    "config.json", "data_manifest.json", "state.json", "result.json", "source_snapshot.json",
})
_ARTIFACT_LIMITS = {
    "config.json": 128 * 1024,
    "data_manifest.json": 8 * 1024 * 1024,
    "state.json": 128 * 1024 * 1024,
    "result.json": 64 * 1024 * 1024,
    "source_snapshot.json": 8 * 1024 * 1024,
}
_CONTROL_DIR = ".control"
_CONTROL_LIMIT = 128 * 1024
_STORE_LOCK = ".portfolio_execution.lock"
_MAX_ARCHIVE_ENTRIES = 10000
# Full fixed-source map of the synchronous producer published at fa70bb3.
# Schema 2 alone cannot distinguish it from later supervised producers.
_LEGACY_SYNCHRONOUS_SOURCE_MAPS = frozenset({
    "264683a30afb3d4da1aba956662f41b1aadc0ab4ecae3cc839e3ab9701724f96",
})
_BOOTSTRAP = (
    "import json,os,resource,signal,sys,time;"
    "project_root=os.path.abspath(bytes.fromhex(sys.argv[1]).decode('utf-8'));"
    "root=os.path.abspath(bytes.fromhex(sys.argv[2]).decode('utf-8'));"
    "run_id=sys.argv[3];timeout_arg=sys.argv[4];timeout=float(timeout_arg);"
    "limit=int(timeout)+5;soft,hard=resource.getrlimit(resource.RLIMIT_CPU);"
    "limit=min(limit,soft) if soft!=resource.RLIM_INFINITY else limit;"
    "limit=min(limit,hard) if hard!=resource.RLIM_INFINITY else limit;"
    "resource.setrlimit(resource.RLIMIT_CPU,(limit,limit));"
    "signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(int(timeout)+5);"
    "token=sys.stdin.readline(129).strip();"
    "sys.exit(70) if len(run_id)!=32 or any(c not in '0123456789abcdef' for c in run_id) or len(token)!=32 else None;"
    "control=os.path.join(root,run_id,'.control');"
    "owner_raw=open(os.path.join(control,'owner'),encoding='utf-8').read(131073);"
    "sys.exit(72) if len(owner_raw)>131072 else None;owner=json.loads(owner_raw);"
    "sys.exit(71) if owner.get('token')!=token or owner.get('run_id')!=run_id or owner.get('store_root')!=root else None;"
    "started={'token':token,'run_id':run_id,'store_root':root,'pid':os.getpid(),'pgid':os.getpgrp(),'timeout_arg':timeout_arg,'python_executable':sys.executable};"
    "payload=json.dumps(started,separators=(',',':')).encode('utf-8');"
    "tmp=os.path.join(control,'started.'+str(os.getpid())+'.tmp');"
    "fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.write(fd,payload);os.fsync(fd);os.close(fd);"
    "os.replace(tmp,os.path.join(control,'started'));"
    "sys.path.insert(0,project_root);"
    "module=__import__('research_center.portfolio_cli',fromlist=['_portfolio_worker_entry']);"
    "raise SystemExit(getattr(module,'_portfolio_worker_entry')(root,run_id,token,timeout))"
)


class PortfolioExecutionBusy(RuntimeError):
    """Raised when the archive root has a live or unidentifiable worker."""


class _RunFailure(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.public_message = message


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def _atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600):
    path = Path(path)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def write_json(path, value):
    """Atomically write one finite JSON artifact."""
    _atomic_write_bytes(Path(path), _json_bytes(value))


def _read_bytes(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise ValueError("组合内部文件类型或大小无效")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("组合内部文件过大")
    return content


def _read_json(path: Path, limit: int):
    def reject_constant(_):
        raise ValueError("组合内部 JSON 含非有限数值")
    return json.loads(_read_bytes(path, limit), parse_constant=reject_constant)


def _control_path(run_directory: Path, name: str) -> Path:
    return run_directory / _CONTROL_DIR / name


def _write_phase(run_directory: Path, *, phase: str, token: str, run_id: str,
                 store_root: Path, pid: int | None = None, timeout_arg: str | None = None,
                 python_executable: str | None = None, cleanup_confirmed: bool | None = None,
                 exit_code: int | None = None):
    value = {
        "phase": phase,
        "token": token,
        "run_id": run_id,
        "store_root": str(store_root),
        "execution_pid": pid,
        "timeout_arg": timeout_arg,
        "python_executable": python_executable,
        "cleanup_confirmed": cleanup_confirmed,
        "exit_code": exit_code,
    }
    write_json(_control_path(run_directory, "phase"), value)


def _normal_store_root(store_root) -> Path:
    if not isinstance(store_root, (str, os.PathLike)):
        raise ValueError("组合输出目录无效")
    try:
        root = Path(store_root).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        raise ValueError("组合输出目录无效") from None
    blocked = (
        (ROOT / "data").resolve(),
        (ROOT / "results/research_center").resolve(),
        Path(DEFAULT_DATA_ROOT).resolve(),
    )
    parts = {part.casefold() for part in root.parts}
    if "quantlivepaper" in parts or any(root == path or root.is_relative_to(path) for path in blocked):
        raise ValueError("组合输出必须使用独立的研究目录")
    return root


def _validated_timeout(value) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout_seconds 必须在 0.1 至 300 秒内")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout_seconds 必须在 0.1 至 300 秒内") from None
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 300:
        raise ValueError("timeout_seconds 必须在 0.1 至 300 秒内")
    return timeout


def _validated_run_id(run_id) -> str:
    if run_id is None:
        return uuid.uuid4().hex
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("组合运行编号无效")
    return run_id


def _acquire_store_lock(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(root / _STORE_LOCK, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("组合执行锁文件类型无效")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise PortfolioExecutionBusy("组合执行目录正被占用，拒绝并行执行") from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _safe_receipt(directory: Path):
    try:
        value = _read_json(directory / "receipt.json", 32 * 1024)
    except (OSError, ValueError, TypeError):
        return None
    return value if type(value) is dict else None


def _assert_no_unreaped_runs(root: Path):
    try:
        with os.scandir(root) as source:
            import itertools
            entries = [Path(entry.path) for entry in itertools.islice(source, _MAX_ARCHIVE_ENTRIES + 1)]
    except FileNotFoundError:
        return
    if len(entries) > _MAX_ARCHIVE_ENTRIES:
        raise PortfolioExecutionBusy("组合归档条目超过有界检查范围，请使用独立研究目录")
    for entry in entries:
        if _RUN_ID.fullmatch(entry.name) is None:
            continue
        try:
            if not stat.S_ISDIR(entry.lstat().st_mode):
                continue
        except OSError:
            continue
        alive = execution_is_alive(root, entry.name)
        if alive is not False:
            raise PortfolioExecutionBusy("存在仍在运行或无法确认已回收的组合 worker")


def _private_control(directory: Path, name: str):
    control_dir = directory / _CONTROL_DIR
    try:
        if not stat.S_ISDIR(control_dir.lstat().st_mode):
            return None
        value = _read_json(control_dir / name, _CONTROL_LIMIT)
    except (OSError, ValueError, TypeError):
        return None
    return value if type(value) is dict else None


def _expected_worker_argv(root: Path, run_id: str, timeout_arg: str, python_executable: str) -> list[str]:
    return [python_executable, "-I", "-c", _BOOTSTRAP,
            os.fsencode(str(ROOT)).hex(), os.fsencode(str(root)).hex(), run_id, timeout_arg]


def _process_identity(pid: int, expected_pgid: int, expected_argv: list[str]) -> bool | None:
    if pid <= 0 or expected_pgid != pid:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "pid=,pgid=,stat=,command="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=1.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return None
    fields = output.split(None, 3)
    if len(fields) != 4:
        return None
    try:
        observed_pid, observed_pgid = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    if observed_pid != pid or observed_pgid != expected_pgid:
        return False
    if fields[2].startswith(("Z", "X")):
        return False
    command = fields[3]
    # macOS ps emits the original argv joined by spaces. All dynamic arguments
    # except the executable are hex or validated tokens, so this is unambiguous.
    expected_command = " ".join(expected_argv)
    if command != expected_command:
        if len(command) < len(expected_command) and expected_command.startswith(command):
            return None
        # A code upgrade can change the fixed bootstrap while an older owned
        # worker still runs. Matching archive/run arguments are uncertainty,
        # never proof that that worker has ended.
        if (command.startswith(expected_argv[0] + " -I -c ")
                and command.endswith(" " + " ".join(expected_argv[-3:]))):
            return None
        return False
    return True


def execution_is_alive(store_root, run_id) -> bool | None:
    """Read-only liveness check for this runner's exact child process group.

    False means no owned worker remains, True means the fixed worker command is
    present, and None means the process state could not be established safely.
    """
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        return False
    try:
        root = Path(store_root).expanduser().resolve()
        directory = root / run_id
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return False
    except (OSError, TypeError, ValueError):
        return False

    owner = _private_control(directory, "owner")
    phase = _private_control(directory, "phase")
    started = _private_control(directory, "started")
    if owner is None:
        receipt = _safe_receipt(directory)
        if (receipt is None or receipt.get("run_id") != run_id
                or receipt.get("mode") != "RESEARCH_ONLY" or receipt.get("paper_admission") is not False):
            return None
        if receipt.get("status") in {"completed", "failed", "timed_out", "cancelled", "interrupted"}:
            if "cleanup_confirmed" in receipt:
                return False if receipt["cleanup_confirmed"] is True else None
            # A missing owner/flag is not proof of cleanup. Recognize only
            # pre-supervision schemas or the verified synchronous source corpus.
            try:
                (directory / _CONTROL_DIR).lstat()
            except FileNotFoundError:
                schema = receipt.get("schema_version", 1)
                if type(schema) is int and schema == 1:
                    return False
                if type(schema) is int and schema == 2:
                    try:
                        from .portfolio_provenance import validate_snapshot
                        snapshot = json.loads(_read_bytes(
                            directory / "source_snapshot.json", _ARTIFACT_LIMITS["source_snapshot.json"]))
                        validate_snapshot(snapshot)
                        hashes = {path: entry["sha256"] for path, entry in snapshot["files"].items()}
                        digest = hashlib.sha256(json.dumps(
                            hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        if (snapshot["source_version"] == receipt.get("code_version")
                                and digest in _LEGACY_SYNCHRONOUS_SOURCE_MAPS):
                            return False
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                return None
            except OSError:
                return None
            return None
        return None
    try:
        root_text = str(root)
        token = owner["token"]
        owner_root = owner.get("store_root")
        if (owner.get("run_id") != run_id or type(token) is not str
                or re.fullmatch(r"[a-f0-9]{32}", token) is None or type(owner_root) is not str):
            return None
        if owner_root != root_text:
            # A copied store has different path ownership and cannot claim the
            # process still running for the original root.
            return False if owner.get("store_root") != root_text else None
        if phase is not None:
            if phase.get("token") != token or phase.get("run_id") != run_id or phase.get("store_root") != root_text:
                return None
            if phase.get("phase") == "not_started":
                return False
            if phase.get("phase") == "closed" and phase.get("cleanup_confirmed") is True:
                return False
        candidate = started if started is not None else phase
        if candidate is None:
            return None
        if (candidate.get("token") != token or candidate.get("run_id") != run_id
                or candidate.get("store_root") != root_text):
            return None
        pid = candidate.get("pid", candidate.get("execution_pid"))
        pgid = candidate.get("pgid", candidate.get("execution_pid"))
        timeout_arg = candidate.get("timeout_arg")
        python_executable = candidate.get("python_executable")
        if (type(pid) is not int or type(pgid) is not int or type(timeout_arg) is not str
                or type(python_executable) is not str or not timeout_arg):
            if phase and phase.get("phase") == "spawning":
                return None
            return None
        expected = _expected_worker_argv(root, run_id, timeout_arg, python_executable)
        alive = _process_identity(pid, pgid, expected)
        if alive is False and phase and phase.get("cleanup_confirmed") is False and _group_exists(pgid) is not False:
            return None
        return alive
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _worker_environment() -> dict[str, str]:
    return {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "TMPDIR": "/tmp"}


def _spawn_worker(root: Path, run_id: str, timeout_seconds: float, token: str):
    timeout_arg = repr(float(timeout_seconds))
    argv = _expected_worker_argv(root, run_id, timeout_arg, os.path.abspath(sys.executable))
    process = subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=_worker_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdin is not None:
        try:
            process.stdin.write((token + "\n").encode("ascii"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
    return process


def _cancel_requested(cancel_path) -> bool:
    if cancel_path is None:
        return False
    try:
        return Path(cancel_path).exists()
    except (OSError, TypeError, ValueError):
        return False


def _wait_for_worker(process, deadline_ns: int, cancel_path):
    while True:
        if _cancel_requested(cancel_path):
            return "cancelled", process.poll()
        return_code = process.poll()
        if return_code is not None:
            return None, return_code
        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            return "timed_out", None
        time.sleep(min(0.05, remaining_ns / 1_000_000_000))


def _group_exists(process_group_id: int) -> bool | None:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def _terminate_owned_group(process, grace_seconds: float = 0.35) -> bool:
    """Terminate and reap only the Popen-owned new process group."""
    if process.returncode is not None:
        # A reaped leader no longer reserves its PID. Never signal a group
        # that may have acquired that number afterwards.
        return _reap_completed_worker(process)
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    # Do not poll/wait here: retaining the unreaped leader reserves the group
    # identifier until both group signals have been sent.
    time.sleep(grace_seconds)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    try:
        process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        return False
    exists = _group_exists(process_group_id)
    # A signal can report EPERM for a just-exited zombie on macOS. The final
    # observation, after reaping, establishes whether cleanup actually ended.
    return process.poll() is not None and exists is False


def _reap_completed_worker(process) -> bool:
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return process.poll() is not None and _group_exists(process.pid) is False


def _receipt(run_id: str, directory: Path):
    return {
        "schema_version": 2,
        "run_id": run_id,
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "paper_admission": False,
        "artifact_directory": str(directory),
        "cleanup_confirmed": False,
    }


def _terminalize(receipt: dict, directory: Path, status: str, *, cleanup_confirmed: bool,
                 error: str | None = None):
    receipt["status"] = status
    receipt["cleanup_confirmed"] = bool(cleanup_confirmed)
    if error:
        receipt["error"] = error[:256]
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(directory / "receipt.json", receipt)
    return receipt


def _source_snapshot():
    from .portfolio_provenance import snapshot_sources, validate_snapshot
    snapshot = snapshot_sources(ROOT)
    validate_snapshot(snapshot)
    return snapshot


def _verify_source_snapshot(snapshot):
    from .portfolio_provenance import verify_sources
    verify_sources(ROOT, snapshot)


def _read_completion(directory: Path, *, token: str, run_id: str, store_root: Path,
                     expected_source_version: str | None, started_ns: int, deadline_ns: int):
    control = directory / _CONTROL_DIR
    completion = _read_json(control / "completion", _CONTROL_LIMIT)
    expected_fields = {
        "schema_version", "run_id", "store_root", "token", "config_sha256",
        "source_version", "frames", "artifact_sha256", "completed_monotonic_ns",
    }
    if type(completion) is not dict or set(completion) != expected_fields:
        raise ValueError("worker completion protocol invalid")
    if (completion["schema_version"] != 1 or completion["run_id"] != run_id
            or completion["store_root"] != str(store_root) or completion["token"] != token):
        raise ValueError("worker completion identity invalid")
    source_version = completion["source_version"]
    if type(source_version) is not str or _SHA256.fullmatch(source_version) is None:
        raise ValueError("worker completion version invalid")
    if expected_source_version is not None and source_version != expected_source_version:
        raise _RunFailure("failed", "实际执行源码版本与计划版本不一致")
    try:
        finished_ns = completion["completed_monotonic_ns"]
        frames = completion["frames"]
    except KeyError:
        raise ValueError("worker completion timing invalid") from None
    if (type(finished_ns) is not int or finished_ns < started_ns or finished_ns > deadline_ns
            or type(frames) is not int or frames <= 0):
        raise _RunFailure("timed_out" if type(finished_ns) is int and finished_ns > deadline_ns else "failed",
                          "组合 worker 未在时限内完成")
    hashes = completion["artifact_sha256"]
    if (type(hashes) is not dict or set(hashes) != _ARTIFACT_NAMES
            or any(type(value) is not str or _SHA256.fullmatch(value) is None for value in hashes.values())):
        raise ValueError("worker completion hashes invalid")
    snapshot_raw = _read_bytes(directory / "source_snapshot.json", _ARTIFACT_LIMITS["source_snapshot.json"])
    config_raw = _read_bytes(directory / "config.json", _ARTIFACT_LIMITS["config.json"])
    if (hashlib.sha256(snapshot_raw).hexdigest() != hashes["source_snapshot.json"]
            or hashlib.sha256(config_raw).hexdigest() != hashes["config.json"]
            or hashlib.sha256(config_raw).hexdigest() != completion["config_sha256"]):
        raise ValueError("worker completion base artifact hashes invalid")
    snapshot = json.loads(snapshot_raw)
    from .portfolio_provenance import validate_snapshot
    validate_snapshot(snapshot)
    if snapshot.get("source_version") != source_version:
        raise ValueError("worker completion source snapshot mismatch")

    staged = control / "staging"
    manifest_raw = _read_bytes(staged / "data_manifest.json", _ARTIFACT_LIMITS["data_manifest.json"])
    result_raw = _read_bytes(staged / "result.json", _ARTIFACT_LIMITS["result.json"])
    state_raw = _read_bytes(staged / "state.json", _ARTIFACT_LIMITS["state.json"])
    if (hashlib.sha256(manifest_raw).hexdigest() != hashes["data_manifest.json"]
            or hashlib.sha256(result_raw).hexdigest() != hashes["result.json"]
            or hashlib.sha256(state_raw).hexdigest() != hashes["state.json"]):
        raise ValueError("worker completion staged artifact hashes invalid")
    manifest = json.loads(manifest_raw)
    result = json.loads(result_raw)
    if (type(manifest) is not dict or manifest.get("frames") != frames
            or manifest.get("paper_admission") is not False or manifest.get("mode") != "RESEARCH_ONLY"):
        raise ValueError("worker completion data manifest invalid")
    if (type(result) is not dict or result.get("paper_admission") is not False
            or result.get("mode") != "RESEARCH_ONLY"):
        raise ValueError("worker completion result invalid")
    provenance = result.get("provenance")
    if (type(provenance) is not dict or provenance.get("source_version") != source_version
            or provenance.get("runtime") != snapshot.get("runtime")):
        raise ValueError("worker completion result provenance invalid")
    return hashes, frames, source_version


def _promote_staged_artifacts(directory: Path):
    staged = directory / _CONTROL_DIR / "staging"
    for name in ("data_manifest.json", "state.json", "result.json"):
        os.replace(staged / name, directory / name)


def _remove_published_artifacts(directory: Path):
    for name in ("data_manifest.json", "state.json", "result.json"):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass


def _portfolio_worker_entry(root_text: str, run_id: str, token: str, timeout_seconds: float):
    """Private child entry. The stdlib bootstrap has already installed limits."""
    root = Path(root_text).resolve()
    directory = root / run_id
    control = directory / _CONTROL_DIR
    owner = _read_json(control / "owner", _CONTROL_LIMIT)
    if (type(owner) is not dict or owner.get("token") != token or owner.get("run_id") != run_id
            or owner.get("store_root") != str(root)):
        raise ValueError("worker owner identity invalid")
    config_raw = _read_bytes(directory / "config.json", _ARTIFACT_LIMITS["config.json"])
    source_raw = _read_bytes(directory / "source_snapshot.json", _ARTIFACT_LIMITS["source_snapshot.json"])
    if (hashlib.sha256(config_raw).hexdigest() != owner.get("config_sha256")
            or hashlib.sha256(source_raw).hexdigest() != owner.get("source_snapshot_sha256")):
        raise ValueError("worker control inputs changed")
    config = json.loads(config_raw)
    snapshot = json.loads(source_raw)
    from .portfolio_kernel import normalize_portfolio, initial_state, advance, result_from_state
    from .portfolio_provenance import validate_snapshot
    normalized = normalize_portfolio(config)
    if normalized != config:
        raise ValueError("worker config is not normalized")
    validate_snapshot(snapshot)
    source_version = snapshot["source_version"]
    if source_version != owner.get("source_version"):
        raise ValueError("worker source version changed")

    _verify_source_snapshot(snapshot)
    frames, manifest = load_aligned_frames(config["symbol_configs"])
    state = advance(config, initial_state(config), frames, final=True)
    result = result_from_state(config, state)
    from .data_evidence import assess_data
    readiness = {}
    for single in config["symbol_configs"]:
        symbol = single["symbol"]
        readiness[symbol] = assess_data(single, {
            "bars": [frame[symbol] for frame in frames],
            "data_manifest": manifest["sources"][symbol],
        })
    result["data_readiness"] = {
        "status": "mismatch" if any(item["status"] == "mismatch" for item in readiness.values()) else "unverified",
        "by_symbol": readiness,
        "paper_admission": False,
    }
    _verify_source_snapshot(snapshot)
    result["provenance"] = {
        "source_version": source_version,
        "runtime": snapshot["runtime"],
        "source_snapshot": "source_snapshot.json",
        "scope": "current_sources_match_archived_snapshot_before_and_after_execution",
    }

    stage = control / "staging"
    write_json(stage / "data_manifest.json", manifest)
    write_json(stage / "state.json", state)
    write_json(stage / "result.json", result)
    hashes = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("config.json", "source_snapshot.json")
    }
    hashes.update({
        name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
        for name in ("data_manifest.json", "state.json", "result.json")
    })
    completion = {
        "schema_version": 1,
        "run_id": run_id,
        "store_root": str(root),
        "token": token,
        "config_sha256": owner["config_sha256"],
        "source_version": source_version,
        "frames": len(frames),
        "artifact_sha256": hashes,
        "completed_monotonic_ns": time.monotonic_ns(),
    }
    write_json(control / "completion", completion)
    return 0


def run_bounded(config, store_root, *, run_id=None, timeout_seconds=300, cancel_path=None,
                expected_source_version=None) -> dict:
    """Run one isolated portfolio replay and publish a schema-2 receipt.

    The worker is a fixed Python entry point; config cannot select a command or
    environment. The parent process owns cancellation, timeout, reaping, and the
    sole completed receipt publication.
    """
    from .portfolio_kernel import normalize_portfolio

    normalized = normalize_portfolio(config)
    timeout = _validated_timeout(timeout_seconds)
    chosen_id = _validated_run_id(run_id)
    if expected_source_version is not None and (
            type(expected_source_version) is not str or _SHA256.fullmatch(expected_source_version) is None):
        raise ValueError("expected_source_version 必须是 64 位 SHA-256")
    root = _normal_store_root(store_root)
    lock_fd = _acquire_store_lock(root)
    try:
        _assert_no_unreaped_runs(root)
        directory = root / chosen_id
        directory.mkdir(mode=0o700, exist_ok=False)
    except BaseException:
        os.close(lock_fd)
        raise

    receipt = _receipt(chosen_id, directory)
    control = directory / _CONTROL_DIR
    stage = control / "staging"
    token = uuid.uuid4().hex
    process = None
    process_pid = None
    cleanup_confirmed = True
    started_ns = None
    deadline_ns = None
    final_status = "failed"
    final_error = None
    completion_hashes = None

    try:
        control.mkdir(mode=0o700)
        stage.mkdir(mode=0o700)
        config_bytes = _json_bytes(normalized)
        _atomic_write_bytes(directory / "config.json", config_bytes)
        snapshot = _source_snapshot()
        snapshot_bytes = _json_bytes(snapshot)
        _atomic_write_bytes(directory / "source_snapshot.json", snapshot_bytes)
        receipt["code_version"] = snapshot["source_version"]
        owner = {
            "token": token,
            "run_id": chosen_id,
            "store_root": str(root),
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "source_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "source_version": snapshot["source_version"],
        }
        write_json(_control_path(directory, "owner"), owner)
        _write_phase(directory, phase="not_started", token=token, run_id=chosen_id, store_root=root,
                     cleanup_confirmed=True)
        write_json(directory / "receipt.json", receipt)
        _verify_source_snapshot(snapshot)
        if (expected_source_version is not None
                and snapshot["source_version"] != expected_source_version):
            raise _RunFailure("failed", "实际执行源码版本与计划版本不一致")
        if _cancel_requested(cancel_path):
            raise _RunFailure("cancelled", "组合任务已取消")

        timeout_arg = repr(float(timeout))
        _write_phase(directory, phase="spawning", token=token, run_id=chosen_id, store_root=root,
                     timeout_arg=timeout_arg, python_executable=os.path.abspath(sys.executable),
                     cleanup_confirmed=False)
        started_ns = time.monotonic_ns()
        deadline_ns = started_ns + int(timeout * 1_000_000_000)
        process = _spawn_worker(root, chosen_id, timeout, token)
        process_pid = process.pid
        _write_phase(directory, phase="active", token=token, run_id=chosen_id, store_root=root,
                     pid=process_pid, timeout_arg=timeout_arg, python_executable=os.path.abspath(sys.executable),
                     cleanup_confirmed=False)
        write_json(directory / "receipt.json", receipt)
        requested_terminal, return_code = _wait_for_worker(process, deadline_ns, cancel_path)
        if requested_terminal:
            cleanup_confirmed = (
                _reap_completed_worker(process) if return_code is not None
                else _terminate_owned_group(process)
            )
            _write_phase(directory, phase="closed" if cleanup_confirmed else "cleanup_unconfirmed",
                         token=token, run_id=chosen_id, store_root=root, pid=process_pid,
                         timeout_arg=timeout_arg, python_executable=os.path.abspath(sys.executable),
                         cleanup_confirmed=cleanup_confirmed, exit_code=process.poll())
            final_status = requested_terminal
            final_error = "组合任务已取消" if requested_terminal == "cancelled" else "组合任务超过墙钟时限"
            raise _RunFailure(final_status, final_error)

        cleanup_confirmed = _reap_completed_worker(process)
        _write_phase(directory, phase="closed" if cleanup_confirmed else "cleanup_unconfirmed",
                     token=token, run_id=chosen_id, store_root=root, pid=process_pid,
                     timeout_arg=timeout_arg, python_executable=os.path.abspath(sys.executable),
                     cleanup_confirmed=cleanup_confirmed, exit_code=return_code)
        if not cleanup_confirmed:
            raise _RunFailure("failed", "无法确认组合 worker 已回收")
        if return_code != 0:
            raise _RunFailure("failed", "组合 worker 执行失败")
        if _cancel_requested(cancel_path):
            raise _RunFailure("cancelled", "组合任务已取消")
        if time.monotonic_ns() > deadline_ns:
            # The worker's signed-by-token completion timestamp below decides
            # whether finalization itself crossed the wall-clock deadline.
            pass

        _verify_source_snapshot(snapshot)
        completion_hashes, frames, source_version = _read_completion(
            directory, token=token, run_id=chosen_id, store_root=root,
            expected_source_version=expected_source_version,
            started_ns=started_ns, deadline_ns=deadline_ns,
        )
        if source_version != snapshot["source_version"]:
            raise _RunFailure("failed", "执行期间源码版本发生变化")
        _promote_staged_artifacts(directory)
        actual_hashes = {
            name: hashlib.sha256(_read_bytes(directory / name, _ARTIFACT_LIMITS[name])).hexdigest()
            for name in sorted(_ARTIFACT_NAMES)
        }
        if actual_hashes != completion_hashes:
            _remove_published_artifacts(directory)
            raise _RunFailure("failed", "组合产物提升后哈希校验失败")
        if _cancel_requested(cancel_path):
            _remove_published_artifacts(directory)
            raise _RunFailure("cancelled", "组合任务已取消")

        receipt.update(
            status="completed",
            frames=frames,
            artifact_sha256=actual_hashes,
            cleanup_confirmed=True,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        # This last read is the cancellation/completion linearization point.
        if _cancel_requested(cancel_path):
            _remove_published_artifacts(directory)
            raise _RunFailure("cancelled", "组合任务已取消")
        final_status = "completed"
        write_json(directory / "receipt.json", receipt)
        return receipt
    except _RunFailure as error:
        final_status = error.status
        final_error = error.public_message
    except BaseException:
        final_status = "failed"
        final_error = "组合执行未完成，请检查本地数据和执行范围"
        if process is not None:
            if process.poll() is None:
                cleanup_confirmed = _terminate_owned_group(process)
            else:
                cleanup_confirmed = _reap_completed_worker(process)
            if process_pid is not None:
                _write_phase(directory, phase="closed" if cleanup_confirmed else "cleanup_unconfirmed",
                             token=token, run_id=chosen_id, store_root=root, pid=process_pid,
                             timeout_arg=repr(float(timeout)), python_executable=os.path.abspath(sys.executable),
                             cleanup_confirmed=cleanup_confirmed, exit_code=process.poll())
    finally:
        try:
            if final_status != "completed":
                try:
                    _remove_published_artifacts(directory)
                except OSError:
                    pass
                _terminalize(receipt, directory, final_status, cleanup_confirmed=cleanup_confirmed,
                             error=final_error)
        finally:
            os.close(lock_fd)
    return receipt


def _timeout_arg(value):
    try:
        return _validated_timeout(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="共享资金组合历史研究")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    actions = parser.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan")
    plan.add_argument("--file", type=Path, required=True)
    run = actions.add_parser("run")
    run.add_argument("--file", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=_timeout_arg, default=300.0)
    show = actions.add_parser("show")
    show.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.action == "show":
        if _RUN_ID.fullmatch(args.run_id) is None:
            parser.error("运行编号无效")
        print((args.store / args.run_id / "receipt.json").read_text(encoding="utf-8"))
        return 0
    with args.file.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        parser.error("配置文件最多 64 KiB")
    from .portfolio_kernel import normalize_portfolio
    config = normalize_portfolio(json.loads(raw))
    if args.action == "plan":
        print(json.dumps({"config": config, "mode": "RESEARCH_ONLY", "paper_admission": False},
                         ensure_ascii=False, allow_nan=False))
        return 0
    receipt = run_bounded(config, args.store, timeout_seconds=args.timeout_seconds)
    print(json.dumps(receipt, ensure_ascii=False, allow_nan=False))
    return 0 if receipt["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
