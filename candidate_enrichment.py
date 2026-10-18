"""1.0.0: bounded source acquisition and persisted confidence calibration."""
import copy
import argparse
import json
from pathlib import Path
from datetime import datetime,timezone
from event_evidence import collect,verify
from outcome_ledger import calibrate,number


def enrich(pipeline,shortlist,events=None,persisted_calibration=None,fetcher=collect,as_of=None):
    out=copy.deepcopy(pipeline);events=events or {};by_key={str(r.get('key') or r.get('ticker')).upper():r for r in shortlist}
    if len(by_key)>10 or set(out['shortlisted_keys'])!=set(by_key):raise ValueError('ENRICHMENT_GLOBAL_BUDGET_OR_KEYS')
    diagnostics=[]
    for row in out['rows']:
        native=by_key[row['key']];required=row.get('catalyst_required') is True or bool(set(native.get('detected_by',[])) & {'A3','A4'})
        row['catalyst_required']=required
        probability=row.get('forecast_probability')
        if number(probability):
            if not row.get('forecast_reference'):raise ValueError('ORIGINAL_FORECAST_REFERENCE_REQUIRED')
            row['confidence_calibration']=calibrate(probability,(persisted_calibration or {}).get(row.get('category'),[]))
            row['confidence_calibration']['source_reference']=row['forecast_reference']
        if not required or row.get('gates',{}).get('identity',{}).get('state')!='PASS':continue
        event=copy.deepcopy(events.get(row['key']) or {})
        if event.get('urls'):
            docs=fetcher(event['urls'],event.get('registry',[]))
            annotations=event.get('document_reviews',{})
            for doc in docs:
                review=annotations.get(doc.get('url'),{})
                # A semantic annotation applies only to the exact bytes reviewed.
                if review.get('content_sha256')==doc.get('content_sha256'):
                    doc.update({k:review[k] for k in ('fact_review','origin_id','origin_evidence_reference') if k in review})
            event['documents']=docs
        result=verify(event);row['catalyst_evidence']=event
        gate=row['gates']['events'];now=as_of or datetime.now(timezone.utc).isoformat()
        # Collection alone never changes an unperformed financial analysis to PASS.
        if result['state']!='PASS' and gate.get('state')=='PASS':
            row['gates']['events']=dict(state='MISSING',reason=result['reason'],reference='event_evidence: '+row['key'],checked_at=now,
                retrieval=dict(attempted_at=now,result_reference='event_evidence documents',result=result['reason']))
            if row.get('outcome')=='PROPONIBILE':row['outcome']='BLOCCATO_DATI'
            row.setdefault('next_action',dict(action='Completare fonte primaria o due origini indipendenti e verifica fatti',source='IR/regolatore/fonti finanziarie',deadline=now))
        diagnostics.append(dict(key=row['key'],**result))
    return dict(candidate_pipeline=out,evidence_checks=diagnostics,ticket_authorized=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('input');parser.add_argument('output');args=parser.parse_args()
    data=json.loads(Path(args.input).read_text())
    result=enrich(data['candidate_pipeline'],data['shortlist'],data.get('events'),data.get('persisted_calibration'),as_of=data.get('as_of'))
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
