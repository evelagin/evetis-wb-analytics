# Dedicated historical continuation, opt-in only. No marketplace credentials,
# no ordinary Scheduler invocation grants, no RAW/ref mutation permission.
locals {
  backfill = var.contract.orchestration
  backfill_project_grants = local.backfill == null ? {} : {
    for g in local.backfill.matrix : g.role => g
    if g.resource == "projects/${var.contract.project_id}"
  }
  backfill_job_grants = local.backfill == null ? {} : {
    for g in local.backfill.matrix : "${g.role}|${g.resource}" => g
    if strcontains(g.resource, "/jobs/")
  }
  backfill_table_grants = local.backfill == null ? {} : {
    for g in local.backfill.matrix : g.resource => g
    if strcontains(g.resource, "/tables/")
  }
}

resource "google_service_account" "backfill" {
  for_each     = local.backfill == null ? {} : local.backfill.accounts
  project      = var.contract.project_id
  account_id   = each.value.id
  display_name = "Tenant backfill ${each.key}"
  depends_on   = [google_project_service.this, terraform_data.backfill_guard]
}

resource "google_project_iam_custom_role" "backfill" {
  for_each    = local.backfill == null ? {} : local.backfill.roles
  project     = var.contract.project_id
  role_id     = each.key
  title       = "Tenant backfill ${each.key}"
  permissions = each.value
  stage       = "GA"
  depends_on  = [terraform_data.backfill_guard]
}

resource "google_project_iam_member" "backfill" {
  for_each   = local.backfill_project_grants
  project    = var.contract.project_id
  role       = each.value.role
  member     = "serviceAccount:${each.value.principal}"
  depends_on = [google_project_iam_custom_role.backfill, google_service_account.backfill]
}

resource "google_bigquery_table_iam_member" "backfill" {
  for_each   = local.backfill_table_grants
  project    = var.contract.project_id
  dataset_id = split("/", each.value.resource)[3]
  table_id   = split("/", each.value.resource)[5]
  role       = each.value.role
  member     = "serviceAccount:${each.value.principal}"
  depends_on = [google_project_iam_custom_role.backfill, google_service_account.backfill, google_bigquery_table.this]
}

resource "google_service_account_iam_member" "backfill_append" {
  count              = local.backfill == null ? 0 : 1
  service_account_id = "projects/${var.contract.project_id}/serviceAccounts/${local.backfill.accounts["append"].email}"
  role               = "projects/${var.contract.project_id}/roles/backfillDelegateAppend"
  member             = "serviceAccount:${local.backfill.accounts["controller"].email}"
  depends_on         = [google_project_iam_custom_role.backfill, google_service_account.backfill]
}

resource "google_cloud_run_v2_job" "backfill" {
  count               = local.backfill == null ? 0 : 1
  project             = var.contract.project_id
  location            = var.contract.region
  name                = local.backfill.job.name
  deletion_protection = true
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = local.backfill.accounts["controller"].email
      timeout         = local.backfill.job.timeout
      max_retries     = 0
      containers {
        image   = local.backfill.job.image
        command = ["python", "-m", "tools.tenancy.cloud_controller"]
        dynamic "env" {
          for_each = local.backfill.job.env
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = { cpu = "1000m", memory = "1Gi" }
        }
      }
    }
  }
  depends_on = [google_service_account.backfill, google_project_iam_member.backfill,
    google_service_account_iam_member.backfill_append, google_bigquery_dataset.this,
  google_bigquery_table_iam_member.backfill]
}

resource "google_cloud_run_v2_job_iam_member" "backfill" {
  for_each = local.backfill_job_grants
  project  = var.contract.project_id
  location = var.contract.region
  name     = reverse(split("/", each.value.resource))[0]
  role     = each.value.role
  member   = "serviceAccount:${each.value.principal}"
  depends_on = [google_project_iam_custom_role.backfill, google_service_account.backfill,
  google_cloud_run_v2_job.backfill, module.ozon]
}

