"""Учётные данные не покидают процесс (Tenancy T2, T2.2).

1. ПО ПОСТРОЕНИЮ (AST). Ни один вызов журнала (log, record_run, _strict_cap,
   safe_error_text) и ни одно поднимаемое исключение не получает в аргументах
   значения секрета: вызова secret()/seller_headers()/perf_token(), заголовков
   запроса или кэша _secrets.
2. ПО ПОВЕДЕНИЮ (T2.2). Худшие случаи — сервер повторил ключ в ошибке, упала запись
   журнала, упал сам хук исключений. Граница безопасности обязана вырезать СЕКРЕТЫ
   (Api-Key, client_secret, токен Performance) из stdout, stderr, журнала и текста
   исключений, сохранив остальную диагностику, и не трогать ИДЕНТИФИКАТОРЫ
   (Client-Id, client_id Performance).
"""
from __future__ import annotations

import ast
import io
import os
import sys
import textwrap
import types
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
    # T5: seller_client_id / perf_client_id читают ИДЕНТИФИКАТОРЫ (не секреты) для отпечатка кабинета.
    assert readers == {"seller_headers", "perf_token", "seller_client_id", "perf_client_id"}
    for name in ("entities.py", "main.py", "promo.py", "lifecycle.py", "identity.py", "credentials.py",
                 "lifecycle_core.py", "checkpoints.py", "history.py", "quota.py", "dq.py", "control_store.py"):
        tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
        assert not any(isinstance(c, ast.Call) and _callee(c) == "secret" for c in ast.walk(tree)), name


# ─────────────────────────────────────────────────── поведение (T2.2)
# Все значения — синтетика. Секреты загружаются настоящим путём C.secret(), чтобы
# проверялась и регистрация в границе безопасности, а не только её вывод.
SELLER_KEY = "0b7c0f3e-5a41-4c0e-9d1a-2f6b8e4c1a77"      # формат Api-Key, синтетика
CLIENT_ID = "5550199"                                     # формат Client-Id, синтетика
PERF_SECRET = "SYNTH-perf-secret-Qx7vL2pT9"               # client_secret, синтетика
ESCAPEY_KEY = 'SYNTH"key\\with\nnew\ttab-ключ-☃'           # символы, которые JSON экранирует


class _FakeSM:
    def __init__(self, values):
        self.values = values

    def access_secret_version(self, request):
        name = request["name"].split("/secrets/")[1].split("/")[0]
        payload = types.SimpleNamespace(data=self.values[name].encode("utf-8"))
        return types.SimpleNamespace(payload=payload)


@pytest.fixture
def boundary(monkeypatch):
    """Чистая граница безопасности и загрузчик секретов через C.secret()."""
    monkeypatch.setattr(C, "_secrets", {})
    monkeypatch.setattr(C, "_redactions", {})
    monkeypatch.setattr(C, "_redaction_re", None)
    monkeypatch.setattr(C, "_perf_token", {"value": None, "at": 0})

    def load(**by_role):
        names = {"seller_api_key": C.CONFIG.secret_seller_api_key,
                 "seller_client_id": C.CONFIG.secret_seller_client_id,
                 "perf_client_id": C.CONFIG.secret_perf_client_id,
                 "perf_client_secret": C.CONFIG.secret_perf_client_secret}
        monkeypatch.setattr(C, "_sm", _FakeSM({names[r]: v for r, v in by_role.items()}))
        for role in by_role:
            C.secret(names[role])
    return load


def test_secret_is_redacted_in_place_and_the_event_survives(boundary, capsys):
    boundary(seller_api_key=SELLER_KEY)
    C.log(event="entity_failed", entity="seller_info", error=f"401 bad key {SELLER_KEY}")
    out = json.loads(capsys.readouterr().out)
    assert out == {"event": "entity_failed", "entity": "seller_info",
                   "error": "401 bad key <redacted:seller_api_key>"}


