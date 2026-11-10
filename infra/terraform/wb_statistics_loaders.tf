# ============================================================================
# M2 — перенос статистических загрузок WB из Apps Script в Cloud Run.
#   orders           ← runWbOrdersIncremental   → RAW_WB_ORDERS
#   sales            ← runWbSalesIncremental    → RAW_WB_SALES_RETURNS
#   sales-reconcile  ← runWbSalesNightReconcile → RAW_WB_SALES_RETURNS
# Runbook: docs/ops/MIG_M2_STATISTICS_RUNBOOK.md. Код: cloud/src/loaders/statistics/*.
#
# Контуры — СВОИ runtime-SA (общее правило миграции; sa-loaders-* не расширяются):
#   *-shadow → sa-wb-statistics-shadow: пишет ТОЛЬКО RAW_WB_ORDERS__CR / RAW_WB_SALES_RETURNS__CR;
#   *-prod   → sa-wb-statistics-prod:   пишет ТОЛЬКО боевые RAW_WB_ORDERS / RAW_WB_SALES_RETURNS.
# Контур дополнительно проверяет код (statistics/targets.ts) ДО секрета и WB API.
#
# 🔴 Оба контура — под флагами единой схемы M2–M5 (tools/tests/test_stream_enable_flags.py), оба bool,
# по умолчанию false; в infra.yml подключены к GitHub Variables M2_SHADOW_ENABLED / M2_PROD_ENABLED:
#   var.m2_shadow_enabled — ВЕСЬ shadow-контур (SA, его права, доступ к секрету, таблицы __CR, Job,
#     invoker, Scheduler). Включение — шаг M2-SHADOW (runbook §4);
#   var.m2_prod_enabled — ВЕСЬ prod-контур (SA, его права, доступ к секрету, Job, invoker, Scheduler).
#     Включение — шаг M2-CUTOVER (runbook §5).
# Пока флаг не задан, нецелевой `infra.yml apply` не создаёт ни одного ресурса M2. Все Scheduler
# создаются НА ПАУЗЕ. Ресурсы, которые уже есть в main, под флаги не поставлены.
# Алерт отдельный не нужен: политики loader_failed и scheduler_attempt_failed не сужены до Job'а и
# покрывают новые Job'ы сразу (scheduler.tf, unitka_monitoring.tf).
# ============================================================================

locals {
  # Расписание (Europe/Moscow). statistics-api: лимит WB 1 запрос/мин на метод, а 429 у WB бывает
  # «global limiter, per seller» — поэтому заказы и продажи разнесены на 19 минут, сверка — на 2 ч.
  # Число запусков равно Apps Script после PR #414: заказы 4/сут, продажи 4/сут, сверка 1/сут.
  #   prod: sales 00/06/12/18:22, orders 00/06/12/18:41, reconcile 04:20 (как в Apps Script).
  #         06:xx — последняя попытка перед витриной 09:00, 12:00-витрина видит попытку 06:xx.
  #   shadow (на время паритета): минуты подальше от Apps Script (:22 продажи, :31 заказы) и от
  #         будущего prod — :52 / :08 / 04:50.
  statistics_loaders = {
    orders = {
      job = "wb-orders"
      # Пагинация: пауза 61 с между страницами, обычно 1–2 страницы; худший случай без сбоев —
      # 30 страниц ≈ 29 × 61 с + запросы ≈ 1 900 с, поэтому 3 600 с.
      timeout         = "3600s"
      prod_schedule   = "41 0,6,12,18 * * *"
      shadow_schedule = "8 1,7,13,19 * * *"
    }
    sales = {
      job = "wb-sales"
      # Худший случай: попытка (4 запроса × таймаут 60 с + паузы 20/40/60 с ≈ 360 с) + повтор cli
      # (пауза 60 с, вторая попытка ≈ 360 с) + BigQuery ≈ 800 с → 900 с. Меньше — прогон убит в STARTED.
      timeout         = "900s"
      prod_schedule   = "22 0,6,12,18 * * *"
      shadow_schedule = "52 0,6,12,18 * * *"
    }
    "sales-reconcile" = {
      job             = "wb-sales-reconcile"
      timeout         = "900s" # тот же расчёт, что у sales
      prod_schedule   = "20 4 * * *"
      shadow_schedule = "50 4 * * *"
    }
  }

  statistics_common_env = {
    WB_STATISTICS_HOST   = "https://statistics-api.wildberries.ru"
    WB_STATISTICS_SECRET = "WB_TOKEN_STATISTICS"
    REF_SKU_TABLE        = "REF_SKU_MASTER"
    SALES_RECONCILE_DAYS = "7"
  }
  # Таблицы и REF, которые читают/пишут оба контура.
  statistics_prod_tables = { orders = "RAW_WB_ORDERS", sales = "RAW_WB_SALES_RETURNS" }
  statistics_ref_tables  = toset(["REF_SKU_MASTER", "REF_SKU_MASTER_DATA", "REF_ACTIVE_VERSION"])
}

