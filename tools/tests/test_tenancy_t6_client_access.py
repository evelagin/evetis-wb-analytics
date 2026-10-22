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


def test_client_facing_views_are_the_nine_published_ones():
    assert CA.client_facing_views() == ["advertising_daily", "data_coverage", "inventory_current", "orders", "price_history",
                                        "profitability_daily", "sales_daily", "sku_daily", "store_costs_daily"]


# ═══════════════════════════════════════ контракт
def test_mode_none_yields_no_client_access_block():
    assert CA.contract_block(doc(mode="NONE", principals=()), DATASETS) is None
    assert R.terraform_inputs("client_001")["client_access"] is None            # живой реестр: доступ не выдан


def test_viewer_mode_renders_reader_job_user_and_exact_edges():
    block = CA.contract_block(doc(principals=("group:bi@example-client.test", "user:analyst@example-client.test")), DATASETS)
    assert block["mode"] == "ANALYTICS_SHARE_VIEWER" and block["dataset_key"] == "analytics_share"
    assert block["principals"] == [{"kind": "group", "email": "bi@example-client.test"}, {"kind": "user", "email": "analyst@example-client.test"}]
    assert block["project_roles"] == ["roles/bigquery.jobUser"]
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
    assert "google_project_iam_member" in ca and "client_access.project_roles" in ca
    for tf in TF.glob("*.tf"):
        text = tf.read_text(encoding="utf-8")
        assert "google_bigquery_dataset_iam" not in text and "google_project_iam_binding" not in text, tf.name   # только аддитивные member-привязки
    tests = (TF / "tests" / "client_access.tftest.hcl").read_text(encoding="utf-8")
    assert "client_access.synthetic.contract.json" in tests and "client_001.contract.json" in tests
