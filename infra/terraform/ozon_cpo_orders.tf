# ══════════════════════════════════════════════════════════════════════════════
# OZON CPO — «Оплата за заказ» по заказам (Phase B, OWNER ACK 2026-10-08).
# Док: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md. Код: cloud/src/loaders/ozon_cpo/.
#
# Отдельный Job в образе wb-loader (`node dist/cli.js ozon-cpo-orders`). Образ Ozon runtime
# (ozon-runtime-ingest@sha256:24e3c6d6…) и его Job'ы НЕ трогаются.
#
# ИДЕНТИЧНОСТЬ — свой SA с минимумом прав, а не sa-loaders-prod (WB) и не sa-ozon-ingestion:
#   • запись — ТОЛЬКО в четыре таблицы ozon_raw (RAW, stage, журнал, аренда), поресурсно;
#     ни одного права на датасеты WB, на ozon_mart и на справочники: вью вычисляет всё на чтении;
#   • секреты — ТОЛЬКО два секрета Performance API, поресурсно; значения в Terraform не попадают;
#   • bigquery.jobUser — как у остальных загрузчиков (запросы и загрузки). Ключ Seller API НЕ выдаётся.
# Ключи Performance API могут менять кампании и ставки: код загрузчика физически ограничен
# белым списком пяти маршрутов чтения (perfApi.ts, assertAllowedRoute).
#
# Rollout (каждый шаг — отдельное решение владельца):
#   1) targeted apply ЭТОГО файла + строки actAs в iam.tf → таблицы, SA, права, Job, Scheduler (paused);
#   2) вью ozon_mart (tools/ozon_cpo_views_deploy.py --apply) — ДО образа;
#   3) deploy-prod — тот же digest на все prod-Job'ы, включая ozon-cpo-orders-prod (флаг Юнитки выключен);
#   4) ручной бэкфилл истории: execute --update-env-vars OZON_CPO_BACKFILL_FROM=2025-05-01,…TO=<вчера>;
#   5) QA sql/ozon/qa_cpo_orders_v1.sql = PASS → снять паузу Scheduler (scheduler-control.yml).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  ozon_cpo_env = {
    LOADER_NAME = "ozon-cpo-orders"
    # Изоляция маркетплейсов: аренда (LOADER_RUNS) и журнал — в домене Ozon, не в wb_raw.
    BQ_RAW_DATASET = "ozon_raw"
    # 60 суток (OWNER ACK 2026-10-08): задержка заказ→списание max 41, p99 27; цена 45 и 60 одинакова
    # (3 календарных блока × 2 отчёта). Живое значение выставлено gcloud — env в ignore_changes,
    # здесь фиксируется для пересоздания Job'а.
    OZON_CPO_LOOKBACK_DAYS         = "60"
    OZON_PERF_CLIENT_ID_SECRET     = "EVETIS_OZON_PERFORMANCE_CLIENT_ID"
    OZON_PERF_CLIENT_SECRET_SECRET = "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"
  }

  ozon_cpo_raw_schema = [
    { name = "row_key", type = "STRING", mode = "REQUIRED" },
    { name = "report_family", type = "STRING", mode = "REQUIRED" },
    { name = "charge_date", type = "DATE", mode = "REQUIRED" },
    { name = "order_id", type = "STRING", mode = "REQUIRED" },
    { name = "order_number", type = "STRING" },
    { name = "ordered_sku", type = "STRING", mode = "REQUIRED" },
    { name = "promoted_sku", type = "STRING", mode = "REQUIRED" },
    { name = "ordered_offer_id_reported", type = "STRING" },
    { name = "order_source", type = "STRING" },
    { name = "product_name", type = "STRING" },
    { name = "quantity", type = "INT64" },
    { name = "unit_sale_price_rub", type = "NUMERIC" },
    { name = "sale_value_rub", type = "NUMERIC" },
    { name = "rate_pct", type = "NUMERIC" },
    { name = "rate_rub", type = "NUMERIC" },
    { name = "expense_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "completeness_status", type = "STRING", mode = "REQUIRED" },
    { name = "report_uuid", type = "STRING" },
    { name = "report_title", type = "STRING" },
    { name = "requested_from", type = "DATE" },
    { name = "requested_to", type = "DATE" },
    { name = "requested_from_utc", type = "TIMESTAMP" },
    { name = "requested_to_utc", type = "TIMESTAMP" },
    { name = "fetched_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "raw_line", type = "STRING" },
  ]

  ozon_cpo_runs_schema = [
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "record_type", type = "STRING", mode = "REQUIRED" },
    { name = "mode", type = "STRING" },
    { name = "status", type = "STRING", mode = "REQUIRED" },
    { name = "window_from", type = "DATE" },
    { name = "window_to", type = "DATE" },
    { name = "chunk_from", type = "DATE" },
    { name = "chunk_to", type = "DATE" },
    { name = "rows_all_sku", type = "INT64" },
    { name = "rows_search", type = "INT64" },
    { name = "expense_all_sku_rub", type = "NUMERIC" },
    { name = "expense_search_rub", type = "NUMERIC" },
    { name = "rows_replaced", type = "INT64" },
    { name = "expense_replaced_rub", type = "NUMERIC" },
    { name = "report_uuids", type = "STRING" },
    { name = "chunks_total", type = "INT64" },
    { name = "chunks_loaded", type = "INT64" },
    { name = "started_at", type = "TIMESTAMP" },
    { name = "completed_at", type = "TIMESTAMP" },
    { name = "error_code", type = "STRING" },
    { name = "error_message", type = "STRING" },
    { name = "image_digest", type = "STRING" },
    { name = "git_sha", type = "STRING" },
    { name = "execution_id", type = "STRING" },
  ]

  # Тот же контракт, что у wb_raw.LOADER_RUNS (bigquery.tf) — его читает cli.ts (execution-guard).
  ozon_loader_runs_schema = [
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
  ]
}

