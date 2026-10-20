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
#
# Классы сигналов (OWNER 21.10.2026: отказы отдельно от информационных, без ежедневного
# потока писем по одному событию):
#   1. heartbeat (PromQL)       — нет полезного результата за окно: LCD WB/Ozon, публикация P&L WB/Ozon.
#   2. unitka_writers_failed    — настоящий отказ прогона (код приложения или execution failed),
#                                 в т. ч. отказ создания месяца 2-го числа (CHAIN_GAP, COGS_MISSING,
#                                 TEMPLATE_MISMATCH, MONTH_*). Письмо на каждый отказ (не чаще 1/ч).
#   3. unitka_publication_blocked (PromQL по лог-метрике) — новая/неразобранная операция или гейт
#                                 публикации P&L. Одно письмо при появлении, одно при снятии.
#   4. unitka_owner_attention   (PromQL по лог-метрике) — отклонённые/накопленные перерасчёты
#                                 (RECON_*), расхождение источников (INTEGRITY_DATA_ERROR). Одно
#                                 письмо при появлении по каждому коду, повтор — только после 26 ч тишины.
#   5. unitka_writer_config     — пауза расписания писателя или изменение конфигурации задания
#                                 (ReplaceJob: PUBLISH/WRITE_ENABLED, образ).
# Почему 3 и 4 — PromQL, а не совпадение лога: у лог-политики каждое повторное совпадение при
# открытом алерте шлёт ещё одно письмо (docs.cloud.google.com/logging/docs/alerting/log-based-incidents:
# «If another log entry matches the condition of an already open alert, Monitoring sends an
# additional notification»), т. е. постоянный сигнал сверки = письмо каждый день. Метрическая
# политика уведомляет при открытии и закрытии алерта.
# NEXT_MONTH_SECTION_MISSING (WARNING 27-го … 1-го) сигналом НЕ является: с Gate 10 месяц создаёт
# суточный прогон 2-го числа сам; до этого предупреждение верно всегда и было бы ложным шумом.
# Неготовый месяц = отказ создания 2-го (класс 2) + отсутствие LCD (класс 1).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  # Четыре писателя Юнитки и P&L магазина.
  unitka_writer_jobs_filter = "resource.labels.job_name=(\"unitka-engine-prod\" OR \"ozon-unitka-prod\" OR \"unitka-store-pnl-prod\" OR \"ozon-store-pnl-prod\")"

  # Сигналы, при которых прогон COMPLETE, но нужен владелец. Не отказ.
  unitka_attention_codes = "(\"RECON_RESTRICTED_REQUIRES_ACK\" OR \"RECON_RESIDUAL_PERSISTENT\" OR \"INTEGRITY_DATA_ERROR\")"

  # Публикация P&L заблокирована (новая/неразобранная операция, гейт модели или источников).
  # Коды отказа Ozon P&L (throw) и предупреждения, которые пишутся до решения о публикации.
  unitka_blocked_codes  = "(\"OZON_STORE_PNL_PUBLISH_GATE\" OR \"OZON_STORE_PNL_SOURCE_NOT_READY\")"
  unitka_blocked_events = "(\"store_pnl_new_operations\" OR \"store_pnl_publish_gate\" OR \"ozon_store_pnl_publish_gate\")"

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

# ── Класс 2. Отказ любого из четырёх писателей: код приложения (кроме сигналов сверки и блоков публикации) или
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
        ((jsonPayload.code!="" AND NOT jsonPayload.code=${local.unitka_attention_codes} AND NOT jsonPayload.code=${local.unitka_blocked_codes}) OR (logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event" AND protoPayload.methodName="/Jobs.RunJob"))
      EOT
      # Без метки кода отказа: отказ пишется двумя записями (системное событие без кода и запись с кодом) —
      # одинаковые метки (job, execution) держат их в одном алерте, второе письмо гасит лимит 1/ч.
      label_extractors = {
        job       = "EXTRACT(resource.labels.job_name)"
        execution = "EXTRACT(labels.\"run.googleapis.com/execution_name\")"
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
    content   = "Прогон писателя упал (метки job, execution; код отказа — в журнале прогона). WB: резерв 09:30 UTC повторит, детерминированный код (INVARIANT_FAIL, QA) резерв не лечит. 2-го числа коды CHAIN_GAP / COGS_MISSING / TEMPLATE_MISMATCH / MONTH_* — новый месяц не создан (ноябрь: docs/ops/UNITKA_MONTH_TRANSITION_2026-11.md). Детали — wb_ops.UNITKA_ENGINE_RUNS / wb_raw.LOADER_RUNS. Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Лог-метрики для классов 3 и 4: по одному ряду на (job, code[, event]).
resource "google_logging_metric" "unitka_owner_attention_events" {
  name   = "unitka_owner_attention_events"
  filter = <<-EOT
    resource.type="cloud_run_job"
    ${local.unitka_writer_jobs_filter}
    severity>=WARNING
    jsonPayload.code=${local.unitka_attention_codes}
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key        = "job"
      value_type = "STRING"
    }
    labels {
      key        = "code"
      value_type = "STRING"
    }
  }
  label_extractors = {
    job  = "EXTRACT(resource.labels.job_name)"
    code = "EXTRACT(jsonPayload.code)"
  }
}

resource "google_logging_metric" "unitka_publication_blocked_events" {
  name   = "unitka_publication_blocked_events"
  filter = <<-EOT
    resource.type="cloud_run_job"
    ${local.unitka_writer_jobs_filter}
    severity>=WARNING
    (jsonPayload.code=${local.unitka_blocked_codes} OR jsonPayload.message=${local.unitka_blocked_events})
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key        = "job"
      value_type = "STRING"
    }
  }
  # Один ряд на писателя: у Ozon одно событие блока даёт и предупреждение гейта, и код отказа —
  # разметка по коду открыла бы два алерта на одну причину. Причина — в журнале прогона.
  label_extractors = {
    job = "EXTRACT(resource.labels.job_name)"
  }
}

