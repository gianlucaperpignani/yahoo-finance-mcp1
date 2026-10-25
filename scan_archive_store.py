"""1.0.0: durable scanner-only objects; no account data or broker operations."""
import json
import re
import threading
from archive_client import ArchiveClient, ArchiveError, configuration_status, digest as text_digest
from adaptive_method import digest

VERSION = '1.0.0'
RECOVERY_SLOTS = threading.BoundedSemaphore(2)


def enabled():
    return configuration_status()['configured']


def validate(artifact):
    if not isinstance(artifact, dict) or not isinstance(artifact.get('opportunities'), list):
        raise ArchiveError('SCAN_ARCHIVE_SCHEMA_INVALID')
    if digest({k:v for k,v in artifact.items() if k!='archive_id'}) != artifact.get('archive_id'):
        raise ArchiveError('SCAN_ARCHIVE_HASH_INVALID')
    return artifact


def put(job_id, artifact, client=None):
    validate(artifact)
    if not re.fullmatch(r'cycle-\d+', job_id):
        raise ArchiveError('SCAN_JOB_ID_INVALID')
    # Stable cycle identity across retries, derived from the actual job creation.
    from datetime import datetime, timezone
    run = datetime.fromtimestamp(int(job_id[6:])/1e9, timezone.utc).strftime('RUN-%Y%m%d-%H%M%S')
    wrapper = dict(kind='SCAN_ARCHIVE', cycle_id=run, job_id=job_id, archive=artifact)
    return (client or ArchiveClient.from_env()).put('SCAN_ARCHIVE', run, wrapper)


def get(job_id, object_id, client=None):
    if not re.fullmatch(r'cycle-\d+', job_id) or not re.fullmatch(r'[0-9a-f]{64}', object_id or ''):
        raise ArchiveError('SCAN_ARCHIVE_REFERENCE_INVALID')
    if not RECOVERY_SLOTS.acquire(blocking=False):
        raise ArchiveError('SCAN_ARCHIVE_RECOVERY_BUSY')
    try:
        result = (client or ArchiveClient.from_env()).get(object_id)
    finally:
        RECOVERY_SLOTS.release()
    wrapper = json.loads(result['payload_json'])
    # Bind the signed hash to the SCAN_ARCHIVE namespace: this public download
    # route must never retrieve RISK_BUNDLE, COHORT_STORE or other private kinds.
    run = wrapper.get('cycle_id', '')
    expected = text_digest('SCAN_ARCHIVE\n'+run+'\n'+result['payload_sha256'])
    if expected != object_id or wrapper.get('kind') != 'SCAN_ARCHIVE' or wrapper.get('job_id') != job_id:
        raise ArchiveError('SCAN_ARCHIVE_NAMESPACE_MISMATCH')
    return validate(wrapper.get('archive'))
