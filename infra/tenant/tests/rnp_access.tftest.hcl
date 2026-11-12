# РНП владельца — узкое исключение из T6 (tools/tenancy/rnp_access.py), офлайн, мок провайдера.
# Фикстура rnp_access.client_001 — вывод реестра client_001 с rnp_access = OWNER_RNP_SERVICE (bi_access = NONE);
# актуальность проверяет tools/tests/test_tenancy_rnp_access.py.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { folder_id = "881419274207", number = "123456789012", billing_account = "000000-000000-000000" }
  }
  mock_data "google_projects" {
    defaults = { projects = [{ project_id = "not-the-tenant", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
}

run "rnp_access_renders_one_reader_and_t6_edges_only" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/rnp_access.client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if a.role == "READER" && a.user_by_email == "sa-rnp-web@mpa-t-client-001.iam.gserviceaccount.com"]) == 1
    error_message = "РНП: sa-rnp-web must be READER on analytics_share exactly once"
  }
  assert {
    condition = alltrue([for k in ["ozon_raw", "ref", "ozon_mart", "tenant_ops", "tenant_locks"] :
      length([for a in google_bigquery_dataset.this[k].access : a if a.user_by_email == "sa-rnp-web@mpa-t-client-001.iam.gserviceaccount.com"]) == 0
    ])
    error_message = "РНП: sa-rnp-web must not appear in any internal dataset ACL"
  }
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if a.group_by_email != null]) == 0 && length([for a in google_bigquery_dataset.this["analytics_share"].access : a if a.role == "READER"]) == 1
    error_message = "РНП: no client identities on analytics_share (bi_access = NONE)"
  }
  assert {
    condition     = toset([for a in google_bigquery_dataset.this["ozon_mart"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["analytics_share", "tenant_ops"]) && toset([for a in google_bigquery_dataset.this["tenant_ops"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["analytics_share"])
    error_message = "РНП: authorized datasets on ozon_mart/tenant_ops equal the T6 edges"
  }
  assert {
    condition     = toset([for a in google_bigquery_dataset.this["ozon_raw"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["ozon_mart"]) && toset([for a in google_bigquery_dataset.this["ref"].access : one(a.dataset).dataset[0].dataset_id if length(a.dataset) > 0]) == toset(["ozon_mart", "tenant_ops"])
    error_message = "РНП: ozon_raw authorizes only ozon_mart views; ref authorizes ozon_mart and tenant_ops views"
  }
  assert {
    condition     = length([for a in google_bigquery_dataset.this["analytics_share"].access : a if length(a.dataset) > 0]) == 0 && length([for a in google_bigquery_dataset.this["tenant_locks"].access : a if length(a.dataset) > 0]) == 0
    error_message = "РНП: no authorized dataset on analytics_share itself or on tenant_locks"
  }
  assert {
    condition     = length(google_project_iam_member.client_access) == 0 && length(google_project_iam_custom_role.client_query) == 0
    error_message = "РНП: no project roles and no client query role in the tenant root"
  }
}

run "rnp_access_rejects_other_service_account" {
  command = plan
  variables {
    contract = merge(jsondecode(file("tests/fixtures/rnp_access.client_001.contract.json")), {
      rnp_access = merge(jsondecode(file("tests/fixtures/rnp_access.client_001.contract.json")).rnp_access, {
        service_account = "sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com"
      })
    })
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [var.contract]
}

run "rnp_access_rejects_internal_dataset" {
  command = plan
  variables {
    contract = merge(jsondecode(file("tests/fixtures/rnp_access.client_001.contract.json")), {
      rnp_access = merge(jsondecode(file("tests/fixtures/rnp_access.client_001.contract.json")).rnp_access, {
        dataset_key = "ozon_mart"
      })
    })
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [var.contract]
}
