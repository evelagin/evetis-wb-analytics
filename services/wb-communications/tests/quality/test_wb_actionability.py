"""Only reviews that really need a FIRST seller answer may become a Telegram moderation card.

Fixtures are the live WB objects of 10.10.2026 (Марина — a rewritten text review, child of an
answered rating; Татьяна — a rating without words, isAnswered=true, no seller answer)."""
import copy

import pytest

from app.domain.exceptions import WBApiError
from app.domain.models import make_doc_id
from app.services import wb_actionability as act
from app.services.pipeline import run_poll
from app.services.wb_completeness import status_text
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import FixedV2, fb
from tests.quality.test_r24a1_completeness import rating_only

PARENT_ANSWER = "Марина, благодарим за высокую оценку крема для рук EVETIS. Нам особенно приятно, что Вы выбрали именно его."


def marina():
    f = fb(text="Отличный крем")
    f.update(id="cOnHei2Q31I2QSfHayHD", userName="Марина", state="none", answer=None, wasViewed=False,
             parentFeedbackId="2GZmmHRRFfNKCW85Odr7", childFeedbackId=None, createdDate="2026-10-10T02:37:59.612Z")
    return f


def marina_parent():
    f = rating_only("2GZmmHRRFfNKCW85Odr7", hours_ago=20)
    f.update(userName="Марина", state="wbRu", childFeedbackId="cOnHei2Q31I2QSfHayHD", parentFeedbackId=None)
    return f


def tatiana():
    f = rating_only("fAryFDvgSBkCHN67pRB1", hours_ago=2)
    f.update(userName="Татьяна", state="wbRu", wasViewed=True, isAnswered=True, parentFeedbackId=None, childFeedbackId=None)
    return f


def deps(feedbacks=(), **extra):
    extra.setdefault("wb_rating_only_ingest_enabled", True)
    d = make_deps(list(feedbacks), openai=FixedV2(), v31_only_operator_enabled=True,
                  wb_reconcile_external_answers_enabled=True, **extra)
    d.publication_validator = None
    return d


def cards(d):
    return [doc["source_id"] for doc in d.repo.docs.values() if doc.get("telegram_message_id")]


def card_text(d, source_id):
    doc_id = make_doc_id("wb", "review", source_id)
    message_id = int(d.repo.get(doc_id)["telegram_message_id"])
    return d.telegram.sent[message_id - 1][1] if len(d.telegram.sent) >= message_id else d.telegram.sent[-1][1]


# --- pure classification ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,verdict,reason", [
    ({"id": "R", "state": "none", "answer": None}, act.ACTIONABLE_FIRST_RESPONSE, "no_seller_answer"),
    ({"id": "R", "state": "wbRu", "answer": None, "isAnswered": True}, act.ACTIONABLE_FIRST_RESPONSE, "no_seller_answer"),
    ({"id": "R", "state": "wbRu", "answer": {"text": "Спасибо!", "state": "wbRu", "editable": True}},
     act.ALREADY_ANSWERED, "seller_answer_exists"),
    ({"id": "R", "state": "wbRu", "answer": {"text": "Спасибо!", "state": "reviewRequired", "editable": False}},
     act.ALREADY_ANSWERED, "seller_answer_exists"),
    ({"id": "R", "state": "wbRu", "answer": None, "childFeedbackId": "NEW"}, act.NON_ACTIONABLE, "superseded_by_newer_version"),
    ({"id": "R", "state": "deleted", "answer": None}, act.AMBIGUOUS, "unproven_state:deleted"),
    ({"id": "R", "state": "wbRu", "answer": {"text": "  "}}, act.AMBIGUOUS, "answer_without_text"),
    ({"id": "R", "state": "wbRu"}, act.AMBIGUOUS, "missing_answer_field"),
    ({"id": "OTHER", "state": "wbRu", "answer": None}, act.AMBIGUOUS, "identity_mismatch"),
    (None, act.AMBIGUOUS, "identity_mismatch"),
])
def test_classification_contract(raw, verdict, reason):
    decision = act.classify(raw, "R")
    assert (decision.verdict, decision.reason) == (verdict, reason)
    assert decision.actionable is (verdict == act.ACTIONABLE_FIRST_RESPONSE)


# --- the two live cases of 10.10 -----------------------------------------------------------------------
def test_marina_rewritten_text_review_is_one_card_with_previous_answer_shown():
    d = deps([marina()])
    d.wb.answered_feedbacks = [marina_parent()]
    d.wb.feedback_answers["2GZmmHRRFfNKCW85Odr7"] = PARENT_ANSWER
    summary = run_poll(d)
    assert summary["actionability"]["actionable"] == 1 and summary["processed"] == 1
    assert cards(d) == ["cOnHei2Q31I2QSfHayHD"]
    doc = d.repo.get(make_doc_id("wb", "review", "cOnHei2Q31I2QSfHayHD"))
    assert doc["wb_parent_feedback_id"] == "2GZmmHRRFfNKCW85Odr7" and doc["wb_previous_answer"] == PARENT_ANSWER
    assert "Покупатель обновил отзыв" in d.telegram.sent[0][1] and "Нам особенно приятно" in d.telegram.sent[0][1]
    # the parent (superseded, already answered) never becomes a second card
    assert summary["rating_only"]["processed"] == 0
    assert run_poll(d)["actionability"]["known"] == 1 and len(d.telegram.sent) == 1


