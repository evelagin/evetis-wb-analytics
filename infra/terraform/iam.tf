# ── Deployer (GitHub Actions): деплоит Job, НЕ читает значения токенов ──
resource "google_project_iam_member" "deployer_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_ar" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Право деплоить Job, работающий КАК runtime SA (impersonation при деплое).
resource "google_service_account_iam_member" "deployer_actas_shadow" {
  service_account_id = google_service_account.loaders_shadow.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_actas_prod" {
  service_account_id = google_service_account.loaders_prod.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
# ВНИМАНИЕ: у deployer НЕТ roles/secretmanager.secretAccessor и НЕТ Scheduler Admin.

# ── Terraform identity (infra.yml, approval-gated). Первый apply — человек-админ. ──
locals {
  terraform_roles = [
    "roles/run.admin",
    "roles/cloudscheduler.admin",
    "roles/artifactregistry.admin",
    "roles/secretmanager.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/bigquery.admin",
  ]
}

resource "google_project_iam_member" "terraform_roles" {
  for_each = toset(local.terraform_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.terraform.email}"
}

# ── Runtime loaders: BigQuery Job User (project) + доступ к секретам ──
resource "google_project_iam_member" "loaders_shadow_jobuser" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_project_iam_member" "loaders_prod_jobuser" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# Оба runtime читают значения WB-секретов (нужны для вызова WB API).
resource "google_secret_manager_secret_iam_member" "shadow_secret_access" {
  for_each  = google_secret_manager_secret.wb
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_secret_manager_secret_iam_member" "prod_secret_access" {
  for_each  = google_secret_manager_secret.wb
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Scheduler identities: право ЗАПУСТИТЬ конкретный Job ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_stocks_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_stocks_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}
