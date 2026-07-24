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

# Terraform identity (infra.yml) — привилегированная, применяется только с approval.
resource "google_service_account" "terraform" {
  account_id   = "sa-terraform"
  display_name = "Terraform infra (infra.yml, approval-gated)"
}