@pytest.mark.parametrize("payload", [
    {"rows_received": int(CLIENT_ID)}, {"campaign_id": CLIENT_ID}, {"http": f"HTTP 400 {CLIENT_ID}"},
    {"ts": f"2026-09-24T10:00:00.{CLIENT_ID[:6]}"}, {"sku": f"9{CLIENT_ID}3"},
    {"error": f"RuntimeError('campaign {CLIENT_ID} not found')"},
])
def test_l4_client_id_is_an_identifier_and_never_suppresses_events(boundary, capsys, payload):
    """L4: Client-Id — идентификатор. Совпадение числа не прячет и не портит событие."""
    boundary(seller_api_key=SELLER_KEY, seller_client_id=CLIENT_ID)
    C.log(event="entity_done", **payload)
    assert json.loads(capsys.readouterr().out) == {"event": "entity_done", **payload}


def test_l7_escaped_secret_is_redacted_before_serialization(boundary, capsys):
    boundary(seller_api_key=ESCAPEY_KEY)
    C.log(event="x", error=f"echo {ESCAPEY_KEY}", nested={"repr": repr({"k": ESCAPEY_KEY})})
    line = capsys.readouterr().out
    doc = json.loads(line)                                  # лог остаётся валидным JSON
    for form in (ESCAPEY_KEY, json.dumps(ESCAPEY_KEY)[1:-1], repr(ESCAPEY_KEY)[1:-1],
                 json.dumps(ESCAPEY_KEY, ensure_ascii=False)[1:-1], "key\\\\with", "ключ-☃"):
        assert form not in line
    assert doc["error"] == "echo <redacted:seller_api_key>"
    assert "<redacted:seller_api_key>" in doc["nested"]["repr"]


def test_truncation_never_leaves_a_fragment(boundary):
    boundary(seller_api_key=SELLER_KEY)
    text = C.safe_error_text("x" * 390 + " " + SELLER_KEY)   # ключ на границе 400 символов
    assert SELLER_KEY[:6] not in text and text.startswith("x" * 390)
    assert C.safe_error_text("x" * 500) == "x" * 400        # без секретов — прежняя обрезка


def test_http_error_payload_is_redacted_before_it_is_truncated(boundary, monkeypatch):
    """_request обрезает тело ошибки до 400: вырезание обязано идти раньше обрезки."""
    boundary(seller_api_key=SELLER_KEY)
    import urllib.error

    body = ("y" * 395 + SELLER_KEY).encode()

    def raising_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(C.urllib.request, "urlopen", raising_urlopen)
    code, d = C._request(C.urllib.request.Request("https://api-seller.ozon.ru/x"))
    assert code == 401 and SELLER_KEY[:5] not in d["_error"]


def test_one_secret_inside_another_and_overlaps(boundary):
    boundary(seller_api_key="SYNTH-longkey-ABCDEF123456", perf_client_secret="ABCDEF123456")
    out = C.redact_text("a SYNTH-longkey-ABCDEF123456 b ABCDEF123456 c")
    assert out == "a <redacted:seller_api_key> b <redacted:performance_client_secret> c"


def test_performance_token_and_secret_are_secrets_client_id_is_not(boundary, monkeypatch):
    boundary(perf_client_id="1234-5678@advertising.performance.ozon.ru", perf_client_secret=PERF_SECRET)
    monkeypatch.setattr(C, "_request", lambda req, *a, **k: (200, {"access_token": "SYNTH-token-9Zq"}))
    C.perf_token()
    out = C.redact_text(f"{PERF_SECRET} SYNTH-token-9Zq 1234-5678@advertising.performance.ozon.ru")
    assert out == ("<redacted:performance_client_secret> <redacted:performance_access_token> "
                   "1234-5678@advertising.performance.ozon.ru")


def test_performance_token_failure_does_not_echo_response(boundary, monkeypatch):
    boundary(perf_client_id="1234-5678@advertising.performance.ozon.ru", perf_client_secret=PERF_SECRET)
    monkeypatch.setattr(C, "_request", lambda req, *a, **k: (400, {"_error": req.data.decode()}))
    with pytest.raises(RuntimeError) as e:
        C.perf_token()
    assert PERF_SECRET not in str(e.value)


@pytest.mark.parametrize("with_unrelated_secret", [False, True])
def test_log_output_is_byte_identical_when_no_secret_is_present(boundary, capsys,
                                                                 with_unrelated_secret):
    """Без секрета в событии вывод побайтно равен прежнему json.dumps(default=str)."""
    import datetime
    import decimal
    if with_unrelated_secret:
        boundary(seller_api_key=SELLER_KEY)
    kw = {"event": "e", "when": datetime.date(2026, 9, 24), "amount": decimal.Decimal("1.50"),
          "pair": (1, "a"), "none": None, "flag": True, "n": 7, "f": 0.25, "текст": "ключ"}
    C.log(**kw)
    assert capsys.readouterr().out == json.dumps(kw, ensure_ascii=False, default=str) + "\n"


