"""NDJSON worker intended to run only inside a separately isolated Linux guest.

NEVER invoke this worker on the host with untrusted Python source.  It compiles
and executes the supplied source with ordinary Python privileges in its own
process.  The protocol bounds frames and serializable values; it is not a Python
security boundary.  The guest/container controller must enforce OS isolation,
resource limits, and strict response validation independently.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import math
import re
import sys
from typing import Any


MAX_SOURCE_BYTES = 64 * 1024
MAX_INPUT_LINE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_STDERR_BYTES = 8 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_ERROR_STARTUP_FAILED = "startup_failed"
_ERROR_CALLBACK_FAILED = "callback_failed"
_ERROR_RESULT_INVALID = "result_invalid"
_ERROR_RESULT_TOO_LARGE = "result_too_large"


class _ProtocolError(ValueError):
    """An internal parse/validation failure; details never reach the protocol."""


class _BoundedStderrSink:
    """Forward at most a fixed number of UTF-8 bytes to the original stderr."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, stderr: Any, limit: int = MAX_STDERR_BYTES) -> None:
        self._stderr = stderr
        self._limit = limit
        self._written = 0

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("write() argument must be str")
        remaining = self._limit - self._written
        if remaining > 0 and value:
            # Slice before encoding so a huge print cannot create another huge
            # encoded buffer.  At most 4 * remaining bytes are transiently made.
            data = value[:remaining].encode("utf-8", "replace")[:remaining]
            if data:
                self._stderr.write(data)
                self._stderr.flush()
                self._written += len(data)
        return len(value)

    def flush(self) -> None:
        self._stderr.flush()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise _ProtocolError("nonfinite")


def _parse_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _ProtocolError("nonfinite")
    return number


def _validate_json_value(value: Any) -> None:
    """Require strict JSON-compatible builtins and bound depth and node count."""
    nodes = 0
    ancestors: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        if depth > MAX_JSON_DEPTH:
            raise _ProtocolError("depth")
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise _ProtocolError("nodes")

        item_type = type(item)
        if item is None or item_type in (bool, int):
            return
        if item_type is float:
            if not math.isfinite(item):
                raise _ProtocolError("nonfinite")
            return
        if item_type is str:
            try:
                item.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise _ProtocolError("invalid_unicode") from exc
            return
        if item_type not in (list, dict):
            raise _ProtocolError("unsupported_type")

        identity = id(item)
        if identity in ancestors:
            raise _ProtocolError("cycle")
        ancestors.add(identity)
        try:
            if item_type is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise _ProtocolError("non_string_key")
                    nodes += 1
                    if nodes > MAX_JSON_NODES:
                        raise _ProtocolError("nodes")
                    try:
                        key.encode("utf-8", "strict")
                    except UnicodeEncodeError as exc:
                        raise _ProtocolError("invalid_unicode") from exc
                    visit(child, depth + 1)
        finally:
            ancestors.remove(identity)

    visit(value, 0)


def _strict_json_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            object_pairs_hook=_unique_object,
        )
        _validate_json_value(value)
        return value
    except _ProtocolError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise _ProtocolError("invalid_json") from exc


def _read_line(stream: Any) -> bytes | None:
    raw = stream.readline(MAX_INPUT_LINE_BYTES + 1)
    if raw == b"":
        return None
    if len(raw) > MAX_INPUT_LINE_BYTES or not raw.endswith(b"\n"):
        raise _ProtocolError("line_size_or_termination")
    return raw[:-1]


def _parse_initial_frame(raw: bytes) -> tuple[str, str]:
    frame = _strict_json_loads(raw)
    if type(frame) is not dict or set(frame) != {"request_id", "source"}:
        raise _ProtocolError("initial_shape")
    request_id = frame["request_id"]
    source = frame["source"]
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise _ProtocolError("request_id")
    if type(source) is not str:
        raise _ProtocolError("source_type")
    try:
        source_bytes = source.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise _ProtocolError("source_unicode") from exc
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise _ProtocolError("source_size")
    return request_id, source


def _parse_callback_frame(raw: bytes) -> tuple[str, Any]:
    frame = _strict_json_loads(raw)
    if type(frame) is not dict or set(frame) != {"request_id", "payload"}:
        raise _ProtocolError("callback_shape")
    request_id = frame["request_id"]
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise _ProtocolError("request_id")
    return request_id, frame["payload"]


def _encode_response(request_id: str, *, result: Any = None, error: str | None = None) -> bytes:
    if error is None:
        response = {"request_id": request_id, "result": result}
    else:
        response = {"request_id": request_id, "error": error}
    _validate_json_value(response)
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict") + b"\n"
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise _ProtocolError("response_encoding") from exc
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise _ProtocolError("response_size")
    return encoded


def _write_response(stream: Any, request_id: str, *, result: Any = None, error: str | None = None) -> None:
    frame = _encode_response(request_id, result=result, error=error)
    stream.write(frame)
    stream.flush()


def _serve(stdin: Any, protocol_stdout: Any, protocol_stderr: Any) -> int:
    """Serve the protocol on supplied binary streams.

    This private helper is directly exercised only with benign synthetic source
    and owned in-memory streams. Production callers must use the public CLI from
    an independently verified isolated guest.
    """
    stderr_sink = _BoundedStderrSink(protocol_stderr)
    redirect_stdout = contextlib.redirect_stdout
    redirect_stderr = contextlib.redirect_stderr
    strategy_exec = builtins.exec
    strategy_compile = builtins.compile

    try:
        raw = _read_line(stdin)
        if raw is None:
            return 2
        request_id, source = _parse_initial_frame(raw)
    except _ProtocolError:
        return 2

    namespace: dict[str, Any] = {
        "__builtins__": builtins.__dict__,
        "__name__": "strategy_guest",
    }
    try:
        with redirect_stdout(stderr_sink), redirect_stderr(stderr_sink):
            code = strategy_compile(source, "<strategy>", "exec")
            strategy_exec(code, namespace, namespace)
        handle = namespace.get("handle")
        if not callable(handle):
            raise TypeError("missing callback")
    except BaseException:
        try:
            _write_response(protocol_stdout, request_id, error=_ERROR_STARTUP_FAILED)
        except BaseException:
            return 1
        return 1

    try:
        _write_response(protocol_stdout, request_id, result={"ready": True})
    except BaseException:
        return 1

    while True:
        try:
            raw = _read_line(stdin)
            if raw is None:
                return 0
            request_id, payload = _parse_callback_frame(raw)
        except _ProtocolError:
            return 2

        try:
            with redirect_stdout(stderr_sink), redirect_stderr(stderr_sink):
                result = handle(payload)
        except BaseException:
            response = None
            error = _ERROR_CALLBACK_FAILED
        else:
            try:
                response = _encode_response(request_id, result=result)
            except _ProtocolError as exc:
                response = None
                error = (
                    _ERROR_RESULT_TOO_LARGE
                    if str(exc) == "response_size"
                    else _ERROR_RESULT_INVALID
                )
            except BaseException:
                response = None
                error = _ERROR_RESULT_INVALID
            else:
                error = None

        if response is not None:
            try:
                protocol_stdout.write(response)
                protocol_stdout.flush()
            except BaseException:
                return 1
            continue

        try:
            _write_response(protocol_stdout, request_id, error=error)
        except BaseException:
            return 1


def main() -> int:
    """Linux-only CLI entry point; Linux itself does not prove isolation."""
    if sys.platform != "linux":
        sys.stderr.write("vm_strategy_worker is only enabled on Linux\n")
        sys.stderr.flush()
        return 78
    return _serve(sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
