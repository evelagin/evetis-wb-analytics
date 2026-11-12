# ============================================================================
# M3 — реклама WB: перенос runWbAdsDaily (+ catch-up) и недельного аудита расходов из Apps Script.
# Код: cloud/src/loaders/ads/*. Runbook: docs/ops/MIG_ADS_RUNBOOK.md.
#
#   wb-ads-{shadow,prod}         `ads`: campaigns + costs + fullstats → журнал 'ads' (гейт витрины);
#   wb-ads-query-{shadow,prod}   `ads-query`: хвост Ads-3 (ставки) + Ads-2 (query stats) — отдельный Job, чтобы
#                                журнал 'ads' закрывался ДО хвоста (в эталоне ingestFinalizeByStatus_ шёл раньше Ads-3);
#   wb-ads-costs-audit-*         ADSAUDIT_ D−46…D−8 раз в неделю; prod — только после ACK ADS-UTC-EDGE (PR #370)
#                                И ADS_COSTS_AUDIT_ENABLED=1 (без флага загрузчик отказывает сам).
#   shadow пишет ТОЛЬКО RAW_WB_ADV_*__CR, prod — только боевые RAW_WB_ADV_*; оба читают V_ADV_CAMPAIGN_STATS /
#   V_ADV_COSTS (scope и телеметрия Ads-2 — как Apps Script).
#
# 🔴 Свои runtime-SA `sa-wb-ads-{shadow,prod}` (а не sa-loaders-*): право писать рекламу и читать WB_TOKEN_ADS
#    получает ровно один контур, а не десяток прочих загрузчиков.
# 🔴 Два флага потока по общей схеме M2–M5 (tools/tests/test_stream_enable_flags.py), оба bool, default false:
#    `var.m3_shadow_enabled` — ВСЕ shadow-ресурсы этого файла: SA, 9 таблиц `__CR`, IAM, секрет, Job'ы, invoker,
#      Scheduler'ы. Включение — переменная репозитория M3_SHADOW_ENABLED=true на OWNER ACK SHADOW потока (runbook §7.1);
#    `var.m3_prod_enabled` — ВСЕ prod-ресурсы: SA, IAM на боевые таблицы, секрет, Job'ы, invoker, Scheduler, алерт.
#      Включение — M3_PROD_ENABLED=true на OWNER ACK M3-CUTOVER (runbook §7.3).
#    Весь файл — новый в PR #419: ни один ресурс здесь не существует в origin/main, поэтому флаг = false
#    ничего существующего не удаляет (план без флагов — 0 ресурсов этого файла).
#
# 🔴 Все расписания создаются PAUSED. Применение этого файла НИЧЕГО не пишет в боевые таблицы:
#    prod-Job без запуска не исполняется, а загрузчик проверяет контур до любого вызова WB/BigQuery
#    (cloud/src/loaders/ads/targets.ts) — prod с `__CR` или shadow с боевым именем падают сразу.
# 🔴 Вью V_ADV_* здесь не создаются и не меняются. wbAdsBqCreateViews() не воспроизводится нигде.
#
# Почему расписания такие:
#   prod   ads 05:00 / 06:00 / 08:00 МСК, ads-query 05:30 / 06:30 / 08:30 — вместо runWbAdsDaily (~05:00) и
#          runWbAdsDailyCatchUp (~06:15, ~08:15). Повторное окно ничего не делает при COMPLETE за D−1
#          (lease cli = SKIP_COMPLETE), ждёт свежий STARTED (< 30 мин = SKIP_RUNNING) и повторяет ERROR.
#          Витрина (07/09/12/16 МСК) видит COMPLETE до первого окна, как сейчас.
#          ads-query стартует, только когда последняя попытка 'ads' за тот же D−1 терминальна (иначе ERROR
#          ADS_QUERY_NOT_READY → следующее окно).
#   shadow ads 05:40 / 07:40, ads-query 06:05 / 07:55 — НЕ пересекается с Apps Script (05:00–05:15, catch-up 06:15/08:15):
#          лимиты Advert API общие на продавца (fullstats 3 запроса/мин), параллельный прогон дал бы 429
#          обоим. Ads-2 в shadow читает V_ADV_CAMPAIGN_STATS ПОСЛЕ загрузки Apps Script — scope одинаков.
#   audit  воскресенье 04:30 МСК — до суточного прогона; upd 1 запрос/с, окон два.
# ============================================================================

variable "m3_shadow_enabled" {
  description = "M3 (реклама WB): создать shadow-контур (sa-wb-ads-shadow, таблицы RAW_WB_ADV_*__CR, IAM, секрет, Job'ы, Scheduler'ы PAUSED). Только по OWNER ACK SHADOW потока M3."
  type        = bool
  default     = false
}

variable "m3_prod_enabled" {
  description = "M3 (реклама WB): создать prod-контур (SA, IAM на боевые RAW_WB_ADV_*, секрет, Job'ы, Scheduler'ы PAUSED, алерт). Только по OWNER ACK M3-CUTOVER."
  type        = bool
  default     = false
}

