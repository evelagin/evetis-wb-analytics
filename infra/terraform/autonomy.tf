# ── Autonomous Engineering v1: read-only идентичность автономного контура ──────
#
# НЕ ПРИМЕНЕНО. Применяет только владелец, целевым apply после просмотра плана
# (docs/architecture/AE_V1_RUNBOOK.md §2). Полный apply из main запрещён CLAUDE.md.
#
# Что даёт: чтение датасетов, по которым работают ворота Platform Baseline, запуск
# запросов (jobUser) и чтение журнала заданий для аудита мутаций. Чего НЕ даёт: ни одной
# роли записи. BigQuery DDL/DML требует tables.create/updateData на датасете — у
# dataViewer их нет, поэтому попытка мутации падает на стороне BigQuery, а не на
# добросовестности агента. tools/tests/test_autonomy_security.py проверяет, что сюда
# не просочилась роль шире разрешённого набора.
#
# Кто может выпустить токен этого SA: только перечисленные job'ы AE на main
# (attribute.job_workflow_ref). Ревьюер (autonomy-review.yml) в списке ОТСУТСТВУЕТ: у него
# id-token есть только ради федерации Claude API, данных production ему не нужно.

resource "google_service_account" "ae_reader" {
  account_id   = "sa-ae-reader"
  display_name = "EVETIS Autonomous Engineering v1 (READ-ONLY)"
  description  = "AE v1: чтение данных для доказательств ворот. Ролей записи нет по построению."
}

locals {
  ae_read_datasets = toset([
    var.raw_dataset, var.mart_dataset, "wb_ops", "evetis_ref", "evetis_ops", "evetis_mart", "ozon_raw", "ozon_mart",
  ])
  ae_project_roles = toset([
    "roles/bigquery.jobUser",        # запуск запросов (сами по себе данных не дают)
    "roles/bigquery.resourceViewer", # INFORMATION_SCHEMA.JOBS_BY_PROJECT — аудит мутаций
    "roles/logging.viewer",          # чтение логов Cloud Run/Scheduler при расследовании
  ])
  # Файлы, где ОБЪЯВЛЕН job (job_workflow_ref). У autonomy-watch.yml (верхний уровень) он равен
  # workflow_ref; остальные — переиспользуемые, вызываемые из autonomy-run.yml.
  ae_reader_jobs = toset(["autonomy-watch.yml", "autonomy-gate.yml", "autonomy-engineer.yml", "autonomy-test.yml"])
}

resource "google_project_iam_member" "ae_reader" {
  for_each = local.ae_project_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.ae_reader.email}"
}

resource "google_bigquery_dataset_iam_member" "ae_reader_read" {
  for_each   = local.ae_read_datasets
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ae_reader.email}"
}

resource "google_service_account_iam_member" "ae_reader_wif" {
  for_each           = local.ae_reader_jobs
  service_account_id = google_service_account.ae_reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.job_workflow_ref/${local.github_workflows}/${each.value}@refs/heads/main"
}
