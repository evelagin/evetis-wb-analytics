# ══════════════════════════════════════════════════════════════════════════════
# WB_SECURE — закрытый датасет коммерческих доказательств (OWNER DECISION 2026-10-11, WB FIRE / LOST INVENTORY).
# Док: docs/finance/WB_FIRE_LOSS_2026-10-11.md §0. Вью: sql/unitka/store_inventory_loss_v1.sql.
#
# Репозиторий публичный: поштучные доказательства утраты (склад × nm × количество), документы WB и претензия в Git НЕ
# лежат и НЕ лежат в wb_ops (у wb_ops широкий ACL: projectReaders/Writers, sa-ops-health, sa-ae-reader). Здесь — только
# схемы и права; данные разово пишет владелец tools/wb_inventory_loss_import.py из манифеста вне Git.
#
# ACL датасета — АВТОРИТАТИВНЫЙ (блоки access ниже заменяют умолчания BigQuery): только projectOwners (владелец проекта).
# Ни projectReaders, ни projectWriters, ни сервисных учёток на уровне датасета.
# sa-loaders-prod — dataViewer РОВНО на две таблицы, которые читают агрегатные вью P&L в wb_mart; таблицу претензии
# загрузчик не читает и прав на неё не получает. Записи у SA нет.
#
# 🔴 Ресурсы Terraform не доказывают фактическую изоляцию: права уровня проекта действуют в обход ACL датасета
# (сейчас: metabase-read-only — bigquery.dataViewer на весь проект; sa-terraform-apply — bigquery.admin + projectIamAdmin).
# До импорта — матрица фактического доступа (docs/finance/WB_FIRE_LOSS_2026-10-11.md §0); при обходе — STOP.
#
# APPLY — только targeted, с просмотренным планом: 1 датасет + 3 таблицы + 2 права = 6 to add, 0 to change, 0 to destroy.
# ══════════════════════════════════════════════════════════════════════════════

resource "google_bigquery_dataset" "wb_secure" {
  dataset_id    = "wb_secure"
  friendly_name = "WB secure evidence"
  description   = "Закрытые коммерческие доказательства WB (утрата товара на складах WB, документы компенсации, претензия). Только владелец; sa-loaders-prod читает две таблицы через табличные права. Не публиковать, не выгружать в Git."
  location      = var.bq_location

  delete_contents_on_destroy = false

  # Авторитативный ACL: умолчания BigQuery (projectReaders/Writers, создатель) не добавляются.
  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  labels = {
    domain = "wb"
    data   = "restricted"
  }
}

locals {
  wb_secure_import_cols = [
    { name = "import_run_id", type = "STRING", mode = "REQUIRED" },
    { name = "manifest_sha256", type = "STRING", mode = "REQUIRED" },
    { name = "imported_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ]
}

# Позиции утраты: склад × nm × количество отчёта WB о пострадавших товарах, событие и его дата.
resource "google_bigquery_table" "wb_inventory_loss_evidence" {
  dataset_id          = google_bigquery_dataset.wb_secure.dataset_id
  table_id            = "WB_INVENTORY_LOSS_EVIDENCE"
  deletion_protection = true
  description         = "Утрата товара на складах WB: позиции отчётов WB о пострадавших товарах (склад × nm × количество), событие и дата. Пишет только tools/wb_inventory_loss_import.py по OWNER ACK."
  schema = jsonencode(concat([
    { name = "incident_id", type = "STRING", mode = "REQUIRED" },
    { name = "report_warehouse", type = "STRING", mode = "REQUIRED" },
    { name = "wb_report_ref", type = "STRING", mode = "REQUIRED" },
    { name = "incident_date", type = "DATE", mode = "REQUIRED" },
    { name = "incident_evidence", type = "STRING", mode = "REQUIRED" },
    { name = "nm_id", type = "INT64", mode = "REQUIRED" },
    { name = "lost_qty", type = "INT64", mode = "REQUIRED" },
  ], local.wb_secure_import_cols))
}

# Выплаты WB за утраченный товар: документ WB, дата проводки, сумма; месяц события — только если документ его доказывает.
resource "google_bigquery_table" "wb_inventory_loss_compensation_evidence" {
  dataset_id          = google_bigquery_dataset.wb_secure.dataset_id
  table_id            = "WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE"
  deletion_protection = true
  description         = "Компенсации WB за утраченный товар: номер документа WB, дата проводки, сумма, атрибуция месяцу события (DOCUMENT_LINKED) или месяцу проводки (BOOKING_MONTH). Пишет только tools/wb_inventory_loss_import.py."
  schema = jsonencode(concat([
    { name = "wb_document", type = "STRING", mode = "REQUIRED" },
    { name = "booking_date", type = "DATE", mode = "REQUIRED" },
    { name = "amount_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "attribution_basis", type = "STRING", mode = "REQUIRED" },
    { name = "attributed_incident_month", type = "DATE" },
    { name = "evidence", type = "STRING", mode = "REQUIRED" },
  ], local.wb_secure_import_cols))
}

# Претензия к WB — справочно, вне P&L. Загрузчик не читает.
resource "google_bigquery_table" "wb_inventory_loss_claim" {
  dataset_id          = google_bigquery_dataset.wb_secure.dataset_id
  table_id            = "WB_INVENTORY_LOSS_CLAIM"
  deletion_protection = true
  description         = "Претензия к WB по утраченному товару — справочно, в P&L не входит: документальная себестоимость, заявлено (товар/прочее), дата, статус. Пишет только tools/wb_inventory_loss_import.py; читает только владелец."
  schema = jsonencode(concat([
    { name = "claim_id", type = "STRING", mode = "REQUIRED" },
    { name = "filed_date", type = "DATE", mode = "REQUIRED" },
    { name = "claim_documented_goods_cost_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "compensation_before_claim_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "claimed_goods_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "claimed_other_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "status", type = "STRING", mode = "REQUIRED" },
    { name = "evidence", type = "STRING", mode = "REQUIRED" },
  ], local.wb_secure_import_cols))
}

# Чтение (не запись) ровно двух таблиц, которые читают агрегатные вью P&L магазина под sa-loaders-prod.
resource "google_bigquery_table_iam_member" "wb_secure_loader_read" {
  for_each = {
    evidence     = google_bigquery_table.wb_inventory_loss_evidence.table_id
    compensation = google_bigquery_table.wb_inventory_loss_compensation_evidence.table_id
  }
  dataset_id = google_bigquery_dataset.wb_secure.dataset_id
  table_id   = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}
