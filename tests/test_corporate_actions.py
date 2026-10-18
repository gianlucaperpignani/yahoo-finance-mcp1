import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from etoro_corporate_actions import build_adjustments
class Actions(unittest.TestCase):
 def setUp(self):
  self.days=['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16']
  self.entry={'instrument':{'instrumentId':3434,'symbol':'CSPX.L'},'currency':{'code':'USD'},'corporate_actions':{'verified':True,'coverage_complete':True,'instrument_id':3434,'ticker':'CSPX.L','currency':'USD','raw_price_basis':'UNADJUSTED','raw_price_basis_reference':'SYNTHETIC TEST','reference':'SYNTHETIC TEST','checked_at':'2026-10-17T00:00:00Z','coverage_start':self.days[0],'coverage_end':self.days[-1],'events':[]}}
  self.response={'body':{'interval':'OneDay','candles':[{'instrumentId':3434,'candles':[{'instrumentID':3434,'fromDate':d+'T00:00:00Z','close':100} for d in self.days]}]}}
  self.cal={'sessions':[{'date':d,'close_timestamp':d+'T16:00:00Z'} for d in self.days]}
 def build(self):return build_adjustments(self.entry,self.response,self.cal,'2026-10-17T12:00:00Z')
 def test_verified_empty_ledger_neutral_factors(self):self.assertEqual(set(self.build()['factors_by_date'].values()),{1})
 def test_unverified_empty_never_accepted(self):
  self.entry['corporate_actions']['verified']=False
  with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):self.build()
 def test_no_events_is_not_default(self):
  self.entry['corporate_actions']['events']=None
  with self.assertRaisesRegex(ValueError,'EVENTS_MISSING'):self.build()
 def test_split_two_for_one(self):
  self.entry['corporate_actions']['events']=[{'type':'SPLIT','ex_date':self.days[2],'new_shares':2,'old_shares':1,'reference':'SYNTHETIC TEST'}]
  f=self.build()['factors_by_date'];self.assertEqual(f[self.days[0]],0.5);self.assertEqual(f[self.days[2]],1)
 def test_cash_distribution(self):
  self.entry['corporate_actions']['events']=[{'type':'CASH_DISTRIBUTION','ex_date':self.days[2],'amount_per_share':2,'currency':'USD','reference':'SYNTHETIC TEST'}]
  self.assertEqual(self.build()['factors_by_date'][self.days[0]],0.98)
 def test_missing_previous_close_blocks_dividend(self):
  self.response['body']['candles'][0]['candles'].pop(1)
  self.entry['corporate_actions']['events']=[{'type':'CASH_DISTRIBUTION','ex_date':self.days[2],'amount_per_share':2,'currency':'USD','reference':'SYNTHETIC TEST'}]
  with self.assertRaisesRegex(ValueError,'PREVIOUS_SESSION'):self.build()
 def test_expired_coverage(self):
  self.entry['corporate_actions']['coverage_end']=self.days[-2]
  with self.assertRaisesRegex(ValueError,'EXPIRED'):self.build()
 def test_wrong_identity(self):
  self.entry['corporate_actions']['instrument_id']=15435
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.build()
 def test_double_event_requires_reconciliation(self):
  event={'type':'SPLIT','ex_date':self.days[2],'new_shares':2,'old_shares':1,'reference':'SYNTHETIC TEST'}
  self.entry['corporate_actions']['events']=[event,event]
  with self.assertRaisesRegex(ValueError,'SAME_DATE'):self.build()
 def test_adjusted_basis_not_double_adjusted(self):
  self.entry['corporate_actions']['raw_price_basis']='ADJUSTED'
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.build()
if __name__=='__main__':unittest.main()