# ── Класс 3. Публикация P&L заблокирована / новая операция: одно письмо при появлении, одно при снятии
#    (алерт открыт, пока событие повторяется хотя бы раз за 26 ч). Сама публикация при этом не идёт —
#    отсутствие результата параллельно ловит heartbeat класса 1.
resource "google_monitoring_alert_policy" "unitka_publication_blocked" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "P&L: публикация заблокирована / новая операция"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "гейт публикации или неразобранная операция за 26 ч"
    condition_prometheus_query_language {
      query               = "sum by (job) (increase(logging_googleapis_com:user_${google_logging_metric.unitka_publication_blocked_events.name}{monitored_resource=\"cloud_run_job\"}[26h])) > 0"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Публикация P&L магазина заблокирована. Ozon: OZON_STORE_PNL_PUBLISH_GATE — новый/неразобранный тип начисления (ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS, QA Q03/Q05) или нарушение модели; OZON_STORE_PNL_SOURCE_NOT_READY — источники не свежие. WB: store_pnl_new_operations — новая операция финотчёта (V_WB_FINANCE_NEW_OPERATIONS), месяц в PENDING_CLASSIFICATION. Классификация — только решением владельца (пример: тип 125, PR #366). Пока не разобрано — лист остаётся на прошлой публикации."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Класс 4. Нужна проверка владельца: отклонённые/накопленные перерасчёты и расхождение источников.
#    Прогон COMPLETE. Одно письмо на код при появлении; пока код повторяется в каждом прогоне — тишина.
resource "google_monitoring_alert_policy" "unitka_owner_attention" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА: нужна проверка владельца (перерасчёты, сверка)"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "сигнал сверки за 26 ч (по коду)"
    condition_prometheus_query_language {
      query               = "sum by (job, code) (increase(logging_googleapis_com:user_${google_logging_metric.unitka_owner_attention_events.name}{monitored_resource=\"cloud_run_job\"}[26h])) > 0"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Прогон завершён, но нужен владелец. RECON_RESTRICTED_REQUIRES_ACK — правка прошлого месяца отклонена политикой restricted и ждёт решения (окно сверки 35 дней: после выхода дня из окна правка не применится); RECON_RESIDUAL_PERSISTENT — остаток сверки держится; INTEGRITY_DATA_ERROR — расхождение источников (observe). Список ячеек — журнал unitka_controlled_refused. Алерт закроется сам после 26 ч без сигнала."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Класс 5. Пауза расписания писателя или изменение конфигурации задания (ReplaceJob — в т. ч.
#    PUBLISH=0 / UNITKA_WRITE_ENABLED=0 / смена образа). lifecycle.ignore_changes = [paused] у всех пяти
#    расписаний: пауза вне Terraform живёт бессрочно. Проверено на журнале аудита 20.09–20.10.2026:
#    14 записей PauseJob, все — реальные паузы, включая 15.10 ozon-store-pnl-prod; ReplaceJob 20.10
#    (образ unitka-engine-prod 08:43Z, PUBLISH=1 ozon-store-pnl-prod 16:44Z). Плановое изменение в окне
#    выпуска — ожидаемое письмо; незапланированное — повод проверить.
resource "google_monitoring_alert_policy" "unitka_writer_config" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: пауза расписания или изменение задания писателя"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "PauseJob расписания или ReplaceJob задания писателя"
    condition_matched_log {
      filter = <<-EOT
        logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
        ((protoPayload.serviceName="cloudscheduler.googleapis.com"
          protoPayload.methodName="google.cloud.scheduler.v1.CloudScheduler.PauseJob"
          protoPayload.resourceName=~"/jobs/(unitka-engine-prod-(morning|reserve)|ozon-unitka-prod|unitka-store-pnl-prod|ozon-store-pnl-prod)$")
        OR (protoPayload.serviceName="run.googleapis.com"
          protoPayload.methodName=("google.cloud.run.v1.Jobs.ReplaceJob" OR "google.cloud.run.v2.Jobs.UpdateJob")
          protoPayload.resourceName=~"/jobs/(unitka-engine-prod|ozon-unitka-prod|unitka-store-pnl-prod|ozon-store-pnl-prod)$"))
      EOT
      label_extractors = {
        resource = "EXTRACT(protoPayload.resourceName)"
        method   = "EXTRACT(protoPayload.methodName)"
        actor    = "EXTRACT(protoPayload.authenticationInfo.principalEmail)"
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
    content   = "Расписание писателя на паузе или задание изменено (метки resource, method, actor). Если это не окно выпуска по ACK — проверить gcloud scheduler jobs describe / gcloud run jobs describe (UNITKA_STORE_PNL_PUBLISH, OZON_STORE_PNL_PUBLISH, UNITKA_WRITE_ENABLED, образ). Простой без паузы ловит heartbeat."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
