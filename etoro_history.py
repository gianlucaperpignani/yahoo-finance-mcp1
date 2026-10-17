"""eToro read-only HTTP adapter 1.0.0. No account or trading endpoints."""
import copy
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from prepare_etoro_history import prepare

VERSION = '1.0.0'
BASE = 'https://public-api.etoro.com'
_LOCK = threading.Lock()
_CACHE = {}
_NEXT_REQUEST = 0.0

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def auth_headers():
    token = os.environ.get('ETORO_ACCESS_TOKEN', '').strip()
    api = os.environ.get('ETORO_API_KEY', '').strip()
    user = os.environ.get('ETORO_USER_KEY', '').strip()
    if token and (api or user):
        raise ValueError('Configurare Bearer oppure coppia chiavi eToro, non entrambi')
    if token:
        result = {'Authorization': 'Bearer ' + token}
    elif api and user:
        result = {'x-api-key': api, 'x-user-key': user}
    else:
        raise ValueError('ETORO_AUTH_NOT_CONFIGURED: impostare credenziali su Render')
    result.update({'x-request-id': str(uuid.uuid4()), 'Accept': 'application/json'})
    return result


def status():
    try:
        auth_headers()
        configured = True
    except ValueError:
        configured = False
    return {'adapter_version': VERSION, 'credentials_configured': configured,
            'read_only': True, 'risk_source_enabled': False,
            'adjustments_verified': False}


def fetch(entry):
    """One bounded GET; cache coalesces concurrent requests, global 1 req/s budget.

    Never log response bodies/headers; never retry expired tokens or follow redirects.
    """
    global _NEXT_REQUEST
    headers = auth_headers()
    instrument_id = entry['instrument']['instrumentId']
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE.get(instrument_id)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])
        if now < _NEXT_REQUEST:
            raise ValueError('ETORO_RATE_LIMIT: riprovare dopo la pausa API')
        _NEXT_REQUEST = now + 1.0
        path = f'/api/v1/market-data/instruments/{instrument_id}/history/candles/desc/OneDay/1000'
        req = Request(BASE + path, headers=headers, method='GET')
        try:
            with build_opener(NoRedirect()).open(req, timeout=15) as response:
                raw = response.read(2_000_001)
                if response.status != 200 or len(raw) > 2_000_000:
                    raise ValueError('ETORO_RESPONSE_INVALID')
                body = json.loads(raw)
        except HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = max(60, int(exc.headers.get('Retry-After', '60')))
                except (TypeError, ValueError):
                    delay = 60
                _NEXT_REQUEST = time.monotonic() + delay
            raise ValueError(f'ETORO_HTTP_{exc.code}: verificare accesso/quota API') from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise ValueError('ETORO_FETCH_FAILED: errore rete o risposta JSON') from None
        result = {'statusCode': 200, 'isSuccess': True, 'body': body,
                  'bodyTruncated': False}
        _CACHE[instrument_id] = (time.monotonic() + 1200, result)
        return copy.deepcopy(result)


def get_history(ticker, registry, calendar_factory, as_of):
    ticker = str(ticker or '').upper()
    if ticker not in registry:
        raise ValueError('ETORO_ETF_NOT_VERIFIED: ticker non presente nella mappa verificata')
    entry = copy.deepcopy(registry[ticker])
    if entry['instrument'].get('symbol') != ticker or entry['instrument'].get('type') != 'ETF':
        raise ValueError('ETORO_ETF_MAP_INVALID')
    body = fetch(entry)
    raw = body['body']
    starts = [r['fromDate'][:10] for g in raw.get('candles', []) for r in g.get('candles', [])]
    if not starts:
        raise ValueError('ETORO_HISTORY_EMPTY')
    calendar = calendar_factory(ticker=ticker, start=min(starts), end=as_of[:10])
    entry.update(as_of=datetime.now(timezone.utc).isoformat(), response=body, calendar=calendar)
    # Calendar end is exclusive; server wrapper supplies tomorrow.
    result = prepare(entry)
    result['adapter_version'] = VERSION
    result['mandate_changed'] = False
    result['risk_source_enabled'] = False
    result['ticket_authorized'] = False
    return result
