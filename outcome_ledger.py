"""1.0.1: closed-trade KPI; calibration uses the same bins as aggregation."""
import copy
import hashlib
import json
import math
from datetime import datetime,date


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def timely_forecast(link, trade):
    try:
        recorded=datetime.fromisoformat(link['forecast_recorded_at'].replace('Z','+00:00'))
        opened=datetime.fromisoformat(trade['openTime'].replace('Z','+00:00'))
        return recorded.utcoffset() is not None and opened.utcoffset() is not None and recorded<=opened
    except (KeyError,TypeError,ValueError): return False


def ingest(pages, currency, prior=None, links=None):
    if currency != "USD": raise ValueError("ACCOUNT_CURRENCY_UNSUPPORTED_FOR_USD_LEDGER")
    entries=copy.deepcopy(prior or {})
    links=links or {}
    coverage=[];errors=[]
    expected_page=1
    period=None
    for page in pages:
        if page.get("account")!="real" or page.get("statusCode")!=200 or page.get("page")!=expected_page:
            raise ValueError("HISTORY_PAGE_INVALID")
        try:
            date.fromisoformat(page['minDate'])
            if type(page.get('pageSize')) is not int or not 1<=page['pageSize']<=200 or type(page.get('hasMore')) is not bool:raise ValueError()
            if period is not None and page['minDate']!=period:raise ValueError()
            period=page['minDate']
        except (KeyError,ValueError,TypeError):raise ValueError('HISTORY_PERIOD_OR_PAGINATION_UNDOCUMENTED')
        expected_page+=1
        coverage.append({k:page.get(k) for k in ("minDate","page","pageSize","hasMore","xRequestId")})
        for trade in page.get("trades",[]):
            identity={k:trade.get(k) for k in ("positionId","orderId","openTime","closeTime","units")}
            if any(v is None for v in identity.values()):
                errors.append(dict(reason="CLOSE_IDENTITY_INCOMPLETE",raw_hash=digest(trade)));continue
            key=digest(identity)
            raw_hash=digest(trade)
            if key in entries:
                if entries[key]["raw_hash"]!=raw_hash:
                    errors.append(dict(key=key,reason="BROKER_CLOSE_CHANGED_RECONCILE"));continue
                if key not in links: continue
            mirror=trade.get("isMirrorTrade") is True or bool(trade.get("parentPositionId")) or bool(trade.get("socialTradeId"))
            net=trade.get("netProfit");fees=trade.get("fees");investment=trade.get("investment")
            valid=number(net) and number(fees) and fees>=0 and number(investment) and investment>0
            link=links.get(key) or {}
            linked=(link.get("verified") is True and link.get("ticket_unchanged") is True and link.get("position_id")==trade["positionId"] and bool(link.get("signal_id")) and bool(link.get("evidence_reference")))
            origin="COPY" if mirror else "AI_ESEGUITA" if linked else "DA_VERIFICARE"
            pnl=net-fees if valid else None
            entries[key]=dict(close_key=key,raw_hash=raw_hash,raw=trade,currency=currency,
                origin=origin,signal_id=link.get("signal_id") if linked else None,category=link.get("category") if linked else None,
                net_pnl_usd=pnl,net_return=pnl/investment if valid else None,
                benchmark_return=link.get("benchmark_return") if linked else None,
                forecast_probability=link.get("forecast_probability") if linked and timely_forecast(link,trade) else None,
                direction_correct=link.get("direction_correct") if linked else None,
                economic_status="VALIDO" if valid else "DATI_MANCANTI",
                economic_issue=None if valid else "negative/signed fees convention or economic fields require verified source; no automatic sign conversion",
                attribution_status="VERIFICATA" if linked else "NON_ATTRIBUITA",
                initial_risk_usd=link.get("initial_risk_usd") if linked else None)
    return dict(version="1.0.0",entries=entries,coverage=coverage,errors=errors,
                pagination_exhausted_hint=bool(pages) and pages[-1].get("hasMore") is False,
                coverage_certified=False,coverage_note="upstream hasMore is heuristic, not guaranteed completeness",
                inserted=len(entries)-len(prior or {}),ticket_authorized=False)


def kpis(entries):
    values=list(entries.values())
    eligible=[e for e in values if e.get("origin")=="AI_ESEGUITA" and e.get("economic_status")=="VALIDO" and number(e.get("net_pnl_usd"))]
    wins=[e["net_pnl_usd"] for e in eligible if e["net_pnl_usd"]>0]
    losses=[e["net_pnl_usd"] for e in eligible if e["net_pnl_usd"]<0]
    excess=[e["net_return"]-e["benchmark_return"] for e in eligible if number(e.get("net_return")) and number(e.get("benchmark_return"))]
    forecasts=[e for e in eligible if number(e.get("forecast_probability")) and 0<=e["forecast_probability"]<=1 and type(e.get("direction_correct")) is bool]
    bins=[]
    for lower,upper in ((0,.5),(.5,.7),(.7,.85),(.85,1.0000001)):
        sample=[e for e in forecasts if lower<=e["forecast_probability"]<upper]
        bins.append(dict(lower=lower,upper=min(1,upper),n=len(sample),empirical_probability=sum(e["direction_correct"] for e in sample)/len(sample) if sample else None,
                         usable_for_calibration=len(sample)>=30))
    return dict(n=len(eligible),excluded=len(values)-len(eligible),net_pnl_usd=sum(e["net_pnl_usd"] for e in eligible) if eligible else None,
                win_rate=len(wins)/len(eligible) if eligible else None,profit_factor=sum(wins)/abs(sum(losses)) if losses else None,
                excess_return_mean=sum(excess)/len(excess) if excess else None,excess_n=len(excess),
                brier_score=sum((e["forecast_probability"]-int(e["direction_correct"]))**2 for e in forecasts)/len(forecasts) if forecasts else None,
                forecast_n=len(forecasts),confidence_calibration=bins,
                copy_excluded=True,scope="verified AI executed trades only; no claims on unattributed trades")


def calibrate(probability,bins):
    if not number(probability) or not 0<=probability<=1:raise ValueError('ORIGINAL_PROBABILITY_REQUIRED')
    for row in bins:
        in_bin = row['lower'] <= probability < row['upper'] or probability == 1 and row['upper'] == 1
        if in_bin and row.get('usable_for_calibration') and row.get('n',0)>=30 and number(row.get('empirical_probability')):
            return dict(original_probability=probability,calibrated_probability=row['empirical_probability'],n=row['n'],method='EMPIRICAL_VALIDATED_BIN_NOT_GUARANTEE')
    return dict(original_probability=probability,calibrated_probability=None,n=0,method='INSUFFICIENT_SAMPLE')
