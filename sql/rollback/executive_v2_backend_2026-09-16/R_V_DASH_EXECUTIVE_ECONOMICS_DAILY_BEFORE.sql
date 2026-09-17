-- ОТКАТ Executive V2 backend: определение ДО изменений, снято из production 2026-09-16
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` AS
WITH
-- Суточный агрегат Product COGS. Fail-closed выполняется ЗДЕСЬ, до join:
-- достаточно одной неразрешённой строки витрины, чтобы суточная величина
-- стала неизвестной. Счётчики выводятся всегда — они делают ноль доказуемым.
cogs_day AS (
  SELECT
    day,
    COUNT(*)                                                                   AS cogs_rows,
    COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE'))      AS cogs_unresolved_rows,
    SUM(buyouts_qty)                                                           AS product_cogs_units,
    SUM(reversal_events)                                                       AS product_cogs_reversal_events,
    SUM(reversal_resolved_events)                                              AS product_cogs_reversal_resolved_events,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(net_product_cogs_operational_rub),      NULL)                       AS product_cogs_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_operational_rub),          NULL)                       AS product_cogs_gross_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_reversal_operational_rub), NULL)                       AS product_cogs_reversal_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`
  GROUP BY day
)

SELECT
  f.day,

  -- ── PASS-THROUGH Stage 3.1C PR2. Значения не пересчитываются. ──
  f.period_result_eligible,
  f.contribution_pre_cogs_rub,
  f.sales_revenue_seller_base_rub,
  f.period_result_pre_cogs_corrected_rub,
  f.revenue_base_period_result_rub,

  -- ── PRODUCT COGS (operational). NULL = неизвестно, 0 = доказуемо нет событий ──
  d.product_cogs_rub,
  d.product_cogs_gross_rub,
  d.product_cogs_reversal_rub,
  IFNULL(d.product_cogs_units, 0)                          AS product_cogs_units,
  IFNULL(d.cogs_rows, 0)                                   AS cogs_rows,
  IFNULL(d.cogs_unresolved_rows, 0)                        AS cogs_unresolved_rows,
  IFNULL(d.product_cogs_reversal_events, 0)                AS product_cogs_reversal_events,
  IFNULL(d.product_cogs_reversal_resolved_events, 0)       AS product_cogs_reversal_resolved_events,

  -- ── ПОКРЫТИЕ. Сутки без строк витрины — НЕ покрыты (d.cogs_rows IS NULL),
  --    а не «ноль себестоимости». Fail-closed по умолчанию.
  (d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS product_cogs_covered,
  (f.period_result_eligible
   AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS after_product_cogs_eligible,

  -- ── ЭКОНОМИКА ПОСЛЕ СЕБЕСТОИМОСТИ ТОВАРА ──
  -- 🔴 Числитель и знаменатель живут на ОДНОЙ маске after_product_cogs_eligible.
  --    Разные маски — это дефект, который чинил Stage 3.1C PR2; повторять нельзя.
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.period_result_pre_cogs_corrected_rub - d.product_cogs_rub,
     NULL)                                                 AS period_result_after_product_cogs_rub,
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.revenue_base_period_result_rub,
     NULL)                                                 AS revenue_base_after_product_cogs_rub,

  'AFTER_PRODUCT_COGS'                                     AS economics_basis,
  'Результат после SKU-level и account-level расходов WB и после себестоимости товара (Product COGS). НЕ ВКЛЮЧЕНЫ: fulfilment/FF costs, OPEX, ЗП, аренда, банковские расходы и налог. Это НЕ чистая прибыль и НЕ валовая маржа. Product COGS взят по OPERATIONAL-серии (единицы выкупа), возврат сторнирует себестоимость исходной продажи.'
                                                           AS economics_note,
  CURRENT_TIMESTAMP()                                      AS generated_at

FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` f
LEFT JOIN cogs_day d USING (day);
