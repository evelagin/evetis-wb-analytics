"""Golden / adversarial decision corpus — deterministic layer only (no LLM)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.v3.engine import V3Engine

CORPUS = yaml.safe_load((Path(__file__).parent / "golden_corpus.yaml").read_text(encoding="utf-8"))


def _msg(c):
    i = c["in"]
    return {"communication_id": c["id"], "entity_type": i["entity"], "nm_id": i["nm"], "text": i.get("text", ""),
            "rating": i.get("rating"), "buyer_name": i.get("buyer", "")}


@pytest.mark.parametrize("case", CORPUS["decisions"], ids=[c["id"] for c in CORPUS["decisions"]])
def test_golden_decision(case, snap):
    d, _ = V3Engine(snap).decide(_msg(case))
    e = case["expect"]
    s = d.classification.safety
    if "risk" in e:
        assert d.plan.risk_level == e["risk"], (d.plan.risk_level, s.to_dict())
    if "route" in e:
        assert s.route == e["route"], s.to_dict()
    for m in e.get("markers", []):
        assert m in s.emergency_markers
    for t in e.get("negated", []):
        ev = [x for x in s.events if x.type == t]
        assert ev and all(not x.active for x in ev), s.to_dict()
    if "outcome" in e:
        assert d.final_outcome == e["outcome"], (d.final_outcome, d.failure_code, d.plan.reasons)
    if "failure" in e:
        assert d.failure_code == e["failure"], (d.failure_code, d.plan.reasons)
    if "strategy" in e:
        assert d.plan.strategy == e["strategy"], (d.plan.strategy, d.plan.failure_code, d.plan.reasons)
    if "template" in e:
        assert e["template"] in d.plan.template_ids
    allowed = " ".join(r.customer_value_ru or "" for r in d.plan.allowed)
    for x in e.get("allowed_contains", []):
        assert x in allowed, allowed
    for x in e.get("allowed_not_contains", []):
        assert x not in allowed, allowed
    for x in e.get("draft_contains", []):
        assert x in (d.draft or ""), d.draft
    for w in e.get("warnings", []):
        assert w in d.plan.operator_warnings
    if "product_kind" in e:
        assert d.resolution.kind == e["product_kind"]
    if "scope_products" in e:
        assert len({r.product_id for r in d.plan.resolved}) == e["scope_products"]
    # invariants on EVERY case
    if d.plan.risk_level == "R4":
        assert d.final_outcome == "HUMAN_REVIEW" and d.draft is None and s.emergency_markers
    if d.draft:
        assert d.verification is not None and not d.verification.blocked, d.verification.to_dict()


def test_r4_requires_emergency_marker(snap):
    for c in CORPUS["decisions"]:
        d, _ = V3Engine(snap).decide(_msg(c))
        if d.plan.risk_level == "R4":
            assert d.classification.safety.emergency_markers, c["id"]


def test_stars_never_lower_safety(snap):
    for rating in (1, 3, 5):
        d, _ = V3Engine(snap).decide({"communication_id": "x", "entity_type": "review", "nm_id": "438775437",
                                      "text": "Начался сильный отёк губ", "rating": rating})
        assert d.plan.risk_level == "R4"
