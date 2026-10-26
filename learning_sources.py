"""1.0.0: real read-only inputs for existing cohort protocol; no trade sizing.

Research cost notional is USD1000 (or the larger broker minimum), explicitly a
shadow-comparison unit. Never an order, allocated amount or portfolio risk rule.
"""
import hashlib
import html
import json
import re
import uuid
import os
from acquire_ecb_fx import acquire as acquire_ecb
from datetime import datetime,timedelta,timezone
from urllib.request import urlopen,Request
from urllib.parse import urlparse
from adaptive_method import digest,stamp
from etoro_learning_client import Client,ReadFailure,unpack
from learning_acquisition import prepare_entry
from learning_sessions import session_plan,close_from_history

FEE_URL='https://www.etoro.com/trading/fees/'

class Pending(RuntimeError):
    def __init__(self,code,seconds=300):super().__init__(code);self.seconds=seconds


def now():return datetime.now(timezone.utc).isoformat()

RATE_TIME_REFERENCE='https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments; rate.date: UTC timestamp of the rate'


def rate_timestamp(value):
    """Normalize only v2 rate.date: the official schema specifies UTC.

    Broker responses may omit the suffix. Other timestamps remain strict;
    event time never becomes retrieval time and raw responses stay unchanged.
    """
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})?',value):
        raise ValueError('ETORO_RATE_TIMESTAMP_INVALID')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.utcoffset() is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:raise ValueError('ETORO_RATE_TIMESTAMP_INVALID') from None


def request_id(key):return str(uuid.uuid5(uuid.NAMESPACE_URL,'etoro-research/'+key))

def account_fingerprint(me):return digest({k:me.get(k) for k in ('gcid','realCid','username')})


def fetch_fees(opener=urlopen):
    with opener(Request(FEE_URL,headers={'User-Agent':'yahoo-finance-mcp1/1.7.56 (read-only research collector)','Accept':'text/html'}),timeout=15) as r:
        if urlparse(r.url).hostname!='www.etoro.com':raise Pending('FEE_SOURCE_REDIRECT_INVALID',86400)
        raw=r.read(2_000_001)
    if len(raw)>2_000_000:raise Pending('FEE_SOURCE_TOO_LARGE',86400)
    text=' '.join(html.unescape(re.sub('<[^>]+>',' ',raw.decode('utf-8'))).split())
    stocks=bool(re.search(r'\$1\s+or\s+\$2.*?opening\s+and\s+closing',text,re.I))
    etfs=bool(re.search(r'zero\s+commission\s+for\s+ETF\s+trades',text,re.I))
    if not stocks or not etfs:raise Pending('EXIT_FEE_SOURCE_CHANGED_OR_UNREADABLE',86400)
    return dict(url=FEE_URL,sha256=hashlib.sha256(raw).hexdigest(),retrieved_at=now(),stocks_open_close_flat_schedule=True,real_etf_zero_commission=True,document_text=text)


def broker_quote_currency(metadata,opener=urlopen,name_check=None):
    symbol=metadata['symbol']
    if not re.fullmatch(r'[A-Za-z0-9.^=-]{1,40}',symbol):raise Pending('BROKER_SYMBOL_UNSAFE',86400)
    url='https://www.etoro.com/markets/'+symbol.lower()
    with opener(Request(url,headers={'User-Agent':'yahoo-finance-mcp1/1.7.56 (read-only research collector)'}),timeout=15) as r:
        if urlparse(r.url).hostname!='www.etoro.com':raise Pending('BROKER_CURRENCY_REDIRECT_INVALID',86400)
        raw=r.read(2_000_001)
    if len(raw)>2_000_000:raise Pending('BROKER_CURRENCY_SOURCE_TOO_LARGE',86400)
    found=[]
    for text in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',raw.decode('utf-8'),re.S|re.I):
        try:value=json.loads(text)
        except ValueError:continue
        if isinstance(value,dict) and isinstance(value.get('offers'),dict):
            matched=value.get('name')==metadata['displayName']
            if name_check and not matched:
                matched=name_check(yahoo_ticker=symbol,yahoo_name=metadata['displayName'],etoro_ticker=symbol,etoro_name=value.get('name')).get('status')=='PASS'
            code=value['offers'].get('priceCurrency')
            if matched and code:found.append(code)
    if len(set(found))!=1:raise Pending('BROKER_QUOTE_CURRENCY_NOT_EXPOSED:'+symbol,86400)
    return found[0],url+' sha256='+hashlib.sha256(raw).hexdigest()+'; structured broker product quote currency'


