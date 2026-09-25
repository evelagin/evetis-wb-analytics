"""Редакция секретоподобных значений ДО любой записи на диск, в артефакты и в `autonomy-state`.

Инцидент M6 (2026-09-25): упавший тест вывел repr stdout `gcloud auth print-access-token`, хвост
попал в `baseline.json` и был закоммичен в ветку состояния. Хвосты вывода, ошибки агента и
доказательства — недоверенный текст: в нём может оказаться что угодно.

Правила:
  * `redact_text` заменяет каждое совпадение на `[REDACTED:<вид>]`, включая ОБРЕЗАННЫЕ токены
    (pytest вырезает середину длинного repr — префикс и хвост всё равно секрет);
  * `ensure_clean` повторно ищет — остаток означает ошибку редакции, и запись НЕ выполняется
    (fail closed: `RedactionError`, а не запись «как есть»);
  * `redact_obj` рекурсивно обрабатывает JSON-документы (ключи и значения);
  * дифф кандидата не редактируется (это изменило бы код): `find_secrets` → кандидат UNSAFE.
"""
from __future__ import annotations

import json
import re

_STOP = r"[^\s'\"`\\,;)\]}]"
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----[\s\S]*?(?:-----END[ A-Z0-9]*PRIVATE KEY-----|\Z)")),
    ("private_key_field", re.compile(r"\"private_key(?:_id)?\"\s*:\s*\"[^\"]*\"")),
    ("google_access_token", re.compile(r"\bya29\." + _STOP + r"*")),
    ("anthropic_key", re.compile(r"\bsk-ant-" + _STOP + r"*")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})" + _STOP + r"*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,2}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-" + _STOP + r"+")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
]


class RedactionError(RuntimeError):
    """Остаток секретоподобного текста после редакции — запись запрещена."""


def find_secrets(text: str) -> list[str]:
    return sorted({name for name, rx in PATTERNS if rx.search(text or "")})


def redact_text(text: str) -> str:
    if not text:
        return text
    for name, rx in PATTERNS:
        text = rx.sub(f"[REDACTED:{name}]", text)
    return text


def ensure_clean(text: str) -> str:
    left = find_secrets(text)
    if left:
        raise RedactionError(f"после редакции остались совпадения: {left} — запись отменена")
    return text


def redact_obj(obj):
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {redact_text(k) if isinstance(k, str) else k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)
    return obj


def safe_dumps(doc, **kw) -> str:
    """JSON для записи: редакция + повторная проверка сериализованного текста."""
    return ensure_clean(json.dumps(redact_obj(doc), ensure_ascii=False, **kw))


def safe_text(text: str) -> str:
    return ensure_clean(redact_text(text))
