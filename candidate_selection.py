"""1.0.0: deterministic global shortlist budget; no ticket authorization."""
import copy
import json
import math
from pathlib import Path

from policy_contract import load_policy
CONFIG = load_policy("adaptive_config.json")


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def score(features, profile):
    weights = CONFIG["profiles"][profile]
    if not isinstance(features, dict) or set(features) != set(weights) or any(not finite(v) or not 0 <= v <= 100 for v in features.values()):
        return None
    return sum(features[k] * weights[k] / 100 for k in weights)


def select(data):
    profile = data.get("active_profile", "balanced")
    if profile not in CONFIG["profiles"]:
        raise ValueError("PROFILE_NOT_REGISTERED")
    limit = data.get("max_results", CONFIG["max_chat_candidates"])
    if type(limit) is not int or not 0 <= limit <= CONFIG["max_chat_candidates"]:
        raise ValueError("GLOBAL_BUDGET_INVALID")
    pool, conflicts, excluded, invalid = {}, [], [], []
    for ordinal, row in enumerate(data.get("candidates", [])):
        if not isinstance(row,dict):
            invalid.append(dict(ordinal=ordinal,reason='CANDIDATE_NOT_OBJECT'));continue
        row = copy.deepcopy(row)
        key = row.get("key") or row.get("ticker")
        if not isinstance(key, str) or not key.strip():
            invalid.append(dict(ordinal=ordinal,reason='CANDIDATE_KEY_REQUIRED'));continue
        key = key.upper().strip()
        if row.get("extended") or row.get("setup") == "ESTESO" or row.get("actionability") in {"NO_CHASE", "WATCH_ONLY"}:
            excluded.append(key); continue
        scope=data.get("profile_scope")
        effective=profile if not scope or (row.get("category")==scope.get("category") and row.get("method_id")==scope.get("method_id")) else "balanced"
        adaptive = score(row.get("ranking_features"), effective)
        native = row.get("fusion_score")
        row.update(key=key, selection_score=adaptive,
                   native_score=native if finite(native) and 0<=native<=100 else None, input_ordinal=ordinal,
                   applied_profile=effective,score_basis="ADAPTIVE_COMPLETE_FEATURES" if adaptive is not None else "NATIVE_OR_ORDER_NO_IMPUTATION")
        prior = pool.get(key)
        if prior:
            if prior.get("instrument_id") and row.get("instrument_id") and prior["instrument_id"] != row["instrument_id"]:
                conflicts.append(key)
            continue
        pool[key] = row
    pending = {key.upper().strip() for key in data.get("pending_keys", []) if isinstance(key,str)}
    rows = [row for key, row in pool.items() if key not in conflicts]
    # Unknown component is not zero; baseline ordinal stays explicit.
    def order(r):
        rank = r["selection_score"] if r["selection_score"] is not None else r["native_score"]
        return (r["key"] not in pending,rank is None,-rank if rank is not None else 0,r["input_ordinal"] if rank is None else 0,r["key"])
    rows.sort(key=order)
    return dict(version="1.0.0", active_profile=profile, budget=limit,
                shortlist=rows[:limit], deferred_keys=[r["key"] for r in rows[limit:]],
                identity_conflicts=sorted(set(conflicts)), excluded_keys=sorted(set(excluded)),
                invalid_rows=invalid,
                monitor_positions=data.get("monitor_positions", []), union_count=len(pool),
                ticket_authorized=False, identity_gate_required=True)
