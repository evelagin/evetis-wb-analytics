"""Integration: message -> classifier(+LLM) -> planner -> generator(LLM) -> verifier."""
from __future__ import annotations

from app.domain.exceptions import OpenAIError
from app.v3.engine import V3Engine
from tests.v3.conftest import FakeV3LLM


def _engine(snap, llm):
    return V3Engine(snap, llm_factory=lambda: llm)


def _q(nm, text, **kw):
    return {"communication_id": "c1", "entity_type": kw.pop("entity", "question"), "nm_id": nm, "text": text, **kw}


def test_good_grounded_answer_passes(snap):
    llm = FakeV3LLM(answer="В составе сыворотки салициловая кислота — 1% по спецификации производителя.")
    d, stats = _engine(snap, llm).decide(_q("305101361", "Какая концентрация салициловой кислоты?"))
    assert d.plan.strategy == "FACT_ANSWER" and d.final_outcome == "FACT_ANSWER", d.verification.to_dict()
    assert stats.calls == 2  # one classifier + one generator call, no duplicates


def test_generator_payload_is_structured_evidence_not_a_kb_dump(snap):
    seen = {}

    def answer(payload):
        seen.update(payload)
        return "Салициловая кислота в сыворотке — 1%."
    llm = FakeV3LLM(answer=answer)
    _engine(snap, llm).decide(_q("305101361", "Какая концентрация салициловой кислоты?"))
    assert seen["strategy"] == "FACT_ANSWER" and seen["approved_guidance"] == []
    blob = str(seen)
    assert "6.94" not in blob and "Lost Cherry" not in blob and "ниацинамид" not in blob.lower()
    assert all("customer_value" in f and f["fact_ids"] for f in seen["allowed_facts"])


def test_restricted_value_never_reaches_the_generator(snap):
    seen = {}
    llm = FakeV3LLM(answer=lambda p: seen.update(p) or "В составе крема есть салициловая кислота.")
    d, _ = _engine(snap, llm).decide(_q("438775617", "Сколько процентов салициловой кислоты?"))
    assert "2.25" not in str(seen) and "2,25" not in str(seen)
    assert d.final_outcome == "FACT_ANSWER", d.verification.to_dict()


def test_model_leaking_2_25_is_blocked(snap):
    llm = FakeV3LLM(answer="В составе есть салициловая кислота. Её 2,25%, срок годности — 2 года с даты изготовления.")
    d, _ = _engine(snap, llm).decide(_q("438775617", "Сколько процентов салициловой кислоты и какой срок годности?"))
    assert d.generation.mode == "llm"
    assert d.final_outcome == "BLOCK" and "V-RESTRICTED" in d.verification.rule_ids()


def test_mixed_answer_keeps_abstraction_verbatim(snap):
    llm = FakeV3LLM(answer="В составе есть салициловая кислота. Срок годности — 2 года с даты изготовления.")
    d, _ = _engine(snap, llm).decide(_q("438775617", "Сколько процентов салициловой кислоты и какой срок годности?"))
    assert d.final_outcome == "FACT_ANSWER", d.verification.to_dict()
    llm2 = FakeV3LLM(answer="Салициловая кислота присутствует. Срок годности — 2 года с даты изготовления.")
    d2, _ = _engine(snap, llm2).decide(_q("438775617", "Сколько процентов салициловой кислоты и какой срок годности?"))
    assert d2.final_outcome == "BLOCK" and "V-TEMPLATE" in d2.verification.rule_ids()


def test_model_adding_general_advice_is_blocked(snap):
    llm = FakeV3LLM(answer="Салициловая кислота — 1%. Начните с 1 раза в день, лучше вечером, и используйте SPF.")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Какая концентрация салициловой кислоты?"))
    assert d.final_outcome == "BLOCK"
    assert {"V-GENERAL", "V-NUM"} <= set(d.verification.rule_ids())


def test_model_adding_unasked_facts_is_blocked(snap):
    llm = FakeV3LLM(answer="Салициловая кислота — 1%. Ещё в составе ниацинамид 5% и масло чайного дерева, сделано в Китае.")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Какая концентрация салициловой кислоты?"))
    assert d.final_outcome == "BLOCK"
    assert {"V-FACT", "V-UNKNOWN"} <= set(d.verification.rule_ids())


