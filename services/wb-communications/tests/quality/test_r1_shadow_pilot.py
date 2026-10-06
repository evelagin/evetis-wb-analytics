"""R1 zero-write shadow pilot: explicit opt-in, activation window, no backfill,
one transactional claim per communication, global cap, v2 untouched."""
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.v3 import pilot
from app.v3 import shadow as shadow_mod
from app.v3.engine import V3Engine
from app.v3.registry import load_policy
from app.v3.shadow import FirestoreShadowStore, MemoryShadowStore, V3Runtime, run_shadow
from app.v3.snapshot import load_snapshot
from tests.v3.conftest import FakeV3LLM
from tests.v3.test_shadow import Writer

SNAP = load_snapshot()
START = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(days=14)
NOW = START + timedelta(days=1)
LEGACY = (START - timedelta(days=30)).isoformat()


def r1(**overrides):
    base = dict(v31_quality_shadow_enabled=True, v31_shadow_activation_id="r1-test",
                v31_shadow_start_at=START.isoformat(), v31_shadow_end_at=END.isoformat(),
                v31_shadow_pilot_max_communications="20", v31_operator_recovery_enabled=False,
                v31_owner_override_enabled=False, v3_knowledge_snapshot_id=SNAP.snapshot_id,
                v3_shadow_max_items_per_poll=10, v3_shadow_budget_seconds=60, v3_shadow_poll_deadline_seconds=150)
    base.update(overrides)
    return SimpleNamespace(**base)


def doc(created, seen=None, *, text="Крем приятный, руки мягкие", nm="252442517"):
    return {"entity_type": "review", "nm_id": nm, "text": text, "rating": "5",
            "ai_answer": "Спасибо за отзыв!", "final_answer": "Спасибо за отзыв!",
            "answer_versions": [{"source": "ai"}], "status": "pending_approval",
            "source_created_at": created, "first_seen_at": seen or created}


def new_docs(n, at=NOW):
    return {f"n{i:02d}": doc((at - timedelta(minutes=i)).isoformat()) for i in range(n)}


class CountingLLM(FakeV3LLM):
    """Separates 3.1E renderer calls (case_contract payload) from the v3 baseline."""
    def __init__(self):
        super().__init__(answer=lambda payload: "Рады, что крем Вам понравился."
                         if "case_contract" in payload else "Спасибо за отзыв.")

    @property
    def r1_calls(self):
        return sum(kind == "generator" and "case_contract" in json.loads(user) for kind, user in self.calls)


def runtime(docs, settings=None, store=None, llm=None):
    llm = llm or CountingLLM()
    rt = V3Runtime(engine=V3Engine(SNAP, llm_factory=lambda: llm), store=store or MemoryShadowStore(docs),
                   writer=Writer(), settings=settings or r1())
    return rt, llm


def poll(rt, max_items=10, now=NOW):
    return run_shadow(rt, max_items=max_items, deadline=1e18, wall_clock=lambda: now)


def records(store):
    return [v for k, v in store.pilot.items() if k.startswith("C.")]

# A. default OFF -------------------------------------------------------------------
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("V31_QUALITY_SHADOW_ENABLED", raising=False)
    assert Settings().v31_quality_shadow_enabled is False


@pytest.mark.parametrize("settings", [SimpleNamespace(v3_knowledge_snapshot_id=SNAP.snapshot_id),
                                      r1(v31_quality_shadow_enabled=False)], ids=["absent", "false"])
def test_flag_absent_or_false_means_zero_generation(settings):
    rt, llm = runtime(new_docs(3), settings)
    s = poll(rt)
    assert s.decided == 3 and s.v31_evaluated == 0 and s.v31_skips == {}
    assert llm.r1_calls == 0 and records(rt.store) == []
    assert all("response_quality_v31" not in v for v in rt.store.ledger.values())

# B. flag ON + eligible new communication --------------------------------------------
def test_eligible_post_activation_is_evaluated_once_with_bounded_llm():
    rt, llm = runtime(new_docs(1))
    s = poll(rt)
    assert s.v31_evaluated == 1 and llm.r1_calls <= 1
    [rec] = records(rt.store)
    assert rec["status"] == "COMPLETED" and rec["activation_id"] == "r1-test"
    assert rec["logical_llm_calls"] == llm.r1_calls and rec["hard_verdict"] and rec["quality_verdict"]
    for key in ("engine_version", "snapshot_id", "policy_version", "route", "candidate_sha256", "source_sha256",
                "v2_ai_sha256", "product_id", "product_resolution_status", "selected_expertise_ids",
                "generation_mode", "latency_ms", "evaluated_at", "claimed_at", "ledger_key"):
        assert key in rec
    # Hashes and verdicts only: no customer or draft text in the pilot record.
    blob = json.dumps(rec, ensure_ascii=False)
    assert "Крем приятный" not in blob and "Рады, что" not in blob and "Спасибо" not in blob
    assert rt.store.pilot[pilot.counter_id("r1-test")]["claimed"] == 1