locals {
  ads_shadow_n = var.m3_shadow_enabled ? 1 : 0
  ads_prod_n   = var.m3_prod_enabled ? 1 : 0
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
  # Имена строкой, а не ссылкой на google_bigquery_table.ads_cr: при m3_shadow_enabled=false таблиц нет.
  ads_shadow_tables_env = { for t, v in local.ads_table_env : v => "${t}__CR" }
  ads_prod_tables_env   = { for t, v in local.ads_table_env : v => t }
}

# ── Runtime SA ──────────────────────────────────────────────────────────────
resource "google_service_account" "wb_ads_shadow" {
  count        = local.ads_shadow_n
  account_id   = "sa-wb-ads-shadow"
  display_name = "WB ads runtime (SHADOW): Advert API read + RAW_WB_ADV_*__CR"
}

resource "google_service_account" "wb_ads_prod" {
  count        = local.ads_prod_n
  account_id   = "sa-wb-ads-prod"
  display_name = "WB ads runtime (PROD): Advert API read + RAW_WB_ADV_* production"
}

locals {
  # Контур → SA (каждый — только при включённом флаге своего контура). Ключ = environment.
  ads_sa = merge(
    var.m3_shadow_enabled ? { shadow = google_service_account.wb_ads_shadow[0] } : {},
    var.m3_prod_enabled ? { prod = google_service_account.wb_ads_prod[0] } : {},
  )
  # (контур, объект чтения) — scope и телеметрия Ads-2 нужны обоим контурам.
  ads_read_grants = { for p in setproduct(keys(local.ads_sa), local.ads_read_objects) : "${p[0]}:${p[1]}" => { env = p[0], table = p[1] } }
}

# ── Теневые таблицы ─────────────────────────────────────────────────────────
resource "google_bigquery_table" "ads_cr" {
  for_each            = var.m3_shadow_enabled ? local.ads_raw : {}
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

# ── Права: задания BigQuery, журнал, свои таблицы, объекты чтения, свой секрет ─
resource "google_project_iam_member" "wb_ads_jobuser" {
  for_each = local.ads_sa
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${each.value.email}"
}

# LOADER_RUNS: lease cli.ts (MERGE/UPDATE) + проверка готовности 'ads' в ads-query.
resource "google_bigquery_table_iam_member" "wb_ads_loader_runs" {
  for_each   = local.ads_sa
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.loader_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value.email}"
}

resource "google_bigquery_table_iam_member" "wb_ads_shadow_write_cr" {
  for_each   = google_bigquery_table.ads_cr
  dataset_id = var.raw_dataset
  table_id   = each.value.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_ads_shadow[0].email}"
}

# Сами боевые таблицы Terraform не создаёт — ими владеет Apps Script/DDL Stage 3B.1 (как RAW_WB_STOCKS в Mig1b-0).
resource "google_bigquery_table_iam_member" "wb_ads_prod_write" {
  for_each   = var.m3_prod_enabled ? local.ads_raw : {}
  dataset_id = var.raw_dataset
  table_id   = each.key
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_ads_prod[0].email}"
}

# Вью не authorized → нужны и базовые таблицы. V_ADV_COSTS = V_ADV_COSTS_SNAPSHOT → COSTS + COSTS_RUNS.
resource "google_bigquery_table_iam_member" "wb_ads_read_sources" {
  for_each   = local.ads_read_grants
  dataset_id = var.raw_dataset
  table_id   = each.value.table
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${local.ads_sa[each.value.env].email}"
}

resource "google_secret_manager_secret_iam_member" "wb_ads_token" {
  for_each  = local.ads_sa
  secret_id = google_secret_manager_secret.wb["WB_TOKEN_ADS"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.email}"
}

