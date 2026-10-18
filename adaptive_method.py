"""1.0.0: monthly deterministic champion/challenger; never changes risk limits.

No network. Store returned state in original Registro and read it back before use.
Opportunity returns are shadow simulations, never booked as actual performance.
"""
import copy
import hashlib
import json
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from candidate_selection import CONFIG, score, finite

VERSION = "1.0.0"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def stamp(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("LEARNING_TIMEZONE_REQUIRED")
    return value


def initial():
    state = dict(schema_version=1, protocol=CONFIG["version"], active="balanced", previous=None,
                pending=None, activated_at=None, last_review_month=None, revision=0,
                status="ATTESA_CAMPIONE", used_cohorts=[], confidence_calibration=[])
    state['state_hash']=digest(state)
    return state


def validate_state(state):
    if state.get("schema_version") != 1 or state.get("protocol") != CONFIG["version"] or state.get("active") not in CONFIG["profiles"]:
        raise ValueError("LEARNING_STATE_INVALID")
    if not state.get("state_hash") or state["state_hash"] != digest({k: v for k, v in state.items() if k != "state_hash"}):
        raise ValueError("LEARNING_STATE_HASH_MISMATCH")
    return state


def cohorts(rows, as_of):
    valid, excluded, seen = [], [], set()
    for row in rows:
        if not isinstance(row,dict):
            excluded.append(dict(reason='INVALID_COHORT_TYPE'));continue
        try:
            key = row["cohort_id"]
            if key in seen: raise ValueError("DUPLICATE_COHORT")
            seen.add(key)
            start, end = stamp(row["signal_at"]), stamp(row["closed_at"])
            if not stamp(CONFIG["registered_at"]) <= start < end <= as_of: raise ValueError("TIME_ORDER")
            if stamp(row["universe_frozen_at"]) > start or not end <= stamp(row["outcomes_observed_at"]) <= as_of: raise ValueError("LOOK_AHEAD_OR_EARLY_OUTCOME")
            if row.get("complete_universe") is not True or row.get("kind") != "SHADOW_SIMULATION": raise ValueError("INCOMPLETE_OR_MIXED_COHORT")
            if not row.get("source_reference") or row.get("category") not in {"CORE", "SWING", "SPECULATIVO"} or not row.get("method_id"): raise ValueError("COHORT_PROVENANCE")
            candidates = row["candidates"]
            if not candidates or len({c["key"] for c in candidates}) != len(candidates): raise ValueError("CANDIDATES_INVALID")
            for c in candidates:
                if score(c.get("features"), "balanced") is None or not all(finite(c.get(k)) for k in ("net_return", "benchmark_return", "initial_risk_fraction")): raise ValueError("OUTCOME_OR_FEATURES_MISSING")
                if c["initial_risk_fraction"] <= 0 or c["net_return"] <= -1 or c.get("costs_included") is not True or c.get("slippage_included") is not True: raise ValueError("COSTS_OR_RISK_MISSING")
                if stamp(c["features_recorded_at"]) > start: raise ValueError("FEATURE_LOOK_AHEAD")
            valid.append(row)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            excluded.append(dict(cohort_id=row.get("cohort_id"), reason=str(exc)))
    valid.sort(key=lambda r: (stamp(r["signal_at"]), r["cohort_id"]))
    # One category/method per comparison; overlapping cohorts are not independent.
    groups = {(r["category"], r["method_id"]) for r in valid}
    if len(groups) > 1: return [], excluded + [dict(reason="MIXED_CATEGORY_OR_METHOD")]
    if any(stamp(a["closed_at"]) > stamp(b["signal_at"]) for a, b in zip(valid, valid[1:])):
        return [], excluded + [dict(reason="OVERLAPPING_COHORTS")]
    return valid, excluded


def evaluate(rows, profile):
    returns, excess, utility = [], [], []
    for row in rows:
        chosen = min(row["candidates"], key=lambda c: (-score(c["features"], profile), c["key"]))
        returns.append(chosen["net_return"])
        excess.append(chosen["net_return"] - chosen["benchmark_return"])
        utility.append(chosen["net_return"] / chosen["initial_risk_fraction"])
    wealth = peak = 1.; drawdown = 0.
    for value in returns:
        wealth *= 1 + value; peak = max(peak, wealth); drawdown = max(drawdown, 1 - wealth / peak)
    return dict(n=len(rows), mean_r=sum(utility) / len(utility) if utility else None,
                mean_excess=sum(excess) / len(excess) if excess else None,
                drawdown=drawdown, net_compound_return=wealth - 1, utility=utility)


def compare(rows, candidate, baseline):
    left, right = evaluate(rows, candidate), evaluate(rows, baseline)
    diffs = [a - b for a, b in zip(left["utility"], right["utility"])]
    if not diffs: return dict(pass_=False, n=0)
    randomizer = random.Random(20261018)
    samples = sorted(sum(randomizer.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(1000))
    passed = (samples[24] > 0 and left["mean_excess"] >= right["mean_excess"] and
              left["drawdown"] <= right["drawdown"] and left["net_compound_return"] > right["net_compound_return"])
    return dict(pass_=passed, n=len(rows), paired_mean_r=sum(diffs) / len(diffs),
                interval95=[samples[24], samples[974]], challenger={k:v for k,v in left.items() if k!='utility'},
                champion={k:v for k,v in right.items() if k!='utility'})


def review(data):
    now = stamp(data["as_of"]); state = copy.deepcopy(validate_state(data.get("state") or initial()))
    state.pop("state_hash", None)
    rows, excluded = cohorts(data.get("cohorts", []), now)
    if rows:
        scope=dict(category=rows[0]['category'],method_id=rows[0]['method_id'])
        if state.get('scope') and state['scope']!=scope:
            excluded.append(dict(reason='STATE_SCOPE_MISMATCH'));rows=[]
        else:state['scope']=scope
    result = dict(version=VERSION, as_of=data["as_of"], excluded=excluded, sample=len(rows),
                  action="NONE", evidence={}, ticket_authorized=False, automatic_trade=False)
    month = now.astimezone(ZoneInfo('Europe/Rome')).strftime("%Y-%m")
    if state.get("last_review_month") == month:
        result["status"] = "RIUSATO_MESE"
    else:
        state["last_review_month"] = month
        pending = state.get("pending")
        if pending:
            fresh = [r for r in rows if stamp(r["signal_at"]) > stamp(pending["frozen_at"]) and r["cohort_id"] not in state["used_cohorts"]]
            if len(fresh) >= CONFIG["min_shadow"]:
                selected = fresh[:CONFIG["min_shadow"]]
                comparison = compare(selected, pending["profile"], state["active"])
                result["evidence"] = comparison
                state["used_cohorts"] += [r["cohort_id"] for r in selected]
                if comparison["pass_"]:
                    state.update(previous=state["active"], active=pending["profile"], activated_at=data["as_of"], revision=state["revision"]+1)
                    result["action"] = "ACTIVATE"
                state["pending"] = None
                result["status"] = "ATTIVATO" if comparison["pass_"] else "CHALLENGER_RESPINTO"
            else: result["status"] = "ATTESA_SHADOW"
        elif state.get("previous"):
            fresh = [r for r in rows if stamp(r["signal_at"]) > stamp(state["activated_at"]) and r["cohort_id"] not in state["used_cohorts"]]
            if len(fresh) >= CONFIG["min_monitoring"]:
                selected=fresh[:CONFIG["min_monitoring"]]
                comparison=compare(selected,state["previous"],state["active"])
                result["evidence"]=comparison
                state["used_cohorts"] += [r["cohort_id"] for r in selected]
                if comparison["pass_"]:
                    state.update(active=state["previous"],previous=None,activated_at=data["as_of"],revision=state["revision"]+1)
                    result["action"]="ROLLBACK"
                result["status"]="RIPRISTINATO" if comparison["pass_"] else "MONITORATO"
                if not comparison["pass_"]:
                    available=[r for r in rows if r["cohort_id"] not in state["used_cohorts"]]
                    n,v=CONFIG["min_training"],CONFIG["min_validation"]
                    if len(available)>=n+v:
                        training,validation=available[:n],available[n:n+v]
                        chosen=max(sorted(CONFIG["profiles"]),key=lambda p:(evaluate(training,p)["mean_r"],p==state["active"],p))
                        check=compare(validation,chosen,state["active"])
                        state["used_cohorts"] += [r["cohort_id"] for r in training+validation]
                        if chosen!=state["active"] and check["pass_"]:
                            state["pending"]=dict(profile=chosen,frozen_at=data["as_of"],training_hash=digest(training),validation_hash=digest(validation))
                            result.update(action="FREEZE_CHALLENGER",status="SHADOW_AVVIATO",evidence=check)
            else: result["status"]="ATTESA_MONITORAGGIO"
        else:
            available = [r for r in rows if r["cohort_id"] not in state["used_cohorts"]]
            n, v = CONFIG["min_training"], CONFIG["min_validation"]
            if len(available) < n + v: result["status"] = "ATTESA_CAMPIONE"
            else:
                training, validation = available[:n], available[n:n+v]
                profiles=sorted(CONFIG["profiles"])
                chosen=max(profiles,key=lambda p:(evaluate(training,p)["mean_r"],p==state["active"],p))
                comparison=compare(validation,chosen,state["active"])
                result["evidence"]=comparison
                state["used_cohorts"] += [r["cohort_id"] for r in training+validation]
                if chosen!=state["active"] and comparison["pass_"]:
                    state["pending"]=dict(profile=chosen,frozen_at=data["as_of"],training_hash=digest(training),validation_hash=digest(validation))
                    result["action"]="FREEZE_CHALLENGER"
                result["status"]="SHADOW_AVVIATO" if state.get("pending") else "CONFIGURAZIONE_CONFERMATA"
    if result["status"] != "RIUSATO_MESE": state["status"]=result["status"]
    state["state_hash"]=digest(state)
    result["state"]=state;result["dataset_hash"]=digest(rows)
    return result
