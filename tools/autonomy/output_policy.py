"""Политика вывода данных AE (AE-R1 / решение владельца B3).

Пока репозиторий публичный, всё, что AE пишет в `autonomy-state`, артефакты Actions, issues, логи и
тела PR, — публично. Поэтому наружу уходят только метаданные: перечисления, счётчики, пути файлов,
идентификаторы, отпечатки (sha256 + длина). Редакция секретов (`redact.py`) — отдельный обязательный
слой, но её недостаточно: сырая строка BigQuery, выручка, процент выкупа или текст отзыва покупателя
секретом не являются и `redact.py` их не ловит.

Три режима:
  * `fingerprint(text)` — свободный текст агента С ДОСТУПОМ к данным (инженер читает BigQuery):
    наружу только `[sha256:<16 hex>;chars:<n>]`. Сам текст никуда не пишется.
  * `mask_data(text)` — текст, выведенный из ПУБЛИЧНЫХ входов (ревьюер без GCP видит только дифф и
    уже очищенные доказательства): денежные суммы, проценты, длинные числа, e-mail, телефоны, URL с
    параметрами и строки, похожие на табличные данные, заменяются на `[DATA:<вид>]`; длина режется.
  * `ensure_public(text)` — последний рубеж перед публичным приёмником (issue, PR): остаток
    данных → `DataExposureError`, запись не выполняется (fail closed).
"""
from __future__ import annotations

import hashlib
import re

FINGERPRINT_RE = re.compile(r"^\[sha256:[0-9a-f]{16};chars:\d+\]$")
_NUM = r"\d[\d   ]*(?:[.,]\d+)?"
# Порядок важен: сначала составные виды, затем общие числа.
DATA_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("url_query", re.compile(r"https?://\S+?[?#]\S*")),
    ("money", re.compile(r"(?i)(?:₽\s*" + _NUM + r"|" + _NUM + r"\s*(?:₽|руб(?:\.|л[а-я]*)?(?![а-я])|rub\b|р\.(?![а-я])))")),
    ("percent", re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?%")),
    ("phone", re.compile(r"(?<![\w-])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?![\w-])")),
    # BigQuery REST: {"f":[{"v":...}]} и CSV/TSV-строки с несколькими числовыми полями.
    ("bq_row", re.compile(r"\{\s*\"f\"\s*:\s*\[\s*\{\s*\"v\"")),
    ("table_row", re.compile(r"(?m)^[^\n]*\d[^\n]*(?:[,;\t|][^\n,;\t|]*\d[^\n,;\t|]*){3,}$")),
    # Строка с тремя и более «реальными» числами (≥4 цифр или с дробной частью) — похоже на строку данных.
    # Границы как у long_number: части дат/времени («2026-10-07T09:30:47Z»), версий и путей не считаются.
    ("numeric_row", re.compile(r"(?m)^(?:[^\n]*?(?<![\w.:/-])(?:\d{4,}|\d+[.,]\d+)(?![\w.:/-])){3,}")),
    # Длинное самостоятельное число: nm_id, номер заказа, srid-часть, сумма без копеек.
    ("long_number", re.compile(r"(?<![\w.:/-])\d{6,}(?![\w.:/-])")),
]


class DataExposureError(RuntimeError):
    """В тексте для публичного приёмника остались данные — запись запрещена."""


def fingerprint(text: str | None) -> str:
    """Идемпотентно: отпечаток отпечатка — тот же отпечаток (доверенный ingest повторно обезличивает
    уже обезличенный вывод недоверенного job'а, и хеш плана не должен от этого меняться)."""
    text = text or ""
    if FINGERPRINT_RE.match(text):
        return text
    return f"[sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]};chars:{len(text)}]"


def find_data(text: str | None) -> list[str]:
    return sorted({name for name, rx in DATA_PATTERNS if rx.search(text or "")})


def mask_data(text: str | None, limit: int = 600) -> str:
    # Сначала срез (с запасом на границу числа), потом маскирование и окончательный срез: регулярные
    # выражения не гоняются по мегабайтам недоверенного текста.
    out = (text or "")[:limit + 64]
    for name, rx in DATA_PATTERNS:
        out = rx.sub(f"[DATA:{name}]", out)
    out = out[:limit]
    # Срез мог отрезать хвост числа в маскированном тексте — повторная проверка на остатке.
    for name, rx in DATA_PATTERNS:
        out = rx.sub(f"[DATA:{name}]", out)
    return out


def ensure_public(text: str) -> str:
    left = find_data(text)
    if left:
        raise DataExposureError(f"в публичном выводе остались данные: {left} — запись отменена")
    return text


def public_obj(obj, limit: int = 600):
    """Рекурсивная маскировка строк документа, выведенного из публичных входов."""
    if isinstance(obj, str):
        return mask_data(obj, limit)
    if isinstance(obj, dict):
        return {k: public_obj(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [public_obj(v, limit) for v in obj]
    return obj


def sanitize_engineer_report(report: dict, narrative_fields: list[str]) -> dict:
    """Отчёт инженера (агент с доступом к BigQuery) → публичная форма.

    Перечисления, булевы и пути файлов остаются: по ним работают детерминированные ворота. Свободный
    текст заменяется отпечатком той же формы (строка), поэтому документ остаётся валидным по схеме
    `engineer_report` и хеш плана (ACK владельца) считается по публичной форме."""
    out = dict(report)
    for k in narrative_fields:
        if k not in out:
            continue
        v = out[k]
        if isinstance(v, str):
            out[k] = fingerprint(v) if v else v
        elif isinstance(v, list):
            out[k] = [fingerprint(x) if isinstance(x, str) else x for x in v]
    return out


_TEST_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?::\S+)|^\s*(?:FAIL|×)\s+(\S+\.test\.ts)(?:\s+>\s+.*)?$")


def public_tail(tail: str | None, max_lines: int = 20) -> dict:
    """Хвост вывода тестов → только идентификаторы упавших тестов (это код, он и так публичен)
    плюс отпечаток всего хвоста. Сам вывод (в нём могли быть фикстуры и строки данных) не публикуется."""
    ids: list[str] = []
    for line in (tail or "").splitlines():
        m = _TEST_ID.match(line.strip())
        if m:
            tid = (m.group(1) or m.group(2) or "")[:200]
            if tid and tid not in ids and not find_data(tid):
                ids.append(tid)
        if len(ids) >= max_lines:
            break
    return {"failed_tests": ids, "fingerprint": fingerprint(tail or "")}
