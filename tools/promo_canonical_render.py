#!/usr/bin/env python3
"""PR-PROMO-2 — рендер запросов к каноническому слою состояния акций.

Зачем. Канонические представления акций (16 VIEW в wb_mart, ozon_mart, evetis_mart) нужно
проверять ДО развёртывания и на данных, которых в production нет (неоднозначный title,
несопоставленный товар, пустые детали, повтор слота с ошибкой…). Второй реализации логики
на Python быть не должно: тест, который проверяет копию SQL, ничего не доказывает. Поэтому
инструмент берёт ТЕ ЖЕ тела представлений из Git и подставляет их в запрос как CTE.

Два режима, оба — чистое преобразование текста, без сети и без учётных данных:

  predeploy <query.sql>   ссылки на 16 представлений заменяются их телами из Git, RAW и
                          evetis_ref остаются живыми. Так проверки production
                          (sql/promotions/pr_promo2_canonical_validation.sql) исполнимы
                          до развёртывания, read-only.
  fixtures                собирает регрессионные сценарии: RAW-таблицы и REF_SKU_CHANNEL_MAP
                          заменены типизированными фикстурами (схема — из DDL PR-PROMO-1),
                          тела представлений — из Git, утверждения — SELECT со status
                          PASS/FAIL, один блок `-- @check` на сценарий. Исполняется в
                          BigQuery read-only и не читает ни одной таблицы.

Результат `fixtures` (~1 МБ: тела и фикстуры повторяются в каждом блоке, потому что
планировщик BigQuery не выдерживает все утверждения в одном операторе) в Git не хранится —
он всегда собирается из текущих тел, поэтому разойтись с представлениями не может.

Третий режим — `run` — исполняет блоки через tools/lib/bq_readonly (только SELECT/WITH,
отказ до отправки для всего остального). Код выхода: 0 — все PASS, 1 — FAIL/EMPTY, 3 — ошибка.

usage:
  python tools/promo_canonical_render.py predeploy <query.sql>            # в stdout
  python tools/promo_canonical_render.py fixtures [--out <path>]          # в stdout или файл вне Git
  python tools/promo_canonical_render.py run fixtures --project <P> --token-command "gcloud auth print-access-token"
  python tools/promo_canonical_render.py run predeploy <checks.sql> --project <P> --token-command "…"
  python tools/promo_canonical_render.py run live <checks.sql> --project <P> --token-command "…"
"""
from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
RAW_DDL = ROOT / "sql" / "promotions" / "pr_promo1_raw.sql"

# Порядок = порядок зависимостей (каждый объект ссылается только на предыдущие).
OBJECTS: list[tuple[str, str, str]] = [
    ("wb_mart", "V_WB_PROMO_OBSERVATION_HISTORY", "sql/promotions/pr_promo2/wb_mart"),
    ("wb_mart", "V_WB_PROMO_HISTORY", "sql/promotions/pr_promo2/wb_mart"),
    ("wb_mart", "V_WB_PROMO_RANGING_HISTORY", "sql/promotions/pr_promo2/wb_mart"),
    ("wb_mart", "V_WB_PROMO_SKU_EVIDENCE_HISTORY", "sql/promotions/pr_promo2/wb_mart"),
    ("wb_mart", "V_WB_PROMO_SKU_STATE_HISTORY", "sql/promotions/pr_promo2/wb_mart"),
    ("ozon_mart", "V_OZON_PROMO_OBSERVATION_HISTORY", "sql/current/ozon_mart"),
    ("ozon_mart", "V_OZON_PROMO_HISTORY", "sql/current/ozon_mart"),
    ("ozon_mart", "V_OZON_PROMO_SKU_EVIDENCE_HISTORY", "sql/current/ozon_mart"),
    ("ozon_mart", "V_OZON_PROMO_SKU_STATE_HISTORY", "sql/current/ozon_mart"),
    ("evetis_mart", "V_PROMO_OBSERVATION_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_STATE_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_STATE_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_SKU_EVIDENCE_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_SKU_STATE_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_SKU_STATE_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_OBSERVABILITY_CURRENT", "sql/current/evetis_mart"),
]

RAW_TABLES: list[tuple[str, str]] = [
    ("wb_raw", "WB_PROMO_OBSERVATIONS"),
    ("wb_raw", "RAW_WB_PROMO_CALENDAR"),
    ("wb_raw", "RAW_WB_PROMO_RANGING"),
    ("wb_raw", "RAW_WB_PROMO_NOMENCLATURE"),
    ("ozon_raw", "OZON_PROMO_OBSERVATIONS"),
    ("ozon_raw", "RAW_OZON_PROMO_ACTIONS"),
    ("ozon_raw", "RAW_OZON_PROMO_PRODUCTS"),
    ("ozon_raw", "RAW_OZON_PROMO_AUTO_ADD"),
    ("ozon_raw", "RAW_OZON_PROMO_PRODUCT_MARKETING"),
    ("ozon_raw", "RAW_OZON_PROMO_PRODUCT_ACTION"),
    ("evetis_ref", "REF_SKU_CHANNEL_MAP"),
]

# Схема справочника каналов — живая (INFORMATION_SCHEMA, 2026-09-23). Его DDL нет в PR-PROMO-1.
REF_SKU_CHANNEL_MAP_SCHEMA: list[tuple[str, str]] = [
    ("internal_sku", "STRING"), ("marketplace", "STRING"), ("marketplace_sku", "STRING"),
    ("marketplace_product_id", "STRING"), ("offer_id", "STRING"), ("vendor_code", "STRING"),
    ("valid_from", "DATE"), ("valid_to", "DATE"), ("is_current", "BOOL"), ("mapping_source", "STRING"),
    ("mapping_status", "STRING"), ("verified_at", "DATE"), ("loaded_at", "TIMESTAMP"),
]

REF_RE = re.compile(r"`" + re.escape(PROJECT) + r"\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)`")


def object_path(dataset: str, name: str) -> Path:
    for ds, obj, folder in OBJECTS:
        if ds == dataset and obj == name:
            return ROOT / folder / f"{obj}.sql"
    raise KeyError(f"{dataset}.{name}")


def view_body(dataset: str, name: str) -> str:
    """Тело представления: текст после строки `AS` оператора CREATE OR REPLACE VIEW."""
    sql = object_path(dataset, name).read_text(encoding="utf-8")
    head, sep, body = sql.partition("\nAS\n")
    if not sep or "CREATE OR REPLACE VIEW" not in head:
        raise SystemExit(f"cannot locate the view body in {object_path(dataset, name)}")
    return body.strip().rstrip(";").strip()


def cte_name(dataset: str, name: str) -> str:
    return f"{'fx' if (dataset, name) in RAW_TABLES else 'v'}__{dataset}__{name}"


def _replace_refs(sql: str, replace_raw: bool) -> tuple[str, set[tuple[str, str]]]:
    """Заменить ссылки на объекты слоя (и, по флагу, на RAW) именами CTE."""
    used: set[tuple[str, str]] = set()
    objects = {(d, n) for d, n, _ in OBJECTS}

    def sub(m: re.Match) -> str:
        key = (m.group(1), m.group(2))
        if key in objects or (replace_raw and key in RAW_TABLES):
            used.add(key)
            return cte_name(*key)
        return m.group(0)

    return REF_RE.sub(sub, sql), used


