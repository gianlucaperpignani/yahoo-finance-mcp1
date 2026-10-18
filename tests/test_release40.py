"""Release 1.7.40: actual functions isolated from HTTP, Yahoo and the warmer."""
import ast, copy, json, math, re, unittest, unicodedata
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'server.py').read_text())
NAMES={'_history_continuity','_apply_history_policy','_session_gap','_verified_etf','verify_instrument_identity','_identity_name_tokens','_fetch_stooq_fallback','_try_stooq_gap'}
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES],type_ignores=[]),'<actual release40>','exec')
class Calendar:
 def sessions_in_range(self,a,b):return pd.bdate_range(a,b)
class ReleaseTests(unittest.TestCase):
 def setUp(self):
  self.g=dict(pd=pd,re=re,math=math,copy=copy,unicodedata=unicodedata,ETF_PRODUCTS={'CSPX.L':{'isin':'IE00B5BMR087'}},ETF_TOLERANCE_SUFFIXES=('.L','.DE','.PA','.MI','.AS','.MC','.SW','.ST','.CO','.OL','.HE'),PROVISIONAL_WARNING='STORICO_PROVVISORIO: ultima barra T-1, accettata con tolleranza 1 seduta',_get_cal=lambda c:Calendar(),ETORO_ISIN_MAP=json.loads((ROOT/'etoro_isin_map.json').read_text()),STOOQ_ETF_MAP={})
  exec(compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="_IDENTITY_LEGAL_TOKENS" for t in n.targets)],type_ignores=[]),"<actual constants>","exec"),self.g)
  exec(CODE,self.g)
 def payload(self,t='CSPX.L',end='2026-10-15',expected='2026-10-16'):
  bars=[dict(date=d.strftime('%Y-%m-%d'),open=10,high=11,low=9,close=10,volume=20) for d in pd.bdate_range(end=end,periods=260)]
  return dict(ticker=t,instrument_type='ETF' if t.endswith('.L') else 'EQUITY',interval='1d',calendar='XLON',coverage_end=end,last_completed_session_expected=expected,data_current=end==expected,data_quality={'calendar_validated':True,'missing_internal_sessions':[expected] if end!=expected else []},history=bars,warnings=[])
 def test_etf_t1_provisional_accepted_not_current(self):
  p=self.g['_apply_history_policy'](self.payload());self.assertTrue(p['storico_provvisorio']);self.assertTrue(p['screening_history_accepted']);self.assertFalse(p['data_current']);self.assertEqual(p['data_quality']['stale_sessions'],1);self.assertEqual(p['warnings'],[self.g['PROVISIONAL_WARNING']])
 def test_us_stock_t1_blocked(self):self.assertFalse(self.g['_apply_history_policy'](self.payload('AAPL'))['screening_history_accepted'])
 def test_european_stock_not_etf(self):
  p=self.payload('SHEL.L');p['instrument_type']='EQUITY';self.assertFalse(self.g['_apply_history_policy'](p)['screening_history_accepted'])
 def test_gap_two_blocked(self):self.assertFalse(self.g['_apply_history_policy'](self.payload(end='2026-10-14'))['screening_history_accepted'])
 def test_weekend_is_not_a_session(self):self.assertEqual(self.g['_session_gap'](self.payload(end='2026-10-16',expected='2026-10-19')),1)
 def test_future_not_accepted(self):self.assertFalse(self.g['_apply_history_policy'](self.payload(end='2026-10-19'))['screening_history_accepted'])
 def test_internal_hole_stays_blocked(self):
  p=self.payload();p['data_quality']['missing_internal_sessions']+=['2026-10-13'];p['history']=[b for b in p['history'] if b['date']!='2026-10-13'];self.assertFalse(self.g['_apply_history_policy'](p)['screening_history_accepted'])
 def test_current_accepted(self):self.assertTrue(self.g['_apply_history_policy'](self.payload(end='2026-10-16'))['screening_history_accepted'])
 def test_unverified_calendar_does_not_allow_tolerance(self):
  p=self.payload();p['data_quality']['calendar_validated']=False;self.assertFalse(self.g['_apply_history_policy'](p)['screening_history_accepted'])
 def test_isin_match_beats_names(self):
  r=self.g['verify_instrument_identity']('T','AT&T','US00206R1023','T','Different','US00206R1023');self.assertEqual(r['status'],'PASS');self.assertEqual(r['reason'],'ISIN coincidente, nome diverso accettato')
 def test_isin_discordance_name_fallback(self):self.assertEqual(self.g['verify_instrument_identity']('AAPL','Apple','AAA','AAPL','Apple','BBB')['status'],'PASS')
 def test_crypto_ambiguity_not_bypassed_by_static_map(self):self.assertEqual(self.g['verify_instrument_identity']('T','AT&T Inc',None,'T','Threshold Network',None)['status'],'FAIL')
 def test_missing_identity_blocked(self):self.assertEqual(self.g['verify_instrument_identity']('AAPL',etoro_ticker='AAPL')['status'],'BLOCCATO_PER_DATI')
 def test_stooq_requires_verified_mapping(self):
  with self.assertRaisesRegex(ValueError,'non verificato'):self.g['_fetch_stooq_fallback']('CSPX.L')
 def test_stooq_failure_preserves_original_bars(self):
  p=self.payload(end='2026-10-14');r=self.g['_try_stooq_gap'](p);self.assertEqual(r['history'],p['history']);self.assertIn('non utilizzabile',r['warnings'][-1])
 def test_stooq_not_called_for_us(self):
  p=self.payload('AAPL',end='2026-10-14');self.assertEqual(self.g['_try_stooq_gap'](p),p)
 def test_secondary_source_never_ticket_current(self):
  p=self.payload(end='2026-10-16');p['secondary_source_used']=True;r=self.g['_apply_history_policy'](p);self.assertFalse(r['data_current']);self.assertTrue(r['storico_provvisorio'])
 def stooq_fixture(self):
  p=self.payload(end='2026-10-14');p.update(currency='USD',adjustment_method='adjusted')
  p['history']=[dict(date=d,open=10,high=11,low=9,close=10,volume=20) for d in ['2026-10-12','2026-10-13','2026-10-14']]
  p['data_quality']['missing_internal_sessions']=['2026-10-15','2026-10-16']
  extra=dict(mapping=dict(isin='IE00B5BMR087',currency='USD',price_basis='adjusted'),bars=p['history']+[dict(date='2026-10-15',open=10,high=11,low=9,close=10,volume=20)],url='synthetic-fixture',fetched_at='synthetic-time')
  self.g['_fetch_stooq_fallback']=lambda t:extra
  return p,extra
 def test_verified_fallback_recovers_one_bar_without_bridge(self):
  p,extra=self.stooq_fixture();r=self.g['_apply_history_policy'](self.g['_try_stooq_gap'](p));self.assertEqual(r['coverage_end'],'2026-10-15');self.assertFalse(r['screening_history_accepted']);self.assertEqual(r['contiguous_tail_bars'],4);self.assertFalse(r['data_current']);self.assertEqual(len(r['history']),4);self.assertEqual(len(p['history']),3)
 def test_fallback_wrong_currency_rejected(self):
  p,extra=self.stooq_fixture();extra['mapping']['currency']='GBP';r=self.g['_try_stooq_gap'](p);self.assertEqual(r['coverage_end'],'2026-10-14');self.assertIn('incompatibili',r['warnings'][-1])
 def test_ttl_and_reconciliation_constants(self):
  from policy_contract import T
  s=(ROOT/'server.py').read_text();self.assertIn('_LEVEL_B_TTL = 900',s);self.assertEqual(T['reconciliation_equity_fraction'],0.0025);self.assertIn('T["reconciliation_equity_fraction"] * equity',s);self.assertIn('and not ind.get("storico_provvisorio")',s)
 def test_level_b_versions_limits(self):
  import level_b
  ns=vars(level_b)
  self.assertEqual([ns[k] for k in ['A1_MAX_HISTORY_CHECKS','A2_MAX_HISTORY_CHECKS','A1_MAX_RESULTS','A2_MAX_RESULTS']],[12,15,8,8]);self.assertEqual(ns['A2_VERSION'],'a2-price-momentum-0.6-tolerance')
if __name__=='__main__':unittest.main()
