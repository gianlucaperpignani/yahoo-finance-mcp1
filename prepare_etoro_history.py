#!/usr/bin/env python3
"""Validate eToro daily broker candles offline; never infer adjusted prices or FX."""
import argparse
import copy
import hashlib
import json
import math
from datetime import datetime, time, timezone
from pathlib import Path

VERSION = '1.1.0'
SOURCE = 'ETORO_DAILY_BID'


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp senza fuso')
    return parsed


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def envelope(response):
    if response.get('statusCode') != 200 or response.get('isSuccess') is not True or response.get('bodyTruncated'):
        raise ValueError('Risposta eToro fallita o troncata')
    body = response.get('body')
    return json.loads(body) if isinstance(body, str) else body


def evidence_ok(item, as_of):
    if not isinstance(item, dict) or item.get('verified') is not True or not item.get('reference'):
        return False
    try:
        return stamp(item['checked_at']) <= as_of
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def prepare(data):
    now = stamp(data['as_of'])
    body = envelope(data['response'])
    instrument = data['instrument']
    ident = data.get('identity', {})
    if type(instrument.get('instrumentId')) is not int or instrument.get('type') != 'ETF' or not instrument.get('symbol'):
        raise ValueError('Identita ETF non valida')
    if not evidence_ok(ident, now) or ident.get('status') != 'PASS' or ident.get('instrument_id') != instrument['instrumentId'] or ident.get('ticker') != instrument['symbol']:
        raise ValueError('Identita broker non attestata per ID/ticker')
    currency = data.get('currency', {})
    if not evidence_ok(currency, now) or currency.get('instrument_id') != instrument['instrumentId'] or currency.get('ticker') != instrument['symbol'] or currency.get('code') not in {'USD', 'EUR', 'GBP', 'CHF', 'DKK', 'NOK', 'SEK'}:
        raise ValueError('Valuta/unità della specifica quotazione non verificate')
    calendar = data['calendar']
    if not calendar.get('source') or not calendar.get('timezone') or not calendar.get('sessions'):
        raise ValueError('Calendario documentato assente')
    if not calendar.get('generated_at_utc') or stamp(calendar['generated_at_utc']) > now:
        raise ValueError('Acquisizione calendario non valida')
    sessions = {}
    for row in calendar['sessions']:
        day = row['date']
        if day in sessions:
            raise ValueError('Seduta calendario duplicata')
        sessions[day] = row
    dates = sorted(sessions)
    if dates != [r['date'] for r in calendar['sessions']]:
        raise ValueError('Calendario non ordinato')
    if not calendar.get('start') or not calendar.get('end') or calendar['end'] <= now.date().isoformat():
        raise ValueError('Copertura calendario insufficiente')
    completed = [d for d in dates if stamp(sessions[d]['close_timestamp']) <= now]
    if not completed:
        raise ValueError('Nessuna seduta conclusa nel calendario')
    if body.get('interval') != 'OneDay':
        raise ValueError('Usare la route history/candles con intervallo OneDay')
    groups = body.get('candles')
    if not isinstance(groups, list) or len(groups) != 1 or groups[0].get('instrumentId') != instrument['instrumentId']:
        raise ValueError('Storico di uno strumento diverso o ambiguo')
    raw = groups[0].get('candles')
    if not isinstance(raw, list) or not raw:
        raise ValueError('Storico eToro vuoto')
    retained, excluded, seen = [], [], set()
    for row in raw:
        if row.get('instrumentID') != instrument['instrumentId']:
            raise ValueError('instrumentID incoerente sulla barra')
        dt = stamp(row['fromDate'])
        # Legacy history route labels the trading date at 00:00 UTC.
        # Do not apply this rule to the separate /data route's previous-evening timestamps.
        if dt.utcoffset().total_seconds() != 0 or dt.time() != time(0):
            raise ValueError('Convenzione data giornaliera non verificata')
        day = dt.date().isoformat()
        if day in seen:
            raise ValueError('Data duplicata: '+day)
        seen.add(day)
        if not calendar['start'] <= day < calendar['end']:
            raise ValueError('Barra fuori copertura calendario: '+day)
        if day not in sessions:
            excluded.append({'date': day, 'reason': 'NON_TRADING_SESSION'})
            continue
        if day not in completed:
            excluded.append({'date': day, 'reason': 'SESSION_NOT_COMPLETED'})
            continue
        if not all(positive(row.get(k)) for k in ('open', 'high', 'low', 'close')):
            excluded.append({'date': day, 'reason': 'INVALID_PRICE'})
            continue
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            excluded.append({'date': day, 'reason': 'INVALID_OHLC'})
            continue
        volume = row.get('volume')
        if type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0:
            volume = None
        retained.append({'date': day, **{k:row[k] for k in ('open','high','low','close')}, 'volume':volume})
    retained.sort(key=lambda row:row['date'])
    if not retained:
        raise ValueError('Nessuna barra utilizzabile')
    first, latest = retained[0]['date'], retained[-1]['date']
    expected = [d for d in completed if d >= first]
    got = {r['date'] for r in retained}
    missing = [d for d in expected if d not in got]
    stale = len([d for d in completed if d > latest])
    provisional = stale == 1 and instrument['symbol'].endswith(('.L','.DE','.PA','.MI','.AS','.MC','.SW','.ST','.CO','.OL','.HE'))
    pairs = [(a,b) for a,b in zip(expected,expected[1:]) if a in got and b in got]
    last61 = [d for d in completed if d <= latest][-61:]
    screening = (stale == 0 or provisional) and len(last61) == 61 and all(d in got for d in last61)
    adjustment = data.get('adjustments', {})
    adjusted = evidence_ok(adjustment, now) and adjustment.get('instrument_id') == instrument['instrumentId'] and adjustment.get('ticker') == instrument['symbol'] and adjustment.get('coverage_start','9999') <= first and adjustment.get('coverage_end','') >= latest
    factors = None
    if adjusted:
        method = adjustment.get('method')
        if method == 'EXPLICIT_TOTAL_RETURN_FACTORS':
            factors = adjustment.get('factors_by_date', {})
            adjusted = all(positive(factors.get(d)) for d in got)
        elif method != 'PROVIDER_SPLIT_AND_DIVIDEND_ADJUSTED':
            adjusted = False
    risk_ready = bool(adjusted and len(pairs) >= 200 and (stale == 0 or provisional)
                      and len(expected) >= 2 and expected[-2] in got)
    issues = []
    if not adjusted:issues.append('RETTIFICHE_SPLIT_DIVIDENDI_NON_VERIFICATE')
    if len(pairs)<200:issues.append('CAMPIONE_RENDIMENTI_INSUFFICIENTE')
    if stale>1:issues.append('STORICO_OLTRE_TOLLERANZA')
    if len(expected)<2 or expected[-2] not in got:issues.append('ULTIMO_RENDIMENTO_NON_DISPONIBILE')
    asset = None
    if risk_ready:
        asset = {'instrument_id':instrument['instrumentId'],'ticker':instrument['symbol'],'asset_class':'ETF',
                 'currency':currency['code'],'calendar_id':calendar['calendar'],
                 'history_source':SOURCE,'history_adjusted':True,'usable_for_risk':True,
                 'adjustments_reference':adjustment['reference'],'storico_provvisorio':provisional,
                 'history':[{'date':r['date'],'adjusted_close':r['close']*(factors[r['date']] if factors else 1),
                             'fx_to_usd':1.0 if currency['code']=='USD' else None} for r in retained]}
    if asset is not None and adjustment.get('evidence_policy'):
        asset['history_evidence_policy'] = copy.deepcopy(adjustment['evidence_policy'])
    return {'adapter_version':VERSION,'source':SOURCE,'input_hash':digest(data),'instrument_id':instrument['instrumentId'],
            'ticker':instrument['symbol'],'currency':currency['code'],'price_side':'bid','as_of':data['as_of'],
            'status':'PREPARATO_DA_VALIDARE_NEL_MOTORE' if risk_ready else 'PREPARATO_PER_ANALISI_NON_VALIDATO_PER_RISCHIO',
            'coverage_start':first,'coverage_end':latest,'last_completed_session_expected':completed[-1],
            'data_current':stale==0,'storico_provvisorio':provisional,'history_adjusted':bool(adjusted),
            'usable_for_screening':bool(screening),'usable_for_risk':risk_ready,'ticket_authorized':False,
            'risk_blockers':issues,'next_action':'verificare rettifiche split/dividendi presso il provider' if not adjusted else 'ricalcolare rischio e completare tutti i gate del ticket',
            'data_quality':{'stale_sessions':stale,'bars_received':len(raw),'bars_retained':len(retained),'valid_return_pairs':len(pairs),
                            'missing_sessions':missing,'excluded':excluded,'no_interpolation':True,'no_bridge_returns':True},
            'history':retained,'risk_asset':asset,
            'audit':{'x_request_id':data['response'].get('xRequestId'),'identity':ident,'currency_evidence':currency,
                     'adjustment_evidence':adjustment,'calendar_source':calendar['source'],
                     'source_path':f"/api/v1/market-data/instruments/{instrument['instrumentId']}/history/candles/desc/OneDay/1000",
                     'date_rule':'legacy OneDay fromDate at 00:00 UTC is trading-session date; /data route is not accepted'}}


