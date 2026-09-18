# ── UNITKA COGS PUBLICATION V1 · физическая копия канонического COGS для Integrity Guard ──────────
# Док: docs/UNITKA_INTEGRITY_GUARD_V1.md §6. SQL: sql/unitka/cogs_publication_v1.sql
# (wb_mart.UNITKA_COGS_EFFECTIVE, UNITKA_COGS_PUBLISH_LOG, _UNITKA_COGS_PUBLISH_LOCK, sp_publish_unitka_cogs).
#
# ЗАЧЕМ. Runtime-учётки Unitka (sa-loaders-*) НЕ получают доступ к evetis_ref (решение владельца 1C1B/1C2A).
# Канон читает отдельная учётка sa-unitka-cogs-pub и публикует ФИЗИЧЕСКУЮ копию в wb_mart, которую
# sa-loaders-* уже читают штатным доступом к wb_mart. Истина остаётся в evetis_ref.
#
# МЕХАНИЗМ — прецедент executive_v2_layer.tf / sku_performance_v2_layer.tf: Cloud Scheduler → BigQuery
# jobs API → `CALL wb_mart.sp_publish_unitka_cogs('scheduler')`. Образ не нужен.
#
# РАСПИСАНИЕ: :50 каждого часа, 07:50–23:50 МСК (17 запусков). Минута :50 свободна: :00/:30 заняты
#   Unitka/витриной/воронкой/Ozon, :10 — Executive V2, :20 — SKU V2 и наблюдатель цен, :40 — Control Tower
#   и наблюдатель цен, :45 — хранение, :15 — тарифы. 09:50 даёт свежую копию утреннему окну Unitka 10:00.
#   Расписание Unitka от публикации НЕ зависит и наоборот. Штатная свежесть ≈ 1 ч; 26 ч — ПОРОГ
#   безопасности COGS_SNAPSHOT_STALE, а не целевой SLA. Стоимость ничтожна (~40 строк канона).
#
# ОТКАЗ: ASSERT P1–P8 или ошибка в транзакции → ROLLBACK, копия от последней удачной публикации,
#   UNITKA_COGS_PUBLISH_LOG = FAILED. Процедура таблиц не создаёт. retry_count = 0 (замок отсёк бы дубль).
#
# ПРАВА — минимально необходимые (карта операций → права — в доке §6):
#   jobUser (проект)                          — bigquery.jobs.create (запуск CALL; TEMP-staging в сессии скрипта);
#   dataViewer на evetis_ref (датасет)        — V_PRODUCT_COGS_EFFECTIVE — логическая вью над несколькими
#       таблицами evetis_ref; вью требует getData и на базовых таблицах, поэтому уже датасета нельзя.
#       Тип ресурса — google_bigquery_dataset_iam_member, КАК У ВСЕХ ТЕКУЩИХ ЧИТАТЕЛЕЙ evetis_ref
#       (ct_refresh, exec_v2_layer, sku_v2_layer): смешения с google_bigquery_dataset_access нет;
#   dataViewer на ОДНУ процедуру (routine IAM) — bigquery.routines.get (право вызвать CALL), без датасета wb_mart;
#   dataEditor ПОТАБЛИЧНО на три таблицы      — getData/updateData для DELETE+INSERT, UPDATE замка и журнала.
#       Ни одна другая таблица wb_mart на запись не выдаётся, dataset-wide wb_mart dataEditor не выдаётся.
#   sa-loaders-* здесь НЕ упоминаются: доступа к evetis_ref они не получают.
#
# APPLY — только ЦЕЛЕВОЙ, с просмотренным планом, ПОСЛЕ SQL-миграции §1–§2 (routine/table IAM требуют
#   существующих объектов). Ожидаемый план: 1 SA + 1 project IAM + 1 dataset IAM + 1 routine IAM +
#   3 table IAM + 1 actAs (iam.tf) + 1 scheduler = 9 to add, 0 to change, 0 to destroy.

resource "google_service_account" "unitka_cogs_pub" {
  account_id   = "sa-unitka-cogs-pub"
  display_name = "EVETIS Unitka COGS publication (evetis_ref -> wb_mart.UNITKA_COGS_EFFECTIVE)"
}

resource "google_project_iam_member" "unitka_cogs_pub_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.unitka_cogs_pub.email}"
}

resource "google_bigquery_dataset_iam_member" "unitka_cogs_pub_read_ref" {
  dataset_id = "evetis_ref"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.unitka_cogs_pub.email}"
}

resource "google_bigquery_routine_iam_member" "unitka_cogs_pub_call" {
  project    = var.project_id
  dataset_id = var.mart_dataset
  routine_id = "sp_publish_unitka_cogs"
  # dataViewer на уровне ОДНОЙ процедуры = bigquery.routines.get (у процедуры нет данных). Первый плановый
  # CALL в 1C2B подтверждает достаточность; запасной вариант — metadataViewer на датасет wb_mart.
  role   = "roles/bigquery.dataViewer"
  member = "serviceAccount:${google_service_account.unitka_cogs_pub.email}"
}

locals {
  # Таблицы, которые пишет sp_publish_unitka_cogs (DELETE/INSERT/UPDATE). Создаются SQL-миграцией.
  unitka_cogs_pub_write_tables = toset([
    "UNITKA_COGS_EFFECTIVE",
    "UNITKA_COGS_PUBLISH_LOG",
    "_UNITKA_COGS_PUBLISH_LOCK",
  ])
}

resource "google_bigquery_table_iam_member" "unitka_cogs_pub_write" {
  for_each   = local.unitka_cogs_pub_write_tables
  dataset_id = var.mart_dataset
  table_id   = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.unitka_cogs_pub.email}"
}

resource "google_cloud_scheduler_job" "unitka_cogs_publication" {
  name      = "unitka-cogs-publication"
  region    = var.region
  schedule  = "50 7-23 * * *"
  time_zone = "Europe/Moscow"

  paused = false

  # Публикация — секунды; jobs.insert возвращает управление сразу после постановки скрипта.
  attempt_deadline = "320s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://bigquery.googleapis.com/bigquery/v2/projects/${var.project_id}/jobs"
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({
      configuration = {
        query = {
          query        = "CALL `${var.mart_dataset}.sp_publish_unitka_cogs`('scheduler')"
          useLegacySql = false
        }
        labels = {
          evetis-job = "unitka-cogs-publication"
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.unitka_cogs_pub.email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
  ]
}
