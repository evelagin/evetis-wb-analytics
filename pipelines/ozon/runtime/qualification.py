"""Reviewed, exact qualification continuation; historical plan != current artifact.

Only the accepted qualification root can resume across this compatible runtime change.
No arbitrary hash/image bypass, no credential or marketplace I/O. Ordinary plans
retain their original 15-export guard. The packaged file is code-hashed evidence.
"""
import copy
import json
import re
from pathlib import Path

ROOT = "4f4387b2bdaa39c8942735a3b9b386e665b20c61006c8e8a467832a7542b1779"
SKU = "d54a84904bba901fbf2788a60bff74eed178a62fd4f56eb41fbc19c8dbc5f701"


def manifest():
    import backfill_core as B
    doc = json.loads(Path(__file__).with_name("qualification_resume.json").read_text())
    if doc.get("hash") != ROOT or B.digest({k:v for k,v in doc.items() if k != "hash"}) != ROOT:
        raise B.EvidenceError("qualification compatibility evidence corrupt")
    return doc


def accepted_doc(pid):
    docs = [d for d in manifest()["plans"] if d["runtime_plan"]["plan_id"] == pid]
    return copy.deepcopy(docs[0]) if len(docs) == 1 else None


def matches(doc):
    return isinstance(doc, dict) and doc == accepted_doc(doc.get("runtime_plan", {}).get("plan_id"))


def resume(candidate, pid):
    import backfill_core as B
    doc = accepted_doc(pid)
    old = doc["runtime_plan"] if doc else None
    semantic = lambda p: {k:v for k,v in p.items() if k not in {"implementation_hash", "plan_id"}}
    if old is None or semantic(candidate) != semantic(old):
        raise B.EvidenceError("qualification resume scope differs; never reset/relabel history")
    # Retain immutable historical identity, not an assertion of current source.
    return old


def guard(p, state):
    import backfill_core as B
    if p.get("plan_id") != SKU:
        return 15
    if p != accepted_doc(SKU)["runtime_plan"]:
        raise B.EvidenceError("calibration outside accepted qualification")
    B.validate(p, state)
    pending = state["progress"].get("pending")
    if state["complete"]:
        return 15
    if not isinstance(pending, list) or len(set(pending)) != len(pending) or not 0 < len(pending) <= 60:
        raise B.EvidenceError("qualification prefix not preserved")
    completed = 90 - len(pending)
    # Start ten units above 15; each reconciled cohort permits at most ten more.
    cap = min(90, 25 + ((completed - 30) // 10) * 10)
    limit = state["progress"].get("rate_limit")
    if limit:
        if type(limit.get("safe_cap")) is not int or not 1 <= limit["safe_cap"] <= 90:
            raise B.EvidenceError("rate-limit guard evidence corrupt")
        cap = min(cap, limit["safe_cap"])
    return cap


def cohort(p, day, batch):
    import backfill_core as B
    if not isinstance(batch, list) or not 1 <= len(batch) <= 10 or len(set(batch)) != len(batch):
        raise B.EvidenceError("invalid report cohort")
    if any(not isinstance(x,str) or not re.fullmatch(r"[0-9]{1,20}",x) or not 0 < int(x) < 2**64 for x in batch):
        raise B.EvidenceError("campaign must be a uint64 string")
    return B.digest({"plan_id":p["plan_id"], "day":day, "campaigns":batch})
