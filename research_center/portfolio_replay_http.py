"""Explicit local-only HTTP operations for historical portfolio accounts."""
from __future__ import annotations

import json

from .store import ROOT


def handle_portfolio_replays(handler, store, method, path, respond):
    if path in {'/portfolio-replay', '/portfolio-replay/'}:
        if method != 'GET':
            respond(handler, 405, {'error': '演练页面只允许读取'}, headers={'Allow': 'GET'})
        else:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/portfolio_replay.html').read_bytes(),
                    'text/html; charset=utf-8')
        return True
    if path == '/api/research/portfolio-replay.js':
        if method != 'GET':
            respond(handler, 405, {'error': '页面脚本只允许读取'}, headers={'Allow': 'GET'})
        else:
            respond(handler, 200, (ROOT / 'live_paper_compare/static/portfolio_replay.js').read_bytes(),
                    'text/javascript; charset=utf-8')
        return True
    prefix = '/api/research/portfolio-replays'
    if path != prefix and not path.startswith(prefix + '/'):
        return False
    parts = path.strip('/').split('/')
    collection = len(parts) == 3
    detail = len(parts) == 4
    action = parts[4] if len(parts) == 5 else None
    exports = {'equity.csv', 'orders.csv', 'trades.csv', 'positions.csv', 'events.csv'}
    allowed = 'GET, POST' if collection else 'GET' if detail or action in exports else 'POST' if action in {'resume', 'pause', 'step'} else None
    if allowed is None:
        raise FileNotFoundError('组合演练接口不存在')
    if method not in allowed.split(', '):
        respond(handler, 405, {'error': '此演练接口不支持该方法'}, headers={'Allow': allowed})
        return True
    # The shared handler has already checked Host and verification-only mode.
    # Check Origin and the exact body before opening any account store.
    body = None
    if method == 'POST':
        origin = handler.headers.get('Origin')
        if origin and origin != 'http://' + handler.headers.get('Host', ''):
            respond(handler, 403, {'error': '仅允许本机同源研究请求'})
            return True
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('需要 application/json')
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= 4096:
            raise ValueError('组合演练请求体大小无效')
        body = json.loads(handler.rfile.read(length))
        expected = {'run_id', 'label', 'request_id'} if collection else {'expected_revision', 'steps', 'request_id'} if action == 'step' else {'expected_revision'}
        required = expected - {'label'} if collection else expected
        if type(body) is not dict or set(body) - expected or required - set(body):
            raise ValueError('组合演练请求字段无效')
    from .portfolio_replay_accounts import PortfolioReplayAccounts
    accounts = PortfolioReplayAccounts(store)
    if method == 'GET':
        if collection:
            respond(handler, 200, {'accounts': accounts.list_accounts()})
        elif detail:
            respond(handler, 200, accounts.detail(parts[3]))
        else:
            respond(handler, 200, accounts.export_csv(parts[3], action[:-4]), 'text/csv; charset=utf-8')
    elif collection:
        respond(handler, 201, accounts.create(body['run_id'], body.get('label', ''), body['request_id']))
    elif action == 'step':
        respond(handler, 200, accounts.step(parts[3], body['steps'], body['expected_revision'], body['request_id']))
    else:
        respond(handler, 200, getattr(accounts, 'start' if action == 'resume' else 'pause')(
            parts[3], body['expected_revision']))
    return True
