# =============================================================================
#  Yahoo Finance Storico - server MCP per il progetto "Trading eToro"
#  Versione 1.7.34 - SOLA LETTURA
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
import http.cookiejar
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo
from datetime import date, datetime, time as dt_time, timedelta, timezone

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
VERSION = "1.7.34"
RISK_ENGINE_VERSION = "server-risk 1.0.0"
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


def scan_level_b_volume(market="US", max_history_checks=24, max_results=12):
    market = str(market or "US").upper()
    level_a = filtered_level_a(market)
    return discover_a1_volume(
        market=market, yahoo_get=_yahoo_get, yahoo_post=_yahoo_post, load_series=_load_series,
        level_a=level_a, excluded_symbols=EXCLUDED_SYMBOLS.keys(),
        session_progress_fn=_session_progress, intraday_rvol_fn=_load_intraday_rvol_at_time,
        max_history_checks=int(max_history_checks), max_results=int(max_results),
    )


def scan_level_b_momentum(market="US", max_history_checks=30, max_results=12):
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


def scan_level_b_fusion(market="US", max_results=10):
    market = str(market or "US").upper()
    # Ogni antenna conserva la propria logica; la fusione riceve solo output compatti.
    a1 = scan_level_b_volume(market, 24, 12)
    a2 = scan_level_b_momentum(market, 30, 12)
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


def scan_level_b_verification_pack(market="US", max_results=10):
    """Compact downstream pack: Fusion shortlist + final Yahoo technical drill-down.

    eToro verification deliberately stays outside Render: the consumer must batch-check
    the returned etoro_symbols with the read-only eToro connector immediately before
    any operational proposal.
    """
    market = str(market or "US").upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US oppure EU")
    lim = max(1, min(int(max_results or 10), 10))
    fusion = scan_level_b_fusion(market, lim)
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
            quality_ok = bool(ind.get("data_current") is True and not ind.get("stale_cache"))
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
            dropped.append({"date": d, "reason": "close mancante"})
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
            "name": meta.get("longName") or meta.get("shortName")}


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
        "ticker": ticker, "name": raw["name"], "interval": interval,
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
        "data_quality": {"strict_json": True, "dropped_bars_count": len(raw["dropped"]),
                         "dropped_bars": raw["dropped"][-5:],
                         "risk_sample_sufficient": len(bars) - 1 >= 250},
        "warnings": warnings,
        "history": bars,
        "actions": [a for a in raw["actions"] if a["date"] in dates],
    }
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
        stored["blob"] = blob
        stored["blob_bytes"] = len(blob)
    with CACHE_LOCK:
        for k in [k for k, v in CACHE.items() if now - v["ts"] >= STALE_MAX_AGE]:
            del CACHE[k]
        while len(CACHE) >= CACHE_MAX_ITEMS:
            del CACHE[min(CACHE, key=lambda k: CACHE[k]["ts"])]
        CACHE[key] = stored


def _is_fresh(entry, code, expected, interval, end):
    age = time.time() - entry["ts"]
    if end:
        return age < 86400
    if interval != "1d" or code is None or expected is None:
        return age < NO_CAL_TTL
    if entry["expected"] != expected:
        return False
    if not entry["after_close"]:
        return age < MIN_REFETCH_S
    if entry.get("coverage_end") != expected:
        return age < LAG_REFETCH_S
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


