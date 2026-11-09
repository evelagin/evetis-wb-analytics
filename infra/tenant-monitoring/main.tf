# Сигналы проверены чтением реальных логов mpa-t-client-001 (19.10.2026):
#   * STOPPED — stdout контроллера `{"status":"STOPPED",...}`, 11–17.10 по ~20 записей в час
#     (каждый тик, exit 2): поэтому метрика + PromQL, а не log-match с письмом на каждый тик;
#   * RECONCILED_BOUNDARY + current_complete=true — сертификат суток (CURRENT_CERT: данные
#     записаны и сверены), по одной записи на сущность в сутки: 14, 15, 16 (×2), 19.10
#     catalog/stocks/prices/finance_accrual; реклама и FBO — ни одной;
#   * AttemptFinished tenant-backfill-tick: успех без поля status, отказ — severity ERROR
#     и jsonPayload.status (как у Юнитки, scheduler.tf); за 30 суток отказов нет;
#   * run.googleapis.com/job/completed_execution_count{job_name, result=succeeded|failed}.
locals {
  controller_stdout = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"tenant-backfill-controller\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstdout\"",
  ])
  who = "${var.tenant_id} (${var.project_id})"
  # STOP и проверка доставки: почта + push приложения Google Cloud (решение владельца 27.10).
  # Канал push создаёт само приложение при выборе проекта; Terraform им не управляет, только ссылается.
  stop_channels = concat([google_monitoring_notification_channel.owner_email.id], var.owner_push_channel == null ? [] : [var.owner_push_channel])
  runbook       = "Регламент: docs/ops/TENANT_ALERTS_CLIENT001.md. Автоматического восстановления нет; аварийная пауза — только по действующему ACK владельца."
  # Окно 24 ч = snapshot_sla_seconds/fbo_sla_seconds политики суточного контура (86400).
  # Для пользовательских лог-метрик PromQL видит не больше 25 ч: окно 24 ч + повтор 30 мин.
  # Счёт событий, а не наличие точек: лог-метрика пишет нули рядом с событием, и
  # absent_over_time принял бы ряд из одних нулей за «есть данные» (проверено 20.10 на ряде
  # completed_execution_count: окно из 2–3 нулевых точек → absent пусто, increase 0).
  # Нет ряда вовсе → `or vector(0)`. Сущность ставится label_replace: инцидент на сущность.
  # Группа (catalog, stocks, prices, finance, orders, ads) — один инцидент: метка entity = имя группы,
  # а `or` отбрасывает справа ряд с той же меткой, поэтому три устаревшие сущности рекламы дают ОДНО письмо.
  freshness_query = join(" or ", flatten([for g in sort(keys(var.freshness_groups)) : [for e in var.freshness_groups[g] :
  "label_replace((sum(increase(logging_googleapis_com:user_tenant_current_certified{monitored_resource=\"cloud_run_job\",entity=\"${e}\"}[24h])) or vector(0)), \"entity\", \"${g}\", \"\", \"\") < 1"]]))
  # То же для завершений контроллера: нет ряда и ряд из нулей — оба «0 успешных завершений».
  # Окно 60 мин (было 30): бэктест 30.10–05.11 — 25 ложных открытий при 30 мин (WAIT-исполнения
  # завершаются ошибкой, разрывы между успешными до 49 мин), 0 при 60 мин.
  completions_query = "(sum(increase(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"tenant-backfill-controller\",result=\"succeeded\"}[60m])) or vector(0)) < 1"
  # STOP: окно 3 ч (было 10 мин). Под STOP строки STOPPED идут не на каждом тике (между ними
  # WAIT_ACTIVE_CONTROLLER), разрывы 05.11–09.11 до 133 мин. При 10 мин инцидент закрывался и
  # открывался заново: за STOP eca0335a — 58 инцидентов и 58 писем. При 3 ч — один.
  stop_query = "sum(increase(logging_googleapis_com:user_tenant_controller_stopped{monitored_resource=\"cloud_run_job\"}[3h])) > 0"
}

resource "terraform_data" "target_guard" {
  input = { tenant_id = var.tenant_id, project_id = var.project_id }
  lifecycle {
    precondition {
      condition     = var.project_id == "mpa-t-${replace(var.tenant_id, "_", "-")}"
      error_message = "Проект не совпадает с арендатором: оповещения создаются только в проекте этого арендатора."
    }
  }
}

resource "google_monitoring_notification_channel" "owner_email" {
  display_name = "Владелец — оповещения ${local.who}"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [terraform_data.target_guard]
}

