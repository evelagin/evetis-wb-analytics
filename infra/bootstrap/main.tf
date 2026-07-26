# Bootstrap: создаёт GCS-bucket для remote state ОСНОВНОГО модуля (infra/terraform).
# Свой state — локальный (это единственный ресурс до появления remote backend).
# Запускается ОДИН раз человеком-админом. См. docs/GCP_SETUP_CHECKLIST.md.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "europe-west1"
}
variable "state_bucket" {
  type        = string
  description = "Глобально-уникальное имя bucket'а для tfstate."
}

resource "google_storage_bucket" "tfstate" {
  name                        = var.state_bucket
  location                    = "EU"
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "state_bucket" {
  value = google_storage_bucket.tfstate.name
}

variable "terraform_plan_sa_email" {
  type        = string
  default     = ""
  description = "Email SA sa-terraform-plan (из terraform output основного модуля). Пусто на первичном bootstrap до создания SA."
}

variable "terraform_apply_sa_email" {
  type        = string
  default     = ""
  description = "Email SA sa-terraform-apply (из terraform output основного модуля). Пусто на первичном bootstrap до создания SA."
}

# Доступ Terraform-SA к объектам state-бакета (state r/w + .tflock). Bucket-level, НЕ project.
# Управляется здесь (bootstrap, запускает Owner), чтобы CI-identity НЕ рефрешил IAM бакета.
resource "google_storage_bucket_iam_member" "tfstate_plan" {
  count  = var.terraform_plan_sa_email == "" ? 0 : 1
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${var.terraform_plan_sa_email}"
}

resource "google_storage_bucket_iam_member" "tfstate_apply" {
  count  = var.terraform_apply_sa_email == "" ? 0 : 1
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${var.terraform_apply_sa_email}"
}
