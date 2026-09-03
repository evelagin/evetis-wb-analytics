#!/usr/bin/env python3
"""Исторический bootstrap домена Ozon: GCS JSONL → BigQuery ozon_raw.

Одноразовая загрузка canonical-артефактов Stage 2.1. Не runtime-загрузчик из API:
тот будет отдельно в pipelines/ozon/runtime/.

Идемпотентность: для каждой таблицы данные грузятся во временную staging-таблицу
нативным load job, затем сливаются в целевую через MERGE по натуральному ключу.
Повторный запуск не создаёт дублей и не удваивает суммы.

Секреты не читаются и не логируются: bootstrap не ходит в Ozon API.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT_ID", "project-fa311fc0-4d87-4781-986")
DATASET = os.environ.get("BQ_RAW_DATASET", "ozon_raw")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
BUCKET = os.environ["STAGING_BUCKET"]
PREFIX = os.environ.get("STAGING_PREFIX", "bootstrap_v1")
ONLY = [t for t in os.environ.get("ONLY_TABLES", "").split(",") if t]

# файл → целевая таблица → натуральный ключ
SPEC = {
    "catalog":           ("RAW_OZON_CATALOG",           ["snapshot_date", "sku"]),
    "prices":            ("RAW_OZON_PRICES",            ["snapshot_ts", "offer_id"]),
    "stocks":            ("RAW_OZON_STOCKS",            ["snapshot_date", "sku", "warehouse_id"]),
    "orders_fbo":        ("RAW_OZON_POSTINGS_FBO",      ["posting_number", "sku"]),
    "finance_accrual":   ("RAW_OZON_FINANCE_ACCRUAL",   ["accrual_id", "type_id", "sku"]),
    "ads_campaigns":     ("RAW_OZON_ADS_CAMPAIGNS",     ["snapshot_date", "campaign_id"]),
    "ads_expense_daily": ("RAW_OZON_ADS_EXPENSE_DAILY", ["date", "campaign_id"]),
    "ads_sku_daily":     ("RAW_OZON_ADS_SKU_DAILY",     ["date", "campaign_id", "sku"]),
    # Stage 3.2A — supply/inventory
    "clusters":          ("RAW_OZON_CLUSTERS",          ["snapshot_date", "warehouse_id"]),
    "supply_orders":     ("RAW_OZON_SUPPLY_ORDERS",     ["order_id"]),
    "supplies":          ("RAW_OZON_SUPPLIES",          ["order_id", "supply_id"]),
    "supply_bundles":    ("RAW_OZON_SUPPLY_BUNDLES",    ["bundle_id", "sku"]),
    # новый снимок серии остатков: прошлый снимок НЕ перезаписывается
    "stocks_20260903":   ("RAW_OZON_STOCKS",            ["snapshot_date", "sku", "warehouse_id"]),
}


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def merge_sql(client, target, staging, keys):
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{target}")
    cols = [f.name for f in tgt.schema]
    # NULL-безопасное сравнение: в finance_accrual sku бывает NULL и это валидно
    on = " AND ".join(
        f"COALESCE(CAST(T.{k} AS STRING),'\\x00') = COALESCE(CAST(S.{k} AS STRING),'\\x00')"
        for k in keys)
    setter = ", ".join(f"T.{c} = S.{c}" for c in cols if c not in keys)
    collist = ", ".join(cols)
    srclist = ", ".join(f"S.{c}" for c in cols)
    return (f"MERGE `{PROJECT}.{DATASET}.{target}` T\n"
            f"USING `{PROJECT}.{DATASET}.{staging}` S\n"
            f"ON {on}\n"
            f"WHEN MATCHED THEN UPDATE SET {setter}\n"
            f"WHEN NOT MATCHED THEN INSERT ({collist}) VALUES ({srclist})")


def load_one(client, name, run_id):
    target, keys = SPEC[name]
    staging = f"_stg_{target}_{run_id.replace('-', '')[:12]}"
    uri = f"gs://{BUCKET}/{PREFIX}/{name}.jsonl"
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{target}")

    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=tgt.schema,                      # явная схема, без автоопределения
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )
    job = client.load_table_from_uri(uri, f"{PROJECT}.{DATASET}.{staging}",
                                     job_config=cfg, location=LOCATION)
    job.result()
    if job.errors:
        raise RuntimeError(f"load job {job.job_id} завершился с ошибками: {job.errors}")
    staged = client.get_table(f"{PROJECT}.{DATASET}.{staging}").num_rows

    before = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{target}`",
                               location=LOCATION).result())[0]["c"]
    m = client.query(merge_sql(client, target, staging, keys), location=LOCATION)
    m.result()
    after = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{target}`",
                              location=LOCATION).result())[0]["c"]
    client.delete_table(f"{PROJECT}.{DATASET}.{staging}", not_found_ok=True)

    log(event="table_loaded", table=target, source=uri, ingestion_run_id=run_id,
        rows_staged=staged, rows_before=before, rows_after=after,
        rows_inserted=after - before, load_job_id=job.job_id, merge_job_id=m.job_id,
        natural_key="+".join(keys))
    return dict(table=target, staged=staged, before=before, after=after)


def main():
    run_id = os.environ.get("INGESTION_RUN_ID") or f"bootstrap-{uuid.uuid4()}"
    started = datetime.now(timezone.utc).isoformat()
    names = ONLY or list(SPEC)
    log(event="run_start", ingestion_run_id=run_id, started_at=started,
        tables=names, bucket=BUCKET, prefix=PREFIX, dataset=DATASET)
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    results, failed = [], []
    for n in names:
        if n not in SPEC:
            failed.append((n, "неизвестная таблица"))
            continue
        try:
            results.append(load_one(client, n, run_id))
        except Exception as e:                                    # noqa: BLE001
            failed.append((n, repr(e)))
            log(event="table_failed", table=n, error=repr(e)[:400])
    log(event="run_end", ingestion_run_id=run_id,
        completed_at=datetime.now(timezone.utc).isoformat(),
        tables_ok=len(results), tables_failed=len(failed),
        total_rows_after=sum(r["after"] for r in results),
        status="OK" if not failed else "PARTIAL")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
