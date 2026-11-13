"""Bounded IPC probe machinery and the unavailable public Python sandbox.

The private native process runner is retained only for known synthetic OS probes
and controlled diagnostics. It is not safe for untrusted Python: a native
KERN_PROCARGS2 probe read a random synthetic victim-process environment under
the current macOS sandbox profile. The public ``PythonSandbox`` constructor
therefore fails closed before examining source. This module does not load market
data, implement a strategy DSL, run a backtest, or grant paper/live admission.

The private runner still bounds JSON IPC, sampled RSS, wall time, outputs and
owned-process cleanup for these synthetic probes. RSS is not a hard instantaneous
memory limit; CPU/resource limits are best effort. Those controls do not repair
the native process-environment isolation gap.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


MAX_SOURCE_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_STDERR_BYTES = 8 * 1024
MAX_STDOUT_BYTES = 64 * 1024
POLL_INTERVAL_SECONDS = 0.05
SANDBOX_EXEC_PATH = Path("/usr/bin/sandbox-exec")
_SAFE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}
_PACKAGE_NAMES = ("numpy", "pandas", "dateutil", "pytz", "tzdata", "six.py")


class SandboxError(RuntimeError):
    """A fail-closed sandbox error, exposing only a stable error category."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SandboxUnavailable(SandboxError):
    """Raised when no verified isolation boundary is available for user code."""


class _StrictJSONError(ValueError):
    pass


def _validate_json_value(value: Any) -> None:
    """Reject non-JSON Python values and bound JSON depth and node count."""
    nodes = 0
    ancestors: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        if depth > MAX_JSON_DEPTH:
            raise _StrictJSONError("depth")
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise _StrictJSONError("nodes")

        item_type = type(item)
        if item is None or item_type is bool or item_type is int:
            return
        if item_type is float:
            if not math.isfinite(item):
                raise _StrictJSONError("nonfinite")
            return
        if item_type is str:
            try:
                item.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise _StrictJSONError("invalid_unicode") from exc
            return
        if item_type not in (list, dict):
            raise _StrictJSONError("unsupported_type")

        identity = id(item)
        if identity in ancestors:
            raise _StrictJSONError("cycle")
        ancestors.add(identity)
        try:
            if item_type is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise _StrictJSONError("non_string_key")
                    nodes += 1
                    if nodes > MAX_JSON_NODES:
                        raise _StrictJSONError("nodes")
                    try:
                        key.encode("utf-8", "strict")
                    except UnicodeEncodeError as exc:
                        raise _StrictJSONError("invalid_unicode") from exc
                    visit(child, depth + 1)
        finally:
            ancestors.remove(identity)

    visit(value, 0)


def _reject_json_constant(value: str) -> Any:
    raise _StrictJSONError("nonfinite")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _StrictJSONError("float_overflow")
    return number


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("duplicate_key")
        result[key] = value
    return result


def _strict_json_loads(raw: bytes | str) -> Any:
    try:
        text = raw.decode("utf-8", "strict") if isinstance(raw, bytes) else raw
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            object_pairs_hook=_unique_object,
        )
        _validate_json_value(value)
        return value
    except _StrictJSONError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise _StrictJSONError("invalid_json") from exc


def _parse_response(raw: bytes, request_id: str) -> Any:
    """Parse exactly one bounded response line and verify its request nonce."""
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise _StrictJSONError("response_size")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise _StrictJSONError("response_lines")
    response = _strict_json_loads(raw[:-1])
    if type(response) is not dict or set(response) != {"request_id", "result"}:
        raise _StrictJSONError("response_shape")
    if type(response["request_id"]) is not str or response["request_id"] != request_id:
        raise _StrictJSONError("response_nonce")
    return response["result"]


