"""1.0.0: bounded, optional background archive probe; no financial collection.

One immutable synthetic object is reused across processes. A pre-existing object
proves retrieval from remote storage, not by itself a controlled Render restart.
No public route accepts payloads, archive IDs or secrets.
"""
import copy
import os
import threading
import time
from archive_client import ArchiveClient, ArchiveError, canonical, digest, configuration_status

VERSION = '1.0.0'
PROBE_RUN = 'RUN-20261025-000000'
PROBE_PAYLOAD = {'kind': 'persistent archive runtime probe', 'schema': 1, 'no_account_data': True}
PROBE_SHA = digest(canonical(PROBE_PAYLOAD))
PROBE_ID = digest('TECHNICAL_TEST\n' + PROBE_RUN + '\n' + PROBE_SHA)


def probe(factory=ArchiveClient.from_env):
    client = factory()
    client.status()
    previous = False
    try:
        recovered = client.get(PROBE_ID)
        if recovered['payload_json'] != canonical(PROBE_PAYLOAD):
            raise ArchiveError('ARCHIVE_PROBE_CONTENT_MISMATCH')
        previous = True
    except ArchiveError as exc:
        if str(exc) != 'ARCHIVE_REMOTE_REJECTED:ARCHIVE_OBJECT_NOT_FOUND':
            raise
    receipt = client.put('TECHNICAL_TEST', PROBE_RUN, PROBE_PAYLOAD)
    recovered = factory().get(PROBE_ID)
    if recovered['payload_json'] != canonical(PROBE_PAYLOAD):
        raise ArchiveError('ARCHIVE_PROBE_CONTENT_MISMATCH')
    # Deliberately exclude Drive IDs, Registry ranges, URLs and all private data.
    return dict(state='PASS', durable_storage_verified=receipt['durable_verified'],
                previous_object_recovered=previous, fresh_client_readback_verified=True,
                object_sha256=PROBE_ID, controlled_restart_verified=False)


class ArchiveRuntime:
    def __init__(self, factory=ArchiveClient.from_env, config=configuration_status):
        self.factory = factory
        self.config = config
        self.lock = threading.Lock()
        self.pid = None
        self.state = dict(state='NOT_STARTED', durable_storage_verified=False)

    def start(self):
        """At most one bounded probe per serving PID; no automatic retry loop."""
        with self.lock:
            pid = os.getpid()
            if self.pid == pid:
                return
            self.pid = pid
            cfg = self.config()
            self.state = dict(version=VERSION, configured=cfg['configured'],
                              state='RUNNING' if cfg['configured'] else 'NOT_CONFIGURED',
                              durable_storage_verified=False, started_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            if cfg['configured']:
                threading.Thread(target=self._run, args=(pid,), name='archive-probe', daemon=True).start()

    def _run(self, pid):
        started = time.monotonic()
        try:
            result = probe(self.factory)
        except ArchiveError as exc:
            # ArchiveError codes are sanitized by the client; never expose causes.
            result = dict(state='ERROR', error=str(exc), durable_storage_verified=False)
        except Exception:
            result = dict(state='ERROR', error='ARCHIVE_PROBE_FAILED', durable_storage_verified=False)
        with self.lock:
            if self.pid == pid:
                self.state.update(result, duration_s=round(time.monotonic()-started, 2))

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)


runtime = ArchiveRuntime()
