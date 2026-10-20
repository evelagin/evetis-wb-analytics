# ══════════════════════════════════════════════════════════════════════════════
# ЮНИТКА и P&L магазина: сигналы полезного результата (heartbeat), разделение
# «упал» / «публикация заблокирована» / «нужна проверка владельца», изменение конфигурации.
# Приёмка 20.10.2026, подзадача F (каталог доказательств: evetis-manifests/
# unitka-ops-acceptance-2026-10-20/F_alerts). Всё создаётся ВЫКЛЮЧЕННЫМ.
#
# Зачем:
#   1. Действующие политики ловят только ОШИБКУ. Успешный прогон без сдвига LCD
#      (01.10.2026: LCD WB простоял на 29.09 двое суток, оба прогона COMPLETE),
#      расписание на паузе (ozon-store-pnl-prod 15.10–20.10: ни одного запуска)
#      и PUBLISH=0 — тишина.
#   2. Действующие лог-политики смешивают отказы с сигналами сверки (RECON_*,
#      INTEGRITY_DATA_ERROR приходят 4–7 раз в сутки при COMPLETE-прогоне) и шлют
#      письмо на каждое совпадение: настоящий INVARIANT_FAIL 20.10 пришёл в тот же поток.
#
# Порядок включения (каждый шаг — OWNER ACK):
#   a) targeted apply этого файла: 7 лог-метрик + 8 политик, все enabled = false;
#   b) не раньше чем через 25 ч — Metrics Explorer: PromQL каждой политики возвращает
#      ряды с меткой job_name; heartbeat на исправном дне ≥ 1 (increase() по DELTA на одиночном
#      событии — не меньше 1); затем enabled = true (меняется ТОЛЬКО enabled);
#   c) только после того, как новые политики подтверждённо сработали (живой случай или
#      проверочный), — выключить ВСЕ пять старых: unitka_engine_failed, unitka_platform_failed,
#      ozon_unitka_failed, unitka_store_pnl_failed, ozon_store_pnl_failed
#      (ТОЛЬКО enabled: condition на месте не менять — 06.10.2026 UpdateAlertPolicy
#      с updateMask=conditions молча выключил политику).
#
# Предел окна: PromQL-политика по пользовательской лог-метрике (logging.googleapis.com/user/)
# видит только последние 25 ч данных — docs.cloud.google.com/monitoring/promql/promql-in-alerting:
# «You can only alert on the most recent 25 hours of data for custom metrics … and user-defined
# Log-Based metrics». Повторная проверка (duration, аналог «for») — не больше 24 ч, и
# duration + окно + offset ≤ 25 ч (там же, Restrictions). Поэтому heartbeat = окно 24 ч +
# duration 1 ч (срабатывание через 25 ч после последнего события, допуск сдвига — 1 ч),
# классы 2–4 — окно 24h30m без duration.
# Следствие для WB: срабатывание ~08:02Z, РАНЬШЕ резерва 09:30Z. В день, когда утро упало,
# а резерв закоммитил, heartbeat WB даст пару писем «открыт/закрыт» (класс 2 в этот день и так
# пишет). Позже 25 ч сигналить по лог-метрике нельзя — принято сознательно.
# `or vector(0)`: нет ни одного ряда (метрика ни разу не писалась, расписание на паузе) =
# 0 событий = срабатывание. increase() над DELTA-метрикой корректен (Monarch типизирован:
# gauge-функции вроде sum_over_time к DELTA не применяются).
#
# Классы сигналов (OWNER 21.10.2026: отказы отдельно от информационных, без ежедневного
# потока писем по одному событию). Классы 1–4 — метрические PromQL-политики: письмо при
# открытии и при закрытии алерта, пока причина держится — тишина. Лог-политика так не умеет:
# каждое новое совпадение при открытом алерте шлёт ещё одно письмо
# (docs.cloud.google.com/logging/docs/alerting/log-based-incidents: «If another log entry matches
# the condition of an already open alert, Monitoring sends an additional notification»).
#   1. heartbeat             — нет полезного результата 25 ч: LCD WB / Ozon, публикация P&L WB / Ozon.
#   2. unitka_writers_failed — сбой: прогон упал (код приложения или execution failed), в т. ч. отказ
#                              создания месяца 2-го числа; или прогон завершён, но сбой источника /
#                              подсистемы записан ERROR с кодом (LCD_HELD_BY_COVERAGE_GAP — LCD придержан
#                              дырой покрытия; unitka_integrity_failed — упала сама проверка целостности).
#                              Одно письмо на писателя; повтор в течение 24,5 ч — без письма.
#   3. unitka_publication_blocked — гейт публикации P&L / новая неразобранная операция.
#   4. unitka_owner_attention — прогон завершён, нужен владелец: отклонённые/накопленные перерасчёты,
#                              остаток сверки, расхождение источников, нераспознанный CPO Ozon.
#   5. unitka_writer_config  — пауза / изменение / удаление расписания писателя; изменение или удаление
#                              задания (PUBLISH, WRITE_ENABLED, образ) не через sa-deployer. Лог-политика:
#                              каждое изменение — отдельное событие, письмо не чаще 1 в час.
# Классы 2–4 не пересекаются: гейт публикации (коды *_PUBLISH_GATE / SOURCE_NOT_READY, в бою — исключение и
# exit 1) исключён из класса 2 по коду, а системное событие execution failed у заданий P&L в класс 2 не
# входит вовсе (всякий отказ P&L пишет запись с кодом — fatalRecord, cli.ts; падение платформы без
# записи ловит heartbeat публикации). Сигналы класса 4 исключены из класса 2 по имени события.
# Класс 1 намеренно дублирует причину (класс 2/3) следствием: «упал» — что случилось, heartbeat — что
# результата нет. Остаток: если событие класса 3/4 пропало дольше 24,5 ч (например, его замаскировал
# отказ раньше сверки), алерт закроется и откроется заново — смена состояния, не ежедневный поток.
# NEXT_MONTH_SECTION_MISSING (WARNING 27-го … 1-го) сигналом НЕ является: с Gate 10 месяц создаёт
# суточный прогон 2-го числа сам; до этого предупреждение верно всегда.
# Неготовый месяц = отказ создания 2-го (класс 2) + отсутствие LCD (класс 1).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  # Четыре писателя Юнитки и P&L магазина.
  unitka_writer_jobs_filter = "resource.labels.job_name=(\"unitka-engine-prod\" OR \"ozon-unitka-prod\" OR \"unitka-store-pnl-prod\" OR \"ozon-store-pnl-prod\")"
  unitka_pnl_jobs           = "(\"unitka-store-pnl-prod\" OR \"ozon-store-pnl-prod\")"

  # Класс 4 — по имени события (ctx.message его не затирает, проверено на живых логах). У
  # unitka_controlled_refused шесть кодов отказа (reconcile.ts), все — один сигнал.
  unitka_attention_events = "(\"unitka_controlled_refused\" OR \"unitka_recon_residual_persistent\" OR \"unitka_integrity_data_error\" OR \"ozon_unitka_cpo_unresolved\")"

  # Класс 3. Коды исключения гейта (PUBLISH=1) и предупреждения, которые пишутся до решения о публикации.
  unitka_blocked_codes  = "(\"OZON_STORE_PNL_PUBLISH_GATE\" OR \"OZON_STORE_PNL_SOURCE_NOT_READY\" OR \"STORE_PNL_PUBLISH_GATE\")"
  unitka_blocked_events = "(\"store_pnl_new_operations\" OR \"store_pnl_publish_gate\" OR \"ozon_store_pnl_publish_gate\")"

  # Окно запроса классов 2–4: ≤ 25 ч (предел лог-метрик), 30 мин запаса на сдвиг суточного события.
  unitka_signal_window = "24h30m"

  # Heartbeat: событие полезного результата → окно 24 ч + duration 1 ч = 25 ч (предел лог-метрик).
  #   WB LCD 07:00Z (коммит 07:01–07:15Z), Ozon LCD 07:00Z, P&L WB 10:00Z, P&L Ozon 10:20Z.
  # Фильтры событий проверены на живых логах 20.09–20.10.2026: WB LCD_COMMITTED 25 (пропуск 01.10 —
  # реальный простой LCD), Ozon LCD_COMMITTED 26 (ежедневно), store_pnl_tab_published 15,
  # ozon_store_pnl_readback 3 (расписание было на паузе до 20.10).
  # `jsonPayload.message` здесь — имя события: logLifecycle и эти logger.info не передают ctx.message.
  unitka_heartbeats = {
    unitka_wb_lcd_committed = {
      job      = "unitka-engine-prod"
      event    = "jsonPayload.message=\"unitka_lifecycle\"\njsonPayload.lifecycle_event=\"LCD_COMMITTED\"\njsonPayload.platform=\"WB\""
      duration = "3600s"
      title    = "UNITKA WB: LCD не сдвигался 25 ч"
      runbook  = "LCD WB (ZZ_CONFIG!B2) не закоммичен 25 ч (письмо ~08:02Z, до резерва 09:30Z: если резерв закоммитит — алерт закроется сам). Смотреть unitka_lifecycle (LCD_NOT_ADVANCED: notes), wb_ops.UNITKA_ENGINE_RUNS, паузу unitka-engine-prod-morning/-reserve, UNITKA_WRITE_ENABLED. 2-го числа — отказ создания месяца (docs/ops/UNITKA_MONTH_TRANSITION_2026-11.md). Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    }
    unitka_ozon_lcd_committed = {
      job      = "ozon-unitka-prod"
      event    = "jsonPayload.message=\"unitka_lifecycle\"\njsonPayload.lifecycle_event=\"LCD_COMMITTED\"\njsonPayload.platform=\"OZON\""
      duration = "3600s"
      title    = "UNITKA Ozon: LCD не сдвигался 25 ч"
      runbook  = "OZON_LAST_CLOSED_DATE (ZZ_CONFIG!B30) не закоммичен 25 ч. Смотреть unitka_lifecycle ozon-unitka-prod, LOADER_RUNS(ozon-unitka), паузу расписания, OZON_UNITKA_WRITE_ENABLED. Runbook: docs/ops/OZON_UNITKA_OPERATIONS.md"
    }
    unitka_store_pnl_published = {
      job      = "unitka-store-pnl-prod"
      event    = "jsonPayload.message=\"store_pnl_tab_published\"\njsonPayload.action=(\"WRITE\" OR \"NOOP\")"
      duration = "3600s"
      title    = "WB STORE P&L: вкладка не публиковалась 25 ч"
      runbook  = "«WB Магазин P&L» не публиковалась (WRITE/NOOP) 25 ч: пауза unitka-store-pnl-prod, UNITKA_STORE_PNL_PUBLISH=0, гейт STORE_PNL_PUBLISH_GATE или отказ. Runbook: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md"
    }
    ozon_store_pnl_published = {
      job      = "ozon-store-pnl-prod"
      event    = "jsonPayload.message=\"ozon_store_pnl_readback\""
      duration = "3600s"
      title    = "OZON STORE P&L: вкладка не подтверждена 25 ч"
      runbook  = "«Ozon Магазин P&L» без успешного перечитывания 25 ч: пауза ozon-store-pnl-prod, OZON_STORE_PNL_PUBLISH=0, гейт OZON_STORE_PNL_PUBLISH_GATE/SOURCE_NOT_READY. Runbook: docs/finance/OZON_STORE_PNL_PHASE_D4.md"
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

# ── Класс 1. Нет полезного результата.
resource "google_monitoring_alert_policy" "unitka_heartbeat" {
  for_each     = { for k, v in local.unitka_heartbeats : k => v if var.unitka_alert_email != "" }
  display_name = each.value.title
  combiner     = "OR"
  enabled      = false # включается по ACK не раньше чем через 25 ч после появления метрики (задним числом не заполняется)

  conditions {
    display_name = "нет события ${each.key} за 24 ч дольше ${each.value.duration}"
    condition_prometheus_query_language {
      query               = "(sum(increase(logging_googleapis_com:user_${google_logging_metric.unitka_heartbeat[each.key].name}{monitored_resource=\"cloud_run_job\"}[24h])) or vector(0)) < 1"
      duration            = each.value.duration
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

# ── Класс 2. Сбой любого из четырёх писателей. Проверено на логах 20.09–20.10.2026: фильтр ловит ровно
#    5 реальных отказов (22.09 LOADER_ERROR, 06.10 zbfk7 без кода, 07.10 SHEETS_API, 11.10 SHEETS_API,
#    20.10 INVARIANT_FAIL); сигналов сверки — 0. Блоки публикации P&L исключены (класс 3).
resource "google_logging_metric" "unitka_writer_failures" {
  name   = "unitka_writer_failures"
  filter = <<-EOT
    resource.type="cloud_run_job"
    ${local.unitka_writer_jobs_filter}
    severity>=ERROR
    ((jsonPayload.code!="" AND NOT jsonPayload.message=${local.unitka_attention_events} AND NOT jsonPayload.code=${local.unitka_blocked_codes}) OR (logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event" AND protoPayload.methodName="/Jobs.RunJob" AND NOT resource.labels.job_name=${local.unitka_pnl_jobs}))
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "unitka_writers_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: сбой прогона или источника"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "отказ писателя за ${local.unitka_signal_window} (по job_name)"
    condition_prometheus_query_language {
      query               = "sum by (job_name) (increase(logging_googleapis_com:user_${google_logging_metric.unitka_writer_failures.name}{monitored_resource=\"cloud_run_job\"}[${local.unitka_signal_window}])) > 0"
      duration            = "0s"
      evaluation_interval = "60s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Сбой писателя: прогон упал, или прогон завершён, но LCD придержан дырой покрытия источника (LCD_HELD_BY_COVERAGE_GAP) / упала проверка целостности (unitka_integrity_failed) (метка job_name; код отказа — в журнале прогона: severity>=ERROR, jsonPayload.code). Пока алерт открыт (24,5 ч после последнего отказа), повторные отказы того же писателя нового письма не дают — смотреть журнал. WB: резерв 09:30 UTC повторит, детерминированный код (INVARIANT_FAIL, QA) резерв не лечит. 2-го числа коды CHAIN_GAP / COGS_* / TEMPLATE_MISMATCH / MONTH_* / OZON_UNITKA_* — новый месяц не создан (docs/ops/UNITKA_MONTH_TRANSITION_2026-11.md). Детали — wb_ops.UNITKA_ENGINE_RUNS / wb_raw.LOADER_RUNS. Runbook: docs/UNITKA_ENGINE_V1_RUNBOOK.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Класс 3. Публикация P&L заблокирована / новая операция.
resource "google_logging_metric" "unitka_publication_blocked_events" {
  name   = "unitka_publication_blocked_events"
  filter = <<-EOT
    resource.type="cloud_run_job"
    ${local.unitka_writer_jobs_filter}
    severity>=WARNING
    (jsonPayload.code=${local.unitka_blocked_codes} OR jsonPayload.message=${local.unitka_blocked_events})
  EOT
  # Без меток: ряд на писателя даёт ресурсная метка job_name. У одного блока несколько записей
  # (предупреждение гейта и код исключения) — разметка по коду открыла бы два алерта на одну причину.
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "unitka_publication_blocked" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "P&L: публикация заблокирована / новая операция"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "гейт публикации или неразобранная операция за ${local.unitka_signal_window}"
    condition_prometheus_query_language {
      query               = "sum by (job_name) (increase(logging_googleapis_com:user_${google_logging_metric.unitka_publication_blocked_events.name}{monitored_resource=\"cloud_run_job\"}[${local.unitka_signal_window}])) > 0"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Публикация P&L магазина заблокирована. Ozon: OZON_STORE_PNL_PUBLISH_GATE — новый/неразобранный тип начисления (ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS, QA Q03/Q05) или нарушение модели; OZON_STORE_PNL_SOURCE_NOT_READY — источники не свежие. WB: STORE_PNL_PUBLISH_GATE — гейт модели; store_pnl_new_operations — новая операция финотчёта (V_WB_FINANCE_NEW_OPERATIONS), месяц в PENDING_CLASSIFICATION. Классификация — только решением владельца (пример: тип 125, PR #366). Пока не разобрано — лист остаётся на прошлой публикации; через 25 ч добавится heartbeat публикации. Алерт закроется сам через 24,5 ч без сигнала."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Класс 4. Прогон завершён, нужен владелец. Одно письмо на (писатель, событие) при появлении;
#    пока событие повторяется в каждом прогоне — тишина.
resource "google_logging_metric" "unitka_owner_attention_events" {
  name   = "unitka_owner_attention_events"
  filter = <<-EOT
    resource.type="cloud_run_job"
    ${local.unitka_writer_jobs_filter}
    severity>=WARNING
    jsonPayload.message=${local.unitka_attention_events}
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key        = "event"
      value_type = "STRING"
    }
  }
  label_extractors = {
    event = "EXTRACT(jsonPayload.message)"
  }
}

resource "google_monitoring_alert_policy" "unitka_owner_attention" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА: нужна проверка владельца (перерасчёты, сверка)"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "сигнал сверки за ${local.unitka_signal_window} (по job_name, event)"
    condition_prometheus_query_language {
      query               = "sum by (job_name, event) (increase(logging_googleapis_com:user_${google_logging_metric.unitka_owner_attention_events.name}{monitored_resource=\"cloud_run_job\"}[${local.unitka_signal_window}])) > 0"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Прогон завершён, но нужен владелец. unitka_controlled_refused — правка прошлого месяца отклонена (RECON_RESTRICTED_REQUIRES_ACK, RECON_WITHDRAWAL_REQUIRES_ACK, RECON_CORRECTION_CAP_EXCEEDED, RECON_OUT_OF_SCOPE, RECON_CANCELS_*) и ждёт решения; окно сверки 35 дней — после выхода дня из окна правка не применится; список ячеек — в самом событии. unitka_recon_residual_persistent — остаток сверки держится. unitka_integrity_data_error — расхождение источников (observe). ozon_unitka_cpo_unresolved — строки CPO Ozon без SKU. Алерт закроется сам через 24,5 ч без сигнала."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}

# ── Класс 5. Пауза / изменение / удаление расписания писателя; изменение или удаление задания писателя
#    (PUBLISH=0 / UNITKA_WRITE_ENABLED=0 / образ). lifecycle.ignore_changes = [paused] у расписаний: пауза
#    вне Terraform живёт бессрочно. Штатные выкаты образа идут от sa-deployer по ACK — исключены
#    (за 30 дней 46 из 64 ReplaceJob). Проверено на журнале аудита 20.09–20.10.2026: 14 PauseJob, все —
#    реальные паузы, включая 15.10 ozon-store-pnl-prod; ReplaceJob 20.10 PUBLISH=1 ozon-store-pnl-prod.
resource "google_monitoring_alert_policy" "unitka_writer_config" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "ЮНИТКА/P&L: пауза расписания или изменение задания писателя"
  combiner     = "OR"
  enabled      = false

  conditions {
    display_name = "Pause/Update/Delete расписания или Replace/Update/Delete задания писателя"
    condition_matched_log {
      filter = <<-EOT
        logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
        ((protoPayload.serviceName="cloudscheduler.googleapis.com"
          protoPayload.methodName=("google.cloud.scheduler.v1.CloudScheduler.PauseJob" OR "google.cloud.scheduler.v1.CloudScheduler.UpdateJob" OR "google.cloud.scheduler.v1.CloudScheduler.DeleteJob")
          protoPayload.resourceName=~"/jobs/(unitka-engine-prod-(morning|reserve)|ozon-unitka-prod|unitka-store-pnl-prod|ozon-store-pnl-prod)$")
        OR (protoPayload.serviceName="run.googleapis.com"
          protoPayload.methodName=("google.cloud.run.v1.Jobs.ReplaceJob" OR "google.cloud.run.v2.Jobs.UpdateJob" OR "google.cloud.run.v1.Jobs.DeleteJob" OR "google.cloud.run.v2.Jobs.DeleteJob")
          protoPayload.resourceName=~"/jobs/(unitka-engine-prod|ozon-unitka-prod|unitka-store-pnl-prod|ozon-store-pnl-prod)$"
          NOT protoPayload.authenticationInfo.principalEmail="${google_service_account.deployer.email}"))
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
    content   = "Расписание писателя на паузе / изменено / удалено или задание изменено не штатным выкатом (метки resource, method, actor). Если это не окно выпуска по ACK — проверить gcloud scheduler jobs describe / gcloud run jobs describe (UNITKA_STORE_PNL_PUBLISH, OZON_STORE_PNL_PUBLISH, UNITKA_WRITE_ENABLED, образ). Простой без паузы ловит heartbeat."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
