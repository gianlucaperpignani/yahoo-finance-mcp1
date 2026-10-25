"""Synthetic end-to-end checkpoints: restart, exact exit and immutable publish."""
import copy,json,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from adaptive_method import digest
from archive_client import ArchiveError
from collector_store import Conflict
from opportunity_cohorts import update
from learning_collector import Worker,add_task,eligible,MAX_ATTEMPTS,initialize
from learning_sources import Pending,account_fingerprint
from etoro_learning_client import Client,ReadFailure
import test_completion_flow as fixtures

ME=dict(gcid=1,realCid=2,username='synthetic',scopes=['etoro-public:trade.real:read'])

class Backend:
    def __init__(self,seed):self.value=None;self.head=None;self.objects={};self.seed=seed;self.file_ids={};self.client=self;self.published={};self.fail_save=False;self.lose_publish_ack=False
    def bootstrap(self):return self.seed,dict(account_fingerprint=account_fingerprint(ME)),'a'*64
    def load(self):return copy.deepcopy(self.value),self.head
    def save(self,state,base):
        if self.fail_save:raise ArchiveError('SYNTHETIC_WRITE_FAILED')
        if base!=self.head:raise Conflict('synthetic competing writer')
        self.value=copy.deepcopy(state);self.head=digest(state);return self.head
    def put(self,*args):
        if len(args)==1:
            value=copy.deepcopy(args[0]);key=digest(value);self.objects[key]=value;return key
        kind,run,payload=args;key=digest(dict(kind=kind,run=run,payload=payload));self.published[key]=copy.deepcopy(payload)
        if self.lose_publish_ack:
            self.lose_publish_ack=False;raise ArchiveError('SYNTHETIC_ACK_LOST')
        return dict(durable_verified=True,object_id=key)
    def get(self,key):return copy.deepcopy(self.objects[key])
    def scan_refs(self):return []
    def tickets(self):return []

