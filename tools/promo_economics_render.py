#!/usr/bin/env python3
"""PR-PROMO-3 — регрессионные сценарии и предразвёртывание слоя экономики акций.

Тот же механизм, что у PR-PROMO-2 (tools/promo_canonical_render.py): тела представлений
берутся из Git дословно и подставляются в запрос как CTE, RAW и снимки базиса заменяются
типизированными фикстурами (схемы — из DDL PR-PROMO-1 и PR-PROMO-3), исполняет движок BigQuery.
Второй реализации экономики на Python нет: Python здесь только ПОДГОТАВЛИВАЕТ канонические
значения снимка фикстуры (как их положила бы копия канонической вью), а считает вклад SQL.

usage:
  python tools/promo_economics_render.py predeploy <checks.sql>        # в stdout
  python tools/promo_economics_render.py fixtures [--out <path>]       # вне Git
  python tools/promo_economics_render.py run fixtures --project <P> --token-command "…"
  python tools/promo_economics_render.py run predeploy <checks.sql> --project <P> --token-command "…"
  python tools/promo_economics_render.py run live <checks.sql> --project <P> --token-command "…"
"""
from __future__ import annotations

import copy
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_render as base  # noqa: E402

ROOT = base.ROOT
PROJECT = base.PROJECT
BASIS_DDL = ROOT / "sql" / "promotions" / "pr_promo3_basis_snapshot.sql"

