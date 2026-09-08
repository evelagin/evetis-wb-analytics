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
# СОСТОЯНИЕ TERRAFORM. Ни SA, ни его прежние проектные роли Terraform никогда не
#   принадлежали — в state их нет. import-блоки ниже принимают SA и целевые
#   привязки; снятые проектные роли в конфиге отсутствуют намеренно: их удаление
#   выполнено напрямую (apply заблокирован сетевым фильтром, см.
#   services/wb-communications/PROVENANCE.md §5), и повторно создавать их нечем.
# ============================================================================

resource "google_service_account" "ozon_ingestion" {
  account_id   = "sa-ozon-ingestion"
  display_name = "Ozon ingestion (Cloud Run jobs)"
}

resource "google_service_account" "ozon_scheduler" {
  account_id   = "sa-ozon-scheduler"
  display_name = "Ozon schedulers (Cloud Run invoker)"
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
