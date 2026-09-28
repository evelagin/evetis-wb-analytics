"""Manual-edit publish gate: OFF by default (no behaviour change); ON only by owner decision."""
from __future__ import annotations

from app.services.pipeline import handle_update
from app.v3.engine import V3Engine
from app.v3.shadow import MemoryShadowStore, V3Runtime
from tests.conftest import SAMPLE_FEEDBACK, make_deps
from tests.v3.test_shadow import Settings, Writer


def _deps_with_manual(snap, enforce: bool, text: str):
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    from app.services.pipeline import run_poll
    run_poll(deps)
    doc_id = next(iter(deps.repo.docs))
    d = deps.repo.docs[doc_id]
    d["final_answer"] = text
    d.setdefault("answer_versions", []).append({"source": "manual", "text": text})
    deps.settings.wb_publish_enabled = True
    deps.settings.v3_enforce_manual_edit_verifier = enforce
    deps.v3 = V3Runtime(engine=V3Engine(snap), store=MemoryShadowStore({}), writer=Writer(), settings=Settings())
    return deps, doc_id


def _cb(doc_id):
    return {"update_id": 900, "callback_query": {"id": "cq", "data": f"pub:{doc_id}", "from": {"id": 302044578},
                                                 "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


BAD = "Это адаптация кожи, продолжайте пользоваться, можно даже беременным."


def test_flag_off_publication_unchanged(snap):
    deps, doc_id = _deps_with_manual(snap, False, BAD)
    handle_update(deps, _cb(doc_id))
    assert deps.wb.published and deps.wb.published[-1][1] == BAD   # v2 behaviour exactly as before


def test_flag_on_blocks_manual_text_without_state_change(snap):
    deps, doc_id = _deps_with_manual(snap, True, BAD)
    status_before = deps.repo.docs[doc_id]["status"]
    r = handle_update(deps, _cb(doc_id))
    assert r["status"] == "v3_blocked" and not deps.wb.published
    assert deps.repo.docs[doc_id]["status"] == status_before
    assert "не прошёл проверку" in deps.telegram.sent[-1][1]


def test_flag_on_allows_clean_manual_text(snap):
    deps, doc_id = _deps_with_manual(snap, True, "Спасибо за отзыв! Рады, что понравилось.")
    handle_update(deps, _cb(doc_id))
    assert deps.wb.published
