-- ============================================================================
-- PR-2 Phase C — форвардная экономика WB (PRE-TAX). Версия модели: WB_FE_V1.
--
-- ЭТОТ ФАЙЛ — АВТОРИТЕТНЫЙ ИСТОЧНИК. Тела всех объектов извлечены из
-- production через INFORMATION_SCHEMA и сверены по SHA-256 побайтово,
-- а не восстановлены по памяти. Хеши приведены над каждым объектом;
-- проверка воспроизводится скриптом sql/pricing/pr2_ddl_verify.sql.
--
-- Порядок развёртывания = порядок в файле (объекты зависят от предыдущих).
-- Чистое окружение, прогнав pr2_wb_tariffs.sql и затем этот файл, получает
-- полный экономический слой PR-2.
--
-- ── Доказанное тождество расчёта WB (38 698 строк, 97,2 % ±0,02 ₽) ──────────
--   for_pay = retail_price_withdisc_rub × (1 − commission_pct/100) − acquiring_fee
-- База выручки — цена ПРОДАВЦА. СПП финансирует WB и выплату не уменьшает.
--
-- ── Формулы (P — цена продавца; take = комиссия% + эквайринг%) ──────────────
--   contribution_before_ads_pre_tax = P − P×take − E[logistics] − COGS
--   break_even_before_ads_pre_tax   = (E[logistics] + COGS) / (1 − take)
--   break_even_with_ads(d)          = (E[logistics] + COGS) / (1 − take − d)
--   target_contribution_price(c)    = (E[logistics] + COGS + c) / (1 − take)
--   max_affordable_drr(P)           = contribution_before_ads(P) / P
--
-- ── Оценщики, фактически используемые production ───────────────────────────
--   Комиссия:   base = WB_TARIFF_API paidStorageKgvp (предмет SKU)
--               addon = REF_MARKETPLACE_COMMISSION_COMPONENT, действующая строка
--               effective = base + addon; сверяется с медианой факта за 45 сут
--   Эквайринг:  окно 45 суток, доля acquiring_fee / retail_price_withdisc_rub
--               BASE = p50 · CONSERVATIVE = p75 · STRESS = p90
--   Логистика:  окно 90 суток, БАЗА = Σлогистика / Σпродажи (нагрузка на
--               проданную единицу — включает невыкуп),
--               CONSERVATIVE = БАЗА × (p75/mean построчного распределения),
--               STRESS       = БАЗА × (p90/mean)
--
-- НЕ ВХОДЯТ намеренно: налог (tax_model_status = PRE_TAX), хранение, приёмка,
-- штрафы, удержания уровня кабинета, фулфилмент, OPEX.
-- ============================================================================

-- ── V_WB_TARIFFS_CURRENT ───────────────────────────────────────────────
-- SHA-256 тела: 99ac20833e5185766b3e6a58499e0e0a233adb42f5031bef86794b7e1aec722b
-- Длина тела: 618
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_TARIFFS_CURRENT` AS
SELECT tariff_kind, entity_key, entity_name, parent_key, parent_name, metric, value_num, value_raw, is_parsed, effective_next, effective_till_max, observed_at, observation_date, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, HOUR) AS tariff_age_hours, CASE WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, HOUR) <= 36 THEN 'FRESH' WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, HOUR) <= 96 THEN 'DELAYED' ELSE 'STALE' END AS tariff_freshness FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_TARIFFS` QUALIFY ROW_NUMBER() OVER (PARTITION BY tariff_kind, entity_key, metric ORDER BY observed_at DESC) = 1;

