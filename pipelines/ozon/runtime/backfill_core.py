"""WINDOW_V1: pure bounded-plan and continuation contract; no I/O or tenant constants."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

VERSION = "WINDOW_V1"
MSK = timezone(timedelta(hours=3))
DOMAINS = frozenset({"fbo_postings", "finance_accrual", "catalog", "supplies",
                     "ads_campaigns", "ads_expense_daily", "ads_sku_daily",
                     "prices", "stocks", "seller_info", "clusters"})
DATED = frozenset({"fbo_postings", "finance_accrual", "ads_expense_daily", "ads_sku_daily"})


class EvidenceError(RuntimeError):
    pass


class CapacityReached(EvidenceError):
    """A full nonterminal source window must split, never silently truncate."""


class BudgetReached(EvidenceError):
    """No incomplete unit is committed; retry continues from previous durable proof."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def implementation_hash():
    # Qualified app code only. No credentials/configuration stores are inspected.
    root = Path(__file__).resolve().parent
    names = ("backfill_core.py", "backfill.py", "main.py", "entities.py", "common.py",
             "identity.py", "seller_policy.py", "seller_method_policy.json", "runtime_execution_contract.json",
             "checkpoints.py", "control_store.py", "credentials.py", "dq.py", "history.py", "lifecycle.py",
             "lifecycle_core.py", "promo.py", "quota.py", "requirements.txt")
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def utc_ms(day):
    return int(datetime.combine(date.fromisoformat(day), time.min, MSK).timestamp() * 1000)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def integer(env, key, default, low, high):
    value = env.get(key, str(default))
    if not re.fullmatch(r"[0-9]{1,9}", value) or not low <= int(value) <= high:
        raise EvidenceError(f"invalid bounded parameter {key}")
    return int(value)


