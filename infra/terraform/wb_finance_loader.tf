# ============================================================================
# M4 · Финансовые отчёты WB (Finance API v1) — перенос runWbFinanceDaily из Apps Script в Cloud Run.
#
# Код: cloud/src/loaders/finance/. Runbook: docs/ops/MIG_FINANCE_RUNBOOK.md. Паритет: sql/mig_finance_parity.sql.
#
# Состав:
#   • пять теневых таблиц `__CR` — зеркало схемы боевых (снимок метаданных 10.11.2026, порядок колонок как в проде);
#   • свои SA sa-wb-finance-shadow / sa-wb-finance-prod: jobUser, LOADER_RUNS, ТОЛЬКО свои таблицы, ТОЛЬКО секрет
#     WB_TOKEN_FINANCE, actAs для deployer и terraform-apply (образец — ref_sync, PR #417). Общие sa-loaders-* прав
#     на финансы не получают;
#   • wb-finance-shadow: пишет ТОЛЬКО в `__CR` (+ LOADER_RUNS environment='shadow');
#   • prod-контур (SA, его права, Job, invoker, Scheduler, алерт) создаётся ТОЛЬКО при var.finance_prod_enabled = true
#     (по умолчанию false): нецелевой apply infra.yml до cutover prod не создаёт. Включение — runbook §5.
#
# Контур проверяет и сам загрузчик (finance/targets.ts): shadow → только свои `__CR`, prod → только боевые
# и FINANCE_CUTOVER=2026-07-13. Неверный env не доходит ни до токена, ни до BigQuery.
#
# Расписание. Apps Script: 07/12/18 МСК nearMinute(30) — фактически 07:26 / 12:24 / 18:36, прогон ≤ 4,5 мин.
# Лимит Finance API — 1 запрос/мин НА ПРОДАВЦА (один токен на оба контура), поэтому shadow сдвинут на ~35 мин
# позже Apps Script (08:05 / 13:05 / 19:05 МСК): контуры не делят минуту. prod = слоты Apps Script (07:30 / 12:30 / 18:30).
# ============================================================================

