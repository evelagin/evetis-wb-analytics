"""WB questions publication — regression suite for the 2026-09 incident.

Incident: the service sent {"id","text","state"} to PATCH /api/v1/questions. The
official contract nests the text: {"id","answer":{"text"},"state"}. WB answered
200 {"error": false} yet created no answer, and the operator saw «Опубликовано».

QPUB-01…08 pin the fixed behaviour: read-before-write, contract body, bounded
read-back, no false success, no blind duplicate write, reviews unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.utils.retry as retry_mod
from app.domain.exceptions import WBApiError, WBPublishOutcomeUnknown
from app.domain.models import make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from app.services.wb_client import WBClient
from app.utils.logging import configure_logging, redact
from tests.conftest import SAMPLE_FEEDBACK, make_deps, make_settings

TG_USER = 302044578


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda *_: None)


def _q(qid="Q1"):
    return {"id": qid, "createdDate": "2026-09-27T15:08:00Z", "text": "Какая концентрация?",
            "state": "suppliersPortalSynch",
            "productDetails": {"productName": "Сыворотка", "supplierArticle": "Сыворотка Акне",
                               "nmId": 305101361, "imtId": 1, "brandName": "EVETIS"}}


def _qdoc(qid="Q1"):
    return make_doc_id("wb", "question", qid)


def _cb(update_id, action, doc_id):
    return {"update_id": update_id, "callback_query": {
        "id": f"cq{update_id}", "data": f"{action}:{doc_id}", "from": {"id": TG_USER},
        "message": {"message_id": 1001, "chat": {"id": TG_USER}}}}


def _qdeps(**kw):
    deps = make_deps(feedbacks=[], questions=[_q()], wb_questions_enabled=True,
                     wb_question_publish_enabled=True, **kw)
    run_poll(deps)  # creates the question card (pending_approval)
    return deps


def _last_edit(deps):
    return deps.telegram.edits[-1][1] if deps.telegram.edits else ""


def _events(deps, event_type):
    """Events queued in the Firestore outbox (delivered to BQ by flush_events)."""
    queued = [rec["payload"] for rec in deps.repo.outbox.values()]
    return [e for e in queued + deps.bq.events if e["event_type"] == event_type]


# --- QPUB-01: accepted and read back -> PUBLISH_VERIFIED ---------------------
def test_qpub01_accepted_and_verified():
    deps = _qdeps()
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r["status"] == Status.PUBLISHED.value
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "published" and doc["publication_state"] == "published"
    assert doc["verified_at"]
    assert deps.wb.question_answers["Q1"] == doc["final_answer"]
    assert "Опубликовано" in _last_edit(deps) and "подтверждён" in _last_edit(deps)
    trace = doc["publish_trace"][-1]
    assert trace["precheck"] == "unanswered" and trace["write"] == "sent"
    assert trace["verification_result"] == "verified"
    assert trace["final_publication_state"] == "published"
    ev = _events(deps, "published")[-1]
    assert json.loads(ev["payload_json"])["publication_attempt_id"] == trace["publication_attempt_id"]


# --- QPUB-02: explicit WB error -> PUBLISH_FAILED, still actionable -----------
def test_qpub02_explicit_error_is_failed_and_actionable():
    deps = _qdeps(question_publish_error=WBApiError("rejected", status_code=422, body="bad"))
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r == {"status": "publish_failed", "code": 422}
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "publish_failed"
    assert "Опубликовано" not in _last_edit(deps) and "422" in _last_edit(deps)
    # actionable: a later tap may publish once WB accepts
    deps.wb.question_publish_error = None
    assert handle_update(deps, _cb(11, "pub", _qdoc()))["status"] == "published"


def test_qpub02b_error_true_in_2xx_body_is_failure():
    def handler(request):
        return httpx.Response(200, json={"data": None, "error": True,
                                         "errorText": "Некорректный текст", "additionalErrors": None})

    wb = _wbclient(handler)
    with pytest.raises(WBApiError) as ei:
        wb.publish_question_answer("Q1", "ответ")
    assert "error=true" in str(ei.value) and ei.value.body == "Некорректный текст"


# --- QPUB-03: 2xx but still unanswered after bounded read-back --------------
def test_qpub03_accepted_not_visible_is_not_success():
    deps = _qdeps()
    deps.wb.question_visible_after_publish = False  # e.g. WB pre-moderation
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r["status"] == Status.PUBLISH_ACCEPTED.value
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "publish_accepted" and doc.get("published_at") is None
    edit = _last_edit(deps)
    assert "Опубликовано" not in edit and "не подтверждена" in edit
    # precheck (1) + bounded read-back (default 3); writes exactly once
    assert deps.wb.get_question_calls == 4 and len(deps.wb.published_questions) == 1
    assert deps.repo.get(_qdoc())["publish_trace"][-1]["verification_attempts"] == 3


def test_qpub03b_poll_reverification_confirms_later():
    deps = _qdeps()
    deps.wb.question_visible_after_publish = False
    handle_update(deps, _cb(10, "pub", _qdoc()))
    deps.wb.question_answers["Q1"] = deps.repo.get(_qdoc())["final_answer"]  # moderation passed
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["verified"] == 1
    assert deps.repo.get(_qdoc())["status"] == "published"
    assert "Опубликовано" in _last_edit(deps)
    assert len(deps.wb.published_questions) == 1  # re-verification never writes


def test_qpub03c_window_expired_becomes_unknown():
    deps = _qdeps()
    deps.wb.question_visible_after_publish = False
    handle_update(deps, _cb(10, "pub", _qdoc()))
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    deps.repo.docs[_qdoc()]["publish_accepted_at"] = old
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["unknown"] == 1
    assert deps.repo.get(_qdoc())["status"] == "publish_unknown"
    assert "Опубликовано" not in _last_edit(deps)


# --- QPUB-04: timeout, answer actually landed -> verified, no second write ---
def test_qpub04_timeout_but_landed_is_verified_without_rewrite():
    deps = _qdeps()
    deps.wb.question_publish_timeout = True
    deps.wb.question_timeout_lands = True
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r["status"] == "published"
    assert len(deps.wb.published_questions) == 1
    trace = deps.repo.get(_qdoc())["publish_trace"][-1]
    assert trace["write"] == "outcome_unknown" and trace["verification_result"] == "verified"


# --- QPUB-05: timeout, not landed -> unknown, no blind retry ------------------
def test_qpub05_timeout_not_landed_is_unknown_and_not_resent():
    deps = _qdeps()
    deps.wb.question_publish_timeout = True
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r["status"] == Status.PUBLISH_UNKNOWN.value
    assert len(deps.wb.published_questions) == 1          # not re-sent automatically
    assert "Опубликовано" not in _last_edit(deps)
    # explicit operator re-tap: reads WB first; still unanswered -> one new write
    deps.wb.question_publish_timeout = False
    assert handle_update(deps, _cb(11, "pub", _qdoc()))["status"] == "published"
    assert len(deps.wb.published_questions) == 2


def test_qpub05b_retap_after_late_landing_does_not_write_again():
    deps = _qdeps()
    deps.wb.question_publish_timeout = True
    handle_update(deps, _cb(10, "pub", _qdoc()))
    deps.wb.question_answers["Q1"] = deps.repo.get(_qdoc())["final_answer"]  # landed late
    r = handle_update(deps, _cb(11, "pub", _qdoc()))
    assert r == {"status": "published", "write": "skipped"}
    assert len(deps.wb.published_questions) == 1


def test_qpub05c_client_read_timeout_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(WBPublishOutcomeUnknown) as ei:
        _wbclient(handler).publish_question_answer("Q1", "ответ")
    assert calls == ["PATCH"] and ei.value.request_sha256


def test_qpub05d_client_5xx_after_send_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502)

    with pytest.raises(WBPublishOutcomeUnknown):
        _wbclient(handler).publish_question_answer("Q1", "ответ")
    assert len(calls) == 1


def test_qpub05e_client_connect_error_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"data": None, "error": False})

    res = _wbclient(handler).publish_question_answer("Q1", "ответ")
    assert len(calls) == 2 and res["status_code"] == 200


# --- QPUB-06: review publication unchanged ------------------------------------
def test_qpub06_review_publication_regression():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], wb_questions_enabled=True,
                     wb_question_publish_enabled=True)
    run_poll(deps)
    deps.wb.get_feedback_calls = 0  # the pre-card actionability read belongs to ingestion
    doc_id = make_doc_id("wb", "review", "REVIEW_1")
    assert handle_update(deps, _cb(10, "pub", doc_id))["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", deps.repo.get(doc_id)["final_answer"])]
    assert deps.wb.get_question_calls == 0 and deps.wb.published_questions == []
    assert deps.wb.get_feedback_calls == 2
    assert deps.repo.get(doc_id)["verified_at"]
    assert "Опубликовано" in _last_edit(deps)


def test_qpub06b_review_body_and_endpoint_unchanged():
    seen = {}

    def handler(request):
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(204)

    _wbclient(handler).publish_answer("R1", "ответ")
    assert seen == {"method": "POST", "path": "/api/v1/feedbacks/answer",
                    "body": {"id": "R1", "text": "ответ"}}


def test_qpub06c_review_timeout_is_unknown_not_duplicated():
    deps = make_deps([dict(SAMPLE_FEEDBACK)],
                     publish_error=WBPublishOutcomeUnknown("WB publish outcome unknown (ReadTimeout)"))
    run_poll(deps)
    doc_id = make_doc_id("wb", "review", "REVIEW_1")
    assert handle_update(deps, _cb(10, "pub", doc_id))["status"] == "publish_unknown"
    assert deps.repo.get(doc_id)["status"] == "publish_unknown"
    assert "Опубликовано" not in _last_edit(deps)


def test_qpub06d_client_review_read_timeout_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(WBPublishOutcomeUnknown):
        _wbclient(handler).publish_answer("R1", "ответ")
    assert len(calls) == 1


# --- QPUB-07: question and review ids never cross publication methods --------
def test_qpub07_routing_is_entity_specific():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], questions=[_q()], wb_questions_enabled=True,
                     wb_question_publish_enabled=True)
    run_poll(deps)
    handle_update(deps, _cb(10, "pub", _qdoc()))
    assert [x[0] for x in deps.wb.published_questions] == ["Q1"] and deps.wb.published == []
    handle_update(deps, _cb(11, "pub", make_doc_id("wb", "review", "REVIEW_1")))
    assert [x[0] for x in deps.wb.published] == ["REVIEW_1"]
    assert [x[0] for x in deps.wb.published_questions] == ["Q1"]


# --- QPUB-08: double tap stays idempotent -------------------------------------
def test_qpub08_double_tap_publishes_once():
    deps = _qdeps()
    first = handle_update(deps, _cb(10, "pub", _qdoc()))
    second = handle_update(deps, _cb(11, "pub", _qdoc()))
    assert first["status"] == "published" and second["status"] == "stale"
    assert len(deps.wb.published_questions) == 1


def test_qpub08b_double_tap_while_accepted_is_stale():
    deps = _qdeps()
    deps.wb.question_visible_after_publish = False
    handle_update(deps, _cb(10, "pub", _qdoc()))
    assert handle_update(deps, _cb(11, "pub", _qdoc()))["status"] == "stale"
    assert len(deps.wb.published_questions) == 1


# --- answered in the WB cabinet with another text -----------------------------
def test_answered_externally_is_not_overwritten():
    deps = _qdeps()
    deps.wb.question_answers["Q1"] = "Ответ из кабинета"
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r == {"status": "answered_externally", "write": "skipped"}
    assert deps.wb.published_questions == []
    assert "Опубликовано" not in _last_edit(deps)


def test_precheck_failure_does_not_write_blind():
    deps = _qdeps()
    deps.wb.get_question_error = WBApiError("WB question fetch failed", status_code=503)
    r = handle_update(deps, _cb(10, "pub", _qdoc()))
    assert r["status"] == "publish_failed" and deps.wb.published_questions == []


# --- WB client contract --------------------------------------------------------
def _wbclient(handler, **settings):
    s = make_settings(**settings)
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=s.wb_api_base_url)
    return WBClient(s, "TOKEN", client=http)


def test_get_question_reads_single_question():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/v1/question"
        assert request.url.params["id"] == "Q1"
        return httpx.Response(200, json={"data": {"id": "Q1", "answer": {"text": "да"}},
                                         "error": False})

    assert _wbclient(handler).get_question("Q1")["answer"]["text"] == "да"


# --- secrets never reach logs ---------------------------------------------------
_FAKE_TG = "1234567890:" + "A" * 35


def test_redact_masks_telegram_token_inside_bot_url():
    line = f'HTTP Request: POST https://api.telegram.org/bot{_FAKE_TG}/sendMessage "HTTP/1.1 200 OK"'
    out = redact(line)
    assert _FAKE_TG not in out and "A" * 35 not in out and "<telegram_token>" in out


def test_configure_logging_drops_telegram_request_lines(capsys):
    # 1.4.1 design: httpx lines for api.telegram.org are dropped by host; WB lines stay
    configure_logging("INFO")
    log = logging.getLogger("httpx")
    log.info(f"HTTP Request: POST https://api.telegram.org/bot{_FAKE_TG}/sendMessage")
    log.info("HTTP Request: PATCH https://feedbacks-api.wildberries.ru/api/v1/questions")
    out = capsys.readouterr().out
    assert "api.telegram.org" not in out and "A" * 35 not in out
    assert "feedbacks-api.wildberries.ru/api/v1/questions" in out
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_publication_trace_holds_no_token_or_answer_text():
    deps = _qdeps()
    handle_update(deps, _cb(10, "pub", _qdoc()))
    doc = deps.repo.get(_qdoc())
    blob = json.dumps(doc["publish_trace"], ensure_ascii=False) + "".join(
        rec["payload"].get("payload_json") or "" for rec in deps.repo.outbox.values())
    assert "TOKEN" not in blob and doc["final_answer"] not in blob


# --- manual operator text goes through the same validators -------------------
def test_manual_text_is_validated_like_ai_draft():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], primary=True)
    run_poll(deps)
    doc_id = make_doc_id("wb", "review", "REVIEW_1")
    handle_update(deps, _cb(10, "edit", doc_id))
    prompt_id = deps.repo.get_editing_session(TG_USER)["prompt_message_id"]
    reply = {"update_id": 11, "message": {"message_id": 6, "chat": {"id": TG_USER},
             "from": {"id": TG_USER}, "text": "Этот крем лечит акне за неделю",
             "reply_to_message": {"message_id": prompt_id}}}
    assert handle_update(deps, reply)["status"] == "edited"
    card = deps.telegram.sent[-1][1]
    assert "ручной текст" in card and "лечит акне" in card


def test_accepted_doc_without_timestamp_falls_back_and_expires():
    # backward compatibility: a publish_accepted doc lacking publish_accepted_at
    deps = _qdeps()
    deps.wb.question_visible_after_publish = False
    handle_update(deps, _cb(10, "pub", _qdoc()))
    d = deps.repo.docs[_qdoc()]
    d.pop("publish_accepted_at", None)
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    d["published_at"] = None
    d["updated_at"] = old
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["unknown"] == 1
    assert deps.repo.get(_qdoc())["status"] == "publish_unknown"


# --- legacy (pre-1.5.0) «published» questions are reconciled against WB --------
def _legacy_published(deps, qid="Q1"):
    """Simulate a pre-1.5.0 doc: marked published on a 2xx, never read back."""
    d = deps.repo.docs[_qdoc(qid)]
    d.update(status="published", published_at="2026-09-27T19:25:08+00:00",
             wb_response={"data": None, "error": False})
    d.pop("publication_state", None)
    d.pop("verified_at", None)
    return d


def test_legacy_published_without_answer_becomes_actionable_then_publishes_once():
    deps = _qdeps()
    _legacy_published(deps)                     # WB: no answer (question_answers empty)
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["legacy"]["unknown"] == 1
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "publish_unknown"
    assert doc["publish_trace"][-1]["phase"] == "legacy_reconcile"
    _, warning, markup = deps.telegram.sent[-1]   # a NEW card, not an edit (1.5.2)
    assert "Опубликовано" not in warning and "не подтверждена" in warning
    assert _has_publish_button(markup)
    assert deps.wb.published_questions == []    # reconciliation never writes
    # operator taps «Опубликовать» -> corrected path, exactly one write, read back
    r = handle_update(deps, _cb_on(20, "pub", _qdoc(), int(doc["telegram_message_id"])))
    assert r["status"] == "published" and len(deps.wb.published_questions) == 1
    assert deps.repo.get(_qdoc())["verified_at"]
    # next poll: nothing legacy left, no further writes
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["legacy"]["checked"] == 0
    assert len(deps.wb.published_questions) == 1


def test_legacy_published_with_our_answer_is_verified_silently():
    deps = _qdeps()
    d = _legacy_published(deps)
    deps.wb.question_answers["Q1"] = d["final_answer"]
    edits_before = len(deps.telegram.edits)
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["legacy"]["verified"] == 1
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "published" and doc["verified_at"] and doc["legacy_reconciled_at"]
    assert doc["published_at"] == "2026-09-27T19:25:08+00:00"   # original kept
    assert len(deps.telegram.edits) == edits_before and deps.wb.published_questions == []


def test_legacy_published_with_other_answer_is_answered_externally():
    deps = _qdeps()
    _legacy_published(deps)
    deps.wb.question_answers["Q1"] = "Ответ из кабинета"
    run_poll(deps)
    assert deps.repo.get(_qdoc())["status"] == "answered_externally"
    assert deps.wb.published_questions == []


def test_question_reconciler_excludes_reviews_feedback_reconciles_separately():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], questions=[_q()], wb_questions_enabled=True,
                     wb_question_publish_enabled=True)
    run_poll(deps)
    handle_update(deps, _cb(10, "pub", _qdoc()))                          # 1.5.0 verified
    handle_update(deps, _cb(11, "pub", make_doc_id("wb", "review", "REVIEW_1")))
    calls = deps.wb.get_question_calls
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["legacy"]["checked"] == 0
    assert deps.wb.get_question_calls == calls


# --- 1.5.2: recovery card must be actionable (incident ce1a68fb, 2026-09-28) ---
# The operator edited the answer manually: _handle_message sent a NEW card but kept
# the OLD message id in Firestore; «Опубликовать» was pressed on the new card, which
# became «✅ Опубликовано». Legacy reconciliation later edited the OLD message, so the
# visible card stayed «Опубликовано» without a button.
def _cb_on(update_id, action, doc_id, message_id):
    cb = _cb(update_id, action, doc_id)
    cb["callback_query"]["message"]["message_id"] = message_id
    return cb


def _has_publish_button(markup, doc_id=None):
    buttons = [b for row in (markup or {}).get("inline_keyboard", []) for b in row]
    want = f"pub:{doc_id or _qdoc()}"
    return any(b.get("callback_data") == want for b in buttons)


def _manual_edit(deps, new_text="Концентрация 2 %. Подробнее — в описании товара."):
    """Operator: «Изменить» on the stored card, then replies with the new text."""
    card_id = int(deps.repo.get(_qdoc())["telegram_message_id"])
    handle_update(deps, _cb_on(30, "edit", _qdoc(), card_id))
    prompt_id = deps.telegram._id  # force-reply prompt message
    handle_update(deps, {"update_id": 31, "message": {
        "message_id": prompt_id + 1, "chat": {"id": TG_USER}, "from": {"id": TG_USER},
        "text": new_text, "reply_to_message": {"message_id": prompt_id}}})
    new_card = deps.telegram.sent[-1]
    return card_id, deps.telegram._id, new_card


def _legacy_after_manual_edit(deps):
    """Reproduce ce1a68fb: manual edit -> pre-1.5.0 publish on the NEW card."""
    old_id, new_id, _ = _manual_edit(deps)
    _legacy_published(deps)
    return old_id, new_id


def test_manual_edit_persists_new_card_and_retires_old():
    deps = _qdeps()
    old_id, new_id, (_, _, markup) = _manual_edit(deps)
    assert new_id != old_id and _has_publish_button(markup)
    doc = deps.repo.get(_qdoc())
    assert doc["telegram_message_id"] == str(new_id)            # root-cause fix
    assert deps.telegram.edit_markups[-1] == (str(old_id), None)  # old card: no buttons


# 1. legacy published + answer=null -> publish_unknown -> warning card WITH «Опубликовать»
def test_recovery_legacy_null_answer_sends_actionable_warning_card():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    deps.telegram.fail_edits = True           # the stored card cannot even be edited
    summary = run_poll(deps)
    assert summary["questions"]["reverified"]["legacy"]["unknown"] == 1
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "publish_unknown" and doc["recovery_card_sent_at"]
    _, text, markup = deps.telegram.sent[-1]
    assert "Публикация не подтверждена на Wildberries" in text
    assert "Опубликовано" not in text and _has_publish_button(markup)
    assert doc["telegram_message_id"] == str(deps.telegram._id)
    assert deps.wb.published_questions == []


# 2. + 3. + 4. recovered card: callback accepted, GET first, exactly one PATCH
def test_recovery_publish_is_accepted_reads_first_and_patches_once():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    run_poll(deps)
    card = int(deps.repo.get(_qdoc())["telegram_message_id"])
    gets = deps.wb.get_question_calls
    r = handle_update(deps, _cb_on(40, "pub", _qdoc(), card))
    assert r["status"] == "published" and r["write"] == "sent"      # state machine accepted
    assert deps.wb.get_question_calls > gets
    trace = deps.repo.get(_qdoc())["publish_trace"][-1]
    assert trace["precheck"] == "unanswered"                        # GET before the PATCH
    assert len(deps.wb.published_questions) == 1
    assert deps.telegram.edit_markups[-1] == (card, None)
    assert "Опубликовано" in _last_edit(deps)


# 5. WB got an answer between reconciliation and the tap -> 0 PATCH, reconciled
def test_recovery_answer_appeared_before_tap_no_patch():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    run_poll(deps)
    doc = deps.repo.get(_qdoc())
    deps.wb.question_answers["Q1"] = doc["final_answer"]
    r = handle_update(deps, _cb_on(41, "pub", _qdoc(), int(doc["telegram_message_id"])))
    assert r == {"status": "published", "write": "skipped"}
    assert deps.wb.published_questions == []
    assert deps.repo.get(_qdoc())["publish_trace"][-1]["precheck"] == "already_answered"


# 6. double tap after recovery -> at most one PATCH
def test_recovery_double_tap_single_patch():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    run_poll(deps)
    card = int(deps.repo.get(_qdoc())["telegram_message_id"])
    handle_update(deps, _cb_on(42, "pub", _qdoc(), card))
    r2 = handle_update(deps, _cb_on(43, "pub", _qdoc(), card))
    assert r2["status"] == "stale" and len(deps.wb.published_questions) == 1


def test_recovery_double_tap_while_publishing_is_rejected():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    run_poll(deps)
    deps.repo.begin_publish(_qdoc())         # first tap holds the lease
    r = handle_update(deps, _cb(44, "pub", _qdoc()))
    assert r["status"] == "stale" and deps.wb.published_questions == []


# 7. legacy published + matching WB answer -> verified, no button
def test_recovery_legacy_matching_answer_verified_no_button():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    deps.wb.question_answers["Q1"] = deps.repo.get(_qdoc())["final_answer"]
    sent = len(deps.telegram.sent)
    run_poll(deps)
    doc = deps.repo.get(_qdoc())
    assert doc["status"] == "published" and doc["verified_at"]
    assert not any(_has_publish_button(m) for _, _, m in deps.telegram.sent[sent:])


# 8. legacy published + different WB answer -> answered_externally, no button
def test_recovery_legacy_other_answer_external_no_button():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    deps.wb.question_answers["Q1"] = "Ответ из кабинета"
    sent = len(deps.telegram.sent)
    run_poll(deps)
    assert deps.repo.get(_qdoc())["status"] == "answered_externally"
    assert not any(_has_publish_button(m) for _, _, m in deps.telegram.sent[sent:])
    assert deps.telegram.edit_markups[-1][1] is None             # card corrected, no button
    assert "другой ответ" in _last_edit(deps)
    assert deps.wb.published_questions == []


# Docs already in publish_unknown before 1.5.2 (ce1a68fb itself): the poll restores
# a live card once, after a read-only GET; never writes to WB.
def test_existing_publish_unknown_gets_card_once():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    d = deps.repo.docs[_qdoc()]
    d.update(status="publish_unknown", publication_state="publish_unknown")  # 1.5.1 result
    s1 = run_poll(deps)["questions"]["reverified"]["recovery_cards"]
    assert s1["sent"] == 1
    _, text, markup = deps.telegram.sent[-1]
    assert _has_publish_button(markup) and "не подтверждена на Wildberries" in text
    sent = len(deps.telegram.sent)
    s2 = run_poll(deps)["questions"]["reverified"]["recovery_cards"]
    assert s2["checked"] == 0 and len(deps.telegram.sent) == sent
    assert deps.wb.published_questions == []


def test_existing_publish_unknown_answered_meanwhile_is_resolved_without_card():
    deps = _qdeps()
    _legacy_after_manual_edit(deps)
    d = deps.repo.docs[_qdoc()]
    d.update(status="publish_unknown", publication_state="publish_unknown")
    deps.wb.question_answers["Q1"] = d["final_answer"]
    sent = len(deps.telegram.sent)
    s = run_poll(deps)["questions"]["reverified"]["recovery_cards"]
    assert s["resolved"] == 1 and deps.repo.get(_qdoc())["status"] == "published"
    assert len(deps.telegram.sent) == sent and deps.wb.published_questions == []


@pytest.mark.parametrize("text,status,writes", [
    ("В составе 2,25% салициловой кислоты.", "policy_blocked", 0),
    ("Спасибо за вопрос.", "published", 1),
])
def test_question_uses_real_current_policy_gate(text, status, writes):
    deps = _qdeps()
    deps.publication_validator = None
    deps.repo.docs[_qdoc()].update(nm_id="438775617", supplier_article="", final_answer=text)
    result = handle_update(deps, _cb(101, "pub", _qdoc()))
    assert result["status"] == status
    assert len(deps.wb.published_questions) == writes
    assert deps.repo.get(_qdoc())["publish_trace"][-1]["policy"]["snapshot"]