def exit_policy(meta,cost,fees,as_of):
    d=unpack(cost);components={x['costType']:x for x in d.get('costs',[])}
    commission=components.get('transactionFee',{}).get('value')
    if any(components.get(k,{}).get('value',0)!=0 for k in ['markup','overnightFee','overWeekendFee']):raise Pending('EXIT_ADDITIONAL_COST_SOURCE_REQUIRED',86400)
    if meta['type']=='Stocks' and commission in (1.,2.) and fees.get('stocks_open_close_flat_schedule'):fee=commission
    elif meta['type']=='ETF' and commission==0 and fees.get('real_etf_zero_commission'):fee=0.
    else:raise Pending('EXIT_FEE_APPLICABILITY_NOT_VERIFIED',86400)
    if (stamp(as_of)-stamp(fees['retrieved_at'])).total_seconds()>86400:raise Pending('EXIT_FEE_SOURCE_EXPIRED')
    return dict(instrument_id=meta['instrumentId'],currency='USD',fee_usd=fee,verified=True,
                verified_at=fees['retrieved_at'],valid_until=(stamp(fees['retrieved_at'])+timedelta(days=1)).isoformat(),
                source_reference=fees['url']+' sha256='+fees['sha256']+'; published opening/closing schedule + actual account open preview; ex-ante shadow fees, not future fill attestation')


