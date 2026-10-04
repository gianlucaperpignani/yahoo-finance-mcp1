# -*- coding: utf-8 -*-
"""
Level B - Radar dinamico per Yahoo Finance Storico / eToro.

Modulo tecnico. NON contiene regole finanziarie normative: il Mandato operativo
eToro resta l'unica fonte normativa.

A1 v0.2: Volume Anomaly Radar
- discovery tramite screener/movers Yahoo per regione;
- deduplica contro Level A e simboli esclusi;
- ranking preliminare usando volume corrente e medie-volume fornite dallo screener;
- conferma primaria sui migliori candidati con vero RVOL-at-time intraday cumulativo
  su barre 5m del server Yahoo Finance Storico;
- fallback prudente su media delle ultime 20 sedute + avanzamento seduta soltanto
  quando il profilo intraday non e disponibile.

Il modulo restituisce shortlist tecnica, non ticket operativi.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

A1_VERSION = "a1-volume-0.3-regional-rvol-at-time"

# Regioni Yahoo utili alla copertura USA + principali mercati europei.
# Un errore su una regione non blocca le altre: viene esposto in source_errors.
A1_REGIONS = {
    "US": ("US",),
    "EU": ("GB", "DE", "FR", "IT", "ES", "NL", "BE", "CH", "SE", "DK", "NO", "FI"),
}

A1_SCREENERS = ("most_actives",)

# Yahoo usa screener nazionali distinti per i market movers europei.
# Il solo parametro region sullo screener US most_actives non basta.
A1_REGION_SCREENERS = {
    "US": "most_actives",
    "GB": "most_actives_gb",
    "DE": "most_actives_de",
    "FR": "most_actives_fr",
    "IT": "most_actives_it",
    "ES": "most_actives_es",
    "NL": "most_actives_nl",
    "BE": "most_actives_be",
    "CH": "most_actives_ch",
    "SE": "most_actives_se",
    "DK": "most_actives_dk",
    "NO": "most_actives_no",
    "FI": "most_actives_fi",
}
A1_DISCOVERY_COUNT_PER_REGION = 25
A1_MAX_HISTORY_CHECKS = 24
A1_MAX_RESULTS = 12

# Filtri tecnici iniziali, volutamente non aggressivi: servono a discovery.
A1_MIN_PRICE = 1.0
A1_MIN_CURRENT_TURNOVER = 500_000.0  # valuta locale; solo pre-filtro grossolano
A1_MIN_RAW_RATIO_10D = 0.12          # volume corrente / media giornaliera 10d
A1_MIN_PACE_RVOL = 1.50              # soglia comune RVOL-at-time / fallback proxy
A1_STRONG_PACE_RVOL = 2.00
A1_VERY_STRONG_PACE_RVOL = 3.00
A1_INTRADAY_LOOKBACK = 10
A1_INTRADAY_MIN_SESSIONS = 5

# Titoli esplicitamente gia' esclusi dalla costruzione Level A per dipendenza
# prevalente dalla tesi crypto. Applicazione tecnica dell'esclusione del Mandato.
A1_LEVEL_B_EXCLUDED = {"IREN", "CORZ", "CLSK", "MARA", "RIOT"}

A1_EU_SUFFIXES = {
    "GB": (".L", ".AQ"),
    "DE": (".DE", ".F", ".MU", ".BE", ".DU", ".HM", ".SG"),
    "FR": (".PA", ".NX"),
    "IT": (".MI",),
    "ES": (".MC",),
    "NL": (".AS",),
    "BE": (".BR",),
    "CH": (".SW", ".VX"),
    "SE": (".ST",),
    "DK": (".CO",),
    "NO": (".OL",),
    "FI": (".HE",),
}


def _finite_number(value: Any) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _norm_symbol(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip().upper()
    return s or None


def _extract_quotes(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Supporta la forma standard finance.result[0].quotes dei predefined screeners."""
    try:
        result = document.get("finance", {}).get("result") or []
        if not result:
            return []
        quotes = result[0].get("quotes") or []
        return [q for q in quotes if isinstance(q, dict)]
    except Exception:
        return []


def _quote_record(q: Dict[str, Any], region: str, screener: str) -> Optional[Dict[str, Any]]:
    symbol = _norm_symbol(q.get("symbol"))
    if not symbol:
        return None
    price = _finite_number(q.get("regularMarketPrice"))
    volume = _finite_number(q.get("regularMarketVolume"))
    avg10 = _finite_number(q.get("averageDailyVolume10Day"))
    avg3m = _finite_number(q.get("averageDailyVolume3Month"))
    change_pct = _finite_number(q.get("regularMarketChangePercent"))
    market_cap = _finite_number(q.get("marketCap"))
    market_time = _finite_number(q.get("regularMarketTime"))

    return {
        "ticker": symbol,
        "name": q.get("shortName") or q.get("longName"),
        "region": str(q.get("region") or region).upper(),
        "source_region": str(q.get("region") or "").upper() or None,
        "requested_region": region,
        "screener": screener,
        "exchange": q.get("fullExchangeName") or q.get("exchange"),
        "quote_type": q.get("quoteType"),
        "currency": q.get("currency"),
        "market_state": q.get("marketState"),
        "price": price,
        "change_pct": change_pct,
        "current_volume": volume,
        "avg_volume_10d": avg10,
        "avg_volume_3m": avg3m,
        "market_cap": market_cap,
        "sector": q.get("sector") or q.get("sectorDisp"),
        "industry": q.get("industry") or q.get("industryDisp"),
        "regular_market_time": int(market_time) if market_time is not None else None,
    }


def _pre_score(rec: Dict[str, Any]) -> float:
    """Score solo per decidere chi merita il costo della serie storica."""
    volume = rec.get("current_volume") or 0.0
    avg10 = rec.get("avg_volume_10d") or rec.get("avg_volume_3m") or 0.0
    raw = volume / avg10 if avg10 > 0 else 0.0
    turnover = (rec.get("price") or 0.0) * volume
    # saturazione: evita che un singolo mega-volume monopolizzi la graduatoria.
    raw_part = min(raw, 3.0) / 3.0 * 70.0
    liq_part = min(math.log10(max(turnover, 1.0)) / 9.0, 1.0) * 30.0
    return round(raw_part + liq_part, 2)


def _session_progress_safe(session_progress_fn: Optional[Callable[[str], Optional[float]]], ticker: str) -> Optional[float]:
    if session_progress_fn is None:
        return None
    try:
        p = session_progress_fn(ticker)
        if p is None:
            return None
        p = float(p)
        if not math.isfinite(p):
            return None
        return min(max(p, 0.0), 1.0)
    except Exception:
        return None


