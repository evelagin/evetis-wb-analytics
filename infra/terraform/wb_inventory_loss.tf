# ══════════════════════════════════════════════════════════════════════════════
# WB FIRE / LOST INVENTORY — закрытые доказательства утраты товара на складах WB (OWNER DECISION 2026-10-11).
# Док: docs/finance/WB_FIRE_LOSS_2026-10-11.md. Вью: sql/unitka/store_inventory_loss_v1.sql.
#
# Репозиторий публичный: поштучные доказательства (склад × nm × количество), номера документов WB и данные претензии
# в Git НЕ лежат. Здесь только схемы. Данные пишет разово владелец инструментом tools/wb_inventory_loss_import.py
# из манифеста вне Git (PLAN → APPLY по отпечатку → перечитывание).
#
# Права — минимальные: sa-loaders-prod получает ТОЛЬКО чтение двух таблиц, которые читают вью P&L
# (V_WB_INVENTORY_LOSS_ITEMS, V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE → V_WB_STORE_ACCOUNT_LEDGER / P&L).
# Таблица претензии — вне P&L, загрузчик её не читает, права не выдаются. Записи у SA нет ни в одну.
#
# APPLY — только targeted, с просмотренным планом: 3 таблицы + 2 права = 5 to add, 0 to change, 0 to destroy.
# ══════════════════════════════════════════════════════════════════════════════

locals {
  wb_loss_import_cols = [
    { name = "import_run_id", type = "STRING", mode = "REQUIRED" },
    { name = "manifest_sha256", type = "STRING", mode = "REQUIRED" },
    { name = "imported_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ]
}

# Позиции утраты: склад × nm × количество отчёта WB о пострадавших товарах, событие и его дата.
resource "google_bigquery_table" "wb_inventory_loss_evidence" {
  dataset_id          = "wb_ops"
  table_id            = "WB_INVENTORY_LOSS_EVIDENCE"
  deletion_protection = true
  description         = "Утрата товара на складах WB: позиции отчётов WB о пострадавших товарах (склад × nm × количество), событие и дата. Пишет только tools/wb_inventory_loss_import.py по OWNER ACK; читает wb_mart.V_WB_INVENTORY_LOSS_ITEMS."
  schema = jsonencode(concat([
    { name = "incident_id", type = "STRING", mode = "REQUIRED" },
    { name = "report_warehouse", type = "STRING", mode = "REQUIRED" },
    { name = "wb_report_ref", type = "STRING", mode = "REQUIRED" },
    { name = "incident_date", type = "DATE", mode = "REQUIRED" },
    { name = "incident_evidence", type = "STRING", mode = "REQUIRED" },
    { name = "nm_id", type = "INT64", mode = "REQUIRED" },
    { name = "lost_qty", type = "INT64", mode = "REQUIRED" },
  ], local.wb_loss_import_cols))
}

# Выплаты WB за утраченный товар: документ WB, дата проводки, сумма; месяц события — только если документ его доказывает.
resource "google_bigquery_table" "wb_inventory_loss_compensation_evidence" {
  dataset_id          = "wb_ops"
  table_id            = "WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE"
  deletion_protection = true
  description         = "Компенсации WB за утраченный товар: номер документа WB, дата проводки, сумма, атрибуция месяцу события (DOCUMENT_LINKED) или месяцу проводки (BOOKING_MONTH). Пишет только tools/wb_inventory_loss_import.py; читает wb_mart.V_WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE."
  schema = jsonencode(concat([
    { name = "wb_document", type = "STRING", mode = "REQUIRED" },
    { name = "booking_date", type = "DATE", mode = "REQUIRED" },
    { name = "amount_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "attribution_basis", type = "STRING", mode = "REQUIRED" },
    { name = "attributed_incident_month", type = "DATE" },
    { name = "evidence", type = "STRING", mode = "REQUIRED" },
  ], local.wb_loss_import_cols))
}

# Претензия к WB — справочно, вне P&L: документальная себестоимость, заявленные суммы, статус.
resource "google_bigquery_table" "wb_inventory_loss_claim" {
  dataset_id          = "wb_ops"
  table_id            = "WB_INVENTORY_LOSS_CLAIM"
  deletion_protection = true
  description         = "Претензия к WB по утраченному товару — справочно, в P&L не входит: документальная себестоимость, заявлено (товар/прочее), дата, статус. Пишет только tools/wb_inventory_loss_import.py; читает wb_mart.V_WB_INVENTORY_LOSS_CLAIM_TRACKING (владелец)."
  schema = jsonencode(concat([
    { name = "claim_id", type = "STRING", mode = "REQUIRED" },
    { name = "filed_date", type = "DATE", mode = "REQUIRED" },
    { name = "claim_documented_goods_cost_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "compensation_before_claim_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "claimed_goods_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "claimed_other_rub", type = "NUMERIC", mode = "REQUIRED" },
    { name = "status", type = "STRING", mode = "REQUIRED" },
    { name = "evidence", type = "STRING", mode = "REQUIRED" },
  ], local.wb_loss_import_cols))
}

# Чтение (не запись) двух таблиц, которые читают вью P&L магазина под sa-loaders-prod.
resource "google_bigquery_table_iam_member" "wb_inventory_loss_read" {
  for_each = {
    evidence     = google_bigquery_table.wb_inventory_loss_evidence.table_id
    compensation = google_bigquery_table.wb_inventory_loss_compensation_evidence.table_id
  }
  dataset_id = "wb_ops"
  table_id   = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.loaders_prod.email}"
}
