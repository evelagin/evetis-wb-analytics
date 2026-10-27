# ── Облачное наблюдение Heldi (поток E, после S4.1a) ─────────────────────────────────────
# Предложение: HELDI_CLOUD_WATCH_PROPOSAL. Регламент: docs/ops/HELDI_CLOUD_WATCH.md.
# Заменяет локальный observe_loop владельца: 25.10 его gcloud-токен истёк в 14:56Z, и наблюдение
# умерло. Здесь нет ни одного права записи в данные, Scheduler контроллера или Run контроллера.
#
# Слой 1 — сигналы логов контроллера без кода:
#   H1 STOP     — уже есть: google_monitoring_alert_policy.controller_stopped (main.tf). Метрика
#                 tenant_controller_stopped считала STOPPED с 20:10Z 25.10 (10 за 30 мин всю ночь).
#   H2 падение  — Traceback в stderr контроллера (необработанное исключение, как kc4bg 25.10 20:09Z).
#                 «Container called exit(1)» НЕ сигнал: штатный WAITING_READ_RETRY тоже выходит с кодом 1
#                 (≈20 за 14 суток), а Traceback за те же 14 суток был ровно один — kc4bg.
#   H3 тишина   — нет ни одной строки jsonPayload.status 20 мин. Бэктест 21–27.10: вне пауз тика
#                 разрывы ≤ 17 мин (25.10 12:00 после resume), окно 15 мин дало бы ложные срабатывания.
# Слой 2 — Cloud Run Job heldi-health-eval (tools/heldi_watch/health.py) раз в 30 мин:
#   HEALTH FAIL → критическое письмо, HEALTH DEGRADED/OK (смена состояния) → уведомление,
#   нет HEARTBEAT 75 мин → сторож (наблюдатель сам ослеп).
#
# Канал — та же почта владельца (owner_email). Telegram не используется: исключение из F-18
# не дано (как у DRO-1, infra/terraform/evetis_health.tf).
locals {
  controller_stderr = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"tenant-backfill-controller\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstderr\"",
  ])
  watch_enabled = var.watch_image != null
  watch_stdout = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"heldi-health-eval\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstdout\"",
  ])
  watch_runbook = "Регламент: docs/ops/HELDI_CLOUD_WATCH.md. Наблюдатель ничего не ставит на паузу; аварийная пауза — только по действующему ACK владельца."
  # Четыре датасета, которые читает наблюдатель: те же, что у читателя контроллера (cloud_access).
  watch_datasets = ["ozon_raw", "ref", "tenant_ops", "tenant_locks"]
  watch_dataset_condition = join(" || ", flatten([for d in local.watch_datasets : [
    "resource.name == \"projects/${var.project_id}/datasets/${d}\"",
    "resource.name.startsWith(\"projects/${var.project_id}/datasets/${d}/\")",
  ]]))
  # Права наблюдателя. Ни одного create/update/delete/run, кроме bigquery.jobs.create (запуск SELECT;
  # писать запросом некуда: на таблицы нет updateData/create). НЕ roles/bigquery.jobUser:
  # в нём dataform.*.create и cloudaicompanion.topics.create (см. vtsClientQueryRunner, PR #380).
  watch_read_permissions = [
    "bigquery.jobs.create",
    "cloudscheduler.jobs.list",
    "logging.logEntries.list",
    "run.executions.get",
    "run.executions.list",
    "run.jobs.get",
  ]
  watch_data_permissions = [
    "bigquery.datasets.get",
    "bigquery.tables.get",
    "bigquery.tables.getData",
    "bigquery.tables.list",
  ]
}

# ── Слой 1: H2 падение контроллера ───────────────────────────────────────────────────────
resource "google_logging_metric" "controller_crash" {
  name        = "heldi_controller_crash"
  description = "Необработанное исключение контроллера: Traceback в stderr (исполнение без статуса и без закрытия решения)."
  filter      = "${local.controller_stderr} AND severity>=ERROR AND textPayload:\"Traceback (most recent call last)\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [terraform_data.target_guard]
}

