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
    orchestration = optional(object({
      accounts       = map(object({ id = string, email = string }))
      roles          = map(list(string))
      dataset_grants = list(object({ dataset_key = string, role = string, email = string }))
      matrix         = list(object({ principal = string, role = string, resource = string, permissions = list(string) }))
      job            = object({ name = string, image = string, env = map(string), timeout = string })
      scheduler      = object({ name = string, schedule = string, time_zone = string, state = string, uri = string })
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
    sql_deployer = object({
      account_id = string
      email      = string
      grants = list(object({
        dataset_key = string
        role        = string
        condition = optional(object({
          title       = string
          description = string
          expression  = string
        }))
      }))
    })
    # T5 (D1): control plane арендатора. null — площадок нет (control обслуживает runtime Ozon).
    control = optional(object({
      account_id = string
      email      = string
      grants = list(object({
        dataset_key = string
        role        = string
        condition = optional(object({
          title       = string
          description = string
          expression  = string
        }))
      }))
      job = object({
        name = string
        env  = map(string)
      })
    }))
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
    condition     = toset(keys(var.contract.datasets)) == toset(["analytics_share", "ozon_mart", "ozon_raw", "ref", "tenant_locks", "tenant_ops"])
    error_message = "Датасеты арендатора — ровно ozon_raw, ref, ozon_mart, tenant_ops, analytics_share, tenant_locks (naming.DEDICATED_DATASETS)."
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
    # T4.1: SA деплоера SQL — только в проекте арендатора, имя фиксировано платформой.
    condition = (var.contract.sql_deployer.account_id == local.platform.sql_deployer_account_id &&
    var.contract.sql_deployer.email == "${local.platform.sql_deployer_account_id}@${var.contract.project_id}.iam.gserviceaccount.com")
    error_message = "SQL-деплоер: только sa-sql-deployer@<проект арендатора>."
  }
  validation {
    # Ровно утверждённая матрица: только роли mpaSql* организации, только датасеты матрицы, без
    # повторов; изменение в tenant_ops — только условное (без условия дало бы менять/«истекать»
    # 7 таблиц платформы), лишнего гранта (например, изменение в ozon_raw) нет.
    condition = length(var.contract.sql_deployer.grants) == length(local.platform.sql_grant_matrix) && toset([
      for g in var.contract.sql_deployer.grants :
      "${g.dataset_key}|${trimprefix(g.role, "organizations/${local.platform.organization_id}/roles/")}|${g.condition != null}"
    ]) == toset(local.platform.sql_grant_matrix)
    error_message = "SQL-деплоер: гранты не равны утверждённой матрице (local.platform.sql_grant_matrix)."
  }
  validation {
    # Условная запись — только изменение в tenant_ops и только положительное условие на V_*.
    condition = alltrue([for g in var.contract.sql_deployer.grants : g.condition == null ? true : (
      g.dataset_key == local.platform.sql_conditional_dataset &&
      g.role == "organizations/${local.platform.organization_id}/roles/mpaSqlViewUpdate" &&
      g.condition.title == local.platform.sql_condition_title &&
      g.condition.description == local.platform.sql_condition_desc &&
      g.condition.expression == format(
        "resource.type == \"bigquery.googleapis.com/Table\" && resource.service == \"bigquery.googleapis.com\" && resource.name.startsWith(\"projects/%s/datasets/%s/tables/%s\")",
    var.contract.project_id, var.contract.datasets[local.platform.sql_conditional_dataset], local.platform.sql_view_prefix))])
    error_message = "SQL-деплоер: условие допустимо только на mpaSqlViewUpdate в tenant_ops и только V_*."
  }
  validation {
    # T5 (D1): control plane есть ровно тогда, когда есть runtime Ozon.
    condition     = (var.contract.marketplaces.ozon == null) == (var.contract.control == null)
    error_message = "Control plane (sa-tenant-control) обязателен при включённом Ozon и запрещён без него."
  }
  validation {
    condition = var.contract.control == null ? true : (
      var.contract.control.account_id == local.platform.control_account_id &&
      var.contract.control.email == "${local.platform.control_account_id}@${var.contract.project_id}.iam.gserviceaccount.com" &&
    var.contract.control.job.name == local.platform.control_job_name)
    error_message = "Control: только sa-tenant-control@<проект арендатора> и job tenant-control."
  }
  validation {
    # Ровно утверждённая матрица control: без условий, без повторов, без ozon_mart/analytics_share,
    # без записи в ozon_raw и ref (привязку подтверждает только владелец).
    condition = var.contract.control == null ? true : (
      length(var.contract.control.grants) == length(local.platform.control_grant_matrix) && toset([
        for g in var.contract.control.grants :
        "${g.dataset_key}|${trimprefix(g.role, "organizations/${local.platform.organization_id}/roles/")}|${g.condition != null}"
    ]) == toset(local.platform.control_grant_matrix))
    error_message = "Control: гранты не равны утверждённой матрице (local.platform.control_grant_matrix)."
  }
  validation {
    # Control сам данные не грузит и привязку не проверяет как runtime; его окружение — только
    # проект, датасеты, ИМЕНА секретов и сущности.
    condition = var.contract.control == null ? true : (
      !contains(keys(var.contract.control.job.env), "TENANT_BINDING_REQUIRED") &&
      !contains(keys(var.contract.control.job.env), "ENTITIES") &&
      lookup(var.contract.control.job.env, "GCP_PROJECT_ID", "") == var.contract.project_id &&
      lookup(var.contract.control.job.env, "TENANT_OPS_DATASET", "") == var.contract.datasets["tenant_ops"] &&
      lookup(var.contract.control.job.env, "TENANT_LOCKS_DATASET", "") == var.contract.datasets["tenant_locks"] &&
    lookup(var.contract.control.job.env, "STRICT_PAGE_CAPS", "") == "1")
    error_message = "Control: окружение tenant-control не соответствует контракту control_identity.job_env."
  }
  validation {
    # T5 (п.3): каждый job runtime грузит только в подтверждённый владельцем кабинет.
    condition = var.contract.marketplaces.ozon == null ? true : alltrue([
    for j in values(var.contract.marketplaces.ozon.jobs) : lookup(j.env, "TENANT_BINDING_REQUIRED", "") == "1"])
    error_message = "Runtime: каждый job Ozon обязан иметь TENANT_BINDING_REQUIRED=1 (проверка привязки кабинета)."
  }
  validation {
    # Образ: только неизменяемый digest в реестре платформы. Нет образа — нет плана.
    condition = var.contract.marketplaces.ozon == null ? true : can(regex(
      "^europe-west1-docker\\.pkg\\.dev/mpa-platform/mpa-runtime/[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}$",
    coalesce(var.contract.marketplaces.ozon.runtime_image, "<нет утверждённого образа>")))
    error_message = "Образ runtime: только europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/<image>@sha256:<64 hex> из infra/tenant/runtime_release.json. Теги (в т.ч. latest), пустая ссылка и реестр EVETIS запрещены."
  }

}