# ─────────────────────────────────────────────── L3: двойной сбой (процесс)
_STUBS = ("import sys, types\n"
          "g=types.ModuleType('google'); c=types.ModuleType('google.cloud')\n"
          "b=types.ModuleType('google.cloud.bigquery'); s=types.ModuleType('google.cloud.secretmanager')\n"
          "b.Client=object; s.SecretManagerServiceClient=object\n"
          "c.bigquery, c.secretmanager, g.cloud = b, s, c\n"
          "sys.modules.update({'google':g,'google.cloud':c,'google.cloud.bigquery':b,"
          "'google.cloud.secretmanager':s})\n")


def _run_main(journal_fails, key=SELLER_KEY):
    """Настоящий процесс: main() как в Cloud Run, необработанное исключение → excepthook."""
    import subprocess
    code = _STUBS + textwrap.dedent(f"""
        import types
        sys.path.insert(0, {str(RUNTIME)!r})
        import common as C, main as M
        vals = {{C.CONFIG.secret_seller_client_id: {CLIENT_ID!r}, C.CONFIG.secret_seller_api_key: {key!r}}}
        class SM:
            def access_secret_version(self, request):
                n = request["name"].split("/secrets/")[1].split("/")[0]
                return types.SimpleNamespace(payload=types.SimpleNamespace(data=vals[n].encode()))
        C._sm = SM()
        C._request = lambda req, *a, **k: (401, {{"_error": "bad key " + req.get_header("Api-key")}})
        class BQ:
            def insert_rows_json(self, table, rows):
                if {journal_fails!r}:
                    raise RuntimeError("BigQuery insertAll 503 backendError")
                print("JOURNAL_ROW " + __import__("json").dumps(rows, ensure_ascii=False))
        C.bq = lambda: BQ()
        M.main()
    """)
    env = {"PATH": os.environ["PATH"], "GCP_PROJECT_ID": "offline-test-project",
           "ENTITIES": "seller_info", "INGESTION_RUN_ID": "rt-l3"}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=60)


def _forms(value):
    return {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
            repr(value)[1:-1]}


@pytest.mark.parametrize("key", [SELLER_KEY, ESCAPEY_KEY])
def test_l3_primary_failure_only(key):
    r = _run_main(journal_fails=False, key=key)
    assert r.returncode == 1
    blob = r.stdout + r.stderr
    assert not any(f in blob for f in _forms(key)), "секрет в stdout/stderr/журнале"
    assert "<redacted:seller_api_key>" in r.stdout               # диагностика сохранена
    assert "JOURNAL_ROW" in r.stdout and '"status": "FAILED"' in r.stdout


@pytest.mark.parametrize("key", [SELLER_KEY, ESCAPEY_KEY])
def test_l3_primary_failure_plus_journal_failure(key):
    r = _run_main(journal_fails=True, key=key)
    assert r.returncode == 1                                      # сбой не проглочен
    blob = r.stdout + r.stderr
    assert not any(f in blob for f in _forms(key)), "секрет в stdout/stderr/трассировке"
    assert "During handling of the above exception" not in r.stderr
    last = r.stderr.strip().splitlines()[-1]
    assert last.startswith("common.JournalWriteError: seller_info:")
    assert "RuntimeError: BigQuery insertAll 503 backendError" in last      # сбой журнала виден
    assert "исходная ошибка сущности — RuntimeError" in last                # категория исходной видна
    assert "<redacted:seller_api_key>" in last
    assert "Traceback (most recent call last)" in r.stderr                  # трассировка не выключена


# ─────────────────────────────── глобальный sys.excepthook (T2.2, ревью)
HOOK_KEY = "SYNTHKEY9Q7Z-ABCD-4455-EEFF-001122334455"


