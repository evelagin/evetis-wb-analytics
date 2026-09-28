"""WP1/WP2 — registry integrity + knowledge build gate (must FAIL on each corruption)."""
from __future__ import annotations

import copy
import json

import pytest

from app.v3.registry import load_policy, registry_tables, seed_sha256
from app.v3.snapshot import KnowledgeSnapshot, KnowledgeSnapshotError, load_snapshot
from app.v3.snapshot_builder import SnapshotGateError, assemble, build_snapshot, validate
from tests.v3.conftest import tables


def _gate(t, policy=None):
    snap = assemble(t, policy or load_policy(), as_of="2026-09-28")
    return validate(snap, t)


def test_seed_passes_gate():
    r = _gate(tables())
    assert r["status"] == "PASS", r["errors"]
    assert r["checks"]["recipe_sum_flagged_documents"] == "WARN"   # SP-TS-2 document sums to 100.3


def test_every_recipe_sums_to_100_or_is_flagged(snap):
    for pid, p in snap.products.items():
        rec = (p.get("ingredients") or {}).get("recipe") or []
        if rec:
            total = sum(o["concentration_pct"] for o in rec)
            assert abs(total - 100) <= 0.05 or "RECIPE_SUM_MISMATCH" in p["ingredients"]["recipe_quality_flags"], pid


def _mut(fn):
    t = copy.deepcopy(tables())
    fn(t)
    return _gate(t)


def test_gate_fails_when_restricted_salicylic_made_public():
    def f(t):
        for r in t["REF_PRODUCT_INGREDIENT"]:
            if r["product_id"] == "EVT-FC-ACNE-50" and r["ingredient_id"] == "salicylic_acid":
                r["concentration_disclosure_policy"] = "PUBLIC"
    r = _mut(f)
    assert r["status"] == "FAIL" and any("ODR-07" in e for e in r["errors"])


def test_gate_fails_on_ph_conflict_made_public():
    def f(t):
        for r in t["REF_PRODUCT_FACT"]:
            if r["fact_type"] == "ph" and r["product_id"] == "EVT-FS-ACNE-30":
                r["disclosure_policy"] = "PUBLIC"
    assert _mut(f)["status"] == "FAIL"


def test_gate_fails_on_duplicate_active_identifier():
    def f(t):
        t["REF_SKU_CHANNEL_MAP"].append({"internal_sku": "EVT-FC-ACNE-50", "marketplace": "WB",
                                         "marketplace_sku": "305101361", "vendor_code": "x", "is_current": "true"})
    assert _mut(f)["checks"]["unique_active_identifiers"] == "FAIL"


def test_gate_fails_on_unknown_bundle_component():
    def f(t):
        t["REF_BUNDLE_COMPONENTS"].append({"bundle_internal_sku": "EVT-SET-HAND-BODY", "component_internal_sku": "EVT-GHOST",
                                           "component_qty": "1", "effective_from": "2025-05-01", "effective_to": None})
    assert _mut(f)["checks"]["bundle_components_known"] == "FAIL"


def test_gate_fails_on_public_fact_without_source():
    def f(t):
        t["REF_PRODUCT_FACT"][0]["source_id"] = "SRC-DOES-NOT-EXIST"
    assert _mut(f)["checks"]["facts_have_known_sources"] == "FAIL"


def test_gate_fails_on_unknown_disclosure_enum():
    def f(t):
        t["REF_PRODUCT_FACT"][0]["disclosure_policy"] = "SORT_OF_PUBLIC"
    assert _mut(f)["checks"]["enums_known"] == "FAIL"


def test_gate_fails_on_restricted_product_exposed():
    def f(t):
        t["REF_PRODUCT_FACT"].append({"fact_id": "EVT-HC-BODY-300.volume", "product_id": "EVT-HC-BODY-300",
                                      "fact_type": "volume", "value_json": "300", "unit": "ml",
                                      "customer_value_ru": "300 мл", "source_id": "SRC-TU-SP-TS-1", "source_tier": "T1a",
                                      "fact_status": "VERIFIED", "reliability": "A", "extraction_confidence": "MEDIUM",
                                      "disclosure_policy": "PUBLIC", "quality_flags_json": "[]"})
    assert _mut(f)["checks"]["restricted_not_public"] == "FAIL"


def test_gate_fails_on_owner_decision_to_missing_fact():
    def f(t):
        t["REF_OWNER_DECISION"][0]["affected_facts_json"] = json.dumps(["EVT-FS-ACNE-30.nonexistent"])
    assert _mut(f)["checks"]["conflicts_and_decisions_consistent"] == "FAIL"


