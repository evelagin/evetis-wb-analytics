# SYNTHETIC offline contract. No tenant registry opt-in or real release evidence.
mock_provider "google" {
  mock_data "google_project" {
    defaults = { folder_id = "881419274207", number = "123456789012", billing_account = "000000-000000-000000" }
  }
  mock_data "google_projects" {
    defaults = { projects = [{ project_id = "mpa-t-client-002", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
}
run "dedicated_opt_in_renders_and_preserves_ordinary_pause" {
  command = plan
  variables { contract = jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")) }
  assert {
    condition     = length(google_service_account.backfill) == 3 && length(google_project_iam_custom_role.backfill) == 11 && length(google_bigquery_table_iam_member.backfill) == 3 && length(google_cloud_run_v2_job_iam_member.backfill) == 6
    error_message = "Only exact dedicated identities, roles and table/job grants are permitted."
  }
  assert {
    condition     = google_cloud_run_v2_job.backfill[0].template[0].template[0].service_account == "sa-backfill-controller@mpa-t-client-002.iam.gserviceaccount.com" && google_cloud_run_v2_job.backfill[0].template[0].template[0].max_retries == 0 && google_cloud_run_v2_job.backfill[0].template[0].task_count == 1
    error_message = "Controller must be bounded and use its own identity."
  }
  assert {
    condition     = google_cloud_scheduler_job.backfill[0].paused && alltrue(values(output.ozon.paused))
    error_message = "Initial historical and all ordinary schedules must remain PAUSED."
  }
}

run "reject_broad_role" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration, { roles = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration.roles, { backfillRead = ["bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.delete"] }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}

run "reject_foreign_identity" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration, { accounts = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration.accounts, { controller = { id = "sa-backfill-controller", email = "sa-backfill-controller@mpa-t-client-003.iam.gserviceaccount.com" } }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}

run "reject_scheduler_override" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration.scheduler, { uri = "https://run.googleapis.com/v2/projects/mpa-t-client-002/locations/europe-west1/jobs/ozon-runtime-daily:run" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}

run "reject_binding_bypass" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration.job, { env = merge(jsondecode(file("tests/fixtures/backfill.synthetic.contract.json")).orchestration.job.env, { TENANT_BINDING_REQUIRED = "0" }) }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
