-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Помесячная траектория запаса физического SKU по календарным месяцам:
--   остаток на начало − продажи периода + пополнение = остаток на конец.
-- Две независимые основы (trajectory_basis), не смешиваются и не складываются:
--   TARGET_REQUIRED_RUN_RATE  — нейтральная раскладка требуемого темпа цели
--                               (V_SKU_INVENTORY_TARGET_CURRENT, статусы COMPUTED и
--                               CURRENT_VELOCITY_ZERO) по дням месяца. Это не план, а
--                               арифметика «сколько нужно в каждом месяце при ровном темпе».
--   APPROVED_SALES_PLAN       — план Control Tower, утверждённый владельцем
--                               (V_SALES_PLAN_MONTHLY_CURRENT), в физических единицах по BOM
--                               (обе площадки, наборы и одиночные карточки). Текущий месяц —
--                               остаток плана после факта; будущие — план целиком.
-- Пополнение НЕ моделируется: производство и закупки не выдумываются
-- (replenishment_status = REPLENISHMENT_NOT_MODELLED, replenishment_units = NULL).
-- closing_units_unconstrained может уйти ниже нуля: это нехватка позиции под план, а не
-- отрицательный запас (projected_shortfall_units).
--
-- Календарь: период месяца = пересечение месяца с горизонтом, дни — датами (28/29/30/31),
-- неполные первый и последний месяцы помечены. Месяц × 30 не используется.
-- Запас на начало — позиция последнего дня истории (V_SKU_SELL_THROUGH_CURRENT); если запас
-- не FRESH, план не раскладывается (trajectory_status = INVENTORY_STALE, числа NULL).
--
-- Грейн: internal_sku × trajectory_basis × basis_key × month.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`
OPTIONS (description = "PR-PROMO-4. Помесячная траектория запаса физического SKU по календарным месяцам: остаток на начало − продажи + пополнение (не моделируется) = остаток на конец. Основы: нейтральный требуемый темп цели или утверждённый владельцем план Control Tower. Настоящие дни месяцев, неполные месяцы помечены.")
AS
WITH tgt AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`
  WHERE target_status IN ('COMPUTED', 'CURRENT_VELOCITY_ZERO')
),
tgt_months AS (
  SELECT
    t.internal_sku,
    'TARGET_REQUIRED_RUN_RATE' AS trajectory_basis,
    t.target_key AS basis_key,
    t.target_type,
    t.target_date,
    t.target_ending_units,
    t.inventory_as_of_date,
    t.inventory_position_units,
    t.required_units_per_day,
    t.current_units_per_day,
    mo AS month,
    GREATEST(mo, t.inventory_as_of_date) AS period_start,
    LEAST(LAST_DAY(mo), DATE_SUB(t.target_date, INTERVAL 1 DAY)) AS period_end
  FROM tgt t
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(DATE_TRUNC(t.inventory_as_of_date, MONTH),
                                        DATE_TRUNC(DATE_SUB(t.target_date, INTERVAL 1 DAY), MONTH),
                                        INTERVAL 1 MONTH)) AS mo
),
tgt_rows AS (
  SELECT
    internal_sku, trajectory_basis, basis_key, target_type, target_date, target_ending_units,
    CAST(NULL AS STRING) AS plan_version,
    month, period_start, period_end,
    DATE_DIFF(period_end, period_start, DAY) + 1 AS period_days,
    EXTRACT(DAY FROM LAST_DAY(month)) AS days_in_month,
    inventory_as_of_date,
    'COMPUTED' AS trajectory_status,
    CAST(inventory_position_units - required_units_per_day * DATE_DIFF(period_start, inventory_as_of_date, DAY) AS FLOAT64)
      AS opening_units,
    required_units_per_day * (DATE_DIFF(period_end, period_start, DAY) + 1) AS sales_units,
    'NEUTRAL_REQUIRED_RUN_RATE_NOT_APPROVED_PLAN' AS sales_basis,
    current_units_per_day * (DATE_DIFF(period_end, period_start, DAY) + 1) AS current_velocity_units
  FROM tgt_months
),
plan_card AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`
  WHERE plan_approval_status = 'APPROVED'
),
bom AS (
  SELECT card_sku, component_sku, component_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`
),
plan_comp AS (
  SELECT
    b.component_sku AS internal_sku,
    p.plan_version,
    p.month,
    ANY_VALUE(p.month_position) AS month_position,
    MIN(p.plan_period_start) AS plan_period_start,
    MAX(p.plan_period_end) AS plan_period_end,
    ANY_VALUE(p.sales_as_of) AS sales_as_of,
    SUM(p.planned_cards * b.component_qty) AS planned_units
  FROM plan_card p
  JOIN bom b ON b.card_sku = p.internal_sku
  GROUP BY b.component_sku, p.plan_version, p.month
),
actual_comp AS (
  SELECT pc.internal_sku, pc.plan_version, pc.month,
    SUM(IF(d.d BETWEEN pc.plan_period_start AND LEAST(pc.sales_as_of, pc.plan_period_end), d.units_ordered, 0)) AS actual_units
  FROM plan_comp pc
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY` d
    ON d.internal_sku = pc.internal_sku AND d.d BETWEEN pc.plan_period_start AND pc.plan_period_end
  GROUP BY pc.internal_sku, pc.plan_version, pc.month
),
inv AS (
  SELECT internal_sku, inventory_as_of_date, inventory_position_units, inventory_freshness_status, reference_units_per_day
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
plan_sales AS (
  SELECT
    pc.internal_sku, pc.plan_version, pc.month, pc.month_position, pc.plan_period_start, pc.plan_period_end,
    i.inventory_as_of_date, i.inventory_position_units, i.inventory_freshness_status, i.reference_units_per_day,
    CASE pc.month_position
      WHEN 'PAST' THEN NULL
      WHEN 'CURRENT' THEN GREATEST(pc.planned_units - IFNULL(a.actual_units, 0), 0)
      ELSE pc.planned_units
    END AS sales_units,
    CASE pc.month_position
      WHEN 'CURRENT' THEN DATE_ADD(pc.sales_as_of, INTERVAL 1 DAY)
      ELSE pc.plan_period_start
    END AS period_start
  FROM plan_comp pc
  LEFT JOIN actual_comp a USING (internal_sku, plan_version, month)
  LEFT JOIN inv i ON i.internal_sku = pc.internal_sku
  WHERE pc.month_position IN ('CURRENT', 'FUTURE')
),
plan_rows AS (
  SELECT
    internal_sku,
    'APPROVED_SALES_PLAN' AS trajectory_basis,
    CONCAT('PLAN|', plan_version) AS basis_key,
    CAST(NULL AS STRING) AS target_type,
    CAST(NULL AS DATE) AS target_date,
    CAST(NULL AS INT64) AS target_ending_units,
    plan_version,
    month, period_start, plan_period_end AS period_end,
    DATE_DIFF(plan_period_end, period_start, DAY) + 1 AS period_days,
    EXTRACT(DAY FROM LAST_DAY(month)) AS days_in_month,
    inventory_as_of_date,
    CASE
      WHEN inventory_position_units IS NULL THEN 'SKU_NOT_IN_INVENTORY'
      WHEN inventory_freshness_status != 'FRESH' THEN 'INVENTORY_STALE'
      ELSE 'COMPUTED'
    END AS trajectory_status,
    IF(inventory_freshness_status = 'FRESH',
       inventory_position_units - IFNULL(SUM(sales_units) OVER (PARTITION BY internal_sku, plan_version ORDER BY month
                                                              ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0),
       NULL) AS opening_units,
    IF(inventory_freshness_status = 'FRESH', sales_units, NULL) AS sales_units,
    'APPROVED_SALES_PLAN_PHYSICAL_UNITS' AS sales_basis,
    IF(inventory_freshness_status = 'FRESH', reference_units_per_day * (DATE_DIFF(plan_period_end, period_start, DAY) + 1), NULL)
      AS current_velocity_units
  FROM plan_sales
),
u AS (
  SELECT * FROM tgt_rows
  UNION ALL
  SELECT * FROM plan_rows
)
SELECT
  internal_sku,
  trajectory_basis,
  basis_key,
  target_type,
  target_date,
  target_ending_units,
  plan_version,
  month,
  period_start,
  period_end,
  period_days,
  days_in_month,
  period_days < days_in_month AS is_partial_month,
  inventory_as_of_date,
  trajectory_status,
  opening_units,
  sales_units,
  sales_basis,
  CAST(NULL AS FLOAT64) AS replenishment_units,
  'REPLENISHMENT_NOT_MODELLED' AS replenishment_status,
  opening_units - sales_units AS closing_units_unconstrained,
  GREATEST(sales_units - opening_units, 0) AS projected_shortfall_units,
  current_velocity_units,
  current_velocity_units - sales_units AS current_velocity_gap_units
FROM u
