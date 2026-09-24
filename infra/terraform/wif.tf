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
    # AE v1: ограничение по КОНКРЕТНОМУ workflow-файлу И ref. workflow_ref = owner/repo/.github/
    # workflows/<file>@<ref> — файл ВЕРХНЕГО уровня, запустивший прогон (у job'ов переиспользуемых
    # workflows это вызывающий файл). job_workflow_ref — файл, в котором объявлен САМ job (для
    # переиспользуемого — вызываемый файл). Оба claim выставляет GitHub, а не содержимое репозитория.
    "attribute.workflow_ref"     = "assertion.workflow_ref"
    "attribute.job_workflow_ref" = "assertion.job_workflow_ref"
    # terraform-plan нужен и с feature-веток, поэтому ref не фиксирован; файл — фиксирован ТОЧНЫМ
    # равенством. НЕ extract('{path}@'): тот берёт текст до ПЕРВОГО «@» и принимает файл
    # «infra.yml@x.yml» за infra.yml (tools/tests/test_autonomy_wif.py, случай E).
    # Ветки кандидатов ae/* исключены: plan там никому не нужен.
    "attribute.tf_plan_workflow" = "(assertion.workflow_ref == assertion.repository + '/.github/workflows/infra.yml@' + assertion.ref && !assertion.ref.startsWith('refs/heads/ae/')) ? 'infra.yml' : 'none'"
  }
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  pool_principal = "principalSet://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}"
}

# ── AE v1: привилегированные SA привязаны к КОНКРЕТНЫМ workflow-файлам на main ─────────
# Было (живой провайдер на 2026-09-24): deployer и terraform-plan — ЛЮБОЙ workflow репозитория,
# terraform-apply — любой workflow на main. Автономный контур добавляет субъект, исполняющий
# ПРОИЗВОЛЬНЫЙ код (тесты агента, код кандидата) внутри job'а с id-token: write на main; на
# раннере GitHub у него есть sudo, значит он может выпустить OIDC-токен с любой audience и
# обменять его на любой широко привязанный SA. Инвариант: код в job'е AE НЕ получает
# deployer / terraform-apply / terraform-plan. Доказательство — tools/tests/test_autonomy_wif.py
# (случаи A–E на точных claims GitHub) и tools/autonomy/wif_check.py (та же проверка живого
# провайдера после применения).
# НЕ ПРИМЕНЕНО: это решение владельца (AE_V1_SECURITY.md §2, AE_V1_RUNBOOK.md §2).
locals {
  github_workflows = "${var.github_repo}/.github/workflows"
  # deployer — deploy-prod (ручной dispatch) и deploy-shadow (push в main): ОБА только из main.
  # История 2026-09-12…2026-09-23: все запуски deploy-* — из main, запусков с веток нет.
  deployer_workflow_refs = toset([
    "${local.github_workflows}/deploy-prod.yml@refs/heads/main",
    "${local.github_workflows}/deploy-shadow.yml@refs/heads/main",
  ])
  # terraform_apply — только эти файлы и только на main.
  terraform_apply_workflow_refs = toset([
    "${local.github_workflows}/infra.yml@refs/heads/main",
    "${local.github_workflows}/scheduler-control.yml@refs/heads/main",
  ])
}

resource "google_service_account_iam_member" "deployer_wif" {
  for_each           = local.deployer_workflow_refs
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.workflow_ref/${each.value}"
}

# terraform-plan: только infra.yml (с любой ветки — plan с feature-веток используется), SA read-only
# (roles/viewer), но viewer читает ВСЕ датасеты проекта — шире, чем sa-ae-reader.
resource "google_service_account_iam_member" "terraform_plan_wif" {
  service_account_id = google_service_account.terraform_plan.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.tf_plan_workflow/infra.yml"
}

# terraform-apply: ТОЛЬКО свои файлы на main. Прежнее условие repo@refs/heads/main пропускало
# ЛЮБОЙ workflow на main — включая autonomy-*.yml, где исполняется код агента.
resource "google_service_account_iam_member" "terraform_apply_wif" {
  for_each           = local.terraform_apply_workflow_refs
  service_account_id = google_service_account.terraform_apply.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal}/attribute.workflow_ref/${each.value}"
}
