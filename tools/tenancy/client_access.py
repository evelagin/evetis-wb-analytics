"""Tenancy T6 — клиентский доступ к `analytics_share` (ADR-08, решение владельца 2026-09-25).

Единственный официальный интерфейс данных для клиента — датасет `analytics_share` проекта
арендатора, только чтение. Клиент не получает прав на ozon_raw, ref, ozon_mart, tenant_ops,
tenant_locks, Secret Manager, Cloud Run, Scheduler, проект платформы и EVETIS.

Как это выражено в Terraform (infra/tenant/client-access.tf и ACL датасетов в main.tf):
  * READER на `analytics_share` каждому principal из реестра (`bi_access.principals`,
    только user:/group:, никогда serviceAccount/domain/allUsers);
  * roles/bigquery.jobUser на проекте арендатора тем же principals — иначе запрос не
    запустить ни в BI, ни в консоли (jobUser не даёт ни одного права на данные);
  * авторизованные датасеты (target VIEWS) по РЕАЛЬНЫМ зависимостям вью пакета
    sql/tenant/ozon: analytics_share → ozon_mart, tenant_ops; ozon_mart → ozon_raw, ref;
    tenant_ops → ref, ozon_mart. Без них вью analytics_share для клиента не читаются:
    у него нет прав на источники. Авторизация датасета — не доступ клиенту к источникам:
    он читает только те вью, что лежат в analytics_share.

Блок появляется в контракте только при bi_access.mode == ANALYTICS_SHARE_VIEWER; в режиме
NONE контракт и ACL арендатора не меняются (T6 ничего не выдаёт до решения владельца).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODE = "ANALYTICS_SHARE_VIEWER"
CLIENT_DATASET_KEY = "analytics_share"
PROJECT_ROLES = ("roles/bigquery.jobUser",)
PRINCIPAL_RE = re.compile(r"^(user|group):([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,63})$")
# (кому авторизовано: датасет ВЬЮ, на чём: датасет-источник). Сверяется тестом с SQL пакета.
AUTHORIZED_VIEW_EDGES = (
    ("analytics_share", "ozon_mart"), ("analytics_share", "tenant_ops"),
    ("ozon_mart", "ozon_raw"), ("ozon_mart", "ref"),
    ("tenant_ops", "ref"), ("tenant_ops", "ozon_mart"),
)
INTERNAL_DATASET_KEYS = ("ozon_raw", "ref", "ozon_mart", "tenant_ops", "tenant_locks")
SQL_PACKAGE = REPO / "sql" / "tenant" / "ozon"
_REF_RE = re.compile(r"`[^`]*\.(ozon_raw|ref|ozon_mart|tenant_ops|analytics_share)\.[A-Za-z_]+`")


class ClientAccessError(ValueError):
    pass


def principal(text: str) -> dict:
    m = PRINCIPAL_RE.match(text or "")
    if not m:
        raise ClientAccessError(f"principal {text!r}: только user:<email> или group:<email>")
    kind, email = m.group(1), m.group(2).lower()
    if email.endswith("gserviceaccount.com") or email.endswith("google.com"):
        raise ClientAccessError(f"principal {text!r}: сервисные аккаунты и домены Google клиентским доступом не бывают")
    return {"kind": kind, "email": email}


def contract_block(doc: dict, datasets: dict) -> dict | None:
    """Блок `client_access` контракта Terraform или None (режим без клиентского доступа)."""
    bi = doc["bi_access"]
    if bi["mode"] != MODE:
        return None
    if CLIENT_DATASET_KEY not in datasets:
        raise ClientAccessError("датасета analytics_share нет в контракте арендатора")
    principals = [principal(p) for p in bi.get("principals", [])]
    if not principals:
        raise ClientAccessError("ANALYTICS_SHARE_VIEWER без получателей доступа")
    seen = {(p["kind"], p["email"]) for p in principals}
    if len(seen) != len(principals):
        raise ClientAccessError("дубликат principal в bi_access.principals")
    missing = sorted({k for e in AUTHORIZED_VIEW_EDGES for k in e} - set(datasets))
    if missing:
        raise ClientAccessError(f"авторизованные датасеты ссылаются на отсутствующие ключи {missing}")
    return {
        "mode": MODE,
        "dataset_key": CLIENT_DATASET_KEY,
        "principals": sorted(principals, key=lambda p: (p["kind"], p["email"])),
        "authorized_views": [{"viewer_key": v, "source_key": s} for v, s in AUTHORIZED_VIEW_EDGES],
        "project_roles": list(PROJECT_ROLES),
    }


def dependency_edges(package: Path = SQL_PACKAGE) -> set[tuple[str, str]]:
    """(датасет вью, датасет источника) по всем вью пакета — из самого SQL, не из документации."""
    edges = set()
    for path in sorted(package.rglob("*.sql")):
        viewer = path.parent.name
        for src in {m.group(1) for m in _REF_RE.finditer(path.read_text(encoding="utf-8"))}:
            if src != viewer:
                edges.add((viewer, src))
    return edges


def client_facing_views(package: Path = SQL_PACKAGE) -> list[str]:
    return sorted(p.stem for p in (package / CLIENT_DATASET_KEY).glob("*.sql"))
