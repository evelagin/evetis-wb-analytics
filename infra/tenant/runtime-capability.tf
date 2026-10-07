# Owner/bootstrap creates this role. Canonical provisioner permissions are unchanged.
# Explicit SELECT needs table getData and existing project jobUser, no dataset read.
resource "google_project_iam_custom_role" "runtime_capability_read" {
  count       = local.ozon == null ? 0 : 1
  project     = var.contract.project_id
  role_id     = "runtimeCapabilityRead"
  title       = "Runtime capability preflight data read"
  permissions = ["bigquery.tables.getData"]
  stage       = "GA"
}

resource "google_bigquery_table_iam_member" "runtime_capability_read" {
  count      = local.ozon == null ? 0 : 1
  project    = var.contract.project_id
  dataset_id = var.contract.datasets.tenant_ops
  table_id   = "CAPABILITY_PROFILE"
  role       = "projects/${var.contract.project_id}/roles/runtimeCapabilityRead"
  member     = "serviceAccount:${local.ozon_runtime_email}"
  depends_on = [google_bigquery_table.this, google_project_iam_custom_role.runtime_capability_read]
}
