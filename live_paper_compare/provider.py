"""Market data routes, including an explicit credential-free public mode."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re
from typing import Any
from urllib.parse import urlsplit

import requests


LEGACY_PLAN1_URL = "https://quantdata888.duckdns.org"
LEGACY_PLAN2_URL = "http://175.27.156.38/v1/tushare/query"
NEW_PLAN1_URL = "http://datahubco.com/app-api/openapi/v1/tushare"
NEW_PLAN2_URL = "https://ai-tool.indevs.in/tushare/pro"
SINA_URL = "https://hq.sinajs.cn/list="
TENCENT_URL = "https://qt.gtimg.cn/q="
LEGACY_PLAN1_SERVICE = "quant-research-live-plan1-token"
LEGACY_PLAN2_SERVICE = "quant-research-live-plan2-key"
NEW_PLAN1_SERVICE = "quant-research-tushare-datahub"
NEW_PLAN2_SERVICE = "quant-research-tushare-relay-new"
NEW_PLAN2_PROXY_SERVICE = "quant-research-tushare-relay-new-proxy"
NEW_PLAN1_SERVICE_ALIASES = (NEW_PLAN1_SERVICE, "quant-research-live-new-plan1-key")
NEW_PLAN2_SERVICE_ALIASES = (NEW_PLAN2_SERVICE, "quant-research-live-new-plan2-key")
NEW_PLAN2_PROXY_SERVICE_ALIASES = (NEW_PLAN2_PROXY_SERVICE, "quant-research-live-new-plan2-proxy")
PROMAX_URL = "https://pcd.mobcvb.cn/tushare/pro"
PROMAX_SERVICE = "quant-research-tushare-promax"

# The Promax hand-book recommends small, explicit minute requests.  Keep the
# batch bound here so a caller cannot accidentally turn a realtime request
# into a full-universe query.
PROMAX_MINUTE_BATCH_SIZE = 10
PROMAX_MINUTE_LIMIT = 15
PROMAX_MAX_ATTEMPTS = 3  # one request plus at most two retries
PROMAX_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
PROMAX_FRESH_MAX_AGE_SECONDS = 300.0
PROMAX_MAX_TIMEOUT_SECONDS = 60.0
_A_SHARE_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_PROMAX_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

# Keep the legacy names import-compatible for scripts that only inspect the
# old provider constants.  Runtime selection is controlled by
# LIVE_PAPER_PROVIDER_MODE below.
PLAN1_URL = LEGACY_PLAN1_URL
PLAN2_URL = LEGACY_PLAN2_URL
PLAN1_SERVICE = LEGACY_PLAN1_SERVICE
PLAN2_SERVICE = LEGACY_PLAN2_SERVICE


class FeedError(RuntimeError):
    pass


def keychain_secret(service: str) -> str:
    account = os.environ.get("USER") or os.getlogin()
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or not result.stdout.strip():
        raise FeedError(f"macOS Keychain 中缺少凭据: {service}")
    return result.stdout.strip()


def keychain_has_secret(service: str) -> bool:
    """Return whether a Keychain item exists without exposing its value."""
    account = os.environ.get("USER") or os.getlogin()
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _first_keychain_service(services: tuple[str, ...]) -> str | None:
    for service in services:
        if keychain_has_secret(service):
            return service
    return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise FeedError("provider response is not a JSON object")
    if payload.get("ok") is False:
        message = payload.get("message") or payload.get("error") or "provider returned ok=false"
        raise FeedError(str(message))
    # Promax can return a JSON business error with HTTP 200.  Do not let a
    # truthy error field fall through to the empty/partial-row parser.
    if payload.get("error"):
        raise FeedError(str(payload.get("message") or payload.get("error")))
    if str(payload.get("status") or "").strip().lower() in {"error", "failed", "failure"}:
        raise FeedError(str(payload.get("message") or payload.get("msg") or payload.get("status")))
    code = payload.get("code", 0)
    if code not in (0, None):
        message = payload.get("msg") or payload.get("message") or f"provider code={code}"
        raise FeedError(str(message))
    data = payload.get("data")
    if isinstance(data, list):
        if not data or all(isinstance(row, dict) for row in data):
            return list(data)
        raise FeedError("provider data list has an unsupported shape")
    if not isinstance(data, dict):
        data = payload
    fields = data.get("fields") or []
    items = data.get("items") or data.get("rows") or []
    if items and all(isinstance(row, dict) for row in items):
        return list(items)
    if not fields and not items:
        return []
    return [dict(zip(fields, row)) for row in items]


def _validate_ts_code(value: Any) -> str:
    """Validate and canonicalize an A-share/ETF Tushare code.

    The Promax minute endpoints are used only with explicit numeric A-share
    codes.  Wildcards, bare numeric strings, and market names outside SH/SZ
    are rejected before a request is sent so a malformed caller cannot spend
    the paid route's quota or receive an unbounded response.
    """
    if not isinstance(value, str):
        raise FeedError("Promax ts_code 必须是六位数字加 .SH/.SZ")
    code = value.strip().upper()
    if not _A_SHARE_CODE.fullmatch(code):
        raise FeedError(f"Promax ts_code 无效: {value!r}")
    return code


def _normalise_ts_codes(ts_codes: list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(ts_codes, (str, bytes)) or not isinstance(ts_codes, (list, tuple)):
        raise FeedError("ts_codes 必须是代码列表")
    result: list[str] = []
    seen: set[str] = set()
    for value in ts_codes:
        code = _validate_ts_code(value)
        if code not in seen:
            result.append(code)
            seen.add(code)
    return result


def _parse_promax_time(value: Any) -> datetime:
    """Parse a Promax ``trade_time`` as an aware Shanghai datetime.

    The vendor has returned both ISO-like strings and compact Tushare-style
    timestamps.  A naive value is interpreted in the A-share market timezone;
    no current or synthetic timestamp is ever created.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty timestamp")
        if not any(separator in text for separator in ("T", "t", " ")) and not text.isdigit():
            raise ValueError("timestamp has no time component")
        iso_text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_text)
        except ValueError:
            parsed = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y%m%d%H%M%S",
                "%Y%m%d%H%M",
            ):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise ValueError("unsupported timestamp format")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_PROMAX_TIMEZONE)
    return parsed.astimezone(_PROMAX_TIMEZONE)


def _normalise_promax_rows(
    rows: list[dict[str, Any]],
    *,
    requested_codes: set[str] | None = None,
    minute: bool = False,
) -> list[dict[str, Any]]:
    """Apply the small amount of provider normalization consumed downstream.

    ``trade_time`` is retained verbatim in ``vendor_time`` while ``time`` is
    normalized to a naive Shanghai ISO timestamp for the existing MinuteBook.
    The endpoint's minute label semantics are not documented, so this helper
    intentionally does not subtract a minute or synthesize a current stamp.
    Volume and amount are validated as finite non-negative values but are left
    in the vendor's shares/yuan units for LedgerStore's existing inference.
    """
    clean: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise FeedError("Promax response contains a non-object row")
        row = dict(raw)
        raw_code = row.get("ts_code")
        if raw_code in (None, ""):
            for identity_field in ("code", "ticker", "symbol"):
                if row.get(identity_field) not in (None, ""):
                    raw_code = row.get(identity_field)
                    break
        if row.get("ts_code") in (None, "") and raw_code not in (None, ""):
            row["ts_code"] = raw_code
        if raw_code not in (None, "") and requested_codes is not None:
            code = _validate_ts_code(raw_code)
            row["ts_code"] = code
            if code not in requested_codes:
                raise FeedError(f"Promax response contains unrequested ts_code: {code}")
        elif raw_code in (None, "") and requested_codes is not None:
            raise FeedError("Promax minute response row missing ts_code")

        if minute:
            stamp_value = row.get("trade_time")
            if stamp_value in (None, ""):
                raise FeedError("Promax 分钟响应缺少有效 trade_time")
            stamp = str(stamp_value).strip()
            try:
                parsed_stamp = _parse_promax_time(stamp)
            except (TypeError, ValueError) as exc:
                raise FeedError("Promax trade_time 不是有效时间") from exc
            # Preserve the vendor text exactly for the audit trail.  The
            # consumer's MinuteBook requires a naive Shanghai ISO timestamp.
            row["time"] = parsed_stamp.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            row["vendor_time"] = stamp
            required_fields = ("open", "high", "low", "close", "vol", "amount")
            for field_name in required_fields:
                if field_name not in row or row[field_name] in (None, ""):
                    raise FeedError(f"Promax 分钟响应缺少 {field_name}")
                try:
                    number = float(row[field_name])
                except (TypeError, ValueError) as exc:
                    raise FeedError(f"Promax {field_name} 不是有效数值") from exc
                if not math.isfinite(number):
                    raise FeedError(f"Promax {field_name} 不是有限数值")
                if field_name in {"vol", "amount"} and number < 0:
                    raise FeedError(f"Promax {field_name} 不是有效非负数值")
                if field_name in {"open", "high", "low", "close"} and number <= 0:
                    raise FeedError(f"Promax {field_name} 必须大于零")
                row[field_name] = number
            try:
                open_price = float(row["open"])
                high_price = float(row["high"])
                low_price = float(row["low"])
                close_price = float(row["close"])
            except (TypeError, ValueError) as exc:
                raise FeedError("Promax OHLC 不是有效数值") from exc
            if not (
                low_price <= open_price <= high_price
                and low_price <= close_price <= high_price
            ):
                raise FeedError("Promax OHLC 价格关系无效")
        else:
            stamp = str(row.get("trade_time") or "").strip()
            if stamp:
                # Preserve the vendor text exactly.  Consumers perform
                # freshness checks and the conservative start-labelled
                # timing conversion.
                row["time"] = stamp
                row["vendor_time"] = stamp
            elif row.get("time") not in (None, ""):
                row["vendor_time"] = str(row.get("vendor_time") or row["time"])
        clean.append(row)
    return clean


