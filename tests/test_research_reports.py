"""Report integration: historical immutability and explicit failure visibility."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from research_center.store import RunStore
from research_center.cli import main


def fixture():
    return {'equity': [{'source_datetime':None,'cash':1000,'quantity':0,'equity':1000},
                       {'source_datetime':'2024-01-02T15:00:00','cash':1000,'quantity':0,'equity':1000}],
            'bars': [], 'trades': [], 'metrics': {'total_return':0}, 'data_manifest':{}}


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RunStore(self.temp.name)
        self.record=self.store.create({'initial_cash':1000,'symbol':'003999.SZSE','start':'2024-01-02','end':'2024-01-02'})
        self.run=self.record['run_id']
        self.store.save_result(self.run,fixture())
        self.store.update(self.run,status='completed')

    def test_legacy_reads_and_exports_never_rewrite_history(self):
        before={p.name:p.read_bytes() for p in Path(self.temp.name).iterdir() if p.is_file()}
        run_files={p.name:p.read_bytes() for p in self.store.directory(self.run).iterdir()}
        record=self.store.get(self.run)
        detail=self.store.detail(self.run)
        self.assertEqual(detail['analytics']['origin'],'derived_read_only')
        self.assertEqual(detail['analytics']['metrics']['closed_pair_count'],0)
        self.assertIsNone(detail['analytics']['metrics']['win_rate'])
        self.assertFalse(detail['data_readiness']['paper_admission'])
        self.assertIn(b'2024-01-02',self.store.csv_bytes(self.run,'daily_returns'))
        self.assertEqual(self.store.csv_bytes(self.run,'closed_trades'),b'')
        self.assertEqual(record,self.store.get(self.run))
        self.assertEqual(run_files,{p.name:p.read_bytes() for p in self.store.directory(self.run).iterdir()})
        self.assertEqual(before,{p.name:p.read_bytes() for p in Path(self.temp.name).iterdir() if p.is_file()})

    def test_bad_legacy_ledger_is_unavailable_and_failed_runs_are_not_enriched(self):
        bad=fixture();bad['equity'][-1]['cash']=999
        self.store.save_result(self.run,bad)
        self.assertEqual(self.store.detail(self.run)['analytics']['status'],'unavailable')
        self.store.update(self.run,status='failed')
        self.assertNotIn('analytics',self.store.detail(self.run))

    def test_saved_enrichment_is_stable_and_cli_report_excludes_minute_payload(self):
        result=fixture();result['analytics']={'status':'available','daily_returns':[{'date':'2024-01-02','return':0}], 'metrics':{}}
        result['data_readiness']={'status':'unverified','paper_admission':False}
        self.store.save_result(self.run,result)
        with patch('research_center.analytics.build_analytics',side_effect=AssertionError('must not rederive')):
            self.assertEqual(self.store.detail(self.run)['analytics'],result['analytics'])
            with patch('sys.argv',['cli','--store',self.temp.name,'report',self.run]),patch('builtins.print') as output:
                self.assertEqual(main(),0)
                report=json.loads(output.call_args.args[0]);self.assertNotIn('bars',report)
                self.assertEqual(report['analytics'],result['analytics'])
        self.assertIn(b'2024-01-02',self.store.csv_bytes(self.run,'daily_returns'))
        with self.assertRaises(ValueError):self.store.csv_bytes(self.run,'../escape')

    def test_report_csv_http_guard_and_content(self):
        import http.client
        import threading
        from research_center.http import make_server
        server=make_server(self.store,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for name in ('daily_returns','monthly_returns','yearly_returns','closed_trades'):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
                url=f'/api/research/runs/{self.run}/{name}.csv'
                connection.request('GET',url);response=connection.getresponse()
                self.assertEqual(response.status,200);content=response.read();connection.close()
                if name!='closed_trades':self.assertIn(b'2024',content)
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
                connection.request('GET',url,headers={'Host':'untrusted.invalid'})
                response=connection.getresponse();self.assertEqual(response.status,403)
                response.read();connection.close()
        finally:
            server.shutdown();server.server_close();thread.join()
