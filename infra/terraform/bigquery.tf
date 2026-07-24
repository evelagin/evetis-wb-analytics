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
