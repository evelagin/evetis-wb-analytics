# ── DRO-1 · Data Reliability & Observability v1: обнаружение и email-оповещения ──
# Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md · SQL: sql/health/dro1_*.sql
#
# 🔴 НЕ ПРИМЕНЁН. Деплой — отдельный owner ACK. Порядок (подробно — в доке):
#   1) targeted apply: датасет evetis_health + права sa-ops-health (ресурсы *_dataset / *_iam_member ниже);
#   2) SQL sql/health/dro1_01 … dro1_06 по порядку номеров;
#   3) ручной CALL детектора и проверка меток в журнале аудита;
#   4) targeted apply: Scheduler, лог-метрика, политики.
#   Шаг 4 раньше шага 2 = Scheduler зовёт несуществующую процедуру каждые 30 минут.
#
# Почему без нового runtime. BigQuery не шлёт письма, но каждое задание попадает в
# журнал аудита Cloud Logging вместе с метками (проверено 2026-10-04: путь
# protoPayload.metadata.jobChange.job.jobConfig.labels.*, data_access-логи идут в
# _Default без исключений). Детектор ставит метки на задание-маркер, политики ниже
# ловят маркер и шлют письмо в уже существующий канал unitka_email. Содержимое
# письма — метки (extracted labels). Telegram и ops-notifier не используются: F-18.
#
# Как и прочие алерты проекта, всё ниже создаётся только при заданном
# var.unitka_alert_email (repo variable UNITKA_ALERT_EMAIL, см. infra.yml).

resource "google_bigquery_dataset" "evetis_health" {
  dataset_id    = "evetis_health"
  friendly_name = "EVETIS data health"
  description   = "DRO-1: cross-domain read model of pipeline health (WB, Ozon, derived). Reads run logs and freshness metadata of both marketplaces, never produces business facts."
  location      = var.bq_location

  # Снимки состояния и журнал доставки — доказательства; destroy только после явного DROP
  # (sql/health/dro1_99_rollback.sql).
  delete_contents_on_destroy = false

  labels = {
    domain = "observability"
    stage  = "dro1"
  }
}

# ── Права детектора ──────────────────────────────────────────────────────────
# Детектор работает под уже существующим sa-ops-health (ops_health.tf): у него есть
# jobUser, dataEditor на wb_ops (инциденты и события ADS-1B) и чтение wb_raw/wb_mart.
# Добавляется только недостающее, на уровне датасетов, без ролей проекта.
resource "google_bigquery_dataset_iam_member" "ops_health_edit_evetis_health" {
  dataset_id = google_bigquery_dataset.evetis_health.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_health_read_ozon_raw" {
  dataset_id = "ozon_raw"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_health_read_evetis_ref" {
  dataset_id = "evetis_ref"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_health.email}"
}

# ── Расписание детектора: каждые 30 минут круглосуточно ──────────────────────
# 30 минут — из контракта снимков: первый WARN приходит через 90–135 минут после слота,
# потеря наступает в 00:00, запас ≥ 8 ч. Интервал 3 ч (как у ADS-1B) съел бы до трети
# этого запаса. Ночью детектор тоже работает: подтверждённая потеря в 00:00 и сторож.
# Стоимость прогона по backtest 2026-10-04: ≈ 3 МБ на пробу, ≈ 7 МБ на прогон
# (≈ 10 ГБ в месяц).
#
# Создавался НА ПАУЗЕ (DRO-1 Gate B, 2026-10-05): политики и метрика фильтруют маркеры по
# principalEmail = sa-ops-health, ручной CALL владельца их не задевает. Доверенный прогон
# 2026-10-07 07:00 UTC (resume → одно исполнение → pause) доказал цепочку целиком: задания
# под sa-ops-health, письма пакета и дайджеста, пульс, срабатывание сторожа.
# Gate C (owner ACK 2026-10-07): регулярная оценка включена, paused = false.
resource "google_cloud_scheduler_job" "dro_health_eval" {
  name      = "dro-health-eval"
  region    = var.region
  schedule  = "*/30 * * * *"
  time_zone = "Europe/Moscow"
  paused    = false

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
        labels = {
          dro_component = "detector"
        }
        query = {
          query        = "CALL `evetis_health.sp_evaluate_data_health`()"
          useLegacySql = false
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.ops_health.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
    google_bigquery_dataset_iam_member.ops_health_edit_evetis_health,
  ]
}

locals {
  # Общая часть фильтров: завершённые задания BigQuery детектора.
  dro_job_done_filter = <<-EOT
    resource.type="bigquery_project"
    protoPayload.serviceName="bigquery.googleapis.com"
    protoPayload.metadata.jobChange.after="DONE"
    protoPayload.authenticationInfo.principalEmail="${google_service_account.ops_health.email}"
  EOT
  dro_label_path      = "protoPayload.metadata.jobChange.job.jobConfig.labels"
}

