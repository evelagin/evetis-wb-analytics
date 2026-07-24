variable "project_id" {
  type        = string
  description = "GCP project id (EVETIS analytics)."
}

variable "project_number" {
  type        = string
  description = "GCP project number (для WIF principalSet)."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "Регион Cloud Run/Artifact Registry (входит в географию BigQuery EU)."
}

variable "bq_location" {
  type        = string
  default     = "EU"
  description = "Location датасета/задач BigQuery. Должен совпадать с location датасета."
}

variable "raw_dataset" {
  type        = string
  default     = "wb_raw"
  description = "Датасет с RAW/manifest таблицами."
}

variable "artifact_repo" {
  type        = string
  default     = "wb-loaders"
  description = "Имя репозитория Artifact Registry (Docker)."
}

variable "github_repo" {
  type        = string
  description = "GitHub-репозиторий в формате owner/name (для WIF-условия)."
}

variable "container_image" {
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
  description = "Bootstrap-образ для создания Job. CI продвигает реальный immutable digest (prod образ не пересобирается)."
}
