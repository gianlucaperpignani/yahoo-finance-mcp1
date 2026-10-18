"""1.1.0: acquisition -> attribution -> KPIs -> cohorts -> monthly review.

All outputs are proposals until native Registro persistence/readback. No orders.
"""
import argparse
import copy
import json
from pathlib import Path
from adaptive_method import review,digest,initial,validate_state
from candidate_selection import select
from outcome_ledger import ingest,kpis,category_kpis,post_mortem_updates
from execution_attribution import match
from opportunity_cohorts import update as update_cohorts,build_terms
from candidate_enrichment import enrich


def run(data):
    trades=[t for p in data.get('history_pages',[]) for t in p.get('trades',[])]
    attribution=match(trades,data.get('ticket_records',[]))
    links=dict(data.get('execution_links') or {})
    for key,link in attribution['links'].items():
        if key in links and links[key]!=link:raise ValueError('CONFLICTING_EXECUTION_ATTRIBUTION')
        links[key]=link
    ledger=ingest(data.get('history_pages',[]),data['account_currency'],data.get('prior_ledger'),links)
    metrics=kpis(ledger['entries']);categories=category_kpis(ledger['entries'])
    observations=copy.deepcopy(data.get('learning_observations',{}))
    acquisition_issues=[]
    for key,observation in observations.items():
        if observation.get('cost_previews') is not None:
            try:observation['terms']=build_terms(observation['quotes'],observation['indicators'],observation['cost_previews'],observation['observed_at'])
            except (KeyError,ValueError,TypeError) as exc:
                observation.pop('terms',None)
                acquisition_issues.append(dict(cohort_id=key,reason=str(exc)))
    collection=update_cohorts(dict(as_of=data['as_of'],store=data.get('cohort_store'),
        archive=data.get('opportunity_archive'),observations=observations))
    collection['errors'].extend(acquisition_issues)
    # Existing caller-supplied cohorts are replay compatibility, not generated observations.
    cohorts=collection['closed']+data.get('shadow_cohorts',[])
    learned=review(dict(as_of=data['as_of'],state=data.get('learning_state'),cohorts=data.get('shadow_cohorts',[])))
    if learned['status']!='RIUSATO_MESE':
        state=learned['state'];state.pop('state_hash',None)
        state['confidence_calibration']=metrics['confidence_calibration']
        state['confidence_calibration_by_category']={k:v['confidence_calibration'] for k,v in categories.items()}
        state['state_hash']=digest(state)
    persisted=validate_state(data.get('learning_state') or initial())
    persisted_states=data.get('learning_states') or {};proposed_states=copy.deepcopy(persisted_states);reviews={}
    grouped={}
    for c in collection['closed']:
        grouped.setdefault(c['scope_key'],[]).append(c)
    for key,rows in grouped.items():
        result=review(dict(as_of=data['as_of'],state=persisted_states.get(key),cohorts=rows))
        proposed_states[key]=result['state'];reviews[key]=result
    selection=select(dict(candidates=data.get('candidates',[]),pending_keys=data.get('pending_keys',[]),
        active_profile=persisted['active'],profile_scope=persisted.get('scope'),learning_states=persisted_states,
        monitor_positions=data.get('monitor_positions',[])))
    enrichment=None
    if data.get('candidate_pipeline') is not None:
        enrichment=enrich(data['candidate_pipeline'],selection['shortlist'],data.get('event_inputs'),
            persisted.get('confidence_calibration_by_category'),as_of=data['as_of'])
    return dict(version='1.1.0',as_of=data['as_of'],ledger=ledger,kpis=metrics,category_kpis=categories,
        attribution_issues=attribution['issues'],learning=learned,learning_reviews=reviews,learning_states=proposed_states,
        cohort_collection=collection,selection=selection,enrichment=enrichment,
        post_mortem_updates=post_mortem_updates(ledger['entries'],data.get('bundle_id') or 'PENDING_BUNDLE_ID'),
        persistence_status='PENDING_REGISTRO_READBACK',activation_requires_persisted_state=True,ticket_authorized=False,
        effective_profile=persisted['active'],proposed_profile=learned['state']['active'],
        target_progress_reference=data.get('target_progress_reference'))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('input');parser.add_argument('output');args=parser.parse_args()
    result=run(json.loads(Path(args.input).read_text()))
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