def _load_series(ticker, period=DEFAULT_PERIOD, interval="1d", start=None, end=None,
                 user=True, allow_fetch=True, stale_ok=False):
    ticker = _validate_ticker(ticker)
    _validate_period(period, interval, start, end)
    code = _calendar_code(ticker)
    expected, close_utc, cal_error = None, None, None
    if code and interval == "1d":
        try:
            expected, close_utc = _last_completed(code)
        except Exception as exc:
            cal_error = str(exc)
    key = _series_key(ticker, period, interval, start, end)
    entry = _cache_get(key)
    if entry and _is_fresh(entry, code, expected, interval, end):
        if user:
            _count("cache_hits")
        return _from_cache(entry, expected)
    usable_stale = bool(entry and time.time() - entry["ts"] < STALE_MAX_AGE)
    if not allow_fetch:
        if stale_ok and usable_stale:
            d = _from_cache(entry, expected)
            d["stale_cache"] = True
            return d
        return None
    fail = _FAILS.get(ticker)
    if fail and time.time() - fail[0] < FAIL_SKIP_S and not usable_stale:
        raise ValueError(f"Fallito di recente: {fail[1]}")
    try:
        raw = _fetch_yahoo_chart(ticker, period, interval, start, end, retries=1 if user else 0)
        payload = _build_payload(ticker, period, interval, start, end, code, expected, cal_error, raw)
    except ValueError as exc:
        msg = str(exc)
        if "429" not in msg and "pausa" not in msg:
            _FAILS[ticker] = (time.time(), msg[:150])
        if usable_stale:
            d = _from_cache(entry, expected)
            d["stale_cache"] = True
            d["warnings"].append(f"Fonte non disponibile ({msg[:100]}): restituita cache precedente")
            return d
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
        if len(bbars) > 21:
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
        "data_current": payload.get("data_current"), "stale_cache": bool(payload.get("stale_cache")),
        "bars_used": n, "source": "Yahoo_Finance_Storico", "adjusted": True,
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
        "high_52w": _rnd(hi252), "low_52w": _rnd(lo252), "dist_high_52w_pct": _pct(last / hi252 - 1),
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
               "as_of", "trend", "rsi14", "ret_20d_pct", "rs_20d_vs_benchmark_pct", "rvol_giornaliero",
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
        if p.get("data_current") is False:
            excluded["dati_non_aggiornati"].append(t)
            continue

        fresh_acquired += 1
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
        "coverage_pct": _rnd(fresh_acquired / n * 100, 1) if n else None,
        "coverage_definition": "ticker con storico disponibile e aggiornato all'ultima seduta completa attesa",
        "coverage_summary": {
            "status": "COMPLETE" if n and fresh_acquired == n else "PARTIAL",
            "expected_total": n,
            "data_acquired": acquired_any,
            "data_current": fresh_acquired,
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
        "scan_complete": bool(n and fresh_acquired == n and not excluded["errore_dati"]
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
    hist = payload["history"]
    s = pd.Series([b["close"] for b in hist], index=[b["date"] for b in hist], dtype=float)
    major = payload.get("major_currency") or payload.get("currency")
    if major == "USD":
        return s, None
    fx_t = f"{major}USD=X"
    fx = _load_series(fx_t)
    f = pd.Series([b["close"] for b in fx["history"]], index=[b["date"] for b in fx["history"]], dtype=float)
    df = pd.concat([s, f], axis=1, join="inner").dropna().sort_index()
    if len(df) < 30:
        raise ValueError(f"Allineamento con il cambio {fx_t} insufficiente")
    return df.iloc[:, 0] * df.iloc[:, 1], fx_t


def _var_es(R, w, equity, eurusd=None):
    out = {}
    r1 = R.values @ w
    r5 = ((1 + R).rolling(5).apply(np.prod, raw=True) - 1).dropna().values @ w
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
                if abs(val) > 0.70:
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
    tol = max((fx_eur or 0.0) * 1.0, 0.001 * equity)
    recon_ok = abs(diff) <= tol
    if not recon_ok:
        issues.append(f"Riconciliazione fallita: scarto {diff:.2f} USD oltre tolleranza {tol:.2f} USD")

    prices, fx_used, missing, stale, ends = {}, {}, [], [], []
    for t in list(agg) + ([cand["ticker"]] if cand else []):
        if t in prices:
            continue
        try:
            pl = _load_series(t)
            s, fxt = _close_series_usd(pl)
            prices[t] = s
            if fxt:
                fx_used[t] = fxt
            if pl.get("data_current") is False:
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
        df = pd.concat({t: prices[t] for t in port_t}, axis=1, join="inner").sort_index().dropna()
        R = df.pct_change().dropna().tail(500)
        n = len(R)
        sample = {"returns_common": int(n),
                  "start": R.index[0] if n else None, "end": R.index[-1] if n else None}
        if n < 200:
            issues.append(f"Campione comune insufficiente: {n} rendimenti (minimo 200, ordinario 250)")
        elif n < 250:
            alerts.append(f"Campione provvisorio: {n} rendimenti (200-249)")
        if n >= 30:
            w = np.array([agg[t]["value"] / equity for t in port_t])
            result_var = _var_es(R, w, equity, fx_eur)
            v1, v5 = result_var.get("var99_1d_pct"), result_var.get("var99_5d_pct")
            if v1 is not None and v5 is not None:
                if v1 > 4.0 or v5 > 10.0:
                    critical.append(f"VaR99 critico (1g {v1}%, 5g {v5}%): blocco nuovi acquisti")
                elif v1 > 3.0 or v5 > 7.5:
                    alerts.append(f"SEGNALAZIONE RISCHIO: VaR99 1g {v1}% / 5g {v5}%")
            corr120 = _corr_info(R, 120)
            corr60 = _corr_info(R, 60)
            if corr120["pairs_over_070"]:
                alerts.append("ALERT CORRELAZIONE su 120 sedute")

    cash_pct = cash / equity
    if cash_pct < 0.15 or cash_pct > 0.30:
        alerts.append(f"Liquidita' {cash_pct * 100:.2f}% fuori fascia 15-30%")
    for t, a in agg.items():
        wt = a["value"] / equity
        if not a["is_etf"] and wt > 0.20:
            alerts.append(f"Emittente {t} al {wt * 100:.1f}%" + (" (oltre 25%: no ulteriori acquisti)" if wt > 0.25 else ""))

    cand_out = None
    if cand:
        ct = cand["ticker"]
        cand_out = {"ticker": ct, "amount_usd": cand["amount"], "esito": None, "motivi": []}
        if ct not in prices:
            cand_out.update(esito="BLOCCATO PER DATI", motivi=["Serie del candidato non disponibile"])
        elif cand["amount"] > cash:
            cand_out.update(esito="BLOCCATO PER DATI", motivi=["Importo superiore alla liquidita' dichiarata"])
        else:
            cols = port_t + ([ct] if ct not in port_t else [])
            dfc = pd.concat({t: prices[t] for t in cols}, axis=1, join="inner").sort_index().dropna()
            Rc = dfc.pct_change().dropna().tail(500)
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
            high = [x["ticker"] for x in corr_c if x["corr_120"] is not None and x["corr_120"] > 0.80]
            group_w = sum(new_val[t] for t in high) / equity + new_val[ct] / equity
            motivi = []
            esito = "PASS"
            if len(Rc) < 200:
                esito = "BLOCCATO PER DATI"
                motivi.append(f"Campione comune con il candidato: {len(Rc)} rendimenti")
            if high and group_w > 0.40:
                esito = "BLOCCATO PER RISCHIO"
                motivi.append(f"Correlazione >0,80 con {', '.join(high)} e gruppo al {group_w * 100:.1f}%")
            if after:
                v1, v5 = after.get("var99_1d_pct"), after.get("var99_5d_pct")
                if v1 is not None and v5 is not None and (v1 > 4.0 or v5 > 10.0):
                    esito = "BLOCCATO PER RISCHIO"
                    motivi.append(f"VaR99 dopo l'acquisto critico (1g {v1}%, 5g {v5}%)")
                elif v1 is not None and v5 is not None and (v1 > 3.0 or v5 > 7.5) and esito == "PASS":
                    esito = "REVISIONE RICHIESTA"
                    motivi.append(f"VaR99 dopo l'acquisto in alert (1g {v1}%, 5g {v5}%)")
            wct = new_val[ct] / equity
            if not cand["is_etf"] and wct > 0.25:
                esito = "BLOCCATO PER RISCHIO"
                motivi.append(f"Emittente {ct} oltre 25% dopo l'acquisto")
            if (cash - cand["amount"]) / equity < 0.15 and esito == "PASS":
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
    notes.append("Simulazione storica su rendimenti giornalieri rettificati convertiti in USD; 5 giorni con "
                 "finestre mobili effettive. Non e' una perdita massima garantita.")
    return {
        "server_version": VERSION, "engine": RISK_ENGINE_VERSION, "generated_at_utc": _utc_stamp(),
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
        return age >= LAG_REFETCH_S
    return False


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
        "level_b_a1_version": A1_VERSION,
        "level_b_a2_version": A2_VERSION,
        "level_b_a3_version": A3_VERSION,
        "level_b_a4_version": A4_VERSION,
        "level_b_a5_version": A5_VERSION,
        "level_b_fusion_version": FUSION_VERSION,
        "level_b_verification_pack_version": VERIFICATION_PACK_VERSION,
        "exchange_calendars_installed": xcals is not None,
        "universe_version": UNIVERSE_VERSION,
        "universe_US": len(filtered_level_a("US")), "ready_US": ready(filtered_level_a("US")),
        "universe_EU": len(filtered_level_a("EU")), "ready_EU": ready(filtered_level_a("EU")),
        "universe_stats": universe_stats(),
        "portfolio_watch": PORTFOLIO_WATCH, "ready_portfolio": ready(PORTFOLIO_WATCH),
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
                                  "/risk (POST)", "/exchange-calendar", "/etf-lookthrough", "/level-b/volume", "/level-b/momentum", "/level-b/catalyst", "/level-b/attention", "/level-b/sector-rotation", "/level-b/fusion", "/level-b/verification-pack", "/mcp (POST)"]})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": VERSION})


