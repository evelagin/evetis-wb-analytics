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
    "security_schema", "event_type", "event_id", "ts", "trace_id", "alt_trace_id", "span_id", "correlation_id",
    "route", "method", "service", "revision", "auth_mechanism", "principal_class", "result", "reason",
    "mutation_id", "mutation_class", "target_system", "target_ref", "error_class", "auth_event_id",
    "authz_event_id"})
_INT_FIELDS = frozenset({"status_code", "mutation_attempts", "duration_ms", "seq"})
_BOOL_FIELDS = frozenset({"allowlist_configured"})

_write_lock = threading.Lock()


@dataclass
class RequestAudit:
    correlation_id: str
    trace_id: str | None
    span_id: str | None
    route: str
    method: str
    alt_trace_id: str | None = None         # second, DIFFERENT trace header value (client-controlled headers)
    started: float = field(default_factory=time.monotonic)
    auth_event_id: str | None = None        # last successful auth_ok of THIS request
    auth_denied: bool = False
    authz_event_id: str | None = None       # last authz_ok of THIS request with a CONFIGURED allow-list
    authz_denied: bool = False
    attempts: int = 0
    seq: int = 0                            # per-request event order (ingestion timestamps can tie)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq


_current: ContextVar[RequestAudit | None] = ContextVar("security_request_audit", default=None)


def _project() -> str:
    return os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def _xctc(value: str) -> tuple[str | None, str | None]:
    trace, _, rest = (value or "").partition("/")
    span = rest.split(";", 1)[0].strip()
    trace = trace.strip().lower()
    if not _HEX32.match(trace):
        return None, None
    return trace, (format(int(span), "016x") if _DEC.match(span) and int(span) < 2 ** 64 else None)


def _traceparent(value: str) -> tuple[str | None, str | None]:
    parts = (value or "").strip().lower().split("-")
    if len(parts) == 4 and _HEX32.match(parts[1]) and _HEX16.match(parts[2]):
        return parts[1], parts[2]
    return None, None


def parse_trace(headers) -> tuple[str | None, str | None, str | None]:
    """(trace_id, span_id, alt_trace_id). Trace headers are CLIENT-CONTROLLED on a public service (the Google front
    end accepts an incoming X-Cloud-Trace-Context), and a client may send several or conflicting ones. We never
    guess which one the platform logged: the first valid trace (X-Cloud-Trace-Context before traceparent, in header
    order) is `trace_id`, a second DIFFERENT one is `alt_trace_id`, more than two distinct → no trace at all (the
    audit then cannot attribute the request and fails closed). `headers` is a list of (name, value) pairs or a dict."""
    pairs = list(headers.items()) if isinstance(headers, dict) else list(headers)
    found: list[tuple[str, str | None]] = []
    for parser, name in ((_xctc, "x-cloud-trace-context"), (_traceparent, "traceparent")):
        for k, v in pairs:
            if k.lower() == name:
                t, span = parser(v)
                if t:
                    found.append((t, span))
    distinct = list(dict.fromkeys(t for t, _ in found))
    if not distinct or len(distinct) > 2:
        return None, None, None
    span = next(sp for t, sp in found if t == distinct[0])
    return distinct[0], span, (distinct[1] if len(distinct) == 2 else None)


def _clean(key: str, value):
    if key in _INT_FIELDS:
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 9 else -1
    if key in _BOOL_FIELDS:
        return value if isinstance(value, bool) else None
    if value is None:
        return None
    text = redact(str(value))
    return text if _SAFE.fullmatch(text) else "<invalid>"


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
            "alt_trace_id": ctx.alt_trace_id if ctx else None,
            "span_id": ctx.span_id if ctx else None,
            "route": ctx.route if ctx else "none",
            "seq": ctx.next_seq() if ctx else 0,
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

