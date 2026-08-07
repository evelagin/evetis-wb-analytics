# Манифест execution-guard. Один на оба окружения (ключ содержит environment).
resource "google_bigquery_table" "loader_runs" {
  dataset_id          = var.raw_dataset
  table_id            = "LOADER_RUNS"
  deletion_protection = true

  time_partitioning {
    type  = "DAY"
    field = "started_at"
  }
  clustering = ["environment", "loader_name", "logical_period"]

  schema = jsonencode([
    { name = "environment", type = "STRING", mode = "REQUIRED" },
    { name = "loader_name", type = "STRING", mode = "REQUIRED" },
    { name = "logical_period", type = "STRING", mode = "REQUIRED" },
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "execution_id", type = "STRING" },
    { name = "image_digest", type = "STRING" },
    { name = "git_sha", type = "STRING" },
    { name = "status", type = "STRING", mode = "REQUIRED" },
    { name = "attempt_count", type = "INT64" },
    { name = "started_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "completed_at", type = "TIMESTAMP" },
    { name = "error_code", type = "STRING" },
    { name = "error_message", type = "STRING" },
    { name = "rows_fetched", type = "INT64" },
    { name = "rows_loaded", type = "INT64" },
  ])
}

# Пилотные ТЕНЕВЫЕ таблицы остатков (schema-mirror прод). Пишет только shadow SA.
resource "google_bigquery_table" "raw_wb_stocks_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_STOCKS__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "_snapshot_date"
  }
  clustering = ["nm_id", "warehouse_id"]
  schema = jsonencode([
    { name = "load_id", type = "STRING" },
    { name = "snapshot_id", type = "STRING" },
    { name = "snapshot_ts", type = "TIMESTAMP" },
    { name = "source_api", type = "STRING" },
    { name = "nm_id", type = "INT64" },
    { name = "chrt_id", type = "INT64" },
    { name = "warehouse_id", type = "INT64" },
    { name = "warehouse_name", type = "STRING" },
    { name = "region_name", type = "STRING" },
    { name = "quantity", type = "INT64" },
    { name = "in_way_to_client", type = "INT64" },
    { name = "in_way_from_client", type = "INT64" },
    { name = "is_aggregate_warehouse", type = "BOOL" },
    { name = "internal_sku", type = "STRING" },
    { name = "sku_match_status", type = "STRING" },
    { name = "raw_json", type = "STRING" },
    { name = "_snapshot_date", type = "DATE" },
  ])
}

resource "google_bigquery_table" "wb_stocks_snapshots_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "WB_STOCKS_SNAPSHOTS__CR"
  deletion_protection = false
  schema = jsonencode([
    { name = "snapshot_id", type = "STRING", mode = "REQUIRED" },
    { name = "started_at", type = "TIMESTAMP" },
    { name = "completed_at", type = "TIMESTAMP" },
    { name = "status", type = "STRING" },
    { name = "period_from", type = "STRING" },
    { name = "period_to", type = "STRING" },
    { name = "expected_rows", type = "INT64" },
    { name = "written_rows", type = "INT64" },
    { name = "distinct_keys", type = "INT64" },
    { name = "duplicate_keys", type = "INT64" },
    { name = "unique_nm_ids", type = "INT64" },
    { name = "warehouses_count", type = "INT64" },
    { name = "qty_positive_rows", type = "INT64" },
    { name = "qty_zero_rows", type = "INT64" },
    { name = "aggregate_warehouse_rows", type = "INT64" },
    { name = "sum_quantity_all_t6", type = "INT64" },
    { name = "sum_quantity_physical_t6", type = "INT64" },
    { name = "unmatched_nm_ids", type = "STRING" },
    { name = "control_status", type = "STRING" },
    { name = "control_delta", type = "INT64" },
    { name = "error_message", type = "STRING" },
  ])
}

# Табличный IAM: shadow пишет ТОЛЬКО в __CR и манифест; читает REF_SKU_MASTER.
resource "google_bigquery_table_iam_member" "shadow_write_raw_cr" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.raw_wb_stocks_cr.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_write_manifest_cr" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.wb_stocks_snapshots_cr.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_write_runs" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.loader_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_read_ref" {
  dataset_id = var.raw_dataset
  table_id   = "REF_SKU_MASTER"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

# REF_SKU_MASTER — обычный (не authorized) view над REF_SKU_MASTER_DATA + REF_ACTIVE_VERSION.
# Для выполнения запроса runtime-SA нужен table-level read на обе базовые таблицы,
# иначе BigQuery раскрывает SQL вью и падает на первой недоступной зависимости.
resource "google_bigquery_table_iam_member" "shadow_read_ref_active_version" {
  dataset_id = var.raw_dataset
  table_id   = "REF_ACTIVE_VERSION"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_read_ref_data" {
  dataset_id = var.raw_dataset
  table_id   = "REF_SKU_MASTER_DATA"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

# prod пишет манифест + читает REF (прод-таблицы остатков привяжем в PR-Mig1b на cutover).
resource "google_bigquery_table_iam_member" "prod_write_runs" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.loader_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_read_ref" {
  dataset_id = var.raw_dataset
  table_id   = "REF_SKU_MASTER"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# Те же базовые зависимости REF-вью для prod-SA (понадобится на Mig1b cutover).
resource "google_bigquery_table_iam_member" "prod_read_ref_active_version" {
  dataset_id = var.raw_dataset
  table_id   = "REF_ACTIVE_VERSION"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_read_ref_data" {
  dataset_id = var.raw_dataset
  table_id   = "REF_SKU_MASTER_DATA"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── PR-Mart3b-2: доступ prod-загрузчика витрины `mart` к данным ──────────────
# Запись (dataEditor) на весь датасет wb_mart: процедуры sp_bootstrap_facts /
# sp_build_mart_sku_daily делают CREATE OR REPLACE TABLE для FACT_*/MART_SKU_DAILY
# (+ __BUILD, _MART_BOOTSTRAP_LOCK), а терминальная строка MART_RUNS пишется MERGE
# (mutating DML) + read-back SELECT. Нужны tables.create/update/updateData/getData на
# СЕМЕЙСТВЕ таблиц → dataset-level dataEditor соразмерен (insert-only упал бы на первом MERGE).
resource "google_bigquery_dataset_iam_member" "prod_edit_mart" {
  dataset_id = var.mart_dataset
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# Чтение (dataViewer) на весь датасет wb_raw: freshness-gate читает V_INGEST_HEARTBEAT
# (→ INGEST_RUNS + LOADER_RUNS), а sp_bootstrap_facts — V_WB_ORDERS / V_WB_SALES_RETURNS /
# V_ADV_CAMPAIGN_STATS / V_ADV_COSTS / V_WB_FINANCE_CANONICAL / WB_STOCKS_SNAPSHOTS / RAW_WB_STOCKS
# И их БАЗОВЫЕ таблицы (не-authorized вью раскрываются на зависимостях). Витрина — бизнес-ролап
# ВСЕГО канонического слоя wb_raw, поэтому dataset-level read соразмерен функции и устойчив к
# эволюции источников. Табличная (least-privilege) альтернатива описана в PR-ноте — решение за аудитом.
resource "google_bigquery_dataset_iam_member" "prod_view_raw" {
  dataset_id = var.raw_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}
