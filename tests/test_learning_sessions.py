import unittest,copy,hashlib
from unittest.mock import patch
from learning_sessions import exit_plan,validate_close,close_from_history,session_plan
from adaptive_method import digest,stamp
from mandate_targets import active_targets


class SessionTests(unittest.TestCase):
    def entry(self):
        return dict(instrument_id=1,ticker='AAA',timestamp='2026-10-19T14:00:00+00:00',calendar_id='XNYS',currency='USD',ask=100,bid=99.9)

    def close(self):
        e=self.entry();p=exit_plan({'AAA':e},e,e['timestamp'])['entries']['AAA']
        q=dict(kind='SESSION_CLOSE',instrument_id=1,calendar_id='XNYS',session_date='2026-10-20',valued_at=p['close_at'],retrieved_at='2026-10-21T07:20:00+00:00',price_basis='VERIFIED_DAILY_CLOSE',close=102,fx_to_usd=1,source_reference='synthetic history',fx_reference='synthetic USD',adjustments_reference='synthetic verified no actions',entry_basis_verified=True,entry_quote_hash=digest(e))
        return e,p,q

    def test_delayed_correct_session_is_valid(self):
        e,p,q=self.close();validate_close(q,p,e,stamp('2026-10-21T07:20:00+00:00'))

    def test_next_day_live_or_close_cannot_substitute(self):
        e,p,q=self.close();q.update(session_date='2026-10-21',valued_at='2026-10-21T20:00:00+00:00')
        with self.assertRaisesRegex(ValueError,'WRONG_SESSION'):validate_close(q,p,e,stamp('2026-10-22T07:20:00+00:00'))
        q.update(session_date='2026-10-20',kind='LIVE_QUOTE')
        with self.assertRaises(ValueError):validate_close(q,p,e,stamp('2026-10-22T07:20:00+00:00'))

    def test_wrong_close_and_basis_rejected(self):
        for field,value in [('valued_at','2026-10-21T14:00:00+00:00'),('entry_quote_hash','different'),('instrument_id',2)]:
            e,p,q=self.close();q[field]=value
            with self.assertRaises(ValueError):validate_close(q,p,e,stamp('2026-10-21T15:00:00+00:00'))

    def test_holiday_early_close_and_dst(self):
        p=session_plan('XNYS','2026-11-25T15:00:00+00:00')
        self.assertEqual(p['session_date'],'2026-11-27');self.assertEqual(stamp(p['close_at']).hour,18)
        self.assertEqual(stamp(session_plan('XNYS','2026-10-30T14:00:00+00:00')['close_at']).hour,21)
        with self.assertRaises(ValueError):session_plan('XNYS','2026-10-19T07:20:00+00:00')

    def test_history_normalization_rejects_events_wrong_identity(self):
        e,p,q=self.close();payload=dict(ticker='AAA',calendar='XNYS',interval='1d',adjusted=True,data_quality={'calendar_validated':True},generated_at_utc=q['retrieved_at'],source='SYNTHETIC',actions=[],coverage_start='2026-10-19',major_currency='USD',history=[{'date':'2026-10-20','close':102}])
        v=close_from_history(payload,p,e,q['retrieved_at'],1,'synthetic USD');self.assertEqual(v['close'],102)
        for field,value in [('actions',[{'date':'2026-10-20','split':2}]),('ticker','BBB'),('history',[])]:
            d=copy.deepcopy(payload);d[field]=value
            with self.assertRaises(ValueError):close_from_history(d,p,e,q['retrieved_at'],1,'synthetic USD')


class TargetTests(unittest.TestCase):
    def test_only_initial_bounds_from_verified_text_no_reset(self):
        text='| Obiettivo iniziale | Portafoglio a 18.500–19.000 € lordi nell’orizzonte operativo di circa 2–3 mesi, indicativamente entro dicembre 2026. |'
        sha=hashlib.sha256(text.encode()).hexdigest()
        with patch('mandate_targets.validate_mandate'):
            rows=active_targets(text,'SYNTHETIC',sha)
        self.assertEqual([r['amount_eur'] for r in rows],[18500,19000]);self.assertEqual(rows[0]['deadline'],'2026-12');self.assertTrue(rows[0]['deadline_indicative']);self.assertFalse(rows[0]['future_reset_active'])
        with self.assertRaises(ValueError):active_targets(text,'SYNTHETIC','bad')


if __name__=='__main__':unittest.main()