def _history_volume_stats(payload: Dict[str, Any]) -> Optional[Dict[str, float]]:
    bars = payload.get("history") or []
    vols: List[float] = []
    for b in bars[-30:]:
        v = _finite_number(b.get("volume")) if isinstance(b, dict) else None
        if v is not None and v > 0:
            vols.append(v)
    if len(vols) < 15:
        return None
    sample = vols[-20:]
    mean20 = sum(sample) / len(sample)
    med20 = sorted(sample)[len(sample) // 2] if sample else None
    return {"avg_volume_20d": mean20, "median_volume_20d": med20, "n": len(sample)}


def _pace_ratio(current_volume: float, avg_daily_volume: float, progress: Optional[float]) -> Tuple[Optional[float], str]:
    if not current_volume or not avg_daily_volume or avg_daily_volume <= 0:
        return None, "MANCANTE"
    if progress is None:
        # Senza calendario non inventiamo l'avanzamento della seduta.
        return current_volume / avg_daily_volume, "RAW_NO_TIME_ADJUSTMENT"
    # Evita distorsioni enormi nei primissimi minuti. Non è un vero profilo intraday:
    # è una stima prudente della velocità di volume rispetto alla media giornaliera.
    effective_progress = max(progress, 0.15)
    return current_volume / (avg_daily_volume * effective_progress), "SESSION_PROGRESS_PROXY"


def _strength_label(pace_rvol: Optional[float]) -> str:
    if pace_rvol is None:
        return "N/D"
    if pace_rvol >= A1_VERY_STRONG_PACE_RVOL:
        return "MOLTO_FORTE"
    if pace_rvol >= A1_STRONG_PACE_RVOL:
        return "FORTE"
    if pace_rvol >= A1_MIN_PACE_RVOL:
        return "INTERESSANTE"
    return "DEBOLE"


def discover_a1_volume(
    *,
    market: str,
    yahoo_get: Callable[[str, Dict[str, Any], int], bytes],
    load_series: Callable[..., Dict[str, Any]],
    level_a: Iterable[str],
    excluded_symbols: Iterable[str],
    session_progress_fn: Optional[Callable[[str], Optional[float]]] = None,
    intraday_rvol_fn: Optional[Callable[[str, Optional[int]], Optional[Dict[str, Any]]]] = None,
    max_history_checks: int = A1_MAX_HISTORY_CHECKS,
    max_results: int = A1_MAX_RESULTS,
) -> Dict[str, Any]:
    """Esegue A1 e restituisce una shortlist tecnica compatta.

    yahoo_get deve essere la funzione serializzata/anti-429 del server.
    load_series deve essere il loader storico validato Yahoo Finance Storico.
    """
    market = str(market or "US").upper()
    if market not in A1_REGIONS:
        raise ValueError("market deve essere US o EU")

    level_a_set: Set[str] = {_norm_symbol(x) for x in level_a if _norm_symbol(x)}
    excluded_set: Set[str] = {_norm_symbol(x) for x in excluded_symbols if _norm_symbol(x)} | set(A1_LEVEL_B_EXCLUDED)

    discovered: Dict[str, Dict[str, Any]] = {}
    source_errors: List[Dict[str, str]] = []
    source_calls = 0

    for region in A1_REGIONS[market]:
        screeners = (A1_REGION_SCREENERS.get(region, "most_actives"),)
        for screener in screeners:
            params = {
                "formatted": "false",
                "lang": "en-US",
                "region": region,
                "scrIds": screener,
                "count": A1_DISCOVERY_COUNT_PER_REGION,
                "corsDomain": "finance.yahoo.com",
            }
            try:
                body = yahoo_get("/v1/finance/screener/predefined/saved", params, 1)
                source_calls += 1
                doc = json.loads(body)
                quotes = _extract_quotes(doc)
                if not quotes:
                    source_errors.append({"region": region, "screener": screener, "error": "nessuna quote"})
                    continue
                for q in quotes:
                    rec = _quote_record(q, region, screener)
                    if not rec:
                        continue
                    # Difesa geografica: Yahoo puo' ignorare region su alcuni
                    # predefined screener. Se la quote dichiara la regione,
                    # deve coincidere con quella richiesta.
                    source_region = str(rec.get("source_region") or "").upper()
                    if source_region and source_region != region:
                        continue
                    t = rec["ticker"]
                    # Ulteriore barriera EU solo quando Yahoo non espone la regione:
                    # il simbolo deve allora appartenere al suffisso locale.
                    if market == "EU" and not source_region:
                        suffixes = A1_EU_SUFFIXES.get(region, ())
                        if suffixes and not t.endswith(suffixes):
                            continue
                    if t in level_a_set or t in excluded_set:
                        continue
                    # A1 è per azioni/ETF. Fondi, future, crypto ecc. non passano.
                    qt = str(rec.get("quote_type") or "").upper()
                    if qt and qt not in ("EQUITY", "ETF"):
                        continue
                    price = rec.get("price")
                    volume = rec.get("current_volume")
                    if price is None or volume is None or price < A1_MIN_PRICE or volume <= 0:
                        continue
                    turnover = price * volume
                    if turnover < A1_MIN_CURRENT_TURNOVER:
                        continue
                    base_avg = rec.get("avg_volume_10d") or rec.get("avg_volume_3m")
                    raw_ratio = volume / base_avg if base_avg and base_avg > 0 else None
                    if raw_ratio is not None and raw_ratio < A1_MIN_RAW_RATIO_10D:
                        continue
                    rec["turnover_current_local"] = round(turnover, 0)
                    rec["raw_volume_ratio_screener"] = round(raw_ratio, 2) if raw_ratio is not None else None
                    rec["pre_score"] = _pre_score(rec)
                    old = discovered.get(t)
                    if old is None or rec["pre_score"] > old.get("pre_score", -1):
                        discovered[t] = rec
            except Exception as exc:
                source_calls += 1
                source_errors.append({"region": region, "screener": screener, "error": str(exc)[:180]})

    pre_ranked = sorted(discovered.values(), key=lambda x: (-x.get("pre_score", 0), x["ticker"]))
    to_check = pre_ranked[:max(1, min(int(max_history_checks), 60))]

    verified: List[Dict[str, Any]] = []
    history_errors: List[Dict[str, str]] = []
    intraday_ok = 0
    intraday_fallback = 0
    for rec in to_check:
        t = rec["ticker"]
        try:
            rvol = None
            rvol_method = None
            intraday_meta: Dict[str, Any] = {}

            # Metodo primario: vero RVOL-at-time cumulativo. Confronta il volume
            # accumulato della seduta target fino alla stessa barra con la media
            # cumulativa delle precedenti sedute comparabili.
            if intraday_rvol_fn is not None:
                try:
                    intraday = intraday_rvol_fn(t, rec.get("regular_market_time"))
                except Exception as exc:
                    intraday = None
                    history_errors.append({"ticker": t, "stage": "intraday", "error": str(exc)[:180]})
                if intraday and _finite_number(intraday.get("rvol_at_time")) is not None:
                    rvol = float(intraday["rvol_at_time"])
                    rvol_method = "TRUE_RVOL_AT_TIME_5M_CUMULATIVE"
                    intraday_meta = dict(intraday)
                    intraday_ok += 1

            # Fallback: mantiene operativa A1 se Yahoo intraday non restituisce
            # abbastanza sedute/barre comparabili. Non viene etichettato come vero RVOL.
            payload = None
            stats = None
            progress = None
            raw20 = None
            if rvol is None:
                intraday_fallback += 1
                payload = load_series(t, period="2y", interval="1d", user=False, allow_fetch=True, stale_ok=False)
                stats = _history_volume_stats(payload)
                if not stats:
                    history_errors.append({"ticker": t, "stage": "daily_fallback", "error": "storico volumi insufficiente"})
                    continue
                progress = _session_progress_safe(session_progress_fn, t)
                rvol, rvol_method = _pace_ratio(rec["current_volume"], stats["avg_volume_20d"], progress)
                raw20 = rec["current_volume"] / stats["avg_volume_20d"] if stats["avg_volume_20d"] > 0 else None

            if rvol is None or rvol < A1_MIN_PACE_RVOL:
                continue

            anomaly_part = min(rvol / A1_VERY_STRONG_PACE_RVOL, 1.0) * 75.0
            liq = rec["turnover_current_local"]
            liq_part = min(math.log10(max(liq, 1.0)) / 9.0, 1.0) * 20.0
            # Il vero intraday ottiene pieno data score; il fallback resta leggermente penalizzato.
            data_part = 5.0 if rvol_method == "TRUE_RVOL_AT_TIME_5M_CUMULATIVE" else 3.0
            a1_score = round(min(anomaly_part + liq_part + data_part, 100.0), 1)

            out = dict(rec)
            out.update({
                "rvol_at_time": round(rvol, 2),
                "rvol_pace": round(rvol, 2),  # compatibilita temporanea con consumer v0.1
                "rvol_method": rvol_method,
                "volume_strength": _strength_label(rvol),
                "a1_score": a1_score,
                "history_source": "Yahoo_Finance_Storico",
            })
            if rvol_method == "TRUE_RVOL_AT_TIME_5M_CUMULATIVE":
                out.update({
                    "intraday_interval": intraday_meta.get("interval"),
                    "intraday_target_session": intraday_meta.get("target_session"),
                    "intraday_cutoff_local": intraday_meta.get("cutoff_local"),
                    "intraday_current_cum_volume": intraday_meta.get("current_cum_volume"),
                    "intraday_expected_cum_volume": intraday_meta.get("expected_cum_volume"),
                    "intraday_median_cum_volume": intraday_meta.get("median_cum_volume"),
                    "intraday_comparison_sessions": intraday_meta.get("comparison_sessions"),
                    "intraday_bars_elapsed": intraday_meta.get("bars_elapsed"),
                    "intraday_last_bar_utc": intraday_meta.get("last_bar_utc"),
                    "history_as_of": intraday_meta.get("target_session"),
                    "history_data_current": intraday_meta.get("data_current"),
                })
            else:
                out.update({
                    "avg_volume_20d": round(stats["avg_volume_20d"], 0) if stats else None,
                    "median_volume_20d": round(stats["median_volume_20d"], 0) if stats else None,
                    "raw_current_vs_avg20": round(raw20, 2) if raw20 is not None else None,
                    "session_progress": round(progress, 3) if progress is not None else None,
                    "history_as_of": payload.get("coverage_end") if payload else None,
                    "history_data_current": payload.get("data_current") if payload else None,
                })
            verified.append(out)
        except Exception as exc:
            history_errors.append({"ticker": t, "stage": "verify", "error": str(exc)[:180]})

    verified.sort(key=lambda x: (-x["a1_score"], -x.get("rvol_at_time", 0), x["ticker"]))
    result_limit = max(1, min(int(max_results), 30))
    shortlist = verified[:result_limit]

    return {
        "antenna": "A1_VOLUME_ANOMALY",
        "a1_version": A1_VERSION,
        "market": market,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo predefined market movers + Yahoo_Finance_Storico",
        "method": "most_actives -> dedup Level A/exclusions -> pre-rank -> true 5m cumulative RVOL-at-time -> daily/session-progress fallback",
        "limitations": [
            "il vero RVOL-at-time richiede almeno 5 sedute intraday comparabili; in caso contrario usa fallback dichiarato",
            "la discovery dipende dalla copertura dei predefined screeners per regione",
            "A1 e' discovery tecnica: negoziabilita' eToro X1 e tesi completa vengono verificate dopo",
        ],
        "thresholds": {
            "min_price": A1_MIN_PRICE,
            "min_current_turnover_local": A1_MIN_CURRENT_TURNOVER,
            "min_raw_ratio_10d": A1_MIN_RAW_RATIO_10D,
            "min_rvol_pace": A1_MIN_PACE_RVOL,
            "strong_rvol_pace": A1_STRONG_PACE_RVOL,
            "very_strong_rvol_pace": A1_VERY_STRONG_PACE_RVOL,
            "intraday_interval": "5m",
            "intraday_lookback_sessions": A1_INTRADAY_LOOKBACK,
            "intraday_min_comparison_sessions": A1_INTRADAY_MIN_SESSIONS,
        },
        "coverage": {
            "regions_expected": len(A1_REGIONS[market]),
            "source_calls": source_calls,
            "unique_outside_level_a": len(discovered),
            "history_checked": len(to_check),
            "verified_anomalies": len(verified),
            "intraday_rvol_ok": intraday_ok,
            "intraday_fallback": intraday_fallback,
            "returned": len(shortlist),
        },
        "source_errors": source_errors[:20],
        "history_errors": history_errors[:20],
        "shortlist": shortlist,
    }

# -----------------------------------------------------------------------------
# A2 - Price & Momentum Radar
# -----------------------------------------------------------------------------
A2_VERSION = "a2-price-momentum-0.1"
A2_MAX_HISTORY_CHECKS = 30
A2_MAX_RESULTS = 12
A2_MIN_BARS = 80
A2_MIN_PRICE = 1.0
A2_MIN_TURNOVER = 500_000.0
A2_VOLUME_CONFIRM = 1.20

# Discovery: negli USA aggiungiamo i gainers; in Europa usiamo i radar nazionali
# che Yahoo espone realmente e lasciamo dichiarati gli eventuali 404 per i paesi
# senza predefined screener disponibile.
A2_US_SCREENERS = ("day_gainers", "most_actives")


def _rsi14_series(close):
    import pandas as pd
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(loss > 0, 100.0)


def _a2_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    import pandas as pd
    import numpy as np

    bars = payload.get("history") or []
    if len(bars) < A2_MIN_BARS:
        raise ValueError(f"Sedute concluse insufficienti ({len(bars)} < {A2_MIN_BARS})")

    df = pd.DataFrame(bars)
    c = pd.to_numeric(df["close"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    v = pd.to_numeric(df["volume"], errors="coerce")
    if c.isna().iloc[-1]:
        raise ValueError("close finale mancante")

    n = len(c)
    last = float(c.iloc[-1])
    prev_close = float(c.iloc[-2])
    s20 = float(c.iloc[-20:].mean()) if n >= 20 else None
    s50 = float(c.iloc[-50:].mean()) if n >= 50 else None
    s200 = float(c.iloc[-200:].mean()) if n >= 200 else None

    rsi = _rsi14_series(c)
    rsi_v = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None

    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atrs = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr = float(atrs.iloc[-1]) if pd.notna(atrs.iloc[-1]) else None

    def ret(k: int) -> Optional[float]:
        if len(c) <= k or c.iloc[-1-k] <= 0:
            return None
        return float(last / float(c.iloc[-1-k]) - 1)

    ret1, ret5, ret20 = ret(1), ret(5), ret(20)
    hi20_prior = float(h.iloc[-21:-1].max())
    hi60_prior = float(h.iloc[-61:-1].max()) if n >= 61 else hi20_prior
    lo10 = float(l.iloc[-10:].min())

    prev20_vol = v.iloc[-21:-1]
    avg20_vol = float(prev20_vol.mean()) if prev20_vol.notna().sum() >= 15 else None
    last_vol = float(v.iloc[-1]) if pd.notna(v.iloc[-1]) else None
    rvol_daily = last_vol / avg20_vol if (last_vol is not None and avg20_vol and avg20_vol > 0) else None
    volume_confirmed = bool(rvol_daily is not None and rvol_daily >= A2_VOLUME_CONFIRM)

    sma20_series = c.rolling(20).mean()
    sma50_series = c.rolling(50).mean()
    reclaim50_recent = False
    cross20_50_recent = False
    for j in range(max(51, n - 10), n):
        if j <= 0 or pd.isna(sma50_series.iloc[j]) or pd.isna(sma50_series.iloc[j-1]):
            continue
        if c.iloc[j-1] <= sma50_series.iloc[j-1] and c.iloc[j] > sma50_series.iloc[j]:
            reclaim50_recent = True
        if (pd.notna(sma20_series.iloc[j-1]) and pd.notna(sma20_series.iloc[j]) and
                sma20_series.iloc[j-1] <= sma50_series.iloc[j-1] and
                sma20_series.iloc[j] > sma50_series.iloc[j]):
            cross20_50_recent = True

    atr_safe = atr if atr and atr > 0 else max(last * 0.01, 1e-9)
    dist_s20_atr = (last - s20) / atr_safe if s20 else None
    dist_s50_atr = (last - s50) / atr_safe if s50 else None
    breakout_distance_atr = (last - hi20_prior) / atr_safe if hi20_prior else None

    # Anti-chase del Mandato: troppo distante dal trend/ATR oppure accelerazione 5d estrema.
    extended_reasons: List[str] = []
    if s20 is not None and last - s20 > 2.0 * atr_safe:
        extended_reasons.append("oltre_2ATR_da_SMA20")
    if ret5 is not None and ret5 > 0.10:
        extended_reasons.append("rialzo_5d_oltre_10pct")
    if hi20_prior and last > hi20_prior + 1.25 * atr_safe:
        extended_reasons.append("breakout_oltre_1.25ATR")
    extended = bool(extended_reasons)

    states: List[str] = []
    evidence: List[str] = []

    # BREAKOUT_EARLY: rottura dei massimi recenti ancora vicina al livello di trigger.
    if (last > hi20_prior and breakout_distance_atr is not None and 0 <= breakout_distance_atr <= 1.0
            and rsi_v is not None and 52 <= rsi_v <= 72):
        states.append("BREAKOUT_EARLY")
        evidence.append("close_sopra_massimo_20d")

    # AVVIO_TREND: condizioni minime esplicitamente richieste dal Mandato.
    if (s50 is not None and last > s50 and rsi_v is not None and 50 <= rsi_v <= 65
            and volume_confirmed and (reclaim50_recent or cross20_50_recent)):
        states.append("AVVIO_TREND")
        evidence.append("prezzo_sopra_SMA50_RSI50_65_volume_confermato")

    # PULLBACK_REBOUND: trend di fondo non compromesso, ritorno vicino alla media e reazione positiva.
    trend_ok = bool((s50 is not None and last > s50) or (s200 is not None and last > s200))
    near_trend = bool((dist_s20_atr is not None and -0.6 <= dist_s20_atr <= 0.9) or
                      (dist_s50_atr is not None and -0.4 <= dist_s50_atr <= 0.8))
    recent_pullback = bool(lo10 < prev_close and ret5 is not None and ret5 <= 0.04)
    rebound_day = bool(ret1 is not None and ret1 > 0 and last > prev_close)
    if trend_ok and near_trend and recent_pullback and rebound_day and rsi_v is not None and 38 <= rsi_v <= 62:
        states.append("PULLBACK_REBOUND")
        evidence.append("reazione_positiva_vicino_media_trend")

    # ACCELERATION: momentum in rafforzamento, ma non ancora automaticamente un segnale operativo.
    if (ret5 is not None and 0.03 <= ret5 <= 0.12 and ret20 is not None and ret20 > 0.04
            and rsi_v is not None and 55 <= rsi_v <= 75 and volume_confirmed):
        states.append("ACCELERATION")
        evidence.append("momentum_5d_20d_con_volume")

    # Score tecnico di discovery, non regola finanziaria normativa.
    base_by_state = {
        "BREAKOUT_EARLY": 78.0,
        "AVVIO_TREND": 82.0,
        "PULLBACK_REBOUND": 74.0,
        "ACCELERATION": 76.0,
    }
    base = max((base_by_state[s] for s in states), default=0.0)
    volume_bonus = min(max((rvol_daily or 0) - 1.0, 0.0) * 10.0, 12.0)
    multi_bonus = min(max(len(states) - 1, 0) * 4.0, 8.0)
    freshness_bonus = 2.0 if payload.get("data_current") else 0.0
    penalty = 25.0 if extended else 0.0
    score = max(0.0, min(base + volume_bonus + multi_bonus + freshness_bonus - penalty, 100.0))

    return {
        "as_of": bars[-1].get("date"),
        "data_current": payload.get("data_current"),
        "last_close": round(last, 6),
        "ret_1d_pct": round((ret1 or 0) * 100, 2) if ret1 is not None else None,
        "ret_5d_pct": round((ret5 or 0) * 100, 2) if ret5 is not None else None,
        "ret_20d_pct": round((ret20 or 0) * 100, 2) if ret20 is not None else None,
        "sma20": round(s20, 6) if s20 is not None else None,
        "sma50": round(s50, 6) if s50 is not None else None,
        "sma200": round(s200, 6) if s200 is not None else None,
        "rsi14": round(rsi_v, 1) if rsi_v is not None else None,
        "atr14": round(atr, 6) if atr is not None else None,
        "rvol_daily": round(rvol_daily, 2) if rvol_daily is not None else None,
        "volume_confirmed": volume_confirmed,
        "resistance_20d_prior": round(hi20_prior, 6),
        "resistance_60d_prior": round(hi60_prior, 6),
        "dist_sma20_atr": round(dist_s20_atr, 2) if dist_s20_atr is not None else None,
        "dist_sma50_atr": round(dist_s50_atr, 2) if dist_s50_atr is not None else None,
        "states": states,
        "evidence": evidence,
        "extended": extended,
        "extended_reasons": extended_reasons,
        "actionability": "NO_CHASE" if extended else ("CANDIDATE" if states else "NONE"),
        "a2_score": round(score, 1),
        "source": "Yahoo_Finance_Storico",
    }


def discover_a2_momentum(
    *,
    market: str,
    yahoo_get: Callable[[str, Dict[str, Any], int], bytes],
    load_series: Callable[..., Dict[str, Any]],
    level_a: Iterable[str],
    excluded_symbols: Iterable[str],
    max_history_checks: int = A2_MAX_HISTORY_CHECKS,
    max_results: int = A2_MAX_RESULTS,
) -> Dict[str, Any]:
    """A2: discovery prezzo/momentum fuori dal Level A + verifica su storico server."""
    market = str(market or "US").upper()
    if market not in A1_REGIONS:
        raise ValueError("market deve essere US o EU")

    level_a_set: Set[str] = {_norm_symbol(x) for x in level_a if _norm_symbol(x)}
    excluded_set: Set[str] = {_norm_symbol(x) for x in excluded_symbols if _norm_symbol(x)} | set(A1_LEVEL_B_EXCLUDED)

    discovered: Dict[str, Dict[str, Any]] = {}
    source_errors: List[Dict[str, str]] = []
    source_calls = 0

    for region in A1_REGIONS[market]:
        screeners = A2_US_SCREENERS if region == "US" else (A1_REGION_SCREENERS.get(region, "most_actives"),)
        for screener in screeners:
            params = {
                "formatted": "false", "lang": "en-US", "region": region,
                "scrIds": screener, "count": A1_DISCOVERY_COUNT_PER_REGION,
                "corsDomain": "finance.yahoo.com",
            }
            try:
                body = yahoo_get("/v1/finance/screener/predefined/saved", params, 1)
                source_calls += 1
                quotes = _extract_quotes(json.loads(body))
                if not quotes:
                    source_errors.append({"region": region, "screener": screener, "error": "nessuna quote"})
                    continue
                for q in quotes:
                    rec = _quote_record(q, region, screener)
                    if not rec:
                        continue
                    source_region = str(rec.get("source_region") or "").upper()
                    if source_region and source_region != region:
                        continue
                    t = rec["ticker"]
                    if market == "EU" and not source_region:
                        suffixes = A1_EU_SUFFIXES.get(region, ())
                        if suffixes and not t.endswith(suffixes):
                            continue
                    if t in level_a_set or t in excluded_set:
                        continue
                    qt = str(rec.get("quote_type") or "").upper()
                    if qt and qt not in ("EQUITY", "ETF"):
                        continue
                    price = rec.get("price")
                    volume = rec.get("current_volume")
                    if price is None or volume is None or price < A2_MIN_PRICE or volume <= 0:
                        continue
                    turnover = price * volume
                    if turnover < A2_MIN_TURNOVER:
                        continue
                    # Pre-score privilegia movimento + turnover ma resta solo filtro di costo.
                    chg = abs(rec.get("change_pct") or 0.0)
                    move_part = min(chg / 8.0, 1.0) * 65.0
                    liq_part = min(math.log10(max(turnover, 1.0)) / 9.0, 1.0) * 35.0
                    rec["turnover_current_local"] = round(turnover, 0)
                    rec["a2_pre_score"] = round(move_part + liq_part, 2)
                    old = discovered.get(t)
                    if old is None or rec["a2_pre_score"] > old.get("a2_pre_score", -1):
                        discovered[t] = rec
            except Exception as exc:
                source_calls += 1
                source_errors.append({"region": region, "screener": screener, "error": str(exc)[:180]})

    pre_ranked = sorted(discovered.values(), key=lambda x: (-x.get("a2_pre_score", 0), x["ticker"]))
    to_check = pre_ranked[:max(1, min(int(max_history_checks), 80))]

    candidates: List[Dict[str, Any]] = []
    extended_watch: List[Dict[str, Any]] = []
    history_errors: List[Dict[str, str]] = []
    for rec in to_check:
        t = rec["ticker"]
        try:
            payload = load_series(t, period="2y", interval="1d", user=False, allow_fetch=True, stale_ok=False)
            m = _a2_metrics(payload)
            if not m["states"]:
                continue
            out = dict(rec)
            out.update(m)
            if m["extended"]:
                extended_watch.append(out)
            else:
                candidates.append(out)
        except Exception as exc:
            history_errors.append({"ticker": t, "error": str(exc)[:180]})

    candidates.sort(key=lambda x: (-x["a2_score"], -abs(x.get("change_pct") or 0), x["ticker"]))
    extended_watch.sort(key=lambda x: (-x["a2_score"], x["ticker"]))
    lim = max(1, min(int(max_results), 30))

    return {
        "antenna": "A2_PRICE_MOMENTUM",
        "a2_version": A2_VERSION,
        "market": market,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo predefined movers + Yahoo_Finance_Storico",
        "method": "movers/gainers -> dedup Level A/exclusions -> daily technical verification -> anti-chase ESTESO",
        "states": ["BREAKOUT_EARLY", "AVVIO_TREND", "PULLBACK_REBOUND", "ACCELERATION"],
        "anti_chase": "ESTESO => NO_CHASE; resta solo in extended_watchlist",
        "thresholds": {
            "min_price": A2_MIN_PRICE,
            "min_turnover_local": A2_MIN_TURNOVER,
            "volume_confirmation_rvol_daily": A2_VOLUME_CONFIRM,
            "avvio_trend_rsi": "50-65",
            "extended_sma20_atr": 2.0,
            "extended_ret5_pct": 10.0,
        },
        "coverage": {
            "regions_expected": len(A1_REGIONS[market]),
            "source_calls": source_calls,
            "unique_outside_level_a": len(discovered),
            "history_checked": len(to_check),
            "qualified_non_extended": len(candidates),
            "extended_no_chase": len(extended_watch),
            "returned": min(len(candidates), lim),
        },
        "source_errors": source_errors[:20],
        "history_errors": history_errors[:20],
        "shortlist": candidates[:lim],
        "extended_watchlist": extended_watch[:min(6, lim)],
    }

# =============================================================================
# A3 - Catalyst & News Radar
# =============================================================================
A3_VERSION = "a3-catalyst-news-0.3-geo-filter-fix"
A3_MAX_RESULTS = 12
A3_NEWS_PER_QUERY = 12
A3_LOOKBACK_HOURS = 96

# Query tematiche: discovery indipendente da A1/A2, per cogliere catalizzatori
# anche prima della reazione di prezzo/volume. I termini sono volutamente ampi;
# la classificazione e lo scoring avvengono dopo sul titolo della notizia.
A3_QUERIES_US = (
    "earnings guidance",
    "FDA approval clinical trial",
    "contract order partnership",
    "acquisition merger takeover",
    "buyback repurchase dividend",
    "analyst upgrade downgrade",
    "CEO CFO management",
    "regulatory investigation restructuring",
    "product launch technology",
)
A3_QUERIES_EU = (
    # Global thematic searches: Yahoo US search has broader international news
    # coverage than a single GB region. EU symbols are filtered afterwards.
    "earnings guidance",
    "regulatory approval clinical trial",
    "contract order partnership",
    "acquire merger takeover tender offer",
    "buyback repurchase dividend",
    "analyst upgrade downgrade",
    "CEO CFO management",
    "regulatory investigation restructuring",
    "product launch technology",
    # Targeted European discovery to improve recall outside US-centric news.
    "UK company earnings contract acquisition",
    "Germany company earnings contract acquisition",
    "France company earnings contract acquisition",
    "Italy Spain company earnings contract acquisition",
)

_A3_PRIMARY_PUBLISHERS = (
    "business wire", "globenewswire", "pr newswire", "accesswire",
    "sec", "company press release", "investor relations",
)
_A3_TOP_PUBLISHERS = ("reuters", "associated press", "ap news", "bloomberg")
_A3_STRONG_PUBLISHERS = (
    "financial times", "wall street journal", "wsj", "cnbc", "barron's",
    "marketwatch", "dow jones", "the fly",
)
_A3_WEAK_PUBLISHERS = ("motley fool", "investorplace", "zacks")
_A3_CANONICAL = {"BRK-B": "BRK.B"}


def _a3_canonical_symbol(symbol: Any) -> Optional[str]:
    s = _norm_symbol(symbol)
    if not s:
        return None
    return _A3_CANONICAL.get(s, s)


def _a3_is_eu_symbol(symbol: str) -> bool:
    s = symbol.upper()
    suffixes = tuple(x for vals in A1_EU_SUFFIXES.values() for x in vals)
    return s.endswith(suffixes)


def _a3_is_us_symbol(symbol: str) -> bool:
    """Conservative US symbol gate for news-discovery relatedTickers.

    Rejects numeric/non-US exchange symbols (e.g. Japanese 5801 / 5801.T) while
    allowing normal US equity symbols and class tickers represented with a dash.
    Final tradability is still verified downstream on eToro.
    """
    s = _a3_canonical_symbol(symbol)
    if not s or _a3_is_eu_symbol(s):
        return False
    if s.isdigit():
        return False
    # US class tickers may normalize to BRK.B/BF.B; foreign suffixes (.T, .TO, .AX, etc.) are rejected.
    return bool(re.fullmatch(r"[A-Z]{1,7}(?:[.-][A-Z])?", s))


def _a3_extract_news(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = doc.get("news") or []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        provider = content.get("provider") if isinstance(content.get("provider"), dict) else {}
        canonical = content.get("canonicalUrl") if isinstance(content.get("canonicalUrl"), dict) else {}
        clickthrough = content.get("clickThroughUrl") if isinstance(content.get("clickThroughUrl"), dict) else {}

        title = item.get("title") or content.get("title")
        publisher = item.get("publisher") or provider.get("displayName") or provider.get("name")
        link = item.get("link") or canonical.get("url") or clickthrough.get("url")
        ts = item.get("providerPublishTime") or item.get("published_at")
        if ts is None:
            pub_iso = content.get("pubDate") or content.get("displayTime")
            if isinstance(pub_iso, str):
                try:
                    ts = datetime.fromisoformat(pub_iso.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = None

        related = item.get("relatedTickers") or content.get("relatedTickers") or []
        if not related and isinstance(content.get("finance"), dict):
            fin = content["finance"]
            related = fin.get("stockTickers") or fin.get("tickers") or []
        clean_related: List[str] = []
        if isinstance(related, (list, tuple)):
            for x in related:
                if isinstance(x, dict):
                    x = x.get("symbol") or x.get("ticker")
                c = _a3_canonical_symbol(x)
                if c and c not in clean_related:
                    clean_related.append(c)

        if isinstance(title, str) and title.strip():
            out.append({
                "title": title.strip(),
                "publisher": str(publisher or "N/D").strip(),
                "url": link if isinstance(link, str) else None,
                "published_ts": float(ts) if _finite_number(ts) is not None else None,
                "related_tickers": clean_related,
            })
    return out


def _a3_is_corporate_ma_title(title: str) -> bool:
    """Conservative M&A detector.

    The word ``acquisition`` alone is NOT enough: it is often used for
    operating/property/customer acquisitions and produced false positives.
    """
    t = " " + title.lower() + " "
    operating_context = (
        "acquisitions guidance", "acquisition guidance", "gross acquisitions",
        "property acquisition", "property acquisitions", "asset acquisition",
        "asset acquisitions", "portfolio acquisition", "portfolio acquisitions",
        "customer acquisition", "user acquisition", "land acquisition",
        "bolt-on acquisition", "store acquisition",
    )
    if any(x in t for x in operating_context):
        return False
    explicit = (
        "agrees to acquire", "agree to acquire", "to acquire ", "will acquire ",
        "acquisition of ", "merger agreement", "merger with ", "merge with ",
        "takeover", "buyout", "tender offer", "strategic alternatives",
        "bid for ", "offer for ", "sale of the company", "sale of company",
        "acquired by ", "acquires ", "merger",
    )
    return any(x in t for x in explicit)


def _a3_category(title: str) -> Tuple[str, float]:
    t = title.lower()
    if _a3_is_corporate_ma_title(title):
        return "M&A", 95.0
    groups = [
        ("FDA_REGULATORY", 90.0, ("fda", "ema", "regulatory approval", "clinical trial", "phase 2", "phase 3", "drug approval")),
        ("EARNINGS_GUIDANCE", 85.0, ("earnings", "guidance", "revenue", "eps", "profit warning", "outlook", "preliminary results")),
        ("CONTRACT_ORDER", 80.0, ("contract", "order", "awarded", "award", "partnership", "supply agreement", "customer deal")),
        ("LEGAL_RESTRUCTURE", 78.0, ("restructuring", "bankruptcy", "chapter 11", "investigation", "lawsuit", "settlement", "antitrust")),
        ("CAPITAL_RETURN", 68.0, ("buyback", "repurchase", "dividend", "capital return")),
        ("PRODUCT_TECH", 62.0, ("product launch", "launches", "unveils", "new product", "platform", "technology")),
        ("MANAGEMENT", 58.0, (" ceo", "cfo", "chief executive", "chief financial", "appoints", "resigns", "steps down")),
        ("ANALYST", 48.0, ("upgrade", "downgrade", "price target", "rating", "initiates coverage")),
    ]
    padded = " " + t
    for name, importance, terms in groups:
        if any(term in padded for term in terms):
            return name, importance
    return "OTHER_CATALYST", 45.0


def _a3_source_quality(publisher: str) -> Tuple[float, bool]:
    p = (publisher or "").lower()
    if any(x in p for x in _A3_PRIMARY_PUBLISHERS):
        return 92.0, True
    if any(x in p for x in _A3_TOP_PUBLISHERS):
        return 96.0, False
    if any(x in p for x in _A3_STRONG_PUBLISHERS):
        return 86.0, False
    if any(x in p for x in _A3_WEAK_PUBLISHERS):
        return 52.0, False
    if "benzinga" in p or "seeking alpha" in p:
        return 62.0, False
    return 68.0, False


def _a3_freshness(published_ts: Optional[float], now_ts: float) -> Tuple[float, Optional[float]]:
    if not published_ts:
        return 35.0, None
    age_h = max(0.0, (now_ts - published_ts) / 3600.0)
    if age_h <= 2:
        score = 100.0
    elif age_h <= 6:
        score = 92.0
    elif age_h <= 24:
        score = 80.0
    elif age_h <= 48:
        score = 62.0
    elif age_h <= 72:
        score = 48.0
    elif age_h <= A3_LOOKBACK_HOURS:
        score = 35.0
    else:
        score = 15.0
    return score, age_h


def _a3_surprise(title: str) -> float:
    t = title.lower()
    strong = ("raises guidance", "cuts guidance", "beats estimates", "misses estimates",
              "wins contract", "awarded contract", "approved", "approval", "rejected",
              "halts", "terminates", "record revenue", "profit warning")
    medium = ("beats", "misses", "raises", "cuts", "surges", "plunges", "unexpected",
              "larger than expected", "smaller than expected")
    if any(x in t for x in strong):
        return 90.0
    if any(x in t for x in medium):
        return 72.0
    return 55.0


def _a3_impact(category: str, title: str) -> float:
    base = {
        "M&A": 92.0, "FDA_REGULATORY": 88.0, "EARNINGS_GUIDANCE": 86.0,
        "CONTRACT_ORDER": 78.0, "LEGAL_RESTRUCTURE": 82.0,
        "CAPITAL_RETURN": 66.0, "PRODUCT_TECH": 62.0,
        "MANAGEMENT": 56.0, "ANALYST": 50.0, "OTHER_CATALYST": 45.0,
    }.get(category, 45.0)
    t = title.lower()
    if any(x in t for x in ("billion", "bn ", "$1b", "€1b", "multi-year", "definitive agreement")):
        base += 7.0
    return min(base, 100.0)


def _a3_deal_status(category: str, title: str, source_primary: bool) -> Tuple[bool, str, bool]:
    if category != "M&A":
        return False, "NON_MA", True
    t = title.lower()
    rumor_terms = ("rumor", "reportedly", "considering", "explores", "exploring", "weighs",
                   "could", "may ", "in talks", "sources say", "mulls", "potential bid",
                   "said to be interested", "considering a bid")
    confirmed_terms = ("definitive agreement", "agrees to acquire", "to acquire ", "will acquire ",
                       "merger agreement", "launches tender offer", "completes acquisition",
                       "acquired by ", "acquires ")
    rumor = any(x in t for x in rumor_terms) and not any(x in t for x in confirmed_terms)
    official_primary = source_primary and any(x in t for x in confirmed_terms)
    if rumor:
        return True, "RUMOR_OR_SPECULATION", False
    if official_primary:
        return False, "OFFICIAL_PRIMARY_EQUIVALENT", True
    if any(x in t for x in confirmed_terms):
        return False, "REPORTED_CONFIRMED_NEEDS_PRIMARY_CHECK", False
    return False, "M&A_NEEDS_PRIMARY_CHECK", False


def discover_a3_catalyst(
    *,
    market: str,
    yahoo_get: Callable[[str, Dict[str, Any], int], bytes],
    level_a: Iterable[str],
    excluded_symbols: Iterable[str],
    max_results: int = A3_MAX_RESULTS,
) -> Dict[str, Any]:
    """A3: discovery di catalizzatori/notizie fuori dal Level A.

    Non produce ticket. Rumor/M&A senza fonte primaria restano WATCH_ONLY.
    """
    market = str(market or "US").upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US o EU")

    level_a_set = {_a3_canonical_symbol(x) for x in level_a if _a3_canonical_symbol(x)}
    excluded_set = {_a3_canonical_symbol(x) for x in excluded_symbols if _a3_canonical_symbol(x)} | set(A1_LEVEL_B_EXCLUDED)
    queries = A3_QUERIES_US if market == "US" else A3_QUERIES_EU
    region = "US"
    now_ts = datetime.now(timezone.utc).timestamp()

    source_calls = 0
    source_errors: List[Dict[str, str]] = []
    articles_seen: Set[str] = set()
    ticker_items: Dict[str, List[Dict[str, Any]]] = {}

    for q in queries:
        params = {
            "q": q,
            "quotesCount": 0,
            "newsCount": A3_NEWS_PER_QUERY,
            "listsCount": 0,
            "enableFuzzyQuery": "false",
            "quotesQueryId": "tss_match_phrase_query",
            "newsQueryId": "news_cie_vespa",
            "enableCb": "false",
            "enableNavLinks": "false",
            "enableResearchReports": "false",
            "recommendedCount": 0,
            "lang": "en-US",
            "region": region,
        }
        try:
            body = yahoo_get("/v1/finance/search", params, 1)
            source_calls += 1
            doc = json.loads(body)
            for art in _a3_extract_news(doc):
                key = (art.get("url") or art.get("title") or "").strip().lower()
                if not key or key in articles_seen:
                    continue
                articles_seen.add(key)
                fresh_score, age_h = _a3_freshness(art.get("published_ts"), now_ts)
                if age_h is not None and age_h > A3_LOOKBACK_HOURS:
                    continue
                title = art["title"]
                category, importance = _a3_category(title)
                source_quality, source_primary = _a3_source_quality(art.get("publisher") or "")
                surprise = _a3_surprise(title)
                impact = _a3_impact(category, title)
                score = (0.30 * importance + 0.25 * source_quality + 0.20 * fresh_score +
                         0.15 * surprise + 0.10 * impact)
                rumor, confirmation, deal_ticket_eligible = _a3_deal_status(category, title, source_primary)

                for ticker in art.get("related_tickers") or []:
                    ticker = _a3_canonical_symbol(ticker)
                    if not ticker or ticker in level_a_set or ticker in excluded_set:
                        continue
                    is_eu = _a3_is_eu_symbol(ticker)
                    if market == "EU" and not is_eu:
                        continue
                    if market == "US" and not _a3_is_us_symbol(ticker):
                        continue
                    ticker_items.setdefault(ticker, []).append({
                        "category": category,
                        "catalyst_score_raw": round(score, 1),
                        "importance": importance,
                        "source_quality": source_quality,
                        "freshness": fresh_score,
                        "surprise": surprise,
                        "economic_impact": impact,
                        "title": title,
                        "publisher": art.get("publisher"),
                        "published_utc": (datetime.fromtimestamp(art["published_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                          if art.get("published_ts") else None),
                        "age_hours": round(age_h, 1) if age_h is not None else None,
                        "url": art.get("url"),
                        "source_primary": source_primary,
                        "rumor": rumor,
                        "confirmation_status": confirmation,
                        "deal_ticket_eligible": deal_ticket_eligible,
                    })
        except Exception as exc:
            source_calls += 1
            source_errors.append({"query": q, "error": str(exc)[:180]})

    ranked: List[Dict[str, Any]] = []
    rumor_watch: List[Dict[str, Any]] = []
    for ticker, items in ticker_items.items():
        items.sort(key=lambda x: (-x["catalyst_score_raw"], x.get("age_hours") if x.get("age_hours") is not None else 9999))
        top = items[0]
        publishers = {str(x.get("publisher") or "").strip().lower() for x in items if x.get("publisher")}
        diversity_bonus = min(max(len(publishers) - 1, 0) * 3.0, 9.0)
        final_score = min(100.0, top["catalyst_score_raw"] + diversity_bonus)
        categories = []
        for x in items:
            if x["category"] not in categories:
                categories.append(x["category"])

        any_rumor = any(x.get("rumor") for x in items)
        any_primary_deal = any(x.get("deal_ticket_eligible") for x in items if x["category"] == "M&A")
        has_ma = "M&A" in categories
        actionability = "WATCH_ONLY" if has_ma and not any_primary_deal else "CATALYST_CANDIDATE"

        rec = {
            "ticker": ticker,
            "catalyst_score": round(final_score, 1),
            "categories": categories[:4],
            "article_count": len(items),
            "independent_publishers": len(publishers),
            "actionability": actionability,
            "rumor_present": any_rumor,
            "ma_primary_confirmed": any_primary_deal,
            "top_evidence": [{
                "category": x["category"],
                "title": x["title"],
                "publisher": x["publisher"],
                "published_utc": x["published_utc"],
                "age_hours": x["age_hours"],
                "score": x["catalyst_score_raw"],
                "confirmation_status": x["confirmation_status"],
                "url": x["url"],
            } for x in items[:3]],
        }
        if actionability == "WATCH_ONLY":
            rumor_watch.append(rec)
        else:
            ranked.append(rec)

    ranked.sort(key=lambda x: (-x["catalyst_score"], -x["article_count"], x["ticker"]))
    rumor_watch.sort(key=lambda x: (-x["catalyst_score"], x["ticker"]))
    lim = max(1, min(int(max_results), 30))

    return {
        "antenna": "A3_CATALYST_NEWS",
        "a3_version": A3_VERSION,
        "market": market,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance Search/News",
        "method": "global thematic news discovery + EU-targeted queries -> related tickers -> geographic filter -> dedup Level A/exclusions -> catalyst classification/scoring",
        "score_weights": {"importance": 30, "source_reliability": 25, "freshness": 20, "surprise": 15, "economic_impact": 10},
        "ma_rule": "rumor/speculation or M&A without primary-equivalent confirmation => WATCH_ONLY; never trade-ticket eligible from A3 alone",
        "lookback_hours": A3_LOOKBACK_HOURS,
        "coverage": {
            "queries_expected": len(queries),
            "source_calls": source_calls,
            "unique_articles": len(articles_seen),
            "unique_tickers_outside_level_a": len(ticker_items),
            "catalyst_candidates": len(ranked),
            "rumor_watch": len(rumor_watch),
            "returned": min(len(ranked), lim),
        },
        "source_errors": source_errors[:20],
        "shortlist": ranked[:lim],
        "rumor_watchlist": rumor_watch[:min(8, lim)],
        "limitations": [
            "A3 usa titoli/metadati Yahoo per discovery; la verifica finale del catalizzatore va fatta su fonte ufficiale/primaria o fonte finanziaria autorevole",
            "relatedTickers Yahoo puo' essere incompleto: A3 non sostituisce la verifica finale ChatGPT+web",
            "A3 non produce ticket operativi e non verifica negoziabilita' eToro X1/spread",
        ],
    }




# =============================================================================
# A4 - Attention / Special Situations Radar
# =============================================================================
A4_VERSION = "a4-attention-special-situations-0.1"
A4_MAX_RESULTS = 12
A4_NEWS_PER_QUERY = 12
A4_LOOKBACK_HOURS = 120

A4_QUERIES_US = (
    "IPO new listing begins trading",
    "insider buying director buys shares",
    "13D 13G activist investor stake",
    "high short interest short squeeze catalyst",
    "spin off spinoff separation",
    "restructuring strategic alternatives",
    "takeover speculation acquisition talks",
    "unusual attention stock surges volume",
)
A4_QUERIES_EU = (
    "Europe IPO new listing begins trading",
    "UK insider buying director buys shares",
    "Europe activist investor stake",
    "Europe short interest squeeze catalyst",
    "Europe spin off separation",
    "Europe restructuring strategic alternatives",
    "Europe takeover speculation acquisition talks",
    "European shares unusual attention surge",
)


def _a4_category(title: str) -> Tuple[str, float, bool]:
    t = " " + str(title or "").lower() + " "
    groups = [
        ("IPO_NEW_LISTING", 92.0, False, (" ipo ", "initial public offering", "new listing", "begins trading", "debut", "lists on ", "listed on ")),
        ("INSIDER_BUYING", 86.0, False, ("insider buying", "insider buys", "director buys", "ceo buys", "cfo buys", "purchases shares", "buys shares")),
        ("ACTIVIST_13D_13G", 90.0, False, ("13d", "13g", "activist investor", "activist stake", "takes stake", "builds stake")),
        ("SHORT_INTEREST", 78.0, True, ("short interest", "short squeeze", "heavily shorted", "short sellers", "short seller")),
        ("SPINOFF", 84.0, False, ("spin-off", "spinoff", "spin off", "separation", "separates business", "demerger")),
        ("RESTRUCTURING", 82.0, False, ("restructuring", "restructure", "strategic alternatives", "turnaround plan", "asset sale", "cost-cutting plan")),
        ("MA_SPECULATION", 88.0, True, ("takeover speculation", "takeover interest", "acquisition talks", "merger talks", "potential bid", "considering a bid", "mulls bid", "reportedly interested", "sale process")),
        ("ATTENTION_SPIKE", 66.0, True, ("unusual attention", "retail traders", "most active", "surges on volume", "volume spike", "trading frenzy", "goes viral")),
    ]
    for name, importance, watch_only, terms in groups:
        if any(term in t for term in terms):
            return name, importance, watch_only
    return "OTHER_SPECIAL_SITUATION", 45.0, True


def _a4_freshness(published_ts: Optional[float], now_ts: float) -> Tuple[float, Optional[float]]:
    if not published_ts:
        return 30.0, None
    age_h = max(0.0, (now_ts - published_ts) / 3600.0)
    if age_h <= 6: score = 100.0
    elif age_h <= 24: score = 86.0
    elif age_h <= 48: score = 68.0
    elif age_h <= 72: score = 52.0
    elif age_h <= A4_LOOKBACK_HOURS: score = 36.0
    else: score = 15.0
    return score, age_h


def discover_a4_attention(
    *,
    market: str,
    yahoo_get: Callable[[str, Dict[str, Any], int], bytes],
    level_a: Iterable[str],
    excluded_symbols: Iterable[str],
    max_results: int = A4_MAX_RESULTS,
) -> Dict[str, Any]:
    """A4: attention e special situations fuori dal Level A. Solo discovery/WATCH."""
    market = str(market or "US").upper()
    if market not in ("US", "EU"):
        raise ValueError("market deve essere US o EU")
    lim = max(1, min(int(max_results or A4_MAX_RESULTS), 30))
    level_a_set = {_a3_canonical_symbol(x) for x in level_a if _a3_canonical_symbol(x)}
    excluded_set = {_a3_canonical_symbol(x) for x in excluded_symbols if _a3_canonical_symbol(x)} | set(A1_LEVEL_B_EXCLUDED)
    queries = A4_QUERIES_US if market == "US" else A4_QUERIES_EU
    now_ts = datetime.now(timezone.utc).timestamp()
    source_calls = 0
    source_errors: List[Dict[str, str]] = []
    articles_seen: Set[str] = set()
    ticker_items: Dict[str, List[Dict[str, Any]]] = {}

    for q in queries:
        params = {
            "q": q, "quotesCount": 0, "newsCount": A4_NEWS_PER_QUERY,
            "enableFuzzyQuery": "false", "quotesQueryId": "tss_match_phrase_query",
            "multiQuoteQueryId": "multi_quote_single_token_query",
            "newsQueryId": "news_cie_vespa", "enableCb": "true",
            "enableNavLinks": "false", "enableEnhancedTrivialQuery": "true",
            "region": "US", "lang": "en-US",
        }
        try:
            body = yahoo_get("/v1/finance/search", params, 1)
            source_calls += 1
            doc = json.loads(body)
            for art in _a3_extract_news(doc):
                key = str(art.get("url") or art.get("title") or "").strip().lower()
                if not key or key in articles_seen:
                    continue
                articles_seen.add(key)
                fresh_score, age_h = _a4_freshness(art.get("published_ts"), now_ts)
                if age_h is not None and age_h > A4_LOOKBACK_HOURS:
                    continue
                category, importance, category_watch = _a4_category(art.get("title") or "")
                if category == "OTHER_SPECIAL_SITUATION":
                    continue
                source_score, source_primary = _a3_source_quality(art.get("publisher") or "")
                for raw_t in art.get("related_tickers") or []:
                    ticker = _a3_canonical_symbol(raw_t)
                    if not ticker or ticker in level_a_set or ticker in excluded_set:
                        continue
                    if market == "EU" and not _a3_is_eu_symbol(ticker):
                        continue
                    if market == "US" and _a3_is_eu_symbol(ticker):
                        continue
                    # US special-situations radar evita indici/futures/FX e simboli con suffissi esteri.
                    if ticker.startswith("^") or ticker.endswith("=X") or ticker.endswith("=F"):
                        continue
                    item = {
                        "category": category, "importance": importance,
                        "title": art.get("title"), "publisher": art.get("publisher"),
                        "published_ts": art.get("published_ts"),
                        "published_utc": (datetime.fromtimestamp(art["published_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                          if art.get("published_ts") else None),
                        "age_hours": round(age_h, 1) if age_h is not None else None,
                        "url": art.get("url"), "source_score": source_score,
                        "source_primary": source_primary, "category_watch": category_watch,
                        "freshness_score": fresh_score,
                    }
                    ticker_items.setdefault(ticker, []).append(item)
        except Exception as exc:
            source_calls += 1
            source_errors.append({"query": q, "error": str(exc)[:180]})

    rows: List[Dict[str, Any]] = []
    for ticker, items in ticker_items.items():
        items.sort(key=lambda x: (-(x["importance"] * 0.45 + x["source_score"] * 0.25 + x["freshness_score"] * 0.30),
                                  x.get("age_hours") if x.get("age_hours") is not None else 9999))
        top = items[0]
        publishers = {str(x.get("publisher") or "").strip().lower() for x in items if x.get("publisher")}
        categories: List[str] = []
        for x in items:
            if x["category"] not in categories:
                categories.append(x["category"])
        attention_score = min(100.0, 35.0 + min(len(items), 5) * 8.0 + min(len(publishers), 4) * 6.0)
        confirmation = 100.0 if any(x.get("source_primary") for x in items) else (72.0 if len(publishers) >= 2 else 50.0)
        score = (top["importance"] * 0.35 + top["source_score"] * 0.20 + top["freshness_score"] * 0.20 +
                 attention_score * 0.15 + confirmation * 0.10)
        watch = any(x.get("category_watch") for x in items) or not any(x.get("source_primary") for x in items)
        rows.append({
            "ticker": ticker, "attention_score": round(min(score, 100.0), 1),
            "categories": categories[:4], "article_count": len(items),
            "independent_publishers": len(publishers),
            "actionability": "WATCH_ONLY" if watch else "SPECIAL_SITUATION_CANDIDATE",
            "primary_source_present": any(x.get("source_primary") for x in items),
            "top_evidence": [{
                "category": x["category"], "title": x["title"], "publisher": x["publisher"],
                "published_utc": x["published_utc"], "age_hours": x["age_hours"], "url": x["url"],
            } for x in items[:3]],
        })

    rows.sort(key=lambda x: (-x["attention_score"], -x["article_count"], x["ticker"]))
    return {
        "antenna": "A4_ATTENTION_SPECIAL_SITUATIONS",
        "a4_version": A4_VERSION, "market": market,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance Search/News",
        "method": "special-situation thematic discovery -> related tickers -> geographic filter -> dedup Level A/exclusions -> attention scoring",
        "lookback_hours": A4_LOOKBACK_HOURS,
        "coverage": {
            "queries_expected": len(queries), "source_calls": source_calls,
            "unique_articles": len(articles_seen), "unique_tickers_outside_level_a": len(ticker_items),
            "returned": min(len(rows), lim),
        },
        "source_errors": source_errors[:20], "shortlist": rows[:lim],
        "limitations": [
            "A4 e un radar di discovery: attention, short interest o rumor non costituiscono segnali BUY",
            "IPO/new listing e 13D/13G sono rilevati da news/metadati; la verifica finale richiede fonte primaria o filing ufficiale",
            "M&A speculation resta WATCH_ONLY fino a conferma primaria secondo il Mandato",
            "A4 non verifica negoziabilita eToro X1, spread o sizing",
        ],
    }

# -----------------------------------------------------------------------------
# A5 - Sector Rotation Radar
# -----------------------------------------------------------------------------
A5_VERSION = "a5-sector-rotation-0.2-eu-map-sector-enrichment"

A5_SECTOR_PROXIES = {
    "US": {
        "TECHNOLOGY": "XLK", "SEMICONDUCTORS": "SOXX", "FINANCIALS": "XLF",
        "INDUSTRIALS": "XLI", "HEALTHCARE": "XLV", "CONSUMER_DISCRETIONARY": "XLY",
        "CONSUMER_STAPLES": "XLP", "ENERGY": "XLE", "UTILITIES": "XLU",
        "MATERIALS": "XLB", "REAL_ESTATE": "XLRE", "COMMUNICATION": "XLC",
    },
    "EU": {
        "BANKS": "EXV1.DE",
        "TELECOMMUNICATIONS": "EXV2.DE",
        "TECHNOLOGY": "EXV3.DE",
        "HEALTHCARE": "EXV4.DE",
        "AUTOMOBILES": "EXV5.DE",
        "BASIC_RESOURCES": "EXV6.DE",
        "CHEMICALS": "EXV7.DE",
        "CONSTRUCTION_MATERIALS": "EXV8.DE",
        "TRAVEL_LEISURE": "EXV9.DE",
        "OIL_GAS": "EXH1.DE",
        "FINANCIAL_SERVICES": "EXH2.DE",
        "FOOD_BEVERAGE": "EXH3.DE",
        "INDUSTRIALS": "EXH4.DE",
        "INSURANCE": "EXH5.DE",
        "MEDIA": "EXH6.DE",
        "PERSONAL_HOUSEHOLD": "EXH7.DE",
        "RETAIL": "EXH8.DE",
        "UTILITIES": "EXH9.DE",
        "REAL_ESTATE": "EXI5.DE",
    },
}
A5_BENCHMARKS = {"US": "SPY", "EU": "EXSA.DE"}


def _a5_closes(payload: Dict[str, Any]) -> List[float]:
    out: List[float] = []
    for b in (payload.get("history") or []):
        if not isinstance(b, dict):
            continue
        x = _finite_number(b.get("close"))
        if x is not None and x > 0:
            out.append(x)
    return out


def _a5_return(closes: Sequence[float], days: int) -> Optional[float]:
    if len(closes) <= days:
        return None
    a, b = closes[-days - 1], closes[-1]
    if not a or a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def _a5_classify(r1: float, r5: float, r20: float, rs1: float, rs5: float, rs20: float) -> str:
    # ESTESO prevale: forza elevata ma rischio di inseguire un movimento maturo.
    if r20 >= 12.0 and rs20 >= 6.0 and r5 >= 3.0:
        return "SETTORE_ESTESO"
    # Avvio di rotazione: breve periodo gira sopra benchmark mentre il 20d non e ancora forte.
    if rs1 > 0.0 and rs5 >= 0.75 and rs20 < 2.0 and r5 > 0.0:
        return "ROTATION_START"
    if rs5 >= 1.0 and rs20 >= 2.0 and r20 > 0.0:
        return "SETTORE_FORTE"
    if rs5 <= -1.0 and rs20 <= -2.0:
        return "SETTORE_FREDDO"
    return "SETTORE_NEUTRO"


def discover_a5_sector_rotation(
    *, market: str, load_series: Callable[..., Dict[str, Any]], max_results: int = 12,
) -> Dict[str, Any]:
    """A5: rotazione settoriale 1D/5D/20D vs benchmark. Radar di contesto, non ticket."""
    market = str(market or "US").upper()
    if market not in A5_SECTOR_PROXIES:
        raise ValueError("market deve essere US o EU")
    max_results = max(1, min(int(max_results or 12), 20))
    benchmark = A5_BENCHMARKS[market]
    errors: List[Dict[str, str]] = []

    try:
        bp = load_series(benchmark, period="6mo", interval="1d", user=False, allow_fetch=True, stale_ok=False)
        bc = _a5_closes(bp)
        br = {d: _a5_return(bc, d) for d in (1, 5, 20)}
        if any(br[d] is None for d in (1, 5, 20)):
            raise ValueError("storico benchmark insufficiente")
    except Exception as exc:
        return {"version": A5_VERSION, "market": market, "benchmark": benchmark,
                "status": "BLOCCATO PER DATI", "reason": str(exc)[:180],
                "sector_count": len(A5_SECTOR_PROXIES[market]), "evaluated": 0,
                "data_errors": [{"ticker": benchmark, "error": str(exc)[:180]}], "sectors": []}

    rows: List[Dict[str, Any]] = []
    for sector, ticker in A5_SECTOR_PROXIES[market].items():
        try:
            p = load_series(ticker, period="6mo", interval="1d", user=False, allow_fetch=True, stale_ok=False)
            c = _a5_closes(p)
            rr = {d: _a5_return(c, d) for d in (1, 5, 20)}
            if any(rr[d] is None for d in (1, 5, 20)):
                raise ValueError("storico insufficiente")
            rs = {d: rr[d] - br[d] for d in (1, 5, 20)}
            state = _a5_classify(rr[1], rr[5], rr[20], rs[1], rs[5], rs[20])
            # Score 0-100 solo per ordinamento del radar, non e probabilita ne segnale BUY.
            score = 50.0 + 3.0 * rs[5] + 1.5 * rs[20] + 1.0 * rs[1]
            if state == "ROTATION_START": score += 8.0
            elif state == "SETTORE_FORTE": score += 5.0
            elif state == "SETTORE_ESTESO": score -= 12.0
            score = max(0.0, min(score, 100.0))
            rows.append({
                "sector": sector, "ticker": ticker, "state": state,
                "return_1d_pct": round(rr[1], 2), "return_5d_pct": round(rr[5], 2),
                "return_20d_pct": round(rr[20], 2), "relative_1d_pp": round(rs[1], 2),
                "relative_5d_pp": round(rs[5], 2), "relative_20d_pp": round(rs[20], 2),
                "a5_score": round(score, 1),
                "actionability": "NO_CHASE" if state == "SETTORE_ESTESO" else "CONTEXT_ONLY",
            })
        except Exception as exc:
            errors.append({"sector": sector, "ticker": ticker, "error": str(exc)[:180]})

    order = {"ROTATION_START": 0, "SETTORE_FORTE": 1, "SETTORE_NEUTRO": 2,
             "SETTORE_FREDDO": 3, "SETTORE_ESTESO": 4}
    rows.sort(key=lambda x: (order.get(x["state"], 9), -x["a5_score"], x["sector"]))
    return {
        "version": A5_VERSION, "market": market, "benchmark": benchmark,
        "benchmark_returns_pct": {"1d": round(br[1], 2), "5d": round(br[5], 2), "20d": round(br[20], 2)},
        "sector_count": len(A5_SECTOR_PROXIES[market]), "evaluated": len(rows),
        "data_errors": errors, "returned": min(len(rows), max_results), "sectors": rows[:max_results],
        "note": "A5 e un radar di contesto: non genera ticket operativi autonomi.",
    }


# =============================================================================
# Level B Fusion Engine
# =============================================================================
FUSION_VERSION = "level-b-fusion-0.4-a5-full-sector-map"
FUSION_WEIGHTS = {"A1_VOLUME": 25.0, "A2_TECHNICAL": 25.0, "A3_CATALYST": 25.0,
                  "A5_SECTOR": 15.0, "A4_ATTENTION": 10.0}


def _fusion_sector_key(value: Any, market: str) -> Optional[str]:
    t = str(value or "").upper().replace("&", "AND").replace("/", " ")
    if not t:
        return None
    if market == "US":
        aliases = {
            "SEMICONDUCTOR": "SEMICONDUCTORS", "TECH": "TECHNOLOGY", "SOFTWARE": "TECHNOLOGY",
            "FINANC": "FINANCIALS", "BANK": "FINANCIALS", "INDUSTR": "INDUSTRIALS",
            "HEALTH": "HEALTHCARE", "BIOTECH": "HEALTHCARE",
            "CONSUMER CYCLICAL": "CONSUMER_DISCRETIONARY", "CONSUMER DISCRETIONARY": "CONSUMER_DISCRETIONARY",
            "CONSUMER DEFENSIVE": "CONSUMER_STAPLES", "CONSUMER STAPLES": "CONSUMER_STAPLES",
            "ENERGY": "ENERGY", "OIL": "ENERGY", "UTILIT": "UTILITIES",
            "MATERIAL": "MATERIALS", "BASIC MATERIAL": "MATERIALS",
            "REAL ESTATE": "REAL_ESTATE", "COMMUNICATION": "COMMUNICATION",
        }
    else:
        aliases = {
            "BANK": "BANKS",
            "FINANCIAL SERVICE": "FINANCIAL_SERVICES", "CAPITAL MARKET": "FINANCIAL_SERVICES",
            "INSURANCE": "INSURANCE",
            "BASIC RESOURCE": "BASIC_RESOURCES", "BASIC MATERIAL": "BASIC_RESOURCES", "MINING": "BASIC_RESOURCES",
            "CHEM": "CHEMICALS",
            "CONSTRUCTION": "CONSTRUCTION_MATERIALS", "BUILDING MATERIAL": "CONSTRUCTION_MATERIALS",
            "INDUSTR": "INDUSTRIALS",
            "AUTO": "AUTOMOBILES",
            "HEALTH": "HEALTHCARE", "BIOTECH": "HEALTHCARE",
            "TECH": "TECHNOLOGY", "SOFTWARE": "TECHNOLOGY", "SEMICONDUCTOR": "TECHNOLOGY",
            "TELECOM": "TELECOMMUNICATIONS", "COMMUNICATION SERVICE": "TELECOMMUNICATIONS",
            "ENERGY": "OIL_GAS", "OIL": "OIL_GAS", "GAS": "OIL_GAS",
            "FOOD": "FOOD_BEVERAGE", "BEVERAGE": "FOOD_BEVERAGE",
            "RETAIL": "RETAIL",
            "UTILIT": "UTILITIES",
            "REAL ESTATE": "REAL_ESTATE",
            "MEDIA": "MEDIA",
            "TRAVEL": "TRAVEL_LEISURE", "LEISURE": "TRAVEL_LEISURE", "AIRLINE": "TRAVEL_LEISURE",
            "PERSONAL": "PERSONAL_HOUSEHOLD", "HOUSEHOLD": "PERSONAL_HOUSEHOLD", "LUXURY": "PERSONAL_HOUSEHOLD",
        }
    for needle, key in aliases.items():
        if needle in t:
            return key
    return None


def fuse_level_b_results(*, market: str, a1: Dict[str, Any], a2: Dict[str, Any], a3: Dict[str, Any],
                         a4: Dict[str, Any], a5: Dict[str, Any], max_results: int = 10,
                         sector_lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Fonde A1-A5 in uno score di discovery 0-100. Non produce ticket operativi."""
    market = str(market or "US").upper()
    lim = max(1, min(int(max_results or 10), 10))
    pool: Dict[str, Dict[str, Any]] = {}

    def rec(t):
        t = _norm_symbol(t)
        if not t: return None
        return pool.setdefault(t, {"ticker": t, "detected_by": [], "signals": {}, "penalties": [],
                                   "sector_hint": None, "name": None})

    for x in (a1.get("shortlist") or []):
        r=rec(x.get("ticker"));
        if not r: continue
        r["detected_by"].append("A1"); r["signals"]["A1"] = float(x.get("a1_score") or 0)
        r["name"] = r["name"] or x.get("name"); r["sector_hint"] = r["sector_hint"] or x.get("sector") or x.get("industry")
        r["a1"]={k:x.get(k) for k in ("a1_score","rvol_at_time","rvol_method","volume_strength","turnover_current_local")}
        if x.get("rvol_method") and x.get("rvol_method") != "TRUE_RVOL_AT_TIME_5M_CUMULATIVE":
            r["penalties"].append({"code":"RVOL_FALLBACK","points":3.0})

    for key, is_extended in (("shortlist",False),("extended_watchlist",True),("extended_watch",True)):
        for x in (a2.get(key) or []):
            r=rec(x.get("ticker"));
            if not r: continue
            if "A2" not in r["detected_by"]: r["detected_by"].append("A2")
            r["signals"]["A2"] = max(r["signals"].get("A2",0), float(x.get("a2_score") or 0))
            r["name"] = r["name"] or x.get("name"); r["sector_hint"] = r["sector_hint"] or x.get("sector") or x.get("industry")
            r["a2"]={k:x.get(k) for k in ("a2_score","states","extended","extended_reasons","actionability","rsi14","rvol_daily")}
            ext = bool(is_extended or x.get("extended") or x.get("actionability") == "NO_CHASE")
            if ext and not any(p["code"]=="ESTESO_NO_CHASE" for p in r["penalties"]):
                r["penalties"].append({"code":"ESTESO_NO_CHASE","points":20.0})
            if x.get("data_current") is False:
                r["penalties"].append({"code":"TECH_DATA_NOT_CURRENT","points":5.0})

    for key, rumor in (("shortlist",False),("rumor_watchlist",True)):
        for x in (a3.get(key) or []):
            r=rec(x.get("ticker"));
            if not r: continue
            if "A3" not in r["detected_by"]: r["detected_by"].append("A3")
            r["signals"]["A3"] = max(r["signals"].get("A3",0), float(x.get("catalyst_score") or 0))
            r["a3"]={k:x.get(k) for k in ("catalyst_score","categories","actionability","rumor_present","ma_primary_confirmed")}
            if rumor or x.get("actionability") == "WATCH_ONLY":
                if not any(p["code"]=="CATALYST_WATCH_ONLY" for p in r["penalties"]):
                    r["penalties"].append({"code":"CATALYST_WATCH_ONLY","points":10.0})

    for x in (a4.get("shortlist") or []):
        r=rec(x.get("ticker"));
        if not r: continue
        if "A4" not in r["detected_by"]: r["detected_by"].append("A4")
        r["signals"]["A4"] = max(r["signals"].get("A4",0), float(x.get("attention_score") or 0))
        r["a4"]={k:x.get(k) for k in ("attention_score","categories","actionability","article_count","primary_source_present")}
        if x.get("actionability") == "WATCH_ONLY":
            r["penalties"].append({"code":"ATTENTION_WATCH_ONLY","points":5.0})

    # Arricchimento settoriale solo sui candidati gia emersi: evita scansioni massive e
    # permette ad A5 di contribuire realmente al ranking. Il lookup puo fallire senza bloccare.
    sector_lookup_attempted = 0
    sector_lookup_resolved = 0
    if sector_lookup is not None:
        for t, r in pool.items():
            if _fusion_sector_key(r.get("sector_hint"), market):
                continue
            sector_lookup_attempted += 1
            try:
                meta = sector_lookup(t) or {}
                hint = meta.get("sector") or meta.get("industry")
                if hint:
                    r["sector_hint"] = hint
                    r["sector_source"] = meta.get("source") or "YAHOO_SEARCH"
                    sector_lookup_resolved += 1
            except Exception:
                pass

    sector_map = {str(x.get("sector") or "").upper(): x for x in (a5.get("sectors") or [])}
    rows=[]
    for t,r in pool.items():
        sk=_fusion_sector_key(r.get("sector_hint"), market)
        sx=sector_map.get(sk) if sk else None
        sector_score=float(sx.get("a5_score") or 0.0) if sx else None
        sector_state=sx.get("state") if sx else "UNKNOWN_NOT_SCORED"
        if sx and sx.get("actionability") == "NO_CHASE":
            r["penalties"].append({"code":"SECTOR_ESTESO_NO_CHASE","points":8.0})
        comps={
            "A1_VOLUME": round((r["signals"].get("A1",0)/100.0)*25.0,2),
            "A2_TECHNICAL": round((r["signals"].get("A2",0)/100.0)*25.0,2),
            "A3_CATALYST": round((r["signals"].get("A3",0)/100.0)*25.0,2),
            "A5_SECTOR": round((sector_score/100.0)*15.0,2) if sector_score is not None else 0.0,
            "A4_ATTENTION": round((r["signals"].get("A4",0)/100.0)*10.0,2),
        }
        base=sum(comps.values()); penalty=sum(float(p["points"]) for p in r["penalties"])
        final=max(0.0,min(100.0,base-penalty))
        no_chase=any(p["code"] in ("ESTESO_NO_CHASE","SECTOR_ESTESO_NO_CHASE") for p in r["penalties"])
        watch=any(p["code"] in ("CATALYST_WATCH_ONLY","ATTENTION_WATCH_ONLY") for p in r["penalties"])
        action="NO_CHASE" if no_chase else ("WATCH_ONLY" if watch else "FUSION_CANDIDATE")
        rows.append({"ticker":t,"name":r.get("name"),"fusion_score":round(final,1),"base_score":round(base,1),
                     "score_components":comps,"detected_by":r["detected_by"],"signal_count":len(r["detected_by"]),
                     "sector_hint":r.get("sector_hint"),"sector_source":r.get("sector_source"),"sector_key":sk,"sector_state":sector_state,
                     "sector_score":round(sector_score,1) if sector_score is not None else None,"penalties":r["penalties"],"penalty_total":round(penalty,1),
                     "actionability":action,"evidence":{"a1":r.get("a1"),"a2":r.get("a2"),"a3":r.get("a3"),"a4":r.get("a4")}})
    rows.sort(key=lambda x:(-x["fusion_score"], -x["signal_count"], x["ticker"]))
    actionable=[x for x in rows if x["actionability"] == "FUSION_CANDIDATE"]
    watch=[x for x in rows if x["actionability"] == "WATCH_ONLY"]
    no_chase=[x for x in rows if x["actionability"] == "NO_CHASE"]
    shortlist=actionable[:lim]
    return {"version":FUSION_VERSION,"market":market,"weights":FUSION_WEIGHTS,
            "method":"A1+A2+A3+A4+A5 weighted discovery score; penalties applied separately; no autonomous ticket",
            "candidates_union":len(rows),"actionable_count":len(actionable),"watch_only_count":len(watch),
            "no_chase_count":len(no_chase),"sector_lookup_attempted":sector_lookup_attempted,
            "sector_lookup_resolved":sector_lookup_resolved,"returned":len(shortlist),"shortlist":shortlist,
            "watchlist":watch[:lim],"no_chase":no_chase[:lim],
            "limitations":["A5 contributes zero points only when candidate sector remains unresolved after lightweight candidate-only sector enrichment; unknown sector is not treated as positive evidence",
                           "WATCH_ONLY and NO_CHASE are kept outside the verification shortlist to avoid wasting downstream eToro/Yahoo checks",
                           "eToro X1 eligibility, live spread/liquidity and final Yahoo drilldown are not yet verified by this fusion stage",
                           "Fusion score is a ranking score, not probability of gain and not a BUY signal"]}
