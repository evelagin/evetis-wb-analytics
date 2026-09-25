# Tenancy T3.2 — офлайн-доказательства корня арендатора (terraform test, мок провайдера).
# Ни облака, ни state: провайдер google подменён. Фикстуры — вывод реестра
# (tools/tenancy/synthetic.py); их актуальность проверяет tools/tests/test_tenancy_t32.py.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      folder_id       = "881419274207"
      number          = "123456789012"
      billing_account = "000000-000000-000000"
    }
  }
  mock_data "google_projects" {
    defaults = {
      projects = [{ project_id = "mock-tenant-project" }]
    }
  }
}

run "client_001_renders" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  assert {
    condition     = output.project_id == "mpa-t-client-001" && output.state_prefix == "tenants/client_001"
    error_message = "client_001: проект или префикс state не из реестра"
  }
  assert {
    condition     = length(google_bigquery_table.this) == 15 && toset(keys(google_bigquery_dataset.this)) == toset(["ozon_raw", "ref"])
    error_message = "client_001: ожидается 15 таблиц runtime и датасеты ozon_raw, ref"
  }
  assert {
    condition     = output.ozon.jobs == tolist(["ozon-runtime-daily", "ozon-runtime-fast", "ozon-runtime-weekly"])
    error_message = "client_001: job'ы не совпадают с контрактом (promo не включён)"
  }
  assert {
    condition     = alltrue([for k, p in output.ozon.paused : p == true]) && length(output.ozon.paused) == 3
    error_message = "расписания обязаны создаваться на паузе"
  }
  assert {
    condition     = alltrue([for k, i in output.ozon.images : i == "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"])
    error_message = "образ job'ов — только утверждённый digest контракта"
  }
  assert {
    condition     = alltrue([for k, e in output.ozon.env : e["GCP_PROJECT_ID"] == "mpa-t-client-001" && e["BQ_REF_DATASET"] == "ref" && e["STRICT_PAGE_CAPS"] == "1" && e["OZON_SECRET_SELLER_API_KEY"] == "ozon-seller-api-key"])
    error_message = "окружение job'ов: проект, ref, строгий режим и имена секретов — только арендатора"
  }
  assert {
    condition     = output.ozon.secrets == tolist(["performance_client_id", "performance_client_secret", "seller_api_key", "seller_client_id"])
    error_message = "контейнеры секретов — ровно четыре роли Ozon"
  }
}

run "client_002_same_code_different_tenant" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_002.contract.json"))
  }
  assert {
    condition     = output.project_id == "mpa-t-client-002" && output.state_prefix == "tenants/client_002"
    error_message = "client_002: проект или префикс state не из реестра"
  }
  assert {
    condition     = length(google_bigquery_table.this) == 15 && output.ozon.jobs == tolist(["ozon-runtime-daily", "ozon-runtime-fast", "ozon-runtime-weekly"])
    error_message = "client_002: форма графа ресурсов обязана совпадать с client_001"
  }
  assert {
    condition     = alltrue([for k, e in output.ozon.env : e["GCP_PROJECT_ID"] == "mpa-t-client-002"])
    error_message = "client_002: job'ы обязаны смотреть в свой проект"
  }
}

run "guard_rejects_project_outside_tenants_folder" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "999999999999", number = "123456789012", billing_account = "000000-000000-000000" }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_project_without_billing" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "881419274207", number = "123456789012", billing_account = "" }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_inactive_or_missing_project" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_evetis_project_number" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "881419274207", number = "37074083763", billing_account = "000000-000000-000000" }
  }
  expect_failures = [terraform_data.guard]
}

run "contract_rejects_arbitrary_project" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/arbitrary_project.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_evetis_project" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/evetis_project.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_evetis_ref_dataset" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/evetis_ref_dataset.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_evetis_secret_name" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/evetis_secret_name.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_evetis_state_bucket" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/evetis_state_bucket.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_foreign_state_prefix" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/foreign_state_prefix.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_foreign_tenant_project" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/foreign_tenant_project.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_empty" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_empty.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_evetis_registry" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_evetis_registry.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_latest_tag" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_latest_tag.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_missing" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_missing.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_mutable_tag" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_mutable_tag.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_image_short_digest" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/image_short_digest.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_org_root_parent" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/org_root_parent.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_platform_state_prefix" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/platform_state_prefix.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_scheduler_enabled" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/scheduler_enabled.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_unknown_contract_version" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/unknown_contract_version.contract.json"))
  }
  expect_failures = [var.contract]
}
