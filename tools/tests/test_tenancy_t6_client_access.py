"""Tenancy T6 — клиентский доступ к analytics_share: контракт, изоляция по реальным зависимостям вью.

Офлайн. Реальная клиентская идентичность в Git не хранится: фикстура — синтетические principals.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import client_access as CA  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402

TF = REPO / "infra" / "tenant"


def doc(mode="ANALYTICS_SHARE_VIEWER", principals=("user:analyst@example-client.test",)):
    return {"bi_access": {"mode": mode, "principals": list(principals)}}


DATASETS = {"ozon_raw": "ozon_raw", "ref": "ref", "ozon_mart": "ozon_mart", "tenant_ops": "tenant_ops",
            "analytics_share": "analytics_share", "tenant_locks": "tenant_locks"}


# ═══════════════════════════════════════ изоляция по РЕАЛЬНЫМ зависимостям SQL
def test_authorized_edges_equal_the_real_view_dependencies_of_the_package():
    assert CA.dependency_edges() == set(CA.AUTHORIZED_VIEW_EDGES)


def test_client_views_read_only_mart_and_ops_never_raw_ref_or_locks():
    sources = {s for v, s in CA.dependency_edges() if v == "analytics_share"}
    assert sources == {"ozon_mart", "tenant_ops"}
    for sql in (CA.SQL_PACKAGE / "analytics_share").glob("*.sql"):
        text = sql.read_text(encoding="utf-8")
        assert ".ozon_raw." not in text and ".tenant_locks." not in text and "`ref." not in text and ".ref." not in text, sql.name


def test_no_authorized_dataset_targets_analytics_share_or_tenant_locks():
    assert all(s not in ("analytics_share", "tenant_locks") for _, s in CA.AUTHORIZED_VIEW_EDGES)
    assert all(v != "tenant_locks" for v, _ in CA.AUTHORIZED_VIEW_EDGES)


def test_client_facing_views_are_the_eleven_published_ones():
    assert CA.client_facing_views() == ["advertising_daily", "data_coverage", "data_freshness", "inventory_cover", "inventory_current",
                                        "orders", "price_history", "profitability_daily", "sales_daily", "sku_daily", "store_costs_daily"]


# ═══════════════════════════════════════ контракт
def test_mode_none_yields_no_client_access_block():
    assert CA.contract_block(doc(mode="NONE", principals=()), DATASETS) is None
    assert R.terraform_inputs("client_001")["client_access"] is None            # живой реестр: доступ не выдан


def test_viewer_mode_renders_reader_job_user_and_exact_edges():
    block = CA.contract_block(doc(principals=("group:bi@example-client.test", "user:analyst@example-client.test")), DATASETS)
    assert block["mode"] == "ANALYTICS_SHARE_VIEWER" and block["dataset_key"] == "analytics_share"
    assert block["principals"] == [{"kind": "group", "email": "bi@example-client.test"}, {"kind": "user", "email": "analyst@example-client.test"}]
    assert block["query_role"] == {"role_id": "vtsClientQueryRunner",
                                   "permissions": ["bigquery.config.get", "bigquery.jobs.create", "resourcemanager.projects.get"]}
    assert "project_roles" not in block
    assert {(e["viewer_key"], e["source_key"]) for e in block["authorized_views"]} == set(CA.AUTHORIZED_VIEW_EDGES)


@pytest.mark.parametrize("principal", ["serviceAccount:x@mpa-t-client-001.iam.gserviceaccount.com", "user:sa@mpa-platform.iam.gserviceaccount.com",
                                       "domain:example-client.test", "allUsers", "user:evelagin@google.com", "user:x@y", "User:analyst@example-client.test"])
def test_non_human_or_malformed_principals_are_rejected(principal):
    with pytest.raises(CA.ClientAccessError):
        CA.contract_block(doc(principals=(principal,)), DATASETS)


def test_duplicate_and_empty_principals_are_rejected():
    with pytest.raises(CA.ClientAccessError):
        CA.contract_block(doc(principals=("user:a@example-client.test", "user:A@example-client.test")), DATASETS)
    with pytest.raises(CA.ClientAccessError):
        CA.contract_block(doc(principals=()), DATASETS)


def test_missing_internal_dataset_is_rejected():
    ds = copy.deepcopy(DATASETS); ds.pop("tenant_ops")
    with pytest.raises(CA.ClientAccessError):
        CA.contract_block(doc(), ds)


# ═══════════════════════════════════════ фикстуры и Terraform
def test_client_access_fixture_is_current_registry_output():
    path = SY.FIXTURE_DIR / "client_access.synthetic.contract.json"
    assert path.read_text(encoding="utf-8") == SY.client_access_fixture_text()
    c = json.loads(path.read_text(encoding="utf-8"))
    assert c["client_access"]["principals"] == [{"email": "bi-readers@example-client.test", "kind": "group"}, {"email": "analyst@example-client.test", "kind": "user"}]
    assert all(".test" in p["email"] for p in c["client_access"]["principals"])     # никакой реальной идентичности в Git


def test_terraform_exposes_client_access_only_through_reader_jobuser_and_authorized_views():
    main = (TF / "main.tf").read_text(encoding="utf-8"); ca = (TF / "client-access.tf").read_text(encoding="utf-8")
    assert 'role          = "READER"' in main and "group_by_email = access.value.email" in main and 'target_types = ["VIEWS"]' in main
    assert "google_project_iam_member" in ca and "google_project_iam_custom_role" in ca and "query_role" in ca
    assert '"roles/bigquery.jobUser"' not in ca
    for tf in TF.glob("*.tf"):
        text = tf.read_text(encoding="utf-8")
        assert "google_bigquery_dataset_iam" not in text and "google_project_iam_binding" not in text, tf.name   # только аддитивные member-привязки
    tests = (TF / "tests" / "client_access.tftest.hcl").read_text(encoding="utf-8")
    assert "client_access.synthetic.contract.json" in tests and "client_001.contract.json" in tests


# ═══════════════════════════════════════ роль запуска запросов: не jobUser
# Живой состав roles/bigquery.jobUser на 2026-10-25 (gcloud iam roles describe, только чтение).
JOB_USER_2026_10_25 = {"bigquery.config.get", "bigquery.jobs.create", "cloudaicompanion.topics.create", "dataform.folders.create",
                       "dataform.locations.get", "dataform.locations.list", "dataform.repositories.create",
                       "dataform.repositories.list", "geminidataanalytics.locations.chat", "resourcemanager.projects.get",
                       "resourcemanager.projects.list"}


def test_client_query_role_creates_nothing_and_reads_no_data():
    perms = set(CA.QUERY_ROLE_PERMISSIONS)
    assert perms == {"bigquery.jobs.create", "bigquery.config.get", "resourcemanager.projects.get"}
    assert perms < JOB_USER_2026_10_25
    creating = {p for p in JOB_USER_2026_10_25 if p.endswith(".create") and p != "bigquery.jobs.create"} | {"geminidataanalytics.locations.chat"}
    assert creating and not (perms & creating)
    assert not any(p.startswith(("bigquery.tables.", "bigquery.datasets.", "bigquery.readsessions.")) for p in perms)


# ═══════════════════════════════════════ сканер плана релиза принимает ровно форму T6
def _client_plan():
    from tools.tests.test_tenancy_t32_remediation import _plan_for, _rc
    c = SY.client_access_contract()
    plan = _plan_for(c)
    p, ca = c["project_id"], c["client_access"]
    for rc in plan["resource_changes"]:
        if rc["type"] != "google_bigquery_dataset":
            continue
        key = next(k for k, v in c["datasets"].items() if v == rc["change"]["after"]["dataset_id"])
        acc = rc["change"]["after"]["access"]
        if key == ca["dataset_key"]:
            acc += [{"role": "READER", f"{x['kind']}_by_email": x["email"]} for x in ca["principals"]]
        acc += [{"role": "", "dataset": [{"dataset": [{"project_id": p, "dataset_id": c["datasets"][e["viewer_key"]]}],
                                          "target_types": ["VIEWS"]}]}
                for e in ca["authorized_views"] if e["source_key"] == key]
    plan["resource_changes"].append(_rc("google_project_iam_custom_role.client_query[0]", "google_project_iam_custom_role",
                                        {"project": p, "role_id": "vtsClientQueryRunner", "stage": "GA",
                                         "permissions": list(ca["query_role"]["permissions"])}, {"name": True, "id": True}))
    for x in ca["principals"]:
        m = f"{x['kind']}:{x['email']}"
        plan["resource_changes"].append(_rc(f'google_project_iam_member.client_access["{m}"]', "google_project_iam_member",
                                            {"project": p, "role": f"projects/{p}/roles/vtsClientQueryRunner", "member": m}, {"etag": True}))
    return c, plan


def test_plan_scan_accepts_the_exact_client_access_plan():
    from tools.tenancy import plan_scan as PS
    c, plan = _client_plan()
    assert PS.scan_plan(plan, c) == []


def _mutate(kind):
    c, plan = _client_plan()
    ds = {rc["change"]["after"]["dataset_id"]: rc for rc in plan["resource_changes"] if rc["type"] == "google_bigquery_dataset"}
    members = [rc for rc in plan["resource_changes"] if rc["address"].startswith("google_project_iam_member.client_access")]
    if kind == "jobUser instead of the query role":
        members[0]["change"]["after"]["role"] = "roles/bigquery.jobUser"
    elif kind == "client READER on ozon_mart":
        ds["ozon_mart"]["change"]["after"]["access"].append({"role": "READER", "user_by_email": "analyst@example-client.test"})
    elif kind == "authorized dataset for TABLES":
        a = next(a for a in ds["ozon_raw"]["change"]["after"]["access"] if a.get("dataset"))
        a["dataset"][0]["target_types"] = ["VIEWS", "TABLES"]
    elif kind == "extra authorized edge analytics_share -> ozon_raw":
        ds["ozon_raw"]["change"]["after"]["access"].append({"dataset": [{"dataset": [{"project_id": c["project_id"], "dataset_id": "analytics_share"}], "target_types": ["VIEWS"]}]})
    elif kind == "authorized view (not dataset)":
        ds["ozon_mart"]["change"]["after"]["access"].append({"view": [{"project_id": c["project_id"], "dataset_id": "analytics_share", "table_id": "orders"}]})
    elif kind == "query role widened":
        rc = next(rc for rc in plan["resource_changes"] if rc["type"] == "google_project_iam_custom_role")
        rc["change"]["after"]["permissions"].append("dataform.repositories.create")
    elif kind == "foreign principal on project":
        members[0]["change"]["after"]["member"] = "user:someone@else.test"
    elif kind == "contract edge drift":
        c["client_access"]["authorized_views"].append({"viewer_key": "analytics_share", "source_key": "ozon_raw"})
    return c, plan


@pytest.mark.parametrize("kind", ["jobUser instead of the query role", "client READER on ozon_mart", "authorized dataset for TABLES",
                                  "extra authorized edge analytics_share -> ozon_raw", "authorized view (not dataset)",
                                  "query role widened", "foreign principal on project", "contract edge drift"])
def test_plan_scan_rejects_any_widening_of_client_access(kind):
    from tools.tenancy import plan_scan as PS
    c, plan = _mutate(kind)
    assert PS.scan_plan(plan, c), kind


def test_plan_scan_rejects_client_resources_when_registry_grants_nothing():
    from tools.tenancy import plan_scan as PS
    _c, plan = _client_plan()
    c0 = SY.client_access_contract(); c0["client_access"] = None
    findings = PS.scan_plan(plan, c0)
    assert any("analytics_share" in f or "лишняя запись" in f for f in findings)
    assert any("client_access" in f or "не входит в разрешённые" in f for f in findings)
