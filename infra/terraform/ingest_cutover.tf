# ══════════════════════════════════════════════════════════════════════════════
# Этап C (OWNER ACK 11.11.2026): координатор cutover и единый writer-ownership gate WB.
# Док: docs/ops/INGEST_CUTOVER_COORDINATOR.md. Код: cloud/src/loaders/cutover/, cloud/src/ownership/.
# DDL таблиц владения — sql/ops/ingest_writer_ownership.sql (применяет владелец ДО этого файла:
# table-level IAM ниже ссылается на уже существующие таблицы).
#
# Всё под флагом var.cutover_coordinator_enabled (default false; GitHub Variable
# CUTOVER_COORDINATOR_ENABLED = 'true' → infra.yml). Флаг false → ни одного ресурса этого файла.
#
# Права координатора (sa-ingest-cutover) — минимальные:
#   • своя роль: bigquery.jobs.create + config.get (запуск SELECT/CAS, без побочных прав jobUser — Dataform,
#     Gemini) и bigquery.jobs.listAll (INFORMATION_SCHEMA.JOBS_BY_PROJECT: дренаж и доказательство gate);
#   • dataEditor ТОЛЬКО на две таблицы wb_ops (владение, журнал переходов); квалификацию — только чтение;
#   • dataViewer ТОЛЬКО на журналы и RAW, нужные для доказательств (поресурсно, не на датасет);
#   • НИКАКИХ прав на Cloud Scheduler, Cloud Run admin и секреты WB: координатор не трогает ни триггеры,
#     ни расписания, ни Job'ы — только строку владения.
# ══════════════════════════════════════════════════════════════════════════════

locals {
  # Пишет координатор: только владение и журнал переходов. Квалификацию — только читает.
  cutover_ops_tables = {
    ownership = "INGEST_WRITER_OWNERSHIP"
    log       = "INGEST_CUTOVER_LOG"
  }
  cutover_qualification_table = "INGEST_QUALIFICATION"
  # Журналы и RAW потоков в wb_raw — ровно то, что читают доказательства (cloud/src/loaders/cutover/store.ts).
  cutover_evidence_tables = toset([
    "LOADER_RUNS", "INGEST_RUNS", "WB_STOCKS_SNAPSHOTS", "RAW_WB_ORDERS", "RAW_WB_SALES_RETURNS",
    "RAW_WB_ADV_COSTS_RUNS", "FINANCE_LOADER_RUNS", "RAW_WB_SUPPLIES", "REF_SYNC_RUNS", "REF_ACTIVE_VERSION",
  ])
  # Prod-SA писателей потоков: читают ТОЛЬКО строку владения (gate cli.ts). SA потоков M2–M5 создают их
  # Terraform-файлы флагами mN_prod_enabled; привязка — когда SA уже есть (var.cutover_gate_reader_streams).
  cutover_stream_prod_sas = {
    statistics = "sa-wb-statistics-prod@${var.project_id}.iam.gserviceaccount.com"
    ads        = "sa-wb-ads-prod@${var.project_id}.iam.gserviceaccount.com"
    finance    = "sa-wb-finance-prod@${var.project_id}.iam.gserviceaccount.com"
    supplies   = "sa-wb-supplies-prod@${var.project_id}.iam.gserviceaccount.com"
    ref        = "sa-ref-sync-prod@${var.project_id}.iam.gserviceaccount.com"
  }
  # Кто законно пишет таблицы потока (INFORMATION_SCHEMA.JOBS.user_email): prod-SA Cloud Run по потокам.
  cutover_writer_sas = merge(local.cutover_stream_prod_sas, { stocks = "sa-loaders-prod@${var.project_id}.iam.gserviceaccount.com" })
  cutover_env = {
    LOADER_NAME              = "cutover"
    WRITER_OWNERSHIP_DATASET = "wb_ops"
    # tick — так зовёт Scheduler. Команды (fence/activate/rollback/status) — разовым исполнением с
    # --update-env-vars CUTOVER_ACTION=…,CUTOVER_STREAM=… (docs/ops/INGEST_CUTOVER_COORDINATOR.md §6).
    CUTOVER_ACTION = "tick"
    # Пусто: tick сам fence не ставит ни одному потоку (колонка auto_cutover_enabled тоже false).
    CUTOVER_AUTO_STREAMS    = ""
    CUTOVER_STREAM_PROD_SAS = jsonencode(local.cutover_writer_sas)
    # Учётная запись владельца, от которой работает Apps Script (персональные данные — не в репозитории:
    # GitHub Variable CUTOVER_APPS_SCRIPT_USER). Пусто — fence не ставится: доказательство gate невозможно.
    CUTOVER_APPS_SCRIPT_USER = var.cutover_apps_script_user
  }
  qualify_env = {
    LOADER_NAME              = "qualify"
    WRITER_OWNERSHIP_DATASET = "wb_ops"
  }
}