# C/D. boundary and fail-closed time ---------------------------------------------------
@pytest.mark.parametrize("created,seen,expected", [
    (START - timedelta(seconds=1), START + timedelta(seconds=5), pilot.BEFORE_ACTIVATION),
    (START, START, None),                                   # start is inclusive
    (START + timedelta(seconds=1), START + timedelta(seconds=1), None),
    (START + timedelta(seconds=1), START - timedelta(seconds=1), pilot.BEFORE_ACTIVATION),
    (END, END, pilot.WINDOW_CLOSED),                        # end is exclusive
])
def test_activation_boundary(created, seen, expected):
    act, _ = pilot.activation(r1())
    assert pilot.eligibility({"source_created_at": created.isoformat(), "first_seen_at": seen.isoformat()},
                             act, max(NOW, seen)) == expected


def test_window_closed_by_time_even_for_new_items():
    act, _ = pilot.activation(r1())
    d = {"source_created_at": NOW.isoformat(), "first_seen_at": NOW.isoformat()}
    assert pilot.eligibility(d, act, END) == pilot.WINDOW_CLOSED


@pytest.mark.parametrize("created,seen", [(None, NOW.isoformat()), (NOW.isoformat(), None), ("", ""),
                                          ("2026-10-08T10:00:00", NOW.isoformat()), ("вчера", NOW.isoformat())])
def test_missing_or_naive_time_is_skipped_fail_closed(created, seen):
    docs = {"x": doc(created, seen)}
    docs["x"]["source_created_at"], docs["x"]["first_seen_at"] = created, seen
    rt, llm = runtime(docs)
    s = poll(rt)
    assert s.v31_skips == {pilot.UNKNOWN_TIME: 1} and llm.r1_calls == 0 and records(rt.store) == []

# E/F. version changes never replay history --------------------------------------------
def test_engine_version_change_does_not_replay(monkeypatch):
    docs = {"old": doc(LEGACY), **new_docs(1)}
    rt, llm = runtime(docs)
    poll(rt)
    first = llm.r1_calls
    monkeypatch.setattr(shadow_mod, "ENGINE_VERSION", "v3.0.1-shadow")   # new ledger identity
    s = poll(rt)
    assert s.decided == 2                                   # v3 baseline behaviour unchanged
    assert s.v31_evaluated == 0 and llm.r1_calls == first
    assert s.v31_skips == {pilot.BEFORE_ACTIVATION: 1, pilot.ALREADY_EVALUATED: 1}


def test_snapshot_identity_change_does_not_replay(monkeypatch):
    rt, llm = runtime({"old": doc(LEGACY), **new_docs(1)})
    poll(rt)
    first = llm.r1_calls
    monkeypatch.setattr(shadow_mod, "ledger_key", lambda doc_id, snap: f"{doc_id}|other-snapshot")
    s = poll(rt)
    assert s.decided == 2 and s.v31_evaluated == 0 and llm.r1_calls == first


def test_response_quality_version_change_does_not_backfill(monkeypatch):
    rt, llm = runtime(new_docs(2))
    poll(rt)
    first = llm.r1_calls
    import app.response_quality as rq
    monkeypatch.setattr(rq, "VERSION", "v3.1e-next")
    s = poll(rt)
    assert s.decided == 0 and s.v31_evaluated == 0 and llm.r1_calls == first

# G/H. idempotency across polls and restarts --------------------------------------------
def test_second_poll_skips_same_communication():
    rt, llm = runtime(new_docs(2))
    poll(rt)
    first = llm.r1_calls
    s = poll(rt)
    assert s.decided == 0 and s.v31_evaluated == 0 and llm.r1_calls == first and len(records(rt.store)) == 2


def test_restart_with_new_engine_and_lost_ledger_does_not_duplicate():
    docs = new_docs(2)
    rt, llm = runtime(docs)
    poll(rt)
    rt.store.ledger.clear()                                  # missing previous v3 decision
    rt2, llm2 = runtime(docs, store=rt.store)                # new engine/runtime object
    s = poll(rt2)
    assert s.decided == 2 and s.v31_evaluated == 0 and llm2.r1_calls == 0
    assert s.v31_skips == {pilot.ALREADY_EVALUATED: 2}

