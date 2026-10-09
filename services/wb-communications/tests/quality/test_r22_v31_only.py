"""R2.2: v3.1E is the only normal operator answer. No V2 promotion, no V2 button; a bounded
safe information gap (restock) is answered honestly; every operator text is published under
the v3.1E policy (v31_human_safety for serious safety). Verified publisher unchanged."""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.pipeline import handle_update, publication_mode_for, run_poll
from app.v3 import pilot
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import (  # noqa: F401 — policies is a fixture
    V2_TEXT, FixedV2, buttons, doc_of, fb, last_trace, policies, tap)
from tests.quality.test_r21_primary_operator import LIVE_CASE, SET_NM, anna, card, edit

RESTOCK = ("Здравствуйте! Точную дату следующего поступления крема пока подтвердить не можем. "
           "Рекомендуем следить за наличием в карточке товара на Wildberries. Спасибо за интерес к EVETIS!")
SAFE_OPERATOR_TEXT = "Здравствуйте! Спасибо за вопрос, мы ответим подробнее в ближайшее время."


def only(feedback=None, **extra):
    extra.setdefault("openai", FixedV2())
    d = make_deps([feedback or fb()], v31_only_operator_enabled=True, **extra)
    d.publication_validator = None
    return d


def question(text, nm=438775437, monkeypatch=None, v2="Крем появится через неделю, он лечит акне."):
    import app.services.pipeline as pl
    if monkeypatch:
        monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": "2026-10-08T13:00:00Z", "text": text,
         "productDetails": {"productName": "Крем для лица", "supplierArticle": str(nm), "nmId": nm,
                            "imtId": 1, "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2(v2), v31_only_operator_enabled=True)
    d.publication_validator = None
    return d


def published_questions(d):
    return [t for _, t, _ in d.wb.published_questions]


# flag ---------------------------------------------------------------------------------------
def test_flag_default_off_and_interlock(monkeypatch):
    monkeypatch.delenv("V31_ONLY_OPERATOR_ENABLED", raising=False)
    assert Settings().v31_only_operator_enabled is False
    s = SimpleNamespace(v31_quality_shadow_enabled=True, v31_only_operator_enabled=True)
    assert pilot.activation(s) == (None, pilot.OPERATOR_SURFACES_ON)
    d = make_deps([fb()], openai=FixedV2(), v31_primary_operator_enabled=True)     # R2.1 unchanged
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["operator_mode"] == "v31_primary" and f"sv2:{doc_id}:{doc['generation_number']}" in buttons(d)


# normal review ------------------------------------------------------------------------------
def test_normal_review_v31_only_card_and_verified_publish(policies):
    d = only()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert doc["operator_mode"] == "v31_only" and doc["final_answer"] == doc["v31_draft"]["text"]
    assert "Рекомендуемый ответ 3.1E" in card(d) and V2_TEXT not in card(d)
    assert buttons(d) == [f"pub:{doc_id}:{gen}", f"edit:{doc_id}", f"regen:{doc_id}", f"skip:{doc_id}"]
    assert d.wb.published == []
    assert tap(d, f"pub:{doc_id}:{gen - 1}")["status"] == "stale"
    assert tap(d, f"pub:{doc_id}:{gen}", n=2)["status"] == "published"
    assert tap(d, f"pub:{doc_id}:{gen}", n=3)["status"] == "stale"
    assert [t for _, t in d.wb.published] == [doc["final_answer"]]
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []


def test_manual_edit_of_normal_answer_is_v31(policies):
    d = only()
    run_poll(d)
    text = "Людмила, спасибо за отзыв! Рады, что крем понравился."
    doc = edit(d, text)
    doc_id = doc_of(d)[0]
    assert "Ответ оператора</b> (правила 3.1E)" in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=30)["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []


def test_v2_is_never_offered_or_adoptable():
    d = only()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert not any(c.startswith(("sv2:", "u2:")) for c in buttons(d))
    assert tap(d, f"sv2:{doc_id}:{gen}")["status"] == "stale"
    assert tap(d, f"u2:{doc_id}:{gen}", n=2)["status"] == "stale"
    assert d.repo.get(doc_id)["final_answer"] != V2_TEXT and not d.repo.get(doc_id).get("ai_answer")


# questions ----------------------------------------------------------------------------------
def test_live_restock_question_gets_safe_answer_and_publishes(monkeypatch, policies):
    d = question("Здравствуйте когда появится крем 438775437 ?", monkeypatch=monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "READY" and doc["v31_draft"]["route"] == "SAFE_INFORMATION_GAP"
    assert doc["final_answer"] == RESTOCK and not doc.get("ai_answer")              # V2 never ran
    assert "Рекомендуемый ответ 3.1E" in card(d) and "через неделю" not in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert published_questions(d) == [RESTOCK] and last_trace(d)["publication_mode"] == "v31"
    assert policies.live.calls == []


@pytest.mark.parametrize("text, answer", [
    ("Сколько нажатий нужно на одно применение?", "Подтверждённое количество нажатий в доступной документации не указано."),
    ("Как часто можно использовать крем?", "Подтверждённая частота применения в документации не указана."),
])
def test_unknown_amount_and_frequency_questions(monkeypatch, text, answer):
    d = question(text, nm=252442517, monkeypatch=monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["final_answer"] == answer
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert published_questions(d) == [answer]


def test_factual_question(monkeypatch):
    d = question("Какой аромат у набора?", nm=SET_NM, monkeypatch=monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "READY" and "Amber Vanilla" in doc["final_answer"]
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"


def test_mixed_restock_and_return_question_stays_human(monkeypatch, policies):
    d = question("Когда появится? И можно ли вернуть?", monkeypatch=monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "HUMAN_REVIEW" and "Нужна проверка человеком" in card(d)
    assert "через неделю" not in card(d)                                           # V2 never shown
    assert buttons(d) == [f"edit:{doc_id}", f"skip:{doc_id}"]
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "no_v31_answer"
    assert published_questions(d) == [] and policies.live.calls == []


# human review (legal / authenticity): operator text under v3.1E -------------------------------
def test_authenticity_question_needs_operator_text_under_v31(monkeypatch, policies):
    d = question("Это оригинал?", monkeypatch=monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["human_reason"] == "HUMAN_REVIEW_DOMAIN"
    assert "подлинность" in card(d) and buttons(d) == [f"edit:{doc_id}", f"skip:{doc_id}"]
    doc = edit(d, SAFE_OPERATOR_TEXT)
    assert publication_mode_for(doc) == "v31"
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=40)["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []
    assert published_questions(d) == [SAFE_OPERATOR_TEXT]


# technical failure: no V2 lottery ------------------------------------------------------------
def test_technical_failure_offers_retry_write_skip_never_v2(monkeypatch, policies):
    import app.response_quality.core as core
    real = core.prepare
    monkeypatch.setattr(core, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = only()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert doc["v31_draft"]["status"] == "ERROR" and "Не удалось подготовить ответ 3.1E" in card(d)
    assert V2_TEXT not in card(d)
    assert buttons(d) == [f"regen:{doc_id}", f"edit:{doc_id}", f"skip:{doc_id}"]
    assert tap(d, f"pub:{doc_id}:{gen}")["status"] == "no_v31_answer" and d.wb.published == []
    # retry while still failing: no V2 regenerated or promoted, nothing changes
    assert tap(d, f"regen:{doc_id}", n=2)["status"] == "regen_no_v31"
    after = d.repo.get(doc_id)
    assert after["generation_number"] == gen and after["status"] == "pending_approval"
    assert not after.get("answer_versions") and not after.get("final_answer")
    # retry after recovery: the 3.1E answer becomes active and publishable
    monkeypatch.setattr(core, "prepare", real)
    assert tap(d, f"regen:{doc_id}", n=3)["status"] == "regenerated"
    after = d.repo.get(doc_id)
    assert after["answer_versions"][-1]["source"] == "v31e"
    assert tap(d, f"pub:{doc_id}:{after['generation_number']}", n=4)["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []


# safety ---------------------------------------------------------------------------------------
def test_moderate_safety_stays_v31(policies):
    d = only(feedback=anna())
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["attention"] == "MODERATE_DISCOMFORT" and "Требует внимания оператора" in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []


def test_serious_safety_operator_text_is_v31_human_safety(policies):
    d = only(feedback=fb(text="После крема опух язык и тяжело дышать", rating=1))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["restriction"] == "SERIOUS_SAFETY" and buttons(d) == [f"edit:{doc_id}", f"skip:{doc_id}"]
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "human_review_required"
    text = "Ольга, нам очень жаль. Пожалуйста, прекратите использование крема."
    doc = edit(d, text)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=40)["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31_human_safety" and policies.live.calls == []
    assert [t for _, t in d.wb.published] == [text]


# engine ---------------------------------------------------------------------------------------
def test_engine_safe_gap_only_when_asked_and_bounded():
    from app.response_quality.core import prepare
    from app.v3.snapshot import load_snapshot
    snap = load_snapshot(None)

    def run(text, nm="438775437", gaps=True):
        return prepare({"text": text, "nm_id": nm, "supplier_article": nm, "entity_type": "question"},
                       "", snap, force_generation=True, safe_gaps=gaps)
    assert run("Когда появится крем?", gaps=False).status == "HUMAN_REVIEW"              # R2.1 unchanged
    r = run("Здравствуйте когда появится крем 438775437 ?")
    assert r.status == "READY" and r.text == RESTOCK and r.final_policy["verdict"] == "PASS"
    assert run("Когда снова будет в продаже?").text == RESTOCK                          # lexicon gap
    assert "набора" in run("Будет ли набор в наличии?", nm=str(SET_NM)).text
    for text in ("Когда появится? И можно ли вернуть?", "Когда поступит и подойдёт ли для сухой кожи?",
                 "Это оригинал?", "Подойдёт ли крем мужчине?"):
        assert run(text).status == "HUMAN_REVIEW", text
    review = prepare({"text": "Когда появится крем? Очень жду", "nm_id": "438775437", "supplier_article": "438775437",
                      "entity_type": "review", "rating": 5}, "", snap, force_generation=True, safe_gaps=True)
    assert review.plan.route != "SAFE_INFORMATION_GAP"                                   # questions only


def test_live_anna_case_unchanged_under_r22():
    from app.response_quality.core import prepare
    from app.v3.snapshot import load_snapshot
    msg = {"text": LIVE_CASE, "nm_id": str(SET_NM), "supplier_article": "Набор вишня+амбра", "rating": 2,
           "buyer_name": "Анна", "entity_type": "review"}
    r = prepare(msg, V2_TEXT, load_snapshot(None), force_generation=True, moderate_safety=True, safe_gaps=True)
    assert r.status == "READY" and r.plan.route == "MODERATE_DISCOMFORT"


# R2.2 blocker: V2 is not in the critical path -------------------------------------------------
class CountingV2:
    """A V2 generator that records every call and can fail."""
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def generate_answer(self, *a):
        from app.domain.models import GenerationResult
        self.calls += 1
        if self.fail:
            raise RuntimeError("V2 generator down")
        return GenerationResult(V2_TEXT, "fake-v2", "reviews_v1", {}, 0, "")


def v2_prompt_calls(d, monkeypatch, fail=False):
    counter = {"n": 0}
    if d.engine is not None:
        real = d.engine.build_prompt

        def build_prompt(*a, **k):
            counter["n"] += 1
            if fail:
                raise RuntimeError("V2 prompt builder down")
            return real(*a, **k)
        monkeypatch.setattr(d.engine, "build_prompt", build_prompt)
    return counter


@pytest.mark.parametrize("fail", [False, True])
def test_review_never_calls_v2_and_survives_v2_failure(monkeypatch, policies, fail):
    v2 = CountingV2(fail=fail)
    d = only(openai=v2, communication_engine_v2_primary=True)
    prompts = v2_prompt_calls(d, monkeypatch, fail=fail)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert v2.calls == 0 and prompts["n"] == 0
    assert doc["status"] == "pending_approval" and doc["v31_draft"]["status"] == "READY"
    assert "Рекомендуемый ответ 3.1E" in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and policies.live.calls == []
    assert v2.calls == 0 and prompts["n"] == 0


@pytest.mark.parametrize("fail", [False, True])
def test_question_never_calls_v2_and_survives_prompt_builder_failure(monkeypatch, policies, fail):
    d = question("Здравствуйте когда появится крем 438775437 ?", monkeypatch=monkeypatch)
    d.openai = v2 = CountingV2(fail=fail)
    prompts = v2_prompt_calls(d, monkeypatch, fail=fail)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert v2.calls == 0 and prompts["n"] == 0 and doc["final_answer"] == RESTOCK
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert published_questions(d) == [RESTOCK] and policies.live.calls == []


def test_v31_failure_card_without_any_v2_generation(monkeypatch):
    import app.response_quality.core as core
    monkeypatch.setattr(core, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    v2 = CountingV2()
    d = only(openai=v2, communication_engine_v2_primary=True)
    prompts = v2_prompt_calls(d, monkeypatch)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["status"] == "pending_approval" and "Не удалось подготовить ответ 3.1E" in card(d)
    assert buttons(d) == [f"regen:{doc_id}", f"edit:{doc_id}", f"skip:{doc_id}"]
    assert tap(d, f"regen:{doc_id}", n=2)["status"] == "regen_no_v31"
    edit(d, "Людмила, спасибо за отзыв!", n=10)                       # manual text: no V2 validators
    assert "валидатор" not in card(d)
    assert v2.calls == 0 and prompts["n"] == 0


def test_flag_off_r21_still_uses_the_v2_fallback(monkeypatch, policies):
    import app.response_quality.core as core
    monkeypatch.setattr(core, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    v2 = CountingV2()
    d = make_deps([fb()], openai=v2, v31_primary_operator_enabled=True)
    d.publication_validator = None
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert v2.calls == 1 and doc["operator_mode"] == "v31_primary" and doc["final_answer"] == V2_TEXT
    assert "Резервный ответ V2" in card(d)
    tap(d, f"pub:{doc_id}:{doc['generation_number']}")
    assert last_trace(d)["publication_mode"] == "live_v2" and policies.live.calls == [V2_TEXT]
