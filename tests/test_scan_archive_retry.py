import unittest
from unittest.mock import patch
import cycle_scanner as module

class RetryTests(unittest.TestCase):
    def test_pending_archive_retried_without_new_scan_and_only_one_inflight(self):
        job=dict(id='cycle-123',finished=1,archive={'archive_id':'synthetic'},persistence={'durable_verified':False},persist_attempt_at=1)
        class Thread:
            def __init__(self,target,args,**kw):self.target=target;self.args=args
            def start(self):pass
        with patch.dict(module.JOBS,{'synthetic':job},clear=True),patch.object(module.scan_archive_store,'enabled',return_value=True),patch.object(module.threading,'Thread',Thread),patch.object(module.time,'monotonic',return_value=100):
            module.retry_pending();self.assertTrue(job['persisting']);self.assertEqual(job['persistence']['state'],'WRITING')
            module.retry_pending();self.assertEqual(len(module.JOBS),1)
