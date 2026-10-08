"""State repositories.

Two implementations with identical semantics:

* ``FirestoreRepository`` — production, backed by Firestore transactions.
* ``MemoryRepository`` — in-memory twin for unit tests and the local smoke test.

Guarantees enforced here:
  * idempotent claim (no resend) with crash-safe LEASES on all in-flight states;
  * a full action state machine (stale buttons rejected);
  * edit/regenerate are LOCKED intermediate states (EDITING / REGENERATING) with a
    lock token + version guard, so a concurrent publish/skip/second-draft cannot
    race a stale write back into pending_approval;
  * atomic publish (no double publish; a crashed PUBLISHING lease is recovered
    but flagged for verification, never silently re-published);
  * BigQuery events via a Firestore OUTBOX (at-least-once into BQ, deduplicated
    by deterministic event_id) — NOT claimed as exactly-once;
  * webhook update de-dup (received→processing→processed) with attempts + poison
    guard and lease recovery.

Google/Firestore infra errors (ServiceUnavailable, DeadlineExceeded,
InternalServerError, Aborted, transport) are translated to
``FirestoreTransientError`` so the webhook returns 5xx instead of losing an update.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from app.domain.exceptions import (
    FirestoreTransientError,
    InvalidTransition,
    NotFound,
)
from app.domain.models import Review, make_doc_id
from app.domain.statuses import DRAFTABLE_FROM, Status, action_allowed

RETRIABLE = {Status.ERROR.value}
# PUBLISH_UNKNOWN is re-publishable, but the pipeline reads WB state first and
# only writes when the question is still unanswered (no blind duplicate).
PUBLISHABLE_FROM = {Status.PENDING_APPROVAL.value, Status.PUBLISH_FAILED.value,
                    Status.PUBLISH_UNKNOWN.value, Status.POLICY_BLOCKED.value,
                    Status.POLICY_CHECK_FAILED.value}
UPDATE_MAX_ATTEMPTS = 5

_FS_TRANSIENT_NAMES = {
    "ServiceUnavailable", "DeadlineExceeded", "InternalServerError", "Aborted",
    "TooManyRequests", "RetryError", "GatewayTimeout", "BadGateway", "Cancelled",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _token() -> str:
    return uuid.uuid4().hex


def _lease_expired(doc: dict, now_iso: Optional[str] = None) -> bool:
    exp = doc.get("lock_expires_at")
    if not exp:
        return False
    return exp < (now_iso or _now())


def is_firestore_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    if name in _FS_TRANSIENT_NAMES:
        return True
    if module.startswith("google.") and name in _FS_TRANSIENT_NAMES:
        return True
    if any(tag in name for tag in ("Timeout", "Unavailable", "Connection", "Transport")):
        return True
    return False


def translate_fs_errors(fn):
    """Wrap google/Firestore transient errors into FirestoreTransientError."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (FirestoreTransientError, InvalidTransition, NotFound):
            raise
        except Exception as exc:  # noqa: BLE001
            if is_firestore_transient(exc):
                raise FirestoreTransientError(f"firestore transient: {type(exc).__name__}") from exc
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Shared decision helpers
# --------------------------------------------------------------------------- #
def _initial_doc(review: Review, lease_seconds: int) -> dict:
    now = _now()
    return {
        "channel": review.platform.lower() if review.platform else "wb",
        "entity_type": "review", "source_id": review.review_id,
        "status": Status.PROCESSING.value, "source_created_at": review.created_date,
        "first_seen_at": now, "updated_at": now, "published_at": None,
        "processing_started_at": now, "publishing_started_at": None,
        "lock_expires_at": _expiry(lease_seconds), "lock_token": None,
        "rating": review.rating, "text": review.text, "pros": review.pros, "cons": review.cons,
        "bables": list(review.bables or []),
        "buyer_name": review.user_name, "product_name": review.product_name,
        "supplier_article": review.supplier_article, "nm_id": review.nm_id,
        "imt_id": review.imt_id, "brand_name": review.brand_name,
        "ai_answer": "", "final_answer": "", "answer_versions": [], "generation_number": 0,
        "openai_model": "", "prompt_version": "", "openai_usage": {}, "openai_latency_ms": 0,
        "telegram_chat_id": "", "telegram_message_id": "", "publish_attempts": 0,
        "last_error_code": None, "last_error_message": None, "created_by": "poll",
        "published_by_telegram_user_id": None,
    }


def _initial_question_doc(question, lease_seconds: int) -> dict:
    """Initial Firestore doc for a WB buyer question (entity_type=question).

    Same shape as a review doc so all downstream state-machine / generation /
    publish code is reused unchanged; review-only fields (rating/pros/cons) are
    empty, and the question text lives in ``text``.
    """
    now = _now()
    return {
        "channel": (question.platform or "wb").lower(),
        "entity_type": "question", "source_id": question.question_id,
        "status": Status.PROCESSING.value, "source_created_at": question.created_date,
        "first_seen_at": now, "updated_at": now, "published_at": None,
        "processing_started_at": now, "publishing_started_at": None,
        "lock_expires_at": _expiry(lease_seconds), "lock_token": None,
        "rating": None, "text": question.text, "pros": "", "cons": "",
        "buyer_name": "", "product_name": question.product_name,
        "supplier_article": question.supplier_article, "nm_id": question.nm_id,
        "imt_id": question.imt_id, "brand_name": question.brand_name,
        "wb_state": question.state,
        "ai_answer": "", "final_answer": "", "answer_versions": [], "generation_number": 0,
        "openai_model": "", "prompt_version": "", "openai_usage": {}, "openai_latency_ms": 0,
        "telegram_chat_id": "", "telegram_message_id": "", "publish_attempts": 0,
        "last_error_code": None, "last_error_message": None, "created_by": "poll",
        "published_by_telegram_user_id": None,
    }


