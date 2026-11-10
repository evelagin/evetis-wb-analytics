# ============================================================================
# M2 — перенос статистических загрузок WB из Apps Script в Cloud Run.
#   orders           ← runWbOrdersIncremental   → RAW_WB_ORDERS
#   sales            ← runWbSalesIncremental    → RAW_WB_SALES_RETURNS
#   sales-reconcile  ← runWbSalesNightReconcile → RAW_WB_SALES_RETURNS
# Runbook: docs/ops/MIG_M2_STATISTICS_RUNBOOK.md. Код: cloud/src/loaders/statistics/*.
#
# Контуры (как у остатков):
#   *-shadow пишет ТОЛЬКО в RAW_WB_ORDERS__CR / RAW_WB_SALES_RETURNS__CR (sa-loaders-shadow);
#   *-prod   пишет ТОЛЬКО в боевые RAW (sa-loaders-prod). Контур дополнительно проверяет код
#            (statistics/targets.ts) ДО секрета и WB API.
# Все Scheduler создаются НА ПАУЗЕ. Снятие паузы — scheduler-control.yml по ACK владельца.
#
# Выпуск поэтапный: shadow-ресурсы применяются -target'ами (M2-SHADOW), prod-ресурсы — в вечер
# переключения (M2-CUTOVER). Списки target'ов — в runbook, раздел «Terraform».
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
      job             = "wb-orders"
      timeout         = "1800s" # пагинация: пауза 61 с между страницами, обычно 1–2 страницы
      prod_schedule   = "41 0,6,12,18 * * *"
      shadow_schedule = "8 1,7,13,19 * * *"
    }
    sales = {
      job             = "wb-sales"
      timeout         = "600s" # один запрос + до 3 повторов 429/5xx
      prod_schedule   = "22 0,6,12,18 * * *"
      shadow_schedule = "52 0,6,12,18 * * *"
    }
    "sales-reconcile" = {
      job             = "wb-sales-reconcile"
      timeout         = "600s"
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
}

# ── Секрет «Статистика». Создан владельцем вручную (план M2, §2: v1 от 28.09; актуальность
#    проверить до shadow). Terraform его НЕ создаёт и значения не хранит.
data "google_secret_manager_secret" "wb_token_statistics" {
  secret_id = "WB_TOKEN_STATISTICS"
}

resource "google_secret_manager_secret_iam_member" "shadow_access_wb_token_statistics" {
  secret_id = data.google_secret_manager_secret.wb_token_statistics.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_secret_manager_secret_iam_member" "prod_access_wb_token_statistics" {
  secret_id = data.google_secret_manager_secret.wb_token_statistics.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Теневые RAW: зеркало живых схем (tables.get, 10.11.2026), включая ПОРЯДОК колонок,
#    партицию и кластер. Все колонки заказов — STRING (так их пишет Apps Script).
resource "google_bigquery_table" "raw_wb_orders_cr" {
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

# ── Табличный IAM: каждый контур пишет ровно в свои две таблицы. LOADER_RUNS и чтение
#    REF_SKU_MASTER (+ базовые таблицы вью) уже выданы обоим SA в bigquery.tf.
resource "google_bigquery_table_iam_member" "shadow_write_orders_cr" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.raw_wb_orders_cr.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_bigquery_table_iam_member" "shadow_write_sales_cr" {
  dataset_id = var.raw_dataset
  table_id   = google_bigquery_table.raw_wb_sales_returns_cr.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

# Боевые таблицы создал Apps Script (wbOrdersBqEnsureTable_ / wbSalesBqEnsureTable_), Terraform ими
# не владеет — только выдаёт право записи (как у воронки и хранения). Применяется в вечер переключения.
resource "google_bigquery_table_iam_member" "prod_write_orders" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_ORDERS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_bigquery_table_iam_member" "prod_write_sales" {
  dataset_id = var.raw_dataset
  table_id   = "RAW_WB_SALES_RETURNS"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Cloud Run Jobs ───────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "wb_statistics_shadow" {
  for_each            = local.statistics_loaders
  name                = "${each.value.job}-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_shadow.email
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
            ORDERS_RAW_TABLE = google_bigquery_table.raw_wb_orders_cr.table_id
            SALES_RAW_TABLE  = google_bigquery_table.raw_wb_sales_returns_cr.table_id
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
    google_secret_manager_secret_iam_member.shadow_access_wb_token_statistics,
  ]
}

resource "google_cloud_run_v2_job" "wb_statistics_prod" {
  for_each            = local.statistics_loaders
  name                = "${each.value.job}-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.loaders_prod.email
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
            ORDERS_RAW_TABLE = "RAW_WB_ORDERS"
            SALES_RAW_TABLE  = "RAW_WB_SALES_RETURNS"
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
  depends_on = [google_project_service.enabled]
}

# ── Право запуска — ПОРЕСУРСНО (контракт scheduler_invoker_contract.test.ts) ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_statistics_shadow_invoke" {
  for_each = local.statistics_loaders
  location = var.region
  name     = google_cloud_run_v2_job.wb_statistics_shadow[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_statistics_prod_invoke" {
  for_each = local.statistics_loaders
  location = var.region
  name     = google_cloud_run_v2_job.wb_statistics_prod[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Scheduler: имя = имя Job (scheduler-control.yml собирает «<loader>-<environment>») ──
resource "google_cloud_scheduler_job" "wb_statistics_shadow" {
  for_each  = local.statistics_loaders
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
  for_each  = local.statistics_loaders
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
