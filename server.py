# =============================================================================
#  Yahoo Finance Storico - server MCP per il progetto "Trading eToro"
#  Versione 1.7.50 - SOLA LETTURA
#  Sprint2: shortlist globale asincrona, protocollo adattivo e master r8.
#  18/10/2026: policy condivisa e manifest reale di codice/mappe/dipendenze.
#  18/10/2026: lacune interne isolate, indicatori su coda consecutiva;
#  rischio su coppie di sedute valide, senza barre o rendimenti sintetici.
#  18/10/2026: storico ETF Yahoo completo, fallback eToro su serie intera;
#  conteggio rendimenti su sedute consecutive, soglie finanziarie invariate.
#
#  Strumenti MCP:
#   get_market_data        storico OHLCV rettificato (solo sedute concluse)
#   get_indicators         indicatori tecnici calcolati sul server
#   scan_market            scanner US / EU con shortlist compatta
#   get_portfolio_risk     VaR/ES, correlazioni, allocazione, simulazione candidato
#   get_exchange_calendar  calendari di borsa (exchange_calendars)
#   get_etf_lookthrough    composizione ETF opzionale; mai bloccante per scanner/valutazione
#   get_server_status      stato cache, preriscaldamento, chiamate Yahoo
#
#  Le liste UNIVERSE_US / UNIVERSE_EU / PORTFOLIO_WATCH si possono modificare:
#  un ticker Yahoo per voce, tra virgolette, seguito da virgola.
# =============================================================================

import os
import time
import json
import zlib
import gc
import math
import re
import html
import hashlib
import random
import logging
import threading
import queue
import copy
import http.cookiejar
import unicodedata
from urllib.request import Request, build_opener, HTTPCookieProcessor, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo
from datetime import date, datetime, time as dt_time, timedelta, timezone

import etoro_history
from policy_contract import T
from runtime_manifest import build as build_runtime_manifest
import cycle_scanner
from adaptive_method import validate_state
from acquire_ecb_fx import acquire as acquire_ecb_history
from prepare_ecb_fx import target_open

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request

try:
    import exchange_calendars as xcals
except Exception:
    xcals = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yfs")
app = Flask(__name__)

# -----------------------------------------------------------------------------
# Parametri
# -----------------------------------------------------------------------------
VERSION = "1.7.50"
RUNTIME_MANIFEST = build_runtime_manifest()
RISK_ENGINE_VERSION = "server-risk 1.1.0"
DEFAULT_PERIOD = "2y"
HTTP_TIMEOUT = 15
MAX_RETURNED_BARS = 300
DEFAULT_RETURNED_BARS = 300
OHLC_REL_TOLERANCE = 1e-6
STALE_MAX_AGE = 4 * 86400
NO_CAL_TTL = 6 * 3600
MIN_REFETCH_S = 20 * 60
LAG_REFETCH_S = int(os.environ.get("LAG_REFETCH_S", str(6 * 3600)))
ETF_CACHE_TTL = 6 * 3600
INTRADAY_RVOL_CACHE_TTL = 180
INTRADAY_RVOL_CACHE_MAX = 120
SECTOR_CACHE_TTL = 24 * 3600
SECTOR_CACHE_MAX = 250
MAX_HOLDINGS_AGE_DAYS = 10
CACHE_MAX_ITEMS = 1100
MEMORY_SOFT_LIMIT_MB = float(os.environ.get("MEMORY_SOFT_LIMIT_MB", "400"))
MEMORY_HARD_LIMIT_MB = float(os.environ.get("MEMORY_HARD_LIMIT_MB", "460"))
SCAN_TIME_BUDGET = 25
SCAN_MAX_FETCH = 8
FAIL_SKIP_S = 6 * 3600

YAHOO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_MIN_INTERVAL = 1.5
YAHOO_COOLDOWN = 180

# Simboli eToro che richiedono un alias diverso su Yahoo Finance.
# L’identità operativa resta quella eToro; l’alias è usato solo nella richiesta Yahoo.
YAHOO_SYMBOL_MAP = {"BRK.B": "BRK-B"}
WARMER_ENABLED = os.environ.get("WARMER", "0") != "0"
WARMER_PAUSE = 3.0
WARMER_DAILY_MAX = int(os.environ.get("WARMER_DAILY_MAX", "2200"))

ROME = ZoneInfo("Europe/Rome")
SHORT_PERIOD_DAYS = {"1d": 4, "5d": 8, "1mo": 32, "3mo": 93, "6mo": 184, "1y": 367}

# -----------------------------------------------------------------------------
# Universo tecnico esterno: dati/versioni soltanto; regole nel Mandato.
# -----------------------------------------------------------------------------
from universe import (
    UNIVERSE_VERSION, PORTFOLIO_WATCH, UNIVERSE_US, UNIVERSE_EU,
    LEVEL_A_STATUS, LEVEL_A_TARGET_MIN, LEVEL_A_TARGET_MAX,
    filtered_level_a, universe_stats, EXCLUDED_SYMBOLS,
)


from level_b import (A1_VERSION, A2_VERSION, A3_VERSION, A4_VERSION, A5_VERSION, FUSION_VERSION, discover_a1_volume,
                    discover_a2_momentum, discover_a3_catalyst, discover_a4_attention, discover_a5_sector_rotation,
                    fuse_level_b_results)

BENCHMARKS = {"US": "SPY", "EU": "EXSA.DE"}
VERIFICATION_PACK_VERSION = "level-b-verification-pack-0.2-ticker-normalization"
FX_PAIRS = ["EURUSD=X", "GBPUSD=X", "CHFUSD=X", "DKKUSD=X", "SEKUSD=X", "NOKUSD=X"]
MIN_TURNOVER_EUR = {"US": 20e6, "EU": 5e6}
APPROX_UNITS_PER_EUR = {"EUR": 1.0, "USD": 1.15, "GBP": 0.86, "CHF": 0.94,
                        "SEK": 11.0, "NOK": 11.5, "DKK": 7.46}

# -----------------------------------------------------------------------------
# Calendari
# -----------------------------------------------------------------------------
SUFFIX_CALENDAR = {
    ".L": "XLON", ".PA": "XPAR", ".DE": "XETR", ".F": "XFRA", ".MI": "XMIL",
    ".AS": "XAMS", ".BR": "XBRU", ".MC": "XMAD", ".LS": "XLIS", ".SW": "XSWX",
    ".ST": "XSTO", ".CO": "XCSE", ".HE": "XHEL", ".OL": "XOSL", ".IR": "XDUB",
    ".VI": "XWBO",
}
INDEX_CALENDAR = {
    "^GSPC": "XNYS", "^IXIC": "XNYS", "^NDX": "XNYS", "^DJI": "XNYS", "^RUT": "XNYS",
    "^VIX": "XNYS", "^GDAXI": "XETR", "^STOXX50E": "XETR", "^STOXX": "XETR",
    "^FCHI": "XPAR", "^FTSE": "XLON", "^AEX": "XAMS", "^IBEX": "XMAD", "^SSMI": "XSWX",
}
EXCHANGE_ALIASES = {
    "US": "XNYS", "NYSE": "XNYS", "NASDAQ": "XNYS", "XNAS": "XNYS",
    "LSE": "XLON", "LONDON": "XLON", "PARIS": "XPAR", "EURONEXT PARIS": "XPAR",
    "XETRA": "XETR", "FRANKFURT": "XETR", "MILANO": "XMIL", "MILAN": "XMIL",
    "BORSA ITALIANA": "XMIL", "AMSTERDAM": "XAMS", "MADRID": "XMAD",
    "SIX": "XSWX", "SWISS": "XSWX", "ZURIGO": "XSWX",
}
for _c in set(SUFFIX_CALENDAR.values()) | {"XNYS"}:
    EXCHANGE_ALIASES[_c] = _c

# -----------------------------------------------------------------------------
# ETF look-through
# -----------------------------------------------------------------------------
ETF_PRODUCTS = {
    "CSPX.L": {"provider": "iShares", "product_id": "253743", "isin": "IE00B5BMR087",
               "fund_name": "iShares Core S&P 500 UCITS ETF USD (Acc)",
               "source_url": "https://www.ishares.com/uk/individual/en/products/253743"},
    "EIMI.L": {"provider": "iShares", "product_id": "264659", "isin": "IE00BKM4GZ66",
               "fund_name": "iShares Core MSCI EM IMI UCITS ETF USD (Acc)",
               "source_url": "https://www.ishares.com/uk/individual/en/products/264659"},
    "WDEF.L": {"provider": "WisdomTree", "isin": "IE0002Y8CX98",
               "fund_name": "WisdomTree Europe Defence UCITS ETF EUR Acc",
               "source_url": "https://www.wisdomtree.com/it/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
               "download_urls": [
                   "https://www.wisdomtree.com/it/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
                   "https://www.wisdomtree.com/gb/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
                   "https://www.wisdomtree.com/ch/it/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
                   "https://www.wisdomtree.com/lu/products/equities/wisdomtree-europe-defence-ucits-etf---eur-acc",
               ]},
}
ISHARES_API = "https://www.ishares.com/varnish-api/uk-retail01-product-data/product-data/api/v2/get-product-data"
ETF_ALIASES = {
    "CSP1.L": "CSPX.L", "SXR8.DE": "CSPX.L", "CSSPX.MI": "CSPX.L",
    "EMIM.L": "EIMI.L", "IS3N.DE": "EIMI.L", "EIMI.MI": "EIMI.L",
    "WDEP.L": "WDEF.L", "EUDF.L": "WDEF.L", "EUDF.DE": "WDEF.L",
    "WDEF.PA": "WDEF.L", "WDEF.MI": "WDEF.L",
}

# -----------------------------------------------------------------------------
# Stato condiviso
# -----------------------------------------------------------------------------
CACHE = {}
CACHE_LOCK = threading.Lock()
INTRADAY_RVOL_CACHE = {}
INTRADAY_RVOL_LOCK = threading.Lock()
SECTOR_CACHE = {}
SECTOR_CACHE_LOCK = threading.Lock()
_CAL_LOCK = threading.Lock()
_CAL_OBJ = {}
_CAL_MEMO = {}
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = build_opener(HTTPCookieProcessor(_COOKIE_JAR))
_YLOCK = threading.Lock()
_YS = {"crumb": None, "last_call": 0.0, "cooldown_until": 0.0}
_STATS = {"day": None, "yahoo_calls": 0, "yahoo_429": 0, "cache_hits": 0, "warmer_fetched": 0}
_STATS_LOCK = threading.Lock()
_FAILS = {}
_EXTRA = set()
_EXTRA_LOCK = threading.Lock()
_WS = {"started": False, "day": None, "fetched_today": 0, "last_ticker": None,
       "last_error": None, "last_cycle_utc": None}


# -----------------------------------------------------------------------------
# Utilita'
# -----------------------------------------------------------------------------
def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Dato numerico non finito")
    return value


def _rnd(value, digits=4):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


def _pct(value, digits=2):
    return None if value is None else _rnd(value * 100, digits)


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_stamp():
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_text(data):
    return json.dumps(data, allow_nan=False, separators=(",", ":"), ensure_ascii=False)


def _parse_iso_date(value, field_name="data"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} non valida: usare YYYY-MM-DD")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _validate_ticker(ticker):
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^=\-]{0,39}", ticker.strip()):
        raise ValueError(f"Ticker non valido: {ticker}")
    return ticker.strip().upper()


def _validate_period(period, interval, start, end):
    if interval not in ("1d", "1wk", "1mo"):
        raise ValueError("Interval non supportato: usare 1d, 1wk o 1mo")
    if period not in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"):
        raise ValueError("Period non supportato")
    for value in (start, end):
        if value is not None:
            _parse_iso_date(value)
    if start and end and start >= end:
        raise ValueError("start deve precedere end (esclusivo)")


def _count(field, n=1):
    today = datetime.now(ROME).date().isoformat()
    with _STATS_LOCK:
        if _STATS["day"] != today:
            _STATS.update(day=today, yahoo_calls=0, yahoo_429=0, cache_hits=0, warmer_fetched=0)
        _STATS[field] += n


def _register_extra(tickers):
    with _EXTRA_LOCK:
        for t in tickers:
            if len(_EXTRA) < 120:
                _EXTRA.add(t)


def _yahoo_cooldown_left():
    return max(0.0, _YS["cooldown_until"] - time.time())


# -----------------------------------------------------------------------------
# Calendari (exchange_calendars)
# -----------------------------------------------------------------------------
def _calendar_code(ticker):
    t = ticker.upper()
    if t.endswith("=X") or t.endswith("=F"):
        return None
    if t.startswith("^"):
        return INDEX_CALENDAR.get(t)
    if "." in t:
        return SUFFIX_CALENDAR.get("." + t.rsplit(".", 1)[1])
    return "XNYS"


def _get_cal(code):
    if xcals is None:
        raise ValueError("Libreria exchange_calendars non installata")
    with _CAL_LOCK:
        if code not in _CAL_OBJ:
            _CAL_OBJ[code] = xcals.get_calendar(code)
        return _CAL_OBJ[code]


