# Tenancy T6 — клиентский доступ к analytics_share (офлайн, мок провайдера).
# Фикстура client_access.synthetic — вывод реестра для client_001 с ВКЛЮЧЁННЫМ bi_access
# (синтетические principals; актуальность проверяет tools/tests/test_tenancy_t6_client_access.py).

mock_provider "google" {
  mock_data "google_project" {
    defaults = { folder_id = "881419274207", number = "123456789012", billing_account = "000000-000000-000000" }
  }
  mock_data "google_projects" {
    defaults = { projects = [{ project_id = "not-the-tenant", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
}

run "client_access_renders_reader_and_authorized_views_only" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_access.synthetic.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  # Клиент: ровно два READER на analytics_share (user и group), ни одной записи клиента в других датасетах.
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if a.role == "READER" && a.user_by_email == "analyst@example-client.test"]) == 1
    error_message = "T6: user principal must be READER on analytics_share"
  }
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if a.role == "READER" && a.group_by_email == "bi-readers@example-client.test"]) == 1
    error_message = "T6: group principal must be READER on analytics_share"
  }
  assert {
    condition = alltrue([for k in ["ozon_raw", "ref", "ozon_mart", "tenant_ops", "tenant_locks"] :
      length([for a in google_bigquery_dataset.this[k].access : a if contains(["analyst@example-client.test", "bi-readers@example-client.test"], coalesce(a.user_by_email, a.group_by_email, "-"))]) == 0
    ])
    error_message = "T6: the client principal must not appear in any internal dataset ACL"
  }
  # Авторизованные датасеты (VIEWS) ровно по реальным зависимостям вью пакета.
  assert {
    condition     = toset([for a in google_bigquery_dataset.this["ozon_mart"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["analytics_share", "tenant_ops"])
    error_message = "T6: ozon_mart must authorize the views of analytics_share and tenant_ops"
  }
  assert {
    condition     = toset([for a in google_bigquery_dataset.this["tenant_ops"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["analytics_share"])
    error_message = "T6: tenant_ops must authorize only the views of analytics_share"
  }
  assert {
    condition     = toset([for a in google_bigquery_dataset.this["ozon_raw"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["ozon_mart"]) && toset([for a in google_bigquery_dataset.this["ref"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["ozon_mart", "tenant_ops"])
    error_message = "T6: ozon_raw authorizes only ozon_mart views; ref authorizes ozon_mart and tenant_ops views"
  }
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if length(a.dataset) > 0]) == 0 && length([for a in google_bigquery_dataset.this["tenant_locks"].access : a if length(a.dataset) > 0]) == 0
    error_message = "T6: no authorized dataset on analytics_share itself or on tenant_locks"
  }
  assert {
    condition     = alltrue([for a in flatten([for k, d in google_bigquery_dataset.this : d.access]) : one(a.dataset).target_types == tolist(["VIEWS"]) if length(a.dataset) > 0])
    error_message = "T6: authorized datasets must target VIEWS only"
  }
  # Проект: только bigquery.jobUser, ровно двум principals; ничего больше на проекте клиенту.
  assert {
    condition     = toset([for m in google_project_iam_member.client_access : "${m.role}|${m.member}"]) == toset(["roles/bigquery.jobUser|user:analyst@example-client.test", "roles/bigquery.jobUser|group:bi-readers@example-client.test"])
    error_message = "T6: exactly jobUser for the two client principals on the tenant project"
  }
}

run "client_001_without_client_access_renders_nothing_for_a_client" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = length(google_project_iam_member.client_access) == 0 && length(flatten([for k, d in google_bigquery_dataset.this : [for a in d.access : a if length(a.dataset) > 0]])) == 0
    error_message = "T6: with bi_access.mode NONE the contract must not change any ACL or project IAM"
  }
}