resource "google_service_account" "ozon_cpo_loader" {
  account_id   = "sa-ozon-cpo-loader"
  display_name = "Ozon CPO orders loader runtime (PROD)"
}

# ── Таблицы ozon_raw. RAW, журнал и аренда — под защитой удаления; stage — служебный. ───────────
resource "google_bigquery_table" "ozon_cpo_raw" {
  dataset_id          = "ozon_raw"
  table_id            = "RAW_OZON_ADS_CPO_ORDERS"
  deletion_protection = true
  description         = "Оплата за заказ (CPO) Ozon: строки отчёта по заказам двух семейств (ALL_SKU_PROMO, SEARCH_PROMO), только полные сутки. Пишет ozon-cpo-orders-prod (атомарная замена блока окна). Каноническая вью: ozon_mart.V_OZON_ADS_CPO_ORDERS."
  schema              = jsonencode(local.ozon_cpo_raw_schema)

  time_partitioning {
    type  = "MONTH"
    field = "charge_date"
  }
  clustering = ["report_family", "ordered_sku"]
}

# Постоянный stage: атомарная замена без tables.create/delete у SA (шаблон wb_paid_storage_loader.tf).
resource "google_bigquery_table" "ozon_cpo_stage" {
  dataset_id          = "ozon_raw"
  table_id            = "RAW_OZON_ADS_CPO_ORDERS__STAGE"
  deletion_protection = false
  schema              = jsonencode(local.ozon_cpo_raw_schema)

  time_partitioning {
    type  = "MONTH"
    field = "charge_date"
  }
  clustering = ["report_family", "ordered_sku"]
}

resource "google_bigquery_table" "ozon_cpo_runs" {
  dataset_id          = "ozon_raw"
  table_id            = "OZON_CPO_ORDER_RUNS"
  deletion_protection = true
  description         = "Журнал ozon-cpo-orders: CHUNK — блок окна, записан в одной транзакции с данными (есть строка ⇔ данные легли); RUN — итог прогона COMPLETE / PARTIAL / ERROR. Свежесть CPO = MAX(window_to) RUN COMPLETE."
  schema              = jsonencode(local.ozon_cpo_runs_schema)

  time_partitioning {
    type  = "MONTH"
    field = "started_at"
  }
}

resource "google_bigquery_table" "ozon_loader_runs" {
  dataset_id          = "ozon_raw"
  table_id            = "LOADER_RUNS"
  deletion_protection = true
  description         = "Execution-guard (аренда) Job'ов образа wb-loader в домене Ozon. Контракт = wb_raw.LOADER_RUNS."
  schema              = jsonencode(local.ozon_loader_runs_schema)

  time_partitioning {
    type  = "DAY"
    field = "started_at"
  }
  clustering = ["environment", "loader_name", "logical_period"]
}

