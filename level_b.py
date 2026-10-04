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
