import ast
import copy
import logging
import queue
import threading
import time
import unittest
from pathlib import Path

# Execute the actual asynchronous implementation, isolated from Yahoo and warmer.
tree = ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text())
names = {'_level_b_completed','_level_b_publish_fusion','_level_b_worker_loop',
         '_level_b_job','scan_level_b_fusion','scan_level_b_verification_pack'}
nodes = [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
code = compile(ast.Module(body=nodes,type_ignores=[]),'<actual-server-async>','exec')

class FusionTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.release=threading.Event()
        def compute(market, limit):
            self.calls.append(market)
            self.release.wait(1)
            return {'shortlist':[{'ticker':market+str(i)} for i in range(limit)],
                    'data_errors':[{'reason':'preserved antenna limitation'}],
                    'generated_at_utc':'original-source-time'}
        self.g=dict(time=time,threading=threading,copy=copy,queue=queue,
                    log=logging.getLogger('test'),VERSION='1.7.38',
                    _LEVEL_B_LOCK=threading.Lock(),_LEVEL_B_QUEUE=queue.Queue(4),
                    _LEVEL_B_JOBS={},_LEVEL_B_WORKER=None,_LEVEL_B_TTL=900,
                    _utc_stamp=lambda:'test-time',_compute_level_b_fusion=compute,
                    _compute_level_b_verification_pack=compute)
        exec(code,self.g)
    def tearDown(self):
        self.release.set()
        self.g['_LEVEL_B_QUEUE'].join()
    def wait_done(self,kind='fusion',market='US'):
        self.release.set()
        self.g['_LEVEL_B_QUEUE'].join()
        return self.g['_level_b_job'](kind,market,3)
    def test_slow_remote_does_not_hold_request(self):
        start=time.monotonic();r=self.g['scan_level_b_fusion']('US',3)
        self.assertLess(time.monotonic()-start,.1)
        self.assertIn(r['job_status'],['QUEUED','RUNNING'])
        self.assertFalse(r['scan_complete']);self.assertNotIn('shortlist',r)
        self.assertFalse(r['ticket_authorized'])
    def test_poll_coalesces_limits_and_preserves_errors_and_time(self):
        a=self.g['scan_level_b_fusion']('US',3)
        b=self.g['scan_level_b_fusion']('US',10)
        self.assertEqual(a['job_id'],b['job_id'])
        r=self.wait_done();self.assertEqual(self.calls,['US'])
        self.assertEqual(r['job_status'],'COMPLETE');self.assertEqual(len(r['shortlist']),3)
        self.assertEqual(r['generated_at_utc'],'original-source-time')
        self.assertTrue(r['data_errors']);self.assertFalse(r['ticket_authorized'])
    def test_us_eu_are_isolated_and_share_one_worker(self):
        us=self.g['scan_level_b_fusion']('US',3);eu=self.g['scan_level_b_fusion']('EU',3)
        self.assertNotEqual(us['job_id'],eu['job_id']);self.wait_done()
        self.assertEqual(self.calls,['US','EU'])
        self.assertTrue(self.g['scan_level_b_fusion']('EU',2)['shortlist'][0]['ticker'].startswith('EU'))
    def test_expired_result_starts_new_job(self):
        first=self.g['scan_level_b_fusion']('US',3);self.wait_done()
        self.g['_LEVEL_B_JOBS'][('fusion','US')]['finished']-=901
        second=self.g['scan_level_b_fusion']('US',3)
        self.assertNotEqual(first['job_id'],second['job_id'])
        self.assertNotIn('shortlist',second)
    def test_errors_are_explicit_not_empty_pass(self):
        def fail(*args):raise ValueError('upstream unavailable')
        self.g['_compute_level_b_fusion']=fail
        self.g['scan_level_b_fusion']('US',3);r=self.wait_done()
        self.assertEqual(r['job_status'],'ERROR');self.assertEqual(r['status'],'BLOCCATO PER DATI')
        self.assertNotIn('shortlist',r)
    def test_invalid_market_does_not_enqueue(self):
        with self.assertRaises(ValueError):self.g['scan_level_b_fusion']('ALL',3)
        self.assertEqual(len(self.g['_LEVEL_B_JOBS']),0)
    def test_pack_is_asynchronous_and_filters_symbols(self):
        def pack(*args):
            return {'candidates':[{'ticker':str(i),'yahoo_status':'VERIFICATO' if i==0 else 'BLOCCATO_PER_DATI'} for i in range(10)],'data_errors':['preserved']}
        self.g['_compute_level_b_verification_pack']=pack
        self.g['scan_level_b_verification_pack']('EU',2)
        r=self.wait_done('verification','EU')
        self.assertEqual(r['etoro_symbols'],['0']);self.assertEqual(r['yahoo_verified'],1)
        self.assertEqual(len(r['candidates']),3);self.assertTrue(r['data_errors'])
    def test_caller_cannot_mutate_cached_result(self):
        self.g['scan_level_b_fusion']('US',3);r=self.wait_done();r['shortlist'].clear()
        self.assertEqual(len(self.g['scan_level_b_fusion']('US',3)['shortlist']),3)
    def test_fusion_published_by_pack_is_reused(self):
        self.g['_level_b_publish_fusion']('EU',{'shortlist':[{'ticker':'ABC'}]})
        self.assertEqual(self.g['scan_level_b_fusion']('EU',3)['shortlist'][0]['ticker'],'ABC')
        self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
