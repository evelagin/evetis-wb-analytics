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
    OZON_UNITKA_TAIL_FIRST_COLUMN = "562"
    OZON_UNITKA_BLOCK_SLOTS       = "22"
    OZON_UNITKA_LCD_REF           = "$VA$2"
    OZON_UNITKA_EXISTING_CF_RULES = "434"
    # LAST_CLOSED_DATE читается ИЗ КНИГИ: владелец двигает её сам.
    OZON_UNITKA_LCD_CELL = "ZZ_CONFIG!B2"
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

# ── Расписание ────────────────────────────────────────────────────────────────
# Создаётся НА ПАУЗЕ: владелец снимает её после первого удачного ручного прогона.
# Так же устроены расписания unitka-engine (scheduler-control.yml).
resource "google_cloud_scheduler_job" "ozon_unitka_prod" {
  name      = "ozon-unitka-prod"
  region    = var.region
  schedule  = "0 10 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  # Одновременных писателей в один лист быть не должно. Повтор при сбое — следующее окно,
  # а не немедленный ретрай: он наложился бы на ещё живой execution.
  attempt_deadline = "1800s"
  retry_config {
    retry_count = 0
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
