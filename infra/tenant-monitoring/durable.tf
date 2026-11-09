# ── Локальные состояния устойчивого контура A2 (PR #404, «durable daily») ─────────────────────
# После A2 временные сбои больше не дают глобальный STOP, а становятся локальными состояниями.
# Их нужно видеть, иначе «тихий» контур может стоять сутками:
#   * CURRENT: сущность держится UNKNOWN (current_entities.<e> = "UNKNOWN", current_holds) много
#     пробуждений подряд; явный PARTIAL сущности (FBO SOURCE_DISAPPEARED приходит как PARTIAL);
#   * WAITING_READ_RETRY подряд: застрявший запрос BigQuery теперь повторяется бесконечно вместо STOP;
#   * FULL: локальный STOP листа (stage PERSISTED_SOURCE_RECONCILIATION с контекстом листа) — до 3
#     листьев пропускаются; больше 3 — basis FULL_HISTORY_LOCAL_STOP, очередь FULL закрыта;
#   * типизированный инцидент SOURCE_DISAPPEARED (поток B, область OWNER_FBO_REFRESH_V1).
# Поля — из кода #404 (cloud_controller.bounded_wake, durable_daily.entity_status/hold_current,
# controller_diagnostics.safe, full_controller.wake). Лог-метрики создаются сразу (задним числом не
# заполняются, до выпуска #404 считают ноль); политики включаются `durable_daily_alerts = true` только
# после выпуска образа #404 — до него этих полей в логах нет и включать нечего.
locals {
  durable_on = var.durable_daily_alerts
  # Метка метрики = сущность суточного контура (current_policy.ORDER, 8 ≤ 10 меток лог-метрики).
  current_entity_labels = ["catalog", "stocks", "prices", "finance_accrual", "ads_campaigns", "ads_expense_daily", "ads_sku_daily", "fbo_postings"]
  status_metric         = "logging_googleapis_com:user_heldi_current_entity_status{monitored_resource=\"cloud_run_job\""
  # UNKNOWN ≥ N раз за 90 мин и ни одного COMPLETE/PARTIAL той же сущности за это окно = «N пробуждений подряд».
  # Тик */3, но решений меньше: перекрытые пробуждения пишут WAIT_ACTIVE_CONTROLLER. Замер 26.10–05.11:
  # 5,2 итоговых решения в час, медианный разрыв 12 мин, p95 18 мин. N = 5 за 90 мин ≈ час подряд.
  hold_query = join(" or ", [for e in local.current_entity_labels :
  "(label_replace(sum(increase(${local.status_metric},${e}=\"UNKNOWN\"}[90m])), \"entity\", \"${e}\", \"\", \"\") >= ${var.hold_wakes} unless on() (sum(increase(${local.status_metric},${e}=~\"COMPLETE|PARTIAL\"}[90m])) > 0))"])
  # PARTIAL держится всё время, пока сущность частична: окно 3 ч — один инцидент на эпизод, как у STOP.
  partial_query = join(" or ", [for e in local.current_entity_labels :
  "label_replace(sum(increase(${local.status_metric},${e}=\"PARTIAL\"}[3h])), \"entity\", \"${e}\", \"\", \"\") > 0"])
  status_by = "logging_googleapis_com:user_heldi_controller_status{monitored_resource=\"cloud_run_job\""
  # Бэктест 26.10–05.11 (4675 строк статуса): подряд не больше 3 WAITING_READ_RETRY, при N = 5 за 90 мин —
  # 0 срабатываний; при застрявшем запросе (5,2 пробуждения в час) тревога примерно через час.
  retry_query  = "(sum(increase(${local.status_by},status=\"WAITING_READ_RETRY\"}[90m])) >= ${var.read_retry_streak}) unless on() (sum(increase(${local.status_by},status!~\"WAITING_READ_RETRY|WAIT_ACTIVE_CONTROLLER|WAITING_LEASE\"}[90m])) > 0)"
  leaf_stopped = "jsonPayload.status=\"STOPPED\" AND jsonPayload.diagnostic.stage=\"PERSISTED_SOURCE_RECONCILIATION\" AND jsonPayload.diagnostic.context.index:*"
}

