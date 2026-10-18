"""1.0.0: link real close records to immutable proposals and broker receipts."""
from adaptive_method import digest, stamp
from candidate_selection import finite


def match(trades,tickets):
    links={};issues=[]
    for trade in trades:
        from outcome_ledger import digest as close_digest
        key=close_digest({k:trade.get(k) for k in ('positionId','orderId','openTime','closeTime','units')})
        matches=[]
        for record in tickets:
            proposal=record.get('proposal',{});receipt=record.get('execution_receipt',{})
            if receipt.get('position_id')!=trade.get('positionId'):continue
            try:
                if digest(proposal)!=record['proposal_hash'] or receipt.get('proposal_hash')!=record['proposal_hash']:raise ValueError('PROPOSAL_CHANGED')
                if receipt.get('account')!='real' or receipt.get('status')!='EXECUTED' or not receipt.get('reference'):raise ValueError('EXECUTION_NOT_ATTESTED')
                if receipt.get('instrument_id')!=trade['market']['instrumentId'] or proposal.get('instrument_id')!=trade['market']['instrumentId']:raise ValueError('EXECUTION_IDENTITY_MISMATCH')
                if trade.get('direction')!='long' or proposal.get('direction')!='long' or receipt.get('direction')!='long' or any(v!=1 for v in (trade.get('leverage'),proposal.get('leverage'),receipt.get('leverage'))):raise ValueError('EXECUTION_DIRECTION_OR_LEVERAGE_MISMATCH')
                if not finite(trade.get('units')) or trade['units']<=0 or not finite(receipt.get('units')) or receipt['units']<trade['units']:raise ValueError('PARTIAL_CLOSE_UNITS_INVALID')
                if stamp(proposal['created_at'])>stamp(trade['openTime']):raise ValueError('PROPOSAL_AFTER_ENTRY')
                if abs((stamp(receipt['executed_at'])-stamp(trade['openTime'])).total_seconds())>300:raise ValueError('EXECUTION_TIME_MISMATCH')
                if not proposal.get('signal_id') or proposal.get('category') not in {'CORE','SWING','SPECULATIVO'}:raise ValueError('PROPOSAL_METADATA_MISSING')
                origin='AI_MODIFICATA' if record.get('user_decision')=='MODIFIED' else 'AI_ESEGUITA' if record.get('user_decision')=='CONFIRMED' and proposal.get('origin')=='AI' else 'INIZIATIVA_UTENTE'
                if origin=='AI_ESEGUITA':
                    exit_link=record.get('exit_attribution',{})
                    if exit_link.get('close_key')!=key or exit_link.get('status')!='PLAN_UNCHANGED' or not exit_link.get('reference'):raise ValueError('EXIT_ATTRIBUTION_NOT_VERIFIED')
                matches.append(dict(verified=origin=='AI_ESEGUITA',ticket_unchanged=origin=='AI_ESEGUITA',position_id=trade['positionId'],
                    signal_id=proposal['signal_id'],evidence_reference=receipt['reference'],category=proposal['category'],origin=origin,
                    forecast_probability=proposal.get('forecast_probability'),forecast_recorded_at=proposal['created_at'],
                    initial_risk_usd=proposal.get('initial_risk_usd'),benchmark_return=record.get('benchmark_return'),direction_correct=record.get('direction_correct')))
            except (ValueError,KeyError,TypeError) as exc:issues.append(dict(close_key=key,reason=str(exc)))
        if len(matches)==1:links[key]=matches[0]
        elif len(matches)>1:issues.append(dict(close_key=key,reason='AMBIGUOUS_EXECUTION_LINK'))
    return dict(links=links,issues=issues)
