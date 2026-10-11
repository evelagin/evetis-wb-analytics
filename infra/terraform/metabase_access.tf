# ══════════════════════════════════════════════════════════════════════════════
# METABASE — явный белый список датасетов (OWNER DECISION 2026-10-11, F-20).
#
# Было: metabase-read-only — roles/bigquery.dataViewer на ВЕСЬ проект (выдано вручную, не через Terraform): учётка читала
# любой датасет, включая закрытый wb_secure, и автоматически получала любой будущий датасет.
# Стало: dataViewer только на датасеты, которые Metabase реально читает; проектная роль снимается (runbook:
# docs/finance/WB_FIRE_LOSS_2026-10-11.md §7). Отрицательные условия IAM не используются — только позитивный список.
# Проектная roles/bigquery.jobUser (создание заданий) остаётся как есть.
#
# Список — по рабочим запросам Metabase (INFORMATION_SCHEMA.JOBS_BY_PROJECT за 90 дней — фактически 26.08–04.10.2026,
# 13 536 заданий; синхронизация метаданных Metabase — запросы к dataset.INFORMATION_SCHEMA — не считается): базовые таблицы
# (referenced_tables) и вью, названные в тексте. Нужны 9 датасетов: wb_mart 8 550 заданий, wb_raw 6 483, evetis_ref 3 112,
# ozon_raw 1 206, evetis_mart 487, evetis_ops 298, ozon_mart 29, wb_ops 18, evetis_communications 6. Сырые wb_raw / ozon_raw
# нужны: вью витрин над ними не authorized (вызывающему нужен доступ к базовым таблицам), wb_raw ещё и в прямых запросах.
# ozon_stg и evetis_health — только в синхронизации, в рабочих запросах и вью разрешённых датасетов их нет: не выдаются.
# wb_secure — НЕТ, будущие датасеты — нет.
#
# APPLY — targeted, вместе с wb_secure.tf: 9 to add (по одному члену ACL на датасет), 0 to change, 0 to destroy.
# ОТКАТ: docs/finance/WB_FIRE_LOSS_2026-10-11.md §7 (не возвращать проектную роль после импорта в wb_secure).
# ══════════════════════════════════════════════════════════════════════════════

locals {
  metabase_member = "serviceAccount:metabase-read-only@${var.project_id}.iam.gserviceaccount.com"
  metabase_read_datasets = toset([
    "wb_raw", "wb_mart", "wb_ops",
    "ozon_raw", "ozon_mart",
    "evetis_ref", "evetis_ops", "evetis_mart", "evetis_communications",
  ])
}

resource "google_bigquery_dataset_iam_member" "metabase_read" {
  for_each   = local.metabase_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = local.metabase_member
}
