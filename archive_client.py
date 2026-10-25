"""Version 1.0.0 — authenticated persistent archive; no account/trading actions.

Stage 1 only. A configured gateway is NOT proof of durable storage. put()
returns durable_verified=True only after a separate signed readback.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import threading
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

VERSION = '1.0.0'
_GATEWAY_LOCK=threading.Lock()
_METRICS_LOCK=threading.Lock()
_METRICS=dict(calls=0,request_bytes=0,response_bytes=0,failures=0,by_action={})

def io_metrics():
    import copy
    with _METRICS_LOCK:return copy.deepcopy(_METRICS)

def _after_fork():
    global _GATEWAY_LOCK,_METRICS_LOCK
    _GATEWAY_LOCK=threading.Lock();_METRICS_LOCK=threading.Lock()

if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_child=_after_fork)
MAX_PAYLOAD_BYTES = 2_000_000
KINDS = {'SCAN_ARCHIVE','COHORT_STORE','LEARNING_STATE','FEEDBACK','RISK_BUNDLE',
         'CHECKPOINT','RAW_MARKET_DATA','TECHNICAL_TEST'}


class ArchiveError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class GoogleRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        u=urlparse(newurl)
        if u.scheme!='https' or u.hostname not in {'script.google.com','script.googleusercontent.com'} or u.username or u.port not in (None,443):
            raise ArchiveError('ARCHIVE_REDIRECT_REJECTED')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def configuration_status(env=None):
    env=os.environ if env is None else env
    url=env.get('ARCHIVE_WEBAPP_URL','');secret=env.get('ARCHIVE_HMAC_SECRET','')
    valid=bool(re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec',url)) and len(secret)>=32
    return dict(version=VERSION,url_configured=bool(url),secret_configured=bool(secret),
                configured=valid,durable_storage_verified=False,
                note='configuration only; run authenticated self-test before activation')


class ArchiveClient:
    def __init__(self,url,secret,opener=None,clock=time.time):
        if not re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec',url or ''):
            raise ArchiveError('ARCHIVE_WEBAPP_URL_INVALID')
        if not isinstance(secret,str) or len(secret)<32:raise ArchiveError('ARCHIVE_SECRET_MISSING_OR_SHORT')
        self.url=url;self.secret=secret.encode();self.opener=opener or build_opener(GoogleRedirect()).open;self.clock=clock

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('ARCHIVE_WEBAPP_URL',''),os.environ.get('ARCHIVE_HMAC_SECRET',''))

    def _call(self,action,**fields):
        # Serialize calls within one serving process before issuing a nonce.
        # Avoid filling the Apps Script lock queue with our own background IO.
        with _GATEWAY_LOCK:
            with _METRICS_LOCK:
                _METRICS['calls']+=1
                _METRICS['by_action'][action]=_METRICS['by_action'].get(action,0)+1
            try:return self._call_serial(action,**fields)
            except Exception:
                with _METRICS_LOCK:_METRICS['failures']+=1
                raise

    def _call_serial(self,action,**fields):
        nonce=secrets.token_hex(16)
        msg=canonical(dict(protocol=1,action=action,timestamp=int(self.clock()),nonce=nonce,**fields))
        signature=hmac.new(self.secret,msg.encode(),hashlib.sha256).hexdigest()
        body=canonical(dict(message=msg,signature=signature)).encode()
        with _METRICS_LOCK:_METRICS['request_bytes']+=len(body)
        req=Request(self.url,data=body,
                    headers={'Content-Type':'application/json','User-Agent':'etoro-archive/1.0.0'})
        try:
            with self.opener(req,timeout=25) as response:
                target=urlparse(response.url)
                if target.scheme!='https' or target.hostname not in {'script.google.com','script.googleusercontent.com'}:
                    raise ArchiveError('ARCHIVE_RESPONSE_ORIGIN_REJECTED')
                raw=response.read(4_500_001)
            with _METRICS_LOCK:_METRICS['response_bytes']+=len(raw)
            if len(raw)>4_500_000:raise ArchiveError('ARCHIVE_RESPONSE_TOO_LARGE')
            envelope=json.loads(raw)
            text=envelope['message'];sig=envelope['signature']
            expected=hmac.new(self.secret,text.encode(),hashlib.sha256).hexdigest()
            if not isinstance(sig,str) or not hmac.compare_digest(sig,expected):raise ArchiveError('ARCHIVE_RESPONSE_SIGNATURE_INVALID')
            result=json.loads(text)
            if result.get('nonce')!=nonce:raise ArchiveError('ARCHIVE_RESPONSE_NONCE_INVALID')
            if result.get('ok') is not True:raise ArchiveError('ARCHIVE_REMOTE_REJECTED:'+str(result.get('error','UNKNOWN')))
            return result
        except ArchiveError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            raise ArchiveError('ARCHIVE_REQUEST_OR_RESPONSE_FAILED') from exc

    def status(self):
        return self._call('status')

    def get(self,object_id):
        if not re.fullmatch(r'[0-9a-f]{64}',object_id or ''):raise ArchiveError('ARCHIVE_OBJECT_ID_INVALID')
        result=self._call('get',object_id=object_id)
        text=result.get('payload_json')
        if not isinstance(text,str) or digest(text)!=result.get('payload_sha256') or result.get('object_id')!=object_id:
            raise ArchiveError('ARCHIVE_READBACK_HASH_INVALID')
        if len(text.encode())>MAX_PAYLOAD_BYTES:raise ArchiveError('ARCHIVE_READBACK_TOO_LARGE')
        json.loads(text)
        self._receipt(result)
        return result

    @staticmethod
    def _receipt(result):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',str(result.get('file_id',''))):
            raise ArchiveError('ARCHIVE_FILE_RECEIPT_INVALID')
        match=re.fullmatch(r'Registro!A(\d+):Z(\d+)',str(result.get('registry_range','')))
        if not match or match[1]!=match[2] or int(match[1])<4:
            raise ArchiveError('ARCHIVE_REGISTRY_RECEIPT_INVALID')

    def put(self,kind,cycle_id,payload):
        if kind not in KINDS or not re.fullmatch(r'RUN-\d{8}-\d{6}',cycle_id or ''):
            raise ArchiveError('ARCHIVE_KIND_OR_RUN_INVALID')
        text=canonical(payload)
        if len(text.encode())>MAX_PAYLOAD_BYTES:raise ArchiveError('ARCHIVE_PAYLOAD_TOO_LARGE')
        sha=digest(text);key=digest(kind+'\n'+cycle_id+'\n'+sha)
        result=self._call('put',kind=kind,cycle_id=cycle_id,payload_json=text,payload_sha256=sha,object_id=key)
        if result.get('object_id')!=key or result.get('payload_sha256')!=sha:
            raise ArchiveError('ARCHIVE_ACK_MISMATCH')
        self._receipt(result)
        read=self.get(key)
        if read['payload_json']!=text or read.get('file_id')!=result.get('file_id') or read.get('registry_range')!=result.get('registry_range'):
            raise ArchiveError('ARCHIVE_READBACK_MISMATCH')
        return dict(version=VERSION,object_id=key,payload_sha256=sha,file_id=result['file_id'],
                    registry_range=result['registry_range'],durable_verified=True,
                    verified_at=int(self.clock()),ticket_authorized=False)


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['status','self-test']);a=p.parse_args()
    config=configuration_status()
    if not config['configured']:
        print(json.dumps(dict(config,error='ARCHIVE_CONFIGURATION_REQUIRED')));return 2
    try:
        client=ArchiveClient.from_env()
        if a.command=='status':result=client.status()
        else:
            run=time.strftime('RUN-%Y%m%d-%H%M%S',time.gmtime())
            marker=dict(kind='technical archive test',nonce=secrets.token_hex(8),no_account_data=True)
            receipt=client.put('TECHNICAL_TEST',run,marker)
            # Fresh client simulates loss of local process state.
            recovered=ArchiveClient.from_env().get(receipt['object_id'])
            if json.loads(recovered['payload_json'])!=marker:raise ArchiveError('ARCHIVE_RESTART_RECOVERY_FAILED')
            result=dict(receipt,restart_recovery_verified=True)
        # Never print private payload_json or credentials.
        print(json.dumps({k:v for k,v in result.items() if k!='payload_json'}));return 0
    except ArchiveError as exc:
        print(json.dumps(dict(error=str(exc),durable_verified=False,ticket_authorized=False)));return 2


if __name__=='__main__':raise SystemExit(main())
