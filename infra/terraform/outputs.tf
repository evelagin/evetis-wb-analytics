output "workload_identity_provider" {
  description = "Значение для google-github-actions/auth (workload_identity_provider)."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}

output "terraform_plan_service_account" {
  value = google_service_account.terraform_plan.email
}

output "terraform_apply_service_account" {
  value = google_service_account.terraform_apply.email
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}"
}

output "runtime_service_accounts" {
  value = {
    loaders_shadow   = google_service_account.loaders_shadow.email
    loaders_prod     = google_service_account.loaders_prod.email
    scheduler_shadow = google_service_account.scheduler_shadow.email
    scheduler_prod   = google_service_account.scheduler_prod.email
  }
}
