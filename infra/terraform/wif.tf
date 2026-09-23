# Workload Identity Federation: GitHub OIDC → impersonation. БЕЗ JSON-ключей.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions pool"
  depends_on                = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    # Композитный атрибут repo@ref — для точного ограничения привилегированного SA.
    "attribute.repo_ref" = "assertion.repository + \"@\" + assertion.ref"
    # AE v1: ограничение по КОНКРЕТНОМУ workflow-файлу. workflow_ref = owner/repo/.github/
    # workflows/<file>@<ref>. Для job'ов переиспользуемых workflows это ref ВЫЗЫВАЮЩЕГО файла.
    "attribute.workflow_ref"  = "assertion.workflow_ref"
    "attribute.workflow_path" = "assertion.workflow_ref.extract('{path}@')"
  }
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  pool_principal = "principalSet://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}"
}

# ── AE v1: привилегированные SA привязаны к КОНКРЕТНЫМ workflow-файлам ─────────
# Было: deployer — любой workflow репозитория; terraform_apply — любой workflow на main.
# Пока workflows писал только владелец, это было терпимо. Автономный контур добавляет
# субъект, исполняющий ПРОИЗВОЛЬНЫЙ код (тесты агента) внутри job'а с id-token: write на
# main: такой код может выпустить OIDC-токен и обменять его на любой широко привязанный SA.
# Поэтому привилегированные SA больше не доступны «любому workflow» — только своим файлам.
# НЕ ПРИМЕНЕНО: это решение владельца (AE_V1_SECURITY.md §2). До применения AE_ENABLED
# обязан оставаться выключенным.
locals {
  # deployer — по пути файла на ЛЮБОЙ ветке: сохраняет практику деплоя из feature-веток.
  deployer_workflows = toset(["deploy-prod.yml", "deploy-shadow.yml"])
  # terraform_apply — только эти файлы и только на main.
  terraform_apply_workflows = toset(["infra.yml", "scheduler-control.yml"])
}

resource "google_service_account_iam_member" "deployer_wif" {
  for_each           = local.deployer_workflows
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.workflow_path/${var.github_repo}/.github/workflows/${each.value}"
}

# terraform-plan: любой workflow репо (нужен для PR-plan), но SA read-only.
resource "google_service_account_iam_member" "terraform_plan_wif" {
  service_account_id = google_service_account.terraform_plan.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.repository/${var.github_repo}"
}

# terraform-apply: ТОЛЬКО свои файлы на main. Прежнее условие repo@refs/heads/main пропускало
# ЛЮБОЙ workflow на main — включая autonomy-*.yml, где исполняется код агента.
resource "google_service_account_iam_member" "terraform_apply_wif" {
  for_each           = local.terraform_apply_workflows
  service_account_id = google_service_account.terraform_apply.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.workflow_ref/${var.github_repo}/.github/workflows/${each.value}@refs/heads/main"
}
