"""HTTP permission and protocol tests; financial execution is tested separately."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from research_center.http import make_server
from research_center.store import RunStore


class PortfolioReplayHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / 'research')
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.backend = Mock()
        self.backend.list_accounts.return_value = []
        self.backend.create.return_value = {'record': {'account_id': 'a' * 32}}
        self.backend.start.return_value = {'record': {'revision': 1}}
        self.backend.step.return_value = {'record': {'revision': 2}, 'replayed': True}
        self.module = ModuleType('research_center.portfolio_replay_accounts')
        self.factory = Mock(return_value=self.backend)
        self.module.PortfolioReplayAccounts = self.factory
        self.modules = patch.dict(sys.modules, {self.module.__name__: self.module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, suffix='', body=None, **headers):
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)) as connection:
            connection.request(method, '/api/research/portfolio-replays' + suffix,
                               None if body is None else json.dumps(body),
                               {'Content-Type': 'application/json', **headers})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())

    def test_invalid_methods_host_origin_and_fields_never_open_store(self):
        for method, suffix, expected in [('PUT', '', 'GET, POST'), ('GET', '/a/resume', 'POST'),
                                          ('HEAD', '', 'GET, POST'), ('POST', '/a/equity.csv', 'GET')]:
            status, raw, headers = self.request(method, suffix, {})
            self.assertEqual(status, 405)
            self.assertEqual(headers['Allow'], expected)
            if method == 'HEAD':
                self.assertEqual(raw, b'')
        payload = {'run_id': 'b' * 32, 'request_id': 'owned-request'}
        self.assertEqual(self.request('POST', body=payload, Origin='https://outside.example')[0], 403)
        self.assertEqual(self.request('GET', Host='outside.example')[0], 403)
        self.assertEqual(self.request('POST', body={**payload, 'data_root': 'outside'})[0], 400)
        self.assertEqual(self.request('POST', body={'run_id': 'b' * 32})[0], 400)
        self.assertEqual(self.request('POST', '/a/step', {'steps': 1, 'expected_revision': 0})[0], 400)
        self.factory.assert_not_called()

    def test_routes_preserve_request_and_revision_identity(self):
        self.assertEqual(json.loads(self.request('GET')[1]), {'accounts': []})
        payload = {'run_id': 'b' * 32, 'label': '组合', 'request_id': 'owned-create'}
        self.assertEqual(self.request('POST', body=payload)[0], 201)
        self.backend.create.assert_called_once_with('b' * 32, '组合', 'owned-create')
        self.assertEqual(self.request('POST', '/a/resume', {'expected_revision': 0})[0], 200)
        self.backend.start.assert_called_once_with('a', 0)
        step = {'steps': 60, 'expected_revision': 1, 'request_id': 'owned-step'}
        status, raw, _ = self.request('POST', '/a/step', step)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['replayed'])
        self.backend.step.assert_called_once_with('a', 60, 1, 'owned-step')

    def test_verification_only_rejects_write_before_backend(self):
        self.close()
        self.server = make_server(self.store, 0, verification_only=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.request('POST', body={'run_id': 'b' * 32, 'request_id': 'owned'})[0], 403)
        self.factory.assert_not_called()

    def test_precommit_revision_or_source_conflicts_confirm_rollback(self):
        conflict = type('PortfolioReplayRevisionConflict', (ValueError,), {})
        changed = type('PortfolioReplaySourceChanged', (ValueError,), {})
        for error, confirmed in ((conflict('stale'), True), (changed('source'), True), (ValueError('invalid'), False)):
            self.backend.step.side_effect = error
            status, raw, _ = self.request('POST', '/a/step', {
                'expected_revision': 1, 'steps': 1, 'request_id': 'owned-step'})
            data = json.loads(raw)
            self.assertEqual(status, 400 if type(error) is ValueError else 409)
            self.assertEqual(data.get('rollback_confirmed', False), confirmed)


if __name__ == '__main__':
    unittest.main()
