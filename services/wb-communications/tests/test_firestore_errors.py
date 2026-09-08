"""C0-2: Firestore/GCP infra errors are transient → webhook 5xx, update kept."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routes.telegram_webhook as wh
from app.config import Secrets
from app.domain.exceptions import FirestoreTransientError
from app.services.pipeline import Deps
from app.services.prompt_service import PromptService
from app.services.repository import is_firestore_transient, translate_fs_errors
from tests.conftest import FakeBQ, FakeOpenAI, FakeTelegram, FakeWB, make_settings


# --- unit: the translator maps google-style transient errors ---------------
class ServiceUnavailable(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


def test_translate_wraps_service_unavailable():
    @translate_fs_errors
    def boom():
        raise ServiceUnavailable("503")

    with pytest.raises(FirestoreTransientError):
        boom()


def test_translate_wraps_deadline_exceeded():
    @translate_fs_errors
    def boom():
        raise DeadlineExceeded("timeout")

    with pytest.raises(FirestoreTransientError):
        boom()


def test_translate_passes_through_value_error():
    @translate_fs_errors
    def boom():
        raise ValueError("business")

    with pytest.raises(ValueError):
        boom()


def test_is_firestore_transient_names():
    assert is_firestore_transient(ServiceUnavailable())
    assert is_firestore_transient(DeadlineExceeded())
    assert not is_firestore_transient(ValueError())


# --- route: a transient repo error → 503 and update NOT marked processed ----
class _TransientRepo:
    """Minimal repo: update de-dup works, but handling raises a transient error."""

    def __init__(self):
        self.completed = []
        self.released = []
        self._updates = {}

    def begin_update(self, uid):
        if str(uid) in self._updates:
            return "duplicate"
        self._updates[str(uid)] = "processing"
        return "new"

    def complete_update(self, uid):
        self.completed.append(uid)

    def release_update(self, uid):
        self.released.append(uid)

    def begin_action(self, doc_id, action):
        raise FirestoreTransientError("firestore transient: ServiceUnavailable")

    # unused in this path
    def list_pending_events(self, limit=100):
        return []


def _fake_deps():
    settings = make_settings()
    settings.__dict__["secrets"] = Secrets(
        openai_api_key="", wb_api_token="", telegram_bot_token="",
        telegram_webhook_secret="whsecret", scheduler_secret="",
    )
    return Deps(settings=settings, repo=_TransientRepo(), wb=FakeWB(), openai=FakeOpenAI(),
                telegram=FakeTelegram(), bq=FakeBQ(), prompts=PromptService("prompts", "reviews_v1"))


def test_transient_firestore_during_callback_returns_503(monkeypatch):
    from fastapi import FastAPI

    deps = _fake_deps()
    monkeypatch.setattr(wh, "get_deps", lambda: deps)
    app = FastAPI()
    app.include_router(wh.router)
    client = TestClient(app)

    update = {"update_id": 77, "callback_query": {
        "id": "cq", "data": "skip:abcdef", "from": {"id": 302044578},
        "message": {"message_id": 1, "chat": {"id": 302044578}}}}
    resp = client.post("/telegram-webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": "whsecret"})
    assert resp.status_code == 503
    assert deps.repo.released == [77]        # released for redelivery
    assert deps.repo.completed == []         # NOT marked processed
