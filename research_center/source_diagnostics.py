"""Explicit, bounded source samples. Never imported by an account executor.

No credentials, URLs, raw payloads, provider errors or prices are persisted.
The existing provider supplies route constants, authentication and row parsing;
its runtime constructor is deliberately not used: it probes optional credentials
and enables retries/fallbacks that are inappropriate for this diagnostic budget.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import uuid
from zoneinfo import ZoneInfo

from .intake import _snapshot_file, _parse_json

SH = ZoneInfo('Asia/Shanghai')
MAX_BODY = 262144
MAX_RECEIPT = 32768
SYMBOL = re.compile(r'^(?:5\d{5}\.SH|1\d{5}\.SZ)$')
ID = re.compile(r'^[a-f0-9]{32}$')
PROVIDERS = ('promax', 'datahub')
OUTCOMES = {'sample_received', 'empty', 'http_denied', 'http_error',
            'transport_error', 'timeout', 'invalid_response', 'invalid_sample', 'credential_unavailable'}
TEMPORAL = {'no_valid_sample', 'daily_history_sample', 'weekend_sample',
            'outside_regular_hours_sample', 'calendar_unverified_recent_sample', 'stale_sample', 'future_stamp'}


def _finite(value):
    try:
        return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:
        return False


@contextmanager
def deadline(seconds):
    """CLI main-thread wall limit including DNS, streaming and Keychain waits."""
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError('接源检查只允许在独立CLI主线程运行')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(*_):
        raise TimeoutError('source diagnostic deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _blank(kind, api):
    return dict(kind=kind, api=api, outcome='invalid_response', http_status=None,
                response_bytes=0, rows=0, latest_time=None, age_seconds=None,
                temporal_state='no_valid_sample', vendor_stale=None)


def _temporal(stamp, now, stale):
    age = (now-stamp).total_seconds()
    if age < 0:
        return 'future_stamp', age
    if stale is True or age > 300:
        return 'stale_sample', age
    if now.weekday() >= 5:
        return 'weekend_sample', age
    minute = now.hour*60+now.minute
    if not (570 <= minute < 690 or 780 <= minute < 900):
        return 'outside_regular_hours_sample', age
    # A weekday is not proof of an exchange trading day. No calendar inferred.
    return 'calendar_unverified_recent_sample', age


def inspect_payload(payload, *, kind, symbol, now, start, end):
    from live_paper_compare.provider import _rows, _parse_promax_time
    rows = _rows(payload)
    maximum = 7 if kind == 'daily' else 15
    if len(rows) > maximum:
        raise ValueError('row limit')
    stamps = []
    for row in rows:
        if not isinstance(row, dict) or row.get('ts_code') != symbol:
            raise ValueError('symbol mismatch')
        numbers = {}
        for key in ('open', 'high', 'low', 'close', 'vol', 'amount'):
            value = row.get(key)
            if not _finite(value):
                raise ValueError('invalid numeric field')
            numbers[key] = value
        if min(numbers[k] for k in ('open', 'high', 'low', 'close')) <= 0 or min(numbers['vol'], numbers['amount']) < 0:
            raise ValueError('invalid value')
        if numbers['vol'] == 0 and numbers['amount'] != 0:
            raise ValueError('zero volume with amount')
        if numbers['low'] > min(numbers['open'], numbers['close']) or numbers['high'] < max(numbers['open'], numbers['close']) or numbers['low'] > numbers['high']:
            raise ValueError('OHLC relationship')
        if kind == 'daily':
            value = row.get('trade_date')
            if not isinstance(value, str) or not re.fullmatch(r'\d{8}', value):
                raise ValueError('date')
            stamp = datetime.strptime(value, '%Y%m%d').replace(tzinfo=SH)
            if not start <= stamp.date() <= end:
                raise ValueError('outside requested dates')
        else:
            value = row.get('trade_time')
            if not isinstance(value, str) or not re.fullmatch(r'(?:\d{12}(?:\d{2})?|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)', value):
                raise ValueError('timestamp')
            stamp = _parse_promax_time(value)
        stamps.append(stamp)
    if len(stamps) != len(set(stamps)):
        raise ValueError('duplicate timestamp')
    meta = payload.get('meta') if isinstance(payload, dict) else None
    stale = meta.get('stale') if isinstance(meta, dict) else None
    if isinstance(stale, str) and stale.strip().lower() in {'true','1','yes','false','0','no'}:
        stale = stale.strip().lower() in {'true','1','yes'}
    elif type(stale) is int and stale in (0,1):
        stale = bool(stale)
    stale = stale if isinstance(stale, bool) else None
    if not stamps:
        return dict(rows=0, latest_time=None, age_seconds=None, temporal_state='no_valid_sample', vendor_stale=stale)
    latest = max(stamps)
    temporal, age = ('daily_history_sample', None) if kind == 'daily' else _temporal(latest, now, stale)
    return dict(rows=len(rows), latest_time=latest.isoformat(), age_seconds=age,
                temporal_state=temporal, vendor_stale=stale)


def _request(session, url, key, params):
    """One request, no redirects/retries, bounded decompressed response."""
    with deadline(10):
        with session.get(url, headers={'X-API-Key':key, 'Accept':'application/json'},
                         params=params, proxies=None, timeout=(3, 5),
                         allow_redirects=False, stream=True) as response:
            status = int(response.status_code)
            if status != 200:
                return status, 0, None  # Never read/store an error body or Location.
            body = bytearray()
            for block in response.iter_content(8192):
                body.extend(block)
                if len(body) > MAX_BODY:
                    raise ValueError('response exceeds bound')
            return status, len(body), _parse_json(bytes(body), 'source payload')


def run_check(provider, symbol, *, now=None, secret_reader=None, session_factory=None):
    if provider not in PROVIDERS or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError('请选择支持的数据源和单只基金代码')
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError('接源检查只允许在独立CLI主线程运行')
    now = now or datetime.now(SH)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('检查时间必须带时区')
    now = now.astimezone(SH)
    from live_paper_compare.provider import PROMAX_URL, PROMAX_SERVICE, NEW_PLAN1_URL, NEW_PLAN1_SERVICE, keychain_secret
    import requests
    base, service = (PROMAX_URL, PROMAX_SERVICE) if provider == 'promax' else (NEW_PLAN1_URL.replace('http://','https://',1), NEW_PLAN1_SERVICE)
    apis = ('fund_daily','rt_etf_min') if provider == 'promax' else ('fund-daily','rt-min')
    key = None
    try:
        with deadline(5):
            key = (secret_reader or keychain_secret)(service)
        if not isinstance(key, str) or not key.strip():
            key = None
    except Exception:
        key = None
    end = now.date()
    start = end-timedelta(days=6)
    checks, attempts = [], 0
    # Session creation does not trigger a provider constructor, credential scan,
    # proxy selection or account access. No fallback is attempted.
    with (session_factory or requests.Session)() as session:
        session.trust_env = False
        for kind, api in zip(('daily','minute'), apis):
            item = _blank(kind, api)
            if key is None:
                item['outcome'] = 'credential_unavailable'
                checks.append(item)
                continue
            params = {'ts_code':symbol, 'limit':7 if kind == 'daily' else 15}
            if kind == 'daily':
                params.update(start_date=start.strftime('%Y%m%d'), end_date=end.strftime('%Y%m%d'),
                              fields='ts_code,trade_date,open,high,low,close,pre_close,vol,amount')
            else:
                params.update(freq='1min', __probe=0)
            try:
                attempts += 1
                status, size, payload = _request(session, base.rstrip('/')+'/'+api, key, params)
                item.update(http_status=status, response_bytes=size)
                if status != 200:
                    item['outcome'] = 'http_denied' if status in (401,403,407) else 'http_error'
                else:
                    item.update(inspect_payload(payload, kind=kind, symbol=symbol, now=now, start=start, end=end))
                    item['outcome'] = 'sample_received' if item['rows'] else 'empty'
            except (TimeoutError, requests.Timeout):
                item['outcome'] = 'timeout'
            except requests.RequestException:
                item['outcome'] = 'transport_error'
            except Exception:
                item['outcome'] = 'invalid_sample'
            checks.append(item)
    return dict(schema_version=1, provider=provider, symbol=symbol, observed_at=now.isoformat(),
                daily_start=start.isoformat(), daily_end=end.isoformat(), checks=checks,
                network_attempts=attempts, retry_count=0, raw_payload_saved=False,
                mode='RESEARCH_ONLY', source_certification=False, timestamp_semantics_verified=False,
                coverage_complete=False, paper_admission=False, runtime_changed=False)


class SourceChecks:
    """Immutable, bounded diagnostic receipts, independent of runs/accounts."""
    def __init__(self, root):
        self.root = Path(root).resolve()/'source_checks'

    def _directory(self, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError('接源检查目录不可用')
        return True

    @staticmethod
    def _encode(record):
        return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',',':')).encode()

    def save(self, record):
        # Only the generated allowlisted projection crosses this boundary.
        self._safe_validate(record)
        data = self._encode(record)
        if len(data) > MAX_RECEIPT:
            raise ValueError('receipt too large')
        ident = hashlib.sha256(data).hexdigest()[:32]
        self._directory(create=True)
        temporary = self.root/('.pending-'+uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, self.root/(ident+'.json'))
            except FileExistsError:
                if _snapshot_file(self.root/(ident+'.json'), MAX_RECEIPT, 'receipt', None) != data:
                    raise ValueError('receipt collision') from None
            fd=os.open(self.root, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            if temporary.exists(): temporary.unlink()
        return {'check_id':ident, **record}

    def detail(self, ident):
        if not isinstance(ident,str) or not ID.fullmatch(ident):
            raise ValueError('接源检查编号无效')
        if not self._directory():
            raise FileNotFoundError('source check not found')
        data = _snapshot_file(self.root/(ident+'.json'), MAX_RECEIPT, 'receipt', None)
        if hashlib.sha256(data).hexdigest()[:32] != ident:
            raise ValueError('接源检查快照校验失败')
        record = _parse_json(data, 'receipt')
        self._safe_validate(record)
        return {'check_id':ident, **record}

    @classmethod
    def _safe_validate(cls, record):
        try:
            cls._validate(record)
        except (ValueError,TypeError,KeyError,OverflowError):
            # datetime and other parsers may otherwise echo private field text.
            raise ValueError('接源检查收据字段无效') from None

    def list(self):
        if not self._directory():
            return {'records':[], 'truncated':False}
        names, truncated = [], False
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                if re.fullmatch(r'[a-f0-9]{32}\.json', entry.name):
                    names.append(entry.name[:-5])
                if count >= 1000 or len(names) > 100:
                    truncated = True
                    break
        records = []
        for ident in names[:100]:
            summary = dict(check_id=ident, provider=None, symbol=None,
                           observed_at=None, status='CHECK_REQUIRED',
                           mode='RESEARCH_ONLY', paper_admission=False)
            try:
                record = self.detail(ident)
                summary.update(provider=record['provider'], symbol=record['symbol'],
                               observed_at=record['observed_at'], status='SAVED_SAMPLE')
            except (OSError,ValueError,TypeError,KeyError):
                pass  # Preserve a visible invalid entry, never pretend it is absent.
            records.append(summary)
        records.sort(key=lambda row:(row['observed_at'] or '',row['check_id']),reverse=True)
        return {'records':records,'truncated':truncated}

    @staticmethod
    def _validate(record):
        fields={'schema_version','provider','symbol','observed_at','daily_start','daily_end','checks',
                'network_attempts','retry_count','raw_payload_saved','mode','source_certification',
                'timestamp_semantics_verified','coverage_complete','paper_admission','runtime_changed'}
        if not isinstance(record,dict) or set(record)!=fields or type(record['schema_version']) is not int or record['schema_version']!=1:
            raise ValueError('receipt schema')
        if record['provider'] not in PROVIDERS or not isinstance(record['symbol'],str) or not SYMBOL.fullmatch(record['symbol']):
            raise ValueError('receipt identity')
        if record['mode']!='RESEARCH_ONLY' or record['retry_count']!=0 or type(record['retry_count']) is not int:
            raise ValueError('receipt mode')
        for key in ('raw_payload_saved','source_certification','timestamp_semantics_verified','coverage_complete','paper_admission','runtime_changed'):
            if record[key] is not False: raise ValueError('receipt admission')
        now=datetime.fromisoformat(record['observed_at'])
        if now.tzinfo is None or record['daily_end']!=now.astimezone(SH).date().isoformat() or record['daily_start']!=(now.astimezone(SH).date()-timedelta(days=6)).isoformat():
            raise ValueError('receipt dates')
        if type(record['network_attempts']) is not int or not 0<=record['network_attempts']<=2 or not isinstance(record['checks'],list) or len(record['checks'])!=2:
            raise ValueError('receipt bounds')
        for index,item in enumerate(record['checks']):
            if not isinstance(item,dict) or set(item)!=set(_blank('','')):
                raise ValueError('check schema')
            apis=('fund_daily','rt_etf_min') if record['provider']=='promax' else ('fund-daily','rt-min')
            if item['kind']!=('daily','minute')[index] or item['api']!=apis[index] or item['outcome'] not in OUTCOMES or item['temporal_state'] not in TEMPORAL:
                raise ValueError('check category')
            for key,maximum in (('rows',7 if index==0 else 15),('response_bytes',MAX_BODY)):
                if type(item[key]) is not int or not 0<=item[key]<=maximum: raise ValueError('check size')
            if item['http_status'] is not None and (type(item['http_status']) is not int or not 100<=item['http_status']<=599): raise ValueError('HTTP status')
            if item['vendor_stale'] is not None and type(item['vendor_stale']) is not bool: raise ValueError('stale metadata')
            if item['age_seconds'] is not None and not _finite(item['age_seconds']): raise ValueError('age')
            if item['latest_time'] is not None:
                stamp=datetime.fromisoformat(item['latest_time'])
                if stamp.tzinfo is None: raise ValueError('sample timestamp')
            if item['rows']:
                if item['outcome']!='sample_received' or item['http_status']!=200 or item['latest_time'] is None:
                    raise ValueError('sample result inconsistent')
                if index == 0:
                    local=stamp.astimezone(SH)
                    if not record['daily_start']<=local.date().isoformat()<=record['daily_end'] or local.time()!=datetime.min.time() or item['temporal_state']!='daily_history_sample' or item['age_seconds'] is not None:
                        raise ValueError('daily time inconsistent')
                else:
                    expected,age=_temporal(stamp,now.astimezone(SH),item['vendor_stale'])
                    if item['temporal_state']!=expected or item['age_seconds'] is None or abs(item['age_seconds']-age)>1e-6:
                        raise ValueError('minute time inconsistent')
            elif item['outcome']=='sample_received' or item['latest_time'] is not None or item['age_seconds'] is not None or item['temporal_state']!='no_valid_sample':
                raise ValueError('empty sample inconsistent')
