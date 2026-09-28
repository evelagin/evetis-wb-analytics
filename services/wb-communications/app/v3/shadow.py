"""WP10 — v3 shadow runner. Runs v3 on REAL production communications without affecting v2.

Isolation guarantees (tested in tests/v3/test_isolation.py):
* reads v2 Firestore docs, never writes them (its own ledger collection ``v3_shadow_runs``);
* never calls Wildberries or Telegram (no such client is reachable from here);
* runs AFTER the v2 poll finished, inside a time budget; every failure is caught per item
  and the whole runner is wrapped — v3 can never break or delay v2 publication.
Ledger key = (communication, engine_version, snapshot_id): one full decision per pair. When
the v2 final text changes later (manual edit / publication), only the verifier is re-run on
it (run_kind ``v2_final_recheck``, no LLM call).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.utils.logging import get_logger, log_event
from app.v3 import ENGINE_VERSION
from app.v3 import journal as journal_mod
from app.v3 import verifier as verifier_mod
from app.v3.engine import V3Engine
from app.v3.resolver import resolve

logger = get_logger(__name__)
LEDGER_COLLECTION = "v3_shadow_runs"


def _sha(t: Optional[str]) -> Optional[str]:
    return hashlib.sha256(t.encode("utf-8")).hexdigest() if t else None


def ledger_key(doc_id: str, snapshot_id: str) -> str:
    return hashlib.sha1(f"{doc_id}|{ENGINE_VERSION}|{snapshot_id}".encode()).hexdigest()


def message_from_doc(doc_id: str, doc: dict) -> dict:
    versions = doc.get("answer_versions") or []
    last_src = (versions[-1] or {}).get("source") if versions and isinstance(versions[-1], dict) else None
    try:
        rating = int(doc["rating"]) if doc.get("rating") not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "communication_id": doc_id, "entity_type": doc.get("entity_type") or "review",
        "marketplace": (doc.get("channel") or "wb").upper(), "source_id": doc.get("source_id"),
        "nm_id": doc.get("nm_id"), "supplier_article": doc.get("supplier_article"),
        "barcode": doc.get("barcode"), "product_name": doc.get("product_name"),
        "text": doc.get("text") or "", "pros": doc.get("pros") or "", "cons": doc.get("cons") or "",
        "rating": rating, "buyer_name": doc.get("buyer_name") or "",
        "v2_ai_answer": doc.get("ai_answer"), "v2_final_answer": doc.get("final_answer"),
        "v2_answer_source": last_src,
    }


class MemoryShadowStore:
    """Offline store (tests): candidates from a MemoryRepository-like dict + in-memory ledger."""

    def __init__(self, docs: dict):
        self._docs = docs
        self.ledger: dict[str, dict] = {}

    def candidates(self, scan_limit: int) -> list[tuple[str, dict]]:
        items = sorted(self._docs.items(), key=lambda kv: str(kv[1].get("first_seen_at") or ""), reverse=True)
        return [(k, dict(v)) for k, v in items[:scan_limit]
                if v.get("entity_type") in ("review", "question")]

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        return {k: self.ledger[k] for k in keys if k in self.ledger}

    def mark(self, key: str, value: dict) -> None:
        self.ledger[key] = dict(value)


class FirestoreShadowStore:
    def __init__(self, repo, settings):
        self._repo = repo
        self._s = settings

    def _client(self):
        return self._repo._lazy()  # same client/database as the v2 repository (read-only use)

    def candidates(self, scan_limit: int) -> list[tuple[str, dict]]:
        from google.cloud import firestore
        col = self._client().collection(self._s.firestore_collection)
        q = col.order_by("first_seen_at", direction=firestore.Query.DESCENDING).limit(scan_limit)
        return [(s.id, s.to_dict()) for s in q.stream()
                if (s.to_dict() or {}).get("entity_type") in ("review", "question")]

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        col = self._client().collection(LEDGER_COLLECTION)
        refs = [col.document(k) for k in keys]
        return {s.id: s.to_dict() for s in self._client().get_all(refs) if s.exists}

    def mark(self, key: str, value: dict) -> None:
        self._client().collection(LEDGER_COLLECTION).document(key).set(value)


@dataclass
class V3Runtime:
    engine: V3Engine
    store: Any
    writer: Any                               # .insert_v3_decision(row) -> bool
    settings: Any
    cost_in: float = 0.0
    cost_out: float = 0.0
    classifier_prompt_version: str = ""
    generator_prompt_version: str = ""


@dataclass
class ShadowSummary:
    scanned: int = 0
    decided: int = 0
    rechecked: int = 0
    persisted: int = 0
    persist_failed: int = 0
    errors: int = 0
    skipped_budget: int = 0
    outcomes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run_shadow(rt: V3Runtime, *, max_items: int, deadline: float, scan_limit: int = 150,
               clock=time.monotonic) -> ShadowSummary:
    s = ShadowSummary()
    snap_id = rt.engine.snapshot.snapshot_id
    cands = rt.store.candidates(scan_limit)
    s.scanned = len(cands)
    keys = {doc_id: ledger_key(doc_id, snap_id) for doc_id, _ in cands}
    done = rt.store.get_many(list(keys.values()))
    for doc_id, doc in cands:
        if s.decided + s.rechecked >= max_items:
            break
        if clock() >= deadline:
            s.skipped_budget += 1
            break
        key = keys[doc_id]
        prev = done.get(key)
        msg = message_from_doc(doc_id, doc)
        msg.update(_classifier_prompt_version=rt.classifier_prompt_version,
                   _generator_prompt_version=rt.generator_prompt_version, _cost_in=rt.cost_in, _cost_out=rt.cost_out)
        final_sha = _sha(msg.get("v2_final_answer"))
        try:
            if prev and prev.get("v2_final_sha256") == final_sha:
                continue
            if prev:
                row = _recheck_row(rt, msg, snap_id)
                s.rechecked += 1
            else:
                decision, stats = rt.engine.decide(msg)
                row = journal_mod.build_row(decision, msg, stats)
                s.decided += 1
                s.outcomes[decision.final_outcome] = s.outcomes.get(decision.final_outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001 — explicit failure row, never silent
            s.errors += 1
            row = journal_mod.failure_row(msg, snapshot_id=snap_id, engine_version=ENGINE_VERSION,
                                          error_class=type(exc).__name__, failure_code="ENGINE_ERROR")
            log_event(logger, "warning", "v3 shadow decision failed", communication_id=doc_id,
                      error=type(exc).__name__)
        ok = False
        try:
            ok = bool(rt.writer.insert_v3_decision(row))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, "warning", "v3 decision persistence failed", error=type(exc).__name__)
        if ok:
            s.persisted += 1
            try:
                rt.store.mark(key, {"communication_id": doc_id, "engine_version": ENGINE_VERSION,
                                    "snapshot_id": snap_id, "decision_id": row.get("decision_id"),
                                    "final_outcome": row.get("final_outcome"),
                                    "v2_final_sha256": final_sha,
                                    "updated_at": datetime.now(timezone.utc).isoformat()})
            except Exception as exc:  # noqa: BLE001
                log_event(logger, "warning", "v3 ledger write failed", error=type(exc).__name__)
        else:
            s.persist_failed += 1
        log_event(logger, "info", "v3 shadow decision", communication_id=doc_id, engine_version=ENGINE_VERSION,
                  snapshot_id=snap_id, product_resolution_status=row.get("product_resolution_status"),
                  strategy=row.get("strategy"), final_outcome=row.get("final_outcome"),
                  verifier_verdict=row.get("verifier_verdict"), latency_ms=row.get("latency_ms_total"),
                  error_class=row.get("error_class"), run_kind=row.get("run_kind"))
    return s


def _recheck_row(rt: V3Runtime, msg: dict, snap_id: str) -> dict:
    """Verifier-only re-check of a changed v2 final text (manual edit / published text)."""
    snap = rt.engine.snapshot
    res = resolve(snap, nm_id=msg.get("nm_id"), supplier_article=msg.get("supplier_article"),
                  barcode=msg.get("barcode"))
    ctx = verifier_mod.context_for_free_text(snap, [res.product_id] if res.product_id else [])
    r = verifier_mod.verify(msg.get("v2_final_answer"), ctx, snap)
    row = journal_mod.failure_row(msg, snapshot_id=snap_id, engine_version=ENGINE_VERSION,
                                  error_class=None, failure_code=None, run_kind="v2_final_recheck")
    row.update({"decision_id": journal_mod.decision_id(msg["communication_id"] + "|" + (_sha(msg.get("v2_final_answer")) or ""),
                                                       ENGINE_VERSION, snap_id, "v2_final_recheck"),
                "final_outcome": None, "product_id": res.product_id,
                "product_resolution_status": res.status,
                "v2_final_answer_sha256": _sha(msg.get("v2_final_answer")), "v2_final_verdict": r.verdict,
                "v2_final_block_rules": journal_mod._j(r.rule_ids("BLOCK")),
                "v2_answer_source": msg.get("v2_answer_source"),
                "v2_checks_json": journal_mod._j({"v2_final_answer": r.to_dict()})})
    return row


def run_shadow_isolated(rt: Optional[V3Runtime], *, poll_started: float, clock=time.monotonic) -> dict:
    """Entry point used by /poll. Never raises."""
    if rt is None:
        return {"enabled": False}
    try:
        st = rt.settings
        hard_stop = poll_started + float(getattr(st, "v3_shadow_poll_deadline_seconds", 150))
        deadline = min(clock() + float(getattr(st, "v3_shadow_budget_seconds", 60)), hard_stop)
        if clock() >= deadline:
            return {"enabled": True, "skipped": "no_time_budget"}
        return {"enabled": True, **run_shadow(rt, max_items=int(getattr(st, "v3_shadow_max_items_per_poll", 6)),
                                              deadline=deadline, clock=clock).to_dict()}
    except Exception as exc:  # noqa: BLE001 — shadow must never break v2
        log_event(logger, "warning", "v3 shadow runner failed (isolated)", error=type(exc).__name__)
        return {"enabled": True, "error": type(exc).__name__}


def check_text(rt: Optional[V3Runtime], doc_id: str, doc: dict, text: str, *, run_kind: str,
               persist: bool = True):
    """Run the SAME v3 verifier on arbitrary operator/v2 text (free-text context of the
    resolved product). Used for manual edits (shadow record) and, only when the owner turns
    on V3_ENFORCE_MANUAL_EDIT_VERIFIER, as a publish gate. Returns VerifierResult or None."""
    if rt is None or not text:
        return None
    snap = rt.engine.snapshot
    msg = message_from_doc(doc_id, doc)
    res = resolve(snap, nm_id=msg.get("nm_id"), supplier_article=msg.get("supplier_article"),
                  barcode=msg.get("barcode"))
    result = verifier_mod.verify(text, verifier_mod.context_for_free_text(snap, [res.product_id] if res.product_id else []),
                                 snap)
    if persist:
        row = journal_mod.failure_row(msg, snapshot_id=snap.snapshot_id, engine_version=ENGINE_VERSION,
                                      error_class=None, failure_code=None, run_kind=run_kind)
        row.update({"decision_id": journal_mod.decision_id(f"{doc_id}|{_sha(text)}", ENGINE_VERSION,
                                                           snap.snapshot_id, run_kind),
                    "final_outcome": None, "product_id": res.product_id, "product_resolution_status": res.status,
                    "v2_final_answer_sha256": _sha(text), "v2_final_verdict": result.verdict,
                    "v2_final_block_rules": journal_mod._j(result.rule_ids("BLOCK")),
                    "v2_answer_source": "manual", "v2_checks_json": journal_mod._j({run_kind: result.to_dict()})})
        try:
            rt.writer.insert_v3_decision(row)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, "warning", "v3 manual check persistence failed", error=type(exc).__name__)
    return result