# ── Сторож: детектор не работает ─────────────────────────────────────────────
resource "google_logging_metric" "dro_detector_heartbeat" {
  name   = "dro_detector_heartbeat"
  filter = <<-EOT
    ${local.dro_job_done_filter}
    ${local.dro_label_path}.dro_kind="heartbeat"
    protoPayload.metadata.jobChange.job.jobConfig.queryConfig.query:"dro_heartbeat_marker"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "dro_detector_watchdog" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "DRO-1: детектор здоровья данных не работает"
  combiner     = "OR"

  conditions {
    display_name = "Нет пульса dro_detector_heartbeat 90 минут"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.dro_detector_heartbeat.name}\" AND resource.type=\"bigquery_project\""
      duration = "5400s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Детектор DRO-1 не прислал пульс 90 минут: состояние данных EVETIS сейчас НЕИЗВЕСТНО, evetis_health.V_DATA_HEALTH_CURRENT показывает UNKNOWN (DETECTOR_STALE). Проверить Scheduler dro-health-eval и последние задания sa-ops-health в BigQuery. Runbook: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Детектор упал или доставка сломалась ─────────────────────────────────────
resource "google_monitoring_alert_policy" "dro_detector_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "DRO-1: детектор здоровья данных упал"
  combiner     = "OR"

  conditions {
    display_name = "Ошибка задания детектора или сбой доставки алертов"
    condition_matched_log {
      filter = <<-EOT
        resource.type="bigquery_project"
        protoPayload.serviceName="bigquery.googleapis.com"
        protoPayload.metadata.jobChange.after="DONE"
        protoPayload.authenticationInfo.principalEmail="${google_service_account.ops_health.email}"
        ((${local.dro_label_path}.dro_component="detector" AND protoPayload.metadata.jobChange.job.jobStatus.errorResult.code:*)
         OR ${local.dro_label_path}.dro_kind="dispatch_error")
      EOT
      label_extractors = {
        error = "EXTRACT(protoPayload.metadata.jobChange.job.jobStatus.errorResult.message)"
        kind  = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_kind)"
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
    content   = "Детектор DRO-1 завершился ошибкой (error: $${log.extracted_label.error}) или не смог передать алерты (kind=dispatch_error — текст в evetis_health.ALERT_DISPATCH_LOG.summary). Обнаружение при сбое доставки уже записано: evetis_health.V_DATA_HEALTH_CURRENT."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Алерты по конвейерам: пакет раз в прогон ─────────────────────────────────
# Детектор шлёт не больше одного маркера за прогон (каждые 30 мин): все события,
# которые нужно отправить сейчас, уходят одним письмом. Поэтому ограничение частоты
# 5 минут ничего не теряет. Тихие часы, напоминания и восстановления решает
# детектор (evetis_health.sp_dispatch_alerts), а не политика.
resource "google_monitoring_alert_policy" "dro_alert_batch" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "DRO-1: данные EVETIS — сбой, потеря или восстановление"
  combiner     = "OR"

  conditions {
    display_name = "Пакет алертов DRO-1"
    condition_matched_log {
      filter = <<-EOT
        ${local.dro_job_done_filter}
        ${local.dro_label_path}.dro_kind="alert"
        protoPayload.metadata.jobChange.job.jobConfig.queryConfig.query:"dro_delivery_marker"
      EOT
      label_extractors = {
        batch        = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_batch)"
        events       = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_n)"
        opened       = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_open)"
        recovered    = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_recovery)"
        top_type     = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_top_type)"
        top_severity = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_top_severity)"
        top_pipeline = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_top_pipeline)"
        top_reason   = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_top_reason)"
        pipelines    = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_pipelines)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    subject   = "EVETIS данные: $${log.extracted_label.top_severity} $${log.extracted_label.top_type} — $${log.extracted_label.top_pipeline} ($${log.extracted_label.top_reason})"
    content   = <<-EOT
      Событий в пакете: $${log.extracted_label.events} (открыто/напоминаний: $${log.extracted_label.opened}, восстановлено: $${log.extracted_label.recovered}).
      Главное: **$${log.extracted_label.top_severity} $${log.extracted_label.top_type}** — `$${log.extracted_label.top_pipeline}`, причина `$${log.extracted_label.top_reason}`.
      Конвейеры: $${log.extracted_label.pipelines}. Пакет: $${log.extracted_label.batch}.

      Подробности одним запросом:
      `SELECT * FROM evetis_health.V_DATA_HEALTH_CURRENT WHERE serving_status <> 'HEALTHY'`
      События: `wb_ops.OPS_ALERT_EVENT WHERE scope = 'DRO_PIPELINE'`. Runbook: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
    EOT
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Ежедневный дайджест 09:30 МСК ────────────────────────────────────────────
resource "google_monitoring_alert_policy" "dro_digest" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "DRO-1: ежедневный дайджест здоровья данных"
  combiner     = "OR"

  conditions {
    display_name = "Дайджест DRO-1"
    condition_matched_log {
      filter = <<-EOT
        ${local.dro_job_done_filter}
        ${local.dro_label_path}.dro_kind="digest"
        protoPayload.metadata.jobChange.job.jobConfig.queryConfig.query:"dro_digest_marker"
      EOT
      label_extractors = {
        day            = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_day)"
        healthy        = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_healthy)"
        degraded       = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_degraded)"
        stale          = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_stale)"
        blocked        = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_blocked)"
        unknown        = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_unknown)"
        known_loss     = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_known_loss)"
        pending        = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_pending_events)"
        open_incidents = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_open_incidents)"
        attention      = "EXTRACT(protoPayload.metadata.jobChange.job.jobConfig.labels.dro_attention)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    subject   = "EVETIS данные, дайджест $${log.extracted_label.day}: требуют внимания — $${log.extracted_label.attention}"
    content   = <<-EOT
      Конвейеры: HEALTHY $${log.extracted_label.healthy} · DEGRADED $${log.extracted_label.degraded} · STALE $${log.extracted_label.stale} · BLOCKED $${log.extracted_label.blocked} · UNKNOWN $${log.extracted_label.unknown}.
      С подтверждёнными потерями за 35 дней: $${log.extracted_label.known_loss}. Открытых инцидентов: $${log.extracted_label.open_incidents}. Событий, ушедших только в дайджест: $${log.extracted_label.pending}.
      Требуют внимания: $${log.extracted_label.attention}.

      `SELECT * FROM evetis_health.V_DATA_HEALTH_CURRENT ORDER BY serving_status, severity`
    EOT
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
