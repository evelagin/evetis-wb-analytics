"""R2.1: v3.1E is the primary operator draft (flag), V2 only a fallback; moderate fragrance
discomfort gets a cautious draft, serious safety stays human-only; policy follows lineage."""
import copy
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.exceptions import InvalidTransition
from app.domain.models import GenerationResult
from app.services.pipeline import answer_lineage, handle_update, run_poll
from app.v3 import pilot
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import (  # noqa: F401 — policies is a fixture
    V2_TEXT, FixedV2, buttons, doc_of, fb, last_trace, policies, tap)

SET_NM = 930334397
LIVE_CASE = ("Крем очень хороший, но отдушка очень сильная, тошнотворная прям голова кружится. "
             "Хотелось бы чтобы крем приятно пах, а не вонял этой отдушкой")


def primary(feedback=None, **extra):
    extra.setdefault("openai", FixedV2())
    d = make_deps([feedback or fb()], v31_primary_operator_enabled=True, **extra)
    d.publication_validator = None              # real policy adapters
    return d


def anna():
    f = fb(text=LIVE_CASE, nm=SET_NM, rating=2)
    f["userName"] = "Анна"
    f["productDetails"]["supplierArticle"] = "Набор вишня+амбра"
    return f


def card(d):
    return d.telegram.sent[-1][1]


def edit(d, text, n=20):
    doc_id, _ = doc_of(d)
    assert tap(d, f"edit:{doc_id}", n=n)["status"] == "editing_started"
    reply = {"update_id": n + 1, "message": {"chat": {"id": 302044578}, "from": {"id": 302044578}, "text": text,
                                             "reply_to_message": {"message_id": d.telegram._id}}}
    assert handle_update(d, reply)["status"] == "edited"
    return d.repo.get(doc_id)


def tap_on(d, data, message_id, n):
    cq = {"id": f"cq{n}", "from": {"id": 302044578}, "data": data,
          "message": {"message_id": message_id, "chat": {"id": 302044578}}}
    return handle_update(d, {"update_id": n, "callback_query": cq})


# flag ---------------------------------------------------------------------------------------
def test_flag_default_off_keeps_r2(monkeypatch):
    monkeypatch.delenv("V31_PRIMARY_OPERATOR_ENABLED", raising=False)
    assert Settings().v31_primary_operator_enabled is False
    d = make_deps([fb()], openai=FixedV2(), v31_operator_draft_enabled=True)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert "operator_mode" not in doc and doc["final_answer"] == V2_TEXT
    assert "Проект ответа" in card(d) and any(c.startswith("p31:") for c in buttons(d))


def test_r1_pilot_interlocked_by_primary_flag():
    s = SimpleNamespace(v31_quality_shadow_enabled=True, v31_primary_operator_enabled=True,
                        v31_shadow_activation_id="r1", v31_shadow_start_at="2026-10-07T00:00:00+00:00",
                        v31_shadow_end_at="2026-10-21T00:00:00+00:00", v31_shadow_pilot_max_communications="20")
    assert pilot.activation(s) == (None, pilot.OPERATOR_SURFACES_ON)


# primary card ------------------------------------------------------------------------------
def test_ready_v31_is_the_primary_answer():
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    v31 = doc["v31_draft"]["text"]
    assert doc["operator_mode"] == "v31_primary" and doc["final_answer"] == v31
    assert [v["source"] for v in doc["answer_versions"]] == ["ai", "v31e"] and doc["ai_answer"] == V2_TEXT
    assert "Рекомендуемый ответ 3.1E" in card(d) and v31.split(",")[0] in card(d)
    assert V2_TEXT not in card(d) and "Проект ответа" not in card(d)
    gen = doc["generation_number"]
    assert buttons(d) == [f"pub:{doc_id}:{gen}", f"edit:{doc_id}", f"regen:{doc_id}", f"skip:{doc_id}",
                          f"sv2:{doc_id}:{gen}"]
    assert d.wb.published == []                                       # never auto-published


def test_publish_uses_v31_policy_once_with_read_back(policies):
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    policies.v31.calls.clear()
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert policies.v31.calls == [doc["final_answer"]] and policies.live.calls == []
    assert last_trace(d)["publication_mode"] == "v31"
    assert [t for _, t in d.wb.published] == [doc["final_answer"]]
    after = d.repo.get(doc_id)
    assert after["status"] == "published" and after["generation_number"] == doc["generation_number"]