resource "google_bigquery_table_iam_member" "ozon_cpo_write" {
  for_each = {
    raw         = google_bigquery_table.ozon_cpo_raw.table_id
    stage       = google_bigquery_table.ozon_cpo_stage.table_id
    runs        = google_bigquery_table.ozon_cpo_runs.table_id
    loader_runs = google_bigquery_table.ozon_loader_runs.table_id
  }
  dataset_id = "ozon_raw"
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ozon_cpo_loader.email}"
}

resource "google_project_iam_member" "ozon_cpo_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ozon_cpo_loader.email}"
}

# ── Секреты Performance API. Созданы владельцем вручную; Terraform их не создаёт и значений не хранит. ──
# Привязка аддитивная (iam_member): существующий доступ sa-ozon-ingestion не меняется.
data "google_secret_manager_secret" "ozon_perf" {
  for_each  = toset(["EVETIS_OZON_PERFORMANCE_CLIENT_ID", "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"])
  secret_id = each.value
}

resource "google_secret_manager_secret_iam_member" "ozon_cpo_perf_access" {
  for_each  = data.google_secret_manager_secret.ozon_perf
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ozon_cpo_loader.email}"
}

# deploy-prod обновляет образ Job'а → нужен actAs на его SA (тот же контракт, что deployer_actas_prod).
resource "google_service_account_iam_member" "deployer_actas_ozon_cpo" {
  service_account_id = google_service_account.ozon_cpo_loader.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_job" "ozon_cpo_orders_prod" {
  name                = "ozon-cpo-orders-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.ozon_cpo_loader.email
      # Повтор транзиентного отказа делает cli.ts в том же слоте (retryTransient); повтор Cloud Run
      # шёл бы с тем же execution и аренда его бы не пустила.
      max_retries = 0
      # Суточный прогон — 3 блока × 2 отчёта; бэкфилл истории — до 20 блоков (execute --task-timeout).
      timeout = "1800s"
      containers {
        image = var.container_image
        args  = ["ozon-cpo-orders"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ozon_cpo_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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
resource "google_cloud_run_v2_job_iam_member" "scheduler_ozon_cpo_orders_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.ozon_cpo_orders_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# 05:40 UTC = 08:40 МСК: после ozon-daily (06:30 МСК, Performance API) и ozon-fast (07:00 МСК, отправления),
# за 80 минут до Ozon-Юнитки (10:00 МСК). Гонок нет: блок окна меняется одной транзакцией, читатель видит
# либо прежний, либо новый блок; Юнитка свежестью CPO не блокируется (bq.ts, ozonCpoFreshnessSql).
# Создаётся НА ПАУЗЕ: снимается после бэкфилла и QA = PASS.
resource "google_cloud_scheduler_job" "ozon_cpo_orders_prod" {
  name             = "ozon-cpo-orders-prod"
  region           = var.region
  schedule         = "40 5 * * *"
  time_zone        = "Etc/UTC"
  paused           = true
  attempt_deadline = "1800s"

  # Повтор ВЫЗОВА :run, а не прогона; все повторы — в пределах того же часа МСК (слот аренды).
  retry_config {
    retry_count          = 3
    min_backoff_duration = "60s"
    max_backoff_duration = "300s"
    max_doublings        = 2
    max_retry_duration   = "600s"
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ozon_cpo_orders_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Наблюдаемость: отказ с кодом (loader_failed) ИЛИ отказ платформы (execution failed). ─────────────
# Отдельная политика: условия существующих политик на месте не меняются (unitka_engine.tf, 06.10.2026).
resource "google_monitoring_alert_policy" "ozon_cpo_orders_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "OZON CPO: загрузка «Оплаты за заказ» не завершилась"
  combiner     = "OR"

  conditions {
    display_name = "ошибка с кодом или отказ исполнения ozon-cpo-orders-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="ozon-cpo-orders-prod"
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
    content   = "«Оплата за заказ» Ozon не загрузилась. Юнитка НЕ блокируется: CPO свежих суток появится после успешного прогона (окно 45 суток перечитывается). Журнал: ozon_raw.OZON_CPO_ORDER_RUNS (PARTIAL — часть блоков легла, повтор идемпотентен). Коды: CPO_REPORT_* — изменился отчёт Ozon; CPO_ROWS_WOULD_DISAPPEAR — сутки исчезли бы из RAW; CPO_PERF_AUTH — секрет Performance API. Runbook: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
