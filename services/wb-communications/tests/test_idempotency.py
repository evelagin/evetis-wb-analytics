"""Safety-critical behaviours: no resend, no double publish, real editing."""
from __future__ import annotations

from app.domain.models import make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")


def _cb(update_id, action, user_id=302044578, chat_id=302044578, message_id=1001):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq{update_id}",
            "data": f"{action}:{DOC_ID}",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def test_poll_processes_once_then_skips():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    s1 = run_poll(deps)
    assert (s1["fetched"], s1["processed"], s1["skipped"]) == (1, 1, 0)
    assert len(deps.telegram.sent) == 1
    assert deps.repo.get(DOC_ID)["status"] == Status.PENDING_APPROVAL.value

    s2 = run_poll(deps)
    assert (s2["processed"], s2["skipped"]) == (0, 1)
    assert len(deps.telegram.sent) == 1  # not resent


def test_double_publish_is_prevented():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)

    r1 = handle_update(deps, _cb(1, "pub"))
    assert r1["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", "Ответ-вариант-1")]

    r2 = handle_update(deps, _cb(2, "pub"))  # already published -> stale
    assert r2["status"] == "stale"
    assert len(deps.wb.published) == 1


def test_skip_sets_status_and_no_publish():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    r = handle_update(deps, _cb(1, "skip"))
    assert r["status"] == "skipped"
    assert deps.repo.get(DOC_ID)["status"] == Status.SKIPPED.value
    assert deps.wb.published == []


def test_regenerate_increments_generation():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    r = handle_update(deps, _cb(1, "regen"))
    assert r["status"] == "regenerated"
    doc = deps.repo.get(DOC_ID)
    assert doc["generation_number"] == 2
    assert doc["final_answer"] == "Ответ-вариант-2"
    assert doc["ai_answer"] == "Ответ-вариант-1"  # original preserved
    assert doc["status"] == Status.PENDING_APPROVAL.value


def test_edit_requires_reply_and_sets_final_answer():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "edit"))
    session = deps.repo.get_editing_session(302044578)
    assert session["doc_id"] == DOC_ID
    prompt_id = session["prompt_message_id"]

    # a message that is NOT a reply to the prompt is ignored
    non_reply = {"update_id": 2, "message": {"message_id": 5, "chat": {"id": 302044578},
                 "from": {"id": 302044578}, "text": "случайный текст"}}
    assert handle_update(deps, non_reply)["status"] == "ignored"
    assert deps.repo.get(DOC_ID)["final_answer"] == "Ответ-вариант-1"

    # a proper reply to the ForceReply prompt applies the edit
    reply = {"update_id": 3, "message": {"message_id": 6, "chat": {"id": 302044578},
             "from": {"id": 302044578}, "text": "Мой ручной ответ",
             "reply_to_message": {"message_id": prompt_id}}}
    assert handle_update(deps, reply)["status"] == "edited"
    doc = deps.repo.get(DOC_ID)
    assert doc["final_answer"] == "Мой ручной ответ"
    assert doc["ai_answer"] == "Ответ-вариант-1"
    assert deps.repo.get_editing_session(302044578) is None

    handle_update(deps, _cb(4, "pub"))
    assert deps.wb.published == [("REVIEW_1", "Мой ручной ответ")]


def test_edit_rejects_too_long_answer():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "edit"))
    prompt_id = deps.repo.get_editing_session(302044578)["prompt_message_id"]
    long_reply = {"update_id": 2, "message": {"message_id": 7, "chat": {"id": 302044578},
                  "from": {"id": 302044578}, "text": "x" * 1500,
                  "reply_to_message": {"message_id": prompt_id}}}
    r = handle_update(deps, long_reply)
    assert r["status"] == "too_long"
    # not saved, session stays open for a shorter retry
    assert deps.repo.get(DOC_ID)["final_answer"] == "Ответ-вариант-1"
    assert deps.repo.get_editing_session(302044578) is not None


def test_publish_failure_allows_retry():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], fail_publish=True)
    run_poll(deps)
    r = handle_update(deps, _cb(1, "pub"))
    assert r["status"] == "publish_failed"
    assert deps.repo.get(DOC_ID)["status"] == Status.PUBLISH_FAILED.value

    deps.wb.fail_publish = False
    r2 = handle_update(deps, _cb(2, "pub"))  # retry from publish_failed
    assert r2["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", "Ответ-вариант-1")]


def test_foreign_user_is_rejected():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    r = handle_update(deps, _cb(1, "pub", user_id=999999))
    assert r["status"] == "unauthorized"
    assert deps.wb.published == []


def test_cancel_clears_editing_session():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "edit"))
    cancel = {"update_id": 2, "message": {"message_id": 8, "chat": {"id": 302044578},
              "from": {"id": 302044578}, "text": "/cancel"}}
    assert handle_update(deps, cancel)["status"] == "cancelled"
    assert deps.repo.get_editing_session(302044578) is None
