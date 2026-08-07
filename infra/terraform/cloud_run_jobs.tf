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
  # PR-Mig1: env загрузчика остатков (shadow пишет ТОЛЬКО в __CR).
  stocks_env = {
    STOCKS_RAW_TABLE      = "RAW_WB_STOCKS__CR"
    STOCKS_SNAPSHOT_TABLE = "WB_STOCKS_SNAPSHOTS__CR"
    REF_SKU_TABLE         = "REF_SKU_MASTER"
    WB_ANALYTICS_SECRET   = "WB_TOKEN_ANALYTICS"
  }
  # PR-Mart3b-2: env оркестратора витрины. mart НЕ ходит в WB API/секрет и не пишет __CR;
  # LOADER_NAME=mart переопределяет stocks-дефолт (ключ LOADER_RUNS/логи должны быть 'mart').
  mart_env = {
    LOADER_NAME = "mart"
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
        args  = ["stocks"]
        dynamic "env" {
          for_each = merge(local.common_env, local.stocks_env, { ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # Образ управляется deploy-shadow.yml (реальный digest), Terraform его НЕ трогает
  # → нет drift между Terraform (bootstrap hello) и собранным образом.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
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

# PR-Mart3b-2: оркестратор витрины. Тот же образ, что stocks (dispatch по args[0]="mart" в cli.ts);
# ENVIRONMENT=prod (mart prodOnly — публикует production wb_mart, shadow-датасета нет).
# Планировщик НЕ создаётся здесь (PR-Mart3b-3); до тех пор Job запускается вручную/для валидации.
# Образ продвигает деплой реальным digest → ignore_changes на image (как у wb_stocks_shadow),
# иначе Terraform сбросил бы digest обратно на bootstrap hello.
resource "google_cloud_run_v2_job" "wb_mart_prod" {
  name                = "wb-mart-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # max_retries=0 (аудит REV2, блокер #2): task-retry Cloud Run повторяет task в ТОМ ЖЕ
      # execution → тот же CLOUD_RUN_EXECUTION → тот же run_id. Повтор после ERROR вставил бы вторую
      # строку LOADER_RUNS с тем же run_id и словил бы MART_RUNS_CONFLICT. Повторы витрины идут ТОЛЬКО
      # новым execution в следующем окне Scheduler (09/10/11 МСК) — с новым run_id.
      max_retries = 0
      timeout     = "1800s"
      containers {
        image = var.container_image
        args  = ["mart"]
        dynamic "env" {
          for_each = merge(local.common_env, local.mart_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }
  depends_on = [google_project_service.enabled]
}