# ── Runtime-SA ───────────────────────────────────────────────────────────────
resource "google_service_account" "wb_statistics_shadow" {
  count        = var.m2_shadow_enabled ? 1 : 0
  account_id   = "sa-wb-statistics-shadow"
  display_name = "WB statistics loaders runtime (SHADOW): orders/sales → *__CR"
}

resource "google_service_account" "wb_statistics_prod" {
  count        = var.m2_prod_enabled ? 1 : 0
  account_id   = "sa-wb-statistics-prod"
  display_name = "WB statistics loaders runtime (PROD): orders/sales → RAW_WB_ORDERS / RAW_WB_SALES_RETURNS"
}

locals {
  statistics_sa = merge(
    var.m2_shadow_enabled ? { shadow = google_service_account.wb_statistics_shadow[0] } : {},
    var.m2_prod_enabled ? { prod = google_service_account.wb_statistics_prod[0] } : {},
  )
  statistics_ref_grants = merge([
    for env, sa in local.statistics_sa : { for t in local.statistics_ref_tables : "${env}:${t}" => { sa = sa.email, table = t } }
  ]...)
}

resource "google_project_iam_member" "wb_statistics_jobuser" {
  for_each = local.statistics_sa
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${each.value.email}"
}

resource "google_bigquery_table_iam_member" "wb_statistics_write_runs" {
  for_each   = local.statistics_sa
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.loader_runs.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value.email}"
}

# REF_SKU_MASTER — обычный вью: для запроса нужен read и на его базовые таблицы.
resource "google_bigquery_table_iam_member" "wb_statistics_read_ref" {
  for_each   = local.statistics_ref_grants
  dataset_id = var.raw_dataset
  table_id   = each.value.table
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${each.value.sa}"
}