# I/J. global cap -------------------------------------------------------------------------
def test_overlapping_claims_cannot_exceed_cap():
    store, (act, _) = MemoryShadowStore({}), pilot.activation(r1())
    results, barrier = [], threading.Barrier(40)

    def claim(i):
        barrier.wait()
        results.append(store.claim_pilot(act, f"c{i % 30}", {"i": i}))
    threads = [threading.Thread(target=claim, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(pilot.CLAIMED) == 20
    assert store.pilot[pilot.counter_id("r1-test")]["claimed"] == 20


def test_twenty_first_case_skipped_across_polls():
    rt, llm = runtime(new_docs(21))
    totals = {}
    for _ in range(5):                                      # natural polls, 6 items each
        s = poll(rt, max_items=6)
        for k, v in s.v31_skips.items():
            totals[k] = totals.get(k, 0) + v
    assert len(records(rt.store)) == 20 and totals == {pilot.CAP_REACHED: 1}
    assert llm.r1_calls <= 20


class _Snap:
    def __init__(self, data): self.exists, self._d = data is not None, data
    def to_dict(self): return copy.deepcopy(self._d)


class _Ref:
    def __init__(self, db, key): self.db, self.key = db, key
    def get(self, transaction=None): return _Snap(self.db.get(self.key))
    def set(self, data, merge=False):
        self.db[self.key] = {**(self.db.get(self.key) or {}), **data} if merge else dict(data)


class _Tx:
    def __init__(self, db): self.db = db
    def set(self, ref, data): ref.set(data)


class _Client:
    def __init__(self): self.db = {}
    def collection(self, name):
        assert name == pilot.PILOT_COLLECTION
        return SimpleNamespace(document=lambda key: _Ref(self.db, key))
    def transaction(self): return _Tx(self.db)


def test_firestore_claim_is_one_transaction_with_counter(monkeypatch):
    from google.cloud import firestore
    monkeypatch.setattr(firestore, "transactional", lambda f: f)
    client = _Client()
    store = FirestoreShadowStore.__new__(FirestoreShadowStore)
    monkeypatch.setattr(store, "_client", lambda: client)
    act, _ = pilot.activation(r1(v31_shadow_pilot_max_communications="2"))
    assert store.claim_pilot(act, "a", {"status": "CLAIMED"}) == pilot.CLAIMED
    assert store.claim_pilot(act, "a", {"status": "CLAIMED"}) == pilot.ALREADY_EVALUATED
    assert store.claim_pilot(act, "b", {"status": "CLAIMED"}) == pilot.CLAIMED
    assert store.claim_pilot(act, "c", {"status": "CLAIMED"}) == pilot.CAP_REACHED
    assert client.db[pilot.counter_id(act.activation_id)]["claimed"] == 2
    store.complete_pilot(act, "a", {"status": "COMPLETED"})
    assert client.db[pilot.claim_id(act.activation_id, "a")]["status"] == "COMPLETED"


def test_claim_failure_means_no_llm_call(monkeypatch):
    rt, llm = runtime(new_docs(1))
    monkeypatch.setattr(rt.store, "claim_pilot", lambda *a: (_ for _ in ()).throw(RuntimeError("contention")))
    s = poll(rt)
    assert s.v31_skips == {pilot.CLAIM_ERROR: 1} and llm.r1_calls == 0


def test_pilot_record_failure_isolated_and_never_rerun(monkeypatch):
    rt, llm = runtime(new_docs(1))
    monkeypatch.setattr(rt.store, "complete_pilot", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    s = poll(rt)
    assert s.errors == 0 and s.persisted == 1
    rt.store.ledger.clear()
    again = poll(rt)
    assert again.v31_skips == {pilot.ALREADY_EVALUATED: 1}

# activation validation / operator surfaces (N, O, P) ---------------------------------
@pytest.mark.parametrize("override", [
    dict(v31_shadow_activation_id=""), dict(v31_shadow_activation_id="bad id/"),
    dict(v31_shadow_start_at=""), dict(v31_shadow_end_at=""), dict(v31_shadow_start_at="2026-10-07T00:00:00"),
    dict(v31_shadow_end_at=START.isoformat()), dict(v31_shadow_pilot_max_communications="0"),
    dict(v31_shadow_pilot_max_communications="21"), dict(v31_shadow_pilot_max_communications="abc"),
])
def test_invalid_activation_is_fail_closed(override):
    rt, llm = runtime(new_docs(1), r1(**override))
    s = poll(rt)
    assert s.v31_skips == {pilot.ACTIVATION_INVALID: 1} and llm.r1_calls == 0


def test_malformed_cap_never_breaks_settings(monkeypatch):
    monkeypatch.setenv("V31_SHADOW_PILOT_MAX_COMMUNICATIONS", "twenty")
    assert pilot.activation(Settings())[1] == pilot.DISABLED


@pytest.mark.parametrize("flag", ["v31_operator_recovery_enabled", "v31_owner_override_enabled"])
def test_operator_surfaces_block_activation(flag):
    rt, llm = runtime(new_docs(1), r1(**{flag: True}))
    s = poll(rt)
    assert s.v31_skips == {pilot.OPERATOR_SURFACES_ON: 1} and llm.r1_calls == 0


def test_operator_features_and_auto_publish_default_off(monkeypatch):
    for name in ("V31_OPERATOR_RECOVERY_ENABLED", "V31_OWNER_OVERRIDE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.v31_operator_recovery_enabled is False and s.v31_owner_override_enabled is False
    runtime_policy = load_policy()["runtime"]
    assert runtime_policy["auto_publish"] is False and runtime_policy["v3_customer_facing"] is False

# K/L/M. v2 output unchanged; no WB/Telegram mutation ---------------------------------
def _poll_v2(r1_on, now):
    from app.services.pipeline import run_poll
    from tests.conftest import SAMPLE_FEEDBACK, make_deps
    fb = copy.deepcopy(SAMPLE_FEEDBACK)
    fb.update(createdDate=now.isoformat(), text="Крем приятный, руки мягкие",
              productDetails={**fb["productDetails"], "nmId": 252442517, "supplierArticle": "252442517"})
    extra = dict(v31_quality_shadow_enabled=True, v31_shadow_activation_id="r1-v2",
                 v31_shadow_start_at=(now - timedelta(hours=1)).isoformat(),
                 v31_shadow_end_at=(now + timedelta(days=1)).isoformat()) if r1_on else {}
    deps = make_deps([fb], v3_shadow_enabled=True, **extra)
    llm = CountingLLM()
    deps.v3 = V3Runtime(engine=V3Engine(SNAP, llm_factory=lambda: llm), store=MemoryShadowStore(deps.repo.docs),
                        writer=Writer(), settings=deps.settings)
    summary = run_poll(deps)
    return deps, summary, llm


def _v2_view(deps):
    keep = ("status", "ai_answer", "final_answer", "generation_number", "telegram_message_id")
    return ([{k: d.get(k) for k in keep} | {"versions": [v.get("text") for v in d.get("answer_versions") or []]}
             for d in deps.repo.docs.values()], deps.telegram.sent)


def test_v2_output_unchanged_and_zero_external_mutation():
    now = datetime.now(timezone.utc)                         # same input for both runs
    off, s_off, llm_off = _poll_v2(False, now)
    on, s_on, llm_on = _poll_v2(True, now)
    assert _v2_view(on) == _v2_view(off)
    assert s_on["v3_shadow"]["v31_evaluated"] == 1 and llm_on.r1_calls <= 1
    assert s_off["v3_shadow"]["v31_evaluated"] == 0 and llm_off.r1_calls == 0
    for deps in (on, off):
        assert deps.wb.published == [] and deps.wb.published_questions == []
        assert deps.telegram.edits == [] and deps.telegram.acks == [] and len(deps.telegram.sent) == 1
        doc_ = next(iter(deps.repo.docs.values()))
        assert not doc_.get("published_at") and not doc_.get("response_recovery")
        assert not any(k.startswith("override") for k in doc_)
    record = next(v for k, v in on.v3.store.pilot.items() if k.startswith("C."))
    assert record["status"] == "COMPLETED"


# Pilot report: owner labels are validated, unevaluated rows are counted, not guessed.
def test_pilot_report_summary():
    from scripts.r1_pilot_report import summarize_rows
    rows = [dict(communication_id="a", OWNER_PREFERENCE="V31E", EDIT_CLASS="NONE", ISSUES="", operator_state="READY"),
            dict(communication_id="b", OWNER_PREFERENCE="V2", EDIT_CLASS="MATERIAL", ISSUES="FALSE_BLOCK,ROBOTIC",
                 operator_state="BLOCK"),
            dict(communication_id="c", OWNER_PREFERENCE="", EDIT_CLASS="", ISSUES="", status="ERROR")]
    s = summarize_rows(rows)
    assert s["evaluated"] == 2 and s["not_evaluated"] == 1 and s["v31e_preferred_rate"] == 0.5
    assert s["false_block_rate"] == 0.5 and s["robotic_findings"] == 1 and s["errors"] == 1
    with pytest.raises(ValueError):
        summarize_rows([dict(communication_id="x", OWNER_PREFERENCE="MAYBE", EDIT_CLASS="NONE", ISSUES="")])
