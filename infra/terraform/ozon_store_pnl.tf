# ══════════════════════════════════════════════════════════════════════════════
# OZON STORE P&L — D4 (OWNER ACK 2026-10-15): ежедневная публикация управленческого P&L магазина Ozon во вкладку
# «Ozon Магазин P&L» книги Юнитки. Док: docs/finance/OZON_STORE_PNL_PHASE_D4.md. Код: cloud/src/loaders/unitka/ozonstorepnl/.
# Источник — только ozon_mart.V_OZON_STORE_PNL_MONTHLY (D3, развёрнут 2026-10-15) + детектор и покрытие для гейта.
#
# Отдельный Job в образе wb-loader (`node dist/cli.js ozon-store-pnl`) под sa-loaders-prod:
#   • BigQuery — только чтение ozon_mart (права уже выданы ozon_unitka.tf); таблиц и прав здесь не создаётся;
#   • Sheets — без OZON_STORE_PNL_PUBLISH=1 токен со scope spreadsheets.readonly (запись невозможна);
#     с флагом — запись ТОЛЬКО во вкладку sheetId = OZON_STORE_PNL_SHEET_ID (assertOnlyTab), узкое исключение F-18
#     (docs/ops/SECURITY_BACKLOG.md) — действует только после OWNER ACK первой записи.
#
# Rollout (каждый шаг — решение владельца):
#   1) targeted apply ЭТОГО файла → Job, invoker, Scheduler (PAUSED), алерт;
#   2) deploy-prod — тот же digest и на ozon-store-pnl-prod;
#   3) создание вкладки и первая запись — OWNER ACK (OZON_STORE_PNL_SHEET_ID, OZON_STORE_PNL_PUBLISH=1 через gcloud);
#   4) ручной прогон → перечитывание → снять паузу (scheduler-control.yml). Никакой связи с Heldi/client_001.
# ══════════════════════════════════════════════════════════════════════════════

locals {
  ozon_store_pnl_env = {
    LOADER_NAME           = "ozon-store-pnl"
    UNITKA_SPREADSHEET_ID = "1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg"
    # Публикация выключена. Env Job'а под ignore_changes: живые значения ставятся gcloud по OWNER ACK,
    # здесь — значения при пересоздании Job'а. sheetId вкладки появится после её создания (OWNER ACK).
    OZON_STORE_PNL_PUBLISH  = "0"
    OZON_STORE_PNL_SHEET_ID = ""
  }
}

resource "google_cloud_run_v2_job" "ozon_store_pnl_prod" {
  name                = "ozon-store-pnl-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.container_image
        args  = ["ozon-store-pnl"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ozon_store_pnl_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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
resource "google_cloud_run_v2_job_iam_member" "scheduler_ozon_store_pnl_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.ozon_store_pnl_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# 10:20 UTC = 13:20 МСК: финансы Ozon загружены (~03:30 UTC), Юнитка Ozon (07:00 UTC) и P&L WB (10:00 UTC) отработали.
# Готовность источников проверяет сам загрузчик (гейт свежести): при несвежих данных публикации нет, алерт по коду.
# Создаётся НА ПАУЗЕ: снимается только по OWNER ACK после первой ручной публикации.
resource "google_cloud_scheduler_job" "ozon_store_pnl_prod" {
  name             = "ozon-store-pnl-prod"
  region           = var.region
  schedule         = "20 10 * * *"
  time_zone        = "Etc/UTC"
  paused           = true
  attempt_deadline = "600s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ozon_store_pnl_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "ozon_store_pnl_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "OZON STORE P&L: вкладка «Ozon Магазин P&L» не обновилась"
  combiner     = "OR"

  conditions {
    display_name = "ошибка с кодом или отказ исполнения ozon-store-pnl-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="ozon-store-pnl-prod"
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
    content   = "Вкладка «Ozon Магазин P&L» не обновилась и показывает прошлую опубликованную версию. Коды: OZON_STORE_PNL_PUBLISH_GATE — неразобранные операции, нарушение тождества или несошедшиеся деньги (публикация заблокирована); OZON_STORE_PNL_SOURCE_NOT_READY — источники Ozon не свежие; OZON_STORE_PNL_TAB_EDITED / TAB_UNTRACKED — вкладку правили вручную; OZON_STORE_PNL_PUBLISH_READBACK / OTHER_TABS_CHANGED — перечитывание не совпало. Runbook: docs/finance/OZON_STORE_PNL_PHASE_D4.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
