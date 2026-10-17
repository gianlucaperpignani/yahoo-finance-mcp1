"""Acquisizione BCE separata dal motore quantitativo, con cache verificata."""
import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from prepare_ecb_fx import HISTORY_URL, prepare_ecb


def acquire(cache_dir, start, end):
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    xml_path, meta_path = directory/'ecb_history.xml', directory/'ecb_history_meta.json'
    now = datetime.now(timezone.utc)
    if xml_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            body = xml_path.read_bytes()
            if (meta.get('url') == HISTORY_URL and meta.get('sha256') == hashlib.sha256(body).hexdigest()
                    and 0 <= (now-datetime.fromisoformat(meta['retrieved_at'])).total_seconds() < 86400):
                result = prepare_ecb(body, start, end, now.isoformat(), meta['retrieved_at'])
                age = (now-datetime.fromisoformat(meta['retrieved_at'])).total_seconds()
                if not result['errors'] or age < 900:
                    result['acquisition_cache_hit'] = True
                    return result
        except (ValueError, KeyError, TypeError):
            pass
    with urlopen(HISTORY_URL, timeout=30) as response:
        if urlparse(response.url).hostname != 'www.ecb.europa.eu':
            raise ValueError('Redirect fuori fonte ufficiale BCE')
        body = response.read(20_000_001)
    fetched = datetime.now(timezone.utc).isoformat()
    result = prepare_ecb(body, start, end, fetched, fetched)
    xml_path.write_bytes(body)
    meta_path.write_text(json.dumps({'url': HISTORY_URL, 'retrieved_at': fetched,
                                    'sha256': hashlib.sha256(body).hexdigest()}), encoding='utf-8')
    result['acquisition_cache_hit'] = False
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('cache-dir', 'start', 'end', 'output'):
        p.add_argument('--'+flag, required=True)
    args = p.parse_args()
    try:
        result = acquire(args.cache_dir, args.start, args.end)
    except Exception as exc:
        result = {'status': 'BLOCCATO_PER_DATI', 'errors': [str(exc)], 'usd_eur_history': None, 'calendar': None}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    return 2 if result.get('errors') else 0


if __name__ == '__main__':
    raise SystemExit(main())
