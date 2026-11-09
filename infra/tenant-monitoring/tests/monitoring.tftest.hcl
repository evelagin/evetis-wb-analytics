# Offline: mock provider, no cloud call. Synthetic e-mail only.
mock_provider "google" {}

variables {
  tenant_id   = "client_001"
  project_id  = "mpa-t-client-001"
  alert_email = "owner@example.invalid"
}

run "defaults_are_tenant_scoped_and_freshness_starts_disabled" {
  command = plan
  assert {
    condition     = google_monitoring_alert_policy.data_freshness.enabled == false && google_monitoring_alert_policy.controller_stopped.enabled != false
    error_message = "Свежесть включается только вторым apply; остальные политики сразу."
  }
  assert {
    condition     = alltrue([for m in [google_logging_metric.controller_stopped, google_logging_metric.current_certified] : strcontains(m.filter, "logName=\"projects/mpa-t-client-001/logs/run.googleapis.com%2Fstdout\"")])
    error_message = "Сигналы контроллера читаются только из stdout контроллера этого проекта."
  }
  assert {
    condition     = google_monitoring_notification_channel.owner_email.type == "email"
    error_message = "Канал — почта владельца."
  }
}

run "freshness_covers_every_daily_entity_with_policy_sla" {
  command = plan
  variables { freshness_enabled = true }
  assert {
    condition = google_monitoring_alert_policy.data_freshness.enabled == true && alltrue([for p in [["catalog", "catalog"], ["stocks", "stocks"], ["prices", "prices"], ["finance", "finance_accrual"], ["orders", "fbo_postings"], ["ads", "ads_campaigns"], ["ads", "ads_expense_daily"], ["ads", "ads_sku_daily"]] :
    strcontains(google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query, "label_replace((sum(increase(logging_googleapis_com:user_tenant_current_certified{monitored_resource=\"cloud_run_job\",entity=\"${p[1]}\"}[24h])) or vector(0)), \"entity\", \"${p[0]}\", \"\", \"\") < 1")])
    error_message = "Все 8 сущностей суточного контура в 6 группах (catalog, stocks, prices, finance, orders, ads), окно 24 ч, счёт сертификатов."
  }
  assert {
    condition     = length(regexall("< 1", google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query)) == 8 && length(regexall("\"entity\", \"ads\"", google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query)) == 3
    error_message = "Три сущности рекламы — одна группа ads (один инцидент), всего 8 сущностей."
  }
  assert {
    condition     = !strcontains(google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query, "absent_over_time")
    error_message = "Наличие нулевых точек — не свежесть: только счёт событий с vector(0)."
  }
  assert {
    condition     = google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].duration == "10800s"
    error_message = "Окно 24 ч (предел лог-метрик 25 ч) + удержание 3 ч: штатные разрывы сертификатов 23,6–24,5 ч не открывают инцидент."
  }
}

run "single_wait_or_single_tick_failure_is_not_an_incident" {
  command = plan
  assert {
    condition     = strcontains(google_monitoring_alert_policy.tick_attempt_failed.conditions[0].condition_prometheus_query_language[0].query, ">= 2") && google_monitoring_alert_policy.controller_completions.conditions[0].condition_prometheus_query_language[0].query == "(sum(increase(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"tenant-backfill-controller\",result=\"succeeded\"}[60m])) or vector(0)) < 1"
    error_message = "Нужны два отказа Scheduler подряд; «нет завершений» = 0 успешных за 60 мин, включая отсутствие ряда и ряд из нулей."
  }
  assert {
    condition     = google_monitoring_alert_policy.controller_stopped.alert_strategy[0].notification_prompts == toset(["OPENED"]) || google_monitoring_alert_policy.controller_stopped.alert_strategy[0].notification_prompts == tolist(["OPENED"])
    error_message = "Письмо о STOP — только на открытие инцидента."
  }
  assert {
    condition     = strcontains(google_monitoring_alert_policy.controller_stopped.conditions[0].condition_prometheus_query_language[0].query, "user_tenant_controller_stopped{monitored_resource=\"cloud_run_job\"}[3h])) > 0")
    error_message = "Окно STOP 3 ч перекрывает штатные разрывы строк STOPPED (до 133 мин): один STOP — один инцидент, а не письмо на каждый разрыв."
  }
}

run "every_email_subject_names_heldi" {
  command = plan
  variables { watch_image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-health-eval@sha256:0000000000000000000000000000000000000000000000000000000000000000" }
  assert {
    condition = alltrue([for p in [google_monitoring_alert_policy.controller_stopped, google_monitoring_alert_policy.tick_attempt_failed, google_monitoring_alert_policy.controller_completions, google_monitoring_alert_policy.data_freshness, google_monitoring_alert_policy.controller_crash, google_monitoring_alert_policy.controller_silence, google_monitoring_alert_policy.watch_fail[0], google_monitoring_alert_policy.watch_change[0], google_monitoring_alert_policy.watch_watchdog[0]] :
    startswith(p.documentation[0].subject, "Heldi ") && length(p.documentation[0].subject) <= 255])
    error_message = "Тема каждого письма начинается с «Heldi» (05.11 письмо STOP пришло с темой «… on __missing__» во вкладку «Оповещения»)."
  }
}

run "reject_evetis_or_foreign_project" {
  command = plan
  variables { project_id = "mpa-t-client-002" }
  expect_failures = [terraform_data.target_guard]
}

run "reject_non_tenant_project" {
  command = plan
  variables { project_id = "evetis-wb-analytics" }
  expect_failures = [var.project_id]
}

run "reject_unknown_entity" {
  command = plan
  variables { freshness_groups = { supplies = ["supplies"] } }
  expect_failures = [var.freshness_groups]
}

run "push_channel_is_absent_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_alert_policy.controller_stopped.notification_channels) == 1 && length(google_monitoring_alert_policy.delivery_test.notification_channels) == 1
    error_message = "Без owner_push_channel STOP и проверка доставки — только почта, как до 27.10."
  }
}

run "push_channel_added_only_to_stop_and_delivery_test" {
  command = plan
  variables { owner_push_channel = "projects/mpa-t-client-001/notificationChannels/123456789" }
  assert {
    condition     = contains(google_monitoring_alert_policy.controller_stopped.notification_channels, "projects/mpa-t-client-001/notificationChannels/123456789") && length(google_monitoring_alert_policy.controller_stopped.notification_channels) == 2 && contains(google_monitoring_alert_policy.delivery_test.notification_channels, "projects/mpa-t-client-001/notificationChannels/123456789")
    error_message = "Push добавляется к STOP и к проверке доставки, почта остаётся."
  }
  assert {
    condition     = alltrue([for p in [google_monitoring_alert_policy.tick_attempt_failed, google_monitoring_alert_policy.controller_completions, google_monitoring_alert_policy.data_freshness] : length(p.notification_channels) == 1])
    error_message = "Остальные политики не меняются."
  }
}

run "reject_push_channel_of_another_project" {
  command = plan
  variables { owner_push_channel = "projects/mpa-t-client-002/notificationChannels/123456789" }
  expect_failures = [var.owner_push_channel]
}