def test_double_tap_and_stale_card_write_once():
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert tap(d, f"pub:{doc_id}:{gen - 1}")["status"] == "stale"
    assert tap(d, f"pub:{doc_id}:{gen}", n=2)["status"] == "published"
    assert tap(d, f"pub:{doc_id}:{gen}", n=3)["status"] == "stale"
    assert len(d.wb.published) == 1


# manual edit inherits the lineage ----------------------------------------------------------
def test_edit_of_v31_answer_stays_on_v31_policy(policies):
    d = primary()
    run_poll(d)
    text = "Людмила, спасибо за отзыв! Рады, что крем понравился."
    doc = edit(d, text)
    assert answer_lineage(doc) == "v31" and "Ответ оператора</b> (правила 3.1E)" in card(d)
    policies.v31.calls.clear()
    doc_id = doc_of(d)[0]
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=30)["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31"
    assert policies.v31.calls == [text] and policies.live.calls == []


# V2 fallback -------------------------------------------------------------------------------
def test_explicit_switch_to_v2_uses_live_v2(policies):
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert tap(d, f"sv2:{doc_id}:{gen}")["status"] == "v2_shown"
    assert V2_TEXT in card(d) and buttons(d) == [f"u2:{doc_id}:{gen}"]
    assert d.repo.get(doc_id)["final_answer"] != V2_TEXT              # viewing changes nothing
    assert tap(d, f"u2:{doc_id}:{gen}", n=2)["status"] == "v2_adopted"
    after = d.repo.get(doc_id)
    assert after["final_answer"] == V2_TEXT and answer_lineage(after) == "live_v2"
    assert "Вариант V2</b> (выбран оператором)" in card(d)
    assert tap(d, f"u2:{doc_id}:{gen}", n=3)["status"] == "stale"     # old card generation
    policies.live.calls.clear()
    tap(d, f"pub:{doc_id}:{after['generation_number']}", n=4)
    assert last_trace(d)["publication_mode"] == "live_v2" and last_trace(d)["policy"]["gate"] == "LIVE_V2"
    assert policies.live.calls == [V2_TEXT] and policies.v31.calls[-1:] != [V2_TEXT]


def test_v31_technical_failure_falls_back_to_v2(monkeypatch, policies):
    import app.response_quality.core as core
    monkeypatch.setattr(core, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "ERROR" and doc["final_answer"] == V2_TEXT
    assert "Резервный ответ V2</b> (3.1E временно недоступен)" in card(d) and V2_TEXT in card(d)
    assert f"pub:{doc_id}:{doc['generation_number']}" in buttons(d)
    assert not any(c.startswith("sv2:") for c in buttons(d))           # V2 is already the shown answer
    tap(d, f"pub:{doc_id}:{doc['generation_number']}")
    assert last_trace(d)["publication_mode"] == "live_v2"


def test_regenerate_renews_the_v31_answer():
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert tap(d, f"regen:{doc_id}")["status"] == "regenerated"
    after = d.repo.get(doc_id)
    assert after["answer_versions"][-1]["source"] == "v31e" and answer_lineage(after) == "v31"
    assert after["generation_number"] == doc["generation_number"] + 1
    assert "Рекомендуемый ответ 3.1E" in d.telegram.edits[-1][1]


# moderate safety ---------------------------------------------------------------------------
def test_live_case_moderate_discomfort_gets_a_cautious_draft(policies):
    d = primary(feedback=anna())
    run_poll(d)
    doc_id, doc = doc_of(d)
    draft = doc["v31_draft"]
    assert draft["status"] == "READY" and draft["attention"] == "MODERATE_DISCOMFORT" and not draft["restriction"]
    text = draft["text"]
    assert text.startswith("Анна, спасибо, что отдельно отметили само средство")          # positive kept
    assert "слишком насыщенным" in text and "одним из ароматов набора" in text and "отложить" in text
    for forbidden in ("врач", "дерматолог", "прекратите использование", "всех кремов", "церамид", "Amber"):
        assert forbidden not in text
    assert "Требует внимания оператора" in card(d) and f"pub:{doc_id}:{doc['generation_number']}" in buttons(d)
    assert d.wb.published == []                                       # manual publish only
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and [t for _, t in d.wb.published] == [text]


@pytest.mark.parametrize("text", ["От запаха разболелась голова, хотя руки после крема мягкие",
                                  "Аромат слишком сильный, меня от него мутит"])
def test_headache_and_nausea_from_scent_are_moderate(text):
    d = primary(feedback=fb(text=text, rating=3))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["attention"] == "MODERATE_DISCOMFORT" and doc["v31_draft"]["status"] == "READY"
    assert "Требует внимания оператора" in card(d) and d.wb.published == []


@pytest.mark.parametrize("text", ["После крема тяжело дышать, запах резкий",
                                  "Опухли губы и тошнит от запаха",
                                  "На коже рук появились пузыри после крема",
                                  "После крема ожог на лице, запах тоже неприятный",
                                  "От запаха тошнит, потом вырвало"])
def test_serious_cases_stay_hard_human_review(text):
    d = primary(feedback=fb(text=text, rating=1))
    run_poll(d)
    doc_id, doc = doc_of(d)
    draft = doc["v31_draft"]
    assert draft["status"] == "HUMAN_REVIEW" and draft["restriction"] == "SERIOUS_SAFETY" and not draft.get("text")
    assert "Нужна проверка человеком" in card(d) and V2_TEXT not in card(d)
    assert not any(c.startswith(("pub:", "regen:")) for c in buttons(d))
    gen = doc["generation_number"]
    # a crafted/old publish tap is refused server-side, nothing changes
    assert tap(d, f"pub:{doc_id}:{gen}")["status"] == "human_review_required"
    assert d.wb.published == [] and d.repo.get(doc_id)["status"] == "pending_approval"
    # the V2 draft is visible only as a diagnostic, never adoptable
    assert tap(d, f"sv2:{doc_id}:{gen}", n=2)["status"] == "v2_shown" and buttons(d) == []
    assert tap(d, f"u2:{doc_id}:{gen}", n=3)["status"] == "stale"


def test_serious_case_operator_text_can_be_published():
    d = primary(feedback=fb(text="После крема опух язык и тяжело дышать", rating=1))
    run_poll(d)
    text = "Ольга, нам очень жаль. Пожалуйста, прекратите использование крема."
    doc = edit(d, text)
    doc_id = doc_of(d)[0]
    assert f"pub:{doc_id}:{doc['generation_number']}" in buttons(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=40)["status"] == "published"
    assert [t for _, t in d.wb.published] == [text]


def test_engine_moderate_only_when_asked_and_serious_never():
    from app.response_quality.core import prepare
    from app.v3.snapshot import load_snapshot
    snap = load_snapshot(None)
    msg = {"text": LIVE_CASE, "nm_id": str(SET_NM), "supplier_article": "Набор вишня+амбра", "rating": 2,
           "buyer_name": "Анна", "entity_type": "review"}
    assert prepare(msg, V2_TEXT, snap, force_generation=True).status == "HUMAN_REVIEW"   # R2 unchanged
    r = prepare(msg, V2_TEXT, snap, force_generation=True, moderate_safety=True)
    assert r.status == "READY" and r.final_policy["verdict"] == "PASS" and r.quality.verdict == "GOOD"
    serious = dict(msg, text="Отдушка сильная, опух язык и трудно дышать")
    assert prepare(serious, V2_TEXT, snap, force_generation=True, moderate_safety=True).status == "HUMAN_REVIEW"


def test_blocked_moderate_answer_never_falls_back_to_original(monkeypatch):
    import app.response_quality.core as core
    from app.v3.snapshot import load_snapshot
    real = core.validate_for_publication
    monkeypatch.setattr(core, "validate_for_publication",
                        lambda text, *a, **k: real(text, *a, **k) if text == V2_TEXT else {"verdict": "BLOCK", "violations": []})
    msg = {"text": LIVE_CASE, "nm_id": str(SET_NM), "supplier_article": "Набор вишня+амбра", "rating": 2,
           "buyer_name": "Анна", "entity_type": "review"}
    r = core.prepare(msg, "Анна, спасибо за отзыв!", load_snapshot(None), force_generation=True, moderate_safety=True)
    assert r.status == "HUMAN_REVIEW" and r.text is None


def test_quality_flags_ignored_positive_signal_and_over_escalation():
    from app.response_quality.core import make_plan, evaluate
    from app.v3.snapshot import load_snapshot
    snap = load_snapshot(None)
    msg = {"text": LIVE_CASE, "nm_id": str(SET_NM), "supplier_article": "Набор вишня+амбра", "rating": 2,
           "buyer_name": "Анна", "entity_type": "review"}
    p = make_plan(msg, snap, moderate_safety=True)
    bad = "Анна, жаль, что аромат оказался слишком насыщенным. Обратитесь к врачу."
    reasons = evaluate(bad, msg, p, {"verdict": "PASS"}).reasons
    assert "POSITIVE_CUSTOMER_SIGNAL_IGNORED" in reasons and "MODERATE_SAFETY_OVER_ESCALATION" in reasons
    assert "POSITIVE_CUSTOMER_SIGNAL_IGNORED" not in evaluate(p.direct_answer, msg, p, {"verdict": "PASS"}).reasons
    stop_all = "Анна, жаль, что аромат слишком насыщенный. Прекратите использование всех кремов."
    assert "MODERATE_SAFETY_BLANKET_STOP" in evaluate(stop_all, msg, p, {"verdict": "PASS"}).reasons


# card visibility and questions -------------------------------------------------------------
def test_long_customer_text_never_hides_the_primary_answer():
    review = fb(text="Пользуюсь кремом уже месяц, впечатления подробные. " * 80)
    review.update(pros="Мягкость, запах, тюбик. " * 60, cons="Хотелось бы объём побольше. " * 50)
    d = primary(feedback=review, openai=FixedV2(("Людмила, спасибо! " + "Рады, что крем подошёл. " * 60)[:990]))
    run_poll(d)
    doc_id, doc = doc_of(d)
    from app.utils.text import escape_html
    assert len(card(d)) <= 3800 and escape_html(doc["final_answer"]) in card(d)
    assert f"pub:{doc_id}:{doc['generation_number']}" in buttons(d)


def test_question_primary_publishes_via_question_publisher(monkeypatch):
    import app.services.pipeline as pl
    monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": "2026-10-07T12:00:00Z", "text": "В какой стране изготовлен крем?",
         "productDetails": {"productName": "Крем для лица", "supplierArticle": "438775617", "nmId": 438775617,
                            "imtId": 1, "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2("Страна — Китай, крем лечит акне."), v31_primary_operator_enabled=True)
    d.publication_validator = None
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["final_answer"] == "Страна производства — Китай." and "Рекомендуемый ответ 3.1E" in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert [t for _, t, _ in d.wb.published_questions] == ["Страна производства — Китай."]
    assert last_trace(d)["publication_mode"] == "v31"


# repository ------------------------------------------------------------------------------
def test_v2_adoption_requires_exact_v2_text_and_generation():
    d = primary()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    with pytest.raises(InvalidTransition):
        d.repo.adopt_v2_fallback(doc_id, gen, GenerationResult("другой текст", "x", "y", {}, 0, ""))
    with pytest.raises(InvalidTransition):
        d.repo.adopt_v2_fallback(doc_id, gen - 1, GenerationResult(V2_TEXT, "x", "y", {}, 0, ""))


def test_firestore_v2_adoption_and_regenerate_source(monkeypatch):
    from google.cloud import firestore
    from app.services.repository import FirestoreRepository
    from tests.quality.test_r2_operator_draft import _Client, _Ref
    monkeypatch.setattr(firestore, "transactional", lambda f: f)
    client = _Client({"status": "pending_approval", "generation_number": 2, "final_answer": "3.1E",
                      "ai_answer": V2_TEXT, "answer_versions": []})
    repo = FirestoreRepository.__new__(FirestoreRepository)
    monkeypatch.setattr(repo, "_lazy", lambda: client)
    monkeypatch.setattr(repo, "_doc", lambda _: _Ref(client))
    after = repo.adopt_v2_fallback("id", 2, GenerationResult(V2_TEXT, "v2", "p", {}, 0, ""))
    assert len(client.tx.updates) == 1 and after["final_answer"] == V2_TEXT
    assert after["answer_versions"][-1]["source"] == "v2_fallback" and "status" not in client.tx.updates[0]
    client.doc.update(status="regenerating", lock_token="t")
    after = repo.commit_regenerate("id", GenerationResult("новый 3.1E", "v3.1E", "p", {}, 0, ""), "t", source="v31e")
    assert after["answer_versions"][-1]["source"] == "v31e" and answer_lineage(after) == "v31"


def test_auto_publish_stays_off():
    from app.v3.registry import load_policy
    assert load_policy()["runtime"]["auto_publish"] is False
