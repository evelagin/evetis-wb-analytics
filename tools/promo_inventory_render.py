#!/usr/bin/env python3
"""PR-PROMO-4 — регрессионные сценарии и предразвёртывание слоя запасов и распродажи.

Тот же механизм, что у PR-PROMO-2/3 (tools/promo_canonical_render.py): тела семи представлений
берутся из Git дословно и подставляются в запрос как CTE; каждый внешний объект (таблицы
Control Tower, BOM, план, конфигурация, экономика акций PR-PROMO-3) заменяется типизированной
фикстурой. Схемы внешних объектов — sql/promotions/pr_promo4_external_schemas.json (снято с
production, дрейф ловит live-проверка I20), схемы новых справочников — их DDL.

Каждый сценарий — свой маленький мир данных и свой оператор: так запросы остаются малыми,
а планировщик BigQuery не упирается в «query is too complex». Время в фикстурах — от «сейчас»
(UTC): текущие представления честно зависят от CURRENT_DATE / CURRENT_TIMESTAMP, и фикстура
строится относительно того же момента. Календарные сценарии (февраль високосного и
невисокосного года, 30 и 31 день) берут ближайшие такие месяцы после сегодняшнего дня.

usage:
  python tools/promo_inventory_render.py predeploy <checks.sql>        # в stdout
  python tools/promo_inventory_render.py fixtures [--out <path>]       # вне Git
  python tools/promo_inventory_render.py run fixtures --project <P> --token-command "…"
  python tools/promo_inventory_render.py run predeploy <checks.sql> --project <P> --token-command "…"
  python tools/promo_inventory_render.py run live <checks.sql> --project <P> --token-command "…"
"""
from __future__ import annotations

import calendar
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_render as base  # noqa: E402

ROOT = base.ROOT
PROJECT = base.PROJECT
REFS_DDL = ROOT / "sql" / "promotions" / "pr_promo4_planning_refs.sql"
EXTERNAL_SCHEMAS = ROOT / "sql" / "promotions" / "pr_promo4_external_schemas.json"

OBJECTS: list[tuple[str, str, str]] = [
    ("evetis_mart", "V_INVENTORY_POSITION_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_SKU_SELL_THROUGH_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_SKU_INVENTORY_TARGET_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_SALES_PLAN_MONTHLY_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_INVENTORY_CONTEXT_CURRENT", "sql/current/evetis_mart"),
]
NEW_TABLES: list[tuple[str, str]] = [
    ("evetis_ref", "REF_SALES_PLAN_APPROVAL"),
    ("evetis_ref", "REF_SKU_INVENTORY_TARGET"),
]


def external_schemas() -> dict[tuple[str, str], list[tuple[str, str]]]:
    doc = json.loads(EXTERNAL_SCHEMAS.read_text(encoding="utf-8"))
    return {tuple(k.split(".")): [(c["column_name"], c["data_type"]) for c in cols]
            for k, cols in doc["objects"].items()}


EXTERNALS: list[tuple[str, str]] = sorted(external_schemas())
RAW_TABLES = EXTERNALS + NEW_TABLES


def schemas() -> dict[tuple[str, str], list[tuple[str, str]]]:
    out = external_schemas()
    out.update({k: v for k, v in base.raw_schemas([REFS_DDL, base.RAW_DDL], NEW_TABLES).items() if k in NEW_TABLES})
    return out


def ref(name: str) -> str:
    return f"`{PROJECT}.evetis_mart.{name}`"


# ─────────────────────────────────────────────────────────────── время фикстур
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = NOW.date()
A = TODAY - timedelta(days=1)            # sales_as_of: последний полный день обоих каналов


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


def d(n: int) -> str:
    """TODAY + n дней ISO."""
    return (TODAY + timedelta(days=n)).isoformat()


def next_month_matching(pred) -> date:
    m = date(TODAY.year, TODAY.month, 1)
    for _ in range(120):
        m = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
        if pred(m):
            return m
    raise RuntimeError("no month found")


FEB_NONLEAP = next_month_matching(lambda m: m.month == 2 and not calendar.isleap(m.year))
FEB_LEAP = next_month_matching(lambda m: m.month == 2 and calendar.isleap(m.year))
M30 = next_month_matching(lambda m: calendar.monthrange(m.year, m.month)[1] == 30)
M31 = next_month_matching(lambda m: calendar.monthrange(m.year, m.month)[1] == 31 and m.month != 1)
CUR_MONTH = date(TODAY.year, TODAY.month, 1)
# Цель, горизонт которой проходит оба февраля: 15 марта года позднего из двух.
LEAP_TARGET = date(max(FEB_LEAP.year, FEB_NONLEAP.year), 3, 15)


# ─────────────────────────────────────────────────────────────── строители строк
def pm(sku: str, is_bundle: bool = False) -> dict:
    return dict(internal_sku=sku, canonical_product_name=f"Товар {sku}", product_name_short=f"Товар {sku}",
                is_bundle=is_bundle, product_line="Фикстура")


def chmap(sku: str, marketplace: str, msku: str) -> dict:
    return dict(internal_sku=sku, marketplace=marketplace, marketplace_sku=msku,
                offer_id=msku if marketplace == "OZON" else None,
                vendor_code=f"v-{sku}" if marketplace == "WB" else msku, is_current=True, mapping_status="FIXTURE")


def snap(sku: str, day: int, *, ff=100, wb=10, oz=10, fbs=0, wb_tr=0, oz_tr=0, to_client=0, lost=0, inbound=0,
         inbound_eta=None, anchor_age=1, snap_ts=None, wb_asof=None, oz_asof=None, in_bundles=0,
         status="OK") -> dict:
    """Строка CT_INVENTORY_SNAPSHOT_DAILY за день TODAY+day так, как её пишет sp_ct_refresh_daily."""
    sd = TODAY + timedelta(days=day)
    return dict(
        snapshot_date=sd.isoformat(),
        snapshot_ts=snap_ts or ts(datetime(sd.year, sd.month, sd.day, 4, 40, tzinfo=timezone.utc)
                                  if day < 0 else NOW - timedelta(hours=2)),
        internal_sku=sku, product_name=f"Товар {sku}", product_line="Фикстура",
        ff_snapshot_date=(sd - timedelta(days=anchor_age)).isoformat(),
        ff_operational_snapshot=0, ff_pallet_snapshot=ff, ff_outflow_since_snapshot=0,
        wb_in_transit_units=wb_tr, wb_fbo_live_units=wb, wb_fbo_units_in_bundles=0, wb_lost_claimed_units=lost,
        wb_to_client_units=to_client, wb_stock_as_of=wb_asof or sd.isoformat(),
        ozon_fbo_units=oz, ozon_fbo_units_in_bundles=0, ozon_api_transit_units=0,
        ozon_stock_as_of=oz_asof or sd.isoformat(), fbs_units=fbs, inbound_units=inbound,
        inbound_eta=inbound_eta, ff_total_units=ff, ozon_transit_units=oz_tr,
        sellable_units=ff + wb + oz + fbs + wb_tr + oz_tr, marketplace_units=wb + oz + fbs,
        assembled_bundle_units_on_marketplaces=in_bundles, status_code=status, status_reason="фикстура",
        units_per_day_30d=1.0, units_per_day_7d=1.0)


def history(sku: str, days: int, **kw) -> list[dict]:
    """Срезы за дни TODAY−days+1 … TODAY (последний — сегодняшний)."""
    return [snap(sku, -i, **kw) for i in range(days - 1, -1, -1)]


