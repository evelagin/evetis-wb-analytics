# Ozon runtime выделенного арендатора: свои SA, свои секреты (только контейнеры, без
# версий), свои job'ы на неизменяемом образе платформы; расписания без права вызова job'ов
# (ADR-08 И2) и дополнительно на паузе.
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
  # Идентичности выводятся детерминированно, а не из атрибутов создаваемых ресурсов:
  # тогда каждый member/service_account ИЗВЕСТЕН на этапе плана и сканер
  # (tools/tenancy/plan_scan.py) проверяет его точным сравнением, а не «known after apply».
  runtime_email   = "${var.ozon.service_accounts.runtime}@${var.project_id}.iam.gserviceaccount.com"
  scheduler_email = "${var.ozon.service_accounts.scheduler}@${var.project_id}.iam.gserviceaccount.com"
  runtime_member  = "serviceAccount:${local.runtime_email}"
}

# ── Идентичности ──────────────────────────────────────────────────────────────
# runtime видит данные и секреты арендатора. Планировщик в базовом провижининге
# НЕ ИМЕЕТ НИКАКИХ ролей: ни run.invoker, ни чего-либо ещё (модель H2 ниже).
resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.ozon.service_accounts.runtime
  display_name = "Ozon runtime (${var.tenant_id})"
  description  = "Исполняет job'ы Ozon арендатора. Ключей нет; доступ — ресурсные роли."
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = var.ozon.service_accounts.scheduler
  display_name = "Ozon scheduler (${var.tenant_id})"
  description  = "Без ролей до ворот активации: вызывать job'ы не может. run.invoker выдают только ворота активации."
}

# ── BigQuery: доступ к датасетам — НЕ здесь ─────────────────────────────────
# Весь ACL датасета задаётся авторитетно в самом google_bigquery_dataset (корень,
# main.tf, T3.3). Отдельные google_bigquery_dataset_iam_member перетирали бы его и
# оставляли создателя датасета (провижионера) OWNER'ом. Модуль лишь отдаёт email
# runtime SA (output runtime_email) — после создания SA.

# Задания BigQuery (загрузка, MERGE) запускаются от проекта. Роль — единственная в
# allow-list условного projectIamAdmin провижионера (T3.1B).
resource "google_project_iam_member" "runtime_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.runtime_member

  depends_on = [google_service_account.runtime]
}

# ── Секреты: только КОНТЕЙНЕРЫ и их IAM ──────────────────────────────────────
# Инвариант (ADR-08): Terraform никогда не управляет версиями секретов и их
# значениями. Ресурса google_secret_manager_secret_version здесь нет и быть не может
# (тест + сканер плана); значения вносятся вне Terraform и вне его state.
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
  for_each = var.ozon.secret_ids

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = local.runtime_member

  depends_on = [google_secret_manager_secret.ozon, google_service_account.runtime]
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
      service_account = local.runtime_email
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
    google_service_account.runtime,
    google_project_iam_member.runtime_job_user,
    google_secret_manager_secret_iam_member.runtime_access,
  ]
}

# ── Cloud Scheduler: существует, но ВЫЗВАТЬ job не может (модель H2) ─────────
# Провайдер создаёт задание Scheduler запросом create (API всегда создаёт ENABLED:
# поле state — output only), а паузу ставит ОТДЕЛЬНЫМ вызовом pause. Значит, между
# create и pause есть окно, а сбой pause оставит задание ENABLED. Поэтому паузу мы не
# считаем механизмом безопасности. Механизм безопасности — отсутствие права:
#   * у sa-ozon-scheduler в базовом провижининге нет run.invoker (и вообще ролей);
#   * у runtime SA нет run.jobs.run;
# сработавшее задание получает 403 от Cloud Run и загрузки не запускает. (Отдельно:
# сам провижионер на admin-ролях T3.1B run.jobs.run ИМЕЕТ — это его прямая
# возможность, не путь Scheduler; предлагаемая роль T3.3 её не содержит, P2-9.) paused = true
# остаётся вторым слоем (и тишиной в журналах), пустые секреты — третьим.
# run.invoker для sa-ozon-scheduler выдают ТОЛЬКО будущие ворота активации (T5/T6)
# после ключей, сверки и решения владельца. В этом корне такой привязки нет и сканер
# плана её отвергает.
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
    uri         = "${local.run_v2_base}/${each.key}:run"
    oauth_token {
      service_account_email = local.scheduler_email
    }
  }

  depends_on = [google_cloud_run_v2_job.this, google_service_account.scheduler]
}

# Email runtime SA для ACL датасетов в корне. Значение детерминировано (известно на
# плане, сканер сверяет его точно), зависимость — от СОЗДАНИЯ SA: BigQuery принимает в
# ACL только существующий аккаунт.
output "runtime_email" {
  value      = local.runtime_email
  depends_on = [google_service_account.runtime]
}

output "summary" {
  value = {
    runtime_sa   = local.runtime_email
    scheduler_sa = local.scheduler_email
    jobs         = sort(keys(google_cloud_run_v2_job.this))
    schedulers   = sort([for s in google_cloud_scheduler_job.this : s.name])
    secrets      = sort(keys(google_secret_manager_secret.ozon))
    paused       = { for k, s in google_cloud_scheduler_job.this : k => s.paused }
    images       = { for k, j in google_cloud_run_v2_job.this : k => j.template[0].template[0].containers[0].image }
    env          = { for k, j in google_cloud_run_v2_job.this : k => { for e in j.template[0].template[0].containers[0].env : e.name => e.value } }
    schedules    = { for k, s in google_cloud_scheduler_job.this : k => s.schedule }
  }
}
