"""Security audit events (D-19b): machine-verifiable attribution of the public ingress (F-18).

The service is publicly invocable (Telegram must reach the webhook), so Cloud Run IAM cannot say
who caused a write. These events let a trusted audit prove, per request, that every external
mutation happened only after application-level authentication:

    Cloud Run request log (run.googleapis.com/requests, trace T)
      -> request_start (trace T, correlation C)
      -> auth_ok / authz_ok                      (trace T, correlation C)
      -> mutation_attempt -> mutation_success|mutation_failure   (trace T, correlation C, mutation_id M)
      -> request_done (trace T, correlation C, mutation_attempts = number of attempts)

Each event is ONE JSON line on stdout carrying `logging.googleapis.com/trace`, so Cloud Logging
puts it on the same trace as the platform request log. Events bypass the `logging` module on
purpose: they must not depend on log level, handlers or filters.

Only whitelisted fields with bounded values are emitted. Never: headers, tokens, secrets, the
webhook/scheduler/admin secret, message text, buyer data, raw chat ids (hashed instead).
A value that is not in its allowed form is replaced by "<invalid>" — the audit treats that as a
failure, so a bug here fails closed instead of leaking.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.utils.logging import redact

SCHEMA = "wbcomm.security.v1"

# Routes are reported only from this closed set: the raw path is attacker-controlled.
KNOWN_ROUTES = frozenset({"/poll", "/telegram-webhook", "/health",
                          "/admin/test-openai", "/admin/test-wb", "/admin/test-telegram"})
AUTH_MECHANISMS = {"scheduler_secret": "cloud_scheduler",
                   "telegram_webhook_secret": "telegram_bot_api",
                   "admin_token": "admin_operator"}
AUTHZ_MECHANISMS = {"telegram_allowlist": "telegram_user"}
MUTATION_RESULTS = frozenset({"success", "rejected", "error", "outcome_unknown"})

_SAFE = re.compile(r"^[A-Za-z0-9_.:/@+=-]{0,160}$")
PLAIN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_DEC = re.compile(r"^[0-9]{1,20}$")
_STR_FIELDS = frozenset({
    "security_schema", "event_type", "event_id", "ts", "trace_id", "span_id", "correlation_id", "route",
    "method", "service", "revision", "auth_mechanism", "principal_class", "result", "reason",
    "mutation_id", "mutation_class", "target_system", "target_ref", "error_class", "auth_event_id"})
_INT_FIELDS = frozenset({"status_code", "mutation_attempts", "duration_ms"})
_BOOL_FIELDS = frozenset({"allowlist_configured"})

_write_lock = threading.Lock()


@dataclass
class RequestAudit:
    correlation_id: str
    trace_id: str | None
    span_id: str | None
    route: str
    method: str
    started: float = field(default_factory=time.monotonic)
    auth_event_id: str | None = None        # last successful auth_ok of THIS request
    auth_denied: bool = False
    attempts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_current: ContextVar[RequestAudit | None] = ContextVar("security_request_audit", default=None)


def _project() -> str:
    return os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def parse_trace(headers: dict[str, str]) -> tuple[str | None, str | None]:
    """(trace_id, span_id) from Cloud Run's X-Cloud-Trace-Context (preferred) or W3C traceparent.
    Malformed values are ignored — an unparseable trace yields None and the audit fails closed."""
    xctc = headers.get("x-cloud-trace-context") or ""
    if xctc:
        trace, _, rest = xctc.partition("/")
        span = rest.split(";", 1)[0]
        trace = trace.strip().lower()
        if _HEX32.match(trace):
            span_hex = format(int(span), "016x") if _DEC.match(span) and int(span) < 2 ** 64 else None
            return trace, span_hex
    tp = (headers.get("traceparent") or "").strip().lower()
    parts = tp.split("-")
    if len(parts) == 4 and _HEX32.match(parts[1]) and _HEX16.match(parts[2]):
        return parts[1], parts[2]
    return None, None


def _clean(key: str, value):
    if key in _INT_FIELDS:
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 9 else -1
    if key in _BOOL_FIELDS:
        return value if isinstance(value, bool) else None
    if value is None:
        return None
    text = redact(str(value))
    return text if _SAFE.match(text) else "<invalid>"


def emit(event_type: str, ctx: RequestAudit | None = None, **fields) -> str:
    """Write one security event line to stdout. Returns its event_id. Never raises."""
    event_id = uuid.uuid4().hex
    try:
        ctx = ctx if ctx is not None else _current.get()
        payload = {
            "security_schema": SCHEMA,
            "event_type": event_type,
            "event_id": event_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "service": os.environ.get("K_SERVICE", "local"),
            "revision": os.environ.get("K_REVISION", "local"),
            "correlation_id": ctx.correlation_id if ctx else "none",
            "trace_id": ctx.trace_id if ctx else None,
            "span_id": ctx.span_id if ctx else None,
            "route": ctx.route if ctx else "none",
        }
        payload.update(fields)
        doc = {k: _clean(k, v) for k, v in payload.items() if k in _STR_FIELDS | _INT_FIELDS | _BOOL_FIELDS}
        line = {"severity": "NOTICE", "message": f"security_event {doc['event_type']}", **doc}
        if doc.get("trace_id") and _HEX32.match(str(doc["trace_id"])) and _project():
            line["logging.googleapis.com/trace"] = f"projects/{_project()}/traces/{doc['trace_id']}"
            if doc.get("span_id") and _HEX16.match(str(doc["span_id"])):
                line["logging.googleapis.com/spanId"] = doc["span_id"]
        text = json.dumps(line, ensure_ascii=True, separators=(",", ":"))
        with _write_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 — auditing must never break the request; a missing event fails the audit
        pass
    return event_id


# --- request lifecycle (middleware) --------------------------------------------------------

def begin_request(method: str, path: str, headers: dict[str, str]) -> tuple[RequestAudit, object]:
    trace_id, span_id = parse_trace(headers)
    ctx = RequestAudit(correlation_id=uuid.uuid4().hex, trace_id=trace_id, span_id=span_id,
                       route=path if path in KNOWN_ROUTES else "other",
                       method=method if method in {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"}
                       else "other")
    token = _current.set(ctx)
    emit("request_start", ctx, method=ctx.method)
    return ctx, token


def end_request(ctx: RequestAudit, token, status_code: int) -> None:
    with ctx.lock:
        attempts = ctx.attempts
    auth = "ok" if ctx.auth_event_id else "denied" if ctx.auth_denied else "none"
    emit("request_done", ctx, method=ctx.method, status_code=status_code, result=auth,
         mutation_attempts=attempts, duration_ms=int((time.monotonic() - ctx.started) * 1000))
    try:
        _current.reset(token)
    except ValueError:  # reset from another context (should not happen): leave it
        pass


class SecurityAuditMiddleware:
    """Pure ASGI middleware: request_start before the app, request_done after it (also on errors),
    both on the Cloud Run trace. The request context is a contextvar; Starlette copies the context
    into the threadpool that runs sync endpoints, so routes and clients see the same RequestAudit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        ctx, token = begin_request(scope.get("method", ""), scope.get("path", ""), headers)
        status = {"code": 500}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            end_request(ctx, token, status["code"])


