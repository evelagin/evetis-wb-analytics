"""WB buyer questions — separate entity, separate publish gate, no review impact."""
from __future__ import annotations

import json

import httpx
import pytest

import app.utils.retry as retry_mod
from app.domain.exceptions import WBApiError, WBAuthError, WBRateLimitError, WBServerError
from app.domain.models import Question, make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from app.services.wb_client import WBClient
from tests.conftest import SAMPLE_FEEDBACK, make_settings, make_deps


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda *_: None)


def _q(qid="Q1", text="Подойдёт ли крем для сухой кожи?", article="438775437", nm=111):
    return {
        "id": qid, "createdDate": "2026-07-23T10:00:00Z", "text": text, "state": "none",
        "productDetails": {"productName": "Крем для лица", "supplierArticle": article,
                           "nmId": nm, "imtId": 222, "brandName": "EVETIS"},
    }


def _qdoc_id(qid="Q1"):
    return make_doc_id("wb", "question", qid)


def _pub_cb(qid="Q1"):
    doc_id = _qdoc_id(qid)
    return {"update_id": 1, "callback_query": {
        "id": "cq1", "data": f"pub:{doc_id}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


# --- model -------------------------------------------------------------------
def test_question_from_wb_question():
    q = Question.from_wb_question(_q())
    assert q.question_id == "Q1" and q.nm_id == "111"
    assert q.product_name == "Крем для лица" and q.supplier_article == "438775437"
    assert q.rating is None and q.pros == "" and q.user_name == ""  # review-compatible


# --- WB client: pagination + PATCH body --------------------------------------
def _client(handler, **settings):
    s = make_settings(**settings)
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=s.wb_api_base_url)
    return WBClient(s, "TOKEN", client=http)


def test_questions_pagination():
    pages = {0: [_q("A"), _q("B")], 2: [_q("C")]}

    def handler(request):
        assert request.url.path == "/api/v1/questions"
        skip = int(request.url.params.get("skip", 0))
        return httpx.Response(200, json={"data": {"questions": pages.get(skip, [])}})

    wb = _client(handler, wb_poll_batch_size=2)
    ids = [q["id"] for q in wb.iter_unanswered_questions()]
    assert ids == ["A", "B", "C"]


def test_question_publish_patch_body():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    wb = _client(handler)
    wb.publish_question_answer("Q1", "ответ")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/api/v1/questions"
    assert seen["body"] == {"id": "Q1", "text": "ответ", "state": "wbRu"}


# --- pipeline: generation via engine ----------------------------------------
def test_question_drafted_via_engine():
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True)
    summary = run_poll(deps)
    assert summary["questions"]["processed"] == 1
    assert len(deps.telegram.sent) == 1
    card = deps.telegram.sent[0][1]
    assert "новый вопрос" in card and "Вопрос покупателя" in card
    doc = deps.repo.get(_qdoc_id())
    assert doc["entity_type"] == "question"
    assert doc["prompt_version"].startswith("engine_v1:")   # engine, not reviews_v1
    assert doc["status"] == Status.PENDING_APPROVAL.value


def test_question_dedup():
    deps = make_deps(feedbacks=[], questions=[_q(), _q()], wb_questions_enabled=True)
    summary = run_poll(deps)
    # same id twice -> one processed, one skipped
    assert summary["questions"]["processed"] == 1
    assert summary["questions"]["skipped"] == 1


def test_question_first_run_cap():
    qs = [_q(f"Q{i}") for i in range(5)]
    deps = make_deps(feedbacks=[], questions=qs, wb_questions_enabled=True,
                     wb_questions_first_run_max=2)
    summary = run_poll(deps)
    assert summary["questions"]["processed"] == 2
    assert summary["questions"]["capped"] is True
    assert len(deps.telegram.sent) == 2


# --- publish gate + endpoint -------------------------------------------------
def test_question_publish_disabled_by_default():
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True)
    run_poll(deps)
    r = handle_update(deps, _pub_cb())
    assert r["status"] == "publish_disabled"
    assert deps.wb.published_questions == []
    assert "вопрос" in deps.telegram.sent[-1][1].lower()


