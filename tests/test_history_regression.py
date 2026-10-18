import ast,time,unittest,json,zlib
from pathlib import Path
source=(Path(__file__).resolve().parents[1]/'server.py').read_text()
tree=ast.parse(source)
names={'_entry_has_internal_gaps','_is_fresh','_warm_needed','_series_ready','_readiness_reason','_cache_data'}
code=compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'<server39>','exec')
class Tests(unittest.TestCase):
 def setUp(self):
  self.entry={'ts':time.time()-22000,'expected':'2026-10-15','coverage_end':'2026-10-15','after_close':True,'data':{'data_quality':{'missing_internal_sessions':['2026-10-14']}}}
  self.g=dict(time=time,json=json,zlib=zlib,MIN_REFETCH_S=60,LAG_REFETCH_S=21600,NO_CAL_TTL=3600,DEFAULT_PERIOD='2y',_cache_get=lambda k:self.entry,_series_key=lambda *args:'key',_calendar_code=lambda t:'XLON',_last_completed=lambda c:('2026-10-15',None),_close_settled=lambda c:True)
  exec(code,self.g)
 def test_gap_refetched_even_when_last_session_present(self):
  self.assertFalse(self.g['_is_fresh'](self.entry,'XLON','2026-10-15','1d',None));self.assertTrue(self.g['_warm_needed']('X'))
 def test_gap_backoff_avoids_repeated_requests(self):
  self.entry['ts']=time.time()-100
  self.assertTrue(self.g['_is_fresh'](self.entry,'XLON','2026-10-15','1d',None));self.assertFalse(self.g['_warm_needed']('X'))
 def test_compressed_legacy_cache_detects_gap(self):
  self.entry['blob']=zlib.compress(json.dumps(self.entry.pop('data')).encode());self.assertTrue(self.g['_entry_has_internal_gaps'](self.entry))
 def test_complete_series_kept_cached(self):
  self.entry['data']['data_quality']['missing_internal_sessions']=[]
  self.assertTrue(self.g['_is_fresh'](self.entry,'XLON','2026-10-15','1d',None));self.assertFalse(self.g['_warm_needed']('X'))
 def test_scanner_readiness_distinct_from_risk_integrity(self):
  self.assertTrue(self.g['_series_ready']('X'));self.assertTrue(self.g['_entry_has_internal_gaps'](self.entry))
 def test_closed_explicit_range_not_pinned_with_gap(self):
  self.assertFalse(self.g['_is_fresh'](self.entry,'XLON','2026-10-15','1d','2026-10-16'))
if __name__=='__main__':unittest.main()

class RepairTests(unittest.TestCase):
 def setup_raw(self):
  import copy,math
  from datetime import date,timedelta
  nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_repair_recent_gaps']
  self.raw={'bars':[dict(date='2026-10-13',open=10,high=11,low=9,close=10,volume=10),dict(date='2026-10-15',open=12,high=13,low=11,close=12,volume=10)],'dropped':[{'date':'2026-10-14','reason':'close missing'}],'actions':[],'currency':'EUR','tz':'Europe/London','adj_method':'adjusted'}
  self.extra=copy.deepcopy(self.raw);self.extra['bars'][1]['date']='2026-10-14'
  self.g=dict(date=date,timedelta=timedelta,math=math,_utc_stamp=lambda:'checked',_fetch_yahoo_chart=lambda *a:self.extra)
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'<repair>','exec'),self.g)
 def test_bounded_actual_data_recovers_without_overwriting(self):
  self.setup_raw();r=self.g['_repair_recent_gaps']('X','2y','1d',None,None,self.raw,0)
  self.assertEqual([b['date'] for b in r['bars']],['2026-10-13','2026-10-14','2026-10-15']);self.assertEqual(r['bars'][-1]['close'],12);self.assertEqual(r['gap_repair']['status'],'RECOVERED')
 def test_inconsistent_adjustment_rejected(self):
  self.setup_raw();self.extra['bars'][0]['close']=9
  r=self.g['_repair_recent_gaps']('X','2y','1d',None,None,self.raw,0);self.assertEqual(len(r['bars']),2);self.assertFalse(r['gap_repair']['overlap_consistent'])
 def test_source_error_preserves_block(self):
  self.setup_raw()
  def fail(*a):raise ValueError('unavailable')
  self.g['_fetch_yahoo_chart']=fail
  r=self.g['_repair_recent_gaps']('X','2y','1d',None,None,self.raw,0);self.assertEqual(len(r['dropped']),1);self.assertEqual(r['gap_repair']['status'],'FAILED')