def test_tatiana_rating_in_answered_category_without_seller_answer_is_one_card():
    d = deps()
    d.wb.answered_feedbacks = [tatiana()]
    summary = run_poll(d)["rating_only"]
    assert summary["actionable"] == 1 and summary["processed"] == 1
    assert cards(d) == ["fAryFDvgSBkCHN67pRB1"]
    assert "Оценка без комментария" in d.telegram.sent[0][1] and "ответить можно" in d.telegram.sent[0][1]
    assert run_poll(d)["rating_only"]["known"] == 1 and len(d.telegram.sent) == 1


# --- A–N ------------------------------------------------------------------------------------------------
def test_a_real_seller_answer_exists_zero_card_on_both_paths():
    d = deps([fb()])
    d.wb.answered_feedbacks = [rating_only("RO1")]
    d.wb.feedback_answers[fb()["id"]] = "Ответ из кабинета"
    d.wb.feedback_answers["RO1"] = "Ответ из кабинета"
    summary = run_poll(d)
    assert summary["actionability"]["already_answered"] == 1 and summary["rating_only"]["answered_on_wb"] == 1
    assert d.repo.docs == {} and d.telegram.sent == []


def test_b_c_misleading_list_but_direct_truth_no_answer_one_card():
    d = deps()
    d.wb.answered_feedbacks = [{**tatiana(), "answer": {"text": ""}}]       # list says «answered»
    assert run_poll(d)["rating_only"]["processed"] == 1 and len(d.telegram.sent) == 1


def test_d_answered_category_with_real_answer_not_actionable():
    d = deps()
    d.wb.answered_feedbacks = [tatiana()]
    d.wb.feedback_answers["fAryFDvgSBkCHN67pRB1"] = "Татьяна, спасибо!"
    assert run_poll(d)["rating_only"]["answered_on_wb"] == 1 and d.telegram.sent == []


def test_e_direct_read_error_fails_closed_then_one_card_later():
    d = deps([fb()])
    real = d.wb.get_feedback
    d.wb.get_feedback = lambda *a, **k: (_ for _ in ()).throw(WBApiError("down", status_code=503))
    summary = run_poll(d)
    assert summary["actionability"]["ambiguous"] == 1 and d.repo.docs == {} and d.telegram.sent == []
    d.wb.get_feedback = real
    assert run_poll(d)["processed"] == 1 and len(d.telegram.sent) == 1


@pytest.mark.parametrize("direct", [
    {"state": "deleted", "answer": None}, {"state": "none", "answer": {"text": ""}}, {"state": "none"},
])
def test_f_ambiguous_actionability_fails_closed(direct):
    d = deps([fb()], wb_rating_only_ingest_enabled=False)
    d.wb.get_feedback = lambda fid, **k: {"id": fid, **direct}
    assert run_poll(d)["actionability"]["ambiguous"] == 1 and d.repo.docs == {} and d.telegram.sent == []


def test_g_h_next_hour_and_scheduler_retry_zero_duplicate():
    d = deps([fb()])
    run_poll(d)
    reads = d.wb.get_feedback_calls
    for _ in range(3):
        assert run_poll(d)["actionability"]["known"] == 1
    assert len(d.telegram.sent) == 1 and len(d.repo.docs) == 1
    assert d.wb.get_feedback_calls - reads <= 3          # only reconciliation of the pending card reads


@pytest.mark.parametrize("status", ["published", "answered_externally", "skipped", "pending_approval"])
def test_i_j_terminal_or_pending_review_never_reintroduced(status):
    d = deps([fb()])
    run_poll(d)
    doc_id = next(iter(d.repo.docs))
    d.repo.docs[doc_id]["status"] = status
    run_poll(d)
    assert len(d.telegram.sent) == 1 and len(d.repo.docs) == 1


def test_retry_after_card_was_sent_restores_without_second_card():
    d = deps([fb()])
    run_poll(d)
    doc_id = next(iter(d.repo.docs))
    d.repo.docs[doc_id]["status"] = "error"          # e.g. a store failure after Telegram accepted the card
    summary = run_poll(d)
    assert summary["actionability"]["duplicates_prevented"] == 1
    assert len(d.telegram.sent) == 1 and d.repo.get(doc_id)["status"] == "pending_approval"


def test_retry_of_review_that_never_got_a_card_is_processed_once():
    d = deps([fb()])
    d.telegram.send_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
    assert run_poll(d)["errors"] == 1
    from tests.conftest import FakeTelegram
    d.telegram = FakeTelegram()
    assert run_poll(d)["processed"] == 1 and len(d.telegram.sent) == 1


