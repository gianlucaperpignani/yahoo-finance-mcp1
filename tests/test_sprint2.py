import copy
import hashlib
import unittest
from datetime import datetime,timedelta,timezone
import adaptive_method as learning
from candidate_selection import select,CONFIG
from cycle_scanner import run as cycle_scan
from outcome_ledger import ingest,kpis
from event_evidence import verify


def events(start,n,reverse=False):
    out=[]
    for i in range(n):
        day=start+timedelta(days=i)
        stamp=day.isoformat();end=(day+timedelta(hours=1)).isoformat()
        cs=[]
        for key,features,ret in [('A',[100,0,0,0,0],-.02 if reverse else .02),('B',[0,100,10,0,0],.02 if reverse else -.01)]:
            cs.append(dict(key=key,features=dict(zip(CONFIG['profiles']['balanced'],features)),features_recorded_at=stamp,
                           net_return=ret,benchmark_return=0.,initial_risk_fraction=.02,costs_included=True,slippage_included=True))
        out.append(dict(cohort_id=stamp,signal_at=stamp,closed_at=end,universe_frozen_at=stamp,outcomes_observed_at=end,
                        complete_universe=True,kind='SHADOW_SIMULATION',source_reference='SYNTHETIC_TEST',category='SWING',method_id='test1',candidates=cs))
    return out


class LearningTests(unittest.TestCase):
    def test_no_data_does_not_learn_or_change_risk(self):
        r=learning.review(dict(as_of='2026-11-01T12:00:00+00:00'))
        self.assertEqual(r['status'],'ATTESA_CAMPIONE');self.assertEqual(r['state']['active'],'balanced')
        self.assertFalse(r['ticket_authorized'])

    def test_sequential_freeze_future_shadow_activation_and_rollback(self):
        rows=events(datetime(2026,10,20,tzinfo=timezone.utc),40)
        r=learning.review(dict(as_of='2026-12-01T12:00:00+00:00',cohorts=rows))
        self.assertEqual(r['action'],'FREEZE_CHALLENGER');self.assertEqual(r['state']['active'],'balanced')
        self.assertEqual(r['state']['pending']['profile'],'volume')
        # Validation cannot be used again as the future shadow cohort.
        same=learning.review(dict(as_of='2027-01-01T12:00:00+00:00',state=r['state'],cohorts=rows))
        self.assertEqual(same['status'],'ATTESA_SHADOW')
        shadow=events(datetime(2026,12,2,tzinfo=timezone.utc),10)
        active=learning.review(dict(as_of='2027-01-01T12:00:00+00:00',state=r['state'],cohorts=rows+shadow))
        self.assertEqual(active['action'],'ACTIVATE');self.assertEqual(active['state']['active'],'volume')
        bad=events(datetime(2027,1,2,tzinfo=timezone.utc),10,True)
        restored=learning.review(dict(as_of='2027-02-01T12:00:00+00:00',state=active['state'],cohorts=rows+shadow+bad))
        self.assertEqual(restored['action'],'ROLLBACK');self.assertEqual(restored['state']['active'],'balanced')

    def test_same_month_state_is_reused(self):
        r=learning.review(dict(as_of='2026-11-01T12:00:00+00:00'))
        again=learning.review(dict(as_of='2026-11-02T12:00:00+00:00',state=r['state']))
        self.assertEqual(again['state']['state_hash'],r['state']['state_hash'])

    def test_overlap_and_future_outcomes_rejected(self):
        rows=events(datetime(2026,10,20,tzinfo=timezone.utc),2)
        rows[0]['closed_at']=rows[1]['closed_at'];rows[0]['outcomes_observed_at']=rows[1]['closed_at']
        valid,errors=learning.cohorts(rows,learning.stamp('2026-11-01T00:00:00+00:00'))
        self.assertFalse(valid);self.assertTrue(errors)
        rows=events(datetime(2026,10,20,tzinfo=timezone.utc),1)
        rows[0]['outcomes_observed_at']='2027-01-01T00:00:00+00:00'
        self.assertFalse(learning.cohorts(rows,learning.stamp('2026-11-01T00:00:00+00:00'))[0])

    def test_state_tamper_rejected(self):
        s=learning.review(dict(as_of='2026-11-01T00:00:00+00:00'))['state'];s['active']='volume'
        with self.assertRaises(ValueError):learning.validate_state(s)
        s=learning.initial();s.pop('state_hash')
        with self.assertRaises(ValueError):learning.validate_state(s)


