"""Shared fakes and a Deps factory for offline pipeline tests."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.domain.models import GenerationResult
from app.services.pipeline import Deps
from app.services.prompt_service import PromptService
from app.services.repository import MemoryRepository


class FakeWB:
    """Offline WB double. Questions keep an authoritative answer state that
    ``get_question`` reads back, so publication verification is testable:

    * ``question_visible_after_publish`` — WB shows the answer right after an
      accepted PATCH (False = accepted but hidden, e.g. pre-moderation);
    * ``question_publish_timeout`` — the PATCH raises WBPublishOutcomeUnknown;
      ``question_timeout_lands`` decides whether WB nevertheless stored it.
    """

    def __init__(self, feedbacks=None, fail_publish=False, publish_error=None,
                 questions=None, question_publish_error=None):
        self._feedbacks = feedbacks or []
        self._questions = questions or []
        self.published = []
        self.published_questions = []
        self.fail_publish = fail_publish
        self.publish_error = publish_error  # exception instance to raise on review publish
        self.question_publish_error = question_publish_error
        self.question_answers: dict = {}  # question_id -> answer text on WB
        self.question_visible_after_publish = True
        self.question_publish_timeout = False
        self.question_timeout_lands = False
        self.get_question_error = None
        self.get_question_calls = 0

    def iter_unanswered_feedbacks(self):
        return list(self._feedbacks)

    def get_unanswered_feedbacks(self, take=None, skip=0):
        return list(self._feedbacks)

    def publish_answer(self, feedback_id, text):
        if self.publish_error is not None:
            raise self.publish_error
        if self.fail_publish:
            from app.domain.exceptions import WBServerError

            raise WBServerError("boom", status_code=500)
        self.published.append((feedback_id, text))
        return {"ok": True}

    # --- questions ---
    def iter_unanswered_questions(self):
        return list(self._questions)

    def get_unanswered_questions(self, take=None, skip=0):
        return list(self._questions)

    def publish_question_answer(self, question_id, text, state=None):
        if self.question_publish_error is not None:
            raise self.question_publish_error
        self.published_questions.append((question_id, text, state))
        if self.question_publish_timeout:
            from app.domain.exceptions import WBPublishOutcomeUnknown

            if self.question_timeout_lands:
                self.question_answers[question_id] = text
            exc = WBPublishOutcomeUnknown("WB publish outcome unknown (ReadTimeout)")
            exc.request_sha256 = "sha-test"
            raise exc
        if self.question_visible_after_publish:
            self.question_answers[question_id] = text
        return {"status_code": 200, "request_sha256": "sha-test",
                "response": {"data": None, "error": False, "errorText": "", "additionalErrors": None}}

    def get_question(self, question_id):
        self.get_question_calls += 1
        if self.get_question_error is not None:
            raise self.get_question_error
        text = self.question_answers.get(question_id)
        return {"id": question_id, "answer": ({"text": text, "editable": True} if text else None)}


class FakeOpenAI:
    def __init__(self):
        self.calls = 0

    def generate_answer(self, system, user):
        self.calls += 1
        return GenerationResult(
            text=f"Ответ-вариант-{self.calls}", model="gpt-4.1-mini",
            prompt_version="reviews_v1", usage={"input_tokens": 10, "output_tokens": 5},
            latency_ms=42, request_id="resp_test",
        )


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.force_replies = []
        self.edits = []
        self.edit_markups = []  # (message_id, reply_markup) parallel to `edits`
        self.fail_edits = False  # simulate Telegram refusing editMessageText
        self.acks = []
        self._id = 1000

    def send_message(self, chat_id, text, reply_markup=None):
        self._id += 1
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": self._id}

    def send_force_reply(self, chat_id, text):
        self._id += 1
        self.force_replies.append((chat_id, text))
        return {"message_id": self._id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.fail_edits:
            from app.domain.exceptions import TelegramError
            raise TelegramError("Bad Request: message to edit not found")
        self.edits.append((message_id, text))
        self.edit_markups.append((message_id, reply_markup))
        return {"message_id": message_id}

    def answer_callback_query(self, cq_id, text=""):
        self.acks.append((cq_id, text))
        return {"ok": True}


class FakeBQ:
    def __init__(self):
        self.events = []
        self.current = []
        self.shadow = []

    def insert_event(self, event):
        self.events.append(event)
        return True

    def upsert_current(self, row):
        self.current.append(row)
        return True

    def insert_shadow(self, row):
        self.shadow.append(row)
        return True


class FakeShadowRepo:
    """Captures shadow rows; can simulate a BigQuery outage.

    ``fail=True``  -> insert_shadow raises (unexpected error path).
    ``ok=False``   -> insert_shadow records the row but returns False, i.e. a
                      persistence failure the pipeline must detect and log.
    """

    def __init__(self, fail=False, ok=True):
        self.rows = []
        self.fail = fail
        self.ok = ok

    def insert_shadow(self, row):
        if self.fail:
            raise RuntimeError("shadow bq down")
        self.rows.append(row)
        return self.ok


class RaisingShadowEngine:
    """A shadow engine whose evaluate() always raises — to prove isolation."""

    def evaluate(self, review, *, old_answer, old_prompt_version):
        raise RuntimeError("v2 exploded")


class FailingOpenAI:
    """OpenAI double that always fails — used only for the v2 shadow client so the
    main pipeline keeps a healthy client."""

    def __init__(self):
        self.calls = 0

    def generate_answer(self, system, user):
        self.calls += 1
        from app.domain.exceptions import OpenAIError

        raise OpenAIError("v2 model down")


def make_shadow_engine(settings, openai=None):
    """Real ShadowEngineService over the real Communication Engine v2, with an
    injectable OpenAI double."""
    from app.services.engine_prompt_service import EnginePromptService
    from app.services.shadow_engine import ShadowEngineService

    return ShadowEngineService(
        engine_service=EnginePromptService.create(),
        openai=openai or FakeOpenAI(),
        settings=settings,
    )


def make_settings(**overrides) -> Settings:
    base = dict(
        gcp_project_id="",
        telegram_chat_id="302044578",
        telegram_allowed_user_ids={"302044578"},
        wb_min_interval_seconds=0.0,
        wb_publish_enabled=True,  # tests exercise publishing; gate tested explicitly
        wb_question_verify_delay_seconds=0.0,  # bounded read-back without real sleeps
    )
    base.update(overrides)
    return Settings(**base)


def make_engine_service():
    from app.services.engine_prompt_service import EnginePromptService

    return EnginePromptService.create()


def make_deps(
    feedbacks=None,
    fail_publish=False,
    *,
    shadow=False,
    shadow_openai=None,
    shadow_engine=None,
    shadow_repo=None,
    primary=False,
    engine=None,
    openai=None,
    publish_error=None,
    questions=None,
    question_publish_error=None,
    **settings_overrides,
):
    if shadow and "communication_engine_v2_enabled" not in settings_overrides:
        settings_overrides["communication_engine_v2_enabled"] = True
    if primary:
        settings_overrides.setdefault("communication_engine_v2_enabled", True)
        settings_overrides.setdefault("communication_engine_v2_primary", True)
    if settings_overrides.get("wb_questions_enabled"):
        settings_overrides.setdefault("communication_engine_v2_enabled", True)
    settings = make_settings(**settings_overrides)

    if shadow:
        if shadow_engine is None:
            shadow_engine = make_shadow_engine(settings, openai=shadow_openai)
        if shadow_repo is None:
            shadow_repo = FakeShadowRepo()
    # Build the engine when reviews are v2-primary OR questions are enabled.
    if engine is None and (primary or getattr(settings, "wb_questions_enabled", False)):
        engine = make_engine_service()

    return Deps(
        settings=settings,
        repo=MemoryRepository(),
        wb=FakeWB(feedbacks, fail_publish=fail_publish, publish_error=publish_error,
                  questions=questions, question_publish_error=question_publish_error),
        openai=openai or FakeOpenAI(),
        telegram=FakeTelegram(),
        bq=FakeBQ(),
        prompts=PromptService("prompts", "reviews_v1"),
        engine=engine,
        shadow_engine=shadow_engine,
        shadow_repo=shadow_repo,
    )


SAMPLE_FEEDBACK = {
    "id": "REVIEW_1",
    "productValuation": 5,
    "createdDate": "2026-07-20T10:00:00Z",
    "text": "Отличный крем!",
    "userName": "Анна",
    "productDetails": {
        "productName": "Крем для лица увлажняющий",
        "supplierArticle": "438775437",
        "nmId": 111,
        "imtId": 222,
        "brandName": "EVETIS",
    },
}


@pytest.fixture
def deps():
    return make_deps([dict(SAMPLE_FEEDBACK)])
