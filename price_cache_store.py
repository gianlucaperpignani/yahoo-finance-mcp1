"""1.0.1: authenticated incremental compressed price cache, bounded background IO.

Preserves acquisition timestamps; financial freshness is rechecked by server.
No Registry row per price batch, no broker access, no synthetic prices.
"""
import base64
import copy
import hashlib
import json
import threading
import time
import zlib
from archive_client import ArchiveClient, ArchiveError, canonical, digest, MAX_PAYLOAD_BYTES, configuration_status

VERSION='1.0.1'
BUCKETS=32
MAX_AGE=4*86400
MAX_BLOB=512000
MAX_RAM_BYTES=64*1024*1024


def bucket(key):return str(int(hashlib.sha256(key.encode()).hexdigest()[:8],16)%BUCKETS)


def verify_record(result, kind=None):
    text=result.get('payload_json');sha=result.get('payload_sha256')
    if not isinstance(text,str) or len(text.encode())>MAX_PAYLOAD_BYTES or digest(text)!=sha:
        raise ArchiveError('PRICE_CACHE_HASH_INVALID')
    payload=json.loads(text);actual=payload.get('kind')
    if actual not in {'PRICE_CACHE_BATCH','PRICE_CACHE_MANIFEST'} or (kind and actual!=kind):
        raise ArchiveError('PRICE_CACHE_KIND_INVALID')
    if payload.get('schema')!=1 or digest(actual+'\n'+sha)!=result.get('object_id'):
        raise ArchiveError('PRICE_CACHE_ID_INVALID')
    return payload


class Store:
    def __init__(self, client):self.client=client
    def head(self):return self.client._call('cache_head').get('head')
    def get(self,key,kind=None):return verify_record(self.client._call('cache_get',object_id=key),kind)
    def put(self,payload,base=None):
        text=canonical(payload)
        if len(text.encode())>MAX_PAYLOAD_BYTES:raise ArchiveError('PRICE_CACHE_BATCH_TOO_LARGE')
        sha=digest(text);key=digest(payload['kind']+'\n'+sha)
        action='cache_commit' if payload['kind']=='PRICE_CACHE_MANIFEST' else 'cache_put'
        result=self.client._call(action,object_id=key,payload_json=text,payload_sha256=sha,base_head=base)
        if result.get('object_id')!=key:raise ArchiveError('PRICE_CACHE_ACK_INVALID')
        verify_record(result,payload['kind'])
        if self.get(key,payload['kind'])!=payload:raise ArchiveError('PRICE_CACHE_READBACK_INVALID')
        if action=='cache_commit' and (self.head() or {}).get('object_id')!=key:
            raise ArchiveError('PRICE_CACHE_HEAD_READBACK_INVALID')
        return key


def encode_entry(key,entry):
    if not key.startswith('s:') or 'blob' not in entry:raise ArchiveError('PRICE_CACHE_ENTRY_INVALID')
    return dict(key=key,entry={k:v for k,v in entry.items() if k!='blob'},blob=base64.b64encode(entry['blob']).decode())


def decode_entry(row,now):
    key=row['key'];entry=dict(row['entry']);stamp=entry.get('ts')
    if not isinstance(key,str) or not key.startswith('s:') or len(key)>200 or not isinstance(stamp,(int,float)) or not 0<=now-stamp<MAX_AGE:
        return None
    blob=base64.b64decode(row['blob'],validate=True)
    if len(blob)>MAX_BLOB:raise ArchiveError('PRICE_CACHE_BLOB_TOO_LARGE')
    decoder=zlib.decompressobj();raw=decoder.decompress(blob,4_000_001)
    if len(raw)>4_000_000 or not decoder.eof or decoder.unused_data:raise ArchiveError('PRICE_CACHE_BLOB_INVALID')
    payload=json.loads(raw)
    if not isinstance(payload,dict) or payload.get('interval')!='1d' or not isinstance(payload.get('history'),list):
        raise ArchiveError('PRICE_CACHE_PAYLOAD_INVALID')
    # Use stored metadata only if it matches the actual payload bytes.
    if entry.get('coverage_end')!=payload.get('coverage_end') or key.split(':')[1]!=payload.get('ticker'):
        raise ArchiveError('PRICE_CACHE_METADATA_MISMATCH')
    entry.update(blob=blob,blob_bytes=len(blob))
    return key,entry


