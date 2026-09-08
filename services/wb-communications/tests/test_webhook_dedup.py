"""C0-5: update de-dup with received/processing/processed + lease recovery."""
from __future__ import annotations

from app.services.repository import MemoryRepository

PAST = "2000-01-01T00:00:00+00:00"


def test_new_then_duplicate_while_processing():
    repo = MemoryRepository(update_lease_seconds=120)
    assert repo.begin_update(10) == "new"
    assert repo.begin_update(10) == "duplicate"  # still processing


def test_processed_is_duplicate():
    repo = MemoryRepository()
    repo.begin_update(11)
    repo.complete_update(11)
    assert repo.begin_update(11) == "duplicate"


def test_released_update_can_be_reprocessed():
    repo = MemoryRepository()
    repo.begin_update(12)
    repo.release_update(12)  # transient failure -> allow Telegram redelivery
    assert repo.begin_update(12) == "new"


def test_expired_processing_lease_allows_reclaim():
    repo = MemoryRepository()
    repo.begin_update(13)
    repo.updates["13"]["lock_expires_at"] = PAST  # worker crashed mid-handling
    assert repo.begin_update(13) == "new"
