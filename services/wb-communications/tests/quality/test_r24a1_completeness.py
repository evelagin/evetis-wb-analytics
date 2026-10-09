"""R2.4A.1: rating-only WB reviews (isAnswered=true, no seller answer) enter the normal pipeline;
cards already answered in the WB cabinet are closed read-only; /status reads a poll-health record.
The seller-answer truth is the WB answer TEXT, never the isAnswered flag."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.exceptions import WBApiError
from app.services.pipeline import handle_update, run_poll
from app.services.wb_completeness import next_poll, rating_only_without_seller_answer, status_text
from tests.conftest import SAMPLE_FEEDBACK, make_deps
from tests.quality.test_r2_operator_draft import FixedV2, fb

OWNER, GROUP = "302044578", "302044578"


def iso(hours_ago=1.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def rating_only(fid="RO1", hours_ago=1.0, rating=5, answer=None):
    f = copy.deepcopy(SAMPLE_FEEDBACK)
    f.update(id=fid, text="", pros="", cons="", bables=[], productValuation=rating, userName="Анна",
             createdDate=iso(hours_ago), answer=answer)
    f["productDetails"] = {**f["productDetails"], "nmId": 252442517, "supplierArticle": "252442517"}
    return f


def deps(feedbacks=(), *, rating=True, reconcile=False, **extra):
    d = make_deps(list(feedbacks), openai=FixedV2(), v31_only_operator_enabled=True,
                  wb_rating_only_ingest_enabled=rating, wb_reconcile_external_answers_enabled=reconcile, **extra)
    d.publication_validator = None
    return d


def docs(d):
    return list(d.repo.docs.items())


def say(d, text, user=OWNER, chat=GROUP, n=900):
    return handle_update(d, {"update_id": n, "message": {"chat": {"id": int(chat)}, "from": {"id": int(user)},
                                                         "text": text}})


# rating-only ingestion -------------------------------------------------------------------------
def test_predicate_uses_answer_text_not_isanswered():
    assert rating_only_without_seller_answer(rating_only())
    assert not rating_only_without_seller_answer(rating_only(answer={"text": "Спасибо!", "state": "wbRu"}))
    assert rating_only_without_seller_answer(rating_only(answer={"text": "   "}))
    assert not rating_only_without_seller_answer({**rating_only(), "text": "Отлично"})
    assert not rating_only_without_seller_answer({**rating_only(), "bables": ["Хорошо пахнет"]})
    assert not rating_only_without_seller_answer({**rating_only(), "productValuation": 0})


def test_A_B_rating_only_ingested_once_with_one_card():
    d = deps()
    d.wb.answered_feedbacks = [rating_only()]
    summary = run_poll(d)
    assert summary["rating_only"]["processed"] == 1 and len(d.telegram.sent) == 1
    (doc_id, doc), = docs(d)
    assert doc["source_id"] == "RO1" and doc["operator_mode"] == "v31_only"
    assert doc["v31_draft"]["status"] == "READY" and "оценк" in doc["final_answer"]
    assert "Рекомендуемый ответ 3.1E" in d.telegram.sent[-1][1]
    again = run_poll(d)                                                                   # B
    assert again["rating_only"]["processed"] == 0 and again["rating_only"]["skipped"] == 1
    assert len(d.telegram.sent) == 1 and len(d.repo.docs) == 1


def test_C_rating_only_with_real_seller_answer_not_ingested():
    d = deps()
    d.wb.answered_feedbacks = [rating_only(answer={"text": "Спасибо за оценку!", "state": "wbRu"})]
    assert run_poll(d)["rating_only"]["eligible"] == 0 and d.repo.docs == {}


def test_D_normal_unanswered_text_review_unchanged():
    d = deps(feedbacks=[fb()])
    summary = run_poll(d)
    assert summary["processed"] == 1 and summary["rating_only"]["processed"] == 0 and len(d.telegram.sent) == 1


def test_E_answered_text_review_not_ingested():
    d = deps()
    answered = {**rating_only("TX1"), "text": "Хороший крем", "answer": {"text": "Спасибо!", "state": "wbRu"}}
    d.wb.answered_feedbacks = [answered]
    assert run_poll(d)["rating_only"]["eligible"] == 0 and d.repo.docs == {}


def test_F_bounded_lookback_and_cap():
    d = deps(wb_rating_only_max_per_poll=3)
    d.wb.answered_feedbacks = [rating_only("OLD", hours_ago=100)] + [rating_only(f"N{i}") for i in range(5)]
    summary = run_poll(d)["rating_only"]
    assert summary["processed"] == 3 and summary["capped"] is True
    assert "OLD" not in {doc["source_id"] for _, doc in docs(d)}


def test_flag_off_never_reads_the_answered_feed():
    d = deps(rating=False)
    d.wb.answered_feedbacks = [rating_only()]
    summary = run_poll(d)
    assert "rating_only" not in summary and d.wb.answered_feed_calls == 0 and d.repo.docs == {}


def test_G_rating_only_draft_goes_through_preflight_and_verified_publisher():
    d = deps()
    d.wb.answered_feedbacks = [rating_only()]
    run_poll(d)
    (doc_id, doc), = docs(d)
    assert doc["publication_preflight"]["verdict"] == "PASS"
    cq = {"id": "c", "from": {"id": int(OWNER)}, "data": f"pub:{doc_id}:{doc['generation_number']}",
          "message": {"message_id": int(doc["telegram_message_id"]), "chat": {"id": int(GROUP)}}}
    assert handle_update(d, {"update_id": 1, "callback_query": cq})["status"] == "published"
    assert [t for _, t in d.wb.published] == [doc["final_answer"]]


# reconciliation ----------------------------------------------------------------------------------
def pending_card(d):
    run_poll(d)
    (doc_id, doc), = docs(d)
    return doc_id, doc


def test_H_cabinet_answer_closes_card_without_wb_write():
    d = deps(feedbacks=[fb()], reconcile=True)
    doc_id, doc = pending_card(d)
    d.wb.feedback_answers[doc["source_id"]] = "Ответ, написанный в кабинете WB"
    d.wb._feedbacks = []                                    # WB no longer lists it as unanswered
    summary = run_poll(d)
    after = d.repo.get(doc_id)
    assert summary["reconciled"]["answered_externally"] == 1
    assert after["status"] == "answered_externally" and after["reconciled_at"] and after["wb_answer_sha256"]
    msg_id, text = d.telegram.edits[-1]
    assert str(msg_id) == str(doc["telegram_message_id"]) and "уже опубликован ответ" in text
    assert d.telegram.edit_markups[-1][1] is None and d.wb.published == []


def test_I_rating_only_without_answer_stays_pending():
    d = deps(reconcile=True)
    d.wb.answered_feedbacks = [rating_only()]
    doc_id, _ = pending_card(d)
    summary = run_poll(d)                                   # WB isAnswered=true but no answer text
    assert summary["reconciled"]["unchanged"] == 1 and d.repo.get(doc_id)["status"] == "pending_approval"
    assert d.repo.get(doc_id)["wb_reconcile_checked_at"]


def test_J_same_text_on_wb_closes_as_published():
    d = deps(feedbacks=[fb()], reconcile=True)
    doc_id, doc = pending_card(d)
    d.wb.feedback_answers[doc["source_id"]] = doc["final_answer"]
    d.wb._feedbacks = []
    assert run_poll(d)["reconciled"]["published"] == 1
    after = d.repo.get(doc_id)
    assert after["status"] == "published" and after["verified_at"] and "уже опубликован" in d.telegram.edits[-1][1]


def test_K_active_operator_lease_is_never_reconciled():
    d = deps(feedbacks=[fb()], reconcile=True)
    doc_id, doc = pending_card(d)
    d.wb.feedback_answers[doc["source_id"]] = "Ответ из кабинета"
    d.wb._feedbacks = []
    d.repo.docs[doc_id]["status"] = "editing"
    assert run_poll(d)["reconciled"]["checked"] == 0 and d.repo.get(doc_id)["status"] == "editing"
    # race: the operator starts editing while WB is being read → nothing changes underneath
    d.repo.docs[doc_id]["status"] = "pending_approval"
    real = d.wb.get_feedback

    def racing(fid, **kw):
        d.repo.docs[doc_id]["status"] = "editing"
        return real(fid, **kw)
    d.wb.get_feedback = racing
    assert run_poll(d)["reconciled"]["unchanged"] == 1 and d.repo.get(doc_id)["status"] == "editing"


def test_L_wb_read_error_leaves_card_unchanged():
    d = deps(feedbacks=[fb()], reconcile=True)
    doc_id, _ = pending_card(d)
    d.wb._feedbacks = []
    d.wb.get_feedback = lambda *a, **k: (_ for _ in ()).throw(WBApiError("down", status_code=503))
    before = copy.deepcopy(d.repo.get(doc_id))
    assert run_poll(d)["reconciled"]["errors"] == 1
    after = d.repo.get(doc_id)
    assert after["status"] == before["status"] and after.get("wb_reconcile_checked_at") == before.get("wb_reconcile_checked_at")


def test_M_reconciliation_is_bounded_and_rotates():
    d = deps(reconcile=True, wb_reconcile_max_per_poll=2)
    d.wb.answered_feedbacks = [rating_only(f"R{i}") for i in range(5)]
    run_poll(d)
    d.wb.answered_feedbacks = []
    first = run_poll(d)["reconciled"]
    second = run_poll(d)["reconciled"]
    checked = {doc_id for doc_id, doc in docs(d) if doc.get("wb_reconcile_checked_at")}
    assert first["checked"] == 2 and second["checked"] == 2 and len(checked) >= 4


def test_question_answered_in_cabinet_is_closed(monkeypatch):
    import app.services.pipeline as pl
    monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": iso(), "text": "Когда появится крем?",
         "productDetails": {"productName": "Крем", "supplierArticle": "438775437", "nmId": 438775437, "imtId": 1,
                            "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2(), v31_only_operator_enabled=True, wb_reconcile_external_answers_enabled=True)
    d.publication_validator = None
    run_poll(d)
    d.wb._questions = []
    d.wb.question_answers["Q1"] = "Ответ из кабинета"
    assert run_poll(d)["reconciled"]["answered_externally"] == 1
    assert next(iter(d.repo.docs.values()))["status"] == "answered_externally" and d.wb.published_questions == []


# /status ----------------------------------------------------------------------------------------
def test_N_R_owner_status_after_a_poll():
    d = deps(feedbacks=[fb()])
    run_poll(d)
    health = d.repo.get_poll_health()
    assert health["status"] == "ok" and health["telegram_cards_sent"] == 1 and health["pending_count"] == 1
    assert health["reviews_processed"] == 1 and health["last_card_at"]
    assert say(d, "/status")["status"] == "status"
    text = d.telegram.sent[-1][1]
    assert "система работает" in text and "новых карточек 1" in text and "Ожидают решения: 1" in text


def test_R_moscow_times_and_next_poll():
    d = deps()
    d.repo.save_poll_health({"finished_at": "2026-10-09T12:00:38+00:00", "status": "ok", "reviews_fetched": 1,
                             "reviews_processed": 1, "reviews_errors": 0, "questions_fetched": 0,
                             "questions_processed": 0, "questions_errors": 0, "last_card_at": "2026-10-09T12:00:38+00:00",
                             "pending_count": 3, "revision": "r24on-test"})
    now = datetime(2026, 10, 9, 12, 20, tzinfo=timezone.utc)
    text = status_text(d, now=now)
    assert "Последний опрос: 09.10 15:00 МСК ✅" in text and "Следующий опрос: 09.10 16:00 МСК" in text
    assert "Ревизия: r24on-test" in text
    assert next_poll(datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc), d.settings).hour == 8   # 23:30 → 08:00


def test_Q_no_record_yet():
    d = deps()
    say(d, "/status")
    assert "данных о последнем опросе пока нет" in d.telegram.sent[-1][1]


def test_S_errors_and_stale_poll_are_visible():
    d = deps()
    d.repo.save_poll_health({"finished_at": "2026-10-09T12:00:00+00:00", "status": "errors", "reviews_errors": 2})
    assert "с ошибками" in status_text(d, now=datetime(2026, 10, 9, 12, 10, tzinfo=timezone.utc))
    d.repo.save_poll_health({"finished_at": "2026-10-09T09:00:00+00:00", "status": "ok"})
    assert "давно не выполнялся" in status_text(d, now=datetime(2026, 10, 9, 12, 10, tzinfo=timezone.utc))


def test_failed_poll_is_recorded():
    d = deps()
    d.wb.iter_unanswered_feedbacks = lambda: (_ for _ in ()).throw(RuntimeError("wb down"))
    with pytest.raises(RuntimeError):
        run_poll(d)
    assert d.repo.get_poll_health()["status"] == "failed"


def test_O_P_status_for_dynamic_operator_and_denied_for_unknown():
    from tests.quality.test_r24a_team_access import GROUP as TEAM_GROUP, MARIA, STRANGER, approve, say as team_say, \
        team_deps
    d = team_deps()
    approve(d)
    run_poll(d)
    assert team_say(d, "/status", MARIA)["status"] == "status"
    assert team_say(d, "/status", STRANGER)["status"] == "unauthorized"
    from tests.quality.test_r24a_team_access import press
    press(d, f"tx:{MARIA}", "302044578")
    assert team_say(d, "/status", MARIA)["status"] == "unauthorized"


def test_T_status_is_read_only():
    d = deps(feedbacks=[fb()])
    run_poll(d)
    before = copy.deepcopy(d.repo.docs)
    health = copy.deepcopy(d.repo.get_poll_health())
    calls = d.wb.get_feedback_calls
    say(d, "/status")
    assert d.repo.docs == before and d.repo.get_poll_health() == health
    assert d.wb.published == [] and d.wb.get_feedback_calls == calls


def test_repeated_poll_is_idempotent_for_a_scheduler_retry():
    d = deps(feedbacks=[fb()])
    d.wb.answered_feedbacks = [rating_only()]
    run_poll(d)
    run_poll(d)                                             # a Scheduler retry of the same hour
    assert len(d.repo.docs) == 2 and len(d.telegram.sent) == 2
