"""Structured logging for Cloud Logging, with secret redaction.

Cloud Run captures stdout; emitting one JSON object per line makes each log a
structured entry. `severity` maps to Cloud Logging levels. A correlation id can
be attached per request so a whole /poll or webhook flow is traceable.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Patterns that must never reach logs even if a caller passes them by mistake.
# No leading \b: inside ".../bot<id>:<secret>/..." there is NO word boundary
# between "bot" and the digits, so the old r"\b\d{6,}:..." never matched and
# httpx's "HTTP Request: POST https://api.telegram.org/bot<token>/..." lines
# leaked the bot token to Cloud Logging (found 2026-09-28: 245 entries / 30 d).
_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")
_TELEGRAM_URL_RE = re.compile(r"(api\.telegram\.org/(?:file/)?bot)[^/\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b")


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value or "")


def redact(text: str) -> str:
    if not text:
        return text
    text = _TELEGRAM_URL_RE.sub(r"\1<telegram_token>", text)
    text = _TELEGRAM_TOKEN_RE.sub("<telegram_token>", text)
    text = _JWT_RE.sub("<jwt>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": redact(record.getMessage()),
            "logger": record.name,
        }
        cid = _correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        # attach structured extras
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx/httpcore log every request URL at INFO — for Telegram the URL itself
    # carries the bot token. Keep only their warnings/errors (still redacted).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: str, message: str, **fields) -> None:
    """Emit a log line with structured extra fields (redacted)."""
    safe = {k: (redact(v) if isinstance(v, str) else v) for k, v in fields.items()}
    logger.log(getattr(logging, level.upper()), message, extra={"extra_fields": safe})