-- ── V_WB_SKU_COST_INPUTS ───────────────────────────────────────────────
-- SHA-256 тела: 17f2445b1f08f3edd5789e5ef060914b99715c3ee905a6fceb00f6f73268733e
-- Длина тела: 5462
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_COST_INPUTS` AS
WITH px AS (
  SELECT internal_sku, nm_id, seller_list_price, seller_discount_pct, seller_effective_price, wb_club_price, observed_at AS price_observed_at, freshness_status AS price_freshness
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_CURRENT` WHERE environment = 'prod'
),
sku AS (SELECT internal_sku, nm_id, wb_subject_id, wb_subject_name, is_bundle, product_name_short FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active),
cogs AS (SELECT internal_sku, product_cogs_rub, confidence AS cogs_confidence, effective_from AS cogs_effective_from, cost_basis AS cogs_basis FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` WHERE effective_to IS NULL OR effective_to >= CURRENT_DATE()),
comm_base AS (SELECT entity_key, entity_name, value_num AS base_commission_pct, observed_at AS commission_observed_at, tariff_freshness AS commission_tariff_freshness FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_TARIFFS_CURRENT` WHERE tariff_kind='COMMISSION' AND metric='paidStorageKgvp'),
addon AS (SELECT SUM(addon_pct) AS addon_pct, STRING_AGG(component) AS addon_components, MIN(effective_from) AS addon_effective_from, MIN(confidence) AS addon_confidence FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_MARKETPLACE_COMMISSION_COMPONENT` WHERE marketplace='WB' AND effective_from <= CURRENT_DATE() AND (effective_to IS NULL OR effective_to >= CURRENT_DATE())),
realized AS (
  SELECT ROUND(APPROX_QUANTILES(SAFE_CAST(commission_percent AS FLOAT64), 2)[OFFSET(1)], 4) AS realized_commission_pct,
         COUNT(*) AS realized_commission_n,
         COUNT(DISTINCT ROUND(SAFE_CAST(commission_percent AS FLOAT64),4)) AS realized_commission_distinct,
         MAX(_rr_date) AS realized_commission_last_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
  WHERE supplier_oper_name='Продажа' AND _rr_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 45 DAY)
),
acq AS (
  SELECT ROUND(APPROX_QUANTILES(acq_ratio,100)[OFFSET(50)]*100,4) AS acquiring_p50_pct,
         ROUND(APPROX_QUANTILES(acq_ratio,100)[OFFSET(75)]*100,4) AS acquiring_p75_pct,
         ROUND(APPROX_QUANTILES(acq_ratio,100)[OFFSET(90)]*100,4) AS acquiring_p90_pct,
         COUNT(*) AS acquiring_n
  FROM (SELECT SAFE_DIVIDE(SAFE_CAST(acquiring_fee AS FLOAT64), SAFE_CAST(retail_price_withdisc_rub AS FLOAT64)) AS acq_ratio
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
        WHERE supplier_oper_name='Продажа' AND _rr_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 45 DAY)
          AND SAFE_CAST(retail_price_withdisc_rub AS FLOAT64) > 0)
  WHERE acq_ratio IS NOT NULL
),
log_rows AS (SELECT internal_sku, logistics_amount FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` WHERE finance_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) AND internal_sku IS NOT NULL AND logistics_amount IS NOT NULL AND logistics_amount != 0),
sales_cnt AS (SELECT internal_sku, COUNT(*) AS sales_n FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` WHERE finance_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) AND internal_sku IS NOT NULL AND supplier_oper_name='Продажа' GROUP BY 1),
log_stat AS (
  SELECT internal_sku, COUNT(*) AS logistics_events_n, ROUND(SUM(logistics_amount),2) AS logistics_total_rub,
         ROUND(AVG(logistics_amount),2) AS logistics_row_mean_rub,
         ROUND(APPROX_QUANTILES(logistics_amount,100)[OFFSET(50)],2) AS logistics_row_p50_rub,
         ROUND(APPROX_QUANTILES(logistics_amount,100)[OFFSET(75)],2) AS logistics_row_p75_rub,
         ROUND(APPROX_QUANTILES(logistics_amount,100)[OFFSET(90)],2) AS logistics_row_p90_rub,
         ROUND(STDDEV(logistics_amount),2) AS logistics_row_sd_rub
  FROM log_rows GROUP BY 1
)
SELECT s.internal_sku, s.nm_id, s.product_name_short, s.is_bundle, s.wb_subject_id, s.wb_subject_name,
  px.seller_list_price, px.seller_discount_pct, px.seller_effective_price, px.price_observed_at, px.price_freshness,
  c.product_cogs_rub AS cogs_rub, c.cogs_confidence, c.cogs_effective_from, c.cogs_basis,
  cb.base_commission_pct, cb.commission_observed_at, cb.commission_tariff_freshness,
  IFNULL(a.addon_pct, 0) AS commission_addon_pct, a.addon_components, a.addon_effective_from, a.addon_confidence,
  cb.base_commission_pct + IFNULL(a.addon_pct, 0) AS effective_commission_pct,
  r.realized_commission_pct, r.realized_commission_n, r.realized_commission_distinct, r.realized_commission_last_date,
  ROUND(ABS(cb.base_commission_pct + IFNULL(a.addon_pct,0) - r.realized_commission_pct), 4) AS commission_abs_diff_pp,
  q.acquiring_p50_pct, q.acquiring_p75_pct, q.acquiring_p90_pct, q.acquiring_n,
  l.logistics_events_n, l.logistics_total_rub, l.logistics_row_mean_rub, l.logistics_row_p50_rub, l.logistics_row_p75_rub, l.logistics_row_p90_rub, l.logistics_row_sd_rub,
  sc.sales_n AS logistics_sales_n,
  ROUND(SAFE_DIVIDE(l.logistics_total_rub, sc.sales_n), 2) AS logistics_burden_per_sale_rub,
  ROUND(SAFE_DIVIDE(l.logistics_events_n, sc.sales_n), 4) AS logistics_events_per_sale,
  ROUND(SAFE_DIVIDE(l.logistics_row_sd_rub, l.logistics_row_mean_rub), 4) AS logistics_cv
FROM sku s
LEFT JOIN px USING (internal_sku)
LEFT JOIN cogs c USING (internal_sku)
LEFT JOIN comm_base cb ON cb.entity_key = s.wb_subject_id OR (s.wb_subject_id = '' AND cb.entity_name = s.wb_subject_name)
LEFT JOIN log_stat l USING (internal_sku)
LEFT JOIN sales_cnt sc USING (internal_sku)
CROSS JOIN addon a CROSS JOIN realized r CROSS JOIN acq q;

-- ── V_WB_SKU_FORWARD_ECONOMICS_CURRENT ───────────────────────────────────────────────
-- SHA-256 тела: d1de676090e8585ec02379b649710d1383d5728c6b29a22160f18804d50c524c
-- Длина тела: 5264
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` AS
WITH i AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_COST_INPUTS`),
p AS (
  SELECT i.*,
    SAFE_DIVIDE(logistics_row_p75_rub, logistics_row_mean_rub) AS p75_ratio,
    SAFE_DIVIDE(logistics_row_p90_rub, logistics_row_mean_rub) AS p90_ratio,
    CASE WHEN ABS(IFNULL(commission_abs_diff_pp, 99)) <= 0.05 THEN 'MATCHED'
         WHEN ABS(IFNULL(commission_abs_diff_pp, 99)) <= 0.50 THEN 'MINOR_DIVERGENCE'
         ELSE 'MISMATCH' END AS commission_reconciliation_status
  FROM i
),
s AS (
  SELECT p.*,
    logistics_burden_per_sale_rub AS logistics_base_rub,
    ROUND(logistics_burden_per_sale_rub * IFNULL(p75_ratio, 1), 2) AS logistics_conservative_rub,
    ROUND(logistics_burden_per_sale_rub * IFNULL(p90_ratio, 1), 2) AS logistics_stress_rub,
    (effective_commission_pct + acquiring_p50_pct) / 100 AS take_base,
    (effective_commission_pct + acquiring_p75_pct) / 100 AS take_conservative,
    (effective_commission_pct + acquiring_p90_pct) / 100 AS take_stress
  FROM p
),
e AS (
  SELECT s.*,
    ROUND(seller_effective_price * effective_commission_pct / 100, 2) AS commission_rub_at_current,
    ROUND(seller_effective_price * acquiring_p50_pct / 100, 2) AS acquiring_rub_at_current,
    ROUND(seller_effective_price - seller_effective_price * take_base - logistics_base_rub - cogs_rub, 2) AS contribution_before_ads_pre_tax_rub,
    CASE WHEN take_base < 1 THEN ROUND(SAFE_DIVIDE(logistics_base_rub + cogs_rub, 1 - take_base), 2) END AS break_even_before_ads_pre_tax_base,
    CASE WHEN take_conservative < 1 THEN ROUND(SAFE_DIVIDE(logistics_conservative_rub + cogs_rub, 1 - take_conservative), 2) END AS break_even_before_ads_pre_tax_conservative,
    CASE WHEN take_stress < 1 THEN ROUND(SAFE_DIVIDE(logistics_stress_rub + cogs_rub, 1 - take_stress), 2) END AS break_even_before_ads_pre_tax_stress
  FROM s
)
SELECT internal_sku, nm_id, product_name_short, is_bundle, wb_subject_name,
  seller_list_price, seller_discount_pct, seller_effective_price AS seller_effective_price_rub, price_observed_at, price_freshness,
  cogs_rub, cogs_confidence AS cogs_source, cogs_effective_from,
  base_commission_pct, commission_addon_pct, effective_commission_pct,
  'WB_TARIFF_API.paidStorageKgvp + REF_MARKETPLACE_COMMISSION_COMPONENT' AS commission_source,
  realized_commission_pct, commission_abs_diff_pp, commission_reconciliation_status, commission_tariff_freshness,
  acquiring_p50_pct, acquiring_p75_pct, acquiring_p90_pct, 'EMPIRICAL_45D' AS acquiring_model, acquiring_rub_at_current AS acquiring_expected_rub,
  logistics_base_rub AS logistics_expected_rub, logistics_conservative_rub AS logistics_p75_rub, logistics_stress_rub AS logistics_p90_rub,
  logistics_sales_n AS logistics_sample_size, logistics_events_per_sale, logistics_cv, 'EMPIRICAL_90D_BURDEN_PER_SALE' AS logistics_model,
  commission_rub_at_current,
  contribution_before_ads_pre_tax_rub,
  ROUND(SAFE_DIVIDE(contribution_before_ads_pre_tax_rub, seller_effective_price) * 100, 2) AS contribution_before_ads_pre_tax_pct,
  break_even_before_ads_pre_tax_base, break_even_before_ads_pre_tax_conservative, break_even_before_ads_pre_tax_stress,
  ROUND(seller_effective_price - break_even_before_ads_pre_tax_base, 2) AS margin_of_safety_base_rub,
  ROUND(SAFE_DIVIDE(seller_effective_price - break_even_before_ads_pre_tax_base, seller_effective_price) * 100, 2) AS margin_of_safety_base_pct,
  ROUND(SAFE_DIVIDE(contribution_before_ads_pre_tax_rub, seller_effective_price) * 100, 2) AS max_affordable_drr_pre_tax_pct,
  CASE
    WHEN cogs_rub IS NULL THEN 'BLOCKED'
    WHEN seller_effective_price IS NULL OR seller_effective_price <= 0 THEN 'BLOCKED'
    WHEN base_commission_pct IS NULL THEN 'BLOCKED'
    WHEN take_base >= 1 THEN 'BLOCKED'
    WHEN logistics_sample_size_null.x THEN 'BLOCKED'
    WHEN logistics_sales_n < 10 THEN 'LOW'
    WHEN commission_reconciliation_status = 'MISMATCH' THEN 'LOW'
    WHEN commission_reconciliation_status = 'MINOR_DIVERGENCE' OR IFNULL(logistics_cv,1) > 0.30 OR logistics_sales_n < 30 THEN 'MEDIUM'
    WHEN commission_tariff_freshness != 'FRESH' OR price_freshness NOT IN ('FRESH','DELAYED') THEN 'MEDIUM'
    ELSE 'HIGH' END AS economics_confidence,
  CASE
    WHEN cogs_rub IS NULL THEN 'COGS_MISSING'
    WHEN seller_effective_price IS NULL THEN 'PRICE_MISSING'
    WHEN base_commission_pct IS NULL THEN 'COMMISSION_TARIFF_MISSING'
    WHEN logistics_burden_per_sale_rub IS NULL THEN 'LOGISTICS_SAMPLE_MISSING'
    WHEN take_base >= 1 THEN 'TAKE_RATE_EXCEEDS_PRICE'
    ELSE NULL END AS blocked_reason,
  CASE
    WHEN cogs_rub IS NULL OR seller_effective_price IS NULL OR base_commission_pct IS NULL OR logistics_burden_per_sale_rub IS NULL OR take_base >= 1 THEN 'BLOCKED'
    WHEN price_freshness = 'STALE' OR commission_tariff_freshness = 'STALE' THEN 'STALE'
    WHEN seller_effective_price < break_even_before_ads_pre_tax_base THEN 'BELOW_BREAK_EVEN'
    WHEN seller_effective_price < break_even_before_ads_pre_tax_conservative THEN 'THIN_MARGIN'
    ELSE 'HEALTHY' END AS economics_status,
  'PRE_TAX' AS tax_model_status,
  'WB_FE_V1' AS economics_model_version,
  CURRENT_TIMESTAMP() AS economics_calculated_at
FROM e, UNNEST([STRUCT(logistics_burden_per_sale_rub IS NULL AS x)]) AS logistics_sample_size_null;

-- ── TVF_WB_FORWARD_ECONOMICS ───────────────────────────────────────────────
-- SHA-256 тела: 0423e1953bb948de3cdaf33daad0f4d135a3bf0a008a40b1f810c6c77dfa15f3
-- Длина тела: 1989
CREATE OR REPLACE TABLE FUNCTION `project-fa311fc0-4d87-4781-986.wb_mart.TVF_WB_FORWARD_ECONOMICS`(scenario_price NUMERIC, ad_drr_pct NUMERIC, required_contribution_rub NUMERIC) AS (
SELECT
  e.internal_sku, e.nm_id, e.product_name_short,
  e.seller_effective_price_rub AS current_price_rub,
  scenario_price AS scenario_price_rub,
  ad_drr_pct AS scenario_ad_drr_pct,
  e.cogs_rub, e.effective_commission_pct, e.acquiring_p50_pct, e.logistics_expected_rub,
  ROUND(scenario_price - scenario_price * (e.effective_commission_pct + e.acquiring_p50_pct)/100 - e.logistics_expected_rub - e.cogs_rub, 2) AS contribution_before_ads_pre_tax_rub,
  ROUND(scenario_price - scenario_price * (e.effective_commission_pct + e.acquiring_p50_pct + ad_drr_pct)/100 - e.logistics_expected_rub - e.cogs_rub, 2) AS contribution_after_ads_pre_tax_rub,
  ROUND(SAFE_DIVIDE(scenario_price - scenario_price * (e.effective_commission_pct + e.acquiring_p50_pct)/100 - e.logistics_expected_rub - e.cogs_rub, scenario_price) * 100, 2) AS contribution_before_ads_pre_tax_pct,
  e.break_even_before_ads_pre_tax_base,
  CASE WHEN (e.effective_commission_pct + e.acquiring_p50_pct + ad_drr_pct) < 100
       THEN ROUND(SAFE_DIVIDE(e.logistics_expected_rub + e.cogs_rub, 1 - (e.effective_commission_pct + e.acquiring_p50_pct + ad_drr_pct)/100), 2) END AS break_even_with_ads_pre_tax_rub,
  CASE WHEN (e.effective_commission_pct + e.acquiring_p50_pct) < 100
       THEN ROUND(SAFE_DIVIDE(e.logistics_expected_rub + e.cogs_rub + required_contribution_rub, 1 - (e.effective_commission_pct + e.acquiring_p50_pct)/100), 2) END AS target_contribution_price_rub,
  ROUND(scenario_price - e.break_even_before_ads_pre_tax_base, 2) AS margin_of_safety_rub,
  ROUND(SAFE_DIVIDE(scenario_price - scenario_price * (e.effective_commission_pct + e.acquiring_p50_pct)/100 - e.logistics_expected_rub - e.cogs_rub, scenario_price) * 100, 2) AS max_affordable_drr_pre_tax_pct,
  e.economics_confidence, e.economics_status, 'PRE_TAX' AS tax_model_status, e.economics_model_version
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` e
WHERE e.economics_status != 'BLOCKED' AND scenario_price > 0
);

