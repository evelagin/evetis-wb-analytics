#!/usr/bin/env python3
"""Идентичность control plane арендатора (Tenancy T5, решение D1) — доверенная база.

sa-tenant-control@<проект арендатора>: проверка учётных данных, наблюдения identity, профиль
возможностей, границы истории, план и чекпойнты бэкфилла, DQ, переходы автомата. Исполняется Cloud
Run Job `tenant-control` (образ runtime, точка входа lifecycle.py); запускает его владелец.

Права — ТОЛЬКО записи ACL датасетов проекта арендатора и чтение 4 секретов:

| датасет | роль | зачем |
|---|---|---|
| ozon_raw | mpaSqlSourceRead (get, getData) | журнал прогонов runtime, DQ по RAW (tabledata.list) |
| ref | mpaSqlSourceRead | SELLER_BINDING (только чтение!), REF_TENANT_ECONOMICS |
| tenant_ops | mpaSqlSourceRead + mpaTenantControlAppend (get, updateData) | журналы: читать и ДОПИСЫВАТЬ (insertAll) |
| tenant_locks | mpaTenantControlLease (create, get, list) | аренда отрезков: атомарное tables.insert |

Нет: bigquery.jobs.create (значит, нет DML — журналы append-only на уровне IAM), записи в ozon_raw и
ref (подтвердить привязку control не может — это делает только владелец), tables.delete/update,
ролей на проекте/папке/организации, run.jobs.run (запуск исполнителя — отдельные ворота).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import sql_identity as SI  # noqa: E402

CONTROL_ACCOUNT_ID = "sa-tenant-control"
CONTROL_JOB_NAME = "tenant-control"
CONTROL_COMMAND = ("python", "lifecycle.py")
CONTROL_DEFAULT_ARGS = ("status",)          # только чтение журналов; прочие команды — при запуске владельцем
APPEND, LEASE = "mpaTenantControlAppend", "mpaTenantControlLease"
CONTROL_ROLES: dict[str, dict] = {
    APPEND: {
        "title": "MPA control: дописывание журналов арендатора",
        "description": ("Control plane арендатора: дописать строки журналов tenant_ops (insertAll). Без DML: "
                        "у control нет jobs.create. Только ACL датасетов проекта арендатора."),
        "stage": "GA",
        "permissions": frozenset({"bigquery.tables.get", "bigquery.tables.updateData"}),
    },
    LEASE: {
        "title": "MPA control: аренда отрезков бэкфилла",
        "description": ("Control plane арендатора: атомарная аренда отрезка — создать таблицу-замок в "
                        "tenant_locks (409 при занятом имени). Только ACL датасета tenant_locks."),
        "stage": "GA",
        "permissions": frozenset({"bigquery.tables.create", "bigquery.tables.get", "bigquery.tables.list"}),
    },
}
NEVER_FOR_CONTROL = frozenset({
    "bigquery.jobs.create", "bigquery.tables.delete", "bigquery.tables.update", "bigquery.tables.setIamPolicy",
    "bigquery.tables.export", "bigquery.datasets.create", "bigquery.datasets.delete", "bigquery.datasets.update",
    "bigquery.datasets.setIamPolicy", "run.jobs.run", "run.jobs.runWithOverrides", "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.getAccessToken", "resourcemanager.projects.setIamPolicy",
    "secretmanager.versions.add", "secretmanager.secrets.setIamPolicy",
})
GRANT_MATRIX: dict[str, tuple[str, ...]] = {
    "ozon_raw": (SI.SOURCE_READ,),
    "ref": (SI.SOURCE_READ,),
    "tenant_ops": (SI.SOURCE_READ, APPEND),
    "tenant_locks": (LEASE,),
    "ozon_mart": (),
    "analytics_share": (),
}


def control_email(project_id: str) -> str:
    return f"{CONTROL_ACCOUNT_ID}@{project_id}.iam.gserviceaccount.com"


def dataset_grants(datasets: dict[str, str]) -> list[dict]:
    if set(datasets) != set(GRANT_MATRIX):
        raise ValueError(f"датасеты контракта {sorted(datasets)} ≠ матрице control {sorted(GRANT_MATRIX)}")
    return [{"dataset_key": k, "role": SI.role_name(r), "condition": None}
            for k in sorted(GRANT_MATRIX) for r in GRANT_MATRIX[k]]


def job_env(runtime_env: dict[str, str], datasets: dict[str, str], tenant_id: str, entities, seller_inventory_model="STRICT_CAPABILITY_V2") -> dict[str, str]:
    """Окружение tenant-control: то же, что у runtime, плюс датасеты control и сущности арендатора."""
    env = {k: v for k, v in runtime_env.items() if k not in ("TENANT_BINDING_REQUIRED", "ENTITIES", "SELLER_INVENTORY_MODEL", "SELLER_IDENTITY_VERSION")}
    env.update({"TENANT_ID": tenant_id, "TENANT_OPS_DATASET": datasets["tenant_ops"],
                "TENANT_LOCKS_DATASET": datasets["tenant_locks"], "ENABLED_ENTITIES": ",".join(sorted(entities)),
                "STRICT_PAGE_CAPS": "1"})
    if seller_inventory_model not in ("STRICT_CAPABILITY_V2", "BROAD_READ_INVENTORY_V1"):
        raise ValueError("unsupported Seller credential model")
    if seller_inventory_model == "BROAD_READ_INVENTORY_V1":
        env.update(SELLER_INVENTORY_MODEL=seller_inventory_model, SELLER_IDENTITY_VERSION="ozon-seller-core-v2")
    return dict(sorted(env.items()))


def contract_block(project_id: str, datasets: dict[str, str], runtime_env: dict[str, str], tenant_id: str,
                   entities, seller_inventory_model="STRICT_CAPABILITY_V2") -> dict:
    return {"account_id": CONTROL_ACCOUNT_ID, "email": control_email(project_id),
            "grants": dataset_grants(datasets),
            "job": {"name": CONTROL_JOB_NAME, "env": job_env(runtime_env, datasets, tenant_id, entities, seller_inventory_model)}}