def prepare_server_history(payload):
    """Accept actual full server get_market_data or get_etf_history responses.

    The caller must acquire the response, not synthesize evidence metadata.
    The risk engine separately validates the authorization, calendar and sample.
    """
    if payload.get('source') != SOURCE:
        raise ValueError('Fonte server diversa da eToro')
    if payload.get('risk_asset'):
        return copy.deepcopy(payload)
    if payload.get('adjusted') is not True or not payload.get('history_evidence_policy'):
        raise ValueError('Rettifiche/policy server non presenti')
    if type(payload.get('instrument_id')) is not int:
        raise ValueError('instrument_id server mancante o non valido')
    history=payload.get('history')
    if not isinstance(history,list) or len(history)<201:
        raise ValueError('Risposta server priva delle barre necessarie; usare formato full')
    reference=(payload.get('adjustment_evidence') or {}).get('reference')
    if not reference:raise ValueError('Riferimento rettifiche server mancante')
    asset={'instrument_id':payload.get('instrument_id'),'ticker':payload['ticker'],'currency':payload['currency'],
           'asset_class':'ETF','calendar_id':payload['calendar'],'history_source':SOURCE,'history_adjusted':True,
           'usable_for_risk':bool((payload.get('data_quality') or {}).get('risk_sample_sufficient')),
           'adjustments_reference':reference,'storico_provvisorio':bool(payload.get('storico_provvisorio')),
           'history_evidence_policy':copy.deepcopy(payload['history_evidence_policy']),
           'history':[{'date':r['date'],'adjusted_close':r['close'],'fx_to_usd':1.0 if payload['currency']=='USD' else None} for r in history]}
    return {'source':SOURCE,'ticker':payload['ticker'],'currency':payload['currency'],'as_of':payload['generated_at_utc'],
            'input_hash':digest(payload),'usable_for_risk':asset['usable_for_risk'],'risk_asset':asset,
            'risk_blockers':[] if asset['usable_for_risk'] else ['CAMPIONE_SERVER_NON_VALIDATO'],
            'ticket_authorized':False,'audit':{'x_request_id':None}}


