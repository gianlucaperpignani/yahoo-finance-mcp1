"""Private object/checkpoint integrity, conflict and source bootstrap checks."""
import base64,copy,hashlib,json,sys,unittest,zlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from archive_client import ArchiveError,canonical,digest
from collector_store import Store,Conflict

class Client:
    def __init__(self):self.objects={};self.head=None;self.bad_read=False;self.bootstrap_payload=None
    def _call(self,action,**kw):
        if action=='learning_bootstrap':return self.bootstrap_payload
        if action=='learning_head':
            if not self.head:return dict(object_id=None)
            return self.record(self.head)
        if action=='learning_get':return self.record(kw['object_id'])
        if action in {'learning_put','learning_commit'}:
            if action=='learning_commit' and kw['base_head']!=self.head and kw['object_id']!=self.head:raise ArchiveError('ARCHIVE_REMOTE_REJECTED:LEARNING_HEAD_CONFLICT')
            self.objects[kw['object_id']]=dict(payload_json=kw['payload_json'],payload_sha256=kw['payload_sha256'],object_id=kw['object_id'],file_id='synthetic-private-file')
            if action=='learning_commit':self.head=kw['object_id']
            return copy.deepcopy(self.objects[kw['object_id']])
        raise AssertionError(action)
    def record(self,key):
        r=copy.deepcopy(self.objects[key])
        if self.bad_read:r['payload_json']='{}'
        return r

class StoreTests(unittest.TestCase):
    def test_signed_namespace_and_checkpoint_recovery_after_ram_loss(self):
        client=Client();s=Store(client);ref=s.put({'private':'synthetic account read'})
        self.assertEqual(Store(client).get(ref),{'private':'synthetic account read'})
        head=s.save({'task':'pending'},None)
        self.assertEqual(Store(client).load(),({'task':'pending'},head))
        with self.assertRaises(Conflict):s.save({'task':'conflict'},None)
        self.assertEqual(s.save({'task':'pending'},None),head)
    def test_failed_readback_is_not_a_checkpoint_success(self):
        client=Client();s=Store(client);head=s.save({'task':1},None);client.bad_read=True
        with self.assertRaisesRegex(ArchiveError,'HASH'):s.load()
    def test_registry_seed_must_match_private_approved_bootstrap_hash(self):
        # Actual original Registro wrapper; the chunk kind has no version suffix,
        # but the compressed artifact itself is explicitly versioned.
        raw=canonical(dict(kind='SCHEDULED_COLLECTOR_INTEGRATION_1.0.0',feedback=dict(input={},output={}))).encode();sha=hashlib.sha256(raw).hexdigest()
        config=dict(schema=1,account_fingerprint='a'*64,registry_seed_sha256=sha)
        chunk=dict(part=1,parts=1,object_sha256=sha,data=base64.b64encode(zlib.compress(raw)).decode())
        client=Client();client.bootstrap_payload=dict(config=config,chunks=[chunk])
        self.assertEqual(Store(client).bootstrap()[2],sha)
        client.bootstrap_payload['config']['registry_seed_sha256']='b'*64
        with self.assertRaisesRegex(ArchiveError,'APPROVED_SOURCE'):Store(client).bootstrap()

if __name__=='__main__':unittest.main()