resource "google_cloud_scheduler_job" "backfill" {
  count     = local.backfill == null ? 0 : 1
  project   = var.contract.project_id
  region    = var.contract.region
  name      = local.backfill.scheduler.name
  schedule  = local.backfill.scheduler.schedule
  time_zone = local.backfill.scheduler.time_zone
  paused    = local.backfill.scheduler.state == "PAUSED"
  http_target {
    http_method = "POST"
    uri         = local.backfill.scheduler.uri
    body        = base64encode("{}")
    oauth_token {
      service_account_email = local.backfill.accounts["wake"].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.backfill]
}

# Defense in depth: Python registry/plan scanner is the canonical authority;
# direct malformed variable input still cannot broaden the dedicated identities.
locals {
  backfill_permissions = jsondecode(file("${path.module}/backfill-permissions.json"))
  backfill_principals  = { for k in ["controller", "append", "wake"] : k => "sa-backfill-${k}@${var.contract.project_id}.iam.gserviceaccount.com" }
  backfill_role_prefix = "projects/${var.contract.project_id}/roles/"
  backfill_expected_bindings = local.ozon == null ? [] : concat(
    [for k in ["ozon_raw", "ref", "tenant_ops"] : "${local.backfill_principals.controller}|${local.backfill_role_prefix}${k == "ref" ? "backfillReadRef" : "backfillRead"}|projects/${var.contract.project_id}/datasets/${var.contract.datasets[k]}"],
    ["${local.backfill_principals.controller}|${local.backfill_role_prefix}backfillLockRead|projects/${var.contract.project_id}/datasets/${var.contract.datasets.tenant_locks}",
      "${local.backfill_principals.append}|${local.backfill_role_prefix}backfillLockCreate|projects/${var.contract.project_id}/datasets/${var.contract.datasets.tenant_locks}",
    "${local.backfill_principals.append}|${local.backfill_role_prefix}backfillAppendDatasetMetadata|projects/${var.contract.project_id}/datasets/${var.contract.datasets.tenant_ops}"],
    [for r in ["backfillQuery", "backfillInventoryRead"] : "${local.backfill_principals.controller}|${local.backfill_role_prefix}${r}|projects/${var.contract.project_id}"],
    [for j in keys(local.ozon.jobs) : "${local.backfill_principals.controller}|${local.backfill_role_prefix}backfillRuntimeExecute|projects/${var.contract.project_id}/locations/${var.contract.region}/jobs/${j}"],
    [for t in ["BACKFILL_CHECKPOINTS", "DATA_COVERAGE", "DQ_RESULTS"] : "${local.backfill_principals.append}|${local.backfill_role_prefix}backfillAppend|projects/${var.contract.project_id}/datasets/${var.contract.datasets.tenant_ops}/tables/${t}"],
    ["${local.backfill_principals.controller}|${local.backfill_role_prefix}backfillDelegateAppend|projects/${var.contract.project_id}/serviceAccounts/${local.backfill_principals.append}"],
    [for j in ["tenant-control", "tenant-backfill-controller"] : "${local.backfill_principals.controller}|${local.backfill_role_prefix}backfillControllerRead|projects/${var.contract.project_id}/locations/${var.contract.region}/jobs/${j}"],
    ["${local.backfill_principals.wake}|${local.backfill_role_prefix}backfillWake|projects/${var.contract.project_id}/locations/${var.contract.region}/jobs/tenant-backfill-controller"]
  )
}

resource "terraform_data" "backfill_guard" {
  count = local.backfill == null ? 0 : 1
  input = { tenant_id = var.contract.tenant_id, root_hash = local.backfill.job.env["BACKFILL_ROOT_HASH"] }
  lifecycle {
    precondition {
      condition = var.contract.orchestration == null ? true : (
        var.contract.marketplaces.ozon != null && contains(var.contract.apis, "iamcredentials.googleapis.com") &&
        toset(keys(var.contract.orchestration.accounts)) == toset(["controller", "append", "wake"]) &&
        alltrue([for k, a in var.contract.orchestration.accounts :
        a.id == "sa-backfill-${k}" && a.email == "${a.id}@${var.contract.project_id}.iam.gserviceaccount.com"])
      )
      error_message = "Backfill: dedicated Ozon identity boundary required."
    }
    precondition {
      condition = var.contract.orchestration == null ? true : (
        toset(keys(var.contract.orchestration.roles)) == toset(keys(local.backfill_permissions)) &&
        alltrue([for k, p in var.contract.orchestration.roles : try(toset(p) == toset(local.backfill_permissions[k]), false)]) &&
        length(var.contract.orchestration.matrix) == length(local.backfill_expected_bindings) &&
        toset([for g in var.contract.orchestration.matrix : "${g.principal}|${g.role}|${g.resource}"]) == toset(local.backfill_expected_bindings) &&
        alltrue([for g in var.contract.orchestration.matrix : try(toset(g.permissions) == toset(local.backfill_permissions[trimprefix(g.role, local.backfill_role_prefix)]), false)])
      )
      error_message = "Backfill: exact principal/permission/resource matrix required."
    }
    precondition {
      condition = var.contract.orchestration == null ? true : (
        length(var.contract.orchestration.dataset_grants) == 6 &&
        toset([for g in var.contract.orchestration.dataset_grants : "${g.email}|${g.role}|projects/${var.contract.project_id}/datasets/${lookup(var.contract.datasets, g.dataset_key, "invalid")}"]) == toset(slice(local.backfill_expected_bindings, 0, 6)) &&
        var.contract.orchestration.job.name == "tenant-backfill-controller" &&
        var.contract.orchestration.job.timeout == "600s" &&
        can(regex("^europe-west1-docker\\.pkg\\.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:[0-9a-f]{64}$", var.contract.orchestration.job.image)) &&
        lookup(var.contract.orchestration.job.env, "TENANT_BINDING_REQUIRED", "") == "1" &&
        lookup(var.contract.orchestration.job.env, "STRICT_PAGE_CAPS", "") == "1" &&
        lookup(var.contract.orchestration.job.env, "GCP_PROJECT_ID", "") == var.contract.project_id &&
        lookup(var.contract.orchestration.job.env, "TENANT_ID", "") == var.contract.tenant_id &&
        can(regex("^[0-9a-f]{64}$", lookup(var.contract.orchestration.job.env, "BACKFILL_ROOT_HASH", "")))
      )
      error_message = "Backfill: exact data grants, immutable image and binding gates required."
    }
    precondition {
      condition = var.contract.orchestration == null ? true : (
        var.contract.orchestration.scheduler.name == "tenant-backfill-tick" &&
        contains(["PAUSED", "ENABLED"], var.contract.orchestration.scheduler.state) &&
        var.contract.orchestration.scheduler.schedule == "0 * * * *" &&
        var.contract.orchestration.scheduler.time_zone == "Europe/Moscow" &&
        var.contract.orchestration.scheduler.uri == "https://run.googleapis.com/v2/projects/${var.contract.project_id}/locations/${var.contract.region}/jobs/tenant-backfill-controller:run"
      )
      error_message = "Backfill: only the dedicated hourly historical Scheduler is permitted."
    }

  }
}