def _last_completed(code):
    """Ultima seduta conclusa (YYYY-MM-DD) e orario di chiusura UTC. Memo di 60 s."""
    minute = int(time.time() // 60)
    memo = _CAL_MEMO.get(code)
    if memo and memo[0] == minute:
        return memo[1]
    cal = _get_cal(code)
    now = pd.Timestamp.now(tz="UTC")
    today = now.tz_convert(cal.tz).date()
    sessions = cal.sessions_in_range(pd.Timestamp(today - timedelta(days=15)), pd.Timestamp(today))
    result = (None, None)
    for sess in reversed(sessions):
        close = cal.session_close(sess)
        if close <= now:
            result = (sess.strftime("%Y-%m-%d"), close)
            break
    _CAL_MEMO[code] = (minute, result)
    return result


def _close_settled(code):
    if not code:
        return True
    try:
        _, close = _last_completed(code)
    except Exception:
        return True
    return close is None or pd.Timestamp.now(tz="UTC") >= close + pd.Timedelta(minutes=20)


def _session_status(code):
    cal = _get_cal(code)
    now = pd.Timestamp.now(tz="UTC")
    today = pd.Timestamp(now.tz_convert(cal.tz).date())
    open_now = False
    if cal.is_session(today):
        open_now = bool(cal.session_open(today) <= now < cal.session_close(today))
        nxt = today if now < cal.session_close(today) else cal.next_session(today)
    else:
        nxt = cal.date_to_session(today, direction="next")
    last_completed, _ = _last_completed(code)
    return {
        "is_open_now": open_now,
        "last_completed_session": last_completed,
        "next_session": nxt.strftime("%Y-%m-%d"),
        "next_open_utc": cal.session_open(nxt).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_close_utc": cal.session_close(nxt).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _session_progress(ticker):
    """Quota 0..1 della seduta regolare corrente; None se mercato chiuso/non mappato."""
    code = _calendar_code(_validate_ticker(ticker))
    if not code:
        return None
    cal = _get_cal(code)
    now = pd.Timestamp.now(tz="UTC")
    today = pd.Timestamp(now.tz_convert(cal.tz).date())
    if not cal.is_session(today):
        return None
    opn, cls = cal.session_open(today), cal.session_close(today)
    if now < opn:
        return 0.0
    if now >= cls:
        return 1.0
    total = (cls - opn).total_seconds()
    return float((now - opn).total_seconds() / total) if total > 0 else None


def _intraday_cache_get(key):
    now = time.time()
    with INTRADAY_RVOL_LOCK:
        entry = INTRADAY_RVOL_CACHE.get(key)
        if entry and now - entry["ts"] < INTRADAY_RVOL_CACHE_TTL:
            return dict(entry["data"], cache_hit=True, cache_age_s=int(now - entry["ts"]))
        if entry:
            INTRADAY_RVOL_CACHE.pop(key, None)
    return None


def _intraday_cache_put(key, data):
    now = time.time()
    with INTRADAY_RVOL_LOCK:
        for k in [k for k, v in INTRADAY_RVOL_CACHE.items()
                  if now - v["ts"] >= INTRADAY_RVOL_CACHE_TTL]:
            INTRADAY_RVOL_CACHE.pop(k, None)
        while len(INTRADAY_RVOL_CACHE) >= INTRADAY_RVOL_CACHE_MAX:
            oldest = min(INTRADAY_RVOL_CACHE, key=lambda k: INTRADAY_RVOL_CACHE[k]["ts"])
            INTRADAY_RVOL_CACHE.pop(oldest, None)
        INTRADAY_RVOL_CACHE[key] = {"ts": now, "data": dict(data)}


def _load_intraday_rvol_at_time(ticker, market_time_epoch=None, lookback_sessions=20):
    """Vero RVOL-at-time cumulativo su barre Yahoo 5m.

    Confronta il volume cumulato della seduta target fino all'ultima barra disponibile
    con la media cumulata, allo stesso numero di barre dalla apertura, delle precedenti
    sedute comparabili. Il risultato e' un riepilogo, non espone la serie grezza.
    """
    ticker = _validate_ticker(ticker)
    try:
        lookback_sessions = max(20, min(int(lookback_sessions), 40))
    except (TypeError, ValueError):
        lookback_sessions = 20
    cache_key = f"{ticker}:{lookback_sessions}"
    cached = _intraday_cache_get(cache_key)
    if cached:
        return cached

    yahoo_ticker = YAHOO_SYMBOL_MAP.get(ticker, ticker)
    params = {
        "interval": "5m",
        "range": "1mo",
        "includePrePost": "false",
        "events": "",
    }
    body = _yahoo_get("/v8/finance/chart/" + quote(yahoo_ticker, safe=""), params, 0)
    try:
        doc = json.loads(body)
        chart = doc["chart"]
        if chart.get("error"):
            err = chart["error"]
            raise ValueError(f"Yahoo intraday: {err.get('description', err) if isinstance(err, dict) else err}")
        result = chart["result"][0]
        meta = result["meta"]
        tzname = meta.get("exchangeTimezoneName")
        if not tzname:
            raise ValueError("Timezone intraday mancante")
        tz = ZoneInfo(tzname)
        stamps = result.get("timestamp") or []
        vols = result["indicators"]["quote"][0].get("volume") or []
        if not stamps or len(stamps) != len(vols):
            raise ValueError("Barre intraday mancanti o disallineate")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Yahoo intraday"):
            raise
        raise ValueError(f"Risposta intraday non validabile: {exc}") from exc

    sessions = {}
    for stamp, vol in zip(stamps, vols):
        try:
            ts_utc = datetime.fromtimestamp(int(stamp), timezone.utc)
            ts_local = ts_utc.astimezone(tz)
            d = ts_local.date().isoformat()
            v = 0.0 if vol is None else float(vol)
            if not math.isfinite(v) or v < 0:
                v = 0.0
            sessions.setdefault(d, []).append((int(stamp), ts_local, v))
        except Exception:
            continue
    if len(sessions) < 21:
        raise ValueError("Storico intraday insufficiente per RVOL-at-time: servono target + 20 sedute precedenti")
    for d in sessions:
        sessions[d].sort(key=lambda x: x[0])

    if market_time_epoch is not None:
        try:
            target_date = datetime.fromtimestamp(int(market_time_epoch), timezone.utc).astimezone(tz).date().isoformat()
        except Exception:
            target_date = max(sessions)
    else:
        target_date = max(sessions)
    if target_date not in sessions:
        # Se il timestamp dello screener non coincide con le barre disponibili, non
        # sostituiamo silenziosamente una seduta diversa.
        raise ValueError(f"Seduta intraday target {target_date} non disponibile")

    target = sessions[target_date]
    if not target:
        raise ValueError("Seduta intraday target vuota")
    bars_elapsed = len(target)
    current_cum = sum(x[2] for x in target)
    if current_cum <= 0:
        raise ValueError("Volume intraday target non disponibile")

    prior_dates = [d for d in sorted(sessions) if d < target_date]
    comparison = []
    used_dates = []
    # Le sedute devono possedere almeno lo stesso numero di barre trascorse: cosi'
    # confrontiamo sempre lo stesso punto relativo della sessione.
    for d in reversed(prior_dates):
        bars = sessions[d]
        if len(bars) < bars_elapsed:
            continue
        cum = sum(x[2] for x in bars[:bars_elapsed])
        if cum > 0:
            comparison.append(cum)
            used_dates.append(d)
        if len(comparison) >= lookback_sessions:
            break
    if len(comparison) < 20:
        raise ValueError(f"Solo {len(comparison)} sedute intraday comparabili; il Mandato ne richiede 20")

    expected = sum(comparison) / len(comparison)
    ordered = sorted(comparison)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    if expected <= 0:
        raise ValueError("Media cumulativa intraday non valida")
    rvol = current_cum / expected
    last_stamp, last_local, _ = target[-1]
    last_utc = datetime.fromtimestamp(last_stamp, timezone.utc)

    quote_date = target_date
    data_current = None
    if market_time_epoch is not None:
        try:
            quote_ts = datetime.fromtimestamp(int(market_time_epoch), timezone.utc)
            quote_date = quote_ts.astimezone(tz).date().isoformat()
            # Tolleranza ampia: il market timestamp puo' essere l'ultimo trade, non
            # necessariamente la fine esatta della barra 5m.
            data_current = abs((quote_ts - last_utc).total_seconds()) <= 15 * 60
        except Exception:
            data_current = None

    out = {
        "rvol_at_time": round(rvol, 6),
        "interval": "5m",
        "target_session": target_date,
        "cutoff_local": last_local.strftime("%H:%M:%S %Z"),
        "current_cum_volume": int(round(current_cum)),
        "expected_cum_volume": int(round(expected)),
        "median_cum_volume": int(round(median)),
        "comparison_sessions": len(comparison),
        "comparison_session_dates": list(reversed(used_dates)),
        "bars_elapsed": bars_elapsed,
        "last_bar_utc": last_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_session": quote_date,
        "data_current": data_current,
        "source": "Yahoo_Finance_Storico",
        "method": "cumulative 5m volume vs mean cumulative volume at same bar count",
        "cache_hit": False,
    }
    _intraday_cache_put(cache_key, out)
    return out


def scan_level_b_volume(market="US", max_history_checks=12, max_results=8):
    market = str(market or "US").upper()
    level_a = filtered_level_a(market)
    return discover_a1_volume(
        market=market, yahoo_get=_yahoo_get, yahoo_post=_yahoo_post, load_series=_load_series,
        level_a=level_a, excluded_symbols=EXCLUDED_SYMBOLS.keys(),
        session_progress_fn=_session_progress, intraday_rvol_fn=_load_intraday_rvol_at_time,
        max_history_checks=int(max_history_checks), max_results=int(max_results),
    )


def scan_level_b_momentum(market="US", max_history_checks=15, max_results=8):
    market = str(market or "US").upper()
    level_a = filtered_level_a(market)
    return discover_a2_momentum(
        market=market, yahoo_get=_yahoo_get, yahoo_post=_yahoo_post, load_series=_load_series,
        level_a=level_a, excluded_symbols=EXCLUDED_SYMBOLS.keys(),
        max_history_checks=int(max_history_checks), max_results=int(max_results),
    )


def scan_level_b_catalyst(market="US", max_results=12):
    market = str(market or "US").upper()
    level_a = filtered_level_a(market)
    return discover_a3_catalyst(
        market=market, yahoo_get=_yahoo_get, level_a=level_a,
        excluded_symbols=EXCLUDED_SYMBOLS.keys(), max_results=int(max_results),
    )


def get_exchange_calendar(exchange=None, ticker=None, start=None, end=None):
    if ticker:
        code = _calendar_code(_validate_ticker(ticker))
        if not code:
            raise ValueError("Ticker senza calendario di borsa (es. cambi =X, mercato 24/5)")
    else:
        code = EXCHANGE_ALIASES.get(str(exchange or "").strip().upper())
        if not code:
            raise ValueError("Borsa non supportata. Codici: " + ", ".join(sorted(set(EXCHANGE_ALIASES.values()))))
    cal = _get_cal(code)
    tz = cal.tz
    now = pd.Timestamp.now(tz="UTC")
    today = now.tz_convert(tz).date()
    s = _parse_iso_date(start, "start") if start else today - timedelta(days=7)
    e = _parse_iso_date(end, "end") if end else today + timedelta(days=10)
    if s >= e:
        raise ValueError("start deve precedere end (esclusivo)")
    if (e - s).days > 400:
        raise ValueError("Intervallo troppo ampio (massimo 400 giorni)")
    try:
        sessions = cal.sessions_in_range(pd.Timestamp(s), pd.Timestamp(e - timedelta(days=1)))
    except Exception as exc:
        raise ValueError(f"Intervallo fuori copertura del calendario: {exc}")
    out = []
    for sess in sessions:
        o, c = cal.session_open(sess), cal.session_close(sess)
        out.append({"date": sess.strftime("%Y-%m-%d"),
                    "open": o.tz_convert(tz).strftime("%H:%M"),
                    "close": c.tz_convert(tz).strftime("%H:%M"),
                    "open_timestamp": o.isoformat(),
                    "close_timestamp": c.isoformat(),
                    "completed": bool(now >= c)})
    days = {x["date"] for x in out}
    closures = []
    cursor = s
    while cursor < e:
        if cursor.weekday() < 5 and cursor.isoformat() not in days:
            closures.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return {"server_version": VERSION, "source": "Official_Exchange_Calendar",
            "library": "exchange_calendars", "calendar": code, "timezone": str(tz),
            "start": s.isoformat(), "end": e.isoformat(), "end_exclusive": True,
            "generated_at_utc": _utc_stamp(), "status_now": _session_status(code),
            "weekday_closures": closures, "sessions": out}


def scan_level_b_attention(market="US", max_results=12):
    market = str(market or "US").upper()
    level_a = filtered_level_a(market)
    return discover_a4_attention(
        market=market, yahoo_get=_yahoo_get, level_a=level_a,
        excluded_symbols=EXCLUDED_SYMBOLS.keys(), max_results=int(max_results),
    )


def scan_level_b_sector_rotation(market="US", max_results=12):
    market = str(market or "US").upper()
    return discover_a5_sector_rotation(
        market=market, load_series=_load_series, max_results=int(max_results or 12),
    )


def _lookup_yahoo_sector(ticker):
    """Resolve sector/industry only for already-discovered Level B candidates.

    Cached for 24h to keep Yahoo traffic small. Failure is non-blocking: Fusion will
    leave A5 at zero rather than inventing a sector.
    """
    canonical = str(ticker or "").strip().upper()
    if not canonical:
        return None
    now = time.time()
    with SECTOR_CACHE_LOCK:
        entry = SECTOR_CACHE.get(canonical)
        if entry and now - entry["ts"] < SECTOR_CACHE_TTL:
            return dict(entry["data"])
        if entry:
            SECTOR_CACHE.pop(canonical, None)

    query_symbol = YAHOO_SYMBOL_MAP.get(canonical, canonical)
    params = {
        "q": query_symbol, "quotesCount": 8, "newsCount": 0, "listsCount": 0,
        "enableFuzzyQuery": "false", "quotesQueryId": "tss_match_phrase_query",
        "enableNavLinks": "false", "enableResearchReports": "false",
        "region": "US", "lang": "en-US",
    }
    body = _yahoo_get("/v1/finance/search", params, 1)
    doc = json.loads(body)
    quotes = [q for q in (doc.get("quotes") or []) if isinstance(q, dict)]
    picked = None
    for q in quotes:
        sym = str(q.get("symbol") or "").strip().upper()
        if sym in (canonical, query_symbol):
            picked = q
            break
    if picked is None and quotes:
        picked = quotes[0]
    if not picked:
        return None
    data = {
        "sector": picked.get("sector") or picked.get("sectorDisp"),
        "industry": picked.get("industry") or picked.get("industryDisp"),
        "source": "YAHOO_SEARCH",
    }
    if not data["sector"] and not data["industry"]:
        return None
    with SECTOR_CACHE_LOCK:
        for k in [k for k, v in SECTOR_CACHE.items() if now - v["ts"] >= SECTOR_CACHE_TTL]:
            SECTOR_CACHE.pop(k, None)
        while len(SECTOR_CACHE) >= SECTOR_CACHE_MAX:
            oldest = min(SECTOR_CACHE, key=lambda k: SECTOR_CACHE[k]["ts"])
            SECTOR_CACHE.pop(oldest, None)
        SECTOR_CACHE[canonical] = {"ts": now, "data": dict(data)}
    return data


def _compute_level_b_fusion(market="US", max_results=10):
    market = str(market or "US").upper()
    # Ogni antenna conserva la propria logica; la fusione riceve solo output compatti.
    a1 = scan_level_b_volume(market, 12, 8)
    a2 = scan_level_b_momentum(market, 15, 8)
    a3 = scan_level_b_catalyst(market, 12)
    a4 = scan_level_b_attention(market, 12)
    a5 = scan_level_b_sector_rotation(market, 50)
    out = fuse_level_b_results(market=market, a1=a1, a2=a2, a3=a3, a4=a4, a5=a5,
                               max_results=int(max_results or 10), sector_lookup=_lookup_yahoo_sector)
    out["antenna_versions"] = {"A1": A1_VERSION, "A2": A2_VERSION, "A3": A3_VERSION, "A4": A4_VERSION, "A5": A5_VERSION}
    out["coverage"] = {"a1_returned": len(a1.get("shortlist") or []), "a2_returned": len(a2.get("shortlist") or []),
                       "a3_returned": len(a3.get("shortlist") or []), "a4_returned": len(a4.get("shortlist") or []),
                       "a5_evaluated": a5.get("evaluated"), "a5_data_errors": len(a5.get("data_errors") or [])}
    return out


def _compute_level_b_verification_pack(market="US", max_results=10):
    """Compact downstream pack: Fusion shortlist + final Yahoo technical drill-down.

    eToro verification deliberately stays outside Render: the consumer must batch-check
    the returned etoro_symbols with the read-only eToro connector immediately before
    any operational proposal.
    """
    market = str(market or "US").upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US oppure EU")
    lim = max(1, min(int(max_results or 10), 10))
    # Reuse the completed Fusion of this market, never a RUNNING response.
    fusion = _level_b_completed("fusion", market, 10)
    if fusion is None:
        fusion = _compute_level_b_fusion(market, 10)
        _level_b_publish_fusion(market, fusion)
    rows = []
    data_errors = []
    for item in (fusion.get("shortlist") or [])[:lim]:
        ticker_raw = item.get("ticker")
        if not ticker_raw:
            continue
        # Canonicalizza eventuali escape provenienti dai radar (es. TLW\\.L -> TLW.L).
        ticker = str(ticker_raw).replace("\\", "").strip().upper()
        try:
            ind = get_indicators(ticker)
            quality_ok = bool(ind.get("data_current") is True and not ind.get("storico_provvisorio") and not ind.get("stale_cache"))
            status = "VERIFICATO" if quality_ok else "BLOCCATO_PER_DATI"
            flags = []
            if ind.get("extended"):
                flags.append("ESTESO")
            if not ind.get("volume_confirmed"):
                flags.append("VOLUME_NON_CONFERMATO")
            if not ind.get("setup"):
                flags.append("SETUP_NON_CONFERMATO")
            rr = ind.get("rr_ref")
            if rr is not None and rr < 2:
                flags.append("RR_GREZZO_LT_2")
            rows.append({
                "ticker": ticker,
                "name": item.get("name") or ind.get("name"),
                "fusion_score": item.get("fusion_score"),
                "detected_by": item.get("detected_by"),
                "sector_key": item.get("sector_key"),
                "sector_state": item.get("sector_state"),
                "sector_score": item.get("sector_score"),
                "yahoo_status": status,
                "history_source": ind.get("source"),
                "history_evidence_policy": ind.get("history_evidence_policy"),
                "storico_provvisorio": bool(ind.get("storico_provvisorio")),
                "next_action": "aggiornare storico prima del ticket" if ind.get("storico_provvisorio") else None,
                "yahoo": {
                    "as_of": ind.get("as_of"),
                    "data_current": ind.get("data_current"),
                    "stale_cache": ind.get("stale_cache"),
                    "last_close": ind.get("last_close"),
                    "currency": ind.get("currency"),
                    "trend": ind.get("trend"),
                    "setup": ind.get("setup"),
                    "trigger_reached": ind.get("trigger_reached"),
                    "trigger_ref": ind.get("trigger_ref"),
                    "extended": ind.get("extended"),
                    "rsi14": ind.get("rsi14"),
                    "atr_pct": ind.get("atr_pct"),
                    "rvol_daily": ind.get("rvol_giornaliero"),
                    "rs20_vs_benchmark_pct": ind.get("rs_20d_vs_benchmark_pct"),
                    "support_20d": ind.get("support_20d"),
                    "resistance_20d_prior": ind.get("resistance_20d_prior"),
                    "stop_ref": ind.get("stop_ref"),
                    "target_ref": ind.get("target_ref"),
                    "rr_ref": ind.get("rr_ref"),
                    "volume_confirmed": ind.get("volume_confirmed"),
                },
                "review_flags": flags,
            })
        except Exception as exc:
            data_errors.append({"ticker": ticker, "reason": str(exc)[:180]})
    return {
        "version": VERIFICATION_PACK_VERSION,
        "server_version": VERSION,
        "market": market,
        "fusion_version": FUSION_VERSION,
        "generated_at_utc": _utc_stamp(),
        "requested": lim,
        "fusion_returned": len(fusion.get("shortlist") or []),
        "yahoo_verified": sum(1 for x in rows if x.get("yahoo_status") == "VERIFICATO"),
        "data_errors": data_errors,
        "etoro_symbols": [x["ticker"] for x in rows if x.get("yahoo_status") == "VERIFICATO"],
        "candidates": rows,
        "etoro_required_checks": [
            "symbol_resolved", "identity_name_or_isin_match", "allowOpenPosition=true", "allowedLeveragesLong includes 1",
            "bid>0", "ask>0", "spread", "quote_asOf_fresh_for_market_state"
        ],
        "note": "Questo pack non verifica eToro e non e' un BUY signal. Prima di proseguire confrontare nome/ISIN Yahoo con lo strumento eToro per evitare omonimie (es. AERO); poi verificare i simboli restituiti con una sola chiamata batch eToro e applicare Mandato, rischio e ticket.",
    }


# -----------------------------------------------------------------------------
# Level B jobs: never hold an MCP/HTTP request during remote discovery.
# One daemon worker retains the existing serial Yahoo anti-429 discipline.
# COMPLETE means job completion, NOT verified data, coverage, or a BUY gate.
# Results retain their original timestamps and expire after 900 seconds.
# No gate, antenna, universe, or history-check count is reduced.
# Deployment: one Gunicorn worker per Render instance (e.g. --workers 1).
# Jobs/cache are process-local, restart cancels them; the next poll starts a new job.
# -----------------------------------------------------------------------------
_LEVEL_B_LOCK = threading.Lock()
_LEVEL_B_QUEUE = queue.Queue(maxsize=4)
_LEVEL_B_JOBS = {}
_LEVEL_B_WORKER = None
_LEVEL_B_TTL = 900


def _level_b_completed(kind, market, max_results=10):
    with _LEVEL_B_LOCK:
        job = _LEVEL_B_JOBS.get((kind, market))
        if job and job["state"] == "COMPLETE" and time.monotonic() - job["finished"] <= _LEVEL_B_TTL:
            return copy.deepcopy(job["result"])
    return None


def _level_b_publish_fusion(market, result):
    with _LEVEL_B_LOCK:
        key = ("fusion", market)
        job = _LEVEL_B_JOBS.get(key)
        if job is None or job["state"] != "RUNNING":
            _LEVEL_B_JOBS[key] = {"id": "fusion-" + market + "-" + str(time.time_ns()),
                                  "state": "COMPLETE", "finished": time.monotonic(),
                                  "generated_at_utc": _utc_stamp(), "result": copy.deepcopy(result)}


def _level_b_worker_loop():
    while True:
        key, job_id = _LEVEL_B_QUEUE.get()
        started = time.monotonic()
        try:
            with _LEVEL_B_LOCK:
                job = _LEVEL_B_JOBS.get(key)
                if not job or job["id"] != job_id or job["state"] != "QUEUED":
                    continue
                job["state"] = "RUNNING"
                job["started"] = time.monotonic()
            started = time.monotonic()
            kind, market = key
            try:
                compute = _compute_level_b_fusion if kind == "fusion" else _compute_level_b_verification_pack
                result = compute(market, 10)
                if not isinstance(result, dict):
                    raise ValueError("Risultato Level B non valido")
                with _LEVEL_B_LOCK:
                    job.update(state="COMPLETE", finished=time.monotonic(), result=copy.deepcopy(result),
                               generated_at_utc=_utc_stamp(), duration_s=round(time.monotonic()-started, 3))
            except Exception as exc:
                log.exception("Level B job failed: %s", key)
                with _LEVEL_B_LOCK:
                    job.update(state="ERROR", finished=time.monotonic(), error=str(exc)[:300],
                               generated_at_utc=_utc_stamp(), duration_s=round(time.monotonic()-started, 3))
        finally:
            log.info("Level B job %s durata %.1fs", key, time.monotonic() - started)
            _LEVEL_B_QUEUE.task_done()


def _level_b_job(kind, market, max_results):
    global _LEVEL_B_WORKER
    market = str(market or "US").upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US oppure EU")
    limit = max(1, min(int(max_results or 10), 10))
    key = (kind, market)
    with _LEVEL_B_LOCK:
        job = _LEVEL_B_JOBS.get(key)
        if job and job["state"] in ("COMPLETE", "ERROR") and time.monotonic()-job["finished"] > _LEVEL_B_TTL:
            _LEVEL_B_JOBS.pop(key)
            job = None
        if not job:
            job = {"id": kind + "-" + market + "-" + str(time.time_ns()), "state": "QUEUED",
                   "requested_at_utc": _utc_stamp()}
            try:
                _LEVEL_B_QUEUE.put_nowait((key, job["id"]))
            except queue.Full:
                return {"server_version": VERSION, "market": market, "job_status": "BUSY",
                        "status": "IN_ELABORAZIONE", "scan_complete": False, "ticket_authorized": False,
                        "retry_after_s": 30, "note": "Coda piena: ripetere la stessa chiamata, nessun risultato disponibile."}
            _LEVEL_B_JOBS[key] = job
        if _LEVEL_B_WORKER is None or not _LEVEL_B_WORKER.is_alive():
            _LEVEL_B_WORKER = threading.Thread(target=_level_b_worker_loop, daemon=True, name="level-b-worker")
            _LEVEL_B_WORKER.start()
        state = job["state"]
        meta = {"server_version": VERSION, "market": market, "job_id": job["id"], "job_status": state,
                "ticket_authorized": False, "result_cache_ttl_s": _LEVEL_B_TTL}
        if state == "COMPLETE":
            out = copy.deepcopy(job["result"])
            meta.update(result_generated_at_utc=job["generated_at_utc"],
                        result_age_s=round(time.monotonic()-job["finished"], 3), duration_s=job.get("duration_s"))
        elif state == "ERROR":
            return dict(meta, status="BLOCCATO PER DATI", scan_complete=False, reason=job["error"],
                        note="Elaborazione fallita; nessun PASS implicito. Il risultato di errore scade dopo 900 s.")
        else:
            return dict(meta, status="IN_ELABORAZIONE", scan_complete=False, retry_after_s=30,
                        note="Ripetere la stessa chiamata dopo 30 s finché job_status=COMPLETE. "
                             "QUEUED/RUNNING non sono scansioni vuote o complete. Nessun gate superato.")
    out.update(meta)
    if kind == "fusion":
        out["shortlist"] = (out.get("shortlist") or [])[:limit]
        out["returned"] = len(out["shortlist"])
    else:
        out["candidates"] = (out.get("candidates") or [])[:limit]
        out["requested"] = limit
        out["yahoo_verified"] = sum(x.get("yahoo_status") == "VERIFICATO" for x in out["candidates"])
        out["etoro_symbols"] = [x["ticker"] for x in out["candidates"] if x.get("yahoo_status") == "VERIFICATO"]
    out["note_async"] = "COMPLETE certifica solo la fine del job. Conservare limiti/errori delle antenne e tutti i gate del master."
    return out


def scan_level_b_fusion(market="US", max_results=10):
    return _level_b_job("fusion", market, max_results)


def scan_level_b_verification_pack(market="US", max_results=10):
    return _level_b_job("verification", market, max_results)


# -----------------------------------------------------------------------------
# Identita' strumento Yahoo/eToro (anti-omonimia)
# -----------------------------------------------------------------------------
_IDENTITY_LEGAL_TOKENS = {
    "A", "AG", "AS", "CO", "COMPANY", "CORP", "CORPORATION", "GROUP", "HLDG", "HOLDING",
    "HOLDINGS", "INC", "INCORPORATED", "LTD", "LIMITED", "NV", "PLC", "SA", "SE", "SPA",
    "SRL", "ORD", "ORDINARY", "CLASS", "CL", "THE",
}


def _identity_name_tokens(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).upper().strip()
    return {tok for tok in text.split() if tok and tok not in _IDENTITY_LEGAL_TOKENS}


def verify_instrument_identity(yahoo_ticker, yahoo_name=None, yahoo_isin=None,
                               etoro_ticker=None, etoro_name=None, etoro_isin=None):
    """Confronta l'identita' Yahoo/eToro prima dei gate operativi.

    Un ISIN uguale e non vuoto prevale sul nome. In assenza di ISIN, il nome
    deve condividere token sostanziali; ticker uguale da solo non e' sufficiente
    perche' i ticker possono essere omonimi su piattaforme diverse.
    """
    yt = str(yahoo_ticker or "").strip().upper()
    et = str(etoro_ticker or "").strip().upper()
    if not yt or not et:
        return {"status": "BLOCCATO_PER_DATI", "identity_name_or_isin_match": False,
                "reason": "Ticker Yahoo o eToro mancante", "ticker_equal": bool(yt and et and yt == et)}
    yi = str(yahoo_isin or "").strip().upper().replace(" ", "")
    ei = str(etoro_isin or "").strip().upper().replace(" ", "")
    yn = _identity_name_tokens(yahoo_name)
    reference = ETORO_ISIN_MAP.get(yt, {})
    if not yi and yn and yn == _identity_name_tokens(reference.get("issuer")):
        yi = str(reference.get("yahoo_isin") or "").upper()
    en = _identity_name_tokens(etoro_name)
    if yi and ei:
        ok = yi == ei
        if not ok:
            yi = ei = ""  # Requested name fallback on discordance; do not return here.
        else:
            return {"status": "PASS" if ok else "FAIL", "identity_name_or_isin_match": ok,
                "reason": "ISIN coincidente, nome diverso accettato" if yn != en else "ISIN coincidente",
                "ticker_equal": yt == et, "yahoo_ticker": yt, "etoro_ticker": et,
                "yahoo_isin": yi, "etoro_isin": ei,
                "name_overlap_tokens": sorted(yn & en)}
    if not yn or not en:
        return {"status": "BLOCCATO_PER_DATI", "identity_name_or_isin_match": False,
                "reason": "ISIN non disponibile e nome Yahoo/eToro mancante o non confrontabile",
                "ticker_equal": yt == et, "yahoo_ticker": yt, "etoro_ticker": et,
                "name_overlap_tokens": sorted(yn & en)}
    overlap = yn & en
    # Un singolo token molto generico non e' sufficiente; ticker uguale non sana il caso.
    min_required = 1 if min(len(yn), len(en)) == 1 else 2
    ok = len(overlap) >= min_required
    return {"status": "PASS" if ok else "FAIL", "identity_name_or_isin_match": ok,
            "reason": "Nome compatibile" if ok else "Nome Yahoo/eToro incompatibile: possibile omonimia",
            "ticker_equal": yt == et, "yahoo_ticker": yt, "etoro_ticker": et,
            "name_overlap_tokens": sorted(overlap),
            "yahoo_name_tokens": sorted(yn), "etoro_name_tokens": sorted(en)}


# -----------------------------------------------------------------------------
# Risposte compatte: sola presentazione, nessuna cache degli esiti di rischio.
# -----------------------------------------------------------------------------
COMPACT_RESPONSE_VERSION = "compact-response-1.0.0"
COMPACT_TOOLS = {"get_market_data", "scan_market", "get_server_status",
                 "verify_instrument_identity", "verify_instrument_identities"}


def _response_format(value):
    mode = "full" if value is None else str(value).strip().lower()
    if mode not in ("full", "compact"):
        raise ValueError("response_format deve essere full oppure compact")
    return mode


def _format_response(name, data, response_format=None):
    """Conserva esiti, numeri, anomalie e shortlist; elimina solo dettagli noti.

    Nessuna lista sconosciuta viene troncata. Le barre compatte non sono input
    sufficienti per un motore rischio esterno: occorre richiedere full.
    """
    mode = _response_format(response_format)
    if mode == "full" or name not in COMPACT_TOOLS or not isinstance(data, dict):
        return data
    out = dict(data)
    omitted = []
    if name == "get_market_data":
        history = out.pop("history", [])
        omitted.append("history")
        out["history_summary"] = {
            "bars_returned_full": len(history),
            "first_date": history[0].get("date") if history else None,
            "last_date": history[-1].get("date") if history else None,
            "last_bar": history[-1] if history else None,
            "raw_series_required_for_external_risk": True,
        }
    elif name == "scan_market":
        # Alias ripetuti: i valori rimangono in coverage_summary/excluded_counts.
        for key in ("acquired_any", "fresh_acquired", "evaluated"):
            if key in out:
                del out[key]
                omitted.append(key)
        out["coverage_summary"] = dict(data.get("coverage_summary") or {})
        out["coverage_summary"]["percentage_denominator"] = data.get("universe_size")
    elif name == "get_server_status":
        # Campioni, anomalie e tutte le metriche operative restano disponibili.
        for key in ("portfolio_watch",):
            if key in out:
                out[key + "_count"] = len(out.pop(key))
                omitted.append(key)
    elif name == "verify_instrument_identity":
        for key in ("yahoo_name_tokens", "etoro_name_tokens"):
            if key in out:
                del out[key]
                omitted.append(key)
    elif name == "verify_instrument_identities":
        out["results"] = [dict(item, result=_format_response(
            "verify_instrument_identity", item["result"], "compact"))
            for item in data.get("results", [])]
    out["response_meta"] = {
        "format": "compact", "version": COMPACT_RESPONSE_VERSION,
        "omitted_fields": omitted,
        "full_response_available": True,
        "full_request": {"tool": name, "response_format": "full"},
        "risk_recalculation_skipped": False,
    }
    return out


def verify_instrument_identities(instruments):
    """Confronti reali in batch; nessuna acquisizione eToro o Yahoo implicita."""
    if not isinstance(instruments, list) or not 1 <= len(instruments) <= 50:
        raise ValueError("instruments deve contenere da 1 a 50 confronti")
    results = []
    counts = {"PASS": 0, "FAIL": 0, "BLOCCATO_PER_DATI": 0}
    keys = ("yahoo_ticker", "yahoo_name", "yahoo_isin", "etoro_ticker",
            "etoro_name", "etoro_isin")
    for index, item in enumerate(instruments):
        if not isinstance(item, dict):
            result = {"status": "BLOCCATO_PER_DATI", "identity_name_or_isin_match": False,
                      "reason": "Confronto non valido: oggetto richiesto"}
        else:
            result = verify_instrument_identity(**{k: item.get(k) for k in keys})
        counts[result["status"]] += 1
        results.append({"index": index, "result": result})
    return {"server_version": VERSION, "generated_at_utc": _utc_stamp(),
            "source": "instrument-identity", "requested": len(instruments),
            "evaluated": len(results), "status_counts": counts, "results": results,
            "note": "Esiti indipendenti: FAIL o dati mancanti bloccano solo il candidato interessato. "
                    "Il batch non verifica prezzi, mercato o negoziabilita del conto."}


# -----------------------------------------------------------------------------
# Client HTTP / Yahoo (anti-429)
# -----------------------------------------------------------------------------
def _http_get(url):
    req = Request(url, headers={"User-Agent": YAHOO_UA,
                                "Accept": "text/html,application/json,text/csv,*/*;q=0.8",
                                "Accept-Language": "en-GB,en;q=0.8"})
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
            if len(body) > 25_000_000:
                raise ValueError("Risposta remota troppo grande")
            return body, response.headers.get_content_type(), response.geturl()
    except HTTPError as exc:
        raise ValueError(f"Fonte remota HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"Fonte non raggiungibile: {exc}") from exc


def _yahoo_raw(url):
    req = Request(url, headers={"User-Agent": YAHOO_UA,
                                "Accept": "application/json,text/plain,*/*",
                                "Accept-Language": "en-US,en;q=0.9",
                                "Referer": "https://finance.yahoo.com/"})
    with _OPENER.open(req, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def _refresh_crumb():
    try:
        _yahoo_raw("https://fc.yahoo.com")
    except Exception:
        pass
    crumb = _yahoo_raw("https://query2.finance.yahoo.com/v1/test/getcrumb").decode().strip()
    if crumb and "<" not in crumb and len(crumb) < 40:
        _YS["crumb"] = crumb


def _yahoo_get(path, params, retries):
    """Chiamate serializzate, distanziate, con un solo retry e pausa dopo 429."""
    with _YLOCK:
        left = _yahoo_cooldown_left()
        if left > 0:
            raise ValueError(f"Yahoo in pausa anti-429 per altri {int(left)} s")
        for attempt in range(retries + 1):
            wait = _YS["last_call"] + YAHOO_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            query = dict(params)
            if _YS["crumb"]:
                query["crumb"] = _YS["crumb"]
            url = f"https://{YAHOO_HOSTS[attempt % 2]}{path}?{urlencode(query)}"
            _YS["last_call"] = time.time()
            _count("yahoo_calls")
            try:
                return _yahoo_raw(url)
            except HTTPError as exc:
                if exc.code == 429:
                    _count("yahoo_429")
                    if attempt < retries:
                        time.sleep(5 + random.uniform(0, 3))
                        continue
                    _YS["cooldown_until"] = time.time() + YAHOO_COOLDOWN
                    raise ValueError("Yahoo HTTP 429 persistente: pausa automatica di 3 minuti") from exc
                if exc.code in (401, 403) and attempt < retries:
                    # Inizializza cookie/sessione Yahoo, ma non allega il crumb al POST.
                    try:
                        _refresh_crumb()
                    except Exception:
                        pass
                    time.sleep(1)
                    continue
                if exc.code == 404:
                    raise ValueError("Ticker non trovato su Yahoo (HTTP 404)") from exc
                raise ValueError(f"Yahoo HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    time.sleep(2)
                    continue
                raise ValueError(f"Yahoo non raggiungibile: {exc}") from exc
            except Exception as exc:
                raise ValueError(f"Errore di lettura Yahoo: {exc}") from exc
        raise ValueError("Yahoo non disponibile")


# -----------------------------------------------------------------------------
# Serie storiche
# -----------------------------------------------------------------------------
def _format_bar(raw):
    d = raw["date"]
    try:
        bar = {"date": d, "open": finite(raw["open"]), "high": finite(raw["high"]),
               "low": finite(raw["low"]), "close": finite(raw["close"])}
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {"date": d, "reason": f"prezzo non valido: {exc}"}
    if min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0:
        return None, {"date": d, "reason": "prezzo non positivo"}
    upper = max(bar["open"], bar["close"])
    lower = min(bar["open"], bar["close"])
    tol = max(1e-8, upper * OHLC_REL_TOLERANCE)
    if bar["high"] < upper:
        if upper - bar["high"] <= tol:
            bar["high"] = upper
        else:
            return None, {"date": d, "reason": "high inferiore a open/close"}
    if bar["low"] > lower:
        if bar["low"] - lower <= tol:
            bar["low"] = lower
        else:
            return None, {"date": d, "reason": "low superiore a open/close"}
    try:
        vol = finite(raw["volume"])
        bar["volume"] = int(round(vol)) if vol >= 0 else None
    except (TypeError, ValueError, OverflowError):
        bar["volume"] = None
    return bar, None



def _yahoo_post(path, params, payload, retries):
    """POST Yahoo JSON serializzato, con la stessa disciplina anti-429 del GET."""
    with _YLOCK:
        left = _yahoo_cooldown_left()
        if left > 0:
            raise ValueError(f"Yahoo in pausa anti-429 per altri {int(left)} s")
        for attempt in range(retries + 1):
            wait = _YS["last_call"] + YAHOO_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            # Il custom screener POST usa la stessa sessione Yahoo del GET.
            # In alcune regioni Yahoo risponde 401 se il crumb non accompagna il POST.
            if not _YS["crumb"]:
                try:
                    _refresh_crumb()
                except Exception:
                    pass
            query = dict(params or {})
            if _YS["crumb"]:
                query["crumb"] = _YS["crumb"]
            url = f"https://{YAHOO_HOSTS[attempt % 2]}{path}?{urlencode(query)}"
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            req = Request(url, data=data, method="POST", headers={
                "User-Agent": YAHOO_UA,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Referer": "https://finance.yahoo.com/",
            })
            _YS["last_call"] = time.time()
            _count("yahoo_calls")
            try:
                with _OPENER.open(req, timeout=HTTP_TIMEOUT) as response:
                    body = response.read()
                    if len(body) > 25_000_000:
                        raise ValueError("Risposta Yahoo troppo grande")
                    return body
            except HTTPError as exc:
                if exc.code == 429:
                    _count("yahoo_429")
                    if attempt < retries:
                        time.sleep(5 + random.uniform(0, 3))
                        continue
                    _YS["cooldown_until"] = time.time() + YAHOO_COOLDOWN
                    raise ValueError("Yahoo HTTP 429 persistente: pausa automatica di 3 minuti") from exc
                if exc.code in (401, 403) and attempt < retries:
                    try:
                        _refresh_crumb()
                    except Exception:
                        pass
                    continue
                if exc.code == 404:
                    raise ValueError("Screener Yahoo non trovato (HTTP 404)") from exc
                raise ValueError(f"Yahoo HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    time.sleep(2)
                    continue
                raise ValueError(f"Yahoo non raggiungibile: {exc}") from exc
            except Exception as exc:
                raise ValueError(f"Errore di lettura Yahoo: {exc}") from exc
        raise ValueError("Yahoo non disponibile")


def _fetch_yahoo_chart(ticker, period, interval, start, end, retries):
    params = {"interval": interval, "events": "div,splits", "includeAdjustedClose": "true"}
    if start or end:
        params["period1"] = int(datetime.combine(
            _parse_iso_date(start) if start else date(1970, 1, 1), dt_time(), timezone.utc).timestamp())
        params["period2"] = int(datetime.combine(
            _parse_iso_date(end) if end else _utc_now().date() + timedelta(days=1),
            dt_time(), timezone.utc).timestamp())
    else:
        params["range"] = period
    yahoo_ticker = YAHOO_SYMBOL_MAP.get(ticker, ticker)
    body = _yahoo_get("/v8/finance/chart/" + quote(yahoo_ticker, safe=""), params, retries)
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise ValueError("Risposta Yahoo non in formato JSON") from exc
    try:
        chart = document["chart"]
        if chart.get("error"):
            err = chart["error"]
            raise ValueError(f"Yahoo chart: {err.get('description', err) if isinstance(err, dict) else err}")
        result = chart["result"][0]
        meta = result["meta"]
        currency = meta.get("currency")
        tzname = meta.get("exchangeTimezoneName")
        if not currency or not tzname:
            raise ValueError("Valuta/timezone mancanti: storico non validabile")
        tz = ZoneInfo(tzname)
        stamps = result.get("timestamp") or []
        if not stamps:
            raise ValueError("Nessuna barra restituita da Yahoo")
        quote_v = result["indicators"]["quote"][0]
        adj_block = result["indicators"].get("adjclose")
        if adj_block and adj_block[0].get("adjclose") is not None:
            adjusted = adj_block[0]["adjclose"]
            adj_method = "Yahoo adjclose/close factor applied to OHLC"
        elif ticker.endswith("=X"):
            adjusted = quote_v.get("close")
            adj_method = "FX: rettifiche non applicabili"
        else:
            raise ValueError("Close rettificati mancanti")
        cols = {k: quote_v.get(k) for k in ("open", "high", "low", "close", "volume")}
        if (any(not isinstance(v, list) or len(v) != len(stamps) for v in cols.values())
                or not isinstance(adjusted, list) or len(adjusted) != len(stamps)):
            raise ValueError("Colonne Yahoo disallineate")
        events = result.get("events") or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Risposta Yahoo chart incompleta") from exc

    def ev_date(ev, key):
        return datetime.fromtimestamp(int(ev.get("date") or int(key)), tz).strftime("%Y-%m-%d")

    dividends, splits = {}, {}
    for key, ev in (events.get("dividends") or {}).items():
        try:
            d = ev_date(ev, key)
            dividends[d] = dividends.get(d, 0.0) + finite(ev.get("amount", 0))
        except (TypeError, ValueError):
            pass
    for key, ev in (events.get("splits") or {}).items():
        try:
            num, den = ev.get("numerator"), ev.get("denominator")
            if num and den:
                splits[ev_date(ev, key)] = finite(num) / finite(den)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    last_index = {}
    for i, stamp in enumerate(stamps):
        last_index[datetime.fromtimestamp(int(stamp), tz).strftime("%Y-%m-%d")] = i
    duplicates_removed = len(stamps) - len(last_index)

    bars, dropped = [], []
    for d in sorted(last_index):
        i = last_index[d]
        close, adj = cols["close"][i], adjusted[i]
        if close is None or adj is None or not close:
            dropped.append({"date": d, "reason": "close mancante",
                            "raw_close_missing": close is None,
                            "adjusted_close_missing": adj is None,
                            "raw_close_zero": close == 0,
                            "origin": "Yahoo chart response"})
            continue
        factor = float(adj) / float(close)

        def sc(v):
            return None if v is None else v * factor

        bar, issue = _format_bar({"date": d, "open": sc(cols["open"][i]), "high": sc(cols["high"][i]),
                                  "low": sc(cols["low"][i]), "close": adj, "volume": cols["volume"][i]})
        if issue:
            dropped.append(issue)
        else:
            bars.append(bar)
    valid = {b["date"] for b in bars}
    actions = [{"date": d, "dividends": dividends.get(d, 0.0), "stock_splits": splits.get(d, 0.0)}
               for d in sorted(set(dividends) | set(splits)) if d in valid]
    return {"bars": bars, "dropped": dropped, "actions": actions, "currency": currency,
            "tz": tzname, "duplicates_removed": duplicates_removed, "adj_method": adj_method,
            "name": meta.get("longName") or meta.get("shortName"),
            "instrument_type": meta.get("instrumentType") or meta.get("quoteType")}


def _repair_recent_gaps(ticker, period, interval, start, end, raw, retries):
    """One bounded retry from the same Yahoo chart source; never interpolate."""
    if interval != "1d" or not raw.get("bars"):
        return raw
    latest = date.fromisoformat(raw["bars"][-1]["date"])
    gaps = sorted({r["date"] for r in raw.get("dropped", [])
                   if 0 <= (latest-date.fromisoformat(r["date"])).days <= 30})
    if not gaps:
        return raw
    lo = (date.fromisoformat(gaps[0])-timedelta(days=3)).isoformat()
    hi = (date.fromisoformat(gaps[-1])+timedelta(days=3)).isoformat()
    try:
        extra = _fetch_yahoo_chart(ticker, period, interval, lo, hi, retries)
    except Exception as exc:
        raw["gap_repair"] = {"attempted": True, "status": "FAILED", "error": str(exc)[:200]}
        return raw
    old = {b["date"]: b for b in raw["bars"]}
    new = {b["date"]: b for b in extra["bars"]}
    overlap = set(old) & set(new)
    consistent = extra["currency"] == raw["currency"] and extra["tz"] == raw["tz"] and extra["adj_method"] == raw["adj_method"] and bool(overlap)
    consistent = consistent and all(math.isclose(old[d][k], new[d][k], rel_tol=1e-7, abs_tol=1e-6)
                                  for d in overlap for k in ("open", "high", "low", "close")
                                  if old[d][k] is not None and new[d][k] is not None)
    fixed = sorted(set(gaps) & set(new)) if consistent else []
    for d in fixed:
        old[d] = new[d]
    raw["bars"] = [old[d] for d in sorted(old)]
    raw["dropped"] = [r for r in raw["dropped"] if r["date"] not in fixed]
    raw["actions"] += [a for a in extra["actions"] if a["date"] in fixed and a not in raw["actions"]]
    raw["gap_repair"] = {"attempted": True, "status": "RECOVERED" if fixed else "UNRESOLVED",
                         "source": "Yahoo_Finance_Storico", "start": lo, "end_exclusive": hi,
                         "recovered_dates": fixed, "overlap_consistent": consistent, "checked_at": _utc_stamp()}
    return raw


def _build_payload(ticker, period, interval, start, end, code, expected, cal_error, raw):
    tz = ZoneInfo(raw["tz"])
    bars = raw["bars"]
    partial = []
    if interval == "1d":
        limit = expected if expected else (datetime.now(tz).date() - timedelta(days=1)).isoformat()
        partial = [b["date"] for b in bars if b["date"] > limit]
        bars = [b for b in bars if b["date"] <= limit]
    if not bars:
        raise ValueError("Nessuna seduta conclusa valida")
    currency = raw["currency"]
    warnings = []
    if cal_error:
        warnings.append(f"Calendario non disponibile ({cal_error}): completezza stimata")
    if interval == "1d" and code is None:
        warnings.append("Nessun calendario di borsa: barre del giorno corrente escluse")
    if interval != "1d":
        warnings.append("Ultima barra settimanale/mensile potenzialmente parziale")
    if raw["duplicates_removed"]:
        warnings.append(f"Rimosse {raw['duplicates_removed']} barre duplicate di Yahoo")
    end_d = bars[-1]["date"]
    dates = {b["date"] for b in bars}
    payload = {
        "server_version": VERSION, "source": "Yahoo_Finance_Storico",
        "ticker": ticker, "name": raw["name"], "instrument_type": raw.get("instrument_type"), "interval": interval,
        "period": None if (start or end) else period, "start": start, "end": end, "end_exclusive": True,
        "currency": currency, "price_unit": currency, "currency_source": "Yahoo chart metadata",
        "major_currency": {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(currency, currency),
        "price_scale_to_major_currency": 0.01 if currency in ("GBp", "GBX", "ZAc", "ILA") else 1.0,
        "exchange_timezone": raw["tz"], "calendar": code,
        "adjusted": True, "adjustment_method": raw["adj_method"],
        "only_completed_sessions": interval == "1d",
        "partial_bars_excluded": partial,
        "last_completed_session_expected": expected,
        "coverage_start": bars[0]["date"], "coverage_end": end_d,
        "data_current": (end_d == expected) if (interval == "1d" and expected) else None,
        "valid_bars": len(bars), "dates_sorted": True, "duplicates_present": False,
        "generated_at_utc": _utc_stamp(),
        "gap_repair": raw.get("gap_repair"),
        "data_quality": {"strict_json": True, "dropped_bars_count": len(raw["dropped"]),
                         "dropped_bars": raw["dropped"][-5:],
                         "risk_sample_sufficient": False,
                         "calendar_validated": bool(code and expected and not cal_error)},
        "warnings": warnings,
        "history": bars,
        "actions": [a for a in raw["actions"] if a["date"] in dates],
    }
    # La numerosità da sola non prova la continuità: una seduta mancante
    # non deve produrre un rendimento multi-seduta trattato come giornaliero.
    missing = []
    if interval == "1d" and code and expected and not cal_error:
        cal = _get_cal(code)
        target = min(expected, (_parse_iso_date(end) - timedelta(days=1)).isoformat()) if end else expected
        sessions = cal.sessions_in_range(pd.Timestamp(bars[0]["date"]), pd.Timestamp(target))
        missing = [s.strftime("%Y-%m-%d") for s in sessions
                   if s.strftime("%Y-%m-%d") not in dates]
        payload["requested_last_session_expected"] = sessions[-1].strftime("%Y-%m-%d") if len(sessions) else None
        payload["requested_range_complete"] = bool(len(sessions) and not missing)
    payload["data_quality"]["missing_internal_sessions"] = missing
    payload["data_quality"]["risk_sample_sufficient"] = bool(
        interval == "1d" and code and expected and not cal_error
        and not missing and len(bars) - 1 >= 250)
    if missing:
        warnings.append("Sedute interne mancanti: rendimenti giornalieri non continui")
    if ticker.endswith("=X"):
        payload["data_quality"]["fx_calendar_validated"] = False
        warnings.append("FX: calendario e convenzione di chiusura non validati per il rischio")
    json.dumps(payload, allow_nan=False)
    return payload


def _series_key(ticker, period, interval, start, end):
    return f"s:{ticker}:{period}:{interval}:{start}:{end}"


def _cache_get(key):
    with CACHE_LOCK:
        return CACHE.get(key)


def _cache_data(entry):
    """Materializza un payload solo quando serve.

    Le serie storiche restano compresse in RAM; ETF e altri oggetti piccoli
    possono continuare a usare il formato data tradizionale.
    """
    if "blob" in entry:
        return json.loads(zlib.decompress(entry["blob"]).decode("utf-8"))
    return entry["data"]


def _cache_put(key, entry):
    now = time.time()
    stored = entry
    if key.startswith("s:") and "data" in entry:
        payload = entry["data"]
        raw = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        blob = zlib.compress(raw, 6)
        stored = {k: v for k, v in entry.items() if k != "data"}
        stored["coverage_end"] = payload.get("coverage_end")
        stored["requested_range_complete"] = payload.get("requested_range_complete")
        stored["internal_gaps"] = bool(payload.get("data_quality", {}).get("missing_internal_sessions"))
        stored["blob"] = blob
        stored["blob_bytes"] = len(blob)
    with CACHE_LOCK:
        for k in [k for k, v in CACHE.items() if now - v["ts"] >= STALE_MAX_AGE]:
            del CACHE[k]
        while len(CACHE) >= CACHE_MAX_ITEMS:
            del CACHE[min(CACHE, key=lambda k: CACHE[k]["ts"])]
        CACHE[key] = stored


def _entry_has_internal_gaps(entry):
    if "internal_gaps" in entry:
        return entry["internal_gaps"]
    return bool(_cache_data(entry).get("data_quality", {}).get("missing_internal_sessions"))


def _is_fresh(entry, code, expected, interval, end):
    age = time.time() - entry["ts"]
    if interval == "1d" and expected and entry.get("coverage_end") != expected:
        return age < MIN_REFETCH_S
    if interval == "1d" and _entry_has_internal_gaps(entry):
        return age < LAG_REFETCH_S
    if end:
        if entry.get("requested_range_complete") is False:
            return age < MIN_REFETCH_S
        # Un end esplicito non rende storico un intervallo che include
        # l'ultima seduta attesa. Non trattenere dati incompleti per 24 ore.
        if expected and end > expected:
            if entry.get("expected") != expected:
                return False
            if entry.get("coverage_end") != expected:
                return age < MIN_REFETCH_S
            return age < 86400
        return age < 86400
    if interval != "1d" or code is None or expected is None:
        return age < NO_CAL_TTL
    if entry["expected"] != expected:
        return False
    if not entry["after_close"]:
        return age < MIN_REFETCH_S
    if entry.get("coverage_end") != expected:
        return age < min(LAG_REFETCH_S, MIN_REFETCH_S)
    return True


def _from_cache(entry, expected):
    d = dict(_cache_data(entry))
    d["cache_hit"] = True
    d["cache_age_s"] = int(time.time() - entry["ts"])
    d["warnings"] = list(d.get("warnings", []))
    if d.get("interval") == "1d" and expected:
        d["last_completed_session_expected"] = expected
        d["data_current"] = d["coverage_end"] == expected
    return d


ETF_TOLERANCE_SUFFIXES = (".L", ".DE", ".PA", ".MI", ".AS", ".MC", ".SW", ".ST", ".CO", ".OL", ".HE")
PROVISIONAL_WARNING = "STORICO_PROVVISORIO: ultima barra T-1, accettata con tolleranza 1 seduta"


def _load_identity_map(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
        if not isinstance(result, dict):
            raise ValueError("oggetto JSON richiesto")
        return result
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        log.error("Mappa %s non valida: %s", filename, exc)
        return {}


ETORO_ISIN_MAP = _load_identity_map("etoro_isin_map.json")
ETORO_ETF_MAP = _load_identity_map("etoro_etf_map.json")
STOOQ_ETF_MAP = _load_identity_map("stooq_etf_map.json")


def _verified_etf(payload):
    ticker = str(payload.get("ticker", "")).upper()
    return ticker.endswith(ETF_TOLERANCE_SUFFIXES) and (
        ticker in ETF_PRODUCTS or str(payload.get("instrument_type", "")).upper() == "ETF")


def _session_gap(payload):
    code = payload.get("calendar")
    end = payload.get("coverage_end")
    expected = payload.get("last_completed_session_expected")
    if not code or not end or not expected or not payload.get("data_quality", {}).get("calendar_validated"):
        return None
    if end > expected:
        return None
    sessions = _get_cal(code).sessions_in_range(pd.Timestamp(end), pd.Timestamp(expected))
    dates = [s.strftime("%Y-%m-%d") for s in sessions]
    if end not in dates or expected not in dates:
        return None
    return max(0, len(dates)-1)


def _history_continuity(payload):
    """Actual calendar pairs and the uninterrupted tail, without filling gaps."""
    if (payload.get("interval") != "1d" or not payload.get("calendar")
            or not payload.get("data_quality", {}).get("calendar_validated")):
        raise ValueError("Calendario giornaliero non convalidato")
    history = payload.get("history") or []
    dates = [row.get("date") for row in history]
    if (not dates or not all(isinstance(d, str) for d in dates)
            or dates != sorted(set(dates)) or payload.get("duplicates_present")):
        raise ValueError("Date storico non ordinate o duplicate")
    if not all(type(row.get("close")) in (int, float)
               and math.isfinite(row["close"]) and row["close"] > 0 for row in history):
        raise ValueError("Chiusure storico non valide")
    sessions = [d.strftime("%Y-%m-%d") for d in _get_cal(payload["calendar"]).sessions_in_range(
        pd.Timestamp(dates[0]), pd.Timestamp(dates[-1]))]
    present = set(dates)
    if not present.issubset(set(sessions)):
        raise ValueError("Barre fuori calendario")
    missing = [d for d in sessions if d not in present]
    tail = 0
    for d in reversed(sessions):
        if d not in present:
            break
        tail += 1
    return {"valid_return_pairs": sum(a in present and b in present for a, b in zip(sessions, sessions[1:])),
            "contiguous_tail_bars": tail, "missing_internal_sessions": missing,
            "latest_return_complete": tail >= 2}


def _apply_history_policy(payload):
    out = dict(payload)
    quality = out["data_quality"] = dict(payload.get("data_quality", {}))
    out["warnings"] = [w for w in payload.get("warnings", []) if w != PROVISIONAL_WARNING]
    gap = _session_gap(out) if out.get("interval") == "1d" else None
    provisional = bool(_verified_etf(out) and gap == 1)
    # Endpoint tolerance and the usable recent segment are independent checks.
    holes = [d for d in quality.get("missing_internal_sessions", [])
             if d <= out.get("coverage_end", "")]
    out["storico_provvisorio"] = provisional
    quality["stale_sessions"] = gap
    quality["internal_holes_before_last_bar"] = holes
    continuity = None
    if out.get("interval") == "1d":
        try:
            continuity = _history_continuity(out)
        except (ValueError, KeyError, TypeError) as exc:
            quality["continuity_error"] = str(exc)
        else:
            quality.pop("continuity_error", None)
            holes = continuity["missing_internal_sessions"]
            quality["internal_holes_before_last_bar"] = holes
            quality["valid_return_pairs"] = continuity["valid_return_pairs"]
            quality["latest_return_complete"] = continuity["latest_return_complete"]
            out["contiguous_tail_bars"] = continuity["contiguous_tail_bars"]
    # Sixty bars is the existing indicator minimum; SMA200/long returns may be N/D.
    accepted = bool((out.get("data_current") is True or provisional)
                    and continuity and continuity["contiguous_tail_bars"] >= 60)
    out["screening_history_accepted"] = accepted
    if out.get("interval") == "1d":
        quality["risk_sample_sufficient"] = bool((out.get("data_current") is True or provisional)
            and continuity and continuity["latest_return_complete"] and continuity["valid_return_pairs"] >= 250)
    if provisional:
        out["data_current"] = False
        out["warnings"].append(PROVISIONAL_WARNING)
        out["next_action"] = "aggiornare storico prima del ticket"
    if out.get("secondary_source_used"):
        out["storico_provvisorio"] = True
        out["data_current"] = False
        out["screening_history_accepted"] = bool(gap is not None and gap <= 1
            and continuity and continuity["contiguous_tail_bars"] >= 60)
        out["next_action"] = "aggiornare storico prima del ticket"
    return out


def _fetch_stooq_fallback(ticker):
    """CSV only for a separately verified same-listing ETF mapping. Never guess aliases."""
    import csv
    import io
    mapping = STOOQ_ETF_MAP.get(ticker)
    if not isinstance(mapping, dict) or mapping.get("verified") is not True or not all(mapping.get(k) for k in ("symbol", "isin", "currency", "verification_reference", "price_basis")):
        raise ValueError("Stooq: mapping ETF/ISIN/valuta/rettifiche non verificato")
    symbol = str(mapping["symbol"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", symbol):
        raise ValueError("Stooq: simbolo non valido")
    url = "https://stooq.com/q/d/l/?s=" + quote(symbol, safe="") + "&i=d"
    request = Request(url, headers={"User-Agent": YAHOO_UA})
    with urlopen(request, timeout=15) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Stooq: risposta troppo grande")
    rows = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        date.fromisoformat(row["Date"])
        bar, issue = _format_bar({"date": row.get("Date"), "open": float(row["Open"]),
                                 "high": float(row["High"]), "low": float(row["Low"]),
                                 "close": float(row["Close"]), "volume": float(row["Volume"])})
        if issue or bar.get("volume") is None:
            raise ValueError("Stooq: barra non valida")
        rows.append(bar)
    if not rows or len({r["date"] for r in rows}) != len(rows):
        raise ValueError("Stooq: CSV vuoto o date duplicate")
    return {"source": "STOOQ_FALLBACK", "url": url, "mapping": mapping,
            "bars": sorted(rows, key=lambda r: r["date"]), "fetched_at": _utc_stamp()}


def _try_stooq_gap(payload):
    """Recover one missing endpoint, only with matching overlapping adjusted OHLC."""
    gap = _session_gap(payload)
    if not _verified_etf(payload) or gap is None or gap <= 1:
        return payload
    out = copy.deepcopy(payload)
    try:
        extra = _fetch_stooq_fallback(payload["ticker"])
        mapping = extra["mapping"]
        expected_isin = ETF_PRODUCTS.get(payload["ticker"], {}).get("isin")
        if not expected_isin or mapping["isin"] != expected_isin or mapping["currency"] != payload["currency"] or mapping["price_basis"] != payload["adjustment_method"]:
            raise ValueError("Stooq: identita/valuta/base rettificata incompatibili")
        bars = {b["date"]: b for b in extra["bars"]}
        old = {b["date"]: b for b in payload["history"]}
        overlap = sorted(set(old) & set(bars))[-5:]
        if len(overlap) < 2 or not all(math.isclose(old[d][k], bars[d][k], rel_tol=1e-7, abs_tol=1e-6) for d in overlap for k in ("open", "high", "low", "close")):
            raise ValueError("Stooq: sovrapposizione non compatibile")
        sessions = _get_cal(payload["calendar"]).sessions_in_range(
            pd.Timestamp(payload["coverage_end"]), pd.Timestamp(payload["last_completed_session_expected"]))
        # Recover the NEXT missing session, never jump across an absent daily bar.
        expected = sessions[1].strftime("%Y-%m-%d")
        if expected not in bars:
            raise ValueError("Stooq: barra richiesta assente")
        # Never replace Yahoo rows or mark a mixed-source series ticket-current.
        out["history"].append(bars[expected])
        out["history"].sort(key=lambda b: b["date"])
        out["coverage_end"] = expected
        out["valid_bars"] = len(out["history"])
        out["secondary_source_used"] = True
        out["source"] = "Yahoo_Finance_Storico+STOOQ_FALLBACK"
        out["bar_sources"] = {expected: {"source": "STOOQ_FALLBACK", "url": extra["url"], "timestamp": extra["fetched_at"]}}
        out["data_quality"]["missing_internal_sessions"] = [d for d in out["data_quality"].get("missing_internal_sessions",[]) if d != expected]
        out["data_quality"]["dropped_bars"] = [r for r in out["data_quality"].get("dropped_bars",[]) if r.get("date") != expected]
        out["data_quality"]["dropped_bars_count"] = len(out["data_quality"]["dropped_bars"])
        out["warnings"].append("fonte secondaria usata per barra mancante")
    except Exception as exc:
        out["warnings"].append("STOOQ_FALLBACK non utilizzabile: " + str(exc)[:200])
    return out


def _load_series(ticker, period=DEFAULT_PERIOD, interval="1d", start=None, end=None,
                 user=True, allow_fetch=True, stale_ok=False, prefer_etoro=True):
    ticker = _validate_ticker(ticker)
    _validate_period(period, interval, start, end)
    if prefer_etoro and ticker in ETORO_ETF_MAP and interval == "1d" and etoro_history.risk_enabled():
        return _load_etf_series(ticker, period, interval, start, end, user, allow_fetch, stale_ok)
    code = _calendar_code(ticker)
    expected, close_utc, cal_error = None, None, None
    if code and interval == "1d":
        try:
            expected, close_utc = _last_completed(code)
        except Exception as exc:
            cal_error = str(exc)
    key = _series_key(ticker, period, interval, start, end)
    entry = _cache_get(key)
    if entry and _cache_data(entry).get("source") == "ETORO_DAILY_BID":
        entry = None  # Yahoo-only path must not reuse the broker cache after rollback.
    if entry and _is_fresh(entry, code, expected, interval, end):
        if user:
            _count("cache_hits")
        return _apply_history_policy(_from_cache(entry, expected))
    usable_stale = bool(entry and time.time() - entry["ts"] < STALE_MAX_AGE)
    if not allow_fetch:
        if stale_ok and usable_stale:
            d = _from_cache(entry, expected)
            d["stale_cache"] = True
            return _apply_history_policy(d)
        return None
    fail = _FAILS.get(ticker)
    if fail and time.time() - fail[0] < FAIL_SKIP_S and not usable_stale:
        raise ValueError(f"Fallito di recente: {fail[1]}")
    try:
        raw = _fetch_yahoo_chart(ticker, period, interval, start, end, retries=1 if user else 0)
        raw = _repair_recent_gaps(ticker, period, interval, start, end, raw, retries=0)
        payload = _build_payload(ticker, period, interval, start, end, code, expected, cal_error, raw)
        if not end and interval == "1d":
            gap = _session_gap(payload)
            if _verified_etf(payload) and gap is not None and gap > 1 and not (raw.get("gap_repair") or {}).get("attempted"):
                # One explicit retry also when Yahoo omitted a row without a null/dropped record.
                try:
                    retry_raw = _fetch_yahoo_chart(ticker, period, interval, start, end, retries=0)
                    retried = _build_payload(ticker, period, interval, start, end, code, expected, cal_error, retry_raw)
                    if retried["coverage_end"] >= payload["coverage_end"]:
                        payload = retried
                    payload["endpoint_retry"] = {"attempted": True, "source": "Yahoo_Finance_Storico"}
                except ValueError as exc:
                    payload["endpoint_retry"] = {"attempted": True, "error": str(exc)[:150]}
                    payload["warnings"].append("Retry Yahoo non riuscito: " + str(exc)[:150])
            payload = _try_stooq_gap(payload)
        payload = _apply_history_policy(payload)
    except ValueError as exc:
        msg = str(exc)
        if "429" not in msg and "pausa" not in msg:
            _FAILS[ticker] = (time.time(), msg[:150])
        if usable_stale:
            d = _from_cache(entry, expected)
            d["stale_cache"] = True
            d["warnings"].append(f"Fonte non disponibile ({msg[:100]}): restituita cache precedente")
            return _apply_history_policy(d)
        raise
    _FAILS.pop(ticker, None)
    after_close = bool(close_utc is not None and
                       pd.Timestamp.now(tz="UTC") >= close_utc + pd.Timedelta(minutes=20))
    _cache_put(key, {"ts": time.time(), "expected": expected, "after_close": after_close, "data": payload})
    return dict(payload, cache_hit=False)


def get_market_data(ticker, period=DEFAULT_PERIOD, interval="1d", start=None, end=None, max_bars=None):
    period = period or DEFAULT_PERIOD
    interval = interval or "1d"
    sliced_from = None
    if interval == "1d" and not start and not end and (period in SHORT_PERIOD_DAYS or period == "ytd"):
        payload = _load_series(ticker, DEFAULT_PERIOD, "1d")
        if period == "ytd":
            cutoff = date(_utc_now().year, 1, 1).isoformat()
        else:
            cutoff = (_utc_now().date() - timedelta(days=SHORT_PERIOD_DAYS[period])).isoformat()
        history = [b for b in payload["history"] if b["date"] >= cutoff]
        sliced_from = DEFAULT_PERIOD
    else:
        payload = _load_series(ticker, period, interval, start, end)
        history = payload["history"]
    try:
        max_bars = int(max_bars) if max_bars is not None else DEFAULT_RETURNED_BARS
    except (TypeError, ValueError):
        max_bars = DEFAULT_RETURNED_BARS
    max_bars = max(1, min(max_bars, MAX_RETURNED_BARS))
    out_hist = history[-max_bars:]
    if not out_hist:
        raise ValueError("Nessuna seduta conclusa nel periodo richiesto")
    dates = {b["date"] for b in out_hist}
    out = {k: v for k, v in payload.items() if k not in ("history", "actions")}
    out.update(period_requested=period, served_from_cache_of=sliced_from,
               history=out_hist, actions=[a for a in payload["actions"] if a["date"] in dates],
               bars_returned=len(out_hist), truncated=len(history) > len(out_hist),
               next_end=out_hist[0]["date"] if len(history) > len(out_hist) else None)
    out["returned_coverage_start"] = out_hist[0]["date"]
    out["returned_coverage_end"] = out_hist[-1]["date"]
    # Il flag campione si riferisce alle barre effettivamente consegnate.
    out["data_quality"] = dict(out.get("data_quality", {}))
    out["data_quality"]["risk_sample_sufficient"] = bool(
        out["data_quality"].get("risk_sample_sufficient") and len(out_hist) - 1 >= (200 if out.get("source")=="ETORO_DAILY_BID" or out.get("history_source_selection") else 250))
    if out.get("history_source_selection"):
        delivered = _etf_history_quality(dict(out,history=out_hist),ticker)
        out["data_quality"]["risk_sample_sufficient"] = delivered["usable_for_risk"]
        out["data_quality"]["valid_return_pairs"] = delivered["valid_return_pairs"]
        out["data_quality"]["risk_sample_status"] = ("VALIDO" if delivered["valid_return_pairs"]>=250
            else "PROVVISORIO_200_249" if delivered["valid_return_pairs"]>=200 else "INSUFFICIENTE")
        out["contiguous_tail_bars"] = delivered["contiguous_tail_bars"]
    elif interval == "1d" and out["data_quality"].get("calendar_validated"):
        delivered = _history_continuity(dict(out, history=out_hist))
        out["data_quality"]["valid_return_pairs"] = delivered["valid_return_pairs"]
        out["data_quality"]["latest_return_complete"] = delivered["latest_return_complete"]
        out["data_quality"]["risk_sample_sufficient"] = bool(
            out["data_quality"].get("risk_sample_sufficient")
            and delivered["latest_return_complete"] and delivered["valid_return_pairs"] >= 250)
        out["contiguous_tail_bars"] = delivered["contiguous_tail_bars"]
    return out


# -----------------------------------------------------------------------------
# Indicatori
# -----------------------------------------------------------------------------
def _ret_k(close, k):
    if len(close) <= k:
        return None
    base = float(close.iloc[-1 - k])
    return float(close.iloc[-1]) / base - 1 if base > 0 else None


def _indicators_from_payload(payload, bench_payload=None, bench_ticker=None):
    bars = payload["history"]
    if payload.get("interval") == "1d":
        # Indicators require contiguous closes; don't turn holes into shorter SMA periods.
        tail = _history_continuity(payload)["contiguous_tail_bars"]
        bars = bars[-tail:] if tail else []
    if len(bars) < 60:
        raise ValueError(f"Sedute concluse insufficienti per gli indicatori ({len(bars)} < 60)")
    df = pd.DataFrame(bars)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = pd.to_numeric(df["volume"], errors="coerce")
    n = len(c)
    last = float(c.iloc[-1])

    def sma(k):
        return float(c.iloc[-k:].mean()) if n >= k else None

    s20, s50, s200 = sma(20), sma(50), sma(200)
    delta = c.diff()
    avg_g = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_l = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = (100 - 100 / (1 + avg_g / avg_l.replace(0, np.nan))).where(avg_l > 0, 100.0)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])

    v_prev20 = v.iloc[-21:-1]
    vol_prev20 = float(v_prev20.mean()) if v_prev20.notna().sum() >= 15 else None
    last_vol = float(v.iloc[-1]) if pd.notna(v.iloc[-1]) else None
    rvol = last_vol / vol_prev20 if (last_vol is not None and vol_prev20) else None
    chg20, v20 = c.diff().iloc[-20:], v.iloc[-20:]
    up_v, dn_v = float(v20[chg20 > 0].sum()), float(v20[chg20 < 0].sum())
    updown = up_v / dn_v if dn_v > 0 else None
    scale = payload.get("price_scale_to_major_currency", 1.0)
    turnover = float((c.iloc[-20:] * v20).mean()) * scale if v20.notna().sum() >= 15 else None

    vol20 = float(c.pct_change().iloc[-20:].std() * math.sqrt(252))
    hi20_prior = float(h.iloc[-21:-1].max())
    lo20 = float(l.iloc[-20:].min())
    hi60 = float(h.iloc[-60:].max())
    hi252, lo252 = float(h.iloc[-252:].max()), float(l.iloc[-252:].min())

    if s50 and s200:
        trend = "RIALZISTA" if last > s50 > s200 else "RIBASSISTA" if last < s50 < s200 else "LATERALE/MISTO"
    else:
        trend = "N/D"

    ret20 = _ret_k(c, 20)
    rs20, bench_note = None, None
    if bench_payload is not None:
        bbars = bench_payload["history"]
        if bench_payload.get("interval") == "1d":
            try:
                btail = _history_continuity(bench_payload)["contiguous_tail_bars"]
                bbars = bbars[-btail:] if btail else []
            except (ValueError, KeyError, TypeError):
                bbars = []
                bench_note = "Benchmark con calendario/date non convalidati"
        if len(bbars) < 21:
            bench_note = bench_note or "Segmento consecutivo benchmark inferiore a21 barre; forza relativa N/D"
        if len(bbars) >= 21:
            bret = _ret_k(pd.Series([b["close"] for b in bbars], dtype=float), 20)
            if ret20 is not None and bret is not None:
                rs20 = ret20 - bret
            if bbars[-1]["date"] != bars[-1]["date"]:
                bench_note = f"Benchmark al {bbars[-1]['date']}, titolo al {bars[-1]['date']}"

    rsi_v = float(rsi.iloc[-1])
    rsi_min10 = float(rsi.iloc[-10:].min())
    h_last, h_prev = float(hist.iloc[-1]), float(hist.iloc[-4:-1].min())
    breakout = bool(last > hi20_prior)

    # Setup preliminari: servono solo a creare la shortlist; non sono segnali operativi.
    # AVVIO_TREND cerca una ripartenza recente senza inseguire movimenti gia' estesi.
    setup, trigger_ref, trigger_reached = None, None, False
    cross20_50_recent = False
    reclaim50_recent = False
    if s50 is not None and n >= 12:
        sma20_series = c.rolling(20).mean()
        sma50_series = c.rolling(50).mean()
        for j in range(max(1, n - 10), n):
            if pd.notna(sma20_series.iloc[j-1]) and pd.notna(sma50_series.iloc[j-1]):
                if sma20_series.iloc[j-1] <= sma50_series.iloc[j-1] and sma20_series.iloc[j] > sma50_series.iloc[j]:
                    cross20_50_recent = True
            if c.iloc[j-1] <= sma50_series.iloc[j-1] and c.iloc[j] > sma50_series.iloc[j]:
                reclaim50_recent = True
    ret5 = _ret_k(c, 5)
    extended = bool((s20 is not None and atr > 0 and last - s20 > 2 * atr) or
                    (ret5 is not None and ret5 > 0.10))
    volume_confirmed = bool((rvol is not None and rvol >= 1.5) or (updown is not None and updown >= 1.2))

    if (not extended and 50 <= rsi_v <= 65 and (cross20_50_recent or reclaim50_recent)
            and (rs20 is None or rs20 > -0.02)):
        setup = "AVVIO_TREND"
        trigger_ref = s50
        trigger_reached = bool(last > s50) if s50 is not None else False
    elif trend == "RIALZISTA" and 50 <= rsi_v <= 75 and last >= 0.97 * max(hi20_prior, last):
        setup, trigger_ref, trigger_reached = "CONTINUAZIONE", hi20_prior, breakout
    elif (s50 and s200 and s50 > s200 and last > s200 and abs(last / s50 - 1) <= 0.04
          and 35 <= rsi_v <= 55 and (ret20 or 0) < 0):
        trigger_ref = float(h.iloc[-2])
        setup, trigger_reached = "CORREZIONE", bool(last > trigger_ref)
    elif rsi_min10 < 35 and rsi_v >= 40 and h_last > 0 and h_prev <= 0:
        setup, trigger_ref = "INVERSIONE", s20
        trigger_reached = bool(s20 is not None and last > s20)
    stop_ref = max(lo20, last - 2 * atr)
    if stop_ref >= last:
        stop_ref = last - 2 * atr
    target_ref = next((x for x in (hi60, hi252) if x > last * 1.02), last + 3 * atr)
    rr_ref = (target_ref - last) / (last - stop_ref) if last > stop_ref else None

    return {
        "ticker": payload["ticker"], "name": payload.get("name"), "currency": payload["currency"],
        "major_currency": payload.get("major_currency"), "calendar": payload.get("calendar"),
        "as_of": bars[-1]["date"], "expected_session": payload.get("last_completed_session_expected"),
        "data_current": payload.get("data_current"), "storico_provvisorio": bool(payload.get("storico_provvisorio")), "data_quality": payload.get("data_quality", {}), "stale_cache": bool(payload.get("stale_cache")),
        "bars_used": n, "source": payload.get("source", "Yahoo_Finance_Storico"), "adjusted": True,
        "history_evidence_policy": payload.get("history_evidence_policy"),
        "last_close": _rnd(last), "ret_1d_pct": _pct(_ret_k(c, 1)), "ret_5d_pct": _pct(_ret_k(c, 5)),
        "ret_20d_pct": _pct(ret20), "ret_60d_pct": _pct(_ret_k(c, 60)), "ret_250d_pct": _pct(_ret_k(c, 250)),
        "sma20": _rnd(s20), "sma50": _rnd(s50), "sma200": _rnd(s200),
        "dist_sma50_pct": _pct(last / s50 - 1) if s50 else None,
        "dist_sma200_pct": _pct(last / s200 - 1) if s200 else None,
        "trend": trend, "rsi14": _rnd(rsi_v, 1), "macd_hist": _rnd(h_last),
        "atr14": _rnd(atr), "atr_pct": _pct(atr / last), "vol_ann_20d_pct": _pct(vol20),
        "rvol_giornaliero": _rnd(rvol, 2), "updown_vol_ratio_20d": _rnd(updown, 2),
        "turnover_20d_major": _rnd(turnover, 0),
        "support_20d": _rnd(lo20), "resistance_20d_prior": _rnd(hi20_prior), "resistance_60d": _rnd(hi60),
        "high_52w": _rnd(hi252) if n>=252 else None, "low_52w": _rnd(lo252) if n>=252 else None, "dist_high_52w_pct": _pct(last / hi252 - 1) if n>=252 else None,
        "benchmark": bench_ticker, "rs_20d_vs_benchmark_pct": _pct(rs20), "benchmark_note": bench_note,
        "setup": setup, "trigger_ref": _rnd(trigger_ref), "trigger_reached": trigger_reached,
        "extended": extended, "volume_confirmed": volume_confirmed,
        "stop_ref": _rnd(stop_ref), "target_ref": _rnd(target_ref), "rr_ref": _rnd(rr_ref, 2),
    }


def _benchmark_for(ticker):
    return BENCHMARKS["US"] if _calendar_code(ticker) == "XNYS" else BENCHMARKS["EU"]


def get_indicators(ticker):
    ticker = _validate_ticker(ticker)
    _register_extra([ticker])
    payload = _load_series(ticker)
    if payload.get("screening_history_accepted") is False:
        raise ValueError("BLOCCATO_PER_DATI: ultima seduta oltre tolleranza, calendario non valido o segmento consecutivo inferiore a60 barre")
    bench_t = _benchmark_for(ticker)
    try:
        bench = _load_series(bench_t)
    except ValueError:
        bench = None
    result = _indicators_from_payload(payload, bench, bench_t)
    result.update(server_version=VERSION, generated_at_utc=_utc_stamp(),
                  warnings=payload.get("warnings", []),
                  note="Screening su sedute concluse. stop_ref/target_ref sono riferimenti tecnici grezzi, "
                       "non SL/TP del ticket. Prezzo, spread e negoziabilita' vanno verificati su eToro.")
    return result


# -----------------------------------------------------------------------------
# Scanner
# -----------------------------------------------------------------------------
SETUP_ORDER = {"AVVIO_TREND": 0, "CONTINUAZIONE": 1, "CORREZIONE": 2, "INVERSIONE": 3}
SCAN_FIELDS = ("ticker", "name", "setup", "trigger_reached", "trigger_ref", "last_close", "currency",
               "as_of", "data_current", "storico_provvisorio", "data_quality", "trend", "rsi14", "ret_20d_pct", "rs_20d_vs_benchmark_pct", "rvol_giornaliero",
               "volume_confirmed", "atr_pct", "stop_ref", "target_ref", "rr_ref", "dist_high_52w_pct", "extended")


def _rank_key(x):
    rs = x["rs_20d_vs_benchmark_pct"]
    return (not x["trigger_reached"], -(x["rr_ref"] or 0), -(rs if rs is not None else -1e9), x["ticker"])


def scan_market(market="US", tickers=None, max_per_setup=4):
    market = str(market or "").strip().upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US oppure EU")
    try:
        max_per_setup = max(1, min(int(max_per_setup), 10))
    except (TypeError, ValueError):
        max_per_setup = 4
    if tickers:
        universe = list(dict.fromkeys(_validate_ticker(t) for t in tickers))
        _register_extra(universe)
    else:
        universe = list(dict.fromkeys(filtered_level_a(market)))
    bench_t = BENCHMARKS[market]
    try:
        bench = _load_series(bench_t)
    except ValueError:
        bench = None

    t0 = time.time()
    fetched, blocked_429, stale_seen, evaluated, no_setup = 0, False, 0, 0, 0
    fresh_acquired = 0
    provisional_acquired = 0
    excluded = {"errore_dati": [], "dati_non_aggiornati": [], "storico_insufficiente": [],
                "liquidita_insufficiente": []}
    pending, cands = [], []

    for t in universe:
        p = _load_series(t, allow_fetch=False)
        if p is None:
            can_fetch = (not blocked_429 and fetched < SCAN_MAX_FETCH
                         and time.time() - t0 < SCAN_TIME_BUDGET and _yahoo_cooldown_left() == 0)
            if can_fetch:
                try:
                    p = _load_series(t)
                    fetched += 1
                except ValueError as exc:
                    msg = str(exc)
                    if "429" in msg or "pausa" in msg:
                        blocked_429 = True
                        pending.append(t)
                    else:
                        excluded["errore_dati"].append(t)
                    continue
            else:
                # Una cache vecchia e' utile per diagnostica, ma NON conta come copertura dello scanner.
                p = _load_series(t, allow_fetch=False, stale_ok=True)
                if p is None:
                    pending.append(t)
                    continue
        if p.get("stale_cache"):
            stale_seen += 1
        fresh_acquired += int(p.get("data_current") is True)
        provisional_acquired += int(bool(p.get("storico_provvisorio")))
        if p.get("screening_history_accepted") is False:
            reason = "storico_insufficiente" if p.get("data_current") or p.get("storico_provvisorio") else "dati_non_aggiornati"
            excluded[reason].append(t)
            continue

        try:
            ind = _indicators_from_payload(p, bench, bench_t)
        except ValueError:
            excluded["storico_insufficiente"].append(t)
            continue
        evaluated += 1
        turnover, major = ind["turnover_20d_major"], ind["major_currency"]
        if turnover is not None and major in APPROX_UNITS_PER_EUR:
            if turnover / APPROX_UNITS_PER_EUR[major] < MIN_TURNOVER_EUR[market]:
                excluded["liquidita_insufficiente"].append(t)
                continue
        if not ind["setup"]:
            no_setup += 1
            continue
        cands.append(ind)

    shortlist = []
    for setup in sorted(SETUP_ORDER, key=SETUP_ORDER.get):
        group = sorted([c for c in cands if c["setup"] == setup], key=_rank_key)[:max_per_setup]
        shortlist.extend({k: c[k] for k in SCAN_FIELDS} for c in group)

    n = len(universe)
    acquired_any = n - len(pending)
    excluded_total = sum(len(v) for v in excluded.values())
    liquidity_pass = evaluated - len(excluded["liquidita_insufficiente"])
    gate_counts = {
        "universo": n,
        "dati_acquisiti": acquired_any,
        "dati_correnti": fresh_acquired,
        "dati_non_aggiornati": len(excluded["dati_non_aggiornati"]),
        "dati_mancanti_o_errore": len(pending) + len(excluded["errore_dati"]),
        "storico_valutabile": evaluated,
        "liquidita_pass": liquidity_pass,
        "setup_pass": len(cands),
        "shortlist": len(shortlist),
        "eToro_verifica": None,
        "identita_strumento": None,
        "spread_liquidita_eToro": None,
        "fondamentali_news": None,
        "rischio": None,
        "decisione": None,
    }
    return {
        "server_version": VERSION, "generated_at_utc": _utc_stamp(), "market": market,
        "universe_version": UNIVERSE_VERSION if not tickers else "custom",
        "universe_level_a_status": LEVEL_A_STATUS if not tickers else "custom",
        "benchmark": bench_t, "benchmark_as_of": bench["coverage_end"] if bench else None,
        "universe_size": n,
        "acquired_any": acquired_any,
        "acquired_any_pct": _rnd(acquired_any / n * 100, 1) if n else None,
        "fresh_acquired": fresh_acquired,
        "provisional_acquired": provisional_acquired,
        "screening_accepted": fresh_acquired + provisional_acquired,
        "screening_coverage_pct": _rnd((fresh_acquired + provisional_acquired) / n * 100, 1) if n else None,
        "coverage_pct": _rnd(fresh_acquired / n * 100, 1) if n else None,
        "coverage_definition": "ticker con storico disponibile e aggiornato all'ultima seduta completa attesa",
        "coverage_summary": {
            "status": "COMPLETE" if n and fresh_acquired == n else "PARTIAL",
            "expected_total": n,
            "data_acquired": acquired_any,
            "data_current": fresh_acquired,
            "data_provisional": provisional_acquired,
            "screening_accepted": fresh_acquired + provisional_acquired,
            "data_not_updated": len(excluded["dati_non_aggiornati"]),
            "data_missing_or_error": len(pending) + len(excluded["errore_dati"]),
            "history_evaluable": evaluated,
            "missing_or_error_tickers": (pending + excluded["errore_dati"])[:40],
            "not_updated_tickers": excluded["dati_non_aggiornati"][:40],
        },
        "evaluated": evaluated,
        "evaluated_pct": _rnd(evaluated / n * 100, 1) if n else None,
        "excluded_total": excluded_total, "no_setup": no_setup, "candidates_total": len(cands),
        "excluded_counts": {k: len(v) for k, v in excluded.items()},
        "gate_counts": gate_counts,
        "gate_trace_note": "I gate eToro, identita', spread, fondamentali/news, rischio e decisione sono eseguiti dall'orchestratore dopo questa risposta e devono essere valorizzati separatamente; null non significa PASS.",
        "excluded_errore_dati": excluded["errore_dati"][:20],
        "excluded_dati_non_aggiornati": excluded["dati_non_aggiornati"][:40],
        "pending_not_acquired": pending[:40], "pending_count": len(pending),
        "fetched_now": fetched, "stale_cache_seen": stale_seen, "yahoo_429_blocked": blocked_429,
        "scan_complete": bool(n and fresh_acquired + provisional_acquired == n and not excluded["errore_dati"]
                              and not excluded["storico_insufficiente"]),
        "ranking_rule": "per setup: trigger raggiunto, poi R/R tecnico, poi forza relativa 20 sedute, poi ticker",
        "shortlist": shortlist,
        "note": "coverage_pct misura solo dati correnti: una cache vecchia non viene piu' conteggiata come copertura. "
                "Filtro preliminare su sedute concluse; catalizzatori, spread, negoziabilita' X1 e prezzo "
                "corrente vanno verificati su eToro e sulle fonti prima di qualunque ticket.",
    }


# -----------------------------------------------------------------------------
# Rischio di portafoglio
# -----------------------------------------------------------------------------
def _close_series_usd(payload):
    """Risk history validation is independent of stricter screening completeness.

    Missing internal bars remain absent; returns are later formed on calendar pairs.
    """
    quality = payload.get("data_quality", {})
    if not quality.get("calendar_validated") or not payload.get("calendar"):
        raise ValueError("Calendario storico non verificato")
    if payload.get("adjusted") is not True:
        raise ValueError("Rettifiche storiche non verificate")
    gap = _session_gap(payload)
    provisional = bool(_verified_etf(payload) and payload.get("storico_provvisorio") and gap == 1)
    if payload.get("data_current") is not True and not provisional:
        raise ValueError("Storico oltre tolleranza")
    hist = payload["history"]
    dates = [b["date"] for b in hist]
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise ValueError("Date storiche duplicate o non ordinate")
    values = [finite(b["close"]) for b in hist]
    if not values or min(values) <= 0:
        raise ValueError("Prezzi storici mancanti/non positivi")
    scale = finite(payload.get("price_scale_to_major_currency", 1.0))
    if scale <= 0:raise ValueError("Scala valuta storica non valida")
    s = pd.Series(values, index=dates, dtype=float) * scale
    code = payload["calendar"]
    cal = _get_cal(code)
    expected = [x.strftime("%Y-%m-%d") for x in cal.sessions_in_range(pd.Timestamp(dates[0]), pd.Timestamp(dates[-1]))]
    if not expected or any(d not in set(expected) for d in dates):
        raise ValueError("Barre fuori calendario")
    major = payload.get("major_currency") or payload.get("currency")
    fx_source = None
    fx_excluded = []
    if major != "USD":
        # Same methodology as the skill: ECB reference dates, no forward fill.
        try:
            fx = acquire_ecb_history(os.environ.get("ECB_FX_CACHE_DIR", "/tmp/yfs-ecb-fx"), dates[0], max(dates[-1], datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()))
        except Exception:
            raise ValueError("Acquisizione storico FX BCE non disponibile") from None
        if fx.get("status") != "PREPARATO_DA_VALIDARE_NEL_MOTORE":
            raise ValueError("Storico FX BCE non validato")
        rates = fx.get("fx_to_usd_by_date", {})
        converted = {}
        for d, value in s.items():
            rate = rates.get(d, {}).get(major)
            if rate is None:
                if not target_open(date.fromisoformat(d)):
                    fx_excluded.append(d)
                    continue
                raise ValueError("Tasso BCE mancante: " + major + " " + d)
            rate = finite(rate)
            if rate <= 0:raise ValueError("Tasso BCE non positivo")
            converted[d] = value * rate
        s = pd.Series(converted, dtype=float)
        fx_source = "ECB eurofxref: " + major + "USD"
    s.attrs.update(calendar=code, expected_sessions=expected,
                   fx_excluded_dates=fx_excluded, source=payload.get("source"),
                   adjustments_reference=payload.get("adjustment_method"))
    return s, fx_source


def _risk_returns(series):
    """Use (start,end) keys so different holiday calendars cannot misalign returns."""
    sessions = series.attrs.get("expected_sessions")
    if not sessions:
        raise ValueError("Calendario rendimenti mancante")
    if len(sessions) < 2 or sessions[-1] not in series.index or sessions[-2] not in series.index:
        raise ValueError("Ultimo rendimento non disponibile su sedute consecutive")
    pairs, values = [], []
    for a, b in zip(sessions, sessions[1:]):
        if a in series.index and b in series.index:
            value = float(series.loc[b] / series.loc[a] - 1.0)
            if not math.isfinite(value):raise ValueError("Rendimento non finito")
            pairs.append((a, b))
            values.append(value)
    return pd.Series(values, index=pd.MultiIndex.from_tuples(pairs, names=["start", "end"]), dtype=float)


def _risk_sample(prices, tickers):
    maps = {t: _risk_returns(prices[t]) for t in tickers}
    return pd.concat(maps, axis=1, join="inner").sort_index().dropna().tail(500)


def _load_risk_series(ticker):
    if ticker not in ETORO_ETF_MAP or not etoro_history.risk_enabled():
        return _load_series(ticker, prefer_etoro=False)
    return _load_series(ticker)

def _var_es(R, w, equity, eurusd=None):
    out = {}
    r1 = R.values @ w
    weekly = []
    pairs = list(R.index)
    if not isinstance(R.index, pd.MultiIndex):
        raise ValueError("Intervalli di calendario mancanti per VaR settimanale")
    for i in range(4, len(R)):
        window = pairs[i-4:i+1]
        if all(a[1] == b[0] for a, b in zip(window, window[1:])):
            weekly.append((np.prod(1.0 + R.iloc[i-4:i+1].values, axis=0) - 1.0) @ w)
    r5 = np.array(weekly, dtype=float)
    for label, r in (("1d", r1), ("5d", r5)):
        losses = -r
        if len(losses) < 20:
            continue
        for q in (0.95, 0.99):
            var = float(np.quantile(losses, q))
            tail = losses[losses >= var]
            es = float(tail.mean()) if len(tail) else var
            tag = int(q * 100)
            var_usd = var * equity
            es_usd = es * equity
            out[f"var{tag}_{label}_pct"] = _pct(var)
            out[f"var{tag}_{label}_usd"] = _rnd(var_usd, 2)
            out[f"es{tag}_{label}_pct"] = _pct(es)
            out[f"es{tag}_{label}_usd"] = _rnd(es_usd, 2)
            if eurusd is not None and eurusd > 0:
                out[f"var{tag}_{label}_eur"] = _rnd(var_usd / eurusd, 2)
                out[f"es{tag}_{label}_eur"] = _rnd(es_usd / eurusd, 2)
    out["n_1d"] = int(len(r1))
    out["n_5d"] = int(len(r5))
    return out


def _corr_info(R, window):
    sub = R.tail(window)
    if len(sub) < 60 or sub.shape[1] < 2:
        return {"window": window, "obs": int(len(sub)), "max_abs": None, "pairs_over_070": []}
    c = sub.corr()
    cols = list(c.columns)
    pairs, mx = [], 0.0
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            val = c.iat[i, j]
            if pd.notna(val):
                mx = max(mx, abs(float(val)))
                if abs(val) > T["correlation_abs_alert"]:
                    pairs.append({"a": cols[i], "b": cols[j], "corr": _rnd(val, 2)})
    pairs.sort(key=lambda p: -abs(p["corr"]))
    return {"window": window, "obs": int(len(sub)), "max_abs": _rnd(mx, 2), "pairs_over_070": pairs[:10]}


def portfolio_risk(equity_usd, cash_usd, positions, copy_value_usd=0.0, candidate=None, eurusd=None,
                   frozen_cash_usd=0.0, pending_orders=None):
    equity = finite(equity_usd)
    cash = finite(cash_usd)
    copyv = finite(copy_value_usd or 0)
    frozen = finite(frozen_cash_usd or 0)
    if frozen < 0:
        raise ValueError("frozen_cash_usd non puo' essere negativo")
    if equity <= 0:
        raise ValueError("equity_usd deve essere positiva")
    if not isinstance(positions, list) or not positions:
        raise ValueError("positions deve essere un elenco non vuoto")
    agg = {}
    for p in positions:
        t = _validate_ticker(p["ticker"])
        a = agg.setdefault(t, {"value": 0.0, "is_etf": False})
        a["value"] += finite(p["value_usd"])
        a["is_etf"] = a["is_etf"] or str(p.get("type", "")).upper() == "ETF"
    cand = None
    if candidate:
        cand = {"ticker": _validate_ticker(candidate["ticker"]), "amount": finite(candidate["amount_usd"]),
                "is_etf": str(candidate.get("type", "")).upper() == "ETF"}
    _register_extra(list(agg) + ([cand["ticker"]] if cand else []))
    input_hash = hashlib.sha256(_json_text({"e": equity, "c": cash, "frozen": frozen, "copy": copyv,
                                            "p": sorted((t, a["value"]) for t, a in agg.items()),
                                            "cand": cand, "eurusd": eurusd,
                                            "pending_orders": pending_orders or []}).encode()).hexdigest()

    issues, alerts, critical, notes = [], [], [], []
    fx_eur = None
    try:
        fx_eur = finite(eurusd) if eurusd is not None else None
        if fx_eur is None or fx_eur <= 0:
            raise ValueError("EURUSD corrente mancante o non valido")
    except (ValueError, TypeError, KeyError):
        fx_eur = None
        issues.append("EURUSD corrente verificato mancante: conversione rischio in EUR non disponibile")
    total = sum(a["value"] for a in agg.values()) + cash + frozen + copyv
    diff = equity - total
    tol = max((fx_eur or 0.0) * T["reconciliation_floor_eur"], T["reconciliation_equity_fraction"] * equity)
    recon_ok = abs(diff) <= tol
    if not recon_ok:
        issues.append(f"Riconciliazione fallita: scarto {diff:.2f} USD oltre tolleranza {tol:.2f} USD")

    prices, fx_used, missing, stale, ends = {}, {}, [], [], []
    history_sources, history_quality = {}, {}
    provisional_tickers = []
    for t in list(agg) + ([cand["ticker"]] if cand else []):
        if t in prices:
            continue
        try:
            pl = _load_risk_series(t)
            s, fxt = _close_series_usd(pl)
            _risk_returns(s)
            prices[t] = s
            history_sources[t] = {"source":pl.get("source"), "adjustment_reference":pl.get("adjustment_method")}
            history_quality[t] = {"missing_sessions":[d for d in s.attrs["expected_sessions"] if d not in s.index],
                                  "fx_nonpublication_exclusions":s.attrs["fx_excluded_dates"],
                                  "valid_adjacent_returns":len(_risk_returns(s))}
            if fxt:
                fx_used[t] = fxt
            if pl.get("storico_provvisorio"):
                provisional_tickers.append(t)
                alerts.append(t + ": storico provvisorio; aggiornare storico prima del ticket")
            elif pl.get("data_current") is False:
                stale.append(t)
            ends.append(pl["coverage_end"])
        except ValueError as exc:
            missing.append({"ticker": t, "reason": str(exc)[:120]})
    port_missing = [m for m in missing if m["ticker"] in agg]
    if port_missing:
        issues.append("Serie mancanti: " + ", ".join(m["ticker"] for m in port_missing))
    stale_port = [t for t in stale if t in agg]
    if stale_port:
        issues.append("Serie non aggiornate all'ultima seduta conclusa: " + ", ".join(stale_port))

    port_t = [t for t in agg if t in prices]
    result_var, corr120, corr60, sample = None, None, None, {}
    weights = [{"ticker": t, "weight_pct": _pct(agg[t]["value"] / equity), "is_etf": agg[t]["is_etf"]}
               for t in agg]
    if port_t:
        R = _risk_sample(prices, port_t)
        n = len(R)
        sample = {"returns_common": int(n),
                  "start": R.index[0][0] if n else None, "end": R.index[-1][1] if n else None,
                  "no_bridge_returns": True, "alignment": "identical_start_end_session_pairs"}
        if n < T["history_min_returns"]:
            issues.append(f"Campione comune insufficiente: {n} rendimenti (minimo 200, ordinario 250)")
        elif n < T["history_ordinary_returns"]:
            alerts.append(f"Campione provvisorio: {n} rendimenti (200-249)")
        if n >= 30:
            w = np.array([agg[t]["value"] / equity for t in port_t])
            result_var = _var_es(R, w, equity, fx_eur)
            if result_var.get("n_5d", 0) < 20:
                issues.append("Campione settimanale insufficiente: meno di 20 finestre contigue")
            v1, v5 = result_var.get("var99_1d_pct"), result_var.get("var99_5d_pct")
            if v1 is not None and v5 is not None:
                if v1 > T["var99_daily_critical_pct"] or v5 > T["var99_weekly_critical_pct"]:
                    critical.append(f"VaR99 critico (1g {v1}%, 5g {v5}%): blocco nuovi acquisti")
                elif v1 > T["var99_daily_alert_pct"] or v5 > T["var99_weekly_alert_pct"]:
                    alerts.append(f"SEGNALAZIONE RISCHIO: VaR99 1g {v1}% / 5g {v5}%")
            corr120 = _corr_info(R, 120)
            corr60 = _corr_info(R, 60)
            if corr120["pairs_over_070"]:
                alerts.append("ALERT CORRELAZIONE su 120 sedute")

    cash_pct = cash / equity
    if cash_pct < T["cash_min_pct"] / 100 or cash_pct > T["cash_max_pct"] / 100:
        alerts.append(f"Liquidita' {cash_pct * 100:.2f}% fuori fascia 15-30%")
    for t, a in agg.items():
        wt = a["value"] / equity
        if not a["is_etf"] and wt > T["issuer_alert_pct"] / 100:
            alerts.append(f"Emittente {t} al {wt * 100:.1f}%" + (" (oltre 25%: no ulteriori acquisti)" if wt > T["issuer_purchase_limit_pct"] / 100 else ""))

    cand_out = None
    if cand:
        ct = cand["ticker"]
        cand_out = {"ticker": ct, "amount_usd": cand["amount"], "esito": None, "motivi": []}
        if provisional_tickers:
            cand_out.update(esito="BLOCCATO PER DATI", motivi=["aggiornare storico prima del ticket"])
        elif ct not in prices:
            cand_out.update(esito="BLOCCATO PER DATI", motivi=["Serie del candidato non disponibile"])
        elif cand["amount"] > cash:
            cand_out.update(esito="BLOCCATO PER DATI", motivi=["Importo superiore alla liquidita' dichiarata"])
        else:
            cols = port_t + ([ct] if ct not in port_t else [])
            Rc = _risk_sample(prices, cols)
            new_val = {t: agg[t]["value"] for t in port_t}
            new_val[ct] = new_val.get(ct, 0.0) + cand["amount"]
            wc = np.array([new_val[t] / equity for t in cols])
            after = _var_es(Rc, wc, equity, fx_eur) if len(Rc) >= 30 else None
            corr_c = []
            sub = Rc.tail(120)
            if len(sub) >= 60:
                cc = sub.corr()[ct]
                for t in port_t:
                    if t != ct and pd.notna(cc[t]):
                        corr_c.append({"ticker": t, "corr_120": _rnd(cc[t], 2)})
            high = [x["ticker"] for x in corr_c if x["corr_120"] is not None and x["corr_120"] > T["correlation_positive_critical"]]
            group_w = sum(new_val[t] for t in high) / equity + new_val[ct] / equity
            motivi = []
            esito = "PASS"
            if len(Rc) < T["history_min_returns"]:
                esito = "BLOCCATO PER DATI"
                motivi.append(f"Campione comune con il candidato: {len(Rc)} rendimenti")
            if high and group_w > T["correlated_group_purchase_limit_pct"] / 100:
                esito = "BLOCCATO PER RISCHIO"
                motivi.append(f"Correlazione >0,80 con {', '.join(high)} e gruppo al {group_w * 100:.1f}%")
            if after and after.get("n_5d", 0) < 20:
                esito = "BLOCCATO PER DATI"
                motivi.append("Campione candidato settimanale insufficiente: meno di 20 finestre contigue")
            if after:
                v1, v5 = after.get("var99_1d_pct"), after.get("var99_5d_pct")
                if v1 is not None and v5 is not None and (v1 > T["var99_daily_critical_pct"] or v5 > T["var99_weekly_critical_pct"]):
                    esito = "BLOCCATO PER RISCHIO"
                    motivi.append(f"VaR99 dopo l'acquisto critico (1g {v1}%, 5g {v5}%)")
                elif v1 is not None and v5 is not None and (v1 > T["var99_daily_alert_pct"] or v5 > T["var99_weekly_alert_pct"]) and esito == "PASS":
                    esito = "REVISIONE RICHIESTA"
                    motivi.append(f"VaR99 dopo l'acquisto in alert (1g {v1}%, 5g {v5}%)")
            wct = new_val[ct] / equity
            if not cand["is_etf"] and wct > T["issuer_purchase_limit_pct"] / 100:
                esito = "BLOCCATO PER RISCHIO"
                motivi.append(f"Emittente {ct} oltre 25% dopo l'acquisto")
            if (cash - cand["amount"]) / equity < T["cash_min_pct"] / 100 and esito == "PASS":
                esito = "REVISIONE RICHIESTA"
                motivi.append("Liquidita' dopo l'acquisto sotto il 15%")
            cand_out.update(esito=esito, motivi=motivi, var_es_after=after, corr_with_positions=corr_c,
                            weight_after_pct=_pct(wct), cash_after_pct=_pct((cash - cand["amount"]) / equity),
                            correlated_group_after_pct=_pct(group_w), sample_returns=int(len(Rc)))
        if issues:
            cand_out["esito"] = "BLOCCATO PER DATI"
            cand_out["motivi"] = ["Rischio di portafoglio bloccato per dati"] + cand_out["motivi"]
        elif critical and cand_out["esito"] in ("PASS", "REVISIONE RICHIESTA"):
            cand_out["esito"] = "BLOCCATO PER RISCHIO"

    if issues:
        esito = "BLOCCATO PER DATI"
    elif critical:
        esito = "BLOCCATO PER RISCHIO"
    elif alerts:
        esito = "REVISIONE RICHIESTA"
    else:
        esito = "PASS"
    analytic = sum(agg[t]["value"] for t in port_t) / equity
    notes.append("VaR/ES riferiti alla parte analiticamente coperta. Liquidita' e COPY entrano nel "
                 "denominatore a rendimento zero: le COPY sono escluse dai rendimenti come da Mandato, "
                 "quindi il VaR non rappresenta il rischio dell'intero portafoglio.")
    notes.append("Simulazione storica su rendimenti rettificati fra coppie identiche di sedute consecutive; "
                 "nessun riempimento o rendimento a ponte. Finestre 5g solo su cinque intervalli contigui. "
                 "Conversioni storiche non USD con tassi di riferimento BCE, asincroni alle chiusure azionarie. "
                 "Non e' una perdita massima garantita.")
    return {
        "server_version": VERSION, "engine": RISK_ENGINE_VERSION, "generated_at_utc": _utc_stamp(),
        "storico_provvisorio": bool(provisional_tickers),
        "provisional_tickers": provisional_tickers,
        "ticket_data_current": not bool(provisional_tickers or stale or missing),
        "next_action": "aggiornare storico prima del ticket" if provisional_tickers else None,
        "input_hash": input_hash, "esito_rischio": esito, "motivi": issues + critical + alerts,
        "reconciliation": {"equity_usd": _rnd(equity, 2), "sum_components_usd": _rnd(total, 2),
                           "diff_usd": _rnd(diff, 2), "tolerance_usd": _rnd(tol, 2), "ok": recon_ok},
        "allocation": {"cash_pct": _pct(cash_pct), "frozen_cash_pct": _pct(frozen / equity),
                       "available_cash_usd": _rnd(cash), "frozen_cash_usd": _rnd(frozen),
                       "pending_orders_count": len(pending_orders or []),
                       "invested_pct": _pct(1 - cash_pct - frozen / equity),
                       "copy_pct": _pct(copyv / equity), "band_cash": "15-30%"},
        "coverage": {"analytic_pct": _pct(analytic), "copy_excluded_pct": _pct(copyv / equity),
                     "missing": missing, "stale": stale, "fx_used": fx_used,
                     "history_sources": history_sources, "history_quality": history_quality,
                     "eurusd_current": _rnd(fx_eur, 6) if fx_eur is not None else None},
        "sample": sample, "var_es": result_var,
        "correlation_120": corr120, "correlation_60": corr60,
        "weights": weights, "candidate": cand_out, "notes": notes,
    }


# -----------------------------------------------------------------------------
# ETF look-through
# -----------------------------------------------------------------------------
def _number(value):
    text = html.unescape(str(value or "")).replace("\xa0", " ").strip().replace("%", "").replace(" ", "")
    if not text or text in {"-", "—", "N/A", "n/a"}:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
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
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"]),
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
    as_of = _parse_iso_date(payload["as_of"], "as_of")
    age_days = (_utc_now().date() - as_of).days
    if age_days < -1:
        raise ValueError("Data holdings futura")
    if age_days > MAX_HOLDINGS_AGE_DAYS:
        raise ValueError(f"Holdings obsolete: {age_days} giorni")
    if not 95 <= payload["weight_total_pct"] <= 105:
        raise ValueError(f"Somma pesi non valida: {payload['weight_total_pct']:.4f}%")
    payload["age_days"] = age_days
    json.dumps(payload, allow_nan=False)
    return payload


def _aggregate_exposure(holdings, field):
    totals = {}
    for item in holdings:
        label = item.get(field) or "Unclassified"
        totals[label] = totals.get(label, 0.0) + item["weight_pct"]
    return [{"name": k, "weight_pct": _rnd(v, 3)} for k, v in sorted(totals.items(), key=lambda p: (-p[1], p[0]))]


def _ishares_url(product_id):
    params = {"appSubType": "ISHARES", "appType": "PRODUCT_PAGE", "component": "holdings.all",
              "locale": "en_GB", "portfolioId": product_id, "targetSite": "ishares-uk",
              "userType": "individual", "excludeContent": "true", "asOfDate": "", "includeConfig": "true"}
    return ISHARES_API + "?" + urlencode(params)


def _parse_ishares_api(body, config, ticker):
    try:
        document = json.loads(body)
        if str(document["productId"]) != config["product_id"]:
            raise ValueError("ID prodotto iShares non corrispondente")
        points = document["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
        as_of = datetime.strptime(str(points["asOfDate"]["value"]), "%Y%m%d").date()
        fields = {"ticker": "ticker", "name": "issueName", "isin": "isin", "sector": "sectorName",
                  "asset_class": "assetClass", "country": "countryOfRisk",
                  "market_currency": "marketCurrencyCode", "weight_pct": "holdingPercent"}
        columns = {f: points[k]["value"] for f, k in fields.items()}
        count = len(columns["weight_pct"])
        if count < 10 or any(not isinstance(v, list) or len(v) != count for v in columns.values()):
            raise ValueError("Colonne holdings incomplete o disallineate")
        holdings = []
        for i in range(count):
            item = {k: vals[i] for k, vals in columns.items()}
            item["weight_pct"] = finite(item["weight_pct"])
            if not isinstance(item["name"], str) or not item["name"].strip():
                raise ValueError("Holdings iShares con nome non valido")
            holdings.append(item)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError("Risposta holdings iShares non valida o incompleta") from exc
    total = sum(h["weight_pct"] for h in holdings)
    payload = {"server_version": VERSION, "source": "Official_ETF_Issuer", "provider": config["provider"],
               "ticker": ticker, "canonical_ticker": ticker, "fund_name": config["fund_name"],
               "isin": config["isin"], "as_of": as_of.isoformat(), "generated_at_utc": _utc_stamp(),
               "source_url": config["source_url"], "source_hash": hashlib.sha256(body).hexdigest(),
               "holdings_detail": "complete", "proposal_usable": True, "holdings_count": len(holdings),
               "weight_total_pct": total, "issuer_coverage_pct": total, "holdings": holdings,
               "sector_exposure": _aggregate_exposure(holdings, "sector"),
               "country_exposure": _aggregate_exposure(holdings, "country"),
               "data_quality": {"complete_holdings": True, "official_source": True, "warnings": []}}
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
            cells = [_html_text(cell) for cell in
                     re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)]
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
    required = {n.lower() for n in required_names}
    return next((rows for rows in candidates if any(i["name"].lower() in required for i in rows)), [])


def _parse_wisdomtree_holdings(body, config, ticker):
    document = body.decode("utf-8", errors="replace")
    visible = _html_text(document)
    m = re.search(r"(?:As of|Au|Stand|Al)\s+([0-9A-Za-zÀ-ÿ,./ -]{6,24})", visible, flags=re.I)
    as_of = _date_from_text(m.group(1) if m else visible)
    if not as_of:
        raise ValueError("Data holdings WisdomTree mancante")
    candidates = [r for r in (_weighted_rows(t) for t in _table_rows(document)) if r]
    if not candidates:
        raise ValueError("Tabelle holdings WisdomTree non trovate (pagina caricata via JavaScript?)")
    remaining_labels = {"remaining portfolio", "portefeuille restant", "restliches portfolio", "portafoglio rimanente"}
    holdings_rows = _select_weighted_table(candidates, remaining_labels | {
        "bae systems", "thales", "rheinmetall", "leonardo"})
    sector_rows = _select_weighted_table(candidates, {"industrials", "industrie"})
    country_rows = _select_weighted_table(candidates, {"france", "germany", "united kingdom", "italy", "sweden"})
    if holdings_rows in (sector_rows, country_rows):
        holdings_rows = []
    if not 95 <= sum(x["weight_pct"] for x in sector_rows) <= 105:
        sector_rows = []
    if not 95 <= sum(x["weight_pct"] for x in country_rows) <= 105:
        country_rows = []
    remaining = next((x["weight_pct"] for x in holdings_rows if x["name"].lower() in remaining_labels), 0.0)
    holdings = [{"ticker": None, "name": x["name"], "isin": None, "sector": None, "asset_class": "Equity",
                 "country": None, "market_currency": None, "weight_pct": x["weight_pct"]}
                for x in holdings_rows if x["name"].lower() not in remaining_labels]
    if len(holdings) < 10 or not sector_rows or not country_rows:
        raise ValueError("Look-through WisdomTree insufficiente")
    total = sum(x["weight_pct"] for x in holdings) + remaining
    warnings = [f"Dettaglio emittenti parziale: portafoglio restante {remaining:.2f}%"] if remaining else []
    payload = {"server_version": VERSION, "source": "Official_ETF_Issuer", "provider": config["provider"],
               "ticker": ticker, "canonical_ticker": ticker, "fund_name": config["fund_name"],
               "isin": config["isin"], "as_of": as_of.isoformat(), "generated_at_utc": _utc_stamp(),
               "source_url": config["source_url"], "source_hash": hashlib.sha256(body).hexdigest(),
               "holdings_detail": "top_holdings_plus_remainder" if remaining else "complete",
               "proposal_usable": True, "holdings_count": len(holdings), "weight_total_pct": total,
               "issuer_coverage_pct": sum(x["weight_pct"] for x in holdings),
               "remaining_portfolio_pct": remaining, "holdings": holdings,
               "sector_exposure": sector_rows, "country_exposure": country_rows,
               "data_quality": {"complete_holdings": not bool(remaining), "official_source": True,
                                "warnings": warnings}}
    return _validate_holdings(payload)


def get_etf_lookthrough(ticker, top=25):
    requested = _validate_ticker(ticker)
    canonical = ETF_ALIASES.get(requested, requested)
    config = ETF_PRODUCTS.get(canonical)
    if not config:
        raise ValueError("ETF non supportato: disponibili " + ", ".join(ETF_PRODUCTS))
    key = f"etf:{canonical}"
    entry = _cache_get(key)
    if entry and time.time() - entry["ts"] < ETF_CACHE_TTL:
        payload = dict(_cache_data(entry), cache_hit=True)
    else:
        if config["provider"] == "iShares":
            url = _ishares_url(config["product_id"])
            body, _ctype, _final = _http_get(url)
            payload = _parse_ishares_api(body, config, canonical)
        else:
            urls = config.get("download_urls") or [config.get("download_url") or config["source_url"]]
            errors = []
            payload = None
            for url in urls:
                try:
                    body, _ctype, _final = _http_get(url)
                    payload = _parse_wisdomtree_holdings(body, dict(config, source_url=_final), canonical)
                    break
                except Exception as exc:
                    errors.append(f"{url}: {str(exc)[:100]}")
            if payload is None:
                # Il look-through e' un arricchimento opzionale: la sua indisponibilita'
                # non deve bloccare scanner o valutazione tecnica dell'ETF, che restano
                # basati sullo storico/prezzo Yahoo del ticker ETF.
                return {
                    "server_version": VERSION,
                    "source": "Official_ETF_Issuer",
                    "provider": config["provider"],
                    "ticker": requested,
                    "canonical_ticker": canonical,
                    "fund_name": config["fund_name"],
                    "isin": config["isin"],
                    "generated_at_utc": _utc_stamp(),
                    "lookthrough_status": "NON_DISPONIBILE_NON_BLOCCANTE",
                    "proposal_usable": True,
                    "technical_analysis_usable": True,
                    "technical_analysis_source": "Yahoo_Finance_Storico",
                    "lookthrough_required": False,
                    "reason": "Fonte ufficiale holdings non raggiungibile/leggibile dal server",
                    "detail": " | ".join(errors)[:1000],
                    "note": "Valutare l'ETF direttamente per trend, momentum, volumi, volatilita', forza relativa e setup. Holdings/settori/paesi sono solo arricchimento opzionale."
                }
        _cache_put(key, {"ts": time.time(), "data": payload})
        payload = dict(payload, cache_hit=False)
    try:
        top = max(5, min(int(top), 200))
    except (TypeError, ValueError):
        top = 25
    hold = sorted(payload["holdings"], key=lambda h: -h["weight_pct"])[:top]
    out = {k: v for k, v in payload.items() if k != "holdings"}
    out.update(ticker=requested, canonical_ticker=canonical,
               weight_total_pct=_rnd(payload["weight_total_pct"], 3),
               issuer_coverage_pct=_rnd(payload["issuer_coverage_pct"], 3),
               holdings_returned=len(hold),
               top_holdings=[{"ticker": h.get("ticker"), "name": h.get("name"), "sector": h.get("sector"),
                              "country": h.get("country"), "weight_pct": _rnd(h["weight_pct"], 3)}
                             for h in hold])
    return out


# -----------------------------------------------------------------------------
# Preriscaldamento: thread interno + /warm come backup esterno.
# VERSION e WARMER_ENABLED sono definiti una sola volta nei parametri iniziali.
# Il thread usa _warm_needed, quindi a cache completa non riscarica inutilmente.
# -----------------------------------------------------------------------------
WARM_BATCH_MAX = 12
WARM_TIME_BUDGET = 15
_WARM_LOCK = threading.Lock()
_WARM_RUN_LOCK = threading.Lock()
_WS.update(heartbeat_utc=None, stage="idle", current_ticker=None, last_batch=None, batches_today=0, warm_cursor=0)


def _warm_needed(t):
    entry = _cache_get(_series_key(t, DEFAULT_PERIOD, "1d", None, None))
    if entry is None:
        return True
    age = time.time() - entry["ts"]
    code = _calendar_code(t)
    if code is None:
        return age >= NO_CAL_TTL
    try:
        expected, _ = _last_completed(code)
    except Exception:
        return age >= NO_CAL_TTL
    if entry["expected"] != expected:
        return True
    if not entry["after_close"]:
        return _close_settled(code) and age >= MIN_REFETCH_S
    if entry.get("coverage_end") != expected:
        return age >= MIN_REFETCH_S
    return bool(_entry_has_internal_gaps(entry) and age >= LAG_REFETCH_S)


def _series_ready(t):
    """True solo se la cache copre davvero l'ultima seduta conclusa attesa.

    Il backoff del warmer decide quando ritentare un download; non deve mai far
    apparire pronta una serie ancora ferma alla seduta precedente.
    """
    entry = _cache_get(_series_key(t, DEFAULT_PERIOD, "1d", None, None))
    if entry is None:
        return False
    code = _calendar_code(t)
    if code is None:
        return time.time() - entry["ts"] < NO_CAL_TTL
    try:
        expected, _ = _last_completed(code)
    except Exception:
        return time.time() - entry["ts"] < NO_CAL_TTL
    return bool(entry.get("expected") == expected and entry.get("coverage_end") == expected)


def _readiness_reason(t):
    entry = _cache_get(_series_key(t, DEFAULT_PERIOD, "1d", None, None))
    if entry is None:
        return "cache_missing"
    code = _calendar_code(t)
    if code is None:
        return "ready" if time.time() - entry["ts"] < NO_CAL_TTL else "ttl_expired"
    try:
        expected, _ = _last_completed(code)
    except Exception:
        return "calendar_unavailable"
    if entry.get("expected") != expected:
        return "expected_session_changed"
    if entry.get("coverage_end") != expected:
        return "yahoo_coverage_lag"
    return "ready"


def _warm_list():
    with _EXTRA_LOCK:
        extra = sorted(_EXTRA)
    seq = PORTFOLIO_WATCH + list(BENCHMARKS.values()) + FX_PAIRS + extra + filtered_level_a("EU") + filtered_level_a("US")
    return list(dict.fromkeys(seq))


def _rss_mb():
    """RSS corrente del processo su Linux/Render, senza dipendenze esterne."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(float(line.split()[1]) / 1024.0, 1)
    except Exception:
        return None
    return None


def _trim_aux_caches():
    """Libera cache sacrificabili prima di avvicinarsi al limite Render."""
    with INTRADAY_RVOL_LOCK:
        INTRADAY_RVOL_CACHE.clear()
    with SECTOR_CACHE_LOCK:
        SECTOR_CACHE.clear()
    gc.collect()


def _memory_ok_for_warm():
    rss = _rss_mb()
    if rss is None or rss < MEMORY_SOFT_LIMIT_MB:
        return True, rss
    _trim_aux_caches()
    rss = _rss_mb()
    return bool(rss is None or rss < MEMORY_SOFT_LIMIT_MB), rss


def warm_batch(max_items=WARM_BATCH_MAX, budget=WARM_TIME_BUDGET, market=None):
    if not _WARM_RUN_LOCK.acquire(blocking=False):
        return {"server_version": VERSION, "status": "busy",
                "note": "Un lotto e' gia' in corso", "stage": _WS["stage"],
                "current_ticker": _WS["current_ticker"]}
    try:
        max_items = max(1, min(int(max_items), 30))
        budget = max(5, min(int(budget), 25))
    except (TypeError, ValueError):
        max_items, budget = WARM_BATCH_MAX, WARM_TIME_BUDGET
    t0 = time.time()
    done, current, lagging, failed, failed_details, checked, skipped_fail = [], [], [], [], [], 0, 0
    stop_reason = "completato"
    try:
        today = datetime.now(ROME).date().isoformat()
        if _WS["day"] != today:
            _WS.update(day=today, fetched_today=0, batches_today=0, warm_cursor=0)

        market = str(market or "ALL").strip().upper()
        if market == "EU":
            seq = list(dict.fromkeys(PORTFOLIO_WATCH + [BENCHMARKS["EU"]] + FX_PAIRS + filtered_level_a("EU")))
        elif market == "US":
            seq = list(dict.fromkeys(PORTFOLIO_WATCH + [BENCHMARKS["US"]] + FX_PAIRS + filtered_level_a("US")))
        elif market == "ALL":
            seq = _warm_list()
        else:
            raise ValueError("market deve essere ALL, EU oppure US")

        if not seq:
            return {"server_version": VERSION, "status": "ok", "fetched": [], "failed": [], "checked": 0}

        # Round-robin: ogni chiamata /warm riparte dal punto raggiunto dalla precedente.
        # In questo modo cron-job.org non spreca il budget ricontrollando sempre i primi ticker.
        cursor = int(_WS.get("warm_cursor", 0)) % len(seq)
        rotated = seq[cursor:] + seq[:cursor]

        # Prima riempiamo i buchi reali della cache. Con un universo ampio questo evita
        # di spendere il budget su refresh di serie gia' presenti mentre altri ticker
        # non sono mai stati acquisiti. A cache completa torna il normale round-robin.
        with CACHE_LOCK:
            cached_keys = set(CACHE.keys())
        uncached = [t for t in rotated
                    if _series_key(t, DEFAULT_PERIOD, "1d", None, None) not in cached_keys]
        cached = [t for t in rotated
                  if _series_key(t, DEFAULT_PERIOD, "1d", None, None) in cached_keys]
        ordered = uncached + cached

        last_idx = cursor
        for t in ordered:
            # Il cursore resta coerente con la sequenza canonica anche quando
            # i ticker mancanti vengono anticipati per priorita'.
            last_idx = (seq.index(t) + 1) % len(seq)
            if len(done) + len(failed) >= max_items:
                stop_reason = "limite lotto"
                break
            if time.time() - t0 > budget:
                stop_reason = "limite tempo"
                break
            if _yahoo_cooldown_left() > 0:
                stop_reason = "pausa anti-429"
                break
            if _WS["fetched_today"] >= WARMER_DAILY_MAX:
                stop_reason = "limite giornaliero warmer"
                break
            mem_ok, rss_now = _memory_ok_for_warm()
            if not mem_ok:
                stop_reason = f"memory guard {rss_now} MB"
                _WS["last_error"] = stop_reason
                break
            fail = _FAILS.get(t)
            if fail and time.time() - fail[0] < FAIL_SKIP_S:
                skipped_fail += 1
                continue
            _WS.update(stage="check", current_ticker=t, heartbeat_utc=_utc_stamp())
            try:
                checked += 1
                if not _warm_needed(t):
                    continue
                _WS["stage"] = "fetch"
                payload = _load_series(t, user=False)
                done.append(t)
                if payload.get("data_current") is True:
                    current.append(t)
                else:
                    lagging.append(t)
                _WS["fetched_today"] += 1
                _WS["last_ticker"] = t
                _count("warmer_fetched")
            except Exception as exc:
                msg = str(exc)
                failed.append(t)
                failed_details.append({"ticker": t, "reason": msg[:180]})
                _WS["last_error"] = f"{t}: {msg[:120]}"
                if "429" in msg or "pausa" in msg:
                    stop_reason = "429 Yahoo"
                    break
        _WS["warm_cursor"] = last_idx
        _WS["batches_today"] += 1
        summary = {"at_utc": _utc_stamp(), "market": market, "fetched": len(done), "failed": len(failed),
                   "failed_details": failed_details[:20],
                   "checked": checked, "stop": stop_reason, "seconds": round(time.time() - t0, 1),
                   "next_cursor": _WS["warm_cursor"]}
        _WS["last_batch"] = summary
        _WS["last_cycle_utc"] = summary["at_utc"]
        return {"server_version": VERSION, "status": "ok", "market": market,
                "fetched": done, "current": current, "lagging": lagging,
                "failed": failed, "checked": checked,
                "failed_details": failed_details[:20],
                "skipped_recent_failures": skipped_fail, "stop_reason": stop_reason,
                "seconds": summary["seconds"], "fetched_today": _WS["fetched_today"],
                "cooldown_s": int(_yahoo_cooldown_left()), "next_cursor": _WS["warm_cursor"]}
    finally:
        _WS.update(stage="idle", current_ticker=None, heartbeat_utc=_utc_stamp())
        _WARM_RUN_LOCK.release()


@app.route("/warm", methods=["GET"])
def warm_api():
    try:
        return jsonify(warm_batch(request.args.get("max", WARM_BATCH_MAX), request.args.get("budget", WARM_TIME_BUDGET), request.args.get("market")))
    except Exception as exc:
        return _err(exc)


def _warmer_loop():
    time.sleep(20)
    while True:
        try:
            warm_batch()
        except Exception as exc:
            _WS["last_error"] = f"ciclo: {str(exc)[:120]}"
        time.sleep(30)


def _start_warmer():
    with _WARM_LOCK:
        if _WS["started"] or not WARMER_ENABLED:
            return
        _WS["started"] = True
        threading.Thread(target=_warmer_loop, daemon=True, name="warmer").start()
        log.info("Preriscaldamento in background avviato")


# -----------------------------------------------------------------------------
# Stato del server
# -----------------------------------------------------------------------------
_DUAL_YAHOO_CACHE = {}
_ETORO_SERIES_CACHE = {}
_ETF_SOURCE_CACHE = {}
_ETF_SOURCE_LOCK = threading.RLock()
ETF_HISTORY_SELECTION_VERSION = "etf-history-selection-1.0.0"
_DUAL_YAHOO_LOCK = threading.Lock()


def _etf_history_quality(payload, ticker):
    """Count actual calendar-adjacent pairs; neither interpolation nor source splicing."""
    if not isinstance(payload, dict) or payload.get("ticker") != ticker:
        raise ValueError("Identità storico ETF non coerente")
    expected_currency = ETORO_ETF_MAP[ticker]["currency"]["code"]
    if (payload.get("currency") != expected_currency or payload.get("adjusted") is not True
            or payload.get("interval") != "1d" or payload.get("duplicates_present")
            or payload.get("exchange_timezone") != "Europe/London"
            or str(payload.get("instrument_type", "")).upper() != "ETF"
            or not payload.get("data_quality", {}).get("calendar_validated")):
        raise ValueError("Metadati/rettifiche/calendario ETF non validi")
    history = payload.get("history") or []
    if not history or [r["date"] for r in history] != sorted({r["date"] for r in history}):
        raise ValueError("Date storico ETF non valide")
    if not all(type(r.get("close")) in (int, float) and math.isfinite(r["close"]) and r["close"] > 0 for r in history):
        raise ValueError("Prezzi storico ETF non validi")
    sessions = [d.strftime("%Y-%m-%d") for d in _get_cal(payload["calendar"]).sessions_in_range(
        pd.Timestamp(history[0]["date"]), pd.Timestamp(history[-1]["date"]))]
    dates = {r["date"] for r in history}
    if not dates.issubset(set(sessions)):
        raise ValueError("Barre fuori calendario ETF")
    pairs = sum(a in dates and b in dates for a,b in zip(sessions,sessions[1:]))
    tail = 0
    for d in reversed(sessions):
        if d not in dates:break
        tail += 1
    usable = bool(pairs >= 200 and tail >= 2
                  and (payload.get("data_current") is True or payload.get("storico_provvisorio") is True))
    return {"source":payload.get("source"),"coverage_start":history[0]["date"],
            "coverage_end":history[-1]["date"],"valid_return_pairs":pairs,"contiguous_tail_bars":tail,
            "data_current":payload.get("data_current") is True,"usable_for_risk":usable}


def _load_etf_series(ticker, period, interval, start, end, user, allow_fetch, stale_ok):
    """Prefer sufficient current Yahoo history; retain the broker as a whole-series fallback."""
    expected = _last_completed(_calendar_code(ticker))[0]
    key = (ticker, period, interval, start, end, expected)
    with _ETF_SOURCE_LOCK:
        cached = _ETF_SOURCE_CACHE.get(key)
        if cached and time.time()-cached[0] < 1200:
            return dict(copy.deepcopy(cached[1]),cache_hit=True)
        candidates, attempts = [], []
        def consider(payload, label):
            if payload is None:return
            quality = _etf_history_quality(payload,ticker)
            attempts.append(quality)
            if quality["usable_for_risk"]:candidates.append((payload,quality))
        try:
            consider(_load_series(ticker,period,interval,start,end,user=user,allow_fetch=allow_fetch,
                                 stale_ok=stale_ok,prefer_etoro=False),"Yahoo_Finance_Storico")
        except (ValueError,KeyError,TypeError) as exc:
            attempts.append({"source":"Yahoo_Finance_Storico","error":str(exc)[:200]})
        # Once the ordinary sample is available and current, no redundant broker request.
        sufficient = candidates and candidates[0][1]["data_current"] and candidates[0][1]["valid_return_pairs"] >= 250
        if not sufficient:
            try:consider(_load_etoro_series(ticker,period,interval,start,end,allow_fetch),"ETORO_DAILY_BID")
            except (ValueError,KeyError,TypeError) as exc:
                attempts.append({"source":"ETORO_DAILY_BID","error":str(exc)[:200]})
        if not candidates:
            if not allow_fetch:return None
            raise ValueError("Nessuna serie ETF valida: "+json.dumps(attempts,ensure_ascii=False))
        payload,quality = max(candidates,key=lambda x:(x[1]["data_current"],x[1]["valid_return_pairs"]))
        payload = copy.deepcopy(payload)
        payload["contiguous_tail_bars"] = quality["contiguous_tail_bars"]
        payload["screening_history_accepted"] = quality["contiguous_tail_bars"] >= 61
        payload["data_quality"]["risk_sample_sufficient"] = True
        payload["data_quality"]["valid_return_pairs"] = quality["valid_return_pairs"]
        payload["data_quality"]["risk_sample_status"] = "VALIDO" if quality["valid_return_pairs"] >= 250 else "PROVVISORIO_200_249"
        payload["history_source_selection"] = {"version":ETF_HISTORY_SELECTION_VERSION,
            "selected_source":quality["source"],"reason":"CURRENT_YAHOO_ORDINARY_SAMPLE" if sufficient else "BEST_VALID_WHOLE_SERIES",
            "attempts":attempts,"sources_spliced":False,"common_portfolio_sample_not_certified":True}
        if len(_ETF_SOURCE_CACHE)>20:_ETF_SOURCE_CACHE.clear()
        _ETF_SOURCE_CACHE[key]=(time.time(),copy.deepcopy(payload))
        if payload.get("source")=="ETORO_DAILY_BID":_FAILS.pop(ticker,None)
        _cache_put(_series_key(ticker,period,interval,start,end),{"ts":time.time(),"expected":expected,"after_close":True,"data":payload})
        return payload


def _get_yahoo_event_evidence(ticker, start, end):
    key = (ticker, start, end)
    with _DUAL_YAHOO_LOCK:
        cached = _DUAL_YAHOO_CACHE.get(key)
        if cached and time.time()-cached[0] < 1200:
            return copy.deepcopy(cached[1])
        params = {"interval":"1d", "events":"div,splits", "includeAdjustedClose":"true",
                  "period1":int(datetime.combine(date.fromisoformat(start),dt_time(),timezone.utc).timestamp()),
                  "period2":int(datetime.combine(date.fromisoformat(end),dt_time(),timezone.utc).timestamp())}
        path = "/v8/finance/chart/" + quote(ticker, safe="")
        body = _yahoo_get(path, params, retries=0)
        result = {"document":json.loads(body), "retrieved_at":_utc_stamp(),
                  "query_start":start, "query_end":end,
                  "url":"https://query1.finance.yahoo.com"+path+"?"+urlencode(params)}
        _DUAL_YAHOO_CACHE.clear() if len(_DUAL_YAHOO_CACHE)>12 else None
        _DUAL_YAHOO_CACHE[key]=(time.time(), result)
        return copy.deepcopy(result)


def _load_etoro_series(ticker, period, interval, start, end, allow_fetch):
    code = _calendar_code(ticker)
    expected, _, cal_error = _last_completed(code)[0], None, None
    key = (ticker, period, start, end, expected)
    cached = _ETORO_SERIES_CACHE.get(key)
    if cached and time.time()-cached[0] < 1200:
        return dict(copy.deepcopy(cached[1]),cache_hit=True)
    if not allow_fetch:return None
    broker = get_etf_history(ticker)
    if not broker.get("usable_for_risk") or not broker.get("adjusted_history"):
        detail=broker.get("corporate_actions_validation",{}).get("reason")
        raise ValueError("eToro/Yahoo validation: "+str(detail or broker.get("risk_blockers")))
    history = broker["adjusted_history"]
    if start:history=[b for b in history if b["date"]>=start]
    if end:history=[b for b in history if b["date"]<end]
    raw={"bars":history,"currency":broker["currency"],"tz":"Europe/London", "name":ETORO_ETF_MAP[ticker]["instrument"]["displayName"],
         "instrument_type":"ETF","duplicates_removed":0,"dropped":[],"actions":[],
         "adj_method":broker["risk_asset"]["adjustments_reference"]}
    payload=_build_payload(ticker,period,interval,start,end,code,expected,cal_error,raw)
    payload.update(source="ETORO_DAILY_BID",instrument_id=broker["instrument_id"],history_evidence_policy=broker["evidence_policy"],
                   adjustment_evidence=broker["adjustment_evidence"],
                   price_side="bid",issuer_certified=False)
    payload=_apply_history_policy(payload)
    dates={r["date"] for r in history}
    sessions=[x.strftime("%Y-%m-%d") for x in _get_cal(code).sessions_in_range(pd.Timestamp(history[0]["date"]),pd.Timestamp(history[-1]["date"]))]
    tail=0
    for d in reversed(sessions):
        if d not in dates:break
        tail+=1
    payload['contiguous_tail_bars']=tail
    payload['screening_history_accepted']=bool((payload['data_current'] or payload['storico_provvisorio']) and tail>=61)
    payload['data_quality']['risk_sample_sufficient']=bool(broker['usable_for_risk'])
    payload['data_quality']['risk_sample_status']='VALIDO' if broker['data_quality']['valid_return_pairs']>=250 else 'PROVVISORIO_200_249'
    payload['warnings'].append('Eventi Yahoo e prezzi eToro: controllo incrociato autorizzato dall’utente; assenza eventi non certificata dall’emittente')
    if tail<200:payload['warnings'].append('SMA200 non disponibile: meno di 200 sedute contigue; nessun riempimento')
    if len(_ETORO_SERIES_CACHE)>20:_ETORO_SERIES_CACHE.clear()
    _ETORO_SERIES_CACHE[key]=(time.time(),copy.deepcopy(payload))
    _cache_put(_series_key(ticker,period,interval,start,end), {"ts":time.time(),"expected":expected,"after_close":True,"data":payload})
    return dict(payload,cache_hit=False)


def get_etf_history(ticker):
    """Broker ETF candles for preliminary analysis, without changing risk source."""
    def calendar_factory(ticker, start, end):
        tomorrow = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        cursor = date.fromisoformat(start)
        stop = date.fromisoformat(tomorrow)
        merged = None
        while cursor < stop:
            limit = min(cursor + timedelta(days=400), stop)
            part = get_exchange_calendar(ticker=ticker, start=cursor.isoformat(), end=limit.isoformat())
            if merged is None:
                merged = part
            else:
                merged["sessions"].extend(part["sessions"])
                merged["weekday_closures"].extend(part["weekday_closures"])
                merged["end"] = part["end"]
            cursor = limit
        return merged
    return etoro_history.get_history(ticker, ETORO_ETF_MAP, calendar_factory, _utc_stamp(), yahoo_evidence=_get_yahoo_event_evidence)


def get_server_status():
    def ready(lst):
        n = 0
        for t in lst:
            try:
                if _series_ready(t):
                    n += 1
            except Exception:
                pass
        return n

    with CACHE_LOCK:
        series_items = sum(1 for k in CACHE if k.startswith("s:"))
        compressed_bytes = sum(int(v.get("blob_bytes") or 0) for k, v in CACHE.items() if k.startswith("s:"))
    with _STATS_LOCK:
        stats = dict(_STATS)
    full = _warm_list()
    ready_all = ready(full)
    readiness_reasons = {}
    readiness_samples = {}
    for ticker in full:
        reason = _readiness_reason(ticker)
        readiness_reasons[reason] = readiness_reasons.get(reason, 0) + 1
        if reason != "ready" and len(readiness_samples.setdefault(reason, [])) < 10:
            readiness_samples[reason].append(ticker)
    now = time.time()
    recent_failure_details = [
        {"ticker": ticker, "age_s": int(now - stamp), "reason": str(reason)}
        for ticker, (stamp, reason) in sorted(_FAILS.items(), key=lambda item: item[1][0], reverse=True)
        if now - stamp < FAIL_SKIP_S
    ]
    return {
        "server_version": VERSION, "generated_at_utc": _utc_stamp(),
        "runtime_manifest": RUNTIME_MANIFEST,
        "adaptive_method_version": "1.1.0",
        "feedback_integration_version":"1.2.0",
        "opportunity_collector_version":"session-close-1.0.0",
        "target_resolver_version":"mandate-targets-1.0.0",
        "registry_formula_contract_version":"registry-formulas-1.0.0",
        "global_chat_candidate_budget": 10,
        "mandate_master_id": "14UkvOObo3bHHSdRcL72B_lWLAV6ajEBm",
        "efficiency": {"compact_response_version": COMPACT_RESPONSE_VERSION,
                       "response_formats": ["full", "compact"],
                       "default_format": "full", "identity_batch_max": 50,
                       "portfolio_risk_result_cache": False,
                       "skill_risk_engine_migrated": False},
        "etoro_isin_map_size": len(ETORO_ISIN_MAP),
        "etoro_history": dict(etoro_history.status(), verified_etf_map_size=len(ETORO_ETF_MAP)),
        "etf_history_selection_version": ETF_HISTORY_SELECTION_VERSION,
        "history_gap_policy_version": "calendar-contiguous-screening-1.0.0",
        "stooq_verified_etf_map_size": sum(v.get("verified") is True for v in STOOQ_ETF_MAP.values() if isinstance(v, dict)),
        "level_b_cache_ttl_seconds": _LEVEL_B_TTL,
        "etoro_isin_map_note": "Riferimenti emittente; nessun ISIN eToro dedotto dal solo ticker",
        "level_b_a1_version": A1_VERSION,
        "level_b_a2_version": A2_VERSION,
        "level_b_a3_version": A3_VERSION,
        "level_b_a4_version": A4_VERSION,
        "level_b_a5_version": A5_VERSION,
        "level_b_fusion_version": FUSION_VERSION,
        "level_b_async": {"enabled": True, "poll_interval_s": 30,
                          "result_cache_ttl_s": _LEVEL_B_TTL,
                          "required_workers_per_instance": 1,
                          "completion_is_not_a_data_quality_pass": True},
        "level_b_verification_pack_version": VERIFICATION_PACK_VERSION,
        "exchange_calendars_installed": xcals is not None,
        "universe_version": UNIVERSE_VERSION,
        "universe_US": len(filtered_level_a("US")), "ready_US": ready(filtered_level_a("US")),
        "universe_EU": len(filtered_level_a("EU")), "ready_EU": ready(filtered_level_a("EU")),
        "universe_stats": universe_stats(),
        "portfolio_watch": PORTFOLIO_WATCH, "ready_portfolio": ready(PORTFOLIO_WATCH),
        "portfolio_internal_gap_tickers": [t for t in PORTFOLIO_WATCH if (lambda e: e is not None and _entry_has_internal_gaps(e))(_cache_get(_series_key(t, DEFAULT_PERIOD, "1d", None, None)))],
        "readiness_scope": "latest completed session only; internal gaps and risk validity must be checked separately",
        "internal_gap_refetch_s": LAG_REFETCH_S,
        "warm_list_total": len(full), "warm_pending": len(full) - ready_all,
        "cache_series": series_items,
        "cache_mode": "zlib-json-on-demand",
        "cache_compressed_mb": round(compressed_bytes / (1024 * 1024), 2),
        "memory_rss_mb": _rss_mb(),
        "memory_soft_limit_mb": MEMORY_SOFT_LIMIT_MB,
        "memory_hard_limit_mb": MEMORY_HARD_LIMIT_MB,
        "warmer_daily_max": WARMER_DAILY_MAX,
        "lag_refetch_s": LAG_REFETCH_S,
        "readiness_reasons": readiness_reasons,
        "readiness_samples": readiness_samples,
        "yahoo_calls_today": stats.get("yahoo_calls", 0),
        "yahoo_429_today": stats.get("yahoo_429", 0),
        "cache_hits_today": stats.get("cache_hits", 0),
        "cooldown_s": int(_yahoo_cooldown_left()),
        "recent_failures": len(recent_failure_details),
        "recent_failure_details": recent_failure_details[:20],
        "failure_skip_s": FAIL_SKIP_S,
        "warmer": {"mode": "thread" if WARMER_ENABLED else "cron /warm",
                   "batches_today": _WS["batches_today"], "fetched_today": _WS["fetched_today"],
                   "stage": _WS["stage"], "current_ticker": _WS["current_ticker"],
                   "heartbeat_utc": _WS["heartbeat_utc"], "last_ticker": _WS["last_ticker"],
                   "last_error": _WS["last_error"], "last_batch": _WS["last_batch"],
                   "warm_cursor": _WS.get("warm_cursor", 0)},
    }


# -----------------------------------------------------------------------------
# Endpoint HTTP
# -----------------------------------------------------------------------------
def _err(exc, source="Yahoo_Finance_Storico", code=503):
    return jsonify({"status": "BLOCCATO PER DATI", "source": source,
                    "reason": str(exc)[:300], "server_version": VERSION}), code


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Yahoo Finance Storico", "version": VERSION,
                    "endpoints": ["/health", "/status", "/warm", "/market-data", "/indicators", "/scan",
                                  "/risk (POST)", "/verify-identity (POST)", "/verify-identities (POST)", "/exchange-calendar", "/etf-lookthrough", "/level-b/volume", "/level-b/momentum", "/level-b/catalyst", "/level-b/attention", "/level-b/sector-rotation", "/level-b/fusion", "/level-b/verification-pack", "/mcp (POST)"]})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": VERSION})


@app.route("/status", methods=["GET"])
def status_api():
    return jsonify(_dispatch("get_server_status", dict(request.args)))


@app.route('/cycle-archive/<job_id>',methods=['GET'])
def cycle_archive_route(job_id):
    import gzip
    try:
        payload=gzip.compress(json.dumps(cycle_scanner.get_archive(job_id),sort_keys=True,separators=(',',':')).encode(),mtime=0)
        return app.response_class(payload,mimetype='application/gzip',headers={'X-Content-SHA256':hashlib.sha256(payload).hexdigest()})
    except KeyError:return jsonify({'error':'CYCLE_ARCHIVE_EXPIRED_OR_UNKNOWN'}),404


@app.route("/etf-history", methods=["GET"])
def etf_history_api():
    try:
        return jsonify(get_etf_history(request.args.get("ticker")))
    except Exception as exc:
        return _err(exc)


@app.route("/market-data", methods=["GET"])
def market_data_api():
    try:
        a = request.args
        return jsonify(_dispatch("get_market_data", dict(a)))
    except Exception as exc:
        return _err(exc)


@app.route("/indicators", methods=["GET"])
def indicators_api():
    try:
        return jsonify(get_indicators(request.args.get("ticker")))
    except Exception as exc:
        return _err(exc)


@app.route("/scan", methods=["GET"])
def scan_api():
    try:
        raw = request.args.get("tickers")
        tickers = [t for t in raw.split(",") if t.strip()] if raw else None
        return jsonify(_dispatch("scan_market", {"market": request.args.get("market", "US"),
                       "tickers": tickers, "max_per_setup": request.args.get("max_per_setup", 4),
                       "response_format": request.args.get("response_format")}))
    except Exception as exc:
        return _err(exc)


@app.route("/level-b/volume", methods=["GET"])
def level_b_volume_api():
    try:
        a = request.args
        return jsonify(scan_level_b_volume(a.get("market", "US"),
                                           a.get("max_history_checks", 12),
                                           a.get("max_results", 8)))
    except Exception as exc:
        return _err(exc, source="Level_B_A1")


@app.route("/level-b/momentum", methods=["GET"])
def level_b_momentum_api():
    try:
        a = request.args
        return jsonify(scan_level_b_momentum(a.get("market", "US"),
                                             a.get("max_history_checks", 15),
                                             a.get("max_results", 8)))
    except Exception as exc:
        return _err(exc, source="Level_B_A2")


@app.route("/level-b/catalyst", methods=["GET"])
def level_b_catalyst_api():
    try:
        a = request.args
        return jsonify(scan_level_b_catalyst(a.get("market", "US"), a.get("max_results", 12)))
    except Exception as exc:
        return _err(exc, source="Level_B_A3")


@app.route("/level-b/attention", methods=["GET"])
def level_b_attention_api():
    a = request.args
    try:
        return jsonify(scan_level_b_attention(a.get("market", "US"), a.get("max_results", 12)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/level-b/sector-rotation", methods=["GET"])
def level_b_sector_rotation_api():
    try:
        a = request.args
        return jsonify(scan_level_b_sector_rotation(a.get("market", "US"), a.get("max_results", 12)))
    except Exception as exc:
        return _err(exc, source="Level_B_A5")




@app.route("/level-b/fusion", methods=["GET"])
def level_b_fusion_api():
    try:
        a = request.args
        return jsonify(scan_level_b_fusion(a.get("market", "US"), a.get("max_results", 10)))
    except Exception as exc:
        return _err(exc, source="Level_B_Fusion")


@app.route("/level-b/verification-pack", methods=["GET"])
def level_b_verification_pack_api():
    try:
        a = request.args
        return jsonify(scan_level_b_verification_pack(a.get("market", "US"), a.get("max_results", 10)))
    except Exception as exc:
        return _err(exc, source="Level_B_Verification_Pack")


@app.route("/risk", methods=["POST"])
def risk_api():
    try:
        b = request.get_json(silent=True) or {}
        return jsonify(portfolio_risk(b.get("equity_usd"), b.get("cash_usd"), b.get("positions"),
                                      b.get("copy_value_usd", 0), b.get("candidate"), b.get("eurusd"),
                                      b.get("frozen_cash_usd", 0), b.get("pending_orders")))
    except Exception as exc:
        return _err(exc, "server-risk", 400)


@app.route("/verify-identity", methods=["POST"])
def verify_identity_api():
    try:
        b = request.get_json(silent=True) or {}
        out = verify_instrument_identity(b.get("yahoo_ticker"), b.get("yahoo_name"), b.get("yahoo_isin"),
                                          b.get("etoro_ticker"), b.get("etoro_name"), b.get("etoro_isin"))
        out["server_version"] = VERSION
        return jsonify(_format_response("verify_instrument_identity", out, b.get("response_format")))
    except Exception as exc:
        return _err(exc, "instrument-identity", 400)


@app.route("/verify-identities", methods=["POST"])
def verify_identities_api():
    try:
        b = request.get_json(force=True) or {}
        return jsonify(_dispatch("verify_instrument_identities", b))
    except Exception as exc:
        return _err(exc, "instrument-identity", 400)


@app.route("/exchange-calendar", methods=["GET"])
def exchange_calendar_api():
    try:
        a = request.args
        return jsonify(get_exchange_calendar(a.get("exchange"), a.get("ticker"), a.get("start"), a.get("end")))
    except Exception as exc:
        return _err(exc, "Official_Exchange_Calendar", 400)


@app.route("/etf-lookthrough", methods=["GET"])
def etf_lookthrough_api():
    try:
        return jsonify(get_etf_lookthrough(request.args.get("ticker"), request.args.get("top", 25)))
    except Exception as exc:
        return _err(exc, "Official_ETF_Issuer")


# -----------------------------------------------------------------------------
# MCP (JSON-RPC 2.0)
# -----------------------------------------------------------------------------
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

TOOLS = [
    {"name": "get_etf_history", "annotations": RO,
     "description": "Storico ETF eToro con eventi Yahoo, controllo incrociato autorizzato dall’utente, fattori di rettifica, calendario e campione. Restituisce risk_asset per calcolo offline. Non certifica il ticket e non legge il conto.",
     "inputSchema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "get_market_data", "annotations": RO,
     "description": "Storico OHLCV rettificato (solo sedute concluse) da Yahoo, con valuta, timezone, "
                    "calendario, ultima seduta attesa (data_current), split e dividendi. Max 300 barre.",
     "inputSchema": {"type": "object", "properties": {
         "ticker": {"type": "string", "description": "Ticker Yahoo, es. AAPL, SAP.DE, CSPX.L, EURUSD=X"},
         "period": {"type": "string", "default": "2y", "description": "1mo, 3mo, 6mo, 1y, 2y, 5y, ytd, max"},
         "interval": {"type": "string", "default": "1d", "description": "1d, 1wk, 1mo"},
         "start": {"type": "string", "description": "YYYY-MM-DD"},
         "end": {"type": "string", "description": "YYYY-MM-DD esclusiva"},
         "max_bars": {"type": "integer", "description": "Barre restituite, 1-300 (default 300)"}},
         "required": ["ticker"]}},
    {"name": "get_indicators", "annotations": RO,
     "description": "Indicatori tecnici compatti su sedute concluse: trend, SMA 20/50/200, RSI, MACD, ATR, "
                    "RVOL giornaliero, forza relativa 20 sedute vs benchmark, supporti/resistenze, setup.",
     "inputSchema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "scan_market", "annotations": RO,
     "description": "Scanner riproducibile su universo USA o Europa (o lista personalizzata). Restituisce "
                    "copertura, esclusi e shortlist per setup AVVIO_TREND/CONTINUAZIONE/CORREZIONE/INVERSIONE, con copertura reale dei dati correnti.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "tickers": {"type": "array", "items": {"type": "string"},
                     "description": "Opzionale: lista ticker Yahoo da analizzare al posto dell'universo"},
         "max_per_setup": {"type": "integer", "default": 4}},
         "required": ["market"]}},
    {"name": "scan_level_b_volume", "annotations": RO,
     "description": "A1 Level B: scopre ticker fuori dal Level A tramite market movers, conferma anomalie di volume con storico Yahoo Finance Storico e restituisce una shortlist compatta. Non e' un ticket operativo.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_history_checks": {"type": "integer", "default": 12},
         "max_results": {"type": "integer", "default": 8}},
         "required": ["market"]}},
    {"name": "scan_level_b_momentum", "annotations": RO,
     "description": "A2 Level B: radar prezzo/momentum fuori dal Level A. Classifica BREAKOUT_EARLY, AVVIO_TREND, PULLBACK_REBOUND e ACCELERATION; separa ESTESO come NO_CHASE.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_history_checks": {"type": "integer", "default": 15},
         "max_results": {"type": "integer", "default": 8}},
         "required": ["market"]}},
    {"name": "scan_level_b_catalyst", "annotations": RO,
     "description": "A3 Level B: radar catalizzatori/news fuori dal Level A. Classifica e pesa importanza, affidabilita fonte, freschezza, sorpresa e impatto; M&A/OPA non confermate da fonte primaria restano WATCH_ONLY.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 12}},
         "required": ["market"]}},
    {"name": "scan_level_b_attention", "annotations": RO,
     "description": "Level B A4: radar attention/special situations (IPO, insider, activist, short interest, spin-off, restructuring, M&A speculation).",
     "inputSchema": {"type": "object", "properties": {"market": {"type": "string", "enum": ["US", "EU"]},
                                                       "max_results": {"type": "integer", "minimum": 1, "maximum": 30}}}},
    {"name": "scan_level_b_sector_rotation", "annotations": RO,
     "description": "A5 Level B: radar di rotazione settoriale 1D/5D/20D contro benchmark. Classifica SETTORE_FREDDO, SETTORE_NEUTRO, ROTATION_START, SETTORE_FORTE e SETTORE_ESTESO; non genera ticket autonomi.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 12}},
         "required": ["market"]}},
    {"name": "scan_level_b_fusion", "annotations": RO,
     "description": "Level B Fusion asincrona: prima chiamata avvia A1-A5; QUEUED/RUNNING richiedono ripetizione dopo retry_after_s. Solo job_status COMPLETE contiene il risultato, non certifica gate/qualita o BUY. Nessuna scansione vuota implicita. Risultato riusabile 900 s; soglie e pesi invariati.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 10}},
         "required": ["market"]}},
    {"name": "get_level_b_verification_pack", "annotations": RO,
     "description": "Verification Pack asincrono: ripetere la stessa chiamata dopo retry_after_s se QUEUED/RUNNING. Solo COMPLETE contiene candidati/etoro_symbols, da verificare in batch eToro; ERROR blocca. Riusa Fusion corrente senza ripetere A1-A5. Nessun BUY signal.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 10}},
         "required": ["market"]}},
    {"name": "verify_instrument_identity", "annotations": RO,
     "description": "Controllo anti-omonimia Yahoo/eToro: confronta ISIN quando disponibile, altrimenti i nomi normalizzati. Un ticker uguale da solo non basta.",
     "inputSchema": {"type": "object", "properties": {
         "yahoo_ticker": {"type": "string"}, "yahoo_name": {"type": "string"}, "yahoo_isin": {"type": "string"},
         "etoro_ticker": {"type": "string"}, "etoro_name": {"type": "string"}, "etoro_isin": {"type": "string"}},
         "required": ["yahoo_ticker", "etoro_ticker"]}},
    {"name": "get_portfolio_risk", "annotations": RO,
     "description": "VaR/ES 95-99% 1g e 5g (simulazione storica), correlazioni 60/120, allocazione, "
                    "riconciliazione e simulazione di un candidato. positions = sole posizioni dirette "
                    "(ticker Yahoo, valore USD); le COPY vanno solo in copy_value_usd (aggregate).",
     "inputSchema": {"type": "object", "properties": {
         "equity_usd": {"type": "number"}, "cash_usd": {"type": "number"},
         "copy_value_usd": {"type": "number"}, "eurusd": {"type": "number"},
         "frozen_cash_usd": {"type": "number"}, "pending_orders": {"type": "array"},
         "positions": {"type": "array", "items": {"type": "object", "properties": {
             "ticker": {"type": "string"}, "value_usd": {"type": "number"},
             "type": {"type": "string", "description": "STOCK o ETF"}},
             "required": ["ticker", "value_usd"]}},
         "candidate": {"type": "object", "properties": {
             "ticker": {"type": "string"}, "amount_usd": {"type": "number"}, "type": {"type": "string"}},
             "required": ["ticker", "amount_usd"]}},
         "required": ["equity_usd", "cash_usd", "positions", "eurusd"]}},
    {"name": "get_exchange_calendar", "annotations": RO,
     "description": "Sedute, orari, chiusure e stato attuale di una borsa (XNYS, XETR, XMIL, XPAR, XAMS, "
                    "XMAD, XSWX, XLON, nordiche). Indicare exchange oppure ticker.",
     "inputSchema": {"type": "object", "properties": {
         "exchange": {"type": "string"}, "ticker": {"type": "string"},
         "start": {"type": "string"}, "end": {"type": "string"}}}},
    {"name": "get_etf_lookthrough", "annotations": RO,
     "description": "Look-through ETF opzionale (CSPX, EIMI, WDEF): holdings/settori/paesi quando disponibili. "
                    "Se la fonte emittente non e' raggiungibile, restituisce stato non bloccante: scanner e analisi tecnica dell'ETF restano utilizzabili.",
     "inputSchema": {"type": "object", "properties": {
         "ticker": {"type": "string"}, "top": {"type": "integer", "default": 25}},
         "required": ["ticker"]}},
    {"name": "scan_cycle_candidates", "annotations": RO,
     "description": "Scansione asincrona Level A/B per ciclo: massimo 10 nuovi candidati complessivi USA/Europa prima della chat. Poll stesso job; nessun ticket automatico.",
     "inputSchema": {"type": "object", "properties": {
         "markets": {"type": "array", "items": {"type": "string", "enum": ["US", "EU"]}},
         "learning_state": {"type": "object"},
         "learning_states": {"type": "object"},
         "pending_keys": {"type": "array", "items": {"type": "string"}}}, "required": ["markets"]}},
    {"name": "get_server_status", "annotations": RO,
     "description": "Stato del server: titoli pronti in cache, chiamate e 429 Yahoo, pausa anti-429.",
     "inputSchema": {"type": "object", "properties": {}}},
]


TOOLS.append({
    "name": "verify_instrument_identities", "annotations": RO,
    "description": "Esegue 1-50 confronti reali Yahoo/eToro in una chiamata, con esiti indipendenti. "
                   "Non acquisisce dati di conto e non sostituisce overview o altri gate.",
    "inputSchema": {"type": "object", "properties": {
        "instruments": {"type": "array", "minItems": 1, "maxItems": 50,
                        "items": {"type": "object", "properties": {
                            k: {"type": "string"} for k in (
                                "yahoo_ticker", "yahoo_name", "yahoo_isin", "etoro_ticker",
                                "etoro_name", "etoro_isin")},
                                  "required": ["yahoo_ticker", "etoro_ticker"]}}},
                    "required": ["instruments"]}})
for _tool in TOOLS:
    if _tool["name"] in COMPACT_TOOLS:
        _tool["inputSchema"]["properties"]["response_format"] = {
            "type": "string", "enum": ["full", "compact"], "default": "full",
            "description": "compact riduce dettagli ripetuti; full conserva la risposta originale. "
                           "Per calcoli esterni sugli storici usare full."}



def _dispatch_full(name, a):
    if name == "scan_cycle_candidates":
        state = a.get("learning_state")
        states=a.get('learning_states') or {}
        for key,value in states.items():
            validate_state(value)
            if key!=value.get('scope',{}).get('category','')+'|'+value.get('scope',{}).get('method_id',''):raise ValueError('LEARNING_SCOPE_KEY_MISMATCH')
        profile = validate_state(state)["active"] if state else "balanced"
        return cycle_scanner.request(a.get("markets", []), profile, a.get("pending_keys", []),
                                     lambda m: scan_market(m), lambda m: scan_level_b_fusion(m, 10),
                                     profile_scope=state.get("scope") if state else None,learning_states=states)
    if name == "get_etf_history":
        return get_etf_history(a.get("ticker"))
    if name == "get_market_data":
        return get_market_data(a.get("ticker"), a.get("period") or DEFAULT_PERIOD, a.get("interval") or "1d",
                               a.get("start"), a.get("end"), a.get("max_bars"))
    if name == "get_indicators":
        return get_indicators(a.get("ticker"))
    if name == "scan_market":
        return scan_market(a.get("market", "US"), a.get("tickers"), a.get("max_per_setup", 4))
    if name == "scan_level_b_volume":
        return scan_level_b_volume(a.get("market", "US"), a.get("max_history_checks", 12), a.get("max_results", 8))
    if name == "scan_level_b_momentum":
        return scan_level_b_momentum(a.get("market", "US"), a.get("max_history_checks", 15), a.get("max_results", 8))
    if name == "scan_level_b_catalyst":
        return scan_level_b_catalyst(a.get("market", "US"), a.get("max_results", 12))
    if name == "scan_level_b_attention":
        return scan_level_b_attention(a.get("market", "US"), a.get("max_results", 12))
    if name == "scan_level_b_sector_rotation":
        return scan_level_b_sector_rotation(a.get("market", "US"), a.get("max_results", 12))
    if name == "scan_level_b_fusion":
        return scan_level_b_fusion(a.get("market", "US"), a.get("max_results", 10))
    if name == "get_level_b_verification_pack":
        return scan_level_b_verification_pack(a.get("market", "US"), a.get("max_results", 10))
    if name == "verify_instrument_identities":
        return verify_instrument_identities(a.get("instruments"))
    if name == "verify_instrument_identity":
        return verify_instrument_identity(a.get("yahoo_ticker"), a.get("yahoo_name"), a.get("yahoo_isin"),
                                           a.get("etoro_ticker"), a.get("etoro_name"), a.get("etoro_isin"))
    if name == "get_portfolio_risk":
        return portfolio_risk(a.get("equity_usd"), a.get("cash_usd"), a.get("positions"),
                              a.get("copy_value_usd", 0), a.get("candidate"), a.get("eurusd"),
                              a.get("frozen_cash_usd", 0), a.get("pending_orders"))
    if name == "get_exchange_calendar":
        return get_exchange_calendar(a.get("exchange"), a.get("ticker"), a.get("start"), a.get("end"))
    if name == "get_etf_lookthrough":
        return get_etf_lookthrough(a.get("ticker"), a.get("top", 25))
    if name == "get_server_status":
        return get_server_status()
    raise KeyError(name)


def _dispatch(name, a):
    # Convalida prima di acquisire dati: un formato errato non consuma quote.
    mode = _response_format(a.get("response_format"))
    return _format_response(name, _dispatch_full(name, a), mode)


def _rpc(rpc_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return jsonify(body)


@app.route("/mcp", methods=["GET"])
def mcp_get():
    return jsonify({"error": "Usare POST JSON-RPC"}), 405


@app.route("/mcp", methods=["POST"])
def mcp_rpc_server():
    # Gunicorn/Render may import the module before forking workers. In that case a
    # thread started at import time can be lost in the worker. Ensure the warmer
    # is started lazily inside the serving process as soon as MCP traffic arrives.
    _start_warmer()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return _rpc(None, error={"code": -32600, "message": "Invalid Request"}), 400
    method = body["method"]
    if "id" not in body:
        return "", 202
    rpc_id = body.get("id")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _rpc(rpc_id, error={"code": -32602, "message": "Invalid params"})

    if method == "initialize":
        req = params.get("protocolVersion")
        ver = req if req in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _rpc(rpc_id, {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "YahooFinanceAdvancedMCP", "version": VERSION}})
    if method == "ping":
        return _rpc(rpc_id, {})
    if method == "tools/list":
        return _rpc(rpc_id, {"tools": TOOLS})
    if method == "resources/list":
        return _rpc(rpc_id, {"resources": []})
    if method == "prompts/list":
        return _rpc(rpc_id, {"prompts": []})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc(rpc_id, error={"code": -32602, "message": "Invalid arguments"})
        try:
            data = _dispatch(name, args)
            return _rpc(rpc_id, {"content": [{"type": "text", "text": _json_text(data)}], "isError": False})
        except KeyError:
            return _rpc(rpc_id, error={"code": -32602, "message": f"Tool sconosciuto: {name}"})
        except Exception as exc:
            source = {"get_exchange_calendar": "Official_Exchange_Calendar",
                      "get_etf_lookthrough": "Official_ETF_Issuer",
                      "get_portfolio_risk": "server-risk"}.get(name, "Yahoo_Finance_Storico")
            return _rpc(rpc_id, {"isError": True, "content": [{"type": "text", "text": _json_text(
                {"status": "BLOCCATO PER DATI", "source": source, "reason": str(exc)[:300],
                 "server_version": VERSION})}]})
    return _rpc(rpc_id, error={"code": -32601, "message": "Method not found"}), 404


# -----------------------------------------------------------------------------
# Avvio
# -----------------------------------------------------------------------------
_start_warmer()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