locals {
  finance_common_env = {
    LOADER_NAME       = "finance"
    WB_FINANCE_HOST   = "https://finance-api.wildberries.ru"
    WB_FINANCE_SECRET = "WB_TOKEN_FINANCE"
    # 20 мин запросов к API (≈ 19 запросов при пейсере 61 с). Job timeout 1800 с, lease LOADER_RUNS — 30 мин.
    FINANCE_BUDGET_MS = "1200000"
  }
  finance_shadow_env = {
    FINANCE_RAW_TABLE         = google_bigquery_table.raw_wb_finance_cr.table_id
    FINANCE_RUNS_TABLE        = google_bigquery_table.finance_loader_runs_cr.table_id
    FINANCE_LOADS_TABLE       = google_bigquery_table.finance_report_loads_cr.table_id
    FINANCE_WEEK_STATUS_TABLE = google_bigquery_table.finance_week_status_cr.table_id
    FINANCE_WEEK_RECON_TABLE  = google_bigquery_table.finance_week_recon_cr.table_id
    # Shadow начинает календарь недель позже прода, чтобы не перекачивать WEEKLY с июля (≈ 34 отчёта и
    # ≈ 35 мин запросов, делящих лимит с Apps Script). Понедельник недели ПЕРЕД стартом shadow; паритет
    # сравнивает только отчёты, загруженные обоими контурами (runbook §4).
    FINANCE_CUTOVER = "2026-11-09"
  }
  finance_prod_env = {
    FINANCE_RAW_TABLE         = "RAW_WB_FINANCE"
    FINANCE_RUNS_TABLE        = "FINANCE_LOADER_RUNS"
    FINANCE_LOADS_TABLE       = "FINANCE_REPORT_LOADS"
    FINANCE_WEEK_STATUS_TABLE = "FINANCE_WEEK_STATUS"
    FINANCE_WEEK_RECON_TABLE  = "FINANCE_WEEK_RECON"
    FINANCE_CUTOVER           = "2026-07-13"
  }

  # Зеркало боевых схем (tables.get, 10.11.2026). RAW: деньги — STRING (конвенция RAW), служебные колонки
  # контура (report_period/report_type/run_id) — в конце, как их добавил ALTER C0.
  finance_raw_schema = [
    { name = "load_id", type = "STRING" },
    { name = "loaded_at", type = "STRING" },
    { name = "source_api", type = "STRING" },
    { name = "request_date_from", type = "STRING" },
    { name = "request_date_to", type = "STRING" },
    { name = "report_id", type = "STRING" },
    { name = "report_period_from", type = "STRING" },
    { name = "report_period_to", type = "STRING" },
    { name = "row_hash", type = "STRING" },
    { name = "raw_row_number", type = "STRING" },
    { name = "wb_operation_id", type = "STRING" },
    { name = "rrd_id", type = "STRING" },
    { name = "srid", type = "STRING" },
    { name = "rid", type = "STRING" },
    { name = "shk_id", type = "STRING" },
    { name = "sticker_id", type = "STRING" },
    { name = "doc_type_name", type = "STRING" },
    { name = "supplier_oper_name", type = "STRING" },
    { name = "operation_type_normalized", type = "STRING" },
    { name = "order_dt", type = "STRING" },
    { name = "sale_dt", type = "STRING" },
    { name = "rr_dt", type = "STRING" },
    { name = "create_dt", type = "STRING" },
    { name = "period_month", type = "STRING" },
    { name = "last_change_date", type = "STRING" },
    { name = "wb_nm_id", type = "STRING" },
    { name = "wb_vendor_code", type = "STRING" },
    { name = "barcode", type = "STRING" },
    { name = "sa_name", type = "STRING" },
    { name = "ts_name", type = "STRING" },
    { name = "brand_name", type = "STRING" },
    { name = "subject_name", type = "STRING" },
    { name = "internal_sku", type = "STRING" },
    { name = "sku_match_status", type = "STRING" },
    { name = "office_name", type = "STRING" },
    { name = "warehouse_name", type = "STRING" },
    { name = "country_name", type = "STRING" },
    { name = "region_name", type = "STRING" },
    { name = "retail_price", type = "STRING" },
    { name = "retail_amount", type = "STRING" },
    { name = "retail_price_withdisc_rub", type = "STRING" },
    { name = "sale_percent", type = "STRING" },
    { name = "commission_percent", type = "STRING" },
    { name = "product_discount_for_report", type = "STRING" },
    { name = "supplier_promo", type = "STRING" },
    { name = "spp_percent", type = "STRING" },
    { name = "quantity", type = "STRING" },
    { name = "sale_amount", type = "STRING" },
    { name = "return_amount_rub", type = "STRING" },
    { name = "for_pay", type = "STRING" },
    { name = "commission_amount", type = "STRING" },
    { name = "sales_commission", type = "STRING" },
    { name = "logistics_amount", type = "STRING" },
    { name = "storage_fee", type = "STRING" },
    { name = "deduction", type = "STRING" },
    { name = "penalty", type = "STRING" },
    { name = "acceptance", type = "STRING" },
    { name = "additional_payment", type = "STRING" },
    { name = "acquiring_fee", type = "STRING" },
    { name = "compensation_amount", type = "STRING" },
    { name = "other_amount", type = "STRING" },
    { name = "currency", type = "STRING" },
    { name = "money_parse_status", type = "STRING" },
    { name = "money_parse_comment", type = "STRING" },
    { name = "is_duplicate", type = "STRING" },
    { name = "processed_status", type = "STRING" },
    { name = "error_message", type = "STRING" },
    { name = "raw_json", type = "STRING" },
    { name = "error_code", type = "STRING" },
    { name = "rebill_logistics", type = "STRING" },
    { name = "_rr_date", type = "DATE" },
    { name = "report_period", type = "STRING" },
    { name = "report_type", type = "INT64" },
    { name = "run_id", type = "STRING" },
  ]
  finance_runs_schema = [
    { name = "run_id", type = "STRING" },
    { name = "started_at", type = "TIMESTAMP" },
    { name = "finished_at", type = "TIMESTAMP" },
    { name = "status", type = "STRING" },
    { name = "trigger_type", type = "STRING" },
    { name = "reports_discovered", type = "INT64" },
    { name = "reports_loaded", type = "INT64" },
    { name = "reports_errors", type = "INT64" },
    { name = "requests_made", type = "INT64" },
    { name = "error_message", type = "STRING" },
  ]
  finance_loads_schema = [
    { name = "report_id", type = "STRING", mode = "REQUIRED" },
    { name = "report_period", type = "STRING" },
    { name = "report_type", type = "INT64" },
    { name = "date_from", type = "DATE" },
    { name = "date_to", type = "DATE" },
    { name = "status", type = "STRING" },
    { name = "discovered_run_id", type = "STRING" },
    { name = "processing_run_id", type = "STRING" },
    { name = "attempt_count", type = "INT64" },
    { name = "discovered_at", type = "TIMESTAMP" },
    { name = "started_at", type = "TIMESTAMP" },
    { name = "completed_at", type = "TIMESTAMP" },
    { name = "rows_fetched", type = "INT64" },
    { name = "rows_loaded", type = "INT64" },
    { name = "list_forpay", type = "NUMERIC" },
    { name = "list_retail", type = "NUMERIC" },
    { name = "list_delivery", type = "NUMERIC" },
    { name = "list_storage", type = "NUMERIC" },
    { name = "list_acceptance", type = "NUMERIC" },
    { name = "list_deduction", type = "NUMERIC" },
    { name = "list_penalty", type = "NUMERIC" },
    { name = "det_forpay", type = "NUMERIC" },
    { name = "det_retail", type = "NUMERIC" },
    { name = "det_delivery", type = "NUMERIC" },
    { name = "det_storage", type = "NUMERIC" },
    { name = "det_acceptance", type = "NUMERIC" },
    { name = "det_deduction", type = "NUMERIC" },
    { name = "det_penalty", type = "NUMERIC" },
    { name = "list_metrics_json", type = "STRING" },
    { name = "error_message", type = "STRING" },
  ]
  finance_week_status_schema = [
    { name = "week_start", type = "DATE" },
    { name = "report_type", type = "INT64" },
    { name = "weekly_report_id", type = "STRING" },
    { name = "weekly_final", type = "BOOL" },
    { name = "finalized_at", type = "TIMESTAMP" },
    { name = "daily_days_loaded", type = "INT64" },
    { name = "notes", type = "STRING" },
  ]
  finance_week_recon_schema = [
    { name = "week_start", type = "DATE" },
    { name = "report_type", type = "INT64" },
    { name = "metric", type = "STRING" },
    { name = "sum_daily", type = "NUMERIC" },
    { name = "sum_weekly", type = "NUMERIC" },
    { name = "delta", type = "NUMERIC" },
    { name = "recon_status", type = "STRING" },
    { name = "checked_at", type = "TIMESTAMP" },
  ]
}

