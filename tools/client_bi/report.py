"""Tenancy T6 — клиентский отчёт (Looker Studio) поверх analytics_share: спецификация как код.

Что здесь:
  * SPEC — точная спецификация отчёта из 5 разделов: источники (только analytics_share,
    учётные данные ПРОСМАТРИВАЮЩЕГО), элементы, поля, фильтры, вычисляемые поля и правила подписи
    PARTIAL / UNKNOWN / PRE_COGS. Тест сверяет каждое поле с CONTRACT.json витрины;
  * linking_url() — ссылка Linking API: клиент или владелец получает копию шаблона, все источники
    которой указывают на analytics_share своего проекта и тарифицируются в нём же;
  * isolation_probes() / `probe` — проверка изоляции от имени клиентской идентичности: каждая вью
    analytics_share читается, каждый внутренний датасет и чужие проекты — отказ; права на проекте —
    ровно роль vtsClientQueryRunner (testIamPermissions, только чтение).

Ничего не создаёт и не выдаёт: отчёт, шаблон и доступы делает владелец в своём аккаунте Google.

  python -m tools.client_bi.report spec                         # JSON спецификации
  python -m tools.client_bi.report url  <template_id> <project>  # ссылка Linking API
  python -m tools.client_bi.report probe <project> [--run]       # план/прогон проверки изоляции
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import client_access as CA  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402

DATASET = CA.CLIENT_DATASET_KEY
LOOKER_CREATE = "https://lookerstudio.google.com/reporting/create"

# Сущности загрузки Ozon, которые видит клиент в data_freshness (= сущности арендатора без promo).
FRESHNESS_ENTITIES = ("fbo_postings", "stocks", "clusters", "supplies", "ads_expense_daily", "ads_sku_daily",
                      "ads_campaigns", "finance_accrual", "prices", "catalog", "seller_info")

# Подписи статусов (одинаковы на всех страницах). Цвет — условное форматирование Looker Studio.
STATUS_LABELS = {
    "COMPLETE": ("Полные данные", "зелёный"),
    "PARTIAL": ("Неполные данные", "жёлтый"),
    "UNKNOWN": ("Нет данных (не загружено)", "серый"),
    "NOT_AVAILABLE": ("Недоступно в API", "серый"),
    "NOT_APPLICABLE": ("Не применимо", "серый"),
    "FAILED": ("Ошибка загрузки", "красный"),
}
STATUS_CASE = ("CASE {f} WHEN 'COMPLETE' THEN 'Полные данные' WHEN 'PARTIAL' THEN 'Неполные данные' "
               "WHEN 'FAILED' THEN 'Ошибка загрузки' WHEN 'NOT_AVAILABLE' THEN 'Недоступно в API' "
               "WHEN 'NOT_APPLICABLE' THEN 'Не применимо' ELSE 'Нет данных (не загружено)' END")


def _ds(table: str, title: str) -> dict:
    return {"dataset": DATASET, "table": table, "title": title, "credentials": "VIEWER", "freshness_minutes": 60}


def _f(*names: str) -> list[str]:
    return list(names)


SPEC: dict = {
    "report_name": "Аналитика Ozon — {client}",
    "rules": [
        "Источники — только вью analytics_share; учётные данные просматривающего (VIEWER): каждый видит ровно то, что ему выдано в BigQuery.",
        "Платёжный проект запросов — проект арендатора (billingProjectId = projectId).",
        "Нет данных ≠ ноль: сутки со статусом не COMPLETE подписываются, а не закрашиваются нулём; сутки без строки — «Нет данных (не загружено)».",
        "Вклад до себестоимости (PRE_COGS) — не прибыль. Налоги, OPEX, фулфилмент не входят. Пустое значение = «не вычисляется» (финансы не созрели).",
        "Вклад после себестоимости показывается только при cogs_coverage = COMPLETE (себестоимость дал клиент). Сейчас — не показывается.",
        "Реклама: отчёт кампаний — атрибуция; «Списано Ozon за продвижение» — биллинг из начислений. Не складывать и не распределять биллинг по SKU.",
    ],
    "data_sources": {
        "ds_freshness": _ds("data_freshness", "Свежесть данных"),
        "ds_coverage": _ds("data_coverage", "Покрытие по суткам"),
        "ds_sales": _ds("sales_daily", "Продажи по суткам"),
        "ds_sku": _ds("sku_daily", "Товар по суткам"),
        "ds_orders": _ds("orders", "Заказы"),
        "ds_cover": _ds("inventory_cover", "Запас в днях"),
        "ds_stock": _ds("inventory_current", "Остатки по складам"),
        "ds_ads": _ds("advertising_daily", "Реклама по кампаниям"),
        "ds_profit": _ds("profitability_daily", "Экономика SKU"),
        "ds_costs": _ds("store_costs_daily", "Начисления вне заказов"),
    },
    "blends": {
        "bl_drr": {
            "title": "ДРР магазина по суткам",
            "join": "LEFT OUTER",
            "left": {"source": "ds_sales", "key": "sales_date", "fields": _f("ordered_value_rub", "data_status")},
            "right": {"source": "ds_ads", "key": "stat_date", "fields": _f("expense_rub", "data_status")},
            "calc": {"ДРР магазина": "SUM(expense_rub) / SUM(ordered_value_rub)"},
            "filter": "ds_sales.data_status = 'COMPLETE' AND ds_ads.data_status = 'COMPLETE'",
            "note": "ДРР считается только по суткам, где полны и заказы, и расходы кампаний.",
        },
    },
    "pages": [
        {
            "id": "sales", "title": "1. Продажи и заказы",
            "controls": [{"type": "date_range", "default": "последние 28 суток"},
                         {"type": "dropdown", "source": "ds_sku", "field": "offer_id", "label": "Артикул"},
                         {"type": "dropdown", "source": "ds_orders", "field": "warehouse_name", "label": "Склад отгрузки"}],
            "banner": {"source": "ds_freshness", "filter": "entity = 'fbo_postings'",
                       "fields": _f("last_complete_date", "days_since_last_complete", "as_of_status", "is_stale"),
                       "text": "Заказы полны по {last_complete_date}; отставание {days_since_last_complete} сут. Позже — нет данных, а не ноль."},
            "elements": [
                {"type": "scorecards", "source": "ds_sales", "filter": "data_status = 'COMPLETE'",
                 "fields": _f("ordered_value_rub", "ordered_units", "orders", "cancelled_units", "delivered_units", "in_progress_units", "delivered_value_rub"),
                 "calc": {"Доля отмен": "SUM(cancelled_units) / SUM(ordered_units)", "Доля доставленных": "SUM(delivered_units) / SUM(ordered_units)"}},
                {"type": "time_series", "source": "ds_sales", "dimension": "sales_date",
                 "fields": _f("ordered_value_rub", "delivered_value_rub", "cancelled_units"),
                 "breakdown_style": "точки суток с data_status ≠ COMPLETE — серым (условный цвет по data_status)",
                 "status_field": "data_status"},
                {"type": "table", "title": "Товары", "source": "ds_sku", "filter": "data_status = 'COMPLETE'",
                 "dimensions": _f("offer_id", "ozon_name", "sku"),
                 "fields": _f("ordered_units", "ordered_value_rub", "cancelled_units", "delivered_units", "delivered_value_rub"),
                 "calc": {"Доля отмен": "SUM(cancelled_units) / SUM(ordered_units)"}, "sort": "ordered_value_rub DESC"},
                {"type": "table", "title": "Склады отгрузки", "source": "ds_orders",
                 "dimensions": _f("warehouse_name"), "fields": _f("posting_number", "quantity", "is_cancelled", "is_delivered", "price_rub"),
                 "calc": {"Отправлений": "COUNT_DISTINCT(posting_number)", "Отменено": "SUM(CASE WHEN is_cancelled THEN quantity ELSE 0 END)",
                          "Доставлено": "SUM(CASE WHEN is_delivered THEN quantity ELSE 0 END)"}},
                {"type": "table", "title": "Статус суток", "source": "ds_sales", "dimensions": _f("sales_date", "data_status"),
                 "calc": {"Статус": STATUS_CASE.format(f="data_status")}},
            ],
        },
        {
            "id": "inventory", "title": "2. Остатки",
            "controls": [{"type": "dropdown", "source": "ds_stock", "field": "cluster_name", "label": "Кластер"},
                         {"type": "dropdown", "source": "ds_cover", "field": "velocity_data_status", "label": "Статус скорости"}],
            "banner": {"source": "ds_cover", "fields": _f("snapshot_date", "velocity_basis_from", "velocity_basis_to", "velocity_basis_lag_days", "velocity_data_status"),
                       "text": "Остатки на {snapshot_date}. Скорость — по заказам {velocity_basis_from}–{velocity_basis_to} (база старше снимка на {velocity_basis_lag_days} сут.). При «Неполные данные» запас в днях — оценка по устаревшей базе."},
            "elements": [
                {"type": "scorecards", "source": "ds_cover", "fields": _f("available_units", "transit_units")},
                {"type": "table", "title": "Запас в днях по товарам", "source": "ds_cover",
                 "dimensions": _f("offer_id", "ozon_name", "sku", "velocity_data_status", "velocity_status_reason", "stock_data_status"),
                 "fields": _f("available_units", "transit_units", "clusters_with_stock", "avg_daily_units", "days_of_cover"),
                 "conditional": {"days_of_cover": "< 14 — красный; 14–45 — жёлтый; > 45 — зелёный; пусто — «не вычисляется»",
                                 "velocity_data_status": "по STATUS_LABELS"},
                 "sort": "days_of_cover ASC"},
                {"type": "bar", "title": "Остаток по кластерам", "source": "ds_stock", "dimensions": _f("cluster_name"),
                 "fields": _f("available_stock_count", "transit_stock_count")},
                {"type": "table", "title": "Товар × склад", "source": "ds_stock",
                 "dimensions": _f("offer_id", "ozon_name", "warehouse_name", "cluster_name", "turnover_grade"),
                 "fields": _f("available_stock_count", "transit_stock_count", "days_without_sales", "snapshot_age_days")},
            ],
        },
        {
            "id": "ads", "title": "3. Реклама",
            "controls": [{"type": "date_range", "default": "последние 28 суток"},
                         {"type": "dropdown", "source": "ds_ads", "field": "campaign_title", "label": "Кампания"}],
            "banner": {"source": "ds_freshness", "filter": "entity IN ('ads_expense_daily', 'ads_sku_daily')",
                       "fields": _f("entity_title", "last_complete_date", "last_status", "as_of_status"),
                       "text": "Расходы кампаний полны по {last_complete_date}. Расход по товарам: {last_status} — ДРР по товарам не показывается, пока статус не «Полные данные»."},
            "elements": [
                {"type": "scorecards", "source": "ds_ads", "filter": "data_status = 'COMPLETE'",
                 "fields": _f("expense_rub", "impressions", "clicks", "orders", "revenue_rub"),
                 "calc": {"CTR": "SUM(clicks) / SUM(impressions)", "CPC": "SUM(expense_rub) / SUM(clicks)",
                          "ACOS (расход / выручка по отчёту)": "SUM(expense_rub) / SUM(revenue_rub)"}},
                {"type": "scorecard", "title": "ДРР магазина (расход кампаний / заказано)", "source": "bl_drr"},
                {"type": "scorecard", "title": "Списано Ozon за продвижение (биллинг, не атрибуция)", "source": "ds_costs",
                 "filter": "accrual_class IN ('PROMOTION_BILLING', 'PROMOTION_SERVICES')", "fields": _f("amount_rub", "accrual_class", "data_status")},
                {"type": "time_series", "source": "ds_ads", "dimension": "stat_date", "fields": _f("expense_rub", "revenue_rub"), "status_field": "data_status"},
                {"type": "table", "title": "Кампании", "source": "ds_ads", "dimensions": _f("campaign_id", "campaign_title"),
                 "fields": _f("expense_rub", "bonus_expense_rub", "impressions", "clicks", "orders", "revenue_rub"),
                 "calc": {"CTR": "SUM(clicks) / SUM(impressions)", "CPC": "SUM(expense_rub) / SUM(clicks)", "ACOS": "SUM(expense_rub) / SUM(revenue_rub)"},
                 "sort": "expense_rub DESC"},
                {"type": "table", "title": "Товары: атрибуция рекламы", "source": "ds_sku",
                 "dimensions": _f("offer_id", "ozon_name", "ads_data_status"),
                 "fields": _f("ad_spend_attributed_rub", "ad_orders", "ad_clicks", "ad_revenue_rub", "ordered_value_rub", "drr_ratio"),
                 "rule": "Таблица видна с подписью «Неполные данные / нет данных», пока ads_data_status ≠ COMPLETE; ДРР по товару не суммируется в магазин."},
            ],
        },
        {
            "id": "economics", "title": "4. Экономика SKU",
            "controls": [{"type": "date_range", "default": "последние 60 суток"},
                         {"type": "dropdown", "source": "ds_profit", "field": "revenue_basis", "label": "Расчёт Ozon по отправлениям"}],
            "banner": {"source": "ds_freshness", "filter": "entity = 'finance_accrual'",
                       "fields": _f("last_complete_date", "unknown_days_30", "last_status"),
                       "text": "Предварительно. Вклад до себестоимости (PRE_COGS) вычисляется только для суток с созревшими полными начислениями; пусто — «не вычисляется», а не ноль. Себестоимость не передана — вклад после себестоимости не показывается."},
            "elements": [
                {"type": "scorecards", "source": "ds_profit",
                 "fields": _f("delivered_units", "seller_revenue_rub", "commission_rub", "logistics_rub", "acquiring_rub",
                              "return_logistics_rub", "promotion_rub", "other_costs_rub", "contribution_pre_cogs_rub"),
                 "labels": {"contribution_pre_cogs_rub": "Вклад до себестоимости (PRE_COGS), не прибыль"}},
                {"type": "table", "title": "Товары", "source": "ds_profit",
                 "dimensions": _f("offer_id", "ozon_name", "sku", "revenue_basis", "finance_data_status", "finance_status_reason"),
                 "fields": _f("delivered_units", "seller_revenue_rub", "commission_rub", "logistics_rub", "acquiring_rub",
                              "return_logistics_rub", "promotion_rub", "other_costs_rub", "unsettled_postings", "contribution_pre_cogs_rub"),
                 "calc": {"Комиссия, % выручки": "-SUM(commission_rub) / SUM(seller_revenue_rub)",
                          "Логистика, % выручки": "-SUM(logistics_rub) / SUM(seller_revenue_rub)"},
                 "rule": "Проценты считаются только по строкам revenue_basis = SETTLED (Ozon рассчитал отправления)."},
                {"type": "stacked_bar", "title": "Начисления вне заказов по суткам", "source": "ds_costs", "dimension": "event_date",
                 "breakdown": "accrual_class", "fields": _f("amount_rub"), "status_field": "data_status"},
                {"type": "table", "title": "Начисления по классу и уровню", "source": "ds_costs",
                 "dimensions": _f("accrual_class", "cost_scope"), "fields": _f("amount_rub", "accruals", "unresolved_accruals")},
            ],
        },
        {
            "id": "coverage", "title": "5. Покрытие данных",
            "controls": [],
            "elements": [
                {"type": "table", "title": "Свежесть по сущностям", "source": "ds_freshness",
                 "dimensions": _f("entity_title", "dashboard_section", "as_of_date", "as_of_status", "last_status", "last_reason",
                                  "first_complete_date", "last_complete_date", "is_stale"),
                 "fields": _f("days_since_last_complete", "expected_lag_days", "complete_days_30", "partial_days_30",
                              "problem_days_30", "unknown_days_30"),
                 "calc": {"Сегодня": STATUS_CASE.format(f="as_of_status"), "Последние оценённые сутки": STATUS_CASE.format(f="last_status")}},
                {"type": "pivot_heatmap", "title": "Статус по суткам (45 суток)", "source": "ds_coverage",
                 "rows": "entity", "columns": "coverage_date", "fields": _f("status", "reason"),
                 "rule": "Пустая клетка = нет оценки = «Нет данных (не загружено)» (серый), не ноль."},
            ],
        },
    ],
}


def spec_fields() -> dict[str, set[str]]:
    """Поля каждой вью, на которые ссылается спецификация (для сверки с CONTRACT.json)."""
    out: dict[str, set[str]] = {}
    tables = {k: v["table"] for k, v in SPEC["data_sources"].items()}

    def add(src: str, names) -> None:
        if src in tables:
            out.setdefault(tables[src], set()).update(n for n in names if n)

    def refs(text: str) -> list[str]:
        return re.findall(r"\b([a-z][a-z0-9_]+)\b", re.sub(r"'[^']*'", "", text))

    for bl in SPEC["blends"].values():
        for side in ("left", "right"):
            add(bl[side]["source"], [bl[side]["key"]] + bl[side]["fields"])
    for page in SPEC["pages"]:
        for c in page["controls"]:
            add(c.get("source", ""), [c.get("field")])
        b = page.get("banner")
        if b:
            add(b["source"], b["fields"] + [w for w in refs(b.get("filter", "")) if w not in ("in", "and")])
        for e in page["elements"]:
            src = e.get("source", "")
            names = list(e.get("fields", [])) + list(e.get("dimensions", [])) + [e.get("dimension"), e.get("breakdown"),
                                                                              e.get("status_field"), e.get("rows"), e.get("columns")]
            names += [w for w in refs(e.get("filter", "")) if w not in ("in", "and")]
            for formula in e.get("calc", {}).values():
                names += [w for w in refs(formula) if w.islower() and w not in ("then", "else", "end", "when", "case")]
            add(src, names)
    return out


def linking_url(template_id: str, project_id: str, report_name: str) -> str:
    """Linking API: копия шаблона, все источники (ds.*) — analytics_share проекта арендатора.

    Без ds.*.connector — режим «обновить»: таблица каждого источника берётся из шаблона, меняются
    только проект, датасет и платёжный проект. Учётные данные нового источника — того, кто открыл
    ссылку (документация Linking API), поэтому ссылку открывает сам клиент под своей идентичностью.
    """
    params = [("c.reportId", template_id), ("r.reportName", report_name),
              ("ds.*.projectId", project_id), ("ds.*.datasetId", DATASET), ("ds.*.billingProjectId", project_id)]
    return LOOKER_CREATE + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")


INTERNAL_PROBES = {
    "ozon_raw": "ozon_raw.RAW_OZON_POSTINGS_FBO",
    "ref": "ref.REF_SKU_CHANNEL_MAP",
    "ozon_mart": "ozon_mart.FACT_OZON_SALES_DAILY",
    "tenant_ops": "tenant_ops.DATA_COVERAGE",
    "tenant_locks": "tenant_locks.INFORMATION_SCHEMA.TABLES",
}
# Права, которых у клиента быть НЕ должно (testIamPermissions на проекте арендатора).
FORBIDDEN_PROJECT_PERMISSIONS = ("bigquery.datasets.create", "bigquery.tables.getData", "bigquery.tables.create",
                                 "bigquery.tables.list", "bigquery.jobs.listAll", "dataform.repositories.create",
                                 "dataform.folders.create", "cloudaicompanion.topics.create", "secretmanager.versions.access",
                                 "run.jobs.run", "cloudscheduler.jobs.run", "iam.serviceAccounts.actAs")


def isolation_probes(project_id: str) -> list[dict]:
    """Проверки от имени клиентской идентичности: ALLOW — пробный запуск проходит, DENY — отказ доступа."""
    probes = [{"expect": "ALLOW", "object": f"{project_id}.{DATASET}.{v}",
               "sql": f"SELECT * FROM `{project_id}.{DATASET}.{v}` LIMIT 1"} for v in CA.client_facing_views()]
    probes += [{"expect": "DENY", "object": f"{project_id}.{t}", "sql": f"SELECT * FROM `{project_id}.{t}` LIMIT 1"}
               for t in INTERNAL_PROBES.values()]
    for foreign in (PL.PLATFORM_PROJECT_ID, PL.EVETIS_PROJECT_ID):
        probes.append({"expect": "DENY", "object": f"{foreign}.region-eu.INFORMATION_SCHEMA.SCHEMATA",
                       "sql": f"SELECT schema_name FROM `{foreign}`.`region-eu`.INFORMATION_SCHEMA.SCHEMATA LIMIT 1"})
    return probes


def _dry_run(sql: str, project_id: str) -> tuple[bool, str]:
    cmd = ["bq", f"--project_id={project_id}", "query", "--dry_run", "--use_legacy_sql=false", sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    text = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (text[-1] if text else "")


def _test_permissions(project_id: str) -> set[str]:
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    body = json.dumps({"permissions": sorted(set(FORBIDDEN_PROJECT_PERMISSIONS) | set(CA.QUERY_ROLE_PERMISSIONS))})
    r = subprocess.run(["curl", "-sS", "-X", "POST", "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
                        "-d", body, f"https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}:testIamPermissions"],
                       capture_output=True, text=True, check=True)
    return set(json.loads(r.stdout or "{}").get("permissions", []))


def run_probes(project_id: str) -> int:
    """Только чтение: пробные запуски (--dry_run) и testIamPermissions. Ничего не создаёт."""
    fails = 0
    for p in isolation_probes(project_id):
        ok, msg = _dry_run(p["sql"], project_id)
        good = ok if p["expect"] == "ALLOW" else (not ok and re.search(r"(?i)access denied|permission|not found", msg) is not None)
        fails += not good
        print(f"{'PASS' if good else 'FAIL'} {p['expect']:5} {p['object']}  {'' if good else msg[:160]}")
    granted = _test_permissions(project_id)
    extra = granted & set(FORBIDDEN_PROJECT_PERMISSIONS)
    missing = set(CA.QUERY_ROLE_PERMISSIONS) - granted
    print(f"{'PASS' if not extra and not missing else 'FAIL'} testIamPermissions: лишние={sorted(extra)} недостающие={sorted(missing)}")
    return fails + bool(extra or missing)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("spec", "url", "probe"):
        print(__doc__)
        return 2
    if argv[0] == "spec":
        print(json.dumps(SPEC, ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "url":
        print(linking_url(argv[1], argv[2], argv[3] if len(argv) > 3 else "Аналитика Ozon"))
        return 0
    project = argv[1]
    if "--run" not in argv:
        for p in isolation_probes(project):
            print(f"{p['expect']:5} {p['sql']}")
        return 0
    return 1 if run_probes(project) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
