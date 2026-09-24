# ── Deployer (GitHub Actions): деплоит Job, НЕ читает значения токенов ──
resource "google_project_iam_member" "deployer_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_ar" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_actas_shadow" {
  service_account_id = google_service_account.loaders_shadow.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_actas_prod" {
  service_account_id = google_service_account.loaders_prod.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
# У deployer НЕТ secretAccessor и НЕТ Scheduler Admin.

# ── Terraform PLAN (read-only) ──
resource "google_project_iam_member" "terraform_plan_viewer" {
  project = var.project_id
  role    = "roles/viewer"
  member  = "serviceAccount:${google_service_account.terraform_plan.email}"
}
# Доступ к чтению backend-state bucket даётся на сам bucket (см. bootstrap/чек-лист).

# ── Terraform APPLY (привилегированный, main-only + approval) ──
locals {
  terraform_apply_roles = [
    "roles/run.admin",
    "roles/cloudscheduler.admin",
    "roles/artifactregistry.admin",
    "roles/secretmanager.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/bigquery.admin",
  ]
  # SA, которыми Terraform привязывает Job/Scheduler → нужен serviceAccountUser (actAs).
  terraform_actas_targets = {
    loaders_shadow   = google_service_account.loaders_shadow.name
    loaders_prod     = google_service_account.loaders_prod.name
    scheduler_shadow = google_service_account.scheduler_shadow.name
    scheduler_prod   = google_service_account.scheduler_prod.name
    # Stage ADS-1B. Без этой записи создание wb-ops-health-prod падает с
    # 403 iam.serviceAccounts.actAs на sa-ops-health: Cloud Scheduler требует,
    # чтобы создающий принципал имел actAs на SA из oauth_token. Проверено
    # фактическим отказом apply 06.09.2026 (run 34048893917) — пять ресурсов
    # создались, scheduler упал именно на этом.
    ops_health = google_service_account.ops_health.name
    # Control Tower Phase 1.1: Scheduler ct-refresh-prod вызывает BigQuery от имени sa-ct-refresh
    # (ct_refresh.tf) — тот же actAs-контракт, что у ops_health.
    ct_refresh = google_service_account.ct_refresh.name
    # Executive V2 Phase C2: Scheduler executive-v2-layer-build вызывает BigQuery от имени
    # sa-exec-v2-layer (executive_v2_layer.tf) — тот же actAs-контракт.
    exec_v2_layer = google_service_account.exec_v2_layer.name
    # SKU Performance V2 Phase B: Scheduler sku-performance-v2-layer-build → sa-sku-v2-layer (sku_performance_v2_layer.tf).
    sku_v2_layer = google_service_account.sku_v2_layer.name
    # Unitka Integrity Guard V1: Scheduler unitka-cogs-publication → sa-unitka-cogs-pub (unitka_cogs_publication.tf).
    unitka_cogs_pub = google_service_account.unitka_cogs_pub.name
    # PR-PROMO-1. Тот же отказ, что у ops_health, повторился 22.09.2026 (run 35741630852):
    # создание ozon-runtime-promo упало с 403 iam.serviceAccounts.actAs на sa-ozon-ingestion.
    # Причина не в новом job'е: три существующих Ozon-job'а Terraform НЕ создавал, а
    # импортировал из ручного состояния GCP (ozon_ingestion.tf, шапка) — поэтому actAs ему
    # ни разу и не понадобился, и пробел не проявлялся. Первый же СОЗДАВАЕМЫЙ Ozon-job его
    # обнаружил. Политика обоих SA на момент отказа была пуста (get-iam-policy → только etag).
    ozon_ingestion = google_service_account.ozon_ingestion.name
    ozon_scheduler = google_service_account.ozon_scheduler.name
    # PR-PROMO-3: Scheduler promo-econ-basis-{wb,ozon} вызывает BigQuery от имени
    # sa-promo-econ-{wb,ozon} (promo_economics_snapshot.tf) — тот же actAs-контракт.
    promo_econ_wb   = google_service_account.promo_econ["wb"].name
    promo_econ_ozon = google_service_account.promo_econ["ozon"].name
  }
}

resource "google_project_iam_member" "terraform_apply_roles" {
  for_each = toset(local.terraform_apply_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# Fix 5: apply SA может actAs на runtime/scheduler SAs (иначе падает iam.serviceAccounts.actAs).
resource "google_service_account_iam_member" "terraform_apply_actas" {
  for_each           = local.terraform_actas_targets
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.terraform_apply.email}"
}

# ── Runtime loaders: BigQuery Job User (project) + доступ к секретам ──
resource "google_project_iam_member" "loaders_shadow_jobuser" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_project_iam_member" "loaders_prod_jobuser" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.loaders_prod.email}"
}

resource "google_secret_manager_secret_iam_member" "shadow_secret_access" {
  for_each  = google_secret_manager_secret.wb
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_shadow.email}"
}

resource "google_secret_manager_secret_iam_member" "prod_secret_access" {
  for_each  = google_secret_manager_secret.wb
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}

# ── Scheduler identities: право ЗАПУСТИТЬ конкретный Job ──
resource "google_cloud_run_v2_job_iam_member" "scheduler_shadow_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_stocks_shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_shadow.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_stocks_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}

# PR-Mart3b-3: invoker выдаётся ПОРЕСУРСНО, а не на проект — sa-scheduler-prod уже мог запускать
# wb-stocks-prod, но на wb-mart-prod прав не имел бы, и Scheduler молча падал бы по 403.
resource "google_cloud_run_v2_job_iam_member" "scheduler_mart_prod_invoke" {
  location = var.region
  name     = google_cloud_run_v2_job.wb_mart_prod.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_prod.email}"
}
