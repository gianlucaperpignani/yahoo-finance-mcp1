"""1.0.0: autonomous checkpointed read collection, existing cohort/feedback rules.

One persisted task lease precedes every external block. A failed read/write never
advances its task; same task/request IDs survive process loss. No orders/LLM.
"""
import copy
import threading
import time
import uuid
from datetime import timedelta
from archive_client import ArchiveClient,ArchiveError,configuration_status
from collector_store import Store,Conflict
from adaptive_method import digest,stamp,validate_state
from opportunity_cohorts import update as update_cohorts
from outcome_ledger import ingest,kpis,category_kpis
from feedback_cycle import run as feedback
from learning_sources import Pending,now
from etoro_learning_client import ReadFailure

VERSION='1.0.0';MAX_ATTEMPTS=3;LEASE_SECONDS=600


def one_store(row):
    value=dict(schema_version=1,cohorts=[row]);value['store_hash']=digest(value);return value


def describe(row,ref):
    return dict(ref=ref,state=row['state'],scope_key=row['scope_key'],planned_exit_at=row.get('planned_exit_at'),closed_at=row.get('closed_at'),universe_frozen_at=row['universe_frozen_at'],
                tickers=[c['key'] for c in row['candidates']],benchmark='SPY' if 'US' in row['method_id'] else 'EXSA.DE')


def add_task(state,op,key,arguments):
    ident=digest(dict(op=op,key=key,arguments=arguments))
    if ident in state.get('completed_tasks',{}):return ident
    if ident not in state['tasks']:
        state['tasks'][ident]=dict(id=ident,op=op,key=key,arguments=arguments,state='PENDING',attempts=0,not_before=now(),created_at=now())
    return ident


def remember(store,state,value):
    ref=store.put(value)
    if hasattr(store,'file_ids') and ref in store.file_ids:state['file_ids'][ref]=store.file_ids[ref]
    return ref


def initialize(store):
    archive,config,source_hash=store.bootstrap();output=archive['output'];source=archive['input']
    cohorts=output['cohort_collection']['store'];unsigned={k:v for k,v in cohorts.items() if k!='store_hash'}
    if digest(unsigned)!=cohorts.get('store_hash'):raise ArchiveError('COLLECTOR_INITIAL_COHORT_HASH_INVALID')
    state=dict(schema=1,version=VERSION,account_fingerprint=config['account_fingerprint'],source_registry_sha=source_hash,
        bootstrap_as_of=output['as_of'],cohorts={},tasks={},seen_scans=[],file_ids={},account_verified=False,
        ledger_ref=None,learning_state=source.get('learning_state'),learning_states=source.get('learning_states',{}),
        ticket_records=source.get('ticket_records',[]),history_complete_at=output['as_of'])
    if state['learning_state']:validate_state(state['learning_state'])
    for value in state['learning_states'].values():validate_state(value)
    state['ledger_ref']=remember(store,state,output['ledger']['entries'])
    for row in cohorts['cohorts']:
        state['cohorts'][row['cohort_id']]=describe(row,remember(store,state,row))
    add_task(state,'ACCOUNT','initial',{})
    return state


