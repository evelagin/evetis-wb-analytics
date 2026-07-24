# Cloud Scheduler запускает Job через Run Admin API (v2 :run). Управляется ТОЛЬКО
# здесь (infra); включение/выключение при cutover — через scheduler-control.yml.
# Foundation: оба PAUSED (никаких авто-запусков, пока идёт фундамент).
locals {
  run_v2_base = "https://${var.region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs"
}

resource "google_cloud_scheduler_job" "wb_stocks_shadow" {
  name      = "wb-stocks-shadow"
  region    = var.region
  schedule  = "30 6 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  retry_config {
    retry_count = 1 # schedule-retry (уровень 3 из трёх)
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_stocks_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_stocks_prod" {
  name      = "wb-stocks-prod"
  region    = var.region
  schedule  = "30 6 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # prod включается ТОЛЬКО на cutover (после подписи parity)

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_stocks_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }
  depends_on = [google_project_service.enabled]
}
