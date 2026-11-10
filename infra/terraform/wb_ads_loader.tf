# ============================================================================
# M3 — реклама WB: перенос runWbAdsDaily (+ catch-up) и недельного аудита расходов из Apps Script.
# Код: cloud/src/loaders/ads/*. Runbook: docs/ops/MIG_ADS_RUNBOOK.md.
#
#   wb-ads-shadow             пишет ТОЛЬКО RAW_WB_ADV_*__CR (9 таблиц ниже), читает V_ADV_CAMPAIGN_STATS /
#                             V_ADV_COSTS (scope и телеметрия Ads-2 — как Apps Script);
#   wb-ads-prod               боевые RAW_WB_ADV_* — только после паритета и OWNER ACK M3;
#   wb-ads-costs-audit-*      ADSAUDIT_ D−46…D−8 раз в неделю; prod — только после ACK ADS-UTC-EDGE (PR #370)
#                             И ADS_COSTS_AUDIT_ENABLED=1 (без флага загрузчик отказывает сам).
#
# 🔴 Все расписания создаются PAUSED. Применение этого файла НИЧЕГО не пишет в боевые таблицы:
#    prod-Job без запуска не исполняется, а загрузчик проверяет контур до любого вызова WB/BigQuery
#    (cloud/src/loaders/ads/targets.ts) — prod с `__CR` или shadow с боевым именем падают сразу.
# 🔴 Вью V_ADV_* здесь не создаются и не меняются. wbAdsBqCreateViews() не воспроизводится нигде.
#
# Почему расписания такие:
#   prod   05:00 / 06:00 / 08:00 МСК — одна задача и три окна вместо runWbAdsDaily (~05:00) и
#          runWbAdsDailyCatchUp (~06:15, ~08:15). Повторное окно ничего не делает при COMPLETE за D−1
#          (lease cli = SKIP_COMPLETE), ждёт свежий STARTED (< 30 мин = SKIP_RUNNING) и повторяет ERROR.
#          Витрина (07/09/12/16 МСК) видит COMPLETE до первого окна, как сейчас.
#   shadow 05:40 / 07:40 МСК — НЕ пересекается с Apps Script (05:00–05:15, catch-up 06:15/08:15):
#          лимиты Advert API общие на продавца (fullstats 3 запроса/мин), параллельный прогон дал бы 429
#          обоим. Ads-2 в shadow читает V_ADV_CAMPAIGN_STATS ПОСЛЕ загрузки Apps Script — scope одинаков.
#   audit  воскресенье 04:30 МСК — до суточного прогона; upd 1 запрос/с, окон два.
# ============================================================================

