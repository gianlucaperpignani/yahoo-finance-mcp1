import ast,math,os,unittest
from pathlib import Path
from datetime import date,datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import pandas as pd
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'server.py').read_text())
NAMES={'_risk_returns','_risk_sample','_var_es','_close_series_usd','_load_risk_series'}
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES],type_ignores=[]),'actual-risk-server','exec')
class Calendar:
 def sessions_in_range(self,a,b):return pd.bdate_range(a,b)
class RiskPairs(unittest.TestCase):
 def setUp(self):
  self.g=dict(pd=pd,np=np,math=math,os=os,date=date,datetime=datetime,ZoneInfo=ZoneInfo,finite=float,
              _pct=lambda x:round(x*100,6),_rnd=lambda x,n=6:round(x,n),
              _get_cal=lambda code:Calendar(),_session_gap=lambda p:0,_verified_etf=lambda p:True)
  exec(CODE,self.g)
 def series(self,dates,values,sessions=None):
  s=pd.Series(values,index=dates,dtype=float);s.attrs['expected_sessions']=sessions or dates;return s
 def test_internal_missing_no_bridge(self):
  days=['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16']
  s=self.series([days[i] for i in [0,1,3,4]],[100,101,110,111],days)
  r=self.g['_risk_returns'](s)
  self.assertEqual(list(r.index),[(days[0],days[1]),(days[3],days[4])])
  self.assertNotIn((days[1],days[3]),r.index)
 def test_different_holidays_do_not_align_on_end_date_only(self):
  a=self.series(['2026-10-12','2026-10-13','2026-10-14'],[100,110,120])
  b=self.series(['2026-10-12','2026-10-14'],[100,120])
  r=self.g['_risk_sample']({'A':a,'B':b},['A','B'])
  self.assertEqual(len(r),0)
 def test_missing_last_predecessor_blocks(self):
  s=self.series(['2026-10-12','2026-10-14'],[100,120],['2026-10-12','2026-10-13','2026-10-14'])
  with self.assertRaisesRegex(ValueError,'Ultimo rendimento'):self.g['_risk_returns'](s)
 def test_weekly_excludes_cross_gap_windows(self):
  dates=pd.bdate_range('2026-01-01',periods=50).strftime('%Y-%m-%d').tolist()
  s=self.series([d for i,d in enumerate(dates) if i!=20],[100+i for i in range(50) if i!=20],dates)
  r=self.g['_risk_sample']({'A':s},['A'])
  out=self.g['_var_es'](r,np.array([1.0]),1000,1.1)
  expected=sum(all(a[1]==b[0] for a,b in zip(list(r.index)[i-4:i+1],list(r.index)[i-3:i+1])) for i in range(4,len(r)))
  self.assertEqual(out['n_5d'],expected)
  self.assertLess(out['n_5d'],len(r)-4)
 def test_complete_sample_unchanged(self):
  dates=pd.bdate_range('2026-01-01',periods=50).strftime('%Y-%m-%d').tolist()
  s=self.series(dates,[100+i for i in range(50)])
  r=self.g['_risk_sample']({'A':s},['A'])
  out=self.g['_var_es'](r,np.array([1.0]),1000,1.1)
  self.assertEqual(out['n_1d'],49);self.assertEqual(out['n_5d'],45)
 def test_close_accepts_internal_holes_without_screening_override(self):
  p={'screening_history_accepted':False,'adjusted':True,'calendar':'XLON','currency':'USD','data_current':True,'data_quality':{'calendar_validated':True},'history':[{'date':d,'close':p} for d,p in [('2026-10-12',100),('2026-10-14',101),('2026-10-15',102),('2026-10-16',103)]]}
  s,_=self.g['_close_series_usd'](p)
  self.assertEqual(len(self.g['_risk_returns'](s)),2)
 def test_non_usd_uses_ecb_no_fill(self):
  p={'adjusted':True,'calendar':'XLON','currency':'EUR','data_current':True,'data_quality':{'calendar_validated':True},'history':[{'date':d,'close':100} for d in ['2026-10-12','2026-10-13','2026-10-14']]}
  self.g['acquire_ecb_history']=Mock(return_value={'status':'PREPARATO_DA_VALIDARE_NEL_MOTORE','fx_to_usd_by_date':{'2026-10-12':{'EUR':1.1},'2026-10-14':{'EUR':1.2}}})
  self.g['target_open']=lambda d:True
  with self.assertRaisesRegex(ValueError,'Tasso BCE mancante'):self.g['_close_series_usd'](p)
 def test_ecb_nonpublication_excluded_and_cannot_bridge(self):
  dates=['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16']
  p={'adjusted':True,'calendar':'XLON','currency':'EUR','data_current':True,'data_quality':{'calendar_validated':True},'history':[{'date':d,'close':100} for d in dates]}
  self.g['acquire_ecb_history']=lambda *a:{'status':'PREPARATO_DA_VALIDARE_NEL_MOTORE','fx_to_usd_by_date':{d:{'EUR':1.1} for d in dates if d!=dates[2]}}
  self.g['target_open']=lambda d:d.isoformat()!=dates[2]
  s,fx=self.g['_close_series_usd'](p)
  self.assertEqual(s.attrs['fx_excluded_dates'],[dates[2]])
  self.assertEqual(len(self.g['_risk_returns'](s)),2)
 def test_raw_series_rejected(self):
  with self.assertRaisesRegex(ValueError,'Rettifiche'):self.g['_close_series_usd']({'adjusted':False,'calendar':'XLON','data_quality':{'calendar_validated':True}})
 def test_etoro_gate_does_not_fallback_or_fabricate(self):
  self.g['ETORO_ETF_MAP']={'CSPX.L':{}}
  self.g['etoro_history']=type('Broker',(),{'risk_enabled':staticmethod(lambda:True)})
  self.g['get_etf_history']=lambda t:{'usable_for_risk':False,'risk_blockers':['RETTIFICHE_SPLIT_DIVIDENDI_NON_VERIFICATE']}
  self.g['_load_series']=Mock(side_effect=ValueError('RETTIFICHE_NON_VERIFICATE'))
  with self.assertRaisesRegex(ValueError,'RETTIFICHE'):self.g['_load_risk_series']('CSPX.L')
 def test_yahoo_risk_default_kept_for_unactivated_source(self):
  self.g['ETORO_ETF_MAP']={'CSPX.L':{}}
  self.g['etoro_history']=type('Broker',(),{'risk_enabled':staticmethod(lambda:False)})
  self.g['_load_series']=lambda t,**kwargs:{'source':'Yahoo'}
  self.assertEqual(self.g['_load_risk_series']('CSPX.L')['source'],'Yahoo')
 def test_portfolio_and_candidate_use_pair_sample(self):
  fn=next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='portfolio_risk')
  calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call)]
  self.assertEqual(sum(isinstance(n.func,ast.Name) and n.func.id=='_risk_sample' for n in calls),2)
  self.assertFalse(any(isinstance(n.func,ast.Attribute) and n.func.attr=='pct_change' for n in calls))
