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


# ------------------------------------------ анонимный датасет результатов (M6 Phase 1) ---
ANON = "_04b400eee6d4a73c33dc1603988f749f3eef88d8"
SA = iam_check.SA


def with_anonymous(ds=ANON, **over):
    """Точная форма живого датасета (API 2026-09-26) и задания, которое его создало."""
    s = fixed()
    s["dataset_roles"][ds] = "OWNER"
    s["datasets_all"] = list(iam_check.READ_DATASETS) + [ds]
    s["datasets_visible"] = list(iam_check.READ_DATASETS)
    meta = {"kind": "bigquery#dataset", "etag": "e", "id": f"{iam_check.PROJECT}:{ds}", "selfLink": "x",
            "datasetReference": {"datasetId": ds, "projectId": iam_check.PROJECT},
            "defaultTableExpirationMs": "86400000", "defaultPartitionExpirationMs": "86400000",
            "access": [{"role": "OWNER", "userByEmail": SA}], "creationTime": "1790358993328",
            "lastModifiedTime": "1790358993328", "location": "EU", "maxTimeTravelHours": "168", "type": "DEFAULT"}
    job = {"user_email": SA, "job_type": "QUERY", "statement_type": "SELECT",
           "creation_time": "2026-09-25T17:56:33.151Z", "end_time": "2026-09-25T17:56:35.513Z"}
    ev = {"meta": meta, "jobs": {"region": "region-eu", "linked": [job], "sa_jobs": [dict(job)]}}
    for path, value in over.items():
        target = ev
        keys = path.split("__")
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    s["anonymous_evidence"] = {ds: ev}
    return s


def test_proven_anonymous_result_dataset_is_not_a_write_grant():
    r = iam_check.evaluate(with_anonymous())
    assert r["status"] == "PASS", r
    assert [e["dataset"] for e in r["anonymous_result_datasets"]] == [ANON]
    ev = r["anonymous_result_datasets"][0]
    assert ev["criteria_unmet"] == [] and ev["birth_job"]["creation_time"] == "2026-09-25T17:56:33.151Z"
    assert not any("createSnapshot" in f for f in r["findings"])


OTHER = "someone@example.iam.gserviceaccount.com"


@pytest.mark.parametrize("criterion,snap", [
    ("A1", lambda: with_anonymous(ds="_04B400EEE6D4A73C33DC1603988F749F3EEF88D8")),
    ("A1", lambda: with_anonymous(ds="_04b400ee")),
    ("A2", lambda: {**with_anonymous(), "datasets_visible": list(iam_check.READ_DATASETS) + [ANON]}),
    ("A2", lambda: {k: v for k, v in with_anonymous().items() if k != "datasets_all"}),
    ("A3", lambda: with_anonymous(meta__access=[{"role": "OWNER", "userByEmail": SA},
                                               {"role": "READER", "userByEmail": OTHER}])),
    ("A3", lambda: with_anonymous(meta__access=[{"role": "OWNER", "userByEmail": OTHER}])),
    ("A3", lambda: with_anonymous(meta__access=[{"role": "OWNER", "groupByEmail": "g@example.com"}])),
    ("A4", lambda: with_anonymous(meta__labels={"env": "prod"})),
    ("A4", lambda: with_anonymous(meta__description="production data")),
    ("A4", lambda: with_anonymous(meta__type="LINKED")),
    ("A5", lambda: with_anonymous(meta__defaultTableExpirationMs=None)),
    ("A5", lambda: with_anonymous(meta__defaultPartitionExpirationMs="3600000")),
    ("A6", lambda: with_anonymous(jobs__linked=[])),
    ("A6", lambda: with_anonymous(jobs__linked=[{"user_email": OTHER, "job_type": "QUERY", "statement_type": "SELECT",
                                                 "creation_time": "2026-09-25T17:56:33.151Z",
                                                 "end_time": "2026-09-25T17:56:35.513Z"}])),
    ("A6", lambda: with_anonymous(jobs__linked=[{"user_email": SA, "job_type": "QUERY",
                                                 "statement_type": "CREATE_TABLE_AS_SELECT",
                                                 "creation_time": "2026-09-25T17:56:33.151Z",
                                                 "end_time": "2026-09-25T17:56:35.513Z"}])),
    ("A7", lambda: with_anonymous(meta__creationTime="1790000000000")),         # создан задолго до задания
    ("A7", lambda: with_anonymous(meta__creationTime="1790359999999")),         # создан после задания
    ("A8", lambda: with_anonymous(jobs__sa_jobs=[{"user_email": SA, "job_type": "QUERY", "statement_type": "INSERT"}])),
    ("A8", lambda: with_anonymous(jobs__sa_jobs=[{"user_email": SA, "job_type": "LOAD", "statement_type": None}])),
    ("A8", lambda: with_anonymous(jobs__sa_jobs=[])),
    ("A9", lambda: with_anonymous(jobs__region="region-us")),
    ("A9", lambda: with_anonymous(meta__location="US")),
])
def test_every_unproven_criterion_keeps_the_old_fail(criterion, snap):
    r = iam_check.evaluate(snap())
    assert r["status"] == "FAIL"
    assert r["anonymous_result_datasets"] == []
    assert any("допустим только READER" in f for f in r["findings"])
    unmet = next(iter(r["anonymous_not_proven"].values()), [])
    assert any(u.startswith(criterion) for u in unmet) or criterion == "A1", unmet


def test_normal_dataset_owner_remains_fail():
    s = fixed()
    s["dataset_roles"]["wb_mart"] = "OWNER"
    r = iam_check.evaluate(s)
    assert r["status"] == "FAIL" and any("wb_mart: роль OWNER" in f for f in r["findings"])


def test_script_dataset_is_not_auto_ignored():
    ds = "_script0034f00" + "a" * 26
    r = iam_check.evaluate(with_anonymous(ds=ds))
    assert r["status"] == "FAIL" and r["anonymous_result_datasets"] == []


def test_snapshot_without_evidence_is_fail_closed():
    s = with_anonymous()
    del s["anonymous_evidence"]
    assert iam_check.evaluate(s)["status"] == "FAIL"


def test_anonymous_writer_role_is_not_accepted():
    s = with_anonymous()
    s["dataset_roles"][ANON] = "WRITER"
    assert iam_check.evaluate(s)["status"] == "FAIL"
