# ============================================================================
# UNITKA ENGINE v1 (Stage E1, 12.09.2026) — суточное обновление September Master.
#
# Док: docs/UNITKA_ENGINE_V1_DESIGN.md · runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md
# Код: cloud/src/loaders/unitka/ (тот же образ, dispatch по args[0] = "unitka").
# Подготовленный слой: sql/unitka/engine_v1_views.sql (wb_mart.V_UNITKA_*).
#
# Архитектура: WB API → RAW → canonical/mart → V_UNITKA_* → Engine → Google Sheets.
# Engine читает ТОЛЬКО вью, пишет только факт-ячейки закрытых дней + ставки + LCD,
# одним values.batchUpdate и только после QA-гейта. Формулы/УФ/структуру не трогает.
#
# Две среды, два SA, две книги прав:
#   SHADOW  unitka-engine-shadow  sa-loaders-shadow  Sheets scope readonly, книга — Читатель
#           считает план и сверяет с листом, журналирует, НЕ пишет (UNITKA_WRITE_ENABLED=0).
#   PROD    unitka-engine-prod    sa-loaders-prod    Sheets scope rw, книга — Редактор
#           полный цикл (UNITKA_WRITE_ENABLED=1). Scheduler создаётся НА ПАУЗЕ.
#
# 🔴 Предусловия владельца (Engine их проверяет, не обходит):
#   1) infra apply (этот файл + sheets/monitoring в apis.tf + UNITKA_ENGINE_RUNS);
#      ⚠ перед apply развести чужой дрейф плана (см. ops_health.tf).
#   2) открыть книгу 1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg:
#      sa-loaders-shadow@… — Читатель, sa-loaders-prod@… — Редактор.
#   3) снять паузу с wb-funnel-prod — иначе LAST_CLOSED_DATE стоит на 10.09 (SOURCE_STALE).
#   4) deploy-shadow.yml → ручной прогон unitka-engine-shadow → журнал.
#   Порядок acceptance: SHADOW несколько дней → один controlled write-day (снять паузу
#   prod-Scheduler'а на одно окно) → 2–3 автоматических прогона → PRODUCTION READY = YES.
# ============================================================================

locals {
  unitka_env = {
    LOADER_NAME           = "unitka"
    UNITKA_SPREADSHEET_ID = "1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg"
    UNITKA_SHEET_NAME     = "WB_Юнит_2025"
    UNITKA_MART_DATASET   = var.mart_dataset
    UNITKA_OPS_DATASET    = "wb_ops"
    UNITKA_RUNS_TABLE     = "UNITKA_ENGINE_RUNS"
    # Порог владельца: своя ставка SKU при n >= 10, иначе fallback магазина.
    UNITKA_MIN_N = "10"
    # LAST_CLOSED_DATE = MIN по гейтящим источникам (воронка, витрина). Отставание от D-1
    # на 1 сутки штатно (воронка догружается в 09:30 МСК); > 2 суток — SOURCE_STALE.
    UNITKA_MAX_LAG_DAYS = "2"
  }
}

# ── Журнал прогонов (§8 брифа). Append-only, пишут обе среды. ──────────────
resource "google_bigquery_table" "unitka_engine_runs" {
  dataset_id          = "wb_ops"
  table_id            = "UNITKA_ENGINE_RUNS"
  deletion_protection = true

  time_partitioning {
    type  = "DAY"
    field = "started_at"
  }
  clustering = ["environment", "qa_status"]

  schema = jsonencode([
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "environment", type = "STRING", mode = "REQUIRED" },
    { name = "mode", type = "STRING", mode = "REQUIRED", description = "SHADOW | WRITE" },
    { name = "started_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "completed_at", type = "TIMESTAMP" },
    { name = "last_closed_date", type = "DATE" },
    { name = "source_freshness_json", type = "STRING" },
    { name = "rows_read", type = "INT64" },
    { name = "cells_planned", type = "INT64" },
    { name = "cells_written", type = "INT64" },
    { name = "qa_status", type = "STRING", description = "PASS | FAIL | SHADOW_MATCH | SHADOW_DIFF | SHADOW_FAIL | NOT_RUN" },
    { name = "qa_json", type = "STRING" },
    { name = "error_code", type = "STRING" },
    { name = "error_message", type = "STRING" },
    { name = "git_sha", type = "STRING" },
    { name = "image_digest", type = "STRING" },
    { name = "engine_version", type = "STRING" },
  ])
}

resource "google_bigquery_table_iam_member" "unitka_runs_write" {
  for_each = {
    shadow = google_service_account.loaders_shadow.email
    prod   = google_service_account.loaders_prod.email
  }
  dataset_id = google_bigquery_table.unitka_engine_runs.dataset_id
  table_id   = google_bigquery_table.unitka_engine_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value}"
}

