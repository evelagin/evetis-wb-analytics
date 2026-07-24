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
