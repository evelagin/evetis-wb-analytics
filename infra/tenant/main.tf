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
resource "google_bigquery_dataset" "this" {
  for_each = var.contract.datasets

  project                    = var.contract.project_id
  dataset_id                 = each.value
  location                   = var.contract.bq_location
  description                = "VTS tenant ${var.contract.tenant_id}: ${each.key}"
  delete_contents_on_destroy = false

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

  project_id  = var.contract.project_id
  region      = var.contract.region
  tenant_id   = var.contract.tenant_id
  ozon        = var.contract.marketplaces.ozon
  raw_dataset = google_bigquery_dataset.this[var.contract.marketplaces.ozon.raw_dataset_key].dataset_id
  ref_dataset = google_bigquery_dataset.this[var.contract.marketplaces.ozon.ref_dataset_key].dataset_id

  depends_on = [google_project_service.this, google_bigquery_table.this]
}
