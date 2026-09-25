# Ozon runtime выделенного арендатора: свои SA, свои секреты (только контейнеры, без
# версий), свои job'ы на неизменяемом образе платформы, расписания — PAUSED.
# Ни одной ссылки на EVETIS: проект, датасеты, секреты и SA — только арендатора.
terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "tenant_id" {
  type = string
}

variable "raw_dataset" {
  type = string
}

variable "ref_dataset" {
  type = string
}

variable "ozon" {
  type = object({
    service_accounts = object({
      runtime   = string
      scheduler = string
    })
    secret_ids      = map(string)
    raw_dataset_key = string
    ref_dataset_key = string
    jobs = map(object({
      scheduler = string
      schedule  = string
      time_zone = string
      entities  = list(string)
      env       = map(string)
    }))
    runtime_image = optional(string)
  })
}

locals {
  run_v2_base = "https://${var.region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs"
}

# ── Идентичности: runtime видит данные и секреты, планировщик — только вызывает job ──
resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.ozon.service_accounts.runtime
  display_name = "Ozon runtime (${var.tenant_id})"
  description  = "Исполняет job'ы Ozon арендатора. Ключей нет; доступ — ресурсные роли."
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = var.ozon.service_accounts.scheduler
  display_name = "Ozon scheduler invoker (${var.tenant_id})"
  description  = "Только run.invoker на job'ы Ozon арендатора."
}

# ── BigQuery: права на датасеты, а не на проект ─────────────────────────────
resource "google_bigquery_dataset_iam_member" "runtime_raw_editor" {
  project    = var.project_id
  dataset_id = var.raw_dataset
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_bigquery_dataset_iam_member" "runtime_ref_viewer" {
  project    = var.project_id
  dataset_id = var.ref_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

# Задания BigQuery (загрузка, MERGE) запускаются от проекта. Роль — единственная в
# allow-list условного projectIamAdmin провижионера (T3.1B).
resource "google_project_iam_member" "runtime_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# ── Секреты: только контейнеры. Версий (значений) Terraform не создаёт никогда ───
resource "google_secret_manager_secret" "ozon" {
  for_each = var.ozon.secret_ids

  project   = var.project_id
  secret_id = each.value
  labels    = { marketplace = "ozon", secret_role = replace(each.key, "_", "-") }

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each = google_secret_manager_secret.ozon

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# ── Cloud Run jobs ───────────────────────────────────────────────────────────
# Окружение целиком из контракта (GCP_PROJECT_ID, датасеты, ИМЕНА секретов, ENTITIES,
# STRICT_PAGE_CAPS=1). Runtime T2+ без GCP_PROJECT_ID отказывает (fail-closed), так
# что откатиться на проект EVETIS он не может.
resource "google_cloud_run_v2_job" "this" {
  for_each = var.ozon.jobs

  project             = var.project_id
  name                = each.key
  location            = var.region
  deletion_protection = true
  labels              = { marketplace = "ozon", tenant = var.tenant_id }

  template {
    labels = { marketplace = "ozon", tenant = var.tenant_id }
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "3600s"
      containers {
        image = var.ozon.runtime_image
        dynamic "env" {
          for_each = each.value.env
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = {
            cpu    = "1000m"
            memory = "2Gi"
          }
        }
      }
    }
  }

  depends_on = [
    google_bigquery_dataset_iam_member.runtime_raw_editor,
    google_bigquery_dataset_iam_member.runtime_ref_viewer,
    google_project_iam_member.runtime_job_user,
    google_secret_manager_secret_iam_member.runtime_access,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke" {
  for_each = google_cloud_run_v2_job.this

  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# ── Cloud Scheduler: ВСЕГДА на паузе ────────────────────────────────────────
# paused = true зашит, а не параметр: только что созданный арендатор не может начать
# загрузку сам. Снятие паузы — отдельные ворота жизненного цикла (не этот корень).
resource "google_cloud_scheduler_job" "this" {
  for_each = var.ozon.jobs

  project   = var.project_id
  name      = each.value.scheduler
  region    = var.region
  schedule  = each.value.schedule
  time_zone = each.value.time_zone
  paused    = true

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.run_v2_base}/${google_cloud_run_v2_job.this[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoke]
}

output "summary" {
  value = {
    runtime_sa   = google_service_account.runtime.email
    scheduler_sa = google_service_account.scheduler.email
    jobs         = sort(keys(google_cloud_run_v2_job.this))
    schedulers   = sort([for s in google_cloud_scheduler_job.this : s.name])
    secrets      = sort(keys(google_secret_manager_secret.ozon))
    paused       = { for k, s in google_cloud_scheduler_job.this : k => s.paused }
    images       = { for k, j in google_cloud_run_v2_job.this : k => j.template[0].template[0].containers[0].image }
    env          = { for k, j in google_cloud_run_v2_job.this : k => { for e in j.template[0].template[0].containers[0].env : e.name => e.value } }
    schedules    = { for k, s in google_cloud_scheduler_job.this : k => s.schedule }
  }
}
