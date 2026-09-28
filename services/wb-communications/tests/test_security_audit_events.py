"""D-19b: security audit events that make the public ingress attributable (F-18).

The trusted audit (tools/autonomy, F-18 semantics) correlates, per request:
Cloud Run request log (trace) -> request_start -> auth_ok/authz -> mutation_attempt ->
mutation_success|failure -> request_done. These tests pin that contract, its fail-closed cases,
and that no credential ever reaches stdout (synthetic secrets only).
"""
from __future__ import annotations


import json
import re


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.admin as admin_route
import app.routes.poll as poll_route
import app.routes.telegram_webhook as wh
from app.config import Secrets
from app.domain.exceptions import TelegramError, WBApiError, WBPublishOutcomeUnknown, WBRateLimitError
from app.services.telegram_client import TelegramClient
from app.services.wb_client import WBClient
from app.utils import audit_events
from app.utils.audit_events import SecurityAuditMiddleware
from app.utils.security import is_allowed
from tests.conftest import SAMPLE_FEEDBACK, make_deps, make_settings

# Synthetic credentials: never real, distinctive enough to grep for.
SCHED = "sched-SYNTHETIC-9f3c2e1d7a6b"
WEBHOOK = "whook-SYNTHETIC-4b8e2a9c1f7d"
ADMIN = "admin-SYNTHETIC-7c1d9e4f2a8b"
BOT = "123456789:AAFsyntheticTokenValue_abcdefghijklmnop"
WBTOKEN = "wbtok-SYNTHETIC-5e2a8c7b1d9f"
ALL_SECRETS = (SCHED, WEBHOOK, ADMIN, BOT, WBTOKEN, BOT.split(":", 1)[1])

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
XCTC = {"X-Cloud-Trace-Context": f"{TRACE}/123;o=1"}

ALLOWED_KEYS = {"severity", "message", "logging.googleapis.com/trace", "logging.googleapis.com/spanId",
                *audit_events._STR_FIELDS, *audit_events._INT_FIELDS, *audit_events._BOOL_FIELDS}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@+=-]{0,160}$")


class _Captured:
    """Everything written to stdout AND stderr during the test (pytest swaps sys.stdout between
    phases, so read through capsys rather than replacing the stream)."""

    def __init__(self, capsys):
        self._capsys, self._text = capsys, ""

    def getvalue(self) -> str:
        got = self._capsys.readouterr()
        self._text += got.out + got.err
        return self._text


@pytest.fixture
def out(capsys, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-test")
    monkeypatch.setenv("K_SERVICE", "evetis-wb-communications")
    monkeypatch.setenv("K_REVISION", "evetis-wb-communications-00099-abc")
    return _Captured(capsys)


def events(buf) -> list[dict]:
    rows = []
    for line in buf.getvalue().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("security_schema") == audit_events.SCHEMA:
            rows.append(d)
    return rows


def tg_transport(handler=None):
    def default(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
    return httpx.MockTransport(handler or default)


def real_telegram(handler=None) -> TelegramClient:
    return TelegramClient(BOT, client=httpx.Client(transport=tg_transport(handler)))


def with_secrets(deps, scheduler=SCHED, webhook=WEBHOOK):
    deps.settings.__dict__["secrets"] = Secrets(openai_api_key="", wb_api_token=WBTOKEN,
                                                 telegram_bot_token=BOT, telegram_webhook_secret=webhook,
                                                 scheduler_secret=scheduler)
    return deps


def client_for(router_module, deps, monkeypatch) -> TestClient:
    monkeypatch.setattr(router_module, "get_deps", lambda: deps)
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)
    app.include_router(router_module.router)
    return TestClient(app, raise_server_exceptions=False)


def assert_no_leak(buf):
    text = buf.getvalue()
    for s in ALL_SECRETS:
        assert s not in text, "a synthetic credential reached stdout"
    for e in events(buf):
        assert set(e) <= ALLOWED_KEYS, set(e) - ALLOWED_KEYS
        for k, v in e.items():
            if isinstance(v, str) and k not in ("message", "logging.googleapis.com/trace"):
                assert SAFE_VALUE.match(v), (k, v)


def chain_ok(evs: list[dict]) -> None:
    """The invariants the trusted audit checks, evaluated on one request's events."""
    starts = [e for e in evs if e["event_type"] == "request_start"]
    dones = [e for e in evs if e["event_type"] == "request_done"]
    assert len(starts) == 1 and len(dones) == 1
    cid, trace = starts[0]["correlation_id"], starts[0]["trace_id"]
    assert all(e["correlation_id"] == cid and e["trace_id"] == trace for e in evs)
    auth = [e for e in evs if e["event_type"] == "auth_ok"]
    attempts = [e for e in evs if e["event_type"] == "mutation_attempt"]
    outcomes = [e for e in evs if e["event_type"] in ("mutation_success", "mutation_failure")]
    assert dones[0]["mutation_attempts"] == len(attempts)
    assert sorted(a["mutation_id"] for a in attempts) == sorted(o["mutation_id"] for o in outcomes)
    authz = {e["event_id"]: e for e in evs if e["event_type"] == "authz_ok" and e["allowlist_configured"] is True}
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))      # provable per-request order
    for a in attempts:
        assert auth and a["auth_event_id"] == auth[-1]["event_id"]
        assert auth[-1]["seq"] < a["seq"]
        if a["target_system"] == "wb" and a["route"] == "/telegram-webhook":
            z = authz.get(a["authz_event_id"])
            assert z is not None and z["seq"] < a["seq"], "WB write from Telegram without allow-list authorization"


