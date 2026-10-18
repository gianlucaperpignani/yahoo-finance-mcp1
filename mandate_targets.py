"""1.0.0: derive only the active initial target from the authenticated mandate."""
import re
import hashlib
from policy_contract import validate_mandate


def active_targets(text,version,sha256):
    if not isinstance(text,str) or hashlib.sha256(text.encode()).hexdigest()!=sha256:raise ValueError('TARGET_MANDATE_TEXT_HASH_MISMATCH')
    validate_mandate(version,sha256)
    match=re.search(r'\| Obiettivo iniziale \| Portafoglio a ([\d.]+)–([\d.]+) € lordi.*?indicativamente entro ([a-z]+) (\d{4})\.',text)
    if not match:raise ValueError('ACTIVE_TARGET_ANCHOR_MISSING')
    months={'gennaio':1,'febbraio':2,'marzo':3,'aprile':4,'maggio':5,'giugno':6,'luglio':7,'agosto':8,'settembre':9,'ottobre':10,'novembre':11,'dicembre':12}
    low,high=int(match[1].replace('.','')),int(match[2].replace('.',''))
    if low<=0 or high<low or match[3] not in months:raise ValueError('ACTIVE_TARGET_INVALID')
    return [dict(id='INITIAL_'+label,amount_eur=amount,reference=version+' section5; sha256='+sha256,
        deadline=match[4]+'-'+str(months[match[3]]).zfill(2),deadline_precision='MONTH',deadline_indicative=True,
        bound=label,future_reset_active=False) for label,amount in [('LOWER',low),('UPPER',high)]]
