"""Synthetic wire/storage checks; no external calls or account data."""
import hashlib
import hmac
import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import URLError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from archive_client import ArchiveClient,ArchiveError,canonical,digest,configuration_status

URL='https://script.google.com/macros/s/testDeployment/exec'
SECRET='synthetic-test-secret-only-'+'a'*40

class Response(io.BytesIO):
    url='https://script.googleusercontent.com/macros/echo'

class Backend:
    def __init__(self):self.objects={};self.calls=[];self.corrupt_get=False
    def open(self,request,timeout):
        env=json.loads(request.data);text=env['message']
        assert hmac.compare_digest(env['signature'],hmac.new(SECRET.encode(),text.encode(),hashlib.sha256).hexdigest())
        req=json.loads(text);self.calls.append(req['action'])
        if req['action']=='put':
            self.objects.setdefault(req['object_id'],dict(req,file_id='synthetic-file-0001',registry_range='Registro!A200:Z200'))
        record=self.objects.get(req.get('object_id'),{})
        r={'ok':True,'nonce':req['nonce'],'object_id':req.get('object_id'),
           'payload_sha256':record.get('payload_sha256'),'file_id':record.get('file_id'),
           'registry_range':record.get('registry_range')}
        if req['action']=='get':r['payload_json']='{}' if self.corrupt_get else record['payload_json']
        message=canonical(r)
        return Response(canonical(dict(message=message,signature=hmac.new(SECRET.encode(),message.encode(),hashlib.sha256).hexdigest())).encode())

class ArchiveTests(unittest.TestCase):
    def setUp(self):self.backend=Backend();self.client=ArchiveClient(URL,SECRET,opener=self.backend.open,clock=lambda:1000)
    def test_write_readback_restart_and_idempotence(self):
        payload={'unicode':'chiusura €','rows':[1,2],'missing':None}
        a=self.client.put('TECHNICAL_TEST','RUN-20261025-120000',payload)
        self.assertTrue(a['durable_verified']);self.assertEqual(self.backend.calls,['put','get'])
        recovered=ArchiveClient(URL,SECRET,opener=self.backend.open).get(a['object_id'])
        self.assertEqual(json.loads(recovered['payload_json']),payload)
        b=self.client.put('TECHNICAL_TEST','RUN-20261025-120000',payload)
        self.assertEqual(a['object_id'],b['object_id']);self.assertEqual(len(self.backend.objects),1)
    def test_failed_readback_never_reports_durable(self):
        self.backend.corrupt_get=True
        with self.assertRaisesRegex(ArchiveError,'HASH'):self.client.put('CHECKPOINT','RUN-20261025-120000',{'x':1})
    def test_network_error_and_html_response_are_explicit(self):
        def fail(*a,**k):raise URLError('offline')
        with self.assertRaises(ArchiveError):ArchiveClient(URL,SECRET,opener=fail).status()
        with self.assertRaises(ArchiveError):ArchiveClient(URL,SECRET,opener=lambda *a,**k:Response(b'<html>Login</html>')).status()
    def test_unsigned_or_wrong_nonce_is_rejected(self):
        def reply(req,timeout):
            r={'ok':True,'nonce':'wrong'};m=canonical(r)
            return Response(canonical({'message':m,'signature':hmac.new(SECRET.encode(),m.encode(),hashlib.sha256).hexdigest()}).encode())
        with self.assertRaisesRegex(ArchiveError,'NONCE'):ArchiveClient(URL,SECRET,opener=reply).status()
        with self.assertRaises(ArchiveError):ArchiveClient(URL,SECRET,opener=lambda *a,**k:Response(b'{"ok":true}')).status()
    def test_invalid_config_payload_and_scope(self):
        self.assertFalse(configuration_status({})['configured'])
        self.assertFalse(configuration_status({'ARCHIVE_WEBAPP_URL':URL,'ARCHIVE_HMAC_SECRET':'x'})['configured'])
        for url in ['http://script.google.com/macros/s/x/exec','https://evil.example/exec',URL+'?secret=x']:
            with self.assertRaises(ArchiveError):ArchiveClient(url,SECRET)
        for kind,run in [('TRADE','RUN-20261025-120000'),('CHECKPOINT','not-a-run')]:
            with self.assertRaises(ArchiveError):self.client.put(kind,run,{})
        with self.assertRaises(ValueError):self.client.put('CHECKPOINT','RUN-20261025-120000',{'nan':float('nan')})
        with self.assertRaisesRegex(ArchiveError,'TOO_LARGE'):self.client.put('CHECKPOINT','RUN-20261025-120000',{'data':'x'*2_000_000})
        self.assertEqual(self.backend.calls,[])

if __name__=='__main__':unittest.main()
