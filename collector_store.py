"""1.0.0: signed private checkpoint CAS, immutable raw objects and source import."""
import base64
import hashlib
import json
import zlib
from archive_client import ArchiveClient,ArchiveError,canonical,digest,MAX_PAYLOAD_BYTES

VERSION='1.0.0'

class Conflict(ArchiveError):pass


def decode_record(r,kind):
    text=r.get('payload_json')
    if not isinstance(text,str) or len(text.encode())>MAX_PAYLOAD_BYTES or digest(text)!=r.get('payload_sha256'):
        raise ArchiveError('COLLECTOR_HASH_INVALID')
    value=json.loads(text)
    if value.get('kind')!=kind or value.get('schema')!=1 or digest(kind+'\n'+r['payload_sha256'])!=r.get('object_id'):
        raise ArchiveError('COLLECTOR_NAMESPACE_INVALID')
    return value


class Store:
    def __init__(self,client=None):self.client=client or ArchiveClient.from_env();self.file_ids={}
    def load(self):
        r=self.client._call('learning_head')
        if not r.get('object_id'):return None,None
        value=decode_record(r,'COLLECTOR_STATE')
        return value['state'],r['object_id']
    def save(self,state,base):
        payload=dict(kind='COLLECTOR_STATE',schema=1,state=state,previous_head=base)
        result=self._put(payload,'learning_commit',base)
        read,head=self.load()
        if head!=result or read!=state:raise ArchiveError('COLLECTOR_CHECKPOINT_READBACK_FAILED')
        return head
    def _put(self,value,action,base=None):
        text=canonical(value)
        if len(text.encode())>MAX_PAYLOAD_BYTES:raise ArchiveError('COLLECTOR_OBJECT_TOO_LARGE')
        sha=digest(text);key=digest(value['kind']+'\n'+sha)
        try:r=self.client._call(action,payload_json=text,payload_sha256=sha,object_id=key,base_head=base)
        except ArchiveError as exc:
            if str(exc)=='ARCHIVE_REMOTE_REJECTED:LEARNING_HEAD_CONFLICT':raise Conflict(str(exc)) from None
            raise
        decode_record(r,value['kind'])
        if r.get('file_id'):self.file_ids[key]=r['file_id']
        if r.get('object_id')!=key:raise ArchiveError('COLLECTOR_ACK_INVALID')
        if action=='learning_put' and self.get(key)!=value['value']:raise ArchiveError('COLLECTOR_RAW_READBACK_FAILED')
        return key
    def put(self,value):return self._put(dict(kind='COLLECTOR_DATA',schema=1,value=value),'learning_put')
    def get(self,key):return decode_record(self.client._call('learning_get',object_id=key),'COLLECTOR_DATA')['value']
    def bootstrap(self):
        r=self.client._call('learning_bootstrap')
        config=r.get('config',{});chunks=r.get('chunks',[])
        if config.get('schema')!=1 or not config.get('account_fingerprint') or not chunks:
            raise ArchiveError('COLLECTOR_BOOTSTRAP_REQUIRED')
        ordered=sorted(chunks,key=lambda c:c['part'])
        if [c['part'] for c in ordered]!=list(range(1,ordered[0]['parts']+1)) or len({c['object_sha256'] for c in ordered})!=1:
            raise ArchiveError('COLLECTOR_BOOTSTRAP_CHUNKS_INVALID')
        zipped=base64.b64decode(''.join(c['data'] for c in ordered),validate=True)
        d=zlib.decompressobj();raw=d.decompress(zipped,20_000_001)
        if len(raw)>20_000_000 or not d.eof or hashlib.sha256(raw).hexdigest()!=ordered[0]['object_sha256']:
            raise ArchiveError('COLLECTOR_BOOTSTRAP_HASH_INVALID')
        if config.get('registry_seed_sha256')!=ordered[0]['object_sha256']:
            raise ArchiveError('COLLECTOR_BOOTSTRAP_NOT_APPROVED_SOURCE')
        artifact=json.loads(raw)
        if artifact.get('kind')!='SCHEDULED_COLLECTOR_INTEGRATION_1.0.0':raise ArchiveError('COLLECTOR_BOOTSTRAP_KIND_INVALID')
        return artifact['feedback'],config,ordered[0]['object_sha256']
    def tickets(self):return self.client._call('learning_tickets').get('tickets',[])
    def scan_refs(self):return self.client._call('learning_scan_refs').get('refs',[])
    def scanner_archive(self,reference):
        r=self.client.get(reference);payload=json.loads(r['payload_json']);run=payload.get('cycle_id','')
        if digest('SCAN_ARCHIVE\n'+run+'\n'+r['payload_sha256'])!=reference or payload.get('kind')!='SCAN_ARCHIVE':
            raise ArchiveError('COLLECTOR_SCAN_NAMESPACE_INVALID')
        return payload['archive']
