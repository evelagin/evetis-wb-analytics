"""Tenancy T3.2 — только что созданный арендатор: контейнеры секретов без версий.

Окружение каждого job'а берётся из контракта реестра (registry.py terraform-inputs),
а не пишется руками. Ручной запуск job'а до внесения ключей обязан:
  * упасть (ненулевой код выхода, сущности FAILED в журнале арендатора);
  * обращаться только к секретам своего проекта и только по именам из реестра;
  * не сделать ни одного HTTP-вызова к Ozon без учётных данных;
  * не упомянуть EVETIS нигде (проект, датасеты, имена секретов).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[2]
EVETIS = ("project-fa311fc0-4d87-4781-986", "EVETIS_OZON_", "evetis_ref")


def _contract():
    r = subprocess.run([sys.executable, str(REPO / "tools/tenancy/registry.py"), "terraform-inputs",
                        "client_001"], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


CONTRACT = _contract()
JOBS = CONTRACT["marketplaces"]["ozon"]["jobs"]


def _run(env_job):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("OZON_", "BQ_")) or k in ("GCP_PROJECT_ID", "ENTITIES", "STRICT_PAGE_CAPS"))}
    env.update(env_job)
    r = subprocess.run([sys.executable, str(TESTS / "_empty_secret_driver.py")], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr


@pytest.mark.parametrize("job", sorted(JOBS))
def test_new_tenant_job_is_rejected_by_binding_before_secrets_and_api(job):
    """T5: job арендатора (TENANT_BINDING_REQUIRED=1) без подтверждённой владельцем привязки
    отказывает первым шагом — листает только знаки владельца в ref своего проекта; секретов, HTTP и
    записи нет."""
    assert JOBS[job]["env"]["TENANT_BINDING_REQUIRED"] == "1"
    seen, stderr = _run(JOBS[job]["env"])
    project = CONTRACT["project_id"]
    assert seen["exit"] == 3
    assert seen["http"] == [] and seen["secret_paths"] == []
    assert seen["bq"] == [["list_tables", f"{project}.ref"]]
    rejected = [l for l in seen["log"] if l.get("event") == "run_rejected"]
    assert rejected and rejected[0]["binding"] == "seller:UNBOUND"
    blob = json.dumps(seen) + stderr
    for m in EVETIS:
        assert m not in blob, m


@pytest.mark.parametrize("job", sorted(JOBS))
def test_job_without_secret_versions_fails_closed_inside_its_own_project(job):
    # Historical dev-trial omission now rejects before secrets, HTTP or BigQuery.
    seen, stderr = _run({k: v for k, v in JOBS[job]["env"].items() if k != "TENANT_BINDING_REQUIRED"})
    assert seen["exit"] == 2
    assert seen["http"] == seen["secret_paths"] == seen["bq"] == []
    assert seen["config"]["project"] == CONTRACT["project_id"]
    assert any(l.get("event") == "run_rejected" for l in seen["log"])


def test_missing_project_env_still_refuses_to_start():
    env = dict(JOBS["ozon-runtime-daily"]["env"])
    env.pop("GCP_PROJECT_ID")
    clean = {k: v for k, v in os.environ.items() if k != "GCP_PROJECT_ID"}
    clean.update(env)
    r = subprocess.run([sys.executable, str(TESTS / "_empty_secret_driver.py")], env=clean,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "GCP_PROJECT_ID" in r.stderr