def _decide_claim(existing: Optional[dict]) -> str:
    if existing is None:
        return "create"
    status = existing.get("status")
    if status in RETRIABLE:
        return "retry"
    if status == Status.PROCESSING.value and _lease_expired(existing):
        return "retry"
    return "skip"


def _check_publishable(existing: Optional[dict], expected_generation=None) -> tuple[dict, bool]:
    """Return (doc, recovered_from_publishing)."""
    if existing is None:
        raise NotFound("record not found")
    if expected_generation is not None and existing.get("generation_number", 0) != expected_generation:
        raise InvalidTransition("publication generation changed; operator must read the new card")
    status = existing.get("status")
    if status in PUBLISHABLE_FROM:
        return existing, False
    if status == Status.PUBLISHING.value and _lease_expired(existing):
        return existing, True  # crashed publish — recoverable but must be verified
    raise InvalidTransition(f"cannot publish from status={status}")


def _request_override(doc, pending, generation):
    from app.services.owner_override import validate_binding
    if doc is None: raise NotFound('record not found')
    if doc.get('status') not in DRAFTABLE_FROM: raise InvalidTransition('override requires draft state')
    if doc.get('generation_number',0)!=generation: raise InvalidTransition('override generation changed')
    validate_binding(doc,pending)
    return {'owner_override_pending': copy.deepcopy(pending), 'updated_at': _now()}


def _consume_override(doc, confirmation):
    from app.services.owner_override import validate_binding, source_text
    if confirmation is None: return {}
    pending=doc.get('owner_override_pending') or {}
    if not pending or pending.get('consumed_at') or pending!=confirmation:
        raise InvalidTransition('override confirmation superseded or consumed')
    validate_binding(doc,pending)
    text=source_text(doc,pending['source_version'])
    now=_now(); version=doc.get('generation_number',0)+1
    audit={k:copy.deepcopy(v) for k,v in pending.items() if k not in {'nonce','expires_at','current_hash','current_generation'}}
    audit.update(override=True, override_confirmed_at=now)
    consumed={**pending,'consumed_at':now}
    return dict(owner_override_pending=consumed, owner_override_audit=(doc.get('owner_override_audit') or [])+[audit],
        final_answer=text,generation_number=version,response_review_required=False,
        answer_versions=(doc.get('answer_versions') or [])+[dict(text=text,source='owner_override',
            generation_number=version,created_at=now,override=True,source_version=pending['source_version'])])


def _draftable(existing: Optional[dict], lock_status: str) -> dict:
    """Allow starting edit/regenerate from a draftable status or an expired own lock."""
    if existing is None:
        raise NotFound("record not found")
    status = existing.get("status")
    if status in DRAFTABLE_FROM:
        return existing
    if status == lock_status and _lease_expired(existing):
        return existing  # recover a crashed edit/regenerate
    raise InvalidTransition(f"cannot start {lock_status} from status={status}")


def _apply_generation(doc: dict, gen, source: str) -> None:
    gen_no = doc.get("generation_number", 0) + 1
    version = {
        "text": gen.text, "source": source, "generation_number": gen_no,
        "openai_model": gen.model, "prompt_version": gen.prompt_version, "created_at": _now(),
    }
    doc["generation_number"] = gen_no
    doc["answer_versions"] = (doc.get("answer_versions") or []) + [version]
    doc["final_answer"] = gen.text
    if source == "ai" and not doc.get("ai_answer"):
        doc["ai_answer"] = gen.text
    doc.update({
        "openai_model": gen.model, "prompt_version": gen.prompt_version,
        "openai_usage": gen.usage, "openai_latency_ms": gen.latency_ms, "updated_at": _now(),
    })


def _check_v31_adoptable(doc, expected_generation, gen) -> None:
    """The operator's «Опубликовать 3.1E» tap binds to the card it saw: same generation,
    a draftable status (never publishing/publish_unknown: an earlier write may exist),
    and exactly the 3.1E text stored for this communication."""
    draft = doc.get("v31_draft") or {}
    if (doc.get("status") not in DRAFTABLE_FROM
            or doc.get("generation_number", 0) != expected_generation
            or not draft.get("text") or draft.get("text") != gen.text):
        raise InvalidTransition("v31 draft not adoptable from this card state")


def _check_v2_adoptable(doc, expected_generation, gen) -> None:
    """«Использовать вариант V2» (R2.1 fallback): same card generation, a draftable status and
    exactly the stored V2 draft."""
    if (doc.get("status") not in DRAFTABLE_FROM
            or doc.get("generation_number", 0) != expected_generation
            or not doc.get("ai_answer") or doc.get("ai_answer") != gen.text):
        raise InvalidTransition("v2 draft not adoptable from this card state")


