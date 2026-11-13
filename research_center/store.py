"""Durable research records; entirely separate from shadow accounts."""
from __future__ import annotations
import csv
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYTIC_EXPORTS = frozenset({'daily_returns', 'monthly_returns', 'yearly_returns', 'closed_trades'})
DEFAULT_STORE = ROOT / 'results' / 'research_center'


class RunStore:
    def __init__(self, root=DEFAULT_STORE):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, record TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.root / 'runs.sqlite3', timeout=15)

    def directory(self, run_id):
        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ValueError('invalid run_id')
        return self.root / run_id

    def create(self, config):
        run_id = uuid.uuid4().hex
        self.directory(run_id).mkdir()
        record = dict(run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(),
                      status='queued', progress=0, message='等待研究任务', config=config,
                      strategy=config.get('strategy'), symbol=config.get('symbol'),
                      backend=config.get('backend', 'reference'),
                      strategy_version=config.get('strategy_version'),
                      experiment_id=config.get('experiment_id'), parent_run_id=config.get('parent_run_id'),
                      label=config.get('label', ''),
                      start=config.get('start'), end=config.get('end'),
                      initial_cash=config.get('initial_cash'), metrics={},
                      mode='RESEARCH_ONLY', paper_admission=False,
                      report_url='http://127.0.0.1:8060/research#' + run_id)
        with self.connect() as con:
            con.execute('INSERT INTO runs VALUES (?, ?)', (run_id, json.dumps(record, ensure_ascii=False, allow_nan=False)))
        return record

    def get(self, run_id):
        self.directory(run_id)
        with self.connect() as con:
            row = con.execute('SELECT record FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError('run not found')
        return json.loads(row[0])

    def update(self, run_id, **values):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT record FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError('run not found')
            record = {**json.loads(row[0]), **values}
            con.execute('UPDATE runs SET record=? WHERE run_id=?',
                        (json.dumps(record, ensure_ascii=False, allow_nan=False), run_id))
        return record

    def transition(self, run_id, allowed_statuses, **values):
        """Compare and set inside one transaction; terminal states stay terminal."""
        self.directory(run_id)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT record FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError('run not found')
            record = json.loads(row[0])
            if record['status'] not in allowed_statuses:
                return record, False
            record.update(values)
            con.execute('UPDATE runs SET record=? WHERE run_id=?',
                        (json.dumps(record, ensure_ascii=False, allow_nan=False), run_id))
        return record, True

    def list(self):
        with self.connect() as con:
            return [json.loads(r[0]) for r in con.execute('SELECT record FROM runs ORDER BY rowid DESC LIMIT 500')]

    def detail(self, run_id):
        record = self.get(run_id)
        directory = self.directory(run_id)
        path = directory / 'result.json'
        unavailable = (record['status'] != 'completed'
                       or record.get('artifact_commit_failed') is True
                       or (directory / 'artifact-commit-failed').exists())
        result = json.loads(path.read_text()) if not unavailable and path.exists() else {}
        # Legacy reports are enriched in memory only. Never rewrite their result,
        # configuration, original metrics or provenance on a read request.
        if record['status'] == 'completed' and result:
            for key, module, function in (
                ('analytics', 'analytics', 'build_analytics'),
                ('data_readiness', 'data_evidence', 'assess_data'),
            ):
                if key not in result:
                    try:
                        from importlib import import_module
                        result[key] = getattr(import_module('.' + module, __package__), function)(record['config'], result)
                        result[key]['origin'] = 'derived_read_only'
                    except Exception as exc:
                        result[key] = {'status': 'unavailable', 'reason': str(exc),
                                       'origin': 'derived_read_only', 'paper_admission': False}
        log = directory / 'execution.log'
        return {**result, 'record': record, 'logs': log.read_text(errors='replace')[-100000:] if log.exists() else ''}

    def save_result(self, run_id, result):
        directory = self.directory(run_id)
        temporary = directory / 'result.json.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        temporary.replace(directory / 'result.json')
        for name in ('equity', 'orders', 'trades', 'positions', *sorted(ANALYTIC_EXPORTS)):
            rows = result.get('analytics', {}).get(name, []) if name in ANALYTIC_EXPORTS else result.get(name, [])
            if rows:
                fields = list(dict.fromkeys(key for row in rows for key in row))
                with (directory / (name + '.csv')).open('w', newline='', encoding='utf-8-sig') as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)

    def csv_bytes(self, run_id, name):
        record = self.get(run_id)
        if (record['status'] != 'completed'
                or record.get('artifact_commit_failed') is True
                or (self.directory(run_id) / 'artifact-commit-failed').exists()):
            raise ValueError('本次任务尚无已完成的报告，不能导出 CSV')
        if name not in {'trades', 'orders', 'equity', 'positions'} | ANALYTIC_EXPORTS:
            raise ValueError('unsupported export')
        path = self.directory(run_id) / (name + '.csv')
        if path.exists():
            return path.read_bytes()
        if name in ANALYTIC_EXPORTS:
            rows = self.detail(run_id).get('analytics', {}).get(name, [])
            if rows:
                output = io.StringIO(newline='')
                writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
                writer.writeheader()
                writer.writerows(rows)
                return output.getvalue().encode('utf-8-sig')
        return b''
