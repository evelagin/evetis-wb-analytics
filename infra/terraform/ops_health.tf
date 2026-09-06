# ── Stage ADS-1B: планировщик детектора здоровья ─────────────────────────────
# Док: docs/ADS1B_HEALTH_DETECTOR_2026-09-06.md · SQL: sql/ops/ads1b_health_detector.sql
#
# 🔴 НЕ ПРИМЕНЁН. Объекты BigQuery (процедуры и вью) развёрнуты и проверены,
#    но АВТОМАТИЧЕСКИЙ ЗАПУСК требует terraform apply. До apply детектор
#    запускается вручную: CALL `wb_ops.sp_evaluate_pipeline_health`().
#
# 🔴 ПОЧЕМУ APPLY НЕ СДЕЛАН С РАБОЧЕЙ МАШИНЫ (проверено 06.09.2026, два блокера).
#
#   1. ПЛАН СОДЕРЖИТ UNRELATED ИЗМЕНЕНИЯ — 4 штуки, ни одно не относится к ADS-1B:
#        google_cloud_run_v2_job.ozon_runtime["ozon-runtime-daily"]    ~ in-place
#        google_cloud_run_v2_job.ozon_runtime["ozon-runtime-fast"]     ~ in-place
#        google_cloud_run_v2_job.ozon_runtime["ozon-runtime-weekly"]   ~ in-place
#        google_cloud_run_v2_job.wb_stocks_shadow                      ~ in-place
#      Первые три — от НЕЗАКОММИЧЕННОЙ правки infra/terraform/ozon_ingestion.tf
#      (Stage 3.4D.2/3.4D.3, смена ozon_runtime_image и сущность seller_info).
#      Четвёртое — дрейф: у wb-stocks-shadow затёрлись бы client/client_version
#      ("gcloud"/"568.0.0" -> null), то есть job последний раз трогали мимо Terraform.
#      `terraform apply` применяет ВСЮ конфигурацию, а не файл, поэтому включение
#      детектора протащило бы с собой чужой Ozon-релиз. Это прямое STOP-условие.
#
#   2. `terraform plan` ЗАВЕРШАЕТСЯ КОДОМ 1. Одиннадцать ПРЕДСУЩЕСТВУЮЩИХ ресурсов
#      падают на refresh с HTTP 403 `getIamPolicy` (LOADER_RUNS, RAW_WB_STOCKS__CR,
#      WB_STOCKS_SNAPSHOTS__CR, prod/shadow_read_ref*, prod_edit_mart, prod_view_raw).
#      Это не дефект конфигурации: у локальной учётки нет прав читать IAM-политики —
#      ровно та же стена 403, что у BigQuery REST с этой машины. Ни один из ресурсов
#      ops_health в ошибках НЕ фигурирует; все шесть корректно планируются к созданию.
#
# ШТАТНЫЙ ПУТЬ ПРИМЕНЕНИЯ — .github/workflows/infra.yml (workflow_dispatch,
#   action=apply, WIF + TERRAFORM_APPLY_SA, ручной approval через environment `infra`).
#   Инфраструктура этого проекта применяется из CI с привилегированным SA, а не с
#   ноутбука — именно поэтому локальная учётка и получает 403. Порядок:
#     а) решить, что делать с четырьмя unrelated изменениями (отдельно применить
#        Ozon-релиз или сначала закоммитить/откатить ozon_ingestion.tf);
#     б) влить эту декларацию в main;
#     в) запустить workflow `infra` с action=apply и подтвердить approval.
#
# Ожидаемый результат apply: Plan: 6 to add, 0 to change, 0 to destroy
#   (после того как unrelated изменения будут разведены с ADS-1B).
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

  # 🔴 ОТЛИЧИЕ ОТ scheduler.tf — осознанное. Загрузчики создаются `paused = true` с
  # `ignore_changes = [paused]`, потому что их состояние pause/resume принадлежит
  # .github/workflows/scheduler-control.yml. Этот workflow управляет ТОЛЬКО списком
  # [wb-stocks, wb-mart] (type: choice), то есть `wb-ops-health-prod` им не владеется
  # НИКЕМ, кроме Terraform. Поэтому здесь ни `ignore_changes`, ни стартовой паузы нет:
  # иначе repo объявлял бы paused, а production работал бы enabled — ровно то
  # расхождение repo↔production, которое чинили в ADS-1A.
  paused = false

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

  depends_on = [google_project_service.enabled]
}