resource "google_service_account" "ingest_cutover" {
  count        = var.cutover_coordinator_enabled ? 1 : 0
  account_id   = "sa-ingest-cutover"
  display_name = "WB ingest cutover coordinator (PROD)"
}

resource "google_project_iam_custom_role" "ingest_cutover_bigquery" {
  count       = var.cutover_coordinator_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ingestCutoverBigQuery"
  title       = "Ingest cutover: задания BigQuery и список заданий"
  description = "Этап C: запуск заданий (без побочных прав jobUser) и INFORMATION_SCHEMA.JOBS_BY_PROJECT для дренажа и доказательства gate."
  permissions = ["bigquery.jobs.create", "bigquery.config.get", "bigquery.jobs.listAll"]
}

resource "google_project_iam_member" "ingest_cutover_bigquery" {
  count   = var.cutover_coordinator_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.ingest_cutover_bigquery[0].id
  member  = "serviceAccount:${google_service_account.ingest_cutover[0].email}"
}

resource "google_bigquery_table_iam_member" "ingest_cutover_edit_ops" {
  for_each   = var.cutover_coordinator_enabled ? local.cutover_ops_tables : {}
  dataset_id = "wb_ops"
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest_cutover[0].email}"
}

resource "google_bigquery_table_iam_member" "ingest_cutover_read_qualification" {
  count      = var.cutover_coordinator_enabled ? 1 : 0
  dataset_id = "wb_ops"
  table_id   = local.cutover_qualification_table
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ingest_cutover[0].email}"
}

resource "google_bigquery_table_iam_member" "ingest_cutover_read_evidence" {
  for_each   = var.cutover_coordinator_enabled ? local.cutover_evidence_tables : toset([])
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ingest_cutover[0].email}"
}

