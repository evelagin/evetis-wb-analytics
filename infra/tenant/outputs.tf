output "tenant_id" {
  value = var.contract.tenant_id
}

output "project_id" {
  value = var.contract.project_id
}

output "state_prefix" {
  value = var.contract.state.prefix
}

output "datasets" {
  value = { for k, d in google_bigquery_dataset.this : k => d.dataset_id }
}

output "dataset_access" {
  value = { for k, d in google_bigquery_dataset.this : k => [for a in d.access : { role = a.role, special_group = a.special_group, user_by_email = a.user_by_email, condition = try(a.condition[0].expression, null) }] }
}

output "tables" {
  value = sort(keys(google_bigquery_table.this))
}

output "ozon" {
  value = length(module.ozon) == 0 ? null : module.ozon[0].summary
}

output "control_email" {
  value = length(google_service_account.control) == 0 ? null : google_service_account.control[0].email
}

output "sql_deployer_email" {
  value = google_service_account.sql_deployer.email
}
