from __future__ import annotations

import pytest

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import Namespace, Urgency
from app.communication_engine.knowledge.registry import (
    KnowledgeError,
    KnowledgeRegistry,
    MarkdownKnowledgeSource,
    parse_markdown,
)
from app.communication_engine.models.knowledge import KnowledgeRef


# --- parse_markdown ---------------------------------------------------------
def test_parse_front_matter_and_body():
    raw = "---\nid: x\nversion: 3\ntags:\n  - a\n  - b\n---\nHello body"
    doc = parse_markdown(Namespace.BRAND, "x", raw)
    assert doc.version == 3
    assert doc.tags == ("a", "b")
    assert doc.body == "Hello body"


def test_parse_no_front_matter():
    doc = parse_markdown(Namespace.BRAND, "x", "just text")
    assert doc.metadata == {}
    assert doc.body == "just text"
    assert doc.version == 1


def test_parse_id_mismatch_raises():
    with pytest.raises(KnowledgeError):
        parse_markdown(Namespace.BRAND, "x", "---\nid: y\n---\nbody")


def test_parse_non_mapping_front_matter_raises():
    with pytest.raises(KnowledgeError):
        parse_markdown(Namespace.BRAND, "x", "---\n- just\n- a list\n---\nbody")


# --- MarkdownKnowledgeSource ------------------------------------------------
def _write(tmp_path, namespace, doc_id, content):
    d = tmp_path / namespace
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc_id}.md").write_text(content, encoding="utf-8")


def test_source_load_exists_list(tmp_path):
    _write(tmp_path, "products", "p1", "---\nid: p1\narticles: ['1','2']\n---\nbody")
    _write(tmp_path, "products", "p2", "---\nid: p2\narticles: ['3']\n---\nbody")
    cfg = EngineConfig(knowledge_root=tmp_path)
    src = MarkdownKnowledgeSource(cfg)
    assert src.exists(Namespace.PRODUCTS, "p1")
    assert not src.exists(Namespace.PRODUCTS, "nope")
    assert src.list_ids(Namespace.PRODUCTS) == ["p1", "p2"]
    assert src.list_ids(Namespace.CASES) == []  # missing dir -> empty
    doc = src.load(Namespace.PRODUCTS, "p1")
    assert doc.doc_id == "p1"


def test_source_load_missing_raises(tmp_path):
    cfg = EngineConfig(knowledge_root=tmp_path)
    with pytest.raises(KnowledgeError):
        MarkdownKnowledgeSource(cfg).load(Namespace.PRODUCTS, "ghost")


# --- Registry with real knowledge base --------------------------------------
def test_registry_cache_returns_same_object(registry):
    ref = KnowledgeRef(namespace=Namespace.BRAND, doc_id="brand")
    assert registry.get(ref) is registry.get(ref)  # cached


def test_article_index_and_lookup(registry):
    assert registry.product_id_for_article("305101361") == "acne_serum"
    assert registry.product_id_for_article("593111985") == "body_cream"  # second aroma article
    assert registry.product_id_for_article("000") is None
    assert registry.product_id_for_article(None) is None


def test_scenario_rules_sorted_by_priority(registry):
    rules = registry.scenario_rules()
    ids = [r.case_id for r in rules]
    assert ids[0] == "allergy"  # priority 10
    allergy = next(r for r in rules if r.case_id == "allergy")
    assert allergy.urgency is Urgency.HIGH


def test_known_actives_map(registry):
    actives = registry.known_actives()
    assert "папаин" in actives["enzyme_powder"]
    assert "папаин" not in actives["acne_serum"]


def test_register_helpers(registry):
    assert registry.register_product("acne_serum").id == "acne_serum"
    assert registry.register_case("allergy").doc_id == "allergy"
    assert registry.register_marketplace("wildberries").doc_id == "wildberries"
    assert registry.register_brand("tone").doc_id == "tone"
    assert "products/acne_serum" in registry.active_refs


def test_register_missing_raises(registry):
    with pytest.raises(KnowledgeError):
        registry.register_product("does_not_exist")


def test_brand_meta_list_missing_doc(registry):
    assert registry.brand_meta_list("nonexistent", "x") == []
