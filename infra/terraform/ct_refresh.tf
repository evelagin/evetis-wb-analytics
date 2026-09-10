# ── Control Tower Phase 1.1: автозапуск sp_ct_refresh_daily ───────────────────
# Док: docs/control_tower/CT_PHASE11_IMPLEMENTATION_2026-09-10.md · SQL: sql/control_tower/ct_05_procedures.sql
#
# МЕХАНИЗМ — прецедент проекта, а не новая система: Cloud Scheduler → BigQuery jobs API →
# `CALL evetis_ref.sp_ct_refresh_daily()` под отдельным SA. Ровно так уже работает в
# production `wb-ops-health-prod` (ops_health.tf, ADS-1B): проверено данными
# INFORMATION_SCHEMA.JOBS — sa-ops-health исполняет процедуру каждые 3 часа с 06.09.2026.
# Control Tower — чистый SQL-слой поверх готовых витрин; Cloud Run Job / образ ему не нужен.
#
# РАСПИСАНИЕ ВЫБРАНО ПО ДАННЫМ (LOADER_RUNS / MART_RUNS / INGEST_RUNS, 03–10.09.2026, МСК):
#   ads 05:07→05:11 · sales/orders hourly :22/:31 · WB stocks 06:30 · Ozon daily 06:30, fast 07:00/13:00/19:00
#   витрина wb-mart-prod: окна 07/09/12/16, при успехе завершается 07:04–07:06.
#   → 07:40 основное окно (после витрины и Ozon 07:00); 09:40 и 12:40 — после резервных окон витрины;
#     16:40 — после глубокого фолбэка; 19:40 — после вечерней загрузки Ozon (заявки/остатки → срез запаса).
#   Окна не пересекаются с витриной (:00–:06) и с загрузчиками (:22/:31). Пять окон = не пять
#   пересборок: процедура сама сравнивает отпечаток источников (MART_RUNS, V_DASH_SKU_DAILY, Ozon,
#   WB stocks, срез ФФ, очередь) с последним успешным прогоном за день и пишет SKIP, если ничего
#   не изменилось. Повтор после ERROR безопасен: пересборка идемпотентна, партиция — за день.
#
# НАБЛЮДАЕМОСТЬ. Каждый шаг пишет CT_REFRESH_LOG (OK / SKIP / WARN / ERROR); V_CT_REFRESH_STATUS
# сводит это в OK / STALE (> refresh_sla_hours без успеха) / ERROR и в строку
# «CONTROL TOWER UPDATED AT …» на Owner Home. Отказ на уровне вызова (403/5xx) виден в
# Cloud Scheduler (lastAttemptTime / status) и в INFORMATION_SCHEMA.JOBS по принципалу sa-ct-refresh.
#
# ПРАВА — минимально необходимые:
#   jobUser (проект) — запускать query-job;
#   dataViewer на wb_mart / wb_raw / ozon_raw / evetis_ref — LIVE-витрины читают production;
#   dataEditor ПОТАБЛИЧНО только на шесть CT_* таблиц, которые пишут процедуры (DML, без TRUNCATE).
#   Ни одна production-таблица на запись не выдаётся. Marketplace credentials не используются.
#
# APPLY — штатный путь: .github/workflows/infra.yml (workflow_dispatch, action=apply, WIF +
#   TERRAFORM_APPLY_SA, ручной approval через environment `infra`). Ожидаемый план:
#   1 SA + 1 project IAM + 4 dataset IAM + 6 table IAM + 1 actAs (iam.tf) + 1 scheduler = 14 to add.
#   До apply Control Tower обновляется ручным CALL (owner / Claude) — Owner Home честно покажет STALE.

resource "google_service_account" "ct_refresh" {
  account_id   = "sa-ct-refresh"
  display_name = "EVETIS Control Tower refresh (PROD)"
}

resource "google_project_iam_member" "ct_refresh_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ct_refresh.email}"
}

locals {
  ct_refresh_read_datasets = toset([var.mart_dataset, var.raw_dataset, "ozon_raw", "evetis_ref"])
  # Таблицы, которые пишут sp_ct_refresh_daily / sp_ct_generate_actions / sp_ct_action_update.
  ct_refresh_write_tables = toset([
    "CT_ACTUAL_DAILY",
    "CT_INVENTORY_SNAPSHOT_DAILY",
    "CT_OWNER_ACTION_QUEUE",
    "CT_ACTION_STATUS_LOG",
    "CT_REFRESH_LOG",
    "CT_CONFIG",
  ])
}

resource "google_bigquery_dataset_iam_member" "ct_refresh_read" {
  for_each   = local.ct_refresh_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ct_refresh.email}"
}

resource "google_bigquery_table_iam_member" "ct_refresh_write" {
  for_each   = local.ct_refresh_write_tables
  dataset_id = "evetis_ref"
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ct_refresh.email}"
}

resource "google_cloud_scheduler_job" "ct_refresh_prod" {
  name      = "ct-refresh-prod"
  region    = var.region
  schedule  = "40 7,9,12,16,19 * * *"
  time_zone = "Europe/Moscow"

  # Как у wb-ops-health-prod: scheduler-control.yml этим job'ом не владеет, поэтому
  # ни стартовой паузы, ни ignore_changes — repo и production должны совпадать.
  paused = false

  # Процедура укладывается в ~75 с при полной пересборке (замер 10.09: 74 107 мс); SKIP — 5 с.
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
          query        = "CALL `evetis_ref.sp_ct_refresh_daily`()"
          useLegacySql = false
        }
        labels = {
          evetis-job = "ct-refresh"
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.ct_refresh.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  # Тот же порядок, что у ops_health: Scheduler требует actAs на SA из oauth_token у
  # создающего принципала; связь через local.terraform_actas_targets (iam.tf, ключ ct_refresh).
  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
  ]
}
