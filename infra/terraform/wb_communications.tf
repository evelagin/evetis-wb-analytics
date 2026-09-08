# ============================================================================
# Stage A / A2 — принятие сервиса коммуникаций WB под Terraform.
#
# ЧТО ЭТО. evetis-wb-communications — единственный во всей системе EVETIS
#   канал ЗАПИСИ во внешний маркетплейс: он публикует ответы на отзывы и вопросы
#   покупателей в Wildberries. До Stage A ресурсы создавались вручную через
#   `gcloud run deploy`, исходников в Git не было, Terraform ими не управлял
#   (находка F-01, CRITICAL, аудит 2026-09-08).
#
# 🔴 ЭТО ADOPTION, А НЕ СОЗДАНИЕ. Все три ресурса УЖЕ СУЩЕСТВУЮТ в production и
#   работают. Блоки `import` ниже говорят Terraform принять их в state, а не
#   создавать заново. Пересоздание сервиса означало бы новый URL, потерю ревизии
#   00025-rq8 и остановку публикации.
#
# 🔴 APPLY В STAGE A НЕ ВЫПОЛНЯЛСЯ. `terraform plan` с рабочей машины падает:
#   провайдер Google ходит на bigquery.googleapis.com, а этот хост фильтруется
#   на сетевом уровне (HTTP 403 от Google frontend даже без аутентификации).
#   Доказать «no destroy / no replacement» локально невозможно, а принимать
#   ресурсы вслепую запрещено.
#   ПОРЯДОК ЗАВЕРШЕНИЯ: .github/workflows/infra.yml с action=plan → убедиться,
#   что план не содержит destroy и forces replacement → только затем apply.
#
# ЧТО TERRAFORM НАМЕРЕННО НЕ КОНТРОЛИРУЕТ.
#   1. Содержимое template сервиса (образ, переменные, ресурсы). Ими управляет
#      деплой, и ровно так же сделано для загрузчиков в cloud_run_jobs.tf.
#      Ревизии 11…25 меняли ТОЛЬКО переменные окружения; если Terraform начнёт
#      их выравнивать, он выключит публикацию в WB — расхождение разобрано в
#      services/wb-communications/PROVENANCE.md §2.
#   2. Заголовок X-Scheduler-Secret у планировщика. Это разделяемый секрет, его
#      значение не должно попадать в репозиторий и в state-diff. Terraform его
#      не читает и не переписывает.
#   3. Состояние paused у планировщика — как и у всех остальных расписаний.
# ============================================================================

# ── Service account сервиса ─────────────────────────────────────────────────
resource "google_service_account" "wb_comms" {
  account_id   = "evetis-wb-comms"
  display_name = "EVETIS WB communications service"
}

import {
  to = google_service_account.wb_comms
  id = "projects/${var.project_id}/serviceAccounts/evetis-wb-comms@${var.project_id}.iam.gserviceaccount.com"
}

# Роли проектного уровня, выданные вручную при создании сервиса.
# Объявлены как есть — Stage A фиксирует факт, а не переописывает модель прав.
# Сужение до ресурсного уровня — отдельное решение (см. отчёт Stage A, F-19).
resource "google_project_iam_member" "wb_comms_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.wb_comms.email}"
}

resource "google_project_iam_member" "wb_comms_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.wb_comms.email}"
}

import {
  to = google_project_iam_member.wb_comms_bq_job_user
  id = "${var.project_id} roles/bigquery.jobUser serviceAccount:evetis-wb-comms@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = google_project_iam_member.wb_comms_datastore_user
  id = "${var.project_id} roles/datastore.user serviceAccount:evetis-wb-comms@${var.project_id}.iam.gserviceaccount.com"
}

# ── Cloud Run Service ───────────────────────────────────────────────────────
# Образ зафиксирован тем digest, который реально исполняется (сборка
# e7f0e35c от 2026-07-23). Он же указан в services/wb-communications/PROVENANCE.md.
resource "google_cloud_run_v2_service" "wb_communications" {
  name                = "evetis-wb-communications"
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.wb_comms.email
    containers {
      image = "europe-west1-docker.pkg.dev/${var.project_id}/cloud-run-source-deploy/evetis-wb-communications@sha256:0d33bb926944c5a1ef958ce46dfa9198b126de895948d6e4647ec1a94425bca5"
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  # Весь template — зона ответственности деплоя, не Terraform. Без этого
  # Terraform на первом же apply выровнял бы переменные окружения по своему
  # (пустому) представлению и остановил бы публикацию в Wildberries.
  lifecycle {
    ignore_changes = [
      template,
      client,
      client_version,
      traffic,
    ]
  }
  depends_on = [google_project_service.enabled]
}

import {
  to = google_cloud_run_v2_service.wb_communications
  id = "projects/${var.project_id}/locations/${var.region}/services/evetis-wb-communications"
}

# 🔴 Сервис ОТКРЫТ В ИНТЕРНЕТ: roles/run.invoker выдан allUsers, ingress=all.
#   Единственная защита /poll — статический заголовок X-Scheduler-Secret.
#   Это фактическое состояние production, и Stage A его НЕ меняет: перевод на
#   IAM-аутентификацию остановил бы планировщик до перенастройки его OIDC.
#   Зафиксировано как находка F-18 отчёта Stage A с планом устранения.
resource "google_cloud_run_v2_service_iam_member" "wb_communications_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.wb_communications.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

import {
  to = google_cloud_run_v2_service_iam_member.wb_communications_public
  id = "projects/${var.project_id}/locations/${var.region}/services/evetis-wb-communications roles/run.invoker allUsers"
}

# ── Cloud Scheduler ─────────────────────────────────────────────────────────
resource "google_cloud_scheduler_job" "wb_comms_poll" {
  name             = "evetis-wb-poll"
  region           = var.region
  schedule         = "0 8,11,14,17,20 * * *"
  time_zone        = "Europe/Moscow"
  attempt_deadline = "180s"

  retry_config {
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }

  http_target {
    http_method = "POST"
    uri         = "https://evetis-wb-communications-${var.project_number}.${var.region}.run.app/poll"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }
  }

  # headers содержит X-Scheduler-Secret. Значение живёт в Secret Manager
  # (EVETIS_SCHEDULER_SECRET) и в конфигурации планировщика; в репозиторий оно
  # не попадает и Terraform его не трогает.
  lifecycle {
    ignore_changes = [
      paused,
      http_target[0].headers,
    ]
  }
  depends_on = [google_project_service.enabled]
}

import {
  to = google_cloud_scheduler_job.wb_comms_poll
  id = "projects/${var.project_id}/locations/${var.region}/jobs/evetis-wb-poll"
}
