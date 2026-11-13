"""CLI and HTTP integration using only isolated candidate files and stores."""
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from research_center.http import make_server
from research_center.store import RunStore, ROOT


class CandidateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root / 'store')
        self.rows = [dict(ts_code='501018.SH', trade_date=d, open=2., high=2.1,
                          low=1.9, close=2., pre_close=2., vol=100., amount=20.)
                     for d in ('20260728', '20260729')]
        self.rows_path = self.root / 'rows.json'
        self.rows_path.write_text(json.dumps(self.rows))
        self.manifest = dict(mode='RESEARCH_ONLY', status='ISOLATED_CANDIDATE_NOT_ADMITTED',
                             row_count=2, symbols=['501018.SH'], paper_admission=False,
                             candidate_promoted_to_runtime=False,
                             rows_sha256=hashlib.sha256(self.rows_path.read_bytes()).hexdigest(),
                             unknown_private_field='do-not-publish-this-test-sentinel')
        self.manifest_path = self.root / 'manifest.json'
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'research_center.cli', '--store', str(self.store.root),
                               'candidate', *args], cwd=ROOT, capture_output=True, text=True, timeout=10)

    def register(self, label='本地候选'):
        result = self.cli('register', '--manifest', str(self.manifest_path), '--rows', str(self.rows_path), '--label', label)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def request(self, path, method='GET', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = (response.status, response.read())
        connection.close()
        return result

    def test_cli_register_http_detail_download_and_guards(self):
        before = (self.manifest_path.read_bytes(), self.rows_path.read_bytes())
        detail = self.register()
        candidate_id = detail['record']['candidate_id']
        base = '/api/research/candidates'
        status, raw = self.request(base)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)['candidates']), 1)
        status, raw = self.request(base + '/' + candidate_id)
        self.assertEqual(status, 200)
        record = json.loads(raw)['record']
        self.assertFalse(record['paper_admission'])
        self.assertFalse(record['candidate_promoted_to_runtime'])
        self.assertEqual(json.loads(raw)['preview_rows'], self.rows)
        self.assertNotIn(b'do-not-publish-this-test-sentinel', raw)
        self.assertNotIn(str(self.root).encode(), raw)
        status, raw = self.request(base + '/' + candidate_id + '/rows.json')
        self.assertEqual(status, 200)
        self.assertEqual(raw, before[1])
        self.assertEqual(self.request('/data-check')[0], 200)
        self.assertIn('数据候选核查'.encode(), self.request('/research')[1])
        for path in ('/data-check', base, base + '/' + candidate_id):
            self.assertEqual(self.request(path, headers={'Host': 'untrusted.invalid'})[0], 403)
        self.assertEqual(self.request(base, 'POST', {'Content-Type': 'application/json'}, b'{"manifest":"/etc/passwd"}')[0], 405)
        self.assertIn(self.request(base + '/../../private')[0], (400, 404))
        self.assertEqual(before, (self.manifest_path.read_bytes(), self.rows_path.read_bytes()))
        self.assertEqual(self.store.list(), [], 'registration must not create a backtest or account')

    def test_cli_reopen_deduplicate_and_corruption_fail_closed(self):
        first = self.register()
        candidate_id = first['record']['candidate_id']
        second = self.register('另一个名称')
        self.assertEqual(first['record'], second['record'])
        reopened = self.cli('show', candidate_id)
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertEqual(json.loads(reopened.stdout)['record'], first['record'])
        self.assertEqual(len(json.loads(self.cli('list').stdout)['candidates']), 1)
        frozen = self.store.root / 'data_candidates' / candidate_id / 'rows.json'
        frozen.write_text('[]')
        self.assertEqual(self.request('/api/research/candidates/' + candidate_id)[0], 400)
        self.assertNotEqual(self.cli('show', candidate_id).returncode, 0)

    def test_invalid_source_cli_returns_controlled_error(self):
        self.rows_path.write_text('[]')
        result = self.cli('register', '--manifest', str(self.manifest_path), '--rows', str(self.rows_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn('候选数据检查失败', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('do-not-publish-this-test-sentinel', result.stderr)


if __name__ == '__main__':
    unittest.main()