def phys(sku: str, first: int, last: int, per_day: int, marketplace="WB") -> list[dict]:
    """V_CT_PHYSICAL_DAILY: per_day заказанных единиц в каждый день TODAY+first … TODAY+last."""
    return [dict(d=d(k), marketplace=marketplace, internal_sku=sku, units_ordered=per_day,
                 units_ordered_solo=per_day, units_ordered_via_bundle=0, units_sold=per_day,
                 units_sold_solo=per_day, units_sold_via_bundle=0, units_cancelled=0)
            for k in range(first, last + 1) if per_day]


def ct_actual_marks(wb_last=-1, oz_last=0, refreshed_hours_ago=3) -> list[dict]:
    """CT_ACTUAL_DAILY: только то, из чего выводится sales_as_of (последние дни каналов, пересборка)."""
    at = ts(NOW - timedelta(hours=refreshed_hours_ago))
    return [dict(d=d(wb_last), marketplace="WB", internal_sku="EVT-FX-MARK", sales_mode="SOLO", refreshed_at=at),
            dict(d=d(oz_last), marketplace="OZON", internal_sku="EVT-FX-MARK", sales_mode="SOLO", refreshed_at=at)]


def batch(sku: str, bid: str, expiry: str, source="INFERENCE_IMPORT_MINUS_70D", conf="MEDIUM", inbound=False) -> dict:
    return dict(internal_sku=sku, batch_id=bid, expiry_date=expiry, expiry_source=source, expiry_confidence=conf,
                shelf_life_months=24, is_inbound=inbound, created_at=ts(NOW))


def config_rows() -> dict:
    return {
        ("evetis_ref", "CT_CONFIG"): [dict(config_key="refresh_sla_hours", config_value="26")],
        ("evetis_ops", "OPS_CONFIG"): [dict(config_key="c1_expiry_margin_days", config_value="30",
                                            note="C1 OWNER_CONVENTION fixture")],
    }


def world(**tables) -> dict:
    """Мир сценария: конфигурация + отметки продаж по умолчанию + переданные таблицы."""
    data = config_rows()
    data[("evetis_ref", "CT_ACTUAL_DAILY")] = ct_actual_marks()
    for key, rows in tables.items():
        ds, name = key.split("__", 1)
        data[(ds, name)] = rows
    return data


def one(view: str, where: str, col: str) -> str:
    return f"(SELECT ANY_VALUE({col}) FROM {ref(view)} WHERE {where})"


def cnt(view: str, where: str = "TRUE") -> str:
    return f"(SELECT COUNT(*) FROM {ref(view)} WHERE {where})"


def near(expr: str, value: float, tol: float = 1e-9) -> str:
    return f"(ABS(({expr}) - ({value})) <= {tol})"


ST, TG, TR, PL, BC, CX, PH = ("V_SKU_SELL_THROUGH_CURRENT", "V_SKU_INVENTORY_TARGET_CURRENT",
                              "V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT", "V_SALES_PLAN_MONTHLY_CURRENT",
                              "V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT", "V_PROMO_INVENTORY_CONTEXT_CURRENT",
                              "V_INVENTORY_POSITION_HISTORY")


def sku(s: str) -> str:
    return f"internal_sku = '{s}'"


def tgt(s: str, t: str) -> str:
    return f"internal_sku = '{s}' AND target_type = '{t}'"


# ─────────────────────────────────────────────────────────────── сценарии
SCENARIOS: list[tuple[str, str, dict, list[tuple[str, str]]]] = []


def scenario(sid: str, title: str, data: dict, assertions: list[tuple[str, str]]):
    SCENARIOS.append((sid, title, data, assertions))


def mapped(*skus: str) -> dict:
    return dict(evetis_ref__REF_PRODUCT_MASTER=[pm(s) for s in skus],
                evetis_ref__REF_SKU_CHANNEL_MAP=[chmap(s, "WB", f"9{i}") for i, s in enumerate(skus)]
                + [chmap(s, "OZON", f"8{i}") for i, s in enumerate(skus)])


# 1 · Нормальный SKU: запас есть, продажи ровные 2/день 120 дней, наличие видно 100 дней.
N = "EVT-FX-NORMAL"
scenario("FX01", "normal SKU, steady sales, full availability", world(
    **mapped(N),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(N, 100, ff=200, wb=50, oz=40, wb_tr=5, oz_tr=5),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(N, -120, -1, 2),
    evetis_ref__CT_EXPIRY_BATCH=[batch(N, "B-N", d(400))]), [
    ("velocity_30d_is_2", near(one(ST, sku(N), "units_per_day_30d"), 2.0)),
    ("velocity_90d_is_2", near(one(ST, sku(N), "units_per_day_90d"), 2.0)),
    ("quality_all_windows_normal", f"{cnt(ST, sku(N) + ' AND velocity_quality_7d = "NORMAL" AND velocity_quality_14d = "NORMAL" AND velocity_quality_30d = "NORMAL" AND velocity_quality_60d = "NORMAL" AND velocity_quality_90d = "NORMAL"')} = 1"),
    ("position_is_300", f"{one(ST, sku(N), 'inventory_position_units')} = 300"),
    ("cover_30d_is_150_days", near(one(ST, sku(N), "cover_days_30d"), 150.0)),
    ("cover_status_computed", f"{one(ST, sku(N), 'cover_status_30d')} = 'COMPUTED'"),
    ("inventory_fresh", f"{one(ST, sku(N), 'inventory_freshness_status')} = 'FRESH'"),
    ("sales_as_of_yesterday", f"{one(ST, sku(N), 'sales_as_of')} = DATE '{A.isoformat()}'"),
    ("availability_observed_90_of_90", f"{one(ST, sku(N), 'availability_observed_days_90d')} = 90"),
])

