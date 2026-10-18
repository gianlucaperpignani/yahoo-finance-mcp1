import unittest,json,copy
from learning_costs import normalize_open


class CostTests(unittest.TestCase):
    def inputs(self):
        now='2026-10-19T14:00:00+00:00'
        request={'path':'/api/v2/trading/info/costs','body':dict(action='open',transaction='buy',instrumentId=1,settlementType='real',leverage=1,orderCurrency='usd',amount=1000)}
        raw=dict(statusCode=200,xRequestId='SYNTHETIC',body=json.dumps(dict(instrumentId=1,lastUpdated=now,costs=[dict(costType='transactionFee',currency='USD',value=1),dict(costType='marketSpread',currency='USD',value=.75)])))
        policy=dict(instrument_id=1,currency='USD',fee_usd=1,verified=True,source_reference='SYNTHETIC primary policy',verified_at=now,valid_until='2026-10-19T20:00:00+00:00')
        return raw,request,now,policy

    def test_open_preview_normalized_without_double_spread(self):
        result=normalize_open(*self.inputs());self.assertEqual(result['entry_fee_usd'],1);self.assertEqual(result['exit_fee_usd'],1)

    def test_exit_fee_is_not_guessed(self):
        raw,req,now,_=self.inputs()
        with self.assertRaisesRegex(ValueError,'EXIT_FEE_POLICY'):normalize_open(raw,req,now,None)

    def test_stale_preview_or_wrong_identity_rejected(self):
        raw,req,now,policy=self.inputs()
        with self.assertRaisesRegex(ValueError,'STALE'):normalize_open(raw,req,'2026-10-19T14:06:00+00:00',policy)
        req['body']['instrumentId']=2
        with self.assertRaisesRegex(ValueError,'IDENTITY'):normalize_open(raw,req,now,policy)


if __name__=='__main__':unittest.main()
