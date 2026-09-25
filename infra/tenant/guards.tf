# Проверки живого проекта ДО любого изменения. Все ресурсы корня зависят от guard:
# если проект не тот, не в той папке, не активен или без биллинга — план падает, а не
# создаёт что-то «рядом». Проект создаёт человек (OD-4); здесь он только читается.
data "google_project" "tenant" {
  project_id = var.contract.project_id
}

data "google_projects" "tenant_active" {
  filter = "id:${var.contract.project_id} lifecycleState:ACTIVE"
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
      condition     = length(data.google_projects.tenant_active.projects) == 1
      error_message = "Проект арендатора не существует или не ACTIVE."
    }
    precondition {
      condition     = data.google_project.tenant.project_id == var.contract.project_id
      error_message = "Прочитан не тот проект."
    }
    precondition {
      condition     = data.google_project.tenant.folder_id == local.platform.tenants_folder_id
      error_message = "Проект арендатора обязан лежать прямо в tenants/ (folders/881419274207)."
    }
    precondition {
      condition     = !contains(concat(local.evetis_forbidden_markers, [local.platform.project_number]), data.google_project.tenant.number)
      error_message = "Номер проекта принадлежит EVETIS или платформе."
    }
    precondition {
      condition     = try(data.google_project.tenant.billing_account, "") != ""
      error_message = "У проекта арендатора не включён биллинг (привязывает человек, OD-4)."
    }
  }
}
