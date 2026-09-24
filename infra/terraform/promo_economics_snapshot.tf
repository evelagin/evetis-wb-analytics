# ── PR-PROMO-3 · снимки канонического базиса экономики акций ─────────────────────
# Док: docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md §16–17
# SQL: sql/promotions/pr_promo3_basis_snapshot.sql
#   wb_mart.sp_snapshot_wb_promo_economics_basis   → wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT
#   ozon_mart.sp_snapshot_ozon_promo_economics_basis → ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT
#
# ЗАЧЕМ. Экономика акций считается из канонических форвардных вью (WB_FE_V1, Ozon
# FORWARD_MODELLED), а они считаются от «сейчас». Чтобы экономика прошлого наблюдения не
# переписывалась новым COGS или тарифом, на каждом слоте наблюдения акций канон копируется
# в append-only таблицу. Процедура идемпотентна по слоту, ничего не изменяет и не удаляет.
#
# МЕХАНИЗМ — прецедент executive_v2_layer.tf / ct_refresh.tf: Cloud Scheduler → BigQuery
# jobs API → CALL процедуры под отдельным SA. Образ не нужен. Маркетплейсы не вызываются.
#
# РАСПИСАНИЕ: :10 после каждого слота наблюдателей акций (04/09/14/19 UTC; WB-наблюдатель
# в :00 UTC, Ozon — 07/12/17/22 МСК = те же моменты). Слот процедура выводит сама из времени.
#
# ИЗОЛЯЦИЯ ПЛОЩАДОК — два SA, у каждого доступ только к своей площадке и evetis_ref:
#   dataViewer на датасеты, которые читает каноническая вью (проверено по её зависимостям);
#   dataEditor ПОТАБЛИЧНО только на свою таблицу снимков. Ни одна прочая таблица на запись
#   не выдаётся.
#
# APPLY — только целевой, с просмотренным планом. Таблицы снимков должны существовать ДО
# apply (создаются DDL развёртывания). Ожидаемый план: 2 SA + 2 jobUser + 6 dataset IAM +
# 2 table IAM + 2 scheduler + 2 actAs (iam.tf) = 16 to add, 0 to change, 0 to destroy.

locals {
  promo_econ = {
    wb = {
      account_id    = "sa-promo-econ-wb"
      display_name  = "EVETIS PR-PROMO-3 WB economics basis snapshot (PROD)"
      read_datasets = [var.mart_dataset, var.raw_dataset, "evetis_ref"]
      write_dataset = var.raw_dataset
      write_table   = "WB_PROMO_ECONOMICS_BASIS_SNAPSHOT"
      procedure     = "${var.mart_dataset}.sp_snapshot_wb_promo_economics_basis"
      job_name      = "promo-econ-basis-wb"
    }
    ozon = {
      account_id    = "sa-promo-econ-ozon"
      display_name  = "EVETIS PR-PROMO-3 Ozon economics basis snapshot (PROD)"
      read_datasets = ["ozon_mart", "ozon_raw", "evetis_ref"]
      write_dataset = "ozon_raw"
      write_table   = "OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT"
      procedure     = "ozon_mart.sp_snapshot_ozon_promo_economics_basis"
      job_name      = "promo-econ-basis-ozon"
    }
  }
  promo_econ_reads = merge([
    for k, v in local.promo_econ : { for ds in v.read_datasets : "${k}/${ds}" => { key = k, dataset = ds } }
  ]...)
}

resource "google_service_account" "promo_econ" {
  for_each     = local.promo_econ
  account_id   = each.value.account_id
  display_name = each.value.display_name
}

resource "google_project_iam_member" "promo_econ_job_user" {
  for_each = local.promo_econ
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.promo_econ[each.key].email}"
}

resource "google_bigquery_dataset_iam_member" "promo_econ_read" {
  for_each   = local.promo_econ_reads
  dataset_id = each.value.dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.promo_econ[each.value.key].email}"
}

resource "google_bigquery_table_iam_member" "promo_econ_write" {
  for_each   = local.promo_econ
  dataset_id = each.value.write_dataset
  table_id   = each.value.write_table
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.promo_econ[each.key].email}"
}

resource "google_cloud_scheduler_job" "promo_econ_snapshot" {
  for_each  = local.promo_econ
  name      = each.value.job_name
  region    = var.region
  schedule  = "10 4,9,14,19 * * *"
  time_zone = "Etc/UTC"

  paused = false

  attempt_deadline = "320s"

  # Повтор бесполезен: процедура идемпотентна по слоту, а пропуск слота виден проверкой E15.
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
          query        = "CALL `${each.value.procedure}`('scheduler')"
          useLegacySql = false
        }
        labels = {
          evetis-job = each.value.job_name
        }
      }
    }))
    oauth_token {
      service_account_email = google_service_account.promo_econ[each.key].email
      scope                 = "https://www.googleapis.com/auth/bigquery"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_service_account_iam_member.terraform_apply_actas,
  ]
}
