output "identities" {
  value = { exec = google_service_account.exec.email, rehearse = google_service_account.rehearse.email }
}

output "journal_bucket" {
  value = google_storage_bucket.journal.name
}

output "jobs" {
  value = {
    exec     = try(google_cloud_run_v2_job.exec[0].name, null)
    rehearse = try(google_cloud_run_v2_job.rehearse[0].name, null)
  }
}
