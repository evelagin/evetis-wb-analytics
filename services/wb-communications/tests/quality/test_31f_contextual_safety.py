"""3.1F — contextual review understanding and calibrated safety routing.

Live case 10.10.2026 (doc bc515c5fe29e3f9b6173, enzyme powder 535580776): the rules lexicon
pattern «высып» matched the verb «высыпаю» (to pour) → rash → S1 → the stop-use / doctor template
on a 5★ review about a dispenser. Replay of 4 363 real WB reviews: 225 safety routes, most of
them praise («убирает покраснения», «меньше высыпаний», «аллергии не вызывает»)."""
import re

import pytest

from app.response_quality.core import make_plan, prepare
from app.response_quality.generation_contract import invented_safety_context
from app.v3 import safety
from app.v3.classifier import classify_rules
from app.v3.planner import plan as hard_plan
from app.v3.resolver import resolve
from app.v3.snapshot import load_snapshot

SNAP = load_snapshot()
POL = SNAP.policy
DOCTOR = re.compile(r"врач|дерматолог|аллерголог")
MARINA = {"communication_id": "bc515c5fe29e3f9b6173", "entity_type": "review", "nm_id": "535580776", "text": "",
          "pros": "Понравилась по качеству, приятная нежная текстура - пенка",
          "cons": ("Внутренний дозатор выняла, выкинула и только тогда можно нормально пользоваться не вдыхая "
                   "мелкие распылленные частички из воздуха, просто высыпаю горстку комочков на влажные руки "
                   "и вспениваю между ладонями."), "tags": [], "rating": 5, "buyer_name": "Марина"}
GOOD_MIXED = ("Марина, рады, что Вам понравились качество пудры и нежная пенка. Жаль, что внутренний дозатор "
              "оказался неудобным и при использовании поднимаются мелкие частички. Спасибо за такой подробный отзыв!")


def review(text, rating=3, nm="535581674", **kw):
    return {"entity_type": "review", "nm_id": nm, "text": text, "pros": "", "cons": "", "tags": [], "rating": rating,
            "buyer_name": "Анна", **kw}


def route_of(m):
    return safety.assess(" ".join(m.get(k) or "" for k in ("text", "pros", "cons")), POL).route


# --- exact Marina case ------------------------------------------------------------------------------
def test_marina_is_not_a_safety_case():
    cls = classify_rules(MARINA, SNAP)
    assert cls.safety.route is None and not [e for e in cls.safety.events if e.active]
    hard = hard_plan(SNAP, resolve(SNAP, nm_id=MARINA["nm_id"]), cls, MARINA)
    assert hard.strategy not in ("SAFETY_TEMPLATE", "HUMAN_REVIEW")


def test_marina_understanding_is_aspect_first():
    u = make_plan(MARINA, SNAP).understanding
    assert {"product_liked", "texture"} <= set(u["positive_aspects"])
    assert "dispenser_inconvenient" in u["negative_aspects"]
    assert u["actual_safety_symptoms"] == [] and u["safety_route"] is None
    assert u["exposure_or_avoidance_context"] and u["exposure_only"] is True
    assert u["customer_workaround"]
    assert any(c["type"] == "rash" and c["role"] == "not_a_symptom" for c in u["safety_context_not_symptoms"])


def test_marina_deterministic_draft_is_mixed_and_contextual():
    r = prepare(MARINA, "", SNAP, force_generation=True, moderate_safety=True, safe_gaps=True)
    t = r.text.lower()
    assert r.status == "READY" and r.plan.route not in ("SAFETY_TEMPLATE", "HUMAN_REVIEW", "MILD_REACTION")
    assert re.search(r"качеств|текстур|пенк", t) and "дозатор" in t
    assert not DOCTOR.search(t) and "прекрат" not in t and "ощущения" not in t and "раздраж" not in t
    assert "расскажите" not in t                         # she already explained everything
    assert "выньте" not in t and "высыпайте" not in t    # the workaround is never endorsed


def test_marina_gpt_mixed_answer_is_accepted():
    r = prepare(MARINA, "", SNAP, render=lambda *_a, **_k: GOOD_MIXED, force_generation=True, moderate_safety=True)
    assert r.status == "READY" and r.text == GOOD_MIXED and r.final_policy["verdict"] != "BLOCK"


