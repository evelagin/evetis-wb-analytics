# M5 (перенос поставок WB и REF Sync из Apps Script). Флаги включения ресурсов
# wb_supplies_loader.tf и ref_sync_loader.tf. По умолчанию ВСЁ выключено: merge кода не меняет
# ни один plan, а отсутствие секрета WB_TOKEN_SUPPLIES не роняет чужие инфраструктурные задачи.
#
# Включение — PR, меняющий default (а не `-var` на одном запуске: следующий нецелевой apply без
# той же переменной уничтожил бы созданное). Порядок — docs/ops/MIG_SUPPLIES_REF_RUNBOOK.md §4:
#   S1: m5_shadow_enabled = true  (после создания секрета владельцем);
#   S4: m5_prod_enabled   = true  (cutover, ACK MIG-M5-CUTOVER).
variable "m5_shadow_enabled" {
  description = "M5: shadow-контур поставок и REF Sync (__CR-таблицы, SA, Job, Scheduler PAUSED)."
  type        = bool
  default     = false
}

variable "m5_prod_enabled" {
  description = "M5: prod-контур поставок и REF Sync (SA, IAM на боевые таблицы, Job, Scheduler PAUSED)."
  type        = bool
  default     = false
}
