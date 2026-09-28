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
import urllib.parse
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Two independent layers, so one miss is not a leak (incident 2026-09-27: httpx logged
# `https://api.telegram.org/bot<TOKEN>/...` and the old `\b\d{6,}:` pattern never
# matched, because there is no word boundary between "bot" and the digits):
#   1. value-based: every secret loaded at startup is registered and masked verbatim
#      (plain, URL-encoded), whatever the surrounding format;
#   2. pattern-based: Telegram tokens (also inside `/bot<TOKEN>` and `%3A`-encoded),
#      JWTs and bearer values, for secrets that were never registered.
_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b")
_MIN_SECRET_LEN = 8
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this exact value (and its URL-encoded forms) in every log line from now on."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    forms = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote(value)}
    if ":" in value:  # Telegram bot token: the part after ':' is the secret half
        tail = value.split(":", 1)[1]
        if len(tail) >= _MIN_SECRET_LEN:
            forms.add(tail)
    _known_secrets.update(f for f in forms if len(f) >= _MIN_SECRET_LEN)


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value or "")


def redact(text: str) -> str:
    if not text:
        return text
    for secret in sorted(_known_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "<redacted>")
    text = _TELEGRAM_TOKEN_RE.sub("<telegram_token>", text)
    text = _JWT_RE.sub("<jwt>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return text


def _redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # A failing format must not fall through to logging's error handler, which prints
        # the RAW message and arguments to stderr (bypassing both redaction layers).
        try:
            return self._format(record)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"severity": "ERROR", "logger": getattr(record, "name", "?"),
                               "message": redact(f"log record could not be formatted: {type(exc).__name__}; "
                                                 f"template={getattr(record, 'msg', '')!s}")})

    def _format(self, record: logging.LogRecord) -> str:
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
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))
        # attach structured extras
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = _redact_value(value)
        return json.dumps(payload, ensure_ascii=False, default=lambda v: redact(str(v)))


# httpx logs every request URL at INFO. WB and OpenAI keep their credentials in headers, and
# those lines are the only evidence of an actual WB publication, so they stay. The Telegram
# Bot API puts the token in the URL path: such lines are DROPPED by host (not by regex).
# httpcore (wire-level, DEBUG) is capped at WARNING.
_TELEGRAM_API_HOST = "api.telegram.org"
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore")


class _DropTelegramApiRequests(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — unformattable record: drop rather than risk it
            return False
        return _TELEGRAM_API_HOST not in text


_TELEGRAM_FILTER = _DropTelegramApiRequests()
# uvicorn installs its own plain-text handlers; route them through the redacting formatter.
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    httpx_logger = logging.getLogger("httpx")
    if _TELEGRAM_FILTER not in httpx_logger.filters:
        httpx_logger.addFilter(_TELEGRAM_FILTER)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for name in _SERVER_LOGGERS:
        server = logging.getLogger(name)
        server.handlers.clear()
        server.addHandler(handler)
        server.propagate = False
    logging.captureWarnings(True)  # Python warnings also pass through redaction
    logging.raiseExceptions = False  # never print raw records to stderr on a handler error


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: str, message: str, **fields) -> None:
    """Emit a log line with structured extra fields (redacted)."""
    safe = {k: _redact_value(v) for k, v in fields.items()}
    logger.log(getattr(logging, level.upper()), message, extra={"extra_fields": safe})