def _run_hooked(body: str, install_hook: bool = True):
    """Процесс с runtime-common, зарегистрированным секретом и (опционально) хуком."""
    import subprocess
    code = (_STUBS + f"sys.path.insert(0, {str(RUNTIME)!r})\nimport common as C\n"
            f"C.register_secret({HOOK_KEY!r}, 'seller_api_key')\n"
            + ("sys.excepthook = C.safe_excepthook\n" if install_hook else "")
            + textwrap.dedent(body))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], "GCP_PROJECT_ID": "offline-test-project"})


def test_hook_output_equals_standard_traceback_without_secrets():
    """Обычная ошибка: тот же текст, что у стандартной трассировки Python."""
    r = _run_hooked("""
        import io, traceback
        def work():
            raise ValueError('ordinary failure')
        try:
            work()
        except ValueError as e:
            buf = io.StringIO(); real = sys.stderr; sys.stderr = buf
            C.safe_excepthook(type(e), e, e.__traceback__)
            sys.stderr = real
            assert buf.getvalue() == ''.join(traceback.format_exception(type(e), e, e.__traceback__))
        work()
    """)
    assert r.returncode == 1
    assert "Traceback (most recent call last)" in r.stderr and 'in work' in r.stderr
    assert r.stderr.strip().endswith("ValueError: ordinary failure")
    assert C.LEAK_MARKER not in r.stderr


@pytest.mark.parametrize("body,last", [
    (f"raise RuntimeError('api said ' + {HOOK_KEY!r})",
     "RuntimeError: api said <redacted:seller_api_key>"),
    (f"try:\n    raise KeyError({HOOK_KEY!r})\nexcept KeyError as e:\n    raise RuntimeError('wrapped') from e",
     "RuntimeError: wrapped"),
    (f"try:\n    raise KeyError({HOOK_KEY!r})\nexcept KeyError:\n    raise RuntimeError(repr({{'k': {HOOK_KEY!r}}}))",
     "RuntimeError: {'k': '<redacted:seller_api_key>'}"),
    (f"try:\n    raise RuntimeError('primary ' + {HOOK_KEY!r})\nexcept RuntimeError as e:\n"
     f"    raise C.JournalWriteError('seller_info', e, RuntimeError('bq 503')) from None",
     "common.JournalWriteError: seller_info: запись в OZON_INGESTION_RUNS не удалась — RuntimeError: bq 503; "
     "исходная ошибка сущности — RuntimeError: primary <redacted:seller_api_key>"),
])
def test_hook_redacts_every_component_of_a_chain(body, last):
    r = _run_hooked(body)
    assert r.returncode == 1 and HOOK_KEY not in r.stderr
    assert "Traceback (most recent call last)" in r.stderr
    assert r.stderr.strip().split("\n")[-1] == last


def test_hook_never_raises_and_never_falls_back_to_the_unsafe_default():
    """Сбой вырезания или записи в stderr не должен включать стандартный вывод CPython
    «Original exception was:» — тот напечатал бы исключение без вырезания."""
    r = _run_hooked(f"""
        C.redact_text = lambda t: (_ for _ in ()).throw(RuntimeError('boom'))
        raise RuntimeError('secret ' + {HOOK_KEY!r})
    """)
    assert r.returncode == 1 and HOOK_KEY not in r.stderr
    assert "Original exception was" not in r.stderr and "Error in sys.excepthook" not in r.stderr
    assert "RuntimeError: <текст исключения не удалось безопасно отформатировать>" in r.stderr

    r = _run_hooked(f"""
        class Broken:
            def write(self, t): raise OSError('stderr gone')
            def flush(self): pass
        sys.stderr = Broken()
        raise RuntimeError('secret ' + {HOOK_KEY!r})
    """)
    assert r.returncode == 1 and HOOK_KEY not in r.stderr
    assert "Original exception was" not in r.stderr and "Error in sys.excepthook" not in r.stderr
    assert "RuntimeError: secret <redacted:seller_api_key>" in r.stderr      # ушло в sys.__stderr__


@pytest.mark.parametrize("stmt,code", [("sys.exit(0)", 0), ("sys.exit(3)", 3)])
def test_hook_does_not_touch_system_exit(stmt, code):
    r = _run_hooked(stmt)
    assert r.returncode == code and r.stderr == ""


