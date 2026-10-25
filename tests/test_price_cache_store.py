"""Loss of RAM, stale timestamps, interrupted saves and source corruption."""
import copy,json,sys,threading,time,unittest,zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from archive_client import ArchiveError,canonical,digest
from price_cache_store import PriceCache,encode_entry,decode_entry,bucket,verify_record
from cron_collector import due,Collector
from datetime import datetime
from zoneinfo import ZoneInfo

NOW=1792922400

def entry(ticker='SYNTHETIC',ts=NOW):
    payload=dict(ticker=ticker,interval='1d',coverage_end='2026-10-23',bars=[dict(date='2026-10-23',close=123.45)])
    blob=zlib.compress(json.dumps(payload).encode())
    return dict(ts=ts,expected='2026-10-23',coverage_end='2026-10-23',after_close=True,blob=blob,blob_bytes=len(blob))

class Backend:
    def __init__(self):self.objects={};self.h=None;self.fail=False
    def head(self):return self.h
    def get(self,key,kind=None):return copy.deepcopy(self.objects[key])
    def put(self,value,base=None):
        if self.fail:raise ArchiveError('PRICE_CACHE_READBACK_INVALID')
        if value['kind']=='PRICE_CACHE_MANIFEST' and base!=(self.h or {}).get('object_id'):raise ArchiveError('ARCHIVE_REMOTE_REJECTED:CACHE_HEAD_CONFLICT')
        key=digest(canonical(value));self.objects[key]=copy.deepcopy(value)
        if value['kind']=='PRICE_CACHE_MANIFEST':self.h=dict(object_id=key)
        return key

class PriceTests(unittest.TestCase):
    def test_persist_restart_preserves_acquisition_time_and_compressed_bars(self):
        b=Backend();c={'s:SYNTHETIC:2y:1d:None:None':entry()};lock=threading.Lock()
        first=PriceCache(clock=lambda:NOW);first.restore(b,c,lock,'compat',1100);first.flush(b,c,lock,'compat')
        self.assertTrue(first.is_verified(next(iter(c)),next(iter(c.values()))))
        restored={};second=PriceCache(clock=lambda:NOW+60);second.restore(b,restored,lock,'compat',1100)
        self.assertEqual(restored,c);self.assertEqual(second.info['restored_entries'],1)
        self.assertEqual(next(iter(restored.values()))['ts'],NOW)
    def test_failed_write_does_not_authorize_eviction_or_move_head(self):
        b=Backend();b.fail=True;r=PriceCache(clock=lambda:NOW);c={'s:SYNTHETIC:2y:1d:None:None':entry()}
        with self.assertRaises(ArchiveError):r.flush(b,c,threading.Lock(),'compat')
        self.assertFalse(r.is_verified(next(iter(c)),next(iter(c.values()))));self.assertIsNone(b.h)
    def test_old_future_and_mismatched_metadata_are_not_relabelled_current(self):
        key='s:SYNTHETIC:2y:1d:None:None'
        self.assertIsNone(decode_entry(encode_entry(key,entry(ts=NOW-4*86400)),NOW))
        self.assertIsNone(decode_entry(encode_entry(key,entry(ts=NOW+1)),NOW))
        row=encode_entry(key,entry());row['entry']['coverage_end']='2026-10-24'
        with self.assertRaisesRegex(ArchiveError,'METADATA'):decode_entry(row,NOW)
    def test_changed_policy_never_restores_incompatible_data(self):
        b=Backend();r=PriceCache(clock=lambda:NOW);c={'s:SYNTHETIC:2y:1d:None:None':entry()};lock=threading.Lock()
        r.flush(b,c,lock,'old');out={};PriceCache(clock=lambda:NOW).restore(b,out,lock,'new',1100)
        self.assertEqual(out,{})
    def test_newer_local_download_is_not_overwritten_by_restore(self):
        b=Backend();r=PriceCache(clock=lambda:NOW);key='s:SYNTHETIC:2y:1d:None:None';lock=threading.Lock()
        r.flush(b,{key:entry()},lock,'compat');local={key:entry(ts=NOW+10)}
        restored=PriceCache(clock=lambda:NOW+20);restored.restore(b,local,lock,'compat',1100)
        self.assertEqual(local[key]['ts'],NOW+10);self.assertFalse(restored.is_verified(key,local[key]))
    def test_changed_ram_entry_loses_old_eviction_permission(self):
        b=Backend();r=PriceCache(clock=lambda:NOW);key='s:SYNTHETIC:2y:1d:None:None';c={key:entry()}
        r.flush(b,c,threading.Lock(),'compat');c[key]['ts']+=1
        self.assertFalse(r.is_verified(key,c[key]))
    def test_corrupt_hash_or_cross_kind_is_rejected(self):
        text=canonical(dict(kind='PRICE_CACHE_BATCH',schema=1,entries=[]));sha=digest(text)
        result=dict(payload_json=text,payload_sha256=sha,object_id=digest('PRICE_CACHE_BATCH\n'+sha))
        self.assertEqual(verify_record(result)['schema'],1)
        with self.assertRaises(ArchiveError):verify_record(dict(result,payload_sha256='0'*64))
        with self.assertRaises(ArchiveError):verify_record(result,'PRICE_CACHE_MANIFEST')
    def test_restore_respects_item_cap(self):
        b=Backend();r=PriceCache(clock=lambda:NOW);c={f's:T{i}:2y:1d:None:None':entry(f'T{i}') for i in range(20)};lock=threading.Lock()
        for _ in range(20):r.flush(b,c,lock,'compat')
        out={};PriceCache(clock=lambda:NOW).restore(b,out,lock,'compat',3)
        self.assertLessEqual(len(out),3)

