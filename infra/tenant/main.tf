# ── API проекта арендатора ────────────────────────────────────────────────
# Список — из контракта (platform.TENANT_BASE_APIS + API включённых площадок).
# bootstrap_apis включает человек при создании проекта; здесь их не трогаем.
resource "google_project_service" "this" {
  for_each = toset(var.contract.apis)

  project                    = var.contract.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false

  depends_on = [terraform_data.guard]
}

# ── BigQuery: датасеты и таблицы runtime ───────────────────────────────────
# Состав датасетов — из контракта (сейчас ozon_raw и ref; T4 добавит ozon_mart и
# analytics_share без изменения этого файла). Таблицы — все, что runtime пишет для
# включённых сущностей арендатора (tools/tenancy/ozon_contract.py), со схемами из Git.
#
# ACL датасета — АВТОРИТЕТНЫЙ и полный (T3.3). Если access не задан при создании, BigQuery
# добавляет создателя датасета (провижионера) как OWNER — это чтение, запись и удаление
# данных арендатора в обход роли провижионера. Поэтому доступ перечислен целиком:
#   * projectOwners → OWNER (владелец проекта арендатора; у него это есть и через roles/owner);
#   * runtime SA площадки → WRITER на свой raw, READER на ref (эквиваленты dataEditor/dataViewer).
# Больше никого: ни провижионера, ни SA планировщика, ни клиента. Лишнюю запись (в т.ч.
# созданную API) Terraform увидит как дрейф и снимет; сканер плана сверяет ACL точно.
locals {
  ozon               = var.contract.marketplaces.ozon
  ozon_runtime_email = local.ozon == null ? null : one(module.ozon[*].runtime_email)
  dataset_data_grants = local.ozon == null ? [] : [
    { dataset_key = local.ozon.raw_dataset_key, role = "WRITER", user_by_email = local.ozon_runtime_email },
    { dataset_key = local.ozon.ref_dataset_key, role = "READER", user_by_email = local.ozon_runtime_email },
  ]
}

resource "google_bigquery_dataset" "this" {
  for_each = var.contract.datasets

  project                    = var.contract.project_id
  dataset_id                 = each.value
  location                   = var.contract.bq_location
  description                = "VTS tenant ${var.contract.tenant_id}: ${each.key}"
  delete_contents_on_destroy = false

  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  dynamic "access" {
    for_each = [for g in local.dataset_data_grants : g if g.dataset_key == each.key]
    content {
      role          = access.value.role
      user_by_email = access.value.user_by_email
    }
  }

  # T4.1: деплоер SQL — роли организации mpaSql* только здесь, в ACL датасетов арендатора.
  # Email — литерал контракта (известен на плане, сканер сверяет ACL точно); SA создаётся раньше.
  dynamic "access" {
    for_each = [for g in var.contract.sql_deployer.grants : g if g.dataset_key == each.key]
    content {
      role          = access.value.role
      user_by_email = var.contract.sql_deployer.email

      dynamic "condition" {
        for_each = access.value.condition == null ? [] : [access.value.condition]
        content {
          title       = condition.value.title
          description = condition.value.description
          expression  = condition.value.expression
        }
      }
    }
  }

  # T5 (D1): control plane — роли организации только здесь, без условий (матрица control_identity).
  dynamic "access" {
    for_each = var.contract.control == null ? [] : [for g in var.contract.control.grants : g if g.dataset_key == each.key]
    content {
      role          = access.value.role
      user_by_email = var.contract.control.email
    }
  }

  depends_on = [google_project_service.this, google_service_account.sql_deployer, google_service_account.control]
}

# ── Деплоер SQL (T4.1) ─────────────────────────────────────────────────────
# Отдельная идентичность пакета SQL: провижионер data-blind, а BigQuery требует у создателя VIEW
# bigquery.tables.getData на источниках. Ролей на проекте у деплоера нет. Привязку
# roles/iam.workloadIdentityUser на этот SA создаёт ВЛАДЕЛЕЦ (tools/tenancy/tenant_bootstrap.py
# bind): у провижионера нет iam.serviceAccounts.setIamPolicy, и давать его не будем.
resource "google_service_account" "sql_deployer" {
  project      = var.contract.project_id
  account_id   = var.contract.sql_deployer.account_id
  display_name = "VTS SQL deployer"
  description  = "Tenancy T4.1: views of the approved SQL package via Tables API; dataset ACL roles only"

  depends_on = [google_project_service.this]
}

# ── Control plane арендатора (T5, D1) ────────────────────────────────────────
# Проверка учётных данных, наблюдения identity, возможности, история, план и чекпойнты,
# DQ, переходы автомата. Ролей на проекте нет (в т.ч. bigquery.jobUser: без jobs.create
# журналы tenant_ops append-only на уровне IAM). Доступ — ACL датасетов выше и чтение 4
# секретов (module.ozon). Записи в ref нет: привязку подтверждает только владелец.
resource "google_service_account" "control" {
  count = var.contract.control == null ? 0 : 1

  project      = var.contract.project_id
  account_id   = var.contract.control.account_id
  display_name = "VTS tenant control plane"
  description  = "Tenancy T5: credential validation, identity observations, lifecycle; dataset ACL roles only"

  depends_on = [google_project_service.this]
}

resource "google_bigquery_table" "this" {
  for_each = { for t in var.contract.tables : "${t.dataset_key}.${t.table_id}" => t }

  project                  = var.contract.project_id
  dataset_id               = google_bigquery_dataset.this[each.value.dataset_key].dataset_id
  table_id                 = each.value.table_id
  schema                   = each.value.schema_json
  clustering               = length(each.value.clustering) > 0 ? each.value.clustering : null
  require_partition_filter = each.value.require_partition_filter
  deletion_protection      = true

  dynamic "time_partitioning" {
    for_each = each.value.time_partitioning_type == null ? [] : [each.value]
    content {
      type  = time_partitioning.value.time_partitioning_type
      field = time_partitioning.value.time_partitioning_field
    }
  }
}

# ── Площадки ───────────────────────────────────────────────────────────────
# Ozon — первый модуль площадки. WB добавится соседним module "wb" (sa-wb-*, wb_raw,
# свои секреты и job'ы) без изменения изоляции: граница — проект арендатора.
module "ozon" {
  source = "./modules/ozon_runtime"
  count  = var.contract.marketplaces.ozon == null ? 0 : 1

  project_id = var.contract.project_id
  region     = var.contract.region
  tenant_id  = var.contract.tenant_id
  ozon       = var.contract.marketplaces.ozon
  control    = var.contract.control

  # Датасеты ждут runtime SA (он в их ACL), поэтому модуль от датасетов и таблиц не зависит.
  # SA control создаётся в корне (он и в ACL датасетов); секреты модуля выдают ему accessor.
  depends_on = [google_project_service.this, google_service_account.control]
}
