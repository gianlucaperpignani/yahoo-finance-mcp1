"""Archive runtime failures must never authorize cache eviction or trading."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_client import ArchiveError, canonical
from archive_runtime import ArchiveRuntime, probe, PROBE_PAYLOAD, PROBE_ID


class Backend:
    def __init__(self):
        self.saved = False
        self.puts = 0
    def status(self):
        return {'ok': True}
    def get(self, key):
        assert key == PROBE_ID
        if not self.saved:
            raise ArchiveError('ARCHIVE_REMOTE_REJECTED:ARCHIVE_OBJECT_NOT_FOUND')
        return {'payload_json': canonical(PROBE_PAYLOAD)}
    def put(self, *args):
        self.saved = True
        self.puts += 1
        return {'durable_verified': True}


class RuntimeTests(unittest.TestCase):
    def test_first_write_then_prior_process_recovery(self):
        backend = Backend()
        self.assertFalse(probe(lambda: backend)['previous_object_recovered'])
        result = probe(lambda: backend)
        self.assertTrue(result['previous_object_recovered'])
        self.assertFalse(result['controlled_restart_verified'])
        self.assertTrue(result['durable_storage_verified'])

    def test_failed_read_does_not_write_or_claim_durability(self):
        backend = Backend()
        with patch.object(backend, 'get', side_effect=ArchiveError('ARCHIVE_REQUEST_OR_RESPONSE_FAILED')):
            with self.assertRaises(ArchiveError):
                probe(lambda: backend)
        self.assertEqual(backend.puts, 0)

    def test_once_per_pid_disabled_and_sanitized_errors(self):
        off = ArchiveRuntime(config=lambda: {'configured': False})
        off.start()
        self.assertEqual(off.status()['state'], 'NOT_CONFIGURED')
        backend = Backend()
        runtime = ArchiveRuntime(factory=lambda: backend, config=lambda: {'configured': True})
        for _ in range(8):
            runtime.start()
        for _ in range(100):
            if runtime.status()['state'] != 'RUNNING': break
            time.sleep(.005)
        self.assertEqual(runtime.status()['state'], 'PASS')
        self.assertEqual(backend.puts, 1)
        failed = ArchiveRuntime(factory=lambda: (_ for _ in ()).throw(ValueError('secret-data')), config=lambda: {'configured': True})
        failed.start()
        for _ in range(100):
            if failed.status()['state'] != 'RUNNING': break
            time.sleep(.005)
        self.assertEqual(failed.status()['error'], 'ARCHIVE_PROBE_FAILED')
        self.assertNotIn('secret-data', str(failed.status()))

if __name__ == '__main__': unittest.main()