class CronTests(unittest.TestCase):
    def test_authorized_slots_weekdays_only_and_dst(self):
        tz=ZoneInfo('Europe/Rome')
        self.assertIsNone(due(datetime(2026,10,25,9,20,tzinfo=tz)))
        self.assertEqual(due(datetime(2026,10,26,9,20,tzinfo=tz)),('2026-10-26|09:20',['EU']))
        self.assertEqual(due(datetime.fromisoformat('2026-10-26T15:00:00+00:00')),('2026-10-26|16:00',['US']))
        for h,m in [(9,35),(14,40),(17,15),(21,30)]:self.assertIsNone(due(datetime(2026,10,26,h,m,tzinfo=tz)))
    def test_claimed_slot_does_not_start_second_scan(self):
        class Client:
            def _call(self,*a,**k):return dict(claimed=False)
        c=Collector(factory=Client);c._work('2026-10-26|09:20',['EU'],lambda m:self.fail('duplicate scan'))
        self.assertEqual(c.status()['state'],'ALREADY_CLAIMED')
    def test_archived_completion_requires_verified_receipt(self):
        class Client:
            calls=[]
            def _call(self,action,**kw):
                self.calls.append(action)
                return dict(claimed=True,claim_token='synthetic') if action=='collector_claim' else dict(finished=True)
        c=Collector(factory=Client)
        c._work('2026-10-26|09:20',['EU'],lambda m:dict(job_status='COMPLETE',job_id='synthetic',returned=3,scan_complete=False,opportunity_archive=dict(persistence=dict(durable_verified=True,object_id='a'*64))))
        self.assertEqual(c.status()['state'],'ARCHIVED');self.assertEqual(Client.calls,['collector_claim','collector_finish'])
        self.assertFalse(c.status()['scan_complete']);self.assertFalse(c.status()['account_collected'])

if __name__=='__main__':unittest.main()

class ServerCacheTests(unittest.TestCase):
    def test_capacity_never_evicts_unverified_price_series(self):
        import server
        saved=server.CACHE.copy()
        try:
            server.CACHE.clear()
            payload=dict(ticker='SYNTHETIC',interval='1d',coverage_end='2026-10-23',bars=[])
            with patch.object(server,'CACHE_MAX_ITEMS',1),patch.object(server.price_cache_runtime,'is_verified',return_value=False):
                server._cache_put('s:SYNTHETIC:2y:1d:None:None',dict(ts=time.time(),expected='2026-10-23',after_close=True,data=payload))
                with self.assertRaisesRegex(ValueError,'PENDING_CAPACITY'):
                    server._cache_put('s:SECOND:2y:1d:None:None',dict(ts=time.time(),expected='2026-10-23',after_close=True,data=dict(payload,ticker='SECOND')))
                self.assertIn('s:SYNTHETIC:2y:1d:None:None',server.CACHE)
            with patch.object(server,'CACHE_MAX_ITEMS',1),patch.object(server.price_cache_runtime,'is_verified',return_value=True):
                server._cache_put('s:SECOND:2y:1d:None:None',dict(ts=time.time(),expected='2026-10-23',after_close=True,data=dict(payload,ticker='SECOND')))
                self.assertEqual(list(server.CACHE),['s:SECOND:2y:1d:None:None'])
        finally:server.CACHE.clear();server.CACHE.update(saved)
    def test_restore_does_not_override_financial_freshness(self):
        import server
        old=entry();p=server._from_cache(old,'2026-10-26')
        self.assertFalse(p['data_current']);self.assertEqual(p['last_completed_session_expected'],'2026-10-26')
        self.assertEqual(old['ts'],NOW)
