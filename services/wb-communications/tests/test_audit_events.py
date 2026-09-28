"""D-19b: структурированные события аутентификации и внешних изменений, связанные trace запроса.

Доверенный аудит AE сопоставляет журнал запроса Cloud Run → auth_ok → mutation_* по trace.
Все секреты здесь СИНТЕТИЧЕСКИЕ.
"""
from __future__ import annotations

import io
import json
import logging
import sys

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils import logging as L

TRACE = "0123456789abcdef0123456789abcdef"
XCTC = f"{TRACE}/12345;o=1"
FAKE_TG = "123456789:AAFAKE_fake-token-for-tests_0123456789abcdef"
FAKE_WH = "synthetic-webhook-secret-xyz-123456"
FAKE_SCH = "synthetic-scheduler-secret-abc-987654"


@pytest.fixture
def events(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-x")
    monkeypatch.setenv("K_SERVICE", "evetis-wb-communications")
    monkeypatch.setenv("K_REVISION", "evetis-wb-communications-00099-abc")
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, set(L._known_secrets))
    L._known_secrets.clear()
    L.configure_logging("INFO")
    for s in (FAKE_TG, FAKE_WH, FAKE_SCH):
        L.register_secret(s)

    def read() -> list[dict]:
        lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
        buf.seek(0)
        buf.truncate()
        return lines
    yield read
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    L._known_secrets.clear()
    L._known_secrets.update(saved[2])
    logging.getLogger("httpx").filters.clear()
    logging.captureWarnings(False)
    logging.raiseExceptions = True


def audit(lines, kind=None):
    return [x for x in lines if x.get("audit_event") and (kind is None or x["audit_event"] == kind)]


def assert_no_secrets(lines):
    text = json.dumps(lines)
    for s in (FAKE_TG, FAKE_TG.split(":")[1], FAKE_WH, FAKE_SCH):
        assert s not in text


# ------------------------------------------------------------------ trace ---
@pytest.mark.parametrize("xctc,tp,expected", [
    (XCTC, None, (TRACE, format(12345, "016x"))),
    (TRACE.upper() + "/7", None, (TRACE, format(7, "016x"))),
    (None, f"00-{TRACE}-00f067aa0ba902b7-01", (TRACE, "00f067aa0ba902b7")),
    ("not-a-trace/1", None, ("", "")),
    (None, "00-" + "0" * 32 + "-00f067aa0ba902b7-01", ("", "")),
    (None, "garbage", ("", "")),
    ("", "", ("", "")),
])
def test_parse_trace(xctc, tp, expected):
    assert L.parse_trace(xctc, tp) == expected


def test_log_lines_carry_cloud_logging_trace(events):
    tokens = L.set_request_trace(XCTC, None)
    try:
        logging.getLogger("app.x").info("hello")
    finally:
        L.reset_request_trace(tokens)
    line = events()[-1]
    assert line["logging.googleapis.com/trace"] == f"projects/proj-x/traces/{TRACE}"
    assert line["trace_id"] == TRACE and line["logging.googleapis.com/spanId"] == format(12345, "016x")
    logging.getLogger("app.x").info("outside request")
    assert "trace_id" not in events()[-1]


# ------------------------------------------------------------------ audit_event contract ---
def test_audit_event_schema_and_field_allowlist(events):
    L.audit_event("auth_ok", route="/poll", mechanism="m", principal_class="p", result="ok",
                  token=FAKE_TG, secret=FAKE_WH, extra={"a": 1}, http_status=200, ratio=0.5)
    e = audit(events())[0]
    assert e["audit_event"] == "auth_ok" and e["audit_schema"] == "wbc-audit/1"
    assert e["service"] == "evetis-wb-communications" and e["revision"].endswith("00099-abc")
    assert {"token", "secret", "extra", "ratio"}.isdisjoint(e) and e["http_status"] == 200
    assert_no_secrets([e])


def test_unknown_event_names_are_not_emitted(events):
    L.audit_event("auth_maybe", route="/poll")
    assert audit(events()) == []


