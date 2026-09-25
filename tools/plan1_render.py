#!/usr/bin/env python3
"""PR-PLAN-1 — регрессионные сценарии плана продаж и траектории запаса на фикстурах.

Механизм PR-PROMO-2/3/4 (tools/promo_canonical_render.py, tools/promo_inventory_render.py): тела
представлений берутся из Git дословно и подставляются как CTE; каждый внешний объект и каждая
таблица PR-PLAN-1 заменяется типизированной фикстурой своего сценария. Хеш содержимого версий
в фикстурах считается в Python тем же каноном, что в V_PLAN_VERSION_STATUS и процедурах, — так
сценарии заодно проверяют, что канон воспроизводим вне BigQuery.

Покрытие §17 спецификации — SPEC_CHECKS ниже; тест проверяет полноту карты.

usage:
  python tools/plan1_render.py fixtures [--out <path>]
  python tools/plan1_render.py run fixtures --project <P> --token-command "…"
  python tools/plan1_render.py run predeploy <checks.sql> --project <P> --token-command "…"
  python tools/plan1_render.py run live <checks.sql> --project <P> --token-command "…"
"""
from __future__ import annotations

import calendar
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_render as base  # noqa: E402
import promo_inventory_render as inv  # noqa: E402

TODAY, NOW, A, CUR = inv.TODAY, inv.NOW, inv.A, inv.CUR_MONTH
FEB_NONLEAP, FEB_LEAP, M30, M31 = inv.FEB_NONLEAP, inv.FEB_LEAP, inv.M30, inv.M31
world, mapped, history, phys, batch, pm, chmap = inv.world, inv.mapped, inv.history, inv.phys, inv.batch, inv.pm, inv.chmap
plan_tables, plan_sha, actual, near, ref = inv.plan_tables, inv.plan_sha, inv.actual, inv.near, inv.ref


def add_months(m: date, n: int) -> date:
    y, mo = divmod(m.month - 1 + n, 12)
    return date(m.year + y, mo + 1, 1)


M1, M2, M3 = add_months(CUR, 1), add_months(CUR, 2), add_months(CUR, 3)
PREV = add_months(CUR, -1)

ST, AP, LN, PH, IN, TJ, EX, OV, HD = ("V_PLAN_VERSION_STATUS", "V_SALES_PLAN_APPROVED", "V_PLAN_LINE_MONTHLY_ALL",
                                      "V_PLAN_PHYSICAL_MONTHLY", "V_INBOUND_LOT_CURRENT", "V_PLAN_TRAJECTORY_MONTHLY",
                                      "V_PLANNING_EXCEPTIONS", "V_PLANNING_SKU_OVERVIEW", "V_PLANNING_HEADER")


def one(view: str, where: str, col: str) -> str:
    return f"(SELECT ANY_VALUE({col}) FROM {ref(view)} WHERE {where})"


def cnt(view: str, where: str = "TRUE") -> str:
    return f"(SELECT COUNT(*) FROM {ref(view)} WHERE {where})"


def total(view: str, where: str, col: str) -> str:
    return f"(SELECT SUM({col}) FROM {ref(view)} WHERE {where})"


def pv(v: str) -> str:
    return f"plan_version = '{v}'"


def lot(inbound_id: str, sku: str, qty: int, state: str, *, eta: date | None = None, eta_status="UNKNOWN", blocker=None,
        received: date | None = None, received_qty=None, hours_ago=1, evidence="OWNER_FACT") -> dict:
    return dict(inbound_id=inbound_id, internal_sku=sku, quantity=qty, lot_state=state, blocker=blocker,
                eta_date=eta.isoformat() if eta else None, eta_status=eta_status,
                received_date=received.isoformat() if received else None, received_quantity=received_qty,
                evidence_class=evidence, evidence_ref="fixture", recorded_at=inv.ts(NOW - timedelta(hours=hours_ago)),
                recorded_by="fixture")


SCENARIOS: list[tuple[str, str, dict, list[tuple[str, str]]]] = []


def scenario(sid: str, title: str, data: dict, assertions: list[tuple[str, str]]):
    SCENARIOS.append((sid, title, data, assertions))


