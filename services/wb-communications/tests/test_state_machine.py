"""C0-3: stale callbacks are rejected by the action state machine."""
from __future__ import annotations

from app.domain.models import make_doc_id
from app.domain.statuses import Status
from app.services.pipeline import handle_update, run_poll
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")


def _cb(update_id, action):
    return {"update_id": update_id, "callback_query": {
        "id": f"cq{update_id}", "data": f"{action}:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


def _published_deps():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "pub"))
    assert deps.repo.get(DOC_ID)["status"] == Status.PUBLISHED.value
    return deps


def test_skip_on_published_is_stale():
    deps = _published_deps()
    assert handle_update(deps, _cb(2, "skip"))["status"] == "stale"
    assert deps.repo.get(DOC_ID)["status"] == Status.PUBLISHED.value


def test_edit_on_published_is_stale():
    deps = _published_deps()
    assert handle_update(deps, _cb(2, "edit"))["status"] == "stale"
    assert deps.repo.get_editing_session(302044578) is None


def test_regenerate_on_published_is_stale():
    deps = _published_deps()
    before = deps.openai.calls
    assert handle_update(deps, _cb(2, "regen"))["status"] == "stale"
    assert deps.openai.calls == before  # no wasted OpenAI call


def test_show_is_always_allowed():
    deps = _published_deps()
    assert handle_update(deps, _cb(2, "show"))["status"] == "shown"


def test_actions_on_skipped_are_stale_but_show_ok():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "skip"))
    assert handle_update(deps, _cb(2, "edit"))["status"] == "stale"
    assert handle_update(deps, _cb(3, "regen"))["status"] == "stale"
    assert handle_update(deps, _cb(4, "show"))["status"] == "shown"


def test_restore_moves_skipped_back_to_pending():
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    run_poll(deps)
    handle_update(deps, _cb(1, "skip"))
    deps.repo.begin_action(DOC_ID, "restore")
    assert deps.repo.get(DOC_ID)["status"] == Status.PENDING_APPROVAL.value
