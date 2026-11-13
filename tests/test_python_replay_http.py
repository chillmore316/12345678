"""New account routes use real kernel + synthetic data + fake guest only."""
import http.client
import json
import subprocess
import sys
import threading
import unittest

from research_center.http import make_server
from tests import test_python_replay_accounts as fixtures


class PythonReplayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PythonReplayAccountsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, suffix='', value=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        method = 'GET' if value is None else 'POST'
        body = None if value is None else json.dumps(value).encode()
        connection.request(method, '/api/research/python-replays' + suffix, body,
                           {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        status, raw = response.status, response.read()
        connection.close()
        return status, raw

    def test_new_namespace_roundtrip_guards_idempotency_and_close_rollback(self):
        self.assertEqual(json.loads(self.request()[1]), {'accounts': []})
        self.assertFalse(self.fixture.accounts.root.exists())
        payload = {'run_id': self.fixture.source_run['run_id']}
        self.assertEqual(self.request(value=payload, headers={'Origin': 'https://external.invalid'})[0], 403)
        self.assertEqual(self.request(headers={'Host': 'external.invalid'})[0], 403)
        self.assertEqual(self.request(value={**payload, 'runtime': {}})[0], 400)
        self.assertFalse(self.fixture.accounts.root.exists())
        status, raw = self.request(value=payload)
        self.assertEqual(status, 201, raw)
        detail = json.loads(raw)
        ident = detail['record']['account_id']
        self.assertNotIn(b'guest-only', raw, 'never expose frozen source code')
        status, raw = self.request('/' + ident + '/resume', {'expected_revision': 0})
        self.assertEqual(status, 200, raw)
        step = {'expected_revision': 1, 'steps': 2, 'request_id': 'http-owned-step'}
        status, raw = self.request('/' + ident + '/step', step)
        self.assertEqual(status, 200, raw)
        detail = json.loads(raw)
        self.assertEqual(detail['record']['cursor'], 2)
        instances = self.fixture.fake_adapter.instances
        status, raw = self.request('/' + ident + '/step', step)
        self.assertTrue(json.loads(raw)['replayed'])
        self.assertEqual(instances, self.fixture.fake_adapter.instances)
        status, raw = self.request('/' + ident + '/step', {**step, 'request_id': 'stale-other'})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)['code'], 'PythonReplayRevisionConflict')
        self.fixture.fake_adapter.fail_close = True
        status, raw = self.request('/' + ident + '/step', {'expected_revision': 2, 'steps': 2, 'request_id': 'close-failure'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(self.request('/' + ident)[1])['record']['cursor'], 2)
        self.fixture.fake_adapter.fail_close = False
        self.assertEqual(self.request('/' + ident + '/equity.csv')[0], 200)
        self.assertEqual(self.request('/' + ident + '/pause', {'expected_revision': 2})[0], 200)

    def test_cli_gets_reopen_persisted_account_without_vm(self):
        created = self.fixture.create_account()
        ident = created['record']['account_id']
        for args in [('list',), ('show', ident)]:
            result = subprocess.run([sys.executable, '-m', 'research_center.cli', '--store', str(self.store.root),
                                     'python-replay', *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            if args[0] == 'show':
                self.assertEqual(value['record']['account_id'], ident)
                self.assertFalse(value['record']['paper_admission'])


if __name__ == '__main__':
    unittest.main()
