"""M3 — sa-ae-reader read-only по ЭФФЕКТИВНЫМ правам, а не по названиям ролей.

Снимок — M3 в том виде, в каком он был применён 2026-09-24 и откатан: roles/bigquery.jobUser
оказался с правами создания ресурсов Dataform и чатом Gemini. Проверка обязана это ловить, а
исправленная конфигурация (пользовательская роль aeBigQueryJobRunner) — проходить."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tools.autonomy import iam_check

REPO = Path(__file__).resolve().parents[2]
SNAP = json.loads((REPO / "docs/architecture/ae_evidence/ae_reader_m3_2026-09-24/m3_as_applied_iam_snapshot.json")
                  .read_text(encoding="utf-8"))
CUSTOM = "projects/project-fa311fc0-4d87-4781-986/roles/aeBigQueryJobRunner"


def fixed(snap=SNAP):
    s = copy.deepcopy(snap)
    s["project_roles"] = [r for r in s["project_roles"] if r != "roles/bigquery.jobUser"] + [CUSTOM]
    tf = (REPO / "infra/terraform/autonomy.tf").read_text()
    perms = re.findall(r'"([a-z.]+)"', re.search(r'"ae_job_runner" \{.*?permissions = \[(.*?)\]', tf, re.S).group(1))
    s["role_permissions"][CUSTOM] = perms
    return s


def test_as_applied_m3_is_not_read_only():
    r = iam_check.evaluate(SNAP)
    assert r["status"] == "FAIL"
    assert any("dataform.repositories.create" in f for f in r["findings"])
    assert any("geminidataanalytics" in f for f in r["findings"])


def test_fixed_design_is_read_only_and_reads_everything_needed():
    r = iam_check.evaluate(fixed())
    assert r["status"] == "PASS", r
    assert r["missing_read"] == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: s["dataset_roles"].__setitem__("wb_mart", "WRITER"), "WRITER"),
    (lambda s: s["project_roles"].append("roles/bigquery.dataEditor") or
     s["role_permissions"].__setitem__("roles/bigquery.dataEditor", ["bigquery.tables.updateData"]), "updateData"),
    (lambda s: s["referenced_by_other_sa"].append("sa-deployer@x"), "других SA"),
    (lambda s: s["dataset_roles"].__setitem__("secret_ds", "READER"), "вне утверждённых"),
    (lambda s: s["project_roles"].append("roles/iam.serviceAccountTokenCreator") or
     s["role_permissions"].__setitem__("roles/iam.serviceAccountTokenCreator", ["iam.serviceAccounts.getAccessToken"]),
     "getAccessToken"),
])
def test_any_extra_privilege_fails(mutate, needle):
    s = fixed()
    mutate(s)
    r = iam_check.evaluate(s)
    assert r["status"] == "FAIL" and any(needle in f for f in r["findings"]), r


def test_missing_read_access_is_reported_not_silently_passed():
    s = fixed()
    del s["dataset_roles"]["ozon_raw"]
    r = iam_check.evaluate(s)
    assert r["status"] == "FAIL" and any("ozon_raw" in m for m in r["missing_read"])


def test_inert_permissions_become_findings_once_any_write_exists():
    s = fixed()
    s["dataset_roles"]["wb_ops"] = "WRITER"
    assert any("createSnapshot" in f for f in iam_check.evaluate(s)["findings"])


def test_absent_sa_is_not_a_pass():
    s = fixed()
    s["sa_exists"] = False
    assert iam_check.evaluate(s)["status"] == "FAIL"
