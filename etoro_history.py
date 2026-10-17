"""eToro read-only HTTP adapter 1.0.3. No account or trading endpoints."""
import base64
import logging
import re
from urllib.parse import quote, quote_plus

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

VERSION = '1.0.3'
BASE = 'https://public-api.etoro.com'
USER_AGENT = 'yahoo-finance-mcp1/1.7.41 (eToro market-data client; +https://yahoo-finance-mcp1.onrender.com)'
_LOCK = threading.Lock()
_CACHE = {}
_NEXT_REQUEST = 0.0
_LAST_DIAGNOSTIC = None

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
    result.update({'x-request-id': str(uuid.uuid4()), 'Accept': 'application/json', 'User-Agent': USER_AGENT})
    return result


def safe_text(value, limit=500):
    """Bound and redact provider messages before they reach public endpoints/logs."""
    text = str(value)[:20000]
    # Includes escaped/encoded forms if an upstream service echoes a credential.
    for key in ('ETORO_API_KEY', 'ETORO_USER_KEY', 'ETORO_ACCESS_TOKEN'):
        secret = os.environ.get(key, '').strip()
        if secret:
            variants = {secret, quote(secret, safe=''), quote_plus(secret),
                        base64.b64encode(secret.encode()).decode(),
                        json.dumps(secret)[1:-1]}
            for variant in sorted(variants, key=len, reverse=True):
                text = text.replace(variant, '[REDACTED]')
    text = re.sub(r'(?i)bearer\s+[^\s,;<>]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:x[-_])?(?:api[-_]?key|user[-_]?key)|access[-_]?token|authorization|password|secret)([\s\"\x27:=]+)[^\s,;}<>]+', r'\1\2[REDACTED]', text)
    text = re.sub(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', text)
    text = re.sub(r'[A-Za-z0-9_+/=.-]{24,}', '[OPAQUE_VALUE]', text)
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return text[:limit]


def json_error_details(raw, headers):
    """Only allowlisted error fields; exclude request dumps and credentials."""
    details, fields = [], []
    parse_status = 'NOT_JSON'
    try:
        document = json.loads(raw)
        parse_status = 'PARSED'
    except (ValueError, UnicodeError, RecursionError):
        document = None
    allowed = {'message', 'errormessage', 'error', 'errorcode', 'code', 'title',
               'detail', 'description', 'reason', 'status', 'statuscode', 'type'}
    containers = {'error', 'errors', 'innererror', 'innererrors', 'details', 'response',
                  'data', 'body', 'result', 'exception', 'extensions'}
    sensitive = {'request', 'headers', 'authorization', 'token', 'apikey', 'userkey',
                 'password', 'secret', 'accesstoken', 'requestbody'}
    references = {}
    def reference(key, value):
        # References must be UUID or W3C trace IDs, not arbitrary echoed strings.
        if isinstance(value, str) and (re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value)
                                      or re.fullmatch(r'[0-9a-fA-F]{32}', value)
                                      or re.fullmatch(r'00-[0-9a-fA-F]{32}-[0-9a-fA-F]{16}-0[01]', value)):
            if not any(os.environ.get(k, '').strip() == value for k in ('ETORO_API_KEY','ETORO_USER_KEY','ETORO_ACCESS_TOKEN')):
                references[key] = value
    def walk(obj, depth=0):
        if depth > 5 or len(details) >= 8:
            return
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:50]:
                normalized = re.sub('[^a-z]', '', str(key).lower())
                if normalized in sensitive:
                    continue
                if depth == 0:
                    fields.append(safe_text(key, 50))
                if normalized in {'requestid', 'correlationid', 'traceid'}:
                    reference(normalized, value)
                if normalized in containers and isinstance(value, str) and value.lstrip().startswith(('{', '[')):
                    try:value = json.loads(value)
                    except (ValueError, RecursionError):pass
                if normalized in allowed and isinstance(value, (str, int, float, bool)):
                    details.append({'field': safe_text(key, 50), 'value': safe_text(value)})
                elif normalized in containers:
                    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
                        try:value = json.loads(value)
                        except (ValueError, RecursionError):pass
                    walk(value, depth+1)
                if len(details) >= 8:
                    break
        elif isinstance(obj, list):
            for value in obj[:8]:
                if isinstance(value, str):
                    details.append({'field': 'errors', 'value': safe_text(value)})
                else:walk(value, depth+1)
                if len(details) >= 8:break
    walk(document)
    for header in ('x-request-id', 'x-correlation-id', 'request-id', 'trace-id'):
        reference(header, headers.get(header))
    return {'provider_error': details, 'provider_error_fields': fields[:20],
            'provider_request_references': references, 'json_parse_status': parse_status}


