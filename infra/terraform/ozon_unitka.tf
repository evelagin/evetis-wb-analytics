# ══════════════════════════════════════════════════════════════════════════════
# OZON UNITKA — суточный писатель листа OZON_Юнит_2025 (Gate 9).
#
# Отдельный Job, а не ветка внутри unitka-engine-prod: домены Ozon и WB разделены жёстко.
# У них разные источники, разные окна и разные сроки прихода денег; общая у них только книга.
#
# ТРИ ОКНА, и их нельзя путать (Gate 9 §6):
#   загрузка начислений   30 суток — pipelines/ozon/runtime/entities.py, REGISTRY
#   перезапись Юнитки     45 суток — cloud/src/loaders/unitka/ozon/window.ts
#   глубокая сверка      120 суток — там же, включается первого числа месяца
# Здесь не задаётся ни одно из них: окна живут в коде, который их обосновывает.
#
# Расписание 10:00 МСК. Вся суточная загрузка Ozon завершается к 06:42 МСК
# (ozon-daily 06:30), отправления и остатки — к 07:01 (ozon-fast 07:00). Запас около трёх
# часов. Временного зазора мало, поэтому прогон дополнительно проверяет свежесть источников
# по журналу OZON_INGESTION_RUNS и при отставании ОТКАЗЫВАЕТСЯ писать (SOURCE_STALE).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  ozon_unitka_env = {
    LOADER_NAME            = "ozon-unitka"
    UNITKA_SPREADSHEET_ID  = "1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg"
    OZON_UNITKA_SHEET_NAME = "OZON_Юнит_2025"
    # Геометрия принятого листа. Правее панели владельца движок не пишет никогда.
    # GATE 10: четыре константы ниже НЕ авторитетны — хвост, слоты, зеркало LCD и префикс правил УФ
    # писатель выводит из листа и лишь предупреждает о расхождении. Оставлены как перекрёстная проверка.
    OZON_UNITKA_TAIL_FIRST_COLUMN = "562"
    OZON_UNITKA_BLOCK_SLOTS       = "22"
    OZON_UNITKA_LCD_REF           = "$VA$2"
    OZON_UNITKA_EXISTING_CF_RULES = "434"
    # GATE 10: собственный LCD Ozon (именованный диапазон → ZZ_CONFIG!B30). Писатель сам коммитит
    # его после записи, перечитывания и проверки публикации; B2 — LCD WB, Ozon его больше не читает.
    # ⚠ env этого Job в lifecycle.ignore_changes: значение применяется `gcloud run jobs update
    # --update-env-vars`, файл фиксирует его для пересоздания Job'а. Откат — "ZZ_CONFIG!B2" (LEGACY).
    OZON_UNITKA_LCD_CELL = "OZON_LAST_CLOSED_DATE"
    # Загрузка суточная: отставание больше суток — это упавший или пропущенный прогон.
    OZON_UNITKA_MAX_SOURCE_LAG_DAYS = "1"
    # Идентичность SKU берётся из evetis_ref.REF_SKU_CHANNEL_MAP — списка offer_id здесь НЕТ
    # и быть не должно: он устарел бы при первом же новом товаре. Единственная known-опечатка
    # подписи листа объявлена явно (решение владельца D-5E-3), а не угадывается движком.
    OZON_UNITKA_OFFER_ALIASES = "{\"9099514444\":\"909951444\"}"
  }
}

