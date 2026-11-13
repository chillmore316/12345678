"""Saved-source receipt API tests: temporary stores, zero provider access."""
import http.client
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from research_center.http import make_server
from research_center.store import RunStore
from research_center.source_diagnostics import SourceChecks, run_check
from tests.test_source_diagnostics import NOW, Response, Session, row


class SourceChecksHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=RunStore(self.root)
        session=Session([Response({'data':[row('daily')]}),Response({'data':[row()]})])
        self.record=run_check('promax','510300.SH',now=NOW,secret_reader=lambda _:'test-only-secret',session_factory=lambda:session)
        self.checks=SourceChecks(self.root);self.saved=self.checks.save(self.record)
        self.server=make_server(self.store,0);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,method='GET',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,headers=headers or {});res=conn.getresponse();body=res.read();status=res.status;conn.close();return status,body
    def test_only_saved_read_routes_with_host_guard_no_source_calls(self):
        base='/api/research/source-checks'
        with patch('live_paper_compare.provider.keychain_secret',side_effect=AssertionError('credentials accessed')),patch('research_center.source_diagnostics.run_check',side_effect=AssertionError('network check called')):
            status,body=self.request(base);self.assertEqual(status,200)
            self.assertEqual(json.loads(body)['records'][0]['status'],'SAVED_SAMPLE')
            status,body=self.request(base+'/'+self.saved['check_id']);self.assertEqual(status,200);self.assertEqual(json.loads(body),self.saved)
            self.assertNotIn(b'test-only-secret',body);self.assertNotIn(str(self.root).encode(),body)
            self.assertEqual(self.request('/source-check')[0],200)
            for path in ('/source-check',base,base+'/'+self.saved['check_id']):
                self.assertEqual(self.request(path,headers={'Host':'evil.invalid'})[0],403)
            self.assertEqual(self.request(base,'POST')[0],405)
            self.assertIn(self.request(base+'/../../private')[0],(400,404))
            self.assertEqual(self.store.list(),[])
    def test_corrupt_receipt_visible_but_detail_fails_closed(self):
        (self.checks.root/(self.saved['check_id']+'.json')).write_text('{}')
        status,body=self.request('/api/research/source-checks');self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['records'][0]['status'],'CHECK_REQUIRED')
        self.assertEqual(self.request('/api/research/source-checks/'+self.saved['check_id'])[0],400)
    def test_scan_bound_preserves_pending_and_unknown_entries(self):
        for index in range(1002):(self.checks.root/('.pending-'+str(index))).touch()
        result=self.checks.list();self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['records']),100)
        self.assertEqual(len(list(self.checks.root.glob('.pending-*'))),1002)
    def test_temporal_and_row_count_inconsistency_rejected(self):
        from copy import deepcopy
        for field,value in [('temporal_state','calendar_unverified_recent_sample'),('age_seconds',0),('rows',0)]:
            bad=deepcopy(self.record);bad['checks'][1][field]=value
            with self.assertRaises(ValueError):self.checks.save(bad)
        bad=deepcopy(self.record);bad['checks'][0]['latest_time']='2026-10-30T12:00:00+08:00'
        with self.assertRaises(ValueError):self.checks.save(bad)
    def test_invalid_timestamp_error_does_not_echo_record_text(self):
        from copy import deepcopy
        bad=deepcopy(self.record);bad['observed_at']='PRIVATE-SOURCE-TIMESTAMP-SENTINEL'
        data=self.checks._encode(bad);ident=hashlib.sha256(data).hexdigest()[:32]
        (self.checks.root/(ident+'.json')).write_bytes(data)
        status,body=self.request('/api/research/source-checks/'+ident)
        self.assertEqual(status,400);self.assertNotIn(b'PRIVATE-SOURCE',body)
        status,body=self.request('/api/research/source-checks')
        self.assertEqual(status,200);self.assertNotIn(b'PRIVATE-SOURCE',body)

if __name__=='__main__':unittest.main()