@pytest.mark.parametrize("bad", [
    "Марина, нам очень жаль, что после применения средства появились такие ощущения. Рекомендуем прекратить использование.",
    "Марина, спасибо! Если появится раздражение, обратитесь к дерматологу.",
    "Марина, рады, что пудра понравилась. Не переживайте, частички безопасны для дыхания.",
])
def test_marina_invented_safety_context_is_rejected(bad):
    p = make_plan(MARINA, SNAP)
    assert invented_safety_context(bad, p)
    from app.response_quality.generation_contract import GenerationContractError

    def render(*_a, **_k):
        raise GenerationContractError(invented_safety_context(bad, p))
    r = prepare(MARINA, "", SNAP, render=render, force_generation=True, moderate_safety=True)
    assert r.text and not DOCTOR.search(r.text.lower()) and "ощущения" not in r.text


# --- exposure is not an event; actual symptoms stay safety ----------------------------------------------
@pytest.mark.parametrize("text", ["Пудра пылит, стараюсь не вдыхать", "Мелкие частички летят в воздухе при открытии",
                                  "Пылит при открытии, приходится аккуратно"])
def test_exposure_only_is_not_a_breathing_event(text):
    assert route_of(review(text)) is None
    assert safety.exposure_only(text)


def test_actual_breathing_is_serious_human_review():
    m = review("После пудры стало трудно дышать", rating=1, nm="535580776")
    a = safety.assess(m["text"], POL)
    assert a.route == "SAFETY_URGENT" and "EM-01" in a.emergency_markers
    assert make_plan(m, SNAP).human_reason == "SAFETY_ESCALATION"


def test_actual_mild_skin_reaction_gets_attention_draft_without_doctor():
    m = review("После средства появилось лёгкое раздражение", rating=3)
    p = make_plan(m, SNAP, moderate_safety=True)
    assert p.route == "MILD_REACTION" and p.attention == "MILD_REACTION"
    assert "дискомфорт" in p.direct_answer and not DOCTOR.search(p.direct_answer.lower())
    assert "адаптац" not in p.direct_answer and "причин" not in p.direct_answer


def test_persistent_reaction_goes_to_a_human_not_a_machine_template():
    m = review("Покраснение не проходит уже неделю", rating=1)
    p = make_plan(m, SNAP, moderate_safety=True)
    assert p.human_reason == "SAFETY_ESCALATION" and p.direct_answer is None


def test_mixed_positive_and_packaging_both_addressed():
    m = review("Крем отличный, приятная текстура, но дозатор неудобный — выдавливается с трудом", rating=4)
    r = prepare(m, "", SNAP, force_generation=True, moderate_safety=True)
    t = r.text.lower()
    assert r.status == "READY" and "текстур" in t and "дозатор" in t


# --- calibration of the rules lexicon on real wording -------------------------------------------------
@pytest.mark.parametrize("text", [
    "Отличный тоник, избавляет от высыпаний", "Покраснения стали заметно меньше", "Убирает шелушения",
    "Сыворотка увлажняет, высыпаний и аллергии после нее нет", "Не вызывает раздражения", "Аллергию не вызывает",
    "Как для аллергика это жирный плюс", "После неё прошли высыпания", "Новые высыпания появляются реже",
    "Высыпаю немного пудры на ладонь", "Пятна от постакне посветлели", "Запах раздражающий, резкий",
    "Не всегда высыпаюсь, а кожа выглядит отдохнувшей", "Очень большой расход",
])
def test_praise_profile_negation_and_lookalikes_are_not_safety(text):
    assert route_of(review(text, rating=5)) is None


@pytest.mark.parametrize("text,expected", [
    ("После сыворотки лицо обсыпало прыщами", "S1"), ("Не помог, немного жжет кожу", "S1"),
    ("При использовании появился зуд", "S1"), ("Жжение прошло", "S1R"), ("Раздражение не проходит", "S2"),
    ("Крем вызывает аллергию, покраснения, жжение", "S1"), ("Не могу сказать, что раздражения совсем не было", "HUMAN_REVIEW"),
    ("После умывания появляются ожоги на лице", "HUMAN_REVIEW"), ("Начался сильный отёк губ", "SAFETY_URGENT"),
])
def test_real_reactions_keep_their_routes(text, expected):
    assert route_of(review(text, rating=1)) == expected


# --- LLM calibration ------------------------------------------------------------------------------------
def _llm_event(etype, span):
    return [{"type": etype, "evidence_span": span, "negated": False, "temporal_state": "current", "certainty": "asserted",
             "severity": "unknown", "persistence": "none", "worsening": False, "body_location": "face", "confidence": 0.8}]


def test_llm_only_speculative_skin_event_does_not_route():
    text = MARINA["pros"] + "\n" + MARINA["cons"]
    base = safety.assess(text, POL)
    merged = safety.merge_llm_events(base, _llm_event("irritation", "не вдыхая мелкие распылленные частички"), text, POL)
    assert merged.route is None and merged.possible_context and merged.possible_context[0]["type"] == "irritation"