# ── Теневые таблицы ─────────────────────────────────────────────────────────
resource "google_bigquery_table" "raw_wb_finance_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_FINANCE__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "_rr_date"
  }
  clustering = ["wb_nm_id", "rrd_id"]
  schema     = jsonencode(local.finance_raw_schema)
}

resource "google_bigquery_table" "finance_loader_runs_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "FINANCE_LOADER_RUNS__CR"
  deletion_protection = false
  schema              = jsonencode(local.finance_runs_schema)
}

resource "google_bigquery_table" "finance_report_loads_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "FINANCE_REPORT_LOADS__CR"
  deletion_protection = false
  schema              = jsonencode(local.finance_loads_schema)
}

resource "google_bigquery_table" "finance_week_status_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "FINANCE_WEEK_STATUS__CR"
  deletion_protection = false
  schema              = jsonencode(local.finance_week_status_schema)
}

resource "google_bigquery_table" "finance_week_recon_cr" {
  dataset_id          = var.raw_dataset
  table_id            = "FINANCE_WEEK_RECON__CR"
  deletion_protection = false
  schema              = jsonencode(local.finance_week_recon_schema)
}

# ── Сервис-аккаунты финансов ────────────────────────────────────────────────
resource "google_service_account" "finance_shadow" {
  account_id   = "sa-wb-finance-shadow"
  display_name = "WB финансы runtime (SHADOW): Finance API + FINANCE_*__CR"
}

