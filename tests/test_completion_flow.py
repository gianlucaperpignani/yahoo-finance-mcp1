import copy
import unittest
from adaptive_method import digest,initial,cohorts,stamp
from candidate_selection import CONFIG,select
from level_b import fuse_level_b_results
from cycle_scanner import run as scan
from opportunity_cohorts import update,market_rows,build_terms
from execution_attribution import match
from outcome_ledger import ingest,category_kpis,post_mortem_updates
from candidate_enrichment import enrich
from feedback_cycle import run as feedback


class CompletionTests(unittest.TestCase):
    def fusion(self):
        def source(name,values):return {'shortlist':[dict(ticker=t,sector='Technology',**{name:v}) for t,v in values]}
        return fuse_level_b_results(market='US',a1=source('a1_score',[('AAA',100),('BBB',0)]),
            a2=source('a2_score',[('AAA',0),('BBB',80)]),a3=source('catalyst_score',[('AAA',0),('BBB',30)]),
            a4=source('attention_score',[('AAA',0),('BBB',0)]),a5={'sectors':[dict(sector='TECHNOLOGY',a5_score=50,state='SETTORE_NEUTRO')]})

    def archive(self):
        rows=self.fusion()['shortlist']
        value=dict(schema_version=1,frozen_at='2026-11-02T09:00:00+00:00',opportunities=rows)
        value['archive_id']=digest(value);return value

    def quote(self,price=100,time='2026-11-02T09:01:00+00:00',identity=1):
        return dict(instrument_id=identity,ask=price,bid=price-.1,timestamp=time,source_reference='synthetic broker',
            fx_to_usd=1,fx_reference='synthetic USD quote',identity_reference='synthetic verified identity')

    def entry(self):
        return dict(observed_at='2026-11-02T09:01:00+00:00',quotes={'AAA':self.quote(),'BBB':self.quote(identity=2)},
            benchmark=self.quote(identity=3),planned_exit_at='2026-11-03T16:00:00+00:00',calendar_reference='synthetic XNYS session',
            terms={k:dict(entry_fee_fraction=.001,exit_fee_fraction=.001,slippage_fraction=.001,initial_risk_fraction=.02,
                source_reference='synthetic cost preview',risk_reference='synthetic ATR',registered_at='2026-11-02T09:01:00+00:00') for k in ('AAA','BBB')})

    def test_real_fusion_shape_uses_promoted_research_scope(self):
        rows=self.fusion()['shortlist'];s=initial();s.update(active='volume',scope=rows[0]['learning_scope']);s.pop('state_hash');s['state_hash']=digest(s)
        r=select(dict(candidates=rows,learning_states={'DISCOVERY|FUSION_US_UNION_V1':s}))
        self.assertEqual(r['shortlist'][0]['ticker'],'AAA');self.assertEqual(r['shortlist'][0]['applied_profile'],'volume')
        self.assertNotIn('category',r['shortlist'][0]) # no financial classification invented

    def test_level_a_duplicate_does_not_erase_fusion_features(self):
        rows=[dict(ticker='AAA',setup='AVVIO_TREND')]+self.fusion()['shortlist']
        r=select(dict(candidates=rows));a=next(x for x in r['shortlist'] if x['ticker']=='AAA')
        self.assertIsNotNone(a['selection_score']);self.assertEqual(a['level_a_context']['setup'],'AVVIO_TREND')

    def test_archive_keeps_nonselected_opportunities_off_chat(self):
        artifacts=[]
        a=lambda m:dict(shortlist=[dict(ticker='T'+str(i)) for i in range(25)],scan_complete=True)
        b=lambda m:dict(job_status='COMPLETE',shortlist=[])
        r=scan(['US'],'balanced',[],a,b,archive_sink=artifacts.append)
        self.assertEqual(r['returned'],10);self.assertEqual(len(artifacts[0]['opportunities']),25)
        self.assertNotIn('opportunities',r)

    def test_freeze_entry_exit_complete_universe_and_causality(self):
        first=update(dict(as_of='2026-11-02T09:00:00+00:00',archive=self.archive()))
        row=first['store']['cohorts'][0];key=row['cohort_id']
        entered=update(dict(as_of='2026-11-02T09:01:00+00:00',store=first['store'],observations={key:self.entry()}))
        self.assertEqual(entered['store']['cohorts'][0]['state'],'WAIT_EXIT')
        exit=dict(observed_at='2026-11-03T16:00:00+00:00',session_complete=True,calendar_reference='synthetic XNYS close',
            quotes={'AAA':self.quote(103,'2026-11-03T16:00:00+00:00'),'BBB':self.quote(99,'2026-11-03T16:00:00+00:00',2)},
            benchmark=self.quote(101,'2026-11-03T16:00:00+00:00',3))
        closed=update(dict(as_of=exit['observed_at'],store=entered['store'],observations={key:exit}))
        self.assertEqual(len(closed['closed']),1);self.assertEqual(len(closed['closed'][0]['candidates']),2)
        valid,errors=cohorts(closed['closed'],stamp(exit['observed_at']));self.assertEqual(len(valid),1);self.assertFalse(errors)
        self.assertAlmostEqual(next(c for c in valid[0]['candidates'] if c['key']=='AAA')['net_return'],.025)

    def test_missing_cost_is_not_assumed_zero_and_update_is_atomic(self):
        first=update(dict(as_of='2026-11-02T09:00:00+00:00',archive=self.archive()));key=first['store']['cohorts'][0]['cohort_id']
        entry=self.entry();entry['terms']['BBB'].pop('exit_fee_fraction')
        bad=update(dict(as_of=entry['observed_at'],store=first['store'],observations={key:entry}))
        self.assertTrue(bad['errors']);self.assertEqual(bad['store']['cohorts'][0]['state'],'WAIT_ENTRY')
        self.assertFalse(bad['closed'])

    def test_missing_identity_never_advances_a_frozen_cohort(self):
        first=update(dict(as_of='2026-11-02T09:00:00+00:00',archive=self.archive()));key=first['store']['cohorts'][0]['cohort_id']
        entry=self.entry();entry['quotes']['AAA'].pop('instrument_id')
        bad=update(dict(as_of=entry['observed_at'],store=first['store'],observations={key:entry}))
        self.assertEqual(bad['store']['cohorts'][0]['state'],'WAIT_ENTRY')
        self.assertIn('LEARNING_INSTRUMENT_ID_REQUIRED',[e['reason'] for e in bad['errors']])

    def test_attribution_and_category_kpi_preserve_evidence(self):
        trade=dict(positionId=1,orderId=2,units=2,openTime='2026-11-02T10:00:00+00:00',closeTime='2026-11-03T10:00:00+00:00',
            market=dict(instrumentId=7),investment=100,netProfit=12,fees=2,direction='long',leverage=1)
        proposal=dict(instrument_id=7,created_at='2026-11-02T09:00:00+00:00',signal_id='SIG',category='SWING',origin='AI',forecast_probability=.7,direction='long',leverage=1)
        record=dict(proposal=proposal,proposal_hash=digest(proposal),user_decision='CONFIRMED',execution_receipt=dict(position_id=1,
            instrument_id=7,account='real',status='EXECUTED',reference='synthetic broker receipt',units=3,executed_at=trade['openTime'],proposal_hash=digest(proposal),direction='long',leverage=1))
        from outcome_ledger import digest as close_digest
        record['exit_attribution']=dict(status='PLAN_UNCHANGED',reference='synthetic linked exit plan',close_key=close_digest({k:trade[k] for k in ('positionId','orderId','openTime','closeTime','units')}))
        links=match([trade],[record]);self.assertFalse(links['issues'])
        page=dict(account='real',statusCode=200,page=1,pageSize=200,minDate='2026-11-01',hasMore=False,trades=[trade])
        ledger=ingest([page],'USD',links=links['links']);self.assertEqual(category_kpis(ledger['entries'])['SWING']['n'],1)
        self.assertEqual(post_mortem_updates(ledger['entries'],'BND')[0]['fields'][5],10)
        record['proposal']['forecast_probability']=.9
        self.assertFalse(match([trade],[record])['links'])

    def test_source_collection_is_bounded_and_cannot_pass_on_http_alone(self):
        pipeline=dict(shortlisted_keys=['AAA'],rows=[dict(key='AAA',outcome='PROPONIBILE',gates={'identity':{'state':'PASS'},'events':{'state':'PASS'}})])
        calls=[]
        def fetch(urls,registry):calls.append(urls);return []
        result=enrich(pipeline,[dict(ticker='AAA',detected_by=['A3'])],{'AAA':dict(urls=['https://example.test/ir'],registry=[])},fetcher=fetch,as_of='2026-11-02T10:00:00+00:00')
        row=result['candidate_pipeline']['rows'][0];self.assertEqual(len(calls),1)
        self.assertTrue(row['catalyst_required']);self.assertEqual(row['gates']['events']['state'],'MISSING')

    def test_feedback_emits_recoverable_acquisition_requests_without_promoting(self):
        result=feedback(dict(as_of='2026-11-02T09:00:00+00:00',account_currency='USD',opportunity_archive=self.archive()))
        self.assertEqual(len(result['cohort_collection']['requests']),1)
        self.assertEqual(result['effective_profile'],'balanced');self.assertEqual(result['kpis']['n'],0)
        self.assertTrue(result['activation_requires_persisted_state'])

    def test_cost_and_risk_units_are_computed_from_observed_inputs(self):
        q=self.quote();q['price_unit']='USD/share'
        technical={'AAA':dict(atr14=2,price_unit='USD/share',source_reference='synthetic ATR')}
        costs={'AAA':dict(currency='USD',notional_usd=1000,entry_fee_usd=1,exit_fee_usd=1,
            timestamp=q['timestamp'],source_reference='synthetic preview')}
        result=build_terms({'AAA':q},technical,costs,q['timestamp'])['AAA']
        self.assertEqual(result['entry_fee_fraction'],.001);self.assertEqual(result['initial_risk_fraction'],.02)
        q.pop('price_unit');technical['AAA'].pop('price_unit')
        with self.assertRaises(ValueError):build_terms({'AAA':q},technical,costs,q['timestamp'])


if __name__=='__main__':unittest.main()