class Sources:
    def __init__(self,history,indicators,identity,client=None):
        self.history=history;self.indicators=indicators;self.identity=identity;self.client=client or Client()
    def verify_account(self,expected,task):
        response=self.client.read('/api/v1/me',request_id=request_id(task))
        if account_fingerprint(unpack(response))!=expected:raise ReadFailure('COLLECTOR_ACCOUNT_MISMATCH',permanent=True)
        return response
    def contexts(self,tickers,task):
        response=self.client.read('/api/v2/market-data/instruments',query={'symbols':','.join(tickers),'pageSize':'100'},request_id=request_id(task+'/metadata'))
        data=unpack(response)
        if data.get('pagination',{}).get('hasNext'):raise Pending('METADATA_PAGINATION_REQUIRED',3600)
        exchanges_response=self.client.read('/api/v1/market-data/exchanges',request_id=request_id(task+'/exchanges'))
        exchanges={r['exchangeID']:r['exchangeDescription'] for r in unpack(exchanges_response).get('exchangeInfo',[])}
        out={}
        for ticker in tickers:
            rows=[r for r in data.get('results',[]) if r.get('symbol','').upper()==ticker and r.get('type') in {'Stocks','ETF'}]
            if len(rows)!=1:raise Pending('INSTRUMENT_METADATA_MISSING_OR_AMBIGUOUS:'+ticker,86400)
            meta=rows[0];hist=self.history(ticker);tech=self.indicators(ticker)
            identity=self.identity(yahoo_ticker=ticker,yahoo_name=hist.get('name'),etoro_ticker=meta['symbol'],etoro_name=meta['displayName'])
            if identity.get('status')!='PASS':raise Pending('INSTRUMENT_IDENTITY_NOT_VERIFIED:'+ticker,86400)
            currency=hist.get('major_currency',hist.get('currency'));unit=hist.get('price_unit',hist.get('currency'))
            # Current public broker rate metadata omits quote currency. Accept
            # native USD US listings only; keep other units explicit, never infer FX.
            venue=exchanges.get(meta.get('exchangeId'),'')
            usd_listing=currency=='USD' and hist.get('calendar') in {'XNYS','XNAS'} and bool(re.search(r'NASDAQ|NYSE|NEW YORK',venue,re.I))
            broker_currency=meta.get('currency');currency_ref=response['xRequestId']
            if not usd_listing and broker_currency is None:
                broker_currency,currency_ref=broker_quote_currency(meta,name_check=self.identity)
            if usd_listing:broker_currency='USD'
            major={'GBp':'GBP','GBX':'GBP','ZAc':'ZAR'}.get(broker_currency,broker_currency)
            if major!=currency:raise Pending('BROKER_QUOTE_CURRENCY_MISMATCH:'+ticker,86400)
            broker_scale=.01 if broker_currency in {'GBp','GBX','ZAc'} else 1.
            technical_scale=hist.get('price_scale_to_major_currency',1.)
            out[ticker]=dict(metadata=meta,calendar_id=hist['calendar'],currency=currency,price_unit=currency,broker_scale=broker_scale,currency_reference=currency_ref,
                history_reference=hist.get('source','')+' sha256='+digest(hist),
                identity=dict(status='PASS',instrument_id=meta['instrumentId'],reference=response['xRequestId']+'; '+digest(identity)),
                indicators=dict(atr14=tech.get('atr14')*technical_scale if tech.get('atr14') is not None else None,price_unit=currency,source_reference='server indicators sha256='+digest(tech)))
        return dict(contexts=out,raw_metadata=response,raw_exchanges=exchanges_response)
    def entry(self,row,contexts,benchmark,task):
        tickers=[c['key'] for c in row['candidates']];all_keys=tickers+[benchmark]
        if set(all_keys)-set(contexts):raise Pending('LEARNING_CONTEXTS_INCOMPLETE')
        # Closed exchange is normal waiting, checked before broker quote/cost calls.
        moment=now()
        for key in all_keys:
            try:session_plan(contexts[key]['calendar_id'],moment)
            except ValueError as exc:
                if str(exc)=='ENTRY_SESSION_NOT_OPEN':raise Pending('WAIT_OPEN_SESSION',300) from None
                raise
        ids=[contexts[k]['metadata']['instrumentId'] for k in all_keys]
        eligibility=self.client.read('/api/v2/trading/info/eligibility',body=dict(instrumentIds=ids,currency='USD'),request_id=request_id(task+'/eligibility'))
        allowed={r['instrumentId']:r for r in unpack(eligibility).get('eligibilities',[])}
        fee_source=fetch_fees();costs={};notional=1000.
        for key in tickers:
            meta=contexts[key]['metadata'];e=allowed.get(meta['instrumentId'],{})
            configs=[c for c in e.get('leverageConfigs',[]) if c.get('settlementType')=='real' and c.get('direction')=='long' and 1 in c.get('leverageValues',[]) and c.get('isPotential') is False]
            if e.get('allowOpenPosition') is not True or len(configs)!=1:raise Pending('REAL_LONG_X1_ELIGIBILITY_NOT_CONFIRMED:'+key,3600)
            minimum=configs[0].get('minPositionAmount')
            if type(minimum) not in (int,float) or minimum<=0:raise Pending('RESEARCH_NOTIONAL_MINIMUM_UNAVAILABLE:'+key,3600)
            notional=max(notional,minimum)
        started=stamp(now())
        for key in tickers:
            if (stamp(now())-started).total_seconds()>270:raise Pending('COHORT_COST_WINDOW_EXCEEDED',300)
            meta=contexts[key]['metadata'];body=dict(action='open',transaction='buy',instrumentId=meta['instrumentId'],settlementType='real',orderType='mkt',leverage=1,amount=notional,orderCurrency='usd')
            response=self.client.read('/api/v2/trading/info/costs',body=body,request_id=request_id(task+'/cost/'+key))
            costs[key]=dict(response=response,request=dict(path='/api/v2/trading/info/costs',body=body),exit_fee_policy=exit_policy(meta,response,fee_source,now()))
        fx_values,raw_fx=self.live_fx([contexts[k]['currency'] for k in all_keys],task)
        rates=self.client.read('/api/v2/market-data/rates',query={'instrumentIds':','.join(map(str,ids))},request_id=request_id(task+'/quotes'))
        byid={q['instrumentId']:q for q in unpack(rates).get('results',[])};overview=dict(statusCode=200,xRequestId=rates['xRequestId'],instruments=[])
        normalized={};identities={};timestamp_evidence={}
        for key in all_keys:
            context=contexts[key];q=byid.get(context['metadata']['instrumentId'],{})
            if q.get('quoteType')!='realtime':raise Pending('ENTRY_QUOTE_DELAYED_OR_UNAVAILABLE:'+key)
            quote_at=rate_timestamp(q.get('date'))
            if not 0<=(stamp(now())-stamp(quote_at)).total_seconds()<=300:raise Pending('ENTRY_QUOTE_STALE_OR_FUTURE:'+key)
            timestamp_evidence[key]=dict(raw=q['date'],normalized=quote_at,reference=RATE_TIME_REFERENCE)
            scale=context.get('broker_scale',1.)
            overview['instruments'].append(dict(market=dict(symbol=key,instrumentId=context['metadata']['instrumentId']),quote=dict(ask=q.get('ask')*scale if q.get('ask') else None,bid=q.get('bid')*scale if q.get('bid') else None,asOf=quote_at)))
            fx,reference=fx_values[context['currency']]
            normalized[key]=dict(context,fx_to_usd=fx,fx_reference=reference)
            identities[key]=context['identity']
        observed=now()
        result=prepare_entry(overview,normalized,identities,costs,benchmark,observed)
        result.update(raw_rates=rates,quote_timestamp_evidence=timestamp_evidence,raw_eligibility=eligibility,raw_fee_source=fee_source,raw_fx=raw_fx,research_notional_usd=notional)
        return result
    def exit(self,row,task):
        if stamp(now())<stamp(row['planned_exit_at']):raise Pending('WAIT_PLANNED_CLOSE',300)
        quotes={};entry=row['entry'];plan=row['exit_plan']
        for key,q in [*entry['quotes'].items(),('__benchmark__',entry['benchmark'])]:
            expected=plan['benchmark'] if key=='__benchmark__' else plan['entries'][key]
            hist=self.history(q['ticker'],start=stamp(q['timestamp']).date().isoformat(),end=(stamp(now()).date()+timedelta(days=1)).isoformat(),prefer_etoro=False)
            # A cached payload fetched before close must be refreshed by normal
            # calendar policy, never stamped now to pass a timestamp check.
            if q['currency']=='USD':fx,fx_ref=1.,'USD/USD identity'
            else:
                ecb=acquire_ecb(os.environ.get('ECB_FX_CACHE_DIR','/tmp/yfs-ecb-fx'),expected['session_date'],expected['session_date'])
                fx=ecb['fx_to_usd_by_date'].get(expected['session_date'],{}).get(q['currency'])
                if not fx:raise Pending('EXACT_SESSION_ECB_FX_MISSING',3600)
                fx_ref='ECB dated reference '+expected['session_date']+'; asynchronous to exchange close; '+digest(ecb['sources'])
            try:quotes[key]=close_from_history(hist,expected,q,now(),fx,fx_ref)
            except ValueError as exc:
                if str(exc) in {'EXIT_SESSION_BAR_MISSING_OR_DUPLICATE','EXIT_HISTORY_BEFORE_CLOSE'}:raise Pending(str(exc),300) from None
                raise
        benchmark=quotes.pop('__benchmark__')
        return dict(observed_at=now(),quotes=quotes,benchmark=benchmark,session_complete=True,calendar_reference='recorded exact-session plan '+plan['plan_hash'])
    def live_fx(self,currencies,task):
        pairs={'EUR':('EURUSD',False),'GBP':('GBPUSD',False),'CHF':('USDCHF',True),'SEK':('USDSEK',True),'NOK':('USDNOK',True),'DKK':('USDDKK',True)}
        needed=sorted(set(currencies)-{'USD'});values={'USD':(1.,'USD/USD identity')}
        if not needed:return values,[]
        if any(c not in pairs for c in needed):raise Pending('LIVE_FX_CURRENCY_UNSUPPORTED',86400)
        metadata=self.client.read('/api/v2/market-data/instruments',query=dict(symbols=','.join(pairs[c][0] for c in needed),pageSize=100),request_id=request_id(task+'/fx-meta'))
        rows={r['symbol']:r for r in unpack(metadata).get('results',[]) if r.get('type')=='Forex'}
        if any(pairs[c][0] not in rows for c in needed):raise Pending('LIVE_FX_PAIR_UNAVAILABLE',3600)
        response=self.client.read('/api/v2/market-data/rates',query=dict(instrumentIds=','.join(str(rows[pairs[c][0]]['instrumentId']) for c in needed)),request_id=request_id(task+'/fx-rates'))
        quotes={r['instrumentId']:r for r in unpack(response).get('results',[])}
        for currency in needed:
            symbol,inverse=pairs[currency];q=quotes.get(rows[symbol]['instrumentId'],{})
            if q.get('quoteType')!='realtime' or not q.get('date'):raise Pending('LIVE_FX_QUOTE_STALE_OR_MISSING')
            quote_at=rate_timestamp(q['date'])
            if not 0<=(stamp(now())-stamp(quote_at)).total_seconds()<=300 or not 0<q.get('bid',0)<=q.get('ask',0):raise Pending('LIVE_FX_QUOTE_STALE_OR_MISSING')
            mid=(q['bid']+q['ask'])/2;value=1/mid if inverse else mid
            values[currency]=(value,response['xRequestId']+' '+symbol+' raw='+q['date']+' normalized='+quote_at+'; '+RATE_TIME_REFERENCE+'; observed midpoint for shadow comparison, not booked broker conversion')
        return values,[metadata,response]

    def history_page(self,minimum,page,task):
        response=self.client.read('/api/v1/trading/info/trade/history',query=dict(minDate=minimum,page=page,pageSize=200),request_id=request_id(task))
        data=unpack(response)
        if not isinstance(data,list):raise ReadFailure('TRADE_HISTORY_SHAPE_UNSUPPORTED',permanent=True)
        trades=[]
        for r in data:
            trade=dict(r,openTime=r.get('openTimestamp'),closeTime=r.get('closeTimestamp'),market=dict(instrumentId=r.get('instrumentId')),
                       direction='long' if r.get('isBuy') is True else 'short' if r.get('isBuy') is False else None,isMirrorTrade=bool(r.get('parentPositionId')))
            trades.append(trade)
        return dict(account='real',statusCode=200,xRequestId=response['xRequestId'],minDate=minimum,page=page,pageSize=200,hasMore=len(data)==200,trades=trades,raw_response=response)
