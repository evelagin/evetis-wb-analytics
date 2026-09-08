from __future__ import annotations

from app.communication_engine.config import default_config
from app.communication_engine.constants import Marketplace, Sentiment
from app.communication_engine.models.classification import Classification, ReviewInput
from app.communication_engine.models.prompt_context import PromptContext
from app.communication_engine.validators.base import ValidationInput
from app.communication_engine.validators.duplicate_validator import DuplicateValidator
from app.communication_engine.validators.hallucination_validator import HallucinationValidator
from app.communication_engine.validators.length_validator import LengthValidator
from app.communication_engine.validators.medical_validator import MedicalValidator
from app.communication_engine.validators.tone_validator import ToneValidator


def _ctx(engine, **kw) -> PromptContext:
    return engine.build_context(ReviewInput(**kw))


def _bare_ctx(product_id=None, sentiment=Sentiment.NEUTRAL) -> PromptContext:
    return PromptContext(
        classification=Classification(marketplace=Marketplace.WB, product_id=product_id,
                                      sentiment=sentiment)
    )


# --- tone ---
def test_tone_forbidden_phrase_and_opener(engine, registry, config):
    v = ToneValidator(registry, config)
    issues = v.validate(ValidationInput(answer="Спасибо за отзыв, всё отлично", context=_bare_ctx()))
    kinds = {i.message for i in issues}
    assert "forbidden phrase" in kinds
    assert "forbidden opener" in kinds


def test_tone_clean_answer(engine, registry, config):
    v = ToneValidator(registry, config)
    assert v.validate(ValidationInput(answer="Анна, состав работает мягко.", context=_bare_ctx())) == []


# --- medical ---
def test_medical_claim_flagged(registry, config):
    v = MedicalValidator(registry, config)
    issues = v.validate(ValidationInput(answer="Этот крем лечит акне за неделю", context=_bare_ctx()))
    assert any(i.message == "medical claim" for i in issues)


# --- length ---
def test_length_empty_and_over_hard_and_soft():
    v = LengthValidator(default_config())
    assert v.validate(ValidationInput(answer="   ", context=_bare_ctx()))[0].message == "empty answer"

    over_hard = v.validate(ValidationInput(answer="я" * 1001, context=_bare_ctx()))
    assert any(i.message == "over WB hard limit" for i in over_hard)

    soft = v.validate(ValidationInput(answer="я" * 600, context=_bare_ctx(sentiment=Sentiment.POSITIVE)))
    assert any(i.message == "over soft length limit" for i in soft)
    assert all(i.severity.value == "warning" for i in soft)  # soft is only a warning


# --- duplicate ---
def test_duplicate_detects_similar():
    v = DuplicateValidator(default_config())
    prev = "Анна, витамин С в составе даёт сияние через 4–6 недель регулярного ухода."
    same = prev
    assert v.validate(ValidationInput(answer=same, context=_bare_ctx(), recent_answers=(prev,)))
    assert v.validate(ValidationInput(answer="Совсем другой ответ про текстуру.",
                                      context=_bare_ctx(), recent_answers=(prev,))) == []


def test_duplicate_empty_answer_is_ignored():
    v = DuplicateValidator(default_config())
    assert v.validate(ValidationInput(answer="   ", context=_bare_ctx(), recent_answers=("x",))) == []


def test_pipeline_exposes_validators(engine):
    assert len(engine.pipeline.validators) == 6


# --- hallucination ---
def test_hallucination_cross_product_ingredient(engine, registry):
    v = HallucinationValidator(registry)
    ctx = _ctx(engine, rating=5, supplier_article="305101361")  # acne_serum
    # папаин belongs to enzyme_powder, not acne_serum -> flagged
    flagged = v.validate(ValidationInput(answer="Папаин мягко очищает кожу", context=ctx))
    assert any(i.message == "ingredient not in this product" for i in flagged)
    # ниацинамид IS in acne_serum -> allowed
    assert v.validate(ValidationInput(answer="Ниацинамид работает постепенно", context=ctx)) == []


def test_hallucination_skipped_when_product_unknown(registry):
    v = HallucinationValidator(registry)
    assert v.validate(ValidationInput(answer="Папаин и церамиды", context=_bare_ctx())) == []


# --- pipeline via engine.validate ---
def test_pipeline_aggregates_and_ok_flag(engine):
    ctx = _ctx(engine, rating=5, supplier_article="535580776")
    bad = engine.validate("Спасибо за отзыв! Крем лечит акне.", ctx)
    assert not bad.ok
    assert len(bad.errors) >= 1
    good = engine.validate("Дарья, папайя-фермент мягко обновляет кожу без трения.", ctx)
    assert good.ok
