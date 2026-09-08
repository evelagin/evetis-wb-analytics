"""C0-1: edit/regenerate races cannot write a stale draft back to pending."""
from __future__ import annotations

import pytest

from app.domain.exceptions import InvalidTransition
from app.domain.models import GenerationResult, make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")
PAST = "2000-01-01T00:00:00+00:00"


def _cb(uid, action):
    return {"update_id": uid, "callback_query": {
        "id": f"cq{uid}", "data": f"{action}:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


def _gen(n=9):
    return GenerationResult(text=f"gen{n}", model="m", prompt_version="v", usage={}, latency_ms=1)


def _ready():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    return deps


# 1. regenerate in-flight vs publish
def test_regenerate_inflight_blocks_publish():
    deps = _ready()
    deps.repo.begin_regenerate(DOC_ID)  # lock held (REGENERATING)
    assert handle_update(deps, _cb(1, "pub"))["status"] == "stale"
    assert deps.wb.published == []
    assert deps.repo.get(DOC_ID)["status"] == Status.REGENERATING.value


# 2. regenerate in-flight vs skip
def test_regenerate_inflight_blocks_skip():
    deps = _ready()
    deps.repo.begin_regenerate(DOC_ID)
    assert handle_update(deps, _cb(1, "skip"))["status"] == "stale"
    assert deps.repo.get(DOC_ID)["status"] == Status.REGENERATING.value


# 3. edit session vs publish
def test_edit_inflight_blocks_publish():
    deps = _ready()
    deps.repo.begin_edit(DOC_ID)  # lock held (EDITING)
    assert handle_update(deps, _cb(1, "pub"))["status"] == "stale"
    assert deps.wb.published == []


# 4. edit session vs skip
def test_edit_inflight_blocks_skip():
    deps = _ready()
    deps.repo.begin_edit(DOC_ID)
    assert handle_update(deps, _cb(1, "skip"))["status"] == "stale"
    assert deps.repo.get(DOC_ID)["status"] == Status.EDITING.value


# 5. two concurrent regenerate — only one wins
def test_two_concurrent_regenerate():
    deps = _ready()
    deps.repo.begin_regenerate(DOC_ID)  # first regenerate holds the lock
    assert handle_update(deps, _cb(1, "regen"))["status"] == "stale"


# 5b. a committed regenerate with a stale token is rejected
def test_regenerate_commit_with_stale_token_rejected():
    deps = _ready()
    doc, token_a = deps.repo.begin_regenerate(DOC_ID)
    # a recovery path takes over (lease expired) with a new token
    deps.repo.docs[DOC_ID]["lock_expires_at"] = PAST
    _, token_b = deps.repo.begin_regenerate(DOC_ID)
    deps.repo.commit_regenerate(DOC_ID, _gen(2), token_b)  # winner commits
    with pytest.raises(InvalidTransition):
        deps.repo.commit_regenerate(DOC_ID, _gen(1), token_a)  # loser rejected


# 6. stale edit after another manual edit changed the document
def test_stale_edit_after_another_edit_is_rejected():
    deps = _ready()
    _, token_a, gen_a = deps.repo.begin_edit(DOC_ID)         # session A
    deps.repo.docs[DOC_ID]["lock_expires_at"] = PAST         # A abandoned
    _, token_b, gen_b = deps.repo.begin_edit(DOC_ID)         # session B takes over
    deps.repo.commit_manual_answer(DOC_ID, "B win", token_b, gen_b)
    assert deps.repo.get(DOC_ID)["final_answer"] == "B win"
    # A's late reply must be rejected (status no longer EDITING / version changed)
    with pytest.raises(InvalidTransition):
        deps.repo.commit_manual_answer(DOC_ID, "A late", token_a, gen_a)
