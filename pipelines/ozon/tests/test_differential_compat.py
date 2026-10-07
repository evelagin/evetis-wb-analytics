"""Различающий прогон: Ozon runtime до T2 против текущего с конфигурацией EVETIS (T2.2, L8).

Часть контракта совместимости runtime: любое изменение pipelines/ozon/runtime обязано
проходить этот тест. Эталон — замороженная копия runtime до Tenancy T2
(compat/baseline_pre_t2, провенанс и sha256 — в PROVENANCE.json).

Сравнивается ВСЁ наблюдаемое (compat/driver.py): HTTP, какие секреты запрошены,
SQL, загружаемые данные, строки журнала, stdout, stderr, код выхода.

Разрешённые разницы — ровно две, и каждая проверяется явно, а не игнорируется:

  D1  Вырезание секретов. Где эталон выводил значение класса СЕКРЕТ (Api-Key,
      client_secret, токен Performance) — в stdout, строке журнала, тексте ошибки, SQL
      манифеста, — текущий runtime выводит маркер «<redacted:роль>». Нормализация:
      в следе ЭТАЛОНА каждая запись секрета, включая экранированные, заменяется
      маркером; после этого следы обязаны совпасть побайтно. Идентификаторы
      (Client-Id, client_id Performance) не вырезаются и не нормализуются (L4).
  D2  Сбой записи журнала внутри обработки ошибки сущности. Эталон выпускал в stderr
      неявную цепочку исключений с сырым текстом исходной ошибки; текущий runtime
      поднимает JournalWriteError с очищенной сводкой обеих ошибок (L3). Поток
      управления, stdout, операции BigQuery и код выхода обязаны совпасть; последняя
      строка stderr обязана назвать сущность и обе ошибки.

Всё остальное — побайтное равенство. Строгий режим (07) у эталона отсутствует и
проверяется только на текущем runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
COMPAT = TESTS / "compat"
BASELINE = COMPAT / "baseline_pre_t2"
CURRENT = TESTS.parent / "runtime"
sys.path.insert(0, str(COMPAT))
import scenarios as S  # noqa: E402

ALLOWED_DIFFERENCES = ("D1_secret_redaction", "D2_journal_failure_rendering")


def _run(runtime: Path, name: str) -> dict:
    sc = S.SCENARIOS[name]
    env = {"PATH": os.environ["PATH"], **S.LEGACY_EVETIS_ENV, "ENTITIES": sc["entities"],
           **sc.get("env", {})}
    r = subprocess.run([sys.executable, str(COMPAT / "driver.py"), str(runtime), name],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"драйвер упал ({runtime.name}/{name}):\n{r.stderr[-2000:]}"
    return json.loads(r.stdout.strip().split("\n")[-1])


@pytest.fixture(scope="module")
def traces():
    jobs = [(rt, n) for n, sc in S.SCENARIOS.items()
            for rt in ((CURRENT,) if sc.get("compare") == "current_only" else (BASELINE, CURRENT))]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(jobs, pool.map(lambda j: _run(*j), jobs)))
    elapsed = time.monotonic() - started
    print(f"\n[differential] {len(jobs)} прогонов драйвера за {elapsed:.1f} с")
    return {(("baseline" if rt == BASELINE else "current"), n): t for (rt, n), t in results.items()}


# ───────────────────────────────────── нормализация D1 (независима от runtime)
def _forms(value: str) -> set[str]:
    forms, frontier = {value}, {value}
    for _ in range(3):
        grown = set()
        for f in frontier:
            grown |= {json.dumps(f)[1:-1], json.dumps(f, ensure_ascii=False)[1:-1], repr(f)[1:-1],
                      f.encode("unicode_escape").decode("ascii")}
        grown -= forms
        forms |= grown
        frontier = grown
    return forms


def _secret_forms(scenario: dict) -> dict[str, str]:
    out = {}
    for role, value in S.secret_values_by_role(scenario).items():
        for f in _forms(value):
            out.setdefault(f, S.SECRET_MARKERS[role])
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def _map_strings(obj, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    return obj


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)


def _d1_normalize(trace: dict, scenario: dict) -> dict:
    forms = _secret_forms(scenario)

    def fn(s):
        for f, marker in forms.items():
            s = s.replace(f, marker)
        return s
    return _map_strings(trace, fn)


def _leaks(trace: dict, scenario: dict) -> list[str]:
    forms = _secret_forms(scenario)
    return sorted({marker for s in _strings(trace) for f, marker in forms.items() if f in s})


COMPARED = [n for n, sc in S.SCENARIOS.items() if sc.get("compare") != "current_only"]


# ─────────────────────────────────────────────────────────── проверки
@pytest.mark.parametrize("name", COMPARED)
def test_functional_behavior_is_identical(traces, name):
    sc = S.SCENARIOS[name]
    base = _d1_normalize(traces[("baseline", name)], sc)
    cur = traces[("current", name)]
    for part in ("http", "secrets", "bq", "stdout", "exit"):
        assert cur[part] == base[part], f"{name}: расхождение в {part}"


@pytest.mark.parametrize("name", COMPARED)
def test_stderr_differs_only_by_d2(traces, name):
    base = _d1_normalize(traces[("baseline", name)], S.SCENARIOS[name])["stderr"]
    cur = traces[("current", name)]["stderr"]
    if not base:
        assert cur == "", f"{name}: у текущего runtime появился stderr"
        return
    # D2: оба упали с трассировкой; текущий — очищенной сводкой вместо неявной цепочки.
    assert "Traceback (most recent call last)" in base and "Traceback (most recent call last)" in cur
    base_last, cur_last = base.strip().split("\n")[-1], cur.strip().split("\n")[-1]
    assert cur_last.startswith("common.JournalWriteError: "), cur_last
    assert base_last in cur_last, f"{name}: сбой журнала эталона не назван: {base_last}"
    assert "исходная ошибка сущности — " in cur_last
    assert "During handling of the above exception" not in cur


@pytest.mark.parametrize("name", list(S.SCENARIOS))
def test_current_runtime_never_emits_secret_material(traces, name):
    assert _leaks(traces[("current", name)], S.SCENARIOS[name]) == []


@pytest.mark.parametrize("name", ["10_api_and_journal_failure_with_echo", "11_credential_echo",
                                  "12_escaped_secret_echo"])
def test_harness_sees_the_baseline_leak(traces, name):
    """Нормализация D1 не пустая: эталон в этих сценариях действительно выводил секрет."""
    assert _leaks(traces[("baseline", name)], S.SCENARIOS[name]) == ["<redacted:seller_api_key>"]


def test_l3_journal_failure_keeps_both_categories(traces):
    cur = traces[("current", "10_api_and_journal_failure_with_echo")]
    last = cur["stderr"].strip().split("\n")[-1]
    assert cur["exit"] == 1
    assert "seller_info" in last and "BigQuery insertAll 503" in last
    assert "исходная ошибка сущности — RuntimeError: seller/info 401" in last
    assert "<redacted:seller_api_key>" in last


def test_l4_client_id_as_data_is_neither_redacted_nor_suppressed(traces):
    base = traces[("baseline", "13_client_id_as_data")]
    cur = traces[("current", "13_client_id_as_data")]
    assert cur["stdout"] == base["stdout"] and cur["bq"] == base["bq"]
    assert any(S.CLIENT_ID in line for line in cur["stdout"])
    assert not any("credential_material_suppressed" in line for line in cur["stdout"])


def test_d1_is_the_only_stdout_change_under_echo(traces):
    base = traces[("baseline", "12_escaped_secret_echo")]
    cur = traces[("current", "12_escaped_secret_echo")]
    changed = [(b, c) for b, c in zip(base["stdout"], cur["stdout"]) if b != c]
    assert changed, "эталон не выводил секрет — сценарий бессилен"
    for b, c in changed:
        assert "<redacted:seller_api_key>" in c and json.loads(c)       # валидный JSON


def test_strict_mode_rejects_truncation_without_partial_writes(traces):
    cur = traces[("current", "07_page_caps_strict")]
    runs = [r for op in cur["bq"] if op[0] == "insert" for r in op[2]]
    assert {r["entity"]: r["status"] for r in runs} == {"fbo_postings": "FAILED",
                                                        "finance_accrual": "FAILED"}
    assert all("StrictLimitError" in r["error_message"] for r in runs)
    loads = [op[1] for op in cur["bq"] if op[0] == "load"]
    assert not any("POSTINGS_FBO" in t or "FINANCE_ACCRUAL" in t for t in loads)
    assert cur["exit"] == 1


def test_lenient_caps_still_truncate_like_the_baseline(traces):
    cur = traces[("current", "06_page_caps_lenient")]
    fbo_pages = [h for h in cur["http"] if h[1].endswith("/v3/posting/fbo/list")]
    assert len(fbo_pages) == 201                                  # прежний потолок, молча


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_baseline_is_the_frozen_pre_t2_runtime():
    """Эталон — побайтная копия runtime dc7fda4: sha256 и git blob id закреплены.

    git blob id совпадает с `git rev-parse dc7fda4:pipelines/ozon/runtime/<файл>`, поэтому
    тождество с исходным коммитом доказуемо и без истории в неглубоком клоне CI.
    """
    prov = json.loads((BASELINE / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert prov["source_commit"] == "dc7fda451cae302a974d2dc62488a4a940794536"
    assert prov["production_image"].endswith(
        "@sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b")
    assert "ИСТОРИЧЕСКОЕ ТЕСТОВОЕ ДОКАЗАТЕЛЬСТВО" in prov["purpose"]
    actual = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                       "git_blob": _git_blob(p.read_bytes())}
              for p in sorted(BASELINE.glob("*.py"))}
    assert actual == prov["files"], "эталон изменён — это доказательство, а не код"


def test_baseline_is_isolated_from_production_code_and_image():
    """Эталон — только тестовое доказательство: runtime, bootstrap и образ его не видят."""
    ozon = TESTS.parent
    for part in ("runtime", "bootstrap"):
        for f in (ozon / part).rglob("*"):
            if f.is_file() and f.suffix in (".py", ".txt", "") or f.name == "Dockerfile":
                text = f.read_text(encoding="utf-8", errors="ignore")
                assert "baseline_pre_t2" not in text and "tests/compat" not in text, f
    dockerfile = (ozon / "runtime" / "Dockerfile").read_text(encoding="utf-8")
    copies = [line.split()[1:-1] for line in dockerfile.splitlines() if line.upper().startswith("COPY ")]
    assert copies == [["requirements.txt"], ["common.py", "entities.py", "main.py", "promo.py", "catalog_identity.py", "backfill.py", "backfill_core.py", "qualification.py", "qualification_resume.json"],
                      # T5: identity/привязка, control plane и политика методов Seller — тоже поимённо
                      ["identity.py", "credentials.py", "lifecycle.py", "lifecycle_core.py", "checkpoints.py",
                       "history.py", "quota.py", "dq.py", "control_store.py", "seller_policy.py", "seller_method_policy.json", "runtime_execution_contract.json"]], \
        "образ копирует файлы поимённо из контекста runtime; иное — пересмотреть изоляцию эталона"


def test_baseline_and_current_share_no_compared_module(traces):
    """Эталон и текущий runtime исполняют каждый СВОЙ код — сравнение не тавтология."""
    for name in COMPARED:
        base = traces[("baseline", name)]["modules"]
        cur = traces[("current", name)]["modules"]
        assert set(base) == set(cur) == {"common", "entities", "promo", "main"}
        for mod in base:
            assert Path(base[mod]).parent == BASELINE.resolve(), (name, mod, base[mod])
            assert Path(cur[mod]).parent == CURRENT.resolve(), (name, mod, cur[mod])


def test_every_required_scenario_is_present():
    required = {"01_seller_success", "02_performance_success", "03_normal_pagination",
                "04_seller_api_error", "05_performance_api_error", "06_page_caps_lenient",
                "07_page_caps_strict", "08_journal_success", "09_journal_failure",
                "10_api_and_journal_failure_with_echo", "11_credential_echo",
                "12_escaped_secret_echo", "13_client_id_as_data"}
    assert required <= set(S.SCENARIOS)
    assert ALLOWED_DIFFERENCES == ("D1_secret_redaction", "D2_journal_failure_rendering")
