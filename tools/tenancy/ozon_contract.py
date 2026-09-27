"""Контракт Ozon runtime для выделенного арендатора (Tenancy T3.2).

ЕДИНСТВЕННОЕ место, где для выделенных арендаторов определено:

  * OZON_JOBS — какие Cloud Run job'ы существуют, какие сущности в них и по какому
    расписанию. Раньше это жило только в locals infra/terraform/ozon_ingestion.tf
    (EVETIS). Состав и расписания совпадают с EVETIS — это проверяет тест
    (test_tenancy_t32.py::test_ozon_jobs_match_evetis_terraform), но EVETIS отсюда
    не читает: его расписание по-прежнему задаёт его Terraform.
  * ENTITY_TABLES — в какие таблицы ozon_raw пишет каждая сущность. Сверяется со
    статическим разбором pipelines/ozon/runtime (тест), чтобы новая таблица в runtime
    не могла остаться без DDL у арендатора.
  * схемы таблиц — pipelines/ozon/schema/<dataset>/<TABLE>.json (снимок метаданных
    EVETIS 2026-09-25, без строк). Паритет с live — tools/tenancy/ozon_schema_parity.py.

Решений по конкретному арендатору здесь нет: одни и те же правила для всех.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO / "pipelines" / "ozon" / "schema"
# Таблицы платформы (T4): не пишутся Ozon runtime по сущностям, а есть у каждого арендатора.
PLATFORM_SCHEMA_DIR = REPO / "tools" / "tenancy" / "schema"

# Имена job'ов и расписания (Europe/Moscow) = EVETIS (infra/terraform/ozon_ingestion.tf).
OZON_TIME_ZONE = "Europe/Moscow"
OZON_JOBS: dict[str, dict] = {
    "ozon-runtime-fast": {
        "scheduler": "ozon-fast",
        "entities": ("stocks", "fbo_postings"),
        "schedule": "0 7,13,19 * * *",
    },
    "ozon-runtime-daily": {
        "scheduler": "ozon-daily",
        "entities": ("catalog", "prices", "seller_info", "finance_accrual", "ads_campaigns",
                     "ads_expense_daily", "ads_sku_daily", "supplies"),
        "schedule": "30 6 * * *",
    },
    "ozon-runtime-weekly": {
        "scheduler": "ozon-weekly",
        "entities": ("clusters",),
        "schedule": "0 5 * * 1",
    },
    "ozon-runtime-promo": {
        "scheduler": "ozon-promo",
        "entities": ("promo",),
        "schedule": "0 7,12,17,22 * * *",
    },
}

# Таблица журнала пишется любым прогоном, при любых сущностях.
RUNS_TABLE = "OZON_INGESTION_RUNS"
ENTITY_TABLES: dict[str, tuple[str, ...]] = {
    "catalog": ("RAW_OZON_CATALOG", "RAW_OZON_PRICE_COMMISSIONS"),
    "prices": ("RAW_OZON_PRICES", "RAW_OZON_PRICE_COMMISSIONS"),
    "seller_info": ("RAW_OZON_SELLER_INFO",),
    "stocks": ("RAW_OZON_STOCKS",),
    "fbo_postings": ("RAW_OZON_POSTINGS_FBO",),
    "finance_accrual": ("RAW_OZON_FINANCE_ACCRUAL",),
    "ads_campaigns": ("RAW_OZON_ADS_CAMPAIGNS",),
    "ads_expense_daily": ("RAW_OZON_ADS_EXPENSE_DAILY",),
    "ads_sku_daily": ("RAW_OZON_ADS_SKU_DAILY",),
    "clusters": ("RAW_OZON_CLUSTERS",),
    "supplies": ("RAW_OZON_SUPPLY_ORDERS", "RAW_OZON_SUPPLIES", "RAW_OZON_SUPPLY_BUNDLES"),
    "promo": ("OZON_PROMO_OBSERVATIONS", "RAW_OZON_PROMO_ACTIONS", "RAW_OZON_PROMO_AUTO_ADD",
              "RAW_OZON_PROMO_PRODUCTS", "RAW_OZON_PROMO_PRODUCT_ACTION",
              "RAW_OZON_PROMO_PRODUCT_MARKETING"),
}
# Справочные таблицы (датасет ref), которые runtime читает.
ENTITY_REF_TABLES: dict[str, tuple[str, ...]] = {"promo": ("REF_SKU_CHANNEL_MAP",)}

# Таблицы, которые есть у КАЖДОГО выделенного арендатора независимо от набора сущностей (T4):
#   ref        — справочники, которые вводит оператор или продавец; runtime их только читает
#                (подтверждённая привязка к кабинету — здесь, чтобы runtime не мог подтвердить сам);
#   tenant_ops — операционное состояние: автомат, наблюдения identity, возможности, границы
#                истории, покрытие, контрольные точки backfill, DQ.
PLATFORM_TABLES: dict[str, tuple[str, ...]] = {
    "ref": ("REF_SKU_CHANNEL_MAP", "SELLER_BINDING", "REF_PRODUCT_MASTER", "REF_COGS", "REF_TENANT_ECONOMICS"),
    "tenant_ops": ("TENANT_STATE_EVENTS", "SELLER_IDENTITY_OBSERVATIONS", "CAPABILITY_PROFILE",
                   "HISTORY_BOUNDARIES", "DATA_COVERAGE", "BACKFILL_CHECKPOINTS", "DQ_RESULTS"),
}

# API, нужные Ozon runtime в проекте арендатора (сверх базовых платформенных).
OZON_APIS = ("bigquery.googleapis.com", "cloudscheduler.googleapis.com", "run.googleapis.com",
             "secretmanager.googleapis.com")

# Строгий режим лимитов страниц для выделенных арендаторов: при упоре в лимит —
# отказ, а не молча обрезанная выгрузка (T2, решение A4 о строгом отказе).
DEDICATED_STRICT_PAGE_CAPS = "1"


class ContractError(ValueError):
    """Сущности арендатора нельзя разложить по контракту."""


def jobs_for(entities) -> dict[str, dict]:
    """Job'ы арендатора: только те, в которых есть хоть одна его сущность.

    Каждая сущность обязана принадлежать ровно одному job'у контракта — иначе отказ
    (сущность без расписания молча не загружалась бы никогда).
    """
    wanted = list(entities)
    owner = {e: j for j, spec in OZON_JOBS.items() for e in spec["entities"]}
    orphans = sorted(set(wanted) - set(owner))
    if orphans:
        raise ContractError(f"сущности без job'а в OZON_JOBS: {orphans}")
    out = {}
    for job, spec in OZON_JOBS.items():
        mine = [e for e in spec["entities"] if e in wanted]
        if mine:
            out[job] = {"scheduler": spec["scheduler"], "entities": mine,
                        "schedule": spec["schedule"], "time_zone": OZON_TIME_ZONE}
    return out


def tables_for(entities, include_platform: bool = True) -> dict[str, list[str]]:
    """{ключ датасета: [таблицы]} — всё, что должно существовать до первого прогона.

    include_platform=False — только таблицы Ozon runtime (для сверки паритета с EVETIS:
    таблиц платформы у EVETIS нет и быть не должно).
    """
    raw, ref = {RUNS_TABLE}, set()
    for e in entities:
        if e not in ENTITY_TABLES:
            raise ContractError(f"для сущности {e!r} не определены таблицы")
        raw.update(ENTITY_TABLES[e])
        ref.update(ENTITY_REF_TABLES.get(e, ()))
    out = {"ozon_raw": sorted(raw), "ref": sorted(ref)}
    if include_platform:
        for ds, names in PLATFORM_TABLES.items():
            out[ds] = sorted(set(out.get(ds, [])) | set(names))
    return out


def schema_path(dataset_key: str, table: str) -> Path:
    ozon = SCHEMA_DIR / dataset_key / f"{table}.json"
    return ozon if ozon.is_file() else PLATFORM_SCHEMA_DIR / dataset_key / f"{table}.json"


def load_table_spec(dataset_key: str, table: str) -> dict:
    """Снимок таблицы из Git. Строгий JSON: повторяющиеся ключи — ошибка."""
    from tools.tenancy.validation import parse_tenant_json   # тот же строгий разборщик
    p = schema_path(dataset_key, table)
    if not p.is_file():
        raise ContractError(f"нет схемы в Git: {p.relative_to(REPO)}")
    spec = parse_tenant_json(p.read_text(encoding="utf-8"))
    if spec.get("table_id") != table:
        raise ContractError(f"{p.name}: table_id {spec.get('table_id')!r} != {table!r}")
    return spec


def normalized_fields(fields: list[dict]) -> list[dict]:
    """Схема для арендатора: только структура, без описаний EVETIS."""
    out = []
    for f in fields:
        g = {"name": f["name"], "type": f["type"], "mode": f.get("mode", "NULLABLE")}
        for k in ("maxLength", "precision", "scale", "defaultValueExpression"):
            if k in f:
                g[k] = f[k]
        if f.get("fields"):
            g["fields"] = normalized_fields(f["fields"])
        out.append(g)
    return out


def terraform_table(dataset_key: str, table: str) -> dict:
    """Определение таблицы для Terraform: схема — JSON-строка, как ждёт провайдер."""
    spec = load_table_spec(dataset_key, table)
    tp = spec.get("time_partitioning")
    return {
        "dataset_key": dataset_key,
        "table_id": table,
        "schema_json": json.dumps(normalized_fields(spec["schema"]), ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":")),
        "time_partitioning_type": tp["type"] if tp else None,
        "time_partitioning_field": tp.get("field") if tp else None,
        "clustering": list(spec.get("clustering", [])),
        "require_partition_filter": bool(spec.get("require_partition_filter", False)),
    }
