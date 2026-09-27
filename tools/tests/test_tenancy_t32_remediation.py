"""Tenancy T3.2 — закрытие находок финального ревью PR #179 (H1, H2, M1–M6, L1–L4).

Офлайн: ни облака, ни сети. Каждая находка — положительный случай и отрицательные
контроли; мутационные тесты доказывают, что правило сканера несёт нагрузку (без него
соответствующий контроль проходит).
"""
from __future__ import annotations

import copy
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.autonomy import gatekeeper as G  # noqa: E402
from tools.autonomy.policy import tcb_paths  # noqa: E402
from tools.tenancy import iam_proposals as I  # noqa: E402
from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import plan_scan as PS  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import tenant_infra as TI  # noqa: E402
from tools.tests import ae_fixtures as F  # noqa: E402

TENANT_ROOT = REPO / "infra" / "tenant"
WORKFLOW = REPO / PL.TENANT_INFRA_WORKFLOW
TF_FILES = sorted(p for p in TENANT_ROOT.rglob("*.tf") if ".terraform" not in p.parts)
NUMBER = "123456789012"


# ═════════════════════════════════════ реалистичный план (как terraform show -json)
def _rc(addr, rtype, after, unknown=None, actions=("create",), module=None):
    rc = {"address": addr, "mode": "managed", "type": rtype, "name": addr.split(".")[-1].split("[")[0],
          "change": {"actions": list(actions), "before": None, "after": after,
                     "after_unknown": unknown or {}}}
    if module:
        rc["module_address"] = module
    return rc


def _plan_for(contract, number=NUMBER):
    p, o, region = contract["project_id"], contract["marketplaces"]["ozon"], contract["region"]
    rt = f"{o['service_accounts']['runtime']}@{p}.iam.gserviceaccount.com"
    sch = f"{o['service_accounts']['scheduler']}@{p}.iam.gserviceaccount.com"
    m = "module.ozon[0]"
    rcs = [_rc("terraform_data.guard", "terraform_data", {"input": {"tenant_id": contract["tenant_id"],
                                                                    "project_id": p}}, {"id": True})]
    rcs += [_rc(f'google_project_service.this["{a}"]', "google_project_service",
                {"project": p, "service": a, "disable_on_destroy": False}, {"id": True}) for a in contract["apis"]]
    grants = {o["raw_dataset_key"]: "WRITER", o["ref_dataset_key"]: "READER"}
    rcs += [_rc(f'google_bigquery_dataset.this["{k}"]', "google_bigquery_dataset",
                {"project": p, "dataset_id": ds, "location": "EU",
                 "access": [_acl("OWNER", special_group="projectOwners")]
                           + ([_acl(grants[k], user_by_email=rt)] if k in grants else [])},
                {"etag": True, "id": True, "access": [{}, {}]})
            for k, ds in contract["datasets"].items()]
    rcs += [_rc(f'google_bigquery_table.this["{t["dataset_key"]}.{t["table_id"]}"]', "google_bigquery_table",
                {"project": p, "dataset_id": contract["datasets"][t["dataset_key"]], "table_id": t["table_id"],
                 "schema": t["schema_json"], "deletion_protection": True}, {"etag": True})
            for t in contract["tables"]]
    # Как у провайдера google 7.x (живой план T3.3): email и member известны на плане.
    for key in ("runtime", "scheduler"):
        email = f"{o['service_accounts'][key]}@{p}.iam.gserviceaccount.com"
        rcs.append(_rc(f"{m}.google_service_account.{key}", "google_service_account",
                       {"project": p, "account_id": o["service_accounts"][key], "email": email,
                        "member": f"serviceAccount:{email}"},
                       {"id": True, "name": True, "unique_id": True}, module=m))
    member = f"serviceAccount:{rt}"
    rcs.append(_rc(f"{m}.google_project_iam_member.runtime_job_user", "google_project_iam_member",
                   {"project": p, "role": "roles/bigquery.jobUser", "member": member}, {"etag": True}, module=m))
    for role, sid in o["secret_ids"].items():
        rcs.append(_rc(f'{m}.google_secret_manager_secret.ozon["{role}"]', "google_secret_manager_secret",
                       {"project": p, "secret_id": sid, "labels": {"marketplace": "ozon"}}, {"name": True}, module=m))
        rcs.append(_rc(f'{m}.google_secret_manager_secret_iam_member.runtime_access["{role}"]',
                       "google_secret_manager_secret_iam_member",
                       {"project": p, "secret_id": sid, "role": "roles/secretmanager.secretAccessor",
                        "member": member}, {"etag": True}, module=m))
    for job, spec in o["jobs"].items():
        rcs.append(_rc(f'{m}.google_cloud_run_v2_job.this["{job}"]', "google_cloud_run_v2_job",
                       {"project": p, "name": job, "location": region, "deletion_protection": True,
                        "template": [{"template": [{"service_account": rt, "containers": [{
                            "image": o["runtime_image"],
                            "env": [{"name": k, "value": v} for k, v in spec["env"].items()]}]}]}]},
                       {"uid": True, "etag": True}, module=m))
        rcs.append(_rc(f'{m}.google_cloud_scheduler_job.this["{job}"]', "google_cloud_scheduler_job",
                       {"project": p, "name": spec["scheduler"], "region": region, "paused": True,
                        "schedule": spec["schedule"], "http_target": [{
                            "http_method": "POST",
                            "uri": f"https://{region}-run.googleapis.com/v2/projects/{p}/locations/{region}/jobs/{job}:run",
                            "oauth_token": [{"service_account_email": sch}]}]},
                       {"state": True}, module=m))
    root_cfg = [{"address": "terraform_data.guard", "mode": "managed", "type": "terraform_data",
                 "provider_config_key": "terraform"},
                {"address": "data.google_project.tenant", "mode": "data", "type": "google_project",
                 "provider_config_key": "google"},
                {"address": "data.google_projects.tenant_active", "mode": "data", "type": "google_projects",
                 "provider_config_key": "google"}]
    root_cfg += [{"address": f"{t}.this", "mode": "managed", "type": t, "provider_config_key": "google"}
                 for t in ("google_project_service", "google_bigquery_dataset", "google_bigquery_table")]
    # Адреса — как в конфигурации корня infra/tenant (terraform show -json).
    mod_cfg = [{"address": a, "mode": "managed", "type": a.split(".")[0], "provider_config_key": "module.ozon:google",
                "expressions": {"project": {"references": ["var.project_id"]}}}
               for a in ("google_service_account.runtime", "google_service_account.scheduler",
                         "google_project_iam_member.runtime_job_user", "google_secret_manager_secret.ozon",
                         "google_secret_manager_secret_iam_member.runtime_access",
                         "google_cloud_run_v2_job.this", "google_cloud_scheduler_job.this")]
    return {
        "format_version": "1.2", "terraform_version": "1.15.8",
        "variables": {"contract": {"value": contract}},
        "configuration": {"provider_config": {"google": {"name": "google",
                                                         "full_name": "registry.terraform.io/hashicorp/google"}},
                          "root_module": {"resources": root_cfg,
                                          "module_calls": {"ozon": {"source": "./modules/ozon_runtime",
                                                                    "module": {"resources": mod_cfg}}}}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.google_project.tenant", "mode": "data", "type": "google_project",
             "values": {"project_id": p, "number": number, "folder_id": PL.TENANTS_FOLDER_ID,
                        "billing_account": "000000-000000-000000"}},
            {"address": "data.google_projects.tenant_active", "mode": "data", "type": "google_projects",
             "values": {"filter": f"id:{p} lifecycleState:ACTIVE",
                        "projects": [{"project_id": p, "number": number, "lifecycle_state": "ACTIVE",
                                      "parent": {"id": PL.TENANTS_FOLDER_ID, "type": "folder"}}]}}]}}},
        "resource_changes": rcs,
        "output_changes": {"project_id": {"actions": ["create"], "after": p}},
    }


