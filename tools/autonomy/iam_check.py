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

Анонимный датасет результатов (M6 Phase 1, 2026-09-25). Первый SELECT любого принципала создаёт
скрытый датасет `_<40 hex>` для кэша результатов (документированное поведение BigQuery: таблицы до 24 ч,
доступ только у создателя задания) с ролью OWNER у этого принципала. Он исключается из «записи»
ТОЛЬКО если доказаны все критерии A1–A9 (`recognize_anonymous`); недоказанный критерий — прежний FAIL.
Имя НЕ выводимо из email (проверено), поэтому связь доказывается заданиями (destination_table).
`_script*` (временные датасеты скриптов) этим правилом не покрываются.

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
# Поля ресурса анонимного датасета результатов (живой API, 2026-09-26, 16 из 16 датасетов). Любое иное
# поле (labels, description, friendlyName, defaultEncryptionConfiguration, …) — признак настроенного датасета.
ANON_KEYS = {"access", "creationTime", "datasetReference", "defaultPartitionExpirationMs", "defaultTableExpirationMs",
             "etag", "id", "kind", "lastModifiedTime", "location", "maxTimeTravelHours", "selfLink", "type"}
ANON_EXPIRATION_MS = "86400000"
CREATION_SKEW_MS = 0          # датасет обязан родиться ВНУТРИ окна успешного задания (review L6)
JOBS_LOOKBACK_DAYS = 180


def jobs_region(location: str) -> str:
    return "region-" + location.lower()