resource "google_service_account" "finance_prod" {
  count        = var.finance_prod_enabled ? 1 : 0
  account_id   = "sa-wb-finance-prod"
  display_name = "WB финансы runtime (PROD): Finance API + RAW_WB_FINANCE / FINANCE_*"
}

locals {
  # Контуры, существующие сейчас: shadow всегда, prod — только под флагом.
  finance_sa = merge(
    { shadow = google_service_account.finance_shadow },
    var.finance_prod_enabled ? { prod = google_service_account.finance_prod[0] } : {},
  )
  finance_cr_tables = {
    raw         = google_bigquery_table.raw_wb_finance_cr.table_id
    runs        = google_bigquery_table.finance_loader_runs_cr.table_id
    loads       = google_bigquery_table.finance_report_loads_cr.table_id
    week_status = google_bigquery_table.finance_week_status_cr.table_id
    week_recon  = google_bigquery_table.finance_week_recon_cr.table_id
  }
  # Боевые таблицы существуют (их создал C0 Apps Script) и Terraform ими НЕ владеет — привязка по имени.
  finance_prod_tables = {
    raw         = local.finance_prod_env.FINANCE_RAW_TABLE
    runs        = local.finance_prod_env.FINANCE_RUNS_TABLE
    loads       = local.finance_prod_env.FINANCE_LOADS_TABLE
    week_status = local.finance_prod_env.FINANCE_WEEK_STATUS_TABLE
    week_recon  = local.finance_prod_env.FINANCE_WEEK_RECON_TABLE
  }
}

resource "google_project_iam_member" "finance_jobuser" {
  for_each = local.finance_sa
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${each.value.email}"
}

# Журнал execution-guard (lease cli и проверка соседнего режима finance/finance-backfill).
resource "google_bigquery_table_iam_member" "finance_write_loader_runs" {
  for_each   = local.finance_sa
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.loader_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value.email}"
}

resource "google_secret_manager_secret_iam_member" "finance_token_access" {
  for_each  = local.finance_sa
  secret_id = google_secret_manager_secret.wb["WB_TOKEN_FINANCE"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.email}"
}

resource "google_service_account_iam_member" "finance_actas_deployer" {
  for_each           = local.finance_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "finance_actas_terraform" {
  for_each           = local.finance_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# shadow: только свои __CR (чтение — тем же dataEditor; других таблиц финансы не читают)
resource "google_bigquery_table_iam_member" "finance_shadow_write_cr" {
  for_each   = local.finance_cr_tables
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.finance_shadow.email}"
}

# prod: только боевые таблицы финансов и только под флагом. sa-loaders-prod прав на них НЕ получает.
resource "google_bigquery_table_iam_member" "finance_prod_write" {
  for_each   = var.finance_prod_enabled ? local.finance_prod_tables : {}
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.finance_prod[0].email}"
}

