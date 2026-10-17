"""Version 1.0.0 — offline backward adjustment of documented raw ETF quotes.
No guessed events. Empty events require an explicitly verified complete ledger.
"""
import hashlib
import json
import math
from datetime import date, datetime, timezone

VERSION = '1.0.0'


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def build_adjustments(entry, response, calendar, as_of):
    ledger = entry.get('corporate_actions')
    if not isinstance(ledger, dict) or ledger.get('verified') is not True:
        raise ValueError('CORPORATE_ACTIONS_COVERAGE_NOT_VERIFIED')
    instrument = entry['instrument']
    if (ledger.get('instrument_id') != instrument['instrumentId']
            or ledger.get('ticker') != instrument['symbol']
            or ledger.get('currency') != entry['currency']['code']
            or ledger.get('raw_price_basis') != 'UNADJUSTED'
            or not ledger.get('raw_price_basis_reference')
            or ledger.get('coverage_complete') is not True
            or not ledger.get('reference') or not ledger.get('checked_at')):
        raise ValueError('CORPORATE_ACTIONS_IDENTITY_OR_EVIDENCE_INVALID')
    now = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    checked = datetime.fromisoformat(ledger['checked_at'].replace('Z', '+00:00'))
    if now.tzinfo is None or checked.tzinfo is None or checked > now:
        raise ValueError('CORPORATE_ACTIONS_TIMESTAMP_INVALID')
    begin = date.fromisoformat(ledger['coverage_start']).isoformat()
    end = date.fromisoformat(ledger['coverage_end']).isoformat()
    if checked.date().isoformat() < end or begin > end:
        raise ValueError('CORPORATE_ACTIONS_COVERAGE_INVALID')
    body = response['body']
    if isinstance(body, str):body = json.loads(body)
    groups = body.get('candles')
    if body.get('interval') != 'OneDay' or not isinstance(groups, list) or len(groups) != 1 or groups[0].get('instrumentId') != instrument['instrumentId']:
        raise ValueError('CORPORATE_ACTIONS_WRONG_RESPONSE')
    completed = sorted(r['date'] for r in calendar['sessions']
                       if datetime.fromisoformat(r['close_timestamp'].replace('Z','+00:00')) <= now)
    if not completed or end < completed[-1]:
        raise ValueError('CORPORATE_ACTIONS_COVERAGE_EXPIRED')
    rows = {}
    for row in groups[0].get('candles', []):
        stamp = datetime.fromisoformat(row['fromDate'].replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0 or stamp.hour or stamp.minute or stamp.second or stamp.microsecond:
            raise ValueError('CORPORATE_ACTIONS_DATE_CONVENTION_INVALID')
        day = stamp.date().isoformat()
        if row.get('instrumentID') != instrument['instrumentId'] or day in rows:
            raise ValueError('CORPORATE_ACTIONS_DUPLICATE_OR_WRONG_ID')
        rows[day] = row
    days = sorted(d for d in rows if d in set(completed))
    if not days or begin > days[0] or end < days[-1]:
        raise ValueError('CORPORATE_ACTIONS_HISTORY_OUTSIDE_COVERAGE')
    events = ledger.get('events')
    if not isinstance(events, list):raise ValueError('CORPORATE_ACTIONS_EVENTS_MISSING')
    multipliers, seen = [], set()
    for event in events:
        ex = date.fromisoformat(event['ex_date']).isoformat()
        if ex in seen:raise ValueError('CORPORATE_ACTIONS_SAME_DATE_REQUIRES_EXPLICIT_RECONCILIATION')
        seen.add(ex)
        if not begin <= ex <= end or ex not in completed or not event.get('reference'):
            raise ValueError('CORPORATE_ACTIONS_EVENT_DATE_OR_REFERENCE_INVALID')
        kind = event.get('type')
        if kind == 'SPLIT':
            new, old = event.get('new_shares'), event.get('old_shares')
            if not positive(new) or not positive(old):raise ValueError('CORPORATE_ACTIONS_SPLIT_INVALID')
            factor = old / new
        elif kind == 'CASH_DISTRIBUTION':
            if event.get('currency') != ledger['currency'] or not positive(event.get('amount_per_share')):
                raise ValueError('CORPORATE_ACTIONS_DIVIDEND_CURRENCY_OR_AMOUNT_INVALID')
            index = completed.index(ex)
            previous = completed[index-1] if index else None
            if previous not in rows or not positive(rows[previous].get('close')):
                raise ValueError('CORPORATE_ACTIONS_PREVIOUS_SESSION_CLOSE_MISSING')
            factor = 1 - event['amount_per_share'] / rows[previous]['close']
        else:raise ValueError('CORPORATE_ACTIONS_EVENT_TYPE_UNSUPPORTED')
        if not positive(factor):raise ValueError('CORPORATE_ACTIONS_FACTOR_INVALID')
        multipliers.append((ex, factor))
    factors = {day:math.prod(f for ex, f in multipliers if day < ex) for day in days}
    if not all(positive(f) for f in factors.values()):raise ValueError('CORPORATE_ACTIONS_COMPOUND_FACTOR_INVALID')
    fingerprint = hashlib.sha256(json.dumps(ledger, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {'verified':True, 'instrument_id':instrument['instrumentId'], 'ticker':instrument['symbol'],
            'reference':ledger['reference']+' | ledger_sha256='+fingerprint,
            'checked_at':ledger['checked_at'], 'coverage_start':begin,'coverage_end':end,
            'method':'EXPLICIT_TOTAL_RETURN_FACTORS', 'factors_by_date':factors,
            'raw_price_basis':'UNADJUSTED','events_count':len(events),'ledger_sha256':fingerprint}
