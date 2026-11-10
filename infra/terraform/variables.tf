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

variable "mart_dataset" {
  type        = string
  default     = "wb_mart"
  description = "Датасет бизнес-витрины (FACT_*/MART_*/MART_RUNS). Пишет ТОЛЬКО prod-загрузчик mart."
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

variable "unitka_alert_email" {
  description = "UNITKA ENGINE v1: e-mail владельца для алертов о падении прогона. Пусто — алерт-политика не создаётся."
  type        = string
  default     = ""
}

# M4 (финансы WB): prod-контур wb-finance-prod (SA, права на боевые таблицы финансов, Job, invoker, Scheduler, алерт)
# создаётся ТОЛЬКО при true. По умолчанию false: нецелевой apply infra.yml до cutover prod не создаёт.
# Включение — docs/ops/MIG_FINANCE_RUNBOOK.md §5 (вечер cutover, отдельный ACK).
variable "finance_prod_enabled" {
  description = "M4: создать prod-контур финансов WB (wb-finance-prod). Только на cutover по ACK."
  type        = bool
  default     = false
}