def _draft_commit_fields(doc):
    return {k: doc[k] for k in ("generation_number", "answer_versions", "final_answer", "ai_answer",
        "openai_model", "prompt_version", "openai_usage", "openai_latency_ms", "updated_at",
        "status", "lock_token", "lock_expires_at", "response_recovery", "response_review_required") if k in doc}


def _apply_response_recovery(doc, prepared_gen, recovery):
    if recovery is None:
        return
    if prepared_gen is not None:
        _apply_generation(doc, prepared_gen, "policy_repair")
    doc["response_recovery"] = recovery
    doc["response_review_required"] = recovery["status"] != "READY"
    if doc["response_review_required"]:
        doc["status"] = Status.POLICY_BLOCKED.value


def _feedback_uncertainty_patch(doc):
    # Preserve legacy/crash uncertainty before policy can change the status.
    # No proof that a previous write did not leave the process => never resend.
    if doc.get("entity_type") == "review" and doc.get("status") in (
            Status.PUBLISH_UNKNOWN.value, Status.PUBLISHING.value):
        return {"feedback_write_intent": True}
    return {}


def _publication_patch(doc, token, fields, trace=None, release=False, claim=False):
    if doc is None:
        raise NotFound("record not found")
    if claim:
        if doc.get("entity_type") != "review" or doc.get("status") not in (
                Status.PUBLISH_ACCEPTED.value, Status.PUBLISH_UNKNOWN.value, Status.PUBLISHED.value, Status.PUBLISHING.value):
            raise InvalidTransition("not a feedback verification candidate")
        if doc.get("status") == Status.PUBLISHING.value and not _lease_expired(doc):
            raise InvalidTransition("not a feedback verification candidate")
    elif doc.get("status") != Status.PUBLISHING.value or doc.get("lock_token") != token or _lease_expired(doc):
        raise InvalidTransition("publication lease superseded or expired")
    patch = {**fields, "updated_at": _now()}
    if release:
        patch.update(lock_token=None, lock_expires_at=None)
    if trace is not None:
        patch["publish_trace"] = (doc.get("publish_trace") or []) + [copy.deepcopy(trace)]
    return patch


