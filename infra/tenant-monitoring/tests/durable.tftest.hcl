# Локальные состояния A2 (durable.tf). Офлайн: mock provider.
mock_provider "google" {}

variables {
  tenant_id   = "client_001"
  project_id  = "mpa-t-client-001"
  alert_email = "owner@example.invalid"
}

run "metrics_now_policies_only_after_404" {
  command = plan
  assert {
    condition     = alltrue([for p in [google_monitoring_alert_policy.current_hold, google_monitoring_alert_policy.current_partial, google_monitoring_alert_policy.read_retry_streak, google_monitoring_alert_policy.full_leaf_stop, google_monitoring_alert_policy.full_local_systemic, google_monitoring_alert_policy.source_disappeared] : p.enabled == false])
    error_message = "До выпуска #404 политики A2 выключены (полей в логах ещё нет)."
  }
  assert {
    condition     = google_logging_metric.controller_stopped.filter == "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"tenant-backfill-controller\" AND logName=\"projects/mpa-t-client-001/logs/run.googleapis.com%2Fstdout\" AND jsonPayload.status=\"STOPPED\""
    error_message = "До #404 фильтр STOP прежний."
  }
  assert {
    condition     = length(google_logging_metric.current_entity_status.metric_descriptor[0].labels) == 8 && google_logging_metric.current_entity_status.label_extractors["finance_accrual"] == "EXTRACT(jsonPayload.current_entities.finance_accrual)"
    error_message = "Метка на каждую из 8 сущностей суточного контура (≤ 10 меток лог-метрики)."
  }
}

run "after_404_local_states_alert_and_leaf_is_not_global_stop" {
  command = plan
  variables { durable_daily_alerts = true }
  assert {
    condition     = alltrue([for p in [google_monitoring_alert_policy.current_hold, google_monitoring_alert_policy.current_partial, google_monitoring_alert_policy.read_retry_streak, google_monitoring_alert_policy.full_leaf_stop, google_monitoring_alert_policy.full_local_systemic, google_monitoring_alert_policy.source_disappeared] : p.enabled == true && startswith(p.documentation[0].subject, "Heldi ") && length(p.display_name) <= 100])
    error_message = "Шесть политик включены, тема начинается с «Heldi»."
  }
  assert {
    condition     = strcontains(google_logging_metric.controller_stopped.filter, "AND NOT jsonPayload.diagnostic.context.index:*") && strcontains(google_logging_metric.full_leaf_stop.filter, "jsonPayload.diagnostic.context.index:*")
    error_message = "Локальный STOP листа считается своей метрикой, а не глобальным STOP."
  }
  assert {
    condition     = length(regexall("UNKNOWN", google_monitoring_alert_policy.current_hold.conditions[0].condition_prometheus_query_language[0].query)) == 8 && strcontains(google_monitoring_alert_policy.current_hold.conditions[0].condition_prometheus_query_language[0].query, "stocks=\"UNKNOWN\"}[90m])), \"entity\", \"stocks\", \"\", \"\") >= 5 unless on()")
    error_message = "UNKNOWN 5 раз за 90 мин без COMPLETE/PARTIAL — инцидент на сущность."
  }
  assert {
    condition     = strcontains(google_monitoring_alert_policy.read_retry_streak.conditions[0].condition_prometheus_query_language[0].query, "status=\"WAITING_READ_RETRY\"}[90m])) >= 5) unless on()")
    error_message = "WAITING_READ_RETRY ≥ 5 за 90 мин без решений (бэктест 26.10–05.11: 0 срабатываний)."
  }
}

run "reject_noisy_thresholds" {
  command = plan
  variables { read_retry_streak = 1 }
  expect_failures = [var.read_retry_streak]
}
