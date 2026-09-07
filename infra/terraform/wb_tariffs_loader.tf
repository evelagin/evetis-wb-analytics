# ============================================================================
# PR-2 — загрузчик тарифов WB (READ-ONLY).
#
# Тот же образ и тот же секрет, что у наблюдателя цен: тарифы доступны
# токеном категории «Цены и скидки», проверено фактическим 200 на всех
# четырёх эндпоинтах. Второй секрет не заводится — это лишний носитель прав.
#
# Каденс — сутки. Ставки WB меняются реже раза в месяц; 20-минутный опрос,
# как у цен, тратил бы лимит без единицы новой информации.
# ============================================================================

locals {
  tariffs_env = {
    LOADER_NAME                = "tariffs"
    WB_TARIFFS_HOST            = "https://common-api.wildberries.ru"
    WB_PRICES_SECRET           = "WB_PRICES_READ_TOKEN"
    TARIFFS_RAW_TABLE          = "RAW_WB_TARIFFS"
    TARIFFS_OBSERVATIONS_TABLE = "WB_TARIFF_OBSERVATIONS"
  }
}

resource "google_cloud_run_v2_job" "wb_tariffs_prod" {
  name                = "wb-tariffs-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # Как и у наблюдателя цен: ретрай Cloud Run идёт в том же execution, то есть
      # с тем же run_id и теми же сутками. Настоящий ретрай — следующие сутки.
      max_retries = 0
      # Комиссия — ~1,9 МБ и 7408 предметов; 600 с с запасом на ретраи 429.
      timeout = "600s"
      containers {
        image = var.container_image
        args  = ["tariffs"]
        dynamic "env" {
          for_each = merge(local.common_env, local.tariffs_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env,
      client,
      client_version,
    ]
  }
  depends_on = [google_project_service.enabled]
}

# 🔴 Право запуска выдаётся ПОРЕСУРСНО. В PR-1 эта строка была пропущена, и первое
# автономное срабатывание Scheduler'а упало с PERMISSION_DENIED, не создав execution:
# в Cloud Run при этом тишина, неотличимая от «наблюдений не было». Не повторяем.
resource "google_cloud_run_v2_job_iam_member" "scheduler_tariffs_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_tariffs_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# Права на собственные таблицы. Уровень таблицы, не датасета: загрузчику нужны
# ровно две таблицы, и запись во весь wb_raw ему не за что выдавать.
resource "google_bigquery_table_iam_member" "prod_write_tariffs_raw" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_TARIFFS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_tariff_observations" {
  dataset_id = var.raw_dataset
  table_id   = "WB_TARIFF_OBSERVATIONS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_cloud_scheduler_job" "wb_tariffs_prod" {
  name      = "wb-tariffs-prod"
  region    = var.region
  schedule  = "15 5 * * *"
  time_zone = "Etc/UTC"
  paused    = true # снимается только после успешного ручного прогона

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_tariffs_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
