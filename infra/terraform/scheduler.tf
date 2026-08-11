# Cloud Scheduler запускает Job через Run Admin API (v2 :run). Существование/конфиг —
# ТОЛЬКО здесь (infra). Состояние pause/resume принадлежит scheduler-control.yml,
# поэтому `paused` исключён из drift (иначе apply мог бы остановить рабочий загрузчик).
locals {
  run_v2_base = "https://${var.region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs"
}

resource "google_cloud_scheduler_job" "wb_stocks_shadow" {
  name      = "wb-stocks-shadow"
  region    = var.region
  schedule  = "30 6 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # начальное состояние; далее управляется scheduler-control.yml

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_stocks_shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# PR-Mart3b-3: ежедневный запуск витрины. Имя ДОЛЖНО быть `wb-mart-prod` — scheduler-control.yml
# собирает имя как "<loader>-<environment>", и Cloud Run Job зовётся так же (как у stocks).
#
# Окна — это НЕ несколько построений витрины, а несколько ПОПЫТОК одной:
#   COMPLETE за D-1 уже есть → acquire() возвращает COMPLETE → guard_skip/exit(0), wb_mart не трогаем;
#   прошлая попытка ERROR    → ERROR не блокирует → новый execution с новым run_id (ретрай);
#   прошлая ещё идёт         → свежий STARTED → ALREADY_RUNNING → exit(0).
#
# ⚠️ РАСПИСАНИЕ ВЫБРАНО ПО ДАННЫМ, А НЕ ПО ИНТУИЦИИ (PR-Mart3b-3, аудит REV2).
# Почасовой прогон freshness-gate (LATEST-ATTEMPT) по V_INGEST_HEARTBEAT за 7 полных суток
# 03–09.08.2026 — см. sql/mart3/pr_mart3b3_freshness_readiness.sql:
#   06:00–10:00 МСК → 7/7 зелёных   (источники готовы уже к ~05:11: ads завершается 05:07→05:11)
#   11:00 МСК       → 0/7 зелёных   ← ЕДИНСТВЕННЫЙ красный час в сутках
#   12:00–23:00 МСК → 7/7 зелёных
# Причина 11:00: hourly-loader sales стартует в 10:22 МСК и 7 раз из 7 падал с WB HTTP 429
# «Limited by global limiter, per seller» → LATEST-ATTEMPT для sales = ERROR ровно до следующей
# hourly-попытки в 11:22. То есть окно 11:00 попадает в детерминированное часовое «окно красноты».
# PR #87 (retry на 429) должен это устранить, НО на 10.08 отказ ещё воспроизводился — фикс живёт
# в main, а в Apps Script-проекте синхронизация подтверждается только новым зелёным 10:22.
# Поэтому 10:00 и 11:00 из расписания ИСКЛЮЧЕНЫ, а окна разнесены по обе стороны отказа:
#   07:00 — основное (источники готовы с ~05:11; безопасный зазор от Apps Script-окна ads 05:00–06:00);
#   09:00 — резерв до отказа sales;
#   12:00 — первый безопасный час ПОСЛЕ восстановления sales в 11:22;
#   16:00 — глубокий фолбэк на случай длительного сбоя WB.
# ⚠️ Ограничение доказательства: ads делает РОВНО ОДНУ попытку в сутки (05:07, hourly-ретраев нет).
# Если падает ads — ни одно окно этого дня витрину не спасёт; это закрывается алертами (PR-Mart3b-5),
# а не расписанием. История heartbeat начинается 02.08 (PR#82), поэтому окно наблюдения = 7 суток.
#
# retry_config.retry_count=1 повторяет ТОЛЬКО HTTP-вызов Run Admin API `:run` (вызов асинхронный:
# 200 = execution создан, исход прогона Scheduler не видит). Если ответ на успешный `:run` потерялся
# и повтор создаст второй execution — его отсечёт lease по свежему STARTED. Ретраем САМОЙ витрины
# управляют окна выше, а не Scheduler и не max_retries Job'а (там 0 — см. cloud_run_jobs.tf).
resource "google_cloud_scheduler_job" "wb_mart_prod" {
  name      = "wb-mart-prod"
  region    = var.region
  schedule  = "0 7,9,12,16 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # включается на rollout (PR-Mart3b-4) через scheduler-control.yml

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_mart_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_stocks_prod" {
  name      = "wb-stocks-prod"
  region    = var.region
  schedule  = "30 6 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # prod включается только на cutover через scheduler-control.yml

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

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
