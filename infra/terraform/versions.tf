terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
  # Remote state в GCS. Bucket создаётся ЗАРАНЕЕ (infra/bootstrap или чек-лист).
  # Конфиг передаётся при init: -backend-config="bucket=..." -backend-config="prefix=..."
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
