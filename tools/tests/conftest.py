"""Общие ограничения тестов tools/tests.

Ни один тест не должен дойти до Anthropic (inference и списание credits запрещены в тестах, ACK
2026-09-26): на всю сессию базовый URL Anthropic указывает на закрытый локальный порт, а учётные
данные и переменные федерации удалены из окружения. Даже случайный запуск настоящего `claude`
завершится ошибкой соединения, а не запросом к модели. Отдельные тесты могут выставлять переменные
через monkeypatch — после них восстанавливается это же безопасное значение.
"""
from __future__ import annotations

import os

import pytest

BLOCKED_BASE_URL = "http://127.0.0.1:9"
DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN")


@pytest.fixture(autouse=True, scope="session")
def _no_anthropic_network():
    saved = {k: os.environ.get(k) for k in (*DROP, "ANTHROPIC_BASE_URL")}
    os.environ["ANTHROPIC_BASE_URL"] = BLOCKED_BASE_URL
    for k in DROP:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
