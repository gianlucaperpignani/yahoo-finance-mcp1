"""1.0.0: download frozen public market opportunities directly into Python.

No universe or deferred tickers are printed into the chat. The scheduled caller
uses returned acquisition requests internally, persists raw responses and then
calls feedback_cycle. Existing eToro read tools supply broker data; no orders.
"""
import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse
from adaptive_method import digest

HOST='https://yahoo-finance-mcp1.onrender.com'


def download(path,opener=urlopen):
    if not re.fullmatch(r'/cycle-archive/cycle-\d+',path):raise ValueError('ARCHIVE_PATH_INVALID')
    with opener(HOST+path,timeout=30) as response:
        if urlparse(response.url).netloc!=urlparse(HOST).netloc:raise ValueError('ARCHIVE_REDIRECT_INVALID')
        blob=response.read(2000001)
        if len(blob)>2000000 or hashlib.sha256(blob).hexdigest()!=response.headers.get('X-Content-SHA256'):raise ValueError('ARCHIVE_TRANSPORT_HASH_MISMATCH')
    raw=gzip.decompress(blob)
    if len(raw)>10000000:raise ValueError('ARCHIVE_TOO_LARGE')
    archive=json.loads(raw)
    if digest({k:v for k,v in archive.items() if k!='archive_id'})!=archive['archive_id']:raise ValueError('ARCHIVE_CONTENT_HASH_MISMATCH')
    return archive


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('archive_path');p.add_argument('output');a=p.parse_args()
    archive=download(a.archive_path);Path(a.output).write_text(json.dumps(archive,ensure_ascii=False))
    print(json.dumps(dict(archive_id=archive['archive_id'],opportunities=len(archive['opportunities']),saved=True)))
