"""РНП арендатора — узкое исключение из правила T6 «сервисные аккаунты не получают доступа к analytics_share».

Правило T6 (`client_access.principal`) не меняется: клиентские получатели — только user:/group:,
сервисные аккаунты по-прежнему отвергаются. Здесь — ОДНО отдельное, явно названное исключение:
учётка сайта РНП владельца `sa-rnp-web@<проект арендатора>` (веб-РНП Heldi в Cloud Run за IAP,
доступ только владельцу; код — приватный репозиторий rnp-heldi, ресурсы — его Terraform,
состояние rnp/<tenant_id>). Ни одного другого SA, проекта, датасета или роли.

Что выдаётся (только при `rnp_access.mode == OWNER_RNP_SERVICE` в реестре):
  * READER на `analytics_share` ровно этой учётке (запись `userByEmail` ACL датасета);
  * авторизованные датасеты (target VIEWS) по тем же реальным рёбрам, что и T6
    (`client_access.AUTHORIZED_VIEW_EDGES`): без них вью analytics_share не читаются никем,
    у кого нет прав на источники. Если включены и T6, и РНП, рёбра не дублируются.
Чего НЕ выдаётся: ролей проекта (роль запуска запросов `rnpHeldiQueryRunner` — в Terraform РНП, не
арендатора), READER на внутренних датасетах, клиентских идентичностей, Cloud Run, секретов.

Исключение разрешено только арендаторам из ALLOWED_TENANTS (решение владельца по пакету
«РНП Heldi в облаке»); новый арендатор — новым явным решением и правкой этого списка.
"""
from __future__ import annotations

import re

from tools.tenancy import client_access as CA

MODE = "OWNER_RNP_SERVICE"
NONE = "NONE"
ACCOUNT_ID = "sa-rnp-web"
DATASET_KEY = CA.CLIENT_DATASET_KEY          # analytics_share
ALLOWED_TENANTS = ("client_001",)
_PROJECT_RE = re.compile(r"^mpa-t[0-9]*-[a-z0-9-]{1,40}$")


class RnpAccessError(ValueError):
    pass


def service_account_email(project_id: str) -> str:
    if not _PROJECT_RE.match(project_id or ""):
        raise RnpAccessError(f"проект {project_id!r} не проект арендатора")
    return f"{ACCOUNT_ID}@{project_id}.iam.gserviceaccount.com"


def contract_block(doc: dict, project_id: str, datasets: dict) -> dict | None:
    """Блок `rnp_access` контракта Terraform или None (поле отсутствует или mode = NONE)."""
    ra = doc.get("rnp_access")
    if ra is None or ra.get("mode") == NONE:
        if ra is not None and set(ra) != {"mode"}:
            raise RnpAccessError("rnp_access.mode = NONE не допускает других полей")
        return None
    if ra.get("mode") != MODE or set(ra) != {"mode", "service_account"}:
        raise RnpAccessError("rnp_access: только {mode: OWNER_RNP_SERVICE, service_account}")
    if doc.get("tenant_id") not in ALLOWED_TENANTS:
        raise RnpAccessError(f"rnp_access разрешён только арендаторам {ALLOWED_TENANTS}")
    want = service_account_email(project_id)
    if ra.get("service_account") != want:
        raise RnpAccessError(f"rnp_access.service_account: допустима только {want}")
    if DATASET_KEY not in datasets:
        raise RnpAccessError("датасета analytics_share нет в контракте арендатора")
    missing = sorted({k for e in CA.AUTHORIZED_VIEW_EDGES for k in e} - set(datasets))
    if missing:
        raise RnpAccessError(f"авторизованные датасеты ссылаются на отсутствующие ключи {missing}")
    return {
        "mode": MODE,
        "dataset_key": DATASET_KEY,
        "service_account": want,
        "authorized_views": [{"viewer_key": v, "source_key": s} for v, s in CA.AUTHORIZED_VIEW_EDGES],
    }


def findings(contract: dict) -> list[str]:
    """Сканер плана: блок rnp_access контракта — ровно форма этого модуля."""
    ra = contract.get("rnp_access")
    if ra is None:
        return []
    out = []
    if set(ra) != {"mode", "dataset_key", "service_account", "authorized_views"}:
        out.append("контракт: rnp_access — лишние или недостающие поля")
    if ra.get("mode") != MODE or ra.get("dataset_key") != DATASET_KEY:
        out.append("контракт: rnp_access — неизвестный режим или датасет")
    if contract.get("tenant_id") not in ALLOWED_TENANTS:
        out.append("контракт: rnp_access у арендатора вне ALLOWED_TENANTS")
    try:
        want = service_account_email(contract.get("project_id", ""))
    except RnpAccessError as e:
        out.append(f"контракт: rnp_access — {e}")
        want = None
    if ra.get("service_account") != want:
        out.append(f"контракт: rnp_access.service_account не равна {want}")
    edges = ra.get("authorized_views", [])
    if {(e.get("viewer_key"), e.get("source_key")) for e in edges} != set(CA.AUTHORIZED_VIEW_EDGES) \
            or len(edges) != len(CA.AUTHORIZED_VIEW_EDGES):
        out.append("контракт: rnp_access.authorized_views не равны реальным зависимостям вью пакета")
    return out


def authorized_edges(contract: dict) -> set[tuple[str, str]]:
    """Рёбра авторизации из всех источников (T6 и РНП) — без дублей."""
    out = set()
    for key in ("client_access", "rnp_access"):
        for e in (contract.get(key) or {}).get("authorized_views", []):
            out.add((e["viewer_key"], e["source_key"]))
    return out
