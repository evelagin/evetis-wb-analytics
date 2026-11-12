# ============================================================================
# M5 — REF Sync (перенос runRefSync из Apps Script): лист SKU_MASTER книги
# «Evetis аналитика 2.0» → REF_SKU_MASTER_DATA + REF_ACTIVE_VERSION (+ журналы REF_SYNC_*).
# Код: cloud/src/loaders/refsync/*, runbook: docs/ops/MIG_SUPPLIES_REF_RUNBOOK.md.
#
# 🔴 Свои runtime-SA, а не sa-loaders-*: чтобы прочитать лист, SA должен быть «Читателем»
# КНИГИ (в Google Sheets права выдаются на файл целиком, на отдельный лист — нельзя). Выдать это
# sa-loaders-prod значило бы открыть всю аналитическую книгу десятку prod-загрузчиков.
# Отдельный SA ограничивает круг одним Job'ом; scope в коде — spreadsheets.readonly.
#
# Доступ к книге Terraform НЕ выдаёт (Drive ACL вне провайдера): владелец добавляет
# sa-ref-sync-shadow@… и sa-ref-sync-prod@… «Читателями» книги вручную (runbook §5).
#
# 🔴 ВСЁ ПОД ФЛАГАМИ (m5_variables.tf): shadow — var.m5_shadow_enabled, prod — var.m5_prod_enabled.
# ============================================================================

resource "google_service_account" "ref_sync_shadow" {
  count        = local.m5_ref_shadow
  account_id   = "sa-ref-sync-shadow"
  display_name = "REF Sync runtime (SHADOW): Sheets read-only + REF *__CR"
}

resource "google_service_account" "ref_sync_prod" {
  count        = local.m5_ref_prod
  account_id   = "sa-ref-sync-prod"
  display_name = "REF Sync runtime (PROD): Sheets read-only + REF_* production"
}

locals {
  m5_ref_shadow = var.m5_shadow_enabled ? 1 : 0
  m5_ref_prod   = var.m5_prod_enabled ? 1 : 0
  # Контуры, включённые флагами: ключ → email SA (для общих прав jobUser / actAs).
  ref_sync_sa = merge(
    var.m5_shadow_enabled ? { shadow = google_service_account.ref_sync_shadow[0] } : {},
    var.m5_prod_enabled ? { prod = google_service_account.ref_sync_prod[0] } : {},
  )
  ref_prod_tables = {
    data   = "REF_SKU_MASTER_DATA"
    active = "REF_ACTIVE_VERSION"
    runs   = "REF_SYNC_RUNS"
    tlog   = "REF_SYNC_TABLE_LOG"
  }
  ref_sync_env_common = {
    LOADER_NAME        = "ref-sync"
    REF_SPREADSHEET_ID = "1MLOCJEX0UR0Gj9gUcDbTBfPrtAhqQc2ctcsoaepSTiU" # «Evetis аналитика 2.0»
    REF_SHEET_NAME     = "SKU_MASTER"
  }
  # Схемы = боевые таблицы (созданы Apps Script refEnsureAll_, сверены tables.get 10.11.2026).
  ref_schemas = {
    data = [
      { name = "ref_run_id", type = "STRING", mode = "REQUIRED" },
      { name = "marketplace", type = "STRING" },
      { name = "nm_id", type = "INTEGER" },
      { name = "internal_sku", type = "STRING" },
      { name = "product_name_short", type = "STRING" },
      { name = "product_name_full", type = "STRING" },
      { name = "category", type = "STRING" },
      { name = "line", type = "STRING" },
      { name = "product_type", type = "STRING" },
      { name = "is_bundle", type = "BOOLEAN" },
      { name = "status", type = "STRING" },
      { name = "active", type = "BOOLEAN" },
      { name = "wb_vendor_code", type = "STRING" },
      { name = "barcode", type = "STRING" },
      { name = "wb_subject_id", type = "STRING" },
      { name = "wb_subject_name", type = "STRING" },
      { name = "brand", type = "STRING" },
      { name = "volume_ml", type = "STRING" },
      { name = "include_in_pnl", type = "BOOLEAN" },
      { name = "include_in_ads_analysis", type = "BOOLEAN" },
      { name = "include_in_supply_plan", type = "BOOLEAN" },
      { name = "include_in_stock_alerts", type = "BOOLEAN" },
      { name = "data_quality_status", type = "STRING" },
      { name = "_synced_at", type = "TIMESTAMP" },
    ]
    active = [
      { name = "id", type = "STRING", mode = "REQUIRED" },
      { name = "active_ref_run_id", type = "STRING" },
      { name = "activated_at", type = "TIMESTAMP" },
    ]
    runs = [
      { name = "run_id", type = "STRING" },
      { name = "status", type = "STRING" },
      { name = "started_at", type = "TIMESTAMP" },
      { name = "finished_at", type = "TIMESTAMP" },
      { name = "active_version_before", type = "STRING" },
      { name = "active_version_after", type = "STRING" },
      { name = "error_message", type = "STRING" },
      { name = "warning_count", type = "INTEGER" },
      { name = "warning_fingerprint", type = "STRING" },
    ]
    tlog = [
      { name = "run_id", type = "STRING" },
      { name = "ref_name", type = "STRING" },
      { name = "source_rows", type = "INTEGER" },
      { name = "normalized_rows", type = "INTEGER" },
      { name = "staged_rows", type = "INTEGER" },
      { name = "published_rows", type = "INTEGER" },
      { name = "validation_error_count", type = "INTEGER" },
      { name = "status", type = "STRING" },
    ]
  }
}

