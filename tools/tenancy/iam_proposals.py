"""Предложения IAM для ворот T3.3 (Tenancy T3.2, пересобрано после ревью M4). НЕ ПРИМЕНЕНО.

Данные, а не код применения. Владелец решает, что создать; для пользовательских ролей на
org нужен roles/iam.organizationRoleAdmin (у владельца его нет, T3.1B). Разрешения проверены
2026-09-25 по `gcloud iam list-testable-permissions` для folders/881419274207.

Классы разрешений (M4):
  REQUIRED_NOW        — нужно при каждом плане/применении корня infra/tenant (чтение, refresh).
  REQUIRED_T3_3_ONLY  — нужно только при создании ресурсов арендатора (онбординг).
  NEEDS_LIVE_PROOF    — используется ли провайдером, доказывается на первом реальном плане T3.3;
                        не подтвердилось — удалить.
  REMOVE              — не нужно корню; было в admin-ролях T3.1B или в первом предложении.

Словарь возможностей (L1b): прямое право; косвенная возможность (цепочка прав);
предотвращено (чем именно); будущая активация (кто и когда).
"""
from __future__ import annotations

from tools.tenancy import platform as PL

REQUIRED_NOW, REQUIRED_T3_3_ONLY, NEEDS_LIVE_PROOF, REMOVE = (
    "REQUIRED_NOW", "REQUIRED_T3_3_ONLY", "NEEDS_LIVE_PROOF", "REMOVE")

