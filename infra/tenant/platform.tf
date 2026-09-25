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
