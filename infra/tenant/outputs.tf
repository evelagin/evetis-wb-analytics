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

output "tables" {
  value = sort(keys(google_bigquery_table.this))
}

output "ozon" {
  value = length(module.ozon) == 0 ? null : module.ozon[0].summary
}