# --------------------------------------------------------------------------- #
# In-memory repository
# --------------------------------------------------------------------------- #
class MemoryRepository:
    def __init__(self, lease_seconds: int = 300, update_lease_seconds: int = 120,
                 edit_lease_seconds: int = 900, outbox_lease_seconds: int = 60):
        self.docs: dict[str, dict] = {}
        self.editing: dict[str, dict] = {}
        self.updates: dict[str, dict] = {}
        self.outbox: dict[str, dict] = {}
        self._lease = lease_seconds
        self._ulease = update_lease_seconds
        self._elease = edit_lease_seconds
        self._olease = outbox_lease_seconds

    # --- claim / poll ---
    def claim_review(self, review: Review):
        doc_id = make_doc_id("wb", "review", review.review_id)
        existing = self.docs.get(doc_id)
        decision = _decide_claim(existing)
        if decision == "skip":
            return False, doc_id, copy.deepcopy(existing)
        if decision == "create":
            doc = _initial_doc(review, self._lease)
            self.docs[doc_id] = doc
            return True, doc_id, copy.deepcopy(doc)
        existing.update({"status": Status.PROCESSING.value, "processing_started_at": _now(),
                         "lock_expires_at": _expiry(self._lease), "lock_token": None, "updated_at": _now()})
        return True, doc_id, copy.deepcopy(existing)

    def claim_question(self, question):
        doc_id = make_doc_id("wb", "question", question.question_id)
        existing = self.docs.get(doc_id)
        decision = _decide_claim(existing)
        if decision == "skip":
            return False, doc_id, copy.deepcopy(existing)
        if decision == "create":
            doc = _initial_question_doc(question, self._lease)
            self.docs[doc_id] = doc
            return True, doc_id, copy.deepcopy(doc)
        existing.update({"status": Status.PROCESSING.value, "processing_started_at": _now(),
                         "lock_expires_at": _expiry(self._lease), "lock_token": None, "updated_at": _now()})
        return True, doc_id, copy.deepcopy(existing)

    def get(self, doc_id: str) -> Optional[dict]:
        doc = self.docs.get(doc_id)
        return copy.deepcopy(doc) if doc else None

    def update(self, doc_id: str, fields: dict) -> None:
        if doc_id not in self.docs:
            raise NotFound(doc_id)
        self.docs[doc_id].update(fields)
        self.docs[doc_id]["updated_at"] = _now()

    def save_generation(self, doc_id: str, gen, *, source: str, set_pending: bool = False) -> dict:
        doc = self.docs[doc_id]
        _apply_generation(doc, gen, source)
        if set_pending:
            doc["status"] = Status.PENDING_APPROVAL.value
            doc["lock_expires_at"] = None
            doc["lock_token"] = None
        return copy.deepcopy(doc)

    def adopt_v31_draft(self, doc_id: str, expected_generation: int, gen) -> dict:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        _check_v31_adoptable(doc, expected_generation, gen)
        _apply_generation(doc, gen, "v31e")
        return copy.deepcopy(doc)

    def adopt_v2_fallback(self, doc_id: str, expected_generation: int, gen) -> dict:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        _check_v2_adoptable(doc, expected_generation, gen)
        _apply_generation(doc, gen, "v2_fallback")
        return copy.deepcopy(doc)

    # --- non-publish action state machine (skip/restore/show) ---
    def begin_action(self, doc_id: str, action: str) -> dict:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        if not action_allowed(doc.get("status"), action):
            raise InvalidTransition(f"action {action} not allowed from {doc.get('status')}")
        snapshot = copy.deepcopy(doc)
        if action == "skip":
            doc.update({"status": Status.SKIPPED.value, "lock_expires_at": None, "updated_at": _now()})
        elif action == "restore":
            doc.update({"status": Status.PENDING_APPROVAL.value, "updated_at": _now()})
        return snapshot

    # --- regenerate (locked) ---
    def begin_regenerate(self, doc_id: str) -> tuple[dict, str]:
        doc = _draftable(self.docs.get(doc_id), Status.REGENERATING.value)
        tok = _token()
        doc.update({"status": Status.REGENERATING.value, "lock_token": tok,
                    "lock_expires_at": _expiry(self._lease), "updated_at": _now()})
        return copy.deepcopy(doc), tok

    def commit_regenerate(self, doc_id: str, gen, lock_token: str, *, prepared_gen=None, recovery=None,
                          source: str = "regenerated") -> dict:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        if doc.get("status") != Status.REGENERATING.value or doc.get("lock_token") != lock_token:
            raise InvalidTransition("regenerate lock lost or superseded")
        _apply_generation(doc, gen, source)
        doc.update({"status": Status.PENDING_APPROVAL.value, "lock_token": None, "lock_expires_at": None})
        _apply_response_recovery(doc, prepared_gen, recovery)
        return copy.deepcopy(doc)

    # --- edit (locked) ---
    def begin_edit(self, doc_id: str) -> tuple[dict, str, int]:
        doc = _draftable(self.docs.get(doc_id), Status.EDITING.value)
        tok = _token()
        doc.update({"status": Status.EDITING.value, "lock_token": tok,
                    "lock_expires_at": _expiry(self._elease), "updated_at": _now()})
        return copy.deepcopy(doc), tok, doc.get("generation_number", 0)

    def commit_manual_answer(self, doc_id: str, text: str, lock_token: str,
                             expected_generation: int, *, prepared_gen=None, recovery=None) -> dict:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        if (doc.get("status") != Status.EDITING.value or doc.get("lock_token") != lock_token
                or doc.get("generation_number", 0) != expected_generation):
            raise InvalidTransition("edit lock lost or document changed")
        gen_no = doc.get("generation_number", 0) + 1
        version = {"text": text, "source": "manual", "generation_number": gen_no, "created_at": _now()}
        doc["generation_number"] = gen_no
        doc["answer_versions"] = (doc.get("answer_versions") or []) + [version]
        doc["final_answer"] = text  # ai_answer preserved for audit
        doc.update({"status": Status.PENDING_APPROVAL.value, "lock_token": None,
                    "lock_expires_at": None, "updated_at": _now()})
        _apply_response_recovery(doc, prepared_gen, recovery)
        return copy.deepcopy(doc)

    def cancel_draft(self, doc_id: str, lock_token: Optional[str] = None) -> None:
        doc = self.docs.get(doc_id)
        if not doc:
            return
        if doc.get("status") not in (Status.EDITING.value, Status.REGENERATING.value):
            return
        if lock_token is not None and doc.get("lock_token") != lock_token and not _lease_expired(doc):
            return
        doc.update({"status": Status.PENDING_APPROVAL.value, "lock_token": None,
                    "lock_expires_at": None, "updated_at": _now()})

    def request_override(self, doc_id, pending, expected_generation):
        doc=self.docs.get(doc_id)
        patch=_request_override(doc,pending,expected_generation)
        doc.update(patch)
        return copy.deepcopy(doc)

    # --- publish ---
    def begin_publish(self, doc_id: str, expected_generation=None, *, override_confirmation=None) -> dict:
        doc, recovered = _check_publishable(self.docs.get(doc_id), expected_generation)
        patch = _consume_override(doc, override_confirmation)
        doc.update(patch)
        doc.update(_feedback_uncertainty_patch(doc))
        doc.update({"status": Status.PUBLISHING.value, "lock_token": _token(), "publishing_started_at": _now(),
                    "lock_expires_at": _expiry(self._lease),
                    "publish_attempts": doc.get("publish_attempts", 0) + 1, "updated_at": _now()})
        out = copy.deepcopy(doc)
        out["recovered_from_publishing"] = recovered
        return out

    def mark_published(self, doc_id: str, wb_response: dict, user_id) -> None:
        self.docs[doc_id].update({"status": Status.PUBLISHED.value, "published_at": _now(),
                                  "published_by_telegram_user_id": str(user_id),
                                  "wb_response": wb_response, "lock_expires_at": None, "updated_at": _now()})

    def mark_publish_failed(self, doc_id: str, code, message: str) -> None:
        self.docs[doc_id].update({"status": Status.PUBLISH_FAILED.value, "last_error_code": code,
                                  "last_error_message": (message or "")[:500],
                                  "lock_expires_at": None, "updated_at": _now()})

    def mark_error(self, doc_id: str, message: str) -> None:
        doc = self.docs.get(doc_id)
        if doc:
            doc.update({"status": Status.ERROR.value, "last_error_message": (message or "")[:500],
                        "lock_expires_at": None, "updated_at": _now()})

    def record_publication(self, doc_id: str, status: str, fields: dict, trace: dict) -> None:
        """Set the publication outcome and append one attempt/verification trace."""
        doc = self.docs[doc_id]
        doc.update({**fields, "status": status, "lock_expires_at": None, "updated_at": _now()})
        doc["publish_trace"] = (doc.get("publish_trace") or []) + [copy.deepcopy(trace)]

    def list_by_status(self, status: str, limit: int = 50,
                       entity_type: Optional[str] = None) -> list[tuple[str, dict]]:
        out = [(k, copy.deepcopy(v)) for k, v in self.docs.items()
               if v.get("status") == status
               and (entity_type is None or v.get("entity_type") == entity_type)]
        return out[:limit]

    def _publication_mutate(self, doc_id, token, fields, trace=None, release=False, claim=False):
        doc = self.docs.get(doc_id)
        old = copy.deepcopy(doc)
        patch = _publication_patch(doc, token, fields, trace, release, claim)
        doc.update(patch)
        if claim:
            old["lock_token"] = patch["lock_token"]
            return old
        return copy.deepcopy(doc)

    def claim_feedback_verification(self, doc_id: str):
        return self._publication_mutate(doc_id, None, {"status": Status.PUBLISHING.value,
            "lock_token": _token(), "lock_expires_at": _expiry(self._lease)}, claim=True)

    def publication_update(self, doc_id, token, fields, trace=None, release=False):
        return self._publication_mutate(doc_id, token, fields, trace=trace, release=release)

    # --- editing sessions ---
    def set_editing_session(self, user_id, doc_id, expires_at, prompt_message_id=None,
                            lock_token=None, expected_generation=None) -> None:
        self.editing[str(user_id)] = {"doc_id": doc_id, "expires_at": expires_at,
                                      "prompt_message_id": prompt_message_id,
                                      "lock_token": lock_token, "expected_generation": expected_generation}

    def get_editing_session(self, user_id) -> Optional[dict]:
        return self.editing.get(str(user_id))

    def clear_editing_session(self, user_id) -> None:
        self.editing.pop(str(user_id), None)

    # --- BigQuery outbox (pending -> sending -> delivered, leased) ---
    def enqueue_event(self, event: dict) -> bool:
        eid = event["event_id"]
        if eid in self.outbox:
            return False
        self.outbox[eid] = {"event_id": eid, "payload": event, "status": "pending",
                            "attempts": 0, "created_at": _now(), "next_retry_at": _now(),
                            "lock_expires_at": None}
        return True

    def list_pending_events(self, limit: int = 100) -> list[dict]:
        # include leased 'sending' rows so an expired lease can be reclaimed;
        # claim_outbox_event gates whether a row is actually taken.
        return [copy.deepcopy(r) for r in self.outbox.values()
                if r["status"] in ("pending", "sending")][:limit]

    def claim_outbox_event(self, event_id: str) -> bool:
        rec = self.outbox.get(event_id)
        if not rec or rec["status"] == "delivered":
            return False
        if rec["status"] == "sending" and not _lease_expired(rec):
            return False  # another flush holds the lease
        rec.update({"status": "sending", "lock_expires_at": _expiry(self._olease)})
        return True

    def mark_event_delivered(self, event_id: str) -> None:
        if event_id in self.outbox:
            self.outbox[event_id].update({"status": "delivered", "delivered_at": _now(),
                                          "lock_expires_at": None})

    def mark_event_failed(self, event_id: str) -> None:
        rec = self.outbox.get(event_id)
        if rec:
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["status"] = "pending"
            rec["lock_expires_at"] = None
            rec["next_retry_at"] = _now()

    # --- telegram update dedup with attempts + poison guard ---
    def begin_update(self, update_id) -> str:
        key = str(update_id)
        rec = self.updates.get(key)
        if rec:
            if rec.get("status") == "processed":
                return "duplicate"
            if rec.get("attempts", 0) >= UPDATE_MAX_ATTEMPTS:
                return "giveup"
            if rec.get("status") == "processing" and not _lease_expired(rec):
                return "duplicate"
            rec.update({"status": "processing", "lock_expires_at": _expiry(self._ulease)})
            return "new"
        self.updates[key] = {"status": "processing", "attempts": 0,
                             "lock_expires_at": _expiry(self._ulease), "received_at": _now()}
        return "new"

    def complete_update(self, update_id) -> None:
        key = str(update_id)
        rec = self.updates.get(key, {})
        rec.update({"status": "processed", "processed_at": _now()})
        self.updates[key] = rec

    def release_update(self, update_id) -> None:
        key = str(update_id)
        rec = self.updates.get(key)
        if rec:
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["status"] = "failed_retryable"
            rec["lock_expires_at"] = None


