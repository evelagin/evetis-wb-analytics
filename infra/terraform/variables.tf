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

# M2: стартовый watermark пустых теневых таблиц RAW_WB_ORDERS__CR / RAW_WB_SALES_RETURNS__CR
# (форма API 'YYYY-MM-DDTHH:mm:ss'). Действует только в shadow и только пока в __CR нет API-строк;
# после первой записи загрузчик берёт MAX(last_change_date) самой __CR. Значение задаёт владелец
# в день старта паритета (runbook M2, «Старт shadow»). Пусто → пустая __CR = ERROR, как в Apps Script.
variable "statistics_shadow_orders_bootstrap_watermark" {
  type    = string
  default = ""
}

variable "statistics_shadow_sales_bootstrap_watermark" {
  type    = string
  default = ""
}

# M2: prod-контур загрузчиков statistics-api (SA, права, секрет, Job, invoker, Scheduler).
# false (по умолчанию) — prod не создаётся ни при каком apply. true — ТОЛЬКО в вечер переключения
# по ACK M2-CUTOVER (docs/ops/MIG_M2_STATISTICS_RUNBOOK.md §5).
variable "m2_prod_enabled" {
  type    = bool
  default = false
}