class BudgetTests(unittest.TestCase):
    def test_union_cap_dedup_pending_and_no_chase(self):
        rows=[dict(ticker='T'+str(i),fusion_score=i) for i in range(25)]
        rows+=copy.deepcopy(rows[:3]);rows[0]['extended']=True
        r=select(dict(candidates=rows,pending_keys=['T1']))
        self.assertLessEqual(len(r['shortlist']),10);self.assertEqual(r['shortlist'][0]['key'],'T1')
        self.assertFalse(r['ticket_authorized'])

    def test_async_aggregator_does_not_leak_unbounded_tickers(self):
        a=lambda m:dict(shortlist=[dict(ticker=m+str(i),fusion_score=i) for i in range(12)],coverage_summary={'expected_total':20})
        b=lambda m:dict(job_status='COMPLETE',shortlist=[dict(ticker=m+str(i),fusion_score=i) for i in range(6,20)])
        r=cycle_scan(['US','EU'],'balanced',[],a,b)
        self.assertEqual(r['returned'],10);self.assertEqual(r['union_count'],40)
        self.assertNotIn('deferred_keys',r)

    def test_local_fusion_error_preserves_level_a(self):
        def failure(m):raise ValueError('synthetic failure')
        a=lambda m:dict(shortlist=[dict(ticker='A')],coverage_summary={'expected_total':1})
        r=cycle_scan(['US'],'balanced',[],a,failure)
        self.assertEqual(r['returned'],1);self.assertFalse(r['scan_complete']);self.assertTrue(r['errors'])


class OutcomeTests(unittest.TestCase):
    def trade(self):return dict(positionId=1,orderId=2,openTime='2026-10-01',closeTime='2026-10-02',units=1,investment=100,netProfit=12,fees=2,isMirrorTrade=False,parentPositionId=0,socialTradeId=None)
    def page(self,trade):return dict(account='real',statusCode=200,minDate='2026-10-01',page=1,pageSize=200,hasMore=False,trades=[trade])
    def test_costs_dedup_and_later_attribution(self):
        p=self.page(self.trade());r=ingest([p],'USD');key=next(iter(r['entries']))
        self.assertEqual(r['entries'][key]['net_pnl_usd'],10);self.assertEqual(kpis(r['entries'])['n'],0)
        r2=ingest([p],'USD',r['entries']);self.assertEqual(r2['inserted'],0)
        link=dict(verified=True,ticket_unchanged=True,position_id=1,signal_id='SIG-1',evidence_reference='verified native row',category='SWING')
        r3=ingest([p],'USD',r['entries'],{key:link});self.assertEqual(kpis(r3['entries'])['n'],1)
        self.assertIsNone(kpis(r3['entries'])['profit_factor'])

    def test_copy_is_not_agent_accuracy(self):
        t=self.trade();t['parentPositionId']=77;r=ingest([self.page(t)],'USD')
        self.assertEqual(next(iter(r['entries'].values()))['origin'],'COPY')
        self.assertEqual(kpis(r['entries'])['n'],0)
    def test_undocumented_history_window_is_rejected(self):
        p=self.page(self.trade());p.pop('minDate')
        with self.assertRaises(ValueError):ingest([p],'USD')


class SourceTests(unittest.TestCase):
    def doc(self,kind='ISSUER_IR',origin=None):
        text='Company announced signed contract 2026-10-18'
        return dict(url='https://example.test/ir/1',text=text,content_sha256=hashlib.sha256(text.encode()).hexdigest(),status='RETRIEVED',
                    registry=dict(issuer_id='ID1',host='example.test',kind=kind,verified_at='2026-10-18',verification_reference='test fixture'),
                    origin_id=origin,origin_evidence_reference='syndication reviewed' if origin else None,fact_review=dict(confirmed=True,reference='fixture material fact review'))
    def test_primary_and_independent_origins(self):
        e=dict(issuer_id='ID1',required_facts=['signed contract'],documents=[self.doc()])
        self.assertEqual(verify(e)['state'],'PASS')
        e['documents']=[self.doc('FINANCIAL_NEWS','same'),self.doc('FINANCIAL_NEWS','same')]
        self.assertEqual(verify(e)['state'],'MISSING')
        d=e['documents'][1];d.update(origin_id='other',url='https://another.test/news/1',text=d['text']+' Independent reporting.')
        d['registry']['host']='another.test';d['content_sha256']=hashlib.sha256(d['text'].encode()).hexdigest()
        self.assertEqual(verify(e)['state'],'PASS')
    def test_publisher_name_or_http_is_not_confirmation(self):
        e=dict(issuer_id='ID1',required_facts=['signed contract'],documents=[self.doc()])
        e['documents'][0].pop('fact_review');self.assertEqual(verify(e)['state'],'MISSING')
        e['documents']=[self.doc()];e['category']='M&A';self.assertEqual(verify(e)['state'],'MISSING')


if __name__=='__main__':unittest.main()