# ─────────────────────────────────────────────────────────────── PX10 · жизненный цикл и хеш
S1 = "EVT-FX-P1"
L_A = [(CUR, "WB", S1, 10), (M1, "WB", S1, 11), (M2, "WB", S1, 12), (M3, "WB", S1, 13)]
L_B = [(CUR, "WB", S1, 20), (M1, "WB", S1, 21), (M2, "WB", S1, 22), (M3, "WB", S1, 23)]
END = date(M3.year, M3.month, calendar.monthrange(M3.year, M3.month)[1])
SHA_A = plan_sha(L_A, [])
scenario("PX10", "version lifecycle: exact-hash approval, MODEL not approvable, invalid events ignored, effective windows",
         world(**mapped(S1), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(S1, 3, ff=100), **plan_tables([
             dict(pv="VA", lines=L_A, end=END, events=[("PROPOSED", 9), ("APPROVED", 8, CUR)]),
             dict(pv="VB", kind="OWNER_AUTHORED", lines=L_B, end=END, events=[("PROPOSED", 7), ("APPROVED", 6, M2)]),
             dict(pv="VM", kind="MODEL_SCENARIO", legacy=True, lines=L_A, end=END,
                  events=[("PROPOSED", 7), ("APPROVED", 6, CUR)]),
             dict(pv="VH", lines=L_A, end=END, events=[("PROPOSED", 5), ("APPROVED", 4, CUR, "0" * 64)]),
             dict(pv="VT", lines=L_A, end=END, tamper=[(M3, "OZON", S1, 99)], events=[("PROPOSED", 5), ("APPROVED", 4, CUR)]),
             dict(pv="VN", lines=L_A, end=END, events=[("APPROVED", 4, CUR)]),
             dict(pv="VE", lines=L_A, end=END, start=PREV, events=[("PROPOSED", 5), ("APPROVED", 4, PREV)]),
             dict(pv="VW", lines=L_A, end=END, events=[("PROPOSED", 5), ("WITHDRAWN", 4), ("APPROVED", 3, CUR)]),
             dict(pv="VD", lines=L_A, end=END),
         ])), [
    ("hash_python_equals_bigquery", f"{one(ST, pv('VA'), 'recomputed_sha256')} = '{SHA_A}'"),
    ("header_hash_equals_recomputed", f"{one(ST, pv('VA'), 'content_sha256 = recomputed_sha256')}"),
    ("va_approved", f"{one(ST, pv('VA'), 'lifecycle_status')} = 'APPROVED'"),
    ("vb_approved_future_window", f"{one(ST, pv('VB'), 'lifecycle_status')} = 'APPROVED'"),
    ("va_window_cut_by_vb", f"{one(ST, pv('VA'), 'effective_to_month_exclusive')} = DATE '{M2.isoformat()}'"),
    ("approved_months_from_va_then_vb",
     f"{total(AP, 'TRUE', 'planned_cards')} = 10 + 11 + 22 + 23 AND {cnt(AP)} = 4"),
    ("approved_month_owner", f"{one(AP, f'month = DATE {chr(39)}{M2.isoformat()}{chr(39)}', 'plan_version')} = 'VB'"),
    ("model_is_model", f"{one(ST, pv('VM'), 'lifecycle_status')} = 'MODEL_SCENARIO'"),
    ("model_events_ignored", f"{one(ST, pv('VM'), 'events_ignored')} = 2 AND {one(ST, pv('VM'), 'ignored_events')} LIKE '%MODEL_NOT_APPROVABLE%'"),
    ("model_never_in_approved", f"{cnt(AP, pv('VM'))} = 0"),
    ("model_not_approvable_now", f"{one(ST, pv('VM'), 'approvable_now')} = FALSE"),
    ("hash_mismatch_ignored", f"{one(ST, pv('VH'), 'lifecycle_status')} = 'PROPOSED' AND {one(ST, pv('VH'), 'ignored_events')} LIKE '%HASH_MISMATCH%'"),
    ("hash_mismatch_still_approvable", f"{one(ST, pv('VH'), 'approvable_now')} = TRUE"),
    ("tampered_integrity_broken", f"{one(ST, pv('VT'), 'lifecycle_status')} = 'INTEGRITY_BROKEN' AND {cnt(AP, pv('VT'))} = 0"),
    ("tampered_hash_differs", f"{one(ST, pv('VT'), 'content_sha256 != recomputed_sha256')}"),
    ("approve_without_proposal_ignored", f"{one(ST, pv('VN'), 'lifecycle_status')} = 'DRAFT' AND {one(ST, pv('VN'), 'ignored_events')} LIKE '%NOT_PROPOSED_BEFORE%'"),
    ("backdated_effective_month_ignored", f"{one(ST, pv('VE'), 'lifecycle_status')} = 'PROPOSED' AND {one(ST, pv('VE'), 'ignored_events')} LIKE '%EFFECTIVE_MONTH_INVALID%'"),
    ("withdrawn_then_approve_ignored", f"{one(ST, pv('VW'), 'lifecycle_status')} = 'WITHDRAWN' AND {cnt(AP, pv('VW'))} = 0"),
    ("draft_not_approvable", f"{one(ST, pv('VD'), 'lifecycle_status')} = 'DRAFT' AND {one(ST, pv('VD'), 'approvable_now')} = FALSE"),
    ("approved_not_reapprovable", f"{one(ST, pv('VA'), 'approvable_now')} = FALSE"),
    ("immutable_content_counts", f"{one(ST, pv('VA'), 'line_rows_actual')} = 4 AND {one(ST, pv('VA'), 'line_count_declared')} = 4"),
])

