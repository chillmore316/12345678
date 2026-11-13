"""One bounded, cancellable subprocess per research run. No imported user code."""
from __future__ import annotations
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager

from .store import RunStore, ROOT, ANALYTIC_EXPORTS

ACTIVE = frozenset({'queued', 'running', 'cancelling'})


@contextmanager
def terminal_lock(store, run_id):
    """Serialize accepted cancellation with the final artifact commit."""
    with (store.directory(run_id) / 'terminal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def finish(store, run_id, status, **values):
    """Cancellation wins when acknowledged before the terminal commit."""
    record, changed = store.transition(run_id, {'running'}, status=status, **values)
    if not changed and record['status'] == 'cancelling':
        record, _ = store.transition(run_id, {'cancelling'}, status='cancelled', message='任务已取消')
    return record


def refresh_status(store, record):
    if record['status'] not in {'queued', 'running', 'cancelling'} or not record.get('pid'):
        return record
    pid = record['pid']
    try:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited:
                raise ProcessLookupError()
        except ChildProcessError:
            pass
        os.kill(pid, 0)
        process = subprocess.run(
            ['/bin/ps', '-ww', '-o', 'stat=', '-o', 'command=', '-p', str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        fields = process.stdout.strip().split(maxsplit=1)
        if process.returncode or len(fields) != 2:
            return record
        if fields[0].startswith('Z'):
            raise ProcessLookupError()
        expected = f" -m research_center.cli --store {store.root} _worker {record['run_id']}"
        if not fields[1].endswith(expected):
            raise ProcessLookupError()
    except ProcessLookupError:
        # Re-read: the worker may have committed completion just before exit.
        record, changed = store.transition(record['run_id'], {'cancelling'}, status='cancelled', message='任务已取消')
        if not changed:
            record, _ = store.transition(record['run_id'], {'queued', 'running'}, status='interrupted', message='此研究目录无对应工作进程；保留未完成记录')
        return record
    except (OSError, subprocess.SubprocessError):
        return record
    return record


def submit(store, config):
    from .registry import normalize_config, configured_strategy_version, BACKENDS
    normalized = normalize_config(config, store)
    # Serial admission across HTTP and CLI processes, separate from the ledger.
    with (store.root / 'admission.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for record in store.list():
            record = refresh_status(store, record)
            if record['status'] in {'queued', 'running', 'cancelling'}:
                pid = record.get('pid')
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except ProcessLookupError:
                        pass
                if alive:
                    raise ValueError('已有研究任务运行，请等待或取消后再提交')
                store.transition(record['run_id'], ACTIVE, status='interrupted', message='worker已退出；保留未完成记录')
        record = store.create(normalized)
        directory = store.directory(record['run_id'])
        worker = None
        try:
            # Snapshot before execution; a later code edit cannot change an old report.
            snapshots = {}
            for path in [*Path(__file__).parent.glob('*.py'), ROOT / 'data/joinquant_minute.py', ROOT / 'strategy/base.py']:
                data = path.read_bytes()
                name = str(path.relative_to(ROOT)).replace('/', '__')
                (directory / name).write_bytes(data)
                snapshots[str(path.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
            if configured_strategy_version(normalized, store) != normalized['strategy_version']:
                raise ValueError('策略在创建快照期间发生变化，请重新提交')
            if normalized['strategy'] == 'custom_python':
                from .custom_source_store import CustomSourceStore
                archive = CustomSourceStore(store.root / 'custom_sources')
                source = archive.read_source(normalized['source_id'])
                for filename, content in (
                    ('custom_strategy.py', source),
                    ('custom_source.json', json.dumps(archive.show(normalized['source_id']),
                                                     ensure_ascii=False, indent=2).encode('utf-8')),
                ):
                    fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(content)
            dependencies = ['pandas', 'numpy', 'pyarrow']
            if normalized['backend'] == 'vnpy':
                dependencies += ['vnpy', 'vnpy_portfoliostrategy']
            provenance = {'source_hashes': snapshots, 'python': sys.version,
                          'dependencies': {p: importlib.metadata.version(p) for p in dependencies},
                          'engine': BACKENDS[normalized['backend']]['engine'],
                          'backend': normalized['backend'], 'strategy_version': normalized['strategy_version'],
                          'mode': 'RESEARCH_ONLY'}
            (directory / 'provenance.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2))
            store.update(record['run_id'], provenance=provenance)
            with (directory / 'execution.log').open('ab') as output:
                worker = subprocess.Popen([sys.executable, '-m', 'research_center.cli', '--store', str(store.root),
                                           '_worker', record['run_id']], cwd=ROOT, stdin=subprocess.DEVNULL,
                                          stdout=output, stderr=output, start_new_session=True,
                                          env={**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
                                               'MKL_NUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'})
            return store.update(record['run_id'], pid=worker.pid)
        except Exception as exc:
            if worker is None:
                with (directory / 'execution.log').open('a') as output:
                    traceback.print_exc(file=output)
                store.transition(record['run_id'], {'queued'}, status='failed', error=str(exc),
                                 message='研究任务准备失败，日志已保存')
            raise


def cancel(store, run_id):
    with terminal_lock(store, run_id):
        record, changed = store.transition(run_id, ACTIVE, status='cancelling', message='已请求取消，等待worker安全退出')
        if changed:
            (store.directory(run_id) / 'cancel').touch()
    return record


def execute(store, run_id):
    from .registry import run_backtest
    directory = store.directory(run_id)
    record, claimed = store.transition(run_id, {'queued'}, status='running', message='读取本地分钟数据')
    if not claimed:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (180, 185))
    # macOS virtual-address limits interfere with Arrow's mmap; bound selection,
    # worker count and wall time instead, plus a maximum output/input row contract.
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('研究任务超过240秒')))
    signal.alarm(240)
    last = [0.0]
    def progress(percent, message):
        now = time.monotonic()
        if now - last[0] > .5 or percent in (0, 100):
            store.update(run_id, progress=percent, message=message)
            print(f'{percent}% {message}', flush=True)
            last[0] = now
    try:
        provenance = json.loads((directory / 'provenance.json').read_text())
        for relative, expected in provenance['source_hashes'].items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
                raise ValueError('任务源码在提交后发生变化，请创建新实验')
        from .custom_source_store import CustomSourceStore
        result = run_backtest(store.get(run_id)['config'], progress=progress,
                              cancelled=lambda: (directory / 'cancel').exists(),
                              source_store=CustomSourceStore(store.root / 'custom_sources'))
        if (directory / 'cancel').exists():
            finish(store, run_id, 'cancelled', message='任务已取消')
            return
        from .analytics import build_analytics
        from .data_evidence import assess_data
        config = record['config']
        result['analytics'] = build_analytics(config, result)
        result['data_readiness'] = assess_data(config, result)
        result['analytics']['origin'] = 'saved_with_run'
        result['data_readiness']['origin'] = 'saved_with_run'
        with terminal_lock(store, run_id):
            # Once commit owns this lock, cancellation cannot be acknowledged
            # until the terminal status is published. A prior accepted cancel
            # wins and leaves no completed-report artifacts.
            latest = store.get(run_id)
            if latest['status'] == 'cancelling' or (directory / 'cancel').exists():
                finish(store, run_id, 'cancelled', message='任务已取消')
                return
            if latest['status'] != 'running':
                return
            artifacts = {'result.json', 'result.json.tmp'} | {
                name + '.csv' for name in {'equity', 'orders', 'trades', 'positions'} | ANALYTIC_EXPORTS}
            existing = {name for name in artifacts if (directory / name).exists()}
            try:
                if existing:
                    raise ValueError('结果目录已有产物，拒绝覆盖')
                store.save_result(run_id, result)
                finish(store, run_id, 'completed', progress=100, message='研究回放完成，未获模拟盘准入',
                             metrics=result['metrics'], data_manifest=result.get('data_manifest'),
                             limitations=result.get('limitations', []))
            except BaseException as exc:
                # Decide failure while still holding the cancellation lock.
                # Readers gate on status; this marker also fails closed if a
                # database failure prevents the terminal transition itself.
                if store.get(run_id)['status'] != 'completed':
                    (directory / 'artifact-commit-failed').touch()
                    for name in artifacts - existing:
                        try:
                            (directory / name).unlink(missing_ok=True)
                        except OSError:
                            pass  # the failure marker keeps remnants private
                    finish(store, run_id, 'failed', error=str(exc), artifact_commit_failed=True,
                           message='报告保存失败，未发布本次结果')
                raise
    except BaseException as exc:
        traceback.print_exc()
        cancelled = (directory / 'cancel').exists()
        finish(store, run_id, 'cancelled' if cancelled else 'failed', error=str(exc),
                     message='任务已取消' if cancelled else '执行失败，日志已保存')
    finally:
        signal.alarm(0)