def _retry_after_seconds(response: Any) -> float | None:
    """Read a numeric or HTTP-date Retry-After value without exposing headers."""
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _redact_secret(text: Any, secret: str) -> str:
    value = str(text)
    return value.replace(secret, "[REDACTED]") if secret else value


def _minute_freshness(
    rows: list[dict[str, Any]], now: datetime | None = None
) -> tuple[str, int, int]:
    """Classify vendor timestamps without changing or synthesizing them."""
    parsed: list[datetime] = []
    for row in rows:
        value = str(row.get("vendor_time") or row.get("time") or "").strip()
        if not value:
            continue
        try:
            stamp = _parse_promax_time(value)
        except (TypeError, ValueError):
            continue
        parsed.append(stamp)
    if len(parsed) != len(rows) or not parsed:
        return "unverified", 0, len(rows)
    current = now or datetime.now(_PROMAX_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_PROMAX_TIMEZONE)
    else:
        current = current.astimezone(_PROMAX_TIMEZONE)
    fresh = [
        stamp for stamp in parsed
        if stamp.date() == current.date()
        and 0 <= (current - stamp).total_seconds() <= PROMAX_FRESH_MAX_AGE_SECONDS
    ]
    if len(fresh) == len(parsed):
        return "fresh", len(fresh), len(parsed)
    if any(stamp > current for stamp in parsed):
        return "future", len(fresh), len(parsed)
    return "stale", len(fresh), len(parsed)


def _raise_for_status_with_detail(response: requests.Response) -> None:
    """Preserve vendor permission/IP-limit diagnostics without dumping payloads."""
    if int(getattr(response, "status_code", 200)) < 400:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("msg") or payload.get("message") or payload.get("error") or "")
    except ValueError:
        detail = ""
    detail = " ".join(detail.split())[:240]
    if detail:
        raise requests.HTTPError(f"HTTP {response.status_code}: {detail}", response=response)
    response.raise_for_status()


def _new_plan1_path(api_name: str) -> str:
    """The 15000-point endpoint exposes Tushare names as kebab-case paths."""
    aliases = {
        "stock_basic": "stock-basic",
        "daily_basic": "daily-basic",
        "trade_cal": "trade-cal",
        "namechange": "name-change",
        "new_share": "new-share",
        "rt_min": "rt-min",
        "rt_idx_min": "rt-idx-min",
        "rt_etf_k": "rt-etf-k",
        "fund_daily": "fund-daily",
        "index_daily": "index-daily",
        "stk_auction": "stk-auction",
    }
    return aliases.get(api_name, api_name.replace("_", "-"))


def _new_plan2_path(api_name: str) -> str:
    """The Replay API follows the direct ``/tushare/pro/{api}`` convention."""
    return api_name.strip("/")


def _validate_proxy(proxy: str) -> str:
    if proxy.count("@") > 1:
        raise FeedError("方案二代理格式无效：凭据与主机之间只能有一个 @")
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise FeedError("方案二代理格式无效：应为 http(s)://[用户名:密码@]主机:端口")
    return proxy


@dataclass
class ProviderHealth:
    mode: str = "legacy"
    plan1_ok: bool = False
    plan2_ok: bool = False
    plan1_latency: float | None = None
    plan2_latency: float | None = None
    plan1_endpoint: str = ""
    plan2_endpoint: str = ""
    plan2_proxy_configured: bool = False
    signal_timeout: float = 45.0
    execution_timeout: float = 30.0
    last_error: str = ""
    # Route-level checks are deliberately separate from plan1_ok/plan2_ok.
    # A maintenance or optional auction failure must not make the realtime
    # signal/execution route look unavailable.
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register_source(
        self,
        key: str,
        *,
        label: str,
        role: str,
        impact: str,
        endpoint: str = "",
        required: bool = False,
    ) -> None:
        row = self.sources.setdefault(key, {})
        row.setdefault("key", key)
        row.setdefault("label", label)
        row.setdefault("role", role)
        row.setdefault("impact", impact)
        row.setdefault("endpoint", endpoint)
        row.setdefault("required", required)
        row.setdefault("status", "not_tested")
        row.setdefault("last_checked_at", "")
        row.setdefault("last_ok_at", "")
        row.setdefault("last_error_at", "")
        row.setdefault("last_error", "")
        row.setdefault("latency_seconds", None)
        row.setdefault("rows", None)
        row.setdefault("coverage", None)
        row.setdefault("freshness", "unverified")
        row.setdefault("api", "")
        row.setdefault("attempts", 0)

    def record_source(
        self,
        key: str,
        *,
        ok: bool,
        label: str = "",
        role: str = "",
        impact: str = "",
        endpoint: str = "",
        required: bool = False,
        latency: float | None = None,
        rows: int | None = None,
        coverage: Any = None,
        api: str = "",
        attempts: int = 1,
        error: str = "",
    ) -> None:
        self.register_source(
            key,
            label=label or key,
            role=role or "diagnostic",
            impact=impact or "diagnostic-only",
            endpoint=endpoint,
            required=required,
        )
        now = datetime.now().isoformat(timespec="seconds")
        row = self.sources[key]
        row.update(
            {
                "last_checked_at": now,
                "latency_seconds": latency,
                "rows": rows,
                "coverage": coverage,
                "api": api,
                "attempts": attempts,
            }
        )
        if ok:
            row.update({"status": "healthy", "last_ok_at": now, "last_error": ""})
        else:
            row.update(
                {
                    "status": "degraded" if row.get("last_ok_at") else "down",
                    "last_error_at": now,
                    "last_error": error,
                }
            )

    def summary(self) -> dict[str, Any]:
        def ready(impact: str) -> bool:
            return any(
                row.get("impact") == impact and row.get("status") == "healthy"
                for row in self.sources.values()
            )

        def api_ready(api_name: str) -> bool:
            return any(
                key.endswith(f".{api_name}") and row.get("status") == "healthy"
                for key, row in self.sources.items()
            )

        return {
            "signal_ready": ready("signal-primary") or ready("signal-fallback"),
            "signal_primary_ready": ready("signal-primary"),
            "signal_fallback_ready": ready("signal-fallback"),
            "execution_ready": ready("execution-primary") or ready("execution-fallback"),
            "execution_primary_ready": ready("execution-primary"),
            "execution_fallback_ready": ready("execution-fallback"),
            "maintenance_ready": api_ready("fund_daily") and api_ready("index_daily"),
            "maintenance_fund_daily_ready": api_ready("fund_daily"),
            "maintenance_index_daily_ready": api_ready("index_daily"),
            "diagnostic_ready": ready("diagnostic-only"),
        }


