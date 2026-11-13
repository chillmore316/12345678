"""Cheap local workspace checks; no feed, raw rows, network or accounts."""
from __future__ import annotations
import importlib.metadata
import os
from pathlib import Path
import re
import sys
from .store import ROOT

DATA_ROOT = ROOT / '闲鱼商品_A股1分钟数据_聚宽版' / '01_数据文件'
_FILE = re.compile(r'^stock_1m_(202[45]-\d{2})_batch\d+.*\.parquet$')
SERVICE_ID = 'quant-local-research-v1'


def data_catalog(store):
    months = {}
    examined = 0
    truncated = False
    if DATA_ROOT.is_dir():
        # Filename inventory only; no Parquet footer or held-out rows opened.
        for year in ('2024', '2025'):
            for path in DATA_ROOT.glob('stock_1m_' + year + '-*.parquet'):
                examined += 1
                if examined > 10000:
                    truncated = True
                    break
                match = _FILE.fullmatch(path.name)
                if match:
                    key = match.group(1)
                    months[key] = months.get(key, 0) + 1
            if truncated:
                break
    examples = []
    seen = set()
    for record in store.list():
        if record.get('status') != 'completed':
            continue
        key = (record.get('symbol'), record.get('start'), record.get('end'))
        if key in seen or not key[1] or not key[2] or key[1] < '2024-01-01' or key[2] > '2025-12-31':
            continue
        seen.add(key)
        examples.append({'symbol': key[0], 'start': key[1], 'end': key[2],
                         'source_run_id': record['run_id'], 'basis': 'previous_completed_replay'})
        if len(examples) == 20:
            break
    return {'schema_version': 1, 'dataset': '本地聚宽分钟历史文件',
            'exists': DATA_ROOT.is_dir(), 'months': [{'month': key, 'file_count': months[key]} for key in sorted(months)],
            'inventory_truncated': truncated, 'inspection': 'filenames_only_no_data_read',
            'examples': examples, 'mode': 'RESEARCH_ONLY', 'paper_admission': False,
            'coverage_verified': False, 'price_basis_verified': False,
            'message': '文件存在不代表标的覆盖或价格认证；实际提交仍执行有界读取和数据核查。'}


def workspace_health(store):
    dependencies = []
    for name in ('pandas', 'numpy', 'pyarrow'):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        dependencies.append({'name': name, 'version': version, 'available': version is not None})
    writable = store.root.is_dir() and os.access(store.root, os.W_OK)
    data_present = DATA_ROOT.is_dir()
    ok = all(row['available'] for row in dependencies) and writable and data_present
    return {'schema_version': 1, 'service': SERVICE_ID, 'status': 'ready' if ok else 'needs_attention',
            'python': '.'.join(str(part) for part in sys.version_info[:3]),
            'dependencies': dependencies, 'result_store_writable': writable,
            'minute_directory_present': data_present,
            'mode': 'RESEARCH_ONLY', 'paper_admission': False,
            'message': '本地软件检查通过；数据认证另看每次报告。' if ok else '请检查项目 Python 环境、本地分钟目录及结果目录权限。'}
