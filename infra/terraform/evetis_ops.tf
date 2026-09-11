# Stage B · Operations backend (2026-09-11).
#
# Отдельный кросс-канальный датасет для операционного запаса ФФ: журнал физических
# единиц, отгрузки, сборки наборов, настройки логистики. Отдельный — по правилу
# изоляции маркетплейсов (docs/control_tower/ARCHITECTURE_DELTA_V1_2026-09-11.md, R8):
# новые операционные объекты не расширяют ни wb_mart, ни evetis_ref.
#
# Terraform держит только сам датасет. Таблицы, витрины и процедуры создаются
# SQL-файлами sql/evetis_ops/ — так же, как объекты Control Tower.
#
# 🔴 Применять ТОЛЬКО целевым планом (-target=google_bigquery_dataset.evetis_ops),
#    пока ресурсы загрузчика воронки не слиты в main (CLAUDE.md, «Что запрещено»).
#
# delete_contents_on_destroy = false: удалить датасет вместе с журналом запаса
# случайным destroy нельзя — сначала явный DROP объектов (см. откат в
# docs/ops/STAGE_B_OPS_BACKEND_2026-09-11.md).
resource "google_bigquery_dataset" "evetis_ops" {
  dataset_id    = "evetis_ops"
  friendly_name = "EVETIS operations"
  description   = "Operational inventory backend (Stage B): physical-unit FF ledger, shipments, bundle builds, logistics settings. Cross-channel layer isolated from wb_mart / ozon_mart / evetis_ref."
  location      = var.bq_location

  delete_contents_on_destroy = false

  labels = {
    domain = "operations"
    stage  = "b"
  }
}
