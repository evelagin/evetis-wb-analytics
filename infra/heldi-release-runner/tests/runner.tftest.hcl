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

run "idle_runner_is_read_only" {
  command = plan
  assert {
    condition     = length(google_project_iam_member.write) == 0 && length(google_project_iam_member.tick) == 0 && length(google_cloud_run_v2_job_iam_member.controller) == 0 && length(google_service_account_iam_member.act_as) == 0
    error_message = "Без окна выпуска у исполнителя нет ни одного права на изменение production."
  }
  assert {
    condition = alltrue([for r in [google_project_iam_custom_role.read, google_project_iam_custom_role.data] :
    alltrue([for p in r.permissions : length(regexall("(delete|setIamPolicy|update|pause|resume|\\.run$|\\.create$)", p)) == 0 || p == "bigquery.jobs.create"])])
    error_message = "Постоянные роли — только чтение (SELECT через jobs.create)."
  }
}

run "release_window_grants_expire_by_themselves" {
  command = plan
  variables {
    release_window = { ack_sha256 = "2222222222222222222222222222222222222222222222222222222222222222", not_before = "2026-11-10T09:00:00Z", expires = "2026-11-10T13:00:00Z" }
  }
  assert {
    condition = alltrue([for c in [google_project_iam_member.write[0].condition[0].expression, google_project_iam_member.tick[0].condition[0].expression, google_cloud_run_v2_job_iam_member.controller[0].condition[0].expression, google_service_account_iam_member.act_as[0].condition[0].expression] :
    strcontains(c, "request.time >= timestamp(\"2026-11-10T09:00:00Z\")") && strcontains(c, "request.time < timestamp(\"2026-11-10T13:00:00Z\")")])
    error_message = "Каждое право на изменение ограничено окном выпуска и гаснет само."
  }
  assert {
    condition     = strcontains(google_project_iam_member.tick[0].condition[0].expression, "request.time.getMinutes() == 1 ||") && strcontains(google_project_iam_member.tick[0].condition[0].expression, "request.time.getMinutes() == 58)") && !strcontains(google_project_iam_member.tick[0].condition[0].expression, "request.time.getMinutes() == 0 ") && !strcontains(google_project_iam_member.tick[0].condition[0].expression, "request.time.getMinutes() == 2 ")
    error_message = "pause/resume tick — только во 2-й минуте цикла */3 (не на границе срабатывания)."
  }
  assert {
    condition     = google_cloud_run_v2_job_iam_member.controller[0].name == "tenant-backfill-controller" && google_service_account_iam_member.act_as[0].service_account_id == "projects/mpa-t-client-001/serviceAccounts/sa-backfill-controller@mpa-t-client-001.iam.gserviceaccount.com" && google_project_iam_member.write[0].member == "serviceAccount:sa-heldi-release-exec@mpa-t-client-001.iam.gserviceaccount.com" && google_project_iam_member.tick[0].member == google_project_iam_member.write[0].member
    error_message = "Права окна — только у sa-heldi-release-exec, только на контроллер Heldi и его SA."
  }
  assert {
    condition     = !strcontains(google_project_iam_member.write[0].condition[0].expression, "ozon_raw") && strcontains(google_project_iam_member.write[0].condition[0].expression, "tenant_locks") && toset(google_project_iam_custom_role.tick.permissions) == toset(["cloudscheduler.jobs.pause", "cloudscheduler.jobs.resume"])
    error_message = "Запись — только tenant_ops/tenant_locks; по расписаниям — только pause/resume."
  }
}

run "reject_window_longer_than_8h" {
  command = plan
  variables {
    release_window = { ack_sha256 = "2222222222222222222222222222222222222222222222222222222222222222", not_before = "2026-11-10T09:00:00Z", expires = "2026-11-10T17:00:01Z" }
  }
  expect_failures = [var.release_window]
}

run "reject_inverted_window" {
  command = plan
  variables {
    release_window = { ack_sha256 = "2222222222222222222222222222222222222222222222222222222222222222", not_before = "2026-11-10T13:00:00Z", expires = "2026-11-10T09:00:00Z" }
  }
  expect_failures = [var.release_window]
}

run "rehearsal_identity_never_writes_production" {
  command = plan
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
