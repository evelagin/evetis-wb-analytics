resource "google_artifact_registry_repository" "loaders" {
  location      = var.region
  repository_id = var.artifact_repo
  format        = "DOCKER"
  description   = "Образы облачных загрузчиков EVETIS WB (один общий образ)."
  depends_on    = [google_project_service.enabled]
}
