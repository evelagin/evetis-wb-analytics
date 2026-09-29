#!/usr/bin/env python3
"""Идентичность развёртывания SQL арендатора (Tenancy T4.1) — доверенная база.

Почему отдельная идентичность. BigQuery требует у создателя VIEW `bigquery.tables.getData` на
каждой таблице, на которую ссылается запрос. Провижионер намеренно data-blind: при создании VIEW
он получил 403 (run 36394395025). Поэтому пакет SQL развёртывает свой SA в проекте арендатора —
`sa-sql-deployer@<project>`. У него есть только три роли организации, выданные записями ACL датасетов
этого проекта. Прав на проект, папку, организацию, EVETIS, mpa-platform и другие арендаторы нет.

Этот файл — единственный источник в Python для:
  * определений трёх ролей организации: точный набор прав, стадия, заголовок, описание.
    Роли созданы владельцем 2026-09-28 (T4.1 bootstrap); их сверяет `platform_roles.py verify`;
  * матрицы грантов датасетов (10 записей) и условия на `tenant_ops`;
  * ожидаемой привязки WIF на SA деплоера (её создаёт владелец: `tenant_bootstrap.py bind`).

Контракт реестра (registry.terraform_inputs), сканер плана, Terraform-guard'ы, bootstrap и
wif_domains берут значения ОТСЮДА. Для второго арендатора код менять не нужно: всё выводится
из project_id и ключей датасетов контракта.

  python tools/tenancy/sql_identity.py deployer-email <tenant_id>   # для шага workflow
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402

# ── Роли организации (T4.1 bootstrap 2026-09-28, владелец) ─────────────────
# Точные наборы: platform_roles verify отвергает и недостающее, и лишнее право.
# Любое новое право — отдельная находка, доказательство и ACK владельца; автоматически не добавлять
# (в частности bigquery.datasets.get, serviceusage.services.use, bigquery.jobs.create,
# bigquery.tables.updateData, bigquery.tables.delete).
ORG_ROLE_PREFIX = f"organizations/{PL.ORGANIZATION_ID}/roles/"
SOURCE_READ, VIEW_CREATE, VIEW_UPDATE = "mpaSqlSourceRead", "mpaSqlViewCreate", "mpaSqlViewUpdate"
SQL_ROLES: dict[str, dict] = {
    SOURCE_READ: {
        "title": "MPA SQL: чтение источников представлений",
        "description": ("Деплоер SQL арендатора: метаданные и строки источников утверждённых представлений. "
                        "Только ACL датасетов проекта арендатора."),
        "stage": "GA",
        "permissions": frozenset({"bigquery.tables.get", "bigquery.tables.getData"}),
    },
    VIEW_CREATE: {
        "title": "MPA SQL: создание представлений",
        "description": ("Деплоер SQL арендатора: создание объектов и их список в целевых датасетах. "
                        "Только ACL датасетов проекта арендатора."),
        "stage": "GA",
        "permissions": frozenset({"bigquery.tables.create", "bigquery.tables.get", "bigquery.tables.list"}),
    },
    VIEW_UPDATE: {
        "title": "MPA SQL: изменение представлений",
        "description": ("Деплоер SQL арендатора: изменение существующих представлений. В tenant_ops только "
                        "с условием на префикс V_. Только ACL датасетов арендатора."),
        "stage": "GA",
        "permissions": frozenset({"bigquery.tables.update", "bigquery.tables.get"}),
    },
}
# Права, которых у деплоера не может быть ни в одной роли (тест и platform_roles verify).
NEVER_FOR_DEPLOYER = frozenset({
    "bigquery.tables.updateData", "bigquery.tables.delete", "bigquery.tables.export",
    "bigquery.tables.setIamPolicy", "bigquery.tables.createSnapshot", "bigquery.jobs.create",
    "bigquery.datasets.create", "bigquery.datasets.delete", "bigquery.datasets.update",
    "bigquery.datasets.setIamPolicy", "bigquery.readsessions.create", "serviceusage.services.use",
    "resourcemanager.projects.setIamPolicy", "iam.serviceAccounts.getAccessToken",
    "iam.serviceAccounts.setIamPolicy", "iam.serviceAccounts.actAs",
    "secretmanager.versions.access", "run.jobs.run", "cloudscheduler.jobs.run",
})


def role_name(role_id: str) -> str:
    return ORG_ROLE_PREFIX + role_id


# ── Идентичность и матрица грантов ─────────────────────────────────────────
DEPLOYER_ACCOUNT_ID = "sa-sql-deployer"
# Ключ датасета контракта → роли (роль, условная ли запись). Условная — только изменение в
# tenant_ops: там 7 таблиц платформы, а все представления пакета начинаются с V_ (инвариант
# sql_package/контракта). В ozon_mart и analytics_share таблиц контракта нет (тоже инвариант),
# а их представления общего префикса не имеют — условие там невыразимо и не нужно.
# analytics_share без SOURCE_READ: пакет не ссылается на клиентский слой (P4 пакета).
GRANT_MATRIX: dict[str, tuple[tuple[str, bool], ...]] = {
    "ozon_raw": ((SOURCE_READ, False),),
    "ref": ((SOURCE_READ, False),),
    "ozon_mart": ((SOURCE_READ, False), (VIEW_CREATE, False), (VIEW_UPDATE, False)),
    "tenant_ops": ((SOURCE_READ, False), (VIEW_CREATE, False), (VIEW_UPDATE, True)),
    "analytics_share": ((VIEW_CREATE, False), (VIEW_UPDATE, False)),
    "tenant_locks": (),                     # T5: замки аренды control — деплоеру не нужны
}
CONDITIONAL_VIEW_PREFIX = "V_"
CONDITION_TITLE = "sql-deployer-tenant-ops-views"
CONDITION_DESCRIPTION = "Only package views V_*; platform tables of tenant_ops are excluded"


def deployer_email(project_id: str) -> str:
    return f"{DEPLOYER_ACCOUNT_ID}@{project_id}.iam.gserviceaccount.com"


def view_prefix_condition(project_id: str, dataset_id: str) -> dict:
    """Положительное условие по документации BigQuery: тип, сервис и имя ресурса-таблицы."""
    expr = ('resource.type == "bigquery.googleapis.com/Table" && '
            'resource.service == "bigquery.googleapis.com" && '
            f'resource.name.startsWith("projects/{project_id}/datasets/{dataset_id}/tables/'
            f'{CONDITIONAL_VIEW_PREFIX}")')
    return {"title": CONDITION_TITLE, "description": CONDITION_DESCRIPTION, "expression": expr}


def dataset_grants(project_id: str, datasets: dict[str, str]) -> list[dict]:
    """Записи ACL деплоера: [{dataset_key, role, condition|None}], детерминированный порядок."""
    if set(datasets) != set(GRANT_MATRIX):
        raise ValueError(f"датасеты контракта {sorted(datasets)} ≠ матрице деплоера {sorted(GRANT_MATRIX)}")
    out = []
    for key in sorted(GRANT_MATRIX):
        for role_id, conditional in GRANT_MATRIX[key]:
            out.append({"dataset_key": key, "role": role_name(role_id),
                        "condition": view_prefix_condition(project_id, datasets[key]) if conditional else None})
    return out


def contract_block(project_id: str, datasets: dict[str, str]) -> dict:
    """Блок `sql_deployer` контракта реестра → Terraform."""
    return {"account_id": DEPLOYER_ACCOUNT_ID, "email": deployer_email(project_id),
            "grants": dataset_grants(project_id, datasets)}


# ── Привязка WIF (создаёт владелец: tenant_bootstrap.py bind) ──────────────
WIF_POOL = "tenant-infra-pool"
WIF_MEMBER = (f"principalSet://iam.googleapis.com/projects/{PL.PLATFORM_PROJECT_NUMBER}/locations/global/"
              f"workloadIdentityPools/{WIF_POOL}/attribute.workflow_ref/"
              f"{PL.GITHUB_REPOSITORY}/{PL.TENANT_INFRA_WORKFLOW}@refs/heads/main")
WIF_ROLE = "roles/iam.workloadIdentityUser"


def expected_sa_bindings() -> list[dict]:
    """Политика SA деплоера — РОВНО одна привязка, без условия. Всё прочее — дрейф."""
    return [{"role": WIF_ROLE, "members": [WIF_MEMBER]}]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "deployer-email":
        print(__doc__, file=sys.stderr)
        return 3
    from tools.tenancy.tenant_infra import contract_for
    c = contract_for(argv[1])
    email = c["sql_deployer"]["email"]
    if email != deployer_email(c["project_id"]):
        print("FAIL email деплоера в контракте не выводится из project_id", file=sys.stderr)
        return 1
    print(f"email={email}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