_LAUNCHER_SOURCE = r'''import io
import json
import math
import os
import resource
import runpy
import sys

MAX_MESSAGE = 65536
MAX_DEPTH = 32
MAX_NODES = 10000

class _Sink:
    encoding = "utf-8"
    errors = "strict"

    def write(self, value):
        return len(value)

    def flush(self):
        return None

    def isatty(self):
        return False

    def writable(self):
        return True

class _Reject(Exception):
    pass

def _constant(value):
    raise _Reject("nonfinite")

def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _Reject("float_overflow")
    return number

def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Reject("duplicate_key")
        result[key] = value
    return result

def _validate(value):
    nodes = [0]
    ancestors = set()

    def visit(item, depth):
        if depth > MAX_DEPTH:
            raise _Reject("depth")
        nodes[0] += 1
        if nodes[0] > MAX_NODES:
            raise _Reject("nodes")
        item_type = type(item)
        if item is None or item_type in (bool, int):
            return
        if item_type is float:
            if not math.isfinite(item):
                raise _Reject("nonfinite")
            return
        if item_type is str:
            item.encode("utf-8", "strict")
            return
        if item_type not in (list, dict):
            raise _Reject("unsupported_type")
        identity = id(item)
        if identity in ancestors:
            raise _Reject("cycle")
        ancestors.add(identity)
        try:
            if item_type is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise _Reject("non_string_key")
                    nodes[0] += 1
                    if nodes[0] > MAX_NODES:
                        raise _Reject("nodes")
                    key.encode("utf-8", "strict")
                    visit(child, depth + 1)
        finally:
            ancestors.remove(identity)

    visit(value, 0)

def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RuntimeError("protocol_write")
        view = view[written:]

def _read_request(stream):
    line = stream.readline(MAX_MESSAGE + 1)
    if not line:
        return None
    if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
        raise RuntimeError("request_size")
    request = json.loads(
        line[:-1].decode("utf-8", "strict"),
        parse_constant=_constant,
        parse_float=_float,
        object_pairs_hook=_object,
    )
    _validate(request)
    if type(request) is not dict or set(request) != {"request_id", "payload"}:
        raise RuntimeError("request_shape")
    if type(request["request_id"]) is not str:
        raise RuntimeError("request_nonce")
    return request

def _run():
    strategy_path = sys.argv[1]
    vendor_path = sys.argv[2]
    protocol_fd = __PROTOCOL_FD__
    cpu_seconds = __CPU_SECONDS__
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (AttributeError, OSError, ValueError):
        pass

    sys.path.insert(0, vendor_path)
    original_stdin = sys.stdin
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    protocol_input = original_stdin.buffer
    sink = _Sink()
    empty_input = io.StringIO("")
    sys.argv = [strategy_path]
    sys.stdin = empty_input
    sys.__stdin__ = empty_input
    sys.stdout = sink
    sys.__stdout__ = sink
    sys.stderr = sink
    sys.__stderr__ = sink
    namespace = runpy.run_path(strategy_path, run_name="__strategy__")
    handler = namespace.get("handle")
    if not callable(handler):
        raise RuntimeError("missing_handle")

    while True:
        request = _read_request(protocol_input)
        if request is None:
            return
        sys.stdin = empty_input
        sys.__stdin__ = empty_input
        sys.stdout = sink
        sys.__stdout__ = sink
        sys.stderr = sink
        sys.__stderr__ = sink
        result = handler(request["payload"])
        response = {"request_id": request["request_id"], "result": result}
        _validate(response)
        encoded = json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict") + b"\n"
        if len(encoded) > MAX_MESSAGE:
            raise RuntimeError("response_size")
        _write_all(protocol_fd, encoded)

_run()
'''


def _sbpl_quote(value: Path | str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise SandboxError("invalid_profile_path")
    return json.dumps(text, ensure_ascii=False)


def _build_profile(
    executable: Path,
    runtime_prefix: Path,
    workspace: Path,
    package_paths: list[Path],
) -> str:
    reads: list[str] = [
        f"(subpath {_sbpl_quote(runtime_prefix)})",
        f"(subpath {_sbpl_quote(Path('/System/Library'))})",
        f"(subpath {_sbpl_quote(Path('/usr/lib'))})",
        f"(subpath {_sbpl_quote(Path('/usr/share/zoneinfo'))})",
        f"(subpath {_sbpl_quote(Path('/private/var/db/timezone'))})",
        f"(subpath {_sbpl_quote(workspace)})",
        f"(literal {_sbpl_quote(Path('/dev/urandom'))})",
        f"(literal {_sbpl_quote(Path('/dev/random'))})",
    ]
    for package_path in package_paths:
        if package_path.is_dir():
            reads.append(f"(subpath {_sbpl_quote(package_path)})")
        else:
            reads.append(f"(literal {_sbpl_quote(package_path)})")
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            f"(allow process-exec (literal {_sbpl_quote(executable)}))",
            "(allow file-read-metadata)",
            "(allow sysctl-read)",
            '(allow file-read* (literal "/"))',
            f"(allow file-read* {' '.join(reads)})",
        )
    )


