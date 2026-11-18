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

# M2: флаги контуров загрузчиков statistics-api — единая схема M2–M5 (mN_{shadow,prod}_enabled, bool,
# default false). В infra.yml: TF_VAR_m2_*_enabled = vars.M2_*_ENABLED == 'true' (не задана → false).
# shadow-контур (SA, права, секрет, таблицы __CR, Job, invoker, Scheduler). false (по умолчанию) —
# shadow не создаётся ни при каком apply. true — по ACK M2-SHADOW (docs/ops/MIG_M2_STATISTICS_RUNBOOK.md §4).
variable "m2_shadow_enabled" {
  type    = bool
  default = false
}

# prod-контур (SA, права, секрет, Job, invoker, Scheduler). false (по умолчанию) — prod не создаётся
# ни при каком apply. true — ТОЛЬКО в вечер переключения по ACK M2-CUTOVER (runbook §5).
variable "m2_prod_enabled" {
  type    = bool
  default = false
}
