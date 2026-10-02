import os
import time
import json
import math
import re
import csv
import io
import html
import hashlib
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from threading import Lock
from zoneinfo import ZoneInfo
from datetime import date, datetime, time as dt_time, timedelta, timezone
from flask import Flask, jsonify, request
import pandas as pd

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 1800  # 30 minuti
CACHE_LOCK = Lock()
VERSION = "1.5.1"
MAX_RETURNED_BARS = 300
MIN_RISK_PRICES = 251
OHLC_REL_TOLERANCE = 1e-6
HTTP_TIMEOUT = 10
ETF_CACHE_TTL = 21600  # 6 ore
MAX_HOLDINGS_AGE_DAYS = 10
MAX_CALENDAR_DAYS = 800

USER_AGENT = "YahooFinanceStorico/1.5 (+portfolio-data-validation)"

# Calendari ricavati dalle pagine ufficiali indicate in source_urls. La copertura
# e' volutamente limitata: fuori intervallo si fallisce invece di inventare sedute.
CALENDARS = {
    "US": {
        "name": "NYSE/Nasdaq US Equities",
        "timezone": "America/New_York", "open": "09:30", "close": "16:00",
        "coverage_start": "2025-01-01", "coverage_end": "2026-12-31",
        "source_urls": [
            "https://www.nyse.com/trade/hours-calendars",
            "https://www.nasdaqtrader.com/trader.aspx?id=Calendar"
        ],
        "closed": {
            "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17",
            "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
            "2025-09-01", "2025-11-27", "2025-12-25",
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
            "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
            "2026-11-26", "2026-12-25"
        },
        "early_close": {
            "2025-07-03": "13:00", "2025-11-28": "13:00", "2025-12-24": "13:00",
            "2026-11-27": "13:00", "2026-12-24": "13:00"
        }
    },
    "XLON": {
        "name": "London Stock Exchange",
        "timezone": "Europe/London", "open": "08:00", "close": "16:30",
        "coverage_start": "2025-01-01", "coverage_end": "2026-12-31",
        "source_urls": ["https://www.londonstockexchange.com/equities-trading/business-days"],
        "closed": {
            "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05",
            "2025-05-26", "2025-08-25", "2025-12-25", "2025-12-26",
            "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04",
            "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28"
        },
        "early_close": {
            "2025-12-24": "12:30", "2025-12-31": "12:30",
            "2026-12-24": "12:30", "2026-12-31": "12:30"
        }
    },
    "XPAR": {
        "name": "Euronext Paris",
        "timezone": "Europe/Paris", "open": "09:00", "close": "17:30",
        "coverage_start": "2025-01-01", "coverage_end": "2026-12-31",
        "source_urls": ["https://www.euronext.com/en/trading/trading-hours-holidays"],
        "closed": {
            "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-01",
            "2025-12-25", "2025-12-26", "2026-01-01", "2026-04-03",
            "2026-04-06", "2026-05-01", "2026-12-25"
        },
        "early_close": {
            "2025-12-24": "14:05", "2025-12-31": "14:05",
            "2026-12-24": "14:05", "2026-12-31": "14:05"
        }
    }
}

ETF_PRODUCTS = {
    "CSPX.L": {
        "provider": "iShares", "product_id": "253743", "isin": "IE00B5BMR087",
        "fund_name": "iShares Core S&P 500 UCITS ETF USD (Acc)",
        "source_url": "https://www.ishares.com/uk/individual/en/products/253743",
        "download_url": "https://www.ishares.com/uk/individual/en/products/253743/ishares-core-sp-500-ucits-etf/1467271812596.ajax?fileType=csv&fileName=CSPX_holdings&dataType=fund"
    },
    "EIMI.L": {
        "provider": "iShares", "product_id": "264659", "isin": "IE00BKM4GZ66",
        "fund_name": "iShares Core MSCI EM IMI UCITS ETF USD (Acc)",
        "source_url": "https://www.ishares.com/uk/individual/en/products/264659",
        "download_url": "https://www.ishares.com/uk/individual/en/products/264659/ishares-core-msci-em-imi-ucits-etf/1467271812596.ajax?fileType=csv&fileName=EIMI_holdings&dataType=fund"
    },
    "WDEF.L": {
        "provider": "WisdomTree", "isin": "IE0002Y8CX98",
        "fund_name": "WisdomTree Europe Defence UCITS ETF EUR Acc",
        "source_url": "https://www.wisdomtree.com/fr/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
        "download_url": "https://www.wisdomtree.com/fr/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc"
    }
}

ISHARES_API = "https://www.ishares.com/varnish-api/uk-retail01-product-data/product-data/api/v2/get-product-data"

ETF_ALIASES = {
    "CSP1.L": "CSPX.L", "SXR8.DE": "CSPX.L", "CSSPX.MI": "CSPX.L",
    "EMIM.L": "EIMI.L", "IS3N.DE": "EIMI.L", "EIMI.MI": "EIMI.L",
    "WDEP.L": "WDEF.L", "EUDF.L": "WDEF.L", "EUDF.DE": "WDEF.L",
    "WDEF.PA": "WDEF.L", "WDEF.MI": "WDEF.L"
}