# --- /poll -------------------------------------------------------------------------------------

def test_poll_authenticated_chain_on_cloud_run_trace(out, monkeypatch):
    deps = with_secrets(make_deps([dict(SAMPLE_FEEDBACK)]))
    deps.telegram = real_telegram()
    client = client_for(poll_route, deps, monkeypatch)
    resp = client.post("/poll", headers={"X-Scheduler-Secret": SCHED, **XCTC})
    assert resp.status_code == 200
    evs = events(out)
    kinds = [e["event_type"] for e in evs]
    assert kinds[0] == "request_start" and kinds[-1] == "request_done"
    assert "auth_ok" in kinds and "mutation_attempt" in kinds          # the review card was sent
    chain_ok(evs)
    ok = next(e for e in evs if e["event_type"] == "auth_ok")
    assert ok["auth_mechanism"] == "scheduler_secret" and ok["principal_class"] == "cloud_scheduler"
    assert all(e["trace_id"] == TRACE and e["span_id"] == format(123, "016x") for e in evs)
    raw = [json.loads(line) for line in out.getvalue().splitlines() if "security_schema" in line]
    assert raw[0]["logging.googleapis.com/trace"] == f"projects/proj-test/traces/{TRACE}"
    done = evs[-1]
    assert done["status_code"] == 200 and done["result"] == "ok"
    assert done["revision"] == "evetis-wb-communications-00099-abc"
    assert_no_leak(out)


@pytest.mark.parametrize("headers,reason", [({}, "missing"), ({"X-Scheduler-Secret": "wrong-value-123"}, "mismatch")])
def test_poll_denied_has_no_mutation(out, monkeypatch, headers, reason):
    deps = with_secrets(make_deps([dict(SAMPLE_FEEDBACK)]))
    deps.telegram = real_telegram()
    client = client_for(poll_route, deps, monkeypatch)
    assert client.post("/poll", headers={**headers, **XCTC}).status_code == 403
    evs = events(out)
    assert [e["event_type"] for e in evs] == ["request_start", "auth_denied", "request_done"]
    assert evs[1]["reason"] == reason
    assert evs[-1]["result"] == "denied" and evs[-1]["mutation_attempts"] == 0
    assert_no_leak(out)


def test_poll_secret_not_configured_is_denied(out, monkeypatch):
    deps = with_secrets(make_deps([]), scheduler="")
    client = client_for(poll_route, deps, monkeypatch)
    assert client.post("/poll", headers={"X-Scheduler-Secret": SCHED, **XCTC}).status_code == 503
    evs = events(out)
    assert evs[1]["event_type"] == "auth_denied" and evs[1]["reason"] == "not_configured"
    assert_no_leak(out)