NEW_OBJECTS: list[tuple[str, str, str]] = [
    ("wb_mart", "V_WB_PROMO_ECONOMICS_BASIS_HISTORY", "sql/promotions/pr_promo3/wb_mart"),
    ("wb_mart", "V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY", "sql/promotions/pr_promo3/wb_mart"),
    ("ozon_mart", "V_OZON_PROMO_ECONOMICS_BASIS_HISTORY", "sql/current/ozon_mart"),
    ("ozon_mart", "V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY", "sql/current/ozon_mart"),
    ("evetis_mart", "V_PROMO_ECONOMICS_SCENARIO_HISTORY", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_ECONOMICS_SCENARIO_CURRENT", "sql/current/evetis_mart"),
    ("evetis_mart", "V_PROMO_ECONOMICS_COVERAGE_CURRENT", "sql/current/evetis_mart"),
]
NEW_TABLES: list[tuple[str, str]] = [
    ("wb_raw", "WB_PROMO_ECONOMICS_BASIS_SNAPSHOT"),
    ("ozon_raw", "OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT"),
]
OBJECTS = base.OBJECTS + NEW_OBJECTS
RAW_TABLES = base.RAW_TABLES + NEW_TABLES


def schemas():
    return base.raw_schemas([base.RAW_DDL, BASIS_DDL], RAW_TABLES)


def _r2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ozon_basis_row(slot: str, captured: str, sku: str, offer: str, price, commission_pct, cogs, last_mile,
                   log_e, log_max, *, product_type="SINGLE", cogs_from="2025-12-01") -> dict:
    """Строка снимка так, как её положила бы копия V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.

    Канонические значения повторяют выражения канона (V_OZON_SKU_CURRENT_TARIFF,
    V_OZON_SKU_FORWARD_ECONOMICS_CURRENT): комиссия ROUND(P × %/100, 2), эквайринг — сумма API
    (здесь 1 % цены), break-even = фикс / (1 − c − ROUND(эквайринг/P×100, 4)/100).
    """
    P, c = Decimal(str(price)), Decimal(str(commission_pct))
    C = None if cogs is None else Decimal(str(cogs))
    L, E, M = Decimal(str(last_mile)), Decimal(str(log_e)), Decimal(str(log_max))
    acq = _r2(P * Decimal("0.01"))
    comm = _r2(P * c / 100)
    a_frac = (acq / P * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) / 100
    denom = 1 - c / 100 - a_frac
    row = dict(snapshot_slot=slot, snapshot_id="OZECON_prod_" + slot.replace("-", "").replace(":", "").replace("T", ""),
               captured_at=captured, trigger="fixture", internal_sku=sku, offer_id=offer, ozon_sku="8" + offer[1:],
               product_name=f"Товар {sku}", product_type=product_type, economics_mode="FORWARD_MODELLED",
               tariff_snapshot_at=captured, tariff_effective_at="2025-12-01", seller_base_price=P,
               current_management_cogs_rub=C, cogs_basis="MANAGEMENT_LANDED", cogs_effective_from=cogs_from,
               commission_pct=c, commission_rub=comm, acquiring_rub=acq, last_mile_rub=L, logistics_min_rub=E,
               logistics_expected_rub=E, logistics_max_rub=M, logistics_expected_basis="FIXTURE",
               forward_economics_ready=True, tariff_freshness_status="FRESH",
               expected_mode_status="CANCEL_ONLY_RETURNS_EXCLUDED")
    if C is not None:
        row["contribution_expected"] = _r2(P - C - comm - acq - E - L)
        row["contribution_worst_case"] = _r2(P - C - comm - acq - M - L)
        row["break_even_price_expected"] = _r2((C + L + E) / denom)
        row["break_even_price_worst"] = _r2((C + L + M) / denom)
    return row


def wb_basis_row(slot: str, captured: str, sku: str, nm: int, price, commission_pct, acq50, acq90, log_e, log90,
                 cogs, *, is_bundle=False) -> dict:
    """Строка снимка так, как её положила бы копия V_WB_SKU_FORWARD_ECONOMICS_CURRENT (FLOAT64)."""
    take, take_s = (commission_pct + acq50) / 100, (commission_pct + acq90) / 100
    row = dict(snapshot_slot=slot, snapshot_id="WBECON_prod_" + slot.replace("-", "").replace(":", "").replace("T", ""),
               captured_at=captured, trigger="fixture", internal_sku=sku, nm_id=nm, product_name_short=f"WB {sku}",
               is_bundle=is_bundle, seller_effective_price_rub=price, price_observed_at=captured,
               price_freshness="FRESH", cogs_rub=cogs, cogs_source="FIXTURE", cogs_effective_from="2025-12-01",
               effective_commission_pct=commission_pct, commission_source="FIXTURE",
               commission_tariff_freshness="FRESH", acquiring_p50_pct=acq50, acquiring_p90_pct=acq90,
               logistics_expected_rub=log_e, logistics_p90_rub=log90, logistics_sample_size=100,
               economics_status="HEALTHY" if cogs is not None else "BLOCKED",
               economics_confidence="HIGH", blocked_reason=None if cogs is not None else "COGS_MISSING",
               tax_model_status="PRE_TAX", economics_model_version="WB_FE_V1")
    if cogs is not None:
        row["contribution_before_ads_pre_tax_rub"] = round(price - price * take - log_e - cogs, 2)
        row["break_even_before_ads_pre_tax_base"] = round((log_e + cogs) / (1 - take), 2)
        row["break_even_before_ads_pre_tax_stress"] = round((log90 + cogs) / (1 - take_s), 2)
    return row


S1, S2 = "2026-01-01T04:00", "2026-01-01T09:00"
C1, C2 = "2026-01-01 04:10:00+00", "2026-01-01 09:10:00+00"


def fixtures() -> dict:
    data = copy.deepcopy(base.fixtures())
    # Цены каталога в снимке наблюдения: основа оси применимости цены участия.
    list_price = {1001: (1000, 900), 1005: (850, 850), 1006: (1000, 1000)}
    for r in data[("ozon_raw", "RAW_OZON_PROMO_PRODUCT_MARKETING")]:
        if r["product_id"] in list_price:
            r["price_rub"], r["marketing_seller_price_rub"] = list_price[r["product_id"]]
    # Валюта автодобавления: RUB, кроме одного случая, который обязан упасть закрыто.
    for r in data[("ozon_raw", "RAW_OZON_PROMO_AUTO_ADD")]:
        r["currency_code"] = "USD" if (r["observation_id"] == base.O2 and r["product_id"] == 1007) else "RUB"
    prods = data[("ozon_raw", "RAW_OZON_PROMO_PRODUCTS")]
    for r in prods:
        if r["action_id"] == 102 and r["product_id"] == 1001:
            r["action_price_rub"] = None                      # NULL цены источника
    dup = next(r for r in prods if r["observation_id"] == base.O1 and r["action_id"] == 101
               and r["product_id"] == 1001 and r["membership"] == "PARTICIPATING")
    prods.append(dict(dup, ingested_at="2026-01-01 04:30:00+00"))   # дубль свидетельства источника

    oz = []
    for slot, cap in ((S1, C1), (S2, C2)):
        second = slot == S2
        oz += [
            ozon_basis_row(slot, cap, "SKU_001", "9001", 900, 40, 200, 20, 80, 120),
            ozon_basis_row(slot, cap, "SKU_002", "9002", 1000, 40, 400 if second else 460.5, 20, 80, 120,
                           cogs_from="2026-01-01" if second else "2025-12-01"),
            ozon_basis_row(slot, cap, "SKU_003", "9003", 1000, 45 if second else 40, 200, 20, 80, 120),
            ozon_basis_row(slot, cap, "SKU_004", "9004", 1000, 40, 600, 20, 80, 120),
            ozon_basis_row(slot, cap, "SKU_005", "9005", 850, 40, 200, 20, 80, 120),
            ozon_basis_row(slot, cap, "SKU_006", "9006", 1000, 40, 430.9, 20, 80, 700, product_type="BUNDLE"),
            ozon_basis_row(slot, cap, "SKU_007", "9007", 1000, 40, 200 if second else None, 20, 80, 120),
            ozon_basis_row(slot, cap, "SKU_009", "9009", 1000, 40, 200, 20, 80, 120),
        ]
    data[("ozon_raw", "OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT")] = oz
    # WB: снимок только на первом слоте — второй слот WB обязан дать TEMPORAL_ALIGNMENT_UNAVAILABLE.
    data[("wb_raw", "WB_PROMO_ECONOMICS_BASIS_SNAPSHOT")] = [
        wb_basis_row(S1, "2026-01-01 04:10:00+00", "SKU_001", 5001, 1000.0, 40.0, 3.5, 4.0, 70.0, 90.0, 250.0),
        wb_basis_row(S1, "2026-01-01 04:10:00+00", "SKU_002", 5002, 1200.0, 40.0, 3.5, 4.0, 90.0, 110.0, None,
                     is_bundle=True),
    ]
    return data


P = f"`{PROJECT}"
OZ_B = f"{P}.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY`"
OZ_S = f"{P}.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY`"
WB_B = f"{P}.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY`"
WB_S = f"{P}.wb_mart.V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY`"
N_H = f"{P}.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`"
N_C = f"{P}.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`"
N_COV = f"{P}.evetis_mart.V_PROMO_ECONOMICS_COVERAGE_CURRENT`"
TOL = "0.00501"


def oz(slot: str, aid, pid, interp: str, col: str) -> str:
    a = "action_id IS NULL" if aid is None else f"action_id = {aid}"
    return (f"(SELECT {col} FROM {OZ_S} WHERE economics_slot = '{slot}' AND {a} AND product_id = {pid} "
            f"AND price_interpretation = '{interp}')")


def ozb(slot: str, sku: str, col: str) -> str:
    return (f"(SELECT {col} FROM {OZ_S} WHERE economics_slot = '{slot}' AND scenario_type = 'BASELINE' "
            f"AND internal_sku = '{sku}')")


def wb(slot: str, where: str, col: str) -> str:
    return f"(SELECT {col} FROM {WB_S} WHERE economics_slot = '{slot}' AND {where})"


def cnt(view: str, where: str = "TRUE") -> str:
    return f"(SELECT COUNT(*) FROM {view} WHERE {where})"


PART, CAND, SCHED, ELIG = ("CURRENT_PARTICIPATION_ACTION_PRICE", "CANDIDATE_MAX_QUALIFYING_ENTRY_PRICE",
                           "SCHEDULED_AUTO_ADD_PRICE", "AUTO_ADD_ELIGIBLE_PRICE")

ASSERTIONS: list[tuple[str, str, str]] = [
    # 1. Промо-цена = базовой.
    ("FE01", "equal_price_zero_delta", f"{oz(S1, 101, 1001, PART, 'delta_contribution_expected_rub')} = 0"),
    ("FE01", "equal_price_uplift_zero", f"{oz(S1, 101, 1001, PART, 'required_sales_uplift_pct')} = 0"),
    ("FE01", "equal_price_no_change", f"{oz(S1, 101, 1001, PART, 'uplift_interpretation')} = 'NO_CHANGE'"),
    ("FE01", "baseline_already_includes_promo",
     f"{oz(S1, 101, 1001, PART, 'baseline_includes_promotion_effect')} = TRUE"),
    # 2. Промо-цена ниже базовой.
    ("FE02", "lower_price_lower_contribution", f"{oz(S1, 101, 1006, PART, 'delta_contribution_expected_rub')} < 0"),
    ("FE02", "lower_price_more_units", f"{oz(S1, 101, 1006, PART, 'uplift_interpretation')} = 'MORE_UNITS_REQUIRED'"),
    # 3. Промо-цена выше базовой.
    ("FE03", "higher_price_positive_delta", f"{oz(S1, 101, 1005, PART, 'delta_contribution_expected_rub')} > 0"),
    ("FE03", "higher_price_binding_axis",
     f"{oz(S1, 101, 1005, PART, 'price_binding_evidence')} = 'ABOVE_CURRENT_SELLER_PRICE_NOT_APPLIED'"),
    # 4. Положительный вклад в акции и расчёт роста.
    ("FE04", "positive_promo_contribution", f"{oz(S2, 101, 1002, PART, 'promo_contribution_expected_rub')} = 31"),
    ("FE04", "positive_baseline_contribution", f"{oz(S2, 101, 1002, PART, 'baseline_contribution_expected_rub')} = 90"),
    ("FE04", "uplift_formula",
     f"ABS({oz(S2, 101, 1002, PART, 'required_sales_uplift_pct')} - (90 / 31 - 1) * 100) < 0.000001"),
    ("FE04", "margin_formula", f"ABS({oz(S2, 101, 1002, PART, 'promo_margin_expected_pct')} - 31 / 900 * 100) < 0.000001"),
    # 5. Нулевой вклад в акции.
    ("FE05", "zero_promo_contribution", f"{oz(S1, 101, 1002, CAND, 'promo_contribution_expected_rub')} = 0"),
    ("FE05", "zero_contribution_uplift_null", f"{oz(S1, 101, 1002, CAND, 'required_sales_uplift_pct')} IS NULL"),
    ("FE05", "zero_contribution_status",
     f"{oz(S1, 101, 1002, CAND, 'uplift_status')} = 'NOT_ACHIEVABLE_PROMO_NON_POSITIVE'"),
    # 6. Отрицательный вклад в акции.
    ("FE06", "negative_promo_contribution", f"{oz(S1, 101, 1004, CAND, 'promo_contribution_expected_rub')} < 0"),
    ("FE06", "negative_uplift_null", f"{oz(S1, 101, 1004, CAND, 'required_sales_uplift_pct')} IS NULL"),
    # 7. Вклад в акции выше базового → отрицательный рост, не обнулённый.
    ("FE07", "negative_uplift_preserved", f"{oz(S1, 101, 1005, PART, 'required_sales_uplift_pct')} < 0"),
    ("FE07", "negative_uplift_value",
     f"ABS({oz(S1, 101, 1005, PART, 'required_sales_uplift_pct')} - (201.5 / 231 - 1) * 100) < 0.000001"),
    ("FE07", "negative_uplift_interpretation",
     f"{oz(S1, 101, 1005, PART, 'uplift_interpretation')} = 'PROMO_UNIT_CONTRIBUTION_HIGHER'"),
    # 8. Базовый вклад ≤ 0.
    ("FE08", "baseline_non_positive_status",
     f"{oz(S1, 101, 1004, CAND, 'uplift_status')} = 'NOT_MEANINGFUL_BASELINE_NON_POSITIVE'"),
    ("FE08", "baseline_non_positive_uplift_null", f"{oz(S1, 101, 1004, SCHED, 'required_sales_uplift_pct')} IS NULL"),
    # 9. Нет COGS.
    ("FE09", "missing_cogs_status", f"{oz(S1, 101, 1007, ELIG, 'economics_status')} = 'MISSING_COGS'"),
    ("FE09", "missing_cogs_no_numbers", f"{oz(S1, 101, 1007, ELIG, 'promo_contribution_expected_rub')} IS NULL"),
    # 10. Нет сопоставления SKU.
    ("FE10", "missing_sku_status", f"{oz(S1, 101, 1008, PART, 'economics_status')} = 'MISSING_CANONICAL_SKU'"),
    ("FE10", "missing_sku_row_kept", f"{cnt(OZ_S, 'product_id = 1008')} = 2"),
    # 11. Неподдерживаемая валюта.
    ("FE11", "unsupported_currency", f"{oz(S2, 101, 1007, ELIG, 'economics_status')} = 'UNSUPPORTED_CURRENCY'"),
    ("FE11", "unsupported_currency_no_numbers", f"{oz(S2, 101, 1007, ELIG, 'promo_contribution_expected_rub')} IS NULL"),
    # 12. Неоднозначная семантика цены: WB planPrice.
    ("FE12", "wb_plan_price_unproven",
     f"{wb(S1, 'nm_id = 5001 AND scenario_type = "PROMO"', 'economics_status')} = 'PRICE_SEMANTICS_UNPROVEN'"),
    ("FE12", "wb_plan_price_no_numbers",
     f"{wb(S1, 'nm_id = 5001 AND scenario_type = "PROMO"', 'promo_contribution_expected_rub')} IS NULL"),
    # 13. Будущее автодобавление при текущих допущениях.
    ("FE13", "future_scheduled_label",
     f"{oz(S1, 101, 1003, SCHED, 'temporal_basis')} = 'CURRENT_ASSUMPTIONS_FOR_FUTURE_PROMO'"),
    ("FE13", "future_scheduled_computable", f"{oz(S1, 101, 1003, SCHED, 'economics_status')} = 'COMPUTABLE'"),
    # 14. Ozon: участие.
    ("FE14", "participation_interpretation",
     f"{oz(S1, 101, 1001, PART, 'price_source')} = 'OZON POST /v1/actions/products.action_price'"),
    ("FE14", "participation_applied_axis",
     f"{oz(S1, 101, 1001, PART, 'price_binding_evidence')} = 'APPLIED_TO_DEFAULT_SELLER_PRICE'"),
    # 15. Ozon: кандидат — по максимальной цене входа, не по action_price = 0.
    ("FE15", "candidate_uses_max_action_price", f"{oz(S1, 101, 1002, CAND, 'scenario_price_rub')} = 950"),
    ("FE15", "candidate_hypothetical", f"{oz(S1, 101, 1002, CAND, 'price_binding_evidence')} = 'HYPOTHETICAL_ENTRY'"),
    # 16. Ozon: запланированное автодобавление.
    ("FE16", "scheduled_price_source",
     f"{oz(S1, 101, 1003, SCHED, 'price_source')} = 'OZON POST /v1/actions/auto-add/products/list.action_price_to_auto_add'"),
    ("FE16", "scheduled_date_kept", f"{oz(S1, 101, 1003, SCHED, 'auto_add_at')} = TIMESTAMP '{base.AUTO_ADD_DATE}'"),
    # 17. Пересекающиеся свидетельства: отдельные сценарии, одно разрешённое состояние.
    ("FE17", "overlap_three_scenarios", f"{cnt(OZ_S, f"economics_slot = '{S1}' AND product_id = 1006 AND scenario_type = 'PROMO'")} = 3"),
    ("FE17", "overlap_one_state",
     f"(SELECT COUNT(DISTINCT sku_state) FROM {OZ_S} WHERE economics_slot = '{S1}' AND product_id = 1006) = 1"),
    ("FE17", "overlap_distinct_interpretations",
     f"(SELECT COUNT(DISTINCT price_interpretation) FROM {OZ_S} WHERE economics_slot = '{S1}' AND product_id = 1006 AND scenario_type = 'PROMO') = 3"),
    # 18. WB: состав не наблюдаем — строк SKU нет, покрытие говорит почему.
    ("FE18", "wb_auto_no_scenarios", f"{cnt(N_H, "marketplace = 'WB' AND source_promotion_id = '201'")} = 0"),
    ("FE18", "wb_auto_coverage_status",
     f"(SELECT economics_coverage_status FROM {N_COV} WHERE marketplace = 'WB' AND source_promotion_id = '201') "
     f"= 'PROMO_MEMBERSHIP_NOT_OBSERVABLE'"),
    # 19. WB: базовая цена сходится с каноном WB_FE_V1.
    ("FE19", "wb_baseline_reconciles",
     f"ABS({wb(S1, 'nm_id = 5001 AND scenario_type = "BASELINE"', 'baseline_contribution_expected_rub - canonical_contribution_at_baseline_rub')}) <= {TOL}"),
    ("FE19", "wb_baseline_value", f"ABS({wb(S1, 'nm_id = 5001 AND scenario_type = "BASELINE"', 'baseline_contribution_expected_rub')} - 245) < 0.000001"),
    ("FE19", "wb_break_even_is_root",
     f"ABS({wb(S1, 'nm_id = 5001 AND scenario_type = "BASELINE"', 'break_even_price_expected_rub')} * 0.565 - 320) < 0.01"),
    # 20. Ozon: базовая цена сходится с каноном FORWARD_MODELLED — для каждого вычислимого SKU.
    ("FE20", "ozon_baseline_reconciles_all",
     f"(SELECT LOGICAL_AND(ABS(baseline_contribution_expected_rub - canonical_contribution_at_baseline_rub) <= {TOL}) "
     f"FROM {OZ_S} WHERE scenario_type = 'BASELINE' AND economics_status = 'COMPUTABLE')"),
    ("FE20", "ozon_worst_reconciles",
     f"(SELECT LOGICAL_AND(ABS(b.baseline_contribution_downside_rub - k.canonical_contribution_downside_rub) <= {TOL}) "
     f"FROM {OZ_S} b JOIN {OZ_B} k ON k.economics_slot = b.economics_slot AND k.internal_sku = b.internal_sku "
     f"WHERE b.scenario_type = 'BASELINE' AND b.economics_status = 'COMPUTABLE')"),
    ("FE20", "ozon_break_even_is_root",
     f"(SELECT LOGICAL_AND(ABS(break_even_price_expected_rub * (1 - 0.40 - 0.01) - (cogs_rub + 100)) < 0.01) "
     f"FROM {OZ_S} WHERE scenario_type = 'BASELINE' AND economics_status = 'COMPUTABLE' AND economics_slot = '{S1}')"),
    # 21. COGS сменился на втором слоте — второй слот на новом COGS.
    ("FE21", "second_slot_new_cogs", f"{oz(S2, 101, 1002, PART, 'cogs_rub')} = 400"),
    ("FE21", "second_slot_cogs_period", f"{oz(S2, 101, 1002, PART, 'cogs_effective_from')} = DATE '2026-01-01'"),
    # 22. Комиссия сменилась на втором слоте.
    ("FE22", "second_slot_new_commission",
     f"{oz(S2, 101, 1003, SCHED, 'promo_contribution_expected_rub')} < {oz(S1, 101, 1003, SCHED, 'promo_contribution_expected_rub')}"),
    # 23. Прошлое не переписано более новыми данными.
    ("FE23", "first_slot_old_cogs", f"{oz(S1, 101, 1002, CAND, 'cogs_rub')} = 460.5"),
    ("FE23", "first_slot_old_commission_contribution", f"{oz(S1, 101, 1003, SCHED, 'promo_contribution_expected_rub')} = 290"),
    ("FE23", "first_slot_basis_snapshot",
     f"{oz(S1, 101, 1003, SCHED, 'basis_snapshot_id')} = 'OZECON_prod_202601010400'"),
    # 24. Набор с поддержанной экономикой (COGS набора есть).
    ("FE24", "bundle_supported", f"{oz(S1, 101, 1006, PART, 'economics_status')} = 'COMPUTABLE'"),
    # 25. Набор без экономики (WB: COGS набора нет) → не придумывается.
    ("FE25", "bundle_without_cogs",
     f"{wb(S1, 'nm_id = 5002 AND scenario_type = "BASELINE"', 'economics_status')} = 'MISSING_COGS'"),
    ("FE25", "bundle_without_cogs_no_numbers",
     f"{wb(S1, 'nm_id = 5002 AND scenario_type = "BASELINE"', 'baseline_contribution_expected_rub')} IS NULL"),
    # 26. Цена ровно на break-even.
    ("FE26", "exact_break_even", f"{oz(S1, 101, 1002, CAND, 'promo_price_minus_break_even_rub')} = 0"),
    # 27. Почти нулевой положительный вклад: считается, без порогов.
    ("FE27", "near_zero_positive", f"ABS({oz(S1, 101, 1006, PART, 'promo_contribution_expected_rub')} - 0.1) < 0.000001"),
    ("FE27", "near_zero_uplift_finite", f"{oz(S1, 101, 1006, PART, 'uplift_status')} = 'COMPUTED'"),
    ("FE27", "near_zero_downside_negative", f"{oz(S1, 101, 1006, PART, 'promo_downside_negative')} = TRUE"),
    # 28. Дубль свидетельства источника → одна строка сценария.
    ("FE28", "duplicate_evidence_one_scenario",
     f"{cnt(OZ_S, f"economics_slot = '{S1}' AND action_id = 101 AND product_id = 1001 AND price_interpretation = '{PART}'")} = 1"),
    # 29. Устаревшее наблюдение: сценарий кандидата исчез во втором слоте — в текущем он виден как отсутствующий.
    ("FE29", "stale_scenario_not_present",
     f"(SELECT present_in_latest_slot FROM {N_C} WHERE marketplace = 'OZON' AND source_promotion_id = '101' "
     f"AND marketplace_product_id = '1002' AND price_interpretation = '{CAND}') = FALSE"),
    ("FE29", "stale_scenario_last_slot",
     f"(SELECT last_economics_slot FROM {N_C} WHERE marketplace = 'OZON' AND source_promotion_id = '101' "
     f"AND marketplace_product_id = '1002' AND price_interpretation = '{CAND}') = '{S1}'"),
    ("FE29", "current_from_latest_slot",
     f"(SELECT scenario_price_rub FROM {N_C} WHERE marketplace = 'OZON' AND source_promotion_id = '101' "
     f"AND marketplace_product_id = '1002' AND price_interpretation = '{PART}') = 900"),
    # 30. NULL цены источника.
    ("FE30", "null_source_price", f"{oz(S1, 102, 1001, PART, 'economics_status')} = 'MISSING_SOURCE_PRICE'"),
    ("FE30", "null_source_price_no_numbers", f"{oz(S1, 102, 1001, PART, 'promo_contribution_expected_rub')} IS NULL"),
    # Время: нет снимка слота → не подставляется текущий базис.
    ("FET", "wb_second_slot_temporal_unavailable",
     f"{wb(S2, 'nm_id = 5001 AND scenario_type = "PROMO"', 'economics_status')} = 'TEMPORAL_ALIGNMENT_UNAVAILABLE'"),
    ("FET", "wb_no_baseline_without_snapshot", f"{cnt(WB_S, f"economics_slot = '{S2}' AND scenario_type = 'BASELINE'")} = 0"),
    ("FET", "error_observation_no_scenarios", f"{cnt(N_H, f"observation_id = '{base.O3_ERR}'")} = 0"),
    # Downside своей площадки, не симметричный.
    ("FED", "ozon_downside_case", f"(SELECT LOGICAL_AND(downside_case = 'OZON_WORST_MAX_LOGISTICS') FROM {OZ_S})"),
    ("FED", "wb_downside_case",
     f"(SELECT LOGICAL_AND(downside_case = 'WB_STRESS_P90_ACQUIRING_AND_LOGISTICS') FROM {WB_S} WHERE downside_case IS NOT NULL)"),
    ("FED", "wb_downside_formula",
     f"ABS({wb(S1, 'nm_id = 5001 AND scenario_type = "BASELINE"', 'baseline_contribution_downside_rub')} - (1000 * 0.56 - 340)) < 0.000001"),
    # Зерно и вывод текущего из истории.
    ("FEG", "grain_ozon_scenarios",
     f"(SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t', (economics_slot, action_id, product_id, internal_sku, scenario_key))) FROM {OZ_S})"),
    ("FEG", "grain_neutral_history",
     f"(SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, economics_slot, scenario_entity_key))) FROM {N_H})"),
    ("FEG", "grain_neutral_current",
     f"(SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, scenario_entity_key))) FROM {N_C})"),
    ("FEG", "ozon_promo_rows_count", f"{cnt(OZ_S, f"economics_slot = '{S1}' AND scenario_type = 'PROMO'")} = 13"),
    ("FEG", "ozon_baseline_rows_count", f"{cnt(OZ_S, f"economics_slot = '{S1}' AND scenario_type = 'BASELINE'")} = 8"),
    ("FEG", "no_recommendation_values",
     f"(SELECT COUNTIF(REGEXP_CONTAINS(CONCAT(IFNULL(uplift_interpretation, ''), IFNULL(economics_status, '')), "
     f"r'ENTER|EXIT|STAY|WATCH|APPROVE|REJECT|GOOD_|BAD_')) FROM {N_H}) = 0"),
]


def render_fixture_checks() -> str:
    order: list[str] = []
    groups: dict[str, list] = {}
    for a in ASSERTIONS:
        groups.setdefault(a[0], []).append(a)
        if a[0] not in order:
            order.append(a[0])
    sch, data = schemas(), fixtures()
    header = ("-- PR-PROMO-3 · регрессионные сценарии экономики акций на фикстурах. СГЕНЕРИРОВАНО\n"
              "-- tools/promo_economics_render.py fixtures. Тела представлений — из Git дословно.\n"
              f"-- Блоков: {len(order)}, утверждений: {len(ASSERTIONS)}. NULL = FAIL.\n")
    blocks = [f"\n-- @check {sid}\n"
              f"{base.render_block(groups[sid], objects=OBJECTS, raw_tables=RAW_TABLES, schemas=sch, data=data)};\n"
              for sid in order]
    return header + "".join(blocks)


def _split_top_level(s: str) -> list[str]:
    """Разбить список выражений SELECT по запятым верхнего уровня (скобки и строки учитываются)."""
    out, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def virtual_snapshot_ctes() -> dict[tuple[str, str], str]:
    """Для предразвёртывания: снимок, который процедура сделала бы СЕЙЧАС, как CTE.

    Текст берётся из самой процедуры в DDL (объявление слота, список колонок INSERT, выражения
    SELECT и FROM), подставляются только now_ts → CURRENT_TIMESTAMP() и run_trigger → 'predeploy'.
    """
    import re
    text = BASIS_DDL.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"DECLARE slot STRING DEFAULT (\(.*?\n  \));\n  INSERT INTO `" + re.escape(PROJECT)
                         + r"\.(\w+)\.(\w+)`\n\s*\((.*?)\)\n  SELECT (.*?)\n  FROM (.*?)\n  WHERE NOT EXISTS", text, re.S):
        slot_expr, ds, table, cols, items, frm = m.groups()
        cols = [c.strip() for c in cols.replace("\n", " ").split(",")]
        items = _split_top_level(items)
        assert len(cols) == len(items), (table, len(cols), len(items))

        def sub(x: str) -> str:
            x = re.sub(r"\bnow_ts\b", "CURRENT_TIMESTAMP()", x)
            x = re.sub(r"\brun_trigger\b", "'predeploy'", x)
            return re.sub(r"\bslot\b", "(" + re.sub(r"\bnow_ts\b", "CURRENT_TIMESTAMP()", slot_expr) + ")", x)
        select = ",\n    ".join(f"{sub(i)} AS {c}" for i, c in zip(items, cols))
        out[(ds, table)] = f"SELECT\n    {select}\n  FROM {frm}"
    assert set(out) == set(NEW_TABLES), sorted(out)
    return out


