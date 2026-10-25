"""Durable scanner archive and bounded retention tests; synthetic data only."""
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from adaptive_method import digest
from archive_client import ArchiveError, canonical, digest as text_digest
import scan_archive_store as store
import cycle_scanner as scanner

JOB='cycle-1792922400000000000'


def artifact():
    a=dict(schema_version=1,frozen_at='2026-10-25T10:00:00+00:00',opportunities=[dict(key='SYNTHETIC')],coverage=[],errors=[])
    a['archive_id']=digest(a)
    return a


class Backend:
    def __init__(self):self.objects={}
    def put(self,kind,run,value):
        text=canonical(value);sha=text_digest(text);key=text_digest(kind+'\n'+run+'\n'+sha)
        self.objects[key]=dict(payload_json=text,payload_sha256=sha)
        return dict(object_id=key,durable_verified=True)
    def get(self,key):return copy.deepcopy(self.objects[key])


class StorageTests(unittest.TestCase):
    def test_read_after_loss_of_all_local_jobs(self):
        b=Backend();a=artifact();r=store.put(JOB,a,b)
        self.assertEqual(store.get(JOB,r['object_id'],b),a)
        with self.assertRaisesRegex(ArchiveError,'NAMESPACE'):
            store.get('cycle-1',r['object_id'],b)

    def test_public_recovery_rejects_private_kind_even_with_valid_wrapper(self):
        b=Backend();run='RUN-20261025-100000'
        w=dict(kind='SCAN_ARCHIVE',cycle_id=run,job_id=JOB,archive=artifact())
        r=b.put('RISK_BUNDLE',run,w)
        with self.assertRaisesRegex(ArchiveError,'NAMESPACE'):
            store.get(JOB,r['object_id'],b)

    def test_artifact_tamper_rejected_before_write_and_on_recovery(self):
        a=artifact();a['opportunities'].append({'key':'TAMPER'})
        with self.assertRaisesRegex(ArchiveError,'HASH'):store.put(JOB,a,Backend())
        b=Backend();r=b.put('SCAN_ARCHIVE','RUN-20261025-100000',dict(kind='SCAN_ARCHIVE',cycle_id='RUN-20261025-100000',job_id=JOB,archive=a))
        with self.assertRaisesRegex(ArchiveError,'HASH'):store.get(JOB,r['object_id'],b)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.backup=scanner.JOBS.copy();scanner.JOBS.clear()
        self.p=patch.object(store,'enabled',return_value=True);self.p.start()
    def tearDown(self):
        self.p.stop();scanner.JOBS.clear();scanner.JOBS.update(self.backup)
    def wait(self):
        for _ in range(1000):
            if all(j['state']!='RUNNING' for j in scanner.JOBS.values()):return
            time.sleep(.002)
        self.fail('worker did not finish')
    def scans(self):
        return (lambda m:dict(shortlist=[dict(ticker='SYNTHETIC',fusion_score=1)],scan_complete=True),
                lambda m:dict(job_status='COMPLETE',shortlist=[]))
    def test_automatic_write_does_not_expand_shortlist_and_no_duplicate_scan(self):
        backend=Backend();a,b=self.scans()
        with patch.object(store,'put',side_effect=lambda job,art:store_put(job,art,backend)):
            first=scanner.request(['EU'],'balanced',[],a,b);self.wait()
            r=scanner.request(['EU'],'balanced',[],lambda m:self.fail('rescanned'),b)
        self.assertEqual(r['job_id'],first['job_id']);self.assertEqual(r['returned'],1)
        self.assertFalse(r['ticket_authorized']);self.assertTrue(r['opportunity_archive']['persistent'])
        self.assertIn('?archive_object_id=',r['opportunity_archive']['path'])
        self.assertEqual(len(backend.objects),1)
        saved=next(iter(scanner.JOBS.values()))['archive'];scanner.JOBS.clear()
        with patch.object(store,'get',side_effect=lambda job,key:store_get(job,key,backend)):
            self.assertEqual(scanner.get_archive(first['job_id'],next(iter(backend.objects))),saved)
    def test_failed_write_is_retained_past_ttl_and_retry_does_not_rescan(self):
        a,b=self.scans()
        with patch.object(store,'put',side_effect=ArchiveError('ARCHIVE_REQUEST_OR_RESPONSE_FAILED')):
            scanner.request(['EU'],'balanced',[],a,b);self.wait()
        job=next(iter(scanner.JOBS.values()));job['finished']-=scanner.TTL+1
        job['persist_attempt_at']-=scanner.ARCHIVE_RETRY_S+1
        self.assertEqual(scanner.get_archive(job['id']),job['archive'])
        with patch.object(store,'put',return_value=dict(object_id='a'*64,durable_verified=True)) as save:
            scanner.request(['EU'],'balanced',[],lambda m:self.fail('rescanned'),b)
            for _ in range(100):
                if not job.get('persisting'):break
                time.sleep(.002)
            self.assertEqual(save.call_count,1)
        self.assertTrue(job['persistence']['durable_verified'])
    def test_unverified_receipt_never_permits_eviction(self):
        a,b=self.scans()
        with patch.object(store,'put',return_value=dict(object_id='b'*64,durable_verified=False)):
            scanner.request(['EU'],'balanced',[],a,b);self.wait()
        job=next(iter(scanner.JOBS.values()))
        self.assertFalse(job['persistence']['durable_verified'])
        self.assertEqual(job['persistence']['error'],'SCAN_ARCHIVE_READBACK_REQUIRED')

    def test_verified_expired_job_can_be_released(self):
        scanner.JOBS[('old',)]=dict(id=JOB,state='COMPLETE',finished=time.monotonic()-scanner.TTL-10,
                                  archive=artifact(),persistence=dict(durable_verified=True))
        a,b=self.scans()
        with patch.object(store,'put',return_value=dict(object_id='c'*64,durable_verified=True)):
            scanner.request(['EU'],'balanced',[],a,b);self.wait()
        self.assertNotIn(('old',),scanner.JOBS)

    def test_unverified_backlog_is_bounded_and_never_silently_evicted(self):
        for i in range(scanner.MAX_JOBS):
            scanner.JOBS[('old',i)]=dict(id='cycle-'+str(i),state='COMPLETE',finished=time.monotonic()-scanner.TTL-10,archive=artifact(),persistence=dict(durable_verified=False))
        a,b=self.scans();r=scanner.request(['US'],'balanced',[],a,b)
        self.assertEqual(r['job_status'],'BUSY');self.assertEqual(len(scanner.JOBS),scanner.MAX_JOBS)
        self.assertEqual(r['reason'],'ARCHIVE_BACKLOG_OR_JOB_CAPACITY')

store_put=store.put
store_get=store.get
if __name__=='__main__':unittest.main()
