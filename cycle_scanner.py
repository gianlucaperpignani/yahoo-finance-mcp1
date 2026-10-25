"""1.1.0: one asynchronous cycle, global budget before chat receives tickers."""
import copy
import threading
import time
from datetime import datetime,timezone
from adaptive_method import digest
from candidate_selection import select
import scan_archive_store
from archive_client import ArchiveError

JOBS={}
LOCK=threading.Lock()
TTL=900
MAX_JOBS=4
ARCHIVE_RETRY_S=60


def _persist(job):
    try:
        receipt=scan_archive_store.put(job["id"], job["archive"])
        if receipt.get("durable_verified") is not True:
            raise ArchiveError("SCAN_ARCHIVE_READBACK_REQUIRED")
        with LOCK:
            job.update(persistence=dict(state="VERIFIED",durable_verified=True,object_id=receipt["object_id"]))
    except Exception as exc:
        error=str(exc) if isinstance(exc,ArchiveError) else "SCAN_ARCHIVE_PERSIST_FAILED"
        with LOCK:
            job.update(persistence=dict(state="RETRY_PENDING",durable_verified=False,error=error))
    finally:
        with LOCK:
            job["persisting"]=False
            job["persist_attempt_at"]=time.monotonic()


def _archive_reference(job):
    ref=dict(path="/cycle-archive/"+job["id"], archive_id=job["archive"]["archive_id"],
             expires_after_s=TTL, instruction="download directly to Python; persist in original Registro bundle; do not expand candidate pool in chat")
    persistence=copy.deepcopy(job.get("persistence",{}))
    if persistence.get("durable_verified") is True:
        ref.update(path=ref["path"]+"?archive_object_id="+persistence["object_id"],persistent=True,
                   persistence=persistence)
    else:
        ref.update(persistent=False,persistence=persistence)
    return ref


def status():
    with LOCK:
        return dict(version="1.1.0", configured=scan_archive_store.enabled(), retained_jobs=len(JOBS),
                    max_jobs=MAX_JOBS, pending_archives=sum(bool(j.get("archive")) and not j.get("persistence",{}).get("durable_verified",False) for j in JOBS.values()),
                    verified_archives=sum(j.get("persistence",{}).get("durable_verified",False) for j in JOBS.values()),
                    price_cache_persistent=False, autonomous_collection=False)


def run(markets,profile,pending,scan_a,scan_b,clock=time.monotonic,wait=time.sleep,timeout=1800,profile_scope=None,learning_states=None,archive_sink=None):
    scans=[];errors=[];candidates=[];started=clock();fusion={}
    for market in markets:
        try:
            result=scan_a(market)
            scans.append(dict(level="A",market=market,coverage=result.get("coverage_summary"),scan_complete=result.get("scan_complete"),generated_at=result.get("generated_at_utc")))
            candidates.extend(copy.deepcopy(result.get("shortlist",[])))
        except Exception as exc: errors.append(dict(level="A",market=market,reason=str(exc)[:180]))
        try: fusion[market]=scan_b(market)
        except Exception as exc: fusion[market]=dict(job_status="ERROR",reason=str(exc)[:180])
    while any(r.get("job_status") in {"QUEUED","RUNNING","BUSY"} for r in fusion.values()) and clock()-started<timeout:
        wait(2)
        for market,result in list(fusion.items()):
            if result.get("job_status") in {"QUEUED","RUNNING","BUSY"}:
                try: fusion[market]=scan_b(market)
                except Exception as exc: fusion[market]=dict(job_status="ERROR",reason=str(exc)[:180])
    for market,result in fusion.items():
        scans.append(dict(level="B",market=market,job_id=result.get("job_id"),job_status=result.get("job_status"),coverage=result.get("coverage"),limitations=result.get("limitations"),generated_at=result.get("result_generated_at_utc")))
        if result.get("job_status")!="COMPLETE": errors.append(dict(level="B",market=market,reason=result.get("reason") or "TIME_BUDGET"))
        else: candidates.extend(copy.deepcopy(result.get("shortlist",[])))
    selection=select(dict(candidates=candidates,active_profile=profile,profile_scope=profile_scope,pending_keys=pending,learning_states=learning_states,_internal=True))
    artifact=dict(schema_version=1,frozen_at=datetime.now(timezone.utc).isoformat(),opportunities=selection.pop('_all_rows'),coverage=scans,errors=errors)
    artifact['archive_id']=digest(artifact)
    if archive_sink:archive_sink(artifact)
    # Deferred candidates remain server-side. Only bounded keys return to chat.
    complete = not errors and all(s.get("scan_complete") is True for s in scans if s["level"] == "A")
    return dict(version="1.1.0",shortlist=selection["shortlist"],returned=len(selection["shortlist"]),
                global_budget=selection["budget"],union_count=selection["union_count"],
                deferred_count=len(selection["deferred_keys"]),identity_conflicts=selection["identity_conflicts"],
                invalid_candidates=selection['invalid_rows'],
                coverage=scans,errors=errors,scan_complete=complete,technical_jobs_finished=not errors,
                coverage_is_not_ticket_validation=True,
                active_profile=profile,ticket_authorized=False)