# --- /telegram-webhook -------------------------------------------------------------------------

CALLBACK_FROM_STRANGER = {"update_id": 501, "callback_query": {
    "id": "cq-stranger", "data": "skip:abcdef", "from": {"id": 999},
    "message": {"message_id": 1, "chat": {"id": 999}}}}


def test_webhook_bad_secret_denied_without_mutation(out, monkeypatch):
    deps = with_secrets(make_deps([]))
    deps.telegram = real_telegram()
    client = client_for(wh, deps, monkeypatch)
    r = client.post("/telegram-webhook", json=CALLBACK_FROM_STRANGER,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "nope", **XCTC})
    assert r.status_code == 403
    evs = events(out)
    assert [e["event_type"] for e in evs] == ["request_start", "auth_denied", "request_done"]
    assert evs[1]["auth_mechanism"] == "telegram_webhook_secret" and evs[1]["reason"] == "mismatch"
    assert_no_leak(out)


def test_webhook_stranger_is_authenticated_but_not_authorized(out, monkeypatch):
    deps = with_secrets(make_deps([]))
    deps.telegram = real_telegram()
    client = client_for(wh, deps, monkeypatch)
    r = client.post("/telegram-webhook", json=CALLBACK_FROM_STRANGER,
                    headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK, **XCTC})
    assert r.status_code == 200
    evs = events(out)
    kinds = [e["event_type"] for e in evs]
    assert "auth_ok" in kinds and "authz_denied" in kinds
    assert kinds.index("authz_denied") < kinds.index("mutation_attempt")   # «Нет доступа» reply after the check
    att = [e for e in evs if e["event_type"] == "mutation_attempt"]
    assert {a["mutation_class"] for a in att} == {"tg_answer_callback"}
    assert not any(e.get("target_system") == "wb" for e in evs)
    denied = next(e for e in evs if e["event_type"] == "authz_denied")
    assert denied["allowlist_configured"] is True
    chain_ok(evs)
    assert all(v not in ("999", 999) for e in evs for v in e.values())   # raw ids are never logged
    assert all(a["target_ref"].startswith("tg_callback:") for a in att)
    assert_no_leak(out)


# --- admin -------------------------------------------------------------------------------------

def test_admin_token_events(out, monkeypatch):
    deps = with_secrets(make_deps([]))
    deps.settings.admin_token = ADMIN
    deps.telegram = real_telegram()
    client = client_for(admin_route, deps, monkeypatch)
    assert client.post("/admin/test-telegram", headers={"X-Admin-Token": "bad", **XCTC}).status_code == 403
    assert client.post("/admin/test-telegram", headers={"X-Admin-Token": ADMIN, **XCTC}).status_code == 200
    evs = events(out)
    first, second = evs[:3], evs[3:]
    assert [e["event_type"] for e in first] == ["request_start", "auth_denied", "request_done"]
    assert [e["event_type"] for e in second][:2] == ["request_start", "auth_ok"]
    assert second[1]["auth_mechanism"] == "admin_token" and second[1]["principal_class"] == "admin_operator"
    chain_ok(second)
    assert first[0]["correlation_id"] != second[0]["correlation_id"]
    assert_no_leak(out)


# --- clients: outcome classification -----------------------------------------------------------

def _wb(handler) -> WBClient:
    s = make_settings()
    return WBClient(s, WBTOKEN, client=httpx.Client(base_url="https://wb.test", transport=httpx.MockTransport(handler)))


def _in_request():
    return audit_events.begin_request("POST", "/telegram-webhook", {"x-cloud-trace-context": f"{TRACE}/1"})


def _outcome(buf):
    evs = events(buf)
    att = [e for e in evs if e["event_type"] == "mutation_attempt"]
    res = [e for e in evs if e["event_type"] in ("mutation_success", "mutation_failure")]
    assert len(att) == 1 and len(res) == 1 and att[0]["mutation_id"] == res[0]["mutation_id"]
    return att[0], res[0]