resource "google_monitoring_alert_policy" "controller_crash" {
  display_name = "Heldi ${local.who}: контроллер упал (необработанное исключение)"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "Traceback в stderr tenant-backfill-controller"
    condition_prometheus_query_language {
      query                     = "sum(increase(logging_googleapis_com:user_heldi_controller_crash{monitored_resource=\"cloud_run_job\"}[10m])) > 0"
      duration                  = "0s"
      evaluation_interval       = "60s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    mime_type = "text/markdown"
    content   = "Исполнение контроллера ${local.who} завершилось необработанным исключением (Traceback, exit 1): решение тика могло остаться без закрытия, следующий тик, вероятно, запишет STOP (так было 25.10: kc4bg 20:09Z → STOP aaf68ede 20:10Z). Проверить исполнение по метке run.googleapis.com/execution_name в stderr. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "controller_crash" }
  depends_on  = [google_logging_metric.controller_crash]
}

# ── Слой 1: H3 тишина контроллера ────────────────────────────────────────────────────────
resource "google_logging_metric" "controller_status" {
  name        = "heldi_controller_status"
  description = "Любая итоговая строка контроллера с jsonPayload.status (пульс тиков)."
  filter      = "${local.controller_stdout} AND jsonPayload.status:*"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "STOPPED, DISPATCHED, WAIT_ACTIVE_CONTROLLER, MONITORING, QUIESCENT_PAUSED, RECONCILED_BOUNDARY, WAITING_READ_RETRY..."
    }
  }
  label_extractors = { status = "EXTRACT(jsonPayload.status)" }
  depends_on       = [terraform_data.target_guard]
}

