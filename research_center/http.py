"""Small shared HTTP surface, without starting any market or account service."""
from __future__ import annotations
import json
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .store import ROOT, RunStore, ANALYTIC_EXPORTS


def respond(handler, status, data, content_type='application/json; charset=utf-8', *, headers=None):
    if not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    if getattr(handler, 'command', 'GET') != 'HEAD':
        handler.wfile.write(data)


def replay_store(store, namespace):
    if namespace == 'python-replays':
        from .python_replay_accounts import PythonReplayAccounts
        return PythonReplayAccounts(store)
    from .replay_accounts import ReplayAccounts
    return ReplayAccounts(store)


def handle(handler, store, method='GET', *, verification_only=False):
    path = urlparse(handler.path).path
    if path not in {'/research', '/research/', '/portfolio', '/portfolio/', '/replay', '/replay/', '/data-check', '/data-check/', '/source-check', '/source-check/', '/python-strategy', '/python-strategy/', '/python-replay', '/python-replay/', '/research-workflows', '/research-workflows/'} and not path.startswith('/api/research/'):
        return False
    # Loopback binding alone does not stop browser DNS rebinding. Protect
    # read paths as well as writes: records contain local research details.
    host = handler.headers.get('Host', '')
    local_hosts = {f'127.0.0.1:{handler.server.server_port}', f'localhost:{handler.server.server_port}'}
    if host not in local_hosts:
        respond(handler, 403, {'error': '仅允许本机研究请求'})
        return True
    if verification_only and method != 'GET':
        respond(handler, 403, {'error': '备份验证模式只允许读取，不能提交任务或修改演练账户。'})
        return True
    try:
        if method == 'GET' and path in {'/portfolio', '/portfolio/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/portfolio.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if path == '/api/research/portfolios' or path.startswith('/api/research/portfolios/'):
            from .portfolio_archive import PortfolioArchive, archive_root
            parts = path.strip('/').split('/')
            archive = PortfolioArchive(archive_root(store))
            if method != 'GET':
                respond(handler, 405, {'error': '组合归档接口只允许读取历史记录'}, headers={'Allow': 'GET'})
            elif len(parts) == 3:
                respond(handler, 200, archive.list())
            elif len(parts) == 4:
                respond(handler, 200, archive.get(parts[3]))
            else:
                raise FileNotFoundError('unknown portfolio route')
            return True
        if method == 'GET' and path in {'/research-workflows', '/research-workflows/'}:
            page = ROOT / 'live_paper_compare/static/research_workflows.html'
            respond(handler, 200, page.read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path == '/api/research/workflow-controls.js':
            script = ROOT / 'live_paper_compare/static/research_workflows.js'
            respond(handler, 200, script.read_bytes(), 'text/javascript; charset=utf-8')
            return True
        if method == 'GET' and path == '/api/research/runtime-controls.js':
            script = ROOT / 'live_paper_compare/static/research_runtime.js'
            respond(handler, 200, script.read_bytes(), 'text/javascript; charset=utf-8')
            return True
        if path == '/api/research/runtime/prepare':
            if method != 'POST':
                respond(handler, 405, {'error': '请通过显式准备操作启动隔离环境'})
                return True
            origin = handler.headers.get('Origin')
            if origin and origin != 'http://' + host:
                respond(handler, 403, {'error': '仅允许本机同源研究请求'})
                return True
            length = int(handler.headers.get('Content-Length', '0'))
            if (handler.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                    or not 0 < length <= 64
                    or json.loads(handler.rfile.read(length)) != {}):
                raise ValueError('准备隔离环境只接受空 JSON 对象')
            from .vm_runtime_manager import RuntimeManagerError, prepare_runtime
            output = prepare_runtime()
            if output['prepared']:
                respond(handler, 200, output)
            else:
                respond(handler, 503, {**output, 'error': RuntimeManagerError(output['error_code']).message})
            return True
        if method == 'GET' and path in {'/python-replay', '/python-replay/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/python_replay.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/python-strategy', '/python-strategy/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/python_strategy.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/research', '/research/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/research.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/replay', '/replay/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/replay.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/data-check', '/data-check/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/data_check.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/source-check', '/source-check/'}:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/source_check.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        if method == 'GET' and path in {'/api/research/health', '/api/research/data'}:
            from .health import workspace_health, data_catalog
            output = workspace_health(store) if path.endswith('/health') else data_catalog(store)
            if verification_only and path.endswith('/health'):
                output.update(verification_only=True, message='备份验证模式 · ' + output['message'])
            respond(handler, 200, output)
            return True
        if method == 'GET' and path == '/api/research/strategies':
            from .registry import catalog
            respond(handler, 200, catalog())
            return True
        parts = path.strip('/').split('/')
        if parts[:3] == ['api', 'research', 'workflows']:
            from .workflow_jobs import WorkflowJobs
            workflows = WorkflowJobs(store)
            if method == 'GET' and len(parts) == 3:
                respond(handler, 200, workflows.list())
            elif method == 'GET' and len(parts) == 4 and parts[3] != 'preview':
                respond(handler, 200, workflows.get(parts[3]))
            elif method == 'POST' and parts[3:] in ([], ['preview']):
                origin = handler.headers.get('Origin')
                if origin and origin != 'http://' + host:
                    respond(handler, 403, {'error': '仅允许本机同源研究请求'})
                    return True
                from .intake import _parse_json
                length = int(handler.headers.get('Content-Length', '0'))
                if (handler.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                        or not 0 < length <= 64 * 1024):
                    raise ValueError('研究实验请求须使用不超过 64 KiB 的 JSON')
                config = _parse_json(handler.rfile.read(length), '研究实验请求')
                expected = {'kind', 'spec'} if parts[3:] else {'kind', 'spec', 'request_id'}
                if type(config) is not dict or set(config) != expected:
                    raise ValueError('研究实验请求字段不完整或含未知字段')
                if parts[3:]:
                    respond(handler, 200, workflows.preview(config['kind'], config['spec']))
                else:
                    respond(handler, 202, workflows.submit(config['kind'], config['spec'], config['request_id']))
            else:
                respond(handler, 405, {'error': '此研究实验接口不支持该操作'})
            return True
        if parts[:3] == ['api', 'research', 'custom-sources']:
            from .custom_source_store import CustomSourceStore
            sources = CustomSourceStore(store.root / 'custom_sources')
            if method == 'GET':
                if len(parts) == 3:
                    output = sources.list()
                elif len(parts) == 4 and parts[3] == 'template.py':
                    respond(handler, 200, (ROOT / 'research_center/templates/sma_strategy.py.txt').read_bytes(), 'text/x-python; charset=utf-8')
                    return True
                elif len(parts) == 4:
                    output = sources.show(parts[3])
                else:
                    raise FileNotFoundError('unknown custom source route')
                respond(handler, 200, output)
            elif method == 'POST' and len(parts) == 3:
                origin = handler.headers.get('Origin')
                if origin and origin != 'http://' + host:
                    respond(handler, 403, {'error': '仅允许本机同源研究请求'})
                    return True
                if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('需要 application/json')
                length = int(handler.headers.get('Content-Length', '0'))
                if not 0 < length <= 128 * 1024:
                    raise ValueError('源码登记请求体超过上限或为空')
                config = json.loads(handler.rfile.read(length))
                if type(config) is not dict or set(config) - {'name', 'description', 'default_params', 'source_base64', 'parent_id'}:
                    raise ValueError('源码登记请求包含未知字段')
                encoded = config.pop('source_base64', None)
                if type(encoded) is not str or len(encoded) > 87384:
                    raise ValueError('源码必须为不超过 64 KiB 的文件')
                try:
                    source = base64.b64decode(encoded, validate=True)
                except (ValueError, UnicodeError):
                    raise ValueError('源码编码无效') from None
                respond(handler, 201, sources.register(source, **config))
            else:
                respond(handler, 405, {'error': '源码记录仅支持登记和读取'})
            return True
        if parts[:3] == ['api','research','source-checks']:
            if method != 'GET':
                respond(handler, 405, {'error':'接源检查记录仅可读取；新请求须用本机CLI显式发起。'})
                return True
            from .source_diagnostics import SourceChecks
            records = SourceChecks(store.root)
            if len(parts) == 3:
                respond(handler, 200, records.list())
            elif len(parts) == 4:
                respond(handler, 200, records.detail(parts[3]))
            else:
                raise FileNotFoundError('unknown source check route')
            return True
        if parts[:3] == ['api', 'research', 'candidates']:
            if method != 'GET':
                respond(handler, 405, {'error': '候选数据仅可读取；登记须使用本机CLI显式选择文件。'})
                return True
            from .intake import CandidateStore
            candidates = CandidateStore(store.root)
            if len(parts) == 3:
                respond(handler, 200, candidates.list())
            elif len(parts) == 4:
                respond(handler, 200, candidates.detail(parts[3]))
            elif len(parts) == 5 and parts[4] == 'rows.json':
                respond(handler, 200, candidates.rows_bytes(parts[3]), 'application/json; charset=utf-8')
            else:
                raise FileNotFoundError('unknown candidate route')
            return True
        if parts[:3] not in (['api', 'research', 'runs'], ['api', 'research', 'replays'], ['api', 'research', 'python-replays']):
            raise FileNotFoundError('unknown route')
        if parts[2] in {'replays', 'python-replays'} and method == 'GET':
            accounts = replay_store(store, parts[2])
            if method == 'GET':
                if len(parts) == 3:
                    respond(handler, 200, {'accounts': accounts.list()})
                elif len(parts) == 4:
                    respond(handler, 200, accounts.detail(parts[3]))
                elif len(parts) == 5 and parts[4] in {'equity.csv', 'orders.csv', 'trades.csv', 'positions.csv', 'events.csv'}:
                    respond(handler, 200, accounts.csv_bytes(parts[3], parts[4][:-4]), 'text/csv; charset=utf-8')
                else:
                    raise FileNotFoundError('unknown replay route')
                return True
        if method == 'GET':
            from .runner import refresh_status
            if len(parts) == 3:
                respond(handler, 200, {'runs': [refresh_status(store, record) for record in store.list()]})
            elif len(parts) == 4:
                refresh_status(store, store.get(parts[3]))
                respond(handler, 200, store.detail(parts[3]))
            elif len(parts) == 5 and parts[4] in {'trades.csv', 'orders.csv', 'equity.csv', 'positions.csv'} | {name + '.csv' for name in ANALYTIC_EXPORTS}:
                respond(handler, 200, store.csv_bytes(parts[3], parts[4][:-4]), 'text/csv; charset=utf-8')
            else:
                raise FileNotFoundError('unknown route')
        elif method == 'POST':
            # Local services can still receive requests from hostile browser pages.
            origin = handler.headers.get('Origin')
            if origin and origin != 'http://' + host:
                respond(handler, 403, {'error': '仅允许本机同源研究请求'})
                return True
            if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('需要 application/json')
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('请求体大小无效')
            config = json.loads(handler.rfile.read(length))
            if not isinstance(config, dict):
                raise ValueError('需要 JSON 对象')
            if parts[2] in {'replays', 'python-replays'}:
                accounts = replay_store(store, parts[2])
                if len(parts) == 3:
                    if set(config) - {'run_id', 'label'}:
                        raise ValueError('模拟演练只接受运行编号和名称')
                    output = accounts.create(config.get('run_id'), config.get('label', ''))
                    respond(handler, 201, output)
                elif len(parts) == 5 and parts[4] in {'resume', 'pause', 'step'}:
                    allowed = {'expected_revision', 'request_id', 'steps'} if parts[4] == 'step' else {'expected_revision'}
                    if set(config) - allowed:
                        raise ValueError('模拟演练请求包含未知字段')
                    if parts[4] == 'step':
                        output = accounts.step(parts[3], config.get('steps'), config.get('expected_revision'), config.get('request_id'))
                    else:
                        output = getattr(accounts, parts[4])(parts[3], config.get('expected_revision'))
                    respond(handler, 200, output)
                else:
                    raise FileNotFoundError('unknown replay route')
            elif len(parts) == 3:
                from .runner import submit
                if 'data_root' in config:
                    raise ValueError('网页不能更改原始数据目录')
                record = submit(store, config)
                record = store.update(record['run_id'], report_url='http://' + host + '/research#' + record['run_id'])
                respond(handler, 202, record)
            elif len(parts) == 5 and parts[4] == 'cancel':
                from .runner import cancel
                respond(handler, 200, cancel(store, parts[3]))
            else:
                raise FileNotFoundError('unknown route')
        else:
            raise FileNotFoundError('unknown method')
    except FileNotFoundError as exc:
        respond(handler, 404, {'error': str(exc)})
    except (ValueError, TypeError, KeyError) as exc:
        kind = type(exc).__name__
        message = str(exc)
        if kind == 'PythonReplayRevisionConflict':
            message = '账户进度已更新，请刷新后再操作；重试推进请求须保留原编号和参数。'
        elif kind == 'PythonReplaySourceChanged':
            message = '策略或执行代码已变化，请用当前版本重跑回测后创建新的演练账户。'
        elif kind == 'PythonReplayAccountError':
            message = '历史演练请求未通过检查，原检查点保持不变。请核对账户状态、参数和隔离执行环境。'
        error = {'error': message, 'code': kind}
        if kind.startswith('PythonReplay'):
            # These account exceptions contain fixed protocol diagnostics,
            # never guest exception text or source. Clients can distinguish a
            # confirmed rollback from an unknown response loss.
            error['reason'] = str(exc)
        respond(handler, 409 if kind in {'RevisionConflict', 'SourceCodeChanged', 'PythonReplayRevisionConflict', 'PythonReplaySourceChanged', 'PortfolioArtifactChanged'} else 400, error)
    except Exception:
        respond(handler, 500, {'error': '研究服务异常，请检查本地日志'})
    return True


def make_server(store, port=8060, *, verification_only=False):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path == '/':
                self.send_response(302)
                self.send_header('Location', '/research')
                self.end_headers()
            elif not handle(self, store, verification_only=verification_only):
                respond(self, 404, {'error': 'not found'})
        def do_POST(self):
            if not handle(self, store, 'POST', verification_only=verification_only):
                respond(self, 404, {'error': 'not found'})
        def _unsupported_method(self):
            path = urlparse(self.path).path
            if path == '/api/research/portfolios' or path.startswith('/api/research/portfolios/'):
                handle(self, store, self.command, verification_only=verification_only)
            else:
                self.send_error(501, 'Unsupported method')
        do_PUT = _unsupported_method
        do_DELETE = _unsupported_method
        do_PATCH = _unsupported_method
        do_OPTIONS = _unsupported_method
        do_HEAD = _unsupported_method
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(store, port=8060, *, verification_only=False):
    server = make_server(store, port, verification_only=verification_only)
    print(f'本地研究中心 http://127.0.0.1:{server.server_port}/research', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
