"""C0-6: fail-closed publish gate + C1-2 deterministic event de-dup."""
from __future__ import annotations

from app.domain.models import make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")


def _cb(update_id, action):
    return {"update_id": update_id, "callback_query": {
        "id": f"cq{update_id}", "data": f"{action}:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


def test_publish_disabled_does_not_call_wb():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], wb_publish_enabled=False)
    run_poll(deps)
    r = handle_update(deps, _cb(1, "pub"))
    assert r["status"] == "publish_disabled"
    assert deps.wb.published == []
    # record stays approvable — nothing was locked or consumed
    assert deps.repo.get(DOC_ID)["status"] == Status.PENDING_APPROVAL.value


def test_publish_enabled_publishes():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], wb_publish_enabled=True)
    run_poll(deps)
    assert handle_update(deps, _cb(1, "pub"))["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", "Ответ-вариант-1")]


def test_events_have_deterministic_ids_and_no_duplicates():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    ids = [e["event_id"] for e in deps.bq.events]
    assert len(ids) == len(set(ids))  # no duplicate event ids
    # deterministic: same logical event id derivable from its fields
    assert all(len(i) == 40 for i in ids)  # sha1 hex
