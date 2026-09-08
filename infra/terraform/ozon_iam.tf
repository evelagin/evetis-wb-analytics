# ============================================================================
# Stage A / A4 — изоляция прав Ozon-загрузчика.
#
# БЫЛО (создано вручную, вне Terraform):
#   sa-ozon-ingestion@… :
#     roles/bigquery.jobUser     на уровне ПРОЕКТА
#     roles/bigquery.dataEditor  на уровне ПРОЕКТА   ← право записи во ВСЕ датасеты
#     roles/bigquery.dataViewer  на уровне ПРОЕКТА   ← право чтения ВСЕХ датасетов
#
#   Проектный dataEditor означает членство в projectWriters, а значит запись в
#   wb_raw, wb_mart, wb_ops и evetis_ref. Ошибка в pipelines/ozon (например,
#   неверный BQ_RAW_DATASET) могла бы перезаписать данные Wildberries.
#   Это прямое нарушение изоляции маркетплейсов — находка F-04 (HIGH).
#
# СТАЛО:
#   roles/bigquery.jobUser     на уровне проекта  (нужен, чтобы запускать job'ы)
#   roles/bigquery.dataEditor  на датасете ozon_raw ТОЛЬКО
#
# ПОЧЕМУ ИМЕННО ozon_raw И ПОЧЕМУ dataEditor, А НЕ dataViewer + что-то ещё.
#   Загрузчик не просто пишет строки: на каждую сущность он создаёт временную
#   таблицу `_rt_<table>_<run>`, грузит в неё LOAD-job'ом, делает MERGE в целевую
#   и удаляет временную (pipelines/ozon/runtime/common.py:146-186). Это требует
#   tables.create и tables.delete внутри датасета, то есть именно dataEditor.
#
# ДОКАЗАТЕЛЬСТВО ДОСТАТОЧНОСТИ (а не предположение).
#   INFORMATION_SCHEMA.JOBS_BY_PROJECT за 180 суток по user_email этого SA:
#     referenced_tables -> ровно один датасет, ozon_raw (502 обращения,
#     310 write-job'ов, 192 SELECT, первое 2026-09-03, последнее 2026-09-08);
#     job_type/destination -> 158 LOAD и 155 MERGE, все в ozon_raw.
#   Ни одного обращения к wb_raw, wb_mart, wb_ops, evetis_ref, ozon_mart,
#   ozon_stg. Упоминание evetis_ref.REF_SKU_CHANNEL_MAP в коде — только в
#   docstring: резолв идентификаторов выполняется во вьюхах ozon_mart, а не в
#   загрузчике.
#
# ЧТО НЕ ТРОГАЕМ.
#   Доступ к GCS уже узкий: roles/storage.objectViewer выдан ПОБАКЕТНО на
#   gs://evetis-ozon-staging-37074083763 (нужен bootstrap-джобу для
#   load_table_from_uri). Проектной роли storage у этого SA нет.
#
# ⚠️ СТАТУС НА 2026-09-08: ПЕРЕХОД НЕ ЗАВЕРШЁН.
#   Выполнено: гранулярный dataEditor на ozon_raw выдан в production (шаг 1).
#   НЕ выполнено: снятие двух проектных ролей (dataEditor, dataViewer) —
#   операция заблокирована политикой рабочей среды, где выполнялся Stage A.
#   Пока они не сняты, изоляция НЕ достигнута и F-04 остаётся открытой.
#   Команды, окно, приёмка и откат: docs/ops/STAGE_A_OZON_IAM_CUTOVER.md.
#
# СОСТОЯНИЕ TERRAFORM. Ни SA, ни его проектные роли Terraform никогда не
#   принадлежали — в state их нет. import-блоки ниже принимают SA и целевые
#   привязки. Снятые проектные роли в конфиге намеренно отсутствуют: Terraform
#   с ресурсами `google_project_iam_member` не является authoritative и сам их
#   не удалит — удаление выполняется командами из runbook выше.
# ============================================================================

resource "google_service_account" "ozon_ingestion" {
  account_id   = "sa-ozon-ingestion"
  display_name = "Ozon loaders runtime"
}

resource "google_service_account" "ozon_scheduler" {
  account_id   = "sa-ozon-scheduler"
  display_name = "Ozon Cloud Scheduler invoker"
}

import {
  to = google_service_account.ozon_ingestion
  id = "projects/${var.project_id}/serviceAccounts/sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = google_service_account.ozon_scheduler
  id = "projects/${var.project_id}/serviceAccounts/sa-ozon-scheduler@${var.project_id}.iam.gserviceaccount.com"
}

# Проектный уровень — только право ЗАПУСКАТЬ job'ы. Данных эта роль не даёт.
resource "google_project_iam_member" "ozon_ingestion_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ozon_ingestion.email}"
}

import {
  to = google_project_iam_member.ozon_ingestion_job_user
  id = "${var.project_id} roles/bigquery.jobUser serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
}

# Единственный датасет, к которому у Ozon-загрузчика есть доступ к данным.
resource "google_bigquery_dataset_iam_member" "ozon_ingestion_edit_raw" {
  dataset_id = "ozon_raw"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ozon_ingestion.email}"
}

# Грант уже выдан в production на шаге A5 Stage A — принимаем, а не создаём.
import {
  to = google_bigquery_dataset_iam_member.ozon_ingestion_edit_raw
  id = "projects/${var.project_id}/datasets/ozon_raw roles/bigquery.dataEditor serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
}

# Чтение staging-бакета для разового bootstrap-джоба. Побакетно, не проектно.
resource "google_storage_bucket_iam_member" "ozon_ingestion_read_staging" {
  bucket = "evetis-ozon-staging-${var.project_number}"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ozon_ingestion.email}"
}

import {
  to = google_storage_bucket_iam_member.ozon_ingestion_read_staging
  id = "b/evetis-ozon-staging-${var.project_number} roles/storage.objectViewer serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
}
