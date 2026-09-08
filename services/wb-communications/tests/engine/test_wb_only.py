"""WB-only round: barcode as a first-class identifier, internal-product_id bundle
components, purge of the Ozon SKU 3735674506, and stricter schema validation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.communication_engine.config import EngineConfig
from app.communication_engine.knowledge.registry import (
    KnowledgeRegistry,
    MarkdownKnowledgeSource,
)
from app.communication_engine.constants import Namespace
from app.communication_engine.models.classification import ReviewInput

SET_ACNE_3STEP_NM = "910584041"
SET_ACNE_3STEP_BARCODE = "2049910829446"
OZON_SKU = "3735674506"


# --- barcode resolution -----------------------------------------------------
def test_barcode_resolves_set_acne_3step(engine):
    c = engine.classify(ReviewInput(barcode=SET_ACNE_3STEP_BARCODE))
    assert c.product_id == "set_acne_3step"
    assert c.resolution_method == "barcode"
    assert c.needs_manual_moderation is False


def test_nm_id_resolves_set_acne_3step(engine):
    c = engine.classify(ReviewInput(nm_id=SET_ACNE_3STEP_NM))
    assert c.product_id == "set_acne_3step"
    assert c.resolution_method == "nm_id"


def test_ozon_sku_resolves_nothing(engine):
    """3735674506 is an Ozon SKU — it must not resolve to any WB product, by any
    identifier field."""
    for kw in ("supplier_article", "nm_id", "barcode"):
        c = engine.classify(ReviewInput(**{kw: OZON_SKU}))
        assert c.product_id is None, kw
        assert c.needs_manual_moderation is True, kw


def test_resolution_priority_article_then_nm_then_barcode_then_alias(registry):
    # article beats everything; barcode beats alias
    assert registry.product_id_for_barcode(SET_ACNE_3STEP_BARCODE) == "set_acne_3step"
    assert registry.product_id_for_nm(SET_ACNE_3STEP_NM) == "set_acne_3step"
    assert registry.product_id_for_barcode(OZON_SKU) is None


# --- bundle components are internal product_ids -----------------------------
def test_bundle_components_are_internal_product_ids(registry):
    p = registry.get_product("set_acne_3step")
    assert p.bundle_components == ("acne_toner", "acne_serum", "acne_cream")
    assert p.supplier_articles == ()          # vendorCode not confirmed
    assert p.wb_imt_ids == ()                 # Ozon SKU removed
    assert p.barcodes == (SET_ACNE_3STEP_BARCODE,)


def test_bundle_expands_component_blocks_by_product_id(engine):
    ctx = engine.build_context(ReviewInput(nm_id=SET_ACNE_3STEP_NM))
    ids = ctx.block_ids()
    assert "products/set_acne_3step" in ids
    assert "products/acne_toner" in ids
    assert "products/acne_serum" in ids
    assert "products/acne_cream" in ids


def test_all_bundle_components_reference_existing_products(registry):
    ids = {p.id for p in registry.products()}
    for product in registry.products():
        for component in product.bundle_components:
            assert component in ids, f"{product.id} -> {component}"
            assert not component.isdigit(), f"{product.id} still uses a numeric id"


# --- schema validation ------------------------------------------------------
def test_live_knowledge_schema_clean(engine):
    assert engine.validate_knowledge() == []


def _registry_from(tmp_path: Path) -> KnowledgeRegistry:
    cfg = EngineConfig(knowledge_root=tmp_path, validate_knowledge_on_startup=False)
    return KnowledgeRegistry(MarkdownKnowledgeSource(cfg), cfg)


def _write_product(products: Path, pid: str, body_meta: str) -> None:
    (products / f"{pid}.md").write_text(
        f"---\nid: {pid}\nversion: 1\nsource_status: verified\n{body_meta}---\n{pid}",
        encoding="utf-8",
    )


def test_schema_flags_duplicate_barcode(tmp_path):
    products = tmp_path / "products"
    products.mkdir(parents=True)
    _write_product(products, "a", 'barcodes: ["111"]\n')
    _write_product(products, "b", 'barcodes: ["111"]\n')
    problems = _registry_from(tmp_path).validate_schema()
    assert any("duplicate barcode 111" in p for p in problems)


def test_schema_flags_bundle_component_missing(tmp_path):
    products = tmp_path / "products"
    products.mkdir(parents=True)
    _write_product(products, "set_x", 'is_bundle: true\nbundle_components: ["ghost"]\n')
    problems = _registry_from(tmp_path).validate_schema()
    assert any("bundle component 'ghost' does not exist" in p for p in problems)


def test_schema_flags_duplicate_bundle_component(tmp_path):
    products = tmp_path / "products"
    products.mkdir(parents=True)
    _write_product(products, "real", "")
    _write_product(products, "set_x", 'is_bundle: true\nbundle_components: ["real", "real"]\n')
    problems = _registry_from(tmp_path).validate_schema()
    assert any("duplicate bundle component 'real'" in p for p in problems)


def test_schema_flags_self_referencing_bundle(tmp_path):
    products = tmp_path / "products"
    products.mkdir(parents=True)
    _write_product(products, "set_x", 'is_bundle: true\nbundle_components: ["set_x"]\n')
    problems = _registry_from(tmp_path).validate_schema()
    assert any("references itself" in p for p in problems)
