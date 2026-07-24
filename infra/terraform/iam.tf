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
# У deployer НЕТ secretAccessor и НЕТ Scheduler Admin.

# ── Terraform PLAN (read-only) ──
resource "google_project_iam_member" "terraform_plan_viewer" {
  project = var.project_id
  role    = "roles/viewer"
  member  = "serviceAccount:${google_service_account.terraform_plan.email}"
}
# Доступ к чтению backend-state bucket даётся на сам bucket (см. bootstrap/чек-лист).

# ── Terraform APPLY (привилегированный, main-only + approval) ──
locals {
  terraform_apply_roles = [
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
  # SA, которыми Terraform привязывает Job/Scheduler → нужен serviceAccountUser (actAs).
  terraform_actas_targets = {
    loaders_shadow   = google_service_account.loaders_shadow.name
    loaders_prod     = google_service_account.loaders_prod.name
    scheduler_shadow = google_service_account.scheduler_shadow.name
    scheduler_prod   = google_service_account.scheduler_prod.name
  }
}

resource "google_project_iam_member" "terraform_apply_roles" {
  for_each = toset(local.terraform_apply_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# Fix 5: apply SA может actAs на runtime/scheduler SAs (иначе падает iam.serviceAccounts.actAs).
resource "google_service_account_iam_member" "terraform_apply_actas" {
  for_each           = local.terraform_actas_targets
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
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
