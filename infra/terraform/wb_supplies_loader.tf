# ============================================================================
# M5 — загрузчик поставок WB (перенос runWbSuppliesDaily из Apps Script).
# Код: cloud/src/loaders/supplies/*, runbook: docs/ops/MIG_SUPPLIES_REF_RUNBOOK.md.
#
# Пары Job / Scheduler: wb-supplies-shadow (sa-wb-supplies-shadow → только __CR) и
# wb-supplies-prod (sa-wb-supplies-prod → боевые RAW_WB_SUPPLIES / _GOODS). Свои SA, а не
# общие sa-loaders-*: секрет «Поставки» и права на таблицы поставок нужны только этому Job'у.
# Оба расписания создаются PAUSED; снятие паузы — scheduler-control.yml по отдельному ACK.
#
# 🔴 ВСЁ ПОД ФЛАГАМИ (m5_variables.tf): var.m5_shadow_enabled / var.m5_prod_enabled, по умолчанию
# false. Пока флаг выключен, ни один ресурс этого файла не попадает ни в какой plan/apply, и
# отсутствующий секрет не роняет чужие инфраструктурные задачи.
#
# 🔴 ПРЕДУСЛОВИЕ включения флага: секрет WB_TOKEN_SUPPLIES (категория «Поставки») заводит
# ВЛАДЕЛЕЦ в Secret Manager вручную, как WB_TOKEN_REPORTS. Terraform его не создаёт и значения не
# хранит; без секрета data-источник уронит plan — это намеренный fail-closed, но только после
# включения флага.
# ============================================================================

locals {
  supplies_env_shadow = {
    LOADER_NAME           = "supplies"
    WB_SUPPLIES_SECRET    = "WB_TOKEN_SUPPLIES"
    SUPPLIES_HEADER_TABLE = "RAW_WB_SUPPLIES__CR"
    SUPPLIES_GOODS_TABLE  = "RAW_WB_SUPPLIES_GOODS__CR"
  }
  supplies_env_prod = {
    LOADER_NAME           = "supplies"
    WB_SUPPLIES_SECRET    = "WB_TOKEN_SUPPLIES"
    SUPPLIES_HEADER_TABLE = "RAW_WB_SUPPLIES"
    SUPPLIES_GOODS_TABLE  = "RAW_WB_SUPPLIES_GOODS"
  }
  m5_supplies_shadow = var.m5_shadow_enabled ? 1 : 0
  m5_supplies_prod   = var.m5_prod_enabled ? 1 : 0
  m5_supplies_secret = var.m5_shadow_enabled || var.m5_prod_enabled ? 1 : 0
  # Схемы = боевые таблицы (созданы Apps Script wbSuppliesEnsureTables, сверены tables.get 10.11.2026).
  supplies_header_schema = [
    { name = "snapshot_ts", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "entity_key", type = "STRING", mode = "REQUIRED" },
    { name = "supply_id", type = "INTEGER" },
    { name = "preorder_id", type = "INTEGER" },
    { name = "status_id", type = "INTEGER" },
    { name = "create_dt", type = "TIMESTAMP" },
    { name = "supply_dt", type = "TIMESTAMP" },
    { name = "fact_dt", type = "TIMESTAMP" },
    { name = "updated_dt", type = "TIMESTAMP" },
    { name = "box_type_id", type = "INTEGER" },
    { name = "is_box_on_pallet", type = "BOOLEAN" },
    { name = "warehouse_id", type = "INTEGER" },
    { name = "warehouse_name", type = "STRING" },
    { name = "transit_warehouse_id", type = "INTEGER" },
    { name = "transit_warehouse_name", type = "STRING" },
    { name = "actual_warehouse_id", type = "INTEGER" },
    { name = "actual_warehouse_name", type = "STRING" },
    { name = "acceptance_cost", type = "NUMERIC" },
    { name = "paid_acceptance_coefficient", type = "NUMERIC" },
    { name = "storage_coef", type = "NUMERIC" },
    { name = "delivery_coef", type = "NUMERIC" },
    { name = "reject_reason", type = "STRING" },
    { name = "supplier_assign_name", type = "STRING" },
    { name = "card_quantity", type = "INTEGER" },
    { name = "card_accepted_quantity", type = "INTEGER" },
    { name = "card_ready_quantity", type = "INTEGER" },
    { name = "card_unloading_quantity", type = "INTEGER" },
    { name = "depersonalized_quantity", type = "INTEGER" },
    { name = "can_show_quantity", type = "BOOLEAN" },
    { name = "is_completed", type = "BOOLEAN" },
    { name = "card_read_ok", type = "BOOLEAN" },
    { name = "raw_json", type = "STRING" },
    { name = "row_hash", type = "STRING" },
  ]
  supplies_goods_schema = [
    { name = "snapshot_ts", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "entity_key", type = "STRING", mode = "REQUIRED" },
    { name = "supply_id", type = "INTEGER" },
    { name = "barcode", type = "STRING" },
    { name = "vendor_code", type = "STRING" },
    { name = "nm_id", type = "INTEGER" },
    { name = "tech_size", type = "STRING" },
    { name = "color", type = "STRING" },
    { name = "tnved", type = "STRING" },
    { name = "need_kiz", type = "BOOLEAN" },
    { name = "supplier_box_amount", type = "INTEGER" },
    { name = "quantity", type = "INTEGER" },
    { name = "accepted_quantity", type = "INTEGER" },
    { name = "ready_for_sale_quantity", type = "INTEGER" },
    { name = "unloading_quantity", type = "INTEGER" },
    { name = "accepted_quantity_zero", type = "BOOLEAN" },
    { name = "acceptance_resolution", type = "STRING" },
    { name = "raw_json", type = "STRING" },
    { name = "row_hash", type = "STRING" },
  ]
}

