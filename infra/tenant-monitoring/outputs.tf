output "policies" {
  description = "Имена политик для проверки чтением после apply."
  value = {
    controller_stopped     = google_monitoring_alert_policy.controller_stopped.name
    tick_attempt_failed    = google_monitoring_alert_policy.tick_attempt_failed.name
    controller_completions = google_monitoring_alert_policy.controller_completions.name
    data_freshness         = google_monitoring_alert_policy.data_freshness.name
    delivery_test          = google_monitoring_alert_policy.delivery_test.name
  }
}

output "freshness_enabled" {
  value = google_monitoring_alert_policy.data_freshness.enabled
}