# ── Теневые таблицы (без вью: сверка паритета читает DATA + ACTIVE напрямую) ──
resource "google_bigquery_table" "ref_cr" {
  for_each            = var.m5_shadow_enabled ? local.ref_prod_tables : {}
  dataset_id          = var.raw_dataset
  table_id            = "${each.value}__CR"
  deletion_protection = false
  clustering          = each.key == "data" ? ["ref_run_id"] : null
  schema              = jsonencode(local.ref_schemas[each.key])
}

# ── Права ───────────────────────────────────────────────────────────────────
resource "google_project_iam_member" "ref_sync_jobuser" {
  for_each = local.ref_sync_sa
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${each.value.email}"
}

# Четыре REF_*__CR + LOADER_RUNS: cli.ts делает acquire/finalize lease до и после обработчика.
resource "google_bigquery_table_iam_member" "ref_sync_shadow_write" {
  for_each = var.m5_shadow_enabled ? merge(
    { for k, v in local.ref_prod_tables : k => "${v}__CR" },
    { loader_runs = google_bigquery_table.loader_runs.table_id },
  ) : {}
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ref_sync_shadow[0].email}"
  depends_on = [google_bigquery_table.ref_cr]
}

resource "google_bigquery_table_iam_member" "ref_sync_prod_write" {
  for_each = var.m5_prod_enabled ? merge(
    local.ref_prod_tables,
    { loader_runs = google_bigquery_table.loader_runs.table_id },
  ) : {}
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ref_sync_prod[0].email}"
}

# actAs: deployer обновляет образ Job'а, terraform-apply создаёт Job/Scheduler с этими SA.
resource "google_service_account_iam_member" "ref_sync_actas_deployer" {
  for_each           = local.ref_sync_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "ref_sync_actas_terraform" {
  for_each           = local.ref_sync_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# ── Jobs ────────────────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "ref_sync_shadow" {
  count               = local.m5_ref_shadow
  name                = "ref-sync-shadow"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.ref_sync_shadow[0].email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.container_image
        args  = ["ref-sync"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ref_sync_env_common, {
            ENVIRONMENT      = "shadow"
            REF_DATA_TABLE   = "${local.ref_prod_tables.data}__CR"
            REF_ACTIVE_TABLE = "${local.ref_prod_tables.active}__CR"
            REF_RUNS_TABLE   = "${local.ref_prod_tables.runs}__CR"
            REF_TLOG_TABLE   = "${local.ref_prod_tables.tlog}__CR"
          })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # env принадлежит Terraform (deploy-shadow.yml меняет только образ).
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.ref_sync_shadow_write,
    google_project_iam_member.ref_sync_jobuser,
    google_service_account_iam_member.ref_sync_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job" "ref_sync_prod" {
  count               = local.m5_ref_prod
  name                = "ref-sync-prod"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.ref_sync_prod[0].email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.container_image
        args  = ["ref-sync"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ref_sync_env_common, local.deploy_managed_env, {
            ENVIRONMENT      = "prod"
            REF_DATA_TABLE   = local.ref_prod_tables.data
            REF_ACTIVE_TABLE = local.ref_prod_tables.active
            REF_RUNS_TABLE   = local.ref_prod_tables.runs
            REF_TLOG_TABLE   = local.ref_prod_tables.tlog
          })
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
  depends_on = [google_project_service.enabled, google_service_account_iam_member.ref_sync_actas_terraform]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ref_sync_shadow_invoke" {
  count    = local.m5_ref_shadow
  location = var.region
  name     = google_cloud_run_v2_job.ref_sync_shadow[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ref_sync_prod_invoke" {
  count    = local.m5_ref_prod
  location = var.region
  name     = google_cloud_run_v2_job.ref_sync_prod[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Scheduler: ~08:22 МСК, как у триггера Apps Script (фактические старты 08:22:26–08:22:30) ──
resource "google_cloud_scheduler_job" "ref_sync_shadow" {
  count     = local.m5_ref_shadow
  name      = "ref-sync-shadow"
  region    = var.region
  schedule  = "32 8 * * *"
  time_zone = "Europe/Moscow"
  paused    = true
  retry_config {
    retry_count = 0
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ref_sync_shadow[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "ref_sync_prod" {
  count     = local.m5_ref_prod
  name      = "ref-sync-prod"
  region    = var.region
  schedule  = "22 8 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # снимается только на cutover, после снятия триггера Apps Script
  retry_config {
    retry_count = 0
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ref_sync_prod[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