resource "google_monitoring_alert_policy" "controller_silence" {
  display_name = "Heldi ${local.who}: контроллер молчит 20 мин"
  combiner     = "OR"
  severity     = "ERROR"
  # Включается вторым apply (как свежесть): метрика не заполняется задним числом, а `or vector(0)`
  # сразу после создания открыл бы ложный инцидент.
  enabled = var.silence_alerts_enabled
  conditions {
    display_name = "Нет ни одной строки status 20 мин"
    condition_prometheus_query_language {
      query                     = "(sum(increase(logging_googleapis_com:user_heldi_controller_status{monitored_resource=\"cloud_run_job\"}[20m])) or vector(0)) < 1"
      duration                  = "0s"
      evaluation_interval       = "60s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    mime_type = "text/markdown"
    content   = "Контроллер ${local.who} 20 мин не напечатал ни одного итогового статуса: тик на паузе (в т.ч. плановой — тогда это ожидаемо), Scheduler не будит задание, исполнения падают до вывода статуса или зависли. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "controller_silence" }
  depends_on  = [google_logging_metric.controller_status]
}

# ── Слой 2: личность наблюдателя (только чтение) ─────────────────────────────────────────
resource "google_service_account" "watch" {
  count        = local.watch_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "sa-heldi-watch"
  display_name = "Heldi watch (read-only)"
  description  = "heldi-health-eval: только чтение контроллера, его записей и логов; записи нет."
  depends_on   = [terraform_data.target_guard]
}

resource "google_service_account" "watch_wake" {
  count        = local.watch_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "sa-heldi-watch-wake"
  display_name = "Heldi watch wake"
  description  = "Scheduler heldi-health-eval: только run.jobs.run на задании наблюдателя."
  depends_on   = [terraform_data.target_guard]
}

resource "google_project_iam_custom_role" "watch_read" {
  count       = local.watch_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "heldiWatchRead"
  title       = "Heldi watch read"
  description = "Наблюдатель: SELECT, метаданные Run/Scheduler, чтение логов. Без записи и без Dataform/Gemini."
  permissions = local.watch_read_permissions
  stage       = "GA"
  depends_on  = [terraform_data.target_guard]
}

resource "google_project_iam_custom_role" "watch_data" {
  count       = local.watch_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "heldiWatchData"
  title       = "Heldi watch data read"
  description = "Наблюдатель: чтение таблиц ozon_raw, ref, tenant_ops, tenant_locks (условие IAM на датасеты)."
  permissions = local.watch_data_permissions
  stage       = "GA"
  depends_on  = [terraform_data.target_guard]
}

resource "google_project_iam_custom_role" "watch_wake" {
  count       = local.watch_enabled ? 1 : 0
  project     = var.project_id
  role_id     = "heldiWatchWake"
  title       = "Heldi watch wake"
  description = "Scheduler будит только задание heldi-health-eval."
  permissions = ["run.jobs.run"]
  stage       = "GA"
  depends_on  = [terraform_data.target_guard]
}

resource "google_project_iam_member" "watch_read" {
  count   = local.watch_enabled ? 1 : 0
  project = var.project_id
  # Имя роли известно на плане (как vtsClientQueryRunner): ревью плана видит ровно эту роль.
  role       = "projects/${var.project_id}/roles/heldiWatchRead"
  member     = "serviceAccount:${google_service_account.watch[0].email}"
  depends_on = [google_project_iam_custom_role.watch_read]
}

# ACL датасетов авторитетны в infra/tenant (T3.3): грант на уровне датасета отсюда стёрся бы
# следующим apply арендатора и дал бы там дрейф. Поэтому — уровень проекта с условием IAM
# ровно на четыре датасета (ни ozon_mart, ни analytics_share). Неавторитетный member: infra/tenant
# политику проекта не ведёт.
resource "google_project_iam_member" "watch_data" {
  count   = local.watch_enabled ? 1 : 0
  project = var.project_id
  role    = "projects/${var.project_id}/roles/heldiWatchData"
  member  = "serviceAccount:${google_service_account.watch[0].email}"
  condition {
    title       = "heldi-watch-datasets"
    description = "Только ozon_raw, ref, tenant_ops, tenant_locks."
    expression  = local.watch_dataset_condition
  }
  depends_on = [google_project_iam_custom_role.watch_data]
}

# ── Слой 2: задание и расписание ─────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "watch" {
  count    = local.watch_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = "heldi-health-eval"
  # Откат = destroy этого корня; задание не хранит состояния (только логи).
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.watch[0].email
      timeout         = "900s"
      max_retries     = 0
      containers {
        image   = var.watch_image
        command = ["python", "-m", "tools.heldi_watch.health"]
        resources {
          limits = { cpu = "1000m", memory = "1Gi" }
        }
      }
    }
  }
  depends_on = [google_project_iam_member.watch_read, google_project_iam_member.watch_data]
}

resource "google_cloud_run_v2_job_iam_member" "watch_wake" {
  count      = local.watch_enabled ? 1 : 0
  project    = var.project_id
  location   = var.region
  name       = google_cloud_run_v2_job.watch[0].name
  role       = "projects/${var.project_id}/roles/heldiWatchWake"
  member     = "serviceAccount:${google_service_account.watch_wake[0].email}"
  depends_on = [google_project_iam_custom_role.watch_wake]
}

# Создаётся НА ПАУЗЕ (как DRO-1 Gate B): первый прогон владелец запускает руками и читает
# HEALTH/HEARTBEAT; регулярная оценка — вторым apply с watch_active = true.
resource "google_cloud_scheduler_job" "watch" {
  count     = local.watch_enabled ? 1 : 0
  project   = var.project_id
  region    = var.region
  name      = "heldi-health-eval"
  schedule  = "*/30 * * * *"
  time_zone = "Europe/Moscow"
  paused    = !var.watch_active
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/heldi-health-eval:run"
    body        = base64encode("{}")
    oauth_token {
      service_account_email = google_service_account.watch_wake[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.watch_wake]
}

# ── Слой 2: сигналы наблюдателя ──────────────────────────────────────────────────────────
resource "google_logging_metric" "watch_heartbeat" {
  count       = local.watch_enabled ? 1 : 0
  name        = "heldi_watch_heartbeat"
  description = "Наблюдатель Heldi завершил оценку (строка HEARTBEAT)."
  filter      = "${local.watch_stdout} AND jsonPayload.heldi_watch=\"HEARTBEAT\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "health"
      value_type  = "STRING"
      description = "OK, DEGRADED, FAIL."
    }
  }
  label_extractors = { health = "EXTRACT(jsonPayload.health)" }
  depends_on       = [terraform_data.target_guard]
}

