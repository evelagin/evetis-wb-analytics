"""Structured logging for Cloud Logging, with secret redaction.

Cloud Run captures stdout; emitting one JSON object per line makes each log a
structured entry. `severity` maps to Cloud Logging levels. A correlation id can
be attached per request so a whole /poll or webhook flow is traceable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
# Request trace (D-19b): taken from Cloud Run's X-Cloud-Trace-Context / traceparent so every log
# line of a request carries the SAME trace as the platform request log -> machine correlation.
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_span_id: ContextVar[str] = ContextVar("span_id", default="")
_TRACE_HEX = re.compile(r"^[0-9a-f]{32}$")
_SPAN_HEX = re.compile(r"^[0-9a-f]{16}$")

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


def parse_trace(cloud_trace: str | None, traceparent: str | None) -> tuple[str, str]:
    """(trace_id, span_id_hex) from `X-Cloud-Trace-Context: TRACE/SPAN;o=1` or W3C `traceparent`.
    Anything malformed yields empty strings — never an exception."""
    try:
        if cloud_trace:
            head = cloud_trace.split(";", 1)[0]
            trace, _, span = head.partition("/")
            trace = trace.strip().lower()
            if _TRACE_HEX.match(trace):
                span_hex = format(int(span), "016x") if span.strip().isdigit() and int(span) < 2 ** 64 else ""
                return trace, span_hex
        if traceparent:
            parts = traceparent.strip().lower().split("-")
            if len(parts) >= 4 and _TRACE_HEX.match(parts[1]) and set(parts[1]) != {"0"}:
                return parts[1], parts[2] if _SPAN_HEX.match(parts[2]) else ""
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def set_request_trace(cloud_trace: str | None, traceparent: str | None):
    trace, span = parse_trace(cloud_trace, traceparent)
    return _trace_id.set(trace), _span_id.set(span)


def reset_request_trace(tokens) -> None:
    _trace_id.reset(tokens[0])
    _span_id.reset(tokens[1])


def current_trace() -> str:
    return _trace_id.get()


async def trace_middleware(request, call_next):
    """Bind the platform request trace to everything logged while serving this request."""
    tokens = set_request_trace(request.headers.get("x-cloud-trace-context"), request.headers.get("traceparent"))
    try:
        return await call_next(request)
    finally:
        reset_request_trace(tokens)


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
        trace = _trace_id.get()
        if trace:
            payload["trace_id"] = trace
            project = os.environ.get("GCP_PROJECT_ID", "")
            if project:  # Cloud Logging promotes this to LogEntry.trace (same format as request logs)
                payload["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace}"
            if _span_id.get():
                payload["logging.googleapis.com/spanId"] = _span_id.get()
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



# ── Security/audit events (D-19b) ──────────────────────────────────────────────
# One schema, a closed set of fields, no free text: auth decisions and external mutations,
# each bound to the platform request trace. The trusted AE audit correlates
#   Cloud Run request log -> auth_ok -> mutation_attempt -> mutation_success|failure
# by trace. Emission never raises: instrumentation must not change behaviour.
AUDIT_SCHEMA = "wbc-audit/1"
AUDIT_EVENTS = frozenset({"instrumentation_ready", "auth_ok", "auth_denied",
                          "mutation_attempt", "mutation_success", "mutation_failure"})
AUDIT_FIELDS = frozenset({"route", "mechanism", "principal_class", "result", "mutation_class",
                          "target_system", "target_ref", "http_status", "error_class"})
_audit_logger = logging.getLogger("app.audit")


def safe_ref(value) -> str:
    """Non-reversible reference to a target id (review/question id): correlation without the raw id."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value not in (None, "") else ""


def audit_event(event: str, **fields) -> None:
    try:
        if event not in AUDIT_EVENTS:
            return
        safe = {k: v for k, v in fields.items()
                if k in AUDIT_FIELDS and isinstance(v, (str, int, bool)) and not isinstance(v, float)}
        payload = {"audit_event": event, "audit_schema": AUDIT_SCHEMA,
                   "service": os.environ.get("K_SERVICE", ""), "revision": os.environ.get("K_REVISION", ""),
                   "trace_id": _trace_id.get(), **safe}
        _audit_logger.info("audit %s", event, extra={"extra_fields": _redact_value(payload)})
    except Exception:  # noqa: BLE001 — never let instrumentation break a request
        pass
