import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ['WARMER']='0'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
import pandas as pd


def series(n=300,source='Yahoo_Finance_Storico',current=True):
    cal=server._get_cal('XLON');dates=cal.sessions_in_range(pd.Timestamp('2024-01-01'),pd.Timestamp('2026-10-16'))[-n:]
    return {'ticker':'EIMI.L','source':source,'currency':'USD','adjusted':True,'interval':'1d',
      'instrument_type':'ETF','duplicates_present':False,'exchange_timezone':'Europe/London','calendar':'XLON',
      'data_current':current,'storico_provvisorio':not current,'data_quality':{'calendar_validated':True},
      'history':[{'date':d.strftime('%Y-%m-%d'),'open':10,'high':11,'low':9,'close':10,'volume':100} for d in dates]}


class Tests(unittest.TestCase):
    def setUp(self):server._ETF_SOURCE_CACHE.clear()
    def run_selection(self,yahoo,broker=None):
        def y(*a,**k):
            if isinstance(yahoo,Exception):raise yahoo
            return copy.deepcopy(yahoo)
        def b(*a,**k):
            if isinstance(broker,Exception):raise broker
            return copy.deepcopy(broker)
        with patch.object(server,'_load_series',side_effect=y),patch.object(server,'_load_etoro_series',side_effect=b) as mock:
            out=server._load_etf_series('EIMI.L','2y','1d',None,None,True,True,False)
        return out,mock
    def test_current_long_yahoo_avoids_short_broker(self):
        p,m=self.run_selection(series(),series(212,'ETORO_DAILY_BID'));self.assertEqual(p['source'],'Yahoo_Finance_Storico');m.assert_not_called()
    def test_broker_fallback_on_yahoo_failure(self):
        p,m=self.run_selection(ValueError('upstream'),series(260,'ETORO_DAILY_BID'));self.assertEqual(p['source'],'ETORO_DAILY_BID')
    def test_old_yahoo_not_selected_over_current_broker(self):
        p,m=self.run_selection(series(current=False),series(260,'ETORO_DAILY_BID'));self.assertEqual(p['source'],'ETORO_DAILY_BID')
    def test_best_current_provisional_sample_keeps_warning(self):
        p,m=self.run_selection(series(230),series(212,'ETORO_DAILY_BID'));self.assertEqual(p['data_quality']['risk_sample_status'],'PROVVISORIO_200_249');self.assertEqual(p['source'],'Yahoo_Finance_Storico')
    def test_neither_source_sufficient_raises(self):
        with self.assertRaisesRegex(ValueError,'Nessuna serie ETF valida'):self.run_selection(series(199),series(190,'ETORO_DAILY_BID'))
    def test_calendar_hole_loses_both_adjacent_returns(self):
        p=series(300);p['history'].pop(100);q=server._etf_history_quality(p,'EIMI.L');self.assertEqual(q['valid_return_pairs'],297)
    def test_no_source_splicing(self):
        y=series(230);b=series(212,'ETORO_DAILY_BID');b['history'][0]['close']=20
        p,m=self.run_selection(y,b);self.assertEqual(p['history'],y['history']);self.assertFalse(p['history_source_selection']['sources_spliced'])
    def test_currency_mismatch_not_accepted(self):
        y=series();y['currency']='EUR'
        p,m=self.run_selection(y,series(260,'ETORO_DAILY_BID'));self.assertEqual(p['source'],'ETORO_DAILY_BID')
    def test_duplicate_dates_rejected(self):
        p=series();p['history'].append(copy.deepcopy(p['history'][-1]))
        with self.assertRaisesRegex(ValueError,'Date storico'):server._etf_history_quality(p,'EIMI.L')
    def test_successful_selection_cached_without_new_fetch(self):
        p,m=self.run_selection(series())
        with patch.object(server,'_load_series',side_effect=AssertionError('no repeated request')):
            cached=server._load_etf_series('EIMI.L','2y','1d',None,None,True,True,False)
        self.assertTrue(cached['cache_hit']);self.assertEqual(cached['history'],p['history'])
    def test_served_short_slice_is_not_marked_sufficient(self):
        p=series();p['history_source_selection']={'version':'test'};p['actions']=[];p['coverage_end']='2026-10-16';p['data_quality']['risk_sample_sufficient']=True
        with patch.object(server,'_load_series',return_value=p):out=server.get_market_data('EIMI.L',max_bars=30)
        self.assertFalse(out['data_quality']['risk_sample_sufficient']);self.assertEqual(out['data_quality']['valid_return_pairs'],29)


if __name__=='__main__':unittest.main()
