"""1.0.0: freeze discovery opportunities and settle them from sourced observations.

No orders, network, invented prices, costs or outcomes. Caller persists/readbacks
the returned store and acquires requests through read-only market-data tools.
DISCOVERY is a research cohort, never a portfolio financial classification.
"""
import copy
from datetime import datetime
from adaptive_method import digest, stamp
from candidate_selection import score, finite


def scope_key(scope):
    return scope['category']+'|'+scope['method_id']


def market_rows(overview):
    """Normalize actual eToro overview rows without computing fictitious fees."""
    if overview.get('statusCode') != 200: raise ValueError('LEARNING_OVERVIEW_FAILED')
    out={}
    for row in overview.get('instruments',[]):
        market=row.get('market',{});q=row.get('quote',{})
        if market.get('symbol'):
            out[market['symbol'].upper()]=dict(instrument_id=market.get('instrumentId'),
                timestamp=q.get('asOf'),ask=q.get('ask'),bid=q.get('bid'),
                source_reference=overview.get('xRequestId'))
    return out


def build_terms(quotes,indicators,cost_previews,as_of):
    """Numerical preparation from normalized read-only previews, no LLM arithmetic.

    Slippage is an ex-ante research model (half observed spread each side),
    not an assertion about an actual fill. ATR is a risk unit, not a broker SL.
    """
    out={}
    for key,q in quotes.items():
        technical=indicators[key];cost=cost_previews[key]
        if cost.get('currency')!='USD' or not cost.get('source_reference') or not technical.get('source_reference'):raise ValueError('COST_OR_ATR_SOURCE_REQUIRED')
        if not finite(cost.get('notional_usd')) or cost['notional_usd']<=0 or not finite(technical.get('atr14')) or technical['atr14']<=0:raise ValueError('COST_NOTIONAL_OR_ATR_INVALID')
        if not all(finite(cost.get(k)) and cost[k]>=0 for k in ('entry_fee_usd','exit_fee_usd')):raise ValueError('COST_PREVIEW_INCOMPLETE')
        if not 0<q['bid']<=q['ask'] or not q.get('price_unit') or technical.get('price_unit')!=q.get('price_unit'):raise ValueError('LEARNING_PRICE_UNITS_MISMATCH')
        if not 0<=(stamp(as_of)-stamp(cost['timestamp'])).total_seconds()<=300:raise ValueError('COST_PREVIEW_STALE')
        out[key]=dict(entry_fee_fraction=cost['entry_fee_usd']/cost['notional_usd'],exit_fee_fraction=cost['exit_fee_usd']/cost['notional_usd'],
            slippage_fraction=(q['ask']-q['bid'])/(q['ask']+q['bid']),initial_risk_fraction=technical['atr14']/q['ask'],
            source_reference=cost['source_reference']+'; slippage=half observed spread per side: '+q['source_reference'],
            risk_reference=technical['source_reference']+'; ATR/ask research risk unit, not SL',registered_at=as_of)
    return out


