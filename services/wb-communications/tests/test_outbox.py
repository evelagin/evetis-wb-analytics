"""C1-1: BigQuery delivery via Firestore outbox — at-least-once, deduped.

Not "exactly-once": a failed BQ write leaves the event pending; the next flush
retries; BQ insertId (here emulated by event_id) dedups so one row results.
"""
from __future__ import annotations

from app.domain.statuses import EventType
from app.services.pipeline import Deps, _emit_event, flush_events
from app.services.prompt_service import PromptService
from app.services.repository import MemoryRepository
from tests.conftest import FakeOpenAI, FakeTelegram, FakeWB, make_settings


class FlakyBQ:
    """Fails the first N insert calls, then succeeds. Dedups by event_id."""

    def __init__(self, fail_first=1):
        self.fail_first = fail_first
        self.calls = 0
        self.rows = {}

    def insert_event(self, event):
        self.calls += 1
        if self.calls <= self.fail_first:
            return False  # transient BQ failure
        self.rows[event["event_id"]] = event  # dedup by id
        return True

    def upsert_current(self, row):
        return True


def _deps(bq):
    return Deps(settings=make_settings(), repo=MemoryRepository(), wb=FakeWB(),
                openai=FakeOpenAI(), telegram=FakeTelegram(), bq=bq,
                prompts=PromptService("prompts", "reviews_v1"))


def _seed_doc(repo):
    repo.docs["d1"] = {"channel": "wb", "entity_type": "review", "source_id": "S1",
                       "generation_number": 1, "openai_usage": {}}
    return repo.docs["d1"]


def test_failed_insert_stays_pending_then_delivered_once():
    bq = FlakyBQ(fail_first=1)
    deps = _deps(bq)
    doc = _seed_doc(deps.repo)

    _emit_event(deps, doc, "d1", EventType.PUBLISHED, status_after="published")
    # exactly one event enqueued in the outbox
    assert len(deps.repo.outbox) == 1
    eid = next(iter(deps.repo.outbox))
    assert deps.repo.outbox[eid]["status"] == "pending"

    # first flush: BQ fails -> event remains pending, attempts incremented
    r1 = flush_events(deps)
    assert (r1["delivered"], r1["failed"]) == (0, 1)
    assert deps.repo.outbox[eid]["status"] == "pending"
    assert deps.repo.outbox[eid]["attempts"] == 1

    # second flush: BQ succeeds -> delivered, and exactly one BQ row
    r2 = flush_events(deps)
    assert (r2["delivered"], r2["failed"]) == (1, 0)
    assert deps.repo.outbox[eid]["status"] == "delivered"
    assert len(bq.rows) == 1


def test_enqueue_is_idempotent_by_event_id():
    deps = _deps(FlakyBQ(fail_first=0))
    doc = _seed_doc(deps.repo)
    _emit_event(deps, doc, "d1", EventType.PUBLISHED, status_after="published")
    _emit_event(deps, doc, "d1", EventType.PUBLISHED, status_after="published")  # same logical event
    assert len(deps.repo.outbox) == 1  # deduped
