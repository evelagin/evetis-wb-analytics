"""Эффективные права sa-ae-reader: read-only не по названиям ролей, а по permissions.

Зачем. Предопределённые роли Google меняет без нас: на 2026-09-24 в roles/bigquery.jobUser
оказались dataform.repositories.create, dataform.folders.create и геминай-чат. Первое применение
M3 это пропустило бы, если бы проверка смотрела на имена ролей. Здесь каждая роль раскрывается
до permissions (живое определение), и решение принимается по ним.

Что проверяется для sa-ae-reader:
  * права записи/создания/IAM/выпуска токенов — нет ни одного, кроме bigquery.jobs.create
    (запуск SELECT; DML/DDL отказывает BigQuery: нет tables.create/updateData ни на одном датасете);
  * «спящие» права, которым нужна запись в цель (createSnapshot, replicateData, export), —
    допустимы только пока у SA нет записи ни в один датасет;
  * на датасетах — только READER; SA не упомянут в политиках других SA (actAs, tokenCreator);
  * нужное чтение есть: jobs.create, jobs.listAll (аудит), tables.getData на восьми датасетах,
    logging.logEntries.list.

Запуск:  python -m tools.autonomy.iam_check --live      (read-only gcloud + BigQuery REST)
Код 0 — read-only доказан; 1 — найдено лишнее право или не хватает нужного.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request

PROJECT = "project-fa311fc0-4d87-4781-986"
SA = f"sa-ae-reader@{PROJECT}.iam.gserviceaccount.com"
READ_DATASETS = ["wb_raw", "wb_mart", "wb_ops", "evetis_ref", "evetis_ops", "evetis_mart", "ozon_raw", "ozon_mart"]
ALLOWED_ACTIVE = {"bigquery.jobs.create"}
INERT_WITHOUT_WRITE = {"bigquery.tables.createSnapshot", "bigquery.tables.replicateData", "bigquery.tables.export",
                       "bigquery.models.export"}
MUTATION = re.compile(r"\.(create\w*|update\w*|delete|setIamPolicy|insert|patch|undelete|restore|run|execute|actAs|"
                      r"getAccessToken|signBlob|signJwt|getOpenIdToken|implicitDelegation|add|access|write|deploy|"
                      r"pause|resume|cancel|replicateData|export|chat|use\w*|invoke)$")
REQUIRED_PROJECT = {"bigquery.jobs.create", "bigquery.jobs.listAll", "logging.logEntries.list"}
REQUIRED_DATASET = {"bigquery.tables.getData", "bigquery.tables.get", "bigquery.tables.list", "bigquery.datasets.get"}
# Права чтения, похожие на мутацию по суффиксу, но ничего не меняющие.
READ_LIKE = {"logging.queries.usePrivate"}


def evaluate(snap: dict) -> dict:
    roles: dict[str, list[str]] = snap["role_permissions"]
    proj_perm = set().union(*(roles[r] for r in snap["project_roles"])) if snap["project_roles"] else set()
    findings, ds_perm = [], {}
    for ds, role in snap["dataset_roles"].items():
        if role != "READER":
            findings.append(f"{ds}: роль {role}, допустим только READER")
        ds_perm[ds] = set(roles["roles/bigquery.dataViewer"]) if role == "READER" else set()
    effective = proj_perm | set().union(*ds_perm.values()) if ds_perm else proj_perm
    writes_anywhere = any(r != "READER" for r in snap["dataset_roles"].values())
    for p in sorted(effective):
        if p in ALLOWED_ACTIVE or p in READ_LIKE:
            continue
        if p in INERT_WITHOUT_WRITE and not writes_anywhere:
            continue
        if MUTATION.search(p):
            findings.append(f"право с побочным эффектом: {p}")
    if snap["referenced_by_other_sa"]:
        findings.append(f"SA упомянут в политиках других SA: {snap['referenced_by_other_sa']}")
    missing = sorted(REQUIRED_PROJECT - proj_perm)
    for ds in READ_DATASETS:
        if ds not in snap["dataset_roles"]:
            missing.append(f"{ds}: нет доступа")
        else:
            missing += [f"{ds}: {p}" for p in sorted(REQUIRED_DATASET - ds_perm[ds])]
    extra_ds = sorted(set(snap["dataset_roles"]) - set(READ_DATASETS))
    if extra_ds:
        findings.append(f"доступ к датасетам вне утверждённых восьми: {extra_ds}")
    status = "PASS" if not findings and not missing and snap["sa_exists"] else "FAIL"
    return {"status": status, "findings": findings, "missing_read": missing,
            "project_roles": sorted(snap["project_roles"]), "effective_permission_count": len(effective)}


def _j(*args, project: bool = True) -> dict | list:
    cmd = ["gcloud", *args, "--format=json"] + ([f"--project={PROJECT}"] if project else [])
    return json.loads(subprocess.check_output(cmd, text=True))


def _describe_role(role: str) -> list[str]:
    if role.startswith("roles/"):                       # предопределённая: без --project
        return _j("iam", "roles", "describe", role, project=False).get("includedPermissions", [])
    return _j("iam", "roles", "describe", role.rsplit("/", 1)[1]).get("includedPermissions", [])


def capture_live() -> dict:
    """Только чтение: политика проекта, ACL всех датасетов, политики всех SA, определения ролей."""
    accounts = [a["email"] for a in _j("iam", "service-accounts", "list")]
    exists = SA in accounts
    pol = _j("projects", "get-iam-policy", PROJECT)
    project_roles = sorted({b["role"] for b in pol.get("bindings", []) if f"serviceAccount:{SA}" in b["members"]})
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

    def bq(path):
        req = urllib.request.Request(f"https://www.googleapis.com/bigquery/v2/projects/{PROJECT}/{path}",
                                     headers={"Authorization": "Bearer " + token})
        return json.load(urllib.request.urlopen(req))
    dataset_roles = {}
    for d in bq("datasets?all=true&maxResults=1000").get("datasets", []):
        ds = d["datasetReference"]["datasetId"]
        for a in bq(f"datasets/{ds}").get("access", []):
            if a.get("userByEmail") == SA or a.get("iamMember") == f"serviceAccount:{SA}":
                dataset_roles[ds] = a["role"]
    refs = []
    for acc in accounts:
        if acc == SA:
            continue
        if SA in json.dumps(_j("iam", "service-accounts", "get-iam-policy", acc)):
            refs.append(acc)
    needed = set(project_roles) | {"roles/bigquery.dataViewer"}
    role_permissions = {}
    for r in sorted(needed):
        role_permissions[r] = _describe_role(r)
    return {"sa_exists": exists, "project_roles": project_roles, "dataset_roles": dataset_roles,
            "referenced_by_other_sa": refs, "role_permissions": role_permissions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true")
    g.add_argument("--snapshot")
    ap.add_argument("--save")
    a = ap.parse_args(argv)
    snap = capture_live() if a.live else json.load(open(a.snapshot, encoding="utf-8"))
    if a.save:
        open(a.save, "w", encoding="utf-8").write(json.dumps(snap, ensure_ascii=False, indent=2) + "\n")
    res = evaluate(snap)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
