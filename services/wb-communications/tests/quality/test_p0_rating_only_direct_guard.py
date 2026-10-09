"""P0 (09.10): a rating-only review from WB's answered feed becomes a card ONLY after the
authoritative direct WB read proves there is no seller answer. The list item alone is never
truth; any doubt fails closed. One WB review id → one EVETIS communication, ever."""
import copy

from app.domain.exceptions import WBApiError
from app.services.pipeline import handle_update, run_poll
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import FixedV2, fb
from tests.quality.test_r24a1_completeness import rating_only

OWNER = "302044578"


def deps(feedbacks=(), **extra):
    extra.setdefault("wb_rating_only_ingest_enabled", True)
    d = make_deps(list(feedbacks), openai=FixedV2(), v31_only_operator_enabled=True,
                  wb_reconcile_external_answers_enabled=True, **extra)
    d.publication_validator = None
    return d


def sent_sources(d):
    return [doc["source_id"] for doc in d.repo.docs.values() if doc.get("telegram_message_id")]


def test_1_list_says_unanswered_but_direct_read_has_seller_answer_no_card():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]                       # list item: answer empty
    d.wb.feedback_answers["RO1"] = "Спасибо за оценку! (ответ из кабинета)"  # direct GET: answered
    summary = run_poll(d)["rating_only"]
    assert summary["answered_on_wb"] == 1 and summary["processed"] == 0
    assert d.repo.docs == {} and d.telegram.sent == []


def test_2_direct_read_confirms_no_answer_one_card():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    assert run_poll(d)["rating_only"]["processed"] == 1
    assert sent_sources(d) == ["RO1"] and len(d.telegram.sent) == 1


def test_3_direct_read_unavailable_fails_closed_then_retries():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    real = d.wb.get_feedback
    d.wb.get_feedback = lambda *a, **k: (_ for _ in ()).throw(WBApiError("down", status_code=503))
    assert run_poll(d)["rating_only"]["read_errors"] == 1 and d.repo.docs == {} and d.telegram.sent == []
    d.wb.get_feedback = real
    assert run_poll(d)["rating_only"]["processed"] == 1 and len(d.telegram.sent) == 1


def test_3b_mismatched_or_malformed_direct_object_fails_closed():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    d.wb.get_feedback = lambda fid, **k: {"id": "OTHER", "answer": None}
    assert run_poll(d)["rating_only"]["read_errors"] == 1 and d.repo.docs == {}
    d.wb.get_feedback = lambda fid, **k: {"id": fid}                      # no answer field at all
    assert run_poll(d)["rating_only"]["read_errors"] == 1 and d.repo.docs == {}


def test_3c_direct_object_with_words_is_not_rating_only():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    d.wb.get_feedback = lambda fid, **k: {"id": fid, "answer": None, "text": "Отличный крем"}
    assert run_poll(d)["rating_only"]["not_rating_only"] == 1 and d.repo.docs == {}


def test_4_5_next_poll_and_scheduler_retry_create_no_duplicate():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1")]
    run_poll(d)
    calls = d.wb.get_feedback_calls
    for _ in range(3):                                                    # later hour + retries
        assert run_poll(d)["rating_only"]["known"] == 1
    assert len(d.repo.docs) == 1 and len(d.telegram.sent) == 1
    assert sent_sources(d) == ["RO1"]
    # a known review costs no extra direct read for ingestion (reconciliation reads are separate)


def test_6_7_locally_published_or_answered_externally_never_reintroduced():
    for status in ("published", "answered_externally", "skipped"):
        d = deps()
        d.wb.answered_feedbacks = [rating_only("RO1")]
        run_poll(d)
        doc_id = next(iter(d.repo.docs))
        d.repo.docs[doc_id]["status"] = status
        before = len(d.telegram.sent)
        assert run_poll(d)["rating_only"]["known"] == 1
        assert len(d.repo.docs) == 1 and len(d.telegram.sent) == before


def test_8_normal_unanswered_text_review_unchanged():
    d = deps(feedbacks=[fb()])
    summary = run_poll(d)
    assert summary["processed"] == 1 and summary["rating_only"]["processed"] == 0


def test_9_questions_unchanged(monkeypatch):
    import app.services.pipeline as pl
    monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": "2026-10-09T12:00:00Z", "text": "Как часто можно использовать крем?",
         "productDetails": {"productName": "Крем", "supplierArticle": "252442517", "nmId": 252442517, "imtId": 1,
                            "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2(), v31_only_operator_enabled=True, wb_rating_only_ingest_enabled=True)
    d.publication_validator = None
    assert run_poll(d)["questions"]["processed"] == 1


def test_10_flag_off_answered_feed_inactive():
    d = deps(wb_rating_only_ingest_enabled=False)
    d.wb.answered_feedbacks = [rating_only("RO1")]
    summary = run_poll(d)
    assert "rating_only" not in summary and d.wb.answered_feed_calls == 0 and d.repo.docs == {}


def test_11_12_reconciliation_closes_without_wb_write_and_respects_operator():
    d = deps()
    d.wb.answered_feedbacks = [rating_only("RO1"), rating_only("RO2")]
    run_poll(d)
    ids = {doc["source_id"]: doc_id for doc_id, doc in d.repo.docs.items()}
    d.wb.feedback_answers["RO1"] = "Ответ появился в кабинете"
    d.wb.feedback_answers["RO2"] = "Ответ появился в кабинете"
    d.repo.docs[ids["RO2"]]["status"] = "editing"                        # an operator is editing RO2
    summary = run_poll(d)["reconciled"]
    assert d.repo.get(ids["RO1"])["status"] == "answered_externally"
    assert d.repo.get(ids["RO2"])["status"] == "editing" and summary["answered_externally"] == 1
    assert d.wb.published == []


def test_13_owner_override_contract_unchanged():
    from types import SimpleNamespace
    from app.services.owner_override import enabled, owner_authority
    assert enabled(SimpleNamespace(v31_owner_override_enabled=True)) is False
    assert owner_authority(SimpleNamespace(v31_owner_override_enabled=True, v31_owner_override_user_ids={"1"}))


def test_14_auto_publish_stays_off_and_poll_never_writes_wb():
    from app.v3.registry import load_policy
    assert load_policy()["runtime"]["auto_publish"] is False
    d = deps(feedbacks=[fb()])
    d.wb.answered_feedbacks = [rating_only("RO1")]
    run_poll(d)
    run_poll(d)
    assert d.wb.published == [] and d.wb.published_questions == []
