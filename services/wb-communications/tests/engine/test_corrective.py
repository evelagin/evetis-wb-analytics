"""Corrective round (v2.1): unresolved products, bundles, communication types,
numeric grounding, negation-aware scenarios, schema validation, neutral address,
verified source_status.
"""
from __future__ import annotations

from app.communication_engine.constants import (
    CommunicationType,
    ResolutionStatus,
    SourceStatus,
)
from app.communication_engine.models.classification import ReviewInput
from app.communication_engine.models.prompt_context import PromptContext
from app.communication_engine.validators.base import ValidationInput
from app.communication_engine.validators.numeric_validator import NumericGroundingValidator


# --- unresolved product -> manual moderation --------------------------------
def test_unresolved_product_flags_manual_moderation(engine):
    c = engine.classify(ReviewInput(supplier_article="does-not-exist", text="хороший крем"))
    assert c.product_id is None
    assert c.product_resolution_status is ResolutionStatus.UNRESOLVED
    assert c.needs_manual_moderation is True


def test_resolved_product_not_flagged(engine):
    c = engine.classify(ReviewInput(supplier_article="305101361"))
    assert c.needs_manual_moderation is False


# --- bundles ----------------------------------------------------------------
def test_bundle_resolves_and_includes_component_blocks(engine):
    # 868597351 = 4-step acne set -> pudra + toner + serum + cream
    ctx = engine.build_context(ReviewInput(supplier_article="868597351", text="набор супер"))
    ids = ctx.block_ids()
    assert "products/set_acne_4step" in ids
    assert "products/acne_serum" in ids   # component expanded
    assert "products/enzyme_powder" in ids


def test_bundle_answer_not_flagged_for_component_ingredient(engine):
    ctx = engine.build_context(ReviewInput(supplier_article="868597351"))
    # mentioning папаин (from the pudra component) must NOT be a hallucination
    result = engine.validate("Папаин из пудры мягко очищает, ниацинамид выравнивает тон.", ctx)
    assert not any(i.validator == "hallucination" for i in result.issues)


# --- communication types ----------------------------------------------------
def test_question_type_uses_question_task(engine):
    bundle = engine.build_prompt(ReviewInput(
        communication_type=CommunicationType.QUESTION,
        supplier_article="305101361", text="можно ли при беременности?"))
    assert "задал публичный вопрос" in bundle.user
    assert bundle.classification.communication_type is CommunicationType.QUESTION


def test_chat_type_uses_chat_task(engine):
    bundle = engine.build_prompt(ReviewInput(
        communication_type=CommunicationType.CHAT, supplier_article="305101361", text="привет"))
    assert "личный чат" in bundle.user


# --- numeric grounding ------------------------------------------------------
def _ctx(engine, **kw) -> PromptContext:
    return engine.build_context(ReviewInput(**kw))


def test_numeric_invented_percentage_flagged(engine):
    ctx = _ctx(engine, supplier_article="305101361")  # acne serum: ниацинамид 5%
    v = NumericGroundingValidator()
    bad = v.validate(ValidationInput(answer="Ниацинамид 10% отлично работает", context=ctx))
    assert any(i.detail == "10%" for i in bad)


def test_numeric_verified_percentage_ok(engine):
    ctx = _ctx(engine, supplier_article="305101361")
    v = NumericGroundingValidator()
    good = v.validate(ValidationInput(answer="Ниацинамид 5% регулирует жирность", context=ctx))
    assert good == []


def test_numeric_invented_ph_flagged(engine):
    ctx = _ctx(engine, supplier_article="305101361")  # acne serum has NO pH in KB
    v = NumericGroundingValidator()
    bad = v.validate(ValidationInput(answer="pH 6.94 идеален для кожи", context=ctx))
    assert any("ph" in i.detail for i in bad)


def test_numeric_verified_ph_ok_for_enzyme(engine):
    ctx = _ctx(engine, supplier_article="535580776")  # enzyme powder pH 8,0–8,5
    v = NumericGroundingValidator()
    assert v.validate(ValidationInput(answer="Щелочной pH 8,0–8,5 активирует фермент", context=ctx)) == []


# --- negation-aware scenarios ----------------------------------------------
def test_negation_suppresses_scenario(engine):
    c = engine.classify(ReviewInput(supplier_article="305101361", text="аллергии не было, всё отлично"))
    assert "allergy" not in c.scenario


def test_positive_scenario_still_detected(engine):
    c = engine.classify(ReviewInput(supplier_article="305101361", text="началась аллергия и зуд"))
    assert "allergy" in c.scenario


