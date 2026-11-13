"""Exercise the local workflow routes without launching an actual worker."""
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


class WorkflowHTTPTests(unittest.TestCase):
    def test_workflow_routes_keep_preview_and_submission_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / 'store')
            with make_server(store, 0) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    self.check_requests(server.server_port)
                finally:
                    server.shutdown()
                    worker.join()

    def check_requests(self, port):
        root = '/api/research/workflows'
        request_id = '12345678123442348234123456781234'
        job_id = '12345678123442348234123456781235'
        spec = {'base': {}, 'cases': []}
        payload = json.dumps({'kind': 'batch', 'spec': spec})
        run = json.dumps({'kind': 'batch', 'spec': spec, 'request_id': request_id})

        def request(method, path, body=None, **headers):
            with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as connection:
                connection.request(method, path, body, {'Content-Type': 'application/json', **headers})
                response = connection.getresponse()
                return response.status, response.read()

        with patch('research_center.workflow_jobs.WorkflowJobs', autospec=True) as factory:
            backend = factory.return_value
            backend.list.return_value = {'jobs': []}
            backend.preview.return_value = {'valid': True, 'cases': []}
            backend.submit.return_value = {'job_id': job_id, 'status': 'queued'}
            backend.get.return_value = {'job_id': job_id, 'status': 'completed'}
            self.assertEqual(request('GET', '/research-workflows')[0], 200)
            self.assertEqual(request('GET', '/api/research/workflow-controls.js')[0], 200)
            self.assertEqual(request('GET', root, Host='foreign.example')[0], 403)
            self.assertEqual(request('POST', root, run, Origin='https://foreign.example')[0], 403)
            self.assertEqual(request('POST', root, run, **{'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(request('POST', root, ' ' * 65537)[0], 400)
            self.assertEqual(request('POST', root, '{"kind":"batch","kind":"rolling","spec":{}}')[0], 400)
            self.assertEqual(request('POST', root, payload)[0], 400)
            self.assertEqual(request('POST', root + '/preview', run)[0], 400)
            self.assertEqual(request('GET', root + '/preview')[0], 405)
            backend.submit.assert_not_called()
            backend.preview.assert_not_called()
            self.assertEqual(request('GET', root)[0], 200)
            self.assertEqual(request('POST', root + '/preview', payload)[0], 200)
            backend.preview.assert_called_once_with('batch', spec)
            backend.submit.assert_not_called()
            status, body = request('POST', root, run, Origin=f'http://127.0.0.1:{port}')
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(body)['job_id'], job_id)
            backend.submit.assert_called_once_with('batch', spec, request_id)
            self.assertEqual(request('GET', root + '/' + job_id)[0], 200)
            backend.get.assert_called_once_with(job_id)
