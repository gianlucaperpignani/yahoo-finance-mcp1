"""Source contract fixtures reflect actual published v1.400 eToro fields."""
import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from learning_sources import Sources,Pending,exit_policy,rate_timestamp
from adaptive_method import stamp
from etoro_learning_client import ReadFailure

AT='2026-10-26T15:01:00+00:00'
class Broker:
    def __init__(self):self.calls=[]
    def read(self,path,**kw):
        self.calls.append((path,kw));body={}
        if path.endswith('/instruments'):body=dict(results=[dict(instrumentId=1,symbol='AAA',displayName='Synthetic Corp',type='Stocks',exchangeId=4)],pagination=dict(hasNext=False))
        elif path.endswith('/exchanges'):body=dict(exchangeInfo=[dict(exchangeID=4,exchangeDescription='NASDAQ')])
        elif path.endswith('/eligibility'):body=dict(eligibilities=[dict(instrumentId=i,allowOpenPosition=True,leverageConfigs=[dict(settlementType='real',direction='long',leverageValues=[1],isPotential=False,minPositionAmount=10)]) for i in [1,2]])
        elif path.endswith('/costs'):body=dict(instrumentId=kw['body']['instrumentId'],lastUpdated=AT,costs=[dict(costType='transactionFee',value=1,currency='USD'),dict(costType='markup',value=0,currency='USD'),dict(costType='overnightFee',value=0,currency='USD')])
        elif path.endswith('/rates'):body=dict(results=[dict(instrumentId=i,bid=100,ask=100.2,date=AT,quoteType='realtime') for i in [1,2]])
        return dict(statusCode=200,body=body,xRequestId='synthetic-source',bodyTruncated=False)

