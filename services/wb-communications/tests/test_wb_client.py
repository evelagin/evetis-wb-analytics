import json

import httpx
import pytest

import app.utils.retry as retry_mod
from app.domain.exceptions import WBApiError, WBAuthError, WBRateLimitError
from app.services.wb_client import WBClient
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda *_: None)


def _client(handler, **settings):
    settings = make_settings(**settings)
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.wb_api_base_url)
    return WBClient(settings, "TOKEN", client=http)


def test_get_unanswered_parses_feedbacks():
    def handler(request):
        assert request.url.path == "/api/v1/feedbacks"
        assert request.headers["Authorization"] == "TOKEN"
        assert request.url.params["isAnswered"] == "false"
        return httpx.Response(200, json={"data": {"feedbacks": [{"id": "A"}, {"id": "B"}]}})

    wb = _client(handler)
    assert [f["id"] for f in wb.get_unanswered_feedbacks(take=30)] == ["A", "B"]


def test_pagination_walks_pages_until_short_page():
    # take=2: page0 -> [A,B], page1(skip=2) -> [C] (short) -> stop. total 3.
    def handler(request):
        skip = int(request.url.params["skip"])
        page = {0: [{"id": "A"}, {"id": "B"}], 2: [{"id": "C"}]}.get(skip, [])
        return httpx.Response(200, json={"data": {"feedbacks": page}})

    wb = _client(handler, wb_poll_batch_size=2)
    all_ids = [f["id"] for f in wb.iter_unanswered_feedbacks()]
    assert all_ids == ["A", "B", "C"]


def test_pagination_respects_max_pages():
    # every page is full -> would loop forever; max_pages caps it.
    def handler(request):
        return httpx.Response(200, json={"data": {"feedbacks": [{"id": "x"}, {"id": "y"}]}})

    wb = _client(handler, wb_poll_batch_size=2, wb_max_pages=3, wb_max_items=1000)
    ids = wb.iter_unanswered_feedbacks()
    assert len(ids) == 6  # 3 pages * 2


def test_publish_sends_id_and_text():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": "ok"})

    wb = _client(handler)
    resp = wb.publish_answer("REV1", "ответ")
    # first answer per current WB spec: POST /api/v1/feedbacks/answer
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/feedbacks/answer"
    assert seen["body"] == {"id": "REV1", "text": "ответ"}
    assert resp == {"result": "ok"}


def test_publish_endpoint_is_configurable():
    # a deploy can still override method/path (e.g. if Swagger differs)
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    wb = _client(handler, wb_answer_method="PATCH", wb_answer_path="/api/v1/feedbacks")
    wb.publish_answer("R", "t")
    assert seen["method"] == "PATCH" and seen["path"] == "/api/v1/feedbacks"


def test_rate_limit_raises_after_retries():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    wb = _client(handler)
    with pytest.raises(WBRateLimitError):
        wb.get_unanswered_feedbacks()


def test_auth_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={})

    wb = _client(handler)
    with pytest.raises(WBAuthError):
        wb.get_unanswered_feedbacks()
    assert calls["n"] == 1  # 401 must NOT be retried


def test_client_4xx_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(422, json={})

    wb = _client(handler)
    with pytest.raises(WBApiError):
        wb.publish_answer("R", "t")
    assert calls["n"] == 1