def plan(env, entity, project, raw, ref, today, now=None):
    if entity not in DOMAINS or env.get("BACKFILL_MODE") != VERSION:
        raise EvidenceError("unsupported backfill contract")
    if env.get("TENANT_BINDING_REQUIRED") != "1" or env.get("STRICT_PAGE_CAPS") != "1":
        raise EvidenceError("backfill requires binding and strict pagination")
    if env.get("BACKFILL_TARGET_PROJECT") != project or raw != "ozon_raw" or ref != "ref":
        raise EvidenceError("backfill target/config mismatch")
    frm, to = env.get("SINCE", ""), env.get("UNTIL", "")
    try:
        a, b = date.fromisoformat(frm), date.fromisoformat(to)
    except ValueError:
        raise EvidenceError("explicit backfill dates required") from None
    if str(a) != frm or str(b) != to or a > b or b >= today:
        raise EvidenceError("backfill requires finished Moscow days")
    generation = env.get("BACKFILL_GENERATION", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", generation):
        raise EvidenceError("explicit initial/refresh generation required")
    origin = env.get("BACKFILL_ORIGIN", "")
    try:
        dt = datetime.fromisoformat(origin.replace("Z", "+00:00"))
        if dt.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise EvidenceError("explicit UTC journal origin required") from None
    if dt > (now or datetime.now(timezone.utc)):
        raise EvidenceError("journal origin is in the future; progress cannot be hidden")
    out = {"version": VERSION, "project": project, "raw": raw, "ref": ref,
           "entity": entity, "from": frm, "to": to, "generation": generation,
           "origin": origin, "window_days": integer(env, "BACKFILL_WINDOW_DAYS", 1, 1, 30),
           "minimum_ms": integer(env, "BACKFILL_MIN_SECONDS", 60, 1, 86400) * 1000,
           "fbo_page_cap": integer(env, "BACKFILL_FBO_PAGE_CAP", 200, 1, 200),
           "order_batch": integer(env, "BACKFILL_ORDER_BATCH", 5, 1, 25),
           "bundle_page_cap": integer(env, "BACKFILL_BUNDLE_PAGE_CAP", 500, 1, 500)}
    out["implementation_hash"] = implementation_hash()
    if entity not in DATED and entity != "supplies":
        out["observation_date"] = str(today)
    out["plan_id"] = digest(out)
    return out


def initial(p):
    e = p["entity"]
    if e == "fbo_postings":
        end = str(date.fromisoformat(p["to"]) + timedelta(days=1))
        progress = {"pending": [[utc_ms(p["from"]), utc_ms(end)]], "completed_to": utc_ms(p["from"])}
    elif e in DATED:
        progress = {"next_day": p["from"]}
    elif e == "supplies":
        progress = {"cursor": "", "list_done": False, "pending": [], "bundle": None,
                    "cursors": [], "seen_orders": [], "bundle_links": {}}
    else:
        progress = {"done": False}
    return {"plan_id": p["plan_id"], "sequence": 0, "complete": False,
            "progress": progress, "rows": 0, "requests": 0, "pages": 0,
            "orders": 0, "supplies": 0, "bundles": 0}


def split(window, minimum_ms):
    a, b = window
    if b - a < 2 * minimum_ms:
        raise EvidenceError("FBO_MINIMUM_WINDOW_EXHAUSTED: source completeness unproven")
    mid = a + (b - a) // 2
    return [a, mid], [mid, b]


def validate(p, state, *, reserve=0):
    if state.get("plan_id") != p["plan_id"] or type(state.get("sequence")) is not int or state["sequence"] < 0:
        raise EvidenceError("checkpoint plan/sequence mismatch")
    if type(state.get("complete")) is not bool:
        raise EvidenceError("checkpoint completion not boolean")
    for key in ("rows", "requests", "pages", "orders", "supplies", "bundles"):
        if type(state.get(key)) is not int or state[key] < 0:
            raise EvidenceError("invalid checkpoint accounting")
    progress = state["progress"]
    if p["entity"] == "fbo_postings":
        end = utc_ms(str(date.fromisoformat(p["to"]) + timedelta(days=1)))
        cursor = progress["completed_to"]
        if not utc_ms(p["from"]) <= cursor <= end:
            raise EvidenceError("checkpoint outside source window")
        for a, b in progress["pending"]:
            if type(a) is not int or type(b) is not int or a != cursor or b <= a or b > end:
                raise EvidenceError("checkpoint gap/overlap")
            cursor = b
        if cursor != end or state["complete"] != (not progress["pending"]):
            raise EvidenceError("checkpoint incomplete coverage claimed complete")
    elif p["entity"] in DATED:
        cur = date.fromisoformat(progress["next_day"])
        end = date.fromisoformat(p["to"]) + timedelta(days=1)
        if not date.fromisoformat(p["from"]) <= cur <= end or state["complete"] != (cur == end):
            raise EvidenceError("invalid day continuation")
    elif p["entity"] == "supplies":
        if state["complete"] != (progress["list_done"] and not progress["pending"] and progress["bundle"] is None):
            raise EvidenceError("unfinished supplies claimed complete")
    elif state["complete"] != progress["done"]:
        raise EvidenceError("unfinished snapshot claimed complete")
    if len(json.dumps(state).encode()) > 900000 - reserve:
        raise EvidenceError("durable continuation exceeds bounded proof size; scope must be partitioned")
    return state


def source_terminal(payload, cursor_name, seen, *, require_flag=False):
    nxt = payload.get(cursor_name)
    if require_flag:
        flag = payload.get("has_next")
        if type(flag) is not bool:
            raise EvidenceError("source has_next missing/invalid")
        if not flag:
            if nxt is not None and not isinstance(nxt, str):
                raise EvidenceError("source cursor invalid")
            return True, nxt or ""
        if not isinstance(nxt, str) or not nxt:
            raise EvidenceError("source has_next without cursor")
    elif not isinstance(nxt, str):
        raise EvidenceError("source cursor missing/invalid")
    elif not nxt:
        return True, nxt
    if nxt in seen:
        raise EvidenceError("source repeated cursor")
    seen.add(nxt)
    return False, nxt