class SourceTests(unittest.TestCase):
    def context(self,id):return dict(metadata=dict(instrumentId=id,type='Stocks'),calendar_id='XNYS',currency='USD',price_unit='USD',indicators=dict(atr14=2,price_unit='USD',source_reference='synthetic daily ATR'),identity=dict(status='PASS',instrument_id=id,reference='synthetic matched identity'))
    def test_real_only_what_if_cost_and_live_entry_contract(self):
        broker=Broker();sources=Sources(None,None,None,broker)
        fee=dict(url='https://www.etoro.com/trading/fees/',sha256='a'*64,retrieved_at=AT,stocks_open_close_flat_schedule=True)
        with patch('learning_sources.now',return_value=AT),patch('learning_sources.fetch_fees',return_value=fee):
            result=sources.entry(dict(candidates=[dict(key='AAA')]),dict(AAA=self.context(1),SPY=self.context(2)),'SPY','test')
        self.assertEqual(result['quotes']['AAA']['ask'],100.2);self.assertEqual(result['terms']['AAA']['exit_fee_fraction'],.001)
        self.assertTrue(all('execution' not in p for p,k in broker.calls))
        self.assertEqual(len([p for p,k in broker.calls if p.endswith('/costs')]),1)
    def test_closed_session_makes_no_quote_or_cost_requests(self):
        broker=Broker();sources=Sources(None,None,None,broker)
        with patch('learning_sources.now',return_value='2026-10-25T15:00:00+00:00'):
            with self.assertRaisesRegex(Pending,'WAIT_OPEN_SESSION'):sources.entry(dict(candidates=[dict(key='AAA')]),dict(AAA=self.context(1),SPY=self.context(2)),'SPY','test')
        self.assertEqual(broker.calls,[])
    def test_unknown_currency_is_not_inferred_from_ticker(self):
        broker=Broker();history=lambda ticker:dict(name='Synthetic Corp',calendar='XPAR',major_currency='EUR',price_unit='EUR')
        sources=Sources(history,lambda ticker:dict(atr14=2),lambda **kw:dict(status='PASS'),broker)
        with patch('learning_sources.broker_quote_currency',side_effect=Pending('BROKER_QUOTE_CURRENCY_NOT_EXPOSED')):
            with self.assertRaisesRegex(Pending,'QUOTE_CURRENCY'):sources.contexts(['AAA'],'test')
    def test_exit_costs_require_source_and_reject_unpriced_extra_fees(self):
        broker=Broker();response=broker.read('/api/v2/trading/info/costs',body=dict(instrumentId=1));fee=dict(url='primary',sha256='a'*64,retrieved_at=AT,stocks_open_close_flat_schedule=True)
        self.assertEqual(exit_policy(dict(type='Stocks',instrumentId=1),response,fee,AT)['fee_usd'],1)
        response['body']['costs'][1]['value']=1
        with self.assertRaisesRegex(Pending,'ADDITIONAL'):exit_policy(dict(type='Stocks',instrumentId=1),response,fee,AT)
    def test_naive_broker_utc_quotes_are_normalized_without_changing_raw(self):
        broker=Broker();read=broker.read
        def naive(path,**kw):
            response=read(path,**kw)
            if path.endswith('/rates'):
                for q in response['body']['results']:q['date']='2026-10-26T15:00:59.507'
            return response
        broker.read=naive;sources=Sources(None,None,None,broker)
        fee=dict(url='primary',sha256='a'*64,retrieved_at=AT,stocks_open_close_flat_schedule=True)
        with patch('learning_sources.now',return_value=AT),patch('learning_sources.fetch_fees',return_value=fee):
            result=sources.entry(dict(candidates=[dict(key='AAA')]),dict(AAA=self.context(1),SPY=self.context(2)),'SPY','test')
        self.assertEqual(result['quotes']['AAA']['timestamp'],'2026-10-26T15:00:59.507000+00:00')
        self.assertEqual(result['raw_rates']['body']['results'][0]['date'],'2026-10-26T15:00:59.507')
        self.assertIn('UTC',result['quote_timestamp_evidence']['AAA']['reference'])
    def test_rate_timestamp_formats_and_generic_timestamps_remain_strict(self):
        expected='2026-10-26T15:01:00+00:00'
        for text in ['2026-10-26T15:01:00','2026-10-26T15:01:00Z','2026-10-26T17:01:00+02:00']:
            self.assertEqual(rate_timestamp(text),expected)
        self.assertEqual(rate_timestamp('2026-10-26T15:01:00.1234567'),'2026-10-26T15:01:00.123456+00:00')
        for invalid in [None,123,'','2026-10-26','2026-99-26T15:01:00','2026-10-26 15:01:00']:
            with self.assertRaisesRegex(ValueError,'ETORO_RATE_TIMESTAMP_INVALID'):rate_timestamp(invalid)
        with self.assertRaisesRegex(ValueError,'LEARNING_TIMEZONE_REQUIRED'):stamp('2026-10-26T15:01:00')
    def test_stale_future_and_delayed_quotes_still_wait(self):
        for date,kind in [('2026-10-26T14:55:59','realtime'),('2026-10-26T15:01:01','realtime'),('2026-10-26T15:01:00','delayed')]:
            broker=Broker();read=broker.read
            def modified(path,**kw):
                response=read(path,**kw)
                if path.endswith('/rates'):
                    for q in response['body']['results']:q.update(date=date,quoteType=kind)
                return response
            broker.read=modified;sources=Sources(None,None,None,broker)
            fee=dict(url='primary',sha256='a'*64,retrieved_at=AT,stocks_open_close_flat_schedule=True)
            with patch('learning_sources.now',return_value=AT),patch('learning_sources.fetch_fees',return_value=fee):
                with self.assertRaises(Pending):sources.entry(dict(candidates=[dict(key='AAA')]),dict(AAA=self.context(1),SPY=self.context(2)),'SPY','test')
    def test_fx_uses_same_documented_rate_timestamp_boundary(self):
        class FxBroker:
            def read(self,path,**kw):
                if path.endswith('/instruments'):body=dict(results=[dict(symbol='EURUSD',type='Forex',instrumentId=10)])
                else:body=dict(results=[dict(instrumentId=10,bid=1.1,ask=1.2,date='2026-10-26T15:00:59.507',quoteType='realtime')])
                return dict(statusCode=200,xRequestId='synthetic-fx',body=body)
        sources=Sources(None,None,None,FxBroker())
        with patch('learning_sources.now',return_value=AT):values,raw=sources.live_fx(['EUR'],'test')
        self.assertAlmostEqual(values['EUR'][0],1.15)
        self.assertIn('normalized=2026-10-26T15:00:59.507000+00:00',values['EUR'][1])
        self.assertEqual(raw[-1]['body']['results'][0]['date'],'2026-10-26T15:00:59.507')

if __name__=='__main__':unittest.main()