if __name__=='__main__':unittest.main()

class PortfolioIntegration(unittest.TestCase):
 setUp=RiskPairs.setUp
 def test_complete_portfolio_and_candidate_function(self):
  import hashlib,json
  from policy_contract import T
  self.g['T']=T
  names={'portfolio_risk','_corr_info'}
  nodes=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in names]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual-portfolio','exec'),self.g)
  self.g.update(hashlib=hashlib,_validate_ticker=lambda t:t,_register_extra=lambda t:None,
                _json_text=lambda d:json.dumps(d,sort_keys=True),VERSION='1.7.42',
                RISK_ENGINE_VERSION='server-risk 1.1.0',_utc_stamp=lambda:'SYNTHETIC TEST')
  days=pd.bdate_range('2025-01-01',periods=310).strftime('%Y-%m-%d').tolist()
  def loader(t):
   rows=[{'date':d,'close':100+0.1*i+np.sin(i)*0.3} for i,d in enumerate(days) if i!=100]
   return {'ticker':t,'adjusted':True,'calendar':'XLON','data_current':True,'currency':'USD',
           'coverage_end':days[-1],'source':'SYNTHETIC TEST','data_quality':{'calendar_validated':True},'history':rows}
  self.g['_load_risk_series']=loader
  r=self.g['portfolio_risk'](10000,2000,[{'ticker':'AAA','value_usd':8000}],candidate={'ticker':'BBB','amount_usd':100},eurusd=1.1)
  self.assertEqual(r['sample']['returns_common'],307)
  self.assertEqual(r['candidate']['sample_returns'],307)
  self.assertTrue(r['sample']['no_bridge_returns'])
  self.assertEqual(r['var_es']['n_5d'],299)
  self.assertTrue(r['coverage']['history_sources']['AAA']['source']=='SYNTHETIC TEST')
  self.assertFalse(r['candidate']['esito']=='BLOCCATO PER DATI')
