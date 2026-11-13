"""CLI used identically by Codex and the local Chinese research page."""
from __future__ import annotations
import argparse
import json
import time
from .store import RunStore, DEFAULT_STORE


def _load_research_spec(path, description):
    import os
    import stat
    from .intake import _parse_json

    limit = 64 * 1024
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'{description}必须是普通 JSON 文件')
        with open(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError(f'{description}超过 64 KiB')
    return _parse_json(raw, description)


def main():
    parser = argparse.ArgumentParser(description='本地分钟策略研究中心（只做研究回放）')
    parser.add_argument('--store', default=str(DEFAULT_STORE))
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--strategy', choices=['buy_hold', 'sma_cross', 'custom_python'], default='buy_hold')
    run.add_argument('--backend', choices=['reference', 'vnpy', 'isolated_python'], default='reference')
    run.add_argument('--source-id', help='已归档 Python 策略的注册编号')
    run.add_argument('--custom-params', default='{}', help='Python 策略参数；执行仓位参数仍用 --params')
    run.add_argument('--history-limit', type=int, default=60)
    run.add_argument('--experiment-id')
    run.add_argument('--parent-run-id')
    run.add_argument('--label', default='')
    run.add_argument('--symbol', default='000001.SZSE')
    run.add_argument('--start', default='2024-01-02')
    run.add_argument('--end', default='2024-01-05')
    run.add_argument('--initial-cash', type=float, default=100000)
    run.add_argument('--params', default='{}', help='JSON策略参数')
    run.add_argument('--wait', action='store_true')
    run.add_argument('--commission-rate', type=float, default=.00025)
    run.add_argument('--min-commission', type=float, default=5)
    run.add_argument('--slippage-bps', type=float, default=5)
    commands.add_parser('list')
    commands.add_parser('strategies')
    commands.add_parser('doctor')
    commands.add_parser('data')
    for name, help_text in (
        ('batch', '校验或执行同区间的有界参数对照'),
        ('rolling', '校验或执行研究期内的固定参数滚动验证'),
        ('symbols', '校验或执行同区间的独立多标的回测'),
    ):
        workflow = commands.add_parser(name, help=help_text)
        workflow_commands = workflow.add_subparsers(dest='workflow_action', required=True)
        for action in ('plan', 'run'):
            workflow_commands.add_parser(action).add_argument('--file', required=True)
    runtime = commands.add_parser('runtime', help='准备专用的离线 Python 执行环境')
    runtime.add_subparsers(dest='runtime_command', required=True).add_parser('prepare')
    python_source = commands.add_parser('python-source', help='归档 AI 编写的 Python 策略；登记时只做静态检查')
    python_commands = python_source.add_subparsers(dest='python_command', required=True)
    python_commands.add_parser('list')
    python_commands.add_parser('show').add_argument('source_id')
    register_python = python_commands.add_parser('register')
    register_python.add_argument('--file', required=True)
    register_python.add_argument('--name', required=True)
    register_python.add_argument('--description', default='')
    register_python.add_argument('--params', default='{}')
    register_python.add_argument('--parent-id')
    source = commands.add_parser('source', help='显式接源小样本诊断；不启动账户')
    source_commands = source.add_subparsers(dest='source_command', required=True)
    check_source = source_commands.add_parser('check')
    check_source.add_argument('--provider', choices=['promax', 'datahub'], required=True)
    check_source.add_argument('--symbol', default='510300.SH')
    source_commands.add_parser('list')
    source_commands.add_parser('show').add_argument('check_id')
    candidates = commands.add_parser('candidate', help='登记和查看未认证日线候选；不会用于交易')
    candidate_commands = candidates.add_subparsers(dest='candidate_command', required=True)
    candidate_commands.add_parser('list')
    candidate_commands.add_parser('show').add_argument('candidate_id')
    register_candidate = candidate_commands.add_parser('register')
    register_candidate.add_argument('--manifest', required=True)
    register_candidate.add_argument('--rows', required=True)
    register_candidate.add_argument('--label', default='')
    for name in ['show', 'report', 'cancel', '_worker']:
        commands.add_parser(name).add_argument('run_id')
    for replay_name in ('replay', 'python-replay'):
        replay = commands.add_parser(replay_name, help='独立历史模拟演练账户')
        replay_commands = replay.add_subparsers(dest='replay_command', required=True)
        replay_commands.add_parser('list')
        create_replay = replay_commands.add_parser('create')
        create_replay.add_argument('run_id')
        create_replay.add_argument('--label', default='')
        replay_commands.add_parser('show').add_argument('account_id')
        for action in ('resume', 'pause', 'step'):
            action_parser = replay_commands.add_parser(action)
            action_parser.add_argument('account_id')
            action_parser.add_argument('--revision', type=int, required=True)
            if action == 'step':
                action_parser.add_argument('--steps', type=int, default=60)
                action_parser.add_argument('--request-id', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8060)
    serve.add_argument('--verification-only', action='store_true', help='备份验证模式，禁止网页提交与账户操作')
    args = parser.parse_args()
    if args.command in {'batch', 'rolling', 'symbols'}:
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        if args.command == 'batch':
            from .batch import execute_batch as execute_workflow, plan_batch as plan_workflow
            description = '批量回测配置'
        elif args.command == 'rolling':
            from .rolling import execute_rolling as execute_workflow, plan_rolling as plan_workflow
            description = '滚动验证配置'
        else:
            from .symbol_comparison import execute_symbols as execute_workflow, plan_symbols as plan_workflow
            description = '多标的对照配置'

        def progress(event):
            print(json.dumps(event, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)

        try:
            spec = _load_research_spec(args.file, description)
            if args.workflow_action == 'plan':
                output = plan_workflow(SimpleNamespace(root=Path(args.store).resolve()), spec)
            else:
                output = execute_workflow(RunStore(args.store), spec, on_event=progress)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            parser.error(f'{description}不可用：{exc}')
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0 if args.workflow_action == 'plan' or output['status'] == 'completed' else 1
    if args.command == 'runtime':
        from .vm_runtime_manager import prepare_runtime
        output = prepare_runtime()
        print(json.dumps(output, ensure_ascii=False))
        return 0 if output['prepared'] else 1
    store = RunStore(args.store)
    if args.command == 'serve':
        from .http import serve
        serve(store, args.port, verification_only=args.verification_only)
        return 0
    if args.command == '_worker':
        from .runner import execute
        execute(store, args.run_id)
        return 0
    if args.command == 'run':
        from .runner import submit, refresh_status
        config = {k: getattr(args, k) for k in ('strategy', 'backend', 'experiment_id', 'parent_run_id', 'label',
                                               'symbol', 'start', 'end', 'initial_cash',
                                               'commission_rate', 'min_commission', 'slippage_bps')}
        config['params'] = json.loads(args.params)
        if args.strategy == 'custom_python':
            config.update(backend='isolated_python', source_id=args.source_id,
                          custom_params=json.loads(args.custom_params), history_limit=args.history_limit)
        record = submit(store, config)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if args.wait:
            while record['status'] in {'queued', 'running', 'cancelling'}:
                time.sleep(.5)
                record = refresh_status(store, store.get(record['run_id']))
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return 0 if record['status'] == 'completed' else 1
        return 0
    if args.command == 'python-source':
        from .custom_source_store import CustomSourceStore
        from pathlib import Path
        archive = CustomSourceStore(store.root / 'custom_sources')
        if args.python_command == 'register':
            # Explicit CLI input is bounded before parsing, never imported.
            with Path(args.file).open('rb') as stream:
                source = stream.read(65537)
            output = archive.register(source, name=args.name, description=args.description,
                                      default_params=json.loads(args.params), parent_id=args.parent_id)
        elif args.python_command == 'list':
            output = archive.list()
        else:
            output = archive.show(args.source_id)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    if args.command == 'source':
        from .source_diagnostics import SourceChecks, run_check
        try:
            diagnostics = SourceChecks(store.root)
            if args.source_command == 'check':
                output = diagnostics.save(run_check(args.provider, args.symbol))
            elif args.source_command == 'list':
                output = diagnostics.list()
            else:
                output = diagnostics.detail(args.check_id)
        except (OSError, ValueError, TypeError, KeyError):
            parser.error('接源检查不可完成或收据无效；请检查环境和编号。未启动交易。')
    elif args.command == 'candidate':
        from .intake import CandidateStore
        candidates = CandidateStore(store.root)
        try:
            if args.candidate_command == 'register':
                output = candidates.register(args.manifest, args.rows, args.label)
            elif args.candidate_command == 'show':
                output = candidates.detail(args.candidate_id)
            else:
                output = candidates.list()
        except (OSError, ValueError, TypeError, KeyError):
            parser.error('候选数据检查失败；请核对文件、SHA、字段和登记编号，原数据不会改写。')
    elif args.command in {'replay', 'python-replay'}:
        from .http import replay_store
        accounts = replay_store(store, 'python-replays' if args.command == 'python-replay' else 'replays')
        if args.replay_command == 'list':
            output = {'accounts': accounts.list()}
        elif args.replay_command == 'create':
            output = accounts.create(args.run_id, args.label)
        elif args.replay_command == 'show':
            output = accounts.detail(args.account_id)
        elif args.replay_command == 'step':
            output = accounts.step(args.account_id, args.steps, args.revision, args.request_id)
        else:
            output = getattr(accounts, args.replay_command)(args.account_id, args.revision)
    elif args.command in {'doctor', 'data'}:
        from .health import workspace_health, data_catalog
        output = workspace_health(store) if args.command == 'doctor' else data_catalog(store)
    elif args.command == 'strategies':
        from .registry import catalog
        output = catalog()
    elif args.command == 'cancel':
        from .runner import cancel
        output = cancel(store, args.run_id)
    elif args.command == 'report':
        detail = store.detail(args.run_id)
        output = {key: detail.get(key) for key in ('record', 'analytics', 'data_readiness', 'logs')}
    elif args.command == 'show':
        output = store.detail(args.run_id)
    else:
        output = {'runs': store.list()}
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