# (разрешение, класс, зачем, косвенная возможность / риск)
PROVISIONER_PERMISSIONS = [
    ("resourcemanager.projects.get", REQUIRED_NOW, "guard: data.google_project, projects.search; getBillingInfo",
     "чтение метаданных проекта арендатора"),
    ("serviceusage.services.use", REQUIRED_NOW, "user_project_override: квота на проект арендатора",
     "вызовы API за счёт арендатора"),
    ("serviceusage.services.get", REQUIRED_NOW, "refresh google_project_service", "—"),
    ("serviceusage.services.list", NEEDS_LIVE_PROOF, "провайдер может читать сервисы пакетно", "—"),
    ("serviceusage.operations.get", NEEDS_LIVE_PROOF, "ожидание LRO включения API (уровень TESTING)", "—"),
    ("serviceusage.services.enable", REQUIRED_T3_3_ONLY, "включение API арендатора",
     "может включить ЛЮБОЙ API в проекте арендатора (стоимость, поверхность атаки)"),
    ("iam.serviceAccounts.get", REQUIRED_NOW, "refresh SA", "—"),
    ("iam.serviceAccounts.actAs", REQUIRED_NOW,
     "job Cloud Run исполняется от runtime SA; OAuth планировщика — от scheduler SA",
     "в связке с cloudscheduler.jobs.create — вызовы API Google от имени любого SA арендатора (см. INDIRECT)"),
    ("iam.serviceAccounts.create", REQUIRED_T3_3_ONLY, "создание sa-ozon-runtime/sa-ozon-scheduler",
     "может создать дополнительные SA и действовать от них (actAs)"),
    ("bigquery.datasets.get", REQUIRED_NOW, "refresh датасетов", "метаданные, не строки"),
    ("bigquery.datasets.getIamPolicy", NEEDS_LIVE_PROOF,
     "чтение ACL датасета при accessPolicyVersion=3 (authoritative access, T3.3); снять, если журнал "
     "аудита первого apply его не покажет", "чтение ACL датасета"),
    ("bigquery.datasets.setIamPolicy", NEEDS_LIVE_PROOF,
     "ACL в datasets.insert/patch при accessPolicyVersion=3 (authoritative access, T3.3); снять, если "
     "журнал аудита первого apply его не покажет",
     "может выдать доступ к строкам любому принципалу, допущенному org policy (см. CONTAINMENT)"),
    ("bigquery.datasets.update", NEEDS_LIVE_PROOF,
     "снятие дрейфа авторитетного access (в т.ч. создателя, если API его добавит), T3.3",
     "может задать default table expiration — будущие таблицы удаляются сами; менять ACL"),
    ("bigquery.datasets.create", REQUIRED_T3_3_ONLY, "создание ozon_raw/ref", "—"),
    ("bigquery.tables.get", REQUIRED_NOW, "refresh таблиц", "схема и метаданные, не строки"),
    ("bigquery.tables.create", REQUIRED_T3_3_ONLY, "создание таблиц runtime",
     "может создать таблицу или представление; читать строки без getData/jobs не может"),
    ("run.jobs.get", REQUIRED_NOW, "refresh job'ов", "—"),
    ("run.operations.get", REQUIRED_NOW, "ожидание LRO create/update job'а", "—"),
    ("run.jobs.create", REQUIRED_T3_3_ONLY, "создание job'ов Ozon",
     "может создать job с любым образом от любого SA, на который есть actAs; запустить его сам не может"),
    ("run.jobs.update", REQUIRED_NOW, "перепривязка утверждённого digest (выпуск/откат)",
     "может подменить образ/команду/env job'а; исполнение — только через вызов (см. PREVENTED/FUTURE)"),
    ("cloudscheduler.jobs.get", REQUIRED_NOW, "refresh расписаний", "—"),
    ("cloudscheduler.jobs.create", REQUIRED_T3_3_ONLY, "создание расписаний (API создаёт их ENABLED)",
     "ИСПОЛНЕНИЕ: созданное задание сразу активно и может вызвать любой API Google с OAuth-токеном "
     "любого SA, на который есть actAs (см. INDIRECT)"),
    ("cloudscheduler.jobs.pause", REQUIRED_T3_3_ONLY, "провайдер ставит паузу вторым вызовом после create", "—"),
    ("secretmanager.secrets.get", REQUIRED_NOW, "refresh контейнеров", "метаданные, не значения"),
    ("secretmanager.secrets.getIamPolicy", REQUIRED_NOW, "refresh google_secret_manager_secret_iam_member", "—"),
    ("secretmanager.secrets.create", REQUIRED_T3_3_ONLY, "создание 4 контейнеров Ozon", "—"),
    ("secretmanager.secrets.setIamPolicy", REQUIRED_T3_3_ONLY, "secretAccessor для runtime SA",
     "может выдать доступ к значениям любому принципалу, допущенному org policy (см. CONTAINMENT)"),
    # ── REMOVE: не нужно корню; было в admin-ролях T3.1B или в первом предложении ──
    ("iam.serviceAccounts.update", REMOVE, "описание SA меняется только через новые ворота", "—"),
    ("iam.serviceAccounts.list", REMOVE, "провайдер читает SA по имени", "—"),
    ("bigquery.tables.update", REMOVE, "эволюция схемы — отдельные ворота",
     "могла задать expirationTime — удаление таблицы без права delete"),
    ("bigquery.tables.list", REMOVE, "корень не перечисляет таблицы", "—"),
    ("run.jobs.list", REMOVE, "—", "—"),
    ("run.locations.list", REMOVE, "—", "—"),
    ("run.jobs.getIamPolicy", REMOVE, "IAM job'ов в базовом корне нет (модель H2)", "—"),
    ("run.jobs.setIamPolicy", REMOVE, "run.invoker выдают только ворота активации",
     "мог выдать вызов job'а любому принципалу"),
    ("cloudscheduler.jobs.update", REMOVE, "смена каденции — отдельные ворота",
     "мог перенацелить расписание (URI, OAuth SA)"),
    ("cloudscheduler.jobs.list", REMOVE, "—", "—"),
    ("cloudscheduler.locations.get", REMOVE, "—", "—"),
    ("cloudscheduler.locations.list", REMOVE, "—", "—"),
    ("secretmanager.secrets.update", REMOVE, "метки контейнера меняются только через новые ворота",
     "мог задать expire_time/ttl — удаление секрета без права delete"),
    ("secretmanager.secrets.list", REMOVE, "—", "—"),
    ("secretmanager.locations.get", REMOVE, "—", "—"),
    ("secretmanager.locations.list", REMOVE, "—", "—"),
]

PROVISIONER_ROLE = {
    "role_id": "mpaTenantProvisioner",
    "parent": f"organizations/{PL.ORGANIZATION_ID}",
    "bind_on": PL.TENANTS_FOLDER,
    "title": "VTS tenant provisioner (see CAPABILITIES: indirect execution/exposure paths exist)",
    "permissions": sorted(p for p, cls, _w, _r in PROVISIONER_PERMISSIONS if cls != REMOVE),
}
# Отдельной привязкой остаётся условный resourcemanager.projectIamAdmin (hasOnly
# roles/bigquery.jobUser, T3.1B). serviceAccountUser заменяется iam.serviceAccounts.actAs в роли.

SECRET_CONTAINER_PERMISSIONS = [p for p, _c, _w, _r in PROVISIONER_PERMISSIONS if p.startswith("secretmanager.")
                                and _c != REMOVE]
SECRET_CONTAINER_ROLE = {
    "role_id": "mpaTenantSecretContainerAdmin",
    "parent": f"organizations/{PL.ORGANIZATION_ID}",
    "bind_on": PL.TENANTS_FOLDER,
    "title": "VTS tenant secret containers (no version/payload permissions; setIamPolicy is an exposure path)",
    "permissions": sorted(SECRET_CONTAINER_PERMISSIONS),
    "note": "Входит в PROVISIONER_ROLE; отдельной ролью — только если секреты решено вести отдельно.",
}

