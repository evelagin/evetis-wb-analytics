# Ozon · загрузки роста EVETIS (2026-11-17): запросы по товарам, фразы кампаний «Поиск», статусы остатков.
#
# Отдельный job вне local.ozon_jobs: эти сущности не входят в контракт арендаторов (tools/tenancy/ozon_contract.py).
# Код — pipelines/ozon/runtime/growth.py (ветка feat/ozon-growth-loaders), вход `python main.py --growth`,
# профиль методов `growth` — только чтение Ozon. Образ продвигается вне Terraform (как у ozon_runtime):
# digest зафиксирован здесь для создания job, дальше — `gcloud run jobs update --image …@sha256:…`.
# Планировщик создаётся на ПАУЗЕ: включает оператор после первого ручного прогона и QA
# (docs/ozon/OZON_GROWTH_LOADERS_2026-11-17.md §6). Откат: пауза планировщика + sql/ozon/…/rollback.sql.

locals {
  # Cloud Build 723552fd (2026-11-17), коммит ea538683 ветки feat/ozon-growth-loaders.
  ozon_growth_image = "europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/ozon-runtime-ingest@sha256:0441a0e38073bf263b78722ef68d1ca92276edf12d5bd0b0d6b7b0f892d2e4e9"
}

resource "google_cloud_run_v2_job" "ozon_growth" {
  name                = "ozon-runtime-growth"
  location            = var.region
  deletion_protection = true

  template {
    labels = {
      domain = "ozon"
      stage  = "growth-loaders"
    }

    template {
      service_account = local.ozon_ingestion_sa
      # Повтор задачи писал бы вторую строку OZON_INGESTION_RUNS с тем же run_id — повтор = следующее окно.
      max_retries = 0
      # Первый бэкфилл ~36 суток запросов ≈ 30–40 мин.
      timeout = "3600s"
      containers {
        image = local.ozon_growth_image
        args  = ["--growth"]
        dynamic "env" {
          for_each = local.ozon_common_env
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = {
            cpu    = "1000m"
            memory = "1Gi"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
  depends_on = [google_project_service.enabled]
}

# 07:20 МСК — после ozon-runtime-daily (06:30) и fast (07:00). Тело запроса пустое (без overrides).
resource "google_cloud_scheduler_job" "ozon_growth" {
  name      = "ozon-growth"
  region    = var.region
  schedule  = "20 7 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ozon_growth.name}:run"
    oauth_token {
      service_account_email = local.ozon_scheduler_sa
    }
  }

  # Пауза/возобновление — за оператором.
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_job_iam_member" "ozon_growth_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.ozon_growth.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.ozon_scheduler_sa}"
}
