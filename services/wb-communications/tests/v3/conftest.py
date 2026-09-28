"""Shared v3 fixtures: a snapshot built offline from the YAML seed + a pinned copy of the
external REF tables (the production snapshot is built from BigQuery by scripts/v3_registry.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.v3.registry import load_policy, registry_tables, seed_sha256
from app.v3.snapshot import KnowledgeSnapshot
from app.v3.snapshot_builder import build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
EXTERNAL = FIXTURES / "external_ref_tables_2026-09-25.json"


def tables():
    t = registry_tables()
    t.update(json.loads(EXTERNAL.read_text(encoding="utf-8")))
    return t


@pytest.fixture(scope="session")
def snapshot_data():
    snap, _ = build_snapshot(tables(), load_policy(), seed_sha256=seed_sha256(), origin="test",
                             as_of="2026-09-28", generated_at="2026-09-28T12:00:00Z")
    return snap


@pytest.fixture(scope="session")
def snap(snapshot_data) -> KnowledgeSnapshot:
    return KnowledgeSnapshot(snapshot_data)


class FakeV3LLM:
    """Scripted stand-in for app.v3.llm.V3LLM (no network)."""

    def __init__(self, classification=None, answer=None, classify_error=None, generate_error=None):
        from app.v3.llm import LLMStats
        self.classification = classification
        self.answer = answer
        self.classify_error = classify_error
        self.generate_error = generate_error
        self.calls = []
        self.stats = LLMStats()

    def structured(self, system, user, name, schema):
        self.calls.append(("classifier", user))
        if self.classify_error:
            raise self.classify_error
        base = {"intents": [], "situations": [], "text_sentiment": "none", "safety_events": [],
                "asked_ingredients": [], "asked_component": "", "classification_confidence": 0.9}
        base.update(self.classification or {})
        self.stats.calls += 1
        return base, {"input_tokens": 100, "output_tokens": 50}

    def structured_timed(self, system, user, name, schema):
        self.calls.append(("generator", user))
        if self.generate_error:
            raise self.generate_error
        self.stats.calls += 1
        text = self.answer(json.loads(user)) if callable(self.answer) else self.answer
        return {"text": text or "", "used_fact_ids": []}, {"input_tokens": 300, "output_tokens": 60}, 5, "fake-model"