# actAs: deployer обновляет образ Job'а, terraform-apply создаёт Job с этим SA.
resource "google_service_account_iam_member" "wb_statistics_actas_deployer" {
  for_each           = local.statistics_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "wb_statistics_actas_terraform" {
  for_each           = local.statistics_sa
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# ── Секрет «Статистика». Создан владельцем вручную (план M2, §2: v1 от 28.09; актуальность
#    проверить до shadow). Terraform его НЕ создаёт и значения не хранит. Читается (data) только
#    при включённом контуре: при обоих флагах false план M2 не обращается к секрету вовсе.
data "google_secret_manager_secret" "wb_token_statistics" {
  count     = length(local.statistics_sa) > 0 ? 1 : 0
  secret_id = "WB_TOKEN_STATISTICS"
}

resource "google_secret_manager_secret_iam_member" "wb_statistics_secret_access" {
  for_each  = local.statistics_sa
  secret_id = data.google_secret_manager_secret.wb_token_statistics[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.email}"
}

# ── Теневые RAW: зеркало живых схем (tables.get, 10.11.2026), включая ПОРЯДОК колонок,
#    партицию и кластер. Все колонки заказов — STRING (так их пишет Apps Script).
resource "google_bigquery_table" "raw_wb_orders_cr" {
  count               = var.m2_shadow_enabled ? 1 : 0
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_ORDERS__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "_order_date"
  }
  clustering = ["wb_nm_id", "srid"]
  schema = jsonencode(concat(
    [for c in [
      "load_id", "loaded_at", "source_api", "row_hash", "raw_row_number",
      "srid", "order_uid", "order_dt", "cancel_dt", "is_cancel",
      "wb_nm_id", "wb_vendor_code", "barcode", "internal_sku", "sku_match_status",
      "category", "subject", "brand", "tech_size", "warehouse_name",
      "region_name", "country_name", "price_with_disc", "quantity", "sticker_id",
      "is_duplicate", "processed_status", "error_message", "last_change_date",
    ] : { name = c, type = "STRING" }],
    # В живой таблице _order_date стоит ПЕРЕД SPP-колонками: они добавлены позже (SPP-1, 28.09).
    [{ name = "_order_date", type = "DATE" }],
    [for c in ["total_price", "discount_percent", "spp", "finished_price"] : { name = c, type = "STRING" }],
  ))
}

resource "google_bigquery_table" "raw_wb_sales_returns_cr" {
  count               = var.m2_shadow_enabled ? 1 : 0
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_SALES_RETURNS__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "_sale_date"
  }
  clustering = ["sale_id", "srid", "wb_nm_id"]
  schema = jsonencode([
    { name = "load_id", type = "STRING" },
    { name = "loaded_at", type = "STRING" },
    { name = "source_api", type = "STRING" },
    { name = "row_hash", type = "STRING" },
    { name = "raw_row_number", type = "INTEGER" },
    { name = "sale_id", type = "STRING" },
    { name = "srid", type = "STRING" },
    { name = "g_number", type = "STRING" },
    { name = "income_id", type = "INTEGER" },
    { name = "sale_dt", type = "STRING" },
    { name = "last_change_date", type = "STRING" },
    { name = "wb_nm_id", type = "INTEGER" },
    { name = "wb_vendor_code", type = "STRING" },
    { name = "barcode", type = "STRING" },
    { name = "internal_sku", type = "STRING" },
    { name = "sku_match_status", type = "STRING" },
    { name = "category", type = "STRING" },
    { name = "subject", type = "STRING" },
    { name = "brand", type = "STRING" },
    { name = "tech_size", type = "STRING" },
    { name = "warehouse_name", type = "STRING" },
    { name = "warehouse_type", type = "STRING" },
    { name = "region_name", type = "STRING" },
    { name = "oblast_okrug_name", type = "STRING" },
    { name = "country_name", type = "STRING" },
    { name = "total_price", type = "NUMERIC" },
    { name = "discount_percent", type = "NUMERIC" },
    { name = "spp", type = "NUMERIC" },
    { name = "payment_sale_amount", type = "NUMERIC" },
    { name = "price_with_disc", type = "NUMERIC" },
    { name = "finished_price", type = "NUMERIC" },
    { name = "for_pay", type = "NUMERIC" },
    { name = "is_supply", type = "BOOLEAN" },
    { name = "is_realization", type = "BOOLEAN" },
    { name = "operation_type", type = "STRING" },
    { name = "is_return", type = "BOOLEAN" },
    { name = "sticker_id", type = "STRING" },
    { name = "processed_status", type = "STRING" },
    { name = "error_message", type = "STRING" },
    { name = "raw_json", type = "STRING" },
    { name = "_sale_date", type = "DATE" },
  ])
}

# ── Запись: каждый контур — ровно в свои две таблицы ──
resource "google_bigquery_table_iam_member" "shadow_write_orders_cr" {
  count      = var.m2_shadow_enabled ? 1 : 0
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.raw_wb_orders_cr[0].table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_statistics_shadow[0].email}"
}

resource "google_bigquery_table_iam_member" "shadow_write_sales_cr" {
  count      = var.m2_shadow_enabled ? 1 : 0
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.raw_wb_sales_returns_cr[0].table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_statistics_shadow[0].email}"
}

# Боевые таблицы создал Apps Script (wbOrdersBqEnsureTable_ / wbSalesBqEnsureTable_), Terraform ими
# не владеет — только выдаёт право записи. Только при m2_prod_enabled.
resource "google_bigquery_table_iam_member" "prod_write_orders" {
  count      = var.m2_prod_enabled ? 1 : 0
  dataset_id = var.raw_dataset
  table_id   = local.statistics_prod_tables.orders
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_statistics_prod[0].email}"
}

resource "google_bigquery_table_iam_member" "prod_write_sales" {
  count      = var.m2_prod_enabled ? 1 : 0
  dataset_id = var.raw_dataset
  table_id   = local.statistics_prod_tables.sales
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_statistics_prod[0].email}"
}