# actAs: deployer обновляет образ Job'ов, terraform-apply создаёт Job'ы с этими SA.
resource "google_service_account_iam_member" "wb_ads_actas_deployer" {
  for_each           = local.ads_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "wb_ads_actas_terraform" {
  for_each           = local.ads_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# ── Jobs ─────────────────────────────────────────────────────────────────────
# max_retries = 0: task-retry Cloud Run идёт в ТОМ ЖЕ execution (тот же run_id в LOADER_RUNS).
# Повтор — следующее окно Scheduler с новым run_id и новым снапшотом (как catch-up Apps Script).
# timeout ≥ дедлайн обработчика + 600 c (ADS_DEADLINE_MS 1800 c, ADS_QUERY_DEADLINE_MS 480 c; targets.ts):
# после дедлайна новые вызовы WB и их повторы не начинаются, исход пишется в журнал до таймаута.
# Shadow: env принадлежит Terraform (deploy-shadow.yml меняет только образ) → в ignore_changes только image.
# Prod: env в ignore_changes, как у остальных prod-Job'ов (deploy-prod ставит IMAGE_DIGEST/GIT_SHA).
locals {
  ads_jobs = {
    "wb-ads"             = { args = "ads", loader = "ads", timeout = "2400s" }
    "wb-ads-query"       = { args = "ads-query", loader = "ads-query", timeout = "900s" }
    "wb-ads-costs-audit" = { args = "ads-costs-audit", loader = "ads-costs-audit", timeout = "900s" }
  }
  ads_schedules = {
    shadow = { "wb-ads" = "40 5,7 * * *", "wb-ads-query" = "5 6,8 * * *", "wb-ads-costs-audit" = "30 4 * * 0" }
    prod   = { "wb-ads" = "0 5,6,8 * * *", "wb-ads-query" = "30 5,6,8 * * *", "wb-ads-costs-audit" = "30 4 * * 0" }
  }
}

resource "google_cloud_run_v2_job" "wb_ads_shadow" {
  for_each            = var.m3_shadow_enabled ? local.ads_jobs : {}
  name                = "${each.key}-shadow"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.wb_ads_shadow[0].email
      max_retries     = 0
      timeout         = each.value.timeout
      containers {
        image = var.container_image
        args  = [each.value.args]
        dynamic "env" {
          for_each = merge(local.common_env, local.ads_common_env, local.ads_shadow_tables_env, { LOADER_NAME = each.value.loader, ENVIRONMENT = "shadow" })
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
    google_project_iam_member.wb_ads_jobuser,
    google_bigquery_table_iam_member.wb_ads_loader_runs,
    google_bigquery_table_iam_member.wb_ads_shadow_write_cr,
    google_bigquery_table_iam_member.wb_ads_read_sources,
    google_secret_manager_secret_iam_member.wb_ads_token,
    google_service_account_iam_member.wb_ads_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job" "wb_ads_prod" {
  for_each            = var.m3_prod_enabled ? local.ads_jobs : {}
  name                = "${each.key}-prod"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.wb_ads_prod[0].email
      max_retries     = 0
      timeout         = each.value.timeout
      containers {
        image = var.container_image
        args  = [each.value.args]
        dynamic "env" {
          # ADS_COSTS_AUDIT_ENABLED нет намеренно: включение аудита — отдельный шаг после ACK ADS-UTC-EDGE.
          for_each = merge(local.common_env, local.ads_common_env, local.ads_prod_tables_env, local.deploy_managed_env, { LOADER_NAME = each.value.loader, ENVIRONMENT = "prod" })
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
  depends_on = [
    google_project_service.enabled,
    google_project_iam_member.wb_ads_jobuser,
    google_bigquery_table_iam_member.wb_ads_loader_runs,
    google_bigquery_table_iam_member.wb_ads_prod_write,
    google_bigquery_table_iam_member.wb_ads_read_sources,
    google_secret_manager_secret_iam_member.wb_ads_token,
    google_service_account_iam_member.wb_ads_actas_terraform,
  ]
}

# ── Право запуска — ПОРЕСУРСНО (cloud/test/scheduler_invoker_contract.test.ts) ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_shadow_invoke" {
  for_each = google_cloud_run_v2_job.wb_ads_shadow
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_shadow[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_ads_prod_invoke" {
  for_each = google_cloud_run_v2_job.wb_ads_prod
  location = var.region
  name     = google_cloud_run_v2_job.wb_ads_prod[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Расписания (все PAUSED; pause/resume — scheduler-control.yml, имя = <loader>-<environment>) ──
resource "google_cloud_scheduler_job" "wb_ads_shadow" {
  for_each  = var.m3_shadow_enabled ? local.ads_jobs : {}
  name      = "${each.key}-shadow"
  region    = var.region
  schedule  = local.ads_schedules.shadow[each.key]
  time_zone = "Europe/Moscow"
  paused    = true
  retry_config {
    retry_count = 1 # повтор только HTTP-вызова :run; второй execution отсечёт lease
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_shadow[each.key].name}:run"
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
  for_each  = var.m3_prod_enabled ? local.ads_jobs : {}
  name      = "${each.key}-prod"
  region    = var.region
  schedule  = local.ads_schedules.prod[each.key]
  time_zone = "Europe/Moscow"
  paused    = true # снимается только на переключении M3 (runbook §7.3); аудит — после ACK ADS-UTC-EDGE
  retry_config {
    retry_count = 1
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_ads_prod[each.key].name}:run"
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
# ADS_QUERY_NOT_READY в 05:30 при штатной работе не возникает ('ads' к ~05:10 завершён); если возникает — сигнал.
resource "google_monitoring_alert_policy" "wb_ads_failed" {
  count        = var.m3_prod_enabled && var.unitka_alert_email != "" ? 1 : 0
  display_name = "WB реклама: прогон завершился ошибкой"
  combiner     = "OR"
  conditions {
    display_name = "loader_failed в логах wb-ads-*-prod"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name=("wb-ads-prod" OR "wb-ads-query-prod" OR "wb-ads-costs-audit-prod")
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
    content   = "Реклама WB: прогон упал (код — label error_code; ADS_PARTIAL/ADS_ERROR у wb-ads-prod закрывают гейт витрины за D−1). Следующее окно (06:00/08:00 МСК) повторит само. Детали — wb_raw.LOADER_RUNS и RAW_WB_ADV_COSTS_RUNS. Runbook: docs/ops/MIG_ADS_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
