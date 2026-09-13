# ============================================================================
# UNITKA Stage E4 — фактическое платное хранение WB.
#
# Источник: WB Paid Storage API. Никаких stock × rate оценок.
# Loader каждый день перезабирает overlap 8 закрытых суток и атомарно заменяет
# это окно в RAW_WB_PAID_STORAGE. Это ловит поздние корректировки WB и гарантирует,
# что storage в UNITKA = факт WB либо GAP/NULL.
#
# Rollout:
#   1) terraform apply — Job + stage + IAM + paused Scheduler;
#   2) deploy-prod — образ с loader `storage`;
#   3) ручной запуск и QA окна, в т.ч. 10–12.09.2026;
#   4) только после PASS снять pause Scheduler;
#   5) запустить UNITKA Engine и проверить запись Storage в September Master.
# ============================================================================

locals {
  storage_env = {
    LOADER_NAME           = "storage"
    WB_ANALYTICS_HOST     = "https://seller-analytics-api.wildberries.ru"
    WB_ANALYTICS_SECRET   = "WB_TOKEN_ANALYTICS"
    STORAGE_RAW_TABLE     = "RAW_WB_PAID_STORAGE"
    STORAGE_LOOKBACK_DAYS = "8"
  }

  storage_schema = [
    { name = "observation_id", type = "STRING" },
    { name = "run_id", type = "STRING" },
    { name = "observed_at", type = "TIMESTAMP" },
    { name = "date_msk", type = "DATE" },
    { name = "nm_id", type = "INT64" },
    { name = "chrt_id", type = "INT64" },
    { name = "barcode", type = "STRING" },
    { name = "warehouse", type = "STRING" },
    { name = "office_id", type = "INT64" },
    { name = "warehouse_coef", type = "NUMERIC" },
    { name = "log_warehouse_coef", type = "NUMERIC" },
    { name = "subject", type = "STRING" },
    { name = "brand", type = "STRING" },
    { name = "vendor_code", type = "STRING" },
    { name = "volume", type = "NUMERIC" },
    { name = "calc_type", type = "STRING" },
    { name = "warehouse_price", type = "NUMERIC" },
    { name = "barcodes_count", type = "INT64" },
    { name = "pallet_place_code", type = "INT64" },
    { name = "pallet_count", type = "NUMERIC" },
    { name = "loyalty_discount", type = "NUMERIC" },
    { name = "tariff_fix_date", type = "STRING" },
    { name = "tariff_lower_date", type = "STRING" },
    { name = "raw_row_json", type = "STRING" },
    { name = "ingested_at", type = "TIMESTAMP" },
  ]
}

# Постоянная staging-таблица нужна для атомарного replace без dataset-level dataEditor.
# Loader SA имеет право редактировать только target и stage.
resource "google_bigquery_table" "wb_paid_storage_stage" {
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_PAID_STORAGE__STAGE"
  deletion_protection = false
  schema              = jsonencode(local.storage_schema)

  time_partitioning {
    type  = "DAY"
    field = "date_msk"
  }
  clustering = ["nm_id", "warehouse"]
}

resource "google_cloud_run_v2_job" "wb_paid_storage_prod" {
  name                = "wb-paid-storage-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 0
      # Async WB report can spend several minutes in task polling.
      timeout = "900s"
      containers {
        image = var.container_image
        args  = ["storage"]
        dynamic "env" {
          for_each = merge(local.common_env, local.storage_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env,
      client,
      client_version,
    ]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_paid_storage_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_paid_storage_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_paid_storage_raw" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_PAID_STORAGE"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_paid_storage_stage" {
  dataset_id = google_bigquery_table.wb_paid_storage_stage.dataset_id
  table_id   = google_bigquery_table.wb_paid_storage_stage.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_cloud_scheduler_job" "wb_paid_storage_prod" {
  name   = "wb-paid-storage-prod"
  region = var.region
  # 08:45 UTC = 11:45 МСК. Storage не гейтит LAST_CLOSED_DATE; это окно специально
  # перед reserve Engine в 12:30 МСК, чтобы D-1 уже успел сформироваться у WB.
  schedule  = "45 8 * * *"
  time_zone = "Etc/UTC"
  paused    = true

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_paid_storage_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
