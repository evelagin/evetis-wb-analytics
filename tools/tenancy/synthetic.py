"""Синтетические контракты для офлайн-доказательств Terraform арендатора (T3.2).

Не данные арендатора: фикстуры строятся ТЕМ ЖЕ путём, что и боевой контракт —
через valid_tenants()/load_tenant() и registry.terraform_inputs. Отличаются только
  * образом — заведомо синтетический digest (реальный выпуск — T3.2b);
  * client_002 — эфемерный дескриптор во временной копии tenants/, в Git как
    арендатор не добавляется.
Файлы infra/tenant/tests/fixtures/*.contract.json обязаны совпадать с выводом
этого модуля (tools/tests/test_tenancy_t32.py).
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path

from tools.tenancy import naming as N
from tools.tenancy import platform as PL
from tools.tenancy.registry import TENANTS_DIR, _terraform_contract, load_tenant
from tools.tenancy.validation import load_tenant_document

FIXTURE_IMAGE = f"{PL.RUNTIME_REGISTRY}/ozon-runtime@sha256:" + "f" * 64
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "infra" / "tenant" / "tests" / "fixtures"
SYNTHETIC_TENANTS = ("client_001", "client_002")


def _with_ephemeral_client_002(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    doc = copy.deepcopy(load_tenant_document(src / "client_001" / "tenant.json"))
    doc["tenant_id"] = "client_002"
    doc["display_name"] = "Синтетический арендатор 002 (эфемерная фикстура T3.2)"
    doc["data_boundary"]["gcp_project_id"] = N.derive_project_id("client_002")
    (dst / "client_002").mkdir()
    (dst / "client_002" / "tenant.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    return dst


def fixture_contract(tenant_id: str) -> dict:
    if tenant_id not in SYNTHETIC_TENANTS:
        raise ValueError(f"фикстуры только для синтетики {SYNTHETIC_TENANTS}")
    with tempfile.TemporaryDirectory() as tmp:
        root = _with_ephemeral_client_002(TENANTS_DIR, Path(tmp) / "tenants")
        # Только тесты: тот же строгий load_tenant, но во временной копии реестра.
        contract = _terraform_contract(load_tenant(tenant_id, root))
    contract["marketplaces"]["ozon"]["runtime_image"] = FIXTURE_IMAGE
    return contract


def fixture_text(tenant_id: str) -> str:
    return json.dumps(fixture_contract(tenant_id), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


RELEASE_FIXTURE_DIR = FIXTURE_DIR / "release"


def release_contract(tenant_id: str) -> dict:
    """Контракт с НАСТОЯЩИМ утверждённым образом из runtime_release.json (T3.2b), без подмены."""
    if tenant_id not in SYNTHETIC_TENANTS:
        raise ValueError(f"фикстуры только для синтетики {SYNTHETIC_TENANTS}")
    with tempfile.TemporaryDirectory() as tmp:
        root = _with_ephemeral_client_002(TENANTS_DIR, Path(tmp) / "tenants")
        contract = _terraform_contract(load_tenant(tenant_id, root))
    if not contract["marketplaces"]["ozon"]["runtime_image"]:
        raise ValueError("в runtime_release.json нет утверждённого образа Ozon")
    return contract


def release_fixture_text(tenant_id: str) -> str:
    return json.dumps(release_contract(tenant_id), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


EVETIS_IMAGE = ("europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/"
                "ozon-runtime-ingest@sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b")


def _set(doc: dict, path: str, value) -> dict:
    d = copy.deepcopy(doc)
    node = d
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return d


def negative_cases() -> dict[str, dict]:
    """Одна мутация на случай: каждый обязан быть отвергнут проверками переменной."""
    base = fixture_contract("client_001")
    reg = PL.RUNTIME_REGISTRY
    return {
        # Подмена проекта — вместе с согласованным блоком деплоера SQL (T4.1): иначе фикстуру
        # отвергал бы guard email деплоера, а не проверяемое правило пространства имён.
        "evetis_project": _project(base, PL.EVETIS_PROJECT_ID),
        "arbitrary_project": _project(base, "some-other-project-1"),
        "foreign_tenant_project": _project(base, N.derive_project_id("client_002")),
        "foreign_state_prefix": _set(base, "state.prefix", "tenants/client_002"),
        "platform_state_prefix": _set(base, "state.prefix", "platform/client_001"),
        "evetis_state_bucket": _set(base, "state.bucket", f"evetis-wb-tfstate-{PL.EVETIS_PROJECT_NUMBER}"),
        "org_root_parent": _set(base, "parent_folder", f"organizations/{PL.ORGANIZATION_ID}"),
        "image_latest_tag": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime:latest"),
        "image_mutable_tag": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime:v1"),
        "image_missing": _set(base, "marketplaces.ozon.runtime_image", None),
        "image_empty": _set(base, "marketplaces.ozon.runtime_image", ""),
        "image_evetis_registry": _set(base, "marketplaces.ozon.runtime_image", EVETIS_IMAGE),
        "image_short_digest": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime@sha256:abc"),
        "scheduler_enabled": _set(base, "scheduler_state", "ENABLED"),
        "evetis_secret_name": _set(base, "marketplaces.ozon.secret_ids.seller_api_key", "EVETIS_OZON_API_KEY"),
        "evetis_ref_dataset": _set(base, "datasets.ref", "evetis_ref"),
        "unknown_contract_version": _set(base, "contract_version", 2),
        # T4: состав датасетов фиксирован; клиентский датасет не заводится мимо naming.
        "extra_customer_dataset": _set(base, "datasets.customer_share", "customer_share"),
        "table_in_undeclared_dataset": _table_in(base, "customer_share"),
        # T4.1: деплоер SQL — только свой SA, только роли mpaSql*, условие ровно одно и ровно V_*.
        "sql_deployer_foreign_project_sa": _set(base, "sql_deployer.email",
                                                "sa-sql-deployer@" + N.derive_project_id("client_002")
                                                + ".iam.gserviceaccount.com"),
        "sql_deployer_other_account": _grant(base, lambda s: s.update(account_id="sa-tenant-provisioner")),
        "sql_deployer_predefined_role": _grant(base, lambda s: s["grants"][0].update(role="roles/bigquery.dataEditor")),
        "sql_deployer_unconditional_tenant_ops_update": _grant(base, lambda s: _tenant_ops_update(s).update(
            condition=None)),
        "sql_deployer_broad_condition": _grant(base, lambda s: _tenant_ops_update(s)["condition"].update(
            expression=_tenant_ops_update(s)["condition"]["expression"].replace("/tables/V_", "/tables/"))),
        "sql_deployer_condition_on_source_read": _grant(base, lambda s: s["grants"][0].update(
            condition=_tenant_ops_update(s)["condition"])),
        "sql_deployer_duplicate_grant": _grant(base, lambda s: s["grants"].append(copy.deepcopy(s["grants"][0]))),
        "sql_deployer_grant_in_undeclared_dataset": _grant(base, lambda s: s["grants"][0].update(
            dataset_key="customer_share")),
        "sql_deployer_extra_raw_update": _grant(base, lambda s: s["grants"].append(
            {"dataset_key": "ozon_raw", "role": s["grants"][-1]["role"].rsplit("/", 1)[0] + "/mpaSqlViewUpdate",
             "condition": None})),
        "sql_deployer_client_layer_reads_rows": _grant(base, lambda s: s["grants"].append(
            {"dataset_key": "analytics_share", "role": s["grants"][-1]["role"].rsplit("/", 1)[0] + "/mpaSqlSourceRead",
             "condition": None})),
        "sql_deployer_condition_title_changed": _grant(base, lambda s: _tenant_ops_update(s)["condition"].update(
            title="anything")),
        # T5 (D1): control plane — только свой SA, ровно матрица control_identity, без записи в ozon_raw и
        # ref (привязку подтверждает только владелец), без условий; runtime — только с проверкой привязки.
        "control_missing": _set(base, "control", None),
        "control_other_account": _control(base, lambda c: c.update(account_id="sa-tenant-provisioner")),
        "control_foreign_project_sa": _control(base, lambda c: c.update(
            email="sa-tenant-control@" + N.derive_project_id("client_002") + ".iam.gserviceaccount.com")),
        "control_writes_ref": _control(base, lambda c: c["grants"].append(
            {"dataset_key": "ref", "role": _org_role(c, "mpaTenantControlAppend"), "condition": None})),
        "control_writes_raw": _control(base, lambda c: c["grants"].append(
            {"dataset_key": "ozon_raw", "role": _org_role(c, "mpaTenantControlAppend"), "condition": None})),
        "control_reads_client_layer": _control(base, lambda c: c["grants"].append(
            {"dataset_key": "analytics_share", "role": _org_role(c, "mpaSqlSourceRead"), "condition": None})),
        "control_predefined_role": _control(base, lambda c: c["grants"][0].update(role="roles/bigquery.dataEditor")),
        "control_conditional_grant": _control(base, lambda c: c["grants"][0].update(condition={
            "title": "t", "description": "d", "expression": "true"})),
        "control_job_renamed": _control(base, lambda c: c["job"].update(name="ozon-daily")),
        "control_env_binding_flag": _control(base, lambda c: c["job"]["env"].update(TENANT_BINDING_REQUIRED="1")),
        "runtime_job_without_binding_flag": _runtime_env(base, lambda e: e.pop("TENANT_BINDING_REQUIRED")),
    }


def _project(base: dict, project_id: str) -> dict:
    from tools.tenancy import control_identity as CI
    from tools.tenancy import sql_identity as SI
    doc = _set(base, "project_id", project_id)
    doc["sql_deployer"] = SI.contract_block(project_id, doc["datasets"])
    # Согласованный блок control (email и проект в окружении job'а), иначе фикстуру отвергал бы guard
    # control, а не проверяемое правило пространства имён (мутационная проверка это ловит).
    doc["control"] = dict(copy.deepcopy(doc["control"]), email=CI.control_email(project_id))
    doc["control"]["job"]["env"]["GCP_PROJECT_ID"] = project_id
    return doc


def _control(base: dict, mutate) -> dict:
    doc = copy.deepcopy(base)
    mutate(doc["control"])
    return doc


def _org_role(block: dict, role_id: str) -> str:
    return block["grants"][0]["role"].rsplit("/", 1)[0] + "/" + role_id


def _runtime_env(base: dict, mutate) -> dict:
    doc = copy.deepcopy(base)
    mutate(next(iter(doc["marketplaces"]["ozon"]["jobs"].values()))["env"])
    return doc


def _grant(base: dict, mutate) -> dict:
    doc = copy.deepcopy(base)
    mutate(doc["sql_deployer"])
    return doc


def _tenant_ops_update(block: dict) -> dict:
    return next(g for g in block["grants"] if g["dataset_key"] == "tenant_ops" and g["role"].endswith("/mpaSqlViewUpdate"))


def _table_in(base: dict, dataset_key: str) -> dict:
    doc = copy.deepcopy(base)
    doc["tables"][0]["dataset_key"] = dataset_key
    return doc


if __name__ == "__main__":     # регенерация: python -m tools.tenancy.synthetic
    for t in SYNTHETIC_TENANTS:
        (FIXTURE_DIR / f"{t}.contract.json").write_text(fixture_text(t), encoding="utf-8")
        print(f"записана фикстура {t}")
    neg = FIXTURE_DIR / "negative"
    neg.mkdir(exist_ok=True)
    for name, doc in negative_cases().items():
        (neg / f"{name}.contract.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"негативных фикстур: {len(negative_cases())}")