def test_wb_publish_success(out):
    ctx, tok = _in_request()
    _wb(lambda r: httpx.Response(204)).publish_answer("FB123", "Спасибо!")
    audit_events.end_request(ctx, tok, 200)
    att, res = _outcome(out)
    assert att["mutation_class"] == "wb_feedback_answer" and att["target_ref"] == "wb:FB123"
    assert res["event_type"] == "mutation_success" and res["result"] == "success"
    assert events(out)[-1]["mutation_attempts"] == 1
    assert "Спасибо" not in out.getvalue()
    assert_no_leak(out)


@pytest.mark.parametrize("status,exc,result", [
    (400, WBApiError, "rejected"),
    (500, WBPublishOutcomeUnknown, "outcome_unknown"),
    (429, WBRateLimitError, "error"),
])
def test_wb_publish_failure_classes(out, status, exc, result):
    ctx, tok = _in_request()
    with pytest.raises(exc):
        _wb(lambda r: httpx.Response(status, json={})).publish_answer("FB1", "x")
    audit_events.end_request(ctx, tok, 200)
    _, res = _outcome(out)
    assert res["event_type"] == "mutation_failure" and res["result"] == result


def test_wb_read_timeout_is_outcome_unknown(out):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    ctx, tok = _in_request()
    with pytest.raises(WBPublishOutcomeUnknown):
        _wb(boom).publish_question_answer("Q1", "ответ")
    audit_events.end_request(ctx, tok, 200)
    att, res = _outcome(out)
    assert att["mutation_class"] == "wb_question_answer" and res["result"] == "outcome_unknown"


def test_wb_in_band_error_true_is_rejected(out):
    ctx, tok = _in_request()
    with pytest.raises(WBApiError):
        _wb(lambda r: httpx.Response(200, json={"error": True, "errorText": "bad"})).publish_question_answer("Q2", "t")
    audit_events.end_request(ctx, tok, 200)
    assert _outcome(out)[1]["result"] == "rejected"


@pytest.mark.parametrize("handler,result,err", [
    (lambda r: httpx.Response(200, json={"ok": False, "description": "bad"}), "rejected", "TelegramNotOk"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)), "error", "ConnectError"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "outcome_unknown", "ReadTimeout"),
    (lambda r: httpx.Response(502, text="<html>"), "outcome_unknown", "NonJsonResponse"),
])
def test_telegram_failure_classes(out, handler, result, err):
    ctx, tok = _in_request()
    with pytest.raises(TelegramError):
        real_telegram(handler).send_message(302044578, "hi")
    audit_events.end_request(ctx, tok, 200)
    att, res = _outcome(out)
    assert att["mutation_class"] == "tg_send_message" and att["target_ref"] in ("tg_chat:configured", "tg_chat:other")
    assert res["result"] == result and res["error_class"] == err
    assert_no_leak(out)


def test_mutation_outside_request_is_unattributed(out):
    real_telegram().send_message(1, "x")
    att = events(out)[0]
    assert att["correlation_id"] == "none" and att["trace_id"] is None and att["auth_event_id"] is None


# --- trace parsing, sanitation, allow-list -----------------------------------------------------

OTHER = "5" * 32
THIRD = "6" * 32


@pytest.mark.parametrize("headers,expected", [
    ({"x-cloud-trace-context": f"{TRACE}/123;o=1"}, (TRACE, format(123, "016x"), None)),
    ({"x-cloud-trace-context": f"{TRACE.upper()}/9"}, (TRACE, format(9, "016x"), None)),
    ({"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"}, (TRACE, "00f067aa0ba902b7", None)),
    ({"x-cloud-trace-context": "not-a-trace/1"}, (None, None, None)),
    ({"traceparent": "garbage"}, (None, None, None)),
    ({}, (None, None, None)),
    # same trace in both headers: one trace
    ([("x-cloud-trace-context", f"{TRACE}/1"), ("traceparent", f"00-{TRACE}-00f067aa0ba902b7-01")],
     (TRACE, format(1, "016x"), None)),
    # conflicting headers: never guess — primary + alt
    ([("x-cloud-trace-context", f"{TRACE}/1"), ("traceparent", f"00-{OTHER}-00f067aa0ba902b7-01")],
     (TRACE, format(1, "016x"), OTHER)),
    # repeated header with different values
    ([("x-cloud-trace-context", f"{TRACE}/1"), ("x-cloud-trace-context", f"{OTHER}/2")],
     (TRACE, format(1, "016x"), OTHER)),
    # more than two distinct traces: ambiguous → no trace (audit fails closed)
    ([("x-cloud-trace-context", f"{TRACE}/1"), ("x-cloud-trace-context", f"{OTHER}/2"),
      ("traceparent", f"00-{THIRD}-00f067aa0ba902b7-01")], (None, None, None)),
])
def test_parse_trace(headers, expected):
    assert audit_events.parse_trace(headers) == expected