resource "google_cloud_run_v2_job" "ozon_unitka_prod" {
  name                = "ozon-unitka-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # Ретрай Cloud Run шёл бы в том же execution с тем же run_id. Настоящий повтор —
      # следующее окно Scheduler'а: план идемпотентен (доказано двумя прогонами подряд).
      max_retries = 0
      # Полный прогон окна 45 суток занимает около 50 с; глубокая сверка 120 суток — больше.
      timeout = "1800s"
      containers {
        image = var.container_image
        args  = ["ozon-unitka"]
        dynamic "env" {
          for_each = merge(local.common_env, local.ozon_unitka_env, local.deploy_managed_env,
          { ENVIRONMENT = "prod", OZON_UNITKA_WRITE_ENABLED = "1" })
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

# ── Право запуска: ПОРЕСУРСНО, тем же способом, что у unitka-engine-prod. ─────
# 🔴 Без этой привязки Scheduler получает PERMISSION_DENIED на :run, execution не создаётся,
# и падение НЕ ВИДНО нигде: алерт ниже слушает логи Job'а, а логов нет — прогона не было.
# Именно так 23.09.2026 пропали данные за 22.09: расписание сработало в 10:00 МСК,
# status.code=7, retry_count=0, следующая попытка — только через сутки.
# Права уровня проекта sa-scheduler-prod НЕ выдаются: он работает только поресурсно.
resource "google_cloud_run_v2_job_iam_member" "scheduler_ozon_unitka_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.ozon_unitka_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Расписание ────────────────────────────────────────────────────────────────
# Создаётся НА ПАУЗЕ: владелец снимает её после первого удачного ручного прогона.
# Так же устроены расписания unitka-engine (scheduler-control.yml).
resource "google_cloud_scheduler_job" "ozon_unitka_prod" {
  name      = "ozon-unitka-prod"
  region    = var.region
  schedule  = "0 10 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  # attempt_deadline относится к ОТВЕТУ на :run, а не к прогону: Run Admin API создаёт
  # execution и отвечает сразу. Длинный дедлайн оставлен намеренно — он делает
  # неоднозначный исход «запрос принят, ответ потерян» практически недостижимым.
  attempt_deadline = "1800s"

  # Ограниченный повтор ВЫЗОВА. Прежняя редакция ставила retry_count = 0, опасаясь
  # наложения на живой execution. Это опасение снято замером: повтор происходит только
  # тогда, когда попытка ВЕРНУЛА не-2xx, то есть execution создан НЕ был, а если он всё же
  # был создан, второй прогон упирается в распределённый guard (cli.ts: ALREADY_RUNNING /
  # OK_NO_NEW → выход 0, лист не трогается). Логический период guard'а — ЧАС по МСК
  # (unitkaSlot), поэтому все повторы обязаны уложиться в тот же час: 60 + 120 + 240 с
  # < 10 мин от окна 10:00 МСК. Увеличивать эти числа нельзя, не пересмотрев guard.
  #
  # Что повтор ЛЕЧИТ: 429/503 Run API, сетевой сбой, недоступность токена.
  # Чего НЕ лечит: 403/404 — это дефект конфигурации, его ловит алерт в scheduler.tf.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "60s"
    max_backoff_duration = "300s"
    max_doublings        = 2
    max_retry_duration   = "600s"
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.ozon_unitka_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Чтение Ozon-слоя. ТОЛЬКО чтение: писатель Юнитки в BigQuery не пишет. ──────
resource "google_bigquery_dataset_iam_member" "ozon_unitka_read_mart" {
  dataset_id = "ozon_mart"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_dataset_iam_member" "ozon_unitka_read_raw" {
  dataset_id = "ozon_raw"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# Справочник каналов и себестоимости. Без него прогон падает на ПЕРВОМ же запросе:
# идентичность SKU берётся из REF_SKU_CHANNEL_MAP (а не из списка offer_id в env), и та же
# связка нужна вью операционного слоя — V_PRODUCT_COGS_EFFECTIVE и REF_PRODUCT_MASTER
# читаются правами вызывающего. Тот же грант тем же способом уже выдан ct_refresh.tf и
# executive_v2_layer.tf: evetis_ref — общий справочный слой обоих маркетплейсов.
resource "google_bigquery_dataset_iam_member" "ozon_unitka_read_ref" {
  dataset_id = "evetis_ref"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Наблюдаемость ─────────────────────────────────────────────────────────────
# Владелец не должен каждый день заглядывать в логи. Прогон падает с кодом
# (SOURCE_STALE / NO_FREE_SKU_SLOT / OZON_UNITKA_GEOMETRY / OZON_UNITKA_REF / SHEETS_API),
# код попадает в метку алерта. Канал тот же, что у WB Engine: второй заводить незачем.
#
# 🔴 ФИЛЬТР НЕ ПО `message`. Событие пишется как logger.error('loader_failed', {code, message}),
# а logging.ts:24-30 раскрывает ctx ПОСЛЕДНИМ — поэтому ctx.message (текст ошибки) ЗАТИРАЕТ
# имя события, и `jsonPayload.message="loader_failed"` не совпадает НИКОГДА. Проверено на
# живых логах: за 90 суток у `loader_failed` нуль совпадений, тогда как реальных падений
# двадцать (ozon-unitka-prod 22.09 LOADER_ERROR, wb-mart-prod FRESHNESS_GATE и другие).
# Признак падения — severity ERROR вместе с кодом LoaderError; по нему и ловим.
resource "google_monitoring_alert_policy" "ozon_unitka_failed" {
  count        = var.unitka_alert_email == "" ? 0 : 1
  display_name = "OZON Unitka: прогон завершился ошибкой"
  combiner     = "OR"

  conditions {
    display_name = "отказ прогона ozon-unitka-prod (код приложения или системное событие)"
    condition_matched_log {
      # Два сигнала в ОДНОМ фильтре (log-match политика допускает одно условие):
      #  1) запись приложения с кодом (loader_failed / fatal — код есть всегда, cloud/src/failure.ts);
      #  2) системное событие Cloud Run «execution has failed to complete» — ловит отказ, даже если
      #     процесс не успел написать ни строки (OOM, таймаут, падение до логгера). Инцидент
      #     2026-10-06 zbfk7: fatal без code, алерт молчал, а событие (2) было.
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="ozon-unitka-prod"
        severity>=ERROR
        (jsonPayload.code!="" OR (logName:"cloudaudit.googleapis.com%2Fsystem_event" AND protoPayload.methodName="/Jobs.RunJob"))
      EOT
      label_extractors = {
        error_code = "EXTRACT(jsonPayload.code)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  notification_channels = [google_monitoring_notification_channel.unitka_email[0].id]
  documentation {
    content   = "Ozon-Юнитка не обновилась. Код ошибки — в метке error_code. SOURCE_STALE: не обновился источник Ozon, лист НЕ перезаписан устаревшими данными — это штатная защита, повтор в следующее окно. NO_FREE_SKU_SLOT: появился 23-й SKU при 22 размеченных слотах, нужна миграция геометрии листа. Эксплуатация: docs/ops/OZON_UNITKA_OPERATIONS.md"
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.enabled]
}