# --- authentication / authorization ---------------------------------------------------------

def record_auth(mechanism: str, ok: bool, reason: str | None = None) -> None:
    """Application-level authentication decision of the current request (never the secret)."""
    ctx = _current.get()
    event_id = emit("auth_ok" if ok else "auth_denied", ctx, auth_mechanism=mechanism,
                    principal_class=AUTH_MECHANISMS.get(mechanism, "unknown"),
                    result="ok" if ok else "denied", reason=None if ok else (reason or "mismatch"))
    if ctx is not None:
        if ok:
            ctx.auth_event_id = event_id
        else:
            ctx.auth_denied = True


def record_authz(mechanism: str, ok: bool, allowlist_configured: bool) -> None:
    emit("authz_ok" if ok else "authz_denied", auth_mechanism=mechanism,
         principal_class=AUTHZ_MECHANISMS.get(mechanism, "unknown"),
         result="ok" if ok else "denied", allowlist_configured=allowlist_configured)


# --- external mutations ---------------------------------------------------------------------

def safe_ref(kind: str, value) -> str:
    """Stable, non-reversible reference (chat ids, callback ids are not published verbatim)."""
    digest = hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


@dataclass
class Mutation:
    mutation_id: str
    mutation_class: str
    target_system: str
    target_ref: str
    done: bool = False


def start_mutation(mutation_class: str, target_system: str, target_ref: str) -> Mutation:
    ctx = _current.get()
    m = Mutation(uuid.uuid4().hex, mutation_class, target_system, target_ref)
    if ctx is not None:
        with ctx.lock:
            ctx.attempts += 1
    emit("mutation_attempt", ctx, mutation_id=m.mutation_id, mutation_class=mutation_class,
         target_system=target_system, target_ref=target_ref, result="attempt",
         auth_event_id=ctx.auth_event_id if ctx else None)
    return m


def finish_mutation(m: Mutation, result: str, error_class: str | None = None) -> None:
    if m.done:
        return
    m.done = True
    if result not in MUTATION_RESULTS:
        result = "outcome_unknown"
    emit("mutation_success" if result == "success" else "mutation_failure", mutation_id=m.mutation_id,
         mutation_class=m.mutation_class, target_system=m.target_system, target_ref=m.target_ref,
         result=result, error_class=error_class)
