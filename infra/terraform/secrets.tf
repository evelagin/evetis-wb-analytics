# Пустые секреты (без значений). Значения токенов кладёт владелец руками.
# Terraform НЕ хранит значения токенов.
locals {
  wb_secrets = ["WB_TOKEN_ANALYTICS", "WB_TOKEN_ADS", "WB_TOKEN_FINANCE"]
}

resource "google_secret_manager_secret" "wb" {
  for_each  = toset(local.wb_secrets)
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

# ── UNITKA 2.0 R6 ───────────────────────────────────────────────────────────
# WB_TOKEN_REPORTS создан владельцем вручную в Secret Manager (токен категории
# «Аналитика» для воронки). Terraform его НЕ создаёт и НЕ хранит значение — иначе
# apply попытался бы создать существующий ресурс и упал бы.
# Второй секрет не заводится: WB_TOKEN_ANALYTICS остаётся нетронутым.
data "google_secret_manager_secret" "wb_token_reports" {
  secret_id = "WB_TOKEN_REPORTS"
}

# Доступ ПОРЕСУРСНО и ровно одной identity — runtime-аккаунту загрузчиков.
# Не project-wide: sa-loaders-prod уже имеет secretAccessor на три секрета из
# local.wb_secrets, и расширять его права на весь проект нет основания.
resource "google_secret_manager_secret_iam_member" "prod_access_wb_token_reports" {
  secret_id = data.google_secret_manager_secret.wb_token_reports.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}
