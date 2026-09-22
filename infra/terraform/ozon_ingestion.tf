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
  #
  # ⚠️ Это поле под `ignore_changes` (см. lifecycle ниже): Terraform его не
  # применяет, образ продвигается отдельно, как у WB-загрузчиков. Значение
  # держим в актуальном состоянии только ради читаемости — иначе файл начнёт
  # врать о том, что крутится в проде.
  #
  # 2026-09-06, Stage 3.4D.2: sha256:a7ce446a… → sha256:43fb3a10…
  # Причина: prices() расширен полным блоком commissions и пишет вторую
  # таблицу RAW_OZON_PRICE_COMMISSIONS. Прежний образ сохранял из тарифа
  # только sales_percent_fbo, из-за чего форвардная экономика не могла
  # обновляться сама. Откат — sql/ozon/stage3_4d2_rollback.sql, раздел про образ.
  #
  # 2026-09-06, Stage 3.4D.3: sha256:43fb3a10… → sha256:14f8a4c8…
  # Причина: добавлена сущность seller_info (статус подписки Premium).
  # Откат — sql/ozon/stage3_4d3_rollback.sql, раздел 4.
  #
  # 2026-09-22, Gate 9B: sha256:b00380d6… → sha256:dccc50ae…
  # Причина: окно загрузки finance_accrual 14 → 30 суток (Gate 9). Изменение лежало в main
  # с 21.09, но образ собирается вручную (pipelines/ozon вне деплойных workflow), и боевой
  # прогон 22.09 06:30 МСК всё ещё брал 14 суток — замер по OZON_INGESTION_RUNS.
  # Потери данных не было: перезагрузка за 113 суток вставила 0 строк.
  # Собран из main 1b651fd. Откат — вернуть предыдущий digest и применить.
  ozon_runtime_image = "europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/ozon-runtime-ingest@sha256:dccc50ae59f77020666c2dbb630dd10f4b6ca8e4bde04ba0f86e91e26c158905"
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
    # 2026-09-06, Stage 3.4D.3: добавлена seller_info. Новая сущность, а не
    # перенос существующей — окно ретроспективы 0, каденция суточная, полнота
    # других сущностей не затрагивается. Даёт статус подписки Premium, который
    # до этого проверялся вручную.
    "ozon-runtime-daily" = {
      entities = "catalog,prices,seller_info,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies"
      schedule = "30 6 * * *"
    }
    "ozon-runtime-weekly" = {
      entities = "clusters"
      schedule = "0 5 * * 1"
    }
    # PR-PROMO-1, 2026-09-22: наблюдатель акций. Отдельный job, а не сущность в
    # суточном, ровно из-за каденции: состав акции и акционные цены меняются
    # ВНУТРИ суток, а дата автодобавления наступает в 21:00 UTC (полночь МСК).
    # Суточный снимок увидел бы автодобавление уже после того, как оно сработало.
    #
    # Часы совпадают с наблюдателем акций WB (PROMO_SLOT_HOURS_UTC в
    # cloud/src/loaders/promo/slot.ts и PROMO_SLOT_HOURS_UTC в
    # pipelines/ozon/runtime/common.py): снимки двух площадок должны сравниваться
    # без поправки на время. Планировщики Ozon объявлены в Europe/Moscow, поэтому
    # 04/09/14/19 UTC записаны здесь как 07/12/17/22 МСК — это те же моменты.
    "ozon-runtime-promo" = {
      entities = "promo"
      schedule = "0 7,12,17,22 * * *"
    }
  }

  ozon_scheduler_names = {
    "ozon-runtime-fast"   = "ozon-fast"
    "ozon-runtime-daily"  = "ozon-daily"
    "ozon-runtime-weekly" = "ozon-weekly"
    "ozon-runtime-promo"  = "ozon-promo"
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
  # 🔴 Stage Drift Reconciliation (06.09.2026). `client` / `client_version` в схеме
  # провайдера объявлены как `optional` (НЕ computed) с описанием "Arbitrary
  # identifier/version for the API client" — проверено `terraform providers schema`.
  # Это отметка о том, КАКИМ ИНСТРУМЕНТОМ последний раз писали ресурс, а не
  # конфигурация job'а: gcloud проставляет "gcloud"/"<версия>", Terraform не
  # проставляет ничего, поэтому план вечно предлагает занулить их. На поведение
  # Cloud Run это не влияет.
  # Владеть ими Terraform не может осмысленно: любое значение, которое он записал бы,
  # было бы неправдой (Terraform — не gcloud), а зануление стирает происхождение
  # ресурса без всякой выгоды. Хардкодить версии (568.0.0 / 577.0.0) ради тишины
  # в плане запрещено — это подгонка под вывод, а не desired state.
  # Поэтому — минимальная нормализация: не реконсилировать эти два поля.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
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
