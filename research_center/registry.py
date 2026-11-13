"""Explicit trusted strategy/backend catalogue shared by CLI, worker and UI."""
from __future__ import annotations
import hashlib
import importlib.metadata
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = {
    'reference': {'name': '保守分钟回放', 'engine': 'bounded-minute-research-v1'},
    'vnpy': {'name': 'VeighNa 原生基准', 'engine': 'vnpy-portfolio-minute-bridge-v1'},
    'isolated_python': {'name': '隔离 Python 回放', 'engine': 'research_center.custom_execution.v2'},
}
STRATEGIES = {
    'buy_hold': {'name': '买入并持有', 'backends': ['reference', 'vnpy'],
                 'description': '共享 Python 事件策略核心，单只股票买入持有；不同后端的成交规则分别记录。'},
    'sma_cross': {'name': '双均线交叉', 'backends': ['reference'],
                  'description': '分钟均线、T+1 可卖持仓；暂只支持保守回放，未完成原生引擎适配。'},
}


def strategy_version(strategy_id):
    if strategy_id not in STRATEGIES:
        raise ValueError('未登记的策略')
    digest = hashlib.sha256(strategy_id.encode())
    for relative in ('research_center/strategies.py', 'strategy/base.py'):
        digest.update(relative.encode())
        digest.update((ROOT / relative).read_bytes())
    return 'core-' + digest.hexdigest()[:16]


def backend_available(backend):
    if backend == 'isolated_python':
        import platform
        return platform.system() == 'Darwin' and Path('/opt/homebrew/bin/limactl').is_file()
    if backend == 'reference':
        return True
    try:
        return (ROOT / 'research_center/vnpy_bridge.py').is_file() and all(
            importlib.metadata.version(name) == version
            for name, version in [('vnpy', '4.4.0'), ('vnpy_portfoliostrategy', '1.3.0')]
        )
    except importlib.metadata.PackageNotFoundError:
        return False


def catalog():
    return {'schema_version': 1, 'mode': 'RESEARCH_ONLY', 'paper_admission': False,
            'backends': [{**spec, 'id': key, 'available': backend_available(key)}
                         for key, spec in BACKENDS.items()],
            'strategies': [{**spec, 'id': key, 'version': strategy_version(key),
                            'backends': [name for name in spec['backends'] if backend_available(name)]}
                           for key, spec in STRATEGIES.items()],
            'limits': {'start': '2024-01-01', 'end': '2025-12-31', 'max_calendar_days': 31,
                       'symbols': 1, 'interval': '1m', 'upload_enabled': True},
            'custom_python': {'url': '/python-strategy', 'interface': 'strategy-base-json-state-v1',
                              'runtime_checked_on_run': True, 'paper_admission': False}}


def configured_strategy_version(config, store):
    if config.get('strategy') == 'custom_python':
        from .custom_engine import strategy_version as custom_version
        from .custom_source_store import CustomSourceStore
        return custom_version(config, CustomSourceStore(store.root / 'custom_sources'))
    return strategy_version(config['strategy'])


def normalize_config(config, store):
    from .engine import validate_config
    if config.get('strategy') == 'custom_python':
        from .custom_engine import normalize_config as normalize_custom
        from .custom_source_store import CustomSourceStore
        normalized = normalize_custom(config, CustomSourceStore(store.root / 'custom_sources'))
        backend = 'isolated_python'
        version = normalized['strategy_version']
    else:
        normalized = validate_config(config)
        backend = config.get('backend', 'reference')
        if backend not in BACKENDS:
            raise ValueError('backend 只能是 reference 或 vnpy')
        if backend not in STRATEGIES[normalized['strategy']]['backends']:
            raise ValueError('VeighNa 原生基准目前只支持买入并持有；请选择保守分钟回放运行双均线')
        if not backend_available(backend):
            raise ValueError('所选回测后端不可用，请使用项目 .venv311 环境或保守分钟回放')
        version = strategy_version(normalized['strategy'])
    if config.get('strategy_version') not in (None, '', version):
        raise ValueError('请求的策略版本与当前源码不符，请创建当前版本的新实验')
    parent_id = config.get('parent_run_id') or None
    experiment_id = config.get('experiment_id') or None
    if experiment_id is not None and (not isinstance(experiment_id, str) or
                                      not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', experiment_id)):
        raise ValueError('experiment_id 仅允许 1–64 位字母、数字、下划线和短横线')
    if parent_id:
        parent = store.get(parent_id)
        if parent['status'] in {'queued', 'running', 'cancelling'}:
            raise ValueError('父实验尚未结束')
        if parent.get('strategy') != normalized['strategy']:
            raise ValueError('父实验须使用同一策略；切换策略请另建实验系列')
        inherited_id = parent.get('experiment_id') or parent['run_id']
        if experiment_id and experiment_id != inherited_id:
            raise ValueError('experiment_id 与父实验所属系列不一致')
        experiment_id = inherited_id
    label = config.get('label', '')
    if not isinstance(label, str) or len(label) > 80:
        raise ValueError('实验名称最长 80 个字符')
    normalized.update(backend=backend, strategy_version=version,
                      experiment_id=experiment_id or uuid.uuid4().hex,
                      parent_run_id=parent_id, label=label)
    return normalized


def run_backtest(config, progress=None, cancelled=None, *, source_store=None):
    backend = config.get('backend', 'reference')
    if backend == 'isolated_python' and config.get('strategy') == 'custom_python':
        from .custom_engine import run_backtest as execute_custom
        return execute_custom(config, progress=progress, cancelled=cancelled, source_store=source_store)
    if backend == 'reference':
        from .engine import run_backtest as execute
    elif backend == 'vnpy' and config.get('strategy') == 'buy_hold':
        from .vnpy_bridge import run_backtest as execute
    else:
        raise ValueError('未登记的策略/后端组合，拒绝执行')
    result = execute(config, progress=progress, cancelled=cancelled)
    result['backend'] = backend
    result['strategy_version'] = config.get('strategy_version')
    return result
