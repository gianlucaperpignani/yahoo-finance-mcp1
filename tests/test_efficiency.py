import json,sys,threading,time,unittest,zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from archive_client import ArchiveError
from archive_runtime import ArchiveRuntime
from price_cache_store import PriceCache,MAX_RAM_BYTES
from test_archive_runtime import Backend as ProbeBackend
from test_price_cache_store import Backend,entry,NOW

class EfficiencyTests(unittest.TestCase):
 def wait(self,r):
  for _ in range(200):
   if r.status()['state']!='RUNNING':return
   time.sleep(.002)
  self.fail('worker stuck')
 def test_transient_probe_retried_same_object_permanent_errors_not_retried(self):
  clock=[0];b=ProbeBackend();r=ArchiveRuntime(lambda:b,lambda:{'configured':True},clock=lambda:clock[0])
  with patch.object(b,'status',side_effect=ArchiveError('ARCHIVE_REMOTE_REJECTED:GATEWAY_FAILED')):r.start();self.wait(r)
  self.assertEqual(r.status()['state'],'ERROR');r.start();self.assertEqual(r.attempts,1)
  clock[0]=61;r.start();self.wait(r)
  self.assertEqual(r.status()['state'],'PASS');self.assertEqual(b.puts,1);self.assertEqual(r.attempts,2)
  r=ArchiveRuntime(lambda:b,lambda:{'configured':True},clock=lambda:clock[0])
  with patch.object(b,'status',side_effect=ArchiveError('ARCHIVE_RESPONSE_SIGNATURE_INVALID')):r.start();self.wait(r)
  clock[0]+=1000;r.start();self.assertEqual(r.attempts,1)
 def test_transient_probe_stops_after_three_attempts(self):
  clock=[0];b=ProbeBackend();r=ArchiveRuntime(lambda:b,lambda:{'configured':True},clock=lambda:clock[0])
  with patch.object(b,'status',side_effect=ArchiveError('ARCHIVE_REMOTE_REJECTED:GATEWAY_FAILED')):
   for _ in range(6):r.start();self.wait(r);clock[0]+=1000
  self.assertEqual(r.attempts,3);self.assertEqual(r.status()['state'],'ERROR')
 def test_full_universe_persistence_and_bounded_restore_resumes_buckets(self):
  import server
  b=Backend();lock=threading.Lock();cache={}
  # Synthetic technical load, never quotes, financial observations or live coverage.
  bars=[dict(date='synthetic-'+str(i),open=i+100,high=i+102,low=i+99,close=i+101,volume=10000+i) for i in range(300)]
  for i in range(790):
   ticker='SYN'+str(i);v=entry(ticker);p=dict(ticker=ticker,interval='1d',coverage_end='2026-10-23',history=bars)
   v['blob']=zlib.compress(json.dumps(p).encode());v['blob_bytes']=len(v['blob']);cache[f's:{ticker}:2y:1d:None:None']=v
  self.assertLess(sum(v['blob_bytes'] for v in cache.values()),MAX_RAM_BYTES)
  first=PriceCache(clock=lambda:NOW)
  for _ in range(16):first.flush(b,cache,lock,'compat')
  self.assertTrue(all(first.is_verified(k,v) for k,v in cache.items()))
  recovered={};second=PriceCache(clock=lambda:NOW+1)
  for i in range(8):
   second.restore(b,recovered,lock,'compat',1100,budget=4)
   if i<7:self.assertFalse(second.ready)
  self.assertTrue(second.ready);self.assertEqual(recovered,cache)
 def test_status_compact_keeps_errors_and_full_is_available(self):
  import server
  d=server.get_server_status();small=server._format_response('get_server_status',d,'compact')
  self.assertLess(len(json.dumps(small)),len(json.dumps(d)))
  self.assertNotIn('runtime_manifest',small);self.assertIn('runtime_manifest_ref',small)
  self.assertEqual(small['persistent_archive'],d['persistent_archive'])
  self.assertIs(server._format_response('get_server_status',d,'full'),d)
if __name__=='__main__':unittest.main()
