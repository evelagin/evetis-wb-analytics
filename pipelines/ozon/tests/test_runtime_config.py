"""Конфигурация Ozon runtime (Tenancy T2): EVETIS без изменений, проект — только явно.

Ожидаемые значения EVETIS ниже — это ЛИТЕРАЛЫ, которые common.py содержал до T2
(origin/main dc7fda4): проект из GCP_PROJECT_ID, датасет ozon_raw, локация EU,
имена секретов EVETIS_OZON_*, справочник evetis_ref (promo.py). Тест доказывает,
что production-окружение EVETIS разрешается ровно в них.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import common as C

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"

# Окружение production-job'ов EVETIS: infra/terraform/ozon_ingestion.tf (ozon_common_env
# + ENTITIES) и gcloud run jobs describe 2026-09-24. OZON_SECRET_*, BQ_REF_DATASET и
# STRICT_PAGE_CAPS там не заданы.
EVETIS_PROD_ENV = {
    "GCP_PROJECT_ID": "project-fa311fc0-4d87-4781-986",
    "BQ_RAW_DATASET": "ozon_raw",
    "BQ_LOCATION": "EU",
    "ENTITIES": "catalog,prices,seller_info,finance_accrual,ads_campaigns,"
                "ads_expense_daily,ads_sku_daily,supplies",
}
EVETIS_BEFORE_T2 = C.RuntimeConfig(
    project="project-fa311fc0-4d87-4781-986", raw_dataset="ozon_raw",
    ref_dataset="evetis_ref", location="EU",
    secret_seller_client_id="EVETIS_OZON_CLIENT_ID",
    secret_seller_api_key="EVETIS_OZON_API_KEY",
    secret_perf_client_id="EVETIS_OZON_PERFORMANCE_CLIENT_ID",
    secret_perf_client_secret="EVETIS_OZON_PERFORMANCE_CLIENT_SECRET",
    strict_page_caps=False)


def test_evetis_production_env_resolves_to_pre_t2_configuration():
    assert C.resolve_config(EVETIS_PROD_ENV) == EVETIS_BEFORE_T2


@pytest.mark.parametrize("job_entities", [
    "stocks,fbo_postings", "clusters", "promo",
    "catalog,prices,seller_info,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies",
])
def test_every_evetis_job_env_resolves_identically(job_entities):
    """Четыре job'а EVETIS отличаются только ENTITIES — конфигурацию он не трогает."""
    assert C.resolve_config(dict(EVETIS_PROD_ENV, ENTITIES=job_entities)) == EVETIS_BEFORE_T2


def test_missing_project_fails_closed():
    env = {k: v for k, v in EVETIS_PROD_ENV.items() if k != "GCP_PROJECT_ID"}
    with pytest.raises(C.ConfigError, match="GCP_PROJECT_ID не задан"):
        C.resolve_config(env)


@pytest.mark.parametrize("bad", ["", "   ", "Project-X", "ab", "project_with_underscore",
                                 "ends-with-dash-", "x" * 31])
def test_invalid_project_fails_closed(bad):
    with pytest.raises(C.ConfigError):
        C.resolve_config(dict(EVETIS_PROD_ENV, GCP_PROJECT_ID=bad))


def test_secret_references_come_from_env_with_legacy_defaults():
    cfg = C.resolve_config(dict(EVETIS_PROD_ENV,
                                OZON_SECRET_SELLER_CLIENT_ID="ozon-seller-client-id",
                                OZON_SECRET_SELLER_API_KEY="ozon-seller-api-key"))
    assert cfg.secret_seller_client_id == "ozon-seller-client-id"
    assert cfg.secret_seller_api_key == "ozon-seller-api-key"
    # не заданные явно — прежние имена EVETIS
    assert cfg.secret_perf_client_id == "EVETIS_OZON_PERFORMANCE_CLIENT_ID"
    assert cfg.secret_perf_client_secret == "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"


@pytest.mark.parametrize("bad", ["tenant/ozon/api_key", "has space", "a.b", "x" * 256, ""])
def test_invalid_secret_reference_fails_without_echoing_value(bad):
    with pytest.raises(C.ConfigError) as e:
        C.resolve_config(dict(EVETIS_PROD_ENV, OZON_SECRET_SELLER_API_KEY=bad))
    if bad.strip():
        assert bad not in str(e.value), "значение (возможно, сам ключ) попало в сообщение"


def test_ref_dataset_is_configurable():
    assert C.resolve_config(dict(EVETIS_PROD_ENV, BQ_REF_DATASET="ref")).ref_dataset == "ref"
    with pytest.raises(C.ConfigError):
        C.resolve_config(dict(EVETIS_PROD_ENV, BQ_REF_DATASET="bad-name"))


@pytest.mark.parametrize("raw,expected", [(None, False), ("0", False), ("1", True)])
def test_strict_page_caps_values(raw, expected):
    env = dict(EVETIS_PROD_ENV)
    if raw is not None:
        env["STRICT_PAGE_CAPS"] = raw
    assert C.resolve_config(env).strict_page_caps is expected


@pytest.mark.parametrize("bad", ["true", "yes", "2", ""])
def test_strict_page_caps_rejects_ambiguous_values(bad):
    with pytest.raises(C.ConfigError):
        C.resolve_config(dict(EVETIS_PROD_ENV, STRICT_PAGE_CAPS=bad))


# ─────────────────────────────── процесс не стартует без проекта
_STUBS = (
    "import sys, types\n"
    "g = types.ModuleType('google'); c = types.ModuleType('google.cloud')\n"
    "b = types.ModuleType('google.cloud.bigquery'); s = types.ModuleType('google.cloud.secretmanager')\n"
    "c.bigquery, c.secretmanager, g.cloud = b, s, c\n"
    "sys.modules.update({'google': g, 'google.cloud': c, 'google.cloud.bigquery': b,"
    " 'google.cloud.secretmanager': s})\n"
)


def _import_common(env):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("GCP_", "BQ_", "OZON_SECRET_", "STRICT_"))}
    clean.update(env)
    code = _STUBS + f"sys.path.insert(0, {str(RUNTIME)!r})\nimport common\nprint(common.PROJECT)\n"
    return subprocess.run([sys.executable, "-c", code], env=clean,
                          capture_output=True, text=True, timeout=60)


def test_module_import_without_project_fails_closed():
    r = _import_common({})
    assert r.returncode != 0
    assert "GCP_PROJECT_ID не задан" in r.stderr
    assert "project-fa311fc0" not in r.stdout, "неявный проект EVETIS всё ещё подставляется"


def test_module_import_with_explicit_project_uses_it():
    r = _import_common({"GCP_PROJECT_ID": "mpa-t-client-001"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "mpa-t-client-001"


def test_bootstrap_loader_has_no_implicit_project():
    src = (RUNTIME.parent / "bootstrap" / "loader.py").read_text(encoding="utf-8")
    assert "project-fa311fc0" not in src
    assert 'os.environ.get("GCP_PROJECT_ID", "").strip()' in src


def test_no_runtime_source_embeds_a_project_id():
    for f in RUNTIME.glob("*.py"):
        assert "project-fa311fc0" not in f.read_text(encoding="utf-8"), f.name
