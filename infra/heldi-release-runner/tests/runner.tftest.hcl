# Offline: mock provider, no cloud call.
mock_provider "google" {}

variables {
  tenant_id  = "client_001"
  project_id = "mpa-t-client-001"
}

run "first_apply_creates_identities_roles_journal_but_no_jobs" {
  command = plan
  assert {
    condition     = length(google_cloud_run_v2_job.exec) == 0 && length(google_cloud_run_v2_job.rehearse) == 0
    error_message = "Без пинов пакета и образа задания не создаются."
  }
  assert {
    condition     = tostring(google_storage_bucket.journal.retention_policy[0].retention_period) == tostring(400 * 86400) && google_storage_bucket.journal.retention_policy[0].is_locked == false && google_storage_bucket.journal.public_access_prevention == "enforced" && google_storage_bucket.journal.force_destroy == false
    error_message = "Журнал: хранение 400 сут (не заблокировано), без публичного доступа, без force_destroy."
  }
}

run "no_role_can_delete_or_touch_foreign_resources" {
  command = plan
  assert {
    condition = alltrue([for r in [google_project_iam_custom_role.read, google_project_iam_custom_role.data, google_project_iam_custom_role.write, google_project_iam_custom_role.tick, google_project_iam_custom_role.controller, google_project_iam_custom_role.act_as] :
    alltrue([for p in r.permissions : length(regexall("(delete|setIamPolicy|\\.create$|\\.update$|undelete)", p)) == 0 || contains(["bigquery.tables.create", "bigquery.jobs.create", "run.jobs.update"], p)])])
    error_message = "Нет delete/setIamPolicy; create только у таблиц и запросов, update только у задания (грант на одном задании)."
  }
  assert {
    condition     = !contains(google_project_iam_custom_role.read.permissions, "bigquery.tables.updateData") && !contains(google_project_iam_custom_role.data.permissions, "bigquery.tables.updateData")
    error_message = "Роли чтения не пишут."
  }
  assert {
    condition     = google_cloud_run_v2_job_iam_member.controller.name == "tenant-backfill-controller" && google_service_account_iam_member.act_as.service_account_id == "projects/mpa-t-client-001/serviceAccounts/sa-backfill-controller@mpa-t-client-001.iam.gserviceaccount.com"
    error_message = "UpdateJob/run и actAs — ровно контроллер Heldi и его SA."
  }
  assert {
    condition     = toset(google_project_iam_custom_role.tick.permissions) == toset(["cloudscheduler.jobs.pause", "cloudscheduler.jobs.resume"])
    error_message = "По расписаниям — только pause/resume (без update/delete/create/run)."
  }
  assert {
    condition     = !strcontains(google_project_iam_member.write.condition[0].expression, "ozon_raw") && !strcontains(google_project_iam_member.write.condition[0].expression, "ref/") && strcontains(google_project_iam_member.write.condition[0].expression, "tenant_locks")
    error_message = "Запись — только tenant_ops/tenant_locks."
  }
}

run "rehearsal_identity_never_writes_production" {
  command = plan
  assert {
    condition     = google_project_iam_member.write.member == "serviceAccount:sa-heldi-release-exec@mpa-t-client-001.iam.gserviceaccount.com" && google_project_iam_member.tick.member == google_project_iam_member.write.member && google_cloud_run_v2_job_iam_member.controller.member == google_project_iam_member.write.member && google_service_account_iam_member.act_as.member == google_project_iam_member.write.member
    error_message = "Права записи — только у sa-heldi-release-exec."
  }
  assert {
    condition     = strcontains(google_storage_bucket_iam_member.creator["rehearse"].condition[0].expression, "/objects/rehearsal/") && !strcontains(google_storage_bucket_iam_member.creator["rehearse"].condition[0].expression, "/objects/journal/")
    error_message = "Репетиция пишет только rehearsal/ своего бакета."
  }
}

run "jobs_pin_package_and_never_retry" {
  command = plan
  variables {
    exec_image     = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-release-runner@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    rehearse_image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-release-runner@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    package_pins = {
      package         = "package-eca0335a"
      ack_sha256      = "2222222222222222222222222222222222222222222222222222222222222222"
      package_sha256  = "3333333333333333333333333333333333333333333333333333333333333333"
      executor_source = "1a2b61a0793d41f08924a515663d0da2318e15cf"
    }
  }
  assert {
    condition     = google_cloud_run_v2_job.exec[0].template[0].template[0].max_retries == 0 && google_cloud_run_v2_job.rehearse[0].template[0].template[0].max_retries == 0 && google_cloud_run_v2_job.exec[0].template[0].parallelism == 1
    error_message = "Никаких автоповторов: POST с неизвестным исходом не повторяется."
  }
  assert {
    condition     = google_cloud_run_v2_job.exec[0].template[0].template[0].service_account == "sa-heldi-release-exec@mpa-t-client-001.iam.gserviceaccount.com" && google_cloud_run_v2_job.rehearse[0].template[0].template[0].service_account == "sa-heldi-rehearse@mpa-t-client-001.iam.gserviceaccount.com"
    error_message = "Каждое задание — своя личность."
  }
  assert {
    condition     = contains([for e in google_cloud_run_v2_job.exec[0].template[0].template[0].containers[0].env : "${e.name}=${e.value}"], "ACK_SHA256=2222222222222222222222222222222222222222222222222222222222222222") && contains([for e in google_cloud_run_v2_job.rehearse[0].template[0].template[0].containers[0].env : "${e.name}=${e.value}"], "RUNNER_MODE=REHEARSE")
    error_message = "Пины пакета передаются в задание и проверяются образом."
  }
}

run "reject_tag_image" {
  command = plan
  variables { exec_image = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/heldi-release-runner:latest" }
  expect_failures = [var.exec_image]
}

run "reject_evetis_project" {
  command = plan
  variables { project_id = "evetis-wb-analytics" }
  expect_failures = [var.project_id]
}

run "reject_write_to_raw" {
  command = plan
  variables { exec_write_datasets = ["ozon_raw"] }
  expect_failures = [var.exec_write_datasets]
}