def plan(state,as_of):
    if not state['account_verified']:return
    # Full discovery archiving happens independently; this imports references,
    # never another scanner request and never old discoveries as current signals.
    moment=stamp(as_of)
    slot=moment.strftime('%Y-%m-%dT%H:')+str(moment.minute//5*5).zfill(2)
    add_task(state,'DISCOVER',slot,{})
    from datetime import datetime
    local=stamp(as_of).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Rome'))
    history_key=local.strftime('%Y-%m-%d')+'|'+('AM' if local.hour<16 else 'PM')
    minimum=(stamp(state['history_complete_at'])-timedelta(days=1)).date().isoformat()
    add_task(state,'HISTORY',history_key,dict(minimum=minimum,page=1))
    for ident,row in state['cohorts'].items():
        if row['state']=='CLOSED':continue
        if row['state']=='WAIT_ENTRY':
            chunks=[row['tickers'][i:i+4] for i in range(0,len(row['tickers']),4)]
            for n,chunk in enumerate(chunks):add_task(state,'CONTEXT',ident+'/'+str(n),dict(cohort=ident,tickers=chunk,context_date=as_of[:10]))
            add_task(state,'CONTEXT',ident+'/benchmark',dict(cohort=ident,tickers=[row['benchmark']],context_date=as_of[:10]))
            add_task(state,'ENTRY',ident,dict(cohort=ident))
        elif row['state']=='WAIT_EXIT':
            add_task(state,'EXIT',ident,dict(cohort=ident))
            task=state['tasks'][digest(dict(op='EXIT',key=ident,arguments=dict(cohort=ident)))]
            task['not_before']=row['planned_exit_at']
    add_task(state,'FEEDBACK',local.strftime('%Y-%m'),{})


def eligible(task,state,as_of):
    if task['state'] not in {'PENDING','RETRY','RUNNING'}:return False
    if stamp(task['not_before'])>stamp(as_of):return False
    if task['state']=='RUNNING':
        if stamp(task['lease_until'])>stamp(as_of):return False
        if task['attempts']>=MAX_ATTEMPTS:
            task.update(state='BLOCKED',error='MAX_ATTEMPTS_AFTER_INTERRUPTION');return False
    if task['op']!='ACCOUNT' and not state['account_verified']:return False
    ident=task['arguments'].get('cohort');row=state['cohorts'].get(ident,{})
    if task['op']=='ENTRY':
        if set([*row.get('tickers',[]),row.get('benchmark')])-set(row.get('contexts',{})):return False
        if any(row.get('context_dates',{}).get(k)!=as_of[:10] for k in [*row.get('tickers',[]),row.get('benchmark')]):return False
    return True


class Worker:
    def __init__(self,sources,store_factory=Store,clock=now):
        self.sources=sources;self.factory=store_factory;self.clock=clock;self.lock=threading.Lock();self.busy=False;self.last_tick=0
        self.owner=uuid.uuid4().hex;self.applied_states=None;self.account_binding=None;self.account_failures=0;self.info=dict(version=VERSION,state='NOT_STARTED',orders_enabled=False,llm_calls=0)
    def status(self):
        with self.lock:return copy.deepcopy(self.info)
    def selection_states(self):
        with self.lock:return copy.deepcopy(self.applied_states)
    def tick(self):
        if not configuration_status()['configured']:return
        with self.lock:
            if self.busy or time.monotonic()-self.last_tick<60:return
            self.busy=True;self.last_tick=time.monotonic()
        threading.Thread(target=self._run,daemon=True,name='learning-collector').start()
    def _run(self):
        try:
            store=self.factory();state,head=store.load()
            restored=state is not None
            if state is None:
                state=initialize(store);head=store.save(state,None)
            import os
            binding=digest([os.environ.get(k,'') for k in ('ETORO_API_KEY','ETORO_USER_KEY','ETORO_ACCESS_TOKEN')])
            if self.account_binding!=binding:
                profile=self.sources.verify_account(state['account_fingerprint'],'process-binding/'+self.owner)
                from etoro_learning_client import unpack
                generation=digest(dict(credentials=binding,scopes=unpack(profile).get('scopes'),adapter=VERSION))
                if state.get('source_generation')!=generation:
                    for old in state['tasks'].values():
                        if old['state']=='BLOCKED' and old.get('error') in {'ETORO_HTTP_401','ETORO_HTTP_403'}:
                            old.update(state='RETRY',attempts=0,not_before=self.clock())
                    state['source_generation']=generation
                state['account_check_ref']=remember(store,state,profile)
                self.account_binding=binding;self.account_failures=0
            plan(state,self.clock())
            # Up to four small completed blocks per wake; pending market/exit
            # conditions do not consume the three error retry attempts.
            for _ in range(4):
                tasks=[t for t in state['tasks'].values() if eligible(t,state,self.clock())]
                if not tasks:break
                priority={'ACCOUNT':0,'PUBLISH':1,'APPLY_FEEDBACK':1,'EXIT':2,'CONTEXT':2,'ENTRY':3,'HISTORY':4,'DISCOVER':5,'FEEDBACK':6}
                tasks.sort(key=lambda t:(priority[t['op']],t['key']))
                task=tasks[0];task['attempts']+=1;task.update(state='RUNNING',lease_owner=self.owner,lease_until=(stamp(self.clock())+timedelta(seconds=LEASE_SECONDS)).isoformat())
                head=store.save(state,head) # Must succeed before the read begins.
                before=copy.deepcopy(state)
                task_id=task['id']
                def rollback():
                    # A read can succeed and mutate cohort/ledger state before a
                    # subsequent archive write fails. Persist only the retry,
                    # never half of that task's effects or its dependent jobs.
                    state.clear();state.update(copy.deepcopy(before))
                    return state['tasks'][task_id]
                if hasattr(self.sources,'client'):self.sources.client.journal=[]
                try:
                    result=self.execute(task,state,store)
                    task['response_ref']=remember(store,state,dict(task_id=task['id'],result=result))
                    task.update(state='DONE',completed_at=self.clock(),error=None)
                    state.setdefault('completed_tasks',{})[task['id']]=dict(op=task['op'],key=task['key'],at=self.clock())
                except Pending as exc:
                    task=rollback()
                    task['attempts']-=1
                    task.update(state='PENDING',not_before=(stamp(self.clock())+timedelta(seconds=exc.seconds)).isoformat(),error=str(exc))
                except (ReadFailure,ArchiveError,ValueError,KeyError,TypeError) as exc:
                    task=rollback()
                    permanent=getattr(exc,'permanent',False)
                    code=str(exc) if isinstance(exc,(ReadFailure,ArchiveError)) else 'COLLECTOR_INPUT_OR_SOURCE_INVALID'
                    task.update(state='BLOCKED' if permanent or task['attempts']>=MAX_ATTEMPTS else 'RETRY',error=code,
                        not_before=(stamp(self.clock())+timedelta(seconds=max(60,getattr(exc,'retry_after',60)*2**(task['attempts']-1)))).isoformat())
                except Exception:
                    task=rollback()
                    task.update(state='BLOCKED' if task['attempts']>=MAX_ATTEMPTS else 'RETRY',error='COLLECTOR_TASK_FAILED',not_before=(stamp(self.clock())+timedelta(seconds=60*task['attempts'])).isoformat())
                finally:
                    journal=getattr(getattr(self.sources,'client',None),'journal',[])
                    if journal:task['raw_response_ref']=remember(store,state,dict(task_id=task['id'],reads=journal))
                head=store.save(state,head)
            # Bounded checkpoint: archive task results immutably, keep only recent
            # completed task descriptors and all unfinished work.
            done=sorted([k for k,v in state['tasks'].items() if v['state']=='DONE'],key=lambda k:state['tasks'][k].get('completed_at',''))
            for key in done[:-160]:del state['tasks'][key]
            cutoff=stamp(self.clock())-timedelta(days=7)
            month=stamp(self.clock()).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Rome')).strftime('%Y-%m')
            state['completed_tasks']={k:v for k,v in state.get('completed_tasks',{}).items()
                if stamp(v['at'])>=cutoff or (v['op']=='FEEDBACK' and v['key']==month)}
            reachable=str({k:v for k,v in state.items() if k!='file_ids'})
            state['file_ids']={k:v for k,v in state['file_ids'].items() if k in reachable}
            head=store.save(state,head)
            with self.lock:self.applied_states=copy.deepcopy(state['learning_states'])
            counts={}
            for row in state['cohorts'].values():counts[row['state']]=counts.get(row['state'],0)+1
            pending=[t for t in state['tasks'].values() if t['state']!='DONE']
            ledger_count=len(store.get(state['ledger_ref']))
            with self.lock:self.info.update(state='READY',error=None,checkpoint_verified=True,cohorts=counts,pending_tasks=len(pending),blocked_tasks=sum(t['state']=='BLOCKED' for t in pending),
                issues=sorted(set(t.get('error') for t in pending if t.get('error')))[:8],last_checked_at=self.clock(),account_verified=state['account_verified'],
                restored_checkpoint=restored,checkpoint_object_id=head,ledger_entries=ledger_count,
                source_registry_sha=state['source_registry_sha'])
        except Conflict:
            with self.lock:self.info.update(state='WAIT_OTHER_WORKER',checkpoint_verified=False)
        except ReadFailure as exc:
            self.account_failures+=1
            with self.lock:
                if self.account_failures>=MAX_ATTEMPTS:self.last_tick=time.monotonic()+86400
                self.info.update(state='ERROR',error=str(exc),checkpoint_verified=False)
        except ArchiveError as exc:
            with self.lock:self.info.update(state='ERROR',error=str(exc),checkpoint_verified=False)
        except Exception:
            with self.lock:self.info.update(state='ERROR',error='COLLECTOR_WORKER_FAILED',checkpoint_verified=False)
        finally:
            with self.lock:self.busy=False
    def publish(self,task,store):
        payload=store.get(task['arguments']['event_ref'])
        receipt=store.client.put('FEEDBACK',stamp(task['created_at']).strftime('RUN-%Y%m%d-%H%M%S'),payload)
        if not receipt.get('durable_verified'):raise ArchiveError('LEARNING_SNAPSHOT_READBACK_REQUIRED')
        return receipt
    def event(self,state,store,kind,task_id,as_of):
        # Frozen immutable event, published only by a separate checkpointed task.
        return remember(store,state,dict(kind=kind,schema=1,as_of=as_of,task_id=task_id,
            cohorts=copy.deepcopy(state['cohorts']),ledger_ref=state['ledger_ref'],file_ids=dict(state['file_ids']),
            learning_states=copy.deepcopy(state['learning_states'])))
    def execute(self,task,state,store):
        op=task['op'];args=task['arguments'];ident=args.get('cohort')
        if op=='ACCOUNT':
            response=self.sources.verify_account(state['account_fingerprint'],task['id']);state['account_verified']=True
            return dict(source=response)
        if op=='DISCOVER':
            for ref in store.scan_refs():
                if ref in state['seen_scans']:continue
                archive=store.scanner_archive(ref)
                if (stamp(self.clock())-stamp(archive['frozen_at'])).total_seconds()>900:
                    state['seen_scans'].append(ref);continue
                # Include existing descriptors to avoid overlapping cohorts in same
                # scope; no closed-universe survivor selection is introduced.
                rows=[dict(cohort_id=k,scope_key=r['scope_key'],state=r['state'],closed_at=r.get('closed_at'),universe_frozen_at=r['universe_frozen_at'],planned_exit_at=r.get('planned_exit_at'),candidates=[dict(key=t) for t in r['tickers']]) for k,r in state['cohorts'].items()]
                cs=dict(schema_version=1,cohorts=rows);cs['store_hash']=digest(cs)
                updated=update_cohorts(dict(as_of=self.clock(),store=cs,archive=archive))
                for row in updated['store']['cohorts']:
                    if row['cohort_id'] not in state['cohorts']:state['cohorts'][row['cohort_id']]=describe(row,remember(store,state,row))
                state['seen_scans'].append(ref)
            state['seen_scans']=state['seen_scans'][-64:]
            return dict(seen=len(state['seen_scans']))
        if op=='HISTORY':
            page=self.sources.history_page(args['minimum'],args['page'],task['id'])
            prior=store.get(state['ledger_ref'])
            from execution_attribution import match
            state['ticket_records']=store.tickets()
            attribution=match(page['trades'],state['ticket_records'])
            links=state.setdefault('execution_links',{})
            for key,link in attribution['links'].items():
                if key in links and links[key]!=link:raise ValueError('CONFLICTING_EXECUTION_ATTRIBUTION')
                links[key]=link
            # ingest expects a page1 sequence; one page block is normalized as
            # an independent batch, retaining the real provider page separately.
            one=dict(page,page=1)
            ledger=ingest([one],'USD',prior=prior,links=state.get('execution_links',{}))
            state['ledger_ref']=remember(store,state,ledger['entries'])
            if page['hasMore']:add_task(state,'HISTORY',task['key']+'/p'+str(args['page']+1),dict(minimum=args['minimum'],page=args['page']+1))
            else:state['history_complete_at']=self.clock()
            state['ledger_kpis']=kpis(ledger['entries']);state['category_kpis']=category_kpis(ledger['entries'])
            return dict(page=page,ledger_errors=ledger['errors'],attribution_issues=attribution['issues'],ticket_records_observed=len(state['ticket_records']),coverage_certified=False)
        if op=='CONTEXT':
            data=self.sources.contexts(args['tickers'],task['id']);desc=state['cohorts'][ident]
            desc.setdefault('contexts',{}).update({k:remember(store,state,value) for k,value in data['contexts'].items()})
            desc.setdefault('context_dates',{}).update({k:args['context_date'] for k in data['contexts']})
            return data
        if op in {'ENTRY','EXIT'}:
            desc=state['cohorts'][ident];row=store.get(desc['ref'])
            if op=='ENTRY':
                contexts={k:store.get(v) for k,v in desc['contexts'].items()}
                observation=self.sources.entry(row,contexts,desc['benchmark'],task['id'])
            else:observation=self.sources.exit(row,task['id'])
            result=update_cohorts(dict(as_of=self.clock(),store=one_store(row),observations={ident:observation}))
            if result['errors']:raise Pending('COHORT_OBSERVATION_REJECTED:'+result['errors'][0]['reason'],300)
            updated=result['store']['cohorts'][0]
            if updated['state']==row['state']:raise Pending('COHORT_WAITING',300)
            next_desc=describe(updated,remember(store,state,updated));next_desc['contexts']=desc.get('contexts',{});next_desc['context_dates']=desc.get('context_dates',{})
            state['cohorts'][ident]=next_desc
            event_ref=self.event(state,store,'AUTONOMOUS_COHORT_OBSERVATION',task['id'],observation['observed_at'])
            add_task(state,'PUBLISH',task['id'],dict(event_ref=event_ref))
            return dict(cohort_id=ident,state=updated['state'],observation=observation)
        if op=='FEEDBACK':
            rows=[]
            for descriptor in state['cohorts'].values():
                row=store.get(descriptor['ref'])
                if row['state']=='CLOSED':
                    row.pop('entry',None);row.pop('exit',None);row['raw_observation_reference']=descriptor['ref']
                rows.append(row)
            cs=dict(schema_version=1,cohorts=rows);cs['store_hash']=digest(cs)
            result=feedback(dict(as_of=self.clock(),account_currency='USD',prior_ledger=store.get(state['ledger_ref']),cohort_store=cs,
                                 learning_state=state.get('learning_state'),learning_states=state['learning_states']))
            # The actual existing learner decides sample sufficiency and month gates.
            # Only after original Registro archive/readback may proposed states apply.
            result_ref=remember(store,state,result)
            proposed=dict(state,learning_states=result['learning_states'])
            event_ref=self.event(proposed,store,'PROPOSED_LEARNING_REVIEW',task['id'],result['as_of'])
            add_task(state,'APPLY_FEEDBACK',task['id'],dict(review_ref=result_ref,event_ref=event_ref))
            return dict(progress=result['collector_progress'],reviews=result['learning_reviews'])
        if op=='PUBLISH':
            receipt=self.publish(task,store);state['last_snapshot']=receipt;return receipt
        if op=='APPLY_FEEDBACK':
            receipt=self.publish(task,store);review=store.get(args['review_ref'])
            state.update(learning_states=review['learning_states'],feedback_ref=args['review_ref'],last_snapshot=receipt)
            return dict(receipt=receipt,applied_after_original_registry_readback=True)
        raise ValueError('UNKNOWN_COLLECTION_TASK')