def test_keyboard_interrupt_is_not_an_application_failure():
    """Ctrl-C внутри сущности: не журналируется как FAILED, не становится
    JournalWriteError, процесс завершается как по SIGINT (как без хука)."""
    import subprocess
    code = _STUBS + textwrap.dedent(f"""
        sys.path.insert(0, {str(RUNTIME)!r})
        import common as C, entities as E, main as M
        def interrupted(*a, **k):
            raise KeyboardInterrupt
        E.REGISTRY["catalog"] = (interrupted, 0, "daily")
        class BQ:
            def insert_rows_json(self, table, rows):
                print("JOURNAL_ROW", rows)
        C.bq = lambda: BQ()
        M.main()
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "GCP_PROJECT_ID": "offline-test-project",
                            "ENTITIES": "catalog"})
    assert r.returncode in (-2, 130)                         # SIGINT, как у стандартного хука
    assert "JOURNAL_ROW" not in r.stdout and "JournalWriteError" not in r.stderr
    assert r.stderr.strip().endswith("KeyboardInterrupt")


# ───────────────────────────── обрезка: ни одного фрагмента секрета (ревью T2.2)
TRUNC_KEY = "SYNTHK9QZ7W4M2PRT8LV6N3H"          # нет 'x'/'y': фрагмент однозначно из секрета
TRUNC_INNER = "M2PRT8LV6N3H"                    # второй секрет — подстрока первого
FRAGMENT = 6


def _fragments(value):
    return {value[i:i + FRAGMENT] for i in range(len(value) - FRAGMENT + 1)}


def test_no_truncation_offset_leaves_a_recognizable_fragment(boundary):
    """Секрет начинается до границы и кончается после; от него остался бы префикс или
    суффикс; один секрет внутри другого — ни при какой обрезке фрагмента нет.

    Суффикс мог бы уцелеть только при обрезке СЛЕВА (text[-n:]). В runtime такой
    обрезки нет; случай всё равно проверен: вырезание идёт до любой обрезки.
    Произвольные короткие (< 6 символов) совпадения с частью секрета в чужом тексте
    возможны естественно и не цензурируются — это невыполнимо и не нужно.
    """
    boundary(seller_api_key=TRUNC_KEY, perf_client_secret=TRUNC_INNER)
    frags = _fragments(TRUNC_KEY) | _fragments(TRUNC_INNER)
    for secret in (TRUNC_KEY, TRUNC_INNER):
        for pad in range(0, len(secret) + 4):
            source = "x" * pad + secret + "y" * 12
            for limit in range(max(1, pad - 3), pad + len(secret) + 4):
                head = C.safe_error_text(source, limit)
                tail = C.redact_text(source)[-limit:]            # обрезка слева — после вырезания
                for out in (head, tail):
                    assert not any(f in out for f in frags), (secret, pad, limit, out)


def test_main_installs_the_safe_hook_for_any_uncaught_exception():
    """Вторая линия защиты: ЛЮБОЕ необработанное исключение процесса runtime (не только
    JournalWriteError, который безопасен и сам) печатается через safe_excepthook.
    Отрицательный контроль: без `sys.excepthook = C.safe_excepthook` в main.py тест падает."""
    import subprocess
    code = _STUBS + textwrap.dedent(f"""
        import types
        sys.path.insert(0, {str(RUNTIME)!r})
        import common as C
        vals = {{C.CONFIG.secret_seller_api_key: {HOOK_KEY!r}}}
        class SM:
            def access_secret_version(self, request):
                n = request["name"].split("/secrets/")[1].split("/")[0]
                return types.SimpleNamespace(payload=types.SimpleNamespace(data=vals[n].encode()))
        C._sm = SM()
        import main
        assert sys.excepthook is C.safe_excepthook, "main.py не поставил безопасный хук"
        key = C.secret(C.CONFIG.secret_seller_api_key)
        def programming_error():
            raise TypeError("unexpected payload " + repr({{"api_key": key}}))
        programming_error()
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "GCP_PROJECT_ID": "offline-test-project"})
    assert r.returncode == 1
    assert HOOK_KEY not in r.stderr
    assert r.stderr.strip().split("\n")[-1] == \
        "TypeError: unexpected payload {'api_key': '<redacted:seller_api_key>'}"
    assert "in programming_error" in r.stderr                 # кадры трассировки на месте
