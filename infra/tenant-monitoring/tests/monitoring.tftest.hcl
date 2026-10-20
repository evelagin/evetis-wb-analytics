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
    condition = google_monitoring_alert_policy.data_freshness.enabled == true && alltrue([for e in ["catalog", "stocks", "prices", "finance_accrual", "ads_campaigns", "ads_expense_daily", "ads_sku_daily", "fbo_postings"] :
    strcontains(google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query, "label_replace((sum(increase(logging_googleapis_com:user_tenant_current_certified{monitored_resource=\"cloud_run_job\",entity=\"${e}\"}[24h])) or vector(0)), \"entity\", \"${e}\", \"\", \"\") < 1")])
    error_message = "Все 8 сущностей суточного контура, окно 24 ч (SLA политики), счёт сертификатов."
  }
  assert {
    condition     = !strcontains(google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].query, "absent_over_time")
    error_message = "Наличие нулевых точек — не свежесть: только счёт событий с vector(0)."
  }
  assert {
    condition     = google_monitoring_alert_policy.data_freshness.conditions[0].condition_prometheus_query_language[0].duration == "1800s"
    error_message = "Окно 24 ч + повтор 30 мин укладываются в 25 ч для лог-метрик."
  }
}

run "single_wait_or_single_tick_failure_is_not_an_incident" {
  command = plan
  assert {
    condition     = strcontains(google_monitoring_alert_policy.tick_attempt_failed.conditions[0].condition_prometheus_query_language[0].query, ">= 2") && google_monitoring_alert_policy.controller_completions.conditions[0].condition_prometheus_query_language[0].query == "(sum(increase(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"tenant-backfill-controller\",result=\"succeeded\"}[30m])) or vector(0)) < 1"
    error_message = "Нужны два отказа Scheduler подряд; «нет завершений» = 0 успешных за 30 мин, включая отсутствие ряда и ряд из нулей."
  }
  assert {
    condition     = google_monitoring_alert_policy.controller_stopped.alert_strategy[0].notification_prompts == toset(["OPENED"]) || google_monitoring_alert_policy.controller_stopped.alert_strategy[0].notification_prompts == tolist(["OPENED"])
    error_message = "Письмо о STOP — только на открытие инцидента."
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
  variables { expected_entities = ["supplies"] }
  expect_failures = [var.expected_entities]
}