def view_ctes(needed: set[tuple[str, str]], replace_raw: bool) -> tuple[list[str], set[tuple[str, str]]]:
    """CTE для всех нужных представлений (транзитивно), в порядке зависимостей."""
    closure: set[tuple[str, str]] = set()
    raw_used: set[tuple[str, str]] = set()
    pending = [k for k in needed if k not in RAW_TABLES]
    bodies: dict[tuple[str, str], str] = {}
    while pending:
        key = pending.pop()
        if key in closure:
            continue
        closure.add(key)
        body, used = _replace_refs(view_body(*key), replace_raw)
        bodies[key] = body
        for u in used:
            if u in RAW_TABLES:
                raw_used.add(u)
            elif u not in closure:
                pending.append(u)
    ordered = [(d, n) for d, n, _ in OBJECTS if (d, n) in closure]
    return [f"{cte_name(*k)} AS (\n{bodies[k]}\n)" for k in ordered], raw_used


def _merge_with(ctes: list[str], query: str) -> str:
    """Добавить CTE перед запросом; если запрос сам начинается с WITH — слить списки."""
    q = query.strip().rstrip(";").strip()
    if not ctes:
        return q
    m = re.match(r"(?is)^with\s+(recursive\s+)?", q)
    if m:
        return "WITH\n" + ",\n".join(ctes) + ",\n" + q[m.end():]
    return "WITH\n" + ",\n".join(ctes) + "\n" + q


def render_predeploy(query: str) -> str:
    """Запрос к живым RAW, но к представлениям слоя из Git (до развёртывания)."""
    q, used = _replace_refs(query, replace_raw=False)
    ctes, _ = view_ctes(used, replace_raw=False)
    return _merge_with(ctes, q)


def render_predeploy_file(text: str) -> str:
    """Отрендерить файл проверок: каждый блок `-- @check` отдельно, заголовки сохраняются."""
    parts = re.split(r"(?m)^(?=-- @check )", text)
    out = []
    for part in parts:
        if not part.startswith("-- @check "):
            out.append(part)
            continue
        lines = part.splitlines(keepends=True)
        header = []
        while lines and lines[0].startswith("--"):
            header.append(lines.pop(0))
        stmt = "".join(lines).strip()
        if not stmt:
            out.append("".join(header))
            continue
        out.append("".join(header) + render_predeploy(stmt) + ";\n\n")
    return "".join(out)


