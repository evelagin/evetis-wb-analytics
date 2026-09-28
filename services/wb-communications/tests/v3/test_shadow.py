"""WP10 — shadow runner: real-input shape, isolation, budget, failure isolation, ledger."""
from __future__ import annotations

import copy

from app.v3.engine import V3Engine
from app.v3.shadow import MemoryShadowStore, V3Runtime, check_text, run_shadow, run_shadow_isolated
from tests.v3.conftest import FakeV3LLM


class Writer:
    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def insert_v3_decision(self, row):
        if self.fail:
            return False
        self.rows.append(row)
        return True


class Settings:
    v3_shadow_max_items_per_poll = 10
    v3_shadow_budget_seconds = 60
    v3_shadow_poll_deadline_seconds = 150


def _docs():
    return {
        "d1": {"entity_type": "review", "nm_id": "305101361", "text": "Раздражение не проходит", "rating": "2",
               "ai_answer": "Это адаптация кожи.", "final_answer": "Это адаптация кожи.",
               "answer_versions": [{"source": "ai"}], "first_seen_at": "2026-09-28T10:00:00Z", "status": "published"},
        "d2": {"entity_type": "question", "nm_id": "438775617", "text": "Какая страна производства?",
               "ai_answer": "Китай", "final_answer": "Китай", "answer_versions": [{"source": "ai"}],
               "first_seen_at": "2026-09-28T09:00:00Z", "status": "pending_approval"},
        "d3": {"entity_type": "review", "nm_id": "999", "text": "ok", "first_seen_at": "2026-09-28T08:00:00Z"},
    }


def _rt(snap, docs, writer=None, llm=None):
    llm = llm or FakeV3LLM(answer="Страна производства — Китай.")
    return V3Runtime(engine=V3Engine(snap, llm_factory=lambda: llm), store=MemoryShadowStore(docs),
                     writer=writer or Writer(), settings=Settings())


def test_runs_on_docs_and_never_mutates_them(snap):
    docs = _docs()
    before = copy.deepcopy(docs)
    rt = _rt(snap, docs)
    s = run_shadow(rt, max_items=10, deadline=1e18)
    assert s.decided == 3 and s.persisted == 3 and s.errors == 0
    assert docs == before                                   # v2 documents untouched
    outcomes = {r["communication_id"]: r["final_outcome"] for r in rt.writer.rows}
    assert outcomes == {"d1": "SAFETY_TEMPLATE", "d2": "FACT_ANSWER", "d3": "HUMAN_REVIEW"}
    r1 = [r for r in rt.writer.rows if r["communication_id"] == "d1"][0]
    assert r1["v2_ai_verdict"] == "BLOCK" and "V-SAFETY" in r1["v2_ai_block_rules"]
    assert r1["knowledge_snapshot_id"] == snap.snapshot_id and r1["message_sha256"]
    assert "Раздражение" not in (r1["classification_json"] or "").split("evidence_span")[0]


def test_ledger_idempotent_and_recheck_on_changed_final_text(snap):
    docs = _docs()
    rt = _rt(snap, docs)
    run_shadow(rt, max_items=10, deadline=1e18)
    n = len(rt.writer.rows)
    s2 = run_shadow(rt, max_items=10, deadline=1e18)
    assert s2.decided == 0 and len(rt.writer.rows) == n       # nothing re-run
    docs["d2"]["final_answer"] = "Возврат гарантирован, напишите нам в чат."
    s3 = run_shadow(rt, max_items=10, deadline=1e18)
    assert s3.rechecked == 1
    last = rt.writer.rows[-1]
    assert last["run_kind"] == "v2_final_recheck" and last["v2_final_verdict"] == "BLOCK"


def test_budget_and_max_items(snap):
    rt = _rt(snap, _docs())
    s = run_shadow(rt, max_items=1, deadline=1e18)
    assert s.decided == 1
    s2 = run_shadow(rt, max_items=10, deadline=10, clock=lambda: 0.0)   # < MIN_ITEM_SECONDS left
    assert s2.decided == 0 and s2.skipped_budget == 1


def test_engine_exception_becomes_explicit_failure_row(snap):
    rt = _rt(snap, _docs())
    rt.engine.decide = lambda msg: (_ for _ in ()).throw(RuntimeError("x"))
    s = run_shadow(rt, max_items=10, deadline=1e18)
    assert s.errors == 3 and all(r["failure_code"] == "ENGINE_ERROR" for r in rt.writer.rows)


def test_persistence_failure_is_not_marked_done(snap):
    rt = _rt(snap, _docs(), writer=Writer(fail=True))
    s = run_shadow(rt, max_items=10, deadline=1e18)
    assert s.persist_failed == 3 and rt.store.ledger == {}


def test_isolated_entry_never_raises(snap):
    class Boom:
        settings = Settings()

        @property
        def engine(self):
            raise RuntimeError("x")
    assert run_shadow_isolated(Boom(), poll_started=0.0, clock=lambda: 1.0)["error"] == "RuntimeError"
    assert run_shadow_isolated(None, poll_started=0.0) == {"enabled": False}


def test_poll_deadline_respected(snap):
    rt = _rt(snap, _docs())
    out = run_shadow_isolated(rt, poll_started=0.0, clock=lambda: 1000.0)
    assert out.get("skipped") == "no_time_budget"


def test_manual_text_check_uses_same_verifier(snap):
    rt = _rt(snap, _docs())
    r = check_text(rt, "d2", _docs()["d2"], "В креме салициловая кислота 2,25%, можно беременным.", run_kind="t")
    assert r.verdict == "BLOCK" and {"V-RESTRICTED", "V-SUITABILITY"} <= set(r.rule_ids())
    assert rt.writer.rows[-1]["run_kind"] == "t"


def test_operator_view_renders_without_actions(snap):
    from app.v3.operator_view import render
    rt = _rt(snap, _docs())
    run_shadow(rt, max_items=10, deadline=1e18)
    row = [r for r in rt.writer.rows if r["communication_id"] == "d1"][0]
    txt = render(row)
    assert "v3 SHADOW" in txt and "SAFETY_TEMPLATE" in txt and "Опубликовать" not in txt