NEVER_FOR_PROVISIONER = (
    "secretmanager.versions.",
    "bigquery.tables.getData", "bigquery.tables.updateData", "bigquery.tables.export",
    "bigquery.tables.delete", "bigquery.datasets.delete", "bigquery.jobs.",
    "bigquery.tables.update", "secretmanager.secrets.update",                 # expiration = удаление
    "run.jobs.run", "run.jobs.runWithOverrides", "run.jobs.delete", "run.executions.",
    "run.jobs.setIamPolicy",
    "cloudscheduler.jobs.run", "cloudscheduler.jobs.enable", "cloudscheduler.jobs.delete",
    "cloudscheduler.jobs.update",
    "iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.signBlob", "iam.serviceAccounts.signJwt",
    "iam.serviceAccounts.getOpenIdToken", "iam.serviceAccounts.implicitDelegation",
    "iam.serviceAccounts.delete", "iam.serviceAccounts.setIamPolicy", "iam.serviceAccountKeys.",
    "serviceusage.services.disable",
    "resourcemanager.projects.delete", "resourcemanager.projects.create", "resourcemanager.projects.move",
    "resourcemanager.projects.updateLiens", "resourcemanager.folders.", "resourcemanager.organizations.",
    "billing.", "orgpolicy.",
)

# ── Возможности провижионера (L1b: точные формулировки) ─────────────────────
CAPABILITIES = {
    "DIRECT": [
        "создавать API, SA, датасеты, таблицы, контейнеры секретов, job'ы Cloud Run и задания Scheduler "
        "в проектах под tenants/",
        "обновлять образ/команду/env job'ов (run.jobs.update)",
        "выдавать ровно roles/bigquery.jobUser на проект (условие hasOnly) и роли на датасеты/секреты "
        "(setIamPolicy) — членам, допущенным org policy",
        "читать и писать объекты state tenants/* в бакете платформы (всех арендаторов)",
    ],
    "INDIRECT": [
        "ИСПОЛНЕНИЕ: cloudscheduler.jobs.create создаёт ENABLED задание; с actAs на runtime SA оно "
        "вызывает любой API Google от имени runtime SA — например, BigQuery EXPORT DATA строк арендатора "
        "в чужой бакет, открытый на запись. Это путь выноса строк внутри границы арендатора.",
        "ИСПОЛНЕНИЕ КОДА ПРИ АКТИВАЦИИ: подменённый run.jobs.update образ исполнится, когда ворота "
        "активации выдадут run.invoker. Ворота активации обязаны сверять образ с утверждённым digest.",
        "ВЫДАЧА ДОСТУПА: setIamPolicy на датасеты и секреты — любому принципалу, которого пропускает "
        "org policy (после CONTAINMENT: SA внутри организации и владелец; до неё — кому угодно).",
        "САМОВЫДАЧА: роль jobUser себе + dataViewer себе на датасет = чтение строк самим провижионером, "
        "если не стоит deny-политика OPTIONAL_DEFENSE_IN_DEPTH.",
        "СТОИМОСТЬ: включение любых API и создание ресурсов в проектах арендаторов.",
    ],
    "PREVENTED": [
        "значения секретов напрямую: нет secretmanager.versions.* (ни access, ни add)",
        "вызов job'ов Cloud Run: нет run.jobs.run; у SA планировщика и runtime SA нет run.invoker",
        "удаление: нет *.delete; update-права с expiration (tables.update, secrets.update) исключены",
        "токены и ключи SA: нет getAccessToken/sign*/keys; ключи SA ещё и запрещены org policy",
        "IAM папки, организации, проектов EVETIS и платформы; создание/удаление/перенос проектов; liens; биллинг",
        "state platform/*: условие на префикс tenants/ в IAM бакета",
    ],
    "FUTURE_ACTIVATION": [
        "run.invoker для sa-ozon-scheduler на job'ы арендатора и снятие паузы расписаний выдаёт "
        "отдельная идентичность ворот активации (T5/T6) после ключей, сверки и решения владельца; "
        "провижионер этих прав не получает никогда",
    ],
}

# Текущие (живые) admin-роли T3.1B сильнее предложения: run.admin включает run.jobs.run,
# bigquery.admin — чтение строк, serviceAccountAdmin — setIamPolicy на SA (самовыдача
# serviceAccountTokenCreator). Замена — ворота T3.3 (P2-9).
LIVE_T3_1B_EXCESS = ("run.jobs.run (roles/run.admin)", "bigquery.tables.getData (roles/bigquery.admin)",
                     "iam.serviceAccounts.setIamPolicy (roles/iam.serviceAccountAdmin)",
                     "cloudscheduler.jobs.enable/run (roles/cloudscheduler.admin)")

