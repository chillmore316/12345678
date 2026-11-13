"""Source intake integration; owned temporary stores, no VM or market data."""
import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from research_center.http import make_server
from research_center.store import RunStore


class CustomSourceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RunStore(self.root / 'runs')
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, method='GET', value=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        body = None if value is None else json.dumps(value).encode()
        merged = {'Content-Type': 'application/json', **(headers or {})}
        connection.request(method, path, body, merged)
        response = connection.getresponse()
        status, raw = response.status, response.read()
        connection.close()
        return status, raw

    def test_intake_roundtrip_never_executes_source_or_creates_run(self):
        base = '/api/research/custom-sources'
        status, raw = self.request(base)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['empty'])
        self.assertFalse((self.store.root / 'custom_sources').exists())
        canary = self.root / 'must-not-exist'
        source = (f"from pathlib import Path\nPath({str(canary)!r}).touch()\n"
                  "def build_strategy(symbol, params):\n    return None\n").encode()
        payload = dict(name='接口测试', source_base64=base64.b64encode(source).decode(), default_params={'period': 5})
        status, raw = self.request(base, 'POST', payload)
        self.assertEqual(status, 201, raw)
        record = json.loads(raw)
        self.assertFalse(record['runtime_interface_verified'])
        self.assertFalse(canary.exists())
        status, raw = self.request(base + '/' + record['registration_id'])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), record)
        self.assertNotIn(b'Path(', raw)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.request('/python-strategy')[0], 200)
        self.assertIn(b'build_strategy', self.request(base + '/template.py')[1])

    def test_browser_and_upload_guards_fail_before_writes(self):
        base = '/api/research/custom-sources'
        source = base64.b64encode(b'def build_strategy(symbol, params):\n    return None\n').decode()
        payload = dict(name='guard', source_base64=source)
        for route in ('/python-strategy', base, base + '/template.py'):
            self.assertEqual(self.request(route, headers={'Host': 'external.invalid'})[0], 403)
        self.assertEqual(self.request(base, 'POST', payload, {'Origin': 'https://external.invalid'})[0], 403)
        for value in ({**payload, 'runtime': {}}, {**payload, 'source_base64': '%%%invalid'},
                      {**payload, 'default_params': {'private_key': 'owned-fixture'}},
                      {**payload, 'source_base64': base64.b64encode(b'x' * 65537).decode()}):
            self.assertEqual(self.request(base, 'POST', value)[0], 400)
        self.assertFalse((self.store.root / 'custom_sources').exists())
        self.assertEqual(self.store.list(), [])


if __name__ == '__main__':
    unittest.main()