# ── Runtime-SA ──────────────────────────────────────────────────────────────
resource "google_service_account" "wb_supplies_shadow" {
  count        = local.m5_supplies_shadow
  account_id   = "sa-wb-supplies-shadow"
  display_name = "WB supplies runtime (SHADOW): WB_TOKEN_SUPPLIES + RAW_WB_SUPPLIES*__CR"
}

resource "google_service_account" "wb_supplies_prod" {
  count        = local.m5_supplies_prod
  account_id   = "sa-wb-supplies-prod"
  display_name = "WB supplies runtime (PROD): WB_TOKEN_SUPPLIES + RAW_WB_SUPPLIES*"
}

resource "google_project_iam_member" "wb_supplies_shadow_jobuser" {
  count   = local.m5_supplies_shadow
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.wb_supplies_shadow[0].email}"
}

resource "google_project_iam_member" "wb_supplies_prod_jobuser" {
  count   = local.m5_supplies_prod
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.wb_supplies_prod[0].email}"
}

# actAs: deployer обновляет образ Job'а, terraform-apply создаёт Job/Scheduler с этим SA.
resource "google_service_account_iam_member" "wb_supplies_shadow_actas_deployer" {
  count              = local.m5_supplies_shadow
  service_account_id = google_service_account.wb_supplies_shadow[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "wb_supplies_shadow_actas_terraform" {
  count              = local.m5_supplies_shadow
  service_account_id = google_service_account.wb_supplies_shadow[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

resource "google_service_account_iam_member" "wb_supplies_prod_actas_deployer" {
  count              = local.m5_supplies_prod
  service_account_id = google_service_account.wb_supplies_prod[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "wb_supplies_prod_actas_terraform" {
  count              = local.m5_supplies_prod
  service_account_id = google_service_account.wb_supplies_prod[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# ── Теневые таблицы ─────────────────────────────────────────────────────────
resource "google_bigquery_table" "raw_wb_supplies_cr" {
  count               = local.m5_supplies_shadow
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_SUPPLIES__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "snapshot_ts"
  }
  schema = jsonencode(local.supplies_header_schema)
}

resource "google_bigquery_table" "raw_wb_supplies_goods_cr" {
  count               = local.m5_supplies_shadow
  dataset_id          = var.raw_dataset
  table_id            = "RAW_WB_SUPPLIES_GOODS__CR"
  deletion_protection = false
  time_partitioning {
    type  = "DAY"
    field = "snapshot_ts"
  }
  schema = jsonencode(local.supplies_goods_schema)
}

# ── Секрет «Поставки»: создаёт владелец; читается только при включённом флаге ──
data "google_secret_manager_secret" "wb_token_supplies" {
  count     = local.m5_supplies_secret
  secret_id = "WB_TOKEN_SUPPLIES"
}

resource "google_secret_manager_secret_iam_member" "wb_supplies_shadow_secret" {
  count     = local.m5_supplies_shadow
  secret_id = data.google_secret_manager_secret.wb_token_supplies[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.wb_supplies_shadow[0].email}"
}

resource "google_secret_manager_secret_iam_member" "wb_supplies_prod_secret" {
  count     = local.m5_supplies_prod
  secret_id = data.google_secret_manager_secret.wb_token_supplies[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.wb_supplies_prod[0].email}"
}

# ── Табличный IAM: shadow — две __CR + LOADER_RUNS, prod — две боевые + LOADER_RUNS ──
# LOADER_RUNS обязателен: cli.ts делает acquire/finalize lease ДО и ПОСЛЕ обработчика.
resource "google_bigquery_table_iam_member" "wb_supplies_shadow_write" {
  for_each = var.m5_shadow_enabled ? {
    header = "RAW_WB_SUPPLIES__CR"
    goods  = "RAW_WB_SUPPLIES_GOODS__CR"
    runs   = google_bigquery_table.loader_runs.table_id
  } : {}
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_supplies_shadow[0].email}"
  depends_on = [google_bigquery_table.raw_wb_supplies_cr, google_bigquery_table.raw_wb_supplies_goods_cr]
}

resource "google_bigquery_table_iam_member" "wb_supplies_prod_write" {
  for_each = var.m5_prod_enabled ? {
    header = "RAW_WB_SUPPLIES"
    goods  = "RAW_WB_SUPPLIES_GOODS"
    runs   = google_bigquery_table.loader_runs.table_id
  } : {}
  dataset_id = var.raw_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.wb_supplies_prod[0].email}"
}

# ── Jobs ────────────────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "wb_supplies_shadow" {
  count               = local.m5_supplies_shadow
  name                = "wb-supplies-shadow"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.wb_supplies_shadow[0].email
      # Ретрай Cloud Run — тот же execution (тот же lease); транзиентный отказ повторяет cli.ts.
      max_retries = 0
      # Первый shadow-прогон — полный снимок (~140 объектов × 2 запроса × 0,9 с + 429) ≈ 5–10 мин;
      # мягкий бюджет загрузчика 20 мин, жёсткий потолок Job'а — 30.
      timeout = "1800s"
      containers {
        image = var.container_image
        args  = ["supplies"]
        dynamic "env" {
          for_each = merge(local.common_env, local.supplies_env_shadow, { ENVIRONMENT = "shadow" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # env принадлежит Terraform (deploy-shadow.yml меняет только образ).
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [
    google_project_service.enabled,
    google_bigquery_table_iam_member.wb_supplies_shadow_write,
    google_secret_manager_secret_iam_member.wb_supplies_shadow_secret,
    google_project_iam_member.wb_supplies_shadow_jobuser,
    google_service_account_iam_member.wb_supplies_shadow_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job" "wb_supplies_prod" {
  count               = local.m5_supplies_prod
  name                = "wb-supplies-prod"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.wb_supplies_prod[0].email
      max_retries     = 0
      timeout         = "1800s"
      containers {
        image = var.container_image
        args  = ["supplies"]
        dynamic "env" {
          for_each = merge(local.common_env, local.supplies_env_prod, local.deploy_managed_env, { ENVIRONMENT = "prod" })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
  # env — совместное владение с deploy-prod.yml (IMAGE_DIGEST/GIT_SHA), как у остальных *-prod.
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
    google_service_account_iam_member.wb_supplies_prod_actas_terraform,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_supplies_shadow_invoke" {
  count    = local.m5_supplies_shadow
  location = var.region
  name     = google_cloud_run_v2_job.wb_supplies_shadow[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_supplies_prod_invoke" {
  count    = local.m5_supplies_prod
  location = var.region
  name     = google_cloud_run_v2_job.wb_supplies_prod[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# ── Scheduler ───────────────────────────────────────────────────────────────
# 02:29 МСК — время триггера Apps Script. 05:29 — догоняющее окно: COMPLETE за сутки уже есть →
# guard_skip (один SELECT по LOADER_RUNS), ERROR первой попытки → второй снимок того же дня.
# Shadow сдвинут на 10 минут: два снимка одного часа удобнее сверять, чем совпадающие секунды.
resource "google_cloud_scheduler_job" "wb_supplies_shadow" {
  count     = local.m5_supplies_shadow
  name      = "wb-supplies-shadow"
  region    = var.region
  schedule  = "39 2,5 * * *"
  time_zone = "Europe/Moscow"
  paused    = true

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_supplies_shadow[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_shadow.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "wb_supplies_prod" {
  count     = local.m5_supplies_prod
  name      = "wb-supplies-prod"
  region    = var.region
  schedule  = "29 2,5 * * *"
  time_zone = "Europe/Moscow"
  paused    = true # снимается только на cutover, после снятия триггера Apps Script

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.wb_supplies_prod[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler_prod.email
    }
  }

  lifecycle {
    ignore_changes = [paused]
  }
  depends_on = [google_project_service.enabled]
}
