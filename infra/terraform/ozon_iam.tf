# ============================================================================
# Stage A / A4 — права Ozon-загрузчика.
#
# 🔴 ПОПРАВКА К НАХОДКЕ F-04 (Stage A Closeout, 2026-09-08).
#   Аудит и первая редакция этого файла утверждали, что sa-ozon-ingestion имеет
#   roles/bigquery.dataEditor и dataViewer «на уровне всего проекта» и потому
#   может писать в wb_raw / wb_mart / wb_ops. ЭТО БЫЛО НЕВЕРНО.
#
#   Обе привязки — УСЛОВНЫЕ (IAM Conditions), и условия ровно те, что нужны:
#     dataEditor  условие ozon_raw_only
#                 resource.name.startsWith(".../datasets/ozon_raw")
#     dataViewer  условие evetis_ref_only
#                 resource.name.startsWith(".../datasets/evetis_ref")
#
#   Ошибка возникла из-за формы вывода:
#     gcloud projects get-iam-policy … --format="value(bindings.role)"
#   молча отбрасывает поле condition, и условная привязка выглядит как
#   безусловная. Проверять права нужно ТОЛЬКО по --format=json с чтением
#   bindings[].condition.
#
#   Вывод: изоляция маркетплейсов была реализована ДО Stage A и реализована
#   правильно — Ozon пишет только в свой RAW-слой и читает только общий
#   справочный слой evetis_ref. Это в точности правило проекта.
#
# ЧТО СДЕЛАЛ И ОТКАТИЛ STAGE A.
#   08.09.2026 в ACL датасета ozon_raw была добавлена запись WRITER для этого SA
#   как «шаг 1 сужения прав». На фоне уже существующего условного dataEditor она
#   была избыточной, а её обоснование — ошибочным, поэтому в Closeout запись
#   удалена: ACL ozon_raw вернулся в состояние до Stage A. Эффективные права SA
#   за весь этап не изменились ни разу.
#
# ДОКАЗАТЕЛЬСТВА ДОСТАТОЧНОСТИ ТЕКУЩИХ ПРАВ.
#   1. Определение: IAM-условие вычисляется самой системой IAM; роль физически
#      не применяется к ресурсу вне выражения условия.
#   2. Других путей доступа нет: ни в одном из семи датасетов проекта
#      (wb_raw, wb_mart, wb_ops, evetis_ref, ozon_raw, ozon_mart, ozon_stg)
#      нет записи ACL для этого SA; прочие проектные роли — только безусловный
#      bigquery.jobUser, который данных не даёт.
#   3. Эмпирика: INFORMATION_SCHEMA.JOBS_BY_PROJECT за 180 суток — 502 обращения
#      к таблицам, все в ozon_raw; 158 LOAD и 155 MERGE, ни одного обращения к
#      WB-датасетам.
#   4. Дымовой прогон 2026-09-08 12:11 UTC уже БЕЗ избыточной записи ACL:
#      ozon-runtime-fast завершился успешно, stocks 197 строк и fbo_postings
#      175 строк смёржены, errors = 0.
#
#   Прямая проверка через impersonation невозможна: у пользователя нет
#   roles/iam.serviceAccountTokenCreator на этом SA, а выдавать её ради проверки
#   означало бы расширить права, чего Stage A делать не должен.
#
# ЧТО НЕ ТРОГАЕМ. Доступ к GCS уже узкий: roles/storage.objectViewer выдан
#   ПОБАКЕТНО на gs://evetis-ozon-staging-37074083763 (нужен bootstrap-джобу
#   для load_table_from_uri). Проектной роли storage у SA нет.
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

# Безусловная роль. Право ЗАПУСКАТЬ job'ы; доступа к данным не даёт.
resource "google_project_iam_member" "ozon_ingestion_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ozon_ingestion.email}"
}

import {
  to = google_project_iam_member.ozon_ingestion_job_user
  id = "${var.project_id} roles/bigquery.jobUser serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com"
}

# 🔑 Запись — ТОЛЬКО в ozon_raw. Условие и есть механизм изоляции.
#   Загрузчик создаёт и удаляет временные таблицы _rt_* вокруг каждого MERGE
#   (pipelines/ozon/runtime/common.py:146-186), поэтому нужен именно dataEditor,
#   а не что-то слабее. Условие ограничивает его одним датасетом.
resource "google_project_iam_member" "ozon_ingestion_edit_ozon_raw" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.ozon_ingestion.email}"

  condition {
    title       = "ozon_raw_only"
    description = "Запись только в датасет ozon_raw и его таблицы"
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/datasets/ozon_raw\")"
  }
}

import {
  to = google_project_iam_member.ozon_ingestion_edit_ozon_raw
  id = "${var.project_id} roles/bigquery.dataEditor serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com ozon_raw_only"
}

# 🔑 Чтение — ТОЛЬКО общий справочный слой. Это ровно то, что разрешает правило
#   изоляции маркетплейсов: единственный общий домен — evetis_ref, на чтение.
#   За 180 суток обращений не зафиксировано, но грант объявлен намеренно и
#   описывает разрешённую зависимость; сужать дальше нечего.
resource "google_project_iam_member" "ozon_ingestion_read_evetis_ref" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.ozon_ingestion.email}"

  condition {
    title       = "evetis_ref_only"
    description = "Чтение только общего справочного слоя"
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/datasets/evetis_ref\")"
  }
}

import {
  to = google_project_iam_member.ozon_ingestion_read_evetis_ref
  id = "${var.project_id} roles/bigquery.dataViewer serviceAccount:sa-ozon-ingestion@${var.project_id}.iam.gserviceaccount.com evetis_ref_only"
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
