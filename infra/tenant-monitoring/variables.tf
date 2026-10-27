variable "tenant_id" {
  description = "Идентификатор арендатора из реестра (client_001 = Heldi)."
  type        = string
  validation {
    condition     = can(regex("^client_[0-9]{3}$", var.tenant_id))
    error_message = "Только выделенный арендатор client_NNN; EVETIS сюда не входит."
  }
}

variable "project_id" {
  description = "Проект арендатора; выводится из tenant_id (naming.derive_project_id)."
  type        = string
  validation {
    condition     = can(regex("^mpa-t-client-[0-9]{3}$", var.project_id))
    error_message = "Только проект выделенного арендатора mpa-t-client-NNN."
  }
}

# Адрес не хранится в репозитории (он публичный): передаётся при apply через -var.
variable "alert_email" {
  description = "Почта владельца для оповещений."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Нужен адрес электронной почты."
  }
}

# Политика свежести включается ВТОРЫМ apply, не раньше чем через 24 ч после создания
# лог-метрики: метрика не заполняется задним числом, и до этого все сущности выглядели бы
# «устаревшими». Перед включением чтением проверяется, что у работающих сущностей есть точки.
variable "freshness_enabled" {
  description = "Включить политику свежести данных по сущностям."
  type        = bool
  default     = false
}

# Сущности, которые текущий контур обязан сертифицировать каждые сутки МСК
# (tools/tenancy/current_policy.py ORDER). Реклама и FBO останутся в инциденте,
# пока их загрузка не восстановлена: это правда о данных, а не ложная тревога.
variable "expected_entities" {
  description = "Сущности суточного контура, свежесть которых проверяется."
  type        = list(string)
  default = ["catalog", "stocks", "prices", "finance_accrual",
  "ads_campaigns", "ads_expense_daily", "ads_sku_daily", "fbo_postings"]
  validation {
    condition = length(var.expected_entities) > 0 && alltrue([for e in var.expected_entities : contains(
    ["catalog", "stocks", "prices", "finance_accrual", "ads_campaigns", "ads_expense_daily", "ads_sku_daily", "fbo_postings"], e)])
    error_message = "Только сущности суточного контура (current_policy.ORDER)."
  }
}

# Push-уведомления приложения Google Cloud (решение владельца 27.10: push + email).
# Канал создаёт само приложение после входа владельца и выбора проекта mpa-t-client-001
# (docs.cloud.google.com/monitoring/support/notification-options, «Google Cloud console Mobile App»);
# API/Terraform его не создают. Здесь — только имя уже существующего канала.
# null — как до 27.10: STOP и проверка доставки уходят только на почту.
variable "owner_push_channel" {
  description = "Канал мобильного приложения Google Cloud владельца (projects/<project>/notificationChannels/<id>)."
  type        = string
  default     = null
  validation {
    condition     = var.owner_push_channel == null || can(regex("^projects/${var.project_id}/notificationChannels/[0-9]+$", var.owner_push_channel))
    error_message = "Нужно полное имя канала уведомлений этого же проекта арендатора."
  }
}
