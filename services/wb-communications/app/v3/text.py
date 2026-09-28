"""Russian text helpers shared by the classifier, planner and verifier.

Everything here is deterministic and dependency-free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

_WS = re.compile(r"\s+")
_CLAUSE_SPLIT = re.compile(r"[.!?;\n]+|,\s*(?=(?:но|а|однако|зато|хотя)\b)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def normalize(text: str | None) -> str:
    """Lowercase, ё→е, NBSP→space, typographic dashes/quotes unified, whitespace collapsed."""
    if not text:
        return ""
    t = str(text).replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    t = t.replace("–", "-").replace("—", "-").replace("«", '"').replace("»", '"')
    return _WS.sub(" ", t.lower()).strip()


def clauses(text: str) -> list[str]:
    """Split normalized text into clauses (sentence punctuation + adversative conjunctions)."""
    return [c.strip() for c in _CLAUSE_SPLIT.split(normalize(text)) if c and c.strip()]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def compile_all(patterns: Iterable[str]) -> list[re.Pattern]:
    return [_compile(p) for p in patterns]


def search_any(patterns: Iterable[str], text: str) -> re.Match | None:
    for p in patterns:
        m = _compile(p).search(text)
        if m:
            return m
    return None


def find_all(patterns: Iterable[str], text: str) -> list[re.Match]:
    out = []
    for p in patterns:
        out.extend(_compile(p).finditer(text))
    return out


def canon_number(raw: str) -> str:
    """'2,25' -> '2.25', '6.00' -> '6', '0,20' -> '0.2'."""
    s = raw.replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@dataclass(frozen=True)
class NumberToken:
    value: str        # canonical
    raw: str
    start: int
    end: int
    unit: str         # '%', 'мл', 'г', 'мес', 'год', 'раз', 'кап', 'наж', 'мин', 'нед', 'дн', 'ph', ''


_UNIT_AFTER = [
    ("%", re.compile(r"^\s*%")),
    ("мл", re.compile(r"^\s*(мл|ml|миллилитр)")),
    ("г", re.compile(r"^\s*(г|гр|грамм|g)\b")),
    ("мес", re.compile(r"^\s*(мес|month)")),
    ("год", re.compile(r"^\s*(год|лет|year)")),
    ("раз", re.compile(r"^\s*раз")),
    ("кап", re.compile(r"^\s*капл")),
    ("наж", re.compile(r"^\s*(нажат|качк|пшик)")),
    ("мин", re.compile(r"^\s*минут")),
    ("нед", re.compile(r"^\s*недел")),
    ("дн", re.compile(r"^\s*(дн|день|сут)")),
    ("°c", re.compile(r"^\s*(°|градус)")),
]
_PH_BEFORE = re.compile(r"(ph|пш)\s*[:=]?\s*$")


def numbers(text: str) -> list[NumberToken]:
    """All numbers in (already normalized) text with the unit that follows them."""
    out: list[NumberToken] = []
    for m in re.finditer(r"(?<![\w])(\d+(?:[.,]\d+)?)", text):
        raw = m.group(1)
        after = text[m.end(): m.end() + 14]
        before = text[max(0, m.start() - 6): m.start()]
        unit = ""
        for name, rx in _UNIT_AFTER:
            if rx.search(after):
                unit = name
                break
        if not unit and _PH_BEFORE.search(before):
            unit = "ph"
        out.append(NumberToken(canon_number(raw), raw, m.start(), m.end(), unit))
    return out


# Spelled-out quantities that also carry numeric advice ("пару недель", "два раза").
NUMBER_WORDS = r"(один|одна|одну|два|две|три|четыре|пять|шесть|семь|восемь|десять|пару|пара|несколько|дважды|трижды)"


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out spans (keeps offsets stable)."""
    chars = list(text)
    for a, b in spans:
        for i in range(max(0, a), min(len(chars), b)):
            chars[i] = " "
    return "".join(chars)


def find_literal_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Case/ё-insensitive literal spans of `needle` inside normalized `haystack`."""
    n = normalize(needle)
    if not n:
        return []
    spans, start = [], 0
    while True:
        i = haystack.find(n, start)
        if i < 0:
            return spans
        spans.append((i, i + len(n)))
        start = i + len(n)