def attach_prepared_history(template, prepared):
    if prepared.get('usable_for_risk') is not True or not prepared.get('risk_asset'):
        raise ValueError('Storico eToro non validato per rischio: '+','.join(prepared.get('risk_blockers',[])))
    result = copy.deepcopy(template)
    asset = prepared['risk_asset']
    targets = [r for r in result.get('positions',[]) if r.get('instrument_id')==asset['instrument_id'] and r.get('ticker')==asset['ticker']]
    if len(targets)!=1 or targets[0].get('currency')!=asset['currency'] or targets[0].get('asset_class')!='ETF':
        raise ValueError('Associazione storico/posizione/valuta non univoca')
    for field in ('history','history_source','history_adjusted','usable_for_risk','adjustments_reference','storico_provvisorio','calendar_id'):
        targets[0][field] = copy.deepcopy(asset[field])
    if asset.get('history_evidence_policy'):
        targets[0]['history_evidence_policy'] = copy.deepcopy(asset['history_evidence_policy'])
    result.setdefault('sources',[]).append({'name':'eToro historical daily bid candles','data_type':'prices',
        'timestamp':prepared['as_of'],'identifier':prepared['audit']['x_request_id'] or prepared['input_hash']})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('input',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    try:
        result=prepare(json.loads(args.input.read_text()))
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        result={'adapter_version':VERSION,'status':'BLOCCATO_PER_DATI','errors':[str(exc)],'usable_for_risk':False,'ticket_authorized':False}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return 2 if result['status']=='BLOCCATO_PER_DATI' else 0

if __name__=='__main__':raise SystemExit(main())
