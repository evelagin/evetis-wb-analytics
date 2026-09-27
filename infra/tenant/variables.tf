# Единственный вход корня — контракт реестра. Оператор его не пишет: его генерирует
# tools/tenancy/tenant_infra.py из `registry.py terraform-inputs <tenant_id>`.
# Проверки ниже — ВТОРАЯ граница, а не второй вывод имён: алгоритм имён живёт только
# в tools/tenancy/naming.py, здесь лишь отсекается то, что заведомо не арендатор.
variable "contract" {
  description = "Вывод registry.py terraform-inputs <tenant_id> (contract_version = 1)."
  type = object({
    contract_version    = number
    tenant_id           = string
    status              = string
    project_id          = string
    project_id_revision = number
    parent_folder       = string
    region              = string
    bq_location         = string
    labels              = map(string)
    state = object({
      bucket = string
      prefix = string
    })
    scheduler_state = string
    bootstrap_apis  = list(string)
    apis            = list(string)
    datasets        = map(string)
    tables = list(object({
      dataset_key              = string
      table_id                 = string
      schema_json              = string
      time_partitioning_type   = optional(string)
      time_partitioning_field  = optional(string)
      clustering               = list(string)
      require_partition_filter = bool
    }))
    marketplaces = object({
      ozon = optional(object({
        service_accounts = object({
          runtime   = string
          scheduler = string
        })
        secret_ids      = map(string)
        raw_dataset_key = string
        ref_dataset_key = string
        jobs = map(object({
          scheduler = string
          schedule  = string
          time_zone = string
          entities  = list(string)
          env       = map(string)
        }))
        runtime_image = optional(string)
      }))
    })
  })

  validation {
    condition     = var.contract.contract_version == 1
    error_message = "Неизвестная версия контракта реестра (ожидается 1)."
  }
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,30}$", var.contract.tenant_id))
    error_message = "tenant_id не соответствует контракту tenant.v1."
  }
  validation {
    # Пространство проектов арендаторов mpa-t-<slug> / mpa-t<r>-<slug>, slug = tenant_id с «_» → «-».
    condition = var.contract.project_id == format("mpa-t%s-%s",
      var.contract.project_id_revision == 1 ? "" : tostring(var.contract.project_id_revision),
    replace(var.contract.tenant_id, "_", "-"))
    error_message = "project_id не принадлежит пространству арендаторов mpa-t… этого tenant_id."
  }
  validation {
    condition     = !anytrue([for m in local.evetis_forbidden_markers : strcontains(jsonencode(var.contract), m)])
    error_message = "Контракт содержит идентификатор EVETIS (список — local.evetis_forbidden_markers)."
  }
  validation {
    condition     = var.contract.state.bucket == local.platform.state_bucket && var.contract.state.prefix == "tenants/${var.contract.tenant_id}"
    error_message = "state арендатора — только gs://<бакет платформы>/tenants/<tenant_id>."
  }
  validation {
    condition     = var.contract.parent_folder == "folders/${local.platform.tenants_folder_id}"
    error_message = "Родитель проекта арендатора — только папка tenants/ (folders/881419274207)."
  }
  validation {
    condition     = var.contract.region == local.platform.region && var.contract.bq_location == "EU"
    error_message = "Регион europe-west1 и BigQuery EU — единственная поддерживаемая география v1."
  }
  validation {
    # T4: состав датасетов выделенного арендатора фиксирован (tools/tenancy/naming.py).
    # Внутренние: ozon_raw, ref, ozon_mart, tenant_ops. Клиентский слой: analytics_share
    # (доступ клиента — T6; в этом корне клиентских участников нет и сканер их отвергает).
    condition     = toset(keys(var.contract.datasets)) == toset(["analytics_share", "ozon_mart", "ozon_raw", "ref", "tenant_ops"])
    error_message = "Датасеты арендатора — ровно ozon_raw, ref, ozon_mart, tenant_ops, analytics_share (naming.DEDICATED_DATASETS)."
  }
  validation {
    condition     = alltrue([for t in var.contract.tables : contains(keys(var.contract.datasets), t.dataset_key)])
    error_message = "Каждая таблица контракта обязана принадлежать объявленному датасету."
  }
  validation {
    condition     = var.contract.scheduler_state == "PAUSED"
    error_message = "До ворот активации расписания арендатора обязаны быть PAUSED."
  }
  validation {
    # Образ: только неизменяемый digest в реестре платформы. Нет образа — нет плана.
    condition = var.contract.marketplaces.ozon == null ? true : can(regex(
      "^europe-west1-docker\\.pkg\\.dev/mpa-platform/mpa-runtime/[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}$",
    coalesce(var.contract.marketplaces.ozon.runtime_image, "<нет утверждённого образа>")))
    error_message = "Образ runtime: только europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/<image>@sha256:<64 hex> из infra/tenant/runtime_release.json. Теги (в т.ч. latest), пустая ссылка и реестр EVETIS запрещены."
  }
}