resource "google_logging_metric" "current_entity_status" {
  name        = "heldi_current_entity_status"
  description = "Статусы сущностей CURRENT сегодняшних суток МСК в результате пробуждения (A2): COMPLETE, PARTIAL, UNKNOWN, STALE."
  filter      = "${local.controller_stdout} AND jsonPayload.current_entities:*"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    dynamic "labels" {
      for_each = local.current_entity_labels
      content {
        key         = labels.value
        value_type  = "STRING"
        description = "Статус ${labels.value} (пусто — сущности нет в результате)."
      }
    }
  }
  label_extractors = { for e in local.current_entity_labels : e => "EXTRACT(jsonPayload.current_entities.${e})" }
  depends_on       = [terraform_data.target_guard]
}

resource "google_logging_metric" "full_leaf_stop" {
  name        = "heldi_full_leaf_stop"
  description = "Локальный STOP листа FULL (PERSISTED_SOURCE_RECONCILIATION с контекстом листа); A2 пропускает до 3 таких листьев."
  filter      = "${local.controller_stdout} AND ${local.leaf_stopped}"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "leaf"
      value_type  = "STRING"
      description = "Индекс листа FULL (diagnostic.context.index)."
    }
  }
  label_extractors = { leaf = "EXTRACT(jsonPayload.diagnostic.context.index)" }
  depends_on       = [terraform_data.target_guard]
}

resource "google_logging_metric" "full_local_systemic" {
  name        = "heldi_full_local_stop_systemic"
  description = "Больше 3 локальных STOP листьев: очередь FULL закрыта (basis FULL_HISTORY_LOCAL_STOP)."
  filter      = "${local.controller_stdout} AND jsonPayload.basis=\"FULL_HISTORY_LOCAL_STOP\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [terraform_data.target_guard]
}

# Типизированный инцидент потока B (OWNER_FBO_REFRESH_V1, REFRESH_PARTIAL reason SOURCE_DISAPPEARED).
# Форма строки ещё не зафиксирована в коде, поэтому поиск по значению в любом поле строки контроллера;
# тот же эпизод виден и как PARTIAL fbo_postings в current_entities (политика current_partial).
resource "google_logging_metric" "source_disappeared" {
  name        = "heldi_source_disappeared"
  description = "Инцидент SOURCE_DISAPPEARED: Ozon больше не возвращает ранее загруженные отправления (локальный PARTIAL дня)."
  filter      = "${local.controller_stdout} AND \"SOURCE_DISAPPEARED\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [terraform_data.target_guard]
}