def test_unknown_converted_into_assertion_is_blocked(snap):
    llm = FakeV3LLM(answer="Пользуйтесь дважды в день, утром и вечером.")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Как часто пользоваться сывороткой?"))
    assert d.plan.strategy == "UNKNOWN_FACT" and d.final_outcome == "BLOCK"
    assert "V-GENERAL" in d.verification.rule_ids() or "V-UNKNOWN" in d.verification.rule_ids()


def test_generation_error_is_explicit(snap):
    llm = FakeV3LLM(generate_error=OpenAIError("boom"))
    d, _ = _engine(snap, llm).decide(_q("305101361", "Какая страна производства?"))
    assert d.final_outcome == "HUMAN_REVIEW" and d.failure_code == "GENERATION_ERROR" and d.draft is None


def test_classifier_llm_can_raise_but_never_lower_risk(snap):
    # LLM wrongly says the irritation is negated/resolved -> rules keep it active (S2)
    llm = FakeV3LLM(classification={"safety_events": [{
        "type": "irritation", "evidence_span": "x", "negated": True, "temporal_state": "historical_absent",
        "certainty": "asserted", "severity": "unknown", "persistence": "none", "worsening": False,
        "body_location": "face", "confidence": 0.99}]}, answer="x")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Раздражение не проходит уже неделю", entity="review", rating=5))
    assert d.plan.risk_level == "R3" and d.classification.safety.route == "S2"
    # LLM adds an event the rules missed -> risk goes up
    llm2 = FakeV3LLM(classification={"safety_events": [{
        "type": "redness", "evidence_span": "кожа горит", "negated": False, "temporal_state": "current",
        "certainty": "asserted", "severity": "unknown", "persistence": "none", "worsening": False,
        "body_location": "face", "confidence": 0.9}]}, answer="x")
    d2, _ = _engine(snap, llm2).decide(_q("305101361", "Кожа как будто горит после сыворотки", entity="review", rating=5))
    assert d2.plan.risk_level == "R3" and d2.plan.strategy == "SAFETY_TEMPLATE"


def test_llm_cannot_declare_r4_without_marker(snap):
    llm = FakeV3LLM(classification={"safety_events": [{
        "type": "irritation", "evidence_span": "x", "negated": False, "temporal_state": "current",
        "certainty": "asserted", "severity": "severe", "persistence": "persistent", "worsening": True,
        "body_location": "face", "confidence": 0.9}]}, answer="x")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Очень плохо стало с кожей", entity="review", rating=1))
    assert d.plan.risk_level == "R3" and not d.classification.safety.emergency_markers


def test_classifier_llm_failure_keeps_rules(snap):
    llm = FakeV3LLM(classify_error=RuntimeError("x"), answer="Страна производства — Китай.")
    d, _ = _engine(snap, llm).decide(_q("305101361", "Какая страна производства?"))
    assert d.classification.llm_error == "RuntimeError" and d.final_outcome == "FACT_ANSWER"


def test_v2_text_checked_by_same_verifier(snap):
    llm = FakeV3LLM(answer="Страна производства — Китай.")
    msg = _q("438775617", "Какая страна производства?",
             v2_ai_answer="Крем с салициловой кислотой 2,25%, начните с 1 раза в день вечером.")
    d, _ = _engine(snap, llm).decide(msg)
    rules = set(d.v2_checks["v2_ai_answer"]["block_rules"])
    assert d.v2_checks["v2_ai_answer"]["verdict"] == "BLOCK" and {"V-RESTRICTED", "V-GENERAL"} <= rules


def test_restricted_abstraction_is_rendered_without_llm(snap):
    llm = FakeV3LLM(answer="Точный процент не указан.")
    d, _ = _engine(snap, llm).decide(_q("438775617", "Сколько процентов салициловой кислоты в креме?"))
    assert d.generation.mode == "deterministic" and d.draft == "В составе есть салициловая кислота."
    assert d.final_outcome == "FACT_ANSWER" and not [c for c in llm.calls if c[0] == "generator"]
