# ══════════════════════════════════════════════════════════════════════════════
# WB STORE P&L — Phase C (OWNER ACK 2026-10-09, C1–C4): управленческая чистая прибыль магазина WB.
# Док: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md. Код: cloud/src/loaders/unitka/storepnl/.
# Вью: sql/unitka/store_pnl_v1.sql (wb_mart.V_WB_STORE_*), QA: sql/unitka/qa_store_pnl_v1.sql.
#
# Отдельный Job в образе wb-loader (`node dist/cli.js unitka-store-pnl`) под sa-loaders-prod:
#   • лист Юнитки — ТОЛЬКО чтение: загрузчик берёт токен со scope spreadsheets.readonly
#     (SheetsRest readonly), пути записи в Sheets в коде нет. Engine Юнитки не затрагивается;
#   • запись — ТОЛЬКО две таблицы ниже (снимок и его stage), поресурсно; чтение wb_raw/wb_mart и
#     аренда wb_raw.LOADER_RUNS у SA уже есть (iam.tf). Новых прав на внешние сервисы нет (F-18).
#
# Rollout (каждый шаг — решение владельца):
#   1) targeted apply ЭТОГО файла → таблицы, права, Job, Scheduler (paused);
#   2) вью (tools/unitka_store_pnl_deploy.py --apply) — после таблицы снимка, до первого прогона;
#   3) deploy-prod — тот же digest и на unitka-store-pnl-prod;
#   4) ручной прогон → QA = PASS → снять паузу (scheduler-control.yml).
# Вкладка «WB Магазин P&L» этим контуром НЕ создаётся и не пишется (отдельное OWNER ACK).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  store_pnl_env = {
    LOADER_NAME           = "unitka-store-pnl"
    UNITKA_SPREADSHEET_ID = "1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg"
    UNITKA_SHEET_NAME     = "WB_Юнит_2025"
    UNITKA_MART_DATASET   = var.mart_dataset
    UNITKA_OPS_DATASET    = "wb_ops"
  }
  # Порядок колонок = snapshotRecord() в bq.ts (INSERT … SELECT * из stage); закреплён тестом.
  store_pnl_snapshot_schema = [
    { name = "snapshot_id", type = "STRING", mode = "REQUIRED" },
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "snapshot_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "lcd", type = "DATE", mode = "REQUIRED" },
    { name = "month_key", type = "STRING", mode = "REQUIRED" },
    { name = "date_msk", type = "DATE", mode = "REQUIRED" },
    { name = "nm_id", type = "INT64", mode = "REQUIRED" },
    { name = "sheet_row", type = "INT64" },
    { name = "block_col", type = "INT64" },
    { name = "orders", type = "FLOAT64" },
    { name = "cancels", type = "FLOAT64" },
    { name = "price", type = "FLOAT64" },
    { name = "commission_rate", type = "FLOAT64" },
    { name = "logistics_per_unit", type = "FLOAT64" },
    { name = "reverse_leg_rate", type = "FLOAT64" },
    { name = "storage", type = "FLOAT64" },
    { name = "ads_in", type = "FLOAT64" },
    { name = "ads_out", type = "FLOAT64" },
    { name = "tax_per_unit", type = "FLOAT64" },
    { name = "cogs_per_unit", type = "FLOAT64" },
    { name = "profit", type = "FLOAT64" },
    { name = "model_gap", type = "FLOAT64" },
    { name = "image_digest", type = "STRING" },
    { name = "git_sha", type = "STRING" },
  ]
}

# ── Снимок значений листа: только добавление, вью берёт MAX(snapshot_id). ─────────────────────
resource "google_bigquery_table" "store_pnl_snapshot" {
  dataset_id          = "wb_ops"
  table_id            = "UNITKA_SKU_COMPONENTS_DAILY"
  deletion_protection = true
  description         = "Phase C: снимок компонент вклада SKU из листа WB_Юнит_2025 (сутки × nm ≤ LCD): цена, ставки комиссии/логистики, налог и COGS на единицу, прибыль W. Только добавление; пишет unitka-store-pnl-prod. Читает wb_mart.V_WB_STORE_PNL_MONTHLY (последний snapshot_id)."
  schema              = jsonencode(local.store_pnl_snapshot_schema)

  time_partitioning {
    type  = "MONTH"
    field = "snapshot_at"
  }
  clustering = ["snapshot_id", "date_msk"]
}

resource "google_bigquery_table" "store_pnl_snapshot_stage" {
  dataset_id          = "wb_ops"
  table_id            = "UNITKA_SKU_COMPONENTS_DAILY__STAGE"
  deletion_protection = false
  schema              = jsonencode(local.store_pnl_snapshot_schema)
}

resource "google_bigquery_table_iam_member" "store_pnl_write" {
  for_each = {
    snapshot = google_bigquery_table.store_pnl_snapshot.table_id
    stage    = google_bigquery_table.store_pnl_snapshot_stage.table_id
  }
  dataset_id = "wb_ops"
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_cloud_run_v2_job" "unitka_store_pnl_prod" {
  name                = "unitka-store-pnl-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.container_image
        args  = ["unitka-store-pnl"]
        dynamic "env" {
          for_each = merge(local.common_env, local.store_pnl_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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

# 🔴 Без invoker расписание молча не запускает Job (инцидент 23.09, ozon_unitka.tf).
resource "google_cloud_run_v2_job_iam_member" "scheduler_unitka_store_pnl_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.unitka_store_pnl_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# 10:00 UTC = 13:00 МСК: после резервного окна Engine (12:30 МСК) — лист уже закрыл вчерашние сутки.
# Создаётся НА ПАУЗЕ: снимается после первого ручного прогона и QA = PASS.
resource "google_cloud_scheduler_job" "unitka_store_pnl_prod" {
  name             = "unitka-store-pnl-prod"
  region           = var.region
  schedule         = "0 10 * * *"
  time_zone        = "Etc/UTC"
  paused           = true
  attempt_deadline = "600s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.unitka_store_pnl_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "unitka_store_pnl_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "WB STORE P&L: снимок P&L магазина не обновился"
  combiner     = "OR"

  conditions {
    display_name = "ошибка с кодом или отказ исполнения unitka-store-pnl-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="unitka-store-pnl-prod"
        severity>=ERROR
        (jsonPayload.code!="" OR (logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event" AND protoPayload.methodName="/Jobs.RunJob"))
      EOT
      label_extractors = {
        error_code = "EXTRACT(jsonPayload.code)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Снимок листа для P&L магазина WB не обновился; P&L показывает предыдущий снимок (snapshot_id в вью). Лист и Engine не затронуты. Коды: STORE_PNL_SHEET_ERROR — ошибка формулы/геометрии листа; STORE_PNL_SNAPSHOT_ORDER / STAGE_* — проверка записи; STORE_PNL_IDENTITY — строка P&L не складывается. Runbook: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
