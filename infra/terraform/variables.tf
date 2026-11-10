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

# M4 (финансы WB): единая схема включения контуров потоков M2–M5 (F4, #423) — ровно m4_shadow_enabled и
# m4_prod_enabled, bool, default false. Значения ставит infra.yml из GitHub Variables M4_{SHADOW,PROD}_ENABLED
# ('true' → true, не задана → false). По умолчанию нецелевой apply infra.yml не создаёт ни одного ресурса потока.
variable "m4_shadow_enabled" {
  description = "M4: создать shadow-контур финансов WB (5 таблиц __CR, sa-wb-finance-shadow, wb-finance-shadow, invoker, Scheduler). По ACK SHADOW потока (runbook §4)."
  type        = bool
  default     = false
}

# prod-контур wb-finance-prod (SA, права на боевые таблицы финансов, Job, invoker, Scheduler, алерт) — только на
# cutover. Включение — docs/ops/MIG_FINANCE_RUNBOOK.md §5 (вечер cutover, отдельный ACK).
variable "m4_prod_enabled" {
  description = "M4: создать prod-контур финансов WB (wb-finance-prod). Только на cutover по ACK."
  type        = bool
  default     = false
}
