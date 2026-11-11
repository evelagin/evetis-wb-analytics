# Серверный исполнитель пакетов выпуска и репетиций Heldi (client_001) — отдельный корень Terraform.
#
# Зачем: исполнители пакетов (~/heldi-work/package-*) и репетиции шли часами на ноутбуке владельца,
# умирали при перезапуске сессии Claude и сне машины, а запросы с домашнего IP упирались в HTTP 403
# защиты Google от частых запросов. Здесь — два Cloud Run Job в проекте арендатора, которые исполняют
# ТОЧНЫЙ образ пакета (по digest) под выделенными SA, и бакет с журналом только на добавление.
#
# Отдельно от infra/tenant и infra/tenant-monitoring: apply и destroy этого корня не трогают
# контроллер, его Scheduler, датасеты, ACL и оповещения. Регламент: docs/ops/HELDI_RELEASE_RUNNER.md.
#
# State: gs://mpa-platform-tfstate-777428383056/ops/tenants/<tenant_id>/release-runner
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.20.0, < 8.0.0"
    }
  }
  backend "gcs" {}
}

# Квота и биллинг вызовов — на проект арендатора, как в infra/tenant.
provider "google" {
  project               = var.project_id
  user_project_override = true
  billing_project       = var.project_id
}
