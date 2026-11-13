"""The whole workspace HTTP surface against isolated synthetic runs."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from research_center.http import make_server
from research_center.store import RunStore
from research_center.registry import normalize_config
from research_center.engine import validate_config
from research_center.replay_kernel import initial_state,advance,result_from_state
from tests.test_research_replay_accounts import synthetic_bars
from research_center.launcher import launch,probe


class WorkspaceHTTPTest(unittest.TestCase):
    def setUp(self, *, verification_only=False):
        self.temp=tempfile.TemporaryDirectory();self.store=RunStore(self.temp.name)
        config=normalize_config({'symbol':'000001.SZSE','start':'2024-01-04','end':'2024-01-04','initial_cash':25000},self.store)
        r=self.store.create(config);self.run=r['run_id'];self.store.update(self.run,status='completed')
        result=result_from_state(config,advance(config,initial_state(config),synthetic_bars(),final=True),{'used_source_files':[]})
        result.update(backend='reference',strategy_version=config['strategy_version'],data_readiness={'status':'unverified','paper_admission':False})
        self.store.save_result(self.run,result)
        (self.store.directory(self.run)/'provenance.json').write_text(json.dumps({'backend':'reference','strategy_version':config['strategy_version']}))
        self.server=make_server(self.store,0,verification_only=verification_only);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        actual={'Content-Type':'application/json',**(headers or {})}
        connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),actual)
        response=connection.getresponse();status=response.status;payload=response.read();connection.close();return status,payload

    def test_health_startup_and_occupied_service_are_distinguished(self):
        self.assertTrue(probe(self.server.server_port))
        with patch('research_center.launcher.subprocess.Popen',side_effect=AssertionError('must reuse')):
            self.assertEqual(launch(self.server.server_port,False),0)
        with patch('research_center.launcher.probe',return_value=False),patch('research_center.launcher.port_free',return_value=False),patch('research_center.launcher.subprocess.Popen') as child:
            with self.assertRaisesRegex(RuntimeError,'占用'):launch(self.server.server_port,False)
            child.assert_not_called()
        self.assertEqual(self.request('/replay')[0],200)
        self.assertEqual(self.request('/api/research/data')[0],200)
        self.assertEqual(self.request('/api/research/health',headers={'Host':'untrusted.invalid'})[0],403)

    def test_replay_complete_flow_conflicts_idempotency_and_exports(self):
        base='/api/research/replays'
        status,payload=self.request(base,{'run_id':self.run,'label':'测试演练'});self.assertEqual(status,201)
        d=json.loads(payload);account=d['record']['account_id'];url=base+'/'+account
        self.assertEqual(d['bars'],[]);self.assertFalse(d['record']['paper_admission'])
        self.assertEqual(self.request(url+'/step',{'expected_revision':0,'steps':1,'request_id':'paused-request'})[0],400)
        d=json.loads(self.request(url+'/resume',{'expected_revision':0})[1]);revision=d['record']['revision']
        self.assertEqual(self.request(url+'/pause',{'expected_revision':0})[0],409)
        body={'expected_revision':revision,'steps':1,'request_id':'exact-once'}
        status,payload=self.request(url+'/step',body);self.assertEqual(status,200);d=json.loads(payload)
        self.assertEqual(d['record']['cursor'],1);self.assertEqual(len(d['bars']),1)
        status,duplicate=self.request(url+'/step',body);duplicate=json.loads(duplicate)
        self.assertTrue(duplicate['replayed']);self.assertEqual(duplicate['record']['cursor'],1)
        d=json.loads(self.request(url+'/pause',{'expected_revision':d['record']['revision']})[1])
        d=json.loads(self.request(url+'/resume',{'expected_revision':d['record']['revision']})[1])
        d=json.loads(self.request(url+'/step',{'expected_revision':d['record']['revision'],'steps':240,'request_id':'finish'})[1])
        self.assertEqual(d['record']['status'],'completed');self.assertEqual(d['record']['cursor'],4)
        for name in ('trades','orders','positions','equity','events'):
            self.assertEqual(self.request(url+'/'+name+'.csv')[0],200)
        status,payload=self.request(url+'/step',{'expected_revision':d['record']['revision'],'steps':1,'request_id':'beyond-end'})
        self.assertEqual(status,400)
        self.assertEqual(self.request(url,headers={'Host':'untrusted.invalid'})[0],403)

    def test_cross_origin_and_unknown_paths_do_not_create_accounts(self):
        base='/api/research/replays'
        self.assertFalse((Path(self.temp.name)/'replay_accounts').exists())
        self.assertEqual(self.request(base,{'run_id':self.run},headers={'Origin':'https://untrusted.invalid'})[0],403)
        self.assertFalse((Path(self.temp.name)/'replay_accounts').exists())
        self.assertEqual(self.request(base,{'run_id':self.run,'data_root':'/tmp/other'})[0],400)
        self.assertEqual(self.request('/api/research/replays/invalid')[0],400)


class VerificationHTTPTest(unittest.TestCase):
    def setUp(self):
        WorkspaceHTTPTest.setUp(self, verification_only=True)

    tearDown = WorkspaceHTTPTest.tearDown
    request = WorkspaceHTTPTest.request

    def test_backup_verification_denies_post_before_dispatch(self):
        status, health = self.request('/api/research/health')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(health)['verification_only'])
        self.assertEqual(self.request(f'/api/research/runs/{self.run}/equity.csv')[0], 200)
        routes = ('runs', 'replays', 'python-replays', 'workflows', 'runtime/prepare')
        for route in routes:
            with self.subTest(route=route):
                status, body = self.request('/api/research/' + route, {'run_id': self.run})
                self.assertEqual(status, 403)
                self.assertIn('备份验证模式', json.loads(body)['error'])
        self.assertEqual(len(self.store.list()), 1)
        self.assertFalse((self.store.root / 'replay_accounts').exists())