# ─────────────────────────────────────────────────────────────── PX11 · замена и отзыв
L_R = [(CUR, "WB", S1, 30), (M1, "WB", S1, 31)]
scenario("PX11", "supersede and revoke: later approval replaces, revoke ends after current month, past kept", world(
    **mapped(S1), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(S1, 3, ff=100), **plan_tables([
        dict(pv="VA", lines=L_A, end=END, events=[("PROPOSED", 9), ("APPROVED", 8, CUR)]),
        dict(pv="VB", lines=L_B, end=END, events=[("PROPOSED", 7), ("APPROVED", 6, CUR)]),
        dict(pv="VR", lines=L_R, end=END, events=[("PROPOSED", 5), ("APPROVED", 4, CUR), ("REVOKED", 1)]),
    ])), [
    ("first_superseded", f"{one(ST, pv('VA'), 'lifecycle_status')} = 'SUPERSEDED'"),
    ("second_superseded_by_third", f"{one(ST, pv('VB'), 'lifecycle_status')} = 'SUPERSEDED'"),
    ("revoked", f"{one(ST, pv('VR'), 'lifecycle_status')} = 'REVOKED'"),
    ("revoke_keeps_current_month", f"{one(ST, pv('VR'), 'effective_to_month_exclusive')} = DATE '{M1.isoformat()}'"),
    ("only_current_month_approved", f"{cnt(AP)} = 1 AND {one(AP, 'TRUE', 'planned_cards')} = 30 AND {one(AP, 'TRUE', 'plan_version')} = 'VR'"),
    ("no_plan_after_revoke", f"{cnt(AP, f'month >= DATE {chr(39)}{M1.isoformat()}{chr(39)}')} = 0"),
])

# ─────────────────────────────────────────────────────────────── PX20 · BOM, MTD, поступления, траектория
KA, KB, KC, KK = "EVT-FX-A", "EVT-FX-B", "EVT-FX-C", "EVT-FX-K"
e = (A - CUR).days + 1 if A >= CUR else 0                     # дни MTD в текущем месяце (по sales_as_of)
P0A = max(100 - 2 * e, 0) + 30 + max(40 - e, 0)               # A: WB соло 2/день, OZON соло 0, набор K 1/день
P0B = 2 * max(40 - e, 0)
END2 = date(M2.year, M2.month, calendar.monthrange(M2.year, M2.month)[1])
V1_LINES = [(CUR, "WB", KA, 100), (CUR, "OZON", KA, 30), (CUR, "WB", KK, 40, "BUNDLE"),
            (M1, "WB", KA, 200), (M1, "OZON", KA, 50), (M1, "WB", KK, 60, "BUNDLE"), (M1, "OZON", KK, 10, "BUNDLE"),
            (M2, "WB", KA, 200), (M2, "WB", KK, 60, "BUNDLE")]
