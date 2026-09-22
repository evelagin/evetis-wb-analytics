#!/usr/bin/env python3
"""Собрать машиночитаемую карту платформы EVETIS Analytics.

Только чтение. Источники:
  * BigQuery — `INFORMATION_SCHEMA` и `__TABLES__` через read-only клиент
    (`tools/lib/bq_readonly.py`): датасеты, объекты, схемы, тела вью, процедуры;
  * репозиторий — реестр загрузчиков, Terraform, Apps Script, CI, sql/current;
  * (опционально, `--with-gcloud`) Cloud Scheduler и Cloud Run Jobs через `gcloud`.

Результат — `docs/architecture/system_inventory.json`: детерминированный,
отсортированный, дифф-пригодный. Значений бизнес-данных в нём нет: только
метаданные, счётчики строк и имена объектов. Зависимости между объектами
выводятся из тел вью, а не из имён файлов.

Запуск:
  python tools/architecture_baseline.py --project <PROJECT> \
      --token-command "gcloud auth print-access-token" --with-gcloud
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bq_readonly import BigQueryError, ReadOnlyBigQuery, resolve_token, strip_sql_comments  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "architecture" / "system_inventory.json"

DATASETS = [
    "wb_raw", "wb_mart", "wb_ops",
    "evetis_ref", "evetis_ops", "evetis_mart", "evetis_communications",
    "ozon_raw", "ozon_stg", "ozon_mart",
]
# Датасет -> домен. Изоляция маркетплейсов — инвариант проекта, не стиль:
# пересечение wb_* и ozon_* вне общего evetis_ref считается дефектом архитектуры.
DOMAIN = {
    "wb_raw": "wb", "wb_mart": "wb", "wb_ops": "wb",
    "ozon_raw": "ozon", "ozon_stg": "ozon", "ozon_mart": "ozon",
    "evetis_ref": "shared", "evetis_ops": "shared",
    "evetis_mart": "shared", "evetis_communications": "shared",
}
LAYER_RULES = [
    (re.compile(r"^RAW_"), "raw"),
    (re.compile(r"^(FACT_|FCT_|MART_|EXECUTIVE_|SKU_PERFORMANCE_)"), "mart"),
    (re.compile(r"^(REF_|CT_|OPS_CONFIG)"), "reference"),
    (re.compile(r"^V_DASH_"), "dashboard"),
    (re.compile(r"^V_OPS_SHEET_"), "sheet_contract"),
    (re.compile(r"^V_"), "semantic"),
    (re.compile(r"(_RUNS|_LOG|_LOCK|OBSERVATIONS|SNAPSHOTS|_STATE|_EVENT|_INCIDENT)$"), "operational"),
    (re.compile(r"^BAK_|_BAK_"), "backup"),
]
REF_RE = re.compile(
    r"`?(?:[A-Za-z0-9_-]+\.)?`?(" + "|".join(DATASETS) + r")`?\.`?([A-Za-z0-9_]+)`?"
)


def layer_of(name: str) -> str:
    for rx, layer in LAYER_RULES:
        if rx.search(name):
            return layer
    return "other"


# ---------------------------------------------------------------- BigQuery ---

def collect_bigquery(bq: ReadOnlyBigQuery) -> dict:
    union = " UNION ALL ".join(
        f"SELECT '{d}' AS ds, table_id, row_count, size_bytes, last_modified_time "
        f"FROM `{bq.project}.{d}.__TABLES__`"
        for d in DATASETS
    )
    storage = {(r["ds"], r["table_id"]): r for r in bq.query(union)}

    union = " UNION ALL ".join(
        f"SELECT '{d}' AS ds, table_name, table_type, "
        f"IFNULL(ddl,'') AS ddl FROM `{bq.project}.{d}`.INFORMATION_SCHEMA.TABLES"
        for d in DATASETS
    )
    tables = bq.query(union)

    union = " UNION ALL ".join(
        f"SELECT '{d}' AS ds, table_name, view_definition "
        f"FROM `{bq.project}.{d}`.INFORMATION_SCHEMA.VIEWS"
        for d in DATASETS
    )
    view_bodies = {(r["ds"], r["table_name"]): (r["view_definition"] or "") for r in bq.query(union)}

    union = " UNION ALL ".join(
        f"SELECT '{d}' AS ds, table_name, column_name, data_type, is_nullable, ordinal_position "
        f"FROM `{bq.project}.{d}`.INFORMATION_SCHEMA.COLUMNS"
        for d in DATASETS
    )
    cols = defaultdict(list)
    for r in bq.query(union, max_results=200000):
        # Псевдоколонки партиционирования (_PARTITIONTIME и т. п.) приходят с
        # ordinal_position = NULL: это не колонки схемы, в инвентарь они не идут.
        if r["ordinal_position"] is None:
            continue
        cols[(r["ds"], r["table_name"])].append(
            (int(r["ordinal_position"]), r["column_name"], r["data_type"], r["is_nullable"])
        )

    union = " UNION ALL ".join(
        f"SELECT '{d}' AS ds, routine_name, routine_type, "
        f"UNIX_SECONDS(last_altered) AS altered, LENGTH(routine_definition) AS body_len, "
        f"IFNULL(routine_definition,'') AS body "
        f"FROM `{bq.project}.{d}`.INFORMATION_SCHEMA.ROUTINES"
        for d in DATASETS
    )
    routines = bq.query(union)

    # Кто ЧИТАЕТ объекты снаружи BigQuery. Без этого «потребитель» остаётся
    # догадкой: граф вью показывает только внутренние связи, а дашборды и листы
    # ходят в витрину извне. Источник — история заданий, а не предположение.
    consumption = collect_consumption(bq)

    objects: dict[str, dict] = {}
    for t in tables:
        ds, name = t["ds"], t["table_name"]
        key = f"{ds}.{name}"
        st = storage.get((ds, name), {})
        # "<имя> <ТИП> NULL|NOT NULL" — одна колонка на строку JSON: и машине
        # разбирать однозначно, и дифф в PR читается построчно.
        schema = [
            f"{c[1]} {c[2]} {'NULL' if c[3] == 'YES' else 'NOT NULL'}"
            for c in sorted(cols.get((ds, name), []))
        ]
        ddl = t.get("ddl") or ""
        objects[key] = {
            "object": key,
            "dataset": ds,
            "name": name,
            "domain": DOMAIN[ds],
            "layer": layer_of(name),
            "type": t["table_type"],
            "columns": len(schema),
            "schema": schema,
            "row_count": int(st["row_count"]) if st.get("row_count") is not None else None,
            "size_bytes": int(st["size_bytes"]) if st.get("size_bytes") is not None else None,
            "last_modified": _iso_ms(st.get("last_modified_time")),
            "partitioned_by": _partition_of(ddl),
            "clustered_by": _cluster_of(ddl),
            "depends_on": [],
            "consumed_by": [],
        }

    # Зависимости — из тел вью. Это факт исполняемого SQL, а не догадка по имени.
    for (ds, name), body in sorted(view_bodies.items()):
        key = f"{ds}.{name}"
        if key not in objects:
            continue
        deps = sorted({
            f"{d}.{t}" for d, t in REF_RE.findall(strip_sql_comments(body)) if f"{d}.{t}" != key
        })
        objects[key]["depends_on"] = deps
    for key, obj in objects.items():
        for dep in obj["depends_on"]:
            if dep in objects:
                objects[dep]["consumed_by"].append(key)
    for obj in objects.values():
        obj["consumed_by"] = sorted(set(obj["consumed_by"]))

    routine_list = []
    for r in routines:
        body = r.get("body") or ""
        refs = sorted({f"{d}.{t}" for d, t in REF_RE.findall(strip_sql_comments(body))})
        routine_list.append({
            "routine": f"{r['ds']}.{r['routine_name']}",
            "dataset": r["ds"],
            "domain": DOMAIN[r["ds"]],
            "type": r["routine_type"],
            "body_length": int(r["body_len"] or 0),
            "last_altered": _iso_s(r.get("altered")),
            "references": refs,
        })
    for key, obj in objects.items():
        obj["external_readers"] = consumption.get(key, {})

    routine_list.sort(key=lambda x: x["routine"])
    return {"objects": objects, "routines": routine_list,
            "consumption_window_days": CONSUMPTION_WINDOW_DAYS}


CONSUMPTION_WINDOW_DAYS = 30


def collect_consumption(bq: ReadOnlyBigQuery) -> dict[str, dict]:
    """Обращения к объектам за окно, по идентичности читателя.

    `INFORMATION_SCHEMA.JOBS_BY_PROJECT` может быть недоступен ограниченной
    учётной записи — тогда карта потребления пуста, и это записывается как
    отсутствие данных, а не как отсутствие потребителей.
    """
    pattern = "|".join(DATASETS)
    sql = f"""
    WITH j AS (
      SELECT user_email, query, creation_time
      FROM `{bq.project}.region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
      WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {CONSUMPTION_WINDOW_DAYS} DAY)
        AND job_type = 'QUERY' AND statement_type = 'SELECT' AND query IS NOT NULL
    ), r AS (
      SELECT
        CASE WHEN user_email LIKE 'metabase-read-only@%' THEN 'metabase'
             WHEN user_email LIKE '%gserviceaccount.com' THEN 'service_account'
             ELSE 'human_or_script' END AS reader,
        obj, creation_time
      FROM j, UNNEST(REGEXP_EXTRACT_ALL(query, r'((?:{pattern})\\.[A-Za-z0-9_]+)')) AS obj
    )
    SELECT obj, reader, COUNT(*) AS queries,
           FORMAT_DATE('%F', MAX(DATE(creation_time))) AS last_read
    FROM r GROUP BY obj, reader
    """
    try:
        rows = bq.query(sql, max_results=50000)
    except BigQueryError:
        return {}
    out: dict[str, dict] = defaultdict(dict)
    for r in rows:
        out[r["obj"]][r["reader"]] = {"queries": int(r["queries"]), "last_read": r["last_read"]}
    return dict(out)


def _partition_of(ddl: str) -> str | None:
    m = re.search(r"PARTITION BY\s+([^\n]+)", ddl or "")
    return m.group(1).strip().rstrip(",") if m else None


def _cluster_of(ddl: str) -> list[str]:
    m = re.search(r"CLUSTER BY\s+([^\n]+)", ddl or "")
    return [p.strip().strip("`") for p in m.group(1).split(",")] if m else []


def _iso_ms(v) -> str | None:
    if v in (None, ""):
        return None
    return dt.datetime.fromtimestamp(int(v) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_s(v) -> str | None:
    if v in (None, ""):
        return None
    return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -------------------------------------------------------------- repository ---

def collect_repository() -> dict:
    out: dict = {}

    registry = (REPO / "cloud" / "src" / "loaders" / "registry.ts").read_text(encoding="utf-8")
    loaders = []
    for m in re.finditer(r"^\s{2}(\w+):\s*\{(.+?)\},\s*$", registry, re.M | re.S):
        spec = m.group(2)
        loaders.append({
            "loader": m.group(1),
            "prod_only": "prodOnly: true" in spec,
            "source": "cloud/src/loaders/registry.ts",
        })
    out["cloud_run_loaders"] = sorted(loaders, key=lambda x: x["loader"])

    tf = []
    tfdir = REPO / "infra" / "terraform"
    for f in sorted(tfdir.glob("*.tf")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'^resource\s+"([^"]+)"\s+"([^"]+)"', text, re.M):
            tf.append({"type": m.group(1), "name": m.group(2), "file": f"infra/terraform/{f.name}"})
    out["terraform_resources"] = tf
    out["terraform_resource_count"] = len(tf)

    gs = sorted((REPO / "apps-script").rglob("*.gs"))
    out["apps_script"] = {
        "file_count": len(gs),
        "projects": sorted({p.relative_to(REPO / "apps-script").parts[0] for p in gs}),
        "in_ci": False,  # ни один workflow не трогает apps-script/**
        "note": "Production-загрузка WB. Ни одного автоматического теста и ни одной CI-проверки.",
    }

    wf = []
    for f in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        text = f.read_text(encoding="utf-8")
        wf.append({
            "workflow": f.name,
            "triggers": sorted(set(re.findall(r"^\s{2}(pull_request|push|workflow_dispatch|schedule):", text, re.M))),
        })
    out["ci_workflows"] = wf

    canon = {}
    for man in sorted((REPO / "sql" / "current").glob("*/MANIFEST.json")):
        data = json.loads(man.read_text(encoding="utf-8"))
        canon[data["dataset"]] = {
            "objects": sorted(o["object_name"] for o in data["objects"]),
            "count": len(data["objects"]),
            "manifest_version": data.get("manifest_version"),
        }
    out["canonical_sql_layer"] = canon

    out["sql_files_total"] = len(list((REPO / "sql").rglob("*.sql")))
    return out


# ------------------------------------------------------------ orchestration ---

def collect_gcloud(project: str, region: str) -> dict:
    def run(args: list[str]) -> list[dict]:
        done = subprocess.run(args, capture_output=True, text=True, shell=False)
        if done.returncode != 0:
            raise BigQueryError(f"gcloud завершился кодом {done.returncode}: {done.stderr[:300]}")
        return json.loads(done.stdout or "[]")

    schedulers = []
    for j in run(["gcloud", "scheduler", "jobs", "list", f"--location={region}",
                  f"--project={project}", "--format=json"]):
        uri = j.get("httpTarget", {}).get("uri", "")
        body = j.get("httpTarget", {}).get("body")
        target_kind = "cloud_run_job" if "run.googleapis.com" in uri else (
            "bigquery_job" if "bigquery" in uri else "http_service")
        statement = None
        if body:
            import base64
            try:
                statement = json.loads(base64.b64decode(body)).get(
                    "configuration", {}).get("query", {}).get("query")
            except Exception:
                statement = None
        schedulers.append({
            "scheduler": j["name"].split("/")[-1],
            "state": j.get("state"),
            "schedule": j.get("schedule"),
            "timezone": j.get("timeZone"),
            "target_kind": target_kind,
            "bigquery_statement": statement,
        })
    schedulers.sort(key=lambda x: x["scheduler"])

    jobs = []
    for j in run(["gcloud", "run", "jobs", "list", f"--region={region}",
                  f"--project={project}", "--format=json"]):
        # run.googleapis.com/v1: spec.template.spec.template.spec.containers[].
        # Ошибиться путём легко, и ошибка тихая: пустой контейнер выглядит как
        # «образ не закреплён по digest» у всех job сразу.
        spec = (j.get("spec", {}).get("template", {}).get("spec", {})
                 .get("template", {}).get("spec", {}).get("containers") or [{}])[0]
        image = spec.get("image") or ""
        if not image:
            raise BigQueryError(
                f"не удалось прочитать образ Cloud Run Job {j['metadata']['name']!r}: "
                "изменилась форма ответа gcloud, инвентарь был бы ложным"
            )
        jobs.append({
            "job": j["metadata"]["name"],
            "args": spec.get("args", []),
            "image_repository": image.split("@")[0].split(":")[0],
            "image_digest_pinned": "@sha256:" in image,
        })
    jobs.sort(key=lambda x: x["job"])

    services = []
    for s in run(["gcloud", "run", "services", "list", f"--project={project}", "--format=json"]):
        services.append({
            "service": s["metadata"]["name"],
            "region": s["metadata"]["labels"].get("cloud.googleapis.com/location"),
            "ingress": s["metadata"]["annotations"].get("run.googleapis.com/ingress", "all"),
        })
    services.sort(key=lambda x: x["service"])
    return {"cloud_scheduler": schedulers, "cloud_run_jobs": jobs, "cloud_run_services": services}


# -------------------------------------------------------------------- main ---

def build(args) -> dict:
    token = resolve_token(args.token_env, args.token_command, os.environ)
    bq = ReadOnlyBigQuery(project=args.project, token=token, location=args.location,
                          host=args.bq_host, label_purpose="architecture-baseline")
    warehouse = collect_bigquery(bq)
    repo = collect_repository()
    orchestration = {}
    orchestration_error = None
    if args.with_gcloud:
        try:
            orchestration = collect_gcloud(args.project, args.region)
        except Exception as e:  # gcloud недоступен — это факт, а не повод врать в отчёте
            orchestration_error = str(e)[:300]

    objects = warehouse["objects"]
    by_dataset = defaultdict(lambda: {"BASE TABLE": 0, "VIEW": 0})
    for o in objects.values():
        by_dataset[o["dataset"]][o["type"]] += 1

    canon = repo["canonical_sql_layer"]
    covered = {f"{ds}.{n}" for ds, d in canon.items() for n in d["objects"]}
    views = {k for k, o in objects.items() if o["type"] == "VIEW"}

    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "tools/architecture_baseline.py",
        "project": args.project,
        "location": args.location,
        "read_only": True,
        "summary": {
            "datasets": len(DATASETS),
            "objects_total": len(objects),
            "base_tables": sum(1 for o in objects.values() if o["type"] == "BASE TABLE"),
            "views": len(views),
            "routines": len(warehouse["routines"]),
            "dependency_edges": sum(len(o["depends_on"]) for o in objects.values()),
            "views_under_canonical_sql": len(views & covered),
            "views_without_canonical_sql": len(views - covered),
            "terraform_resources": repo["terraform_resource_count"],
            "apps_script_files": repo["apps_script"]["file_count"],
            "bigquery_queries_issued": bq.queries_issued,
            "objects_read_by_metabase": sum(
                1 for o in objects.values() if "metabase" in (o.get("external_readers") or {})),
        },
        "datasets": {
            ds: {"domain": DOMAIN[ds], **dict(by_dataset[ds])} for ds in sorted(DATASETS)
        },
        "objects": dict(sorted(objects.items())),
        "routines": warehouse["routines"],
        "repository": repo,
        "orchestration": orchestration,
        "orchestration_error": orchestration_error,
        "views_without_canonical_sql": sorted(views - covered),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True)
    p.add_argument("--location", default="EU")
    p.add_argument("--region", default="europe-west1")
    p.add_argument("--bq-host", default="www.googleapis.com")
    p.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    p.add_argument("--token-command", default=None)
    p.add_argument("--with-gcloud", action="store_true")
    p.add_argument("--output", default=str(DEFAULT_OUT))
    args = p.parse_args(argv)

    try:
        inv = build(args)
    except (BigQueryError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inv, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
                   encoding="utf-8")
    s = inv["summary"]
    print(f"{out}: {s['objects_total']} объектов, {s['views']} вью, "
          f"{s['dependency_edges']} рёбер, {s['bigquery_queries_issued']} запросов к BigQuery")
    if inv["orchestration_error"]:
        print(f"WARNING: оркестрация не собрана: {inv['orchestration_error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
