"""1.0.0: allowlisted, bounded eToro READ client for autonomous research.

Only two POST paths are permitted, both published x-read-only previews.
Never accepts an execution/order path. Credentials and bodies never enter logs.
"""
import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request,build_opener
from urllib.error import HTTPError,URLError
from etoro_history import BASE,NoRedirect,auth_headers,diagnose_http_error

VERSION='1.0.0'
GET_PATHS={'/api/v1/me','/api/v2/market-data/instruments','/api/v2/market-data/rates',
           '/api/v1/market-data/exchanges','/api/v1/trading/info/trade/history'}
POST_PATHS={'/api/v2/trading/info/eligibility','/api/v2/trading/info/costs'}
_LOCK=threading.Lock();_NEXT=0.;_COST_NEXT=0.

class ReadFailure(RuntimeError):
    def __init__(self,code,retry_after=60,permanent=False):
        super().__init__(code);self.retry_after=retry_after;self.permanent=permanent


def unpack(response):
    if response.get('statusCode')!=200 or response.get('bodyTruncated'):raise ReadFailure('ETORO_READ_RESPONSE_INVALID')
    return json.loads(response['body']) if isinstance(response['body'],str) else response['body']


class Client:
    def __init__(self,opener=None,clock=time.monotonic,wait=time.sleep):
        self.open=opener or build_opener(NoRedirect()).open;self.clock=clock;self.wait=wait;self.journal=[]
    def read(self,path,query=None,body=None,request_id=None):
        global _NEXT,_COST_NEXT
        if path not in GET_PATHS|POST_PATHS or ('?' in path) or (body is None)!=(path in GET_PATHS):
            raise ReadFailure('ETORO_ROUTE_NOT_READ_ALLOWLISTED',permanent=True)
        if path.endswith('/costs') and (body.get('action')!='open' or body.get('transaction')!='buy' or body.get('leverage')!=1 or body.get('settlementType')!='real'):
            raise ReadFailure('ETORO_RESEARCH_COST_SCOPE_INVALID',permanent=True)
        with _LOCK:
            next_time=max(_NEXT,_COST_NEXT if path.endswith('/costs') else 0)
            delay=next_time-self.clock()
            if delay>60:raise ReadFailure('ETORO_RATE_BACKOFF',int(delay)+1)
            if delay>0:self.wait(delay)
            _NEXT=self.clock()+1.1
            if path.endswith('/costs'):_COST_NEXT=self.clock()+3.1
        try:headers=auth_headers()
        except ValueError:raise ReadFailure('ETORO_AUTH_NOT_CONFIGURED',permanent=True) from None
        if request_id:headers['x-request-id']=request_id
        data=None if body is None else json.dumps(body,allow_nan=False).encode()
        if data:headers['Content-Type']='application/json'
        url=BASE+path+('?' + urlencode(query) if query else '')
        req=Request(url,headers=headers,data=data,method='GET' if data is None else 'POST')
        try:
            with self.open(req,timeout=15) as r:
                raw=r.read(2_000_001)
                if len(raw)>2_000_000:raise ReadFailure('ETORO_READ_TOO_LARGE',permanent=True)
                if r.status!=200:raise ReadFailure('ETORO_READ_STATUS_'+str(r.status))
                result=json.loads(raw)
            record=dict(statusCode=200,body=result,bodyTruncated=False,xRequestId=headers['x-request-id'],request=dict(path=path,query=query,body=body))
            self.journal.append(record)
            return record
        except HTTPError as e:
            self.journal.append(dict(statusCode=e.code,request=dict(path=path,query=query,body=body),diagnostic=diagnose_http_error(e,headers['x-request-id'])))
            try:retry=min(3600,max(1,int(e.headers.get('Retry-After','60'))))
            except (TypeError,ValueError):retry=60
            if e.code==429:
                with _LOCK:
                    _NEXT=max(_NEXT,self.clock()+retry)
                    if path.endswith('/costs'):_COST_NEXT=max(_COST_NEXT,self.clock()+retry)
            raise ReadFailure('ETORO_HTTP_'+str(e.code),retry,permanent=e.code in {400,401,403,404,422}) from None
        except (URLError,OSError,ValueError):raise ReadFailure('ETORO_READ_NETWORK_OR_JSON') from None