# ── Jobs ────────────────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "wb_finance_shadow" {
  name                = "wb-finance-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.finance_shadow.email
      # 0: повтор task в том же execution дал бы тот же run_id LOADER_RUNS. Повтор — следующий слот;
      # очередь манифеста (DISCOVERED/ERROR < 5 попыток) дочитывается там же.
      max_retries = 0
      timeout     = "1800s"
      containers {
        image = var.container_image
        args  = ["finance"]
        dynamic "env" {
          for_each = merge(local.common_env, local.finance_common_env, local.finance_shadow_env, { ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # Образ ведёт deploy-shadow.yml (шаг wb-finance-shadow меняет ТОЛЬКО --image, env не трогает), поэтому env
  # shadow реконсилирует Terraform без дрейфа — как у wb-stocks-shadow. Если шаг когда-нибудь начнёт ставить
  # IMAGE_DIGEST/GIT_SHA, env нужно добавить в ignore_changes (иначе вечный дрейф плана).
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.finance_shadow_write_cr,
    google_bigquery_table_iam_member.finance_write_loader_runs,
    google_secret_manager_secret_iam_member.finance_token_access,
    google_service_account_iam_member.finance_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job" "wb_finance_prod" {
  count               = var.finance_prod_enabled ? 1 : 0
  name                = "wb-finance-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.finance_prod[0].email
      max_retries     = 0
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["finance"]
        dynamic "env" {
          for_each = merge(local.common_env, local.finance_common_env, local.finance_prod_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # Как у прочих prod-Job'ов: образ и env (IMAGE_DIGEST/GIT_SHA) ведёт деплой. Боевые имена таблиц
  # попадают в env при CREATE; их подмену после этого ловит targets.ts (FINANCE_TARGET_MISMATCH).
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env,
      client,
      client_version,
    ]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.finance_prod_write,
    google_bigquery_table_iam_member.finance_write_loader_runs,
    google_secret_manager_secret_iam_member.finance_token_access,
    google_service_account_iam_member.finance_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_finance_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_finance_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_finance_prod_invoke" {
  count    = var.finance_prod_enabled ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_job.wb_finance_prod[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Scheduler: оба создаются на ПАУЗЕ; снятие — scheduler-control.yml (loader = wb-finance) ──
# retry_count=1 повторяет только вызов :run; второй execution отсекает lease LOADER_RUNS (свежий STARTED).
resource "google_cloud_scheduler_job" "wb_finance_shadow" {
  name      = "wb-finance-shadow"
  region    = var.region
  schedule  = "5 8,13,19 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_finance_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_finance_prod" {
  count     = var.finance_prod_enabled ? 1 : 0
  name      = "wb-finance-prod"
  region    = var.region
  schedule  = "30 7,12,18 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # включается ТОЛЬКО на cutover, после снятия триггера Apps Script (runbook §5)

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_finance_prod[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Наблюдаемость: отказ с кодом (loader_failed, finance_report_error) или отказ исполнения prod-Job'а ──
resource "google_monitoring_alert_policy" "wb_finance_failed" {
  count        = var.finance_prod_enabled && var.unitka_alert_email != "" ? 1 : 0
  display_name = "WB финансы: загрузка финансового отчёта не завершилась"
  combiner     = "OR"

  conditions {
    display_name = "ошибка с кодом или отказ исполнения wb-finance-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="wb-finance-prod"
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
    content   = "Финансовый отчёт WB не загрузился. Журнал: wb_raw.FINANCE_LOADER_RUNS (PARTIAL — хвост очереди, дочитается следующим слотом), манифест: wb_raw.FINANCE_REPORT_LOADS (ERROR повторяется до 5 попыток). Коды: ORPHAN_GUARD — строки отчёта в RAW без владельца (ручной разбор, STOP); KEY_CONFLICT / POSTLOAD_* — нарушение ключа; FINANCE_IMMUTABLE_ANOMALY — WB изменил метрики загруженного отчёта; FINANCE_TARGET_MISMATCH — env Job'а; DETAILED_FAILED / list HTTP — Finance API. Runbook: docs/ops/MIG_FINANCE_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