def request(markets,profile,pending,scan_a,scan_b,profile_scope=None,learning_states=None):
    markets=tuple(sorted(set(markets)))
    if not markets or any(m not in {"US","EU"} for m in markets): raise ValueError("CYCLE_MARKETS_INVALID")
    scope_key=tuple(sorted((profile_scope or {}).items()))
    key=(markets,profile,tuple(sorted(pending)),scope_key,digest(learning_states or {}))
    with LOCK:
        for k in list(JOBS):
            if JOBS[k].get("finished") and time.monotonic()-JOBS[k]["finished"]>TTL:
                old=JOBS[k]
                if not old.get("archive") or old.get("persistence",{}).get("durable_verified"):
                    if not old.get("persisting"):del JOBS[k]
        job=JOBS.get(key)
        if not job:
            if len(JOBS)>=MAX_JOBS:return dict(job_status="BUSY",reason="ARCHIVE_BACKLOG_OR_JOB_CAPACITY",retry_after_s=60,ticket_authorized=False)
            if any(j["state"]=="RUNNING" for j in JOBS.values()):return dict(job_status="BUSY",retry_after_s=30,ticket_authorized=False)
            job=dict(id="cycle-"+str(time.time_ns()),state="RUNNING");JOBS[key]=job
            def worker():
                try:
                    result=run(markets,profile,pending,scan_a,scan_b,profile_scope=profile_scope,learning_states=learning_states,archive_sink=lambda a:job.update(archive=a))
                    if scan_archive_store.enabled():
                        with LOCK:job.update(persisting=True,persistence=dict(state="WRITING",durable_verified=False))
                        _persist(job)
                    else:
                        job['persistence']=dict(state="NOT_CONFIGURED",durable_verified=False)
                    with LOCK:job.update(state="COMPLETE",result=result,finished=time.monotonic())
                except Exception as exc:
                    with LOCK:job.update(state="ERROR",reason=str(exc)[:180],finished=time.monotonic())
            threading.Thread(target=worker,daemon=True,name="cycle-scanner").start()
        if job["state"]=="COMPLETE":
            if (scan_archive_store.enabled() and not job.get("persistence",{}).get("durable_verified")
                    and not job.get("persisting") and time.monotonic()-job.get("persist_attempt_at",0)>=ARCHIVE_RETRY_S):
                job.update(persisting=True,persistence=dict(state="WRITING",durable_verified=False))
                threading.Thread(target=_persist,args=(job,),daemon=True,name="scan-archive-retry").start()
            result=dict(copy.deepcopy(job["result"]),job_id=job["id"],job_status="COMPLETE")
            result['opportunity_archive']=_archive_reference(job)
            return result
        return dict(job_id=job["id"],job_status=job["state"],reason=job.get("reason"),scan_complete=False,retry_after_s=30,ticket_authorized=False)


def get_archive(job_id, object_id=None):
    with LOCK:
        for job in JOBS.values():
            if job['id']==job_id and job.get('archive') and job.get('finished') and (time.monotonic()-job['finished']<=TTL or not job.get("persistence",{}).get("durable_verified")):
                return copy.deepcopy(job['archive'])
    if object_id:
        return scan_archive_store.get(job_id,object_id)
    raise KeyError('CYCLE_ARCHIVE_EXPIRED_OR_UNKNOWN')