class MarketDataProvider:
    """Plan 2 is the minute primary; plan 1 is reserved for sparse cross-checks."""

    def __init__(
        self,
        timeout: float = 45.0,
        mode: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        os.environ.setdefault("NO_PROXY", "*")
        os.environ.setdefault("no_proxy", "*")
        self.timeout = timeout
        self.realtime_timeout = min(timeout, 4.0)
        self.plan1_realtime_timeout = min(timeout, 8.0)
        try:
            configured_signal_timeout = float(os.environ.get("LIVE_PAPER_SIGNAL_TIMEOUT", "45"))
        except ValueError as exc:
            raise FeedError("LIVE_PAPER_SIGNAL_TIMEOUT 必须是秒数") from exc
        self.signal_timeout = min(timeout, max(15.0, min(configured_signal_timeout, 55.0)))
        self.preflight_timeout = min(self.signal_timeout, 25.0)
        self.execution_timeout = min(self.signal_timeout, 30.0)
        requested_mode = (mode or os.environ.get("LIVE_PAPER_PROVIDER_MODE", "auto")).strip().lower()
        if requested_mode not in {"auto", "legacy", "new", "datahub", "promax", "public"}:
            raise FeedError("LIVE_PAPER_PROVIDER_MODE 必须是 auto、legacy、new、datahub、promax 或 public")
        if requested_mode == "auto":
            if keychain_has_secret(NEW_PLAN1_SERVICE):
                requested_mode = "datahub"
            else:
                new_plan1_service = _first_keychain_service(NEW_PLAN1_SERVICE_ALIASES)
                new_plan2_service = _first_keychain_service(NEW_PLAN2_SERVICE_ALIASES)
                new_proxy_service = _first_keychain_service(NEW_PLAN2_PROXY_SERVICE_ALIASES)
                proxy_valid = True
                if new_proxy_service:
                    try:
                        _validate_proxy(keychain_secret(new_proxy_service))
                    except FeedError:
                        # A malformed proxy must not make the always-on service
                        # switch away from its known fallback source.
                        proxy_valid = False
                requested_mode = "new" if new_plan1_service and new_plan2_service and proxy_valid else "legacy"
        self.mode = requested_mode
        self.promax_key = ""
        self.promax_url = ""
        self.plan1_available = True
        if self.mode == "public":
            # Explicit opt-in: no Keychain inspection or paid route fallback.
            self.plan1_url = ""
            self.plan2_url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
            self.plan1_token = self.plan2_key = self.plan2_proxy = ""
            self.plan1_available = False
        elif self.mode == "promax":
            # Promax is a fully independent route.  It never reads the old
            # plan-2 key/proxy.  A Datahub key is optional and is used only
            # for the maintenance cross-check that Promax may not serve.
            self.promax_url = PROMAX_URL
            self.plan2_url = PROMAX_URL
            self.promax_key = keychain_secret(PROMAX_SERVICE)
            self.plan2_key = self.promax_key
            self.plan1_url = NEW_PLAN1_URL
            self.plan1_token = ""
            self.plan1_available = False
            try:
                plan1_service = _first_keychain_service(NEW_PLAN1_SERVICE_ALIASES)
                if plan1_service:
                    self.plan1_token = keychain_secret(plan1_service)
                    self.plan1_available = bool(self.plan1_token)
            except (FeedError, OSError):
                # Datahub maintenance is optional in Promax mode.  A missing
                # or unreadable item must not prevent the independent client
                # from initializing.
                self.plan1_token = ""
                self.plan1_available = False
            self.plan2_proxy = ""
        elif self.mode == "new":
            self.plan1_url = NEW_PLAN1_URL
            self.plan2_url = NEW_PLAN2_URL
            plan1_service = _first_keychain_service(NEW_PLAN1_SERVICE_ALIASES)
            plan2_service = _first_keychain_service(NEW_PLAN2_SERVICE_ALIASES)
            if not plan1_service or not plan2_service:
                raise FeedError("新方案 Keychain 凭据不完整")
            self.plan1_token = keychain_secret(plan1_service)
            self.plan2_key = keychain_secret(plan2_service)
            self.plan2_proxy = ""
            proxy_service = _first_keychain_service(NEW_PLAN2_PROXY_SERVICE_ALIASES)
            if proxy_service:
                self.plan2_proxy = _validate_proxy(keychain_secret(proxy_service))
        elif self.mode == "datahub":
            # The 15000-point datahub is the realtime primary.  Keep the
            # legacy plan-1 client as an independent sparse cross-check and
            # daily fallback; its credential is never sent to datahub.
            self.plan1_url = LEGACY_PLAN1_URL
            self.plan2_url = NEW_PLAN1_URL
            self.plan1_token = keychain_secret(LEGACY_PLAN1_SERVICE)
            self.plan2_key = keychain_secret(NEW_PLAN1_SERVICE)
            self.plan2_proxy = ""
        else:
            self.plan1_url = LEGACY_PLAN1_URL
            self.plan2_url = LEGACY_PLAN2_URL
            self.plan1_token = keychain_secret(LEGACY_PLAN1_SERVICE)
            self.plan2_key = keychain_secret(LEGACY_PLAN2_SERVICE)
            self.plan2_proxy = ""
        self.session = session if session is not None else self._make_session()
        self.quote_session = requests.Session()
        self.quote_session.trust_env = False
        self.health = ProviderHealth(
            mode=self.mode,
            plan1_endpoint=self.plan1_url,
            plan2_endpoint=self.plan2_url,
            plan2_proxy_configured=bool(self.plan2_proxy),
            signal_timeout=self.signal_timeout,
            execution_timeout=self.execution_timeout,
        )
        self._register_sources()
        self.last_signal_source = ""

    def _register_sources(self) -> None:
        """Expose all meaningful routes before the first request is made."""
        plan1_label = self._plan_label("plan1")
        plan2_label = self._plan_label("plan2")
        self.health.register_source(
            "signal.sina", label="新浪累计行情", role="signal", impact="signal-primary",
            endpoint=SINA_URL, required=True,
        )
        self.health.register_source(
            "signal.tencent", label="腾讯累计行情", role="signal", impact="signal-fallback",
            endpoint=TENCENT_URL, required=True,
        )
        if self.mode == "public":
            self.health.register_source(
                "public.eastmoney_minutes", label="东方财富免费分钟", role="execution",
                impact="execution-primary", endpoint=self.plan2_url, required=True,
            )
            return
        for which, endpoint in (("plan1", self.plan1_url), ("plan2", self.plan2_url)):
            plan_label = plan1_label if which == "plan1" else plan2_label
            self.health.register_source(
                f"{which}.rt_min", label=f"{plan_label} rt_min 分钟执行", role="execution",
                impact="execution-primary" if which == "plan2" else "execution-fallback",
                endpoint=endpoint, required=which == "plan2",
            )
            for api in ("fund_daily", "index_daily"):
                self.health.register_source(
                    f"{which}.{api}", label=f"{plan_label} {api} 日线维护", role="maintenance",
                    impact="maintenance", endpoint=endpoint,
                )
            self.health.register_source(
                f"{which}.stk_auction", label=f"{plan_label} 集合竞价诊断", role="auction",
                impact="diagnostic-only", endpoint=endpoint,
            )
        if self.mode == "promax":
            # The ETF route has its own vendor endpoint.  Keep the stock route
            # visible separately because a future stock shadow caller uses
            # rt_min and must not be confused with ETF coverage.
            self.health.register_source(
                "promax.rt_etf_min", label="Promax rt_etf_min ETF分钟执行",
                role="execution", impact="execution-primary",
                endpoint=self.plan2_url, required=True,
            )
            self.health.register_source(
                "promax.rt_min", label="Promax rt_min 股票分钟执行",
                role="execution", impact="execution-primary",
                endpoint=self.plan2_url, required=True,
            )
            self.health.register_source(
                "promax.rt_idx_min", label="Promax rt_idx_min 指数分钟执行",
                role="execution", impact="execution-primary",
                endpoint=self.plan2_url, required=True,
            )

    def _plan_label(self, which: str) -> str:
        mode = getattr(self, "mode", "legacy")
        if mode == "promax":
            return "Datahub日线(可选)" if which == "plan1" else "Promax"
        if mode == "new":
            return "方案一(15000积分)" if which == "plan1" else "全量接口1(独享代理)"
        if mode == "datahub":
            return "旧方案一" if which == "plan1" else "方案一(15000积分)"
        return "旧方案一" if which == "plan1" else "旧方案二"

    @staticmethod
    def _route_source(
        which: str, api_name: str, mode: str | None = None
    ) -> tuple[str, str, str, bool]:
        if mode == "promax" and which == "plan2" and api_name in {"rt_min_daily", "rt_etf_min_daily"}:
            return f"promax.{api_name}", "execution", "execution-fallback", False
        if (
            mode == "promax"
            and which == "plan2"
            and api_name in {"rt_etf_min", "rt_min", "rt_idx_min"}
        ):
            return (
                f"promax.{api_name}", "execution", "execution-primary", True
            )
        if api_name == "rt_min":
            return (
                f"{which}.rt_min", "execution",
                "execution-primary" if which == "plan2" else "execution-fallback",
                which == "plan2",
            )
        if api_name in {"fund_daily", "index_daily"}:
            return f"{which}.{api_name}", "maintenance", "maintenance", False
        if api_name == "stk_auction":
            return f"{which}.stk_auction", "auction", "diagnostic-only", False
        return f"{which}.{api_name}", "diagnostic", "diagnostic-only", False

    def _record_route(
        self,
        which: str,
        api_name: str,
        *,
        ok: bool,
        latency: float | None = None,
        rows: int | None = None,
        coverage: Any = None,
        error: str = "",
        attempts: int = 1,
    ) -> None:
        key, role, impact, required = self._route_source(
            which, api_name, getattr(self, "mode", "legacy")
        )
        endpoint = self.plan1_url if which == "plan1" else self.plan2_url
        self.health.record_source(
            key,
            ok=ok,
            label=f"{self._plan_label(which)} {api_name}",
            role=role,
            impact=impact,
            endpoint=endpoint,
            required=required,
            latency=latency,
            rows=rows,
            coverage=coverage,
            api=api_name,
            attempts=attempts,
            error=error,
        )

    def _record_public_source(
        self,
        key: str,
        *,
        label: str,
        impact: str,
        endpoint: str,
        ok: bool,
        latency: float | None = None,
        rows: int | None = None,
        coverage: Any = None,
        error: str = "",
    ) -> None:
        if hasattr(self, "health"):
            self.health.record_source(
                key,
                ok=ok,
                label=label,
                role="signal",
                impact=impact,
                endpoint=endpoint,
                required=True,
                latency=latency,
                rows=rows,
                coverage=coverage,
                error=error,
            )

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def _renew_session(self) -> None:
        self.session.close()
        self.session = self._make_session()

    def _new_get(
        self,
        which: str,
        api_name: str,
        params: dict[str, Any],
        fields: str,
        timeout: float,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        endpoint = self.plan1_url if which == "plan1" else self.plan2_url
        path = (
            _new_plan1_path(api_name)
            if which == "plan1" or self.mode == "datahub"
            else _new_plan2_path(api_name)
        )
        query = dict(params)
        if fields:
            query["fields"] = fields
        key = self.plan1_token if which == "plan1" else self.plan2_key
        try:
            request_kwargs: dict[str, Any] = {
                "headers": {
                    "X-API-Key": key,
                    "Accept": "application/json",
                },
                "params": query,
                "proxies": (
                    {"http": self.plan2_proxy, "https": self.plan2_proxy}
                    if which == "plan2" and self.plan2_proxy else None
                ),
                "timeout": (
                    min(float(timeout), PROMAX_MAX_TIMEOUT_SECONDS)
                    if self.mode == "promax" else timeout
                ),
            }
            if self.mode == "promax":
                # The optional Datahub route is still inside Promax mode and
                # must not forward the independent key through redirects.
                request_kwargs["allow_redirects"] = False
            response = self.session.get(
                f"{endpoint.rstrip('/')}/{path}",
                **request_kwargs,
            )
            if self.mode == "promax":
                status = int(getattr(response, "status_code", 200))
                if 300 <= status < 400:
                    raise requests.HTTPError(
                        f"HTTP {status}: 禁止重定向", response=response
                    )
            _raise_for_status_with_detail(response)
            rows = _rows(response.json())
            latency = round(time.monotonic() - started, 3)
            self._record_route(which, api_name, ok=True, latency=latency, rows=len(rows))
            if which == "plan1":
                self.health.plan1_ok = True
                self.health.plan1_latency = latency
            else:
                self.health.plan2_ok = True
                self.health.plan2_latency = latency
            self.health.last_error = ""
            return rows
        except Exception as exc:
            if which == "plan1":
                self.health.plan1_ok = False
            else:
                self.health.plan2_ok = False
            message = f"{which} {api_name}: {type(exc).__name__}: {exc}"
            if self.mode == "promax":
                message = _redact_secret(message, str(key))
            self._record_route(which, api_name, ok=False, error=message)
            self.health.last_error = message
            raise FeedError(message) from (None if self.mode == "promax" else exc)

    def _promax_get(
        self,
        api_name: str,
        params: dict[str, Any],
        fields: str,
        retry_delays: tuple[int, ...],
        retry_on_429: bool,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """Call Promax's direct GET route within one bounded time budget."""
        started = time.monotonic()
        # Keep response metadata available to the minute contract without
        # changing the long-standing list[dict] plan2 API.  Clear it before
        # every attempt so a previous daily/minute response cannot authorize a
        # later request when the fake or real session fails early.
        self._promax_last_meta: dict[str, Any] = {}
        try:
            raw_budget = float(timeout)
        except (TypeError, ValueError):
            raise FeedError("Promax 请求超时必须是正数") from None
        if not math.isfinite(raw_budget) or raw_budget <= 0:
            raise FeedError("Promax 请求超时必须是正数") from None
        budget = min(raw_budget, PROMAX_MAX_TIMEOUT_SECONDS)
        deadline = started + budget
        key = str(getattr(self, "promax_key", "") or getattr(self, "plan2_key", ""))
        if not key:
            raise FeedError("Promax Keychain 中缺少独立凭据")
        query = {name: value for name, value in dict(params).items() if value is not None}
        if isinstance(query.get("freq"), str):
            query["freq"] = query["freq"].strip().lower()
        if fields:
            query["fields"] = fields
        endpoint = str(getattr(self, "promax_url", "") or getattr(self, "plan2_url", PROMAX_URL))
        url = f"{endpoint.rstrip('/')}/{api_name.strip('/')}"
        # A caller may pass the legacy (0, 5, 15) defaults.  The number of
        # attempts is nevertheless capped at one initial request plus two
        # retries; the caller's delays remain upper-bound hints unless the
        # server supplies a longer Retry-After value.
        delays: list[float] = []
        for value in tuple(retry_delays)[:PROMAX_MAX_ATTEMPTS]:
            try:
                delays.append(max(0.0, float(value)))
            except (TypeError, ValueError):
                delays.append(0.0)
        if not delays:
            delays = [0.0]
        attempts = 0
        pending_delay = delays[0]
        last_error: Exception | None = None
        for attempt_index in range(len(delays)):
            if attempt_index:
                remaining = deadline - time.monotonic()
                if pending_delay > remaining:
                    last_error = FeedError("Promax 重试等待时间超过 signal/execution 预算")
                    break
                if pending_delay > 0:
                    time.sleep(pending_delay)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = FeedError("Promax 请求超过 signal/execution 预算")
                break
            attempts += 1
            response: Any = None
            try:
                response = self.session.get(
                    url,
                    headers={"X-API-Key": key, "Accept": "application/json"},
                    params=query,
                    # Promax must never inherit the old plan-2 proxy.  The
                    # session itself also has trust_env=False in production.
                    proxies=None,
                    timeout=min(budget, remaining),
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt_index + 1 >= len(delays):
                    break
                pending_delay = max(delays[attempt_index + 1], 0.0)
                continue

            try:
                status = int(getattr(response, "status_code", 200))
            except (TypeError, ValueError):
                status = 200
            if time.monotonic() >= deadline:
                last_error = FeedError("Promax 响应超过 signal/execution 预算")
                break
            if 300 <= status < 400:
                last_error = FeedError(f"Promax HTTP {status}: 禁止重定向")
                break
            retryable = status in PROMAX_RETRYABLE_STATUSES and (
                status != 429 or retry_on_429
            )
            if retryable and attempt_index + 1 < len(delays):
                retry_after = _retry_after_seconds(response)
                fallback_delay = delays[attempt_index + 1]
                pending_delay = max(fallback_delay, retry_after or 0.0)
                last_error = requests.HTTPError(f"HTTP {status}", response=response)
                continue
            try:
                _raise_for_status_with_detail(response)
                payload = response.json()
                rows = _rows(payload)
                meta = payload.get("meta") if isinstance(payload, dict) else None
                self._promax_last_meta = dict(meta) if isinstance(meta, dict) else {}
                latency = round(time.monotonic() - started, 3)
                self._record_route(
                    "plan2", api_name, ok=True, latency=latency,
                    rows=len(rows), attempts=attempts,
                )
                self.health.plan2_ok = True
                self.health.plan2_latency = latency
                self.health.last_error = ""
                return _normalise_promax_rows(rows)
            except Exception as exc:
                # Business errors in a HTTP 200 response are terminal.  A
                # retry is reserved for transport/server statuses above.
                last_error = exc
                break

        if last_error is None:
            last_error = FeedError("Promax 请求失败")
        self.health.plan2_ok = False
        message = _redact_secret(
            f"plan2 {api_name}: {type(last_error).__name__}: {last_error}", key
        )
        self._record_route(
            "plan2", api_name, ok=False, latency=round(time.monotonic() - started, 3),
            error=message, attempts=attempts or len(delays),
        )
        self.health.last_error = message
        # ``last_error`` can contain a request library's diagnostic repr.  It
        # is intentionally not retained as the exception cause because the
        # cause traceback could expose the credential even after the public
        # message has been redacted.
        raise FeedError(message) from None

    def _promax_minute_failure(
        self,
        api_name: str,
        message: str,
        *,
        rows: int = 0,
        coverage: Any = None,
    ) -> None:
        """Record a fail-closed minute result and raise a safe error."""
        safe_message = _redact_secret(
            f"Promax {api_name}: {message}",
            str(getattr(self, "promax_key", "") or getattr(self, "plan2_key", "")),
        )
        health = getattr(self, "health", None)
        if health is not None:
            health.plan2_ok = False
        self._record_route(
            "plan2",
            api_name,
            ok=False,
            rows=rows,
            coverage=coverage,
            error=safe_message,
        )
        if health is not None:
            health.last_error = safe_message
        raise FeedError(safe_message) from None

    def _promax_minute_rows(
        self,
        api_name: str,
        ts_codes: list[str] | tuple[str, ...],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        codes = _normalise_ts_codes(ts_codes)
        if not codes:
            return []
        self._promax_last_meta = {}
        params: dict[str, Any] = {
            "ts_code": ",".join(codes),
            "freq": "1min",
            "__probe": 0,
            # Keep the first formal read small while still allowing one row
            # for each requested code in a normal <=10-code execution batch.
            "limit": max(PROMAX_MINUTE_LIMIT, len(codes)),
        }
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if api_name in {"rt_min_daily", "rt_etf_min_daily"}:
            params["trade_date"] = datetime.now(_PROMAX_TIMEZONE).strftime("%Y%m%d")
        rows = self.plan2(
            api_name,
            params,
            retry_delays=(0, 1, 2),
            retry_on_429=True,
            timeout_override=(
                timeout if timeout is not None else getattr(
                    self, "execution_timeout", getattr(self, "timeout", 30.0)
                )
            ),
        )
        try:
            clean = _normalise_promax_rows(rows, requested_codes=set(codes), minute=True)
        except FeedError as exc:
            self._promax_minute_failure(
                api_name,
                str(exc),
                rows=len(rows),
            )
        meta = getattr(self, "_promax_last_meta", {})
        if api_name in {"rt_min_daily", "rt_etf_min_daily"}:
            # This vendor ignores start/end on its local daily-minute route.
            # Restrict the canonical timestamps locally before the ordinary
            # coverage/freshness gate; never admit its full-day history.
            clean = [row for row in clean
                     if (start_date is None or row["time"] >= start_date)
                     and (end_date is None or row["time"] <= end_date)]
        meta_stale = isinstance(meta, dict) and (
            meta.get("stale") is True
            or str(meta.get("stale") or "").strip().lower() in {"true", "1", "yes"}
        )
        covered = {str(row.get("ts_code") or "") for row in clean} & set(codes)
        coverage = {"covered": len(covered), "requested": len(codes)}
        if covered != set(codes):
            self._promax_minute_failure(
                api_name,
                f"coverage {len(covered)}/{len(codes)}",
                rows=len(clean),
                coverage=coverage,
            )
        freshness, fresh_count, total_count = _minute_freshness(clean)
        coverage.update({"fresh": fresh_count, "fresh_rows": total_count})
        source_key = {
            "rt_etf_min": "promax.rt_etf_min",
            "rt_min": "promax.rt_min",
            "rt_idx_min": "promax.rt_idx_min",
        }.get(api_name, f"promax.{api_name}")
        source = getattr(self, "health", None)
        source_row = source.sources.get(source_key) if source is not None else None
        if source_row is not None:
            source_row["coverage"] = coverage
            source_row["freshness"] = "stale" if meta_stale else freshness
        if meta_stale:
            self._promax_minute_failure(
                api_name,
                "vendor meta.stale=true; freshness gate rejected",
                rows=len(clean),
                coverage=coverage,
            )
        if freshness != "fresh":
            self._promax_minute_failure(
                api_name,
                f"vendor time {freshness}; freshness gate rejected",
                rows=len(clean),
                coverage=coverage,
            )
        return clean

    def _promax_batched_minutes(
        self,
        api_name: str,
        ts_codes: list[str] | tuple[str, ...],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        codes = _normalise_ts_codes(ts_codes)
        if not codes:
            return []
        try:
            budget = float(
                timeout if timeout is not None
                else getattr(self, "execution_timeout", getattr(self, "timeout", 30.0))
            )
        except (TypeError, ValueError):
            raise FeedError("Promax 批量请求超时必须是正数") from None
        if not math.isfinite(budget) or budget <= 0:
            raise FeedError("Promax 批量请求超时必须是正数") from None
        budget = min(budget, PROMAX_MAX_TIMEOUT_SECONDS)
        started = time.monotonic()
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(codes), PROMAX_MINUTE_BATCH_SIZE):
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise FeedError(f"Promax {api_name} 批量请求超过 execution 预算")
            rows.extend(
                self._promax_minute_rows(
                    api_name,
                    codes[offset:offset + PROMAX_MINUTE_BATCH_SIZE],
                    start_date=start_date,
                    end_date=end_date,
                    timeout=remaining,
                )
            )
        return rows

    def plan1(
        self,
        api_name: str,
        params: dict[str, Any],
        fields: str = "",
        timeout_override: float | None = None,
    ) -> list[dict[str, Any]]:
        if self.mode == "public":
            raise FeedError("public 模式不调用付费 plan1；日线维护须使用已审计缓存或独立补齐")
        if self.mode == "promax" and not getattr(self, "plan1_token", ""):
            message = "promax plan1 Datahub 未配置（可选日线路由）"
            self.health.plan1_ok = False
            self._record_route("plan1", api_name, ok=False, error=message)
            self.health.last_error = message
            raise FeedError(message)
        if self.mode == "new" or (self.mode == "promax" and getattr(self, "plan1_token", "")):
            return self._new_get(
                "plan1", api_name, params, fields,
                timeout_override if timeout_override is not None else (
                    self.plan1_realtime_timeout if api_name in {"rt_min", "rt_idx_min", "rt_etf_k"} else self.timeout
                ),
            )
        started = time.monotonic()
        try:
            response = self.session.post(
                self.plan1_url,
                json={
                    "api_name": api_name,
                    "token": self.plan1_token,
                    "params": params,
                    "fields": fields,
                },
                timeout=(
                    timeout_override if timeout_override is not None else (
                        self.plan1_realtime_timeout
                        if api_name in {"rt_min", "rt_idx_min", "rt_etf_k"}
                        else self.timeout
                    )
                ),
            )
            response.raise_for_status()
            rows = _rows(response.json())
            latency = round(time.monotonic() - started, 3)
            self._record_route("plan1", api_name, ok=True, latency=latency, rows=len(rows))
            self.health.plan1_ok = True
            self.health.plan1_latency = latency
            self.health.last_error = ""
            return rows
        except Exception as exc:
            self.health.plan1_ok = False
            message = f"plan1 {api_name}: {type(exc).__name__}: {exc}"
            self._record_route("plan1", api_name, ok=False, error=message)
            self.health.last_error = message
            raise FeedError(self.health.last_error) from exc

    def plan2(
        self,
        api_name: str,
        params: dict[str, Any],
        fields: str = "",
        retry_delays: tuple[int, ...] = (0, 5, 15),
        retry_on_429: bool = True,
        timeout_override: float | None = None,
    ) -> list[dict[str, Any]]:
        if self.mode == "public":
            raise FeedError("public 模式不调用付费 plan2；此 API 尚无已验收免费适配")
        if self.mode == "promax":
            if timeout_override is not None:
                request_timeout = timeout_override
            elif api_name in {"rt_min", "rt_etf_min", "rt_idx_min", "rt_etf_k"}:
                request_timeout = getattr(
                    self, "realtime_timeout", getattr(self, "timeout", 30.0)
                )
            else:
                request_timeout = getattr(self, "timeout", 30.0)
            return self._promax_get(
                api_name,
                params,
                fields,
                retry_delays,
                retry_on_429,
                request_timeout,
            )
        if self.mode in {"new", "datahub"}:
            started = time.monotonic()
            last_request_error: requests.RequestException | None = None
            try:
                for attempt, delay in enumerate(retry_delays):
                    if delay:
                        time.sleep(delay)
                    try:
                        return self._new_get(
                            "plan2", api_name, params, fields,
                            timeout_override if timeout_override is not None else (
                                self.realtime_timeout
                                if api_name in {"rt_min", "rt_idx_min", "rt_etf_k"}
                                else self.timeout
                            ),
                        )
                    except requests.RequestException as exc:
                        last_request_error = exc
                        self._renew_session()
                        if attempt < len(retry_delays) - 1:
                            continue
                        raise
                    except FeedError as exc:
                        # HTTP 429 is retriable only when the caller opted in;
                        # transient network errors may be retried; 403/auth/
                        # shape errors must fail closed immediately.
                        text = str(exc)
                        transient = any(
                            marker in text
                            for marker in ("ReadTimeout", "ConnectTimeout", "ConnectionError", "ProxyError")
                        )
                        if (
                            ("429" in text and retry_on_429) or transient
                        ) and attempt < len(retry_delays) - 1:
                            if transient:
                                self._renew_session()
                            continue
                        raise
                if last_request_error is not None:
                    raise last_request_error
                raise FeedError(f"plan2 {api_name}: exhausted retries")
            except Exception as exc:
                self.health.plan2_ok = False
                message = f"plan2 {api_name}: {type(exc).__name__}: {exc}"
                self._record_route("plan2", api_name, ok=False, error=message)
                self.health.last_error = message
                raise FeedError(self.health.last_error) from exc
        started = time.monotonic()
        try:
            last_request_error: requests.RequestException | None = None
            for attempt, delay in enumerate(retry_delays):
                if delay:
                    time.sleep(delay)
                try:
                    response = self.session.post(
                        self.plan2_url,
                        headers={
                            "X-API-Key": self.plan2_key,
                            "Content-Type": "application/json",
                            "Connection": "close",
                        },
                        json={"api_name": api_name, "params": params, "fields": fields},
                        timeout=(
                            timeout_override if timeout_override is not None else (
                                self.realtime_timeout
                                if api_name in {"rt_min", "rt_idx_min", "rt_etf_k"}
                                else self.timeout
                            )
                        ),
                    )
                except requests.RequestException as exc:
                    last_request_error = exc
                    self._renew_session()
                    if attempt < len(retry_delays) - 1:
                        continue
                    raise
                if response.status_code == 429 and retry_on_429 and attempt < len(retry_delays) - 1:
                    continue
                response.raise_for_status()
                rows = _rows(response.json())
                latency = round(time.monotonic() - started, 3)
                self._record_route("plan2", api_name, ok=True, latency=latency, rows=len(rows), attempts=attempt + 1)
                self.health.plan2_ok = True
                self.health.plan2_latency = latency
                self.health.last_error = ""
                return rows
            if last_request_error is not None:
                raise last_request_error
            raise FeedError(f"plan2 {api_name}: exhausted retries")
        except Exception as exc:
            self.health.plan2_ok = False
            message = f"plan2 {api_name}: {type(exc).__name__}: {exc}"
            self._record_route("plan2", api_name, ok=False, error=message, attempts=len(retry_delays))
            self.health.last_error = message
            raise FeedError(self.health.last_error) from exc

    def _public_minutes(self, ts_codes: list[str]) -> list[dict[str, Any]]:
        from .public_minutes import PublicMinuteError, fetch_minutes

        started = time.monotonic()
        try:
            rows = fetch_minutes(self.session, ts_codes, datetime.now(_PROMAX_TIMEZONE),
                                 timeout=min(self.execution_timeout, 12.0))
        except (PublicMinuteError, requests.RequestException) as exc:
            message = f"public minute unavailable: {type(exc).__name__}: {exc}"
            self.health.record_source("public.eastmoney_minutes", ok=False, error=message,
                                      latency=round(time.monotonic() - started, 3))
            self.health.sources["public.eastmoney_minutes"]["freshness"] = "unverified"
            self.health.last_error = message
            raise FeedError(message) from None
        self.health.record_source(
            "public.eastmoney_minutes", ok=True, rows=len(rows),
            latency=round(time.monotonic() - started, 3),
            coverage={"covered": len({row["ts_code"] for row in rows}),
                      "requested": len(set(ts_codes))},
        )
        self.health.sources["public.eastmoney_minutes"]["freshness"] = "fresh"
        self.health.last_error = ""
        return rows

    def etf_minutes(self, ts_codes: list[str]) -> list[dict[str, Any]]:
        if not ts_codes:
            return []
        if self.mode == "public":
            return self._public_minutes(ts_codes)
        if self.mode == "promax":
            budget = float(getattr(self, "execution_timeout", getattr(self, "timeout", 30.0)))
            started = time.monotonic()
            try:
                return self._promax_batched_minutes("rt_etf_min", ts_codes, timeout=budget / 2)
            except FeedError as primary:
                # The merchant's daily-minute ETF route only honours one
                # ticker per call. Never mistake first-ticker data for full
                # batch coverage. All requested tickers must pass the gate.
                rows = []
                now = datetime.now(_PROMAX_TIMEZONE)
                lower = (now - timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S")
                upper = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
                codes = _normalise_ts_codes(ts_codes)
                try:
                    for code in codes:
                        remaining = budget - (time.monotonic() - started)
                        if remaining <= 0:
                            raise FeedError("ETF fallback exhausted shared execution budget")
                        rows.extend(self._promax_minute_rows(
                            "rt_etf_min_daily", [code], start_date=lower,
                            end_date=upper, timeout=remaining / (len(codes) - codes.index(code)),
                        ))
                    self._record_route("plan2", "rt_etf_min_daily", ok=True, rows=len(rows),
                                       coverage={"covered": len(codes), "requested": len(codes)})
                    return rows
                except FeedError as fallback:
                    raise FeedError(f"ETF minute routes failed: primary={primary}; fallback={fallback}") from None
        params = {"ts_code": ",".join(ts_codes), "freq": "1MIN"}
        # Do not let retries spill into the next minute and consume the three
        # requests reserved for a scheduled signal snapshot.  Missing regular
        # observations are allowed; signal snapshots have their own fail-closed
        # gate and plan 1 remains reserved for sparse cross-checks.
        # A 429 request itself extends this vendor's rolling window, so retry
        # transient network failures once but never retry a 429 here.
        try:
            rows = self.plan2(
                "rt_min",
                params,
                retry_delays=(0,),
                retry_on_429=False,
                timeout_override=self.execution_timeout,
            )
            source = self.health.sources.get("plan2.rt_min")
            if source is not None:
                covered = len({str(row.get("ts_code") or "") for row in rows} & set(ts_codes))
                source["coverage"] = {"covered": covered, "requested": len(set(ts_codes))}
                if covered == 0:
                    self._record_route(
                        "plan2", "rt_min", ok=False, rows=len(rows),
                        error="rt_min 返回 0 个请求标的，执行数据不可用",
                    )
                    raise FeedError("plan2 rt_min: coverage 0/" + str(len(set(ts_codes))))
            return rows
        except FeedError as primary:
            # Keep a short independent route fallback available for providers
            # that expose rt_min on plan 1.  If it is not permitted, the
            # caller preserves the order and retries later; no synthetic bar
            # is manufactured from cumulative quotes.
            if self.mode in {"new", "datahub"}:
                try:
                    rows = self.plan1(
                        "rt_min", params, timeout_override=min(self.plan1_realtime_timeout, 8.0)
                    )
                    source = self.health.sources.get("plan1.rt_min")
                    if source is not None:
                        covered = len({str(row.get("ts_code") or "") for row in rows} & set(ts_codes))
                        source["coverage"] = {"covered": covered, "requested": len(set(ts_codes))}
                        if covered == 0:
                            self._record_route(
                                "plan1", "rt_min", ok=False, rows=len(rows),
                                error="rt_min 返回 0 个请求标的，执行数据不可用",
                            )
                            raise FeedError("plan1 rt_min: coverage 0/" + str(len(set(ts_codes))))
                    return rows
                except FeedError as fallback:
                    raise FeedError(f"execution routes failed: primary={primary}; fallback={fallback}") from fallback
            raise

    def etf_minutes_plan1(self, ts_codes: list[str]) -> list[dict[str, Any]]:
        if not ts_codes:
            return []
        rows = self.plan1("rt_min", {"ts_code": ",".join(ts_codes), "freq": "1MIN"})
        source = self.health.sources.get("plan1.rt_min")
        if source is not None:
            covered = len({str(row.get("ts_code") or "") for row in rows} & set(ts_codes))
            source["coverage"] = {"covered": covered, "requested": len(set(ts_codes))}
        return rows

    def index_minutes(self, ts_codes: list[str]) -> list[dict[str, Any]]:
        if not ts_codes:
            return []
        if self.mode == "promax":
            return self._promax_batched_minutes(
                "rt_idx_min", ts_codes,
                timeout=getattr(self, "execution_timeout", getattr(self, "timeout", 30.0)),
            )
        return self.plan2("rt_idx_min", {"ts_code": ",".join(ts_codes), "freq": "1MIN"})

    def signal_preflight(self, ts_codes: list[str]) -> list[dict[str, Any]]:
        """Warm the authenticated route before a signal minute.

        The result is diagnostic only and is never reused as a signal price.
        It establishes the proxy tunnel/session and detects permission or route
        failures while there is still time to use the independent quote fallback.
        """
        sample = list(dict.fromkeys(ts_codes))[:3]
        if not sample:
            return []
        if getattr(self, "mode", "legacy") == "public":
            return self._public_minutes(sample)
        if getattr(self, "mode", "legacy") == "promax":
            rows = self._promax_minute_rows(
                "rt_etf_min", sample,
                timeout=getattr(
                    self, "preflight_timeout",
                    getattr(self, "execution_timeout", getattr(self, "timeout", 30.0)),
                ),
            )
            covered = {str(row.get("ts_code") or "") for row in rows} & set(sample)
            if len(covered) != len(sample):
                raise FeedError(f"signal preflight coverage insufficient: {len(covered)}/{len(sample)}")
            return rows
        rows = self.plan2(
            "rt_min",
            {"ts_code": ",".join(sample), "freq": "1MIN"},
            retry_delays=(0,),
            retry_on_429=False,
            timeout_override=self.preflight_timeout,
        )
        covered = {str(row.get("ts_code") or "") for row in rows} & set(sample)
        health = getattr(self, "health", None)
        source = health.sources.get("plan2.rt_min") if health is not None else None
        if source is not None:
            source["coverage"] = {"covered": len(covered), "requested": len(sample)}
        if len(covered) != len(sample):
            raise FeedError(f"signal preflight coverage insufficient: {len(covered)}/{len(sample)}")
        return rows

    @staticmethod
    def _sina_code(ts_code: str) -> str:
        code, market = ts_code.split(".")
        return f"{'sh' if market == 'SH' else 'sz'}{code}"

    def sina_signal_snapshot(self, etf_codes: list[str], index_codes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        started = time.monotonic()
        mapping = {self._sina_code(code): code for code in etf_codes + index_codes}
        quote_session = getattr(self, "quote_session", self.session)
        try:
            response = quote_session.get(
                SINA_URL + ",".join(mapping),
                headers={"Referer": "http://finance.sina.com.cn", "Connection": "close"},
                timeout=self.realtime_timeout,
            )
            response.raise_for_status()
            etf_set, index_set = set(etf_codes), set(index_codes)
            etf_rows: list[dict[str, Any]] = []
            index_rows: list[dict[str, Any]] = []
            for line in response.content.decode("gbk", "replace").splitlines():
                if "=" not in line:
                    continue
                variable, value = line.split("=", 1)
                sina_code = variable.rsplit("_", 1)[-1]
                ts_code = mapping.get(sina_code)
                fields = value.strip('";').split(",")
                if not ts_code or len(fields) < 32:
                    continue
                row = {
                    "ts_code": ts_code,
                    "name": fields[0],
                    "open": fields[1],
                    "pre_close": fields[2],
                    "close": fields[3],
                    "high": fields[4],
                    "low": fields[5],
                    "vol": fields[8],
                    "amount": fields[9],
                    "time": f"{fields[30]} {fields[31]}",
                }
                if ts_code in etf_set:
                    etf_rows.append(row)
                elif ts_code in index_set:
                    index_rows.append(row)
            self._record_public_source(
                "signal.sina", label="新浪累计行情", impact="signal-primary", endpoint=SINA_URL,
                ok=True, latency=round(time.monotonic() - started, 3),
                rows=len(etf_rows) + len(index_rows),
                coverage={"etf": len(etf_rows), "index": len(index_rows)},
            )
            return etf_rows, index_rows
        except Exception as exc:
            self._record_public_source(
                "signal.sina", label="新浪累计行情", impact="signal-primary", endpoint=SINA_URL,
                ok=False, latency=round(time.monotonic() - started, 3), error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def tencent_signal_snapshot(self, etf_codes: list[str], index_codes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Independent cumulative-day quote fallback with Sina-compatible units."""
        started = time.monotonic()
        mapping = {self._sina_code(code): code for code in etf_codes + index_codes}
        quote_session = getattr(self, "quote_session", self.session)
        try:
            response = quote_session.get(
                TENCENT_URL + ",".join(mapping),
                headers={"Referer": "http://finance.qq.com", "Connection": "close"},
                timeout=self.realtime_timeout,
            )
            response.raise_for_status()
            etf_set, index_set = set(etf_codes), set(index_codes)
            etf_rows: list[dict[str, Any]] = []
            index_rows: list[dict[str, Any]] = []
            for line in response.content.decode("gbk", "replace").splitlines():
                if '="' not in line:
                    continue
                variable, value = line.split('="', 1)
                quote_code = variable.rsplit("_", 1)[-1]
                ts_code = mapping.get(quote_code)
                fields = value.rstrip('";').split("~")
                if not ts_code or len(fields) < 36:
                    continue
                amount_parts = fields[35].split("/")
                amount = amount_parts[2] if len(amount_parts) >= 3 else 0
                stamp = fields[30]
                if len(stamp) == 14 and stamp.isdigit():
                    stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
                row = {
                    "ts_code": ts_code,
                    "name": fields[1],
                    "open": fields[5],
                    "pre_close": fields[4],
                    "close": fields[3],
                    "high": fields[33],
                    "low": fields[34],
                    # Tencent reports cumulative volume in lots; normalize to
                    # shares so _daily_snapshot_row performs the same shares ->
                    # lots conversion used for Sina and rt_etf_k.
                    "vol": float(fields[6] or 0) * 100.0,
                    "amount": amount,
                    "time": stamp,
                }
                if ts_code in etf_set:
                    etf_rows.append(row)
                elif ts_code in index_set:
                    index_rows.append(row)
            self._record_public_source(
                "signal.tencent", label="腾讯累计行情", impact="signal-fallback", endpoint=TENCENT_URL,
                ok=True, latency=round(time.monotonic() - started, 3),
                rows=len(etf_rows) + len(index_rows),
                coverage={"etf": len(etf_rows), "index": len(index_rows)},
            )
            return etf_rows, index_rows
        except Exception as exc:
            self._record_public_source(
                "signal.tencent", label="腾讯累计行情", impact="signal-fallback", endpoint=TENCENT_URL,
                ok=False, latency=round(time.monotonic() - started, 3), error=f"{type(exc).__name__}: {exc}",
            )
            raise

    @staticmethod
    def _require_snapshot_coverage(
        etf_rows: list[dict[str, Any]],
        index_rows: list[dict[str, Any]],
        etf_codes: list[str],
        index_codes: list[str],
        source: str,
    ) -> None:
        etf_covered = {str(row.get("ts_code") or "") for row in etf_rows} & set(etf_codes)
        index_covered = {str(row.get("ts_code") or "") for row in index_rows} & set(index_codes)
        minimum_etfs = max(1, int(len(set(etf_codes)) * 0.90))
        if len(etf_covered) < minimum_etfs or len(index_covered) != len(set(index_codes)):
            raise FeedError(
                f"{source} coverage insufficient: ETF {len(etf_covered)}/{len(set(etf_codes))}, "
                f"indexes {len(index_covered)}/{len(set(index_codes))}"
            )

    def signal_snapshot(self, etf_codes: list[str], index_codes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """Fetch one cumulative-day cross-section for the frozen daily strategy."""
        if self.mode in {"new", "datahub", "promax", "public"}:
            # rt_min is a single-minute bar, not the cumulative intraday OHLCV
            # required by the daily ranking/volume filters.  Use two independent
            # cumulative quote sources for the signal and reserve paid rt_min
            # for route preflight and actual execution-price bars.
            failures = []
            for source, fetcher in (
                ("sina_cumulative_primary", self.sina_signal_snapshot),
                ("tencent_cumulative_fallback", self.tencent_signal_snapshot),
            ):
                try:
                    etf_rows, index_rows = fetcher(etf_codes, index_codes)
                    try:
                        self._require_snapshot_coverage(etf_rows, index_rows, etf_codes, index_codes, source)
                    except FeedError as exc:
                        self._record_public_source(
                            "signal.sina" if source.startswith("sina") else "signal.tencent",
                            label="新浪累计行情" if source.startswith("sina") else "腾讯累计行情",
                            impact="signal-primary" if source.startswith("sina") else "signal-fallback",
                            endpoint=SINA_URL if source.startswith("sina") else TENCENT_URL,
                            ok=False,
                            rows=len(etf_rows) + len(index_rows),
                            coverage={"etf": len(etf_rows), "index": len(index_rows)},
                            error=str(exc),
                        )
                        raise
                    self.last_signal_source = source
                    return etf_rows, index_rows, source
                except Exception as exc:
                    failures.append(f"{source}: {type(exc).__name__}: {exc}")
            raise FeedError("; ".join(failures))
        wanted = set(etf_codes)
        try:
            etf_rows: list[dict[str, Any]] = []
            for params in (
                {"ts_code": "5*.SH", "topic": "HQ_FND_TICK"},
                {"ts_code": "1*.SZ"},
            ):
                rows = self.plan2("rt_etf_k", params, retry_delays=(0,))
                etf_rows.extend(row for row in rows if str(row.get("ts_code") or "") in wanted)
            index_rows = self.plan2(
                "rt_idx_min",
                {"ts_code": ",".join(index_codes), "freq": "1MIN"},
                retry_delays=(0,),
            )
            self.last_signal_source = "rt_etf_k+rt_idx_min"
            return etf_rows, index_rows, self.last_signal_source
        except FeedError:
            etf_rows, index_rows = self.sina_signal_snapshot(etf_codes, index_codes)
            self.last_signal_source = "sina_realtime_fallback"
            return etf_rows, index_rows, self.last_signal_source

    def probe_public_sources(
        self, etf_codes: list[str], index_codes: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Check both free cumulative quote routes without spending paid quota."""
        sample_etfs = list(dict.fromkeys(etf_codes))[:3]
        sample_indexes = list(dict.fromkeys(index_codes))[:1]
        results: dict[str, dict[str, Any]] = {}
        for key, label, impact, endpoint, fetcher in (
            ("signal.sina", "新浪累计行情", "signal-primary", SINA_URL, self.sina_signal_snapshot),
            ("signal.tencent", "腾讯累计行情", "signal-fallback", TENCENT_URL, self.tencent_signal_snapshot),
        ):
            started = time.monotonic()
            try:
                etf_rows, index_rows = fetcher(sample_etfs, sample_indexes)
                self._require_snapshot_coverage(
                    etf_rows, index_rows, sample_etfs, sample_indexes, label
                )
                results[key] = {
                    "status": "healthy",
                    "rows": len(etf_rows) + len(index_rows),
                    "coverage": {"etf": len(etf_rows), "index": len(index_rows)},
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "error": "",
                }
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._record_public_source(
                    key, label=label, impact=impact, endpoint=endpoint, ok=False,
                    latency=round(time.monotonic() - started, 3), error=error,
                )
                results[key] = {
                    "status": "degraded", "rows": 0, "coverage": {},
                    "latency_seconds": round(time.monotonic() - started, 3), "error": error,
                }
        return results

    def stock_minutes(
        self,
        ts_codes: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch stock minutes for an independent stock shadow caller.

        The caller is responsible for constraining the list (currently the
        Saiteng module uses ``603283.SH``).  Promax intentionally does not
        fall back to ETF or cumulative quote data when a stock row is stale or
        missing.
        """
        if not ts_codes:
            return []
        if self.mode == "public":
            if start_date is not None or end_date is not None:
                raise FeedError("public stock_minutes 仅取当前日，不能用于历史补齐")
            return self._public_minutes(ts_codes)
        if self.mode == "promax":
            budget = float(getattr(self, "execution_timeout", getattr(self, "timeout", 30.0)))
            started = time.monotonic()
            try:
                return self._promax_batched_minutes(
                    "rt_min", ts_codes, start_date=start_date, end_date=end_date,
                    timeout=budget / 2,
                )
            except FeedError as primary:
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise
                now = datetime.now(_PROMAX_TIMEZONE)
                lower = start_date or (now - timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S")
                upper = end_date or (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    return self._promax_batched_minutes(
                        "rt_min_daily", ts_codes, start_date=lower, end_date=upper,
                        timeout=remaining,
                    )
                except FeedError as fallback:
                    raise FeedError(f"stock minute routes failed: primary={primary}; fallback={fallback}") from None
        params: dict[str, Any] = {"ts_code": ",".join(ts_codes), "freq": "1MIN"}
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return self.plan2("rt_min", params)

    def stock_daily(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        trade_date: str | None = None,
        fields: str = "",
    ) -> list[dict[str, Any]]:
        code = _validate_ts_code(ts_code) if self.mode == "promax" else ts_code
        params: dict[str, Any] = {"ts_code": code}
        if trade_date is not None:
            params["trade_date"] = trade_date
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return self.plan2("daily", params, fields=fields)

    def stock_limits(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        trade_date: str | None = None,
        fields: str = "",
    ) -> list[dict[str, Any]]:
        code = _validate_ts_code(ts_code) if self.mode == "promax" else ts_code
        params: dict[str, Any] = {"ts_code": code}
        if trade_date is not None:
            params["trade_date"] = trade_date
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return self.plan2("stk_limit", params, fields=fields)

    # Keep a descriptive alias for stock shadow callers that use the full
    # price-limit name while retaining the compact public method above.
    stock_price_limits = stock_limits

    def fund_daily_cross_section(self, trade_date: str) -> list[dict[str, Any]]:
        if self.mode == "promax":
            # Datahub has a separate daily cross-section route.  Prefer it
            # when its optional credential is present, then keep Promax as an
            # independent fallback because the two providers have different
            # realtime/daily availability.
            if getattr(self, "plan1_token", ""):
                try:
                    rows = self.plan1("fund_daily", {"trade_date": trade_date})
                    if rows:
                        return rows
                    message = "Datahub fund_daily 返回空数据"
                    self.health.plan1_ok = False
                    self._record_route("plan1", "fund_daily", ok=False, rows=0, error=message)
                    self.health.last_error = message
                except FeedError:
                    pass
            return self.plan2("fund_daily", {"trade_date": trade_date})
        if self.mode == "new":
            # Replay requires one concrete ETF symbol and rejects the official
            # trade_date-only cross-section form.  Datahub supports the full
            # daily cross-section and is the correct primary for maintenance.
            return self.plan1("fund_daily", {"trade_date": trade_date})
        try:
            return self.plan2("fund_daily", {"trade_date": trade_date})
        except FeedError:
            return self.plan1("fund_daily", {"trade_date": trade_date})

    def index_daily(self, ts_code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        params = {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
        if self.mode == "promax":
            return self.plan2("index_daily", params)
        try:
            return self.plan2("index_daily", params)
        except FeedError:
            return self.plan1("index_daily", params)

    def auction(self, ts_code: str, trade_date: str) -> list[dict[str, Any]]:
        if self.mode == "public":
            return []  # Optional diagnostic disabled; no paid route in this mode.
        if self.mode == "promax":
            try:
                return self.plan2("stk_auction", {"ts_code": ts_code, "trade_date": trade_date})
            except FeedError:
                return []
        if self.mode == "datahub":
            # Auction capture is diagnostic only for the frozen strategy.  Do
            # not hammer the legacy fallback when the 15000 account does not
            # expose this optional endpoint.
            try:
                return self.plan2("stk_auction", {"ts_code": ts_code, "trade_date": trade_date})
            except FeedError:
                return []
        try:
            return self.plan2("stk_auction", {"ts_code": ts_code, "trade_date": trade_date})
        except FeedError:
            return self.plan1("stk_auction", {"ts_code": ts_code, "trade_date": trade_date})