# ── Cloud Run Jobs ───────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "wb_statistics_shadow" {
  for_each            = var.m2_shadow_enabled ? local.statistics_loaders : {}
  name                = "${each.value.job}-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.wb_statistics_shadow[0].email
      # Ретрай — следующий слот расписания и один повтор транзиентного отказа внутри прогона
      # (retryTransient в registry.ts). Task-retry Cloud Run добавил бы лишние запросы к WB.
      max_retries = 0
      timeout     = each.value.timeout
      containers {
        image = var.container_image
        args  = [each.key]
        dynamic "env" {
          for_each = merge(local.common_env, local.statistics_common_env, {
            LOADER_NAME      = each.key
            ENVIRONMENT      = "shadow"
            ORDERS_RAW_TABLE = google_bigquery_table.raw_wb_orders_cr[0].table_id
            SALES_RAW_TABLE  = google_bigquery_table.raw_wb_sales_returns_cr[0].table_id
            # Стартовый watermark пустой __CR (только shadow, только пока в таблице нет API-строк).
            ORDERS_BOOTSTRAP_WATERMARK = var.statistics_shadow_orders_bootstrap_watermark
            SALES_BOOTSTRAP_WATERMARK  = var.statistics_shadow_sales_bootstrap_watermark
          })
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
      client,
      client_version,
    ]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.shadow_write_orders_cr,
    google_bigquery_table_iam_member.shadow_write_sales_cr,
    google_secret_manager_secret_iam_member.wb_statistics_secret_access,
    google_service_account_iam_member.wb_statistics_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job" "wb_statistics_prod" {
  for_each            = var.m2_prod_enabled ? local.statistics_loaders : {}
  name                = "${each.value.job}-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.wb_statistics_prod[0].email
      max_retries     = 0
      timeout         = each.value.timeout
      containers {
        image = var.container_image
        args  = [each.key]
        dynamic "env" {
          # Env prod-Job'а после CREATE не реконсилируется (ignore_changes). Контур защищён кодом:
          # без ORDERS_RAW_TABLE/SALES_RAW_TABLE = боевые таблицы прогон откажет STATS_TARGET_MISMATCH.
          for_each = merge(local.common_env, local.statistics_common_env, local.deploy_managed_env, {
            LOADER_NAME      = each.key
            ENVIRONMENT      = "prod"
            ORDERS_RAW_TABLE = local.statistics_prod_tables.orders
            SALES_RAW_TABLE  = local.statistics_prod_tables.sales
          })
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
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.prod_write_orders,
    google_bigquery_table_iam_member.prod_write_sales,
    google_secret_manager_secret_iam_member.wb_statistics_secret_access,
    google_service_account_iam_member.wb_statistics_actas_terraform,
  ]
}

# ── Право запуска — ПОРЕСУРСНО (контракт scheduler_invoker_contract.test.ts) ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_statistics_shadow_invoke" {
  for_each = var.m2_shadow_enabled ? local.statistics_loaders : {}
  location = var.region
  name     = google_cloud_run_v2_job.wb_statistics_shadow[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_statistics_prod_invoke" {
  for_each = var.m2_prod_enabled ? local.statistics_loaders : {}
  location = var.region
  name     = google_cloud_run_v2_job.wb_statistics_prod[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Scheduler: имя = имя Job (scheduler-control.yml собирает «<loader>-<environment>») ──
resource "google_cloud_scheduler_job" "wb_statistics_shadow" {
  for_each  = var.m2_shadow_enabled ? local.statistics_loaders : {}
  name      = "${each.value.job}-shadow"
  region    = var.region
  schedule  = each.value.shadow_schedule
  time_zone = "Europe/Moscow"
  paused    = true # снимается на старте паритета (ACK M2-SHADOW)

  # Повтор только HTTP-вызова `:run`; лишний execution отсечёт lease (свежий STARTED того же слота).
  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_statistics_shadow[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_statistics_prod" {
  for_each  = var.m2_prod_enabled ? local.statistics_loaders : {}
  name      = "${each.value.job}-prod"
  region    = var.region
  schedule  = each.value.prod_schedule
  time_zone = "Europe/Moscow"
  paused    = true # снимается в вечер переключения ПОСЛЕ снятия триггера Apps Script (ACK M2-CUTOVER)

  # Повтор только HTTP-вызова `:run`; лишний execution отсечёт lease (свежий STARTED того же слота).
  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_statistics_prod[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