def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Dato numerico non finito")
    return value


def _utc_now():
    return datetime.now(timezone.utc)


def _cache_get(key, ttl):
    now = time.time()
    with CACHE_LOCK:
        item = CACHE.get(key)
        if item and now - item['timestamp'] < ttl:
            return dict(item['data'], cache_hit=True)
    return None


def _cache_put(key, data):
    with CACHE_LOCK:
        for old_key in list(CACHE):
            if time.time() - CACHE[old_key]['timestamp'] >= max(CACHE_TTL, ETF_CACHE_TTL):
                del CACHE[old_key]
        if len(CACHE) >= 128:
            del CACHE[min(CACHE, key=lambda cache_key: CACHE[cache_key]['timestamp'])]
        CACHE[key] = {'timestamp': time.time(), 'data': data}


def _http_get(url):
    request_obj = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/csv,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.8"
    })
    try:
        with urlopen(request_obj, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
            if len(body) > 25_000_000:
                raise ValueError("Risposta remota troppo grande")
            return body, response.headers.get_content_type(), response.geturl()
    except HTTPError as exc:
        if exc.code == 429:
            raise ValueError("Fonte limitata (HTTP 429); riprovare dopo il limite del provider") from exc
        raise ValueError(f"Fonte remota HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"Fonte ufficiale non raggiungibile: {exc}") from exc


def _ishares_url(product_id):
    params = {
        "appSubType": "ISHARES", "appType": "PRODUCT_PAGE",
        "component": "holdings.all", "locale": "en_GB",
        "portfolioId": product_id, "targetSite": "ishares-uk",
        "userType": "individual", "excludeContent": "true",
        "asOfDate": "", "includeConfig": "true"
    }
    return ISHARES_API + "?" + urlencode(params)


def _parse_iso_date(value, field_name="data"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} non valida: usare YYYY-MM-DD")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _calendar_code(exchange=None, ticker=None):
    if ticker:
        symbol = str(ticker).strip().upper()
        if symbol.endswith(".L"):
            return "XLON"
        if symbol.endswith(".PA"):
            return "XPAR"
        if "." not in symbol or symbol.endswith(("=X", "=F")):
            return "US"
    aliases = {
        "NYSE": "US", "NASDAQ": "US", "XNYS": "US", "XNAS": "US", "US": "US",
        "LSE": "XLON", "XLON": "XLON", "LONDON": "XLON",
        "PARIS": "XPAR", "EURONEXT PARIS": "XPAR", "XPAR": "XPAR"
    }
    code = aliases.get(str(exchange or "").strip().upper())
    if not code:
        raise ValueError("Calendario non supportato: usare US, XLON o XPAR, oppure un ticker compatibile")
    return code


def get_exchange_calendar(exchange=None, ticker=None, start=None, end=None):
    code = _calendar_code(exchange, ticker)
    config = CALENDARS[code]
    today = _utc_now().astimezone(ZoneInfo(config['timezone'])).date()
    start_date = _parse_iso_date(start, "start") if start else max(
        _parse_iso_date(config['coverage_start']), today - timedelta(days=450))
    end_date = _parse_iso_date(end, "end") if end else min(
        _parse_iso_date(config['coverage_end']) + timedelta(days=1), today + timedelta(days=2))
    if start_date >= end_date:
        raise ValueError("start deve precedere end (esclusivo)")
    if (end_date - start_date).days > MAX_CALENDAR_DAYS:
        raise ValueError("Intervallo calendario troppo ampio")
    coverage_start = _parse_iso_date(config['coverage_start'])
    coverage_end = _parse_iso_date(config['coverage_end'])
    if start_date < coverage_start or end_date - timedelta(days=1) > coverage_end:
        raise ValueError(
            f"Calendario ufficiale fuori copertura ({config['coverage_start']} - {config['coverage_end']})")

    tz = ZoneInfo(config['timezone'])
    now_local = _utc_now().astimezone(tz)
    sessions = []
    cursor = start_date
    while cursor < end_date:
        day = cursor.isoformat()
        if cursor.weekday() < 5 and day not in config['closed']:
            close_text = config['early_close'].get(day, config['close'])
            open_h, open_m = map(int, config['open'].split(':'))
            close_h, close_m = map(int, close_text.split(':'))
            open_at = datetime.combine(cursor, dt_time(open_h, open_m), tz)
            close_at = datetime.combine(cursor, dt_time(close_h, close_m), tz)
            sessions.append({
                "date": day,
                "open": open_at.isoformat(),
                "close": close_at.isoformat(),
                "session_type": "early_close" if day in config['early_close'] else "full",
                "completed": now_local >= close_at
            })
        cursor += timedelta(days=1)
    if not sessions:
        raise ValueError("Nessuna seduta nell'intervallo richiesto")

    official_payload = {
        "code": code, "coverage_start": config['coverage_start'],
        "coverage_end": config['coverage_end'], "closed": sorted(config['closed']),
        "early_close": config['early_close'], "source_urls": config['source_urls']
    }
    payload = {
        "server_version": VERSION,
        "source": "Official_Exchange_Calendar",
        "calendar": code,
        "calendar_name": config['name'],
        "authoritative": True,
        "timezone": config['timezone'],
        "regular_open": config['open'],
        "regular_close": config['close'],
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "end_exclusive": True,
        "coverage_start": config['coverage_start'],
        "coverage_end": config['coverage_end'],
        "source_urls": config['source_urls'],
        "source_hash": hashlib.sha256(json.dumps(official_payload, sort_keys=True).encode()).hexdigest(),
        "generated_at_utc": _utc_now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "sessions_returned": len(sessions),
        "last_completed_session": next((s['date'] for s in reversed(sessions) if s['completed']), None),
        "sessions": sessions
    }
    json.dumps(payload, allow_nan=False)
    return payload


def _number(value):
    text = html.unescape(str(value or "")).replace("\xa0", " ").strip()
    text = text.replace("%", "").replace(" ", "")
    if not text or text in {"-", "—", "N/A", "n/a"}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = text.replace(",", ".") if len(parts[-1]) <= 3 else text.replace(",", "")
    try:
        return finite(text)
    except (TypeError, ValueError, OverflowError):
        return None


def _date_from_text(text):
    clean = html.unescape(text).replace("\xa0", " ")
    patterns = [
        (r"\b(\d{4}-\d{2}-\d{2})\b", ["%Y-%m-%d"]),
        (r"\b(\d{1,2}/\d{1,2}/\d{4})\b", ["%d/%m/%Y", "%m/%d/%Y"]),
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"])
    ]
    for regex, formats in patterns:
        for candidate in re.findall(regex, clean):
            for fmt in formats:
                try:
                    return datetime.strptime(candidate, fmt).date()
                except ValueError:
                    pass
    return None


def _validate_holdings(payload):
    as_of = _parse_iso_date(payload['as_of'], "as_of")
    age_days = (_utc_now().date() - as_of).days
    if age_days < -1:
        raise ValueError("Data holdings futura")
    if age_days > MAX_HOLDINGS_AGE_DAYS:
        raise ValueError(f"Holdings obsolete: {age_days} giorni")
    if payload['weight_total_pct'] < 95 or payload['weight_total_pct'] > 105:
        raise ValueError(f"Somma pesi non valida: {payload['weight_total_pct']:.4f}%")
    payload['age_days'] = age_days
    payload['data_quality']['strict_json'] = True
    json.dumps(payload, allow_nan=False)
    return payload


def _aggregate_exposure(holdings, field):
    totals = {}
    for item in holdings:
        label = item.get(field) or "Unclassified"
        totals[label] = totals.get(label, 0.0) + item['weight_pct']
    return [
        {"name": label, "weight_pct": weight}
        for label, weight in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _find_csv_header(lines):
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "weight (%)" in lowered and ("issuer ticker" in lowered or "name" in lowered):
            return index
    raise ValueError("Intestazione holdings iShares non trovata")


def _parse_ishares_holdings(body, config, ticker):
    text = body.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_index = _find_csv_header(lines)
    as_of = _date_from_text("\n".join(lines[:header_index]))
    if not as_of:
        raise ValueError("Data holdings iShares mancante")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    holdings = []
    for row in reader:
        normalized = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        name = normalized.get("name") or normalized.get("nome")
        weight = _number(normalized.get("weight (%)") or normalized.get("peso (%)"))
        if not name or weight is None or weight < 0:
            continue
        holdings.append({
            "ticker": normalized.get("issuer ticker") or None,
            "name": name,
            "isin": normalized.get("isin") or None,
            "sector": normalized.get("sector") or normalized.get("settore") or None,
            "asset_class": normalized.get("asset class") or None,
            "country": normalized.get("location") or normalized.get("country") or None,
            "market_currency": normalized.get("market currency") or normalized.get("currency") or None,
            "weight_pct": weight
        })
    if len(holdings) < 10:
        raise ValueError("Holdings iShares insufficienti")
    total = sum(item['weight_pct'] for item in holdings)
    payload = {
        "server_version": VERSION, "source": "Official_ETF_Issuer",
        "provider": config['provider'], "ticker": ticker, "canonical_ticker": ticker,
        "fund_name": config['fund_name'], "isin": config['isin'],
        "as_of": as_of.isoformat(), "generated_at_utc": _utc_now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "source_url": config['source_url'], "download_url": config['download_url'],
        "source_hash": hashlib.sha256(body).hexdigest(), "cache_hit": False,
        "holdings_detail": "complete", "proposal_usable": True,
        "holdings_count": len(holdings), "weight_total_pct": total,
        "issuer_coverage_pct": total,
        "holdings": holdings,
        "sector_exposure": _aggregate_exposure(holdings, "sector"),
        "country_exposure": _aggregate_exposure(holdings, "country"),
        "data_quality": {"complete_holdings": True, "official_source": True, "warnings": []}
    }
    return _validate_holdings(payload)


def _parse_ishares_api(body, config, ticker):
    try:
        document = json.loads(body)
        if str(document['productId']) != config['product_id']:
            raise ValueError("ID prodotto iShares non corrispondente")
        points = document['componentsByNameMap']['holdings']['containersByNameMap']['all']['dataPointsByNameMap']
        as_of = datetime.strptime(str(points['asOfDate']['value']), '%Y%m%d').date()
        fields = {
            'ticker': 'ticker', 'name': 'issueName', 'isin': 'isin',
            'sector': 'sectorName', 'asset_class': 'assetClass',
            'country': 'countryOfRisk', 'market_currency': 'marketCurrencyCode',
            'weight_pct': 'holdingPercent'
        }
        columns = {field: points[key]['value'] for field, key in fields.items()}
        count = len(columns['weight_pct'])
        if count < 10 or any(not isinstance(value, list) or len(value) != count
                             for value in columns.values()):
            raise ValueError("Colonne holdings incomplete o disallineate")
        holdings = []
        for index in range(count):
            item = {key: values[index] for key, values in columns.items()}
            item['weight_pct'] = finite(item['weight_pct'])
            # Small negative cash/FX balances are legitimate fund holdings.
            if not isinstance(item['name'], str) or not item['name'].strip():
                raise ValueError("Holdings iShares con nome o peso non valido")
            holdings.append(item)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError("Risposta holdings iShares non valida o incompleta") from exc
    total = sum(item['weight_pct'] for item in holdings)
    payload = {
        "server_version": VERSION, "source": "Official_ETF_Issuer",
        "provider": config['provider'], "ticker": ticker, "canonical_ticker": ticker,
        "fund_name": config['fund_name'], "isin": config['isin'],
        "as_of": as_of.isoformat(), "generated_at_utc": _utc_now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "source_url": config['source_url'], "download_url": _ishares_url(config['product_id']),
        "source_hash": hashlib.sha256(body).hexdigest(), "cache_hit": False,
        "holdings_detail": "complete", "proposal_usable": True,
        "holdings_count": len(holdings), "weight_total_pct": total,
        "issuer_coverage_pct": total, "holdings": holdings,
        "sector_exposure": _aggregate_exposure(holdings, "sector"),
        "country_exposure": _aggregate_exposure(holdings, "country"),
        "data_quality": {"complete_holdings": True, "official_source": True, "warnings": []}
    }
    return _validate_holdings(payload)


def _html_text(fragment):
    clean = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", fragment,
                   flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return re.sub(r"\s+", " ", html.unescape(clean)).strip()


def _table_rows(document):
    tables = []
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", document, flags=re.I | re.S):
        rows = []
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, flags=re.I | re.S):
            cells = [_html_text(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row,
                                                              flags=re.I | re.S)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _weighted_rows(rows):
    items = []
    for row in rows:
        if len(row) < 2:
            continue
        weight = _number(row[-1])
        name = row[0].strip()
        if weight is None or not name or weight < 0 or weight > 100:
            continue
        lowered = name.lower()
        if any(word in lowered for word in ("name", "nom", "poids", "weight", "country", "pays")):
            continue
        items.append({"name": name, "weight_pct": weight})
    return items


def _select_weighted_table(candidates, required_names):
    required = {name.lower() for name in required_names}
    return next((
        rows for rows in candidates
        if any(item['name'].lower() in required for item in rows)
    ), [])


def _parse_wisdomtree_holdings(body, config, ticker):
    document = body.decode("utf-8", errors="replace")
    visible = _html_text(document)
    as_of_match = re.search(r"(?:As of|Au|Stand)\s+([0-9A-Za-zÀ-ÿ,./ -]{6,24})", visible, flags=re.I)
    as_of = _date_from_text(as_of_match.group(1) if as_of_match else visible)
    if not as_of:
        raise ValueError("Data holdings WisdomTree mancante")
    candidates = [_weighted_rows(rows) for rows in _table_rows(document)]
    candidates = [rows for rows in candidates if rows]
    if not candidates:
        raise ValueError("Tabelle holdings WisdomTree non trovate")

    remaining_labels = {"remaining portfolio", "portefeuille restant", "restliches portfolio"}
    holdings_rows = _select_weighted_table(candidates, remaining_labels | {
        "bae systems", "thales", "rheinmetall", "leonardo"
    })
    sector_rows = _select_weighted_table(candidates, {"industrials", "industrie"})
    country_rows = _select_weighted_table(candidates, {
        "france", "germany", "united kingdom", "italy", "sweden"
    })
    if holdings_rows in (sector_rows, country_rows):
        holdings_rows = []
    sector_total = sum(x['weight_pct'] for x in sector_rows)
    country_total = sum(x['weight_pct'] for x in country_rows)
    if not 95 <= sector_total <= 105:
        sector_rows = []
    if not 95 <= country_total <= 105:
        country_rows = []
    remaining = next((x['weight_pct'] for x in holdings_rows if x['name'].lower() in remaining_labels), 0.0)
    holdings = [{"ticker": None, "name": x['name'], "isin": None, "sector": None,
                 "asset_class": "Equity", "country": None, "market_currency": None,
                 "weight_pct": x['weight_pct']}
                for x in holdings_rows if x['name'].lower() not in remaining_labels]
    if len(holdings) < 10 or not sector_rows or not country_rows:
        raise ValueError("Look-through WisdomTree insufficiente")
    total = sum(x['weight_pct'] for x in holdings) + remaining
    warnings = []
    if remaining:
        warnings.append(f"Dettaglio emittenti parziale: portafoglio restante {remaining:.2f}%")
    payload = {
        "server_version": VERSION, "source": "Official_ETF_Issuer",
        "provider": config['provider'], "ticker": ticker, "canonical_ticker": ticker,
        "fund_name": config['fund_name'], "isin": config['isin'],
        "as_of": as_of.isoformat(), "generated_at_utc": _utc_now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "source_url": config['source_url'], "download_url": config['download_url'],
        "source_hash": hashlib.sha256(body).hexdigest(), "cache_hit": False,
        "holdings_detail": "top_holdings_plus_remainder" if remaining else "complete",
        "proposal_usable": bool(sector_rows and country_rows),
        "holdings_count": len(holdings), "weight_total_pct": total,
        "issuer_coverage_pct": sum(x['weight_pct'] for x in holdings),
        "remaining_portfolio_pct": remaining,
        "holdings": holdings,
        "sector_exposure": sector_rows,
        "country_exposure": country_rows,
        "data_quality": {
            "complete_holdings": not bool(remaining), "official_source": True,
            "sector_coverage_complete": True, "country_coverage_complete": True,
            "warnings": warnings
        }
    }
    return _validate_holdings(payload)


def get_etf_lookthrough(ticker_symbol):
    if not isinstance(ticker_symbol, str):
        raise ValueError("Ticker ETF non valido")
    requested = ticker_symbol.strip().upper()
    canonical = ETF_ALIASES.get(requested, requested)
    config = ETF_PRODUCTS.get(canonical)
    if not config:
        raise ValueError("ETF non supportato: disponibili CSPX.L, EIMI.L e WDEF.L")
    key = f"etf:{canonical}"
    cached = _cache_get(key, ETF_CACHE_TTL)
    if cached:
        cached['ticker'] = requested
        return cached
    url = _ishares_url(config['product_id']) if config['provider'] == "iShares" else config['download_url']
    body, _content_type, final_url = _http_get(url)
    payload = (_parse_ishares_api(body, config, canonical)
               if config['provider'] == "iShares"
               else _parse_wisdomtree_holdings(body, config, canonical))
    payload['ticker'] = requested
    payload['canonical_ticker'] = canonical
    payload['resolved_download_url'] = final_url
    _cache_put(key, payload)
    return payload


def _format_bar(row):
    """Validate one Yahoo bar without letting one bad row poison a series."""
    date_str = row['Date'].strftime('%Y-%m-%d')
    timestamp = int(row['Date'].timestamp())
    try:
        bar = {
            "date": date_str,
            "timestamp": timestamp,
            "open": finite(row['Open']),
            "high": finite(row['High']),
            "low": finite(row['Low']),
            "close": finite(row['Close']),
            "adj_close": finite(row['Close'])
        }
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {"date": date_str, "reason": f"prezzo non valido: {exc}"}, None

    if min(bar['open'], bar['high'], bar['low'], bar['close']) <= 0:
        return None, {"date": date_str, "reason": "prezzo non positivo"}, None

    upper = max(bar['open'], bar['close'])
    lower = min(bar['open'], bar['close'])
    tolerance = max(1e-8, upper * OHLC_REL_TOLERANCE)
    normalized = []
    if bar['high'] < upper:
        if upper - bar['high'] <= tolerance:
            bar['high'] = upper
            normalized.append("high")
        else:
            return None, {"date": date_str, "reason": "high inferiore a open/close"}, None
    if bar['low'] > lower:
        if bar['low'] - lower <= tolerance:
            bar['low'] = lower
            normalized.append("low")
        else:
            return None, {"date": date_str, "reason": "low superiore a open/close"}, None
    if bar['low'] > bar['high']:
        return None, {"date": date_str, "reason": "low superiore a high"}, None

    try:
        volume = finite(row['Volume'])
        if volume < 0 or not volume.is_integer():
            raise ValueError("volume negativo o non intero")
        bar['volume'] = int(volume)
    except (TypeError, ValueError, OverflowError):
        # Volume is optional for scanner/risk. Keep strict JSON and expose the issue.
        bar['volume'] = None
        normalized.append("volume_null")

    adjustment = None
    if normalized:
        adjustment = {"date": date_str, "fields": normalized,
                      "reason": "normalizzazione entro tolleranza o volume non disponibile"}
    return bar, None, adjustment


def _fetch_yahoo_chart(ticker_symbol, period, interval, start, end):
    """One bounded Yahoo chart request; never substitute unadjusted prices."""
    params = {"interval": interval, "events": "div,splits", "includeAdjustedClose": "true"}
    if start or end:
        params['period1'] = int(datetime.combine(
            _parse_iso_date(start) if start else date(1970, 1, 1), dt_time(), timezone.utc).timestamp())
        params['period2'] = int(datetime.combine(
            _parse_iso_date(end) if end else _utc_now().date() + timedelta(days=1),
            dt_time(), timezone.utc).timestamp())
    else:
        params['range'] = period
    from urllib.parse import quote
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(ticker_symbol, safe="") + "?" + urlencode(params)
    body, _, _ = _http_get(url)
    try:
        chart = json.loads(body)['chart']
        if chart.get('error'):
            raise ValueError(f"Yahoo chart: {chart['error']}")
        result = chart['result'][0]
        meta = result['meta']
        currency, exchange_timezone = meta['currency'], meta['exchangeTimezoneName']
        ZoneInfo(exchange_timezone)
        timestamps = result['timestamp']
        indicator = result['indicators']
        quote_values = indicator['quote'][0]
        closes = quote_values['close']
        adjusted = indicator['adjclose'][0]['adjclose']
        if not isinstance(timestamps, list) or not timestamps or len(adjusted) != len(timestamps):
            raise ValueError("Barre Yahoo o close rettificati mancanti")
        columns = {name: quote_values[name] for name in ('open', 'high', 'low', 'close', 'volume')}
        if any(len(values) != len(timestamps) for values in columns.values()):
            raise ValueError("Colonne Yahoo disallineate")
        dividend_events = result.get('events', {}).get('dividends', {})
        split_events = result.get('events', {}).get('splits', {})
        rows = []
        for index, stamp in enumerate(timestamps):
            close, adj_close = closes[index], adjusted[index]
            factor = finite(adj_close) / finite(close) if close and adj_close else float('nan')
            split = split_events.get(str(stamp), {})
            numerator, denominator = split.get('numerator'), split.get('denominator')
            rows.append({
                'Date': pd.Timestamp(stamp, unit='s', tz='UTC'),
                'Open': columns['open'][index] * factor if columns['open'][index] is not None else float('nan'),
                'High': columns['high'][index] * factor if columns['high'][index] is not None else float('nan'),
                'Low': columns['low'][index] * factor if columns['low'][index] is not None else float('nan'),
                'Close': adj_close if adj_close is not None else float('nan'),
                'Volume': columns['volume'][index],
                'Dividends': finite(dividend_events.get(str(stamp), {}).get('amount', 0)),
                'Stock Splits': finite(numerator) / finite(denominator) if numerator and denominator else 0.0
            })
        frame = pd.DataFrame(rows).set_index('Date')
        return frame, {'currency': currency, 'exchangeTimezoneName': exchange_timezone}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError("Risposta Yahoo chart incompleta o non valida") from exc

def get_advanced_data(ticker_symbol, period="5y", interval="1d", start=None, end=None):
    if not isinstance(ticker_symbol, str) or not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^=\-]{0,39}", ticker_symbol.strip()):
        raise ValueError("Ticker non valido")
    ticker_symbol = ticker_symbol.strip().upper()
    if interval not in ("1d", "1wk", "1mo") or period not in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"):
        raise ValueError("Period o interval non supportato")
    for value in (start, end):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Data non valida: usare YYYY-MM-DD")
            datetime.strptime(value, "%Y-%m-%d")
    if start and end and start >= end:
        raise ValueError("start deve precedere end (esclusivo)")
    cache_key = f"{ticker_symbol}_{period}_{interval}_{start}_{end}"
    now = time.time()
    
    with CACHE_LOCK:
        if cache_key in CACHE and (now - CACHE[cache_key]['timestamp'] < CACHE_TTL):
            return dict(CACHE[cache_key]['data'], cache_hit=True)
    
    hist, info = _fetch_yahoo_chart(ticker_symbol, period, interval, start, end)
        
    if hist.empty:
        raise ValueError(f"Nessun dato trovato per il ticker {ticker_symbol}")
        
    # Use metadata of the price chart, never financialCurrency or ticker suffixes.
    info = info or {}
    currency = info.get('currency')
    exchange_timezone = info.get('exchangeTimezoneName')
    if not isinstance(currency, str) or not currency.strip() or not isinstance(exchange_timezone, str):
        raise ValueError("Valuta/timezone mancanti: storico non validabile")
    ZoneInfo(exchange_timezone)
    if hist.index.tz is None or hist.index.has_duplicates:
        raise ValueError("Indice temporale senza timezone o duplicato")
    hist = hist.sort_index()
    hist.index = hist.index.tz_convert(exchange_timezone)
    hist.index.name = 'Date'
    if start:
        hist = hist[hist.index.strftime('%Y-%m-%d') >= start]
    if end:
        hist = hist[hist.index.strftime('%Y-%m-%d') < end]
    if hist.empty:
        raise ValueError("Nessun dato nell'intervallo richiesto")
    if not {'Dividends', 'Stock Splits'}.issubset(hist.columns):
        raise ValueError("Corporate actions mancanti")
    
    total_sessions_available = len(hist)
    latest_source_date = hist.index[-1].strftime('%Y-%m-%d')
    hist_dict = hist.reset_index().to_dict(orient="records")
    formatted_history = []
    dropped_bars = []
    adjusted_bars = []
    
    for row in hist_dict:
        bar, issue, adjustment = _format_bar(row)
        if issue:
            dropped_bars.append(issue)
            continue
        formatted_history.append(bar)
        if adjustment:
            adjusted_bars.append(adjustment)
    if len({bar['date'] for bar in formatted_history}) != len(formatted_history):
        raise ValueError("Date duplicate")
    if dropped_bars and dropped_bars[-1]['date'] == latest_source_date:
        raise ValueError(f"Ultima barra non valida ({latest_source_date}): {dropped_bars[-1]['reason']}")
    if len(formatted_history) < 1:
        raise ValueError("Serie insufficiente dopo la pulizia")

    valid_sessions_available = len(formatted_history)
    truncated = valid_sessions_available > MAX_RETURNED_BARS
    final_history = formatted_history[-MAX_RETURNED_BARS:]
    sessions_returned = len(final_history)
    
    formatted_actions = []
    # Reuse actions from the same history request, not an extra max-history fetch.
    returned_dates = {bar['date'] for bar in final_history}
    actions = hist[['Dividends', 'Stock Splits']]
    if actions is not None and not actions.empty:
        actions_dict = actions.reset_index().to_dict(orient="records")
        for row in actions_dict:
            action_date = row['Date'].strftime('%Y-%m-%d')
            if action_date not in returned_dates:
                continue
            formatted_actions.append({
                "date": action_date,
                "dividends": finite(row['Dividends']),
                "stock_splits": finite(row['Stock Splits'])
            })
    formatted_actions = [a for a in formatted_actions if a['dividends'] or a['stock_splits']]

    payload = {
        "server_version": VERSION,
        "source": "Yahoo_Finance_Storico",
        "interval": interval,
        "period": None if start or end else period,
        "start": start, "end": end, "end_exclusive": True,
        "currency_source": "Yahoo chart metadata",
        "price_unit": currency,
        "prices_normalized": False,
        "major_currency": {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(currency, currency),
        "price_scale_to_major_currency": 0.01 if currency in ("GBp", "GBX", "ZAc", "ILA") else 1.0,
        "adjustment_method": "Yahoo chart adjclose/close factor applied to OHLC",
        "ohlc_adjusted": True,
        "cache_hit": False,
        "coverage_start": final_history[0]['date'],
        "coverage_end": final_history[-1]['date'],
        "next_end": final_history[0]['date'] if truncated else None,
        "actions_scope": "returned_history",
        "warnings": ["Validare l'ultima seduta con get_exchange_calendar; FX da validare separatamente"],
        "ticker": ticker_symbol,
        "currency": currency,
        "exchange_timezone": exchange_timezone,
        "generated_at_utc": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "adjusted": True,
        "total_sessions_available": total_sessions_available,
        "valid_sessions_available": valid_sessions_available,
        "sessions_returned": sessions_returned,
        "truncated": truncated,
        "data_quality": {
            "strict_json": True,
            "dropped_bars_count": len(dropped_bars),
            "dropped_bars": dropped_bars,
            "adjusted_bars_count": len(adjusted_bars),
            "adjusted_bars": adjusted_bars,
            "latest_source_bar_valid": True,
            "minimum_risk_prices": MIN_RISK_PRICES,
            "risk_sample_sufficient": sessions_returned >= MIN_RISK_PRICES
        },
        "history": final_history,
        "actions": formatted_actions
    }
    
    json.dumps(payload, allow_nan=False)
    with CACHE_LOCK:
        for key in list(CACHE):
            if time.time() - CACHE[key]['timestamp'] >= CACHE_TTL:
                del CACHE[key]
        if len(CACHE) >= 128:
            del CACHE[min(CACHE, key=lambda key: CACHE[key]['timestamp'])]
        CACHE[cache_key] = {'timestamp': time.time(), 'data': payload}
    return payload

@app.route('/market-data', methods=['GET'])
def market_data_api():
    ticker = request.args.get('ticker')
    if not ticker:
        return jsonify({"errore": "Parametro ticker obbligatorio"}), 400
    try:
        return jsonify(get_advanced_data(ticker, request.args.get('period', '5y'),
                                        request.args.get('interval', '1d'),
                                        request.args.get('start'), request.args.get('end')))
    except Exception as e:
        return jsonify({"errore": str(e), "status": "BLOCCATO PER DATI"}), 503


@app.route('/exchange-calendar', methods=['GET'])
def exchange_calendar_api():
    try:
        return jsonify(get_exchange_calendar(
            exchange=request.args.get('exchange'), ticker=request.args.get('ticker'),
            start=request.args.get('start'), end=request.args.get('end')))
    except Exception as e:
        return jsonify({"errore": str(e)}), 400


@app.route('/etf-lookthrough', methods=['GET'])
def etf_lookthrough_api():
    ticker = request.args.get('ticker')
    if not ticker:
        return jsonify({"errore": "Parametro ticker obbligatorio"}), 400
    try:
        return jsonify(get_etf_lookthrough(ticker))
    except Exception as e:
        return jsonify({"errore": str(e)}), 500

@app.route('/mcp', methods=['POST'])
def mcp_rpc_server():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0' or not isinstance(body.get('method'), str):
        return jsonify({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}), 400
    method = body.get("method")
    rpc_id = body.get("id")
    if 'id' not in body:
        return "", 204
    if not isinstance(body.get('params', {}), dict):
        return jsonify({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': -32602, 'message': 'Invalid params'}})
    
    if method == "notifications/initialized":
        return "", 204
    
    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "YahooFinanceAdvancedMCP", "version": VERSION}
            }
        })
        
    elif method == "tools/list":
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "tools": [
                    {
                        "name": "get_market_data",
                        "description": "Ottiene storico rettificato, valuta nativa, timezone, split e dividendi per titoli USA ed Europei.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string", "description": "Il simbolo del ticker"},
                                "period": {"type": "string", "default": "5y", "description": "1d, 5d, 1mo, 1y, 5y, max"},
                                "interval": {"type": "string", "default": "1d", "description": "1d, 1wk, 1mo"},
                                "start": {"type": "string", "description": "Data inizio YYYY-MM-DD"},
                                "end": {"type": "string", "description": "Data fine esclusiva YYYY-MM-DD"}
                            },
                            "required": ["ticker"]
                        }
                    },
                    {
                        "name": "get_exchange_calendar",
                        "description": "Restituisce sedute ufficiali, fuso, orari, festivita e chiusure anticipate per USA, LSE o Euronext Paris.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "exchange": {"type": "string", "description": "US, XLON o XPAR"},
                                "ticker": {"type": "string", "description": "Alternativa a exchange; deduce il calendario dal ticker"},
                                "start": {"type": "string", "description": "Data inizio YYYY-MM-DD"},
                                "end": {"type": "string", "description": "Data fine esclusiva YYYY-MM-DD"}
                            },
                            "anyOf": [{"required": ["exchange"]}, {"required": ["ticker"]}]
                        }
                    },
                    {
                        "name": "get_etf_lookthrough",
                        "description": "Ottiene composizione ufficiale e esposizioni di CSPX, EIMI o WDEF con data, copertura e qualita.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string", "description": "Ticker ETF, ad esempio CSPX.L, EIMI.L o WDEF.L"}
                            },
                            "required": ["ticker"]
                        }
                    }
                ]
            }
        })
        
    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return jsonify({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': -32602, 'message': 'Invalid arguments'}})
        ticker = arguments.get("ticker")

        try:
            if tool_name == "get_market_data" and ticker:
                data = get_advanced_data(
                    ticker_symbol=ticker,
                    period=arguments.get("period", "5y"),
                    interval=arguments.get("interval", "1d"),
                    start=arguments.get("start"),
                    end=arguments.get("end")
                )
            elif tool_name == "get_exchange_calendar" and (arguments.get("exchange") or ticker):
                data = get_exchange_calendar(
                    exchange=arguments.get("exchange"), ticker=ticker,
                    start=arguments.get("start"), end=arguments.get("end"))
            elif tool_name == "get_etf_lookthrough" and ticker:
                data = get_etf_lookthrough(ticker)
            else:
                return jsonify({
                    "jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32602, "message": "Tool o parametri non validi"}
                })
            return jsonify({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(data, allow_nan=False)}],
                    "isError": False
                }
            })
        except Exception as e:
            source = ("Official_Exchange_Calendar" if tool_name == "get_exchange_calendar"
                      else "Official_ETF_Issuer" if tool_name == "get_etf_lookthrough"
                      else "Yahoo_Finance_Storico")
            return jsonify({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"isError": True, "content": [{"type": "text", "text": json.dumps({
                    "status": "BLOCCATO PER DATI", "source": source, "reason": str(e)
                })}]}
            })
                
    return jsonify({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": "Method not found"}
    }), 404

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': VERSION})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
