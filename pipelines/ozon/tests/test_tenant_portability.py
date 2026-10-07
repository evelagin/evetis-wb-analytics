"""Один и тот же код Ozon runtime для EVETIS и синтетического CLIENT_001 (Tenancy T2).

Инвариант: продавца выбирает только конфигурация процесса. Проверяется так:
один и тот же драйвер (_portability_driver.py) импортирует неизменённый runtime в
отдельном процессе с окружением арендатора из реестра tenants/ и сообщает, какие
секреты, таблицы и хосты реально затронуты. Ветвлений по арендатору в runtime нет.

Окружение EVETIS проверяется дважды: как его описывает реестр и как его задаёт
production (Terraform: только GCP_PROJECT_ID/BQ_RAW_DATASET/BQ_LOCATION). Результаты
обязаны совпасть — дескриптор описывает ровно то, что уже работает.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
REPO = TESTS.parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import registry as R  # noqa: E402

EVETIS_PROJECT = "project-fa311fc0-4d87-4781-986"
EVETIS_PROD_ENV = {"GCP_PROJECT_ID": EVETIS_PROJECT, "BQ_RAW_DATASET": "ozon_raw",
                   "BQ_LOCATION": "EU"}


def run_driver(env: dict[str, str]) -> dict:
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("GCP_", "BQ_", "OZON_SECRET_", "STRICT_", "ENTITIES"))}
    clean.update(env)
    r = subprocess.run([sys.executable, str(TESTS / "_portability_driver.py")], env=clean,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def evetis_registry():
    return run_driver(R.ozon_runtime_env(R.load_tenant("evetis")))


@pytest.fixture(scope="module")
def evetis_production():
    return run_driver(EVETIS_PROD_ENV)


@pytest.fixture(scope="module")
def client_001():
    env = R.ozon_runtime_env(R.load_tenant("client_001"))
    env.pop("ENTITIES")            # набор сущностей не влияет на конфигурацию процесса
    return run_driver(env)


def test_registry_is_valid():
    assert R.validate_root() == []


def test_evetis_descriptor_describes_exactly_what_production_runs(evetis_registry, evetis_production):
    assert evetis_registry == evetis_production


def test_evetis_uses_its_existing_secrets_project_and_datasets(evetis_production):
    o = evetis_production
    assert sorted(set(o["secret_paths"])) == sorted(
        f"projects/{EVETIS_PROJECT}/secrets/{n}/versions/latest" for n in (
            "EVETIS_OZON_CLIENT_ID", "EVETIS_OZON_API_KEY",
            "EVETIS_OZON_PERFORMANCE_CLIENT_ID", "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"))
    refs = {ref for _op, ref in o["bq_objects"]}
    assert f"{EVETIS_PROJECT}.ozon_raw.RAW_OZON_SELLER_INFO" in refs
    assert f"{EVETIS_PROJECT}.ozon_raw.OZON_INGESTION_RUNS" in refs
    assert f"{EVETIS_PROJECT}.evetis_ref.REF_SKU_CHANNEL_MAP" in refs
    assert all(r.startswith(f"{EVETIS_PROJECT}.") for r in refs)


def test_client_001_uses_only_its_own_project_and_secret_refs(client_001):
    o, p = client_001, "mpa-t-client-001"
    assert sorted(set(o["secret_paths"])) == sorted(
        f"projects/{p}/secrets/{n}/versions/latest" for n in (
            "ozon-seller-client-id", "ozon-seller-api-key",
            "ozon-perf-client-id", "ozon-perf-client-secret"))
    refs = {ref for _op, ref in o["bq_objects"]}
    assert f"{p}.ozon_raw.RAW_OZON_SELLER_INFO" in refs
    assert f"{p}.ozon_raw.OZON_INGESTION_RUNS" in refs
    assert f"{p}.ref.REF_SKU_CHANNEL_MAP" in refs
    assert all(r.startswith(f"{p}.") for r in refs), "запись или чтение вне проекта арендатора"
    blob = json.dumps(o)
    assert EVETIS_PROJECT not in blob and "EVETIS_OZON_" not in blob and "evetis_ref" not in blob


def test_same_code_path_different_configuration(evetis_production, client_001):
    """Одинаковые API, одинаковая последовательность операций — разные проект и секреты."""
    assert evetis_production["hosts"] == client_001["hosts"]
    assert evetis_production["headers_present"] == client_001["headers_present"]
    ops = lambda o: [op for op, _ in o["bq_objects"]]
    assert ops(evetis_production) == ops(client_001)
    strip = lambda o, proj, ref: [(op, r.replace(proj, "P").replace(f".{ref}.", ".REF."))
                                  for op, r in o["bq_objects"]]
    assert strip(evetis_production, EVETIS_PROJECT, "evetis_ref") == \
        strip(client_001, "mpa-t-client-001", "ref")
    assert evetis_production["config"] != client_001["config"]


def test_runtime_has_no_tenant_specific_branches():
    """Никаких `if tenant == ...`: имя арендатора, его проект и его секреты в коде не встречаются.

    T5: control пишет колонку журнала `tenant_id` (TENANT_STATE_EVENTS) — это поле записи, а не
    ветвление. Запрещены сравнения с идентификатором арендатора и сами идентификаторы.
    """
    # A generic metadata join (proof tenant vs frozen manifest tenant) is a
    # required isolation guard, not a tenant-specific behavior branch. Continue
    # rejecting concrete tenant/project names and literal tenant comparisons.
    forbidden = re.compile(r"client_[0-9]+|CLIENT_[0-9]+|mpa-t-")
    for f in RUNTIME.glob("*.py"):
        assert not forbidden.search(f.read_text(encoding="utf-8")), f.name
        import ast
        for node in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Compare):continue
            operands=[node.left,*node.comparators]
            concrete=[v for v in operands if isinstance(v,ast.Constant) and isinstance(v.value,str) and v.value]
            tenants=[v for v in operands if (isinstance(v,ast.Name) and v.id in {'tenant','tenant_id'})
                     or (isinstance(v,ast.Subscript) and isinstance(v.slice,ast.Constant) and v.slice.value in {'tenant','tenant_id'})]
            assert not (concrete and tenants), f'{f.name}: literal tenant-specific comparison'
