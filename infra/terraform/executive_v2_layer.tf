# ── Executive V2 · Phase C2: автосборка материализованного слоя ─────────────────
# Док: docs/EXECUTIVE_V2_PHASE_C2_MATERIALIZED_LAYER_2026-09-17.md
# SQL: sql/dash/executive_v2_daily_v1.sql (wb_mart.sp_build_executive_v2_daily)
#
# МЕХАНИЗМ — прецедент ct_refresh.tf / ops_health.tf: Cloud Scheduler → BigQuery jobs API →
# `CALL wb_mart.sp_build_executive_v2_daily('scheduler')` под отдельным SA. Образ не нужен.
#
# РАСПИСАНИЕ: каждый час в :10, 07:10–23:10 МСК (17 запусков).
#   Витрина wb-mart-prod: окна 07/09/12/16, при успехе завершается 07:04–07:06 →
#   запуски 07:10 / 09:10 / 12:10 / 16:10 — обязательная пересборка после витрины
#   (и после каждого её резервного окна). Остальные часы подхватывают загрузчики
#   sales/orders (:22/:31 предыдущего часа) и поздние финотчёты/рекламу.
#   Если витрина завершилась позже :10, слой догоняет её в следующий час; на дашборде
#   это видно: «Витрина собрана» раньше, чем сборка MART_SKU_DAILY в журнале.
#
# БЕЗ НАЛОЖЕНИЙ: процедура берёт замок `_EXECUTIVE_V2_BUILD_LOCK`; второй запуск пишет
#   SKIPPED_LOCKED и выходит. Сборка ~80 с при интервале 1 ч. Замок старше 90 мин
#   считается зависшим. retry_count = 0: повтор Scheduler'а при таймауте HTTP мог бы
#   стартовать вторую сборку — замок её бы отсёк, но смысла в ней нет.
#
# ОТКАЗ: ASSERT до подмены или ошибка в транзакции → ROLLBACK, таблица остаётся от
#   последней успешной сборки, журнал EXECUTIVE_V2_BUILD_LOG = FAILED, статус виден
#   в карточке свежести Executive («Последняя сборка»).
#
# ПРАВА — минимально необходимые:
#   jobUser (проект) — запускать query-job;
#   dataViewer на wb_mart / wb_raw / evetis_ref — канонические V_DASH_* читают эти датасеты
#     (проверено referenced_tables сборки 17.09.2026);
#   dataEditor ПОТАБЛИЧНО только на три таблицы слоя. Ни одна production-таблица на запись
#   не выдаётся.
#
# APPLY — только целевой, с просмотренным планом (CLAUDE.md, STAGE_0_CLOSURE §5):
#   terraform plan -target=… (список в docs, §Оркестрация). Ожидаемый план:
#   1 SA + 1 project IAM + 3 dataset IAM + 3 table IAM + 1 actAs (iam.tf) + 1 scheduler = 10 to add.

resource "google_service_account" "exec_v2_layer" {
  account_id   = "sa-exec-v2-layer"
  display_name = "EVETIS Executive V2 daily layer build (PROD)"
}

resource "google_project_iam_member" "exec_v2_layer_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.exec_v2_layer.email}"
}

locals {
  exec_v2_layer_read_datasets = toset([var.mart_dataset, var.raw_dataset, "evetis_ref"])
  # Таблицы, которые пишет sp_build_executive_v2_daily (DELETE/INSERT/UPDATE).
  exec_v2_layer_write_tables = toset([
    "EXECUTIVE_V2_DAILY",
    "EXECUTIVE_V2_BUILD_LOG",
    "_EXECUTIVE_V2_BUILD_LOCK",
  ])
}

resource "google_bigquery_dataset_iam_member" "exec_v2_layer_read" {
  for_each   = local.exec_v2_layer_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.exec_v2_layer.email}"
}

resource "google_bigquery_table_iam_member" "exec_v2_layer_write" {
  for_each   = local.exec_v2_layer_write_tables
  dataset_id = var.mart_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.exec_v2_layer.email}"
}

resource "google_cloud_scheduler_job" "exec_v2_layer_build" {
  name      = "executive-v2-layer-build"
  region    = var.region
  schedule  = "10 7-23 * * *"
  time_zone = "Europe/Moscow"

  paused = false

  # Сборка ~80 с (замер 17.09: 78 с, 1,8 ГБ, 13,1 тыс. слот-с). jobs.insert возвращает
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
          query        = "CALL `${var.mart_dataset}.sp_build_executive_v2_daily`('scheduler')"
          useLegacySql = false
        }
        labels = {
          evetis-job = "executive-v2-layer"
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.exec_v2_layer.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
  ]
}
