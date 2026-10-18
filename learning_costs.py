"""1.0.0: normalize documented read-only eToro cost previews.

The open preview does not attest exit charges. Require a sourced exit fee
policy or an actual close preview; never assume symmetric/zero exit costs.
Spread is already in observed buy/sell quotes and is not charged twice.
"""
import json
from adaptive_method import stamp
from candidate_selection import finite


def normalize_open(response,request,as_of,exit_fee_policy):
    if request.get('path')!='/api/v2/trading/info/costs':raise ValueError('COST_PREVIEW_ROUTE_UNVERIFIED')
    body=request['body']
    if body.get('action')!='open' or body.get('transaction')!='buy' or body.get('leverage')!=1 or body.get('orderCurrency')!='usd' or body.get('settlementType')!='real':raise ValueError('COST_PREVIEW_SCOPE_INVALID')
    if response.get('statusCode')!=200 or response.get('bodyTruncated') or not response.get('xRequestId'):raise ValueError('COST_PREVIEW_FAILED')
    d=json.loads(response['body']) if isinstance(response.get('body'),str) else response['body']
    if d.get('instrumentId')!=body.get('instrumentId'):raise ValueError('COST_PREVIEW_IDENTITY_MISMATCH')
    if not 0<=(stamp(as_of)-stamp(d['lastUpdated'])).total_seconds()<=300:raise ValueError('COST_PREVIEW_STALE')
    if not finite(body.get('amount')) or body['amount']<=0:raise ValueError('COST_NOTIONAL_INVALID')
    allowed={'transactionFee','markup','sdrt','marketSpread','overnightFee','overWeekendFee'};seen=set();fee=0.
    for c in d.get('costs',[]):
        k=c.get('costType')
        if k not in allowed or k in seen or c.get('currency')!='USD' or not finite(c.get('value')) or c['value']<0:raise ValueError('COST_COMPONENT_INVALID')
        seen.add(k)
        if k in {'overnightFee','overWeekendFee'} and c['value']!=0:raise ValueError('HOLDING_COST_MODEL_REQUIRED')
        if k in {'transactionFee','markup','sdrt'}:fee+=c['value']
    if 'transactionFee' not in seen:raise ValueError('TRANSACTION_FEE_NOT_REPORTED')
    policy=exit_fee_policy or {}
    if policy.get('instrument_id')!=d['instrumentId'] or policy.get('currency')!='USD' or not policy.get('source_reference') or policy.get('verified') is not True or not finite(policy.get('fee_usd')) or policy['fee_usd']<0:raise ValueError('EXIT_FEE_POLICY_REQUIRED')
    if stamp(policy['verified_at'])>stamp(as_of) or stamp(policy['valid_until'])<stamp(as_of):raise ValueError('EXIT_FEE_POLICY_NOT_CURRENT')
    return dict(currency='USD',instrument_id=d['instrumentId'],notional_usd=body['amount'],entry_fee_usd=fee,exit_fee_usd=policy['fee_usd'],
        timestamp=d['lastUpdated'],source_reference=response['xRequestId']+'; '+policy['source_reference'],raw_open_preview=d,
        price_spread_excluded_from_explicit_fees=True)