def test_llm_exposure_only_breathing_event_does_not_route():
    text = "Пудра пылит, стараюсь не вдыхать"
    merged = safety.merge_llm_events(safety.assess(text, POL), _llm_event("breathing_problem", "стараюсь не вдыхать"), text, POL)
    assert merged.route is None and merged.possible_context[0]["reason"] == "EXPOSURE_ONLY"


def test_llm_backed_by_customer_sensation_still_routes():
    text = "Кожа как будто горит после сыворотки"
    merged = safety.merge_llm_events(safety.assess(text, POL), _llm_event("burning", "кожа горит"), text, POL)
    assert merged.route == "S1"


def test_llm_cannot_lower_a_serious_rules_event():
    text = "После пудры стало трудно дышать"
    merged = safety.merge_llm_events(safety.assess(text, POL), [{**_llm_event("breathing_problem", "x")[0], "negated": True}], text, POL)
    assert merged.route == "SAFETY_URGENT"


# --- no medical escalation in any machine review draft ------------------------------------------------
@pytest.mark.parametrize("text", ["Кожа немного щиплет", "Жжение прошло", "После крема покраснение и зуд на руках",
                                  "Крем вызывает аллергию", "Отличный крем, никакого раздражения"])
def test_machine_review_drafts_never_send_to_a_doctor(text):
    r = prepare(review(text, rating=2), "", SNAP, force_generation=True, moderate_safety=True)
    assert r.text is None or not DOCTOR.search(r.text.lower())


def test_human_written_text_for_severe_reaction_not_forced_to_doctor_template():
    from types import SimpleNamespace
    from app.services.publication_policy import validate_for_publication, validate_human_safety_publication
    m = review("Покраснение не проходит уже неделю", rating=1)
    text = "Анна, нам очень жаль. Спасибо, что написали, — мы передали Ваш отзыв команде."
    st = SimpleNamespace(v3_knowledge_snapshot_id=SNAP.snapshot_id)
    ordinary = validate_for_publication(text, m, st)
    assert any(v["rule_id"] == "V-SAFETY-ROUTE" for v in ordinary.get("violations", []))   # machine path: still blocked
    human = validate_human_safety_publication(text, m, st)
    assert not any(v["rule_id"] == "V-SAFETY-ROUTE" for v in human.get("violations", []))  # human wording is free
    claim = validate_human_safety_publication(text + " Крем лечит дерматит.", m, st)
    assert claim["verdict"] == "BLOCK"                                                        # every other check stays


def test_marina_is_not_asked_what_she_already_explained():
    p = make_plan(MARINA, SNAP)
    assert not [c for c in p.clarifications if c["unknown"] == "specific_difficulty"]


def test_blocked_model_candidate_on_a_new_card_falls_back_to_gated_acknowledgement():
    m = review("Отличный тоник. Хорошо увлажняет кожу лица. Избавляет от высыпаний.", rating=5)
    claim = "Анна, тоник лечит высыпания и восстанавливает кожу."           # V-CLAIM / V-MEDICAL
    r = prepare(m, "", SNAP, render=lambda *_a, **_k: claim, force_generation=True, moderate_safety=True)
    assert r.status == "READY" and r.text and r.text != claim and r.final_policy["verdict"] != "BLOCK"
    assert "CUSTOMER_SIGNALS_SAFE_FALLBACK" in r.reasons and not DOCTOR.search(r.text.lower())


def test_pipeline_persists_understanding_and_never_safety_routes_marina():
    from app.services.pipeline import run_poll
    from tests.conftest import make_deps
    from tests.quality.test_r2_operator_draft import FixedV2, fb
    f = fb(text="", nm=535580776)
    f.update(pros=MARINA["pros"], cons=MARINA["cons"], userName="Марина")
    d = make_deps([f], openai=FixedV2(), v31_operator_draft_enabled=True, v31_primary_operator_enabled=True,
                  v31_only_operator_enabled=True)
    d.publication_validator = None
    run_poll(d)
    draft = next(iter(d.repo.docs.values()))["v31_draft"]
    assert draft["route"] not in ("SAFETY_TEMPLATE", "HUMAN_REVIEW") and draft["attention"] is None
    assert draft["understanding"]["actual_safety_symptoms"] == []
    assert "dispenser_inconvenient" in draft["understanding"]["negative_aspects"]
    assert not DOCTOR.search((draft["text"] or "").lower()) and d.wb.published == []