locals {
  # Зеркало живых таблиц wb_raw (tables.get, 10.11.2026): все колонки STRING, партиция по времени
  # загрузки (DAY, без поля), кластеризация — как у боевых. Порядок колонок = порядок живой схемы.
  ads_raw = {
    RAW_WB_ADV_CAMPAIGNS = {
      partition = true, cluster = ["advertId"]
      columns = ["load_ts", "run_id", "period_from", "period_to", "source_method", "processed_status",
        "advertId", "type", "status", "payment_type", "name", "campName",
      "create_time", "change_time", "start_time", "end_time", "nm_ids", "nm_count", "raw_json"]
    }
    RAW_WB_ADV_CAMPAIGN_STATS = {
      partition = true, cluster = ["date", "advertId", "nmId"]
      columns = ["load_ts", "run_id", "period_from", "period_to", "source_method", "processed_status",
        "advertId", "date", "appType", "nmId", "name", "views", "clicks", "ctr", "cpc", "cr", "atbs", "orders",
      "canceled", "shks", "sum", "sum_price", "source_level", "raw_json"]
    }
    RAW_WB_ADV_BOOSTER_STATS = {
      partition = true, cluster = ["date", "advertId", "nmId"]
      columns = ["load_ts", "run_id", "period_from", "period_to", "source_method", "processed_status",
      "advertId", "date", "nmId", "avg_position", "raw_json"]
    }
    RAW_WB_ADV_COSTS = {
      partition = true, cluster = ["advertId"]
      columns = ["load_ts", "run_id", "period_from", "period_to", "source_method", "processed_status",
        "updTime", "updDate", "updNum", "updSum", "advertId", "campName", "advertType", "paymentType",
      "advertStatus", "raw_json", "window_index"]
    }
    # Живая RAW_WB_ADV_COSTS_RUNS — без партиции и кластера (её создал DDL Stage 3B.1, а не Apps Script).
    RAW_WB_ADV_COSTS_RUNS = {
      partition = false, cluster = []
      columns = ["load_ts", "run_id", "source_method", "window_index", "window_completed_at", "period_from",
        "period_to", "requested_days", "http_status", "http_success", "returned_rows", "returned_days",
      "returned_min_date", "returned_max_date", "rows_out_of_window", "duration_ms", "status", "error_message"]
    }
    RAW_WB_ADV_QUERY_BIDS = {
      partition = true, cluster = ["advert_id", "nm_id"]
      columns = ["load_ts", "run_id", "source_method", "processed_status", "snapshot_ts", "snapshot_date",
        "advert_id", "nm_id", "norm_query", "bid", "bid_kopecks", "currency", "payment_type", "bid_type",
      "campaign_status", "raw_json"]
    }
    RAW_WB_ADV_QUERY_BIDS_RUNS = {
      partition = true, cluster = []
      columns = ["load_ts", "run_id", "source_method", "snapshot_ts", "snapshot_date", "requested_pairs",
        "expected_batches", "requested_batches", "http_status", "http_success", "returned_bid_rows",
      "distinct_pairs_with_bids", "duration_ms", "status", "error_message"]
    }
    RAW_WB_ADV_QUERY_STATS = {
      partition = true, cluster = ["advert_id", "nm_id"]
      columns = ["load_ts", "run_id", "source_method", "processed_status", "period_from", "period_to",
        "advert_id", "nm_id", "norm_query", "views", "clicks", "ctr", "cpc", "cpm", "avg_pos", "atbs",
      "orders", "shks", "spend", "currency", "raw_json"]
    }
    RAW_WB_ADV_QUERY_STATS_RUNS = {
      partition = true, cluster = []
      columns = ["load_ts", "run_id", "source_method", "period_from", "period_to", "requested_pairs",
        "expected_batches", "requested_batches", "returned_packs", "http_status", "http_success",
        "returned_rows", "distinct_pairs_with_rows", "max_keys_per_pair", "rows_with_views", "scope_pairs",
      "scope_spend_rub", "day_spend_costs_rub", "duration_ms", "status", "error_message"]
    }
  }

  # Имя таблицы → переменная окружения загрузчика (cloud/src/loaders/ads/targets.ts, ADS_TABLE_ENV).
  ads_table_env = {
    RAW_WB_ADV_CAMPAIGNS        = "ADS_CAMPAIGNS_TABLE"
    RAW_WB_ADV_CAMPAIGN_STATS   = "ADS_CAMPAIGN_STATS_TABLE"
    RAW_WB_ADV_BOOSTER_STATS    = "ADS_BOOSTER_STATS_TABLE"
    RAW_WB_ADV_COSTS            = "ADS_COSTS_TABLE"
    RAW_WB_ADV_COSTS_RUNS       = "ADS_COSTS_RUNS_TABLE"
    RAW_WB_ADV_QUERY_BIDS       = "ADS_QUERY_BIDS_TABLE"
    RAW_WB_ADV_QUERY_BIDS_RUNS  = "ADS_QUERY_BIDS_RUNS_TABLE"
    RAW_WB_ADV_QUERY_STATS      = "ADS_QUERY_STATS_TABLE"
    RAW_WB_ADV_QUERY_STATS_RUNS = "ADS_QUERY_STATS_RUNS_TABLE"
  }

  # Что ЧИТАЕТ загрузчик (Ads-2 scope + телеметрия). Вью не authorized → нужны и базовые таблицы.
  # V_ADV_COSTS = SELECT * FROM V_ADV_COSTS_SNAPSHOT → RAW_WB_ADV_COSTS + RAW_WB_ADV_COSTS_RUNS.
  ads_read_objects = [
    "V_ADV_CAMPAIGN_STATS", "RAW_WB_ADV_CAMPAIGN_STATS",
    "V_ADV_COSTS", "V_ADV_COSTS_SNAPSHOT", "RAW_WB_ADV_COSTS", "RAW_WB_ADV_COSTS_RUNS",
  ]

  ads_common_env = {
    WB_ADS_HOST   = "https://advert-api.wildberries.ru"
    WB_ADS_SECRET = "WB_TOKEN_ADS"
  }
  ads_shadow_tables_env = { for t, v in local.ads_table_env : v => google_bigquery_table.ads_cr[t].table_id }
  ads_prod_tables_env   = { for t, v in local.ads_table_env : v => t }
}

