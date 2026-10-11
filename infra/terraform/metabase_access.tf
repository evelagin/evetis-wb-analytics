# ══════════════════════════════════════════════════════════════════════════════
# METABASE — явный белый список датасетов (OWNER DECISION 2026-10-11, F-20).
#
# Было: metabase-read-only — roles/bigquery.dataViewer на ВЕСЬ проект (выдано вручную, не через Terraform): учётка читала
# любой датасет, включая закрытый wb_secure, и автоматически получала любой будущий датасет.
# Стало: dataViewer только на датасеты, которые Metabase реально читает; проектная роль снимается (runbook:
# docs/finance/WB_FIRE_LOSS_2026-10-11.md §7). Отрицательные условия IAM не используются — только позитивный список.
# Проектная roles/bigquery.jobUser (создание заданий) остаётся как есть.
#
# Список — по истории заданий Metabase за 90 дней (INFORMATION_SCHEMA.JOBS_BY_PROJECT, referenced_tables, 13 536 заданий):
# читались ровно эти 11 датасетов. Сырые wb_raw / ozon_raw нужны по факту: прямые запросы (648 / 455 заданий) и вью
# витрин над ними (вью не authorized — вызывающему нужен доступ к базовым таблицам). wb_secure — НЕТ, будущие датасеты — нет.
#
# APPLY — targeted, вместе с wb_secure.tf: 11 to add (по одному члену ACL на датасет), 0 to change, 0 to destroy.
# ══════════════════════════════════════════════════════════════════════════════

locals {
  metabase_member = "serviceAccount:metabase-read-only@${var.project_id}.iam.gserviceaccount.com"
  metabase_read_datasets = toset([
    "wb_raw", "wb_mart", "wb_ops",
    "ozon_raw", "ozon_stg", "ozon_mart",
    "evetis_ref", "evetis_ops", "evetis_mart", "evetis_health", "evetis_communications",
  ])
}

resource "google_bigquery_dataset_iam_member" "metabase_read" {
  for_each   = local.metabase_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = local.metabase_member
}
