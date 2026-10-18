"""Calendar continuity is specific to each calculation; missing bars stay missing."""
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ['WARMER'] = '0'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import pandas as pd


def series(n=320, missing=(), ticker='BARC.L', current=True):
    days = server._get_cal('XLON').sessions_in_range(pd.Timestamp('2024-01-01'), pd.Timestamp('2026-10-16'))[-n:]
    rows = [dict(date=d.strftime('%Y-%m-%d'), open=100+i/10, high=102+i/10,
                 low=99+i/10, close=101+i/10, volume=1000+i) for i,d in enumerate(days) if i not in missing]
    return dict(ticker=ticker,name=ticker,instrument_type='EQUITY',interval='1d',calendar='XLON',
                currency='USD',major_currency='USD',source='SYNTHETIC TEST',adjusted=True,
                coverage_end=rows[-1]['date'],last_completed_session_expected=days[-1].strftime('%Y-%m-%d'),
                data_current=current,history=rows,actions=[],warnings=[],
                data_quality={'calendar_validated':True,'missing_internal_sessions':[days[i].strftime('%Y-%m-%d') for i in missing]})


class OldGapTests(unittest.TestCase):
    def test_old_hole_accepts_screening_without_filling_it(self):
        p=series(missing=(20,));old=copy.deepcopy(p)
        out=server._apply_history_policy(p)
        self.assertTrue(out['screening_history_accepted'])
        self.assertEqual(out['contiguous_tail_bars'],299)
        self.assertEqual(out['history'],old['history'])
        self.assertEqual(len(out['data_quality']['internal_holes_before_last_bar']),1)
        self.assertTrue(out['data_quality']['risk_sample_sufficient'])
        self.assertEqual(out['data_quality']['valid_return_pairs'],317)

    def test_recent_hole_blocks_indicators_but_not_sufficient_risk_pairs(self):
        out=server._apply_history_policy(series(missing=(300,)))
        self.assertFalse(out['screening_history_accepted'])
        self.assertEqual(out['contiguous_tail_bars'],19)
        self.assertTrue(out['data_quality']['risk_sample_sufficient'])
        with self.assertRaisesRegex(ValueError,'insufficienti'):
            server._indicators_from_payload(out)

    def test_latest_return_incomplete_is_not_marked_sufficient(self):
        out=server._apply_history_policy(series(missing=(318,)))
        self.assertFalse(out['data_quality']['latest_return_complete'])
        self.assertFalse(out['data_quality']['risk_sample_sufficient'])

    def test_indicators_do_not_compute_sma200_across_a_hole(self):
        out=server._apply_history_policy(series(missing=(157,)))
        ind=server._indicators_from_payload(out)
        self.assertEqual(ind['bars_used'],162)
        self.assertIsNone(ind['sma200'])
        self.assertIsNone(ind['ret_250d_pct'])
        self.assertIsNotNone(ind['sma50'])
        self.assertEqual(ind['sma50'],round(sum(b['close'] for b in out['history'][-50:])/50,4))

    def test_exact_existing_minimum_60_bars(self):
        p=series(n=61,missing=(0,))
        self.assertTrue(server._apply_history_policy(p)['screening_history_accepted'])
        p=series(n=60,missing=(0,))
        self.assertFalse(server._apply_history_policy(p)['screening_history_accepted'])

    def test_stale_us_stock_is_still_blocked(self):
        p=series(ticker='AAPL');p['history'].pop();p['coverage_end']=p['history'][-1]['date'];p['data_current']=False
        self.assertFalse(server._apply_history_policy(p)['screening_history_accepted'])

    def test_metadata_cannot_hide_an_actual_gap(self):
        p=series(missing=(300,));p['data_quality']['missing_internal_sessions']=[]
        q=server._apply_history_policy(p)
        self.assertFalse(q['screening_history_accepted'])
        self.assertEqual(len(q['data_quality']['internal_holes_before_last_bar']),1)

    def test_duplicate_and_off_calendar_rows_rejected(self):
        p=series();p['history'].append(copy.deepcopy(p['history'][-1]))
        self.assertFalse(server._apply_history_policy(p)['screening_history_accepted'])
        p=series();p['history'][1]['date']='2026-10-17'
        with self.assertRaises(ValueError):server._history_continuity(p)

    def test_delivered_pair_count_is_for_the_slice(self):
        p=server._apply_history_policy(series(missing=(200,)))
        with patch.object(server,'_load_series',return_value=p):out=server.get_market_data('BARC.L',max_bars=100)
        self.assertEqual(out['data_quality']['valid_return_pairs'],99)
        self.assertFalse(out['data_quality']['risk_sample_sufficient'])

    def test_risk_keeps_both_sides_of_gap_out(self):
        p=server._apply_history_policy(series(missing=(20,)))
        s,_=server._close_series_usd(p)
        self.assertEqual(len(server._risk_returns(s)),317)

    def test_benchmark_hole_does_not_create_twenty_session_return(self):
        p=server._apply_history_policy(series())
        b=series(missing=(310,))
        ind=server._indicators_from_payload(p,b,'SPY')
        self.assertIsNone(ind['rs_20d_vs_benchmark_pct'])
        self.assertIn('consecutivo',ind['benchmark_note'])

    def test_scan_consumes_repaired_policy(self):
        p=server._apply_history_policy(series(missing=(20,)))
        with patch.object(server,'_load_series',return_value=p):
            out=server.scan_market('EU',tickers=['BARC.L'])
        self.assertEqual(out['evaluated'],1)
        self.assertEqual(out['excluded_counts']['dati_non_aggiornati'],0)

    def test_current_but_short_segment_is_not_reported_stale(self):
        p=server._apply_history_policy(series(missing=(300,)))
        with patch.object(server,'_load_series',return_value=p):
            out=server.scan_market('EU',tickers=['BARC.L'])
        self.assertEqual(out['fresh_acquired'],1)
        self.assertEqual(out['evaluated'],0)
        self.assertEqual(out['excluded_counts']['storico_insufficiente'],1)
        self.assertEqual(out['excluded_counts']['dati_non_aggiornati'],0)
        self.assertFalse(out['scan_complete'])

    def test_etf_selector_does_not_accept_incomplete_latest_return(self):
        p=series(missing=(318,),ticker='CSPX.L')
        p.update(instrument_type='ETF',exchange_timezone='Europe/London')
        self.assertFalse(server._etf_history_quality(p,'CSPX.L')['usable_for_risk'])


if __name__ == '__main__':unittest.main()