def diagnose_http_error(exc, client_request_id=None):
    """Classify a bounded error; publish sanitized allowlisted details only.

    Labels are clues, not proof of whether a key or account is valid.
    """
    try:
        raw = exc.read(16385)
    except Exception:
        raw = b''
    truncated = len(raw) > 16384
    raw = raw[:16384]
    text = raw.decode('utf-8', errors='replace').lower()
    headers = exc.headers or {}
    content_type = str(headers.get('Content-Type', '')).lower()
    html = 'text/html' in content_type or '<html' in text or '<!doctype html' in text
    challenge = str(headers.get('cf-mitigated', '')).lower() == 'challenge'
    category = 'CAUSE_NOT_DISCLOSED'
    if exc.code == 429:
        category = 'RATE_LIMIT'
    elif challenge or ('error 1010' in text and 'access denied' in text) or (html and any(x in text for x in (
            'cloudflare', 'cf-chl-', 'captcha', 'access denied', 'request blocked',
            'request could not be satisfied', 'just a moment'))):
        category = 'EDGE_OR_WAF_REJECTION'
    elif 'insufficient_scope' in text or 'insufficient scope' in text or 'missing scope' in text:
        category = 'SCOPE_REJECTED'
    elif any(x in text for x in ('expired token', 'token expired', 'expired key', 'key expired')):
        category = 'CREDENTIAL_EXPIRED'
    elif any(x in text for x in ('invalid api key', 'invalid user key', 'invalid_api_key',
                                  'invalid_user_key', 'invalid token', 'invalid_token')):
        category = 'CREDENTIAL_REJECTED'
    elif any(x in text for x in ('ip not allowed', 'ip address is not allowed', 'ip whitelist', 'ip allowlist')):
        category = 'IP_RESTRICTION_REPORTED'
    elif any(x in text for x in ('not entitled', 'entitlement', 'subscription required')):
        category = 'ENTITLEMENT_REPORTED'
    details = json_error_details(raw, headers)
    if client_request_id:
        details['client_request_id'] = client_request_id
    return {'http_status': int(exc.code), 'category': category, **details,
            'error_body_truncated': truncated,
            'response_format': 'HTML' if html else ('JSON' if 'json' in content_type else 'OTHER'),
            'body_inspected_bytes': len(raw), 'diagnostic_only': True,
            'checked_at_utc': datetime.now(timezone.utc).isoformat()}


def status():
    try:
        auth_headers()
        configured = True
    except ValueError:
        configured = False
    return {'adapter_version': VERSION, 'credentials_configured': configured,
            'read_only': True, 'risk_source_enabled': False,
            'http_user_agent': USER_AGENT,
            'adjustments_verified': False,
            'last_http_diagnostic': copy.deepcopy(_LAST_DIAGNOSTIC)}


def fetch(entry):
    """One bounded GET; cache coalesces concurrent requests, global 1 req/s budget.

    Never log response bodies/headers; never retry expired tokens or follow redirects.
    """
    global _NEXT_REQUEST, _LAST_DIAGNOSTIC
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
            _LAST_DIAGNOSTIC = diagnose_http_error(exc, headers['x-request-id'])
            logging.getLogger('yfs.etoro').warning('eToro HTTP diagnostic %s', json.dumps(_LAST_DIAGNOSTIC, ensure_ascii=True))
            if exc.code == 429:
                try:
                    delay = max(60, int(exc.headers.get('Retry-After', '60')))
                except (TypeError, ValueError):
                    delay = 60
                _NEXT_REQUEST = time.monotonic() + delay
            raise ValueError(f"ETORO_HTTP_{exc.code}: " + json.dumps(_LAST_DIAGNOSTIC, ensure_ascii=True)) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise ValueError('ETORO_FETCH_FAILED: errore rete o risposta JSON') from None
        _LAST_DIAGNOSTIC = {'http_status': 200, 'category': 'SUCCESS',
                            'checked_at_utc': datetime.now(timezone.utc).isoformat()}
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
