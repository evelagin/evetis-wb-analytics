# ── Stage ADS-1B: планировщик детектора здоровья ─────────────────────────────
# Док: docs/ADS1B_HEALTH_DETECTOR_2026-09-06.md · SQL: sql/ops/ads1b_health_detector.sql
#
# 🔴 НЕ ПРИМЕНЁН. Объекты BigQuery (процедуры и вью) развёрнуты и проверены,
#    но АВТОМАТИЧЕСКИЙ ЗАПУСК требует terraform apply, а это изменение инфраструктуры
#    (новый service account + два IAM-биндинга + scheduler job). Решение за владельцем.
#    До apply детектор запускается вручную: CALL `wb_ops.sp_evaluate_pipeline_health`().
#
# ПОЧЕМУ ТАК, А НЕ ИНАЧЕ.
#   • Штатный механизм проекта — Cloud Scheduler + OAuth-токен (см. scheduler.tf).
#     Здесь он переиспользован БЕЗ изменений: тот же resource, тот же паттерн,
#     только целевой API другой — BigQuery jobs.insert вместо Cloud Run :run.
#     Контейнер, образ и Cloud Run job не нужны — процедура уже живёт в BigQuery.
#   • BigQuery scheduled query (Data Transfer) сознательно НЕ выбран: это новый для
#     проекта механизм, а ТЗ ADS-1B прямо требует не заводить новый, если есть штатный.
#   • Встраивание детектора в существующий job wb-mart-prod отвергнуто: это нарушило бы
#     fail-open — падение детектора роняло бы сборку витрины.
#
# FAIL-OPEN. Job ниже не связан ни с одним конвейером данных. Его отказ не блокирует
# ни ingestion, ни MART. Обратное тоже верно: отказ витрины не мешает детектору работать —
# именно это и нужно, чтобы он мог о ней сообщить.

resource "google_service_account" "ops_health" {
  account_id   = "sa-ops-health"
  display_name = "WB ops health detector (PROD)"
}

# jobUser — право запускать query-job. dataEditor на wb_ops — писать состояние,
# инциденты и события. Чтение источников идёт через уже существующий
# prod_view_raw + доступ к wb_mart; для отдельного SA их нужно выдать явно.
resource "google_project_iam_member" "ops_health_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ops_health.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_health_edit_ops" {
  dataset_id = "wb_ops"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_health_read_raw" {
  dataset_id = var.raw_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_health_read_mart" {
  dataset_id = var.mart_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

# Каденс. Самый короткий осмысленный SLA в реестре — orders.freshness_sla_minutes = 180
# (3 часа), поэтому три часа и взяты: чаще смысла нет, реже — теряем SLA.
# На инциденте 02–06.09 этого достаточно: второй подряд ERROR витрины в 06:04 UTC
# был бы замечен ближайшим прогоном, то есть на трое суток раньше фактического
# обнаружения. Ретрай — один: детектор идемпотентен, повтор безопасен.
resource "google_cloud_scheduler_job" "ops_health_prod" {
  name      = "wb-ops-health-prod"
  region    = var.region
  schedule  = "0 */3 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # включается владельцем после apply, как и остальные job'ы

  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
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
          query        = "CALL `wb_ops.sp_evaluate_pipeline_health`()"
          useLegacySql = false
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.ops_health.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
