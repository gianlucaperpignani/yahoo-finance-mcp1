import io,json,os,sys,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import etoro_history as e
from urllib.error import HTTPError
class Details(unittest.TestCase):
 def diagnose(self,body,headers=None):
  return e.diagnose_http_error(HTTPError('unused',403,'unused',headers or {'Content-Type':'application/json'},io.BytesIO(json.dumps(body).encode())))
 def test_message_and_code(self):
  d=self.diagnose({'errorCode':'Forbidden','errorMessage':'Application access is disabled'})
  self.assertEqual(d['provider_error'][1]['value'],'Application access is disabled')
 def test_nested(self):
  d=self.diagnose({'errors':[{'code':123,'message':'Permission denied'}]})
  self.assertEqual(d['provider_error'][1]['value'],'Permission denied')
 def test_excludes_echoed_headers(self):
  d=self.diagnose({'headers':{'Authorization':'SECRET'},'request':{'token':'SECRET'},'message':'Denied'})
  self.assertNotIn('SECRET',json.dumps(d))
 def test_environment_secret_echo(self):
  with patch.dict(os.environ,{'ETORO_USER_KEY':'my-real-secret'}):
   d=self.diagnose({'message':'Rejected my-real-secret'})
  self.assertNotIn('my-real-secret',json.dumps(d))
 def test_bearer_and_email_redacted(self):
  d=self.diagnose({'message':'Bearer abc123 for user@example.com at 10.0.0.1'})
  s=json.dumps(d);self.assertNotIn('abc123',s);self.assertNotIn('user@example.com',s);self.assertNotIn('10.0.0.1',s)
 def test_opaque_token_redacted(self):
  d=self.diagnose({'message':'Denied '+ 'abcdefgh'*10})
  self.assertNotIn('abcdefgh',json.dumps(d))
 def test_reference_ids(self):
  ref=str(uuid.uuid4());d=self.diagnose({'traceId':ref})
  self.assertEqual(d['provider_request_references']['traceid'],ref)
 def test_reject_arbitrary_reference(self):
  d=self.diagnose({'requestId':'SECRET'})
  self.assertFalse(d['provider_request_references'])
 def test_limits(self):
  d=self.diagnose({'errors':[{'message':'x'*600}]*20})
  self.assertLessEqual(len(d['provider_error']),8)
  self.assertTrue(all(len(x['value'])<=500 for x in d['provider_error']))
 def test_non_json_never_raw(self):
  err=HTTPError('unused',403,'unused',{},io.BytesIO(b'<html>arbitrary sensitive content</html>'))
  d=e.diagnose_http_error(err)
  self.assertNotIn('arbitrary',json.dumps(d));self.assertEqual(d['json_parse_status'],'NOT_JSON')
 def test_encoded_nested_json(self):
  d=self.diagnose({'error':json.dumps({'message':'Missing entitlement'})})
  self.assertEqual(d['provider_error'][0]['value'],'Missing entitlement')
 def test_client_correlation(self):
  ref=str(uuid.uuid4());err=HTTPError('unused',403,'unused',{},io.BytesIO(b'{}'))
  self.assertEqual(e.diagnose_http_error(err,ref)['client_request_id'],ref)
if __name__=='__main__':unittest.main()
