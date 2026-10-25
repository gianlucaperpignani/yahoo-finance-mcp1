"""1.0.1: download frozen public market opportunities directly into Python.

No universe or deferred tickers are printed into the chat. The scheduled caller
uses returned acquisition requests internally, persists raw responses and then
calls feedback_cycle. Existing eToro read tools supply broker data; no orders.
"""
import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse
from adaptive_method import digest

HOST='https://yahoo-finance-mcp1.onrender.com'


def prepare_entry(overview,contexts,identities,cost_inputs,benchmark_key,as_of):
    """Join raw read-only sources; missing inputs remain acquisition requests.

    contexts come from verified daily histories/indicators and sourced live FX;
    identities are actual identity-gate results for the broker instrument ID.
    No network or guessed account-specific commission policy in this adapter.
    """
    from opportunity_cohorts import market_rows,build_terms
    from learning_costs import normalize_open
    from learning_sessions import exit_plan
    from adaptive_method import stamp
    quotes=market_rows(overview);technical={};previews={}
    for key,q in quotes.items():
        context=contexts[key];identity=identities[key]
        if identity.get('status')!='PASS' or identity.get('instrument_id')!=q['instrument_id'] or not identity.get('reference'):raise ValueError('LEARNING_IDENTITY_NOT_VERIFIED')
        if not 0<=(stamp(as_of)-stamp(q['timestamp'])).total_seconds()<=300:raise ValueError('LEARNING_QUOTE_STALE_OR_UNSOURCED')
        for name in ('calendar_id','currency','fx_to_usd','fx_reference','price_unit'):
            q[name]=context[name]
        q.update(ticker=key,identity_reference=identity['reference'])
        technical[key]=context['indicators']
    benchmark=quotes.pop(benchmark_key)
    for key in quotes:
        c=cost_inputs[key];previews[key]=normalize_open(c['response'],c['request'],as_of,c.get('exit_fee_policy'))
    plan=exit_plan(quotes,benchmark,as_of)
    return dict(observed_at=as_of,quotes=quotes,benchmark=benchmark,exit_plan=plan,planned_exit_at=plan['ready_at'],
        calendar_reference='verified per-instrument exchange calendars; '+plan['plan_hash'],
        terms=build_terms(quotes,technical,previews,as_of),raw_overview=overview,raw_cost_inputs=cost_inputs)


def download(path,opener=urlopen):
    if not re.fullmatch(r'/cycle-archive/cycle-\d+(?:\?archive_object_id=[0-9a-f]{64})?',path):raise ValueError('ARCHIVE_PATH_INVALID')
    with opener(HOST+path,timeout=30) as response:
        if urlparse(response.url).netloc!=urlparse(HOST).netloc:raise ValueError('ARCHIVE_REDIRECT_INVALID')
        blob=response.read(2000001)
        if len(blob)>2000000 or hashlib.sha256(blob).hexdigest()!=response.headers.get('X-Content-SHA256'):raise ValueError('ARCHIVE_TRANSPORT_HASH_MISMATCH')
    raw=gzip.decompress(blob)
    if len(raw)>10000000:raise ValueError('ARCHIVE_TOO_LARGE')
    archive=json.loads(raw)
    if digest({k:v for k,v in archive.items() if k!='archive_id'})!=archive['archive_id']:raise ValueError('ARCHIVE_CONTENT_HASH_MISMATCH')
    return archive


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('archive_path');p.add_argument('output');a=p.parse_args()
    archive=download(a.archive_path);Path(a.output).write_text(json.dumps(archive,ensure_ascii=False))
    print(json.dumps(dict(archive_id=archive['archive_id'],opportunities=len(archive['opportunities']),saved=True)))
