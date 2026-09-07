# ============================================================================
# PR-1 — Наблюдатель цен WB (READ-ONLY).
#
# Тот же образ, что stocks/mart: диспетчеризация по args[0] в cli.ts.
# Отдельный Job нужен из-за каденса: 20 минут против суточного у остальных.
#
# 🔴 БЕЗОПАСНОСТЬ. Токен категории «Цены и скидки» у WB даёт И чтение, И запись,
# если в JWT не выставлен бит 30 (read-only). Наблюдатель мутирующих методов не
# содержит вовсе, но секрет в runtime — это возможность, а не только намерение.
# Поэтому:
#   - scheduler создаётся paused и снимается с паузы ТОЛЬКО после подтверждения,
#     что в секрете лежит токен с битом 30;
#   - secretAccessor выдан ровно на один секрет и ровно одному SA (не на проект);
#   - shadow-окружению доступ к ценовому секрету НЕ выдаётся: наблюдателю цен
#     теневой контур не нужен, а лишний носитель права записи — лишний риск.
# ============================================================================

locals {
  # LOADER_NAME обязан быть 'prices': это ключ LOADER_RUNS и имя в логах.
  # common_env выставляет 'wb-stocks' — переопределяем, иначе прогоны наблюдателя
  # смешались бы с остатками в одном логическом ключе.
  prices_env = {
    LOADER_NAME               = "prices"
    WB_PRICES_HOST            = "https://discounts-prices-api.wildberries.ru"
    WB_PRICES_SECRET          = "WB_PRICES_READ_TOKEN"
    PRICES_RAW_TABLE          = "RAW_WB_PRICES"
    PRICES_OBSERVATIONS_TABLE = "WB_PRICES_OBSERVATIONS"
    REF_SKU_TABLE             = "REF_SKU_MASTER"
  }
}

# Секрет создан владельцем вручную; Terraform им НЕ владеет, а только описывает
# право доступа. Ссылка по строковому id, а не по ресурсу, — намеренно: попытка
# завести здесь google_secret_manager_secret потребовала бы import и дала бы
# Terraform право удалить секрет, чего у него быть не должно.
resource "google_secret_manager_secret_iam_member" "prices_secret_access_prod" {
  project   = var.project_id
  secret_id = "WB_PRICES_READ_TOKEN"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_cloud_run_v2_job" "wb_prices_prod" {
  name                = "wb-prices-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # max_retries=0: ретрай Cloud Run происходит в ТОМ ЖЕ execution, то есть с тем
      # же run_id и тем же 20-минутным окном. Пользы от него нет — HTTP-повторы 429/5xx
      # уже сделаны внутри wbHttp, — а вторая строка LOADER_RUNS с тем же run_id
      # засоряет манифест. Настоящий ретрай наблюдателя — следующее окно через 20 минут:
      # цена одного пропущенного окна равна одному наблюдению.
      max_retries = 0
      # Снимок 25 SKU укладывается в один запрос; 300 с — запас на ретраи 429.
      timeout = "300s"
      containers {
        image = var.container_image
        args  = ["prices"]
        dynamic "env" {
          for_each = merge(local.common_env, local.prices_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  # Образ и env продвигает deploy-prod.yml (реальный digest, GIT_SHA/IMAGE_DIGEST) —
  # ровно как у wb-stocks-prod и wb-mart-prod. Без ignore Terraform откатил бы образ
  # на bootstrap hello, который молча выходит с 0 и ничего не наблюдает.
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

# Каденс 20 минут. Обоснование частоты — docs/pricing/PR1_WB_PRICE_OBSERVER.md.
# Имя формата "<loader>-<environment>" обязательно: scheduler-control.yml собирает
# имя именно так.
resource "google_cloud_scheduler_job" "wb_prices_prod" {
  name      = "wb-prices-prod"
  region    = var.region
  schedule  = "*/20 * * * *"
  time_zone = "Etc/UTC" # окно наблюдения считается в UTC — держим планировщик в той же шкале
  paused    = true      # снимается ТОЛЬКО после подтверждения read-only токена

  retry_config {
    # Ретрай планировщика не нужен: следующее окно наступит через 20 минут и
    # запишет собственное наблюдение. Повтор внутри окна лишь продублировал бы
    # попытку, которую guard всё равно подавит.
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_prices_prod.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

# ── Права на таблицы наблюдателя ────────────────────────────────────────────
# Таблицы созданы DDL-скриптом (sql/pricing/pr1_wb_price_observer.sql), Terraform
# ими не владеет — здесь только права, по той же схеме, что prod_write_runs.
# Уровень таблицы, а не датасета: наблюдателю нужны ровно две таблицы, и выдавать
# ему запись на весь wb_raw (где живут финансы, продажи и остатки) не за что.
#
# dataEditor, а не dataViewer: загрузчик делает append в RAW и MERGE/UPDATE
# строки манифеста. Чтение REF_SKU_MASTER и запись LOADER_RUNS уже выданы
# в bigquery.tf (prod_read_ref / prod_write_runs) — здесь не дублируются.
resource "google_bigquery_table_iam_member" "prod_write_prices_raw" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_PRICES"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_prices_observations" {
  dataset_id = var.raw_dataset
  table_id   = "WB_PRICES_OBSERVATIONS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Право Scheduler'а запустить именно этот Job ─────────────────────────────
# Invoker выдаётся ПОРЕСУРСНО, а не на проект (та же причина, что у
# scheduler_mart_prod_invoke в iam.tf). Без этой строки Scheduler отрабатывает
# по расписанию, но получает PERMISSION_DENIED и НЕ создаёт execution: в самом
# Scheduler'е видно только `status.code = 7`, а в Cloud Run — тишина, неотличимая
# от «наблюдений не было». Проверено: первое автономное срабатывание 2026-09-07
# 10:40:00 UTC упало именно так, пока этой строки не было.
resource "google_cloud_run_v2_job_iam_member" "scheduler_prices_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_prices_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}
