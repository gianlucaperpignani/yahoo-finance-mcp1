import ast
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import etoro_history as e
from urllib.error import HTTPError

class HttpTests(unittest.TestCase):
    def setUp(self):
        e._CACHE.clear()
        e._NEXT_REQUEST = 0
    def test_no_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(e.status()['credentials_configured'])
            with self.assertRaises(ValueError): e.auth_headers()
    def test_keys_and_bearer_exclusive(self):
        with patch.dict(os.environ, {'ETORO_API_KEY':'a','ETORO_USER_KEY':'b','ETORO_ACCESS_TOKEN':'c'}, clear=True):
            with self.assertRaises(ValueError): e.auth_headers()
    def test_unique_request_id(self):
        with patch.dict(os.environ, {'ETORO_ACCESS_TOKEN':'test'}, clear=True):
            self.assertNotEqual(e.auth_headers()['x-request-id'],e.auth_headers()['x-request-id'])
            self.assertNotIn('x-api-key',e.auth_headers())
    def test_unknown_symbol_no_http(self):
        with patch.object(e,'fetch') as http:
            with self.assertRaises(ValueError):e.get_history('T',{},None,'2026-10-17T00:00:00Z')
            http.assert_not_called()
    def test_429_redacted_and_backoff(self):
        class Opener:
            def open(self,*a,**kw):raise HTTPError('secret',429,'secret',{'Retry-After':'80'},None)
        with patch.dict(os.environ,{'ETORO_ACCESS_TOKEN':'secret'},clear=True),patch.object(e,'build_opener',return_value=Opener()):
            with self.assertRaisesRegex(ValueError,'ETORO_HTTP_429') as exc:e.fetch({'instrument':{'instrumentId':3434}})
            self.assertNotIn('secret',str(exc.exception))
            self.assertGreater(e._NEXT_REQUEST,e.time.monotonic()+70)
    def test_redirect_blocked(self):
        self.assertIsNone(e.NoRedirect().redirect_request(None,None,None,None,None,None))
    def test_cache_and_get_only(self):
        calls=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return b'{"interval":"OneDay","candles":[]}'
        class Opener:
            def open(self,req,**kw):calls.append(req);return Response()
        with patch.dict(os.environ,{'ETORO_API_KEY':'a','ETORO_USER_KEY':'b'},clear=True),patch.object(e,'build_opener',return_value=Opener()):
            a=e.fetch({'instrument':{'instrumentId':3434}});a['body']['candles'].append('mutated')
            b=e.fetch({'instrument':{'instrumentId':3434}})
        self.assertEqual(b['body']['candles'],[])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].get_method(),'GET')
        self.assertTrue(calls[0].full_url.startswith(e.BASE+'/api/v1/market-data/'))
    def test_real_saved_responses(self):
        from prepare_etoro_history import prepare
        folder=Path('/tmp/etoro-history-live')
        if not folder.exists():self.skipTest('Fixture live locale non inclusa nel pacchetto')
        for ticker in ['CSPX','EIMI','WDEF']:
            data=json.loads((folder/(ticker+'-input.json')).read_text())
            result=prepare(data)
            self.assertEqual(result['coverage_end'],'2026-10-16')
            self.assertTrue(result['usable_for_screening'])
            self.assertFalse(result['usable_for_risk'])
            self.assertFalse(result['ticket_authorized'])
    def test_server_calendar_batches_and_full_adapter(self):
        from datetime import date, timedelta
        folder=Path('/tmp/etoro-history-live')
        if not folder.exists():self.skipTest('Fixture live locale non inclusa')
        source=(Path(__file__).resolve().parents[1]/'server.py').read_text()
        function=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='get_etf_history')
        data=json.loads((folder/'CSPX-input.json').read_text())
        calls=[]
        def calendar(ticker,start,end):
            self.assertLessEqual((date.fromisoformat(end)-date.fromisoformat(start)).days,400)
            calls.append((start,end))
            part=copy.deepcopy(data['calendar'])
            part.update(start=start,end=end)
            part['sessions']=[r for r in part['sessions'] if start<=r['date']<end]
            part['weekday_closures']=[]
            return part
        registry=json.loads((Path(__file__).resolve().parents[1]/'etoro_etf_map.json').read_text())
        scope=dict(etoro_history=e,ETORO_ETF_MAP=registry,date=date,timedelta=timedelta,
                   get_exchange_calendar=calendar,_utc_stamp=lambda:'2026-10-17T19:11:08Z')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'server-calendar','exec'),scope)
        with patch.object(e,'fetch',return_value=data['response']):
            # HTTP fetch normally decodes the body; fixture response contains JSON string.
            body=copy.deepcopy(data['response']);body['body']=json.loads(body['body'])
            with patch.object(e,'fetch',return_value=body):result=scope['get_etf_history']('CSPX.L')
        self.assertGreater(len(calls),1)
        self.assertEqual(result['coverage_end'],'2026-10-16')
        self.assertFalse(result['usable_for_risk'])
        self.assertFalse(result['mandate_changed'])

    def test_server_tool_and_route(self):
        source=(Path(__file__).resolve().parents[1]/'server.py').read_text()
        tree=ast.parse(source)
        self.assertIn('"name": "get_etf_history"',source)
        self.assertIn('@app.route("/etf-history"',source)
        self.assertIn('etoro_history.status()',source)

if __name__=='__main__':unittest.main()