-- ── V_WB_PRICING_ECONOMICS_HEALTH ───────────────────────────────────────────────
-- SHA-256 тела: a64bdf2e477f257df125411a25870547eb606670317d0c95ba4a91970542f938
-- Длина тела: 2340
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PRICING_ECONOMICS_HEALTH` AS
WITH e AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT`), t AS (SELECT MAX(observed_at) AS tariff_observed_at, MIN(tariff_freshness) AS tariff_freshness FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_TARIFFS_CURRENT`), o AS (SELECT status, kinds_ok, kinds_failed, rows_written FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_TARIFF_OBSERVATIONS` QUALIFY ROW_NUMBER() OVER (ORDER BY started_at DESC) = 1) SELECT COUNT(*) AS sku_total, COUNTIF(e.economics_status != 'BLOCKED') AS sku_economics_ready, COUNTIF(e.economics_confidence = 'HIGH') AS sku_conf_high, COUNTIF(e.economics_confidence = 'MEDIUM') AS sku_conf_medium, COUNTIF(e.economics_confidence = 'LOW') AS sku_conf_low, COUNTIF(e.economics_status = 'BLOCKED') AS sku_blocked, COUNTIF(e.economics_status = 'BELOW_BREAK_EVEN') AS sku_below_break_even, COUNTIF(e.economics_status = 'THIN_MARGIN') AS sku_thin_margin, COUNTIF(e.cogs_rub IS NULL) AS sku_missing_cogs, COUNTIF(e.logistics_expected_rub IS NULL) AS sku_missing_logistics, COUNTIF(e.commission_reconciliation_status = 'MATCHED') AS sku_commission_matched, COUNTIF(e.commission_reconciliation_status = 'MISMATCH') AS sku_commission_mismatch, ANY_VALUE(e.effective_commission_pct) AS effective_commission_pct, ANY_VALUE(e.realized_commission_pct) AS realized_commission_pct, ANY_VALUE(e.commission_abs_diff_pp) AS commission_abs_diff_pp, ANY_VALUE(t.tariff_observed_at) AS tariff_observed_at, ANY_VALUE(t.tariff_freshness) AS tariff_freshness, ANY_VALUE(o.status) AS last_tariff_run_status, ANY_VALUE(o.kinds_ok) AS last_tariff_kinds_ok, ANY_VALUE(o.kinds_failed) AS last_tariff_kinds_failed, ANY_VALUE(o.rows_written) AS last_tariff_rows, MIN(e.price_observed_at) AS oldest_price_observed_at, CASE WHEN ANY_VALUE(o.status) IS NULL THEN 'UNKNOWN' WHEN COUNTIF(e.commission_reconciliation_status = 'MISMATCH') > 0 THEN 'RED' WHEN ANY_VALUE(t.tariff_freshness) = 'STALE' THEN 'RED' WHEN COUNTIF(e.economics_status = 'BELOW_BREAK_EVEN') > 0 THEN 'RED' WHEN COUNTIF(e.economics_status = 'BLOCKED') > 0 OR ANY_VALUE(t.tariff_freshness) != 'FRESH' OR ANY_VALUE(o.kinds_failed) IS NOT NULL THEN 'YELLOW' ELSE 'GREEN' END AS economics_health_status, 'PRE_TAX' AS tax_model_status, 'WB_FE_V1' AS economics_model_version, CURRENT_TIMESTAMP() AS generated_at FROM e CROSS JOIN t CROSS JOIN o;
