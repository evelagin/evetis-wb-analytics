-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3: два рекламных контракта Ozon рядом и не смешиваясь.
--   Биллинг магазина (власть для прибыли) — финансовые начисления Seller API: CPC {41, 75, 33, 54 по
--   CPC-кампаниям} по суткам услуги, CPO {54 по CPO-кампаниям} по дате списания, реклама магазина без
--   SKU {47, 96} по месяцу начисления.
--   Атрибуция SKU (для вклада SKU) — та же, что в FCT_OZON_SKU_PNL_DAILY: CPC из RAW_OZON_ADS_SKU_DAILY
--   (только SKU из карты) по дате статистики; CPO — сопоставленные строки V_OZON_ADS_CPO_ORDERS по
--   месяцу заказа (business_date).
--   Performance expense (RAW_OZON_ADS_EXPENSE_DAILY) — независимая сверка биллинга, в деньги не входит.
-- ads_adjustment_rub = атрибуция − биллинг (CPC + CPO + 47/96): «−» — биллинг больше атрибуции.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY`
OPTIONS (description = "Сверка рекламы Ozon для P&L магазина (D3), зерно = календарный месяц. Биллинг (власть для прибыли магазина) - финансовые начисления: cpc_billing_rub {41,75,33,54 CPC}, cpo_billing_rub {54 CPO}, store_ads_rub {47,96}. Атрибуция SKU - как в FCT_OZON_SKU_PNL_DAILY: cpc_attributed_rub по дате статистики, cpo_attributed_rub по месяцу заказа. Performance expense - независимая сверка. ads_adjustment_rub = атрибуция - биллинг; cpc_residual_rub и cpo_residual_rub раскрывают его. ads_attribution_days_missing - сутки с CPC-биллингом без SKU-статистики.")
AS
WITH
led AS (
  SELECT pnl_month m,
    -SUM(IF(pnl_column = 'L', amount_rub, 0)) cpc_billing_rub,
    -SUM(IF(pnl_column = 'M', amount_rub, 0)) cpo_billing_rub,
    -SUM(IF(pnl_column = 'N', amount_rub, 0)) store_ads_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  GROUP BY 1),
cpo_camp AS (
  SELECT DISTINCT campaign_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
  WHERE payment_type = 'CPO'),
perf AS (
  SELECT DATE_TRUNC(e.date, MONTH) m,
    SUM(IF(c.campaign_id IS NULL, e.expense_rub, 0)) perf_cpc_expense_rub,
    SUM(IF(c.campaign_id IS NOT NULL, e.expense_rub, 0)) perf_cpo_expense_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e
  LEFT JOIN cpo_camp c USING (campaign_id)
  GROUP BY 1),
skumap AS (
  SELECT DISTINCT marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'),
cpc_attr AS (
  SELECT DATE_TRUNC(a.date, MONTH) m, SUM(a.attributed_spend_rub) cpc_attributed_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN skumap s ON s.marketplace_sku = a.sku
  GROUP BY 1),
cpo_attr AS (
  SELECT DATE_TRUNC(business_date, MONTH) m, SUM(expense_rub) cpo_attributed_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
  WHERE ordered_mapping_status = 'MAPPED' AND campaign_status IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY')
  GROUP BY 1),
cpc_days AS (
  SELECT DISTINCT booking_date d
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  WHERE pnl_column = 'L' AND amount_rub < 0),
attr_days AS (
  SELECT DISTINCT date d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`),
gaps AS (
  SELECT DATE_TRUNC(c.d, MONTH) m, COUNT(*) ads_attribution_days_missing
  FROM cpc_days c LEFT JOIN attr_days a USING (d)
  WHERE a.d IS NULL
  GROUP BY 1),
months AS (
  SELECT m FROM led UNION DISTINCT SELECT m FROM perf UNION DISTINCT
  SELECT m FROM cpc_attr UNION DISTINCT SELECT m FROM cpo_attr)
SELECT
  months.m pnl_month,
  IFNULL(led.cpc_billing_rub, 0) cpc_billing_rub,
  IFNULL(led.cpo_billing_rub, 0) cpo_billing_rub,
  IFNULL(led.store_ads_rub, 0) store_ads_rub,
  IFNULL(perf.perf_cpc_expense_rub, 0) perf_cpc_expense_rub,
  IFNULL(perf.perf_cpo_expense_rub, 0) perf_cpo_expense_rub,
  IFNULL(led.cpc_billing_rub, 0) + IFNULL(led.cpo_billing_rub, 0)
    - IFNULL(perf.perf_cpc_expense_rub, 0) - IFNULL(perf.perf_cpo_expense_rub, 0) billing_vs_performance_rub,
  IFNULL(cpc_attr.cpc_attributed_rub, 0) cpc_attributed_rub,
  IFNULL(cpo_attr.cpo_attributed_rub, 0) cpo_attributed_rub,
  IFNULL(led.cpc_billing_rub, 0) - IFNULL(cpc_attr.cpc_attributed_rub, 0) cpc_residual_rub,
  IFNULL(led.cpo_billing_rub, 0) - IFNULL(cpo_attr.cpo_attributed_rub, 0) cpo_residual_rub,
  IFNULL(cpc_attr.cpc_attributed_rub, 0) + IFNULL(cpo_attr.cpo_attributed_rub, 0)
    - IFNULL(led.cpc_billing_rub, 0) - IFNULL(led.cpo_billing_rub, 0) - IFNULL(led.store_ads_rub, 0) ads_adjustment_rub,
  IFNULL(gaps.ads_attribution_days_missing, 0) ads_attribution_days_missing
FROM months
LEFT JOIN led USING (m)
LEFT JOIN perf USING (m)
LEFT JOIN cpc_attr USING (m)
LEFT JOIN cpo_attr USING (m)
LEFT JOIN gaps USING (m);
