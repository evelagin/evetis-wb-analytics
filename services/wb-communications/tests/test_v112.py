"""v1.1.2: edit-start rollback, event-enqueue transient handling, outbox lease."""
from __future__ import annotations

import pytest

from app.domain.exceptions import FirestoreTransientError, TelegramError
from app.domain.models import make_doc_id
from app.domain.statuses import EventType, Status
from app.services.pipeline import _emit_event, handle_update, run_poll
from app.services.repository import MemoryRepository
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")


def _edit_cb(uid):
    return {"update_id": uid, "callback_query": {
        "id": f"cq{uid}", "data": f"edit:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


# --- C0: EDITING lock is rolled back if starting the edit fails --------------
def test_start_edit_rollback_on_telegram_failure():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)

    def boom(*_a, **_k):
        raise TelegramError("sendMessage failed")

    deps.telegram.send_force_reply = boom
    with pytest.raises(TelegramError):
        handle_update(deps, _edit_cb(1))

    # lock released — document is back to pending_approval, not stuck in EDITING
    assert deps.repo.get(DOC_ID)["status"] == Status.PENDING_APPROVAL.value
    assert deps.repo.get_editing_session(302044578) is None

    # and a later edit tap can start cleanly
    deps.telegram.send_force_reply = lambda chat, text: {"message_id": 2002}
    assert handle_update(deps, _edit_cb(2))["status"] == "editing_started"
    assert deps.repo.get(DOC_ID)["status"] == Status.EDITING.value


# --- C1a: transient event enqueue ------------------------------------------
def test_webhook_event_enqueue_transient_raises():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)

    def boom(_event):
        raise FirestoreTransientError("firestore transient: ServiceUnavailable")

    deps.repo.enqueue_event = boom
    skip_cb = {"update_id": 1, "callback_query": {
        "id": "cq", "data": f"skip:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}
    # webhook outcome events are critical -> transient enqueue propagates (→503)
    with pytest.raises(FirestoreTransientError):
        handle_update(deps, skip_cb)


def test_poll_event_enqueue_transient_is_best_effort():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])

    def boom(_event):
        raise FirestoreTransientError("firestore transient")

    deps.repo.enqueue_event = boom
    summary = run_poll(deps)  # must not raise
    assert summary["processed"] == 1
    assert len(deps.telegram.sent) == 1
    assert deps.repo.get(DOC_ID)["status"] == Status.PENDING_APPROVAL.value


# --- C1b: outbox lease prevents parallel double-send ------------------------
def test_outbox_claim_is_exclusive_until_lease_expires():
    repo = MemoryRepository(outbox_lease_seconds=60)
    repo.enqueue_event({"event_id": "e1", "payload": {}})
    assert repo.claim_outbox_event("e1") is True   # first flush claims it
    assert repo.claim_outbox_event("e1") is False  # second flush is locked out

    repo.outbox["e1"]["lock_expires_at"] = "2000-01-01T00:00:00+00:00"  # lease expired
    assert repo.claim_outbox_event("e1") is True   # reclaimable after expiry

    repo.mark_event_delivered("e1")
    assert repo.claim_outbox_event("e1") is False  # delivered is terminal
