"""1.0.0 — User-authorized eToro prices + Yahoo-reported corporate actions.
Acceptance of a two-provider snapshot is NOT an issuer certificate of no events.
No fabricated bars, fills, or transfer of Yahoo adjusted price levels.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from etoro_corporate_actions import build_adjustments

VERSION = '1.0.0'
POLICY_ID = 'ETORO_YAHOO_USER_POLICY_2026_10_17'
AUTHORIZATION_TEXT = 'fidandoti di quello che trovi dalle due fonti etoro e yahoo'
AUTHORIZATION_SHA256 = hashlib.sha256(AUTHORIZATION_TEXT.encode()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def build(entry, response, calendar, as_of, evidence):
    now = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    fetched = datetime.fromisoformat(evidence['retrieved_at'].replace('Z', '+00:00'))
    if fetched.tzinfo is None or now.tzinfo is None or fetched > now or now-fetched > timedelta(minutes=20):
        raise ValueError('DUAL_SOURCE_YAHOO_SNAPSHOT_EXPIRED')
    body = evidence['document']
    chart = body.get('chart', {})
    result = chart.get('result')
    if chart.get('error') or not isinstance(result,list) or len(result) != 1:
        raise ValueError('DUAL_SOURCE_YAHOO_RESPONSE_INVALID')
    result = result[0];meta = result.get('meta', {})
    instrument = entry['instrument'];ticker = instrument['symbol'];currency = entry['currency']['code']
    if meta.get('symbol','').upper() != ticker or meta.get('instrumentType','').upper() != 'ETF' or meta.get('currency') != currency or meta.get('exchangeTimezoneName') != calendar['timezone']:
        raise ValueError('DUAL_SOURCE_IDENTITY_CURRENCY_OR_CALENDAR_MISMATCH')
    completed = sorted(r['date'] for r in calendar['sessions'] if datetime.fromisoformat(r['close_timestamp'].replace('Z','+00:00')) <= now)
    if not completed:raise ValueError('DUAL_SOURCE_NO_COMPLETED_SESSION')
    expected = completed[-1]
    if evidence.get('query_start') != calendar['start'] or evidence.get('query_end','') <= expected:
        raise ValueError('DUAL_SOURCE_QUERY_RANGE_INSUFFICIENT')
    stamps=result.get('timestamp') or []
    quotes=(result.get('indicators',{}).get('quote') or [{}])[0]
    closes=quotes.get('close') or []
    adjusted=(result.get('indicators',{}).get('adjclose') or [{}])[0].get('adjclose') or []
    if not stamps or len(stamps)!=len(closes) or len(stamps)!=len(adjusted):
        raise ValueError('DUAL_SOURCE_YAHOO_COLUMNS_INVALID')
    zone=ZoneInfo(calendar['timezone']);prices={};ratios={}
    def positive(x):return type(x) in (int,float) and math.isfinite(x) and x>0
    for ts, close, adj in zip(stamps,closes,adjusted):
        day=datetime.fromtimestamp(ts,zone).date().isoformat()
        if day>expected:continue
        if not positive(close) or not positive(adj):continue
        if day in prices:raise ValueError('DUAL_SOURCE_YAHOO_DUPLICATE_DATE')
        prices[day]=close;ratios[day]=adj/close
    if not prices:raise ValueError('DUAL_SOURCE_YAHOO_PRICES_EMPTY')
    yahoo_last=max(prices)
    if yahoo_last not in completed or len([d for d in completed if d>yahoo_last])>1:
        raise ValueError('DUAL_SOURCE_YAHOO_CONFIRMATION_TOO_OLD')
    groups=response['body'] if isinstance(response['body'],dict) else json.loads(response['body'])
    raw=groups['candles'][0]['candles']
    broker={r['fromDate'][:10]:r['close'] for r in raw if r['fromDate'][:10] in completed and positive(r.get('close'))}
    if not broker:raise ValueError('DUAL_SOURCE_ETORO_EMPTY')
    first=min(broker)
    # Require observed Yahoo prices across the beginning and end of the broker window.
    first_sessions=[d for d in completed if first<=d]
    if min(prices)>first_sessions[min(1,len(first_sessions)-1)]:
        raise ValueError('DUAL_SOURCE_YAHOO_START_COVERAGE_INSUFFICIENT')
    events=result.get('events') or {}
    if not isinstance(events,dict):raise ValueError('DUAL_SOURCE_YAHOO_EVENTS_INVALID')
    if any(v for k,v in events.items() if k not in {'dividends','splits'}):
        raise ValueError('DUAL_SOURCE_UNSUPPORTED_CORPORATE_EVENT')
    ledger_events=[]
    for category in ('splits','dividends'):
        for key,event in (events.get(category) or {}).items():
            day=datetime.fromtimestamp(int(event.get('date',key)),zone).date().isoformat()
            if not first<=day<=expected:continue
            reference=evidence['url']+'#'+category+':'+str(key)
            if category=='splits':
                new,old=event.get('numerator'),event.get('denominator')
                if not positive(new) or not positive(old):raise ValueError('DUAL_SOURCE_SPLIT_RATIO_INVALID')
                ledger_events.append({'type':'SPLIT','ex_date':day,'new_shares':new,'old_shares':old,'reference':reference})
            else:
                amount=event.get('amount')
                if not positive(amount):raise ValueError('DUAL_SOURCE_DIVIDEND_INVALID')
                ledger_events.append({'type':'CASH_DISTRIBUTION','ex_date':day,'amount_per_share':amount,'currency':currency,'reference':reference})
    if not any(x['type']=='CASH_DISTRIBUTION' for x in ledger_events):
        if any(not math.isclose(x,1.0,rel_tol=1e-7,abs_tol=1e-8) for x in ratios.values()):
            raise ValueError('DUAL_SOURCE_ADJCLOSE_DISAGREES_WITH_REPORTED_EVENTS')
    overlaps=sorted(set(broker)&set(prices))
    if len(overlaps)<60:raise ValueError('DUAL_SOURCE_OVERLAP_INSUFFICIENT')
    # Report price differences; do not invent a financial threshold for broker bid vs exchange close.
    differences=[abs(broker[d]/prices[d]-1)*100 for d in overlaps]
    policy={'policy_id':POLICY_ID,'ticker':ticker,'instrument_id':instrument['instrumentId'],'currency':currency,'authorization_sha256':AUTHORIZATION_SHA256,
            'authorized_on':'2026-10-17','verification_basis':'USER_AUTHORIZED_TWO_PROVIDER_SNAPSHOT',
            'issuer_certified':False,'yahoo_events_reported':len(ledger_events),
            'yahoo_snapshot_sha256':fingerprint(body),'etoro_snapshot_sha256':fingerprint(groups),
            'yahoo_url':evidence['url'],'yahoo_retrieved_at':evidence['retrieved_at'],
            'query_start':first,'query_end_exclusive':evidence['query_end'],
            'last_yahoo_price_date':yahoo_last,'last_etoro_price_date':max(broker),
            'overlap_bars':len(overlaps),'max_observed_price_difference_pct':max(differences),
            'limitations':['No issuer-certified exhaustive corporate-action ledger',
                          'No reported Yahoo events means none reported in this snapshot, not guaranteed absence',
                          'Yahoo price endpoint may lag one session; latest broker session accepted by explicit user policy',
                          'eToro raw/unadjusted price basis follows user-supplied provider description']}
    # Reuse the existing factor implementation, with explicit two-source basis in its reference.
    ledger={'verified':True,'coverage_complete':True,'instrument_id':instrument['instrumentId'],
            'ticker':ticker,'currency':currency,'raw_price_basis':'UNADJUSTED',
            'raw_price_basis_reference':'User-supplied eToro description; '+POLICY_ID,
            'reference':POLICY_ID+' | Yahoo reported events | sha256='+policy['yahoo_snapshot_sha256'],
            'checked_at':as_of,'coverage_start':first,'coverage_end':expected,'events':ledger_events}
    adjusted_entry=dict(entry,corporate_actions=ledger)
    factors=build_adjustments(adjusted_entry,response,calendar,as_of)
    factors['evidence_policy']=policy
    return factors,policy