V1_BOM = [(KK, KA, 1), (KK, KB, 2)]
V2_BOM = [(KK, KA, 1), (KK, KB, 1)]
SELL_BY = M1 + timedelta(days=14)
days_m1 = calendar.monthrange(M1.year, M1.month)[1]
closing_a_cur = 1000 - P0A
proj_a_sell_by = closing_a_cur + 500 - 320 * 14 / days_m1
px20 = world(
    evetis_ref__REF_PRODUCT_MASTER=[pm(KA), pm(KB), pm(KC), pm(KK, True)],
    evetis_ref__REF_SKU_CHANNEL_MAP=[chmap(s_, "WB", f"9{i}") for i, s_ in enumerate((KA, KB, KC, KK))]
    + [chmap(s_, "OZON", f"8{i}") for i, s_ in enumerate((KA, KB, KC, KK))],
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(KA, 40, ff=1000, wb=0, oz=0) + history(KB, 40, ff=150, wb=0, oz=0)
    + history(KC, 40, ff=50, wb=0, oz=0),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(KA, -120, -1, 4) + phys(KB, -120, -1, 2) + phys(KC, -300, -200, 1),
    wb_mart__V_CT_ACTUAL_DAILY=actual(KA, -60, -1, 2) + actual(KK, -60, -1, 1, mode="BUNDLE"),
    evetis_ref__CT_EXPIRY_BATCH=[batch(KA, "B-A", (SELL_BY + timedelta(days=30)).isoformat()),
                                 batch(KB, "B-B", (TODAY + timedelta(days=900)).isoformat())],
    evetis_ref__INBOUND_LOT_EVENT=[
        lot("L1", KA, 500, "PRODUCED", eta=M1 + timedelta(days=9), eta_status="CONFIRMED"),
        lot("L2", KA, 300, "PRODUCED", blocker="AWAITING_PAYMENT"),
        lot("L3", KA, 200, "IN_PRODUCTION", eta=M1, eta_status="ESTIMATED"),
        lot("L4", KA, 700, "PLANNED", evidence="HYPOTHESIS"),
        lot("L5", KB, 80, "RECEIVED", received=TODAY, received_qty=80),
        lot("L6", KB, 90, "RECEIVED", received=TODAY - timedelta(days=5), received_qty=90),
        lot("L7", KB, 60, "ORDER_CONFIRMED", eta=M1, eta_status="CONFIRMED", hours_ago=5),
        lot("L7", KB, 60, "CANCELLED", hours_ago=1),
        lot("L8", KA, 100, "ORDER_CONFIRMED", eta=TODAY - timedelta(days=3), eta_status="CONFIRMED"),
    ],
    **plan_tables([
        dict(pv="V1", lines=V1_LINES, bom=V1_BOM, end=END2, events=[("PROPOSED", 5), ("APPROVED", 4, CUR)]),
        dict(pv="V2", kind="OWNER_AUTHORED", lines=V1_LINES, bom=V2_BOM, end=END2, created_hours_ago=2),
    ]))
TJA = "trajectory_basis = 'APPROVED_PLAN'"
SCN = "trajectory_basis = 'VERSION_SCENARIO'"
SCN_V1 = "trajectory_basis = 'VERSION_SCENARIO' AND plan_version = 'V1'"


def tj(sku_: str, m: date, basis: str = TJA) -> str:
    return f"{basis} AND internal_sku = '{sku_}' AND month = DATE '{m.isoformat()}'"


def ph(v: str, sku_: str, m: date) -> str:
    return f"plan_version = '{v}' AND component_sku = '{sku_}' AND month = DATE '{m.isoformat()}'"


