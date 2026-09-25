# Tenancy T3.2 — корень Terraform выделенного арендатора (ADR-08, модель D).
#
# Один исходный код для всех арендаторов. Всё арендаторское приходит одной переменной
# `contract` — выводом `python tools/tenancy/registry.py terraform-inputs <tenant_id>`.
# Корень EVETIS (infra/terraform) отсюда не читается и сюда не читает; state у каждого
# арендатора свой: gs://mpa-platform-tfstate-777428383056/tenants/<tenant_id>.
# Workspaces не используются: изоляция — отдельным префиксом state, который выводит
# реестр (tools/tenancy/tenant_infra.py передаёт его в -backend-config).
#
# Проект арендатора создаёт и удаляет ЧЕЛОВЕК (OD-4, OD-10). Этот корень работает
# только внутри уже созданного проекта и ресурса google_project не содержит.
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

# Квота и биллинг вызовов API — на проект арендатора, а не на mpa-platform:
# платформа не включает у себя API арендаторских сервисов и не платит за них.
provider "google" {
  project               = var.contract.project_id
  region                = var.contract.region
  user_project_override = true
  billing_project       = var.contract.project_id
  default_labels        = var.contract.labels
}
