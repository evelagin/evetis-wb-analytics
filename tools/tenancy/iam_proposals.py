"""Предложения IAM для ворот T3.3 (Tenancy T3.2). НИЧЕГО ИЗ ЭТОГО НЕ ПРИМЕНЕНО.

Данные, а не код применения: тесты проверяют исключения (tools/tests/test_tenancy_t32.py),
владелец решает, какие роли создать (нужен roles/iam.organizationRoleAdmin на org —
у владельца его нет, T3.1B). Все разрешения проверены 2026-09-25 по
`gcloud iam list-testable-permissions //cloudresourcemanager.googleapis.com/folders/881419274207`:
тестируемы на папке и поддерживаются в пользовательских ролях (SUPPORTED).
"""
from __future__ import annotations

from tools.tenancy import platform as PL

# ── Роль «контейнеры секретов без значений» (решение T3.3, вариант B) ───────
SECRET_CONTAINER_ROLE = {
    "role_id": "mpaTenantSecretContainerAdmin",
    "parent": f"organizations/{PL.ORGANIZATION_ID}",
    "bind_on": PL.TENANTS_FOLDER,
    "title": "VTS tenant secret containers (no payload access)",
    "permissions": sorted([
        "secretmanager.locations.get",
        "secretmanager.locations.list",
        "secretmanager.secrets.create",
        "secretmanager.secrets.get",
        "secretmanager.secrets.getIamPolicy",
        "secretmanager.secrets.list",
        "secretmanager.secrets.setIamPolicy",   # доступ runtime SA к своим 4 секретам
        "secretmanager.secrets.update",         # метки/аннотации контейнера
    ]),
}

# ── Минимальная роль провижионера вместо admin-ролей T3.1B ──────────────────
# Заменяет run.admin, cloudscheduler.admin, bigquery.admin, iam.serviceAccountAdmin,
# serviceusage.serviceUsageAdmin (и secret-роль выше). Остаются отдельными привязками:
# iam.serviceAccountUser (actAs) и условный resourcemanager.projectIamAdmin (allow-list).
PROVISIONER_ROLE = {
    "role_id": "mpaTenantProvisioner",
    "parent": f"organizations/{PL.ORGANIZATION_ID}",
    "bind_on": PL.TENANTS_FOLDER,
    "title": "VTS tenant provisioner (no data read, no execution, no delete)",
    "permissions": sorted([
        # чтение проекта для guards
        "resourcemanager.projects.get",
        # API арендатора: включить и учитывать квоту; выключать — нет
        "serviceusage.services.enable", "serviceusage.services.get", "serviceusage.services.list",
        "serviceusage.services.use",
        # SA арендатора: создать и обновить; ни ключей, ни токенов, ни удаления
        "iam.serviceAccounts.create", "iam.serviceAccounts.get", "iam.serviceAccounts.list",
        "iam.serviceAccounts.update",
        # BigQuery: структура и ACL датасетов; строк не читает, не пишет, не удаляет
        "bigquery.datasets.create", "bigquery.datasets.get", "bigquery.datasets.update",
        "bigquery.datasets.getIamPolicy", "bigquery.datasets.setIamPolicy",
        "bigquery.tables.create", "bigquery.tables.get", "bigquery.tables.list", "bigquery.tables.update",
        # Cloud Run: описать job'ы и их IAM; запускать — нет
        "run.jobs.create", "run.jobs.get", "run.jobs.list", "run.jobs.update",
        "run.jobs.getIamPolicy", "run.jobs.setIamPolicy", "run.operations.get", "run.locations.list",
        # Scheduler: создать на паузе; снимать паузу и запускать — нет
        "cloudscheduler.jobs.create", "cloudscheduler.jobs.get", "cloudscheduler.jobs.list",
        "cloudscheduler.jobs.update", "cloudscheduler.jobs.pause",
        "cloudscheduler.locations.get", "cloudscheduler.locations.list",
    ] + SECRET_CONTAINER_ROLE["permissions"]),
}

# Разрешения, которых в ролях провижионера не должно быть НИКОГДА.
NEVER_FOR_PROVISIONER = (
    "secretmanager.versions.",                       # значения секретов: чтение/создание
    "bigquery.tables.getData", "bigquery.tables.updateData", "bigquery.tables.export",
    "bigquery.tables.delete", "bigquery.datasets.delete", "bigquery.jobs.",
    "run.jobs.run", "run.jobs.runWithOverrides", "run.jobs.delete", "run.executions.",
    "cloudscheduler.jobs.run", "cloudscheduler.jobs.enable", "cloudscheduler.jobs.delete",
    "iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.signBlob", "iam.serviceAccounts.signJwt",
    "iam.serviceAccounts.getOpenIdToken", "iam.serviceAccounts.implicitDelegation",
    "iam.serviceAccounts.delete", "iam.serviceAccountKeys.",
    "serviceusage.services.disable",
    "resourcemanager.projects.delete", "resourcemanager.projects.create", "resourcemanager.projects.move",
    "resourcemanager.projects.updateLiens", "resourcemanager.folders.", "resourcemanager.organizations.",
    "billing.", "orgpolicy.",
)

# Косвенный путь к данным, который остаётся (честно): управление ACL датасетов и секретов
# позволяет провижионеру выдать себе доступ. Закрывается deny-политикой ниже, а не allow.
TRANSITIVE_DATA_PATHS = ("bigquery.datasets.setIamPolicy", "secretmanager.secrets.setIamPolicy",
                         "run.jobs.update + iam.serviceAccounts.actAs (код не исполнить без run/enable)")

# ── Deny-политика на tenants/ (предложение, OD-9-подобная, но для провижионера) ─
DENY_POLICY = {
    "attachment_point": f"cloudresourcemanager.googleapis.com/{PL.TENANTS_FOLDER}",
    "policy_id": "deny-provisioner-data-plane",
    "denied_principals": [
        f"principal://iam.googleapis.com/projects/-/serviceAccounts/{PL.PROVISIONER_SA}"],
    "denied_permissions": [
        "bigquery.googleapis.com/tables.getData",
        "secretmanager.googleapis.com/versions.access",
        "secretmanager.googleapis.com/versions.add",
    ],
}

# ── Кросс-проектный реестр образов: точный план привязок T3.3 ───────────────
ARTIFACT_REGISTRY_BINDINGS = {
    "repository": f"projects/{PL.PLATFORM_PROJECT_ID}/locations/{PL.RUNTIME_REGION}/repositories/"
                  f"{PL.RUNTIME_REPOSITORY}",
    "bindings": [
        {"member": f"serviceAccount:{PL.PROVISIONER_SA}", "role": "roles/artifactregistry.reader",
         "why": "развёртывание job'а с образом другого проекта требует чтения репозитория у деплоящего"},
        {"member": "serviceAccount:service-<TENANT_PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com",
         "role": "roles/artifactregistry.reader",
         "why": "сервис-агент Cloud Run проекта арендатора тянет образ при каждом запуске"},
    ],
    "not_needed": ["runtime SA арендатора (образ тянет сервис-агент, не исполняющая идентичность)",
                   "планировщик арендатора", "любой принципал EVETIS", "запись в репозиторий кем-либо, кроме выпуска T3.2b"],
}
