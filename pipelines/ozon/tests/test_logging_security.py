"""Учётные данные не попадают в журнал (Tenancy T2).

Две линии проверки, как и две линии защиты в common.py:

1. ПО ПОСТРОЕНИЮ (AST). Ни один вызов журнала (log, record_run, _strict_cap,
   safe_error_text) и ни одно поднимаемое исключение не получает в аргументах
   значения секрета: вызова secret()/seller_headers()/perf_token(), заголовков
   запроса или кэша _secrets. Это доказывает, что значения не входят в полезную
   нагрузку журнала изначально, а не маскируются потом.
2. ПО ПОВЕДЕНИЮ. Худший случай: Ozon вернул ошибку, дословно повторяющую
   присланный ключ, и она дошла до record_run и log. Предохранитель обязан
   подавить текст целиком, не потеряв статус FAILED.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import common as C

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SOURCES = ["common.py", "entities.py", "main.py", "promo.py"]

LOG_SINKS = {"log", "record_run", "_strict_cap", "safe_error_text", "print"}
CREDENTIAL_SOURCES = {"secret", "seller_headers", "perf_token"}
CREDENTIAL_NAMES = {"_secrets", "headers", "_perf_token"}


def _callee(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


def _credential_refs(tree: ast.AST) -> list[str]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _callee(n) in CREDENTIAL_SOURCES:
            hits.append(f"{_callee(n)}()")
        if isinstance(n, ast.Name) and n.id in CREDENTIAL_NAMES:
            hits.append(n.id)
        if isinstance(n, ast.Attribute) and n.attr in CREDENTIAL_NAMES:
            hits.append(n.attr)
    return hits


@pytest.mark.parametrize("name", SOURCES)
def test_no_log_call_receives_credential_material(name):
    tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _callee(n) in LOG_SINKS:
            args = list(n.args) + [k.value for k in n.keywords]
            for a in args:
                refs = _credential_refs(a)
                if refs:
                    bad.append(f"{name}:{n.lineno} {_callee(n)} ← {refs}")
    assert not bad, "учётные данные в аргументах журнала:\n" + "\n".join(bad)


@pytest.mark.parametrize("name", SOURCES)
def test_no_raised_exception_carries_credential_material(name):
    tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
    bad = [f"{name}:{n.lineno} ← {_credential_refs(n.exc)}"
           for n in ast.walk(tree)
           if isinstance(n, ast.Raise) and n.exc is not None and _credential_refs(n.exc)]
    assert not bad, "учётные данные в тексте исключения:\n" + "\n".join(bad)


def test_the_only_credential_readers_are_known():
    """secret() вызывается только из seller_headers и perf_token."""
    tree = ast.parse((RUNTIME / "common.py").read_text(encoding="utf-8"))
    readers = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        if any(isinstance(c, ast.Call) and _callee(c) == "secret" for c in ast.walk(fn)):
            readers.add(fn.name)
    assert readers == {"seller_headers", "perf_token"}
    for name in ("entities.py", "main.py", "promo.py"):
        tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
        assert not any(isinstance(c, ast.Call) and _callee(c) == "secret" for c in ast.walk(tree)), name


# ─────────────────────────────────────────────────── поведение
SELLER_KEY = "0b7c0f3e-5a41-4c0e-9d1a-2f6b8e4c1a77"      # формат Api-Key, синтетика
CLIENT_ID = "5550199"                                     # формат Client-Id, синтетика


@pytest.fixture
def loaded_secrets(monkeypatch):
    monkeypatch.setattr(C, "_secrets", {C.CONFIG.secret_seller_client_id: CLIENT_ID,
                                        C.CONFIG.secret_seller_api_key: SELLER_KEY})
    monkeypatch.setattr(C, "_perf_token", {"value": None, "at": 0})


def test_log_suppresses_payload_with_credential(loaded_secrets, capsys):
    C.log(event="entity_failed", error=f"401 bad key {SELLER_KEY}")
    out = capsys.readouterr().out
    assert SELLER_KEY not in out
    assert json.loads(out) == {"event": C.LEAK_MARKER, "suppressed_event": "entity_failed"}


def test_short_client_id_inside_longer_number_is_not_a_false_positive(loaded_secrets, capsys):
    C.log(event="entity_done", sku=f"9{CLIENT_ID}3", rows_received=5)
    assert json.loads(capsys.readouterr().out)["event"] == "entity_done"


def test_safe_error_text_checks_before_truncation(loaded_secrets):
    err = "x" * 390 + " " + SELLER_KEY            # ключ пересекает границу 400 символов
    text = C.safe_error_text(err)
    assert SELLER_KEY[:8] not in text and C.LEAK_MARKER in text
    assert C.safe_error_text("x" * 500) == "x" * 400    # без секретов — прежняя обрезка


def test_worst_case_echoed_key_never_reaches_log_or_run_table(loaded_secrets, monkeypatch, capsys):
    """Ozon вернул 401 и повторил присланный Api-Key в теле ошибки."""
    import main as M

    def echo_request(req, *_a, **_k):
        return 401, {"_error": f"invalid Api-Key {req.get_header('Api-key')} "
                               f"for client {req.get_header('Client-id')}"}

    rows = []

    class FakeBQ:
        def insert_rows_json(self, table, batch):
            rows.extend(batch)

    monkeypatch.setattr(C, "_request", echo_request)
    monkeypatch.setattr(C, "bq", lambda: FakeBQ())
    monkeypatch.setenv("ENTITIES", "seller_info")
    with pytest.raises(SystemExit) as e:
        M.main()
    assert e.value.code == 1

    out = capsys.readouterr().out
    assert SELLER_KEY not in out and CLIENT_ID not in out
    (row,) = rows
    assert row["status"] == "FAILED" and row["entity"] == "seller_info"
    assert SELLER_KEY not in json.dumps(row) and CLIENT_ID not in json.dumps(row)
    assert row["error_message"].startswith(C.LEAK_MARKER)


def test_performance_token_failure_does_not_echo_response(loaded_secrets, monkeypatch):
    monkeypatch.setitem(C._secrets, C.CONFIG.secret_perf_client_id, "1234-5678@advertising.performance.ozon.ru")
    monkeypatch.setitem(C._secrets, C.CONFIG.secret_perf_client_secret, "perf-secret-sentinel-value-01")
    monkeypatch.setattr(C, "_request",
                        lambda req, *a, **k: (400, {"_error": req.data.decode()}))
    with pytest.raises(RuntimeError) as e:
        C.perf_token()
    assert "perf-secret-sentinel-value-01" not in str(e.value)
    assert "advertising.performance" not in str(e.value)