# ─────────────────────────────────────────────────────────────── фикстуры
def raw_schemas() -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Схемы RAW-таблиц из DDL PR-PROMO-1 (единственный авторитетный источник их формы)."""
    text = RAW_DDL.read_text(encoding="utf-8")
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS `" + re.escape(PROJECT) + r"\.(\w+)\.(\w+)`\s*\((.*?)\n\)",
                         text, re.S):
        cols = []
        for line in m.group(3).splitlines():
            cm = re.match(r"^\s{2}([a-z_][a-z0-9_]*)\s+(TIMESTAMP|STRING|INT64|NUMERIC|BOOL|DATE)\b", line)
            if cm:
                cols.append((cm.group(1), cm.group(2)))
        out[(m.group(1), m.group(2))] = cols
    out[("evetis_ref", "REF_SKU_CHANNEL_MAP")] = list(REF_SKU_CHANNEL_MAP_SCHEMA)
    missing = [t for t in RAW_TABLES if t not in out]
    if missing:
        raise SystemExit(f"DDL не содержит таблиц: {missing}")
    return out


def _lit(value, dtype: str) -> str:
    if value is None:
        return "NULL"
    if dtype == "BOOL":
        return "TRUE" if value else "FALSE"
    if dtype == "INT64":
        return str(int(value))
    if dtype == "NUMERIC":
        return f"NUMERIC '{Decimal(str(value))}'"
    if dtype == "TIMESTAMP":
        return f"TIMESTAMP '{value}'"
    if dtype == "DATE":
        return f"DATE '{value}'"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def fixture_cte(key: tuple[str, str], schema: list[tuple[str, str]], rows: list[dict]) -> str:
    cols = {c for c, _ in schema}
    for r in rows:
        unknown = set(r) - cols
        if unknown:
            raise SystemExit(f"фикстура {key}: неизвестные колонки {sorted(unknown)}")
    struct = ", ".join(f"{c} {t}" for c, t in schema)
    values = ",\n    ".join("(" + ", ".join(_lit(r.get(c), t) for c, t in schema) + ")" for r in rows)
    return f"{cte_name(*key)} AS (\n  SELECT * FROM UNNEST(ARRAY<STRUCT<{struct}>>[\n    {values}\n  ])\n)"


# Сценарии. Данные намеренно маленькие и читаемые: каждая строка существует ради
# конкретного утверждения ниже. Время — UTC.
O1, O2 = "OZPROMO_prod_202601010400", "OZPROMO_prod_202601010900"
O3_ERR, O_HIST = "OZPROMO_prod_202601011400", "OZPROMOHIST_prod_202601011900"
W1, W2 = "WBPROMO_prod_202601010400", "WBPROMO_prod_202601010900"
W3_ERR, W_HIST = "WBPROMO_prod_202601011400", "WBPROMOHIST_prod_202601011900"
T_O1, T_O2, T_O3, T_OH = ("2026-01-01 04:01:00+00", "2026-01-01 09:01:00+00",
                          "2026-01-01 14:01:00+00", "2026-01-01 19:01:00+00")
T_W1, T_W2, T_W3, T_WH = ("2026-01-01 04:00:30+00", "2026-01-01 09:00:30+00",
                          "2026-01-01 14:00:30+00", "2026-01-01 19:00:30+00")
AUTO_ADD_DATE = "2026-01-05 21:00:00+00"


def _meta(obs: str, ts: str, slot: str) -> dict:
    return {"observation_id": obs, "observed_at": ts, "observation_bucket": slot, "environment": "prod",
            "run_id": f"fixture:{obs}", "ingested_at": ts}


def _ozon_actions(obs: str, ts: str, slot: str, phase: str) -> list[dict]:
    m = _meta(obs, ts, slot)
    a101_part, a101_pot = (5, 3) if phase == "O2" else (4, 4)
    rows = [
        dict(m, action_id=101, title="Акция один", action_type="STOCK_DISCOUNT",
             date_start="2025-12-31 21:00:00+00", date_end="2026-01-31 20:59:59+00",
             auto_add_dates_csv="2026-01-05T21:00:00Z", auto_add_dates_count=1, is_participating=True,
             participating_products_count=a101_part, potential_products_count=a101_pot, source_payload_hash="a101"),
        # Объявлено 5 участников, перечислен 1 → ENUMERATION_INCOMPLETE → UNKNOWN вместо NOT_ELIGIBLE.
        dict(m, action_id=102, title="Акция два", action_type="STOCK_DISCOUNT",
             date_start="2026-02-01 21:00:00+00", date_end="2026-02-10 20:59:59+00",
             auto_add_dates_count=0, is_participating=False,
             participating_products_count=5, potential_products_count=0, source_payload_hash="a102"),
        # Два одинаковых title — связь marketing_actions обязана остаться AMBIGUOUS.
        dict(m, action_id=103, title="Дубль", action_type="STOCK_DISCOUNT",
             date_start="2025-12-20 21:00:00+00", date_end="2026-01-20 20:59:59+00",
             auto_add_dates_count=0, is_participating=False,
             participating_products_count=0, potential_products_count=0, source_payload_hash="a103"),
        dict(m, action_id=104, title="Дубль", action_type="STOCK_DISCOUNT",
             date_start="2025-12-25 21:00:00+00", date_end="2026-01-25 20:59:59+00",
             auto_add_dates_count=0, is_participating=False,
             participating_products_count=0, potential_products_count=0, source_payload_hash="a104"),
        # Даты не отданы → lifecycle UNKNOWN.
        dict(m, action_id=105, title="Без дат", action_type="STOCK_DISCOUNT",
             auto_add_dates_count=0, is_participating=False,
             participating_products_count=0, potential_products_count=0, source_payload_hash="a105"),
        # Граница: date_end = observed_at первого снимка → ACTIVE (включительно); во втором → ENDED.
        dict(m, action_id=107, title="Граница", action_type="STOCK_DISCOUNT",
             date_start="2025-12-31 21:00:00+00", date_end=T_O1,
             auto_add_dates_count=0, is_participating=False,
             participating_products_count=0, potential_products_count=0, source_payload_hash="a107"),
    ]
    if phase == "O1":
        # Завершённая акция, которую Ozon затем убирает из /v1/actions (нет во втором снимке).
        rows.append(dict(m, action_id=106, title="Завершённая", action_type="STOCK_DISCOUNT",
                         date_start="2025-12-01 21:00:00+00", date_end="2025-12-31 20:59:59+00",
                         auto_add_dates_count=0, is_participating=None,
                         participating_products_count=0, potential_products_count=0, source_payload_hash="a106"))
    return rows


def _ozon_products(obs: str, ts: str, slot: str, phase: str) -> list[dict]:
    m = _meta(obs, ts, slot)

    def p(aid, pid, membership, add_mode):
        return dict(m, action_id=aid, membership=membership, product_id=pid, add_mode=add_mode,
                    sku_resolution_status="RESOLVED_BY_PRODUCT_ID", price_rub=1000, action_price_rub=900,
                    max_action_price_rub=950, source_payload_hash=f"{aid}{membership}{pid}")

    part = [(101, 1001), (101, 1005), (101, 1006), (101, 1008), (102, 1001)]
    cand = [(101, 1002), (101, 1004), (101, 1005), (101, 1006)]
    if phase == "O2":
        part.append((101, 1002))
        cand.remove((101, 1002))
    return ([p(a, i, "PARTICIPATING", "MANUAL") for a, i in part]
            + [p(a, i, "CANDIDATE", "NOT_SET") for a, i in cand])


def _ozon_auto_add(obs: str, ts: str, slot: str) -> list[dict]:
    m = _meta(obs, ts, slot)

    def a(pid, kind, add_mode):
        return dict(m, action_id=101, auto_add_at=AUTO_ADD_DATE, list_kind=kind, product_id=pid,
                    offer_id=f"9{pid - 1000:03d}", ozon_sku=8000 + pid - 1000, add_mode=add_mode,
                    sku_resolution_status="RESOLVED_BY_PRODUCT_ID", price_rub=1000,
                    action_price_to_auto_add_rub=1000, source_payload_hash=f"aa{kind}{pid}")

    return [a(1003, "SCHEDULED", "AUTO"), a(1004, "SCHEDULED", "AUTO"), a(1006, "SCHEDULED", "MANUAL"),
            a(1007, "ELIGIBLE", None)]


def _ozon_catalog(obs: str, ts: str, slot: str) -> list[dict]:
    m = _meta(obs, ts, slot)
    return [dict(m, offer_id=f"9{pid - 1000:03d}", product_id=pid, sku_resolution_status="RESOLVED_BY_PRODUCT_ID",
                 actions_count=0, source_payload_hash=f"mk{pid}") for pid in range(1001, 1010)]


def _ozon_marketing_actions(obs: str, ts: str, slot: str) -> list[dict]:
    m = _meta(obs, ts, slot)

    def r(offer, pid, ordinal, title, d_from, d_to):
        return dict(m, offer_id=offer, product_id=pid, action_ordinal=ordinal, action_title=title,
                    action_date_from=d_from, action_date_to=d_to, source_payload_hash=f"ma{offer}{ordinal}")

    return [
        r("9001", 1001, 0, "Акция один", "2025-12-31 21:00:00+00", "2026-01-31 20:59:59+00"),   # точное
        r("9001", 1001, 1, "Дубль", "2025-12-20 21:00:00+00", "2026-01-20 20:59:59+00"),        # 2 акции
        r("9002", 1002, 0, "Акция один", "2025-12-30 21:00:00+00", "2026-01-31 20:59:59+00"),   # окно ≠
        r("9005", 1005, 0, "Программа Ozon", "2025-12-01 21:00:00+00", "2026-03-01 20:59:59+00"),  # нет в /v1/actions
        r("9006", 1006, 0, "Акция один", "2025-12-31 21:00:00+00", "2026-01-31 20:59:59+00"),   # точное
    ]


def _ref_rows() -> list[dict]:
    rows = [dict(internal_sku=f"SKU_{pid - 1000:03d}", marketplace="OZON", marketplace_sku=str(8000 + pid - 1000),
                 marketplace_product_id=str(pid), offer_id=f"9{pid - 1000:03d}", is_current=True)
            for pid in (1001, 1002, 1003, 1004, 1005, 1006, 1007, 1009)]  # 1008 намеренно отсутствует
    rows += [dict(internal_sku="SKU_001", marketplace="WB", marketplace_sku="5001", is_current=True),
             dict(internal_sku="SKU_002", marketplace="WB", marketplace_sku="5002", is_current=True)]
    # Неактуальная строка не должна влиять на сопоставление.
    rows.append(dict(internal_sku="SKU_OLD", marketplace="OZON", marketplace_sku="7999",
                     marketplace_product_id="1008", offer_id="9008", is_current=False))
    return rows


def _wb_calendar(obs: str, ts: str, slot: str) -> list[dict]:
    m = _meta(obs, ts, slot)

    def c(pid, ptype, start, end, details, in_total, not_in, status, **kw):
        return dict(m, promotion_id=pid, promotion_name=f"Акция WB {pid}", promotion_type=ptype,
                    is_auto_promotion=(ptype == "auto"), starts_at=start, ends_at=end, details_available=details,
                    in_promo_total=in_total, not_in_promo_total=not_in, nomenclature_status=status,
                    sku_level_data_available=(status in ("FETCHED", "EMPTY")),
                    source_payload_hash=f"w{pid}", **kw)

    active = ("2025-12-31 21:00:00+00", "2026-01-31 20:59:59+00")
    return [
        c(201, "auto", *active, True, 3, 5, "SKIPPED_AUTO_PROMOTION", ranging_tiers=2,
          ranging_condition="productsInPromotion", participation_pct=38),
        c(202, "auto", "2025-12-01 21:00:00+00", "2025-12-31 20:59:59+00", False, None, None, "SKIPPED_AUTO_PROMOTION"),
        c(203, "regular", *active, True, 1, 2, "FETCHED"),
        c(204, "regular", *active, True, 0, 0, "EMPTY"),
        c(205, "regular", *active, True, 0, 1, "HTTP_ERROR"),
        c(206, "auto", "2026-02-01 21:00:00+00", "2026-02-10 20:59:59+00", True, 0, 4, "SKIPPED_AUTO_PROMOTION"),
    ]


def fixtures() -> dict[tuple[str, str], list[dict]]:
    ozon_obs = [
        dict(observation_id=O1, observation_bucket="2026-01-01T04:00", environment="prod", status="COMPLETE",
             started_at=T_O1, observed_at=T_O1, completed_at=T_O1),
        # REUSED: манифест переписан повтором позже — observed_at канона обязан остаться временем данных.
        dict(observation_id=O2, observation_bucket="2026-01-01T09:00", environment="prod", status="REUSED",
             started_at=T_O2, observed_at="2026-01-01 09:55:00+00", completed_at="2026-01-01 09:55:00+00"),
        dict(observation_id=O3_ERR, observation_bucket="2026-01-01T14:00", environment="prod", status="ERROR",
             started_at=T_O3, observed_at=None, completed_at=T_O3),
        dict(observation_id=O_HIST, observation_bucket="HISTORICAL", environment="prod", status="COMPLETE",
             started_at=T_OH, observed_at=T_OH, completed_at=T_OH),
    ]
    actions = (_ozon_actions(O1, T_O1, "2026-01-01T04:00", "O1") + _ozon_actions(O2, T_O2, "2026-01-01T09:00", "O2")
               + [dict(_meta(O3_ERR, T_O3, "2026-01-01T14:00"), action_id=101, title="Акция один",
                       action_type="STOCK_DISCOUNT", date_start="2025-12-31 21:00:00+00",
                       date_end="2026-01-31 20:59:59+00", auto_add_dates_count=0, is_participating=True,
                       participating_products_count=0, potential_products_count=1, source_payload_hash="err"),
                  dict(_meta(O_HIST, T_OH, "HISTORICAL"), action_id=108, title="Историческая",
                       action_type="STOCK_DISCOUNT", date_start="2024-01-01 21:00:00+00",
                       date_end="2024-01-10 20:59:59+00", auto_add_dates_count=0, is_participating=True,
                       participating_products_count=0, potential_products_count=0, source_payload_hash="hist")])
    products = (_ozon_products(O1, T_O1, "2026-01-01T04:00", "O1") + _ozon_products(O2, T_O2, "2026-01-01T09:00", "O2")
                + [dict(_meta(O3_ERR, T_O3, "2026-01-01T14:00"), action_id=101, membership="CANDIDATE",
                        product_id=1002, add_mode="NOT_SET", source_payload_hash="err")])
    wb_obs = [
        dict(observation_id=W1, observation_bucket="2026-01-01T04:00", environment="prod", status="COMPLETE",
             started_at=T_W1, observed_at=T_W1, completed_at=T_W1),
        dict(observation_id=W2, observation_bucket="2026-01-01T09:00", environment="prod", status="REUSED",
             started_at=T_W2, observed_at="2026-01-01 09:50:00+00", completed_at="2026-01-01 09:50:00+00"),
        dict(observation_id=W3_ERR, observation_bucket="2026-01-01T14:00", environment="prod", status="ERROR",
             started_at=T_W3, observed_at=None, completed_at=T_W3),
        dict(observation_id=W_HIST, observation_bucket="HISTORICAL", environment="prod", status="COMPLETE",
             started_at=T_WH, observed_at=T_WH, completed_at=T_WH),
    ]
    calendar = (_wb_calendar(W1, T_W1, "2026-01-01T04:00") + _wb_calendar(W2, T_W2, "2026-01-01T09:00")
                + [dict(_meta(W3_ERR, T_W3, "2026-01-01T14:00"), promotion_id=201, promotion_type="auto",
                        starts_at="2025-12-31 21:00:00+00", ends_at="2026-01-31 20:59:59+00", in_promo_total=99,
                        details_available=True, nomenclature_status="SKIPPED_AUTO_PROMOTION", source_payload_hash="err"),
                   dict(_meta(W_HIST, T_WH, "HISTORICAL"), promotion_id=208, promotion_type="auto",
                        starts_at="2024-01-01 21:00:00+00", ends_at="2024-01-10 20:59:59+00", details_available=False,
                        nomenclature_status="SKIPPED_AUTO_PROMOTION", source_payload_hash="hist")])
    ranging = [dict(_meta(w, t, s), promotion_id=201, tier_ordinal=i, condition="productsInPromotion",
                    participation_rate=rate, boost_pct=boost, source_payload_hash=f"r{i}")
               for w, t, s in ((W1, T_W1, "2026-01-01T04:00"), (W2, T_W2, "2026-01-01T09:00"))
               for i, rate, boost in ((0, 1, 25), (1, 50, 30))]
    nomenclature = [dict(_meta(w, t, s), promotion_id=203, in_action_requested=req, nm_id=nm, in_action=ina,
                         price=1500, plan_price=1200, discount_pct=20, plan_discount_pct=30, currency_code="RUB",
                         source_payload_hash=f"n{nm}")
                    for w, t, s in ((W1, T_W1, "2026-01-01T04:00"), (W2, T_W2, "2026-01-01T09:00"))
                    for req, nm, ina in ((True, 5001, True), (False, 5002, False), (False, 5003, False))]
    return {
        ("wb_raw", "WB_PROMO_OBSERVATIONS"): wb_obs,
        ("wb_raw", "RAW_WB_PROMO_CALENDAR"): calendar,
        ("wb_raw", "RAW_WB_PROMO_RANGING"): ranging,
        ("wb_raw", "RAW_WB_PROMO_NOMENCLATURE"): nomenclature,
        ("ozon_raw", "OZON_PROMO_OBSERVATIONS"): ozon_obs,
        ("ozon_raw", "RAW_OZON_PROMO_ACTIONS"): actions,
        ("ozon_raw", "RAW_OZON_PROMO_PRODUCTS"): products,
        ("ozon_raw", "RAW_OZON_PROMO_AUTO_ADD"): _ozon_auto_add(O1, T_O1, "2026-01-01T04:00")
        + _ozon_auto_add(O2, T_O2, "2026-01-01T09:00"),
        ("ozon_raw", "RAW_OZON_PROMO_PRODUCT_MARKETING"): _ozon_catalog(O1, T_O1, "2026-01-01T04:00")
        + _ozon_catalog(O2, T_O2, "2026-01-01T09:00"),
        ("ozon_raw", "RAW_OZON_PROMO_PRODUCT_ACTION"): _ozon_marketing_actions(O1, T_O1, "2026-01-01T04:00")
        + _ozon_marketing_actions(O2, T_O2, "2026-01-01T09:00"),
        ("evetis_ref", "REF_SKU_CHANNEL_MAP"): _ref_rows(),
    }


P = f"`{PROJECT}"
OZ_OBS = f"{P}.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`"
OZ_H = f"{P}.ozon_mart.V_OZON_PROMO_HISTORY`"
OZ_EV = f"{P}.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`"
OZ_ST = f"{P}.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`"
WB_OBS = f"{P}.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`"
WB_H = f"{P}.wb_mart.V_WB_PROMO_HISTORY`"
WB_R = f"{P}.wb_mart.V_WB_PROMO_RANGING_HISTORY`"
WB_EV = f"{P}.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`"
WB_ST = f"{P}.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY`"
N_OBS = f"{P}.evetis_mart.V_PROMO_OBSERVATION_HISTORY`"
N_H = f"{P}.evetis_mart.V_PROMO_STATE_HISTORY`"
N_C = f"{P}.evetis_mart.V_PROMO_STATE_CURRENT`"
N_EV = f"{P}.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`"
N_ST = f"{P}.evetis_mart.V_PROMO_SKU_STATE_HISTORY`"
N_SC = f"{P}.evetis_mart.V_PROMO_SKU_STATE_CURRENT`"
N_OBSV = f"{P}.evetis_mart.V_PROMO_OBSERVABILITY_CURRENT`"


def oz_state(obs: str, aid: int, pid: int, col: str) -> str:
    return f"(SELECT {col} FROM {OZ_ST} WHERE observation_id = '{obs}' AND action_id = {aid} AND product_id = {pid})"


def n_row(view: str, where: str, col: str) -> str:
    return f"(SELECT {col} FROM {view} WHERE {where})"


def cnt(view: str, where: str = "TRUE") -> str:
    return f"(SELECT COUNT(*) FROM {view} WHERE {where})"


COUNTS4 = "FORMAT('%d/%d/%d/%d', units_total, units_established, units_not_observable, units_undetermined)"


def grain(view: str, key: str) -> str:
    return f"(SELECT COUNT(*) = COUNT(DISTINCT {key}) FROM {view})"


# (сценарий, утверждение, булево выражение SQL). NULL считается FAIL.
ASSERTIONS: list[tuple[str, str, str]] = [
    # 1. Кандидат, и только.
    ("FX01", "candidate_only_resolves_candidate", f"{oz_state(O1, 101, 1002, 'resolved_state')} = 'CANDIDATE'"),
    ("FX01", "candidate_only_rule_r4", f"{oz_state(O1, 101, 1002, 'resolution_rule')} = 'R4_CANDIDATES_LIST'"),
    ("FX01", "candidate_only_auto_add_not_listed", f"{oz_state(O1, 101, 1002, 'auto_add_state')} = 'NOT_LISTED'"),
    ("FX01", "candidate_only_not_in_scheduled_list", f"{oz_state(O1, 101, 1002, 'in_auto_add_scheduled_list')} = FALSE"),
    # 2. Запланированное автодобавление без участия.
    ("FX02", "scheduled_only_resolves_scheduled", f"{oz_state(O1, 101, 1003, 'resolved_state')} = 'SCHEDULED_AUTO_ADD'"),
    ("FX02", "scheduled_only_not_participating_now", f"{oz_state(O1, 101, 1003, 'participation_state')} = 'NOT_ELIGIBLE'"),
    ("FX02", "scheduled_only_date_kept",
     f"{oz_state(O1, 101, 1003, 'next_scheduled_auto_add_at')} = TIMESTAMP '{AUTO_ADD_DATE}'"),
    ("FX02", "scheduled_only_in_participants_false", f"{oz_state(O1, 101, 1003, 'in_participants_list')} = FALSE"),
    # 3. Участник.
    ("FX03", "participating_resolves_participating", f"{oz_state(O1, 101, 1001, 'resolved_state')} = 'PARTICIPATING'"),
    ("FX03", "participating_add_mode_kept", f"{oz_state(O1, 101, 1001, 'participation_add_mode')} = 'MANUAL'"),
    ("FX03", "participating_no_conflict", f"{oz_state(O1, 101, 1001, 'evidence_conflict')} = FALSE"),
    ("FX03", "participating_direct_source",
     f"{oz_state(O1, 101, 1001, 'resolved_state_evidence_class')} = 'DIRECT_SOURCE'"),
    # 4. Кандидат + запланирован.
    ("FX04", "candidate_scheduled_resolves_scheduled", f"{oz_state(O1, 101, 1004, 'resolved_state')} = 'SCHEDULED_AUTO_ADD'"),
    ("FX04", "candidate_scheduled_candidate_axis_kept", f"{oz_state(O1, 101, 1004, 'participation_state')} = 'CANDIDATE'"),
    ("FX04", "candidate_scheduled_flag_kept", f"{oz_state(O1, 101, 1004, 'in_candidates_list')} = TRUE"),
    # 5. Кандидат + участник (противоречие источника).
    ("FX05", "candidate_participating_resolves_participating",
     f"{oz_state(O1, 101, 1005, 'resolved_state')} = 'PARTICIPATING'"),
    ("FX05", "candidate_participating_conflict_flagged", f"{oz_state(O1, 101, 1005, 'evidence_conflict')} = TRUE"),
    ("FX05", "candidate_participating_both_evidence_kept",
     f"{cnt(OZ_EV, f"observation_id = '{O1}' AND action_id = 101 AND product_id = 1005")} = 2"),
    # 6. Кандидат + запланирован + участник.
    ("FX06", "all_three_resolves_participating", f"{oz_state(O1, 101, 1006, 'resolved_state')} = 'PARTICIPATING'"),
    ("FX06", "all_three_auto_add_axis_kept", f"{oz_state(O1, 101, 1006, 'auto_add_state')} = 'SCHEDULED'"),
    ("FX06", "all_three_conflict_flagged", f"{oz_state(O1, 101, 1006, 'evidence_conflict')} = TRUE"),
    ("FX06", "all_three_evidence_rows_kept",
     f"{cnt(OZ_EV, f"observation_id = '{O1}' AND action_id = 101 AND product_id = 1006")} = 4"),
    ("FX06", "candidate_scheduled_participating_are_distinct",
     f"(SELECT COUNT(DISTINCT resolved_state) FROM {OZ_ST} WHERE observation_id = '{O1}' AND action_id = 101 "
     f"AND product_id IN (1001, 1002, 1003)) = 3"),
    # 7. Неоднозначный title marketing_actions.
    ("FX07", "duplicate_title_is_ambiguous",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9001' AND action_ordinal = 1", 'promotion_link_method')} = 'AMBIGUOUS'"),
    ("FX07", "duplicate_title_not_resolved_to_an_id",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9001' AND action_ordinal = 1", 'action_id IS NULL')}"),
    ("FX07", "duplicate_title_candidates_exposed",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9001' AND action_ordinal = 1", 'link_candidate_action_ids')} = '103,104'"),
    ("FX07", "title_match_with_other_window_is_ambiguous",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9002' AND action_ordinal = 0", 'promotion_link_method')} = 'AMBIGUOUS'"),
    ("FX07", "exact_title_and_window_links",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9001' AND action_ordinal = 0", 'action_id')} = 101"),
    ("FX07", "exact_link_method",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9001' AND action_ordinal = 0", 'promotion_link_method')} = 'DETERMINISTIC_EXACT_MATCH'"),
    ("FX07", "unknown_title_unresolved",
     f"{n_row(OZ_EV, f"observation_id = '{O1}' AND offer_id = '9005' AND action_ordinal = 0", 'promotion_link_method')} = 'UNRESOLVED'"),
    ("FX07", "corroborated_by_exact_link", f"{oz_state(O1, 101, 1001, 'marketing_actions_corroborated')} = TRUE"),
    ("FX07", "ambiguous_product_corroboration_unknown",
     f"{oz_state(O1, 101, 1002, 'marketing_actions_corroborated')} IS NULL"),
    ("FX07", "not_corroborated_is_false_not_null",
     f"{oz_state(O1, 101, 1005, 'marketing_actions_corroborated')} = FALSE"),
    ("FX07", "marketing_rows_never_drive_state",
     f"(SELECT COUNT(*) FROM {OZ_ST} WHERE observation_id = '{O1}' AND product_id = 1005 AND action_id = 103 "
     f"AND resolved_state = 'NOT_ELIGIBLE') = 1"),
    # 8. Товар без сопоставления.
    ("FX08", "unmapped_row_kept_in_state", f"{oz_state(O1, 101, 1008, 'mapping_status')} = 'UNMAPPED'"),
    ("FX08", "unmapped_internal_sku_null", f"{oz_state(O1, 101, 1008, 'internal_sku IS NULL')}"),
    ("FX08", "unmapped_state_still_resolved", f"{oz_state(O1, 101, 1008, 'resolved_state')} = 'PARTICIPATING'"),
    ("FX08", "unmapped_kept_in_current",
     f"{cnt(N_SC, "marketplace = 'OZON' AND source_promotion_id = '101' AND marketplace_product_id = '1008' AND mapping_status = 'UNMAPPED'")} = 1"),
    ("FX08", "non_current_mapping_ignored", f"{oz_state(O1, 101, 1008, 'ozon_sku IS NULL')}"),
    # 9. Автоакция WB: агрегат есть, состава нет.
    ("FX09", "wb_auto_not_observable",
     f"{n_row(N_H, f"marketplace = 'WB' AND source_promotion_id = '201' AND observation_id = '{W1}'", 'sku_membership_observability')} = 'NOT_OBSERVABLE'"),
    ("FX09", "wb_auto_aggregate_kept",
     f"{n_row(N_H, f"marketplace = 'WB' AND source_promotion_id = '201' AND observation_id = '{W1}'", 'source_in_promo_count')} = 3"),
    ("FX09", "wb_auto_seller_participation_from_aggregate",
     f"{n_row(N_H, f"marketplace = 'WB' AND source_promotion_id = '201' AND observation_id = '{W1}'", 'seller_participation_state')} = 'PARTICIPATING'"),
    ("FX09", "wb_auto_no_sku_state_rows", f"{cnt(N_ST, "marketplace = 'WB' AND source_promotion_id = '201'")} = 0"),
    ("FX09", "wb_auto_no_sku_evidence_rows", f"{cnt(N_EV, "marketplace = 'WB' AND source_promotion_id = '201'")} = 0"),
    ("FX09", "wb_auto_no_sku_current_rows", f"{cnt(N_SC, "marketplace = 'WB' AND source_promotion_id = '201'")} = 0"),
    # 10. Завершённая акция WB с пустыми деталями.
    ("FX10", "wb_ended_lifecycle",
     f"{n_row(WB_H, f"promotion_id = 202 AND observation_id = '{W1}'", 'lifecycle_status')} = 'ENDED'"),
    ("FX10", "wb_ended_aggregates_not_returned",
     f"{n_row(WB_H, f"promotion_id = 202 AND observation_id = '{W1}'", 'aggregate_state')} = 'NOT_RETURNED_BY_SOURCE'"),
    ("FX10", "wb_ended_no_synthetic_zero_membership",
     f"{n_row(WB_H, f"promotion_id = 202 AND observation_id = '{W1}'", 'seller_participation_state')} = 'UNKNOWN'"),
    # 11. NULL агрегатов ≠ 0.
    ("FX11", "null_counters_stay_null",
     f"{n_row(N_H, f"marketplace = 'WB' AND source_promotion_id = '202' AND observation_id = '{W1}'", 'source_in_promo_count IS NULL AND source_not_in_promo_count IS NULL')}"),
    ("FX11", "explicit_zero_is_not_participating",
     f"{n_row(N_H, f"marketplace = 'WB' AND source_promotion_id = '204' AND observation_id = '{W1}'", 'seller_participation_state')} = 'NOT_PARTICIPATING'"),
    ("FX11", "ozon_null_flag_is_unknown",
     f"{n_row(OZ_H, f"action_id = 106 AND observation_id = '{O1}'", 'seller_participation_state')} = 'UNKNOWN'"),
    # 12. Два слота, состояние не менялось.
    ("FX12", "unchanged_two_history_rows", f"{cnt(WB_H, 'promotion_id = 201')} = 2"),
    ("FX12", "unchanged_state_identical",
     f"(SELECT COUNT(DISTINCT FORMAT('%t', (lifecycle_status, aggregate_state, in_promo_total, "
     f"sku_membership_observability))) FROM {WB_H} WHERE promotion_id = 201) = 1"),
    ("FX12", "unchanged_current_from_latest",
     f"{n_row(N_C, "marketplace = 'WB' AND source_promotion_id = '201'", 'last_observation_id')} = '{W2}'"),
    ("FX12", "unchanged_current_counts_both",
     f"{n_row(N_C, "marketplace = 'WB' AND source_promotion_id = '201'", 'observations_count')} = 2"),
    # 13. Два слота, состояние изменилось.
    ("FX13", "changed_first_slot_candidate", f"{oz_state(O1, 101, 1002, 'resolved_state')} = 'CANDIDATE'"),
    ("FX13", "changed_second_slot_participating", f"{oz_state(O2, 101, 1002, 'resolved_state')} = 'PARTICIPATING'"),
    ("FX13", "changed_current_is_latest",
     f"{n_row(N_SC, "marketplace = 'OZON' AND source_promotion_id = '101' AND marketplace_product_id = '1002'", 'resolved_state')} = 'PARTICIPATING'"),
    ("FX13", "changed_history_preserved",
     f"{cnt(N_ST, "marketplace = 'OZON' AND source_promotion_id = '101' AND marketplace_product_id = '1002'")} = 2"),
    # 14. Выбор последнего годного снимка и свежесть.
    ("FX14", "error_observation_never_latest_ozon",
     f"(SELECT MAX(marketplace_latest_observation_id) FROM {N_C} WHERE marketplace = 'OZON') = '{O2}'"),
    ("FX14", "error_observation_never_latest_wb",
     f"(SELECT MAX(marketplace_latest_observation_id) FROM {N_C} WHERE marketplace = 'WB') = '{W2}'"),
    ("FX14", "error_rows_excluded_everywhere",
     f"{cnt(N_ST, f"observation_id IN ('{O3_ERR}', '{W3_ERR}')")} + {cnt(N_H, f"observation_id IN ('{O3_ERR}', '{W3_ERR}')")} = 0"),
    ("FX14", "vanished_promotion_not_present",
     f"{n_row(N_C, "marketplace = 'OZON' AND source_promotion_id = '106'", 'present_in_latest_observation')} = FALSE"),
    ("FX14", "vanished_promotion_lifecycle_inferred",
     f"{n_row(N_C, "marketplace = 'OZON' AND source_promotion_id = '106'", 'current_lifecycle_status')} = 'ENDED'"),
    ("FX14", "vanished_promotion_inference_labelled",
     f"{n_row(N_C, "marketplace = 'OZON' AND source_promotion_id = '106'", 'current_lifecycle_evidence_class')} = 'STRONGLY_INFERRED'"),
    ("FX14", "present_promotion_lifecycle_deterministic",
     f"{n_row(N_C, "marketplace = 'OZON' AND source_promotion_id = '107'", 'current_lifecycle_evidence_class')} = 'DERIVED_DETERMINISTIC'"),
    ("FX14", "freshness_exposed",
     f"(SELECT LOGICAL_AND(observation_age_minutes >= 0 AND last_observed_age_minutes >= 0) FROM {N_C})"),
    ("FX14", "vanished_pair_flagged_in_sku_current",
     f"{n_row(N_SC, "marketplace = 'OZON' AND source_promotion_id = '106' AND marketplace_product_id = '1001'", 'present_in_latest_observation')} = FALSE"),
    ("FX14", "reused_observed_at_is_data_time",
     f"{n_row(N_OBS, f"observation_id = '{O2}'", 'observed_at')} = TIMESTAMP '{T_O2}'"),
    ("FX14", "reused_observed_at_basis",
     f"{n_row(N_OBS, f"observation_id = '{W2}'", 'observed_at_basis')} = 'RAW_ROWS'"),
    # 15. Исторический бэкфилл ≠ наблюдённая история.
    ("FX15", "non_slot_observation_excluded",
     f"{cnt(N_OBS, f"observation_id IN ('{O_HIST}', '{W_HIST}')")} = 0"),
    ("FX15", "backfill_promotions_absent",
     f"{cnt(N_C, "source_promotion_id IN ('108', '208')")} + {cnt(N_H, "source_promotion_id IN ('108', '208')")} = 0"),
    ("FX15", "backfill_never_latest",
     f"(SELECT LOGICAL_AND(marketplace_latest_observation_id NOT IN ('{O_HIST}', '{W_HIST}')) FROM {N_C})"),
    ("FX15", "history_class_observed_only",
     f"(SELECT LOGICAL_AND(history_class = 'OBSERVED') FROM {N_H})"),
    # Жизненный цикл.
    ("FXL", "upcoming", f"{n_row(OZ_H, f"action_id = 102 AND observation_id = '{O1}'", 'lifecycle_status')} = 'UPCOMING'"),
    ("FXL", "no_dates_unknown", f"{n_row(OZ_H, f"action_id = 105 AND observation_id = '{O1}'", 'lifecycle_status')} = 'UNKNOWN'"),
    ("FXL", "no_dates_evidence_unknown",
     f"{n_row(OZ_H, f"action_id = 105 AND observation_id = '{O1}'", 'lifecycle_evidence_class')} = 'UNKNOWN'"),
    ("FXL", "end_boundary_inclusive",
     f"{n_row(OZ_H, f"action_id = 107 AND observation_id = '{O1}'", 'lifecycle_status')} = 'ACTIVE'"),
    ("FXL", "after_end_ended",
     f"{n_row(OZ_H, f"action_id = 107 AND observation_id = '{O2}'", 'lifecycle_status')} = 'ENDED'"),
    ("FXL", "msk_date_derivation",
     f"{n_row(WB_H, f"promotion_id = 201 AND observation_id = '{W1}'", 'starts_on_msk')} = DATE '2026-01-01'"),
    ("FXL", "auto_add_next_date",
     f"{n_row(OZ_H, f"action_id = 101 AND observation_id = '{O1}'", 'next_auto_add_at')} = TIMESTAMP '{AUTO_ADD_DATE}'"),
    # UNKNOWN и NOT_ELIGIBLE: «не знаем» ≠ «нет».
    ("FXU", "incomplete_enumeration_flagged",
     f"{n_row(OZ_H, f"action_id = 102 AND observation_id = '{O1}'", 'sku_membership_observability')} = 'ENUMERATION_INCOMPLETE'"),
    ("FXU", "incomplete_enumeration_absent_is_unknown", f"{oz_state(O1, 102, 1003, 'resolved_state')} = 'UNKNOWN'"),
    ("FXU", "incomplete_enumeration_flag_null", f"{oz_state(O1, 102, 1003, 'in_participants_list')} IS NULL"),
    ("FXU", "incomplete_enumeration_direct_still_direct", f"{oz_state(O1, 102, 1001, 'resolved_state')} = 'PARTICIPATING'"),
    ("FXU", "complete_enumeration_absent_is_not_eligible", f"{oz_state(O1, 103, 1002, 'resolved_state')} = 'NOT_ELIGIBLE'"),
    ("FXU", "not_eligible_is_derived",
     f"{oz_state(O1, 103, 1002, 'resolved_state_evidence_class')} = 'DERIVED_DETERMINISTIC'"),
    ("FXU", "no_auto_add_dates_not_applicable", f"{oz_state(O1, 103, 1002, 'auto_add_state')} = 'NOT_APPLICABLE'"),
    ("FXU", "no_auto_add_dates_flag_null", f"{oz_state(O1, 103, 1002, 'in_auto_add_scheduled_list')} IS NULL"),
    ("FXU", "auto_add_eligible_only", f"{oz_state(O1, 101, 1007, 'resolved_state')} = 'AUTO_ADD_ELIGIBLE'"),
    ("FXU", "not_eligible_only_where_enumeration_complete",
     f"(SELECT COUNT(*) FROM {OZ_ST} s JOIN {OZ_H} h USING (observation_id, action_id) "
     f"WHERE s.resolved_state = 'NOT_ELIGIBLE' AND h.sku_membership_observability != 'OBSERVED') = 0"),
    # WB, обычные акции со списком номенклатур.
    ("FXW", "wb_regular_participating",
     f"{n_row(WB_ST, f"observation_id = '{W1}' AND promotion_id = 203 AND nm_id = 5001", 'resolved_state')} = 'PARTICIPATING'"),
    ("FXW", "wb_regular_candidate",
     f"{n_row(WB_ST, f"observation_id = '{W1}' AND promotion_id = 203 AND nm_id = 5002", 'resolved_state')} = 'CANDIDATE'"),
    ("FXW", "wb_regular_unmapped_kept",
     f"{n_row(WB_ST, f"observation_id = '{W1}' AND promotion_id = 203 AND nm_id = 5003", 'mapping_status')} = 'UNMAPPED'"),
    ("FXW", "wb_regular_auto_add_flags_null",
     f"{n_row(WB_ST, f"observation_id = '{W1}' AND promotion_id = 203 AND nm_id = 5001", 'in_auto_add_scheduled_list IS NULL')}"),
    ("FXW", "wb_empty_list_observed_empty",
     f"{n_row(WB_H, f"promotion_id = 204 AND observation_id = '{W1}'", 'sku_membership_observability')} = 'OBSERVED_EMPTY'"),
    ("FXW", "wb_empty_list_no_rows", f"{cnt(WB_ST, 'promotion_id = 204')} = 0"),
    ("FXW", "wb_unrecognised_status_unknown",
     f"{n_row(WB_H, f"promotion_id = 205 AND observation_id = '{W1}'", 'sku_membership_observability')} = 'UNKNOWN'"),
    ("FXW", "wb_ranging_preserved", f"{cnt(WB_R, f"promotion_id = 201 AND observation_id = '{W1}'")} = 2"),
    ("FXW", "wb_no_catalog_cartesian", f"{cnt(WB_ST)} = 6"),
    # Наблюдаемость.
    ("FXO", "linkage_counts_latest",
     f"{n_row(N_OBSV, "marketplace = 'OZON' AND capability = 'MARKETING_ACTION_LINKAGE'", COUNTS4)} = '5/2/1/2'"),
    ("FXO", "wb_membership_counts_latest",
     f"{n_row(N_OBSV, "marketplace = 'WB' AND capability = 'SKU_MEMBERSHIP'", COUNTS4)} = '6/2/3/1'"),
    ("FXO", "wb_auto_add_not_provided",
     f"{n_row(N_OBSV, "marketplace = 'WB' AND capability = 'SKU_AUTO_ADD_SCHEDULE'", 'capability_status')} = 'NOT_PROVIDED_BY_SOURCE'"),
    ("FXO", "ozon_mapping_partial",
     f"{n_row(N_OBSV, "marketplace = 'OZON' AND capability = 'SKU_IDENTITY_MAPPING'", 'capability_status')} = 'PARTIALLY_OBSERVED'"),
    # Зерно каждого представления.
    ("FXG", "grain_wb_observations", grain(WB_OBS, "observation_id")),
    ("FXG", "grain_wb_history", grain(WB_H, "FORMAT('%t', (observation_id, promotion_id))")),
    ("FXG", "grain_wb_ranging", grain(WB_R, "FORMAT('%t', (observation_id, promotion_id, tier_ordinal))")),
    ("FXG", "grain_wb_evidence", grain(WB_EV, "FORMAT('%t', (observation_id, evidence_key))")),
    ("FXG", "grain_wb_state", grain(WB_ST, "FORMAT('%t', (observation_id, promotion_id, nm_id))")),
    ("FXG", "grain_ozon_observations", grain(OZ_OBS, "observation_id")),
    ("FXG", "grain_ozon_history", grain(OZ_H, "FORMAT('%t', (observation_id, action_id))")),
    ("FXG", "grain_ozon_evidence", grain(OZ_EV, "FORMAT('%t', (observation_id, evidence_key))")),
    ("FXG", "grain_ozon_state", grain(OZ_ST, "FORMAT('%t', (observation_id, action_id, product_id))")),
    ("FXG", "grain_neutral_observations", grain(N_OBS, "FORMAT('%t', (marketplace, observation_id))")),
    ("FXG", "grain_neutral_history", grain(N_H, "FORMAT('%t', (marketplace, source_promotion_id, observation_id))")),
    ("FXG", "grain_neutral_current", grain(N_C, "FORMAT('%t', (marketplace, source_promotion_id))")),
    ("FXG", "grain_neutral_evidence", grain(N_EV, "FORMAT('%t', (marketplace, observation_id, evidence_key))")),
    ("FXG", "grain_neutral_state",
     grain(N_ST, "FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id, observation_id))")),
    ("FXG", "grain_neutral_sku_current",
     grain(N_SC, "FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id))")),
    ("FXG", "grain_observability", grain(N_OBSV, "FORMAT('%t', (marketplace, capability))")),
    ("FXG", "ozon_state_universe_size", f"{cnt(OZ_ST, f"observation_id = '{O1}'")} = 7 * 9"),
    ("FXG", "history_current_consistency",
     f"(SELECT COUNT(*) FROM {N_SC} c JOIN {N_ST} h ON h.marketplace = c.marketplace "
     f"AND h.source_promotion_id = c.source_promotion_id AND h.marketplace_product_id = c.marketplace_product_id "
     f"AND h.observation_id = c.last_observation_id AND h.resolved_state = c.resolved_state) = {cnt(N_SC)}"),
]


def render_block(assertions: list[tuple[str, str, str]]) -> str:
    """Один оператор: фикстуры и представления, которые нужны этим утверждениям, и сами утверждения."""
    schemas = raw_schemas()
    data = fixtures()
    assertion_sql = "\n  UNION ALL\n".join(
        f"  SELECT '{sid}' AS scenario, '{name}' AS assertion, ({expr}) AS ok" for sid, name, expr in assertions)
    query = (f"SELECT scenario, assertion, IF(ok IS TRUE, 'PASS', 'FAIL') AS status\nFROM (\n{assertion_sql}\n)\n"
             f"ORDER BY scenario, assertion")
    q, used = _replace_refs(query, replace_raw=True)
    vctes, raw_used = view_ctes(used, replace_raw=True)
    raw_used |= {k for k in used if k in RAW_TABLES}
    fctes = [fixture_cte(k, schemas[k], data.get(k, [])) for k in RAW_TABLES if k in raw_used]
    return _merge_with(fctes + vctes, q)


def render_fixture_checks() -> str:
    # Один блок на сценарий: планировщик BigQuery раскрывает CTE при каждой ссылке, и все
    # утверждения в одном операторе дают «query is too complex».
    order: list[str] = []
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for a in ASSERTIONS:
        if a[0] not in groups:
            order.append(a[0])
            groups[a[0]] = []
        groups[a[0]].append(a)
    header = (
        "-- ============================================================================\n"
        "-- PR-PROMO-2 · регрессионные сценарии канонического слоя акций на фикстурах.\n"
        "-- СГЕНЕРИРОВАНО tools/promo_canonical_render.py fixtures --write. НЕ РЕДАКТИРОВАТЬ.\n"
        "-- Тела 16 представлений взяты из Git дословно; RAW и REF_SKU_CHANNEL_MAP заменены\n"
        "-- типизированными фикстурами (схема — DDL PR-PROMO-1). Ни одной таблицы не читается.\n"
        f"-- Сценариев: {len(order)}, утверждений: {len(ASSERTIONS)}. Каждое утверждение — строка\n"
        "-- со status PASS/FAIL; NULL = FAIL.\n"
        "-- ============================================================================\n"
    )
    blocks = [f"\n-- @check {sid}\n{render_block(groups[sid])};\n" for sid in order]
    return header + "".join(blocks)


def split_blocks(text: str) -> list[tuple[str, str]]:
    """[(id, оператор)] из файла контракта `-- @check`: заголовочные комментарии блока отброшены."""
    out = []
    for part in re.split(r"(?m)^-- @check ", text)[1:]:
        cid, _, rest = part.partition("\n")
        lines = rest.splitlines()
        while lines and lines[0].startswith("--"):
            lines.pop(0)
        out.append((cid.strip(), "\n".join(lines).strip().rstrip(";").strip()))
    return out


def run_blocks(blocks: list[tuple[str, str]], project: str, token_command: str | None, token_env: str | None) -> int:
    """Исполнить блоки read-only (tools/lib/bq_readonly). 0 — все PASS, 1 — есть FAIL, 3 — ошибка."""
    import os
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.bq_readonly import BigQueryError, ReadOnlyBigQuery, resolve_token  # noqa: E402

    bq = ReadOnlyBigQuery(project=project, token=resolve_token(token_env, token_command, os.environ),
                          label_purpose="promo-canonical-checks")
    worst = 0
    for cid, sql in blocks:
        try:
            rows = bq.query(sql)
        except BigQueryError as e:
            print(f"{cid}\tERROR\t{str(e)[:300]}")
            worst = 3
            continue
        bad = [r for r in rows if r.get("status") != "PASS"]
        verdict = "EMPTY" if not rows else ("PASS" if not bad else "FAIL")
        print(f"{cid}\t{verdict}\t{len(rows)} rows")
        for r in bad:
            print("   ", {k: v for k, v in r.items()})
        if verdict != "PASS":
            worst = max(worst, 1)
    print(f"queries={bq.queries_issued} bytes_processed={bq.bytes_billed}")
    return worst


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "predeploy":
        text = Path(argv[1]).read_text(encoding="utf-8")
        sys.stdout.write(render_predeploy_file(text) if "-- @check " in text else render_predeploy(text) + ";\n")
        return 0
    if argv and argv[0] == "fixtures":
        out = render_fixture_checks()
        target = _opt(argv, "--out")
        if target:
            Path(target).write_text(out, encoding="utf-8")
        else:
            sys.stdout.write(out)
        return 0
    if len(argv) >= 2 and argv[0] == "run":
        project = _opt(argv, "--project")
        if not project:
            sys.stderr.write("run требует --project\n")
            return 2
        if argv[1] == "fixtures":
            text = render_fixture_checks()
        elif argv[1] == "predeploy" and len(argv) >= 3:
            text = render_predeploy_file(Path(argv[2]).read_text(encoding="utf-8"))
        elif argv[1] == "live" and len(argv) >= 3:
            text = Path(argv[2]).read_text(encoding="utf-8")
        else:
            sys.stderr.write(__doc__)
            return 2
        return run_blocks(split_blocks(text), project, _opt(argv, "--token-command"), _opt(argv, "--token-env"))
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
