-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLAN_TRAJECTORY_MONTHLY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Помесячная траектория физического запаса по плану продаж:
--   closing(m) = opening(m) + eligible_inbound(m) − planned_physical(m),  opening(m+1) = closing(m)
--   opening(текущий месяц) = позиция запаса на дату среза (V_SKU_INVENTORY_TARGET_CURRENT ←
--   V_SKU_SELL_THROUGH_CURRENT); planned_physical — V_PLAN_PHYSICAL_MONTHLY (текущий месяц —
--   остаток после заказов MTD); eligible_inbound — только партии V_INBOUND_LOT_CURRENT с
--   in_base_trajectory. Отрицательный остаток не обрезается: shortfall = MAX(−closing, 0) —
--   это COMPONENT_SHORTFALL (разбивка «одиночные / в наборах» и список наборов рядом), а не
--   повод урезать план.
--
-- Две основы (trajectory_basis), не смешиваются:
--   APPROVED_PLAN     — бизнес-траектория: в каждом месяце действует утверждённая версия, чьё окно
--                       покрывает месяц (V_PLAN_VERSION_STATUS). Нет утверждения — NO_APPROVED_PLAN,
--                       остатки не считаются; запас не FRESH — INVENTORY_STALE, остатки NULL.
--   VERSION_SCENARIO  — сценарий каждой версии (модельной, предложенной, черновика) на её же
--                       горизонте. На несвежем запасе считается, но помечен
--                       SCENARIO_ON_STALE_INVENTORY. Это не план и не прогноз.
-- Рядом — сравнение без влияния на остатки: наблюдаемый темп 30 дней × дни периода и требуемый
-- темп к дате «срок годности − буфер C1» (V_SKU_INVENTORY_TARGET_CURRENT, NULL при STALE), и
-- остаток на дату sell-by (линейно внутри месяца) для EXPIRY_PRESSURE.
--
-- План запроса: каждое представление читается один раз и сворачивается в массивы.
-- Грейн: trajectory_basis × plan_version (для APPROVED_PLAN — 'APPROVED') × internal_sku × month.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
OPTIONS (description = "PR-PLAN-1. Помесячная траектория физического запаса: начало + подтверждённые поступления − плановые продажи в физ. единицах (BOM версии) = конец, нехватка без обрезки. Основы: утверждённый план (действующая версия месяца) и сценарий каждой версии. Несвежий запас: утверждённая траектория не считается, сценарий помечен. Сравнение с наблюдаемым и требуемым темпом, остаток к дате sell-by.")
AS
WITH tgt AS (
  -- Одно чтение целей: запас, свежесть, скорость 30 дней и даты сроков по физическому SKU.
  SELECT
    internal_sku,
    ANY_VALUE(product_name) AS product_name,
    ANY_VALUE(inventory_as_of_date) AS inventory_as_of_date,
    ANY_VALUE(inventory_freshness_status) AS inventory_freshness_status,
    ANY_VALUE(inventory_freshness_reason) AS inventory_freshness_reason,
    ANY_VALUE(inventory_position_units) AS inventory_position_units,
    ANY_VALUE(sales_as_of) AS sales_as_of,
    ANY_VALUE(current_units_per_day) AS observed_units_per_day_30d,
    ANY_VALUE(current_velocity_quality) AS observed_velocity_quality_30d,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', target_date, NULL)) AS sell_by_date,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', target_status, NULL)) AS sell_by_target_status,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', required_units_per_day, NULL)) AS required_units_per_day_to_sell_by,
    MAX(IF(target_type = 'EXPIRY_DATE', target_date, NULL)) AS expiry_date
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`
  GROUP BY internal_sku
),
phys AS (
  SELECT plan_version, component_sku,
    ARRAY_AGG(STRUCT(month, planned_units_remaining, planned_units_full_month, standalone_units_remaining,
                     via_bundle_units_remaining, wb_units_remaining, ozon_units_remaining, bundle_breakdown_remaining)) AS months
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY`
  WHERE component_sku IS NOT NULL
  GROUP BY plan_version, component_sku
),
inb AS (
  SELECT internal_sku,
    ARRAY_AGG(STRUCT(trajectory_month AS month, trajectory_units AS units, inbound_id)) AS lots
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT`
  WHERE in_base_trajectory
  GROUP BY internal_sku
),
basis AS (
  -- Одно чтение статусов: список версий-сценариев и окна утверждённых версий.
  SELECT
    ARRAY_AGG(STRUCT(plan_version, plan_kind, lifecycle_status, integrity_ok, horizon_to, created_at)) AS versions,
    ARRAY_AGG(IF(lifecycle_status IN ('APPROVED', 'SUPERSEDED', 'REVOKED') AND effective_from_month IS NOT NULL
                 AND effective_to_month_exclusive > effective_from_month,
                 STRUCT(plan_version, lifecycle_status, effective_from_month AS from_month, effective_to_month_exclusive AS to_month),
                 NULL) IGNORE NULLS) AS windows
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
),
b AS (
  -- Основы: сценарий каждой версии + одна утверждённая траектория (окна несёт каждая строка).
  SELECT x.*
  FROM basis,
  UNNEST(ARRAY_CONCAT(
    ARRAY(SELECT AS STRUCT 'VERSION_SCENARIO' AS trajectory_basis, v.plan_version, v.plan_kind, v.lifecycle_status,
                 v.integrity_ok, v.created_at, DATE_TRUNC(v.horizon_to, MONTH) AS last_month, basis.windows AS windows
          FROM UNNEST(basis.versions) v),
    [STRUCT('APPROVED_PLAN' AS trajectory_basis, 'APPROVED' AS plan_version, CAST(NULL AS STRING) AS plan_kind,
            CAST(NULL AS STRING) AS lifecycle_status, TRUE AS integrity_ok, CAST(NULL AS TIMESTAMP) AS created_at,
            (SELECT MAX(DATE_TRUNC(v.horizon_to, MONTH)) FROM UNNEST(basis.versions) v) AS last_month,
            basis.windows AS windows)])) x
),
g AS (
  SELECT
    b.trajectory_basis, b.plan_version, b.plan_kind, b.lifecycle_status AS version_lifecycle_status, b.integrity_ok,
    b.created_at AS plan_created_at,
    t.*,
    mo AS month,
    IF(b.trajectory_basis = 'APPROVED_PLAN',
       (SELECT AS STRUCT w.plan_version, w.lifecycle_status FROM UNNEST(b.windows) w
        WHERE mo >= w.from_month AND mo < w.to_month ORDER BY w.from_month DESC LIMIT 1),
       STRUCT(b.plan_version AS plan_version, b.lifecycle_status AS lifecycle_status)) AS src
  FROM b
  CROSS JOIN tgt t
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(DATE_TRUNC(t.inventory_as_of_date, MONTH),
                                        GREATEST(DATE_TRUNC(t.inventory_as_of_date, MONTH),
                                                 IFNULL(b.last_month, DATE_TRUNC(t.inventory_as_of_date, MONTH))),
                                        INTERVAL 1 MONTH)) AS mo
),
r AS (
  SELECT
    g.*,
    (SELECT AS STRUCT m.* FROM UNNEST(p.months) m WHERE m.month = g.month) AS pm,
    (SELECT IFNULL(SUM(l.units), 0) FROM UNNEST(i.lots) l WHERE l.month = g.month) AS eligible_inbound_units,
    (SELECT STRING_AGG(l.inbound_id, ', ' ORDER BY l.inbound_id) FROM UNNEST(i.lots) l WHERE l.month = g.month) AS eligible_inbound_ids,
    GREATEST(g.month, DATE_ADD(g.sales_as_of, INTERVAL 1 DAY)) AS period_start,
    LAST_DAY(g.month) AS period_end
  FROM g
  LEFT JOIN phys p ON p.plan_version = g.src.plan_version AND p.component_sku = g.internal_sku
  LEFT JOIN inb i ON i.internal_sku = g.internal_sku
),
c AS (
  SELECT
    r.*,
    IF(r.src.plan_version IS NULL, NULL, IFNULL(r.pm.planned_units_remaining, 0)) AS planned_units,
    LOGICAL_AND(r.src.plan_version IS NOT NULL) OVER w AS plan_covered_to_date,
    SUM(r.eligible_inbound_units - IFNULL(r.pm.planned_units_remaining, 0)) OVER w AS net_change_to_date
  FROM r
  WINDOW w AS (PARTITION BY r.trajectory_basis, r.plan_version, r.internal_sku ORDER BY r.month
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),
s AS (
  SELECT
    c.*,
    CASE
      WHEN c.inventory_position_units IS NULL THEN 'SKU_NOT_IN_INVENTORY'
      WHEN c.trajectory_basis = 'APPROVED_PLAN' AND c.inventory_freshness_status != 'FRESH' THEN 'INVENTORY_STALE'
      WHEN c.trajectory_basis = 'APPROVED_PLAN' AND NOT c.plan_covered_to_date THEN 'NO_APPROVED_PLAN'
      WHEN c.trajectory_basis = 'APPROVED_PLAN' THEN 'COMPUTED'
      WHEN NOT c.integrity_ok THEN 'INTEGRITY_BROKEN'
      WHEN c.inventory_freshness_status != 'FRESH' THEN 'SCENARIO_ON_STALE_INVENTORY'
      ELSE 'SCENARIO_COMPUTED'
    END AS trajectory_status
  FROM c
),
o AS (
  SELECT
    s.*,
    s.trajectory_status IN ('COMPUTED', 'SCENARIO_COMPUTED', 'SCENARIO_ON_STALE_INVENTORY') AS numbers_shown,
    s.inventory_position_units + s.net_change_to_date AS closing_raw,
    DATE_DIFF(s.period_end, s.period_start, DAY) + 1 AS period_days,
    CASE
      WHEN s.sell_by_date IS NULL OR s.sell_by_date < s.period_start OR s.sell_by_date > s.period_end THEN NULL
      ELSE DATE_DIFF(s.sell_by_date, s.period_start, DAY)
    END AS days_before_sell_by_in_period
  FROM s
)
SELECT
  trajectory_basis,
  plan_version,
  plan_kind,
  version_lifecycle_status,
  plan_created_at,
  src.plan_version AS plan_source_version,
  src.lifecycle_status AS plan_source_lifecycle_status,
  internal_sku,
  product_name,
  month,
  period_start,
  period_end,
  period_days,
  EXTRACT(DAY FROM LAST_DAY(month)) AS days_in_month,
  month = DATE_TRUNC(inventory_as_of_date, MONTH) AS is_current_month,
  trajectory_status,
  IF(trajectory_basis = 'APPROVED_PLAN', 'BUSINESS_PLAN', 'SCENARIO_NOT_A_PLAN') AS trajectory_meaning,
  inventory_as_of_date,
  inventory_freshness_status,
  inventory_freshness_reason,
  sales_as_of,
  IF(numbers_shown, closing_raw - eligible_inbound_units + planned_units, NULL) AS opening_units,
  eligible_inbound_units,
  eligible_inbound_ids,
  planned_units AS planned_physical_units,
  pm.planned_units_full_month AS planned_physical_units_full_month,
  pm.standalone_units_remaining AS planned_standalone_units,
  pm.via_bundle_units_remaining AS planned_via_bundle_units,
  pm.wb_units_remaining AS planned_wb_units,
  pm.ozon_units_remaining AS planned_ozon_units,
  pm.bundle_breakdown_remaining AS planned_bundle_breakdown,
  IF(numbers_shown, closing_raw, NULL) AS closing_units,
  IF(numbers_shown, GREATEST(-closing_raw, 0), NULL) AS shortfall_units,
  IF(numbers_shown AND closing_raw < 0, 'COMPONENT_SHORTFALL', NULL) AS shortfall_status,
  observed_units_per_day_30d,
  observed_velocity_quality_30d,
  observed_units_per_day_30d * period_days AS observed_run_rate_units,
  sell_by_date,
  expiry_date,
  sell_by_target_status,
  required_units_per_day_to_sell_by,
  required_units_per_day_to_sell_by
    * GREATEST(LEAST(DATE_DIFF(LEAST(period_end, DATE_SUB(sell_by_date, INTERVAL 1 DAY)), period_start, DAY) + 1, period_days), 0)
    AS required_units_to_sell_by,
  IF(numbers_shown AND days_before_sell_by_in_period IS NOT NULL,
     (closing_raw + planned_units) - planned_units * days_before_sell_by_in_period / period_days,
     NULL) AS projected_units_at_sell_by,
  IF(days_before_sell_by_in_period IS NOT NULL, 'LINEAR_WITHIN_MONTH_INBOUND_AT_MONTH_START', NULL) AS sell_by_projection_method
FROM o