def render_predeploy_file(text: str) -> str:
    """Проверки против живых RAW и канонических FE, с вью слоя из Git и виртуальным снимком."""
    import re
    snaps = virtual_snapshot_ctes()
    rendered = base.render_predeploy_file(text, OBJECTS)
    parts = re.split(r"(?m)^(?=-- @check )", rendered)
    out = []
    for part in parts:
        used = [k for k in NEW_TABLES if f"`{PROJECT}.{k[0]}.{k[1]}`" in part]
        for k in used:
            part = part.replace(f"`{PROJECT}.{k[0]}.{k[1]}`", f"vsnap__{k[0]}__{k[1]}")
        if used and part.startswith("-- @check "):
            head, _, stmt = part.partition("\nWITH\n") if "\nWITH\n" in part else (None, None, None)
            ctes = [f"vsnap__{k[0]}__{k[1]} AS (\n  {snaps[k]}\n)" for k in used]
            if head is not None:
                part = head + "\nWITH\n" + ",\n".join(ctes) + ",\n" + stmt
            else:
                lines = part.splitlines(keepends=True)
                hdr = []
                while lines and lines[0].startswith("--"):
                    hdr.append(lines.pop(0))
                part = "".join(hdr) + base._merge_with(ctes, "".join(lines)) + ";\n\n"
        out.append(part)
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
