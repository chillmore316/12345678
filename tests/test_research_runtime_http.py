"""Runtime preparation must be an explicit, bounded loopback action."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_center.http import make_server
from research_center.store import RunStore


class RuntimeHTTPTests(unittest.TestCase):
    def test_only_valid_post_prepares_the_fixed_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore(Path(temporary) / 'unused')
            original_files = {path.name: path.read_bytes() for path in store.root.iterdir()}
            with make_server(store, 0) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    self.check_requests(server.server_port)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertEqual({path.name: path.read_bytes() for path in store.root.iterdir()},
                             original_files)

    def check_requests(self, port):
        def request(method, body=None, **headers):
            with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as connection:
                connection.request(method, '/api/research/runtime/prepare', body,
                                   {'Content-Type': 'application/json', **headers})
                response = connection.getresponse()
                return response.status, json.loads(response.read())

        with patch('research_center.vm_runtime_manager.prepare_runtime') as prepare:
            self.assertEqual(request('GET')[0], 405)
            self.assertEqual(request('POST', '{}', Host='external.invalid')[0], 403)
            self.assertEqual(request('POST', '{}', Origin='https://external.invalid')[0], 403)
            self.assertEqual(request('POST', '{}', **{'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(request('POST', '{"instance":"other"}')[0], 400)
            self.assertEqual(request('POST', '{' + ' ' * 64 + '}')[0], 400)
            prepare.assert_not_called()
            prepare.return_value = {'prepared': False, 'error_code': 'runtime_busy'}
            status, output = request('POST', '{}')
            self.assertEqual(status, 503)
            self.assertEqual(output['error_code'], 'runtime_busy')
            self.assertIn('其他控制器', output['error'])
            prepare.assert_called_once_with()
            prepare.reset_mock()
            prepare.return_value = {'prepared': True, 'recovered_owned_job': False}
            self.assertEqual(request('POST', '{ }', Origin=f'http://127.0.0.1:{port}')[0], 200)
            prepare.assert_called_once_with()
