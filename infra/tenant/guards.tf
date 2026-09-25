# Проверки живого проекта ДО любого изменения. Все ресурсы корня зависят от guard:
# если проект не тот, не в той папке, не активен или без биллинга — план падает, а не
# создаёт что-то «рядом». Проект создаёт человек (OD-4); здесь он только читается.
#
# Идентичность цели устанавливается НЕЗАВИСИМЫМИ фактами (T3.2, находка 16):
#   1. ID проекта выведен реестром (naming.derive_project_id) и проверен в variables.tf;
#   2. поиск Resource Manager (projects.search, фильтр id + ACTIVE) возвращает ровно
#      один проект с тем же ID, родителем folders/881419274207 и тем же номером, что
#      отдельное чтение projects.get — два разных API согласны друг с другом;
#   3. номер проекта — не EVETIS и не платформа;
#   4. биллинг привязан (здесь) и ВКЛЮЧЁН (billingEnabled проверяет tenant_infra.py до
#      плана через Cloud Billing API: провайдер billingEnabled не читает вовсе).
# Сравнение project_id из data.google_project с самим собой было тавтологией (это вход
# источника данных) и удалено.
data "google_project" "tenant" {
  project_id = var.contract.project_id
}

data "google_projects" "tenant_active" {
  filter = "id:${var.contract.project_id} lifecycleState:ACTIVE"
}

locals {
  found_projects = data.google_projects.tenant_active.projects
  found          = length(local.found_projects) == 1 ? local.found_projects[0] : null
}

resource "terraform_data" "guard" {
  input = {
    tenant_id  = var.contract.tenant_id
    project_id = var.contract.project_id
  }

  lifecycle {
    precondition {
      condition     = terraform.workspace == "default"
      error_message = "Workspaces запрещены: арендатор изолирован своим префиксом state, а не workspace."
    }
    precondition {
      condition     = local.found != null
      error_message = "Проект арендатора не существует или не ACTIVE (поиск вернул не ровно один проект)."
    }
    precondition {
      condition     = try(local.found.project_id == var.contract.project_id, false)
      error_message = "Поиск Resource Manager вернул проект с другим ID."
    }
    precondition {
      condition     = try(local.found.parent.id == local.platform.tenants_folder_id && local.found.parent.type == "folder", false)
      error_message = "По данным поиска проект арендатора не лежит прямо в tenants/ (folders/881419274207)."
    }
    precondition {
      condition     = data.google_project.tenant.folder_id == local.platform.tenants_folder_id
      error_message = "Проект арендатора обязан лежать прямо в tenants/ (folders/881419274207)."
    }
    precondition {
      condition     = can(regex("^[0-9]{6,20}$", data.google_project.tenant.number)) && try(local.found.number == data.google_project.tenant.number, false)
      error_message = "Номер проекта не прочитан или projects.get и projects.search расходятся."
    }
    precondition {
      condition     = !contains(concat(local.evetis_forbidden_markers, [local.platform.project_number]), data.google_project.tenant.number)
      error_message = "Номер проекта принадлежит EVETIS или платформе."
    }
    precondition {
      condition     = try(length(data.google_project.tenant.billing_account) > 0, false)
      error_message = "К проекту арендатора не привязан биллинг (null или пусто; привязывает человек, OD-4)."
    }
  }
}