class Sources:
    def __init__(self,fixture,clock):self.fixture=fixture;self.clock=clock;self.entries=0;self.exits=0;self.reads=0
    def verify_account(self,*a):self.reads+=1;return dict(statusCode=200,body=ME)
    def contexts(self,tickers,task):return dict(contexts={t:dict(ticker=t) for t in tickers})
    def entry(self,row,contexts,benchmark,task):
        self.entries+=1
        if self.clock[0]<'2026-11-02T09:01:00+00:00':raise Pending('WAIT_OPEN_SESSION')
        return self.fixture.entry()
    def exit(self,row,task):
        self.exits+=1;entry=row['entry'];plan=row['exit_plan']
        def q(e,p):return dict(kind='SESSION_CLOSE',instrument_id=e['instrument_id'],calendar_id=p['calendar_id'],session_date=p['session_date'],valued_at=p['close_at'],retrieved_at=self.clock[0],price_basis=p['price_basis'],close=103,fx_to_usd=1,fx_reference='synthetic USD identity',entry_quote_hash=digest(e),entry_basis_verified=True,source_reference='synthetic acquired close',adjustments_reference='synthetic no events')
        return dict(observed_at=self.clock[0],quotes={k:q(v,plan['entries'][k]) for k,v in entry['quotes'].items()},benchmark=q(entry['benchmark'],plan['benchmark']),session_complete=True,calendar_reference='synthetic exchange calendar')
    def history_page(self,minimum,page,task):return dict(account='real',statusCode=200,xRequestId='synthetic history',minDate=minimum,page=page,pageSize=200,hasMore=False,trades=[])

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CompletionTests();cs=update(dict(as_of='2026-11-02T09:00:00+00:00',archive=self.f.archive()))['store']
        self.seed=dict(input=dict(learning_states={}),output=dict(as_of='2026-11-02T09:00:00+00:00',ledger=dict(entries={}),cohort_collection=dict(store=cs)))
        self.store=Backend(self.seed);self.clock=['2026-11-02T09:01:00+00:00'];self.sources=Sources(self.f,self.clock)
    def wake(self,worker=None):
        worker=worker or Worker(self.sources,lambda:self.store,clock=lambda:self.clock[0])
        with patch('learning_collector.now',side_effect=lambda:self.clock[0]):worker._run()
        self.assertNotEqual(worker.status()['state'],'ERROR',worker.status())
        return worker
    def test_full_entry_restart_exact_exit_and_persisted_result(self):
        self.wake();w=self.wake();self.assertEqual(list(self.store.value['cohorts'].values())[0]['state'],'WAIT_EXIT')
        self.wake();self.assertEqual(self.sources.entries,1)
        # Destroy Worker RAM; the new worker must use the saved WAIT_EXIT state.
        self.clock[0]='2026-11-03T17:00:00+00:00';new=self.wake()
        self.assertEqual(list(self.store.value['cohorts'].values())[0]['state'],'CLOSED')
        self.assertEqual(self.sources.entries,1);self.assertEqual(self.sources.exits,1)
        row=self.store.get(next(iter(self.store.value['cohorts'].values()))['ref'])
        self.assertTrue(row['exit_session_verified']);self.assertTrue(all(c['costs_included'] for c in row['candidates']))
        self.wake();self.assertEqual(self.sources.exits,1);self.assertEqual(len(self.store.value['cohorts']),1)
        self.assertTrue(new.status()['checkpoint_verified'])
    def test_write_failure_before_read_never_advances_work(self):
        self.store.fail_save=True;w=Worker(self.sources,lambda:self.store,clock=lambda:self.clock[0]);w._run()
        self.assertEqual(w.status()['state'],'ERROR');self.assertEqual(self.sources.reads,0);self.assertIsNone(self.store.value)
    def test_lost_publication_ack_retries_same_immutable_event(self):
        self.wake();self.store.lose_publish_ack=True;w=self.wake()
        published_before=set(self.store.published)
        self.clock[0]='2026-11-02T09:06:00+00:00';self.wake()
        # One idempotent publication plus a separate monthly review event may exist.
        observations=[v for v in self.store.published.values() if v['kind']=='AUTONOMOUS_COHORT_OBSERVATION']
        self.assertEqual(len(observations),1);self.assertEqual(self.sources.entries,1)
    def test_running_lease_and_three_crash_attempts_do_not_loop_forever(self):
        state=dict(tasks={},account_verified=True,cohorts={});ident=add_task(state,'DISCOVER','x',{})
        task=state['tasks'][ident];task.update(state='RUNNING',attempts=MAX_ATTEMPTS,lease_until='2026-11-02T09:00:00+00:00')
        self.assertFalse(eligible(task,state,'2026-11-02T09:01:00+00:00'));self.assertEqual(task['state'],'BLOCKED')
    def test_pruned_done_descriptor_cannot_recreate_completed_work(self):
        state=dict(tasks={})
        ident=add_task(state,'FEEDBACK','2026-11',{})
        state['completed_tasks']={ident:dict(op='FEEDBACK',key='2026-11',at=self.clock[0])}
        state['tasks'].clear()
        self.assertEqual(add_task(state,'FEEDBACK','2026-11',{}),ident)
        self.assertEqual(state['tasks'],{})
    def test_account_mismatch_cannot_collect_trade_history(self):
        with patch.object(self.sources,'verify_account',side_effect=ReadFailure('COLLECTOR_ACCOUNT_MISMATCH',permanent=True)):
            w=Worker(self.sources,lambda:self.store,clock=lambda:self.clock[0]);w._run()
        self.assertEqual(w.status()['error'],'COLLECTOR_ACCOUNT_MISMATCH');self.assertFalse(self.store.value['account_verified'])
    def test_failed_result_archive_rolls_back_task_effects_and_dependent_jobs(self):
        state=initialize(self.store);state.update(account_verified=True,tasks={})
        task_id=add_task(state,'DISCOVER','synthetic-failure',{})
        self.store.save(state,None);original=copy.deepcopy(state['cohorts'])
        worker=Worker(self.sources,lambda:self.store,clock=lambda:self.clock[0])
        put=self.store.put
        def failed_result(value):
            if value.get('task_id')==task_id:raise ArchiveError('SYNTHETIC_RESULT_WRITE_FAILED')
            return put(value)
        def partial_effects(task,current,store):
            next(iter(current['cohorts'].values()))['state']='CLOSED'
            add_task(current,'PUBLISH','must-not-escape',{'event_ref':'missing'})
            return {'synthetic':'partial effects before failed result write'}
        with patch('learning_collector.plan'),patch.object(worker,'execute',side_effect=partial_effects),patch.object(self.store,'put',side_effect=failed_result):
            worker._run()
        self.assertEqual(self.store.value['cohorts'],original)
        self.assertEqual(set(self.store.value['tasks']),{task_id})
        self.assertEqual(self.store.value['tasks'][task_id]['state'],'RETRY')
        self.assertEqual(self.store.value['tasks'][task_id]['attempts'],1)

class AllowlistTests(unittest.TestCase):
    def test_execution_and_arbitrary_get_or_post_rejected_before_network(self):
        c=Client(opener=lambda *a,**kw:self.fail('network'))
        for path,body in [('/api/v2/trading/execution/orders',{'amount':1000}),('/api/v2/trading/execution/orders',None),('/api/v2/market-data/rates',{}),('/api/v2/trading/info/costs',dict(action='close',transaction='sell'))]:
            with self.assertRaises(ReadFailure):c.read(path,body=body)

if __name__=='__main__':unittest.main()
