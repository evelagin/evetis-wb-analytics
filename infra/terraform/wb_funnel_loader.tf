# ============================================================================
# UNITKA 2.0 R3 — загрузчик воронки продаж WB (READ-ONLY).
#
# Зачем: «Переходы» (openCount) и «Положили в корзину» (cartCount) —
# единственные показатели юнитки, которых нет ни в одном датасете. Клики по
# рекламе ими НЕ являются (01.08.2026: в книге переходов 103, кликов в BQ 52),
# поэтому подменять одно другим нельзя.
#
# Тот же образ и тот же аналитический хост, что у загрузчика остатков, но свой
# секрет WB_TOKEN_REPORTS (владелец создал вручную, R6). WB_TOKEN_ANALYTICS не
# трогается: на нём живут остатки и платное хранение.
#
# UNITKA 2.0 R7 (11.09.2026). 403 «Report not available» на POST
# /api/v2/nm-report/downloads был не про права токена: CSV-отчёт воронки WB отдаёт
# только с подпиской «Джем», которой у EVETIS нет. Загрузчик переведён на
# синхронный POST /api/analytics/v3/sales-funnel/products/history — без Джема,
# но глубиной максимум неделя, поэтому окно 7 суток.
#
#    Порядок включения:
#      1) infra.yml → apply  — выдаёт sa-loaders-prod secretAccessor
#                              ПОРЕСУРСНО на WB_TOKEN_REPORTS;
#      2) deploy-prod.yml    — продвигает образ в wb-funnel-prod и ставит
#                              WB_ANALYTICS_SECRET=WB_TOKEN_REPORTS
#                              (env у Job в ignore_changes на UPDATE, поэтому
#                              значение отсюда действует только на CREATE);
#      3) ручной прогон Job  — и только после успеха снимается pause у Scheduler.
# ============================================================================

locals {
  funnel_env = {
    LOADER_NAME               = "funnel"
    WB_ANALYTICS_HOST         = "https://seller-analytics-api.wildberries.ru"
    WB_ANALYTICS_SECRET       = "WB_TOKEN_REPORTS"
    FUNNEL_RAW_TABLE          = "RAW_WB_FUNNEL_DAILY"
    FUNNEL_OBSERVATIONS_TABLE = "WB_FUNNEL_OBSERVATIONS"
    # 7 суток: WB досчитывает воронку задним числом (однодневное окно навсегда
    # зафиксировало бы первую, неполную версию дня), а глубже недели без «Джема»
    # метод не отдаёт.
    FUNNEL_LOOKBACK_DAYS = "7"
  }
}

resource "google_cloud_run_v2_job" "wb_funnel_prod" {
  name                = "wb-funnel-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # Как у цен и тарифов: ретрай Cloud Run идёт в том же execution, то есть
      # с тем же run_id и тем же окном. Настоящий ретрай — следующие сутки.
      max_retries = 0
      # Синхронный метод: запрос на каждые 20 nmID с паузой 21 с (лимит WB 3/мин).
      timeout = "900s"
      containers {
        image = var.container_image
        args  = ["funnel"]
        dynamic "env" {
          for_each = merge(local.common_env, local.funnel_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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

# 🔴 Право запуска — ПОРЕСУРСНО. Пропуск этой строки в PR-1 привёл к тихому
# PERMISSION_DENIED без execution. Не повторяем.
resource "google_cloud_run_v2_job_iam_member" "scheduler_funnel_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_funnel_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# Права на уровне таблиц, а не датасета: загрузчику нужны ровно две таблицы.
resource "google_bigquery_table_iam_member" "prod_write_funnel_raw" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_FUNNEL_DAILY"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_funnel_observations" {
  dataset_id = var.raw_dataset
  table_id   = "WB_FUNNEL_OBSERVATIONS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_cloud_scheduler_job" "wb_funnel_prod" {
  name   = "wb-funnel-prod"
  region = var.region
  # 06:30 UTC = 09:30 МСК: сутки D-1 у WB к этому времени закрыты.
  schedule  = "30 6 * * *"
  time_zone = "Etc/UTC"
  paused    = true # снимается только после успешного ручного прогона

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_funnel_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