scenario("PX20", "physical requirement via BOM basis, MTD, inbound eligibility, trajectory, shortfall, expiry pressure", px20, [
    ("mtd_subtracted_current_month_a", f"{one(PH, ph('V1', KA, CUR), 'planned_units_remaining')} = {P0A}"),
    ("mtd_subtracted_current_month_b_via_bundle", f"{one(PH, ph('V1', KB, CUR), 'planned_units_remaining')} = {P0B}"),
    ("bom_expansion_shared_component", f"{one(PH, ph('V1', KA, M1), 'planned_units_full_month')} = 320"
     f" AND {one(PH, ph('V1', KA, M1), 'standalone_units_full_month')} = 250 AND {one(PH, ph('V1', KA, M1), 'via_bundle_units_full_month')} = 70"),
    ("bom_qty_2", f"{one(PH, ph('V1', KB, M1), 'planned_units_full_month')} = 140"),
    ("bom_basis_is_per_version", f"{one(PH, ph('V2', KB, M1), 'planned_units_full_month')} = 70"),
    ("bundle_physical_reconciliation",
     f"{total(PH, f'plan_version = {chr(39)}V1{chr(39)} AND month = DATE {chr(39)}{M1.isoformat()}{chr(39)}', 'planned_units_full_month')} = 250 * 1 + 70 * 3"),
    ("channel_totals", f"{one(PH, ph('V1', KA, M1), 'wb_units_remaining')} = 260 AND {one(PH, ph('V1', KA, M1), 'ozon_units_remaining')} = 60"),
    ("bundle_breakdown_listed", f"{one(PH, ph('V1', KB, M1), 'bundle_breakdown_full')} = '{KK} 140'"),
    ("inbound_confirmed_eligible", f"{one(IN, 'inbound_id = \"L1\"', 'inclusion_status')} = 'ELIGIBLE_CONFIRMED'"
     f" AND {one(IN, 'inbound_id = \"L1\"', 'trajectory_month')} = DATE '{M1.isoformat()}'"),
    ("inbound_blocked_excluded", f"{one(IN, 'inbound_id = \"L2\"', 'inclusion_status')} = 'BLOCKED'"
     f" AND {one(IN, 'inbound_id = \"L2\"', 'in_base_trajectory')} = FALSE"),
    ("inbound_estimated_eta_excluded", f"{one(IN, 'inbound_id = \"L3\"', 'inclusion_status')} = 'ETA_NOT_CONFIRMED'"),
    ("inbound_hypothetical_excluded", f"{one(IN, 'inbound_id = \"L4\"', 'inclusion_status')} = 'EXCLUDED_HYPOTHETICAL'"),
    ("received_after_anchor_counted", f"{one(IN, 'inbound_id = \"L5\"', 'inclusion_status')} = 'RECEIVED_NOT_IN_POSITION'"
     f" AND {one(IN, 'inbound_id = \"L5\"', 'trajectory_units')} = 80"),
    ("received_before_anchor_not_double_counted", f"{one(IN, 'inbound_id = \"L6\"', 'inclusion_status')} = 'RECEIVED_IN_POSITION'"),
    ("cancelled_latest_event_wins", f"{one(IN, 'inbound_id = \"L7\"', 'inclusion_status')} = 'EXCLUDED_CANCELLED'"
     f" AND {one(IN, 'inbound_id = \"L7\"', 'events_total')} = 2"),
    ("overdue_eta_excluded", f"{one(IN, 'inbound_id = \"L8\"', 'inclusion_status')} = 'ETA_OVERDUE'"),
    ("opening_is_position", f"{one(TJ, tj(KA, CUR), 'opening_units')} = 1000 AND {one(TJ, tj(KA, CUR), 'trajectory_status')} = 'COMPUTED'"),
    ("closing_chain_with_inbound", near(one(TJ, tj(KA, M1), "closing_units"), closing_a_cur + 500 - 320, 1e-6)),
    ("only_eligible_inbound_in_month", f"{one(TJ, tj(KA, M1), 'eligible_inbound_units')} = 500"),
    ("received_inbound_in_current_month", f"{one(TJ, tj(KB, CUR), 'eligible_inbound_units')} = 80"),
    ("shortfall_not_clipped", near(one(TJ, tj(KB, M2), "closing_units"), 150 + 80 - P0B - 140 - 120, 1e-6)),
    ("shortfall_value", near(one(TJ, tj(KB, M2), "shortfall_units"), 30 + P0B, 1e-6)),
    ("component_shortfall_exception",
     f"{cnt(EX, f'exception_code = {chr(39)}COMPONENT_SHORTFALL{chr(39)} AND {TJA} AND internal_sku = {chr(39)}{KB}{chr(39)} AND severity = {chr(39)}BLOCKER{chr(39)} AND month = DATE {chr(39)}{M2.isoformat()}{chr(39)}')} = 1"),
    ("shortfall_breakdown_names_bundle",
     f"{one(EX, f'exception_code = {chr(39)}COMPONENT_SHORTFALL{chr(39)} AND {TJA} AND internal_sku = {chr(39)}{KB}{chr(39)}', 'detail')} LIKE '%{KK} 120%'"),
    ("plan_not_trimmed", f"{one(TJ, tj(KB, M2), 'planned_physical_units')} = 120"),
    ("zero_velocity_sku", f"{one(TJ, tj(KC, M1), 'observed_run_rate_units')} = 0 AND {one(TJ, tj(KC, M1), 'closing_units')} = 50"),
    ("missing_target_no_expiry_exception",
     f"{one(TJ, tj(KC, M1), 'sell_by_date')} IS NULL AND {cnt(EX, f'exception_code = {chr(39)}EXPIRY_PRESSURE{chr(39)} AND internal_sku = {chr(39)}{KC}{chr(39)}')} = 0"),
    ("expiry_pressure_projection", near(one(TJ, tj(KA, M1), "projected_units_at_sell_by"), proj_a_sell_by, 1e-6)),
    ("expiry_pressure_exception", f"{cnt(EX, f'exception_code = {chr(39)}EXPIRY_PRESSURE{chr(39)} AND {TJA} AND internal_sku = {chr(39)}{KA}{chr(39)}')} = 1"),
    ("observed_comparator_calendar_days", near(one(TJ, tj(KA, M1), "observed_run_rate_units"), 4.0 * days_m1, 1e-9)),
    ("inbound_exceptions", f"{cnt(EX, 'exception_code = \"INBOUND_BLOCKED\" AND inbound_id = \"L2\"')} = 1"
     f" AND {cnt(EX, 'exception_code = \"INBOUND_ETA_UNKNOWN\" AND inbound_id IN (\"L2\", \"L3\")')} = 2"
     f" AND {cnt(EX, 'exception_code = \"INBOUND_ETA_OVERDUE\" AND inbound_id = \"L8\"')} = 1"
     f" AND {cnt(EX, 'exception_code = \"INBOUND_HYPOTHETICAL\" AND inbound_id = \"L4\"')} = 1"),
    ("fresh_no_stale_or_no_plan", f"{cnt(EX, 'exception_code IN (\"INVENTORY_STALE\", \"NO_APPROVED_PLAN\")')} = 0"),
    ("header_shows_approved_version", f"{one(HD, 'TRUE', 'approved_plan_version')} = 'V1' AND {one(HD, 'TRUE', 'inventory_freshness_status')} = 'FRESH'"),
    ("header_shows_latest_proposal", f"{one(HD, 'TRUE', 'proposed_plan_version')} = 'V2' AND {one(HD, 'TRUE', 'proposed_approvable_now')} = FALSE"),
    ("overview_four_meanings", f"{one(OV, f'internal_sku = {chr(39)}{KB}{chr(39)}', 'approved_first_shortfall_month')} = DATE '{M2.isoformat()}'"
     f" AND {one(OV, f'internal_sku = {chr(39)}{KB}{chr(39)}', 'proposed_plan_version')} = 'V2'"),
    ("scenario_basis_labelled", f"{one(TJ, tj(KA, M1, SCN_V1), 'trajectory_status')} = 'SCENARIO_COMPUTED'"),
])

