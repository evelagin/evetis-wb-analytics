"""Tenancy T4-a — контракт данных арендатора: датасеты, политика истории, таблицы платформы.

Офлайн. Проверяет контракт реестра → Terraform, а не живые ресурсы (их создаёт отдельное
применение по ACK владельца).
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import ozon_contract as OC  # noqa: E402
from tools.tenancy import plan_scan as PS  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import validation as V  # noqa: E402

CLIENT = REPO / "tenants" / "client_001" / "tenant.json"
# T5: + tenant_locks (аренда отрезков бэкфилла control plane; таблиц контракта в нём нет).
T4_DATASETS = {"ozon_raw", "ref", "ozon_mart", "tenant_ops", "analytics_share", "tenant_locks"}


def _doc():
    return V.load_tenant_document(CLIENT)


def _rules(doc):
    return {f.rule for f in V.validate_tenant(doc, "client_001/tenant.json")}


# ═══════════════════════════════════════ датасеты
def test_dedicated_tenant_has_exactly_the_t4_datasets_and_evetis_is_unchanged():
    assert set(N.expected_datasets("client_001")) == T4_DATASETS
    assert all(k == v for k, v in N.expected_datasets("client_001").items())
    assert N.expected_datasets(N.LEGACY_TENANT_ID) == {"ozon_raw": "ozon_raw", "ref": "evetis_ref"}


@pytest.mark.parametrize("change", [
    lambda d: d["data_boundary"]["datasets"].pop("tenant_ops"),
    lambda d: d["data_boundary"]["datasets"].update({"analytics_share": "share"}),
    lambda d: d["data_boundary"]["datasets"].update({"ozon_mart": "evetis_mart"}),
])
def test_dataset_set_is_fixed_by_naming(change):
    doc = _doc()
    change(doc)
    assert _rules(doc)


def test_unknown_dataset_key_is_rejected_by_schema():
    doc = _doc()
    doc["data_boundary"]["datasets"]["customer_share"] = "customer_share"
    assert "schema" in _rules(doc)


# ═══════════════════════════════════════ политика истории
def test_client_001_requests_earliest_available_history_not_a_hardcoded_date():
    oz = _doc()["marketplaces"]["ozon"]
    assert oz["history_request"] == {"mode": "EARLIEST_AVAILABLE"}
    assert "backfill_start_date" not in oz


@pytest.mark.parametrize("hr,ok", [
    ({"mode": "EARLIEST_AVAILABLE"}, True),
    ({"mode": "FROM_DATE", "from_date": "2024-01-01"}, True),
    ({"mode": "EARLIEST_AVAILABLE", "from_date": "2024-01-01"}, False),
    ({"mode": "FROM_DATE"}, False),
    ({"mode": "FROM_DATE", "from_date": "2024-02-30"}, False),
    ({"mode": "ALL"}, False),
    ({"mode": "EARLIEST_AVAILABLE", "available_from": "2024-01-01"}, False),
])
def test_history_request_rules(hr, ok):
    doc = _doc()
    doc["marketplaces"]["ozon"]["history_request"] = hr
    assert (not _rules(doc)) is ok, hr


def test_history_request_is_mandatory_when_ozon_is_enabled():
    doc = _doc()
    del doc["marketplaces"]["ozon"]["history_request"]
    assert "history" in _rules(doc)


def test_legacy_backfill_start_date_is_gone_from_the_contract():
    doc = _doc()
    doc["marketplaces"]["ozon"]["backfill_start_date"] = "2026-01-01"
    assert "schema" in _rules(doc)


def test_evetis_cannot_declare_a_history_request():
    doc = V.load_tenant_document(REPO / "tenants" / "evetis" / "tenant.json")
    doc["marketplaces"]["ozon"]["history_request"] = {"mode": "EARLIEST_AVAILABLE"}
    assert "evetis_protected" in {f.rule for f in V.validate_tenant(doc, "evetis/tenant.json")}


# ═══════════════════════════════════════ identity продавца не хранится в Git
IDENTITY_KEYS = ("inn", "ogrn", "company_name", "seller_id", "expected_client_id", "client_id_value",
                 "seller_identity", "legal_entity_id")


def test_descriptor_schema_has_no_place_for_marketplace_account_identity():
    schema = json.loads((REPO / "tenants" / "_schema" / "tenant.schema.json").read_text(encoding="utf-8"))
    keys = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in (o.get("properties") or {}).items():
                keys.add(k)
                walk(v)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(schema)
    assert not keys & set(IDENTITY_KEYS), keys & set(IDENTITY_KEYS)


@pytest.mark.parametrize("key", ["inn", "seller_identity", "expected_client_id"])
def test_identity_cannot_be_smuggled_into_the_descriptor(key):
    doc = _doc()
    doc["marketplaces"]["ozon"][key] = "7700000000"
    assert "schema" in _rules(doc)


def _assert_no_account_identifiers(doc):
    # A registered immutable Git SHA may contain a decimal substring. Exempt
    # only this exact schema field after verifying the release provenance;
    # identical numbers in any business/free-text field must still fail.
    assert not V.validate_tenant(doc, "client_001/tenant.json")
    scan = copy.deepcopy(doc)
    profile = scan.get("historical_orchestration")
    if profile is not None:
        sha = profile["release"]
        assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
        release = json.loads((REPO / "infra/tenant/releases/backfill" / (sha + ".json")).read_text())
        assert release["source_sha"] == sha
        profile["release"] = "REGISTERED_IMMUTABLE_SOURCE"
    text = json.dumps(scan, ensure_ascii=False)
    assert not re.search(r"(?<![0-9])[0-9]{10}(?:[0-9]{2}|[0-9]{3})?(?![0-9])", text)


def test_client_001_descriptor_carries_no_inn_like_or_client_id_like_numbers():
    _assert_no_account_identifiers(_doc())


@pytest.mark.parametrize("identifier", ["7700000000", "770000000000", "7700000000000"])
def test_numeric_identifier_in_free_text_remains_forbidden(identifier):
    # Synthetic identifiers, never a live account value.
    doc = _doc()
    doc["legal_name"] += " " + identifier
    with pytest.raises(AssertionError):
        _assert_no_account_identifiers(doc)


def test_source_hash_numeric_substring_is_not_exempt_in_free_text():
    doc = _doc()
    doc["legal_name"] += " synthetic 6108982142"
    with pytest.raises(AssertionError):
        _assert_no_account_identifiers(doc)


@pytest.mark.parametrize("source", ["7700000000", "f" * 39, "../" + "f" * 40, "f" * 40])
def test_unregistered_or_invalid_source_cannot_hide_identity(source):
    doc = _doc()
    doc["historical_orchestration"]["release"] = source
    with pytest.raises((AssertionError, FileNotFoundError)):
        _assert_no_account_identifiers(doc)


# ═══════════════════════════════════════ таблицы платформы
def test_platform_tables_exist_for_every_tenant_regardless_of_entities():
    t = OC.tables_for([])
    assert set(t["tenant_ops"]) == set(OC.PLATFORM_TABLES["tenant_ops"])
    assert set(t["ref"]) == set(OC.PLATFORM_TABLES["ref"])


@pytest.mark.parametrize("ds,table", [(ds, t) for ds, names in OC.PLATFORM_TABLES.items() for t in names])
def test_platform_table_schemas_are_present_and_project_neutral(ds, table):
    spec = OC.load_table_spec(ds, table)
    assert spec["schema"]
    text = OC.schema_path(ds, table).read_text(encoding="utf-8")
    for m in PL.EVETIS_FORBIDDEN_MARKERS + ("client_001", "mpa-t-client"):
        assert m not in text, (table, m)


def test_confirmed_seller_binding_lives_where_the_runtime_can_only_read():
    assert "SELLER_BINDING" in OC.PLATFORM_TABLES["ref"]
    assert "SELLER_BINDING" not in OC.PLATFORM_TABLES["tenant_ops"]
    assert "SELLER_IDENTITY_OBSERVATIONS" in OC.PLATFORM_TABLES["tenant_ops"]
    c = SY.fixture_contract("client_001")
    acl = PS.expected_dataset_access(c)
    runtime = f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com"
    assert ("READER", "user_by_email", runtime, None) in acl["ref"]
    assert not any(who == runtime and role in ("WRITER", "OWNER") for role, _k, who, _c in acl["ref"])


def test_completeness_statuses_are_the_agreed_vocabulary():
    for ds, table, col in (("tenant_ops", "DATA_COVERAGE", "status"),
                           ("tenant_ops", "HISTORY_BOUNDARIES", "completeness_status")):
        f = next(x for x in OC.load_table_spec(ds, table)["schema"] if x["name"] == col)
        for s in ("COMPLETE", "PARTIAL", "NOT_AVAILABLE", "NOT_APPLICABLE", "FAILED", "UNKNOWN"):
            assert s in f["description"], (table, s)


def test_history_boundaries_separate_api_capability_from_seller_activity():
    cols = {f["name"] for f in OC.load_table_spec("tenant_ops", "HISTORY_BOUNDARIES")["schema"]}
    assert {"requested_from", "api_documented_from", "api_verified_from", "first_observed_activity",
            "first_data_date", "available_to", "backfill_from", "backfill_to", "limitation_reason",
            "completeness_status", "confidence", "evidence_json"} <= cols
    nullable = {f["name"] for f in OC.load_table_spec("tenant_ops", "HISTORY_BOUNDARIES")["schema"]
                if f.get("mode", "NULLABLE") == "NULLABLE"}
    # UNKNOWN обязано быть выразимо: ни одна граница не принуждается к значению
    assert {"api_documented_from", "api_verified_from", "first_observed_activity", "available_to"} <= nullable


# ═══════════════════════════════════════ переносимость и ACL
@pytest.mark.parametrize("tid", SY.SYNTHETIC_TENANTS)
def test_every_synthetic_tenant_gets_the_same_data_contract(tid):
    c = SY.fixture_contract(tid)
    assert set(c["datasets"]) == T4_DATASETS
    per = {}
    for t in c["tables"]:
        per.setdefault(t["dataset_key"], set()).add(t["table_id"])
    assert len(per["ozon_raw"]) == 15 and per["ref"] == set(OC.PLATFORM_TABLES["ref"])
    assert per["tenant_ops"] == set(OC.PLATFORM_TABLES["tenant_ops"])


def test_client_001_and_client_002_differ_only_in_tenant_identity():
    a, b = SY.fixture_contract("client_001"), SY.fixture_contract("client_002")
    assert a["datasets"] == b["datasets"] and a["tables"] == b["tables"]
    assert a["project_id"] != b["project_id"] and a["state"] != b["state"]


def test_new_datasets_grant_nothing_to_runtime_or_customer():
    c = SY.fixture_contract("client_001")
    acl = PS.expected_dataset_access(c)
    runtime = f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com"
    deployer = f"sa-sql-deployer@{c['project_id']}.iam.gserviceaccount.com"
    control = f"sa-tenant-control@{c['project_id']}.iam.gserviceaccount.com"
    for ds in ("ozon_mart", "tenant_ops", "analytics_share"):
        # T4.1: кроме projectOwners — только деплоер SQL; ни runtime, ни клиента.
        # T5: control plane читает и дописывает только tenant_ops; витрину и клиентский слой не видит.
        want = {"projectOwners", deployer} | ({control} if ds == "tenant_ops" else set())
        assert {who for _r, _k, who, _c in acl[c["datasets"][ds]]} == want, ds
        assert runtime not in {who for _r, _k, who, _c in acl[c["datasets"][ds]]}, ds


def test_scanner_rejects_a_customer_principal_on_analytics_share():
    from tools.tests.test_tenancy_t32_remediation import _acl, _plan_for
    c = SY.fixture_contract("client_001")
    plan = _plan_for(c)
    ds = next(r for r in plan["resource_changes"]
              if r["type"] == "google_bigquery_dataset" and r["change"]["after"]["dataset_id"] == "analytics_share")
    ds["change"]["after"]["access"].append(_acl("READER", user_by_email="customer-ai@example.com"))
    assert any("analytics_share" in f and "лишняя запись" in f for f in PS.scan_plan(plan, c))


def test_contract_json_is_deterministic_for_new_tables():
    a = json.dumps(R.terraform_inputs("client_001"), sort_keys=True)
    b = json.dumps(R.terraform_inputs("client_001"), sort_keys=True)
    assert a == b and copy.deepcopy(a) == b


# ═══════════════════════════════════════ ревью T4: таблицы в плане и дата истории
def _table_rc(plan, table_id):
    return next(r for r in plan["resource_changes"]
                if r["type"] == "google_bigquery_table" and r["change"]["after"]["table_id"] == table_id)


def test_clean_synthetic_plan_has_no_table_findings():
    from tools.tests.test_tenancy_t32_remediation import _plan_for
    c = SY.fixture_contract("client_001")
    assert not [f for f in PS.scan_plan(_plan_for(c), c) if "таблица" in f or "блок " in f]


@pytest.mark.parametrize("block,value", [
    ("view", [{"query": "SELECT 1", "use_legacy_sql": False}]),
    ("materialized_view", [{"query": "SELECT 1"}]),
    ("external_data_configuration", [{"source_uris": ["gs://x/y"]}]),
    ("table_replication_info", [{"source_table_id": "x"}]),
])
def test_scanner_rejects_a_contract_table_turned_into_a_view_or_external(block, value):
    from tools.tests.test_tenancy_t32_remediation import _plan_for
    c = SY.fixture_contract("client_001")
    plan = _plan_for(c)
    _table_rc(plan, "SELLER_BINDING")["change"]["after"][block] = value
    assert any(f"блок {block} запрещён" in f for f in PS.scan_plan(plan, c))


def test_scanner_rejects_a_block_that_appears_only_after_apply():
    from tools.tests.test_tenancy_t32_remediation import _plan_for
    c = SY.fixture_contract("client_001")
    plan = _plan_for(c)
    rc = _table_rc(plan, "DATA_COVERAGE")
    rc["change"].setdefault("after_unknown", {})["view"] = True
    assert any("блок view запрещён" in f for f in PS.scan_plan(plan, c))


@pytest.mark.parametrize("dataset_key,table_id", [("tenant_ops", "EXTRA_TABLE"), ("ref", "DATA_COVERAGE")])
def test_scanner_rejects_tables_outside_the_contract(dataset_key, table_id):
    from tools.tests.test_tenancy_t32_remediation import _plan_for
    c = SY.fixture_contract("client_001")
    plan = _plan_for(c)
    after = _table_rc(plan, "DQ_RESULTS")["change"]["after"]
    after["dataset_id"], after["table_id"] = c["datasets"][dataset_key], table_id
    assert any("не входит в контракт" in f for f in PS.scan_plan(plan, c))


def test_history_request_cannot_ask_for_the_future():
    doc = _doc()
    doc["marketplaces"]["ozon"]["history_request"] = {"mode": "FROM_DATE", "from_date": "2099-01-01"}
    assert "history" in _rules(doc)
