from __future__ import annotations

from app.communication_engine.constants import Namespace
from app.communication_engine.models.classification import ReviewInput
from app.communication_engine.models.knowledge import KnowledgeDocument
from app.communication_engine.models.product import Product
from app.communication_engine.models.validation import Severity, ValidationIssue, ValidationResult
from app.domain.models import Review
from app.services.engine_prompt_service import EnginePromptService


def test_meta_list_coercions():
    doc = KnowledgeDocument(namespace=Namespace.PRODUCTS, doc_id="p",
                            metadata={"a": ["x", "y"], "b": "single", "c": None})
    assert doc.meta_list("a") == ["x", "y"]
    assert doc.meta_list("b") == ["single"]
    assert doc.meta_list("c") == []


def test_product_from_document():
    doc = KnowledgeDocument(namespace=Namespace.PRODUCTS, doc_id="enzyme_powder",
                            metadata={"name": "Пудра", "supplier_articles": ["1"],
                                      "wb_nm_ids": ["100"], "actives": ["папаин"]})
    p = Product.from_document(doc)
    assert p.id == "enzyme_powder" and p.name == "Пудра"
    assert p.supplier_articles == ("1",) and p.wb_nm_ids == ("100",)
    assert p.articles == ("1",) and p.actives == ("папаин",)  # .articles is the alias


def test_validation_result_flags():
    err = ValidationIssue(validator="v", severity=Severity.ERROR, message="bad")
    warn = ValidationIssue(validator="v", severity=Severity.WARNING, message="meh")
    r = ValidationResult(issues=(err, warn))
    assert r.ok is False
    assert r.errors == [err] and r.warnings == [warn]
    assert ValidationResult().ok is True


def test_engine_end_to_end(engine):
    bundle = engine.build_prompt(ReviewInput(rating=2, supplier_article="305101361",
                                             text="жжение и раздражение", user_name="Света"))
    assert bundle.system and bundle.user
    assert "от прыжей" in bundle.system or "от прыщей" in bundle.system
    assert bundle.classification.product_id == "acne_serum"


def test_engine_prompt_service_adapter():
    svc = EnginePromptService.create()
    review = Review(platform="wb", rating=5, supplier_article="535580776",
                    text="класс", user_name="Оля", nm_id=123)
    bundle = svc.build_prompt(review)
    assert "Энзимная пудра" in bundle.system
    ctx = svc.build_context(review)
    result = svc.validate("Оля, папайя-фермент мягко обновляет кожу.", ctx)
    assert result.ok
