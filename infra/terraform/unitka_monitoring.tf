# ══════════════════════════════════════════════════════════════════════════════
# ЮНИТКА и P&L магазина: сигналы полезного результата (heartbeat), разделение
# «упал» / «нужна проверка владельца», пауза расписаний.
# Приёмка 20.10.2026, подзадача F (каталог доказательств: evetis-manifests/
# unitka-ops-acceptance-2026-10-20/F_alerts). Только описание: без apply.
#
# Зачем:
#   1. Действующие политики ловят только ОШИБКУ. Успешный прогон без сдвига LCD
#      (01.10.2026: LCD WB простоял на 29.09 двое суток, оба прогона COMPLETE),
#      расписание на паузе (ozon-store-pnl-prod 15.10–20.10: ни одного запуска)
#      и PUBLISH=0 — тишина.
#   2. Политика unitka_engine_failed смешивает отказы с сигналами сверки:
#      RECON_RESTRICTED_REQUIRES_ACK / RECON_RESIDUAL_PERSISTENT /
#      INTEGRITY_DATA_ERROR приходят 4–7 раз в сутки при COMPLETE-прогоне.
#      Настоящий INVARIANT_FAIL 20.10 пришёл в тот же поток писем.
#
# Порядок включения (каждый шаг — OWNER ACK):
#   a) apply этого файла: всё создаётся ВЫКЛЮЧЕННЫМ (enabled = false);
#   b) через 27 ч после apply — проверить PromQL heartbeat в Metrics Explorer:
#      на исправной работе выражение пусто, на окне без событий — 0;
#      затем включить heartbeat-политики (меняется ТОЛЬКО enabled);
#   c) включить unitka_writers_failed и unitka_owner_attention; после этого —
#      выключить старые unitka_engine_failed и ozon_unitka_failed
#      (ТОЛЬКО enabled: condition на месте не менять — 06.10.2026
#      UpdateAlertPolicy с updateMask=conditions молча выключил политику).
#
# Почему PromQL, а не condition_absent: окно metric-absence ограничено 1 сутками
# (alignment + duration ≤ 1 day, Cloud Monitoring quotas), а событие суточное —
# 24 ч дают ложные срабатывания при сдвиге времени коммита на минуты. PromQL-
# политики с июня 2026 читают > 25 ч данных.
# `or vector(0)`: нет ни одного ряда (метрика ни разу не писалась, расписание на
# паузе) = 0 событий = срабатывание. Нулевой ряд тоже даёт 0 < 1 = срабатывание.
# ══════════════════════════════════════════════════════════════════════════════

