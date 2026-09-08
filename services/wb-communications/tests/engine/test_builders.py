from __future__ import annotations

from app.communication_engine.builder.context_builder import ContextBuilder
from app.communication_engine.builder.prompt_builder import PromptBuilder
from app.communication_engine.config import EngineConfig, default_config
from app.communication_engine.constants import Marketplace, Namespace
from app.communication_engine.models.classification import Classification, ReviewInput


def test_context_builder_order_and_membership(engine):
    ctx = engine.build_context(ReviewInput(rating=2, supplier_article="305101361",
                                           text="жжение"))
    ids = ctx.block_ids()
    # brand core first, then product, then case, then marketplace
    assert ids[:3] == ["brand/brand", "brand/tone", "brand/response_rules"]
    assert "products/acne_serum" in ids
    assert "cases/irritation" in ids
    assert ids[-1] == "marketplaces/wildberries"


def test_context_builder_skips_missing_product(registry):
    cfg = default_config()
    cb = ContextBuilder(registry, cfg)
    cls = Classification(marketplace=Marketplace.WB, product_id="ghost_product")
    ctx = cb.build(cls)
    # unknown product doc is skipped, no crash; brand + marketplace still present
    assert not any(b.ref.namespace is Namespace.PRODUCTS for b in ctx.blocks)
    assert any(b.ref.doc_id == "wildberries" for b in ctx.blocks)


def test_prompt_builder_system_and_user(engine):
    review = ReviewInput(user_name="Анна", rating=5, supplier_article="535580776",
                         product_name="Энзимная пудра", text="супер")
    bundle = engine.build_prompt(review)
    assert "Энзимная пудра" in bundle.system   # product knowledge present
    assert "Анна" in bundle.user
    assert "супер" in bundle.user
    assert bundle.prompt_version.startswith("engine_v1:")


def test_prompt_version_deterministic_and_template_sensitive(registry):
    review = ReviewInput(rating=5, supplier_article="535580776")
    cb = ContextBuilder(registry, default_config())
    ctx = cb.build(Classification(marketplace=Marketplace.WB, product_id="enzyme_powder"))
    v1 = PromptBuilder(default_config()).build(ctx, review).prompt_version
    v1_again = PromptBuilder(default_config()).build(ctx, review).prompt_version
    v2 = PromptBuilder(EngineConfig(user_template_version=99)).build(ctx, review).prompt_version
    assert v1 == v1_again
    assert v1 != v2