def test_audit_event_never_raises(events, monkeypatch):
    monkeypatch.setattr(L._audit_logger, "info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    L.audit_event("auth_ok", route="/poll")      # must not propagate


def test_safe_ref_hides_raw_id():
    assert L.safe_ref("abc123") == L.safe_ref("abc123") and "abc123" not in L.safe_ref("abc123")
    assert L.safe_ref(None) == "" and L.safe_ref("") == ""


# ------------------------------------------------------------------ routes ---
class _Secrets:
    telegram_webhook_secret = FAKE_WH
    scheduler_secret = FAKE_SCH


class _Settings:
    secrets = _Secrets()
    admin_token = "synthetic-admin-token-777777"
    is_production = True


class _Repo:
    def begin_update(self, update_id):
        return "new"

    def complete_update(self, update_id):
        pass

    def release_update(self, update_id):
        pass


class _Deps:
    settings = _Settings()
    repo = _Repo()


def _client(monkeypatch, handler_result=None, poll_result=None):
    from app.routes import admin, poll, telegram_webhook
    deps = _Deps()
    for mod in (poll, telegram_webhook, admin):
        monkeypatch.setattr(mod, "get_deps", lambda: deps)
    monkeypatch.setattr(telegram_webhook, "handle_update", lambda d, u: handler_result or {"status": "published"})
    monkeypatch.setattr(telegram_webhook, "flush_events", lambda d: None)
    monkeypatch.setattr(poll, "run_poll", lambda d: poll_result or {"fetched": 0})
    app = FastAPI()
    app.middleware("http")(L.trace_middleware)
    app.include_router(poll.router)
    app.include_router(telegram_webhook.router)
    app.include_router(admin.router)
    return TestClient(app)


def test_webhook_auth_ok_bound_to_request_trace(events, monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/telegram-webhook", json={"update_id": 1},
               headers={"X-Telegram-Bot-Api-Secret-Token": FAKE_WH, "X-Cloud-Trace-Context": XCTC})
    assert r.status_code == 200
    lines = events()
    ok = audit(lines, "auth_ok")
    assert len(ok) == 1 and ok[0]["mechanism"] == "telegram_secret_token" and ok[0]["route"] == "/telegram-webhook"
    assert ok[0]["trace_id"] == TRACE and ok[0]["logging.googleapis.com/trace"].endswith(TRACE)
    assert_no_secrets(lines)


def test_webhook_bad_secret_is_denied_never_ok(events, monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/telegram-webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong",
                                                      "X-Cloud-Trace-Context": XCTC})
    assert r.status_code == 403
    lines = events()
    assert audit(lines, "auth_ok") == [] and audit(lines, "auth_denied")[0]["result"] == "bad_secret"


