"""Synthetic endpoint/credential/clock tests; no external requests or Keychain."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import requests
from research_center import source_diagnostics as diag

NOW = datetime(2026,11,1,16,tzinfo=diag.SH)
SECRET = 'SHOULD_NEVER_BE_IN_OUTPUT'

def row(kind='minute', **changes):
    value = dict(ts_code='510300.SH',open=2,high=3,low=1,close=2.5,vol=100,amount=250)
    value.update({'trade_time':'2026-10-30 15:00:00'} if kind=='minute' else {'trade_date':'20261030'})
    return {**value,**changes}

class Response:
    def __init__(self, payload=None, status=200, blocks=None):
        self.status_code=status; self.payload=payload; self.blocks=blocks
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def iter_content(self,size):
        if self.blocks is not None: yield from self.blocks
        else: yield json.dumps(self.payload).encode()

class Session:
    def __init__(self,responses): self.responses=iter(responses);self.calls=[];self.trust_env=True
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs)); result=next(self.responses)
        if isinstance(result,Exception): raise result
        return result

class SourceDiagnosticsTests(unittest.TestCase):
    def check(self,responses,provider='promax',secret=lambda _:SECRET):
        session=Session(responses)
        record=diag.run_check(provider,'510300.SH',now=NOW,secret_reader=secret,session_factory=lambda:session)
        return record,session

    def test_exact_budget_route_and_no_secret_or_fake_admission(self):
        result,session=self.check([Response({'data':[row('daily')]}),Response({'data':[row()]})])
        self.assertEqual(result['network_attempts'],2)
        self.assertEqual(len(session.calls),2)
        self.assertFalse(session.trust_env)
        for url,kwargs in session.calls:
            self.assertTrue(url.startswith('https://'))
            self.assertFalse(kwargs['allow_redirects']);self.assertTrue(kwargs['stream'])
            self.assertIsNone(kwargs['proxies']);self.assertEqual(kwargs['timeout'],(3,5))
        self.assertEqual(session.calls[0][1]['params']['start_date'],'20261026')
        self.assertEqual(session.calls[1][1]['params']['limit'],15)
        self.assertEqual(result['checks'][1]['temporal_state'],'stale_sample')
        self.assertNotIn(SECRET,json.dumps(result))
        for name in ('paper_admission','source_certification','coverage_complete','runtime_changed'):self.assertIs(result[name],False)

    def test_missing_credentials_no_network(self):
        def missing(_): raise RuntimeError(SECRET)
        result,session=self.check([],secret=missing)
        self.assertEqual(session.calls,[]);self.assertEqual(result['network_attempts'],0)
        self.assertEqual({x['outcome'] for x in result['checks']},{'credential_unavailable'})
        self.assertNotIn(SECRET,json.dumps(result))

    def test_no_error_body_redirect_following_or_retry(self):
        result,session=self.check([Response(status=403),Response(status=302)])
        self.assertEqual([x['outcome'] for x in result['checks']],['http_denied','http_error'])
        self.assertEqual(len(session.calls),2)

    def test_bounded_body_and_bad_business_response(self):
        result,_=self.check([Response(blocks=[b'x'*(diag.MAX_BODY+1)]),Response({'error':SECRET})])
        self.assertEqual([x['outcome'] for x in result['checks']],['invalid_sample','invalid_sample'])
        self.assertNotIn(SECRET,json.dumps(result))

    def test_transport_failure_and_timeout_do_not_leak_or_retry(self):
        result,session=self.check([requests.exceptions.ProxyError(SECRET),requests.Timeout(SECRET)])
        self.assertEqual([x['outcome'] for x in result['checks']],['transport_error','timeout'])
        self.assertEqual(len(session.calls),2);self.assertNotIn(SECRET,json.dumps(result))

    def test_payload_rejects_wrong_symbol_duplicates_window_and_ohlc(self):
        for rows in ([row(ts_code='510500.SH')],[row(),row()], [row(high=1)], [row(vol=True)], [row(vol=0)], [row(trade_time='20261030')], [row(close=float('nan'))], [row(close=10**1000)], [row('daily',trade_date='20260101')], [row()]*16):
            with self.subTest(rows=rows),self.assertRaises((ValueError,TypeError)):
                diag.inspect_payload({'data':rows},kind='daily' if 'trade_date' in rows[0] else 'minute',symbol='510300.SH',now=NOW,start=NOW.date().replace(day=1),end=NOW.date())

    def test_recent_sample_does_not_certify_calendar_or_bar_semantics(self):
        monday=datetime(2026,11,2,10,tzinfo=diag.SH)
        values=diag.inspect_payload({'data':[row(trade_time='2026-11-02 09:59:00')]},kind='minute',symbol='510300.SH',now=monday,start=monday.date(),end=monday.date())
        self.assertEqual(values['temporal_state'],'calendar_unverified_recent_sample')
        self.assertEqual(diag._temporal(monday,monday.replace(day=1),False)[0],'future_stamp')
        self.assertEqual(diag._temporal(NOW,NOW,False)[0],'weekend_sample')
        values=diag.inspect_payload({'data':[row(trade_time='2026-11-02 09:59:00')],'meta':{'stale':'true'}},kind='minute',symbol='510300.SH',now=monday,start=monday.date(),end=monday.date())
        self.assertEqual(values['temporal_state'],'stale_sample');self.assertIs(values['vendor_stale'],True)

    def test_datahub_uses_explicit_https_existing_host(self):
        result,session=self.check([Response({'data':[]}),Response({'data':[]})],provider='datahub')
        self.assertIn('https://datahubco.com/',session.calls[0][0])
        self.assertTrue(session.calls[0][0].endswith('/fund-daily'))
        self.assertTrue(session.calls[1][0].endswith('/rt-min'))

    def test_input_rejected_before_credentials(self):
        with patch('live_paper_compare.provider.keychain_secret',side_effect=AssertionError('unexpected')):
            for provider,symbol in [('unknown','510300.SH'),('promax','*'),('promax','000001.SZ')]:
                with self.assertRaises(ValueError):diag.run_check(provider,symbol)

    def test_actual_wall_deadline_interrupts_stalled_get_and_restores_handler(self):
        real_deadline=diag.deadline
        original_handler=diag.signal.getsignal(diag.signal.SIGALRM)
        class Stalled(Session):
            def get(self,*args,**kwargs):
                time.sleep(2)
                raise AssertionError('wall deadline did not interrupt')
        with patch.object(diag,'deadline',side_effect=lambda _:real_deadline(.03)):
            record=diag.run_check('promax','510300.SH',now=NOW,secret_reader=lambda _:SECRET,session_factory=lambda:Stalled([]))
        self.assertEqual([x['outcome'] for x in record['checks']],['timeout','timeout'])
        self.assertEqual(diag.signal.getitimer(diag.signal.ITIMER_REAL)[0],0)
        self.assertEqual(diag.signal.getsignal(diag.signal.SIGALRM),original_handler)

    def test_immutable_reopen_and_invalid_receipt(self):
        record,_=self.check([Response({'data':[]}),Response({'data':[]})])
        with tempfile.TemporaryDirectory() as tmp:
            store=diag.SourceChecks(tmp);saved=store.save(record)
            self.assertEqual(store.save(record),saved)
            self.assertEqual(diag.SourceChecks(tmp).detail(saved['check_id']),saved)
            self.assertEqual(len(list(store.root.glob('*.json'))),1)
            for key in ('paper_admission','coverage_complete'):
                bad=deepcopy(record);bad[key]=True
                with self.assertRaises(ValueError):store.save(bad)
            bad=deepcopy(record);bad['secret']=SECRET
            with self.assertRaises(ValueError):store.save(bad)
            path=store.root/(saved['check_id']+'.json');path.write_text('{}')
            with self.assertRaises(ValueError):store.detail(saved['check_id'])

    def test_receipt_symlink_and_traversal_rejected(self):
        record,_=self.check([Response({'data':[]}),Response({'data':[]})])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'private';target.mkdir();(root/'source_checks').symlink_to(target,target_is_directory=True)
            with self.assertRaises(ValueError):diag.SourceChecks(root).save(record)
            self.assertEqual(list(target.iterdir()),[])
            with self.assertRaises(ValueError):diag.SourceChecks(root).detail('../private')

    def test_cli_reopens_receipt_and_controls_errors(self):
        record,_=self.check([Response({'data':[]}),Response({'data':[]})])
        with tempfile.TemporaryDirectory() as tmp:
            saved=diag.SourceChecks(tmp).save(record)
            command=[sys.executable,'-m','research_center.cli','--store',tmp,'source','show']
            result=subprocess.run(command+[saved['check_id']],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),saved)
            bad=subprocess.run(command+['../private'],capture_output=True,text=True,timeout=5)
            self.assertEqual(bad.returncode,2);self.assertNotIn('Traceback',bad.stderr)
            self.assertNotIn(SECRET,result.stdout+result.stderr+bad.stderr)

if __name__=='__main__':unittest.main()
