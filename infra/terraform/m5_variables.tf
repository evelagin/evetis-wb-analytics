# M5 (перенос поставок WB и REF Sync из Apps Script). Флаги включения ресурсов
# wb_supplies_loader.tf и ref_sync_loader.tf. По умолчанию ВСЁ выключено: merge кода не меняет
# ни один plan, а отсутствие секрета WB_TOKEN_SUPPLIES не роняет чужие инфраструктурные задачи.
#
# Включение — GitHub Variable репозитория (общая схема F4, PR #423): infra.yml передаёт
#   TF_VAR_m5_shadow_enabled = vars.M5_SHADOW_ENABLED == 'true'
#   TF_VAR_m5_prod_enabled   = vars.M5_PROD_ENABLED   == 'true'
# Переменная не задана (или не 'true') → false. Default здесь НЕ меняется: флаг живёт в
# переменной, поэтому каждый запуск infra.yml видит одно значение, и нецелевой apply не
# уничтожит созданное. Локальный terraform вне infra.yml обязан передать те же TF_VAR_*.
# Порядок — docs/ops/MIG_SUPPLIES_REF_RUNBOOK.md §4:
#   S1: M5_SHADOW_ENABLED = true  (после создания секрета владельцем, ACK MIG-M5-SHADOW);
#   S4: M5_PROD_ENABLED   = true  (cutover, ACK MIG-M5-CUTOVER).
# Схема проверяется tools/tests/test_stream_enable_flags.py.
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