def test_unknown_route_and_hostile_values_are_not_echoed(out, monkeypatch):
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    client.get(f"/{BOT}/x", headers={"X-Cloud-Trace-Context": f"{TRACE}/1"})
    evs = events(out)
    assert {e["route"] for e in evs} == {"other"}
    assert evs[-1]["status_code"] == 404
    assert_no_leak(out)


def test_invalid_field_value_fails_closed(out):
    audit_events.emit("auth_ok", None, auth_mechanism="has space and <script>")
    assert events(out)[0]["auth_mechanism"] == "<invalid>"


def test_handler_exception_still_emits_request_done(out, monkeypatch):
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)

    @app.get("/health")
    def boom():
        raise RuntimeError("x")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health", headers=XCTC).status_code == 500
    evs = events(out)
    assert [e["event_type"] for e in evs] == ["request_start", "request_done"]
    assert evs[-1]["status_code"] == 500


@pytest.mark.parametrize("chats,users,chat,user,ok,configured", [
    ({"1"}, {"2"}, 1, 2, True, True),
    ({"1"}, {"2"}, 3, 2, False, True),
    ({"1"}, {"2"}, 1, 3, False, True),
    (set(), set(), 5, 6, True, False),          # permissive when unconfigured: recorded as NOT configured
    ({"1"}, set(), 1, 9, True, False),
])
def test_is_allowed_unchanged_and_recorded(out, chats, users, chat, user, ok, configured):
    assert is_allowed(chat, user, chats, users) is ok
    e = events(out)[0]
    assert e["event_type"] == ("authz_ok" if ok else "authz_denied")
    assert e["allowlist_configured"] is configured
    assert set(e) <= ALLOWED_KEYS                                   # ids are not payload fields


# --- review fixes (H1, M1, M2, L1, L2) ---------------------------------------------------------

def _webhook_publish(out, allowed_chats, allowed_users, chat, user):
    ctx, tok = audit_events.begin_request("POST", "/telegram-webhook", {"x-cloud-trace-context": f"{TRACE}/1"})
    audit_events.record_auth("telegram_webhook_secret", ok=True)
    is_allowed(chat, user, allowed_chats, allowed_users)
    _wb(lambda r: httpx.Response(204)).publish_answer("FB9", "t")
    audit_events.end_request(ctx, tok, 200)
    return events(out)


def test_wb_write_after_allowlist_ok_is_authorized(out):
    evs = _webhook_publish(out, {"1"}, {"2"}, 1, 2)
    chain_ok(evs)
    att = next(e for e in evs if e["event_type"] == "mutation_attempt")
    assert att["authz_event_id"] == next(e for e in evs if e["event_type"] == "authz_ok")["event_id"]


@pytest.mark.parametrize("chats,users,chat,user", [({"1"}, {"2"}, 9, 2), (set(), set(), 9, 9)])
def test_wb_write_after_denied_or_unconfigured_allowlist_fails_the_chain(out, chats, users, chat, user):
    evs = _webhook_publish(out, chats, users, chat, user)
    att = next(e for e in evs if e["event_type"] == "mutation_attempt")
    assert att["authz_event_id"] is None
    with pytest.raises(AssertionError, match="allow-list"):
        chain_ok(evs)


def test_later_denial_revokes_authorization(out):
    ctx, tok = audit_events.begin_request("POST", "/telegram-webhook", {})
    audit_events.record_authz("telegram_allowlist", ok=True, allowlist_configured=True)
    audit_events.record_authz("telegram_allowlist", ok=False, allowlist_configured=True)
    assert ctx.authz_event_id is None
    audit_events.end_request(ctx, tok, 200)