# ── Лог-метрики (считают только записи после создания, задним числом не заполняются) ──
resource "google_logging_metric" "controller_stopped" {
  name        = "tenant_controller_stopped"
  description = "Тик контроллера завершился STOPPED (новый или неразрешённый STOP)."
  filter      = "${local.controller_stdout} AND jsonPayload.status=\"STOPPED\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [terraform_data.target_guard]
}

resource "google_logging_metric" "current_certified" {
  name        = "tenant_current_certified"
  description = "Сертификат суток суточного контура (CURRENT_CERT) по сущности: данные записаны и сверены."
  filter      = "${local.controller_stdout} AND jsonPayload.status=\"RECONCILED_BOUNDARY\" AND jsonPayload.current_complete=true"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "entity"
      value_type  = "STRING"
      description = "Сущность суточного контура (operational_entity)."
    }
  }
  label_extractors = { entity = "EXTRACT(jsonPayload.operational_entity)" }
  depends_on       = [terraform_data.target_guard]
}

resource "google_logging_metric" "tick_attempt_failed" {
  name        = "tenant_tick_attempt_failed"
  description = "Cloud Scheduler вызвал tenant-backfill-tick, но исполнение контроллера не создано."
  filter = join(" AND ", [
    "resource.type=\"cloud_scheduler_job\"",
    "resource.labels.job_id=\"tenant-backfill-tick\"",
    "severity>=ERROR",
    "jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\"",
  ])
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "Код отказа Scheduler (PERMISSION_DENIED, NOT_FOUND, UNAVAILABLE...)."
    }
  }
  label_extractors = { status = "EXTRACT(jsonPayload.status)" }
  depends_on       = [terraform_data.target_guard]
}

# Только для проверки доставки: запись в этот журнал пишет владелец (см. регламент).
resource "google_logging_metric" "delivery_test" {
  name        = "tenant_alert_delivery_test"
  description = "Ручная проверка доставки оповещений владельцу."
  filter      = "logName=\"projects/${var.project_id}/logs/tenant-alert-delivery-test\" AND resource.type=\"global\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "test_id"
      value_type  = "STRING"
      description = "Идентификатор проверки из записи владельца."
    }
  }
  label_extractors = { test_id = "EXTRACT(jsonPayload.test_id)" }
  depends_on       = [terraform_data.target_guard]
}

# ── Политики. PromQL-инцидент закрывается сам через ~5 мин после того, как условие перестало
# выполняться, и следующее срабатывание открывает НОВЫЙ инцидент с новым письмом. Поэтому окно
# условия должно перекрывать самый длинный штатный разрыв сигнала (замер 05.11–09.11: 58 писем
# за один STOP при окне 10 мин). Тема каждого письма начинается с «Heldi». ──
resource "google_monitoring_alert_policy" "controller_stopped" {
  display_name = "Heldi ${local.who}: контроллер в STOP"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "Тики завершаются STOPPED"
    condition_prometheus_query_language {
      query                     = local.stop_query
      duration                  = "0s"
      evaluation_interval       = "60s"
      disable_metric_validation = true
    }
  }
  # Закрытие инцидента после паузы тика не означает, что STOP разрешён: письма о закрытии нет.
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = local.stop_channels
  documentation {
    # Тема письма. Без неё Google пишет «[ALERT - Critical] Тики завершаются STOPPED on __missing__»:
    # ни слова о Heldi, письмо уходит во вкладку «Оповещения» Gmail (так было 05.11).
    subject   = "Heldi STOP: контроллер ${var.tenant_id} остановлен, данные не обновляются"
    mime_type = "text/markdown"
    content   = "Контроллер ${local.who} остановлен STOP: в Ozon он больше ничего не отправляет (fail-closed), каждый тик завершается STOPPED. Данные не обновляются до разрешения STOP новым OWNER ACK. Причина — в логе тика (jsonPayload.reason, diagnostic). ${local.runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "controller_stop" }
  # Метрика должна существовать до политики, ссылающейся на неё по имени.
  depends_on = [google_logging_metric.controller_stopped]
}