# 2–4 · Нули: запас 0 при продажах, запас без продаж, ни запаса ни продаж.
Z1, Z2, Z3 = "EVT-FX-ZEROINV", "EVT-FX-NOVEL", "EVT-FX-ZEROZERO"
scenario("FX02", "zero inventory, positive velocity → cover 0", world(
    **mapped(Z1),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(Z1, 30, ff=0, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(Z1, -200, -1, 1)), [
    ("cover_is_zero_days", f"{one(ST, sku(Z1), 'cover_days_30d')} = 0"),
    ("cover_status_zero_inventory", f"{one(ST, sku(Z1), 'cover_status_30d')} = 'ZERO_INVENTORY'"),
    ("marketplace_cover_zero_inventory", f"{one(ST, sku(Z1), 'marketplace_cover_status_30d')} = 'ZERO_INVENTORY'"),
])
scenario("FX03", "positive inventory, zero velocity → cover NULL, NO_VELOCITY", world(
    **mapped(Z2),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(Z2, 100, ff=80, wb=10, oz=10),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(Z2, -300, -200, 1)), [
    ("cover_null_not_zero", f"{one(ST, sku(Z2), 'cover_days_30d')} IS NULL"),
    ("cover_status_no_velocity", f"{one(ST, sku(Z2), 'cover_status_30d')} = 'NO_VELOCITY'"),
    ("quality_no_sales_with_stock", f"{one(ST, sku(Z2), 'velocity_quality_30d')} = 'NO_SALES_WITH_STOCK'"),
    ("velocity_is_zero", f"{one(ST, sku(Z2), 'units_per_day_30d')} = 0"),
])
scenario("FX04", "zero inventory, zero velocity → separate status", world(
    **mapped(Z3),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(Z3, 30, ff=0, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(Z3, -300, -250, 1)), [
    ("cover_null", f"{one(ST, sku(Z3), 'cover_days_30d')} IS NULL"),
    ("cover_status_no_inventory_no_velocity", f"{one(ST, sku(Z3), 'cover_status_30d')} = 'NO_INVENTORY_NO_VELOCITY'"),
    ("quality_stockout_not_no_demand", f"{one(ST, sku(Z3), 'velocity_quality_30d')} = 'STOCKOUT_CONSTRAINED'"),
])

# 5 · Факты продаж недоступны: скорость и покрытие неизвестны, а не нулевые.
V = "EVT-FX-NOSALES"
w5 = world(**mapped(V), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(V, 10, ff=50),
           wb_mart__V_CT_PHYSICAL_DAILY=phys(V, -20, -1, 1))
w5[("evetis_ref", "CT_ACTUAL_DAILY")] = []
scenario("FX05", "missing velocity → UNKNOWN / VELOCITY_UNAVAILABLE", w5, [
    ("sales_as_of_null", f"{one(ST, sku(V), 'sales_as_of')} IS NULL"),
    ("velocity_null", f"{one(ST, sku(V), 'units_per_day_30d')} IS NULL"),
    ("quality_unknown", f"{one(ST, sku(V), 'velocity_quality_30d')} = 'UNKNOWN'"),
    ("cover_velocity_unavailable", f"{one(ST, sku(V), 'cover_status_30d')} = 'VELOCITY_UNAVAILABLE'"),
])

# 6–7 · Ускорение и обвал: окна не усредняются, отношение видно.
F1, F2 = "EVT-FX-ACCEL", "EVT-FX-COLLAPSE"
scenario("FX06", "7d acceleration vs 90d baseline", world(
    **mapped(F1), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(F1, 100, ff=900),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(F1, -120, -8, 2) + phys(F1, -7, -1, 10)), [
    ("v7_is_10", near(one(ST, sku(F1), "units_per_day_7d"), 10.0)),
    ("v90_blend", near(one(ST, sku(F1), "units_per_day_90d"), (83 * 2 + 7 * 10) / 90)),
    ("ratio_7_to_90", near(one(ST, sku(F1), "velocity_ratio_7d_to_90d"), 10 / ((83 * 2 + 7 * 10) / 90), 1e-9)),
])
scenario("FX07", "recent sales collapse with stock", world(
    **mapped(F2), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(F2, 100, ff=900),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(F2, -120, -8, 5)), [
    ("v7_is_zero", f"{one(ST, sku(F2), 'units_per_day_7d')} = 0"),
    ("quality_7d_no_sales_with_stock", f"{one(ST, sku(F2), 'velocity_quality_7d')} = 'NO_SALES_WITH_STOCK'"),
    ("ratio_7_to_90_zero", f"{one(ST, sku(F2), 'velocity_ratio_7d_to_90d')} = 0"),
    ("v30_not_averaged_away", near(one(ST, sku(F2), "units_per_day_30d"), 23 * 5 / 30)),
])

# 8 · Ограничено наличием: 3 дня нуля на WB в последние 14 дней.
S8 = "EVT-FX-STOCKOUT"
hist8 = history(S8, 100, ff=500)
for r in hist8:
    if r["snapshot_date"] in {d(-3), d(-4), d(-5)}:
        r["wb_fbo_live_units"], r["marketplace_units"] = 0, r["ozon_fbo_units"]
        r["sellable_units"] = r["ff_total_units"] + r["ozon_fbo_units"]
scenario("FX08", "stockout-constrained window is not normal demand", world(
    **mapped(S8), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=hist8,
    wb_mart__V_CT_PHYSICAL_DAILY=phys(S8, -120, -1, 3)), [
    ("stockout_days_7d_is_3", f"{one(ST, sku(S8), 'stockout_days_7d')} = 3"),
    ("quality_7d_constrained", f"{one(ST, sku(S8), 'velocity_quality_7d')} = 'STOCKOUT_CONSTRAINED'"),
    ("quality_90d_constrained", f"{one(ST, sku(S8), 'velocity_quality_90d')} = 'STOCKOUT_CONSTRAINED'"),
    ("wb_channel_attributed", f"{one(ST, sku(S8), 'wb_stockout_days_30d')} = 3 AND {one(ST, sku(S8), 'ozon_stockout_days_30d')} = 0"),
    ("velocity_not_corrected", near(one(ST, sku(S8), "units_per_day_7d"), 3.0)),
])

# 9 · Недостаточная история: первый заказ 10 дней назад.
S9 = "EVT-FX-NEW"
scenario("FX09", "insufficient sales history", world(
    **mapped(S9), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(S9, 100, ff=100),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(S9, -10, -1, 4)), [
    ("q30_insufficient", f"{one(ST, sku(S9), 'velocity_quality_30d')} = 'INSUFFICIENT_HISTORY'"),
    ("q7_normal", f"{one(ST, sku(S9), 'velocity_quality_7d')} = 'NORMAL'"),
    ("first_order_date", f"{one(ST, sku(S9), 'first_order_date')} = DATE '{d(-10)}'"),
])

# 10 · Немаппированный SKU остаётся виден; сценарий акции без SKU — NO_CANONICAL_SKU.
S10 = "EVT-FX-ORPHAN"


def econ(mp: str, key: str, sku_: str | None, *, obs="OBS-1", scen="PROMO", base_c=100.0, promo_c=60.0,
         upl=66.6667, present=True, product="P1") -> dict:
    return dict(marketplace=mp, scenario_entity_key=key, source_promotion_id="PR-1", promotion_type="T",
                marketplace_product_id=product, internal_sku=sku_, mapping_status="FIXTURE", scenario_type=scen,
                scenario_key=key, price_interpretation="FIXTURE", price_binding_evidence="FIXTURE",
                economics_status="COMPUTABLE", baseline_price_rub=1000, scenario_price_rub=900,
                baseline_contribution_expected_rub=base_c, promo_contribution_expected_rub=promo_c,
                delta_contribution_expected_rub=promo_c - base_c, baseline_margin_expected_pct=10,
                promo_margin_expected_pct=6.6667, break_even_price_expected_rub=700, downside_case="FIXTURE",
                promo_contribution_downside_rub=promo_c - 20, promo_downside_negative=promo_c - 20 < 0,
                uplift_status="COMPUTED", required_sales_uplift_pct=upl, present_in_latest_slot=present,
                last_economics_slot="fixture", last_observation_id=obs, last_basis_captured_at=ts(NOW - timedelta(hours=1)))


def obs(mp: str, oid: str, hours_ago: float) -> dict:
    return dict(marketplace=mp, observation_id=oid, observed_at=ts(NOW - timedelta(hours=hours_ago)))


scenario("FX10", "unmapped inventory and unmapped promo SKU stay visible", world(
    evetis_ref__REF_PRODUCT_MASTER=[pm(N)], evetis_ref__REF_SKU_CHANNEL_MAP=[chmap(N, "WB", "91")],
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(S10, 5, ff=10) + history(N, 5, ff=10),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(N, -40, -1, 1),
    evetis_mart__V_PROMO_ECONOMICS_SCENARIO_CURRENT=[econ("OZON", "K-NOSKU", None)],
    evetis_mart__V_PROMO_OBSERVATION_HISTORY=[obs("OZON", "OBS-1", 1)]), [
    ("orphan_row_present", f"{cnt(PH, sku(S10))} = 5"),
    ("orphan_mapping_status", f"{one(PH, sku(S10), 'mapping_status')} = 'NOT_IN_PRODUCT_MASTER'"),
    ("orphan_in_current", f"{cnt(ST, sku(S10))} = 1"),
    ("promo_without_sku_kept", f"{cnt(CX, 'scenario_entity_key = "K-NOSKU"')} = 1"),
    ("promo_without_sku_status", f"{one(CX, 'scenario_entity_key = "K-NOSKU"', 'inventory_context_status')} = 'NO_CANONICAL_SKU'"),
])

# 11 · Устаревший запас: срез ФФ 20 дней, цели не считаются как текущие.
S11 = "EVT-FX-STALE"
scenario("FX11", "stale inventory → INVENTORY_STALE, no required velocity", world(
    **mapped(S11), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(S11, 30, ff=300, anchor_age=20),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(S11, -120, -1, 2),
    evetis_ref__CT_EXPIRY_BATCH=[batch(S11, "B-S", d(200))]), [
    ("freshness_stale", f"{one(ST, sku(S11), 'inventory_freshness_status')} = 'STALE'"),
    ("reason_names_ff", f"STARTS_WITH({one(ST, sku(S11), 'inventory_freshness_reason')}, 'FF_STOCK=STALE')"),
    ("target_inventory_stale", f"{one(TG, tgt(S11, 'EXPIRY_SELL_BY'), 'target_status')} = 'INVENTORY_STALE'"),
    ("no_required_units", f"{one(TG, tgt(S11, 'EXPIRY_SELL_BY'), 'required_units_per_day')} IS NULL"),
    ("no_uplift", f"{one(TG, tgt(S11, 'EXPIRY_SELL_BY'), 'required_inventory_uplift_pct')} IS NULL"),
    ("facts_still_visible", f"{one(ST, sku(S11), 'inventory_position_units')} = 300 + 10 + 10"),
])

# 12–14 · Состав позиции: без двойного счёта; резерв и утрата исключены; в пути включено.
S12 = "EVT-FX-COMPOSE"
scenario("FX12", "overlapping sources / reserved / in transit", world(
    **mapped(S12), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(
        S12, 3, ff=100, wb=20, oz=30, fbs=5, wb_tr=7, oz_tr=3, to_client=11, lost=13, inbound=500,
        in_bundles=9),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(S12, -40, -1, 1)), [
    ("position_identity", f"{one(ST, sku(S12), 'inventory_position_units')} = 100 + 20 + 30 + 5 + 7 + 3"),
    ("position_equals_parts", f"{one(ST, sku(S12), 'warehouse_ff_units + marketplace_available_units + in_transit_units')} = {one(ST, sku(S12), 'inventory_position_units')}"),
    ("marketplace_available", f"{one(ST, sku(S12), 'marketplace_available_units')} = 55"),
    ("bundle_units_subset", f"{one(ST, sku(S12), 'marketplace_units_inside_bundle_cards')} <= {one(ST, sku(S12), 'marketplace_available_units')}"),
    ("reserved_excluded", f"{one(ST, sku(S12), 'reserved_in_delivery_units')} = 11"),
    ("claimed_excluded", f"{one(ST, sku(S12), 'unavailable_claimed_units')} = 13"),
    ("inbound_excluded", f"{one(ST, sku(S12), 'inbound_not_received_units')} = 500"),
    ("in_transit_included", f"{one(ST, sku(S12), 'in_transit_units')} = 10"),
    ("scopes_explicit", f"{one(ST, sku(S12), 'CONCAT(warehouse_ff_scope, "|", marketplace_available_scope, "|", inventory_position_scope)')} = 'WAREHOUSE_SHARED_POOL|CHANNEL_SPECIFIC|GLOBAL_PHYSICAL'"),
])

# 15–18 · Срок годности: явный, нет данных, две партии, уже прошёл.
E1, E2, E3, E4 = "EVT-FX-HARD", "EVT-FX-NOEXP", "EVT-FX-TWOLOTS", "EVT-FX-EXPIRED"
scenario("FX15", "expiry evidence hierarchy and arithmetic", world(
    **mapped(E1, E2, E3, E4),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(E1, 3, ff=1000) + history(E2, 3) + history(E3, 3)
    + history(E4, 3, ff=40),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(E1, -60, -1, 5) + phys(E2, -60, -1, 1) + phys(E3, -60, -1, 1)
    + phys(E4, -60, -1, 1),
    evetis_ref__CT_EXPIRY_BATCH=[batch(E1, "B-H", d(100), "OWNER_FACT_HARD", "HIGH"),
                                 batch(E3, "B-3a", d(200)), batch(E3, "B-3b", d(300)),
                                 batch(E4, "B-4", d(-5))]), [
    ("explicit_owner_expiry", f"{one(ST, sku(E1), 'expiry_evidence_status')} = 'EXPLICIT_EXPIRY_OWNER_FACT'"),
    ("days_to_expiry_100", f"{one(ST, sku(E1), 'days_to_expiry')} = 100"),
    ("sell_by_is_expiry_minus_30", f"{one(ST, sku(E1), 'sell_by_date')} = DATE '{d(70)}'"),
    ("unknown_expiry", f"{one(ST, sku(E2), 'expiry_evidence_status')} = 'EXPIRY_UNAVAILABLE'"),
    ("unknown_expiry_no_target", f"{one(TG, tgt(E2, 'EXPIRY_DATE'), 'target_status')} = 'NO_TARGET'"),
    ("two_lots_flagged", f"{one(ST, sku(E3), 'expiry_lot_status')} = 'MULTIPLE_LOTS_UNITS_NOT_ALLOCATED'"),
    ("two_lots_earliest", f"{one(ST, sku(E3), 'earliest_expiry_date')} = DATE '{d(200)}'"),
    ("two_lots_not_computed", f"{one(TG, tgt(E3, 'EXPIRY_DATE'), 'target_status')} = 'EXPIRY_LOT_ALLOCATION_UNKNOWN'"),
    ("expired_flag", f"{one(ST, sku(E4), 'expiry_passed')} IS TRUE"),
    ("expired_target_invalid", f"{one(TG, tgt(E4, 'EXPIRY_DATE'), 'target_status')} = 'TARGET_DATE_NOT_IN_FUTURE'"),
    ("inferred_evidence_label", f"{one(ST, sku(E4), 'expiry_evidence_status')} = 'MFG_DATE_INFERRED_PLUS_SHELF_LIFE'"),
])

# 19–25 · Требуемая скорость: цели 0 и > 0, будущая и прошедшая дата, хватает / не хватает, ноль скорости.
T1, T2, T3 = "EVT-FX-HAND", "EVT-FX-SLOW", "EVT-FX-OWNER"


def owner_target(tid: str, s: str, ttype: str, day: int, ending: int, status="ACTIVE", recorded_ago=1) -> dict:
    return dict(target_id=tid, internal_sku=s, target_type=ttype, target_date=d(day), target_ending_units=ending,
                target_status=status, approved_by="owner", approved_at=ts(NOW - timedelta(days=2)),
                approval_reference="fixture", recorded_at=ts(NOW - timedelta(hours=recorded_ago)), recorded_by="owner")


scenario("FX19", "required sell-through to targets", world(
    **mapped(T1, T2, T3, N, Z2),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(T1, 3, ff=1000, wb=0, oz=0) + history(T2, 3, ff=500, wb=0, oz=0)
    + history(T3, 3, ff=250, wb=0, oz=0) + history(N, 3, ff=300, wb=0, oz=0) + history(Z2, 3, ff=100, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(T1, -120, -1, 5) + phys(T2, -120, -1, 1) + phys(T3, -120, -1, 2)
    + phys(N, -120, -1, 2) + phys(Z2, -300, -200, 1),
    evetis_ref__CT_EXPIRY_BATCH=[batch(T1, "B-T1", d(100), "OWNER_FACT_HARD", "HIGH"), batch(N, "B-N", d(400)),
                                 batch(Z2, "B-Z2", d(130))],
    evetis_ref__REF_SKU_INVENTORY_TARGET=[
        owner_target("OT-1", T3, "MANUAL_DATE", 50, 50),
        owner_target("OT-2", T3, "SEASON_END", -1, 0),
        owner_target("OT-3", T3, "TARGET_COVER", 40, 10),
        owner_target("OT-4", T2, "MANUAL_DATE", 30, 600),
        owner_target("OT-5", T2, "MANUAL_DATE", 60, 100, status="CANCELLED", recorded_ago=1),
        owner_target("OT-5", T2, "MANUAL_DATE", 60, 100, status="ACTIVE", recorded_ago=5)]), [
    # T1: позиция 1000, скорость 5/день, срок через 100 дней → 10/день, +100 %; sell-by через 70 дней.
    ("expiry_required_units", f"{one(TG, tgt(T1, 'EXPIRY_DATE'), 'required_units_to_sell')} = 1000"),
    ("expiry_required_per_day", near(one(TG, tgt(T1, "EXPIRY_DATE"), "required_units_per_day"), 10.0)),
    ("expiry_uplift_plus_100", near(one(TG, tgt(T1, "EXPIRY_DATE"), "required_inventory_uplift_pct"), 100.0, 1e-9)),
    ("sell_by_uplift", near(one(TG, tgt(T1, "EXPIRY_SELL_BY"), "required_inventory_uplift_pct"),
                            (1000 / 70 / 5 - 1) * 100, 1e-9)),
    ("expiry_target_not_operational", f"{one(TG, tgt(T1, 'EXPIRY_DATE'), 'is_operational_target')} IS FALSE"),
    ("sell_by_operational_with_provenance", f"{one(TG, tgt(T1, 'EXPIRY_SELL_BY'), 'is_operational_target')} IS TRUE AND STRPOS({one(TG, tgt(T1, 'EXPIRY_SELL_BY'), 'target_provenance')}, 'c1_expiry_margin_days') > 0"),
    ("gap_units_per_day", near(one(TG, tgt(T1, "EXPIRY_DATE"), "velocity_gap_units_per_day"), 5.0)),
    # N: 300 / 400 = 0,75/день при 2/день → −62,5 %: текущей хватает, не обнулено.
    ("sufficient_negative_uplift", near(one(TG, tgt(N, "EXPIRY_DATE"), "required_inventory_uplift_pct"), (0.75 / 2 - 1) * 100, 1e-9)),
    ("run_rate_ending_zero", f"{one(TG, tgt(N, 'EXPIRY_DATE'), 'run_rate_ending_units')} = 0"),
    # Z2: скорость 0 → единицы в день есть, процента нет.
    ("zero_velocity_status", f"{one(TG, tgt(Z2, 'EXPIRY_DATE'), 'target_status')} = 'CURRENT_VELOCITY_ZERO'"),
    ("zero_velocity_units_per_day", near(one(TG, tgt(Z2, "EXPIRY_DATE"), "required_units_per_day"), 100 / 130)),
    ("zero_velocity_no_percent", f"{one(TG, tgt(Z2, 'EXPIRY_DATE'), 'required_inventory_uplift_pct')} IS NULL"),
    # T3: цель владельца — остаток 50 через 50 дней: (250 − 50) / 50 = 4/день при 2/день → +100 %.
    ("owner_target_ending_above_zero", near(one(TG, "target_key = 'MANUAL_DATE|OT-1'", "required_units_per_day"), 4.0)),
    ("owner_target_uplift", near(one(TG, "target_key = 'MANUAL_DATE|OT-1'", "required_inventory_uplift_pct"), 100.0, 1e-9)),
    ("owner_target_provenance", f"STRPOS({one(TG, "target_key = 'MANUAL_DATE|OT-1'", 'target_provenance')}, 'REF_SKU_INVENTORY_TARGET OT-1') > 0"),
    ("owner_past_date_invalid", f"{one(TG, "target_key = 'SEASON_END|OT-2'", 'target_status')} = 'TARGET_DATE_NOT_IN_FUTURE'"),
    ("owner_unknown_type_invalid", f"{one(TG, "target_key = 'TARGET_COVER|OT-3'", 'target_status')} = 'INVALID_TARGET'"),
    # T2: цель 600 при позиции 500 → ускорение не нужно, uplift 0.
    ("no_acceleration_required", f"{one(TG, "target_key = 'MANUAL_DATE|OT-4'", 'target_status')} = 'NO_ACCELERATION_REQUIRED'"),
    ("no_acceleration_uplift_zero", f"{one(TG, "target_key = 'MANUAL_DATE|OT-4'", 'required_inventory_uplift_pct')} = 0"),
    ("cancelled_latest_row_wins", f"{cnt(TG, "target_key = 'MANUAL_DATE|OT-5'")} = 0"),
    ("no_invented_cover_target", f"{cnt(TG, "target_type NOT IN ('EXPIRY_DATE', 'EXPIRY_SELL_BY', 'SEASON_END', 'MANUAL_DATE', 'TARGET_COVER')")} = 0"),
])

# Помесячная раскладка цели: настоящие дни месяцев, оба февраля, неполные месяцы.
T4 = "EVT-FX-LONG"
days_to_leap = (LEAP_TARGET - TODAY).days
scenario("FX37", "calendar-month decomposition of a target (Feb leap / non-leap, 30/31, partial)", world(
    **mapped(T4),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(T4, 3, ff=days_to_leap * 3, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(T4, -120, -1, 1),
    evetis_ref__REF_SKU_INVENTORY_TARGET=[owner_target("OT-L", T4, "SEASON_END", days_to_leap, 0)]), [
    ("required_per_day_is_3", near(one(TG, tgt(T4, "SEASON_END"), "required_units_per_day"), 3.0)),
    ("period_days_sum_to_horizon", f"(SELECT SUM(period_days) FROM {ref(TR)} WHERE {sku(T4)}) = {days_to_leap}"),
    ("feb_non_leap_28", f"{one(TR, sku(T4) + f" AND month = DATE '{FEB_NONLEAP.isoformat()}'", 'days_in_month')} = 28"),
    ("feb_non_leap_units", near(one(TR, sku(T4) + f" AND month = DATE '{FEB_NONLEAP.isoformat()}'", "sales_units"), 84.0)),
    ("feb_leap_29", f"{one(TR, sku(T4) + f" AND month = DATE '{FEB_LEAP.isoformat()}'", 'days_in_month')} = 29"),
    ("feb_leap_units", near(one(TR, sku(T4) + f" AND month = DATE '{FEB_LEAP.isoformat()}'", "sales_units"), 87.0)),
    ("month_30_days", f"{one(TR, sku(T4) + f" AND month = DATE '{M30.isoformat()}'", 'period_days')} = 30"),
    ("month_31_days", f"{one(TR, sku(T4) + f" AND month = DATE '{M31.isoformat()}'", 'period_days')} = 31"),
    ("first_month_partial", f"{one(TR, sku(T4) + f" AND month = DATE '{CUR_MONTH.isoformat()}'", 'is_partial_month')} IS {'TRUE' if TODAY.day > 1 else 'FALSE'}"),
    ("last_month_partial_14_days", f"{one(TR, sku(T4) + f" AND month = DATE '{date(LEAP_TARGET.year, 3, 1).isoformat()}'", 'period_days')} = 14"),
    ("closing_reaches_target", near(one(TR, sku(T4) + f" AND month = DATE '{date(LEAP_TARGET.year, 3, 1).isoformat()}'", "closing_units_unconstrained"), 0.0, 1e-6)),
    ("replenishment_not_modelled", f"{cnt(TR, sku(T4) + ' AND (replenishment_status != "REPLENISHMENT_NOT_MODELLED" OR replenishment_units IS NOT NULL)')} = 0"),
    ("no_30_day_month_hack", f"{cnt(TR, sku(T4) + ' AND days_in_month = 30 AND EXTRACT(MONTH FROM month) IN (1, 3, 5, 7, 8, 10, 12)')} = 0"),
])

# 26–31 · Наборы: BOM есть / нет, qty > 1, ограничивающий компонент, общие компоненты, одиночные продажи.
C1, C2, C3 = "EVT-FX-C1", "EVT-FX-C2", "EVT-FX-C3"
BX, BY, BZ, BN = "EVT-SET-FX-X", "EVT-SET-FX-Y", "EVT-SET-FX-Z", "EVT-SET-FX-NOBOM"


def bomrow(card: str, comp: str, qty: int) -> dict:
    return dict(card_sku=card, component_sku=comp, component_qty=qty, is_bundle=card != comp)


def ctb(bundle: str, wb: int, oz: int, cap: int) -> dict:
    return dict(bundle_sku=bundle, wb_cards_live=wb, ozon_cards_live=oz, ozon_cards_transit_api=0,
                assemblable_now_ff=cap)


scenario("FX26", "bundle assembly capacity from authoritative BOM", world(
    evetis_ref__REF_PRODUCT_MASTER=[pm(C1), pm(C2), pm(C3), pm(BX, True), pm(BY, True), pm(BZ, True), pm(BN, True)],
    evetis_ref__REF_SKU_CHANNEL_MAP=[chmap(C1, "WB", "91"), chmap(C2, "WB", "92"), chmap(BX, "WB", "93")],
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(C1, 2, ff=100) + history(C2, 2, ff=30) + history(C3, 2, ff=15),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(C1, -40, -1, 1),
    wb_mart__V_CT_BOM_CURRENT=[bomrow(BX, C1, 1), bomrow(BX, C2, 2), bomrow(BY, C1, 1), bomrow(BY, C3, 1),
                               bomrow(BZ, C3, 1), bomrow(BZ, C2, 2)],
    wb_mart__V_CT_BUNDLE_STATUS=[ctb(BX, 4, 2, 15), ctb(BY, 0, 1, 15), ctb(BZ, 0, 0, 15)]), [
    ("x_capacity_qty2", f"{one(BC, f"bundle_sku = '{BX}' AND component_sku = '{C2}'", 'component_capacity_units')} = 15"),
    ("x_capacity_min", f"{one(BC, f"bundle_sku = '{BX}'", 'technical_assembly_capacity_units')} = 15"),
    ("x_constrained_by_c2", f"{one(BC, f"bundle_sku = '{BX}'", 'constraining_components')} = '{C2}'"),
    ("x_matches_control_tower", f"{one(BC, f"bundle_sku = '{BX}'", 'technical_assembly_capacity_units = ct_assemblable_now_ff')} IS TRUE"),
    ("z_tie_deterministic", f"{one(BC, f"bundle_sku = '{BZ}'", 'constraining_components')} = '{C2}, {C3}' AND {one(BC, f"bundle_sku = '{BZ}'", 'first_constraining_component')} = '{C2}'"),
    ("shared_component_semantics", f"{one(BC, f"bundle_sku = '{BX}'", 'shares_components_with')} = '{BY}, {BZ}'"),
    ("not_independent", f"{one(BC, f"bundle_sku = '{BX}'", 'capacity_is_independent')} IS FALSE"),
    ("c1_used_by_two_bundles", f"{one(BC, f"component_sku = '{C1}'", 'bundles_using_component')} = 2"),
    ("standalone_flag", f"{one(BC, f"component_sku = '{C1}'", 'component_also_sold_standalone')} IS TRUE AND {one(BC, f"component_sku = '{C3}'", 'component_also_sold_standalone')} IS FALSE"),
    ("no_bom_no_capacity", f"{one(BC, f"bundle_sku = '{BN}'", 'capacity_status')} = 'BUNDLE_BOM_UNAVAILABLE' AND {one(BC, f"bundle_sku = '{BN}'", 'technical_assembly_capacity_units')} IS NULL"),
    ("semantics_not_allocated", f"{cnt(BC, 'capacity_semantics != "TECHNICAL_ASSEMBLY_CAPACITY_FROM_FF_UNITS_NOT_ALLOCATED"')} = 0"),
    ("grain_unique", f"(SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t', (bundle_sku, component_sku))) FROM {ref(BC)})"),
])

# 32–36 · Акции: здоровый запас, избыток, давление срока, отрицательный вклад, WB без состава.
P_OK, P_OVER, P_EXP = "EVT-FX-PHEALTHY", "EVT-FX-POVER", "EVT-FX-PEXP"
scenario("FX32", "promotion scenarios receive inventory context without changing economics", world(
    evetis_ref__REF_PRODUCT_MASTER=[pm(P_OK), pm(P_OVER), pm(P_EXP), pm(BX, True), pm(C1), pm(C2)],
    evetis_ref__REF_SKU_CHANNEL_MAP=[chmap(s, "OZON", f"8{i}") for i, s in enumerate([P_OK, P_OVER, P_EXP, C1, C2])],
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(P_OK, 40, ff=100, wb=0, oz=20) + history(P_OVER, 40, ff=5000, wb=0, oz=20)
    + history(P_EXP, 40, ff=1000, wb=0, oz=0) + history(C1, 2, ff=100) + history(C2, 2, ff=30),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(P_OK, -120, -1, 2, "OZON") + phys(P_OVER, -120, -1, 1, "OZON")
    + phys(P_EXP, -120, -1, 5, "OZON"),
    evetis_ref__CT_EXPIRY_BATCH=[batch(P_EXP, "B-PE", d(100), "OWNER_FACT_HARD", "HIGH")],
    wb_mart__V_CT_BOM_CURRENT=[bomrow(BX, C1, 1), bomrow(BX, C2, 2)],
    wb_mart__V_CT_BUNDLE_STATUS=[ctb(BX, 4, 2, 15)],
    evetis_mart__V_PROMO_ECONOMICS_SCENARIO_CURRENT=[
        econ("OZON", "K-OK", P_OK), econ("OZON", "K-OVER", P_OVER),
        econ("OZON", "K-NEG", P_EXP, promo_c=-12.5, upl=None),
        econ("OZON", "K-BUNDLE", BX),
        econ("OZON", "K-OLD", P_OK, obs="OBS-OLD", present=False),
        econ("WB", "W-BASE", P_OK, scen="BASELINE", obs="OBS-WB")],
    evetis_mart__V_PROMO_OBSERVATION_HISTORY=[obs("OZON", "OBS-1", 1), obs("OZON", "OBS-OLD", 60),
                                              obs("WB", "OBS-WB", 1)]), [
    ("grain_preserved", f"{cnt(CX)} = 6 AND (SELECT COUNT(DISTINCT scenario_entity_key) FROM {ref(CX)}) = 6"),
    ("healthy_aligned", f"{one(CX, 'scenario_entity_key = "K-OK"', 'inventory_context_status')} = 'ALIGNED'"),
    ("healthy_cover", near(one(CX, "scenario_entity_key = 'K-OK'", "cover_days_30d"), 60.0)),
    ("channel_units_for_this_marketplace", f"{one(CX, 'scenario_entity_key = "K-OK"', 'this_channel_available_units')} = 20"),
    ("overstock_is_a_fact_not_a_label", near(one(CX, "scenario_entity_key = 'K-OVER'", "cover_days_30d"), 5020.0)),
    ("expiry_pressure_visible", near(one(CX, "scenario_entity_key = 'K-NEG'", "sell_by_required_inventory_uplift_pct"), (1000 / 70 / 5 - 1) * 100, 1e-9)),
    ("negative_contribution_unchanged", f"{one(CX, 'scenario_entity_key = "K-NEG"', 'promo_contribution_expected_rub')} = -12.5"),
    ("economic_uplift_axis_separate", f"{one(CX, 'scenario_entity_key = "K-NEG"', 'required_sales_uplift_pct')} IS NULL"),
    ("economics_passthrough_exact", f"{cnt(CX, 'scenario_entity_key = "K-OK" AND required_sales_uplift_pct = 66.6667 AND promo_contribution_expected_rub = 60 AND baseline_contribution_expected_rub = 100 AND scenario_price_rub = 900')} = 1"),
    ("bundle_capacity_context", f"{one(CX, 'scenario_entity_key = "K-BUNDLE"', 'bundle_technical_assembly_capacity_units')} = 15"),
    ("bundle_channel_cards", f"{one(CX, 'scenario_entity_key = "K-BUNDLE"', 'this_channel_bundle_cards_available')} = 2"),
    ("stale_promo_kept_without_inventory", f"{one(CX, 'scenario_entity_key = "K-OLD"', 'inventory_context_status')} = 'INVENTORY_CONTEXT_STALE' AND {one(CX, 'scenario_entity_key = "K-OLD"', 'inventory_position_units')} IS NULL"),
    ("wb_baseline_only_no_promo_rows", f"{cnt(CX, 'marketplace = "WB" AND scenario_type = "PROMO"')} = 0 AND {cnt(CX, 'marketplace = "WB"')} = 1"),
    ("temporal_gap_exposed", f"{one(CX, 'scenario_entity_key = "K-OK"', 'inventory_temporal_gap_minutes')} = 60"),
    ("alignment_limit_from_ct_config", f"{one(CX, 'scenario_entity_key = "K-OK"', 'alignment_limit_hours')} = 26"),
])

# 37–45 · Месячный план: без утверждения — пусто; с утверждением — исполнение, остаток, темп.
PS, PB = "EVT-FX-PLAN", "EVT-FX-PLANSO"
plan_start = TODAY - timedelta(days=5)


def plan_version(pv: str, status="ACTIVE", start=plan_start, end=date(LEAP_TARGET.year, 3, 31)) -> dict:
    return dict(plan_version=pv, plan_name=pv, scenario_code="FIXTURE", plan_status=status, horizon_from=start.isoformat(),
                horizon_to=end.isoformat(), source_artifact="fixture.csv", created_at=ts(NOW - timedelta(days=10)),
                created_by="fixture")


def plan_month(pv: str, month: date, mp: str, s: str, cards: float, mode="SOLO") -> dict:
    return dict(plan_version=pv, month=month.isoformat(), marketplace=mp, internal_sku=s, sales_mode=mode,
                target_cards=cards, target_physical_units=cards, created_at=ts(NOW))


def actual(s: str, first: int, last: int, cards: int, mp="WB") -> list[dict]:
    return [dict(d=d(k), marketplace=mp, internal_sku=s, sales_mode="SOLO", cards_ordered=cards) for k in range(first, last + 1)]


elapsed = (A - max(plan_start, CUR_MONTH)).days + 1 if A >= max(plan_start, CUR_MONTH) else 0
cur_plan_days = (calendar.monthrange(TODAY.year, TODAY.month)[1] - max(plan_start, CUR_MONTH).day + 1)
months = [CUR_MONTH, FEB_NONLEAP, FEB_LEAP, M30, M31]
plan_rows = [plan_month(pv, m, "WB", s, 300.0) for pv in ("PV-APPROVED", "PV-DRAFTONLY") for m in months for s in (PS, PB)]
scenario("FX42", "monthly plan machinery: approval gate, calendar, attainment, run-rate", world(
    **mapped(PS, PB),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(PS, 40, ff=5000) + history(PB, 40, ff=5000, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(PS, -120, -1, 4) + phys(PB, -120, -1, 4),
    wb_mart__V_CT_ACTUAL_DAILY=actual(PS, -120, -1, 4) + actual(PB, -120, -1, 4),
    wb_mart__V_CT_BOM_CURRENT=[bomrow(PS, PS, 1), bomrow(PB, PB, 1)],
    evetis_ref__CT_PLAN_VERSION=[plan_version("PV-APPROVED"), plan_version("PV-DRAFTONLY"),
                                 plan_version("PV-SUPERSEDED", status="SUPERSEDED")],
    evetis_ref__CT_SEASON_PLAN_MONTHLY=plan_rows + [plan_month("PV-SUPERSEDED", CUR_MONTH, "WB", PS, 1.0)],
    evetis_ref__REF_SALES_PLAN_APPROVAL=[dict(plan_version="PV-APPROVED", approval_status="APPROVED",
                                              approved_scope="MONTHLY_SKU_CHANNEL_UNITS", approved_by="owner",
                                              approved_at=ts(NOW - timedelta(days=1)), recorded_at=ts(NOW - timedelta(days=1)),
                                              recorded_by="owner")]), [
    ("unapproved_plan_not_populated", f"{cnt(PL, 'plan_version = "PV-DRAFTONLY" AND (planned_cards IS NOT NULL OR plan_attainment_pct IS NOT NULL OR run_rate_projection_cards IS NOT NULL)')} = 0"),
    ("unapproved_status", f"{cnt(PL, 'plan_version = "PV-DRAFTONLY" AND planning_metrics_status != "PLAN_APPROVAL_NOT_RECORDED"')} = 0"),
    ("superseded_excluded", f"{cnt(PL, 'plan_version = "PV-SUPERSEDED"')} = 0"),
    ("approved_populated", f"{cnt(PL, 'plan_version = "PV-APPROVED" AND planned_cards = 300')} = {len(months) * 2}"),
    ("feb_non_leap_28_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{FEB_NONLEAP.isoformat()}'", 'plan_days')} = 28"),
    ("feb_leap_29_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{FEB_LEAP.isoformat()}'", 'plan_days')} = 29"),
    ("month_30_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{M30.isoformat()}'", 'days_in_month')} = 30"),
    ("month_31_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{M31.isoformat()}'", 'days_in_month')} = 31"),
    ("partial_month_plan_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", 'plan_days')} = {cur_plan_days}"),
    ("mtd_actual", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", 'actual_cards_to_date')} = {4 * elapsed}"),
    ("remaining_days", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", 'remaining_calendar_days')} = {cur_plan_days - elapsed}"),
    ("attainment_pct", near(one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", "plan_attainment_pct"), 4 * elapsed / 300 * 100, 1e-9)),
    ("required_per_day_remaining", "TRUE" if cur_plan_days == elapsed else
     near(one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", "required_cards_per_day_remaining"),
          max(300 - 4 * elapsed, 0) / (cur_plan_days - elapsed), 1e-9)),
    ("run_rate_projection", near(one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", "run_rate_projection_cards"),
                                  4 * elapsed + 4.0 * (cur_plan_days - elapsed), 1e-9)),
    ("run_rate_quality_reliable", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PS}'", 'projection_velocity_quality')} = 'NORMAL'"),
    ("run_rate_quality_constrained", f"{one(PL, f"plan_version = 'PV-APPROVED' AND month = DATE '{CUR_MONTH.isoformat()}' AND internal_sku = '{PB}'", 'projection_velocity_quality')} = 'STOCKOUT_CONSTRAINED'"),
    ("projection_labelled", f"{cnt(PL, 'plan_version = "PV-APPROVED" AND projection_method NOT LIKE "RUN_RATE_PROJECTION%"')} = 0"),
    ("trajectory_plan_rows", f"{cnt(TR, f"trajectory_basis = 'APPROVED_SALES_PLAN' AND internal_sku = '{PS}'")} = {len(months)}"),
    ("trajectory_opening_is_position", near(one(TR, f"trajectory_basis = 'APPROVED_SALES_PLAN' AND internal_sku = '{PS}' AND month = DATE '{CUR_MONTH.isoformat()}'", "opening_units"), 5020.0)),
    ("trajectory_chain", f"(SELECT LOGICAL_AND(ABS(opening_units - prev_closing) < 1e-6) FROM (SELECT opening_units, LAG(closing_units_unconstrained) OVER (ORDER BY month) AS prev_closing FROM {ref(TR)} WHERE trajectory_basis = 'APPROVED_SALES_PLAN' AND internal_sku = '{PS}') WHERE prev_closing IS NOT NULL)"),
    ("trajectory_unapproved_absent", f"{cnt(TR, 'plan_version = "PV-DRAFTONLY"')} = 0"),
])

# Сценарии спецификации §51 (1–45) → блоки регрессии. Тест проверяет полноту карты.
SPEC_SCENARIOS: dict[int, tuple[str, ...]] = {
    1: ("FX01",), 2: ("FX02",), 3: ("FX03",), 4: ("FX04",), 5: ("FX05",), 6: ("FX06",), 7: ("FX07",),
    8: ("FX08",), 9: ("FX09",), 10: ("FX10",), 11: ("FX11",), 12: ("FX12",), 13: ("FX12",), 14: ("FX12",),
    15: ("FX15",), 16: ("FX15",), 17: ("FX15",), 18: ("FX15",), 19: ("FX19",), 20: ("FX19",), 21: ("FX19",),
    22: ("FX19",), 23: ("FX19",), 24: ("FX19",), 25: ("FX19",), 26: ("FX26",), 27: ("FX26",), 28: ("FX26",),
    29: ("FX26",), 30: ("FX26",), 31: ("FX26",), 32: ("FX32",), 33: ("FX32",), 34: ("FX32",), 35: ("FX32",),
    36: ("FX32",), 37: ("FX37", "FX42"), 38: ("FX37", "FX42"), 39: ("FX37", "FX42"), 40: ("FX37", "FX42"),
    41: ("FX37", "FX42"), 42: ("FX42",), 43: ("FX42",), 44: ("FX42",), 45: ("FX42",),
}


# ─────────────────────────────────────────────────────────────── рендер
def render_block(assertions: list[tuple[str, str, str]], data: dict) -> str:
    return base.render_block(assertions, objects=OBJECTS, raw_tables=RAW_TABLES, schemas=schemas(), data=data)


# Сколько утверждений в одном операторе: каждое утверждение раскрывает дерево представлений
# заново, и тяжёлые представления (цели, план, траектория, контекст акций) дробятся мельче.
CHUNK = {"FX19": 3, "FX37": 3, "FX26": 4, "FX32": 3, "FX42": 3}
DEFAULT_CHUNK = 6


def render_fixture_checks() -> str:
    header = (
        "-- ============================================================================\n"
        "-- PR-PROMO-4 · регрессионные сценарии слоя запасов и распродажи на фикстурах.\n"
        "-- СГЕНЕРИРОВАНО tools/promo_inventory_render.py fixtures. НЕ РЕДАКТИРОВАТЬ.\n"
        "-- Тела 7 представлений взяты из Git дословно; каждый внешний объект заменён типизированной\n"
        "-- фикстурой своего сценария. Ни одной таблицы не читается.\n"
        f"-- Время фикстур: NOW = {ts(NOW)} (UTC). Сценариев: {len(SCENARIOS)}, утверждений: "
        f"{sum(len(a) for *_, a in SCENARIOS)}.\n"
        "-- ============================================================================\n"
    )
    blocks = []
    for sid, title, data, assertions in SCENARIOS:
        size = CHUNK.get(sid, DEFAULT_CHUNK)
        parts = [assertions[i:i + size] for i in range(0, len(assertions), size)]
        for n, part in enumerate(parts, 1):
            bid = sid if len(parts) == 1 else f"{sid}.{n}"
            rows = [(sid, name, expr) for name, expr in part]
            blocks.append(f"\n-- @check {bid}\n-- {title}\n{render_block(rows, data)};\n")
    return header + "".join(blocks)


def _empty_new_tables(sql: str) -> str:
    """До развёртывания новых справочников — пустые типизированные CTE вместо таблиц."""
    sch = schemas()
    used = [k for k in NEW_TABLES if f"`{PROJECT}.{k[0]}.{k[1]}`" in sql]
    for k in used:
        sql = sql.replace(f"`{PROJECT}.{k[0]}.{k[1]}`", f"empty__{k[0]}__{k[1]}")
    ctes = [f"empty__{k[0]}__{k[1]} AS (SELECT * FROM UNNEST(ARRAY<STRUCT<"
            + ", ".join(f"{c} {t}" for c, t in sch[k]) + ">>[]))" for k in used]
    return base._merge_with(ctes, sql) if ctes else sql


def render_predeploy(query: str) -> str:
    return _empty_new_tables(base.render_predeploy(query, OBJECTS))


def render_predeploy_file(text: str) -> str:
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
        out.append("".join(header) + (render_predeploy(stmt) + ";\n\n" if stmt else ""))
    return "".join(out)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "predeploy":
        sys.stdout.write(render_predeploy_file(Path(argv[1]).read_text(encoding="utf-8")))
        return 0
    if argv and argv[0] == "fixtures":
        out = render_fixture_checks()
        target = base._opt(argv, "--out")
        Path(target).write_text(out, encoding="utf-8") if target else sys.stdout.write(out)
        return 0
    if len(argv) >= 2 and argv[0] == "run":
        project = base._opt(argv, "--project")
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
        return base.run_blocks(base.split_blocks(text), project, base._opt(argv, "--token-command"),
                               base._opt(argv, "--token-env"))
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
