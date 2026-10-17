"""Prepara offline lo storico ufficiale BCE, senza rete o riempimenti."""
import argparse
import copy
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ADAPTER_VERSION = 'ecb-fx-adapter 1.0.0'
HISTORY_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml'
METHOD_URL = 'https://www.ecb.europa.eu/stats/pdf/exchange/Frameworkfortheeuroforeignexchangereferencerates.en.pdf'
CALENDAR_URL = 'https://eur-lex.europa.eu/eli/guideline/2022/912/oj/eng'
ANNUAL_URL = 'https://www.ecb.europa.eu/ecb/contacts/working-hours/html/index.en.html'
NS = '{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}'
ZONE = ZoneInfo('Europe/Berlin')


def aware(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp senza fuso')
    return d


def easter(year):
    # Algoritmo gregoriano Meeus/Jones/Butcher.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    v = h + l - 7*m + 114
    return date(year, v // 31, v % 31 + 1)


def target_open(day):
    # Copertura congelata al perimetro documentato e verificato: niente
    # presunzioni sui calendari precedenti al nuovo TARGET o dopo il 2028.
    if not 2023 <= day.year <= 2028:
        raise ValueError('Calendario TARGET supportato 2023–2028; aggiornare fonti fuori copertura')
    e = easter(day.year)
    holidays = {date(day.year, 1, 1), e-timedelta(days=2), e+timedelta(days=1),
                date(day.year, 5, 1), date(day.year, 12, 25), date(day.year, 12, 26)}
    return day.weekday() < 5 and day not in holidays


def prepare_ecb(xml_bytes, start, end, as_of, retrieved_at, source_url=HISTORY_URL):
    now, fetched = aware(as_of), aware(retrieved_at)
    begin, last = date.fromisoformat(start), date.fromisoformat(end)
    if begin > last or last < now.astimezone(ZONE).date():
        raise ValueError('Copertura inclusiva non valida o precedente ad as_of')
    if fetched > now or now-fetched > timedelta(days=1):
        raise ValueError('Acquisizione futura o non aggiornata entro 24 ore')
    if source_url != HISTORY_URL:
        raise ValueError('Fonte richiesta: XML storico ufficiale BCE, nessun fallback')
    if len(xml_bytes) > 20_000_000 or b'<!DOCTYPE' in xml_bytes.upper() or b'<!ENTITY' in xml_bytes.upper():
        raise ValueError('Documento XML non ammesso')
    root = ET.fromstring(xml_bytes)
    if root.tag != '{http://www.gesmes.org/xml/2002-08-01}Envelope':
        raise ValueError('Envelope BCE non valido')
    sender = root.find('{http://www.gesmes.org/xml/2002-08-01}Sender')
    if sender is None or 'European Central Bank' not in ''.join(sender.itertext()):
        raise ValueError('Mittente BCE non valido')
    rows = {}
    for cube in root.iter(NS+'Cube'):
        if 'time' not in cube.attrib:
            continue
        day = date.fromisoformat(cube.attrib['time'])
        if not begin <= day <= last:
            continue
        if day.isoformat() in rows or not target_open(day):
            raise ValueError('Data duplicata o osservazione in giorno non TARGET')
        rates = {'EUR': 1.0}
        for item in cube:
            currency = item.get('currency')
            if not currency or currency in rates:
                raise ValueError('Valuta mancante o duplicata')
            value = float(item.attrib['rate'])
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Cambio BCE non positivo/finito')
            rates[currency] = value
        if 'USD' not in rates:
            raise ValueError('USD assente nella data BCE')
        rows[day.isoformat()] = rates
    sessions, expected = [], []
    cursor = begin
    while cursor <= last:
        if target_open(cursor):
            # 16:30 è una politica prudenziale di disponibilità, non l'ora
            # effettiva di fixing/pubblicazione. Richiedere anche il dato
            # realmente ricevuto. Il rate è determinato circa alle 14:10.
            cutoff = datetime.combine(cursor, time(16, 30), ZONE)
            sessions.append({'date': cursor.isoformat(), 'close_timestamp': cutoff.isoformat(),
                             'timestamp_semantics': 'conservative_reference_rate_availability_cutoff'})
            if cutoff <= now:
                expected.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if not expected:
        raise ValueError('Nessuna osservazione BCE attesa nel periodo')
    missing = [day for day in expected if day not in rows]
    history = [{'date': day, 'usd_to_eur': 1.0/rows[day]['USD']} for day in expected if day in rows]
    for row in history:
        if not math.isfinite(row['usd_to_eur']) or row['usd_to_eur'] <= 0:
            raise ValueError('Inversione USD/EUR non finita/positiva')
    # Nessuna certificazione con buchi, nemmeno se l'ultimo dato è presente.
    usable = not missing and bool(history)
    digest = hashlib.sha256(xml_bytes).hexdigest()
    convention = {'reference': METHOD_URL, 'calendar_reference': CALENDAR_URL,
                  'annual_calendar_reference': ANNUAL_URL, 'timezone': str(ZONE),
                  'observation': 'ECB reference rate determined around 14:10 ECB local time',
                  'publication': 'usually around 16:00 ECB local time',
                  'eligibility_policy': '16:30 Europe/Berlin plus observed XML record',
                  'market_close': False, 'execution_rate': False,
                  'alignment': 'same dated endpoints only; no forward fill; asynchronous to market closes'}
    source = {'name': 'European Central Bank', 'data_type': 'fx', 'timestamp': retrieved_at,
              'url': source_url, 'identifier': 'ECB eurofxref daily USD/EUR', 'sha256': digest,
              'methodology': convention}
    expected_set = set(expected)
    conversions = {day: {currency: rates['USD']/value for currency, value in rates.items()}
                   for day, rates in sorted(rows.items()) if day in expected_set}
    if any(not math.isfinite(v) or v <= 0 for rates in conversions.values() for v in rates.values()):
        raise ValueError('Conversione storica non finita/positiva')
    return {'adapter_version': ADAPTER_VERSION,
            'status': 'PREPARATO_DA_VALIDARE_NEL_MOTORE' if usable else 'BLOCCATO_PER_DATI',
            'errors': [] if usable else ['Osservazioni BCE attese mancanti'],
            'quality': {'expected_last_date': expected[-1], 'received_last_date': history[-1]['date'] if history else None,
                        'missing_dates': missing, 'prices': len(history),
                        'daily_returns': max(0, len(history)-1) if usable else None},
            'calendar': {'timezone': str(ZONE), 'source': CALENDAR_URL,
                         'validated_at': retrieved_at, 'coverage_start': start, 'coverage_end': end,
                         'session_semantics': 'ECB reference observation availability, not FX market close',
                         'sessions': sessions},
            'usd_eur_history': {'source': 'ECB eurofxref | '+METHOD_URL,
                               'calendar_id': 'FX_BCE', 'usable_for_risk': usable,
                               'convention': convention, 'history': history},
            'fx_to_usd_by_date': conversions,
            'sources': [source, {'name': 'TARGET operating calendar', 'data_type': 'calendar',
                                'timestamp': retrieved_at, 'url': CALENDAR_URL,
                                'identifier': 'TARGET 2023–2028 official rules, ECB local timezone'}],
            'warnings': ['BCE reference observation is asynchronous to equity closes; match date endpoints, not timestamps',
                         'Current eToro valuation/execution FX remains separate']}


def attach_ecb_fx(payload, prepared):
    """Collega lo storico separato senza cambiare conto, quote o prezzi titoli.

    La conversione dei prezzi non USD avviene solo sulle medesime date.
    Un tasso mancante resta None e rende la serie inutilizzabile.
    """
    result = copy.deepcopy(payload)
    if prepared.get('status') != 'PREPARATO_DA_VALIDARE_NEL_MOTORE':
        raise ValueError('Storico BCE non utilizzabile')
    result.setdefault('calendars', {})['FX_BCE'] = copy.deepcopy(prepared['calendar'])
    result['usd_eur_history'] = copy.deepcopy(prepared['usd_eur_history'])
    # Conservare la provenienza del cambio corrente del conto e dell'input
    # precedente. La serie scelta è esplicita in usd_eur_history/convention.
    result['sources'] = list(result.get('sources', []))
    result['sources'].extend(copy.deepcopy(prepared['sources']))
    notes = []
    assets = list(result.get('positions', []))
    if isinstance(result.get('candidate'), dict):
        assets.append(result['candidate'])
    for asset in assets:
        currency = asset.get('currency')
        # Normalizzazione pence/centesimi va eseguita nell'acquisizione Yahoo,
        # non qui: non cambiare quantità o quote correnti del conto.
        if currency not in ('USD', 'EUR', 'GBP', 'CHF', 'DKK', 'NOK', 'SEK', 'JPY', 'CAD', 'AUD', 'NZD'):
            asset['usable_for_risk'] = False
            notes.append(str(asset.get('ticker'))+': valuta storica/unità da normalizzare o non supportata')
            continue
        retained, excluded = [], []
        for row in asset.get('history', []):
            if currency == 'USD':
                row['fx_to_usd'] = 1.0
            else:
                value = prepared['fx_to_usd_by_date'].get(row.get('date'), {}).get(currency)
                row['fx_to_usd'] = value
                if value is None:
                    day = date.fromisoformat(row['date'])
                    begin = date.fromisoformat(prepared['calendar']['coverage_start'])
                    end = date.fromisoformat(prepared['calendar']['coverage_end'])
                    if begin <= day <= end and not target_open(day):
                        # Giorno negoziato dal titolo ma senza pubblicazione
                        # TARGET: omettere prezzo convertibile, mantenendo il
                        # calendario originale del titolo. Il motore perde
                        # i due rendimenti adiacenti senza creare un ponte.
                        excluded.append(row['date'])
                        continue
                    asset['usable_for_risk'] = False
                    notes.append(str(asset.get('ticker'))+': tasso BCE assente il '+str(row.get('date')))
            retained.append(row)
        asset['history'] = retained
        asset['history_fx_exclusions'] = excluded
        if excluded:
            notes.append(str(asset.get('ticker'))+': esclusi '+str(len(excluded))+' prezzi senza pubblicazione TARGET; nessun riempimento')
    return result, notes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('xml', 'start', 'end', 'as-of', 'retrieved-at', 'output'):
        p.add_argument('--'+flag, required=True)
    args = p.parse_args()
    try:
        result = prepare_ecb(Path(args.xml).read_bytes(), args.start, args.end, args.as_of, args.retrieved_at)
    except (ValueError, KeyError, ET.ParseError, OverflowError) as exc:
        result = {'status': 'BLOCCATO_PER_DATI', 'errors': [str(exc)],
                  'adapter_version': ADAPTER_VERSION, 'usd_eur_history': None, 'calendar': None}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return 2 if result.get('errors') else 0


if __name__ == '__main__':
    raise SystemExit(main())