resource "google_monitoring_alert_policy" "current_hold" {
  display_name = "Heldi ${local.who}: сущность CURRENT держится UNKNOWN"
  combiner     = "OR"
  severity     = "ERROR"
  enabled      = local.durable_on
  conditions {
    display_name = "UNKNOWN ${var.hold_wakes}+ пробуждений за 90 мин без COMPLETE"
    condition_prometheus_query_language {
      query                     = local.hold_query
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: сущность CURRENT держится UNKNOWN (метка entity в письме)"
    mime_type = "text/markdown"
    content   = "Сущность (метка entity) сегодняшних суток МСК держится UNKNOWN не меньше ${var.hold_wakes} пробуждений подряд: проверка сертификата на урегулированном шарде не проходит (A2, durable_daily.hold_current). Магазин не остановлен, остальные сущности идут дальше, но эта за сегодня не сертифицирована. Подробности — поле current_holds в результате пробуждения. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "current_hold" }
  depends_on  = [google_logging_metric.current_entity_status]
}

resource "google_monitoring_alert_policy" "current_partial" {
  display_name = "Heldi ${local.who}: сущность CURRENT частична (PARTIAL)"
  combiner     = "OR"
  severity     = "WARNING"
  enabled      = local.durable_on
  conditions {
    display_name = "current_entities = PARTIAL"
    condition_prometheus_query_language {
      query                     = local.partial_query
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: данные сущности частичны (PARTIAL, метка entity в письме)"
    mime_type = "text/markdown"
    content   = "Сущность (метка entity) сегодня PARTIAL: есть явный разрыв данных (для fbo_postings — SOURCE_DISAPPEARED, Ozon больше не возвращает ранее загруженные отправления). Это локально: остальные сущности и дни идут дальше; ничего не удаляется, решение по строкам — за владельцем. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "current_partial" }
  depends_on  = [google_logging_metric.current_entity_status]
}

resource "google_monitoring_alert_policy" "read_retry_streak" {
  display_name = "Heldi ${local.who}: чтение повторяется, нет решений"
  combiner     = "OR"
  severity     = "ERROR"
  enabled      = local.durable_on
  conditions {
    display_name = "WAITING_READ_RETRY ${var.read_retry_streak}+ за 90 мин, других решений нет"
    condition_prometheus_query_language {
      query                     = local.retry_query
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = local.stop_channels
  documentation {
    subject   = "Heldi ${var.tenant_id}: загрузка стоит — чтение повторяется без продвижения"
    mime_type = "text/markdown"
    content   = "${var.read_retry_streak} и больше пробуждений за 90 мин закончились WAITING_READ_RETRY, и ни одно не дошло до решения. После A2 застрявший запрос BigQuery или долгий 403/rateLimit повторяются бесконечно вместо STOP: магазин не остановлен, но данные не обновляются. Причина — jsonPayload.reason в строках WAITING_READ_RETRY. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "read_retry_streak" }
  depends_on  = [google_logging_metric.controller_status]
}

resource "google_monitoring_alert_policy" "full_leaf_stop" {
  display_name = "Heldi ${local.who}: локальный STOP листа FULL"
  combiner     = "OR"
  severity     = "WARNING"
  enabled      = local.durable_on
  conditions {
    display_name = "STOPPED листа (PERSISTED_SOURCE_RECONCILIATION)"
    condition_prometheus_query_language {
      # Строка пишется один раз на лист: окно 3 ч — одно письмо на лист.
      query                     = "sum by (leaf) (increase(logging_googleapis_com:user_heldi_full_leaf_stop{monitored_resource=\"cloud_run_job\"}[3h])) > 0"
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: локальный STOP листа FULL (метка leaf в письме)"
    mime_type = "text/markdown"
    content   = "Лист FULL (метка leaf) остановлен локально: доказательство завершения уже сверенного листа не прошло (A2, класс PERSISTED_SOURCE_RECONCILIATION). CURRENT и остальная история идут дальше; лист пропускается, пока его не разберут отдельным восстановлением. Больше 3 таких листьев закрывают FULL целиком (отдельное письмо). ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "full_leaf_stop" }
  depends_on  = [google_logging_metric.full_leaf_stop]
}

resource "google_monitoring_alert_policy" "full_local_systemic" {
  display_name = "Heldi ${local.who}: FULL закрыт — больше 3 локальных STOP"
  combiner     = "OR"
  severity     = "ERROR"
  enabled      = local.durable_on
  conditions {
    display_name = "basis FULL_HISTORY_LOCAL_STOP"
    condition_prometheus_query_language {
      query                     = "sum(increase(logging_googleapis_com:user_heldi_full_local_stop_systemic{monitored_resource=\"cloud_run_job\"}[3h])) > 0"
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = local.stop_channels
  documentation {
    subject   = "Heldi ${var.tenant_id}: история FULL остановлена — больше 3 локальных STOP листьев"
    mime_type = "text/markdown"
    content   = "Неразобранных локальных STOP листьев FULL больше 3: это системная проблема, очередь FULL закрыта (basis FULL_HISTORY_LOCAL_STOP, список — local_full_leaves). CURRENT продолжает работать. ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "full_local_systemic" }
  depends_on  = [google_logging_metric.full_local_systemic]
}

resource "google_monitoring_alert_policy" "source_disappeared" {
  display_name = "Heldi ${local.who}: SOURCE_DISAPPEARED (заказы)"
  combiner     = "OR"
  severity     = "WARNING"
  enabled      = local.durable_on
  conditions {
    display_name = "Инцидент SOURCE_DISAPPEARED"
    condition_prometheus_query_language {
      query                     = "sum(increase(logging_googleapis_com:user_heldi_source_disappeared{monitored_resource=\"cloud_run_job\"}[3h])) > 0"
      duration                  = "0s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: SOURCE_DISAPPEARED — Ozon больше не отдаёт ранее загруженные отправления"
    mime_type = "text/markdown"
    content   = "При повторном чтении дня Ozon вернул меньше отправлений, чем уже лежит в RAW (rows − run_rows = extra > 0). День помечен PARTIAL локально, ничего не удалено; решение по строкам — за владельцем (A2 DESIGN §8). ${local.watch_runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "source_disappeared" }
  depends_on  = [google_logging_metric.source_disappeared]
}
