from types import SimpleNamespace

import pytest

import app.utils.retry as retry_mod
from app.domain.exceptions import OpenAIError, OpenAITransientError
from app.services.openai_client import OpenAIClient
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda *_: None)


class _ApiError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _Responses:
    def __init__(self, reject_temperature=False, raise_status=None):
        self.reject_temperature = reject_temperature
        self.raise_status = raise_status
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_status is not None:
            raise _ApiError(self.raise_status)
        if self.reject_temperature and "temperature" in kwargs:
            raise Exception("Unsupported parameter: 'temperature' is not supported")
        return SimpleNamespace(
            output_text="Готовый ответ",
            usage=SimpleNamespace(input_tokens=12, output_tokens=8, total_tokens=20),
            id="resp_123",
        )


class _Client:
    def __init__(self, responses):
        self.responses = responses


def test_generate_answer_extracts_text_and_usage():
    client = OpenAIClient(make_settings(), "sk-test", client=_Client(_Responses()))
    gen = client.generate_answer("system", "user")
    assert gen.text == "Готовый ответ"
    assert gen.usage["input_tokens"] == 12
    assert gen.model == "gpt-4.1-mini"
    assert gen.request_id == "resp_123"


def test_temperature_fallback_when_model_rejects_it():
    responses = _Responses(reject_temperature=True)
    client = OpenAIClient(make_settings(), "sk-test", client=_Client(responses))
    gen = client.generate_answer("system", "user")
    assert gen.text == "Готовый ответ"
    assert "temperature" in responses.calls[0]
    assert "temperature" not in responses.calls[1]


def test_permanent_400_is_not_retried():
    responses = _Responses(raise_status=400)
    client = OpenAIClient(make_settings(), "sk-test", client=_Client(responses))
    with pytest.raises(OpenAIError):
        client.generate_answer("s", "u")
    assert len(responses.calls) == 1  # no retry on 400


def test_transient_500_is_retried_then_raises_transient():
    responses = _Responses(raise_status=500)
    client = OpenAIClient(make_settings(), "sk-test", client=_Client(responses))
    with pytest.raises(OpenAITransientError):
        client.generate_answer("s", "u")
    assert len(responses.calls) == 3  # initial + 2 retries