def test_k_normal_new_text_review_one_card():
    d = deps([fb()])
    summary = run_poll(d)
    assert summary["actionability"] == {"candidates": 1, "known": 0, "actionable": 1, "already_answered": 0,
                                        "non_actionable": 0, "ambiguous": 0, "duplicates_prevented": 0}
    assert summary["processed"] == 1 and len(d.telegram.sent) == 1
    assert "Оценка без комментария" not in d.telegram.sent[0][1]


def test_l_questions_unchanged(monkeypatch):
    import app.services.pipeline as pl
    monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": "2026-10-09T12:00:00Z", "text": "Как часто можно использовать крем?",
         "productDetails": {"productName": "Крем", "supplierArticle": "252442517", "nmId": 252442517, "imtId": 1,
                            "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2(), v31_only_operator_enabled=True)
    d.publication_validator = None
    assert run_poll(d)["questions"]["processed"] == 1 and d.wb.get_feedback_calls == 0


def test_m_new_rating_only_one_card():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    assert run_poll(d)["rating_only"]["processed"] == 1 and cards(d) == ["RO1"]


def test_n_superseded_review_seller_should_not_answer_zero_card_on_both_paths():
    old = {**fb(), "childFeedbackId": "NEWER"}
    d = deps([old])
    d.wb.answered_feedbacks = [{**rating_only("RO1"), "childFeedbackId": "NEWER2"}]
    summary = run_poll(d)
    assert summary["actionability"]["non_actionable"] == 1 and summary["rating_only"]["non_actionable"] == 1
    assert d.repo.docs == {} and d.telegram.sent == []


# --- O–S --------------------------------------------------------------------------------------------------
def test_o_reconciliation_zero_wb_writes_and_counts_superseded():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1"), rating_only("RO2")]
    run_poll(d)
    d.wb.feedback_answers["RO1"] = "Ответ появился в кабинете"
    d.wb.answered_feedbacks[1]["childFeedbackId"] = "RO2-NEW"            # the buyer rewrote RO2
    summary = run_poll(d)["reconciled"]
    assert summary["answered_externally"] == 1 and summary["superseded"] == 1
    assert d.repo.get(make_doc_id("wb", "review", "RO2"))["status"] == "pending_approval"   # no new state
    assert d.wb.published == [] and d.wb.published_questions == []


def test_p_auto_publish_off_and_polls_never_write():
    from app.v3.registry import load_policy
    assert load_policy()["runtime"]["auto_publish"] is False
    d = deps([fb(), marina()])
    d.wb.answered_feedbacks = [tatiana(), rating_only("RO1")]
    for _ in range(3):
        run_poll(d)
    assert d.wb.published == [] and d.wb.published_questions == []


def test_q_owner_controls_unchanged():
    from types import SimpleNamespace
    from app.services.owner_override import enabled, owner_authority
    assert enabled(SimpleNamespace(v31_owner_override_enabled=True)) is False
    assert owner_authority(SimpleNamespace(v31_owner_override_enabled=True, v31_owner_override_user_ids={"1"}))


def test_s_rating_only_diversity_preserved_on_cards():
    from app.response_quality.core import RATING_ONLY_VARIANTS
    d = deps()
    d.wb.answered_feedbacks = [rating_only(f"RO{i}", hours_ago=1 + i) for i in range(5)]
    run_poll(d)
    answers = {doc["final_answer"] for doc in d.repo.docs.values()}
    assert len(answers) >= 3
    bodies = {v.split("{")[0][:12] for v in RATING_ONLY_VARIANTS}
    assert any(any(b.lower() in a.lower() for b in bodies) for a in answers)


# --- gate flag + observability -------------------------------------------------------------------------
def test_gate_off_is_previous_behaviour():
    d = make_deps([fb()], openai=FixedV2(), v31_only_operator_enabled=True, wb_actionability_gate_enabled=False)
    d.publication_validator = None
    summary = run_poll(d)
    assert "actionability" not in summary and summary["processed"] == 1 and d.wb.get_feedback_calls == 0


def test_poll_health_and_status_say_how_many_needed_a_reply():
    d = deps([fb(), {**fb(), "id": "ANS"}, {**fb(), "id": "OLD", "childFeedbackId": "X"}])
    d.wb.feedback_answers["ANS"] = "Уже ответили"
    d.wb.answered_feedbacks = [tatiana()]
    run_poll(d)
    h = d.repo.get_poll_health()
    assert (h["reviews_candidates"], h["reviews_actionable"], h["filtered_already_answered"],
            h["filtered_non_actionable"], h["filtered_ambiguous"]) == (4, 2, 1, 1, 0)
    assert h["telegram_cards_sent"] == 2
    text = status_text(d)
    assert "кандидатов 4, требуют ответа 2" in text and "уже отвечены 1" in text and "не требуют ответа 1" in text
