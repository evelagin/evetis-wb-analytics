"""Канонический вывод имён ресурсов арендатора (Tenancy T1, ADR-08).

Это ЕДИНСТВЕННОЕ место, где правила имён живут в коде. Terraform-модуль арендатора
(T3), рендерер SQL (T4) и провижининг (T5) получают имена отсюда, а не собирают их
сами: два независимых «почти одинаковых» правила — ровно тот дрейф, из-за которого
одно и то же имя однажды разойдётся между Terraform и runtime.

Модель изоляции (ADR-08, модель D): у каждого внешнего арендатора свой GCP-проект.
Поэтому имена ВНУТРИ проекта (датасеты, секреты, job'ы) у всех выделенных
арендаторов одинаковые и нейтральные: граница — проект, а не префикс имени. Имя
проекта глобально уникально в GCP и выводится из tenant_id.

EVETIS — унаследованный арендатор. Его имена не выводятся, а зафиксированы как
факт (LEGACY_EVETIS) и сверяются с живой конфигурацией тестами. Реестр EVETIS
не управляет (config_authority = LEGACY_EXISTING_CONFIG).
"""
from __future__ import annotations

import re

# ── tenant_id ──────────────────────────────────────────────────────────────
TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")   # контракт tenant.v1
TENANT_ID_MAX = 31

# Зарезервированы для платформы. Внешний арендатор не может называться так, чтобы
# его имя читалось как служебное или как EVETIS.
LEGACY_TENANT_ID = "evetis"
RESERVED_TENANT_IDS = frozenset({
    "evetis", "platform", "control", "admin", "root", "system", "default", "shared",
    "global", "all", "none", "null", "tenant", "tenants", "test", "prod", "production",
    "staging", "shadow", "internal", "mpa", "google", "gcp", "ozon", "wb", "wildberries",
    "schema", "template",
})
RESERVED_TENANT_PREFIXES = ("evetis", "platform", "mpa", "goog")

# ── GCP ────────────────────────────────────────────────────────────────────
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")  # 6–30 символов
PROJECT_ID_MAX = 30
# Префикс проектов выделенных арендаторов: mpa = marketplace analytics.
# Утверждён владельцем 2026-09-24 (ADR-08, A2). Проект GCP не переименовывается.
DEDICATED_PROJECT_PREFIX = "mpa-t-"
PROJECT_SUFFIX_RE = re.compile(r"^[a-z0-9]{1,6}$")
SECRET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")          # Secret Manager secretId
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")

# ── Имена внутри проекта выделенного арендатора ───────────────────────────
# Одинаковы у всех выделенных арендаторов: изоляция — проектом (ADR-08).
# Имя справочного датасета `ref` утверждено владельцем 2026-09-24 (ADR-08, A3).
DEDICATED_DATASETS = {"ozon_raw": "ozon_raw", "ref": "ref"}
DEDICATED_OZON_SECRET_IDS = {
    "seller_client_id": "ozon-seller-client-id",
    "seller_api_key": "ozon-seller-api-key",
    "performance_client_id": "ozon-perf-client-id",
    "performance_client_secret": "ozon-perf-client-secret",
}

# ── EVETIS: зафиксированный факт, не вывод ────────────────────────────────
# Значения сверяются тестами с живым контрактом развёртывания:
# infra/terraform/terraform.tfvars.example (проект), ozon_ingestion.tf
# (BQ_RAW_DATASET, BQ_LOCATION), pipelines/ozon/runtime/common.py
# (LEGACY_EVETIS_SECRET_DEFAULTS, LEGACY_EVETIS_REF_DATASET).
LEGACY_EVETIS = {
    "tenant_id": LEGACY_TENANT_ID,
    "gcp_project_id": "project-fa311fc0-4d87-4781-986",
    "bq_location": "EU",
    "datasets": {"ozon_raw": "ozon_raw", "ref": "evetis_ref"},
    "ozon_secret_refs": {
        "seller_client_id": "EVETIS_OZON_CLIENT_ID",
        "seller_api_key": "EVETIS_OZON_API_KEY",
        "performance_client_id": "EVETIS_OZON_PERFORMANCE_CLIENT_ID",
        "performance_client_secret": "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET",
    },
}

# Роль ссылки на секрет → переменная окружения Ozon runtime (common.resolve_config).
OZON_SECRET_ENV_VARS = {
    "seller_client_id": "OZON_SECRET_SELLER_CLIENT_ID",
    "seller_api_key": "OZON_SECRET_SELLER_API_KEY",
    "performance_client_id": "OZON_SECRET_PERF_CLIENT_ID",
    "performance_client_secret": "OZON_SECRET_PERF_CLIENT_SECRET",
}


