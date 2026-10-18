"""1.0.0: one asynchronous cycle, global budget before chat receives tickers."""
import copy
import threading
import time
from candidate_selection import select

JOBS={}
LOCK=threading.Lock()
TTL=900


def run(markets,profile,pending,scan_a,scan_b,clock=time.monotonic,wait=time.sleep,timeout=1800,profile_scope=None):
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
    selection=select(dict(candidates=candidates,active_profile=profile,profile_scope=profile_scope,pending_keys=pending))
    # Deferred candidates remain server-side. Only bounded keys return to chat.
    return dict(version="1.0.0",shortlist=selection["shortlist"],returned=len(selection["shortlist"]),
                global_budget=selection["budget"],union_count=selection["union_count"],
                deferred_count=len(selection["deferred_keys"]),identity_conflicts=selection["identity_conflicts"],
                invalid_candidates=selection['invalid_rows'],
                coverage=scans,errors=errors,scan_complete=not errors,coverage_is_not_ticket_validation=True,
                active_profile=profile,ticket_authorized=False)


def request(markets,profile,pending,scan_a,scan_b,profile_scope=None):
    markets=tuple(sorted(set(markets)))
    if not markets or any(m not in {"US","EU"} for m in markets): raise ValueError("CYCLE_MARKETS_INVALID")
    scope_key=tuple(sorted((profile_scope or {}).items()))
    key=(markets,profile,tuple(sorted(pending)),scope_key)
    with LOCK:
        for k in list(JOBS):
            if JOBS[k].get("finished") and time.monotonic()-JOBS[k]["finished"]>TTL:del JOBS[k]
        job=JOBS.get(key)
        if not job:
            if any(j["state"]=="RUNNING" for j in JOBS.values()):return dict(job_status="BUSY",retry_after_s=30,ticket_authorized=False)
            job=dict(id="cycle-"+str(time.time_ns()),state="RUNNING");JOBS[key]=job
            def worker():
                try:
                    result=run(markets,profile,pending,scan_a,scan_b,profile_scope=profile_scope)
                    with LOCK:job.update(state="COMPLETE",result=result,finished=time.monotonic())
                except Exception as exc:
                    with LOCK:job.update(state="ERROR",reason=str(exc)[:180],finished=time.monotonic())
            threading.Thread(target=worker,daemon=True,name="cycle-scanner").start()
        if job["state"]=="COMPLETE":return dict(copy.deepcopy(job["result"]),job_id=job["id"],job_status="COMPLETE")
        return dict(job_id=job["id"],job_status=job["state"],reason=job.get("reason"),scan_complete=False,retry_after_s=30,ticket_authorized=False)
