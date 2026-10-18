import io,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import etoro_history as e
from urllib.error import HTTPError
class Agent(unittest.TestCase):
 def test_explicit_identification(self):
  with patch.dict(e.os.environ,{'ETORO_API_KEY':'test-a','ETORO_USER_KEY':'test-b','ETORO_ACCESS_TOKEN':''}):
   headers=e.auth_headers()
  self.assertEqual(headers['User-Agent'],e.USER_AGENT)
  self.assertIn('yahoo-finance-mcp1.onrender.com',headers['User-Agent'])
  self.assertNotIn('Mozilla',headers['User-Agent'])
 def test_json_1010_classification(self):
  body=json.dumps({'title':'Error 1010: Access denied','detail':'Blocked based on browser signature.'}).encode()
  exc=HTTPError('unused',403,'unused',{'Content-Type':'application/json'},io.BytesIO(body))
  self.assertEqual(e.diagnose_http_error(exc)['category'],'EDGE_OR_WAF_REJECTION')
if __name__=='__main__':unittest.main()
