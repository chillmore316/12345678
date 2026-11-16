"""Actual HTTP/CLI restart round trip using a supervised synthetic source run."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from research_center.http import make_server
from research_center.portfolio_archive import archive_root
from research_center.portfolio_cli import run_bounded
from research_center.store import RunStore
from tests.test_portfolio_execution import _portfolio_config, _write_synthetic_parquet


class PortfolioReplayIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root / 'research')
        data_root = self.root / 'synthetic'
        _write_synthetic_parquet(data_root, bars_per_symbol=6)
        receipt = run_bounded(_portfolio_config(data_root), archive_root(self.store), timeout_seconds=10)
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.run_id = receipt['run_id']
        self.reference = json.loads((archive_root(self.store) / self.run_id / 'result.json').read_text())
        self.server = None
        self.start_server()
        self.addCleanup(self.stop_server)

    def start_server(self):
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.server = None

    def request(self, suffix='', body=None):
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)) as connection:
            connection.request('GET' if body is None else 'POST', '/api/research/portfolio-replays' + suffix,
                               None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read()
            self.assertIn(response.status, (200, 201), (response.status, raw[:300]))
            return raw if suffix.endswith('.csv') else json.loads(raw)

    def test_shared_cash_replay_survives_http_restart_cli_reads_and_lost_response_retry(self):
        create = {'run_id': self.run_id, 'label': '合成恢复演练', 'request_id': 'integration-create'}
        detail = self.request(body=create)
        ident = detail['record']['account_id']
        self.assertEqual(self.request(body=create)['record']['account_id'], ident)
        detail = self.request('/' + ident + '/resume', {'expected_revision': detail['record']['revision']})
        step = {'steps': 3, 'expected_revision': detail['record']['revision'], 'request_id': 'integration-first-half'}
        midway = self.request('/' + ident + '/step', step)
        self.assertEqual(midway['record']['cursor'], 3)
        self.assertEqual(len(midway['equity']), 4)
        self.assertTrue(midway['trades'])
        cutoff = midway['equity'][-1]['source_datetime']
        self.assertEqual(midway['trades'], [trade for trade in self.reference['trades']
                                         if trade['fill_datetime'] <= cutoff])
        self.stop_server()
        self.start_server()
        retry = self.request('/' + ident + '/step', step)
        self.assertTrue(retry['replayed'])
        self.assertEqual(retry['record']['cursor'], 3)
        reread = self.request('/' + ident)
        self.assertEqual(reread['record']['revision'], midway['record']['revision'])
        cli = subprocess.run([sys.executable, '-m', 'research_center.cli', '--store', str(self.store.root),
                              'portfolio-replay', 'show', ident], capture_output=True, text=True, timeout=10)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['record']['cursor'], 3)
        final = self.request('/' + ident + '/step', {
            'steps': 3, 'expected_revision': reread['record']['revision'], 'request_id': 'integration-second-half'})
        self.assertEqual(final['record']['status'], 'completed')
        for name in ('equity', 'orders', 'trades', 'positions'):
            self.assertEqual(final[name], self.reference[name], name)
        self.assertEqual(final['record']['current_equity'], self.reference['metrics']['ending_equity'])
        self.assertIs(final['record']['paper_admission'], False)
        self.assertNotIn(str(self.root), json.dumps(final))
        for name in ('equity', 'orders', 'trades', 'positions', 'events'):
            self.assertTrue(self.request('/' + ident + '/' + name + '.csv'))

    def test_concurrent_same_request_advances_cash_and_cursor_once(self):
        created = self.request(body={'run_id': self.run_id, 'request_id': 'concurrent-create'})
        ident = created['record']['account_id']
        self.request('/' + ident + '/resume', {'expected_revision': 0})
        body = {'expected_revision': 1, 'steps': 3, 'request_id': 'concurrent-step'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request('/' + ident + '/step', body), range(2)))
        self.assertEqual(sorted(item['replayed'] for item in responses), [False, True])
        final = self.request('/' + ident)
        self.assertEqual(final['record']['cursor'], 3)
        self.assertEqual(final['record']['revision'], 2)
        self.assertEqual(responses[0]['equity'], responses[1]['equity'])
        self.assertEqual(responses[0]['trades'], responses[1]['trades'])


if __name__ == '__main__':
    unittest.main()