resource "google_monitoring_alert_policy" "watch_fail" {
  count        = local.watch_enabled ? 1 : 0
  display_name = "Heldi ${local.who}: наблюдатель — FAIL"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "HEALTH FAIL от heldi-health-eval"
    condition_matched_log {
      filter = "${local.watch_stdout} AND jsonPayload.heldi_watch=\"HEALTH\" AND jsonPayload.health=\"FAIL\""
      label_extractors = {
        summary = "EXTRACT(jsonPayload.summary)"
        reasons = "EXTRACT(jsonPayload.reasons)"
      }
    }
  }
  # Строка HEALTH пишется только при смене состояния (и напоминание раз в 12 ч), поэтому
  # ограничение 5 мин ничего не теряет.
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi FAIL: $${log.extracted_label.summary}"
    mime_type = "text/markdown"
    content   = "Наблюдатель heldi-health-eval: **FAIL** — $${log.extracted_label.summary} (коды: $${log.extracted_label.reasons}). Подробности — в строке HEALTH (jsonPayload.findings, facts) лога задания heldi-health-eval. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "watch_fail" }
}

resource "google_monitoring_alert_policy" "watch_change" {
  count        = local.watch_enabled ? 1 : 0
  display_name = "Heldi ${local.who}: наблюдатель — смена состояния"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "HEALTH DEGRADED или OK от heldi-health-eval"
    condition_matched_log {
      filter = "${local.watch_stdout} AND jsonPayload.heldi_watch=\"HEALTH\" AND (jsonPayload.health=\"DEGRADED\" OR jsonPayload.health=\"OK\")"
      label_extractors = {
        health   = "EXTRACT(jsonPayload.health)"
        previous = "EXTRACT(jsonPayload.previous)"
        summary  = "EXTRACT(jsonPayload.summary)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi $${log.extracted_label.health} (было $${log.extracted_label.previous}): $${log.extracted_label.summary}"
    mime_type = "text/markdown"
    content   = "Наблюдатель heldi-health-eval: состояние **$${log.extracted_label.health}** (было $${log.extracted_label.previous}). $${log.extracted_label.summary}. DEGRADED — требует внимания, но не авария (пауза тика, сбой чтения, отставание образа наблюдателя от релиза контроллера). ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "watch_change" }
}

resource "google_monitoring_alert_policy" "watch_watchdog" {
  count        = local.watch_enabled ? 1 : 0
  display_name = "Heldi ${local.who}: наблюдатель не работает"
  combiner     = "OR"
  severity     = "ERROR"
  # Вторым apply вместе с регулярной оценкой: до первых пульсов `or vector(0)` открыл бы инцидент.
  enabled = var.watch_active
  conditions {
    display_name = "Нет HEARTBEAT heldi-health-eval 75 мин"
    condition_prometheus_query_language {
      query                     = "(sum(increase(logging_googleapis_com:user_heldi_watch_heartbeat{monitored_resource=\"cloud_run_job\"}[75m])) or vector(0)) < 1"
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    mime_type = "text/markdown"
    content   = "Наблюдатель Heldi 75 мин не завершил ни одной оценки: состояние контроллера сейчас НЕИЗВЕСТНО по слою 2 (слой 1 — STOP, падение, тишина — продолжает работать). Проверить Scheduler heldi-health-eval и строки UNAVAILABLE/stderr задания. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "watch_watchdog" }
  depends_on  = [google_logging_metric.watch_heartbeat]
}