class PriceCache:
    def __init__(self,factory=None,clock=time.time):
        self.factory=factory or (lambda:Store(ArchiveClient.from_env()))
        self.clock=clock;self.lock=threading.Lock();self.busy=False;self.ready=False
        self.refs={};self.head_id=None;self.verified={};self.last_attempt=0
        self.info=dict(version=VERSION,state='NOT_STARTED',restored_entries=0,verified_entries=0)
        self.restored=set();self.cursor=0
    def is_verified(self,key,entry):
        return self.verified.get(key)==(entry.get('ts'),hashlib.sha256(entry.get('blob',b'')).hexdigest())
    def _signature(self,entry):return entry.get('ts'),hashlib.sha256(entry['blob']).hexdigest()
    def status(self):
        with self.lock:return copy.deepcopy(self.info)
    def tick(self,cache,cache_lock,compat,max_items=1100):
        if not configuration_status()['configured']:return
        with self.lock:
            if self.busy or self.clock()-self.last_attempt<60:return
            self.busy=True;self.last_attempt=self.clock();self.info['state']='RESTORING' if not self.ready else 'SAVING'
        threading.Thread(target=self._work,args=(cache,cache_lock,compat,max_items),daemon=True,name='price-cache-io').start()
    def _work(self,cache,cache_lock,compat,max_items):
        try:
            store=self.factory()
            if not self.ready:self.restore(store,cache,cache_lock,compat,max_items)
            else:self.flush(store,cache,cache_lock,compat)
            with self.lock:self.info.update(state='READY',error=None,verified_entries=len(self.verified))
        except ArchiveError as exc:
            if str(exc) in {'ARCHIVE_REMOTE_REJECTED:CACHE_HEAD_CONFLICT','PRICE_CACHE_HEAD_READBACK_INVALID'}:self.ready=False
            with self.lock:self.info.update(state='ERROR',error=str(exc))
        except Exception:
            with self.lock:self.info.update(state='ERROR',error='PRICE_CACHE_IO_FAILED')
        finally:
            with self.lock:self.busy=False
    def restore(self,store,cache,lock,compat,max_items):
        head=store.head();refs={}
        if head:
            manifest=store.get(head['object_id'],'PRICE_CACHE_MANIFEST')
            if manifest.get('compat')==compat:
                refs=manifest.get('buckets',{})
                if not isinstance(refs,dict) or len(refs)>BUCKETS:raise ArchiveError('PRICE_CACHE_MANIFEST_INVALID')
                for name,key in sorted(refs.items()):
                    part=store.get(key,'PRICE_CACHE_BATCH')
                    if part.get('compat')!=compat or str(part.get('bucket'))!=name:raise ArchiveError('PRICE_CACHE_BUCKET_INVALID')
                    rows=part.get('entries',[])
                    if not isinstance(rows,list) or len(rows)>80:raise ArchiveError('PRICE_CACHE_BUCKET_TOO_LARGE')
                    for row in rows:
                        decoded=decode_entry(row,self.clock())
                        if not decoded:continue
                        k,entry=decoded
                        if bucket(k)!=name:raise ArchiveError('PRICE_CACHE_BUCKET_INVALID')
                        with lock:
                            used=sum(v.get('blob_bytes',0) for v in cache.values())
                            if len(cache)>=max_items or used+len(entry['blob'])>MAX_RAM_BYTES:continue
                            if k not in cache or cache[k]['ts']<entry['ts']:cache[k]=entry
                            if cache[k]['ts']==entry['ts']:
                                self.verified[k]=self._signature(entry)
                                self.restored.add(k);self.info['restored_entries']=len(self.restored)
        self.head_id=head['object_id'] if head else None;self.refs=refs;self.ready=True
    def flush(self,store,cache,lock,compat):
        with lock:
            snapshot={k:dict(v) for k,v in cache.items() if k.startswith('s:') and len(k.split(':'))==6 and k.split(':')[3]=='1d' and 'blob' in v and 0<=self.clock()-v['ts']<MAX_AGE}
        dirty=sorted({bucket(k) for k,v in snapshot.items() if not self.is_verified(k,v)})
        if not dirty:return
        refs=dict(self.refs);saved={}
        ordered=sorted(dirty,key=lambda n:(int(n)-self.cursor)%BUCKETS)
        for name in ordered[:2]:
            rows=[encode_entry(k,v) for k,v in snapshot.items() if bucket(k)==name]
            part=dict(kind='PRICE_CACHE_BATCH',schema=1,compat=compat,bucket=name,entries=rows)
            refs[name]=store.put(part)
            for k,v in snapshot.items():
                if bucket(k)==name:saved[k]=self._signature(v)
        manifest=dict(kind='PRICE_CACHE_MANIFEST',schema=1,compat=compat,buckets=refs)
        head_id=store.put(manifest,base=self.head_id)
        self.refs=refs;self.head_id=head_id;self.verified.update(saved)
        self.verified={k:v for k,v in self.verified.items() if k in snapshot}
        self.cursor=(int(ordered[min(1,len(ordered)-1)])+1)%BUCKETS


runtime=PriceCache()


def _after_fork():
    runtime.lock=threading.Lock();runtime.busy=False;runtime.ready=False
    runtime.last_attempt=0;runtime.refs={};runtime.head_id=None;runtime.verified={}
    runtime.restored=set();runtime.info=dict(version=VERSION,state='NOT_STARTED',restored_entries=0,verified_entries=0)

import os
if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_child=_after_fork)