def update(data):
    now=stamp(data['as_of']);store=copy.deepcopy(data.get('store') or {'schema_version':1,'cohorts':[]})
    original=store.pop('store_hash',None)
    if data.get('store') is not None and not original:raise ValueError('COHORT_STORE_HASH_REQUIRED')
    if original and digest(store)!=original: raise ValueError('COHORT_STORE_HASH_MISMATCH')
    rows=store['cohorts'];errors=[];archive=data.get('archive');observations=data.get('observations',{})
    if archive:
        unsigned={k:v for k,v in archive.items() if k!='archive_id'}
        if digest(unsigned)!=archive.get('archive_id'): raise ValueError('OPPORTUNITY_ARCHIVE_HASH_MISMATCH')
        frozen=stamp(archive['frozen_at'])
        if not 0 <= (now-frozen).total_seconds() <= 900: raise ValueError('OPPORTUNITY_ARCHIVE_STALE')
        groups={}
        for c in archive.get('opportunities',[]):
            scope=c.get('learning_scope')
            if scope and score(c.get('ranking_features'),'balanced') is not None:
                groups.setdefault(scope_key(scope),[]).append(c)
        for key,candidates in groups.items():
            # Never sample only the winner: freeze all comparable opportunities.
            if any(r['scope_key']==key and (r['state']!='CLOSED' or stamp(r['closed_at'])>=frozen) for r in rows): continue
            ident=digest(dict(archive=archive['archive_id'],scope=key))
            if any(r['cohort_id']==ident for r in rows):continue
            unique={}
            for c in candidates:
                ticker=(c.get('ticker') or c.get('key')).upper()
                if ticker in unique and unique[ticker]['features']!=c['ranking_features']:
                    raise ValueError('DUPLICATE_OPPORTUNITY_CONFLICT')
                unique[ticker]=dict(key=ticker,features=c['ranking_features'],features_recorded_at=archive['frozen_at'],
                    ranking_penalty=c.get('ranking_penalty',0),instrument_id=c.get('instrument_id'))
            rows.append(dict(cohort_id=ident,scope_key=key,category=candidates[0]['learning_scope']['category'],
                method_id=candidates[0]['learning_scope']['method_id'],state='WAIT_ENTRY',
                universe_frozen_at=archive['frozen_at'],source_reference=archive['archive_id'],
                kind='SHADOW_SIMULATION',complete_universe=True,candidates=list(unique.values()),
                raw_universe_count=len(archive.get('opportunities',[])),comparable_count=len(unique)))
    for index,stored in enumerate(rows):
        row=copy.deepcopy(stored)
        if row['state']=='CLOSED':continue
        observation=observations.get(row['cohort_id'])
        if not observation:continue
        try:
            seen=stamp(observation['observed_at'])
            if not stamp(row['universe_frozen_at']) <= seen <= now:raise ValueError('OBSERVATION_LOOK_AHEAD')
            quotes=observation.get('quotes') or market_rows(observation['overview'])
            if set(quotes)!= {c['key'] for c in row['candidates']}:raise ValueError('COHORT_QUOTES_INCOMPLETE')
            benchmark=observation['benchmark']
            def checked_quote(q):
                if type(q.get('instrument_id')) is not int or q['instrument_id']<=0:raise ValueError('LEARNING_INSTRUMENT_ID_REQUIRED')
                if not all(finite(q.get(k)) and q[k]>0 for k in ('ask','bid')) or q['ask']<q['bid']:raise ValueError('LEARNING_QUOTE_INVALID')
                qt=stamp(q['timestamp'])
                if not 0 <= (seen-qt).total_seconds() <= 300 or not q.get('source_reference'):raise ValueError('LEARNING_QUOTE_STALE_OR_UNSOURCED')
                if not finite(q.get('fx_to_usd')) or q['fx_to_usd']<=0 or not q.get('fx_reference'):raise ValueError('LEARNING_FX_MISSING')
            for q in [*quotes.values(),benchmark]:checked_quote(q)
            if row['state']=='WAIT_ENTRY':
                planned=stamp(observation['planned_exit_at'])
                if not planned>seen or not observation.get('calendar_reference'):raise ValueError('LEARNING_EXIT_CALENDAR_REQUIRED')
                # Costs/slippage are registered ex ante and must have real evidence.
                terms=observation['terms']
                if set(terms)!=set(quotes):raise ValueError('LEARNING_COST_TERMS_INCOMPLETE')
                for c in row['candidates']:
                    q=quotes[c['key']];t=terms[c['key']]
                    if stamp(q['timestamp']) < stamp(row['universe_frozen_at']):raise ValueError('ENTRY_BEFORE_FREEZE')
                    if not all(finite(t.get(k)) and t[k]>=0 for k in ('entry_fee_fraction','exit_fee_fraction','slippage_fraction')) or not finite(t.get('initial_risk_fraction')) or t['initial_risk_fraction']<=0:raise ValueError('LEARNING_TERMS_MISSING')
                    if not t.get('source_reference') or not t.get('risk_reference'):raise ValueError('LEARNING_TERMS_UNSOURCED')
                    if stamp(t['registered_at'])>seen:raise ValueError('COST_TERMS_LOOK_AHEAD')
                    if c.get('instrument_id') and c['instrument_id']!=q.get('instrument_id'):raise ValueError('LEARNING_IDENTITY_CHANGED')
                    if not q.get('identity_reference'):raise ValueError('LEARNING_IDENTITY_EVIDENCE_MISSING')
                if stamp(benchmark['timestamp'])<stamp(row['universe_frozen_at']):raise ValueError('BENCHMARK_BEFORE_FREEZE')
                row.update(state='WAIT_EXIT',signal_at=observation['observed_at'],planned_exit_at=observation['planned_exit_at'],
                    entry=copy.deepcopy(observation),terms=copy.deepcopy(terms))
            else:
                if seen<stamp(row['planned_exit_at']):continue
                entry=row['entry'];eq=entry.get('quotes') or market_rows(entry['overview'])
                if not observation.get('session_complete') or not observation.get('calendar_reference'):raise ValueError('LEARNING_EXIT_SESSION_NOT_COMPLETE')
                if stamp(benchmark['timestamp'])<stamp(row['planned_exit_at']) or benchmark.get('instrument_id')!=entry['benchmark'].get('instrument_id'):raise ValueError('BENCHMARK_EXIT_MISMATCH')
                for c in row['candidates']:
                    q=quotes[c['key']];e=eq[c['key']];t=row['terms'][c['key']]
                    if q.get('instrument_id')!=e.get('instrument_id'):raise ValueError('LEARNING_IDENTITY_CHANGED')
                    if stamp(q['timestamp']) < stamp(row['planned_exit_at']):raise ValueError('EXIT_BEFORE_HORIZON')
                    gross=q['bid']*q['fx_to_usd']/(e['ask']*e['fx_to_usd'])-1
                    bm=benchmark['bid']*benchmark['fx_to_usd']/(entry['benchmark']['ask']*entry['benchmark']['fx_to_usd'])-1
                    net=gross-t['entry_fee_fraction']-t['exit_fee_fraction']-2*t['slippage_fraction']
                    if not finite(net) or not finite(bm) or net<=-1:raise ValueError('LEARNING_RETURN_INVALID')
                    c.update(net_return=net,benchmark_return=bm,initial_risk_fraction=t['initial_risk_fraction'],
                        costs_included=True,slippage_included=True,outcome_reference=q['source_reference'])
                row.update(state='CLOSED',closed_at=observation['observed_at'],outcomes_observed_at=observation['observed_at'],exit=copy.deepcopy(observation))
            rows[index]=row
        except (KeyError,ValueError,TypeError) as exc:errors.append(dict(cohort_id=row['cohort_id'],reason=str(exc)))
    requests=[dict(cohort_id=r['cohort_id'],phase=r['state'],tickers=[c['key'] for c in r['candidates']],
        not_before=r.get('planned_exit_at') if r['state']=='WAIT_EXIT' else r['universe_frozen_at'],
        required=['broker_quotes','benchmark_quote','fx_sources','cost_terms_and_initial_risk' if r['state']=='WAIT_ENTRY' else 'completed_session_calendar']) for r in rows if r['state']!='CLOSED']
    store['store_hash']=digest(store)
    return dict(store=store,closed=[r for r in rows if r['state']=='CLOSED'],requests=requests,errors=errors,
        persistence_status='PENDING_REGISTRO_READBACK',ticket_authorized=False)