# Gate cli.ts: prod-писатели читают свою строку владения. sa-loaders-prod — остатки (M1).
resource "google_bigquery_table_iam_member" "writer_gate_read_loaders_prod" {
  count      = var.cutover_coordinator_enabled ? 1 : 0
  dataset_id = "wb_ops"
  table_id   = local.cutover_ops_tables.ownership
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# deploy-prod: предпроверка «таблица есть, у SA Job'а есть чтение» (bq head / get-iam-policy) — до выката образа с gate.
resource "google_bigquery_table_iam_member" "writer_gate_read_deployer" {
  count      = var.cutover_coordinator_enabled ? 1 : 0
  dataset_id = "wb_ops"
  table_id   = local.cutover_ops_tables.ownership
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_bigquery_table_iam_member" "writer_gate_read_stream_prod" {
  for_each   = var.cutover_coordinator_enabled ? { for s, email in local.cutover_stream_prod_sas : s => email if contains(var.cutover_gate_reader_streams, s) } : {}
  dataset_id = "wb_ops"
  table_id   = local.cutover_ops_tables.ownership
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${each.value}"
}

resource "google_cloud_run_v2_job" "ingest_cutover_prod" {
  count               = var.cutover_coordinator_enabled ? 1 : 0
  name                = "ingest-cutover-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.ingest_cutover[0].email
      max_retries     = 0
      timeout         = "600s"
      containers {
        # hello до выката: образ ставит deploy-prod (only_job=ingest-cutover).
        image = var.container_image
        args  = ["cutover"]
        dynamic "env" {
          for_each = merge(local.common_env, local.cutover_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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

# 🔴 Без invoker расписание молча не запускает Job (инцидент 23.09). Право у SA планировщика, не у координатора.
resource "google_cloud_run_v2_job_iam_member" "scheduler_ingest_cutover_invoke" {
  count    = var.cutover_coordinator_enabled ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_job.ingest_cutover_prod[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# tick раз в 15 минут (тяжёлые проверки в состояниях Apps Script — раз в час). Создаётся НА ПАУЗЕ: снимает владелец после проверки «все prod-писатели видят своего
# владельца» (док §5). tick продвигает каждый поток максимум на один шаг; без auto_cutover_enabled fence не ставит.
resource "google_cloud_scheduler_job" "ingest_cutover_tick" {
  count            = var.cutover_coordinator_enabled ? 1 : 0
  name             = "ingest-cutover-tick"
  region           = var.region
  schedule         = "*/15 * * * *"
  time_zone        = "Europe/Moscow"
  paused           = true
  attempt_deadline = "600s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ingest_cutover_prod[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "ingest_cutover_attention" {
  count        = var.cutover_coordinator_enabled && var.unitka_alert_email != "" ? 1 : 0
  display_name = "INGEST CUTOVER: нужно решение владельца"
  combiner     = "OR"

  conditions {
    display_name = "ingest-cutover-prod: ошибка с кодом"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="ingest-cutover-prod"
        severity>=ERROR
        jsonPayload.code!=""
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
    content   = "Координатор переключения писателей WB требует решения. CUTOVER_STOP — нарушение gate (запись после fence); CUTOVER_UNKNOWN_OUTCOME — прогон без исхода, повтора вслепую нет; CUTOVER_ROLLBACK_REQUIRED — ошибка/дубли/разрыв покрытия у Cloud Run. Состояние: wb_ops.INGEST_WRITER_OWNERSHIP, журнал: wb_ops.INGEST_CUTOVER_LOG. Runbook: docs/ops/INGEST_CUTOVER_COORDINATOR.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

variable "cutover_coordinator_enabled" {
  type        = bool
  default     = false
  description = "Этап C: SA, права, Job ingest-cutover-prod и Scheduler ingest-cutover-tick (PAUSED). GitHub Variable CUTOVER_COORDINATOR_ENABLED."
}

variable "cutover_gate_reader_streams" {
  type        = list(string)
  default     = []
  description = "Этап C: потоки, чьим prod-SA выдать чтение строки владения (gate cli.ts). Только когда SA потока уже создан флагом mN_prod_enabled. GitHub Variable CUTOVER_GATE_READER_STREAMS (JSON-список)."
  validation {
    condition     = alltrue([for s in var.cutover_gate_reader_streams : contains(["statistics", "ads", "finance", "supplies", "ref"], s)])
    error_message = "cutover_gate_reader_streams: только statistics, ads, finance, supplies, ref."
  }
}

variable "cutover_apps_script_user" {
  type        = string
  default     = ""
  description = "Этап C: email учётной записи, от которой работает Apps Script (INFORMATION_SCHEMA.JOBS.user_email). GitHub Variable CUTOVER_APPS_SCRIPT_USER; в репозиторий не пишется."
}

# ══════════════════════════════════════════════════════════════════════════════
# Писатель квалификации (повторное ревью #426): Job ingest-qualify-prod, тот же образ, args ["qualify"].
# Паритет shadow ↔ prod по sql/mig_*_parity.sql потоков (в образе — /app/parity-sql) → INGEST_QUALIFICATION.
# Права: свои задания BigQuery; чтение wb_raw ЦЕЛИКОМ (таблицы __CR появляются и исчезают с флагами
# shadow — табличную привязку к ним не выдать); запись ТОЛЬКО в INGEST_QUALIFICATION. Ни Scheduler, ни Run, ни секретов.
# ══════════════════════════════════════════════════════════════════════════════

resource "google_service_account" "ingest_qualify" {
  count        = var.cutover_coordinator_enabled ? 1 : 0
  account_id   = "sa-ingest-qualify"
  display_name = "WB ingest qualification writer (PROD)"
}

resource "google_project_iam_custom_role" "ingest_qualify_bigquery" {
  count       = var.cutover_coordinator_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ingestQualifyBigQuery"
  title       = "Ingest qualify: задания BigQuery"
  description = "Этап C: запуск SELECT паритета и INSERT вердиктов без побочных прав jobUser."
  permissions = ["bigquery.jobs.create", "bigquery.config.get"]
}

resource "google_project_iam_member" "ingest_qualify_bigquery" {
  count   = var.cutover_coordinator_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.ingest_qualify_bigquery[0].id
  member  = "serviceAccount:${google_service_account.ingest_qualify[0].email}"
}

resource "google_bigquery_dataset_iam_member" "ingest_qualify_read_raw" {
  count      = var.cutover_coordinator_enabled ? 1 : 0
  dataset_id = var.raw_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ingest_qualify[0].email}"
}

resource "google_bigquery_table_iam_member" "ingest_qualify_write" {
  count      = var.cutover_coordinator_enabled ? 1 : 0
  dataset_id = "wb_ops"
  table_id   = local.cutover_qualification_table
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest_qualify[0].email}"
}

resource "google_cloud_run_v2_job" "ingest_qualify_prod" {
  count               = var.cutover_coordinator_enabled ? 1 : 0
  name                = "ingest-qualify-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.ingest_qualify[0].email
      max_retries     = 0
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["qualify"]
        dynamic "env" {
          for_each = merge(local.common_env, local.qualify_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
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

resource "google_cloud_run_v2_job_iam_member" "scheduler_ingest_qualify_invoke" {
  count    = var.cutover_coordinator_enabled ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_job.ingest_qualify_prod[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# 10:40 МСК: после утренних shadow-прогонов потоков (самый поздний — ref 08:32) и до дневных. Раз в сутки
# (паритет ~0,1 ГБ на поток). Создаётся НА ПАУЗЕ.
resource "google_cloud_scheduler_job" "ingest_qualify" {
  count            = var.cutover_coordinator_enabled ? 1 : 0
  name             = "ingest-qualify"
  region           = var.region
  schedule         = "40 10 * * *"
  time_zone        = "Europe/Moscow"
  paused           = true
  attempt_deadline = "600s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ingest_qualify_prod[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
