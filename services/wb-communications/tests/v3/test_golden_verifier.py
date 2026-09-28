"""Same deterministic verifier for manual edits, v2 drafts and AI drafts (free-text context)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.v3.resolver import resolve
from app.v3.verifier import context_for_free_text, verify

CORPUS = yaml.safe_load((Path(__file__).parent / "golden_corpus.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CORPUS["verifier"], ids=[c["id"] for c in CORPUS["verifier"]])
def test_verifier_case(case, snap):
    res = resolve(snap, nm_id=case["nm"])
    r = verify(case["text"], context_for_free_text(snap, [res.product_id]), snap)
    if case.get("pass"):
        assert r.verdict in ("PASS", "INFO"), r.to_dict()
        return
    blocks = set(r.rule_ids("BLOCK"))
    assert r.verdict == "BLOCK", r.to_dict()
    for rule in case["block"]:
        assert rule in blocks, (rule, r.to_dict())
    for rule in case.get("warn", []):
        assert rule in set(r.rule_ids("WARNING")), r.to_dict()
