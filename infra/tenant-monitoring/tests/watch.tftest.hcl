# Облачное наблюдение Heldi (watch.tf). Офлайн: mock provider, синтетические почта и digest.
mock_provider "google" {}

variables {
  tenant_id   = "client_001"
  project_id  = "mpa-t-client-001"
  alert_email = "owner@example.invalid"
}

run "layer1_only_without_watch_image" {
  command = plan
  assert {
    condition     = length(google_cloud_run_v2_job.watch) == 0 && length(google_service_account.watch) == 0 && length(google_project_iam_member.watch_data) == 0 && length(google_monitoring_alert_policy.watch_fail) == 0
    error_message = "Без образа наблюдателя создаются только сигналы слоя 1."
  }
  assert {
    condition     = strcontains(google_logging_metric.controller_crash.filter, "logName=\"projects/mpa-t-client-001/logs/run.googleapis.com%2Fstderr\"") && strcontains(google_logging_metric.controller_crash.filter, "Traceback (most recent call last)") && !strcontains(google_logging_metric.controller_crash.filter, "exit(1)")
    error_message = "H2 — только Traceback в stderr контроллера; exit(1) бывает и у штатного WAITING_READ_RETRY."
  }
  assert {
    condition     = google_monitoring_alert_policy.controller_silence.enabled == false && google_monitoring_alert_policy.controller_crash.enabled != false
    error_message = "H3 включается вторым apply, H2 — сразу."
  }
  assert {
    condition     = google_monitoring_alert_policy.controller_silence.conditions[0].condition_prometheus_query_language[0].query == "(sum(increase(logging_googleapis_com:user_heldi_controller_status{monitored_resource=\"cloud_run_job\"}[20m])) or vector(0)) < 1"
    error_message = "H3: 0 строк статуса за 20 мин, отсутствие ряда — тоже тишина."
  }
}

run "watch_staged_paused_and_read_only" {
  command = plan
  variables {
    watch_image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-health-eval@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }
  assert {
    condition     = google_cloud_scheduler_job.watch[0].paused == true && google_monitoring_alert_policy.watch_watchdog[0].enabled == false
    error_message = "Первый apply: Scheduler на паузе, сторож выключен (доверенный ручной прогон до регулярной оценки)."
  }
  assert {
    condition     = google_cloud_run_v2_job.watch[0].name == "heldi-health-eval" && google_cloud_run_v2_job.watch[0].template[0].template[0].max_retries == 0 && google_cloud_run_v2_job.watch[0].template[0].template[0].containers[0].command == tolist(["python", "-m", "tools.heldi_watch.health"])
    error_message = "Задание наблюдателя: без повторов, точка входа модуля наблюдателя."
  }
  assert {
    condition     = google_cloud_scheduler_job.watch[0].http_target[0].uri == "https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/heldi-health-eval:run"
    error_message = "Scheduler будит только задание наблюдателя."
  }
  assert {
    condition     = toset(google_project_iam_custom_role.watch_read[0].permissions) == toset(["bigquery.jobs.create", "cloudscheduler.jobs.list", "logging.logEntries.list", "run.executions.get", "run.executions.list", "run.jobs.get"]) && toset(google_project_iam_custom_role.watch_data[0].permissions) == toset(["bigquery.datasets.get", "bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.list"]) && toset(google_project_iam_custom_role.watch_wake[0].permissions) == toset(["run.jobs.run"])
    error_message = "Права наблюдателя только на чтение; run.jobs.run — только у SA расписания."
  }
  assert {
    condition     = alltrue([for d in ["ozon_raw", "ref", "tenant_ops", "tenant_locks"] : strcontains(google_project_iam_member.watch_data[0].condition[0].expression, "resource.name == \"projects/mpa-t-client-001/datasets/${d}\"")]) && !strcontains(google_project_iam_member.watch_data[0].condition[0].expression, "analytics_share") && !strcontains(google_project_iam_member.watch_data[0].condition[0].expression, "ozon_mart")
    error_message = "Чтение данных — ровно четыре датасета читателя контроллера."
  }
  assert {
    condition     = google_project_iam_member.watch_read[0].role == "projects/mpa-t-client-001/roles/heldiWatchRead" && google_project_iam_member.watch_data[0].role == "projects/mpa-t-client-001/roles/heldiWatchData" && google_cloud_run_v2_job_iam_member.watch_wake[0].role == "projects/mpa-t-client-001/roles/heldiWatchWake"
    error_message = "Только пользовательские роли: никакого roles/bigquery.jobUser."
  }
}

run "watch_active_enables_schedule_and_watchdog" {
  command = plan
  variables {
    watch_image  = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-health-eval@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    watch_active = true
  }
  assert {
    condition     = google_cloud_scheduler_job.watch[0].paused == false && google_monitoring_alert_policy.watch_watchdog[0].enabled == true && google_cloud_scheduler_job.watch[0].schedule == "*/30 * * * *"
    error_message = "Второй apply: оценка каждые 30 мин и сторож пульса."
  }
}

run "reject_mutable_watch_image" {
  command = plan
  variables {
    watch_image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-health-eval:latest"
  }
  expect_failures = [var.watch_image]
}

run "reject_other_region" {
  command = plan
  variables { region = "europe-west4" }
  expect_failures = [var.region]
}

run "push_goes_to_crash_fail_and_watchdog_only" {
  command = plan
  variables {
    watch_image        = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-health-eval@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    owner_push_channel = "projects/mpa-t-client-001/notificationChannels/123456789"
  }
  assert {
    condition     = alltrue([for p in [google_monitoring_alert_policy.controller_crash, google_monitoring_alert_policy.watch_fail[0], google_monitoring_alert_policy.watch_watchdog[0]] : contains(p.notification_channels, "projects/mpa-t-client-001/notificationChannels/123456789") && length(p.notification_channels) == 2])
    error_message = "Аварии — почта + push."
  }
  assert {
    condition     = length(google_monitoring_alert_policy.controller_silence.notification_channels) == 1 && length(google_monitoring_alert_policy.watch_change[0].notification_channels) == 1
    error_message = "Тишина и смена состояния — только почта (тишина открывается и на плановой паузе)."
  }
}