def test_poll_auth_events(events, monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/poll", headers={"X-Scheduler-Secret": FAKE_SCH, "X-Cloud-Trace-Context": XCTC}).status_code == 200
    lines = events()
    ok = audit(lines, "auth_ok")
    assert len(ok) == 1 and ok[0]["mechanism"] == "scheduler_shared_secret" and ok[0]["trace_id"] == TRACE
    assert c.post("/poll", headers={"X-Scheduler-Secret": "nope"}).status_code == 403
    lines = events()
    assert audit(lines, "auth_ok") == [] and audit(lines, "auth_denied")[0]["result"] == "bad_secret"
    assert_no_secrets(lines)


def test_admin_auth_events(events, monkeypatch):
    from app.routes import admin
    monkeypatch.setattr(admin, "get_deps", lambda: _Deps())
    with pytest.raises(Exception):
        admin._check_admin("wrong")
    assert audit(events(), "auth_denied")[0]["mechanism"] == "admin_token"
    admin._check_admin("synthetic-admin-token-777777")
    lines = events()
    assert audit(lines, "auth_ok")[0]["principal_class"] == "operator"
    assert "synthetic-admin-token-777777" not in json.dumps(lines)


def test_allowlist_decision_is_recorded(events):
    from app.services import pipeline

    class S:
        allowed_chat_ids = {"-100"}
        telegram_allowed_user_ids = {"42"}

    class D:
        settings = S()
    assert pipeline._allowed(D(), "-100", "42") is True
    assert pipeline._allowed(D(), "-100", "43") is False
    lines = audit(events())
    assert [x["audit_event"] for x in lines] == ["auth_ok", "auth_denied"]
    assert all(x["mechanism"] == "telegram_allowlist" for x in lines)
    assert "42" not in json.dumps([{k: v for k, v in x.items() if k != "revision"} for x in lines])


# ------------------------------------------------------------------ mutations ---
def _wb():
    from app.config import Settings
    from app.services.wb_client import WBClient
    s = Settings()
    return WBClient(s, "synthetic-wb-token-5555555555", client=httpx.Client(base_url="https://feedbacks-api.test")), s


@respx.mock
def test_wb_write_success_emits_attempt_and_success_with_hashed_target(events):
    wb, s = _wb()
    respx.request(s.wb_answer_method, f"https://feedbacks-api.test{s.wb_answer_path}").mock(return_value=httpx.Response(204))
    wb.publish_answer("FB-RAW-ID-1", "спасибо")
    ev = audit(events())
    assert [e["audit_event"] for e in ev] == ["mutation_attempt", "mutation_success"]
    assert ev[0]["target_system"] == "wildberries" and ev[0]["target_ref"] == L.safe_ref("FB-RAW-ID-1")
    assert "FB-RAW-ID-1" not in json.dumps(ev) and ev[1]["http_status"] == 204


@respx.mock
def test_wb_write_rejected_and_unknown_outcomes(events):
    from app.domain.exceptions import WBApiError, WBPublishOutcomeUnknown
    wb, s = _wb()
    route = respx.request(s.wb_answer_method, f"https://feedbacks-api.test{s.wb_answer_path}")
    route.mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(WBApiError):
        wb.publish_answer("FB-2", "x")
    assert [e["audit_event"] for e in audit(events())] == ["mutation_attempt", "mutation_failure"]
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(WBPublishOutcomeUnknown):
        wb.publish_answer("FB-3", "x")
    ev = audit(events())
    assert ev[-1]["audit_event"] == "mutation_failure" and ev[-1]["result"] == "outcome_unknown"


URL = f"https://api.telegram.org/bot{FAKE_TG}"


@respx.mock
def test_telegram_mutations_success_failure_and_reads(events):
    from app.domain.exceptions import TelegramError
    from app.services.telegram_client import TelegramClient
    tg = TelegramClient(FAKE_TG)
    respx.post(f"{URL}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    respx.post(f"{URL}/editMessageText").mock(return_value=httpx.Response(200, json={"ok": False, "description": "x"}))
    respx.post(f"{URL}/getMe").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    tg.send_message(1, "hi")
    with pytest.raises(TelegramError):
        tg.edit_message_text(1, 2, "t")
    tg._call("getMe", {})
    lines = events()
    ev = audit(lines)
    assert [(e["audit_event"], e["mutation_class"]) for e in ev] == [
        ("mutation_attempt", "telegram:sendMessage"), ("mutation_success", "telegram:sendMessage"),
        ("mutation_attempt", "telegram:editMessageText"), ("mutation_failure", "telegram:editMessageText")]
    assert_no_secrets(lines)


def test_instrumentation_ready_marker_on_app_import(events, monkeypatch):
    L.audit_event("instrumentation_ready", result="ok")
    e = audit(events(), "instrumentation_ready")[0]
    assert e["audit_schema"] == "wbc-audit/1" and e["revision"].endswith("00099-abc")



def test_request_id_is_server_generated_and_per_request(events, monkeypatch):
    c = _client(monkeypatch)
    for _ in range(2):   # same client-chosen trace twice
        c.post("/poll", headers={"X-Scheduler-Secret": FAKE_SCH, "X-Cloud-Trace-Context": XCTC})
    ok = audit(events(), "auth_ok")
    assert len(ok) == 2 and ok[0]["trace_id"] == ok[1]["trace_id"] == TRACE
    assert ok[0]["request_id"] and ok[1]["request_id"] and ok[0]["request_id"] != ok[1]["request_id"]


def test_open_allowlist_is_recorded_as_open_not_ok(events):
    from app.services import pipeline

    class S:
        allowed_chat_ids = set()
        telegram_allowed_user_ids = set()

    class D:
        settings = S()
    assert pipeline._allowed(D(), "-1", "1") is True            # поведение не меняется (fail-open)
    e = audit(events())[0]
    assert e["result"] == "open_no_allowlist" and e["principal_class"] == "unrestricted"


def test_audit_events_survive_warning_log_level(events, monkeypatch):
    buf = logging.getLogger().handlers[0].stream        # буфер фикстуры (pytest подменяет sys.stdout в фазе теста)
    L.configure_logging("WARNING")
    logging.getLogger().handlers[0].setStream(buf)
    logging.getLogger("app.x").info("info line must be filtered at WARNING")
    L.audit_event("auth_ok", route="/poll", mechanism="m", result="ok")
    lines = events()
    assert audit(lines, "auth_ok") and not any("must be filtered" in x.get("message", "") for x in lines)



def test_partial_allowlist_is_not_a_full_pass(events):
    from app.services import pipeline

    class S:
        allowed_chat_ids = {"-100"}
        telegram_allowed_user_ids = set()

    class D:
        settings = S()
    assert pipeline._allowed(D(), "-100", "999") is True        # поведение прежнее
    assert audit(events())[0]["result"] == "ok_partial_allowlist"
