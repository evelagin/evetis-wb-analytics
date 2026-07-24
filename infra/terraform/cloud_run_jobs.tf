# Пилотные Cloud Run JOBS (не Services). Foundation запускает `noop` — доказывает
# цепочку Scheduler → Job → guard → manifest → exit БЕЗ бизнес-логики.
# PR-Mig1 меняет args на ["stocks"] и добавляет реальный загрузчик.
locals {
  common_env = {
    GCP_PROJECT_ID    = var.project_id
    BQ_LOCATION       = var.bq_location
    BQ_RAW_DATASET    = var.raw_dataset
    BQ_MANIFEST_TABLE = "LOADER_RUNS"
    LOADER_NAME       = "wb-stocks"
    LOG_LEVEL         = "info"
  }
}

resource "google_cloud_run_v2_job" "wb_stocks_shadow" {
  name                = "wb-stocks-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_shadow.email
      max_retries     = 1 # task-retry (уровень 2 из трёх). HTTP-повторы — в wbHttp.
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["noop"]
        dynamic "env" {
          for_each = merge(local.common_env, { ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_job" "wb_stocks_prod" {
  name                = "wb-stocks-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      max_retries     = 1
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["noop"]
        dynamic "env" {
          for_each = merge(local.common_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  depends_on = [google_project_service.enabled]
}