# ─────────────────────────────────────────────────────────────── PX21 · несвежий запас, календарь
KS = "EVT-FX-STALE"
END3 = date(FEB_LEAP.year, 3, 31) if FEB_LEAP > FEB_NONLEAP else date(FEB_NONLEAP.year, 3, 31)
V3_LINES = [(m, "WB", KS, 28) for m in (CUR, FEB_NONLEAP, FEB_LEAP, M30, M31)]
scenario("PX21", "stale inventory: no approved trajectory numbers, labelled scenario; real month lengths", world(
    **mapped(KS),
    evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(KS, 20, ff=500, wb=0, oz=0, anchor_age=10),
    wb_mart__V_CT_PHYSICAL_DAILY=phys(KS, -120, -1, 1),
    **plan_tables([dict(pv="V3", lines=V3_LINES, end=END3, events=[("PROPOSED", 5), ("APPROVED", 4, CUR)])])), [
    ("approved_numbers_null_when_stale",
     f"{cnt(TJ, f'{TJA} AND internal_sku = {chr(39)}{KS}{chr(39)} AND (opening_units IS NOT NULL OR closing_units IS NOT NULL OR shortfall_units IS NOT NULL)')} = 0"),
    ("approved_status_stale", f"{one(TJ, tj(KS, CUR), 'trajectory_status')} = 'INVENTORY_STALE'"),
    ("scenario_on_stale_labelled", f"{one(TJ, tj(KS, CUR, SCN), 'trajectory_status')} = 'SCENARIO_ON_STALE_INVENTORY'"
     f" AND {one(TJ, tj(KS, CUR, SCN), 'opening_units')} = 500"),
    ("stale_exception", f"{cnt(EX, f'exception_code = {chr(39)}INVENTORY_STALE{chr(39)} AND internal_sku = {chr(39)}{KS}{chr(39)}')} = 1"),
    ("feb_28", f"{one(TJ, tj(KS, FEB_NONLEAP), 'days_in_month')} = 28 AND {one(TJ, tj(KS, FEB_NONLEAP), 'period_days')} = 28"),
    ("feb_29", f"{one(TJ, tj(KS, FEB_LEAP), 'days_in_month')} = 29 AND {one(TJ, tj(KS, FEB_LEAP), 'period_days')} = 29"),
    ("month_30", f"{one(TJ, tj(KS, M30), 'period_days')} = 30"),
    ("month_31", f"{one(TJ, tj(KS, M31), 'period_days')} = 31"),
    ("partial_current_month", f"{one(TJ, tj(KS, CUR), 'period_days')} = "
     f"{(date(CUR.year, CUR.month, calendar.monthrange(CUR.year, CUR.month)[1]) - max(CUR, A + timedelta(days=1))).days + 1}"),
    ("header_stale", f"{one(HD, 'TRUE', 'inventory_freshness_status')} = 'STALE'"),
])

