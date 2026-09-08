"""C0-4: crashed PROCESSING / PUBLISHING leases are safely reclaimed."""
from __future__ import annotations

import pytest

from app.domain.exceptions import InvalidTransition
from app.domain.models import Review, make_doc_id
from app.domain.statuses import Status
from app.services.repository import MemoryRepository

PAST = "2000-01-01T00:00:00+00:00"


def _review():
    return Review(review_id="R1", rating=5, text="ok", product_name="Крем")


def test_processing_lease_not_expired_blocks_reclaim():
    repo = MemoryRepository(lease_seconds=300)
    should, doc_id, _ = repo.claim_review(_review())
    assert should  # first claim
    # second claim while lease valid -> skip (no double processing)
    should2, _, _ = repo.claim_review(_review())
    assert should2 is False


def test_expired_processing_lease_is_reclaimed():
    repo = MemoryRepository(lease_seconds=300)
    should, doc_id, _ = repo.claim_review(_review())
    assert should
    # simulate a crashed poll: lease in the past, still PROCESSING
    repo.docs[doc_id]["lock_expires_at"] = PAST
    should2, _, _ = repo.claim_review(_review())
    assert should2 is True
    assert repo.docs[doc_id]["status"] == Status.PROCESSING.value


def test_expired_publishing_lease_can_be_recovered():
    repo = MemoryRepository(lease_seconds=300)
    doc_id = make_doc_id("wb", "review", "R1")
    repo.docs[doc_id] = {
        "status": Status.PUBLISHING.value, "source_id": "R1",
        "lock_expires_at": PAST, "final_answer": "текст", "publish_attempts": 1,
    }
    # a fresh publish attempt is allowed because the previous lease expired
    doc = repo.begin_publish(doc_id)
    assert doc["source_id"] == "R1"
    assert repo.docs[doc_id]["status"] == Status.PUBLISHING.value


def test_active_publishing_lease_blocks_second_publish():
    repo = MemoryRepository(lease_seconds=300)
    doc_id = make_doc_id("wb", "review", "R1")
    repo.docs[doc_id] = {
        "status": Status.PUBLISHING.value, "source_id": "R1",
        "lock_expires_at": "2999-01-01T00:00:00+00:00", "final_answer": "t",
    }
    with pytest.raises(InvalidTransition):
        repo.begin_publish(doc_id)
