-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Помесячная траектория запаса физического SKU по календарным месяцам:
--   остаток на начало − продажи периода + пополнение = остаток на конец.
-- Основа одна (trajectory_basis):
--   TARGET_REQUIRED_RUN_RATE  — нейтральная раскладка требуемого темпа цели
--                               (V_SKU_INVENTORY_TARGET_CURRENT, статусы COMPUTED и
--                               CURRENT_VELOCITY_ZERO) по дням месяца. Это не план, а
--                               арифметика «сколько нужно в каждом месяце при ровном темпе».
-- С PR-PLAN-1 основа APPROVED_SALES_PLAN отсюда убрана: траектория по утверждённому плану,
-- с подтверждёнными поступлениями и базисом BOM версии — evetis_mart.V_PLAN_TRAJECTORY_MONTHLY.
-- Две траектории одного плана с разными правилами не ведутся.
-- Пополнение НЕ моделируется: производство и закупки не выдумываются
-- (replenishment_status = REPLENISHMENT_NOT_MODELLED, replenishment_units = NULL).
-- closing_units_unconstrained может уйти ниже нуля: это нехватка позиции под план, а не
-- отрицательный запас (projected_shortfall_units).
--
-- Календарь: период месяца = пересечение месяца с горизонтом, дни — датами (28/29/30/31),
-- неполные первый и последний месяцы помечены. Месяц × 30 не используется.
-- Запас на начало — позиция последнего дня истории (через V_SKU_INVENTORY_TARGET_CURRENT, где
-- цель при несвежем запасе не считается: статус INVENTORY_STALE, строк траектории нет).
--
-- Грейн: internal_sku × trajectory_basis × basis_key × month.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`
OPTIONS (description = "PR-PROMO-4. Помесячная траектория запаса физического SKU по календарным месяцам: остаток на начало − продажи + пополнение (не моделируется) = остаток на конец. Основа — нейтральный требуемый темп цели; траектория утверждённого плана — V_PLAN_TRAJECTORY_MONTHLY (PR-PLAN-1). Настоящие дни месяцев, неполные месяцы помечены.")
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
u AS (
  SELECT * FROM tgt_rows
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
