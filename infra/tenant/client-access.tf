# ── Клиентский доступ к analytics_share (T6, ADR-08 «Клиентский интерфейс данных») ──
# Данные клиент читает только через ACL датасета analytics_share (main.tf). На проекте ему нужно
# одно право — запускать собственные запросы: roles/bigquery.jobUser не даёт ни одного права на
# данные, таблицы или датасеты. Ничего на ozon_raw/ref/ozon_mart/tenant_ops/tenant_locks, секреты,
# Cloud Run и Scheduler клиенту не выдаётся. Блок пуст, пока в реестре bi_access.mode != ANALYTICS_SHARE_VIEWER.
locals {
  client_project_members = var.contract.client_access == null ? {} : {
    for pair in setproduct(var.contract.client_access.project_roles, var.contract.client_access.principals) :
    "${pair[0]}|${pair[1].kind}:${pair[1].email}" => { role = pair[0], member = "${pair[1].kind}:${pair[1].email}" }
  }
}

resource "google_project_iam_member" "client_access" {
  for_each = local.client_project_members

  project = var.contract.project_id
  role    = each.value.role
  member  = each.value.member

  depends_on = [google_project_service.this]
}