# --- neutral address --------------------------------------------------------
def test_no_name_uses_neutral_address(engine):
    bundle = engine.build_prompt(ReviewInput(supplier_article="305101361", text="ок", user_name=""))
    assert "Покупательница" not in bundle.user
    assert "нейтральное, без имени" in bundle.user


# --- schema validation ------------------------------------------------------
def test_knowledge_schema_is_valid(engine):
    assert engine.validate_knowledge() == []


def test_all_products_are_verified(registry):
    for product in registry.products():
        assert product.source_status is SourceStatus.VERIFIED, product.id


# --- item 2: adapter passes communication_type ------------------------------
def test_adapter_passes_communication_type():
    import pytest

    from app.communication_engine.constants import CommunicationType
    from app.domain.models import Review
    from app.services.engine_prompt_service import EnginePromptService, UnsupportedEntityType

    svc = EnginePromptService.create()
    review = Review(platform="wb", supplier_article="305101361", text="можно ли беременным?")
    bundle = svc.build_prompt(review, CommunicationType.QUESTION)
    assert bundle.classification.communication_type is CommunicationType.QUESTION
    assert "задал публичный вопрос" in bundle.user
    assert EnginePromptService.type_for_entity("question") is CommunicationType.QUESTION
    assert EnginePromptService.type_for_entity("feedback") is CommunicationType.REVIEW


def test_type_for_entity_unknown_does_not_return_review():
    import pytest

    from app.services.engine_prompt_service import EnginePromptService, UnsupportedEntityType

    with pytest.raises(UnsupportedEntityType):
        EnginePromptService.type_for_entity("unknown_type")


# --- item 3: fail-fast schema validation on startup -------------------------
def test_build_fails_fast_on_invalid_knowledge(tmp_path):
    import pytest

    from app.communication_engine.config import EngineConfig
    from app.communication_engine.engine import CommunicationEngine
    from app.communication_engine.knowledge.registry import KnowledgeError

    products = tmp_path / "products"
    products.mkdir(parents=True)
    # two products claiming the SAME article -> schema violation
    (products / "a.md").write_text('---\nid: a\nsource_status: verified\narticles: ["1"]\n---\nA',
                                   encoding="utf-8")
    (products / "b.md").write_text('---\nid: b\nsource_status: verified\narticles: ["1"]\n---\nB',
                                   encoding="utf-8")
    cfg = EngineConfig(knowledge_root=tmp_path, validate_knowledge_on_startup=True)
    with pytest.raises(KnowledgeError):
        CommunicationEngine.build(cfg)


# --- item 6: bundle hallucination = union of components ---------------------
def test_bundle_union_flags_foreign_but_allows_component(engine):
    ctx = engine.build_context(ReviewInput(supplier_article="868597351"))  # 4-step acne set
    # ниацинамид is in a component (allowed); сквалан is a known active of the
    # moisturizing line — foreign to this acne set -> flagged (union logic).
    res = engine.validate("Ниацинамид работает, а сквалан здесь лишний.", ctx)
    halluc = [i for i in res.issues if i.validator == "hallucination"]
    assert any(i.detail == "сквалан" for i in halluc)
    assert not any(i.detail == "ниацинамид" for i in halluc)


# --- item 8: article absent, real nm_id present -> resolved -----------------
def test_resolve_by_nm_id_only(engine):
    c = engine.classify(ReviewInput(supplier_article="", nm_id="535580776"))
    assert c.product_id == "enzyme_powder"
    assert c.resolution_method == "nm_id"
    assert c.needs_manual_moderation is False


# --- item 9: unknown nm_id + ambiguous name -> manual moderation ------------
def test_unknown_nm_and_ambiguous_name_manual(engine):
    # "набор" alias is shared by many bundles -> dropped as ambiguous -> no match
    c = engine.classify(ReviewInput(supplier_article="", nm_id="000000", product_name="набор для лица"))
    assert c.product_id is None
    assert c.needs_manual_moderation is True


# --- item 5: prohibited claim in system prompt, but NOT grounding ------------
def test_prohibited_claim_rendered_but_not_grounded(engine):
    bundle = engine.build_prompt(ReviewInput(supplier_article="305101361"))
    assert "Нельзя заявлять" in bundle.system
    ctx = engine.build_context(ReviewInput(supplier_article="305101361"))
    # even though "10%" appears in the constraints section of the system prompt,
    # the numeric validator must still flag it (constraints aren't grounding text)
    bad = engine.validate("Ниацинамид 10% отлично работает", ctx)
    assert any(i.validator == "numeric_grounding" and i.detail == "10%" for i in bad.issues)