# ── Теневые таблицы ─────────────────────────────────────────────────────────
resource "google_bigquery_table" "ads_cr" {
  for_each            = local.ads_raw
  dataset_id          = var.raw_dataset
  table_id            = "${each.key}__CR"
  deletion_protection = false

  dynamic "time_partitioning" {
    for_each = each.value.partition ? [1] : []
    content {
      type = "DAY" # по времени загрузки, как у боевой (_PARTITIONTIME)
    }
  }
  clustering = length(each.value.cluster) > 0 ? each.value.cluster : null
  schema     = jsonencode([for c in each.value.columns : { name = c, type = "STRING", mode = "NULLABLE" }])
}

# ── IAM: shadow пишет только __CR, читает ровно scope/телеметрию Ads-2 ───────
resource "google_bigquery_table_iam_member" "shadow_write_ads_cr" {
  for_each   = google_bigquery_table.ads_cr
  dataset_id = var.raw_dataset
  table_id   = each.value.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_read_ads_sources" {
  for_each   = toset(local.ads_read_objects)
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

# ── IAM: prod пишет ровно девять боевых таблиц рекламы (читать wb_raw он уже может, bigquery.tf) ──
# Сами таблицы Terraform не создаёт — ими владеет Apps Script/DDL Stage 3B.1, как RAW_WB_STOCKS в Mig1b-0.
# Выдача права ничего не пишет: Scheduler wb-ads-prod PAUSED; первый прогон — только по OWNER ACK M3.
resource "google_bigquery_table_iam_member" "prod_write_ads" {
  for_each   = local.ads_raw
  dataset_id = var.raw_dataset
  table_id   = each.key
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}
# Секрет WB_TOKEN_ADS: secretAccessor у sa-loaders-shadow/-prod уже выдан (secrets.tf, iam.tf: local.wb_secrets).

# ── Jobs ─────────────────────────────────────────────────────────────────────
# max_retries = 0: task-retry Cloud Run идёт в ТОМ ЖЕ execution (тот же run_id в LOADER_RUNS).
# Повтор — следующее окно Scheduler с новым run_id и новым снапшотом (как catch-up Apps Script).
resource "google_cloud_run_v2_job" "wb_ads_shadow" {
  name                = "wb-ads-shadow"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.loaders_shadow.email
      max_retries     = 0
      # Критичный путь ~4–6 мин (fullstats 21 c на пачку), Ads-3 ≤ 90 c, Ads-2 ≤ 120 c + паузы.
      timeout = "1800s"
      containers {
        image = var.container_image
        args  = ["ads"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ads_common_env, local.ads_shadow_tables_env, { LOADER_NAME = "ads", ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.shadow_write_ads_cr,
    google_bigquery_table_iam_member.shadow_read_ads_sources,
  ]
}

resource "google_cloud_run_v2_job" "wb_ads_prod" {
  name                = "wb-ads-prod"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 0
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["ads"]
        dynamic "env" {
          # Боевые имена — только на CREATE (env в ignore_changes, как у остальных prod-Job'ов).
          for_each = merge(local.common_env, local.ads_common_env, local.ads_prod_tables_env, local.deploy_managed_env, { LOADER_NAME = "ads", ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, template[0].template[0].containers[0].env, client, client_version]
  }
  # Право записи — до Job: -target=wb_ads_prod втягивает девять prod_write_ads в тот же план.
  depends_on = [google_project_service.enabled, google_bigquery_table_iam_member.prod_write_ads]
}

resource "google_cloud_run_v2_job" "wb_ads_costs_audit_shadow" {
  name                = "wb-ads-costs-audit-shadow"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.loaders_shadow.email
      max_retries     = 0
      timeout         = "900s"
      containers {
        image = var.container_image
        args  = ["ads-costs-audit"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ads_common_env, local.ads_shadow_tables_env, { LOADER_NAME = "ads-costs-audit", ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_project_service.enabled, google_bigquery_table_iam_member.shadow_write_ads_cr]
}

resource "google_cloud_run_v2_job" "wb_ads_costs_audit_prod" {
  name                = "wb-ads-costs-audit-prod"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 0
      timeout         = "900s"
      containers {
        image = var.container_image
        args  = ["ads-costs-audit"]
        dynamic "env" {
          # ADS_COSTS_AUDIT_ENABLED нет намеренно: включение — отдельный шаг после ACK ADS-UTC-EDGE.
          for_each = merge(local.common_env, local.ads_common_env, local.ads_prod_tables_env, local.deploy_managed_env, { LOADER_NAME = "ads-costs-audit", ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, template[0].template[0].containers[0].env, client, client_version]
  }
  depends_on = [google_project_service.enabled]
}

# ── Право запуска — ПОРЕСУРСНО (cloud/test/scheduler_invoker_contract.test.ts) ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_audit_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_costs_audit_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_audit_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_costs_audit_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Расписания (все PAUSED; pause/resume — scheduler-control.yml) ───────────
resource "google_cloud_scheduler_job" "wb_ads_shadow" {
  name      = "wb-ads-shadow"
  region    = var.region
  schedule  = "40 5,7 * * *"
  time_zone = "Europe/Moscow"
  paused    = true
  retry_config {
    retry_count = 1 # повтор только HTTP-вызова :run; второй execution отсечёт lease
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_ads_prod" {
  name      = "wb-ads-prod"
  region    = var.region
  schedule  = "0 5,6,8 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # снимается только на переключении M3 (runbook §4)
  retry_config {
    retry_count = 1
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_ads_costs_audit_shadow" {
  name      = "wb-ads-costs-audit-shadow"
  region    = var.region
  schedule  = "30 4 * * 0"
  time_zone = "Europe/Moscow"
  paused    = true
  retry_config {
    retry_count = 1
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_costs_audit_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_ads_costs_audit_prod" {
  name      = "wb-ads-costs-audit-prod"
  region    = var.region
  schedule  = "30 4 * * 0"
  time_zone = "Europe/Moscow"
  paused    = true # только после ACK ADS-UTC-EDGE (snapshot v2) и ADS_COSTS_AUDIT_ENABLED=1
  retry_config {
    retry_count = 1
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_costs_audit_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Алерт: падение prod-прогона рекламы (по коду LoaderError, как у Юнитки) ──
# ADS_PARTIAL / ADS_ERROR закрывают freshness-гейт витрины за D−1 — владелец узнаёт до 07:00 МСК.
resource "google_monitoring_alert_policy" "wb_ads_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "WB реклама: прогон завершился ошибкой"
  combiner     = "OR"
  conditions {
    display_name = "loader_failed в логах wb-ads-prod / wb-ads-costs-audit-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name=("wb-ads-prod" OR "wb-ads-costs-audit-prod")
        severity>=ERROR
        jsonPayload.code!=""
      EOT
      label_extractors = {
        error_code = "EXTRACT(jsonPayload.code)"
        job        = "EXTRACT(resource.labels.job_name)"
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
    content   = "Реклама WB: прогон упал (код — label error_code; ADS_PARTIAL/ADS_ERROR закрывают гейт витрины за D−1). Следующее окно (06:00/08:00 МСК) повторит само. Детали — wb_raw.LOADER_RUNS (loader_name='ads') и RAW_WB_ADV_COSTS_RUNS. Runbook: docs/ops/MIG_ADS_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
