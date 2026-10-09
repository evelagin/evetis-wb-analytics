# Synthetic offline input; no release publication, tenant execution or API call.
mock_provider "google" {
  mock_data "google_project" { defaults = { folder_id = "881419274207", number = "123456789012", billing_account = "000000-000000-000000" } }
  mock_data "google_projects" { defaults = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] } }
}
run "approved_3_paused" {
  command = plan
  variables { contract = jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")) }
  assert {
    condition     = google_cloud_scheduler_job.backfill[0].schedule == "*/3 * * * *" && google_cloud_scheduler_job.backfill[0].paused == true && alltrue(values(output.ozon.paused)) && length(google_project_iam_custom_role.backfill) == 12 && google_cloud_run_v2_job.backfill[0].template[0].template[0].max_retries == 0
    error_message = "Only dedicated cadence/state changes; ordinary schedules, IAM and retry boundary stay exact."
  }
}
run "approved_3_enabled" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { state = "ENABLED" }) }) }) }
  assert {
    condition     = google_cloud_scheduler_job.backfill[0].schedule == "*/3 * * * *" && google_cloud_scheduler_job.backfill[0].paused == false && alltrue(values(output.ozon.paused)) && length(google_project_iam_custom_role.backfill) == 12 && google_cloud_run_v2_job.backfill[0].template[0].template[0].max_retries == 0
    error_message = "Only dedicated cadence/state changes; ordinary schedules, IAM and retry boundary stay exact."
  }
}
run "approved_fallback_10" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { state = "ENABLED", schedule = "*/10 * * * *" }) }) }) }
  assert {
    condition     = google_cloud_scheduler_job.backfill[0].schedule == "*/10 * * * *" && google_cloud_scheduler_job.backfill[0].paused == false && alltrue(values(output.ozon.paused)) && length(google_project_iam_custom_role.backfill) == 12 && google_cloud_run_v2_job.backfill[0].template[0].template[0].max_retries == 0
    error_message = "Only dedicated cadence/state changes; ordinary schedules, IAM and retry boundary stay exact."
  }
}
run "reject_unregistered_3" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = null }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_unknown_cron" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { schedule = "*/1 * * * *" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_hourly_under_3_profile" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { schedule = "0 * * * *" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_profile" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { profile = "UNKNOWN" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_wrong_ack" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { owner_ack_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_wrong_root" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { root = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_unknown_policy_field" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { unregistered = "extra" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_string_version" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { version = "1" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_wrong_policy_hash" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job, { env = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job.env, { HISTORICAL_CADENCE_POLICY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_scope_root" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job, { env = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job.env, { BACKFILL_ROOT_HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_foreign_policy_tenant" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { cadence = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.cadence, { tenant = "client_002" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_timezone" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { time_zone = "UTC" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_target" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { scheduler = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.scheduler, { uri = "https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily:run" }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}
run "reject_binding_bypass" {
  command = plan
  variables { contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job, { env = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job.env, { TENANT_BINDING_REQUIRED = "0" }) }) }) }) }
  expect_failures = [terraform_data.backfill_guard]
}

run "reject_unregistered_release_source" {
  command = plan
  variables {
    contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job, { env = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job.env, { CONTROLLER_SOURCE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) }) })
  }
  expect_failures = [terraform_data.backfill_guard]
}

run "reject_image_not_registered_to_policy" {
  command = plan
  variables {
    contract = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")), { orchestration = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration, { job = merge(jsondecode(file("tests/fixtures/cadence.synthetic.contract.json")).orchestration.job, { image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }) }) })
  }
  expect_failures = [terraform_data.backfill_guard]
}
