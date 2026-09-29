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

Все проверки идентификаторов — ПОЛНОЕ совпадение строки (re.fullmatch, T2.1/L5):
значение либо целиком допустимо, либо отвергается. Ничего не нормализуется молча.
"""
from __future__ import annotations

import re

# ── tenant_id ──────────────────────────────────────────────────────────────
TENANT_ID_RE = re.compile(r"[a-z][a-z0-9_]{2,30}")      # контракт tenant.v1, fullmatch
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
# Источник: https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects
# (страница обновлена 2026-09-18 UTC, прочитана 2026-09-24).
#
# ТРЕБОВАНИЯ GOOGLE CLOUD (проверяются ниже как есть):
#   * 6–30 символов; только строчные буквы, цифры и дефис;
#   * первый символ — буква; последний — не дефис;
#   * «restricted strings such as google and ssl» — запрещены (список НЕ
#     исчерпывающий: «such as»);
#   * null и undefined: в списке требований — «avoid», но раздел API прямо
#     говорит, что projects.create() отвергает google, null, undefined и ssl
#     с INVALID_ARGUMENT. Поэтому для нас они тоже запрет, а не пожелание.
#   * Двойной дефис Google НЕ упоминает — ни запрета, ни гарантии.
#   * ID не должен быть занят или использован раньше (включая удалённые проекты) —
#     это выясняется только при создании (T3); на этот случай есть ревизия.
#
# ПОЛИТИКА ПЛАТФОРМЫ (строже Google; см. TENANT_ID_RE, RESERVED_*, грамматику ниже):
#   * префикс mpa-t и ревизия на шестой позиции;
#   * slug только из tenant_id: [a-z][a-z0-9_]{2,30}, без «__» и «_» в конце —
#     поэтому в slug нет «--» и дефиса в конце;
#   * зарезервированные tenant_id и префиксы;
#   * длина: отказ вместо обрезки (slug ≤ 24, с ревизией ≤ 23).
PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")    # требование GCP, fullmatch
PROJECT_ID_MAX = 30                                            # требование GCP
PROJECT_ID_FORBIDDEN_SUBSTRINGS = ("google", "ssl", "null", "undefined")   # требование GCP (API)

# ── Пространство имён проектов выделенных арендаторов (ЗАМОРОЖЕНО, T2.1) ──
#
#   ревизия 1 (по умолчанию):  mpa-t-<slug>
#   ревизия r ∈ {2..9}:        mpa-t<r>-<slug>
#
# slug = tenant_id с «_» → «-». Признак ревизии стоит на ФИКСИРОВАННОЙ позиции —
# шестом символе ID: в базовой форме там всегда «-», в ревизии — цифра. Поэтому
# ревизия не может совпасть ни с каким slug (прежняя схема mpa-t-<slug>-<суффикс>
# давала client + «001» = client_001, находка L6). Отображение
# (tenant_id, ревизия) → ID проекта инъективно и обратимо (parse_project_id).
# Двойной дефис как разделитель не используется: грамматика GCP его не запрещает,
# но и не гарантирует, а эта схема обходится одиночными дефисами.
#
# Ревизия нужна ТОЛЬКО если базовый ID глобально занят в GCP (ID проектов общие
# для всех клиентов Google и не освобождаются даже после удаления). Её выбирает
# владелец платформы при создании проекта, хранится она в tenant.json
# (data_boundary.project_id_revision); случайных суффиксов нет.
#
# Грамматика заморожена для провижининга внешних арендаторов после T2.1. После
# создания первого физического проекта (T3) любое изменение — только через ADR
# миграции. Эталонные значения закреплены в tools/tests/test_tenancy.py.
# Префикс утверждён владельцем 2026-09-24 (ADR-08, A2): mpa = marketplace analytics.
DEDICATED_PROJECT_PREFIX = "mpa-t"
PROJECT_REVISION_MIN, PROJECT_REVISION_MAX = 2, 9
_DEDICATED_PROJECT_ID_RE = re.compile(
    r"mpa-t(?P<rev>[2-9])?-(?P<slug>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)")   # fullmatch

SECRET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")             # Secret Manager secretId
DATASET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,1023}")
LABEL_VALUE_RE = re.compile(r"[a-z0-9_-]{0,63}")

# ── Имена внутри проекта выделенного арендатора ───────────────────────────
# Одинаковы у всех выделенных арендаторов: изоляция — проектом (ADR-08).
# Имя справочного датасета `ref` утверждено владельцем 2026-09-24 (ADR-08, A3).
# T4 (2026-09-27): ozon_mart — нормализованный слой и внутренние витрины; tenant_ops —
# операционное состояние арендатора (identity, возможности, границы истории, покрытие,
# контрольные точки, DQ); analytics_share — клиентский семантический слой (доступ — T6).
# T5 (2026-09-28): tenant_locks — таблицы-замки аренды отрезков бэкфилла (control plane, D1).
DEDICATED_DATASETS = {"ozon_raw": "ozon_raw", "ref": "ref", "ozon_mart": "ozon_mart",
                      "tenant_ops": "tenant_ops", "analytics_share": "analytics_share",
                      "tenant_locks": "tenant_locks"}
DEDICATED_OZON_SECRET_IDS = {
    "seller_client_id": "ozon-seller-client-id",
    "seller_api_key": "ozon-seller-api-key",
    "performance_client_id": "ozon-perf-client-id",
    "performance_client_secret": "ozon-perf-client-secret",
}

# Сервисные аккаунты выделенного арендатора — по одному набору на площадку (T3.2).
# Runtime и планировщик разделены: планировщик только вызывает job'ы, данные и
# секреты видит только runtime. WB позже добавит свой набор (sa-wb-*), не трогая Ozon.
DEDICATED_SERVICE_ACCOUNTS = {
    "ozon": {"runtime": "sa-ozon-runtime", "scheduler": "sa-ozon-scheduler"},
}
SERVICE_ACCOUNT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")   # требование GCP, fullmatch

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
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
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


def check_project_revision(revision) -> int:
    """Ревизия ID проекта: None → 1 (базовая форма), иначе целое 2..9, не bool."""
    if revision is None:
        return 1
    if (isinstance(revision, bool) or not isinstance(revision, int)
            or not PROJECT_REVISION_MIN <= revision <= PROJECT_REVISION_MAX):
        raise NamingError(
            f"ревизия ID проекта — целое {PROJECT_REVISION_MIN}..{PROJECT_REVISION_MAX} "
            "(ревизия 1 — это отсутствие поля)")
    return revision


def check_gcp_project_id(project_id) -> None:
    """Допустим ли ID проекта по грамматике GCP (полное совпадение)."""
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise NamingError(f"ID проекта {project_id!r} не соответствует грамматике GCP")
    bad = [s for s in PROJECT_ID_FORBIDDEN_SUBSTRINGS if s in project_id]
    if bad:
        raise NamingError(f"ID проекта {project_id!r} содержит запрещённые в GCP подстроки {bad}")


def derive_project_id(tenant_id: str, revision: int | None = None) -> str:
    """ID проекта выделенного арендатора: mpa-t-<slug> или mpa-t<r>-<slug>.

    Слишком длинный результат — ошибка, а не обрезка: обрезка могла бы сделать
    имена двух арендаторов одинаковыми.
    """
    check_tenant_id(tenant_id)
    rev = check_project_revision(revision)
    marker = "" if rev == 1 else str(rev)
    pid = f"{DEDICATED_PROJECT_PREFIX}{marker}-{slug(tenant_id)}"
    if len(pid) > PROJECT_ID_MAX:
        raise NamingError(
            f"ID проекта {pid!r} длиннее {PROJECT_ID_MAX} символов: tenant_id слишком "
            "длинный для выделенного проекта (slug до 24 символов, с ревизией до 23)")
    check_gcp_project_id(pid)
    return pid


def parse_project_id(project_id: str) -> tuple[str, int]:
    """Обратное отображение: ID проекта выделенного арендатора → (tenant_id, ревизия).

    Принимает только то, что derive_project_id выдал бы сам (проверяется обратным
    выводом), поэтому у каждого допустимого ID ровно один владелец.
    """
    m = (_DEDICATED_PROJECT_ID_RE.fullmatch(project_id)
         if isinstance(project_id, str) else None)
    if not m:
        raise NamingError(f"{project_id!r} не принадлежит пространству проектов арендаторов")
    tenant_id = m["slug"].replace("-", "_")
    revision = int(m["rev"]) if m["rev"] else 1
    if derive_project_id(tenant_id, None if revision == 1 else revision) != project_id:
        raise NamingError(f"{project_id!r} не является каноническим выводом из tenant_id")
    return tenant_id, revision


def resource_labels(tenant_id: str) -> dict[str, str]:
    """Метки ресурсов арендатора (биллинг, фильтры журналов)."""
    check_tenant_id(tenant_id, legacy=(tenant_id == LEGACY_TENANT_ID))
    if not LABEL_VALUE_RE.fullmatch(tenant_id):
        raise NamingError(f"tenant_id {tenant_id!r} недопустим как значение метки GCP")
    return {"tenant": tenant_id}


def terraform_state_prefix(tenant_id: str) -> str:
    """Префикс state для -backend-config=prefix (T3). Backend сам добавляет «/default.tfstate»."""
    check_tenant_id(tenant_id)
    return f"tenants/{tenant_id}"


def terraform_state_iam_prefix(tenant_id: str) -> str:
    """Префикс объектов state для УСЛОВИЙ IAM — всегда со слэшем на конце (T3.2).

    Строка «tenants/abc» является префиксом «tenants/abc_x/…», поэтому условие
    startsWith(".../objects/tenants/abc") выдало бы арендатору abc state арендатора
    abc_x. Условия по арендатору строятся только от этого значения.
    """
    return terraform_state_prefix(tenant_id) + "/"


def expected_datasets(tenant_id: str) -> dict[str, str]:
    if tenant_id == LEGACY_TENANT_ID:
        return dict(LEGACY_EVETIS["datasets"])
    check_tenant_id(tenant_id)
    return dict(DEDICATED_DATASETS)


def dedicated_service_accounts(tenant_id: str, marketplace: str) -> dict[str, str]:
    """ID сервисных аккаунтов площадки в проекте выделенного арендатора."""
    check_tenant_id(tenant_id)
    if marketplace not in DEDICATED_SERVICE_ACCOUNTS:
        raise NamingError(f"для площадки {marketplace!r} сервисные аккаунты не определены")
    return dict(DEDICATED_SERVICE_ACCOUNTS[marketplace])


def expected_ozon_secret_ids(tenant_id: str) -> dict[str, str]:
    if tenant_id == LEGACY_TENANT_ID:
        return dict(LEGACY_EVETIS["ozon_secret_refs"])
    check_tenant_id(tenant_id)
    return dict(DEDICATED_OZON_SECRET_IDS)