# ── Сдерживание членов IAM-политик (пункт 8): рекомендовано B ───────────────
POLICY_MEMBER_CONTAINMENT = {
    "chosen": "B",
    "mechanism": "org policy iam.managed.allowedPolicyMembers на folders/881419274207",
    "spec": {
        "name": f"folders/{PL.TENANTS_FOLDER_ID}/policies/iam.managed.allowedPolicyMembers",
        "spec": {"rules": [{"enforce": True, "parameters": {
            "allowedPrincipalSets": [f"//cloudresourcemanager.googleapis.com/organizations/{PL.ORGANIZATION_ID}"],
            "allowedMemberSubjects": ["user:evelagin@gmail.com"],
        }}]},
    },
    "blocks": ["allUsers", "allAuthenticatedUsers", "user:/group:/domain: вне организации",
               "SA и workload/workforce-идентичности чужих организаций"],
    "does_not_block": ["SA внутри организации — в том числе EVETIS и mpa-platform: выдачу им прав "
                       "ловит сканер плана (до применения), но не политика",
                       "вынос строк через EXPORT DATA от имени runtime SA (не выдача IAM; см. INDIRECT)"],
    "needs_live_proof": ["распространяется ли на ACL датасетов BigQuery так же, как на IAM секретов и job'ов",
                         "владелец (gmail вне каталога org) обязан быть в allowedMemberSubjects, иначе "
                         "заблокируется автоматическая выдача owner при создании проекта"],
    "t6_customers": "клиентские принципалы analytics_share добавляются владельцем адресно на проект арендатора (T6)",
    "rejected": {
        "A": "отдельная bootstrap-идентичность для статических IAM-привязок: второй WIF, второй state или "
             "ручной шаг на каждого арендатора — сложнее, а выдачу внешним принципалам не закрывает "
             "(тот же setIamPolicy у кого-то остаётся)",
        "deny_stack": "deny на versions.access «всем, кроме runtime SA» требует исключений на каждого "
                      "арендатора — сложно; оставлен один простой deny ниже как необязательный слой",
    },
}

OPTIONAL_DEFENSE_IN_DEPTH_DENY = {
    "attachment_point": f"cloudresourcemanager.googleapis.com/{PL.TENANTS_FOLDER}",
    "policy_id": "deny-provisioner-data-plane",
    "denied_principals": [f"principal://iam.googleapis.com/projects/-/serviceAccounts/{PL.PROVISIONER_SA}"],
    "denied_permissions": ["bigquery.googleapis.com/tables.getData", "bigquery.googleapis.com/jobs.create",
                           "secretmanager.googleapis.com/versions.access",
                           "secretmanager.googleapis.com/versions.add"],
    "closes": "самовыдачу чтения строк и значений самому провижионеру; вынос через runtime SA не закрывает",
}

# ── Кросс-проектный реестр образов: точный план привязок T3.3 ───────────────
ARTIFACT_REGISTRY_BINDINGS = {
    "repository": f"projects/{PL.PLATFORM_PROJECT_ID}/locations/{PL.RUNTIME_REGION}/repositories/"
                  f"{PL.RUNTIME_REPOSITORY}",
    "bindings": [
        {"member": f"serviceAccount:{PL.PROVISIONER_SA}", "role": "roles/artifactregistry.reader",
         "why": "деплоящей идентичности нужно чтение репозитория образа другого проекта (документировано "
                "для сервисов Cloud Run; для job'ов — NEEDS_LIVE_PROOF отрицательным тестом в T3.3)"},
        {"member": "serviceAccount:service-<TENANT_PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com",
         "role": "roles/artifactregistry.reader",
         "why": "сервис-агент Cloud Run проекта арендатора тянет образ при каждом запуске"},
    ],
    "not_needed": ["runtime SA арендатора (образ тянет сервис-агент)", "SA планировщика",
                   "любой принципал EVETIS", "запись в репозиторий кем-либо, кроме выпуска T3.2b"],
}

# ── WIF: предложение к T3.3 (живое условие НЕ менялось) ─────────────────────
WIF_HARDENING = {
    "live_condition": PL.WIF_ATTRIBUTE_CONDITION,
    "proposed_condition": PL.PROPOSED_WIF_ATTRIBUTE_CONDITION,
    "status": "NOT_APPLIED",
    "needs_live_proof": "присутствие claim job_workflow_ref у обычного (не reusable) job'а — пробным токеном",
}

# Разрешения, которые должны оказаться в роли (для проверок тестов).
TRANSITIVE_DATA_PATHS = tuple(CAPABILITIES["INDIRECT"])
