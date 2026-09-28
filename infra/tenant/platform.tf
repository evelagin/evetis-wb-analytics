# Неизменяемые факты платформы (T3.1A/T3.1B). Второй, НЕЗАВИСИМЫЙ от контракта
# источник: guards сверяют контракт с этими значениями, а не контракт сам с собой.
# Совпадение с tools/tenancy/platform.py проверяет tools/tests/test_tenancy_t32.py.
locals {
  platform = {
    tenants_folder_id = "881419274207"
    project_id        = "mpa-platform"
    project_number    = "777428383056"
    state_bucket      = "mpa-platform-tfstate-777428383056"
    region            = "europe-west1"
    runtime_registry  = "europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime"
    organization_id   = "1043233412973"
    # T4.1: идентичность развёртывания SQL и роли организации, созданные владельцем 2026-09-28.
    # Совпадение с tools/tenancy/sql_identity.py проверяет tools/tests/test_tenancy_sql_deployer.py.
    sql_deployer_account_id = "sa-sql-deployer"
    sql_roles               = ["mpaSqlSourceRead", "mpaSqlViewCreate", "mpaSqlViewUpdate"]
    sql_conditional_dataset = "tenant_ops"
    sql_view_prefix         = "V_"
    sql_condition_title     = "sql-deployer-tenant-ops-views"
    sql_condition_desc      = "Only package views V_*; platform tables of tenant_ops are excluded"
    # Точная матрица «датасет|роль|условная» (sql_identity.GRANT_MATRIX; сверяет тест).
    sql_grant_matrix = [
      "analytics_share|mpaSqlViewCreate|false", "analytics_share|mpaSqlViewUpdate|false",
      "ozon_mart|mpaSqlSourceRead|false", "ozon_mart|mpaSqlViewCreate|false", "ozon_mart|mpaSqlViewUpdate|false",
      "ozon_raw|mpaSqlSourceRead|false", "ref|mpaSqlSourceRead|false",
      "tenant_ops|mpaSqlSourceRead|false", "tenant_ops|mpaSqlViewCreate|false", "tenant_ops|mpaSqlViewUpdate|true",
    ]
  }

  # Всё, что не может появиться в конфигурации выделенного арендатора.
  evetis_forbidden_markers = [
    "project-fa311fc0-4d87-4781-986",
    "37074083763",
    "evetis-wb-tfstate-37074083763",
    "evetis_ref",
    "EVETIS_OZON_",
  ]
}