def test_conflicting_trace_headers_are_carried_as_alt(out, monkeypatch):
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/health", headers=[("X-Cloud-Trace-Context", f"{TRACE}/1"),
                                   ("traceparent", f"00-{OTHER}-00f067aa0ba902b7-01")])
    evs = events(out)
    assert all(e["trace_id"] == TRACE and e["alt_trace_id"] == OTHER for e in evs)


@pytest.mark.parametrize("sequence,result,err", [
    (["ReadTimeout", "ConnectError", "ConnectError"], "outcome_unknown", "ConnectError"),   # 1st may have landed
    (["ConnectError", "ConnectError", "ConnectError"], "error", "ConnectError"),            # provably never sent
    (["ReadTimeout", "ok"], "success", None),
])
def test_telegram_retry_sequences(out, sequence, result, err):
    calls = {"n": 0}

    def handler(request):
        kind = sequence[min(calls["n"], len(sequence) - 1)]
        calls["n"] += 1
        if kind == "ok":
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise getattr(httpx, kind)("x", request=request)
    ctx, tok = _in_request()
    if result == "success":
        real_telegram(handler).send_message(1, "x")
    else:
        with pytest.raises(TelegramError):
            real_telegram(handler).send_message(1, "x")
    audit_events.end_request(ctx, tok, 200)
    _, res = _outcome(out)
    assert res["result"] == result and res.get("error_class") == err


def test_wb_non_2xx_accepted_write_is_not_a_known_success(out):
    ctx, tok = _in_request()
    got = _wb(lambda r: httpx.Response(302, headers={"location": "https://x"})).publish_answer("FB5", "t")
    audit_events.end_request(ctx, tok, 200)
    assert got == {"status_code": 302}                                  # business behaviour unchanged
    _, res = _outcome(out)
    assert res["event_type"] == "mutation_failure" and res["result"] == "outcome_unknown"
    assert res["error_class"] == "HTTP302"


def test_chat_ref_reveals_only_the_class(out, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-5578869057")
    assert audit_events.chat_ref(-5578869057) == "tg_chat:configured"
    assert audit_events.chat_ref(302044578) == "tg_chat:other"
    assert audit_events.safe_ref("wb", "\udcff") .startswith("wb:")     # lone surrogate never raises


def test_stdout_failure_does_not_change_the_response(monkeypatch):
    class Broken:
        def write(self, *_):
            raise OSError("stdout closed")

        def flush(self):
            raise OSError("stdout closed")
    monkeypatch.setattr(audit_events.sys, "stdout", Broken())
    deps = with_secrets(make_deps([]))
    client = client_for(poll_route, deps, monkeypatch)
    assert client.post("/poll", headers={"X-Scheduler-Secret": "wrong-value-123"}).status_code == 403


def test_lifespan_passes_through(out):
    started = []
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)

    @app.on_event("startup")
    def _s():
        started.append(True)
    with TestClient(app) as client:
        assert client.get("/nope").status_code == 404
    assert started == [True]
    assert {e["event_type"] for e in events(out)} == {"request_start", "request_done"}


def test_concurrent_requests_do_not_share_context(out, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    deps = with_secrets(make_deps([]))
    deps.telegram = real_telegram()
    app = FastAPI()
    app.add_middleware(SecurityAuditMiddleware)

    @app.post("/poll")
    def one_mutation():
        audit_events.record_auth("scheduler_secret", ok=True)
        deps.telegram.send_message(1, "x")
        return {"ok": True}
    client = TestClient(app)
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda i: client.post("/poll", headers={
            "X-Cloud-Trace-Context": f"{i:032x}/1"}).status_code, range(16)))
    assert codes == [200] * 16
    by_cid: dict[str, list] = {}
    for e in events(out):
        by_cid.setdefault(e["correlation_id"], []).append(e)
    assert len(by_cid) == 16
    for evs in by_cid.values():
        chain_ok(evs)
        assert evs[-1]["mutation_attempts"] == 1