@pytest.fixture
def contract():
    return SY.fixture_contract("client_001")


def _mutated(contract, fn):
    plan = _plan_for(contract)
    fn(plan, contract)
    return plan


def _acl(role, **who):
    """Запись access датасета в форме terraform show -json провайдера google 7.x."""
    entry = {"condition": [], "dataset": [], "domain": "", "group_by_email": "", "iam_member": "",
             "role": role, "routine": [], "special_group": "", "user_by_email": "", "view": []}
    entry.update(who)
    return entry


def _ds(plan, name):
    return next(r for r in plan["resource_changes"]
                if r["type"] == "google_bigquery_dataset" and r["change"]["after"]["dataset_id"] == name)


def _sa(plan, key):
    return next(r for r in plan["resource_changes"]
                if r["type"] == "google_service_account" and r["address"].endswith(f".{key}"))


def _add(plan, rc):
    plan["resource_changes"].append(rc)


def _first(plan, rtype):
    return next(r for r in plan["resource_changes"] if r["type"] == rtype)


P1 = "mpa-t-client-001"
M = "module.ozon[0]"

# ═════════════════════════════════════ отрицательные контроли (все классы ревью)
NEGATIVE = {
    # H1 — принципалы
    "H1 secret -> allUsers": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": "allUsers"}, module=M)),
    "H1 dataset -> allAuthenticatedUsers": lambda p, c: _add(p, _rc(f"{M}.google_bigquery_dataset_iam_member.x", "google_bigquery_dataset_iam_member", {"project": P1, "dataset_id": "ozon_raw", "role": "roles/bigquery.dataViewer", "member": "allAuthenticatedUsers"}, module=M)),
    "H1 secret -> external user": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": "user:attacker@gmail.com"}, module=M)),
    "H1 dataset -> external group": lambda p, c: _add(p, _rc(f"{M}.google_bigquery_dataset_iam_member.x", "google_bigquery_dataset_iam_member", {"project": P1, "dataset_id": "ref", "role": "roles/bigquery.dataViewer", "member": "group:outsiders@example.com"}, module=M)),
    "H1 dataset owner -> domain": lambda p, c: _add(p, _rc(f"{M}.google_bigquery_dataset_iam_member.x", "google_bigquery_dataset_iam_member", {"project": P1, "dataset_id": "ozon_raw", "role": "roles/bigquery.dataOwner", "member": "domain:example.com"}, module=M)),
    "H1 jobUser -> foreign SA": lambda p, c: _add(p, _rc(f"{M}.google_project_iam_member.x", "google_project_iam_member", {"project": P1, "role": "roles/bigquery.jobUser", "member": "serviceAccount:sa-x@mpa-t-client-002.iam.gserviceaccount.com"}, module=M)),
    "H1 secret -> platform WIF principalSet": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "s", "role": "roles/secretmanager.secretAccessor", "member": "principalSet://iam.googleapis.com/projects/777428383056/locations/global/workloadIdentityPools/tenant-infra-pool/*"}, module=M)),
    "H1 secret -> arbitrary principal://": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": "principal://iam.googleapis.com/locations/global/workforcePools/p/subject/x"}, module=M)),
    "H1 jobUser -> platform numeric compute SA": lambda p, c: _add(p, _rc(f"{M}.google_project_iam_member.x", "google_project_iam_member", {"project": P1, "role": "roles/bigquery.jobUser", "member": "serviceAccount:777428383056-compute@developer.gserviceaccount.com"}, module=M)),
    "H1 secret -> platform numeric service agent": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": "serviceAccount:service-777428383056@gcp-sa-artifactregistry.iam.gserviceaccount.com"}, module=M)),
    "H1 secret -> provisioner": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": f"serviceAccount:{PL.PROVISIONER_SA}"}, module=M)),
    "H1 secret -> EVETIS SA": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": "serviceAccount:metabase-read-only@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"}, module=M)),
    "H1 runtime gets owner": lambda p, c: _first(p, "google_project_iam_member")["change"]["after"].update({"role": "roles/owner"}),
    "H1 legit member on wrong secret": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "some-other-secret", "role": "roles/secretmanager.secretAccessor", "member": f"serviceAccount:sa-ozon-runtime@{P1}.iam.gserviceaccount.com"}, module=M)),
    "H1 principal smuggled in a label": lambda p, c: _first(p, "google_bigquery_dataset")["change"]["after"].update({"labels": {"x": "user:attacker@gmail.com"}}),
    # M1 — альтернативные ссылки
    "M1 projects/<platform number>": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after"]["http_target"][0].update({"uri": "https://pubsub.googleapis.com/v1/projects/777428383056/topics/t:publish"}),
    "M1 projects/<other tenant number>": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "projects/210987654321/secrets/ozon-seller-api-key"}),
    "M1 projects/<other tenant id>": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "projects/mpa-t-client-002/secrets/ozon-seller-api-key"}),
    "M1 URL-encoded foreign project": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "projects%2Fmpa-t-client-002%2Fsecrets%2Fx"}),
    "M1 double-encoded platform number": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "projects%252F777428383056"}),
    "M1 gs:// platform state bucket": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "S", "value": "gs://mpa-platform-tfstate-777428383056/platform/default.tfstate"}),
    "M1 storage URL platform bucket": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after"]["http_target"][0].update({"uri": "https://storage.googleapis.com/storage/v1/b/mpa-platform-tfstate-777428383056/o/platform%2Fx"}),
    "M1 any gs:// bucket": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "S", "value": "gs://some-bucket/x"}),
    "M1 gcr.io platform path": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "I", "value": "eu.gcr.io/mpa-platform/x"}),
    "M1 pkg.dev other project": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0].update({"image": "europe-west1-docker.pkg.dev/mpa-t-client-002/r/i@sha256:" + "a" * 64}),
    "M1 platform registry outside image": lambda p, c: _first(p, "google_bigquery_dataset")["change"]["after"].update({"labels": {"x": PL.RUNTIME_REGISTRY + "/y"}}),
    "M1 other-number service agent in env": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "service-210987654321@gcp-sa-pubsub.iam.gserviceaccount.com"}),
    "M1 EVETIS id uppercase": lambda p, c: _first(p, "google_bigquery_dataset")["change"]["after"].update({"labels": {"x": "PROJECT-FA311FC0-4D87-4781-986"}}),
    "M1 EVETIS number": lambda p, c: p["configuration"].update({"x": "service-37074083763@serverless-robot-prod.iam.gserviceaccount.com"}),
    # M2 — структура
    "M2 unexpected data source (config)": lambda p, c: p["configuration"]["root_module"]["resources"].append({"address": "data.google_projects.evil", "mode": "data", "type": "google_projects", "provider_config_key": "google"}),
    "M2 unexpected data source (read)": lambda p, c: p["prior_state"]["values"]["root_module"]["resources"].append({"address": "data.google_secret_manager_secret_version.x", "mode": "data", "type": "google_secret_manager_secret_version", "values": {}}),
    "M2 deferred data read": lambda p, c: _add(p, {"address": "data.google_projects.tenant_active", "mode": "data", "type": "google_projects", "change": {"actions": ["read"], "after": {}}}),
    "M2 unknown member": lambda p, c: (_first(p, "google_project_iam_member")["change"]["after"].pop("member"), _first(p, "google_project_iam_member")["change"]["after_unknown"].update({"member": True})),
    "M2 unknown image": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after_unknown"].update({"template": [{"template": [{"containers": [{"image": True}]}]}]}),
    "M2 unknown scheduler target": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after_unknown"].update({"http_target": [{"uri": True}]}),
    "M2 unknown tenant number": lambda p, c: p["prior_state"]["values"]["root_module"]["resources"][0]["values"].update({"number": None}),
    "M2 local-exec provisioner": lambda p, c: p["configuration"]["root_module"]["resources"][0].update({"provisioners": [{"type": "local-exec"}]}),
    "M2 remote-exec provisioner in module": lambda p, c: p["configuration"]["root_module"]["module_calls"]["ozon"]["module"]["resources"][0].update({"provisioners": [{"type": "remote-exec"}]}),
    "M2 unexpected provider": lambda p, c: p["configuration"]["provider_config"].update({"external": {"name": "external", "full_name": "registry.terraform.io/hashicorp/external"}}),
    "M2 unexpected type in nested module": lambda p, c: _add(p, _rc("module.ozon[0].module.x.google_compute_instance.vm", "google_compute_instance", {"project": P1}, module="module.ozon[0].module.x")),
    "M2 unexpected module in config": lambda p, c: p["configuration"]["root_module"]["module_calls"].update({"extra": {"module": {"resources": []}}}),
    "M2 google_project resource": lambda p, c: _add(p, _rc("google_project.p", "google_project", {"project_id": P1}, actions=("no-op",))),
    "M2 billing resource": lambda p, c: _add(p, _rc("google_billing_project_info.b", "google_billing_project_info", {"project": P1})),
    "M2 folder IAM": lambda p, c: _add(p, _rc("google_folder_iam_member.f", "google_folder_iam_member", {"folder": "folders/881419274207", "role": "roles/viewer", "member": "user:x@example.com"})),
    "M2 delete": lambda p, c: _first(p, "google_bigquery_table")["change"].update({"actions": ["delete"]}),
    "M2 replace": lambda p, c: _first(p, "google_bigquery_table")["change"].update({"actions": ["create", "delete"]}),
    # H2 — вызов и расписания
    "H2 scheduler invoker binding": lambda p, c: _add(p, _rc(f'{M}.google_cloud_run_v2_job_iam_member.scheduler_invoke["ozon-runtime-daily"]', "google_cloud_run_v2_job_iam_member", {"project": P1, "location": "europe-west1", "name": "ozon-runtime-daily", "role": "roles/run.invoker", "member": f"serviceAccount:sa-ozon-scheduler@{P1}.iam.gserviceaccount.com"}, module=M)),
    "H2 run.invoker to runtime SA": lambda p, c: _add(p, _rc(f"{M}.google_project_iam_member.inv2", "google_project_iam_member", {"project": P1, "role": "roles/run.invoker", "member": f"serviceAccount:sa-ozon-runtime@{P1}.iam.gserviceaccount.com"}, module=M)),
    "H2 project-level run.invoker": lambda p, c: _add(p, _rc(f"{M}.google_project_iam_member.inv", "google_project_iam_member", {"project": P1, "role": "roles/run.invoker", "member": f"serviceAccount:sa-ozon-scheduler@{P1}.iam.gserviceaccount.com"}, module=M)),
    "H2 unpaused scheduler": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after"].update({"paused": False}),
    "H2 scheduler retargeted to BigQuery": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after"]["http_target"][0].update({"uri": f"https://bigquery.googleapis.com/bigquery/v2/projects/{P1}/jobs"}),
    "H2 scheduler signs as runtime SA": lambda p, c: _first(p, "google_cloud_scheduler_job")["change"]["after"]["http_target"][0]["oauth_token"][0].update({"service_account_email": f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com"}),
    "H2 job runs as foreign SA": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0].update({"service_account": "x@mpa-t-client-002.iam.gserviceaccount.com"}),
    # И1 — секреты
    "И1 secret version resource": lambda p, c: _add(p, _rc(f"{M}.google_secret_manager_secret_version.v", "google_secret_manager_secret_version", {"secret": f"projects/{P1}/secrets/ozon-seller-api-key", "secret_data": "x"}, module=M)),
    # D — ACL датасетов (T3.3): авторитетный, ровно по контракту
    "D provisioner OWNER on raw (creator default)": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("OWNER", user_by_email=PL.PROVISIONER_SA)),
    "D provisioner READER on ref": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", user_by_email=PL.PROVISIONER_SA)),
    "D allUsers via iam_member": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", iam_member="allUsers")),
    "D allAuthenticatedUsers special group": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("READER", special_group="allAuthenticatedUsers")),
    "D scheduler SA READER": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("READER", user_by_email=f"sa-ozon-scheduler@{P1}.iam.gserviceaccount.com")),
    "D other tenant SA": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("READER", user_by_email="sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com")),
    "D EVETIS SA": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", user_by_email="sa-loaders-prod@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com")),
    "D projectWriters special group": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("WRITER", special_group="projectWriters")),
    "D projectReaders special group": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", special_group="projectReaders")),
    "D domain": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", domain="example.com")),
    "D group": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", group_by_email="clients@example.com")),
    "D customer user": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].append(_acl("READER", user_by_email="customer@example.com")),
    "D runtime WRITER on ref (wider)": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].__setitem__(1, _acl("WRITER", user_by_email=f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com")),
    "D runtime OWNER on raw (wider)": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].__setitem__(1, _acl("OWNER", user_by_email=f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com")),
    "D runtime grant missing": lambda p, c: _ds(p, "ozon_raw")["change"]["after"]["access"].pop(1),
    "D ACL unknown on plan": lambda p, c: (_ds(p, "ozon_raw")["change"]["after"].pop("access"), _ds(p, "ozon_raw")["change"]["after_unknown"].update({"access": True})),
    "D authorized view entry": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("", view=[{"project_id": P1, "dataset_id": "x", "table_id": "v"}])),
    "D entry with two principals": lambda p, c: _ds(p, "ref")["change"]["after"]["access"].append(_acl("READER", special_group="projectOwners", user_by_email="x@example.com")),
    "D separate dataset_iam_member even for runtime": lambda p, c: _add(p, _rc(f"{M}.google_bigquery_dataset_iam_member.x", "google_bigquery_dataset_iam_member", {"project": P1, "dataset_id": "ozon_raw", "role": "roles/bigquery.dataEditor", "member": f"serviceAccount:sa-ozon-runtime@{P1}.iam.gserviceaccount.com"}, module=M)),
    # E — computed member сервисного аккаунта: только он сам и только SA контракта
    "E SA member is another email": lambda p, c: _sa(p, "runtime")["change"]["after"].update({"member": f"serviceAccount:sa-other@{P1}.iam.gserviceaccount.com"}),
    "E SA member is the other contract SA": lambda p, c: _sa(p, "runtime")["change"]["after"].update({"member": f"serviceAccount:sa-ozon-scheduler@{P1}.iam.gserviceaccount.com"}),
    "E SA member is external user": lambda p, c: _sa(p, "runtime")["change"]["after"].update({"member": "user:attacker@gmail.com"}),
    "E SA member unknown": lambda p, c: (_sa(p, "runtime")["change"]["after"].pop("member"), _sa(p, "runtime")["change"]["after_unknown"].update({"member": True})),
    "E SA email/member of foreign project": lambda p, c: _sa(p, "runtime")["change"]["after"].update({"email": "sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com", "member": "serviceAccount:sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com"}),
    "E extra SA not in contract": lambda p, c: _add(p, _rc(f"{M}.google_service_account.extra", "google_service_account", {"project": P1, "account_id": "sa-extra", "email": f"sa-extra@{P1}.iam.gserviceaccount.com", "member": f"serviceAccount:sa-extra@{P1}.iam.gserviceaccount.com"}, module=M)),
    "E own member smuggled into description": lambda p, c: _sa(p, "runtime")["change"]["after"].update({"description": f"serviceAccount:sa-ozon-runtime@{P1}.iam.gserviceaccount.com"}),
    # образ
    "image not the approved digest": lambda p, c: _first(p, "google_cloud_run_v2_job")["change"]["after"]["template"][0]["template"][0]["containers"][0].update({"image": PL.RUNTIME_REGISTRY + "/ozon-runtime@sha256:" + "0" * 64}),
}


def test_scanner_accepts_the_expected_tenant_plan(contract):
    assert PS.scan_plan(_plan_for(contract), contract) == []


def test_sa_member_exception_is_what_lets_the_real_provider_shape_pass(contract, monkeypatch):
    """Без правила E живой план провайдера 7.x (member известен) падал бы — T3.3, план a7ba1c71."""
    monkeypatch.setattr(PS, "_own_sa_member", lambda *a: False)
    findings = PS.scan_plan(_plan_for(contract), contract)
    assert findings and all(".member" in f and "google_service_account" in f for f in findings)


def test_dataset_acl_in_expected_plan_excludes_provisioner_and_scheduler(contract):
    plan = _plan_for(contract)
    acl = json.dumps([r["change"]["after"]["access"] for r in plan["resource_changes"]
                      if r["type"] == "google_bigquery_dataset"])
    assert PL.PROVISIONER_SA not in acl and "sa-ozon-scheduler" not in acl
    want = PS.expected_dataset_access(contract)
    owners = {("OWNER", "special_group", "projectOwners")}
    assert want == {"ozon_raw": owners | {("WRITER", "user_by_email", f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com")},
                    "ref": owners | {("READER", "user_by_email", f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com")},
                    # T4: внутренние слои и клиентский слой — без runtime и без клиента.
                    "ozon_mart": owners, "tenant_ops": owners, "analytics_share": owners}


def test_reconciling_away_a_creator_owner_is_allowed(contract):
    """Если API всё же добавил создателя, следующий план снимает его (update): это проходит."""
    plan = _plan_for(contract)
    ds = _ds(plan, "ozon_raw")
    ds["change"]["actions"] = ["update"]
    ds["change"]["before"] = dict(ds["change"]["after"], access=ds["change"]["after"]["access"]
                                  + [_acl("OWNER", user_by_email=PL.PROVISIONER_SA)])
    assert PS.scan_plan(plan, contract) == []


def _refreshed(plan, project):
    """Форма плана восстановления T3.3: уже созданные привязки секретов — no-op, secret_id полным именем."""
    for rc in plan["resource_changes"]:
        if rc["type"] == "google_secret_manager_secret_iam_member":
            a = rc["change"]["after"]
            a["secret_id"] = f"projects/{project}/secrets/{a['secret_id']}"
            rc["change"].update({"actions": ["no-op"], "before": copy.deepcopy(a)})
    return plan


def test_scanner_accepts_refreshed_secret_iam_in_full_resource_form(contract):
    assert PS.scan_plan(_refreshed(_plan_for(contract), P1), contract) == []


def test_refreshed_secret_iam_without_normalization_would_fail(contract, monkeypatch):
    """Нормализация несёт нагрузку: без неё живой план восстановления T3.3 падал (4 ложные находки)."""
    monkeypatch.setattr(PS, "_iam_target", lambda rtype, after, project: after.get("secret_id") or after.get("project"))
    findings = PS.scan_plan(_refreshed(_plan_for(contract), P1), contract)
    assert len(findings) == 4 and all("secretAccessor" in f for f in findings)


REFRESHED_SECRET_NEGATIVE = {
    "other tenant project": f"projects/mpa-t-client-002/secrets/ozon-seller-api-key",
    "platform project": "projects/mpa-platform/secrets/ozon-seller-api-key",
    "EVETIS project": "projects/project-fa311fc0-4d87-4781-986/secrets/ozon-seller-api-key",
    "project number instead of id": "projects/210987654321/secrets/ozon-seller-api-key",
    "secret outside contract": f"projects/{P1}/secrets/some-other-secret",
    "extra path segment": f"projects/{P1}/secrets/ozon-seller-api-key/versions/1",
    "wrong collection": f"projects/{P1}/topics/ozon-seller-api-key",
}


@pytest.mark.parametrize("name", sorted(REFRESHED_SECRET_NEGATIVE))
def test_refreshed_secret_iam_normalization_is_tenant_scoped(contract, name):
    plan = _refreshed(_plan_for(contract), P1)
    rc = next(r for r in plan["resource_changes"] if r["type"] == "google_secret_manager_secret_iam_member")
    rc["change"]["after"]["secret_id"] = REFRESHED_SECRET_NEGATIVE[name]
    assert PS.scan_plan(plan, contract) != [], name


# ═════════════════════════════════════ F — вычисляемые creator/last_modifier job'а (T3.3)
BUILDER_SA = "sa-runtime-builder@mpa-platform.iam.gserviceaccount.com"


def _job(plan, i=0):
    return [r for r in plan["resource_changes"] if r["type"] == "google_cloud_run_v2_job"][i]


def _converged(plan):
    """Форма плана сходимости: job'ы после refresh — no-op, creator/last_modifier = провижионер."""
    for rc in plan["resource_changes"]:
        if rc["type"] == "google_cloud_run_v2_job":
            rc["change"]["after"].update({"creator": PL.PROVISIONER_SA, "last_modifier": PL.PROVISIONER_SA})
            rc["change"].update({"actions": ["no-op"], "before": copy.deepcopy(rc["change"]["after"])})
    return plan


def test_scanner_accepts_computed_creator_and_last_modifier_of_refreshed_jobs(contract):
    assert PS.scan_plan(_converged(_plan_for(contract)), contract) == []


def test_computed_applier_exception_is_load_bearing(contract, monkeypatch):
    monkeypatch.setattr(PS, "_computed_applier_identity", lambda *a: False)
    findings = PS.scan_plan(_converged(_plan_for(contract)), contract)
    assert findings and all((".creator" in f or ".last_modifier" in f) for f in findings)


def test_computed_applier_exception_fails_closed_without_resource_configuration(contract):
    plan = _converged(_plan_for(contract))
    mod = plan["configuration"]["root_module"]["module_calls"]["ozon"]["module"]["resources"]
    mod[:] = [r for r in mod if r["type"] != "google_cloud_run_v2_job"]
    assert any(".creator" in f for f in PS.scan_plan(plan, contract))


def test_computed_applier_fields_are_computed_only_in_provider_schema():
    """Схема google 7.46.1 (T3.3): creator/last_modifier — только computed; задать их нельзя."""
    assert PS.COMPUTED_APPLIER_FIELDS == {"google_cloud_run_v2_job": frozenset({"creator", "last_modifier"})}


def _cfg_job(plan):
    mod = plan["configuration"]["root_module"]["module_calls"]["ozon"]["module"]["resources"]
    return next(r for r in mod if r["type"] == "google_cloud_run_v2_job")


F_NEGATIVE = {
    "provisioner email in env": lambda p: _job(p)["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": PL.PROVISIONER_SA}),
    "provisioner email in annotation": lambda p: _job(p)["change"]["after"].update({"annotations": {"x": PL.PROVISIONER_SA}}),
    "provisioner email in configurable client": lambda p: _job(p)["change"]["after"].update({"client": PL.PROVISIONER_SA}),
    "provisioner email in labels": lambda p: _job(p)["change"]["after"].update({"labels": {"x": PL.PROVISIONER_SA}}),
    "provisioner email nested in template": lambda p: _job(p)["change"]["after"]["template"][0].update({"annotations": {"creator": PL.PROVISIONER_SA}}),
    "creator configured (not computed)": lambda p: (_job(p)["change"]["after"].update({"creator": PL.PROVISIONER_SA}), _cfg_job(p).setdefault("expressions", {}).update({"creator": {"constant_value": PL.PROVISIONER_SA}})),
    "builder email in creator": lambda p: _job(p)["change"]["after"].update({"creator": BUILDER_SA}),
    "builder email in last_modifier": lambda p: _job(p)["change"]["after"].update({"last_modifier": BUILDER_SA}),
    "arbitrary mpa-platform identity in creator": lambda p: _job(p)["change"]["after"].update({"creator": "x@mpa-platform.iam.gserviceaccount.com"}),
    "EVETIS identity in creator": lambda p: _job(p)["change"]["after"].update({"creator": "sa-deployer@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"}),
    "client_002 identity in last_modifier": lambda p: _job(p)["change"]["after"].update({"last_modifier": "sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com"}),
    "provisioner in scheduler creator field": lambda p: _first(p, "google_cloud_scheduler_job")["change"]["after"].update({"creator": PL.PROVISIONER_SA}),
    "provisioner in dataset field": lambda p: _first(p, "google_bigquery_dataset")["change"]["after"].update({"creator": PL.PROVISIONER_SA}),
    "provisioner as IAM member": lambda p: _add(p, _rc(f"{M}.google_secret_manager_secret_iam_member.x", "google_secret_manager_secret_iam_member", {"project": P1, "secret_id": "ozon-seller-api-key", "role": "roles/secretmanager.secretAccessor", "member": f"serviceAccount:{PL.PROVISIONER_SA}"}, module=M)),
    "provisioner email with suffix in creator": lambda p: _job(p)["change"]["after"].update({"creator": PL.PROVISIONER_SA + ".evil"}),
    "platform project path in env": lambda p: _job(p)["change"]["after"]["template"][0]["template"][0]["containers"][0]["env"].append({"name": "X", "value": "projects/mpa-platform/secrets/x"}),
    "platform state bucket in creator": lambda p: _job(p)["change"]["after"].update({"creator": "gs://" + PL.STATE_BUCKET}),
}


@pytest.mark.parametrize("name", sorted(F_NEGATIVE))
def test_computed_applier_exception_is_strictly_bounded(contract, name):
    plan = _converged(_plan_for(contract))
    F_NEGATIVE[name](plan)
    assert PS.scan_plan(plan, contract) != [], name


def test_scanner_accepts_the_same_shape_for_client_002():
    c2 = SY.fixture_contract("client_002")
    assert PS.scan_plan(_plan_for(c2, number="210987654321"), c2) == []


@pytest.mark.parametrize("name", sorted(NEGATIVE))
def test_scanner_negative_control(contract, name):
    plan = _mutated(contract, NEGATIVE[name])
    assert PS.scan_plan(plan, contract) != [], name


# ── мутации сканера: правило несёт нагрузку (без него контроль проходит) ────
class _Everything(set):
    def __contains__(self, item):
        return True


SCANNER_MUTATIONS = {
    "principal allow-list": (lambda mp: (mp.setattr(PS, "expected_iam", lambda c: _Everything()),
                                         mp.setattr(PS, "_PRINCIPAL_PREFIX", re.compile(r"(?!x)x"))),
                             "H1 secret -> allUsers"),
    "reference parser": (lambda mp: mp.setattr(PS, "_REF_PATTERNS", ()), "M1 projects/<other tenant number>"),
    "bucket rule": (lambda mp: mp.setattr(PS, "_BUCKET_REF", re.compile(r"(?!x)x")), "M1 any gs:// bucket"),
    "URL decoding": (lambda mp: mp.setattr(PS, "_decoded_forms", lambda s: [s]), "M1 URL-encoded foreign project"),
    "provider allow-list": (lambda mp: mp.setattr(PS, "ALLOWED_PROVIDERS", _Everything()), "M2 unexpected provider"),
    "unknown-value rule": (lambda mp: mp.setattr(PS, "CRITICAL_FIELDS", {}), "M2 unknown image"),
    "invocation rule": (lambda mp: (mp.setattr(PS, "invocation_grants", lambda plan: []),
                                    mp.setattr(PS, "ALLOWED_MANAGED_TYPES",
                                               PS.ALLOWED_MANAGED_TYPES | {"google_cloud_run_v2_job_iam_member"}),
                                    mp.setattr(PS, "expected_iam", lambda c: _Everything()),
                                    mp.setattr(PS, "RUN_INVOKING_ROLES", frozenset())),
                        "H2 run.invoker to runtime SA"),
    # Провижионера в ACL ловит ещё и правило маркеров платформы (защита в глубину), поэтому
    # нагрузку правила D доказывает запись, которую не видит больше ни одно правило.
    "dataset ACL rule": (lambda mp: mp.setattr(PS, "dataset_acl_findings", lambda *a: []),
                         "D customer user"),
    "SA member exception conditions": (lambda mp: mp.setattr(PS, "_own_sa_member", lambda *a: True),
                                       "E SA member is another email"),
    "data-source allow-list": (lambda mp: mp.setattr(PS, "ALLOWED_DATA_SOURCES",
                                                     dict(PS.ALLOWED_DATA_SOURCES, **{"data.google_projects.evil": "google_projects"})),
                               "M2 unexpected data source (config)"),
}


@pytest.mark.parametrize("rule", sorted(SCANNER_MUTATIONS))
def test_scanner_rule_is_load_bearing(contract, monkeypatch, rule):
    weaken, control = SCANNER_MUTATIONS[rule]
    plan = _mutated(contract, NEGATIVE[control])
    assert PS.scan_plan(plan, contract) != []            # с правилом — пойман
    weaken(monkeypatch)
    assert PS.scan_plan(plan, contract) == [], f"{control}: ловится не только правилом «{rule}»"


# ═════════════════════════════════════ H2 — модель безопасности расписаний
def _module_text():
    return (TENANT_ROOT / "modules" / "ozon_runtime" / "main.tf").read_text(encoding="utf-8")


def test_base_provisioning_grants_nobody_the_right_to_invoke_cloud_run():
    code = "\n".join(l for l in _module_text().splitlines() if not l.strip().startswith("#"))
    assert "google_cloud_run_v2_job_iam" not in code
    assert "roles/run." not in code
    members = re.findall(r"(?m)^\s+member\s*=\s*(.+)$", code)
    assert members and all(m.strip() == "local.runtime_member" for m in members), members
    assert "scheduler_email" not in "".join(members)


def test_expected_plan_has_no_invocation_capability(contract):
    assert PS.invocation_grants(_plan_for(contract)) == []


def test_failed_pause_still_cannot_start_ingestion(contract):
    """Пауза не встала (задание ENABLED): сканер это видит, но вызвать job'ы всё равно некому."""
    plan = _mutated(contract, NEGATIVE["H2 unpaused scheduler"])
    assert any("не на паузе" in f for f in PS.scan_plan(plan, contract))
    assert PS.invocation_grants(plan) == []
    runtime_roles = {rc["change"]["after"]["role"] for rc in plan["resource_changes"]
                     if rc["type"].endswith("_iam_member")}
    assert runtime_roles.isdisjoint(PS.RUN_INVOKING_ROLES)


def test_invocation_grant_is_detected_when_present(contract):
    plan = _mutated(contract, NEGATIVE["H2 scheduler invoker binding"])
    assert PS.invocation_grants(plan)


def test_scheduler_targets_and_identities_are_plan_time_known_and_exact():
    code = _module_text()
    assert 'uri         = "${local.run_v2_base}/${each.key}:run"' in code
    assert "service_account_email = local.scheduler_email" in code
    assert "service_account = local.runtime_email" in code


# ═════════════════════════════════════ M3 — биллинг
def _info(**kw):
    base = {"name": f"projects/{P1}/billingInfo", "projectId": P1,
            "billingAccountName": "billingAccounts/000000-000000-000000", "billingEnabled": True}
    base.update(kw)
    return lambda pid: base


def test_billing_enabled_passes():
    TI.check_billing_enabled(P1, fetch=_info())


@pytest.mark.parametrize("case", [
    {"billingEnabled": False}, {"billingEnabled": None}, {"billingEnabled": "true"},
    {"billingAccountName": ""}, {"billingAccountName": None}, {"projectId": "mpa-t-client-002"},
])
def test_billing_not_enabled_or_malformed_fails_closed(case):
    info = _info(**case)
    if "billingEnabled" in case and case["billingEnabled"] is None:
        info = (lambda f: (lambda pid: {k: v for k, v in f(pid).items() if k != "billingEnabled"}))(info)
    with pytest.raises(TI.TenantInfraError):
        TI.check_billing_enabled(P1, fetch=info)


def test_billing_read_error_fails_closed():
    def boom(_pid):
        raise PermissionError("403")
    with pytest.raises(TI.TenantInfraError):
        TI.check_billing_enabled(P1, fetch=boom)


def test_terraform_billing_guard_is_null_safe_and_plan_runs_precheck_first():
    g = (TENANT_ROOT / "guards.tf").read_text(encoding="utf-8")
    assert "try(length(data.google_project.tenant.billing_account) > 0, false)" in g
    assert 'try(data.google_project.tenant.billing_account, "") != ""' not in g
    src = inspect.getsource(TI.plan)
    assert src.index("check_billing_enabled") < src.index('"init"')


# ═════════════════════════════════════ находка 16 — guard без тавтологии
def test_project_guard_uses_independent_facts_not_a_tautology():
    g = (TENANT_ROOT / "guards.tf").read_text(encoding="utf-8")
    assert "data.google_project.tenant.project_id == var.contract.project_id" not in g
    for fact in ("local.found.project_id == var.contract.project_id", "local.found.parent.id",
                 "local.found.number == data.google_project.tenant.number",
                 "data.google_project.tenant.folder_id == local.platform.tenants_folder_id",
                 "lifecycleState:ACTIVE"):
        assert fact in g, fact


# ═════════════════════════════════════ И1 — секреты вне state
PAYLOAD_TOKENS = ("google_secret_manager_secret_version", "secret_data", "secret_string", "payload",
                  "secret_value")


def test_secret_state_invariant_no_version_resources_or_payload_fields():
    for p in TF_FILES:
        code = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
        for tok in PAYLOAD_TOKENS:
            assert tok not in code, (p.name, tok)
        assert not re.search(r"sensitive\s*=\s*true", code), p.name


def test_secret_state_invariant_only_the_contract_variable_enters_the_root():
    root_vars = re.findall(r'variable "(\w+)"', "\n".join(p.read_text() for p in TF_FILES if p.parent == TENANT_ROOT))
    assert root_vars == ["contract"]
    names = set(N.DEDICATED_OZON_SECRET_IDS.values())
    secretish = re.compile(r"secret|api_key|token|password|credential", re.I)

    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                if secretish.search(k):
                    vals = set(v.values()) if isinstance(v, dict) else {v}
                    assert vals <= names, (f"{path}.{k}", v)       # только ИМЕНА секретов
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(R.terraform_inputs("client_001"))
    blob = json.dumps(R.terraform_inputs("client_001"))
    for tok in ("secret_data", "secret_string", "password"):
        assert tok not in blob, tok


def test_secret_state_invariant_is_explicit_in_architecture_docs():
    adr = (REPO / "docs/architecture/ADR-08_TENANT_ISOLATION.md").read_text(encoding="utf-8")
    design = (REPO / "docs/architecture/TENANCY_DESIGN.md").read_text(encoding="utf-8")
    for phrase in ("никогда** — версиями секретов", "google_secret_manager_secret_version",
                   "вне Terraform и вне его state"):
        assert phrase in adr, phrase
    assert "ADR-08 И1" in design and "ADR-08 И2" in design


# ═════════════════════════════════════ L1b — ложные утверждения
FALSE_CLAIMS = ("создать на паузе", "запускать — нет", "без чтения строк, запуска job'ов, снятия паузы, удаления",
                "обязаны создаваться на паузе", "no execution, no delete", "код не исполнить без run/enable",
                "расписания только PAUSED", "расписания на паузе)")


def test_no_false_security_claims_remain_in_the_pr_files():
    files = [p for p in TF_FILES] + [REPO / f for f in (
        "tools/tenancy/iam_proposals.py", "docs/architecture/TENANCY_DESIGN.md",
        "docs/architecture/ADR-08_TENANT_ISOLATION.md", "docs/architecture/TECH_DEBT.md", "CHANGELOG.md",
        "infra/tenant/tests/tenant.tftest.hcl")]
    for f in files:
        text = f.read_text(encoding="utf-8")
        for claim in FALSE_CLAIMS:
            assert claim not in text, (f.name, claim)


# ═════════════════════════════════════ M4 — предложение IAM
def test_every_proposed_permission_is_classified_once():
    perms = [p for p, _c, _w, _r in I.PROVISIONER_PERMISSIONS]
    assert len(perms) == len(set(perms))
    assert {c for _p, c, _w, _r in I.PROVISIONER_PERMISSIONS} <= {I.REQUIRED_NOW, I.REQUIRED_T3_3_ONLY,
                                                                   I.NEEDS_LIVE_PROOF, I.REMOVE}
    removed = {p for p, c, _w, _r in I.PROVISIONER_PERMISSIONS if c == I.REMOVE}
    assert removed.isdisjoint(I.PROVISIONER_ROLE["permissions"])
    for p in I.PROVISIONER_ROLE["permissions"]:
        assert not any(p.startswith(n) for n in I.NEVER_FOR_PROVISIONER), p


def test_review_flagged_permissions_are_removed_or_explained():
    cls = {p: c for p, c, _w, _r in I.PROVISIONER_PERMISSIONS}
    for p in ("run.jobs.setIamPolicy", "bigquery.tables.update", "secretmanager.secrets.update",
              "cloudscheduler.jobs.update", "iam.serviceAccounts.update", "bigquery.tables.list"):
        assert cls[p] == I.REMOVE, p
    assert cls["serviceusage.operations.get"] == I.NEEDS_LIVE_PROOF
    assert cls["cloudscheduler.jobs.create"] == I.REQUIRED_T3_3_ONLY
    risk = {p: r for p, _c, _w, r in I.PROVISIONER_PERMISSIONS}
    assert "ИСПОЛНЕНИЕ" in risk["cloudscheduler.jobs.create"]


def test_capabilities_are_stated_honestly():
    indirect = " ".join(I.CAPABILITIES["INDIRECT"])
    assert "ИСПОЛНЕНИЕ" in indirect and "EXPORT DATA" in indirect and "ENABLED" in indirect
    assert I.LIVE_T3_1B_EXCESS and "run.jobs.run" in I.LIVE_T3_1B_EXCESS[0]
    assert "cannot execute" not in I.PROVISIONER_ROLE["title"].lower()


def test_policy_member_containment_is_option_b_with_owner_and_org_set():
    c = I.POLICY_MEMBER_CONTAINMENT
    params = c["spec"]["spec"]["rules"][0]["parameters"]
    assert c["chosen"] == "B" and c["spec"]["name"].startswith(PL.TENANTS_FOLDER)
    assert params["allowedPrincipalSets"] == [f"//cloudresourcemanager.googleapis.com/organizations/{PL.ORGANIZATION_ID}"]
    assert params["allowedMemberSubjects"] == ["user:evelagin@gmail.com"]
    assert any("EVETIS" in x for x in c["does_not_block"])


# ═════════════════════════════════════ M5 / L2 / L4 — цепочка поставки и workflow
def _wf():
    return WORKFLOW.read_text(encoding="utf-8")


def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment():
    uses = re.findall(r"uses:\s*(\S+)(.*)", _wf())
    assert {r.split("@")[0] for r, _ in uses} == {"actions/checkout", "actions/setup-python", "hashicorp/setup-terraform",
                                                "google-github-actions/auth", "actions/upload-artifact",
                                                "actions/download-artifact"}
    assert len({r for r, _ in uses}) == 6                      # одна закреплённая версия на action
    for ref, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.search(r"#\s*v\d+\.\d+\.\d+", comment), ref


def test_workflow_permissions_are_minimal_per_job():
    from tools.tests.test_tenancy_t32 import _job
    wf = _wf()
    assert re.search(r"\npermissions:\n  contents: read\n  id-token: write\n\n", wf)      # верхний уровень не менялся
    assert "permissions:" not in _job(wf, "plan") and "permissions:" not in _job(wf, "sql")
    assert "    permissions:\n      contents: read\n      actions: read\n    env:" in _job(wf, "verify")
    assert "    permissions:\n      contents: read\n      id-token: write\n    env:" in _job(wf, "apply")
    assert re.findall(r"(?m)^\s+([a-z-]+): write$", wf) == ["id-token", "id-token"]  # только id-token (верх и apply)
    runs = "\n".join(re.findall(r"run: (.+)", _job(wf, "plan")))
    assert "apply" not in runs and "tenant_infra.py plan" in runs
    assert "secrets." not in wf and "pull_request" not in wf


def test_terraform_init_uses_the_committed_lockfile_only():
    assert '"-lockfile=readonly"' in inspect.getsource(TI.plan)


def _shape_script():
    wf = _wf()
    blk = wf.split("- name: tenant_id shape (before anything else)")[1].split("run: |\n", 1)[1]
    blk = blk.split("      - uses:")[0]
    return "\n".join(line[10:] for line in blk.splitlines())


@pytest.mark.parametrize("value,ok", [
    ("client_001", True), ("abc", True), ("client_001\n$(id)", False), ("x\nclient_001", False),
    ("client_001\r", False), ("client_001;id", False), ("CLIENT_001", False), ("client-001", False),
    ("", False), ("a" * 32, False)])
def test_workflow_shell_check_rejects_multiline_and_malformed_ids(value, ok):
    r = subprocess.run(["bash", "-c", _shape_script()], env={"TENANT_ID": value, "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert (r.returncode == 0) is ok, (value, r.stdout, r.stderr)


def test_wif_hardening_is_proposed_not_claimed_live():
    assert I.WIF_HARDENING["status"] == "NOT_APPLIED"
    assert I.WIF_HARDENING["live_condition"] == PL.WIF_ATTRIBUTE_CONDITION
    assert "job_workflow_ref" not in PL.WIF_ATTRIBUTE_CONDITION
    assert PL.PROPOSED_WIF_ATTRIBUTE_CONDITION == (
        PL.WIF_ATTRIBUTE_CONDITION + " && assertion.job_workflow_ref == assertion.workflow_ref")


# ═════════════════════════════════════ M6 — доверенная база AE
TENANT_TCB = ["tools/tenancy/plan_scan.py", "tools/tenancy/tenant_infra.py", "tools/tenancy/platform.py",
              "tools/tenancy/registry.py", "tools/tenancy/naming.py", "tenants/client_001/tenant.json",
              "tenants/_schema/tenant.schema.json", "pipelines/ozon/schema/ozon_raw/RAW_OZON_STOCKS.json",
              "infra/tenant/main.tf", "infra/tenant/runtime_release.json", ".github/workflows/tenant-infra.yml"]
GOOD = {"tests": [{"name": "t", "status": "PASS"}], "sql_validation": {"status": "PASS"},
        "runtime_access": {"status": "NOT_APPLICABLE"}, "parity": {"status": "NOT_APPLICABLE"},
        "data_suites": {}, "objective_resolution": "NOT_APPLICABLE"}


@pytest.mark.parametrize("path", TENANT_TCB)
def test_tenant_provisioning_paths_are_tcb(path):
    assert (REPO / path).exists(), path
    assert tcb_paths([path]) == [path]


@pytest.mark.parametrize("path", TENANT_TCB)
def test_autonomous_change_to_tenant_paths_is_never_self_approved(path):
    r = G.evaluate(GOOD, GOOD, F.verdict("PASS"), {"tcb_paths": tcb_paths([path])})
    assert r["verdict"] == "HUMAN_DECISION_REQUIRED"
    assert any("TCB_MODIFICATION" in x for x in r["reasons"]["HUMAN_DECISION_REQUIRED"])


# ═════════════════════════════════════ L3 — нижние уровни без обходов
def test_production_export_accepts_only_a_tenant_id():
    assert list(inspect.signature(R.terraform_inputs).parameters) == ["tenant_id"]
    assert list(inspect.signature(TI.contract_for).parameters) == ["tenant_id"]
    assert list(inspect.signature(TI.render).parameters) == ["tenant_id", "out_dir"]
    src = (REPO / "tools/tenancy/tenant_infra.py").read_text(encoding="utf-8")
    assert "_terraform_contract" not in src and "load_tenant(" not in src


def test_private_contract_builder_is_used_only_by_registry_and_synthetic_fixtures():
    users = [p.relative_to(REPO).as_posix() for p in (REPO / "tools").rglob("*.py")
             if "_terraform_contract(" in p.read_text(encoding="utf-8")]
    assert sorted(users) == ["tools/tenancy/registry.py", "tools/tenancy/synthetic.py",
                             "tools/tests/test_tenancy_t32.py", "tools/tests/test_tenancy_t32_remediation.py"], users


# ═════════════════════════════════════ находка 15 — префиксы state в условиях IAM
def test_state_iam_prefix_has_a_trailing_slash_so_abc_cannot_cover_abc_x():
    assert N.terraform_state_prefix("abc") == "tenants/abc"                 # ключ backend не меняется
    assert N.terraform_state_iam_prefix("abc") == "tenants/abc/"
    abc_x_state = N.terraform_state_prefix("abc_x") + "/default.tfstate"
    assert abc_x_state.startswith(N.terraform_state_prefix("abc"))           # опасность без слэша
    assert not abc_x_state.startswith(N.terraform_state_iam_prefix("abc"))   # правило её закрывает
    assert (N.terraform_state_prefix("abc") + "/default.tfstate").startswith(N.terraform_state_iam_prefix("abc"))


# ═════════════════════════════════════ мутационный прогон Terraform подключён
def test_terraform_mutation_check_is_wired_into_ci_and_covers_the_critical_rules():
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python3 tools/tenancy/tf_mutation_check.py" in ci
    from tools.tenancy import tf_mutation_check as TM
    names = " ".join(n for n, *_ in TM.MUTATIONS)
    for rule in ("billing", "folder", "search", "number", "PAUSED", "image", "EVETIS", "prefix", "namespace",
                 "unpaused"):
        assert rule in names, rule
