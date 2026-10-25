# ── Клиентский доступ к analytics_share (T6, ADR-08 «Клиентский интерфейс данных») ──
# Данные клиент читает только через ACL датасета analytics_share (main.tf). На проекте ему нужно
# одно — запускать собственные запросы. Для этого пользовательская роль проекта vtsClientQueryRunner
# (bigquery.jobs.create, bigquery.config.get, resourcemanager.projects.get), а НЕ roles/bigquery.jobUser:
# в jobUser на 2026-10-25 есть dataform.repositories.create / dataform.folders.create /
# cloudaicompanion.topics.create, а Dataform API в проекте арендатора включён — клиент мог бы создавать
# ресурсы. Ничего на ozon_raw/ref/ozon_mart/tenant_ops/tenant_locks, секреты, Cloud Run и Scheduler клиенту
# не выдаётся. Блок пуст, пока в реестре bi_access.mode != ANALYTICS_SHARE_VIEWER.
locals {
  client_query_role = var.contract.client_access == null ? null : var.contract.client_access.query_role
  client_project_members = var.contract.client_access == null ? {} : {
    for p in var.contract.client_access.principals : "${p.kind}:${p.email}" => "${p.kind}:${p.email}"
  }
}

resource "google_project_iam_custom_role" "client_query" {
  count       = local.client_query_role == null ? 0 : 1
  project     = var.contract.project_id
  role_id     = local.client_query_role.role_id
  title       = "Tenant client query runner"
  description = "T6: клиент запускает собственные запросы к analytics_share; прав на данные и создание ресурсов нет"
  permissions = local.client_query_role.permissions
  stage       = "GA"

  depends_on = [google_project_service.this]
}

resource "google_project_iam_member" "client_access" {
  for_each = local.client_project_members

  project = var.contract.project_id
  # Имя роли — из контракта, известно на плане (сканер плана сверяет тройку проект/роль/принципал).
  role   = "projects/${var.contract.project_id}/roles/${local.client_query_role.role_id}"
  member = each.value

  depends_on = [google_project_iam_custom_role.client_query]
}