locals {
  # Четыре писателя Юнитки и P&L магазина.
  unitka_writer_jobs_filter = "resource.labels.job_name=(\"unitka-engine-prod\" OR \"ozon-unitka-prod\" OR \"unitka-store-pnl-prod\" OR \"ozon-store-pnl-prod\")"

  # Сигналы, при которых прогон COMPLETE, но нужен владелец. Не отказ.
  unitka_attention_codes = "(\"RECON_RESTRICTED_REQUIRES_ACK\" OR \"RECON_RESIDUAL_PERSISTENT\" OR \"INTEGRITY_DATA_ERROR\")"

  # Heartbeat: событие полезного результата → окно (ч), за которое оно обязано случиться.
  #   WB LCD 27 ч: утро 07:00Z коммитит ~07:02Z; 26 ч сработали бы в 09:02Z, ДО резерва 09:30Z.
  #   Ozon LCD 26 ч: один прогон 07:00Z (повторы Scheduler внутри часа).
  #   P&L WB 10:00Z, P&L Ozon 10:20Z: 26 ч.
  # Фильтры событий проверены на живых логах 20.09–20.10.2026: WB LCD_COMMITTED 25 (пропуск 01.10 —
  # реальный простой LCD), Ozon LCD_COMMITTED 26 (ежедневно), store_pnl_tab_published 15,
  # ozon_store_pnl_readback 3 (расписание было на паузе до 20.10).
  # `jsonPayload.message` здесь — имя события: logLifecycle и эти logger.info не передают ctx.message.
  unitka_heartbeats = {
    unitka_wb_lcd_committed = {
      job     = "unitka-engine-prod"
      event   = "jsonPayload.message=\"unitka_lifecycle\"\njsonPayload.lifecycle_event=\"LCD_COMMITTED\"\njsonPayload.platform=\"WB\""
      window  = "27h"
      title   = "UNITKA WB: LCD не сдвигался 27 ч"
      runbook = "LCD WB (ZZ_CONFIG!B2) не закоммичен 27 ч. Смотреть unitka_lifecycle (LCD_NOT_ADVANCED: notes), wb_ops.UNITKA_ENGINE_RUNS, паузу unitka-engine-prod-morning/-reserve, UNITKA_WRITE_ENABLED. Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    }
    unitka_ozon_lcd_committed = {
      job     = "ozon-unitka-prod"
      event   = "jsonPayload.message=\"unitka_lifecycle\"\njsonPayload.lifecycle_event=\"LCD_COMMITTED\"\njsonPayload.platform=\"OZON\""
      window  = "26h"
      title   = "UNITKA Ozon: LCD не сдвигался 26 ч"
      runbook = "OZON_LAST_CLOSED_DATE (ZZ_CONFIG!B30) не закоммичен 26 ч. Смотреть unitka_lifecycle ozon-unitka-prod, LOADER_RUNS(ozon-unitka), паузу расписания, OZON_UNITKA_WRITE_ENABLED. Runbook: docs/ops/OZON_UNITKA_OPERATIONS.md"
    }
    unitka_store_pnl_published = {
      job     = "unitka-store-pnl-prod"
      event   = "jsonPayload.message=\"store_pnl_tab_published\"\njsonPayload.action=(\"WRITE\" OR \"NOOP\")"
      window  = "26h"
      title   = "WB STORE P&L: вкладка не публиковалась 26 ч"
      runbook = "«WB Магазин P&L» не публиковалась (WRITE/NOOP) 26 ч: пауза unitka-store-pnl-prod, UNITKA_STORE_PNL_PUBLISH=0 или отказ. Runbook: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md"
    }
    ozon_store_pnl_published = {
      job     = "ozon-store-pnl-prod"
      event   = "jsonPayload.message=\"ozon_store_pnl_readback\""
      window  = "26h"
      title   = "OZON STORE P&L: вкладка не подтверждена 26 ч"
      runbook = "«Ozon Магазин P&L» без успешного перечитывания 26 ч: пауза ozon-store-pnl-prod, OZON_STORE_PNL_PUBLISH=0, гейт OZON_STORE_PNL_PUBLISH_GATE/SOURCE_NOT_READY. Runbook: docs/finance/OZON_STORE_PNL_PHASE_D4.md"
    }
  }
}

resource "google_logging_metric" "unitka_heartbeat" {
  for_each = local.unitka_heartbeats
  name     = each.key
  filter   = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="${each.value.job}"
    ${each.value.event}
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "unitka_heartbeat" {
  for_each     = { for k, v in local.unitka_heartbeats : k => v if var.unitka_alert_email != "" }
  display_name = each.value.title
  combiner     = "OR"
  enabled      = false # включается по ACK через ≥ window после появления метрики (метрика не заполняется задним числом)

  conditions {
    display_name = "нет события ${each.key} за ${each.value.window}"
    condition_prometheus_query_language {
      # ⚠ Проверить в Metrics Explorer до включения: для DELTA-метрики increase() может
      #   потребоваться заменить на sum_over_time(); критерий — 1 на исправном дне.
      query               = "(sum(increase(logging_googleapis_com:user_${google_logging_metric.unitka_heartbeat[each.key].name}{monitored_resource=\"cloud_run_job\"}[${each.value.window}])) or vector(0)) < 1"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = each.value.runbook
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Отказ любого из четырёх писателей: код приложения (кроме сигналов сверки) или
#    системное событие «execution failed». Проверено на логах 20.09–20.10.2026: 9 совпадений =
#    ровно 5 реальных отказов (22.09 LOADER_ERROR, 06.10 zbfk7 без кода, 07.10 SHEETS_API,
#    11.10 SHEETS_API, 20.10 INVARIANT_FAIL) и их системные события; сигналов сверки — 0.
resource "google_monitoring_alert_policy" "unitka_writers_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: прогон упал"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "код отказа (без сигналов сверки) или execution failed"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        ${local.unitka_writer_jobs_filter}
        severity>=ERROR
        ((jsonPayload.code!="" AND NOT jsonPayload.code=${local.unitka_attention_codes}) OR (logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event" AND protoPayload.methodName="/Jobs.RunJob"))
      EOT
      label_extractors = {
        job        = "EXTRACT(resource.labels.job_name)"
        error_code = "EXTRACT(jsonPayload.code)"
        execution  = "EXTRACT(labels.\"run.googleapis.com/execution_name\")"
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
    content   = "Прогон писателя упал (метки job, error_code, execution). WB: резерв 09:30 UTC повторит, детерминированный код (INVARIANT_FAIL, QA) резерв не лечит. Детали — wb_ops.UNITKA_ENGINE_RUNS / wb_raw.LOADER_RUNS. Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Нужна проверка владельца: прогон COMPLETE, но есть отказ сверки, расхождение источников,
#    неразобранная операция, неготовый следующий месяц или гейт публикации при PUBLISH=0.
#    Проверено на логах 20.09–20.10.2026: 4–7 совпадений в сутки — поэтому отдельная политика
#    и лимит одного письма в сутки на код.
resource "google_monitoring_alert_policy" "unitka_owner_attention" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: нужна проверка владельца"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "сигнал сверки, следующий месяц, новая операция или гейт публикации"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        ${local.unitka_writer_jobs_filter}
        severity>=WARNING
        (jsonPayload.code=${local.unitka_attention_codes} OR jsonPayload.code="NEXT_MONTH_SECTION_MISSING" OR jsonPayload.message=("store_pnl_new_operations" OR "store_pnl_publish_gate" OR "ozon_store_pnl_publish_gate"))
      EOT
      label_extractors = {
        job   = "EXTRACT(resource.labels.job_name)"
        code  = "EXTRACT(jsonPayload.code)"
        event = "EXTRACT(jsonPayload.message)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "86400s"
    }
    auto_close = "172800s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Прогон завершён, но нужен владелец. RECON_RESTRICTED_REQUIRES_ACK — правка цены прошлого месяца ждёт ACK; RECON_RESIDUAL_PERSISTENT — остаток сверки; INTEGRITY_DATA_ERROR — расхождение источников (observe); NEXT_MONTH_SECTION_MISSING — подготовить секцию следующего месяца (unitka-month-prep) до 1-го числа; store_pnl_new_operations — новая операция WB в P&L; *_publish_gate — публикация была бы заблокирована."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Пауза расписания писателя. lifecycle.ignore_changes = [paused] у всех пяти расписаний:
#    пауза вне Terraform живёт бессрочно. Проверено на журнале аудита 20.09–20.10.2026:
#    14 записей PauseJob (по 2 на действие), все — реальные паузы, включая 15.10 ozon-store-pnl-prod.
resource "google_monitoring_alert_policy" "unitka_scheduler_paused" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: расписание поставлено на паузу"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "PauseJob по расписанию писателя"
    condition_matched_log {
      filter = <<-EOT
        logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
        protoPayload.serviceName="cloudscheduler.googleapis.com"
        protoPayload.methodName="google.cloud.scheduler.v1.CloudScheduler.PauseJob"
        protoPayload.resourceName=~"/jobs/(unitka-engine-prod-(morning|reserve)|ozon-unitka-prod|unitka-store-pnl-prod|ozon-store-pnl-prod)$"
      EOT
      label_extractors = {
        scheduler_job = "EXTRACT(protoPayload.resourceName)"
        actor         = "EXTRACT(protoPayload.authenticationInfo.principalEmail)"
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
    content   = "Расписание писателя поставлено на паузу. Плановая пауза (окно развёртывания) — ожидаемо; иначе снять паузу по runbook. Простой без паузы ловит heartbeat-политика."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