resource "google_monitoring_alert_policy" "tick_attempt_failed" {
  display_name = "Heldi ${local.who}: Scheduler не запускает контроллер"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "Два и более отказа tenant-backfill-tick за 15 мин"
    condition_prometheus_query_language {
      # Одиночный временный отказ закрывает следующий тик через 3 мин — это не авария.
      query                     = "sum by (status) (increase(logging_googleapis_com:user_tenant_tick_attempt_failed{monitored_resource=\"cloud_scheduler_job\"}[15m])) >= 2"
      duration                  = "0s"
      evaluation_interval       = "60s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: Scheduler не запускает контроллер"
    mime_type = "text/markdown"
    content   = "Расписание tenant-backfill-tick срабатывает, но исполнение контроллера не создаётся — логов контроллера не будет. Код в метке status: PERMISSION_DENIED — у SA расписания нет run.invoker; NOT_FOUND — задание переименовано или удалено; UNAVAILABLE/DEADLINE_EXCEEDED — отказ API. ${local.runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "tick_attempt" }
  # Метрика должна существовать до политики, ссылающейся на неё по имени.
  depends_on = [google_logging_metric.tick_attempt_failed]
}

resource "google_monitoring_alert_policy" "controller_completions" {
  display_name = "Heldi ${local.who}: нет успешных завершений контроллера"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "60 мин без succeeded-исполнений tenant-backfill-controller"
    condition_prometheus_query_language {
      # Тик каждые 3 мин: 60 мин — 20 тиков. Штатные ожидания (WAITING_READ_RETRY завершаются
      # с ошибкой) не мешают соседним тикам завершиться успешно.
      query               = local.completions_query
      duration            = "0s"
      evaluation_interval = "60s"
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: контроллер 60 мин без успешных завершений"
    mime_type = "text/markdown"
    content   = "Контроллер ${local.who} 60 мин не завершается успешно: tick на паузе (в т.ч. плановой или аварийной), Scheduler не запускает задание, исполнения падают или зависли, либо действует STOP (тогда открыт и инцидент STOP). ${local.runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "controller_completions" }
}

resource "google_monitoring_alert_policy" "data_freshness" {
  display_name = "Heldi ${local.who}: данные сущности не обновлены 27 ч"
  combiner     = "OR"
  severity     = "WARNING"
  enabled      = var.freshness_enabled
  conditions {
    display_name = "Нет сертификата суток по группе сущностей 27 ч"
    condition_prometheus_query_language {
      # Окно 24 ч + удержание 3 ч = тревога через 27 ч после последнего сертификата. Сертификат
      # суток приходит раз в сутки, разрывы 29.10–05.11 — 23,6–24,5 ч: при удержании 30 мин окно
      # 24 ч открывало бы ложный инцидент почти каждую ночь. Пропущенная загрузка суток видна
      # к ~04:00 МСК следующего дня (сертификаты 00:20–01:45 МСК).
      query                     = local.freshness_query
      duration                  = "10800s"
      evaluation_interval       = "300s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED", "CLOSED"] }
  notification_channels = [google_monitoring_notification_channel.owner_email.id]
  documentation {
    subject   = "Heldi ${var.tenant_id}: данные не обновлены 27 ч (группа — в письме, метка entity)"
    mime_type = "text/markdown"
    content   = "У группы сущностей (метка entity: catalog, stocks, prices, finance, orders, ads) 27 ч нет сертификата суток: данные не записаны или не сверены, даже если исполнения контроллера и runtime успешны (например, 429 рекламы или исключённый FBO). Окно = SLA политики суточного контура (86400 с) + 3 ч удержания. Проверка покрытия: tenant_ops.V_COVERAGE_DAILY. ${local.runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "data_freshness" }
  # Метрика должна существовать до политики, ссылающейся на неё по имени.
  depends_on = [google_logging_metric.current_certified]
}

resource "google_monitoring_alert_policy" "delivery_test" {
  display_name = "Heldi ${local.who}: проверка доставки оповещений"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "Запись владельца в журнал tenant-alert-delivery-test"
    condition_prometheus_query_language {
      query                     = "sum by (test_id) (increase(logging_googleapis_com:user_tenant_alert_delivery_test{monitored_resource=\"global\"}[10m])) > 0"
      duration                  = "0s"
      evaluation_interval       = "60s"
      disable_metric_validation = true
    }
  }
  alert_strategy { notification_prompts = ["OPENED"] }
  notification_channels = local.stop_channels
  documentation {
    mime_type = "text/markdown"
    content   = "Это проверка, а не авария. Доставка доказана, когда владелец подтверждает письмо с этим test_id. ${local.runbook}"
  }
  user_labels = { tenant = var.tenant_id, signal = "delivery_test" }
  # Метрика должна существовать до политики, ссылающейся на неё по имени.
  depends_on = [google_logging_metric.delivery_test]
}
