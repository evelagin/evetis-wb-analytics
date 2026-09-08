"""Text helpers: Telegram HTML escaping, safe truncation, WB length checks."""
from __future__ import annotations

# Telegram "HTML" parse mode requires escaping only these three characters in
# text nodes. Escaping them means arbitrary buyer text can never break markup.
_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

# WB feedback answers are limited to 1000 characters (per WB feedbacks docs).
WB_ANSWER_MAX_LEN = 1000

# Telegram message hard limit is 4096 chars; keep a margin for our template.
TELEGRAM_MSG_SOFT_LIMIT = 3800


def escape_html(text: str) -> str:
    if not text:
        return ""
    out = []
    for ch in str(text):
        out.append(_HTML_ESCAPES.get(ch, ch))
    return "".join(out)


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    return text[: limit - len(ellipsis)] + ellipsis


def is_within_wb_limit(text: str) -> bool:
    return 0 < len(text or "") <= WB_ANSWER_MAX_LEN


def clean_answer(text: str) -> str:
    """Normalize an answer before publishing: strip whitespace and stray quotes."""
    if not text:
        return ""
    return str(text).strip().strip('"').strip()
