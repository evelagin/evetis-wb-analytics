# ── Stage ADS-1B: планировщик детектора здоровья ─────────────────────────────
# Док: docs/ADS1B_HEALTH_DETECTOR_2026-09-06.md · SQL: sql/ops/ads1b_health_detector.sql
#
# 🔴 НЕ ПРИМЕНЁН. Объекты BigQuery (процедуры и вью) развёрнуты и проверены,
#    но АВТОМАТИЧЕСКИЙ ЗАПУСК требует terraform apply. До apply детектор
#    запускается вручную: CALL `wb_ops.sp_evaluate_pipeline_health`().
#
# 🔴 APPLY НЕ ВЫПОЛНЕН. Authoritative CI-план (workflow `infra`, action=plan,
#    run 34048138814, 06.09.2026 17:18 UTC, из GitHub main = 52675f2):
#
#        Plan: 6 to add, 5 to change, 0 to destroy
#
#    Шесть создаваемых ресурсов — ровно ADS-1B, ошибок по ним нет. Но пять
#    in-place изменений НЕ относятся к ADS-1B, а `terraform apply` работает по
#    всей конфигурации, а не по файлу. Поэтому apply остановлен.
#
#    1) google_bigquery_table.raw_wb_stocks_cr
#         INTEGER->INT64, BOOLEAN->BOOL — косметика (псевдонимы типов BigQuery);
#         🔴 но также УДАЛЕНИЕ колонки `warehouse_code`. В проде она ЕСТЬ
#         (RAW_WB_STOCKS__CR.warehouse_code, STRING) — в bigquery.tf её нет.
#         Apply попытался бы выпилить колонку из схемы.
#    2) google_cloud_run_v2_job.ozon_runtime["ozon-runtime-daily"]
#         дрейф client/client_version ("gcloud"/"577.0.0" -> null);
#         🔴 и ОТКАТ ENTITIES: из значения пропала бы сущность `seller_info`.
#         Причина — Stage 3.4D.3 живёт ТОЛЬКО в незакоммиченном рабочем дереве
#         (infra/terraform/ozon_ingestion.tf), на main его нет. Apply откатил бы
#         рабочий Ozon-релиз.
#    3) ozon_runtime["ozon-runtime-fast"]   — только дрейф client/client_version.
#    4) ozon_runtime["ozon-runtime-weekly"] — только дрейф client/client_version.
#    5) google_cloud_run_v2_job.wb_stocks_shadow — только дрейф
#         client/client_version ("gcloud"/"568.0.0" -> null).
#
#    ⚠️ ИСПРАВЛЕНИЕ ПРЕЖНЕЙ ЗАПИСИ (была в этом файле и в §10 док-а ADS-1B).
#    Ранее здесь утверждалось, что три изменения ozon_runtime вызваны
#    незакоммиченной правкой ozon_ingestion.tf. Это неверно, и CI-план это
#    показал: дрейф client/client_version существует независимо от неё, а грязный
#    файл, наоборот, МАСКИРОВАЛ откат ENTITIES — локально конфиг содержал
#    seller_info и совпадал с продом, поэтому этой части диффа видно не было.
#
# ШТАТНЫЙ ПУТЬ — .github/workflows/infra.yml (workflow_dispatch, action=apply,
#   WIF + TERRAFORM_APPLY_SA, ручной approval через environment `infra`).
#   Локальный terraform для apply непригоден: у рабочей учётки нет прав на
#   getIamPolicy, plan падает кодом 1 на 11 предсуществующих ресурсах.
#   Перед apply нужно развести пять чужих изменений с ADS-1B, иначе включение
#   детектора потянет за собой откат схемы остатков и Ozon-релиза.
#
# Ожидаемый результат apply после разведения: 6 to add, 0 to change, 0 to destroy.

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