@app.route("/status", methods=["GET"])
def status_api():
    return jsonify(get_server_status())


@app.route("/market-data", methods=["GET"])
def market_data_api():
    try:
        a = request.args
        return jsonify(get_market_data(a.get("ticker"), a.get("period", DEFAULT_PERIOD), a.get("interval", "1d"),
                                       a.get("start"), a.get("end"), a.get("max_bars")))
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
        return jsonify(scan_market(request.args.get("market", "US"), tickers,
                                   request.args.get("max_per_setup", 4)))
    except Exception as exc:
        return _err(exc)


@app.route("/level-b/volume", methods=["GET"])
def level_b_volume_api():
    try:
        a = request.args
        return jsonify(scan_level_b_volume(a.get("market", "US"),
                                           a.get("max_history_checks", 24),
                                           a.get("max_results", 12)))
    except Exception as exc:
        return _err(exc, source="Level_B_A1")


@app.route("/level-b/momentum", methods=["GET"])
def level_b_momentum_api():
    try:
        a = request.args
        return jsonify(scan_level_b_momentum(a.get("market", "US"),
                                             a.get("max_history_checks", 30),
                                             a.get("max_results", 12)))
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
         "max_history_checks": {"type": "integer", "default": 24},
         "max_results": {"type": "integer", "default": 12}},
         "required": ["market"]}},
    {"name": "scan_level_b_momentum", "annotations": RO,
     "description": "A2 Level B: radar prezzo/momentum fuori dal Level A. Classifica BREAKOUT_EARLY, AVVIO_TREND, PULLBACK_REBOUND e ACCELERATION; separa ESTESO come NO_CHASE.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_history_checks": {"type": "integer", "default": 30},
         "max_results": {"type": "integer", "default": 12}},
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
     "description": "Level B Fusion: unisce A1-A5 con pesi 25/25/25/15/10, applica penalita separate e restituisce shortlist 5-10; non genera ticket autonomi.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 10}},
         "required": ["market"]}},
    {"name": "get_level_b_verification_pack", "annotations": RO,
     "description": "Fusion shortlist con drill-down tecnico Yahoo compatto per i candidati Level B. Restituisce etoro_symbols da verificare in una sola chiamata batch eToro; non genera BUY signal.",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string", "description": "US oppure EU"},
         "max_results": {"type": "integer", "default": 10}},
         "required": ["market"]}},
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
    {"name": "get_server_status", "annotations": RO,
     "description": "Stato del server: titoli pronti in cache, chiamate e 429 Yahoo, pausa anti-429.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _dispatch(name, a):
    if name == "get_market_data":
        return get_market_data(a.get("ticker"), a.get("period") or DEFAULT_PERIOD, a.get("interval") or "1d",
                               a.get("start"), a.get("end"), a.get("max_bars"))
    if name == "get_indicators":
        return get_indicators(a.get("ticker"))
    if name == "scan_market":
        return scan_market(a.get("market", "US"), a.get("tickers"), a.get("max_per_setup", 4))
    if name == "scan_level_b_volume":
        return scan_level_b_volume(a.get("market", "US"), a.get("max_history_checks", 24), a.get("max_results", 12))
    if name == "scan_level_b_momentum":
        return scan_level_b_momentum(a.get("market", "US"), a.get("max_history_checks", 30), a.get("max_results", 12))
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