def _ms(ts: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def recognize_anonymous(snap: dict, ds: str, sa: str = SA) -> tuple[bool, list[str], dict]:
    """(распознан, недоказанные критерии, доказательства). Любое отсутствие данных — критерий не доказан."""
    ev = (snap.get("anonymous_evidence") or {}).get(ds) or {}
    meta, jobs = ev.get("meta") or {}, ev.get("jobs")
    unmet: list[str] = []

    def need(cid: str, ok: bool) -> None:
        if not ok:
            unmet.append(cid)
    need("A1 имя _<40 hex>", bool(re.fullmatch(r"_[0-9a-f]{40}", ds)))
    need("A2 скрыт (только с all=true)", ds in (snap.get("datasets_all") or []) and
         ds not in (snap.get("datasets_visible") or [ds]))
    need("A3 ACL = один OWNER — проверяемый SA", meta.get("access") == [{"role": "OWNER", "userByEmail": sa}])
    need("A4 нет настроек, кроме полей анонимного датасета", bool(meta) and set(meta) <= ANON_KEYS
         and meta.get("type") == "DEFAULT" and meta.get("datasetReference", {}).get("datasetId") == ds)
    need("A5 истечение таблиц и партиций 24 ч", meta.get("defaultTableExpirationMs") == ANON_EXPIRATION_MS
         and meta.get("defaultPartitionExpirationMs") == ANON_EXPIRATION_MS)
    linked = [j for j in (jobs or {}).get("linked", [])]
    need("A6 есть задания с назначением в датасет, и все — успешные SELECT этого SA", bool(linked) and all(
        j.get("user_email") == sa and j.get("job_type") == "QUERY" and j.get("statement_type") == "SELECT"
        and j.get("state") == "DONE" and not j.get("error_result") for j in linked))
    created = int(meta["creationTime"]) if str(meta.get("creationTime", "")).isdigit() else None
    birth = [j for j in linked if created is not None and j.get("creation_time") and j.get("end_time")
             and _ms(j["creation_time"]) <= created <= _ms(j["end_time"]) + CREATION_SKEW_MS]
    need("A7 датасет создан во время задания этого SA", bool(birth))
    sa_jobs = (jobs or {}).get("sa_jobs")
    need("A8 все задания SA в регионе — QUERY/SELECT", isinstance(sa_jobs, list) and bool(sa_jobs) and all(
        j.get("job_type") == "QUERY" and j.get("statement_type") == "SELECT" for j in sa_jobs))
    need("A9 регион заданий = регион датасета", bool(meta.get("location")) and (jobs or {}).get("region")
         == jobs_region(meta.get("location", "")))
    evidence = {"dataset": ds, "owner": sa, "location": meta.get("location"), "created_ms": created,
                "linked_jobs": len(linked), "birth_job": ({k: birth[0].get(k) for k in ("creation_time", "end_time")}
                                                          if birth else None),
                "criteria_unmet": unmet}
    return not unmet, unmet, evidence


def evaluate(snap: dict) -> dict:
    roles: dict[str, list[str]] = snap["role_permissions"]
    proj_perm = set().union(*(roles[r] for r in snap["project_roles"])) if snap["project_roles"] else set()
    findings, ds_perm, anonymous, rejected = [], {}, [], {}
    for ds, role in snap["dataset_roles"].items():
        if role != "READER":
            ok, unmet, evidence = recognize_anonymous(snap, ds)
            if ok and role == "OWNER":
                anonymous.append(evidence)          # системный кэш результатов этого SA — не запись в данные
                continue
            if re.fullmatch(r"_[0-9a-f]{40}", ds):
                rejected[ds] = unmet
            findings.append(f"{ds}: роль {role}, допустим только READER")
        ds_perm[ds] = set(roles["roles/bigquery.dataViewer"]) if role == "READER" else set()
    anon_ids = {e["dataset"] for e in anonymous}
    effective = proj_perm | set().union(*ds_perm.values()) if ds_perm else proj_perm
    writes_anywhere = any(r != "READER" for d, r in snap["dataset_roles"].items() if d not in anon_ids)
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
    extra_ds = sorted(set(snap["dataset_roles"]) - set(READ_DATASETS) - anon_ids)
    if extra_ds:
        findings.append(f"доступ к датасетам вне утверждённых восьми: {extra_ds}")
    status = "PASS" if not findings and not missing and snap["sa_exists"] else "FAIL"
    return {"status": status, "findings": findings, "missing_read": missing,
            "project_roles": sorted(snap["project_roles"]), "effective_permission_count": len(effective),
            "anonymous_result_datasets": anonymous, "anonymous_not_proven": rejected}


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
    def query(sql):
        body = json.dumps({"query": sql, "useLegacySql": False, "timeoutMs": 60000}).encode()
        req = urllib.request.Request(f"https://www.googleapis.com/bigquery/v2/projects/{PROJECT}/queries", data=body,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req))
        if not r.get("jobComplete"):
            raise RuntimeError("запрос JOBS не завершился — доказательства нет")
        if r.get("pageToken") or int(r.get("totalRows") or 0) != len(r.get("rows", [])):
            raise RuntimeError("ответ JOBS усечён (страницы) — частичное доказательство не принимается")
        names = [f["name"] for f in r["schema"]["fields"]]
        return [dict(zip(names, [c["v"] for c in row["f"]])) for row in r.get("rows", [])]
    dataset_roles, metas = {}, {}
    def listing(path):
        r = bq(path)
        if r.get("nextPageToken"):
            raise RuntimeError("список датасетов усечён (страницы) — частичный снимок не принимается")
        return [d["datasetReference"]["datasetId"] for d in r.get("datasets", [])]
    all_ids = listing("datasets?all=true&maxResults=1000")
    visible = listing("datasets?maxResults=1000")
    for ds in all_ids:
        meta = bq(f"datasets/{ds}")
        for a in meta.get("access", []):
            if a.get("userByEmail") == SA or a.get("iamMember") == f"serviceAccount:{SA}":
                dataset_roles[ds] = a["role"]
                metas[ds] = meta
    anonymous_evidence = {}
    for ds, role in dataset_roles.items():
        if role == "READER" or not re.fullmatch(r"_[0-9a-f]{40}", ds):
            continue
        meta = metas[ds]
        region = jobs_region(meta.get("location", ""))
        if not re.fullmatch(r"region-[a-z0-9-]{2,30}", region):
            continue
        base = (f"FROM `{PROJECT}.{region}.INFORMATION_SCHEMA.JOBS_BY_PROJECT` WHERE creation_time > "
                f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {JOBS_LOOKBACK_DAYS} DAY)")
        cols = ("user_email, job_type, statement_type, state, error_result.reason AS error_result, "
                "FORMAT_TIMESTAMP('%FT%H:%M:%E3SZ', creation_time) AS creation_time, "
                "FORMAT_TIMESTAMP('%FT%H:%M:%E3SZ', end_time) AS end_time")
        anonymous_evidence[ds] = {"meta": meta, "jobs": {
            "region": region,
            "linked": query(f"SELECT {cols} {base} AND destination_table.dataset_id = '{ds}'"),
            "sa_jobs": query(f"SELECT {cols} {base} AND user_email = '{SA}'")}}
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
            "referenced_by_other_sa": refs, "role_permissions": role_permissions,
            "datasets_all": all_ids, "datasets_visible": visible, "anonymous_evidence": anonymous_evidence}


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
