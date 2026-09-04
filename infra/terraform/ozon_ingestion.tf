# ═══════════════════════════════════════════════════════════════════════
# Домен Ozon: регулярная загрузка. Введён инцидентом P0 от 2026-09-04.
#
# 🔴 ЧТО СЛОМАЛОСЬ И ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ
#
# До 2026-09-04 весь стек Ozon жил ТОЛЬКО в ручном состоянии GCP: ни job,
# ни планировщиков, ни service accounts в Terraform не было. Три
# планировщика вызывали один общий job `ozon-runtime-ingest`, передавая
# `overrides.containerOverrides` с переменной ENTITIES через v1-эндпоинт
# Admin API.
#
# Запуск с overrides требует права `run.jobs.runWithOverrides`.
# В `roles/run.invoker` входят только `run.instances.invoke`,
# `run.jobs.run` и `run.routes.invoke` — нужного права там НЕТ.
# Результат: 4 попытки из 4 → PERMISSION_DENIED, 0 успешных запусков,
# загрузка Ozon стояла с 2026-09-03.
#
# Решение — наименьшая привилегия, а не более широкая роль:
# набор сущностей запечён в КОНФИГУРАЦИЮ каждого job, планировщик зовёт
# v2-эндпоинт `:run` БЕЗ тела запроса. Тогда `roles/run.invoker`
# достаточно, и `roles/run.developer` выдавать не нужно.
#
# ⚠️ Менять этот файл на схему с overrides запрещено: это ровно тот
# отказ, который здесь закрыт.
#
# СОСТОЯНИЕ: девять ресурсов ниже ИМПОРТИРОВАНЫ в state 2026-09-04.
# Значения приведены к фактическому продакшену, а не наоборот: продакшен
# работает и является эталоном. Остаточный дрейф плана — только
# computed-поля провайдера `client` / `client_version`, которые gcloud
# проставляет при создании и которые в HCL выразить нечем (тот же дрейф
# показывает давно управляемый wb_stocks_shadow).
#
# ⚠️ ДРЕЙФ, КОТОРЫЙ ЭТОТ ФАЙЛ НЕ ЗАКРЫВАЕТ.
# Service accounts sa-ozon-ingestion / sa-ozon-scheduler, датасеты
# ozon_raw и ozon_mart, секреты EVETIS_OZON_* и job ozon-runtime-ingest
# остаются вне IaC — существовавший ранее дрейф, а не следствие инцидента.
# ═══════════════════════════════════════════════════════════════════════

locals {
  # Один образ и одна identity на все три расписания: разный только ENTITIES.
  ozon_runtime_image = "europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/ozon-runtime-ingest@sha256:a7ce446a661e612bab7f756f103ec8bcf41688429c1da9361096fae108639409"
  ozon_ingestion_sa  = "sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
  ozon_scheduler_sa  = "sa-ozon-scheduler@${var.project_id}.iam.gserviceaccount.com"

  ozon_common_env = {
    GCP_PROJECT_ID = var.project_id
    BQ_RAW_DATASET = "ozon_raw"
    BQ_LOCATION    = var.bq_location
  }

  # ⚠️ Наборы сущностей перенесены ОДИН В ОДИН из прежних scheduler
  # overrides. Перемещение сущности между каденциями меняет lookback
  # и молча ломает полноту данных — сверять с
  # docs/ozon/OZON_INCREMENTAL_CONTRACT_V1.md перед любой правкой.
  ozon_jobs = {
    "ozon-runtime-fast" = {
      entities = "stocks,fbo_postings"
      schedule = "0 7,13,19 * * *"
    }
    "ozon-runtime-daily" = {
      entities = "catalog,prices,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies"
      schedule = "30 6 * * *"
    }
    "ozon-runtime-weekly" = {
      entities = "clusters"
      schedule = "0 5 * * 1"
    }
  }

  ozon_scheduler_names = {
    "ozon-runtime-fast"   = "ozon-fast"
    "ozon-runtime-daily"  = "ozon-daily"
    "ozon-runtime-weekly" = "ozon-weekly"
  }
}

# ── Выделенные Cloud Run Jobs со статическим ENTITIES ──
resource "google_cloud_run_v2_job" "ozon_runtime" {
  for_each = local.ozon_jobs

  name     = each.key
  location = var.region

  # ⚠️ Совпадает с продакшеном (создан gcloud с защитой по умолчанию).
  # У WB-загрузчиков стоит false, но здесь продакшен — эталон, и ослаблять
  # защиту ради единообразия стиля нельзя. Следствие: удаление job
  # потребует сначала снять защиту — учтено в инструкции отката.
  deletion_protection = true

  template {
    # Метки живут на уровне template, как их положил `gcloud run jobs create
    # --labels`. Переносить их на уровень job означало бы менять продакшен
    # ради вкуса Terraform, а не описывать проверенное состояние.
    labels = {
      domain = "ozon"
      stage  = "p0-scheduler-fix"
    }

    template {
      service_account = local.ozon_ingestion_sa
      # max_retries=0 — как у исходного ozon-runtime-ingest. Повтор задачи
      # внутри того же execution писал бы вторую строку OZON_INGESTION_RUNS
      # с тем же ingestion_run_id. Повторы идут следующим окном расписания.
      max_retries = 0
      timeout     = "3600s"
      containers {
        image = local.ozon_runtime_image
        dynamic "env" {
          for_each = merge(local.ozon_common_env, { ENTITIES = each.value.entities })
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = {
            cpu    = "1000m"
            memory = "2Gi"
          }
        }
      }
    }
  }

  # Образ продвигается отдельно от Terraform (как у WB-загрузчиков),
  # иначе plan откатывал бы digest на зафиксированный здесь.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }
  depends_on = [google_project_service.enabled]
}

# ── Планировщики: v2 :run, БЕЗ тела запроса ──
# Тело отсутствует намеренно. Любое `overrides`/`containerOverrides`
# в теле снова потребует run.jobs.runWithOverrides и вернёт инцидент.
resource "google_cloud_scheduler_job" "ozon_runtime" {
  for_each = local.ozon_jobs

  name      = local.ozon_scheduler_names[each.key]
  region    = var.region
  schedule  = each.value.schedule
  time_zone = "Europe/Moscow"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ozon_runtime[each.key].name}:run"
    oauth_token {
      service_account_email = local.ozon_scheduler_sa
    }
    # message_body не задан — тело пустое.
  }

  # Пауза/возобновление принадлежит оператору, как и у WB-планировщиков.
  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── IAM: право запустить ровно эти три job и ничего больше ──
# Пореcурсно, а не на проект: sa-ozon-scheduler не получает ни
# roles/run.developer, ни admin-ролей Cloud Run.
resource "google_cloud_run_v2_job_iam_member" "ozon_scheduler_invoke" {
  for_each = local.ozon_jobs

  location = var.region
  name     = google_cloud_run_v2_job.ozon_runtime[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.ozon_scheduler_sa}"
}