# --------------------------------------------------------------------------- #
# Firestore repository
# --------------------------------------------------------------------------- #
class FirestoreRepository:
    def __init__(self, settings, client=None):
        self._s = settings
        self._client = client
        self._col_name = settings.firestore_collection
        self._lease = settings.lock_lease_seconds
        self._ulease = settings.update_lease_seconds
        self._elease = settings.telegram_edit_timeout_seconds
        self._olease = settings.outbox_lease_seconds

    def _lazy(self):
        if self._client is None:
            from google.cloud import firestore

            db = self._s.firestore_database
            self._client = (
                firestore.Client(project=self._s.gcp_project_id, database=db)
                if db and db != "(default)"
                else firestore.Client(project=self._s.gcp_project_id)
            )
        return self._client

    def _doc(self, doc_id: str):
        return self._lazy().collection(self._col_name).document(doc_id)

    @translate_fs_errors
    def claim_review(self, review: Review):
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(make_doc_id("wb", "review", review.review_id))

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            existing = snap.to_dict() if snap.exists else None
            decision = _decide_claim(existing)
            if decision == "skip":
                return False, existing
            if decision == "create":
                doc = _initial_doc(review, self._lease)
                transaction.set(ref, doc)
                return True, doc
            reclaim = {"status": Status.PROCESSING.value, "processing_started_at": _now(),
                       "lock_expires_at": _expiry(self._lease), "lock_token": None, "updated_at": _now()}
            transaction.update(ref, reclaim)
            existing.update(reclaim)
            return True, existing

        should, doc = txn(client.transaction())
        return should, ref.id, doc

    @translate_fs_errors
    def claim_question(self, question):
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(make_doc_id("wb", "question", question.question_id))

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            existing = snap.to_dict() if snap.exists else None
            decision = _decide_claim(existing)
            if decision == "skip":
                return False, existing
            if decision == "create":
                doc = _initial_question_doc(question, self._lease)
                transaction.set(ref, doc)
                return True, doc
            reclaim = {"status": Status.PROCESSING.value, "processing_started_at": _now(),
                       "lock_expires_at": _expiry(self._lease), "lock_token": None, "updated_at": _now()}
            transaction.update(ref, reclaim)
            existing.update(reclaim)
            return True, existing

        should, doc = txn(client.transaction())
        return should, ref.id, doc

    @translate_fs_errors
    def get(self, doc_id: str) -> Optional[dict]:
        snap = self._doc(doc_id).get()
        return snap.to_dict() if snap.exists else None

    @translate_fs_errors
    def update(self, doc_id: str, fields: dict) -> None:
        self._doc(doc_id).update({**fields, "updated_at": _now()})

    @translate_fs_errors
    def save_generation(self, doc_id: str, gen, *, source: str, set_pending: bool = False) -> dict:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            _apply_generation(doc, gen, source)
            updates = {k: doc[k] for k in ("generation_number", "answer_versions", "final_answer",
                                           "ai_answer", "openai_model", "prompt_version",
                                           "openai_usage", "openai_latency_ms", "updated_at")
                       if k in doc}
            if set_pending:
                updates["status"] = Status.PENDING_APPROVAL.value
                updates["lock_expires_at"] = None
                updates["lock_token"] = None
            transaction.update(ref, updates)
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def adopt_v31_draft(self, doc_id: str, expected_generation: int, gen) -> dict:
        """Same check and version append as the memory repo, in one transaction."""
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            _check_v31_adoptable(doc, expected_generation, gen)
            _apply_generation(doc, gen, "v31e")
            transaction.update(ref, {k: doc[k] for k in ("generation_number", "answer_versions", "final_answer",
                                                         "openai_model", "prompt_version", "openai_usage",
                                                         "openai_latency_ms", "updated_at") if k in doc})
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def adopt_v2_fallback(self, doc_id: str, expected_generation: int, gen) -> dict:
        """Same check and version append as the memory repo, in one transaction."""
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            _check_v2_adoptable(doc, expected_generation, gen)
            _apply_generation(doc, gen, "v2_fallback")
            transaction.update(ref, {k: doc[k] for k in ("generation_number", "answer_versions", "final_answer",
                                                         "openai_model", "prompt_version", "openai_usage",
                                                         "openai_latency_ms", "updated_at") if k in doc})
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def begin_action(self, doc_id: str, action: str) -> dict:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            if not action_allowed(doc.get("status"), action):
                raise InvalidTransition(f"action {action} not allowed from {doc.get('status')}")
            if action == "skip":
                transaction.update(ref, {"status": Status.SKIPPED.value, "lock_expires_at": None,
                                         "updated_at": _now()})
            elif action == "restore":
                transaction.update(ref, {"status": Status.PENDING_APPROVAL.value, "updated_at": _now()})
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def begin_regenerate(self, doc_id: str) -> tuple[dict, str]:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)
        tok = _token()

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            doc = _draftable(snap.to_dict() if snap.exists else None, Status.REGENERATING.value)
            transaction.update(ref, {"status": Status.REGENERATING.value, "lock_token": tok,
                                     "lock_expires_at": _expiry(self._lease), "updated_at": _now()})
            return doc

        return txn(client.transaction()), tok

    @translate_fs_errors
    def commit_regenerate(self, doc_id: str, gen, lock_token: str, *, prepared_gen=None, recovery=None,
                          source: str = "regenerated") -> dict:
        from google.cloud import firestore
        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            if doc.get("status") != Status.REGENERATING.value or doc.get("lock_token") != lock_token:
                raise InvalidTransition("regenerate lock lost or superseded")
            _apply_generation(doc, gen, source)
            doc.update(status=Status.PENDING_APPROVAL.value, lock_token=None, lock_expires_at=None)
            _apply_response_recovery(doc, prepared_gen, recovery)
            transaction.update(ref, _draft_commit_fields(doc))
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def begin_edit(self, doc_id: str) -> tuple[dict, str, int]:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)
        tok = _token()

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            doc = _draftable(snap.to_dict() if snap.exists else None, Status.EDITING.value)
            transaction.update(ref, {"status": Status.EDITING.value, "lock_token": tok,
                                     "lock_expires_at": _expiry(self._elease), "updated_at": _now()})
            return doc, doc.get("generation_number", 0)

        doc, gen_no = txn(client.transaction())
        return doc, tok, gen_no

    @translate_fs_errors
    def commit_manual_answer(self, doc_id: str, text: str, lock_token: str,
                             expected_generation: int, *, prepared_gen=None, recovery=None) -> dict:
        from google.cloud import firestore
        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            if (doc.get("status") != Status.EDITING.value or doc.get("lock_token") != lock_token
                    or doc.get("generation_number", 0) != expected_generation):
                raise InvalidTransition("edit lock lost or document changed")
            gen_no = doc.get("generation_number", 0) + 1
            doc["answer_versions"] = (doc.get("answer_versions") or []) + [{"text": text,
                "source": "manual", "generation_number": gen_no, "created_at": _now()}]
            doc.update(generation_number=gen_no, final_answer=text, status=Status.PENDING_APPROVAL.value,
                       lock_token=None, lock_expires_at=None, updated_at=_now())
            _apply_response_recovery(doc, prepared_gen, recovery)
            transaction.update(ref, _draft_commit_fields(doc))
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def cancel_draft(self, doc_id: str, lock_token: Optional[str] = None) -> None:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return
            doc = snap.to_dict()
            if doc.get("status") not in (Status.EDITING.value, Status.REGENERATING.value):
                return
            if lock_token is not None and doc.get("lock_token") != lock_token and not _lease_expired(doc):
                return
            transaction.update(ref, {"status": Status.PENDING_APPROVAL.value, "lock_token": None,
                                     "lock_expires_at": None, "updated_at": _now()})

        txn(client.transaction())

    @translate_fs_errors
    def request_override(self, doc_id, pending, expected_generation):
        from google.cloud import firestore
        client=self._lazy();ref=self._doc(doc_id)
        @firestore.transactional
        def txn(transaction):
            snap=ref.get(transaction=transaction)
            doc=snap.to_dict() if snap.exists else None
            patch=_request_override(doc,pending,expected_generation)
            transaction.update(ref,patch)
            doc.update(patch)
            return doc
        return txn(client.transaction())

    @translate_fs_errors
    def begin_publish(self, doc_id: str, expected_generation=None, *, override_confirmation=None) -> dict:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._doc(doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            doc, recovered = _check_publishable(snap.to_dict() if snap.exists else None, expected_generation)
            guard = {**_feedback_uncertainty_patch(doc), **_consume_override(doc, override_confirmation)}
            doc.update(guard)
            doc["lock_token"] = _token()
            transaction.update(ref, {**guard, "status": Status.PUBLISHING.value, "lock_token": doc["lock_token"], "publishing_started_at": _now(),
                                     "lock_expires_at": _expiry(self._lease),
                                     "publish_attempts": doc.get("publish_attempts", 0) + 1,
                                     "updated_at": _now()})
            doc["recovered_from_publishing"] = recovered
            return doc

        return txn(client.transaction())

    @translate_fs_errors
    def mark_published(self, doc_id: str, wb_response: dict, user_id) -> None:
        self._doc(doc_id).update({"status": Status.PUBLISHED.value, "published_at": _now(),
                                  "published_by_telegram_user_id": str(user_id), "wb_response": wb_response,
                                  "lock_expires_at": None, "updated_at": _now()})

    @translate_fs_errors
    def mark_publish_failed(self, doc_id: str, code, message: str) -> None:
        self._doc(doc_id).update({"status": Status.PUBLISH_FAILED.value, "last_error_code": code,
                                  "last_error_message": (message or "")[:500], "lock_expires_at": None,
                                  "updated_at": _now()})

    def mark_error(self, doc_id: str, message: str) -> None:
        try:
            self._doc(doc_id).update({"status": Status.ERROR.value,
                                      "last_error_message": (message or "")[:500],
                                      "lock_expires_at": None, "updated_at": _now()})
        except Exception:  # noqa: BLE001
            pass

    @translate_fs_errors
    def record_publication(self, doc_id: str, status: str, fields: dict, trace: dict) -> None:
        """Set the publication outcome and append one attempt/verification trace."""
        from google.cloud import firestore

        self._doc(doc_id).update({**fields, "status": status, "lock_expires_at": None,
                                  "updated_at": _now(),
                                  "publish_trace": firestore.ArrayUnion([trace])})

    @translate_fs_errors
    def list_by_status(self, status: str, limit: int = 50,
                       entity_type: Optional[str] = None) -> list[tuple[str, dict]]:
        # Equality-only filters: served by Firestore single-field indexes (no
        # composite index needed).
        query = self._lazy().collection(self._col_name).where("status", "==", status)
        if entity_type is not None:
            query = query.where("entity_type", "==", entity_type)
        return [(snap.id, snap.to_dict()) for snap in query.limit(limit).stream()]

    @translate_fs_errors
    def _publication_mutate(self, doc_id, token, fields, trace=None, release=False, claim=False):
        from google.cloud import firestore
        client, ref = self._lazy(), self._doc(doc_id)
        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            doc = snap.to_dict() if snap.exists else None
            patch = _publication_patch(doc, token, fields, trace, release, claim)
            transaction.update(ref, patch)
            if claim:
                return {**doc, "lock_token": patch["lock_token"]}
            return {**doc, **patch}
        return txn(client.transaction())

    def claim_feedback_verification(self, doc_id: str):
        return self._publication_mutate(doc_id, None, {"status": Status.PUBLISHING.value,
            "lock_token": _token(), "lock_expires_at": _expiry(self._lease)}, claim=True)

    def publication_update(self, doc_id, token, fields, trace=None, release=False):
        return self._publication_mutate(doc_id, token, fields, trace=trace, release=release)

    # --- editing sessions ---
    def _editing_ref(self, user_id):
        return self._lazy().collection("editing_sessions").document(str(user_id))

    @translate_fs_errors
    def set_editing_session(self, user_id, doc_id, expires_at, prompt_message_id=None,
                            lock_token=None, expected_generation=None) -> None:
        self._editing_ref(user_id).set({"doc_id": doc_id, "expires_at": expires_at,
                                        "prompt_message_id": prompt_message_id, "lock_token": lock_token,
                                        "expected_generation": expected_generation})

    @translate_fs_errors
    def get_editing_session(self, user_id) -> Optional[dict]:
        snap = self._editing_ref(user_id).get()
        return snap.to_dict() if snap.exists else None

    def clear_editing_session(self, user_id) -> None:
        try:
            self._editing_ref(user_id).delete()
        except Exception:  # noqa: BLE001
            pass

    # --- BigQuery outbox ---
    def _outbox_ref(self, event_id):
        return self._lazy().collection("bq_outbox").document(event_id)

    @translate_fs_errors
    def _enqueue_txn(self, event: dict) -> bool:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._outbox_ref(event["event_id"])

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if snap.exists:
                return False
            transaction.set(ref, {"event_id": event["event_id"], "payload": event, "status": "pending",
                                  "attempts": 0, "created_at": _now(), "next_retry_at": _now(),
                                  "lock_expires_at": None})
            return True

        return txn(client.transaction())

    def enqueue_event(self, event: dict) -> bool:
        # Transient Firestore errors are RAISED (FirestoreTransientError) so a
        # webhook can 503 and not silently drop the event; permanent errors are
        # swallowed (best-effort) to avoid looping on a poison write.
        try:
            return self._enqueue_txn(event)
        except FirestoreTransientError:
            raise
        except Exception:  # noqa: BLE001
            return False

    def list_pending_events(self, limit: int = 100) -> list[dict]:
        try:
            col = self._lazy().collection("bq_outbox")
            docs = col.where("status", "in", ["pending", "sending"]).limit(limit).stream()
            return [d.to_dict() for d in docs]
        except Exception:  # noqa: BLE001
            return []

    def claim_outbox_event(self, event_id: str) -> bool:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._outbox_ref(event_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return False
            rec = snap.to_dict()
            if rec.get("status") == "delivered":
                return False
            if rec.get("status") == "sending" and not _lease_expired(rec):
                return False
            transaction.update(ref, {"status": "sending", "lock_expires_at": _expiry(self._olease)})
            return True

        try:
            return txn(client.transaction())
        except Exception:  # noqa: BLE001
            return False

    def mark_event_delivered(self, event_id: str) -> None:
        try:
            self._outbox_ref(event_id).update({"status": "delivered", "delivered_at": _now(),
                                               "lock_expires_at": None})
        except Exception:  # noqa: BLE001
            pass

    def mark_event_failed(self, event_id: str) -> None:
        try:
            from google.cloud import firestore

            self._outbox_ref(event_id).update({"status": "pending", "next_retry_at": _now(),
                                               "lock_expires_at": None,
                                               "attempts": firestore.Increment(1)})
        except Exception:  # noqa: BLE001
            pass

    # --- telegram update dedup ---
    def _update_ref(self, update_id):
        return self._lazy().collection("telegram_updates").document(str(update_id))

    @translate_fs_errors
    def begin_update(self, update_id) -> str:
        from google.cloud import firestore

        client = self._lazy()
        ref = self._update_ref(update_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if snap.exists:
                rec = snap.to_dict()
                if rec.get("status") == "processed":
                    return "duplicate"
                if rec.get("attempts", 0) >= UPDATE_MAX_ATTEMPTS:
                    return "giveup"
                if rec.get("status") == "processing" and not _lease_expired(rec):
                    return "duplicate"
                transaction.update(ref, {"status": "processing", "lock_expires_at": _expiry(self._ulease)})
                return "new"
            transaction.set(ref, {"status": "processing", "attempts": 0,
                                  "lock_expires_at": _expiry(self._ulease), "received_at": _now()})
            return "new"

        return txn(client.transaction())

    def complete_update(self, update_id) -> None:
        try:
            self._update_ref(update_id).set({"status": "processed", "processed_at": _now()}, merge=True)
        except Exception:  # noqa: BLE001 — must never break the HTTP response
            pass

    def release_update(self, update_id) -> None:
        try:
            from google.cloud import firestore

            self._update_ref(update_id).set(
                {"status": "failed_retryable", "lock_expires_at": None,
                 "attempts": firestore.Increment(1)}, merge=True)
        except Exception:  # noqa: BLE001
            pass
