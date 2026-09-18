WITH
ref1 AS (
  SELECT
    nm_id,
    ANY_VALUE(internal_sku)        AS internal_sku,
    ANY_VALUE(product_name_short)  AS product_name_short,
    ANY_VALUE(product_name_full)   AS product_name_full,
    ANY_VALUE(category)            AS category,
    ANY_VALUE(line)                AS line,
    ANY_VALUE(product_type)        AS product_type,
    ANY_VALUE(brand)               AS brand,
    ANY_VALUE(is_bundle)           AS is_bundle,
    ANY_VALUE(status)              AS sku_status,
    ANY_VALUE(active)              AS sku_active,
    ANY_VALUE(include_in_pnl)      AS include_in_pnl,
    ANY_VALUE(volume_ml)           AS volume_ml,
    COUNT(*)                       AS ref_rows_for_nm_id
  FROM `wb_raw.REF_SKU_MASTER`
  WHERE nm_id IS NOT NULL
  GROUP BY nm_id
)
SELECT
  m.day,
  m.nm_id,

  -- ── Измерения ──
  r.internal_sku,
  r.product_name_short,
  r.product_name_full,
  r.category,
  r.line,
  r.product_type,
  r.brand,
  r.is_bundle,
  r.sku_status,
  r.sku_active,
  r.include_in_pnl,
  r.volume_ml,
  IFNULL(r.ref_rows_for_nm_id, 0) AS ref_rows_for_nm_id,   -- счётчик строк справочника, НЕ метрика
  (r.nm_id IS NULL)               AS is_orphan,

  -- ── Покрытие: PASS-THROUGH из PR1 ──
  c.orders_covered,
  c.sales_covered,
  c.ads_covered,
  c.finance_covered,
  c.contribution_covered,
  c.finance_is_final,
  c.contains_provisional_finance,
  c.days_total,
  c.orders_uncovered_days,
  c.sales_uncovered_days,
  c.ads_uncovered_days,
  c.finance_uncovered_days,
  c.contribution_uncovered_days,
  c.provisional_finance_days,
  c.contribution_provisional_days,

  -- ── SALES ──
  IF(c.orders_covered, m.orders_qty,    NULL) AS orders_qty,
  IF(c.orders_covered, m.orders_rub,    NULL) AS orders_revenue_rub,
  IF(c.orders_covered, m.canceled_qty,  NULL) AS canceled_qty,
  IF(c.orders_covered, m.canceled_rub,  NULL) AS canceled_rub,
  -- Stage 1.10B: брутто-заказы. Контракт и обоснование — см. V_DASH_KPI_DAILY выше.
  IF(c.orders_covered, m.orders_qty + m.canceled_qty, NULL) AS orders_gross_qty,

  -- ── BUYOUTS ──
  IF(c.sales_covered, m.buyouts_qty,                NULL) AS buyouts_qty,
  IF(c.sales_covered, m.buyouts_rub,                NULL) AS sales_revenue_seller_base_rub,
  IF(c.sales_covered, m.returns_qty,                NULL) AS returns_qty,
  IF(c.sales_covered, m.returns_rub,                NULL) AS returns_rub,
  IF(c.sales_covered, m.sales_for_pay_operational,  NULL) AS sales_for_pay_operational_rub,

  -- ── ECONOMICS (уровень SKU; расходов уровня счёта здесь нет) ──
  IF(c.finance_covered, m.marketplace_fee_rub,          NULL) AS marketplace_fee_rub,
  IF(c.finance_covered, m.wb_reward_cost_positive,      NULL) AS wb_reward_rub,
  IF(c.finance_covered, m.logistics_cost_positive,      NULL) AS logistics_rub,
  IF(c.finance_covered, m.finance_for_pay_accounting,   NULL) AS net_settlement_rub,

  -- ── ADVERTISING: только атрибуция ──
  IF(c.ads_covered, m.ad_spend,                      NULL) AS ad_spend_attributed_rub,
  IF(c.ads_covered, m.views,                         NULL) AS views,
  IF(c.ads_covered, m.clicks,                        NULL) AS clicks,
  IF(c.ads_covered, m.ad_orders_raw,                 NULL) AS ad_orders_raw,
  IF(c.ads_covered, m.ads_revenue_raw_rub,           NULL) AS ads_revenue_raw_rub,
  IF(c.ads_covered, m.ads_revenue_dedup_estimate_rub, NULL) AS ads_revenue_dedup_estimate_rub,
  IF(c.ads_covered, m.ad_orders_dedup_estimate,      NULL) AS ad_orders_dedup_estimate,

  -- ── CONTRIBUTION, pre-COGS ──
  IF(c.contribution_covered, m.hybrid_day_contribution_pre_cogs,     NULL) AS contribution_pre_cogs_rub,
  IF(c.contribution_covered, m.settlement_day_contribution_pre_cogs, NULL) AS settlement_contribution_pre_cogs_rub,
  'PRE_COGS' AS economics_basis,
  'REF_COGS отсутствует: вклад до себестоимости, не прибыль и не маржа' AS economics_note,
  'storage / deduction / acceptance приходят от WB без привязки к SKU (is_sku_row = FALSE) и находятся только в V_DASH_KPI_DAILY'
             AS account_level_excluded_note,

  m.build_as_of_date,
  m.built_at AS mart_built_at,
  CURRENT_TIMESTAMP() AS generated_at

FROM `wb_mart.MART_SKU_DAILY` m
LEFT JOIN ref1 r                          ON r.nm_id = m.nm_id
LEFT JOIN `wb_mart.V_DASH_COVERAGE_DAILY` c ON c.day  = m.day