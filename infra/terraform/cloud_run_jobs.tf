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
  # fix/terraform-prod-job-drift: provenance-переменные, которыми ВЛАДЕЕТ deploy-prod.yml
  # (`gcloud run jobs update --update-env-vars IMAGE_DIGEST=…,GIT_SHA=…`). Их читает config.ts
  # (opt(env,'GIT_SHA','unknown')) и кладёт в run_id и LOADER_RUNS — это функциональные входы,
  # а не украшение. До этого фикса Terraform о них не знал и в plan предлагал их УДАЛИТЬ.
  #
  # Terraform объявляет КЛЮЧИ (состав env остаётся под его контролем: лишний ручной env по-прежнему
  # ловится planом), но НЕ навязывает ЗНАЧЕНИЯ — см. ignore_changes у обоих prod-Job'ов. Sentinel
  # совпадает с fallback в config.ts, поэтому до первого промоушена поведение не меняется.
  # ⚠️ Только prod: deploy-shadow.yml обновляет образ, а env НЕ трогает (у shadow эти значения
  # штатно 'unknown' — подтверждено строками LOADER_RUNS).
  #
  # 🔴 ПОРЯДОК КЛЮЧЕЙ ВАЖЕН. Terraform обходит map в `for_each` в ЛЕКСИКОГРАФИЧЕСКОМ порядке,
  # поэтому у обоих prod-Job'ов список env получается такой:
  #   0 BQ_LOCATION · 1 BQ_MANIFEST_TABLE · 2 BQ_RAW_DATASET · 3 ENVIRONMENT · 4 GCP_PROJECT_ID
  #   5 GIT_SHA     · 6 IMAGE_DIGEST      · 7 LOADER_NAME    · 8 LOG_LEVEL
  # (mart_env только ПЕРЕОПРЕДЕЛЯЕТ LOADER_NAME, нового ключа не добавляет — индексы совпадают.)
  # Именно индексы 5 и 6 зашиты в ignore_changes ниже (там нельзя использовать переменные —
  # ignore_changes принимает только статические ссылки на атрибуты).
  # При добавлении env-ключа, сортирующегося РАНЬШЕ GIT_SHA, индексы сместятся и ignore_changes
  # начнёт молча игнорировать не ту переменную. Меняя env prod-Job'ов: пересчитай список выше и
  # проверь, что `terraform plan` сразу после deploy-prod пустой.
  deploy_managed_env = {
    GIT_SHA      = "unknown"
    IMAGE_DIGEST = "unknown"
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
          for_each = merge(local.common_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # fix/terraform-prod-job-drift: у shadow и mart ignore на image уже был, у stocks-prod — НЕ был,
  # хотя deploy-prod.yml продвигает сюда тот же проверенный digest. Из-за этого plan предлагал
  # откатить образ на bootstrap `us-docker.pkg.dev/cloudrun/container/hello`. Job ещё ни разу не
  # исполнялся (в LOADER_RUNS нет ни одной строки environment='prod'), поэтому сейчас это не
  # ломает работающий загрузчик — но на cutover Mig1b Scheduler запустил бы hello, который
  # молча выходит с 0 и ничего не грузит. Это ловушка, а не косметика.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env[5].value, # GIT_SHA      — владеет deploy-prod.yml
      template[0].template[0].containers[0].env[6].value, # IMAGE_DIGEST — владеет deploy-prod.yml
    ]
  }
  depends_on = [google_project_service.enabled]
}

# PR-Mart3b-2: оркестратор витрины. Тот же образ, что stocks (dispatch по args[0]="mart" в cli.ts);
# ENVIRONMENT=prod (mart prodOnly — публикует production wb_mart, shadow-датасета нет).
# Планировщик — в scheduler.tf (`wb-mart-prod`, окна 07/09/12/16 МСК, PR-Mart3b-3); создаётся paused,
# снятие паузы — отдельный шаг rollout (PR-Mart3b-4) через scheduler-control.yml.
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
      # новым execution в следующем окне Scheduler (07/09/12/16 МСК) — с новым run_id.
      max_retries = 0
      timeout     = "1800s"
      containers {
        image = var.container_image
        args  = ["mart"]
        dynamic "env" {
          for_each = merge(local.common_env, local.mart_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # fix/terraform-prod-job-drift: к ignore на image добавлены GIT_SHA/IMAGE_DIGEST. Этот Job УЖЕ
  # работает в проде (3 прогона, run_id вида `prod:mart:2026-08-09:c1f2a58…:wb-mart-prod-dfq9n`),
  # и удаление этих env обнулило бы traceability: config.ts подставил бы 'unknown', и в run_id и в
  # LOADER_RUNS/MART_RUNS вместо коммита и digest'а попало бы 'unknown'. Прогон не упал бы
  # (обе переменные — opt() с fallback), но связь «строка витрины ↔ версия кода» была бы потеряна.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env[5].value, # GIT_SHA      — владеет deploy-prod.yml
      template[0].template[0].containers[0].env[6].value, # IMAGE_DIGEST — владеет deploy-prod.yml
    ]
  }
  depends_on = [google_project_service.enabled]
}
