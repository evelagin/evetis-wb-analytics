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

output "watch" {
  description = "Слой 2 наблюдения Heldi (пусто без watch_image)."
  value = {
    job              = try(google_cloud_run_v2_job.watch[0].name, null)
    scheduler_paused = try(google_cloud_scheduler_job.watch[0].paused, null)
    service_account  = try(google_service_account.watch[0].email, null)
    watchdog_enabled = try(google_monitoring_alert_policy.watch_watchdog[0].enabled, null)
    silence_enabled  = google_monitoring_alert_policy.controller_silence.enabled
    crash_policy     = google_monitoring_alert_policy.controller_crash.name
  }
}
