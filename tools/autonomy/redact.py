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
  * дифф кандидата не редактируется (это изменило бы код): секретоподобное в ДОБАВЛЕННЫХ строках
    (`diff_added_secrets`) → кандидат UNSAFE; хвосты сначала редактируются целиком, потом режутся.
"""
from __future__ import annotations

import json
import re

_STOP = r"[^\s'\"`\\,;)\]}]"
_L = r"(?<![A-Za-z0-9])"          # не \b: «token_ya29.…» — «_» словесный символ, \b там не сработал бы
_DOT = r"(?:\.|%2[Ee]|\\u002[Ee])"  # точка как есть, URL-кодированная и JSON-экранированная
SENSITIVE_KEYS = {"private_key", "private_key_id", "access_token", "refresh_token", "id_token", "client_secret",
                  "api_key", "password", "token", "assertion", "subjectToken", "subject_token"}
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----[\s\S]*?(?:-----END[ A-Z0-9]*PRIVATE KEY-----|\Z)")),
    ("secret_field", re.compile(r"\"(?:" + "|".join(sorted(SENSITIVE_KEYS)) + r")\"\s*:\s*\"(?!\[REDACTED:)[^\"]+\"")),
    ("google_access_token", re.compile(_L + r"ya29" + _DOT + r"[A-Za-z0-9_-]" + _STOP + r"*")),
    ("google_refresh_token", re.compile(_L + r"1//0[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(_L + r"sk-ant-[A-Za-z0-9]" + _STOP + r"*")),
    ("github_token", re.compile(_L + r"(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})" + _STOP + r"*")),
    ("jwt", re.compile(_L + r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,2}")),
    ("google_api_key", re.compile(_L + r"AIza[0-9A-Za-z_-]{20,}")),
    ("aws_access_key", re.compile(_L + r"(?:AKIA|ASIA)[0-9A-Z]{12,}")),
    ("slack_token", re.compile(_L + r"xox[abprs]-" + _STOP + r"+")),
    # Токен после Bearer почти всегда содержит цифру; проза («Bearer authentication…») — нет.
    ("bearer", re.compile(r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,}")),
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
        # Значение под ключом-учётными данными редактируется целиком, какой бы формы оно ни было.
        return {redact_text(k) if isinstance(k, str) else k:
                ("[REDACTED:secret_field]" if isinstance(k, str) and k in SENSITIVE_KEYS and v not in (None, "")
                 else redact_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)
    return obj


def redact_tail(text: str, limit: int) -> str:
    """Сначала редакция ВСЕГО потока, потом срез: срез до редакции отрезал бы опознающий префикс
    токена на границе окна, и хвост без префикса уже не распознаётся."""
    return redact_text(text or "")[-limit:]


def diff_added_secrets(patch: str) -> list[str]:
    """Секретоподобное только в ДОБАВЛЕННЫХ строках диффа: контекст и удалённые строки — уже в
    репозитории (в т.ч. тестовые фикстуры), а удаление утёкшего токена не должно блокироваться."""
    added = "\n".join(line[1:] for line in (patch or "").splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    return find_secrets(added)


def safe_dumps(doc, **kw) -> str:
    """JSON для записи: редакция + повторная проверка сериализованного текста."""
    return ensure_clean(json.dumps(redact_obj(doc), ensure_ascii=False, **kw))


def safe_text(text: str) -> str:
    return ensure_clean(redact_text(text))
