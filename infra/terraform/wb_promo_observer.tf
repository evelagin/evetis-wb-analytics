# ============================================================================
# PR-PROMO-1 — Наблюдатель акций WB (READ-ONLY).
#
# Тот же образ, что stocks/prices/mart: диспетчеризация по args[0] в cli.ts.
# Отдельный Job нужен из-за каденса: 4 раза в сутки против 20 минут у цен и
# суток у остальных.
#
# 🔴 БЕЗОПАСНОСТЬ. Наблюдатель акций использует ТОТ ЖЕ секрет, что наблюдатель
# цен, — токен категории «Цены и скидки» (календарь акций живёт под тем же
# правом). Второй носитель права записи заводить незачем: чем меньше копий
# токена в runtime, тем меньше поверхность.
#
# Токен WB даёт И чтение, И запись, если в JWT не выставлен бит 30 (read-only).
# У EVETIS бит выставлен — проверено 2026-09-22, срок действия до 2027-03-08.
# Тем не менее:
#   - scheduler создаётся paused и снимается с паузы ТОЛЬКО после подтверждения,
#     что в секрете лежит токен с битом 30;
#   - мутирующих путей в загрузчике нет физически, и это проверяется тестом по
#     исходному тексту (cloud/test/promo_security.test.ts);
#   - shadow-окружению доступ к секрету НЕ выдаётся: наблюдателю акций теневой
#     контур не нужен, а лишний носитель права записи — лишний риск.
#
# Права на секрет НЕ выдаются заново: google_secret_manager_secret_iam_member
# для WB_PRICES_READ_TOKEN и того же SA уже объявлен в wb_prices_observer.tf.
# Второй ресурс на ту же пару (secret, member) дал бы конфликт состояния.
# ============================================================================

locals {
  # LOADER_NAME обязан быть 'promo': это ключ LOADER_RUNS и имя в логах.
  # common_env выставляет 'wb-stocks' — переопределяем, иначе прогоны наблюдателя
  # акций смешались бы с остатками в одном логическом ключе.
  promo_env = {
    LOADER_NAME              = "promo"
    WB_PROMO_HOST            = "https://dp-calendar-api.wildberries.ru"
    WB_PROMO_SECRET          = "WB_PRICES_READ_TOKEN"
    PROMO_CALENDAR_TABLE     = "RAW_WB_PROMO_CALENDAR"
    PROMO_RANGING_TABLE      = "RAW_WB_PROMO_RANGING"
    PROMO_NOMENCLATURE_TABLE = "RAW_WB_PROMO_NOMENCLATURE"
    PROMO_OBSERVATIONS_TABLE = "WB_PROMO_OBSERVATIONS"
    REF_SKU_TABLE            = "REF_SKU_MASTER"
  }
}

resource "google_cloud_run_v2_job" "wb_promo_prod" {
  name                = "wb-promo-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
      # max_retries=0: ретрай Cloud Run происходит в ТОМ ЖЕ execution, то есть с тем
      # же run_id и тем же слотом. Пользы от него нет — HTTP-повторы 429/5xx уже
      # сделаны внутри wbHttp, — а вторая строка LOADER_RUNS с тем же run_id
      # засоряет манифест. Настоящий ретрай наблюдателя — следующий слот через
      # пять часов: цена одного пропущенного слота равна одному наблюдению.
      max_retries = 0
      # Снимок укладывается в 2–3 запроса при лимите категории 10 запросов / 6 с;
      # 600 с — запас на ретраи 429 и на появление regular-акции, у которой
      # дополнительно запрашивается состав.
      timeout = "600s"
      containers {
        image = var.container_image
        args  = ["promo"]
        dynamic "env" {
          for_each = merge(local.common_env, local.promo_env, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  # Образ и env продвигает deploy-prod.yml (реальный digest, GIT_SHA/IMAGE_DIGEST) —
  # ровно как у wb-prices-prod. Без ignore Terraform откатил бы образ на bootstrap
  # hello, который молча выходит с 0 и ничего не наблюдает.
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

# Каденс — 4 слота в сутки: 04/09/14/19 UTC = 07/12/17/22 МСК.
# Обоснование частоты — docs/promotions/PROMOTION_DECISION_ENGINE_SPEC_2026-09-22.md §9.
# Кратко: суточный снимок пропустил бы дату автодобавления Ozon (21:00 UTC),
# а почасовой дал бы ~840 запросов в сутки ради величин, которые площадка
# считает от 30-суточной медианы.
#
# Часы ОБЯЗАНЫ совпадать с PROMO_SLOT_HOURS_UTC в cloud/src/loaders/promo/slot.ts:
# расхождение означало бы, что часть слотов никогда не наступает или что два
# запуска попадают в один слот и второй подавляется execution-guard'ом.
# Имя формата "<loader>-<environment>" обязательно: scheduler-control.yml
# собирает имя именно так.
resource "google_cloud_scheduler_job" "wb_promo_prod" {
  name      = "wb-promo-prod"
  region    = var.region
  schedule  = "0 4,9,14,19 * * *"
  time_zone = "Etc/UTC" # слот наблюдения считается в UTC — держим планировщик в той же шкале
  paused    = true      # снимается ТОЛЬКО после подтверждения read-only токена

  retry_config {
    # Ретрай планировщика не нужен: следующий слот наступит по расписанию и
    # запишет собственное наблюдение. Повтор внутри слота лишь продублировал бы
    # попытку, которую execution-guard всё равно подавит.
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_promo_prod.name}:run"
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
# Таблицы созданы DDL-скриптом (sql/promotions/pr_promo1_raw.sql), Terraform ими
# не владеет — здесь только права, по той же схеме, что у наблюдателя цен.
# Уровень таблицы, а не датасета: наблюдателю нужны ровно четыре таблицы, и
# выдавать ему запись на весь wb_raw (где живут финансы, продажи и остатки) не за что.
#
# dataEditor, а не dataViewer: загрузчик делает append в RAW и INSERT/UPDATE строки
# манифеста. Чтение REF_SKU_MASTER и запись LOADER_RUNS уже выданы в bigquery.tf
# (prod_read_ref / prod_write_runs) — здесь не дублируются.
#
# 🔴 Пропуск этих грантов проявился боевым прогоном 22.09.2026 (execution
# wb-promo-prod-hxhbt): образ и конфигурация верны, guard захвачен, и ровно на
# первой записи манифеста — Access Denied на WB_PROMO_OBSERVATIONS.
resource "google_bigquery_table_iam_member" "prod_write_promo_tables" {
  for_each = toset([
    "RAW_WB_PROMO_CALENDAR",
    "RAW_WB_PROMO_RANGING",
    "RAW_WB_PROMO_NOMENCLATURE",
    "WB_PROMO_OBSERVATIONS",
  ])

  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Право Scheduler'а запустить именно этот Job ─────────────────────────────
# Invoker выдаётся ПОРЕСУРСНО, а не на проект — та же причина, что у
# scheduler_prices_prod_invoke. Без этой строки Scheduler отрабатывает по
# расписанию, но получает PERMISSION_DENIED и НЕ создаёт execution: в самом
# Scheduler'е видно только `status.code = 7`, а в Cloud Run — тишина,
# неотличимая от «наблюдений не было». У наблюдателя цен этот дефект уже
# случался 07.09.2026; повторять его нечем.
resource "google_cloud_run_v2_job_iam_member" "scheduler_promo_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_promo_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}
