# Runtime identities (права контейнера во время выполнения) — раздельно shadow/prod.
resource "google_service_account" "loaders_shadow" {
  account_id   = "sa-loaders-shadow"
  display_name = "WB loaders runtime (SHADOW)"
}

resource "google_service_account" "loaders_prod" {
  account_id   = "sa-loaders-prod"
  display_name = "WB loaders runtime (PROD)"
}

# Scheduler identities (право ЗАПУСТИТЬ Job) — отдельно от runtime.
resource "google_service_account" "scheduler_shadow" {
  account_id   = "sa-scheduler-shadow"
  display_name = "Cloud Scheduler invoker (SHADOW)"
}

resource "google_service_account" "scheduler_prod" {
  account_id   = "sa-scheduler-prod"
  display_name = "Cloud Scheduler invoker (PROD)"
}

# Deploy identity (GitHub Actions через WIF) — деплоит Job, НЕ читает значения токенов.
resource "google_service_account" "deployer" {
  account_id   = "sa-deployer"
  display_name = "GitHub Actions deployer (WIF)"
}

# Terraform PLAN (read-only) — доступен из PR-workflow, менять ресурсы НЕ может.
resource "google_service_account" "terraform_plan" {
  account_id   = "sa-terraform-plan"
  display_name = "Terraform plan (read-only, PR)"
}

# Terraform APPLY (привилегированный) — только из main + environment infra + approval.
resource "google_service_account" "terraform_apply" {
  account_id   = "sa-terraform-apply"
  display_name = "Terraform apply (privileged, main-only)"
}