def test_question_publishes_when_enabled():
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True,
                     wb_question_publish_enabled=True)
    run_poll(deps)
    r = handle_update(deps, _pub_cb())
    assert r["status"] == "published"
    assert len(deps.wb.published_questions) == 1
    assert deps.wb.published_questions[0][0] == "Q1"          # question id
    # the review publish endpoint was NOT used
    assert deps.wb.published == []


def test_review_publish_gate_does_not_open_questions():
    # reviews enabled to publish, questions NOT -> question stays gated
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True,
                     wb_publish_enabled=True, wb_question_publish_enabled=False)
    run_poll(deps)
    assert handle_update(deps, _pub_cb())["status"] == "publish_disabled"


# --- publish error messages (question-flavoured) ----------------------------
@pytest.mark.parametrize("exc, code, needle", [
    (WBAuthError("a", status_code=403), 403, "прав API-токена"),
    (WBApiError("nf", status_code=404), 404, "Вопрос не найден"),
    (WBApiError("u", status_code=422), 422, "Вопрос уже имеет ответ"),
    (WBRateLimitError("r", status_code=429), 429, "лимит запросов"),
    (WBServerError("s", status_code=503), 503, "Временная ошибка"),
])
def test_question_publish_error_messages(exc, code, needle):
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True,
                     wb_question_publish_enabled=True, question_publish_error=exc)
    run_poll(deps)
    r = handle_update(deps, _pub_cb())
    assert r["status"] == "publish_failed" and r["code"] == code
    msg = deps.telegram.edits[-1][1]
    assert f"Wildberries вернул {code}" in msg and needle in msg


# --- edit + regenerate ------------------------------------------------------
def test_question_regenerate_uses_engine():
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True)
    run_poll(deps)
    doc_id = _qdoc_id()
    regen = {"update_id": 2, "callback_query": {
        "id": "cq2", "data": f"regen:{doc_id}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}
    r = handle_update(deps, regen)
    assert r["status"] == "regenerated"
    # regenerated card is still a QUESTION card
    assert "Вопрос покупателя" in deps.telegram.edits[-1][1]


def test_question_edit_flow():
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True)
    run_poll(deps)
    doc_id = _qdoc_id()
    # start edit
    edit_cb = {"update_id": 3, "callback_query": {
        "id": "cq3", "data": f"edit:{doc_id}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}
    assert handle_update(deps, edit_cb)["status"] == "editing_started"
    prompt_id = deps.telegram.force_replies and deps.telegram._id  # last force-reply id
    # send the new text as a reply to the ForceReply prompt
    msg = {"update_id": 4, "message": {
        "chat": {"id": 302044578}, "from": {"id": 302044578},
        "text": "Здравствуйте! Крем подойдёт для сухой кожи.",
        "reply_to_message": {"message_id": prompt_id}}}
    r = handle_update(deps, msg)
    assert r["status"] == "edited"
    doc = deps.repo.get(doc_id)
    assert "сухой кожи" in doc["final_answer"]


# --- no impact on reviews ----------------------------------------------------
def test_reviews_untouched_when_questions_enabled():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], questions=[_q()], wb_questions_enabled=True)
    summary = run_poll(deps)
    assert summary["processed"] == 1               # review processed
    assert summary["questions"]["processed"] == 1  # question processed
    # two distinct docs, distinct entity types
    rev = deps.repo.get(make_doc_id("wb", "review", "REVIEW_1"))
    q = deps.repo.get(_qdoc_id())
    assert rev["entity_type"] == "review" and q["entity_type"] == "question"


def test_questions_not_polled_when_disabled():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], questions=[_q()])  # wb_questions_enabled defaults false
    summary = run_poll(deps)
    assert "questions" not in summary
    assert deps.repo.get(_qdoc_id()) is None       # never claimed