# ─────────────────────────────────────────────────────────────── PX22 · legacy-адаптер CT
KL = "EVT-FX-LEG"
LEG = [(CUR, "wb", KL, 10.5), (CUR, "WB", KL, 4.5), (M1, "OZON", KL, 7.25)]
LEG_SHA = plan_sha([(CUR, "WB", KL, 15.0), (M1, "OZON", KL, 7.25)], [])
scenario("PX22", "legacy CT plan is read through the adapter, hash-protected, never approvable", world(
    **mapped(KL), evetis_ref__CT_INVENTORY_SNAPSHOT_DAILY=history(KL, 3, ff=10), **plan_tables([
        dict(pv="SET-FX", kind="MODEL_SCENARIO", legacy=True, lines=LEG, end=END),
        dict(pv="SET-FX2", kind="MODEL_SCENARIO", legacy=True, source_ref="SET-FX2", lines=[(CUR, "WB", KL, 16.0)],
             sha=plan_sha([(CUR, "WB", KL, 15.0)], []), end=END, events=[("PROPOSED", 2), ("APPROVED", 1, CUR)]),
    ])), [
    ("legacy_rows_aggregated_and_uppercased", f"{one(LN, f'plan_version = {chr(39)}SET-FX{chr(39)} AND month = DATE {chr(39)}{CUR.isoformat()}{chr(39)}', 'planned_cards')} = 15"
     f" AND {one(LN, f'plan_version = {chr(39)}SET-FX{chr(39)} AND month = DATE {chr(39)}{CUR.isoformat()}{chr(39)}', 'marketplace')} = 'WB'"),
    ("legacy_not_copied", f"{cnt(LN, 'plan_version = \"SET-FX\"')} = 2"),
    ("legacy_hash_python_equals_bigquery", f"{one(ST, pv('SET-FX'), 'recomputed_sha256')} = '{LEG_SHA}'"),
    ("legacy_is_model", f"{one(ST, pv('SET-FX'), 'lifecycle_status')} = 'MODEL_SCENARIO'"),
    ("legacy_changed_rows_break_integrity", f"{one(ST, pv('SET-FX2'), 'lifecycle_status')} = 'INTEGRITY_BROKEN'"),
    ("legacy_never_approved", f"{cnt(AP)} = 0"),
    ("integrity_exception", f"{cnt(EX, 'exception_code = \"PLAN_INTEGRITY\" AND plan_version = \"SET-FX2\"')} = 1"),
])

