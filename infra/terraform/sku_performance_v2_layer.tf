# ── SKU Performance V2 · Phase B: автосборка материализованного слоя day × SKU ──────────
# Док: docs/SKU_PERFORMANCE_V2_PHASE_B_BACKEND_2026-09-18.md
# SQL: sql/dash/sku_performance_v2_daily_v1.sql (wb_mart.sp_build_sku_performance_v2_daily)
#
# МЕХАНИЗМ — тот же, что executive_v2_layer.tf (Executive V2 Phase C2, в production с 17.09.2026):
# Cloud Scheduler → BigQuery jobs API → `CALL wb_mart.sp_build_sku_performance_v2_daily('scheduler')` под
# отдельным SA. Образ не нужен.
#
# РАСПИСАНИЕ: каждый час в :20, 07:20–23:20 МСК (17 запусков) — на 10 минут позже сборки Executive (:10),
#   чтобы не накладываться. Витрина wb-mart-prod (окна 07/09/12/16) завершается к :04–:06 → 07:20 / 09:20 /
#   12:20 / 16:20 — обязательная пересборка после витрины; остальные часы подхватывают финотчёт DAILY,
#   отчёт платного хранения и наблюдатель цен (читаются из wb_raw напрямую).
#   Стоимость: ~0,49 ГБ × 17 ≈ 8,3 ГБ и ~160 тыс. slot-с в сутки.
#
# БЕЗ НАЛОЖЕНИЙ: замок `_SKU_PERFORMANCE_V2_BUILD_LOCK` (второй запуск → SKIPPED_LOCKED); retry_count = 0.
# ОТКАЗ: ASSERT до подмены или ошибка в транзакции → ROLLBACK, таблица от последней успешной сборки,
#   журнал SKU_PERFORMANCE_V2_BUILD_LOG = FAILED (проверено 18.09.2026 на копиях процедуры).
#
# ПРАВА — минимальные: jobUser (проект); dataViewer на wb_mart / wb_raw / evetis_ref (referenced_tables
#   сборки 18.09.2026); dataEditor ПОТАБЛИЧНО только на три таблицы слоя.
#   ⚠️ Первая сборка и смена схемы таблицы — только миграцией (нужно право создания таблицы в датасете,
#   у SA его нет): при расхождении схемы плановая сборка падает fail-closed на INSERT.
#
# APPLY — только целевой, с просмотренным планом. Ожидаемый план:
#   1 SA + 1 project IAM + 3 dataset IAM + 3 table IAM + 1 actAs (iam.tf) + 1 scheduler = 10 to add.

resource "google_service_account" "sku_v2_layer" {
  account_id   = "sa-sku-v2-layer"
  display_name = "EVETIS SKU Performance V2 daily layer build (PROD)"
}

resource "google_project_iam_member" "sku_v2_layer_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.sku_v2_layer.email}"
}

locals {
  sku_v2_layer_read_datasets = toset([var.mart_dataset, var.raw_dataset, "evetis_ref"])
  # Таблицы, которые пишет sp_build_sku_performance_v2_daily (DELETE/INSERT/UPDATE).
  sku_v2_layer_write_tables = toset([
    "SKU_PERFORMANCE_V2_DAILY",
    "SKU_PERFORMANCE_V2_BUILD_LOG",
    "_SKU_PERFORMANCE_V2_BUILD_LOCK",
  ])
}

resource "google_bigquery_dataset_iam_member" "sku_v2_layer_read" {
  for_each   = local.sku_v2_layer_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.sku_v2_layer.email}"
}

resource "google_bigquery_table_iam_member" "sku_v2_layer_write" {
  for_each   = local.sku_v2_layer_write_tables
  dataset_id = var.mart_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.sku_v2_layer.email}"
}

resource "google_cloud_scheduler_job" "sku_v2_layer_build" {
  name      = "sku-performance-v2-layer-build"
  region    = var.region
  schedule  = "20 7-23 * * *"
  time_zone = "Europe/Moscow"

  paused = false

  # Сборка ~50 с (замер 18.09: 51 с, 0,49 ГБ, 9,5 тыс. слот-с). jobs.insert возвращает
  # управление сразу после постановки скрипта, дедлайн — только на HTTP-вызов.
  attempt_deadline = "320s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://bigquery.googleapis.com/bigquery/v2/projects/${var.project_id}/jobs"
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({
      configuration = {
        query = {
          query        = "CALL `${var.mart_dataset}.sp_build_sku_performance_v2_daily`('scheduler')"
          useLegacySql = false
        }
        labels = {
          evetis-job = "sku-performance-v2-layer"
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.sku_v2_layer.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
  ]
}
