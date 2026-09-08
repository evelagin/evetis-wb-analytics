"""Operator-friendly WB publish error messages (code + plain-language reason)."""
from __future__ import annotations

import pytest

from app.domain.exceptions import WBApiError, WBAuthError, WBRateLimitError, WBServerError
from app.domain.models import make_doc_id
from app.services.pipeline import handle_update, run_poll, _wb_publish_error_message
from tests.conftest import SAMPLE_FEEDBACK, make_deps

DOC_ID = make_doc_id("wb", "review", "REVIEW_1")


def _cb(action):
    return {"update_id": 1, "callback_query": {
        "id": "cq1", "data": f"{action}:{DOC_ID}", "from": {"id": 302044578},
        "message": {"message_id": 1001, "chat": {"id": 302044578}}}}


def _last_edit_text(deps):
    # FakeTelegram.edits holds (message_id, text)
    return deps.telegram.edits[-1][1]


@pytest.mark.parametrize("exc, code, needle", [
    (WBAuthError("auth", status_code=403), 403, "прав API-токена"),
    (WBAuthError("auth", status_code=401), 401, "недействителен"),
    (WBApiError("unproc", status_code=422), 422, "уже имеет ответ"),
    (WBApiError("notfound", status_code=404), 404, "не найден"),
    (WBRateLimitError("rl", status_code=429), 429, "лимит запросов"),
    (WBServerError("srv", status_code=503), 503, "Временная ошибка"),
])
def test_publish_error_message_has_code_and_reason(exc, code, needle):
    deps = make_deps([dict(SAMPLE_FEEDBACK)], wb_publish_enabled=True, publish_error=exc)
    run_poll(deps)
    result = handle_update(deps, _cb("pub"))
    assert result["status"] == "publish_failed" and result["code"] == code
    msg = _last_edit_text(deps)
    assert f"Wildberries вернул {code}" in msg
    assert needle in msg
    assert deps.wb.published == []  # nothing published on failure


def test_publish_error_message_unit():
    # unknown 5xx -> generic transient hint; no code -> generic header
    assert "500" in _wb_publish_error_message(WBServerError("x", status_code=500))
    assert "Временная ошибка" in _wb_publish_error_message(WBServerError("x", status_code=500))
    assert _wb_publish_error_message(WBApiError("x")).startswith("❌ Ошибка публикации в WB")


def test_successful_publish_still_clean():
    deps = make_deps([dict(SAMPLE_FEEDBACK)], wb_publish_enabled=True)
    run_poll(deps)
    assert handle_update(deps, _cb("pub"))["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", "Ответ-вариант-1")]
