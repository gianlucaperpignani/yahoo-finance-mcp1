"""1.0.0: verify existing Registry formulas and parsed quantitative mandate anchors."""
import hashlib
import json
import math
import re
from pathlib import Path
from policy_contract import T,validate_mandate,load_policy


def native_formulas(response):
    data=response.get('structuredContent') or response;out={}
    for sh in data.get('sheets',[]):
        for block in sh.get('data',[]):
            for i,row in enumerate(block.get('rowData',[])):
                for j,cell in enumerate(row.get('values',[])):
                    f=cell.get('userEnteredValue',{}).get('formulaValue')
                    if f is None:continue
                    n=block.get('startColumn',0)+j;col=''
                    while n>=0:col=chr(65+n%26)+col;n=n//26-1
                    key=sh['properties']['title']+'!'+col+str(block.get('startRow',0)+i+1)
                    out[key]=hashlib.sha256(f.encode()).hexdigest()
    return out


def audit(text,response):
    version=re.search(r'Versione:\s*(CONS-[\d-]+-r\d+)',text)
    if not version:raise ValueError('MANDATE_VERSION_MISSING')
    validate_mandate(version[1],hashlib.sha256(text.encode()).hexdigest())
    found={};errors=[]
    rules=[
        (r'\| Alert \| Oltre ([\d,]+)% \| Oltre ([\d,]+)%',('var99_daily_alert_pct','var99_weekly_alert_pct')),
        (r'\| Critico \| Oltre ([\d,]+)% \| Oltre ([\d,]+)%',('var99_daily_critical_pct','var99_weekly_critical_pct')),
        (r'Valore assoluto oltre ([\d,]+).*?positivo oltre ([\d,]+)',('correlation_abs_alert','correlation_positive_critical')),
        (r'gruppo correlato risultante oltre ([\d,]+)%',('correlated_group_purchase_limit_pct',)),
        (r'Fasce ordinarie ([\d,]+)–([\d,]+)% / ([\d,]+)–([\d,]+)%',('invested_min_pct','invested_max_pct','cash_min_pct','cash_max_pct')),
        (r'\| Singolo emittente \| Alert oltre ([\d,]+)% \| Ulteriori acquisti oltre ([\d,]+)%',('issuer_alert_pct','issuer_purchase_limit_pct')),
        (r'\| Settore, tema o gruppo correlato \| Alert oltre ([\d,]+)% \| Nuova esposizione oltre ([\d,]+)%',('sector_theme_alert_pct','sector_theme_purchase_limit_pct')),
        (r'rendimenti giornalieri rettificati: almeno (\d+) sedute;.*?meno di (\d+)',('history_ordinary_returns','history_min_returns')),
        (r'RVOL ≥ ([\d,]+).*?; ≥ ([\d,]+)',('rvol_cause_required','rvol_extreme')),
        (r'Riconcilia valore calcolato.*?maggiore tra ([\d,]+) € e ([\d,]+)%',('reconciliation_floor_eur','reconciliation_equity_fraction'))]
    for pattern,keys in rules:
        m=re.search(pattern,text)
        if not m:errors.append('MANDATE_ANCHOR_MISSING:'+','.join(keys));continue
        for key,value in zip(keys,m.groups()):
            number=float(value.replace(',','.'))
            if key=='reconciliation_equity_fraction':number/=100
            found[key]=number
            if not math.isclose(number,T[key],rel_tol=1e-12):errors.append('THRESHOLD_MISMATCH:'+key)
    expected=load_policy('registry_formula_contract.json')['formula_hashes'];actual=native_formulas(response)
    for cell,sha in expected.items():
        if actual.get(cell)!=sha:errors.append('REGISTRY_FORMULA_MISMATCH:'+cell)
    return dict(status='PASS' if not errors else 'CONFLICT',errors=errors,thresholds_checked=len(found),
        formulas_checked=len(expected),mandate_version=version[1],ticket_authorized=False)
