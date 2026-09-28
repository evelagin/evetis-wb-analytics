# Tenancy T3.2 — офлайн-доказательства корня арендатора (terraform test, мок провайдера).
# Ни облака, ни state: провайдер google подменён. Фикстуры — вывод реестра
# (tools/tenancy/synthetic.py); их актуальность проверяет tools/tests/test_tenancy_t32.py.
# По умолчанию поиск возвращает ЧУЖОЙ проект: каждый положительный прогон обязан явно
# подставить найденный проект арендатора, иначе guard его отвергнет.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { folder_id = "881419274207", number = "123456789012", billing_account = "000000-000000-000000" }
  }
  mock_data "google_projects" {
    defaults = { projects = [{ project_id = "not-the-tenant", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
}

run "client_001_renders" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = output.project_id == "mpa-t-client-001" && output.state_prefix == "tenants/client_001"
    error_message = "client_001: проект или префикс state не из реестра"
  }
  assert {
    condition     = length(google_bigquery_table.this) == 27 && toset(keys(google_bigquery_dataset.this)) == toset(["analytics_share", "ozon_mart", "ozon_raw", "ref", "tenant_ops"])
    error_message = "client_001: ожидается 27 таблиц (15 ozon_raw, 5 ref, 7 tenant_ops) и пять датасетов T4"
  }
  assert {
    condition     = output.ozon.jobs == tolist(["ozon-runtime-daily", "ozon-runtime-fast", "ozon-runtime-weekly"])
    error_message = "client_001: job'ы не совпадают с контрактом (promo не включён)"
  }
  assert {
    condition     = alltrue([for k, p in output.ozon.paused : p == true]) && length(output.ozon.paused) == 3
    error_message = "расписания обязаны быть на паузе (второй слой; первый — отсутствие run.invoker, ADR-08 И2)"
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
  assert {
    condition     = output.ozon.runtime_sa == "sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com" && output.ozon.scheduler_sa == "sa-ozon-scheduler@mpa-t-client-001.iam.gserviceaccount.com"
    error_message = "идентичности выводятся детерминированно и известны на плане"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ozon_raw"] : "${a.role}|${a.special_group == null ? "" : a.special_group}|${a.user_by_email == null ? "" : a.user_by_email}"]) == toset(["OWNER|projectOwners|", "WRITER||sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com", "organizations/1043233412973/roles/mpaSqlSourceRead||sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com"])
    error_message = "ACL ozon_raw: ровно projectOwners OWNER, runtime SA WRITER и чтение деплоера SQL (T3.3: без создателя-провижионера)"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ref"] : "${a.role}|${a.special_group == null ? "" : a.special_group}|${a.user_by_email == null ? "" : a.user_by_email}"]) == toset(["OWNER|projectOwners|", "READER||sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com", "organizations/1043233412973/roles/mpaSqlSourceRead||sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com"])
    error_message = "ACL ref: ровно projectOwners OWNER, runtime SA READER и чтение деплоера SQL"
  }
  assert {
    condition     = !strcontains(jsonencode(output.dataset_access), "sa-tenant-provisioner") && !strcontains(jsonencode(output.dataset_access), "sa-ozon-scheduler")
    error_message = "в ACL датасетов нет ни провижионера, ни SA планировщика"
  }
  assert {
    condition     = alltrue([for k, j in module.ozon[0].scheduler_retry : length(j) == 0])
    error_message = "retry_config у Scheduler не задаётся: default retryCount = 0, явный блок даёт вечный дрейф (T3.3)"
  }
  assert {
    condition     = length([for k, t in google_bigquery_table.this : k if t.dataset_id == "ozon_raw"]) == 15 && length([for k, t in google_bigquery_table.this : k if t.dataset_id == "ref"]) == 5 && length([for k, t in google_bigquery_table.this : k if t.dataset_id == "tenant_ops"]) == 7
    error_message = "T4: 15 таблиц ozon_raw, 5 справочников ref (включая SELLER_BINDING), 7 таблиц tenant_ops"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ozon_mart"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlSourceRead|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|"])
    error_message = "T4.1: ozon_mart — projectOwners и деплоер SQL (чтение, создание, изменение) без условия"
  }
  assert {
    condition     = toset([for a in output.dataset_access["analytics_share"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|"])
    error_message = "T4.1: analytics_share — деплоер создаёт и меняет, но НЕ читает строки; клиента нет"
  }
  assert {
    condition     = toset([for a in output.dataset_access["tenant_ops"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlSourceRead|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com|resource.type == \"bigquery.googleapis.com/Table\" && resource.service == \"bigquery.googleapis.com\" && resource.name.startsWith(\"projects/mpa-t-client-001/datasets/tenant_ops/tables/V_\")"])
    error_message = "T4.1: tenant_ops — изменение деплоером только с условием V_* (7 таблиц платформы вне гранта)"
  }
  assert {
    condition     = alltrue([for ds in ["ozon_raw", "ref"] : length([for a in output.dataset_access[ds] : a if a.user_by_email == "sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com"]) == 1 && anytrue([for a in output.dataset_access[ds] : a.role == "organizations/1043233412973/roles/mpaSqlSourceRead" && a.user_by_email == "sa-sql-deployer@mpa-t-client-001.iam.gserviceaccount.com" && a.condition == null])])
    error_message = "T4.1: в ozon_raw и ref деплоер только читает (mpaSqlSourceRead), без записи"
  }
  assert {
    condition     = google_service_account.sql_deployer.account_id == "sa-sql-deployer" && google_service_account.sql_deployer.project == "mpa-t-client-001"
    error_message = "T4.1: SA деплоера — в проекте арендатора"
  }
  assert {
    condition     = contains([for k, t in google_bigquery_table.this : k], "ref.SELLER_BINDING") && !contains([for k, t in google_bigquery_table.this : k], "tenant_ops.SELLER_BINDING")
    error_message = "подтверждённая привязка к кабинету живёт в ref (runtime только читает), а не в tenant_ops"
  }
}

run "client_002_same_code_different_tenant" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_002.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-002", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = output.project_id == "mpa-t-client-002" && output.state_prefix == "tenants/client_002"
    error_message = "client_002: проект или префикс state не из реестра"
  }
  assert {
    condition     = length(google_bigquery_table.this) == 27 && output.ozon.jobs == tolist(["ozon-runtime-daily", "ozon-runtime-fast", "ozon-runtime-weekly"])
    error_message = "client_002: форма графа ресурсов обязана совпадать с client_001"
  }
  assert {
    condition     = alltrue([for k, e in output.ozon.env : e["GCP_PROJECT_ID"] == "mpa-t-client-002"])
    error_message = "client_002: job'ы обязаны смотреть в свой проект"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ozon_raw"] : "${a.role}|${a.special_group == null ? "" : a.special_group}|${a.user_by_email == null ? "" : a.user_by_email}"]) == toset(["OWNER|projectOwners|", "WRITER||sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com", "organizations/1043233412973/roles/mpaSqlSourceRead||sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com"])
    error_message = "ACL ozon_raw: ровно projectOwners OWNER, runtime SA WRITER и чтение деплоера SQL (T3.3: без создателя-провижионера)"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ref"] : "${a.role}|${a.special_group == null ? "" : a.special_group}|${a.user_by_email == null ? "" : a.user_by_email}"]) == toset(["OWNER|projectOwners|", "READER||sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com", "organizations/1043233412973/roles/mpaSqlSourceRead||sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com"])
    error_message = "ACL ref: ровно projectOwners OWNER, runtime SA READER и чтение деплоера SQL"
  }
  assert {
    condition     = !strcontains(jsonencode(output.dataset_access), "sa-tenant-provisioner") && !strcontains(jsonencode(output.dataset_access), "sa-ozon-scheduler")
    error_message = "в ACL датасетов нет ни провижионера, ни SA планировщика"
  }
  assert {
    condition     = toset([for a in output.dataset_access["ozon_mart"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlSourceRead|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|"])
    error_message = "T4.1: ozon_mart — projectOwners и деплоер SQL (чтение, создание, изменение) без условия"
  }
  assert {
    condition     = toset([for a in output.dataset_access["analytics_share"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|"])
    error_message = "T4.1: analytics_share — деплоер создаёт и меняет, но НЕ читает строки; клиента нет"
  }
  assert {
    condition     = toset([for a in output.dataset_access["tenant_ops"] : "${a.role}|${a.user_by_email == null ? "" : a.user_by_email}|${a.condition == null ? "" : a.condition}"]) == toset(["OWNER||", "organizations/1043233412973/roles/mpaSqlSourceRead|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewCreate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|", "organizations/1043233412973/roles/mpaSqlViewUpdate|sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com|resource.type == \"bigquery.googleapis.com/Table\" && resource.service == \"bigquery.googleapis.com\" && resource.name.startsWith(\"projects/mpa-t-client-002/datasets/tenant_ops/tables/V_\")"])
    error_message = "T4.1: tenant_ops — изменение деплоером только с условием V_* (7 таблиц платформы вне гранта)"
  }
  assert {
    condition     = alltrue([for ds in ["ozon_raw", "ref"] : length([for a in output.dataset_access[ds] : a if a.user_by_email == "sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com"]) == 1 && anytrue([for a in output.dataset_access[ds] : a.role == "organizations/1043233412973/roles/mpaSqlSourceRead" && a.user_by_email == "sa-sql-deployer@mpa-t-client-002.iam.gserviceaccount.com" && a.condition == null])])
    error_message = "T4.1: в ozon_raw и ref деплоер только читает (mpaSqlSourceRead), без записи"
  }
  assert {
    condition     = google_service_account.sql_deployer.account_id == "sa-sql-deployer" && google_service_account.sql_deployer.project == "mpa-t-client-002"
    error_message = "T4.1: SA деплоера — в проекте арендатора"
  }
}

# T3.2b: настоящий утверждённый образ из runtime_release.json (фикстуры release/ — вывод
# synthetic.release_contract; совпадение с дескриптором и общий digest у обоих арендаторов
# проверяет tools/tests/test_tenancy_t32b.py). Образ один, проект/state/SA/окружение — свои.
run "release_client_001_uses_approved_digest" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/release/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = alltrue([for k, i in output.ozon.images : i == var.contract.marketplaces.ozon.runtime_image]) && length(output.ozon.images) == 3
    error_message = "все job'ы client_001 — на утверждённом digest выпуска"
  }
  assert {
    condition     = !strcontains(var.contract.marketplaces.ozon.runtime_image, "ffffffffffffffff")
    error_message = "фикстура выпуска обязана нести настоящий digest, а не синтетический"
  }
  assert {
    condition     = output.project_id == "mpa-t-client-001" && output.state_prefix == "tenants/client_001" && output.ozon.runtime_sa == "sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com"
    error_message = "client_001: проект, state и идентичность — свои"
  }
  assert {
    condition     = alltrue([for k, e in output.ozon.env : e["GCP_PROJECT_ID"] == "mpa-t-client-001"])
    error_message = "client_001: окружение — своего проекта"
  }
}

run "release_client_002_same_digest_different_tenant" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/release/client_002.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-002", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  assert {
    condition     = alltrue([for k, i in output.ozon.images : i == var.contract.marketplaces.ozon.runtime_image]) && length(output.ozon.images) == 3
    error_message = "все job'ы client_002 — на утверждённом digest выпуска"
  }
  assert {
    condition     = output.project_id == "mpa-t-client-002" && output.state_prefix == "tenants/client_002" && output.ozon.runtime_sa == "sa-ozon-runtime@mpa-t-client-002.iam.gserviceaccount.com" && output.ozon.scheduler_sa == "sa-ozon-scheduler@mpa-t-client-002.iam.gserviceaccount.com"
    error_message = "client_002: проект, state и идентичности — свои"
  }
  assert {
    condition     = alltrue([for k, e in output.ozon.env : e["GCP_PROJECT_ID"] == "mpa-t-client-002"])
    error_message = "client_002: окружение — своего проекта"
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
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
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
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_null_billing" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "881419274207", number = "123456789012", billing_account = null }
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
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
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "37074083763", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_platform_project_number" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "881419274207", number = "777428383056", billing_account = "000000-000000-000000" }
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "777428383056", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_search_returning_other_project" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-002", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_default_mock_search_result" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_search_parent_outside_tenants" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "123456789012", lifecycle_state = "ACTIVE", parent = { id = "1043233412973", type = "organization" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_number_mismatch_between_apis" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "999999999999", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
  }
  expect_failures = [terraform_data.guard]
}

run "guard_rejects_empty_project_number" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/client_001.contract.json"))
  }
  override_data {
    target = data.google_project.tenant
    values = { folder_id = "881419274207", number = "", billing_account = "000000-000000-000000" }
  }
  override_data {
    target = data.google_projects.tenant_active
    values = { projects = [{ project_id = "mpa-t-client-001", number = "", lifecycle_state = "ACTIVE", parent = { id = "881419274207", type = "folder" } }] }
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

run "contract_rejects_extra_customer_dataset" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/extra_customer_dataset.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_table_in_undeclared_dataset" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/table_in_undeclared_dataset.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_foreign_project_sa" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_foreign_project_sa.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_other_account" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_other_account.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_predefined_role" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_predefined_role.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_unconditional_tenant_ops_update" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_unconditional_tenant_ops_update.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_broad_condition" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_broad_condition.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_condition_on_source_read" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_condition_on_source_read.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_duplicate_grant" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_duplicate_grant.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_grant_in_undeclared_dataset" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_grant_in_undeclared_dataset.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_extra_raw_update" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_extra_raw_update.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_client_layer_reads_rows" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_client_layer_reads_rows.contract.json"))
  }
  expect_failures = [var.contract]
}

run "contract_rejects_sql_deployer_condition_title_changed" {
  command = plan
  variables {
    contract = jsondecode(file("tests/fixtures/negative/sql_deployer_condition_title_changed.contract.json"))
  }
  expect_failures = [var.contract]
}
