"""1.0.0: freeze exchange-close horizons and validate delayed observations.

Acquisition time and valuation session are separate. No live-price freshness
exception for entry or tickets; historical exit values must match the plan.
"""
from datetime import timedelta
import copy
from adaptive_method import stamp,digest
from candidate_selection import finite


def session_plan(calendar_id,entry_at):
    import exchange_calendars as xc
    import pandas as pd
    cal=xc.get_calendar(calendar_id);entry=stamp(entry_at)
    local=entry.astimezone(cal.tz).date()
    sessions=cal.sessions_in_range(pd.Timestamp(local),pd.Timestamp(local+timedelta(days=14)))
    if len(sessions)<2 or sessions[0].date()!=local:raise ValueError('ENTRY_SESSION_NOT_OPEN')
    opened=cal.session_open(sessions[0]).to_pydatetime();closed=cal.session_close(sessions[0]).to_pydatetime()
    if not opened<=entry<closed:raise ValueError('ENTRY_SESSION_NOT_OPEN')
    target=sessions[1]
    return dict(calendar_id=calendar_id,session_date=target.date().isoformat(),
        close_at=cal.session_close(target).to_pydatetime().isoformat(),
        price_basis='VERIFIED_DAILY_CLOSE',calendar_reference='exchange_calendars '+xc.__version__)


def exit_plan(quotes,benchmark,entry_at):
    rows={k:session_plan(q['calendar_id'],entry_at) for k,q in quotes.items()}
    bm=session_plan(benchmark['calendar_id'],entry_at)
    out=dict(version='session-close-1.0.0',entries=rows,benchmark=bm,
        ready_at=max([r['close_at'] for r in rows.values()]+[bm['close_at']],key=stamp))
    out['plan_hash']=digest(out)
    return out


def validate_plan(plan,quotes,benchmark,entry_at):
    if plan!=exit_plan(quotes,benchmark,entry_at):raise ValueError('EXIT_PLAN_CALENDAR_MISMATCH')
    return plan


def validate_close(q,expected,entry,seen):
    """Quote contains a sourced close, not a stale live snapshot relabelled close."""
    if type(q.get('instrument_id')) is not int or q['instrument_id']!=entry['instrument_id']:raise ValueError('LEARNING_IDENTITY_CHANGED')
    if q.get('kind')!='SESSION_CLOSE' or q.get('session_date')!=expected['session_date'] or q.get('calendar_id')!=expected['calendar_id']:raise ValueError('EXIT_WRONG_SESSION')
    if q.get('price_basis')!=expected['price_basis'] or stamp(q['valued_at'])!=stamp(expected['close_at']):raise ValueError('EXIT_WRONG_VALUATION_TIME')
    fetched=stamp(q['retrieved_at']);close=stamp(expected['close_at'])
    if not close<=fetched<=seen:raise ValueError('EXIT_RETRIEVAL_TIME_INVALID')
    if not q.get('source_reference') or not q.get('adjustments_reference') or not q.get('fx_reference'):raise ValueError('EXIT_CLOSE_PROVENANCE_REQUIRED')
    if q.get('entry_basis_verified') is not True or q.get('entry_quote_hash')!=digest(entry):raise ValueError('EXIT_ENTRY_PRICE_BASIS_NOT_VERIFIED')
    if not finite(q.get('close')) or q['close']<=0 or not finite(q.get('fx_to_usd')) or q['fx_to_usd']<=0:raise ValueError('EXIT_CLOSE_INVALID')


def close_from_history(payload,expected,entry,as_of,fx_to_usd,fx_reference):
    """Normalize an acquired daily history only when raw/adjusted entry agrees.

    No events over the interval is an explicit provider evidence policy, not
    issuer certification. Any reported event requires a separate verified
    adjustment adapter; never silently compare raw entry to adjusted exit.
    """
    if payload.get('calendar')!=expected['calendar_id'] or payload.get('interval')!='1d' or payload.get('adjusted') is not True:raise ValueError('EXIT_HISTORY_BASIS_INVALID')
    quality=payload.get('data_quality',{})
    if quality.get('calendar_validated') is not True or not payload.get('generated_at_utc') or not payload.get('source'):raise ValueError('EXIT_HISTORY_UNVERIFIED')
    fetched=stamp(payload['generated_at_utc']);seen=stamp(as_of)
    if not stamp(expected['close_at'])<=fetched<=seen:raise ValueError('EXIT_HISTORY_BEFORE_CLOSE')
    if 'actions' not in payload or not isinstance(payload['actions'],list):raise ValueError('EXIT_CORPORATE_ACTIONS_MISSING')
    # An event outside this interval still needs an adjustment-factor bridge:
    # Yahoo retro-adjustment can change historical prices before later events.
    if payload['actions']:raise ValueError('EXIT_CORPORATE_ACTION_ADJUSTMENT_REQUIRED')
    if payload.get('ticker')!=entry.get('ticker') or not entry.get('ticker'):raise ValueError('EXIT_HISTORY_IDENTITY_MISMATCH')
    if not payload.get('coverage_start') or payload['coverage_start']>stamp(entry['timestamp']).date().isoformat():raise ValueError('EXIT_ACTIONS_INTERVAL_NOT_COVERED')
    rows=[r for r in payload.get('history',[]) if r.get('date')==expected['session_date']]
    if len(rows)!=1:raise ValueError('EXIT_SESSION_BAR_MISSING_OR_DUPLICATE')
    scale=payload.get('price_scale_to_major_currency',1)
    if not finite(scale) or scale<=0 or payload.get('major_currency',payload.get('currency'))!=entry.get('currency'):raise ValueError('EXIT_CURRENCY_OR_UNIT_MISMATCH')
    return dict(kind='SESSION_CLOSE',instrument_id=entry['instrument_id'],calendar_id=expected['calendar_id'],session_date=expected['session_date'],
        valued_at=expected['close_at'],retrieved_at=payload['generated_at_utc'],price_basis=expected['price_basis'],
        close=rows[0]['close']*scale,fx_to_usd=fx_to_usd,fx_reference=fx_reference,entry_quote_hash=digest(entry),entry_basis_verified=True,
        source_reference=payload['source']+'; payload_sha256='+digest(payload),
        adjustments_reference=payload.get('adjustment_method','')+'; source-reported no actions; not issuer certification')