def begin_request(method: str, path: str, headers) -> tuple[RequestAudit, object]:
    trace_id, span_id, alt = parse_trace(headers)
    ctx = RequestAudit(correlation_id=uuid.uuid4().hex, trace_id=trace_id, span_id=span_id, alt_trace_id=alt,
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
        try:
            headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers") or []]
            ctx, token = begin_request(scope.get("method", ""), scope.get("path", ""), headers)
        except Exception:  # noqa: BLE001 — auditing never breaks a request; missing events fail the audit closed
            return await self.app(scope, receive, send)
        status = {"code": 500}

        async def _send(message):
            if message.get("type") == "http.response.start":
                try:
                    status["code"] = int(message.get("status", 500))
                except (TypeError, ValueError):
                    status["code"] = 500
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
    """Allow-list decision. Only an authz_ok of a CONFIGURED allow-list becomes the request's authorization
    (`authz_event_id`, carried by every later mutation_attempt): the audit requires it for WB writes that a
    Telegram update triggers, so a publish after a denial, or with an empty (permissive) allow-list, fails."""
    ctx = _current.get()
    event_id = emit("authz_ok" if ok else "authz_denied", ctx, auth_mechanism=mechanism,
                    principal_class=AUTHZ_MECHANISMS.get(mechanism, "unknown"),
                    result="ok" if ok else "denied", allowlist_configured=allowlist_configured)
    if ctx is not None:
        if ok and allowlist_configured:
            ctx.authz_event_id = event_id
        elif not ok:
            ctx.authz_denied = True
            ctx.authz_event_id = None      # a later denial revokes an earlier authorization of this request


# --- external mutations ---------------------------------------------------------------------

def safe_ref(kind: str, value) -> str:
    """Opaque reference for random, high-entropy ids (callback ids, unexpected WB ids). Never raises."""
    try:
        digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8", "surrogatepass")).hexdigest()[:16]
        return f"{kind}:{digest}"
    except Exception:  # noqa: BLE001 — a reference must never break the write path
        return "<invalid>"


def chat_ref(chat_id) -> str:
    """Telegram chat/user ids are ~10 digits: a hash of them is brute-forceable, so only the class is recorded —
    the configured operator chat (TELEGRAM_CHAT_ID) or any other chat."""
    try:
        configured = os.environ.get("TELEGRAM_CHAT_ID", "302044578")      # same default as app.config
        return "tg_chat:configured" if configured and str(chat_id) == configured else "tg_chat:other"
    except Exception:  # noqa: BLE001
        return "<invalid>"


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
         auth_event_id=ctx.auth_event_id if ctx else None,
         authz_event_id=ctx.authz_event_id if ctx else None)
    return m


def audited_store(target_system: str, read_only: frozenset = frozenset()):
    """Class decorator for the service's own state stores (Firestore, BigQuery): EVERY public method that is not
    listed as read-only is recorded as an internal mutation of the current request (new methods are audited by
    default). Behaviour is unchanged: the result and any exception pass through untouched. Internal outcomes are
    `success` or `error` (a False return or an exception) — informational; what the audit requires for them is the
    same authentication link as for external writes."""
    import functools

    def wrap(fn, name):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            m = start_mutation(f"{target_system}_{name}", target_system, f"{target_system}:state")
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                finish_mutation(m, "error", type(exc).__name__)
                raise
            finish_mutation(m, "error" if result is False else "success")
            return result
        wrapper.__security_audited__ = True
        return wrapper

    def deco(cls):
        for name, fn in list(vars(cls).items()):
            if name.startswith("_") or name in read_only or not callable(fn) \
                    or isinstance(fn, (staticmethod, classmethod)):
                continue
            setattr(cls, name, wrap(fn, name))
        return cls
    return deco


def finish_mutation(m: Mutation, result: str, error_class: str | None = None) -> None:
    if m.done:
        return
    m.done = True
    if result not in MUTATION_RESULTS:
        result = "outcome_unknown"
    emit("mutation_success" if result == "success" else "mutation_failure", mutation_id=m.mutation_id,
         mutation_class=m.mutation_class, target_system=m.target_system, target_ref=m.target_ref,
         result=result, error_class=error_class)