# §17 спецификации → сценарии. Тест проверяет, что каждый пункт покрыт.
SPEC_CHECKS: dict[str, tuple[str, ...]] = {
    "immutability": ("PX10", "PX22"), "content_hash": ("PX10", "PX22"), "exact_hash_approval": ("PX10",),
    "model_cannot_approve": ("PX10", "PX22"), "effective_dating": ("PX10", "PX11"), "month_28_29_30_31": ("PX21",),
    "partial_months": ("PX21",), "mtd_subtraction": ("PX20",), "bom_expansion": ("PX20",), "shared_components": ("PX20",),
    "bom_change_not_mutating_history": ("PX20",), "component_shortfall": ("PX20",), "zero_velocity": ("PX20",),
    "missing_target": ("PX20",), "stale_inventory": ("PX21",), "inbound_qty_unknown_eta": ("PX20",),
    "hypothetical_excluded": ("PX20",), "confirmed_eta_included": ("PX20",), "received_not_double_counted": ("PX20",),
    "cancelled_excluded": ("PX20",), "legacy_set_hash": ("PX22",), "wb_ozon_channel_totals": ("PX20",),
    "bundle_physical_reconciliation": ("PX20",),
}

CHUNK = {"PX20": 3, "PX21": 3, "PX10": 5, "PX11": 6, "PX22": 4}


def render_fixture_checks() -> str:
    header = (
        "-- ============================================================================\n"
        "-- PR-PLAN-1 · регрессионные сценарии плана продаж и траектории запаса на фикстурах.\n"
        "-- СГЕНЕРИРОВАНО tools/plan1_render.py fixtures. НЕ РЕДАКТИРОВАТЬ.\n"
        f"-- Тела {len(inv.RENDER_OBJECTS)} представлений взяты из Git дословно; внешние объекты и таблицы PR-PLAN-1\n"
        "-- заменены типизированными фикстурами сценария. Ни одной таблицы не читается.\n"
        f"-- Время фикстур: NOW = {inv.ts(NOW)} (UTC). Сценариев: {len(SCENARIOS)}, утверждений: "
        f"{sum(len(a) for *_, a in SCENARIOS)}.\n"
        "-- ============================================================================\n"
    )
    blocks = []
    for sid, title, data, assertions in SCENARIOS:
        size = CHUNK.get(sid, 4)
        parts = [assertions[i:i + size] for i in range(0, len(assertions), size)]
        for n, part in enumerate(parts, 1):
            bid = sid if len(parts) == 1 else f"{sid}.{n}"
            rows = [(sid, name, expr) for name, expr in part]
            blocks.append(f"\n-- @check {bid}\n-- {title}\n{inv.render_block(rows, data)};\n")
    return header + "".join(blocks)


def main(argv: list[str]) -> int:
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
            text = inv.render_predeploy_file(Path(argv[2]).read_text(encoding="utf-8"))
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
