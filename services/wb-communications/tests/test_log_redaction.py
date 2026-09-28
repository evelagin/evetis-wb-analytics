"""Инцидент 2026-09-27: токен Telegram-бота попадал в Cloud Logging.

httpx писал INFO `HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage`, а шаблон
`\\b\\d{6,}:` не срабатывал (между "bot" и цифрами нет границы слова). Все значения здесь
СИНТЕТИЧЕСКИЕ — настоящий токен в тестах не используется.
"""
from __future__ import annotations

import io
import json
import logging
import sys
import urllib.parse
import warnings

import httpx
import pytest
import respx

from app.utils import logging as L

FAKE = "123456789:AAFAKE_fake-token-for-tests_0123456789abcdef"
FAKE_TAIL = FAKE.split(":", 1)[1]
URL = f"https://api.telegram.org/bot{FAKE}/sendMessage"


@pytest.fixture
def logs(monkeypatch):
    """Боевая конфигурация журналирования в собственный буфер; после теста — исходное состояние логгеров."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)              # ошибки форматирования logging пишутся в stderr
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, set(L._known_secrets))
    saved_named = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level, logging.getLogger(n).propagate)
                   for n in (*L._URL_LOGGING_LIBRARIES, *L._SERVER_LOGGERS, "py.warnings")}
    saved_filters = list(logging.getLogger("httpx").filters)
    L._known_secrets.clear()
    L.configure_logging("INFO")

    def out() -> str:
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return text
    yield out
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    L._known_secrets.clear()
    L._known_secrets.update(saved[2])
    for n, (h, lvl, prop) in saved_named.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = h
        lg.setLevel(lvl)
        lg.propagate = prop
    logging.getLogger("httpx").filters[:] = saved_filters
    logging.captureWarnings(False)
    logging.raiseExceptions = True


def assert_clean(text: str, *, allow_empty: bool = False) -> None:
    assert allow_empty or text.strip(), "журнал пуст — проверка отсутствия токена была бы пустой"
    assert FAKE not in text and FAKE_TAIL not in text
    assert urllib.parse.quote(FAKE, safe="") not in text


def test_plain_message_unregistered_token(logs):
    logging.getLogger("app.x").info("token is %s", FAKE)
    text = logs()
    assert_clean(text)
    assert "<telegram_token>" in text


def test_bot_url_the_incident_shape(logs):
    logging.getLogger("app.x").info("HTTP Request: POST %s \"HTTP/1.1 200 OK\"", URL)
    text = logs()
    assert_clean(text)
    assert "api.telegram.org/bot<telegram_token>/sendMessage" in text


def test_url_encoded_token(logs):
    logging.getLogger("app.x").warning("callback %s", urllib.parse.quote(URL, safe=""))
    assert_clean(logs())


def test_exception_carrying_url(logs):
    try:
        raise RuntimeError(f"Client error for url '{URL}'")
    except RuntimeError:
        logging.getLogger("app.x").exception("send failed")
    text = logs()
    assert_clean(text)
    assert json.loads(text.strip().splitlines()[-1])["exception"]


def test_stack_info_is_redacted(logs):
    logging.getLogger("app.x").info("with stack %s", FAKE, stack_info=True)
    assert_clean(logs())


def test_httpx_telegram_lines_dropped_by_host_wb_lines_kept(logs):
    logging.getLogger("httpx").info("HTTP Request: POST %s \"HTTP/1.1 200 OK\"", URL)
    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/botWHATEVER-FORMAT/x")
    logging.getLogger("httpcore").info("send_request_headers %s", URL)
    assert logs() == ""                                      # по хосту, без регулярных выражений
    logging.getLogger("httpx").info("HTTP Request: POST https://feedbacks-api.wildberries.ru/api/v1/feedbacks/answer \"HTTP/1.1 204\"")
    text = logs()
    assert "feedbacks-api.wildberries.ru" in text              # доказательство публикации в WB сохранено


def test_httpx_line_that_cannot_be_formatted_is_dropped(logs):
    logging.getLogger("httpx").info("broken %s %s", URL)      # неверное число аргументов
    assert FAKE not in logs()


@respx.mock
def test_real_httpx_call_through_telegram_client_leaks_nothing(logs):
    from app.services.telegram_client import TelegramClient
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    TelegramClient(FAKE).send_message(1, "hi")
    assert_clean(logs(), allow_empty=True)          # httpx молчит, клиент URL не пишет


@respx.mock
def test_transport_error_becomes_token_free_telegram_error(logs):
    from app.domain.exceptions import TelegramError
    from app.services.telegram_client import TelegramClient
    respx.post(URL).mock(side_effect=httpx.ConnectError(f"cannot connect to {URL}"))
    with pytest.raises(TelegramError) as ei:
        TelegramClient(FAKE).send_message(1, "hi")
    assert FAKE not in str(ei.value) and FAKE_TAIL not in str(ei.value) and "ConnectError" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None       # исходная ошибка httpx не привязана
    logging.getLogger("app.x").exception("poll item failed: %s", ei.value, exc_info=ei.value)
    assert_clean(logs())


def test_structured_fields_nested(logs):
    L.log_event(logging.getLogger("app.x"), "info", "event", url=URL, nested={"u": URL, "l": [URL, 1]})
    assert_clean(logs())


def test_registered_secret_masked_in_any_format(logs):
    wb = "wb-api-token-SYNTHETIC-9f8e7d6c5b4a"               # формат, которого нет ни в одном шаблоне
    L.register_secret(wb)
    L.register_secret(FAKE)
    logging.getLogger("app.x").info("h=%s|%s|tail=%s|enc=%s", wb, FAKE, FAKE_TAIL, urllib.parse.quote(FAKE, safe=""))
    text = logs()
    assert wb not in text
    assert_clean(text)


def test_short_values_are_not_registered():
    before = set(L._known_secrets)
    L.register_secret("abc")
    L.register_secret("")
    L.register_secret(None)
    assert L._known_secrets == before


def test_uvicorn_loggers_go_through_redaction(logs):
    logging.getLogger("uvicorn.error").error("unhandled: %s", URL)
    logging.getLogger("uvicorn.access").info('1.2.3.4 - "POST %s HTTP/1.1" 200', URL)
    text = logs()
    assert_clean(text)
    assert all(json.loads(line)["severity"] for line in text.strip().splitlines())   # JSON, а не plain text


def test_python_warnings_go_through_redaction(logs):
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(f"deprecated call to {URL}")
    assert_clean(logs())


def test_get_secret_registers_value(logs, monkeypatch):
    from app.services import secrets
    monkeypatch.setenv("EVETIS_SYNTHETIC_SECRET", "synthetic-value-7777777777")
    assert secrets.get_secret("EVETIS_SYNTHETIC_SECRET", "p") == "synthetic-value-7777777777"
    logging.getLogger("app.x").info("oops synthetic-value-7777777777")
    assert "synthetic-value-7777777777" not in logs()


def test_registered_secret_url_encoded_form_without_any_pattern(logs):
    odd = "wb/SYNTH+secret==value"                           # ни один шаблон его не узнаёт
    L.register_secret(odd)
    logging.getLogger("app.x").info("q=%s", urllib.parse.quote(odd, safe=""))
    text = logs()
    assert text and urllib.parse.quote(odd, safe="") not in text and "<redacted>" in text


def _logs_with_token_in_source():
    logging.getLogger("app.x").info("stack", stack_info=True)  # 123456789:AAFAKE_fake-token-for-tests_0123456789abcdef


def test_stack_with_token_in_source_line(logs):
    _logs_with_token_in_source()
    text = logs()
    assert "stack" in json.loads(text.strip().splitlines()[-1])
    assert_clean(text)


def test_extra_fields_redacted_by_formatter_even_without_log_event(logs):
    logging.getLogger("app.x").info("raw extra", extra={"extra_fields": {"url": URL, "n": [URL]}})
    assert_clean(logs())


def test_secret_manager_value_is_registered(logs, monkeypatch):
    from app.services import secrets

    class Payload:
        data = b"sm-synthetic-secret-55555555"

    class Client:
        def access_secret_version(self, request):
            return type("R", (), {"payload": Payload()})()
    monkeypatch.delenv("EVETIS_SM_SYNTHETIC", raising=False)
    monkeypatch.setattr(secrets, "_client", lambda: Client())
    assert secrets.get_secret("EVETIS_SM_SYNTHETIC", "p") == "sm-synthetic-secret-55555555"
    logging.getLogger("app.x").info("leak sm-synthetic-secret-55555555")
    text = logs()
    assert text and "sm-synthetic-secret-55555555" not in text



def test_unformattable_record_on_any_logger_leaks_nothing(logs):
    logging.getLogger("app.x").info("two args expected %s %s", FAKE)          # ошибка форматирования
    text = logs()
    assert "could not be formatted" in text
    assert_clean(text)


def test_unserialisable_extra_field_is_redacted_not_dumped_raw(logs):
    import datetime

    class Odd:
        def __str__(self):
            return f"odd {URL}"
    logging.getLogger("app.x").info("x", extra={"extra_fields": {"when": datetime.datetime(2026, 9, 28), "o": Odd()}})
    text = logs()
    assert "2026-09-28" in text
    assert_clean(text)


def test_secrets_repr_hides_values():
    from app.config import Secrets
    s = Secrets(openai_api_key="sk-SYNTH-1111111111", wb_api_token="wb-SYNTH-2222222222",
                telegram_bot_token=FAKE, telegram_webhook_secret="wh-SYNTH-3333333333",
                scheduler_secret="sch-SYNTH-4444444444")
    r = repr(s)
    assert FAKE not in r and "SYNTH" not in r


def test_admin_token_from_env_is_registered(logs, monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("ADMIN_TOKEN", "admin-SYNTHETIC-token-12345")
    s = Settings()
    assert s.admin_token == "admin-SYNTHETIC-token-12345" and "admin-SYNTHETIC" not in repr(s)
    logging.getLogger("app.x").info("admin %s", s.admin_token)
    text = logs()
    assert text and "admin-SYNTHETIC-token-12345" not in text


def test_persisted_error_message_is_redacted():
    from app.services import pipeline
    L.register_secret(FAKE)
    captured = []

    class Repo:
        def enqueue_event(self, payload):
            captured.append(payload)

    class Deps:
        repo = Repo()
    pipeline._emit_event(Deps(), {"source_id": "s"}, "d", pipeline.EventType.FAILED, error_message=f"boom {URL}")
    assert captured and FAKE not in captured[0]["error_message"] and FAKE_TAIL not in captured[0]["error_message"]
