import copy,sys,unittest
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from etoro_dual_source import build
class DualSource(unittest.TestCase):
 def setUp(self):
  self.days=['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16']
  # Synthetic complete 100-session calendar, no reliance on live prices.
  import pandas as pd
  self.days=pd.bdate_range(end='2026-10-16',periods=100).strftime('%Y-%m-%d').tolist()
  self.cal={'start':self.days[0],'end':'2026-10-18','timezone':'Europe/London','sessions':[{'date':d,'close_timestamp':d+'T16:00:00Z'} for d in self.days]}
  self.entry={'instrument':{'instrumentId':3434,'symbol':'CSPX.L'},'currency':{'code':'USD'}}
  self.response={'body':{'interval':'OneDay','candles':[{'instrumentId':3434,'candles':[{'instrumentID':3434,'fromDate':d+'T00:00:00Z','close':100} for d in self.days]}]}}
  stamps=[int(datetime.fromisoformat(d+'T12:00:00+00:00').timestamp()) for d in self.days]
  self.yahoo={'retrieved_at':'2026-10-17T12:00:00Z','query_start':self.days[0],'query_end':'2026-10-18','url':'https://query1.finance.yahoo.com/v8/finance/chart/CSPX.L?events=div%2Csplits','document':{'chart':{'error':None,'result':[{'meta':{'symbol':'CSPX.L','instrumentType':'ETF','currency':'USD','exchangeTimezoneName':'Europe/London'},'timestamp':stamps,'indicators':{'quote':[{'close':[100.0]*100}],'adjclose':[{'adjclose':[100.0]*100}]}}]}}}
 def run_build(self):return build(self.entry,self.response,self.cal,'2026-10-17T12:00:10Z',self.yahoo)
 def test_none_reported_accepted_with_disclosed_policy(self):
  factors,policy=self.run_build()
  self.assertEqual(set(factors['factors_by_date'].values()),{1})
  self.assertFalse(policy['issuer_certified'])
  self.assertEqual(policy['yahoo_events_reported'],0)
  self.assertIn('not guaranteed absence',' '.join(policy['limitations']))
 def test_identity_mismatch(self):
  self.yahoo['document']['chart']['result'][0]['meta']['symbol']='SXR8.DE'
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.run_build()
 def test_currency_mismatch(self):
  self.yahoo['document']['chart']['result'][0]['meta']['currency']='GBP'
  with self.assertRaisesRegex(ValueError,'CURRENCY'):self.run_build()
 def test_expired_snapshot(self):
  self.yahoo['retrieved_at']='2026-10-17T10:00:00Z'
  with self.assertRaisesRegex(ValueError,'EXPIRED'):self.run_build()
 def test_incomplete_query(self):
  self.yahoo['query_end']='2026-10-16'
  with self.assertRaisesRegex(ValueError,'QUERY_RANGE'):self.run_build()
 def test_price_confirmation_more_than_one_session_old(self):
  p=self.yahoo['document']['chart']['result'][0]
  p['timestamp']=p['timestamp'][:-2];p['indicators']['quote'][0]['close']=p['indicators']['quote'][0]['close'][:-2];p['indicators']['adjclose'][0]['adjclose']=p['indicators']['adjclose'][0]['adjclose'][:-2]
  with self.assertRaisesRegex(ValueError,'TOO_OLD'):self.run_build()
 def test_yahoo_error_not_empty_success(self):
  self.yahoo['document']['chart']['error']={'code':'Not Found'}
  with self.assertRaisesRegex(ValueError,'RESPONSE'):self.run_build()
 def test_adjustment_contradiction(self):
  self.yahoo['document']['chart']['result'][0]['indicators']['adjclose'][0]['adjclose'][0]=90
  with self.assertRaisesRegex(ValueError,'DISAGREES'):self.run_build()
 def test_actual_reported_split_applies_to_broker_prices(self):
  ex=self.days[50];stamp=int(datetime.fromisoformat(ex+'T12:00:00+00:00').timestamp())
  self.yahoo['document']['chart']['result'][0]['events']={'splits':{str(stamp):{'date':stamp,'numerator':2,'denominator':1}}}
  f,p=self.run_build();self.assertEqual(f['factors_by_date'][self.days[0]],0.5);self.assertEqual(f['factors_by_date'][ex],1)
 def test_unsupported_event_not_silently_ignored(self):
  self.yahoo['document']['chart']['result'][0]['events']={'capitalGains':{'1':{'amount':1}}}
  with self.assertRaisesRegex(ValueError,'UNSUPPORTED'):self.run_build()
if __name__=='__main__':unittest.main()