def test_gate_fails_when_cosmetic_claims_enabled():
    policy = load_policy()
    policy["claim_policy"]["enabled_classes"] = ["FACT_CLAIM", "APPROVED_CANDIDATE"]
    policy["claim_policy"]["cosmetic_claim_generation"] = True
    r = _gate(copy.deepcopy(tables()), policy)
    assert r["checks"]["claim_policy"] == "FAIL"


def test_gate_fails_when_auto_publish_enabled():
    policy = load_policy()
    policy["runtime"]["auto_publish"] = True
    assert _gate(copy.deepcopy(tables()), policy)["checks"]["claim_policy"] == "FAIL"


def test_gate_fails_on_content_mismatch_without_conflict():
    def f(t):
        t["REF_KNOWLEDGE_CONFLICT"] = [c for c in t["REF_KNOWLEDGE_CONFLICT"] if c["conflict_id"] != "KC-13"]
    assert _mut(f)["checks"]["content_mismatch_has_conflict"] == "FAIL"


def test_build_raises_and_writes_nothing_on_gate_failure():
    t = copy.deepcopy(tables())
    t["REF_PRODUCT_FACT"][0]["fact_status"] = "BOGUS"
    with pytest.raises(SnapshotGateError):
        build_snapshot(t, load_policy(), seed_sha256="x", origin="test")


def test_tampered_snapshot_is_rejected(snapshot_data):
    bad = copy.deepcopy(snapshot_data)
    bad["products"]["EVT-FC-ACNE-50"]["ingredients"]["recipe"][8]["concentration_disclosure_policy"] = "PUBLIC"
    with pytest.raises(KnowledgeSnapshotError):
        KnowledgeSnapshot(bad)


def test_snapshot_has_no_secrets_and_keeps_conflicts(snapshot_data):
    s = json.dumps(snapshot_data, ensure_ascii=False)
    assert "sk-" not in s and "Bearer" not in s
    assert {"KC-01", "KC-05", "KC-08", "KC-13", "KC-21"} <= set(snapshot_data["conflicts"])
    ph = [f for f in snapshot_data["products"]["EVT-FC-ACNE-50"]["facts"] if f["fact_type"] == "ph"][0]
    assert ph["value"] == 6.94 and ph["fact_status"] == "CONFLICT" and ph["disclosure_policy"] == "DO_NOT_DISCLOSE"
    sal = [o for o in snapshot_data["products"]["EVT-FC-ACNE-50"]["ingredients"]["recipe"]
           if o["ingredient_id"] == "salicylic_acid"][0]
    assert sal["concentration_pct"] == 2.25 and sal["concentration_disclosure_policy"] == "DO_NOT_DISCLOSE_PENDING"


def test_reconciliation_statuses(snap):
    assert snap.reconciliation("EVT-FS-ACNE-30") == "MATCH"
    assert snap.reconciliation("EVT-FC-ACNE-50") == "ORDER_ONLY"
    assert snap.reconciliation("EVT-HC-HAND-300") == "CONTENT_MISMATCH"
    assert snap.reconciliation("EVT-FT-MOIST-150") == "LABEL_UNAVAILABLE"
    assert snap.reconciliation("EVT-HC-BODY-300") == "RESTRICTED"


def test_body_cream_restricted_and_hand_pao_12(snap):
    body = snap.product("EVT-HC-BODY-300")
    assert body["customer_fact_generation"] == "RESTRICTED" and body["spec_association_status"] == "DERIVED"
    assert [f["value"] for f in snap.facts("EVT-HC-HAND-300", "pao")] == [12]
    assert [f["value"] for f in snap.facts("EVT-HC-HAND-300", "intended_use")] == [["hands"]]
    assert [f["value"] for f in snap.facts("EVT-EP-ENZYME-75", "intended_use")] == [["face"]]
    assert [f["value"] for f in snap.facts("EVT-EP-ENZYME-75", "net_mass")] == [75]
    assert [f["value"] for f in snap.facts("EVT-EP-ENZYME-75", "container_volume")] == [120]


def test_tea_tree_in_acne_tonic_is_extract_not_oil(snap):
    ids = {o["ingredient_id"] for o in snap.ingredient_rows("EVT-FT-ACNE-150")}
    assert "melaleuca_alternifolia_leaf_extract" in ids and "melaleuca_alternifolia_leaf_oil" not in ids


def test_committed_active_snapshot_is_valid_and_matches_seed():
    s = load_snapshot()  # hash + gate + schema verified on load
    from app.v3.registry import OWNED_TABLES, rows_sha256
    seed = registry_tables()
    for name in OWNED_TABLES:
        assert s.data["source_manifest"]["tables"][name]["sha256"] == rows_sha256(seed[name]), name
    assert s.data["source_manifest"]["origin"].startswith("bigquery:")