class NamingError(ValueError):
    """Из tenant_id нельзя получить допустимые имена ресурсов."""


def check_tenant_id(tenant_id: str, *, legacy: bool = False) -> None:
    """Допустим ли tenant_id. Сверх регулярного выражения контракта:

    * без «__» и без «_» в конце — иначе slug даёт «--» и «-» в конце, а ID
      проекта GCP не может заканчиваться дефисом;
    * зарезервированные ID и префиксы недоступны выделенным арендаторам;
      «evetis» допустим только для унаследованного арендатора.
    """
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.match(tenant_id):
        raise NamingError(f"tenant_id {tenant_id!r} не соответствует {TENANT_ID_RE.pattern}")
    if "__" in tenant_id:
        raise NamingError(f"tenant_id {tenant_id!r}: двойное подчёркивание запрещено")
    if tenant_id.endswith("_"):
        raise NamingError(f"tenant_id {tenant_id!r}: подчёркивание в конце запрещено")
    if legacy:
        if tenant_id != LEGACY_TENANT_ID:
            raise NamingError(f"унаследованный арендатор только один: {LEGACY_TENANT_ID!r}")
        return
    if tenant_id in RESERVED_TENANT_IDS:
        raise NamingError(f"tenant_id {tenant_id!r} зарезервирован")
    if tenant_id.startswith(RESERVED_TENANT_PREFIXES):
        raise NamingError(f"tenant_id {tenant_id!r} начинается с зарезервированного префикса")


def slug(tenant_id: str) -> str:
    """Форма tenant_id для имён GCP: «_» → «-». Биективна: в tenant_id нет «-»."""
    check_tenant_id(tenant_id, legacy=(tenant_id == LEGACY_TENANT_ID))
    return tenant_id.replace("_", "-")


def derive_project_id(tenant_id: str, suffix: str | None = None) -> str:
    """ID проекта выделенного арендатора: mpa-t-<slug>[-<suffix>].

    suffix нужен только если имя уже занято в GCP (ID проектов глобальны).
    Слишком длинный результат — ошибка, а не обрезка: обрезка сделала бы имена
    двух арендаторов одинаковыми.
    """
    check_tenant_id(tenant_id)
    if suffix is not None and not PROJECT_SUFFIX_RE.match(suffix):
        raise NamingError(f"суффикс проекта {suffix!r} не соответствует {PROJECT_SUFFIX_RE.pattern}")
    pid = DEDICATED_PROJECT_PREFIX + slug(tenant_id) + (f"-{suffix}" if suffix else "")
    if len(pid) > PROJECT_ID_MAX:
        raise NamingError(
            f"ID проекта {pid!r} длиннее {PROJECT_ID_MAX} символов: tenant_id слишком "
            f"длинный для выделенного проекта (максимум "
            f"{PROJECT_ID_MAX - len(DEDICATED_PROJECT_PREFIX)} символов slug)")
    if not PROJECT_ID_RE.match(pid):
        raise NamingError(f"ID проекта {pid!r} недопустим в GCP")
    return pid


def project_id_belongs_to(tenant_id: str, project_id: str) -> bool:
    """Выведен ли project_id из tenant_id (с суффиксом или без)."""
    base = derive_project_id(tenant_id)
    if project_id == base:
        return True
    if not project_id.startswith(base + "-"):
        return False
    try:
        return derive_project_id(tenant_id, project_id[len(base) + 1:]) == project_id
    except NamingError:
        return False


def resource_labels(tenant_id: str) -> dict[str, str]:
    """Метки ресурсов арендатора (биллинг, фильтры журналов)."""
    check_tenant_id(tenant_id, legacy=(tenant_id == LEGACY_TENANT_ID))
    if not LABEL_VALUE_RE.match(tenant_id):
        raise NamingError(f"tenant_id {tenant_id!r} недопустим как значение метки GCP")
    return {"tenant": tenant_id}


def terraform_state_prefix(tenant_id: str) -> str:
    """Префикс state Terraform арендатора (T3)."""
    check_tenant_id(tenant_id)
    return f"tenants/{tenant_id}"


def expected_datasets(tenant_id: str) -> dict[str, str]:
    if tenant_id == LEGACY_TENANT_ID:
        return dict(LEGACY_EVETIS["datasets"])
    check_tenant_id(tenant_id)
    return dict(DEDICATED_DATASETS)


def expected_ozon_secret_ids(tenant_id: str) -> dict[str, str]:
    if tenant_id == LEGACY_TENANT_ID:
        return dict(LEGACY_EVETIS["ozon_secret_refs"])
    check_tenant_id(tenant_id)
    return dict(DEDICATED_OZON_SECRET_IDS)