def _install_vendor_links(vendor: Path) -> list[Path]:
    site_packages = Path(sysconfig.get_path("purelib")).resolve()
    allowed: list[Path] = []
    for name in _PACKAGE_NAMES:
        original = site_packages / name
        if not original.exists():
            continue
        target = original.resolve(strict=True)
        link = vendor / name
        link.symlink_to(target, target_is_directory=target.is_dir())
        allowed.append(target)
    return allowed


class _TrustedNativeProbeProcess:
    """Private process runner for fixed synthetic probes and controlled diagnostics.

    Args:
        source: UTF-8 synthetic probe source defining ``handle(payload)``.
        memory_limit_mib: Sampled RSS ceiling in MiB. This is not a hard cap.
        session_timeout: Total wall-clock lifetime in seconds from child startup.

    ``exchange`` sends and receives bounded strict JSON. Failures terminate and
    reap the entire owned process group and remove only this instance's private
    temporary directory. This is not safe for arbitrary or untrusted source.
    """

    def __init__(
        self,
        source: bytes,
        *,
        memory_limit_mib: int = 256,
        session_timeout: float = 180,
    ) -> None:
        if type(source) is not bytes:
            raise SandboxError("invalid_source")
        if len(source) > MAX_SOURCE_BYTES:
            raise SandboxError("source_too_large")
        try:
            source_text = source.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise SandboxError("source_invalid_utf8") from None
        try:
            compile(source_text, "<strategy>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError):
            raise SandboxError("source_syntax") from None
        if type(memory_limit_mib) is not int or memory_limit_mib < 1:
            raise SandboxError("invalid_memory_limit")
        if (
            isinstance(session_timeout, bool)
            or not isinstance(session_timeout, (int, float))
            or not math.isfinite(float(session_timeout))
            or session_timeout <= 0
        ):
            raise SandboxError("invalid_session_timeout")

        self._source = source
        self._memory_limit_kib = memory_limit_mib * 1024
        self._session_timeout = float(session_timeout)
        self._session_deadline: float | None = None
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._workspace: Path | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._protocol_read_fd: int | None = None
        self._protocol_write_fd: int | None = None
        self._monitor_stop = threading.Event()
        self._monitor_lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_failure: str | None = None
        self._stderr_total = 0
        self._closed = False
        self._entered = False

    def __enter__(self) -> "_TrustedNativeProbeProcess":
        if self._entered or self._closed:
            raise SandboxError("session_closed")
        self._entered = True
        try:
            self._check_supported()
            self._start()
            return self
        except SandboxError:
            self._dispose(kill=True)
            raise
        except Exception:
            self._dispose(kill=True)
            raise SandboxError("sandbox_start_failed") from None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._dispose(kill=True)
        return False

    def exchange(
        self,
        payload: Any,
        timeout: float = 3,
        cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        if self._closed:
            raise SandboxError(self._get_monitor_failure() or "session_closed")
        if self._process is None or not self._entered:
            raise SandboxError("session_closed")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            self._abort("invalid_timeout")
        if cancelled is not None and not callable(cancelled):
            self._abort("invalid_cancellation_callback")

        request_id = uuid.uuid4().hex
        try:
            envelope = {"request_id": request_id, "payload": payload}
            _validate_json_value(envelope)
            request = (
                json.dumps(
                    envelope,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8", "strict")
                + b"\n"
            )
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            self._abort("invalid_payload")
        if len(request) > MAX_MESSAGE_BYTES:
            self._abort("request_too_large")

        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        assert self._protocol_read_fd is not None
        if self._session_deadline is None:
            self._abort("session_closed")

        started = time.monotonic()
        exchange_deadline = started + float(timeout)
        session_deadline = self._session_deadline
        deadline = min(exchange_deadline, session_deadline)
        timeout_code = "session_timeout" if session_deadline <= exchange_deadline else "timeout"
        request_offset = 0
        response_buffer = bytearray()
        stdout_count = 0
        result: Any = None
        response_ready = False
        quiet_deadline: float | None = None
        stdout_eof = False
        stderr_eof = False
        protocol_eof = False

        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
            os.set_blocking(self._process.stderr.fileno(), False)
            os.set_blocking(self._protocol_read_fd, False)
            selector.register(self._process.stdin, selectors.EVENT_WRITE, "stdin")
            selector.register(self._process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(self._process.stderr, selectors.EVENT_READ, "stderr")
            selector.register(self._protocol_read_fd, selectors.EVENT_READ, "protocol")

            self._check_rss()
            while True:
                now = time.monotonic()
                if now >= deadline:
                    self._abort(timeout_code)
                if cancelled is not None:
                    try:
                        if cancelled():
                            self._abort("cancelled")
                    except SandboxError:
                        raise
                    except Exception:
                        self._abort("cancellation_check_failed")
                monitor_failure = self._get_monitor_failure()
                if monitor_failure is not None:
                    self._abort(monitor_failure)
                if self._process.poll() is not None:
                    self._abort("worker_exit")

                if response_ready and quiet_deadline is not None and now >= quiet_deadline:
                    return result

                wait_until = deadline
                if response_ready and quiet_deadline is not None:
                    wait_until = min(wait_until, quiet_deadline)
                wait_for = min(POLL_INTERVAL_SECONDS, max(0.0, wait_until - now))
                events = selector.select(wait_for)
                for key, _mask in events:
                    channel = key.data
                    if channel == "stdin":
                        try:
                            written = os.write(
                                self._process.stdin.fileno(), request[request_offset:]
                            )
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            self._abort("worker_exit")
                        if written <= 0:
                            self._abort("worker_exit")
                        request_offset += written
                        if request_offset >= len(request):
                            selector.unregister(self._process.stdin)
                    else:
                        try:
                            chunk = os.read(key.fd, 16 * 1024)
                        except BlockingIOError:
                            continue
                        except OSError:
                            self._abort("ipc_failure")
                        if not chunk:
                            try:
                                selector.unregister(key.fileobj)
                            except Exception:
                                pass
                            if channel == "stdout":
                                stdout_eof = True
                            elif channel == "stderr":
                                stderr_eof = True
                            else:
                                protocol_eof = True
                            continue
                        if channel == "stdout":
                            stdout_count += len(chunk)
                            if stdout_count > MAX_STDOUT_BYTES:
                                self._abort("stdout_limit")
                        elif channel == "stderr":
                            self._stderr_total += len(chunk)
                            if self._stderr_total > MAX_STDERR_BYTES:
                                self._abort("stderr_limit")
                        else:
                            if response_ready:
                                self._abort("protocol_error")
                            response_buffer.extend(chunk)
                            if len(response_buffer) > MAX_MESSAGE_BYTES:
                                self._abort("response_too_large")
                            if b"\n" in response_buffer:
                                try:
                                    result = _parse_response(bytes(response_buffer), request_id)
                                except _StrictJSONError:
                                    self._abort("protocol_error")
                                response_ready = True
                                quiet_deadline = time.monotonic() + 0.01
                if protocol_eof and not response_ready:
                    self._abort("worker_exit")
                if stdout_eof and stderr_eof and self._process.poll() is not None:
                    self._abort("worker_exit")
        except SandboxError:
            self._dispose(kill=True)
            raise
        except Exception:
            monitor_failure = self._get_monitor_failure()
            self._dispose(kill=True)
            raise SandboxError(monitor_failure or "ipc_failure") from None
        finally:
            try:
                selector.close()
            except Exception:
                pass

    def _abort(self, code: str) -> None:
        self._dispose(kill=True)
        raise SandboxError(code)

    @staticmethod
    def _check_supported() -> None:
        if sys.platform != "darwin" or not SANDBOX_EXEC_PATH.is_file():
            raise SandboxError("sandbox_unavailable")

    def _start(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        runtime_prefix = Path(sys.base_prefix).resolve(strict=True)
        self._temp = tempfile.TemporaryDirectory(prefix="quant_strategy_sandbox_")
        workspace = Path(self._temp.name).resolve(strict=True)
        self._workspace = workspace
        vendor = workspace / "vendor"
        vendor.mkdir(mode=0o700)
        strategy_path = workspace / "strategy.py"
        launcher_path = workspace / "launcher.py"
        strategy_path.write_bytes(self._source)
        strategy_path.chmod(0o600)

        package_paths = _install_vendor_links(vendor)
        protocol_read_fd, protocol_write_fd = os.pipe()
        self._protocol_read_fd = protocol_read_fd
        self._protocol_write_fd = protocol_write_fd
        os.set_inheritable(protocol_write_fd, True)
        cpu_seconds = max(1, int(math.ceil(self._session_timeout)))
        launcher = _LAUNCHER_SOURCE.replace("__PROTOCOL_FD__", str(protocol_write_fd))
        launcher = launcher.replace("__CPU_SECONDS__", str(cpu_seconds))
        launcher_path.write_text(launcher, encoding="utf-8")
        launcher_path.chmod(0o600)
        profile = _build_profile(executable, runtime_prefix, workspace, package_paths)

        command = [
            str(SANDBOX_EXEC_PATH),
            "-p",
            profile,
            str(executable),
            "-I",
            "-S",
            "-B",
            str(launcher_path),
            str(strategy_path),
            str(vendor),
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=str(workspace),
                env=dict(_SAFE_ENV),
                close_fds=True,
                start_new_session=True,
                pass_fds=(protocol_write_fd,),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise SandboxError("sandbox_start_failed") from None
        finally:
            try:
                os.close(protocol_write_fd)
            except OSError:
                pass
            self._protocol_write_fd = None
        self._session_deadline = time.monotonic() + self._session_timeout
        self._monitor_thread = threading.Thread(
            target=self._watch_process,
            name="python-sandbox-watchdog",
            daemon=True,
        )
        self._monitor_thread.start()

    def _get_monitor_failure(self) -> str | None:
        with self._monitor_lock:
            return self._monitor_failure

    def _set_monitor_failure(self, code: str) -> None:
        with self._monitor_lock:
            if self._monitor_failure is None:
                self._monitor_failure = code
        process = self._process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self._dispose(kill=True)

    def _sample_rss(self, process: subprocess.Popen[bytes]) -> tuple[int | None, str | None]:
        if process.poll() is not None:
            return None, "worker_exit"
        try:
            remaining = 0.5
            if self._session_deadline is not None:
                remaining = max(0.05, min(0.5, self._session_deadline - time.monotonic()))
            sampled = subprocess.run(
                ["/bin/ps", "-p", str(process.pid), "-o", "rss="],
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                return None, "rss_unavailable"
            return None, "worker_exit"
        if sampled.returncode != 0 or not sampled.stdout.strip():
            if process.poll() is None:
                return None, "rss_unavailable"
            return None, "worker_exit"
        try:
            rss_kib = int(sampled.stdout.strip())
        except ValueError:
            return None, "rss_unavailable"
        return rss_kib, None

    def _watch_process(self) -> None:
        while not self._monitor_stop.wait(POLL_INTERVAL_SECONDS):
            process = self._process
            if process is None or self._closed:
                return
            if process.poll() is not None:
                self._set_monitor_failure("worker_exit")
                return
            if self._session_deadline is not None and time.monotonic() >= self._session_deadline:
                self._set_monitor_failure("session_timeout")
                return
            rss_kib, error = self._sample_rss(process)
            if self._monitor_stop.is_set() or self._closed:
                return
            if error is not None:
                self._set_monitor_failure(error)
                return
            if rss_kib is not None and rss_kib > self._memory_limit_kib:
                self._set_monitor_failure("memory_limit_exceeded")
                return

    def _check_rss(self) -> None:
        process = self._process
        if process is None:
            self._abort("worker_exit")
        failure = self._get_monitor_failure()
        if failure is not None:
            self._abort(failure)
        rss_kib, error = self._sample_rss(process)
        if error is not None:
            self._abort(error)
        if rss_kib is None:
            self._abort("rss_unavailable")
        if rss_kib > self._memory_limit_kib:
            self._abort("memory_limit_exceeded")

    def _dispose(self, *, kill: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._monitor_stop.set()
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=1)
        process = self._process
        if process is not None:
            if kill:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (subprocess.SubprocessError, OSError):
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        for fd_name in ("_protocol_read_fd", "_protocol_write_fd"):
            fd = getattr(self, fd_name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)
        if self._temp is not None:
            try:
                self._temp.cleanup()
            except OSError:
                pass
            self._temp = None
        self._workspace = None
        self._process = None


class PythonSandbox:
    """Unavailable public entry point until cross-process environment isolation is proven.

    macOS ``sandbox-exec`` did not prevent a native KERN_PROCARGS2 read of an
    owned synthetic victim's environment. The constructor intentionally raises
    before inspecting or compiling ``source``. There is no public opt-in or
    unsandboxed fallback; use a verified isolation VM before enabling user Python.
    """

    def __init__(
        self,
        source: bytes,
        *,
        memory_limit_mib: int = 256,
        session_timeout: float = 180,
    ) -> None:
        raise SandboxUnavailable("native_process_environment_not_isolated")

