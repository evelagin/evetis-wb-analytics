variable "tenant_id" {
  description = "Идентификатор арендатора из реестра (client_001 = Heldi)."
  type        = string
  validation {
    condition     = var.tenant_id == "client_001"
    error_message = "Только Heldi (client_001): исполнитель квалифицирован под его контроллер. EVETIS сюда не входит."
  }
}

variable "project_id" {
  description = "Проект арендатора."
  type        = string
  validation {
    condition     = var.project_id == "mpa-t-client-001"
    error_message = "Только проект Heldi mpa-t-client-001."
  }
}

variable "region" {
  description = "Регион заданий (как у контроллера)."
  type        = string
  default     = "europe-west1"
  validation {
    condition     = var.region == "europe-west1"
    error_message = "Исполнитель живёт рядом с контроллером: europe-west1."
  }
}

# Образ пакета: Cloud Build tools/heldi_runner/cloudbuild.yaml (FROM python:3.12-slim@sha256, контекст —
# архив пакета с sha256 из ACK + git archive исходника исполнителя). Только digest, тег запрещён.
# null — задания не создаются (остаются SA, роли и журнал), так выглядит первый apply.
variable "exec_image" {
  description = "Образ исполнителя пакета выпуска heldi-release-runner@sha256 (пакет с правом записи)."
  type        = string
  default     = null
  validation {
    condition     = var.exec_image == null || can(regex("^europe-west1-docker\\.pkg\\.dev/mpa-platform/mpa-runtime/heldi-release-runner@sha256:[0-9a-f]{64}$", var.exec_image))
    error_message = "Только неизменяемый образ heldi-release-runner@sha256 из mpa-runtime."
  }
}

variable "rehearse_image" {
  description = "Образ репетиции heldi-release-runner@sha256 (тот же пакет, режим копии, только чтение production)."
  type        = string
  default     = null
  validation {
    condition     = var.rehearse_image == null || can(regex("^europe-west1-docker\\.pkg\\.dev/mpa-platform/mpa-runtime/heldi-release-runner@sha256:[0-9a-f]{64}$", var.rehearse_image))
    error_message = "Только неизменяемый образ heldi-release-runner@sha256 из mpa-runtime."
  }
}

# Пины пакета (проверяются ВНУТРИ образа при старте каждого исполнения, расхождение — exit 3 до первого запроса):
#   ack_sha256      — sha256 текста OWNER ACK, вшитого в образ (OWNER_ACK.txt);
#   package_sha256  — sha256 детерминированного архива каталога пакета (tar --sort=name --mtime=@0 --owner=0);
#   executor_source — коммит исходника, против которого пакет квалифицирован (CONTROLLER_SOURCE_SHA пакета).
variable "package_pins" {
  description = "Пины пакета: ack_sha256, package_sha256, executor_source, package (имя каталога пакета)."
  type = object({
    package         = string
    ack_sha256      = string
    package_sha256  = string
    executor_source = string
  })
  default = null
  validation {
    condition = var.package_pins == null || try(
      can(regex("^package-[a-z0-9-]{1,40}$", var.package_pins.package)) &&
      can(regex("^[0-9a-f]{64}$", var.package_pins.ack_sha256)) &&
      can(regex("^[0-9a-f]{64}$", var.package_pins.package_sha256)) &&
    can(regex("^[0-9a-f]{40}$", var.package_pins.executor_source)), false)
    error_message = "ack_sha256/package_sha256 — 64 hex, executor_source — 40 hex, package — package-<имя>."
  }
}

# Датасеты, в которые пакет выпуска пишет таблицы (журналы и маркеры контроллера). ozon_raw, ref,
# ozon_mart и analytics_share сюда не входят никогда: данные Ozon пишет только runtime.
variable "exec_write_datasets" {
  description = "Датасеты записи исполнителя пакета."
  type        = list(string)
  default     = ["tenant_ops", "tenant_locks"]
  validation {
    condition     = length(var.exec_write_datasets) > 0 && alltrue([for d in var.exec_write_datasets : contains(["tenant_ops", "tenant_locks"], d)])
    error_message = "Запись только в tenant_ops и tenant_locks."
  }
}

# Предел одного исполнения. Стадии пакета идут минуты; observe и репетиция — часы.
variable "exec_timeout" {
  description = "Таймаут задания исполнителя пакета."
  type        = string
  default     = "21600s"
}

variable "rehearse_timeout" {
  description = "Таймаут задания репетиции (Cloud Run Jobs: до 24 ч)."
  type        = string
  default     = "86400s"
}

# Срок хранения объектов журнала (политика хранения бакета, НЕ заблокирована: владелец может её снять).
variable "journal_retention_days" {
  description = "Сколько дней объекты журнала нельзя удалить или перезаписать."
  type        = number
  default     = 400
  validation {
    condition     = var.journal_retention_days >= 90 && var.journal_retention_days <= 3650
    error_message = "От 90 до 3650 дней."
  }
}