# ── UNITKA INTEGRITY GUARD V1 ───────────────────────────────────────────────────────
#    UNITKA_INTEGRITY_MODE = off у обоих Job'ов ниже — ТОЛЬКО значение при СОЗДАНИИ Job'а.
#    У обоих lifecycle.ignore_changes покрывает env (провайдер v7: env — set, точечно не исключить),
#    поэтому правка этого значения НЕ меняет уже созданный Job. Владелец runtime-значения — deploy-workflow:
#    deploy-shadow.yml ставит unitka-engine-shadow UNITKA_INTEGRITY_MODE=observe; deploy-prod.yml его
#    не задаёт → в prod действует код-по-умолчанию off (решение владельца D5, 1C2A).
#    Журнал issue (UNITKA_QA_ISSUES), алерты, УФ — отложены (решение D4). Копия COGS для Guard —
#    infra/terraform/unitka_cogs_publication.tf. Док: docs/UNITKA_INTEGRITY_GUARD_V1.md.

# ── Чтение подготовленного слоя. Вью wb_mart.V_UNITKA_* читают wb_raw и wb_mart,
#    поэтому SA нужен dataViewer на обоих датасетах (у prod на wb_raw он уже есть —
#    prod_view_raw; на wb_mart у prod dataEditor — prod_edit_mart). Shadow получает
#    ТОЛЬКО чтение. ─────────────────────────────────────────────────────────────
resource "google_bigquery_dataset_iam_member" "unitka_shadow_view_raw" {
  dataset_id = var.raw_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_dataset_iam_member" "unitka_shadow_view_mart" {
  dataset_id = var.mart_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

# ── Jobs ──────────────────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "unitka_engine_shadow" {
  name                = "unitka-engine-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_shadow.email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.container_image
        args  = ["unitka"]
        dynamic "env" {
          for_each = merge(local.common_env, local.unitka_env, local.deploy_managed_env, { ENVIRONMENT = "shadow", UNITKA_WRITE_ENABLED = "0", UNITKA_INTEGRITY_MODE = "off" })
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

resource "google_cloud_run_v2_job" "unitka_engine_prod" {
  name                = "unitka-engine-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # Ретрай Cloud Run шёл бы в том же execution с тем же run_id; настоящий ретрай —
      # следующее окно Scheduler'а (план идемпотентен).
      max_retries = 0
      timeout     = "600s"
      containers {
        image = var.container_image
        args  = ["unitka"]
        dynamic "env" {
          for_each = merge(local.common_env, local.unitka_env, local.deploy_managed_env, { ENVIRONMENT = "prod", UNITKA_WRITE_ENABLED = "1", UNITKA_INTEGRITY_MODE = "off" })
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

# 🔴 Право запуска — ПОРЕСУРСНО (урок PR-1: без этого тихий PERMISSION_DENIED).
resource "google_cloud_run_v2_job_iam_member" "scheduler_unitka_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.unitka_engine_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_unitka_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.unitka_engine_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Расписание: два окна МСК, оба НА ПАУЗЕ до acceptance. ────────────────────
#   10:00 МСК (07:00 UTC) — после витрины (07:04–07:06) и воронки (09:30 МСК);
#   12:30 МСК (09:30 UTC) — резерв: если утром источники отставали, день закроется здесь.
#   Второе окно ничего не пересобирает: при тех же источниках план пуст.
#   Shadow ходит по тому же расписанию, чтобы журнал SHADOW был сопоставим с prod.
locals {
  unitka_schedules = {
    morning = "0 7 * * *"
    reserve = "30 9 * * *"
  }
}

resource "google_cloud_scheduler_job" "unitka_engine_shadow" {
  for_each  = local.unitka_schedules
  name      = "unitka-engine-shadow-${each.key}"
  region    = var.region
  schedule  = each.value
  time_zone = "Etc/UTC"
  paused    = true # снимает владелец после первого ручного прогона (scheduler-control.yml)

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.unitka_engine_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "unitka_engine_prod" {
  for_each  = local.unitka_schedules
  name      = "unitka-engine-prod-${each.key}"
  region    = var.region
  schedule  = each.value
  time_zone = "Etc/UTC"
  paused    = true # controlled write-day: владелец снимает паузу на одно окно, затем — насовсем

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.unitka_engine_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Алерты (§8 брифа): любой FAIL Engine = exit 1 = execution FAILED + запись
#    `loader_failed` с кодом (SOURCE_STALE / SHEETS_API / BQ_MISMATCH / FORMULA_ERROR /
#    PARTIAL_WRITE / INVARIANT_FAIL / STRUCTURE_DRIFT / …). Алерт — по логу prod-Job'а.
#    Канал создаётся только при заданном var.unitka_alert_email. ────────────────
resource "google_monitoring_notification_channel" "unitka_email" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "UNITKA Engine — владелец"
  type         = "email"
  labels = {
    email_address = var.unitka_alert_email
  }
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "unitka_engine_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "UNITKA Engine: прогон завершился ошибкой"
  combiner     = "OR"

  conditions {
    display_name = "loader_failed в логах unitka-engine-*"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name=~"^unitka-engine-"
        severity>=ERROR
        jsonPayload.message="loader_failed"
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
    content   = "UNITKA Engine упал. Код ошибки — в label error_code; детали — в wb_ops.UNITKA_ENGINE_RUNS (error_code, error_message, qa_json). Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
