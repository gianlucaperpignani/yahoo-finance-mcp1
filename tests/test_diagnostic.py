import io,json,sys,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import etoro_history as e
from urllib.error import HTTPError
class Diagnostics(unittest.TestCase):
 def check(self,text,expected,headers=None,status=403):
  err=HTTPError('https://example.invalid',status,'unused',headers or {'Content-Type':'application/json'},io.BytesIO(text.encode()))
  with patch.dict(e.os.environ, {'ETORO_USER_KEY':'SECRET'}):d=e.diagnose_http_error(err)
  self.assertEqual(d['category'],expected)
  self.assertNotIn('SECRET',json.dumps(d))
  return d
 def test_scope(self):self.check('{"error":"insufficient_scope", "token":"SECRET"}','SCOPE_REJECTED')
 def test_waf(self):self.check('<html>Cloudflare Access Denied SECRET</html>','EDGE_OR_WAF_REJECTION',{'Content-Type':'text/html'})
 def test_expired(self):self.check('expired token SECRET','CREDENTIAL_EXPIRED')
 def test_invalid(self):self.check('invalid user key SECRET','CREDENTIAL_REJECTED')
 def test_ip(self):self.check('ip not allowed SECRET','IP_RESTRICTION_REPORTED')
 def test_entitlement(self):self.check('not entitled SECRET','ENTITLEMENT_REPORTED')
 def test_unknown(self):self.check('Forbidden SECRET','CAUSE_NOT_DISCLOSED')
 def test_limit(self):self.check('SECRET','RATE_LIMIT',status=429)
 def test_bounded(self):self.assertEqual(self.check('x'*20000,'CAUSE_NOT_DISCLOSED')['body_inspected_bytes'],16384)
 def test_challenge(self):self.check('SECRET','EDGE_OR_WAF_REJECTION',{'cf-mitigated':'challenge'})
if __name__=='__main__':unittest.main()
