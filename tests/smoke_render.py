"""Read-only HTTP smoke: python tests/smoke_render.py https://YOUR-RENDER-URL"""
import json,sys,time
from urllib.request import urlopen
base=sys.argv[1].rstrip('/')
def get(path):
 with urlopen(base+path,timeout=30) as r:return json.load(r)
s=get('/status')
assert s['server_version']=='1.7.40' if 'server_version' in s else s['version']=='1.7.40'
assert s['etoro_isin_map_size']==6
assert s['level_b_cache_ttl_seconds']==900
assert s['level_b_a1_version']=='a1-volume-0.10-tolerance'
assert s['level_b_a2_version']=='a2-price-momentum-0.6-tolerance'
print(json.dumps({k:s.get(k) for k in ['server_version','version','etoro_isin_map_size','stooq_verified_etf_map_size','level_b_cache_ttl_seconds','level_b_a1_version','level_b_a2_version']},indent=2),flush=True)
for market in ('US','EU'):
 start=time.monotonic();deadline=start+900
 while time.monotonic()<deadline:
  r=get('/level-b/fusion?market='+market+'&max_results=3')
  state=r.get('job_status');print(market,state,'elapsed',round(time.monotonic()-start,1),flush=True)
  if state=='COMPLETE':break
  if state=='ERROR':raise RuntimeError(r)
  time.sleep(5)
 else:raise TimeoutError(market+': non COMPLETE entro 900s; worker non terminato da questo test')
 assert r.get('ticket_authorized') is False
print('HTTP smoke PASS; nessuna operazione o scrittura Registro eseguita')
