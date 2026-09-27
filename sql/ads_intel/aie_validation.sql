-- ============================================================================
-- AIE V1 — набор проверок `ads_intel` (quality/suites.json).
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md. Пороги — нулевые инварианты контракта
-- (грань, сохранение строк, паритет с Ads-4, NULL ≠ 0) и числа Ads-4 без изменений; бизнес-порогов нет.
-- До развёртывания: python tools/aie_render.py run predeploy sql/ads_intel/aie_validation.sql \
--   --project project-fa311fc0-4d87-4781-986 --token-command "gcloud auth print-access-token"
-- ============================================================================

-- @check AIE_W01_PAIR_GRAIN_UNIQUE
-- Одна строка на (as_of_date, policy_id, advert_id, nm_id).
SELECT COUNT(*) AS n, COUNT(DISTINCT FORMAT('%t|%s|%d|%d', as_of_date, policy_id, advert_id, nm_id)) AS k,
       IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%d|%d', as_of_date, policy_id, advert_id, nm_id)) AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`;

-- @check AIE_W02_PAIR_SCOPE_RECONCILES_TO_FACT
-- Состав = все пары с расходом > 0 за 28 суток до as_of в FACT_ADS_SKU_DAILY; расход окна совпадает до копейки.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`),
a AS (SELECT MAX(as_of_date) AS as_of_date FROM v),
f AS (
  SELECT advert_id, nm_id, SUM(stats_spend_rub) AS spend
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`, a
  WHERE `date` <= a.as_of_date AND `date` > DATE_SUB(a.as_of_date, INTERVAL 28 DAY)
  GROUP BY advert_id, nm_id
  HAVING COUNTIF(stats_spend_rub > 0) > 0
)
SELECT (SELECT COUNT(*) FROM f) AS fact_pairs, (SELECT COUNT(*) FROM v) AS view_pairs,
       (SELECT SUM(spend) FROM f) AS fact_spend, (SELECT SUM(spend_28d_attributed_rub) FROM v) AS view_spend,
       IF((SELECT COUNT(*) FROM f) = (SELECT COUNT(*) FROM v)
          AND (SELECT COUNT(*) FROM f LEFT JOIN v USING (advert_id, nm_id) WHERE v.nm_id IS NULL) = 0
          AND (SELECT SUM(spend) FROM f) = (SELECT SUM(spend_28d_attributed_rub) FROM v), 'PASS', 'FAIL') AS status;

-- @check AIE_W03_EVIDENCE_STATUS_MATCHES_ADS4
-- evidence_status и waste пересчитаны независимо по порогам Ads-4 (40 / 10 / 1000).
SELECT COUNTIF(evidence_status != CASE
         WHEN IFNULL(eff_clicks, 0) >= 40 THEN 'ACTIONABLE'
         WHEN IFNULL(eff_clicks, 0) >= 10 OR IFNULL(eff_views, 0) >= 1000 THEN 'OBSERVATIONAL'
         ELSE 'INSUFFICIENT' END) AS status_mismatch,
       COUNTIF(waste_zero_orders_ads4 != (IFNULL(eff_clicks, 0) >= 40 AND IFNULL(eff_orders_raw, 0) = 0
                                         AND IFNULL(eff_spend_attributed_rub, 0) > 0)) AS waste_mismatch,
       IF(COUNTIF(evidence_status != CASE
         WHEN IFNULL(eff_clicks, 0) >= 40 THEN 'ACTIONABLE'
         WHEN IFNULL(eff_clicks, 0) >= 10 OR IFNULL(eff_views, 0) >= 1000 THEN 'OBSERVATIONAL'
         ELSE 'INSUFFICIENT' END) = 0
          AND COUNTIF(waste_zero_orders_ads4 != (IFNULL(eff_clicks, 0) >= 40 AND IFNULL(eff_orders_raw, 0) = 0
                                                AND IFNULL(eff_spend_attributed_rub, 0) > 0)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`;

-- @check AIE_W04_EFFECTIVE_WINDOW_INSIDE_NOMINAL
-- Эффективное окно лежит внутри номинального; сдвиг начала объясняется границей режима; дней не больше 28.
SELECT COUNTIF(effective_window_start < window_start_nominal) AS starts_before_nominal,
       COUNTIF(effective_window_start > window_start_nominal
               AND effective_window_start NOT IN UNNEST(ARRAY(SELECT x FROM UNNEST([campaign_regime_start, bid_regime_start, price_regime_start]) x WHERE x IS NOT NULL))) AS unexplained_shift,
       COUNTIF(effective_days + stockout_days_excluded > 28) AS too_many_days,
       IF(COUNTIF(effective_window_start < window_start_nominal) = 0
          AND COUNTIF(effective_window_start > window_start_nominal
                      AND effective_window_start NOT IN UNNEST(ARRAY(SELECT x FROM UNNEST([campaign_regime_start, bid_regime_start, price_regime_start]) x WHERE x IS NOT NULL))) = 0
          AND COUNTIF(effective_days + stockout_days_excluded > 28) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`;

-- @check AIE_W05_EFFECTIVE_WINDOW_RECONCILES
-- Сумма эффективного окна = FACT за [effective_window_start, as_of] минус дни с нулевым остатком.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`),
s AS (
  SELECT snapshot_date, nm_id, SUM(quantity) AS units
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT` GROUP BY snapshot_date, nm_id
),
r AS (
  SELECT v.advert_id, v.nm_id, SUM(f.stats_spend_rub) AS spend, SUM(f.clicks) AS clicks
  FROM v
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` f
    ON f.advert_id = v.advert_id AND f.nm_id = v.nm_id
   AND f.`date` BETWEEN v.effective_window_start AND v.as_of_date
  LEFT JOIN s ON s.nm_id = f.nm_id AND s.snapshot_date = f.`date`
  WHERE s.units IS NULL OR s.units > 0
  GROUP BY v.advert_id, v.nm_id
)
SELECT COUNTIF(IFNULL(r.spend, 0) != IFNULL(v.eff_spend_attributed_rub, 0)) AS spend_mismatch,
       COUNTIF(IFNULL(r.clicks, 0) != IFNULL(v.eff_clicks, 0)) AS clicks_mismatch,
       IF(COUNTIF(IFNULL(r.spend, 0) != IFNULL(v.eff_spend_attributed_rub, 0)) = 0
          AND COUNTIF(IFNULL(r.clicks, 0) != IFNULL(v.eff_clicks, 0)) = 0, 'PASS', 'FAIL') AS status
FROM v LEFT JOIN r USING (advert_id, nm_id);

-- @check AIE_W06_NULL_IS_NOT_ZERO_STOCK
-- Остаток NULL только когда снимка карточки нет; нулевой остаток — только из наблюдённого снимка.
SELECT COUNTIF(stock_status = 'NO_SNAPSHOT' AND stock_units IS NOT NULL) AS units_without_snapshot,
       COUNTIF(stock_status != 'NO_SNAPSHOT' AND stock_snapshot_date IS NULL) AS status_without_date,
       IF(COUNTIF(stock_status = 'NO_SNAPSHOT' AND stock_units IS NOT NULL) = 0
          AND COUNTIF(stock_status != 'NO_SNAPSHOT' AND stock_snapshot_date IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`;

-- @check AIE_W07_IDENTITY_FROM_CHANNEL_MAP
-- internal_sku = текущее сопоставление WB в REF_SKU_CHANNEL_MAP; неоднозначность видна, а не спрятана.
WITH m AS (
  SELECT SAFE_CAST(marketplace_sku AS INT64) AS nm_id, COUNT(DISTINCT internal_sku) AS n, MIN(internal_sku) AS sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'WB' AND is_current GROUP BY 1
)
SELECT COUNTIF(v.internal_sku IS DISTINCT FROM m.sku) AS sku_mismatch,
       COUNTIF(v.sku_mapping_count != IFNULL(m.n, 0)) AS count_mismatch,
       IF(COUNTIF(v.internal_sku IS DISTINCT FROM m.sku) = 0 AND COUNTIF(v.sku_mapping_count != IFNULL(m.n, 0)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE` v LEFT JOIN m USING (nm_id);

-- @check AIE_W08_P7_THRESHOLD_APPLIED
-- Граница режима цены (P7 из V_AIE_POLICY): начало совпадает со сменой seller_effective_price не меньше порога,
-- и позже начала (до as_of) такой смены нет. Смены ниже порога границу не двигают (считаются для контекста).
WITH v AS (SELECT DISTINCT policy_id, nm_id, as_of_date, price_regime_start
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`),
pol AS (SELECT policy_id, p7_price_change_pct AS p7 FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`),
c AS (
  SELECT nm_id, DATE_ADD(DATE(observed_at, 'Europe/Moscow'), INTERVAL 1 DAY) AS rs,
         DATE(prev_observed_at, 'Europe/Moscow') AS prev_d, ABS(delta_seller_effective_price_pct) AS d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVED_CHANGES`
  WHERE environment = 'prod' AND delta_seller_effective_price IS NOT NULL AND delta_seller_effective_price != 0
),
j AS (
  SELECT v.policy_id, v.nm_id, v.price_regime_start,
         COUNTIF(c.rs = v.price_regime_start AND (pol.p7 IS NULL OR c.d >= pol.p7)) AS start_matches,
         COUNTIF(c.rs > v.price_regime_start AND c.prev_d <= v.as_of_date AND (pol.p7 IS NULL OR c.d >= pol.p7)) AS later_qualifying,
         COUNTIF(c.prev_d <= v.as_of_date AND c.d < pol.p7) AS below_threshold_changes
  FROM v JOIN pol USING (policy_id) LEFT JOIN c ON c.nm_id = v.nm_id
  GROUP BY 1, 2, 3
)
SELECT COUNTIF(price_regime_start IS NOT NULL AND start_matches = 0) AS start_without_qualifying_change,
       COUNTIF(price_regime_start IS NOT NULL AND later_qualifying > 0) AS later_qualifying_ignored,
       SUM(below_threshold_changes) AS below_threshold_changes_ignored,
       IF(COUNTIF(price_regime_start IS NOT NULL AND start_matches = 0) = 0
          AND COUNTIF(price_regime_start IS NOT NULL AND later_qualifying > 0) = 0, 'PASS', 'FAIL') AS status
FROM j;

-- @check AIE_W09_LAST_SPEND_CONSISTENT
-- last_spend_date — последний день с атрибутированным расходом пары не позже as_of (FACT_ADS_SKU_DAILY, 28 суток окна).
WITH v AS (SELECT DISTINCT as_of_date, advert_id, nm_id, last_spend_date, days_since_last_spend
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`),
f AS (SELECT advert_id, nm_id, MAX(IF(stats_spend_rub > 0, `date`, NULL)) AS last_spend
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`
      WHERE `date` <= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)
      GROUP BY 1, 2)
SELECT COUNTIF(v.last_spend_date IS DISTINCT FROM f.last_spend) AS mismatch,
       COUNTIF(v.days_since_last_spend IS DISTINCT FROM DATE_DIFF(v.as_of_date, v.last_spend_date, DAY)) AS days_mismatch,
       IF(COUNTIF(v.last_spend_date IS DISTINCT FROM f.last_spend) = 0
          AND COUNTIF(v.days_since_last_spend IS DISTINCT FROM DATE_DIFF(v.as_of_date, v.last_spend_date, DAY)) = 0
          AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM v LEFT JOIN f USING (advert_id, nm_id);

-- @check AIE_E01_ECON_GRAIN_UNIQUE
SELECT COUNT(*) AS n, COUNT(DISTINCT FORMAT('%t|%s|%d', as_of_date, policy_id, nm_id)) AS k,
       IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%d', as_of_date, policy_id, nm_id)) AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`;

-- @check AIE_E02_PARITY_WITH_ADS4_ECONOMIC_LIMITS
-- Блок ref28_* совпадает с wb_mart.V_ADS_SKU_ECONOMIC_LIMITS (окно 28) до копейки, если даты совпадают.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`),
l AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SKU_ECONOMIC_LIMITS` WHERE window_days = 28)
SELECT (SELECT MAX(as_of_date) FROM v) AS aie_as_of, (SELECT MAX(as_of_date) FROM l) AS ads4_as_of,
       COUNTIF(v.ref28_buyouts_rub IS DISTINCT FROM l.buyouts_rub
            OR v.ref28_marketplace_fee_rub IS DISTINCT FROM l.marketplace_fee_rub
            OR v.ref28_logistics_cost_positive IS DISTINCT FROM l.logistics_cost_positive
            OR v.ref28_net_product_cogs_rub IS DISTINCT FROM l.net_product_cogs_rub
            OR v.ref28_ad_spend_rub IS DISTINCT FROM l.ad_spend_rub
            OR v.ref28_contribution_before_ads_rub IS DISTINCT FROM l.contribution_before_ads_rub) AS mismatches,
       COUNT(*) AS joined,
       IF((SELECT MAX(as_of_date) FROM v) = (SELECT MAX(as_of_date) FROM l)
          AND COUNTIF(v.ref28_buyouts_rub IS DISTINCT FROM l.buyouts_rub
            OR v.ref28_marketplace_fee_rub IS DISTINCT FROM l.marketplace_fee_rub
            OR v.ref28_logistics_cost_positive IS DISTINCT FROM l.logistics_cost_positive
            OR v.ref28_net_product_cogs_rub IS DISTINCT FROM l.net_product_cogs_rub
            OR v.ref28_ad_spend_rub IS DISTINCT FROM l.ad_spend_rub
            OR v.ref28_contribution_before_ads_rub IS DISTINCT FROM l.contribution_before_ads_rub) = 0
          AND COUNT(*) = (SELECT COUNT(*) FROM l), 'PASS', 'FAIL') AS status
FROM v JOIN l ON l.nm_id = v.nm_id AND l.as_of_date = v.as_of_date;

-- @check AIE_E03_ATTRIBUTED_SPEND_SOURCE_PARITY
-- Расход из FACT_ADS_SKU_DAILY = MART_SKU_DAILY.ad_spend (одна атрибуция, биллинг не участвует).
SELECT COUNTIF(ROUND(IFNULL(ref28_fact_ad_spend_rub, 0), 2) != ROUND(IFNULL(ref28_ad_spend_rub, 0), 2)) AS mismatches,
       IF(COUNTIF(ROUND(IFNULL(ref28_fact_ad_spend_rub, 0), 2) != ROUND(IFNULL(ref28_ad_spend_rub, 0), 2)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`;

-- @check AIE_E04_ECON_WINDOW_BOUNDS
-- Экономическое окно заканчивается не позже as_of и не позже известных финансов; не длиннее 28 суток.
SELECT COUNTIF(econ_as_of > as_of_date) AS after_as_of,
       COUNTIF(econ_as_of > finance_known_through) AS after_finance,
       COUNTIF(econ_window_days > 28) AS too_long,
       COUNTIF(econ_window_days = 0 AND economic_state != 'EMPTY_WINDOW') AS empty_not_flagged,
       IF(COUNTIF(econ_as_of > as_of_date) = 0 AND COUNTIF(econ_as_of > finance_known_through) = 0
          AND COUNTIF(econ_window_days > 28) = 0
          AND COUNTIF(econ_window_days = 0 AND economic_state != 'EMPTY_WINDOW') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`;

-- @check AIE_E05_FORWARD_BASE_IDENTITY
-- (1 − take_base)(1 − BE_base / P) = max_affordable_drr_pre_tax_pct / 100 — тождество WB_FE_V1.
-- Допуск выведен из округлений контракта (sql/pricing/pr2_wb_forward_economics.sql): процент — ROUND(…, 2),
-- то есть ±0,005 п.п. = 5e-5; безубыточная цена — ROUND(…, 2), то есть ±0,005 ₽, что даёт ±0,005 / P.
SELECT COUNTIF(ABS(forward_max_drr_base_identity - forward_max_drr_base) > 5e-5 + 0.005 / forward_price_rub + 1e-12) AS mismatches,
       COUNTIF(forward_availability = 'AVAILABLE') AS available,
       MAX(ABS(forward_max_drr_base_identity - forward_max_drr_base)) AS max_abs_diff,
       IF(COUNTIF(ABS(forward_max_drr_base_identity - forward_max_drr_base) > 5e-5 + 0.005 / forward_price_rub + 1e-12) = 0
          AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`
WHERE forward_availability = 'AVAILABLE';

-- @check AIE_E06_NULL_IS_NOT_ZERO_ECONOMICS
-- Без полной себестоимости предел NULL, не 0; состояние MISSING_COGS.
SELECT COUNTIF(net_product_cogs_rub IS NULL AND econ_window_days > 0 AND economic_state != 'MISSING_COGS') AS missing_not_flagged,
       COUNTIF(net_product_cogs_rub IS NULL AND max_ad_drr_breakeven IS NOT NULL) AS limit_without_cogs,
       IF(COUNTIF(net_product_cogs_rub IS NULL AND econ_window_days > 0 AND economic_state != 'MISSING_COGS') = 0
          AND COUNTIF(net_product_cogs_rub IS NULL AND max_ad_drr_breakeven IS NOT NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`;

-- @check AIE_Q01_QUERY_GRAIN_UNIQUE
SELECT COUNT(*) AS n, COUNT(DISTINCT FORMAT('%s|%d|%s', policy_id, nm_id, norm_query)) AS k,
       IF(COUNT(*) = COUNT(DISTINCT FORMAT('%s|%d|%s', policy_id, nm_id, norm_query)) AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`;

-- @check AIE_Q02_QUERY_ROWS_EQUAL_ADS4
-- Каждая строка Ads-4 V_ADS_FUNNEL_QUERY_28D ровно один раз, лишних нет; расход окна тот же.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`),
a AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`)
SELECT (SELECT COUNT(*) FROM a) AS ads4_rows, (SELECT COUNT(*) FROM v) AS aie_rows,
       (SELECT COUNT(*) FROM a LEFT JOIN v USING (nm_id, norm_query) WHERE v.nm_id IS NULL) AS lost,
       IF((SELECT COUNT(*) FROM a) = (SELECT COUNT(*) FROM v)
          AND (SELECT COUNT(*) FROM a LEFT JOIN v USING (nm_id, norm_query) WHERE v.nm_id IS NULL) = 0
          AND ABS((SELECT SUM(spend_sum) FROM a) - (SELECT SUM(spend_attributed_rub) FROM v)) < 1e-6, 'PASS', 'FAIL') AS status;

-- @check AIE_Q03_CLASSES_REQUIRE_EVIDENCE
-- Конверсионные классы — только при ACTIONABLE; «недополучает показы» ⊂ «конвертирует»; основной класс согласован.
SELECT COUNTIF((q_converting OR q_traffic_no_conversion) AND evidence_status != 'ACTIONABLE') AS class_without_evidence,
       COUNTIF(q_possibly_underexposed AND NOT q_converting) AS underexposed_not_converting,
       COUNTIF(primary_query_class = 'Q_INSUFFICIENT' AND evidence_status = 'ACTIONABLE') AS insufficient_mismatch,
       IF(COUNTIF((q_converting OR q_traffic_no_conversion) AND evidence_status != 'ACTIONABLE') = 0
          AND COUNTIF(q_possibly_underexposed AND NOT q_converting) = 0
          AND COUNTIF(primary_query_class = 'Q_INSUFFICIENT' AND evidence_status = 'ACTIONABLE') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`;

-- @check AIE_Q04_NOT_A_BID_DIRECTION_SOURCE
-- Диагностика не выдаёт направления ставки; множители — значения Ads-4 с пометкой P15.
SELECT COUNTIF(is_bid_direction_source) AS direction_rows,
       COUNTIF(diag_below_mult != 0.7 OR diag_above_mult != 1.3 OR diag_mult_source != 'ADS4_DESIGN_CHOICE_UNRESOLVED_P15') AS mult_drift,
       IF(COUNTIF(is_bid_direction_source) = 0
          AND COUNTIF(diag_below_mult != 0.7 OR diag_above_mult != 1.3 OR diag_mult_source != 'ADS4_DESIGN_CHOICE_UNRESOLVED_P15') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`;

-- @check AIE_Q05_CEILINGS_NULL_WHEN_NOT_COMPUTABLE
-- Потолки NULL, если экономика SKU не вычислена или доказательности нет; нулём не подменяются.
SELECT COUNTIF(econ_cpo_ceiling_rub IS NOT NULL AND sku_economic_state NOT IN ('BELOW_BREAKEVEN', 'ABOVE_BREAKEVEN', 'NO_AD_SPEND', 'NEGATIVE_BEFORE_ADS')) AS ceiling_without_economics,
       COUNTIF(econ_cpc_ceiling_rub IS NOT NULL AND evidence_status != 'ACTIONABLE') AS cpc_ceiling_without_evidence,
       IF(COUNTIF(econ_cpo_ceiling_rub IS NOT NULL AND sku_economic_state NOT IN ('BELOW_BREAKEVEN', 'ABOVE_BREAKEVEN', 'NO_AD_SPEND', 'NEGATIVE_BEFORE_ADS')) = 0
          AND COUNTIF(econ_cpc_ceiling_rub IS NOT NULL AND evidence_status != 'ACTIONABLE') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`;

-- @check AIE_O01_OZON_PAIR_GRAIN_UNIQUE
SELECT COUNT(*) AS n, COUNT(DISTINCT FORMAT('%t|%s|%s|%s', as_of_date, policy_id, campaign_id, sku)) AS k,
       IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%s|%s', as_of_date, policy_id, campaign_id, sku)) AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`;

-- @check AIE_O02_OZON_NEVER_PRODUCTION_GRADE
-- Ozon не production-grade до PR-8 (P2-5, P2-6, нет снимка ставок, RAW не на дату) — во всех строках обоих представлений.
SELECT (SELECT COUNTIF(production_grade IS NOT FALSE) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`) AS pair_rows_graded,
       (SELECT COUNTIF(production_grade IS NOT FALSE) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD`) AS econ_rows_graded,
       IF((SELECT COUNTIF(production_grade IS NOT FALSE) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`) = 0
          AND (SELECT COUNTIF(production_grade IS NOT FALSE) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD`) = 0, 'PASS', 'FAIL') AS status;

-- @check AIE_O03_OZON_SCOPE_RECONCILES_TO_RAW
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`),
a AS (SELECT MAX(as_of_date) AS as_of_date FROM v),
r AS (
  SELECT campaign_id, sku, SUM(attributed_spend_rub) AS spend
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`, a
  WHERE `date` <= a.as_of_date AND `date` > DATE_SUB(a.as_of_date, INTERVAL 28 DAY)
  GROUP BY campaign_id, sku HAVING COUNTIF(attributed_spend_rub > 0) > 0
)
SELECT (SELECT COUNT(*) FROM r) AS raw_pairs, (SELECT COUNT(*) FROM v) AS view_pairs,
       IF((SELECT COUNT(*) FROM r) = (SELECT COUNT(*) FROM v)
          AND (SELECT COUNT(*) FROM r LEFT JOIN v USING (campaign_id, sku) WHERE v.sku IS NULL) = 0
          AND (SELECT SUM(spend) FROM r) = (SELECT SUM(spend_28d_attributed_rub) FROM v), 'PASS', 'FAIL') AS status;

-- @check AIE_O04_OZON_P26_SIGNATURE_RECOMPUTED
-- Сигнатура P2-6 пересчитана независимо: день с расходом кампании > 0 и без единой строки SKU.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`),
a AS (SELECT MAX(as_of_date) AS as_of_date FROM v),
x AS (
  SELECT e.campaign_id, COUNTIF(e.expense_rub > 0 AND NOT EXISTS (
           SELECT 1 FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` s
           WHERE s.campaign_id = e.campaign_id AND s.`date` = e.`date`)) AS days
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e, a
  WHERE e.`date` <= a.as_of_date AND e.`date` > DATE_SUB(a.as_of_date, INTERVAL 28 DAY)
  GROUP BY e.campaign_id
)
SELECT COUNTIF(v.p26_suspect_days != IFNULL(x.days, 0)) AS mismatches,
       IF(COUNTIF(v.p26_suspect_days != IFNULL(x.days, 0)) = 0, 'PASS', 'FAIL') AS status
FROM v LEFT JOIN x USING (campaign_id);

-- @check AIE_O05_OZON_AUTOPILOT_FLAG_RECOMPUTED
-- P9: TARGET_BIDS / TARGET_CIR в последнем снимке кампании ≤ as_of.
WITH v AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`),
k AS (
  SELECT campaign_id, product_autopilot_strategy FROM (
    SELECT c.campaign_id, c.product_autopilot_strategy,
           ROW_NUMBER() OVER (PARTITION BY c.campaign_id ORDER BY c.snapshot_date DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS` c
    WHERE c.snapshot_date <= (SELECT MAX(as_of_date) FROM v))
  WHERE rn = 1
)
SELECT COUNTIF(v.autopilot_controls_bids IS DISTINCT FROM (k.product_autopilot_strategy IN ('TARGET_BIDS', 'TARGET_CIR'))) AS mismatches,
       COUNTIF(v.autopilot_controls_bids) AS autopilot_pairs,
       IF(COUNTIF(v.autopilot_controls_bids IS DISTINCT FROM (k.product_autopilot_strategy IN ('TARGET_BIDS', 'TARGET_CIR'))) = 0, 'PASS', 'FAIL') AS status
FROM v LEFT JOIN k USING (campaign_id);

-- @check AIE_O06_OZON_STRUCTURAL_ECONOMICS_CONSISTENT
-- Пауза по экономике только при отрицательном лучшем сценарии и неположительном факте; одна строка на SKU.
SELECT COUNT(*) AS n, COUNT(DISTINCT internal_sku) AS k,
       COUNTIF(negative_all_available_bases AND NOT forward_best_negative) AS pause_without_forward,
       COUNTIF(negative_all_available_bases AND realized_contribution_before_ads_rub >= 0) AS pause_with_positive_fact,
       COUNTIF(run_mode = 'CURRENT' AND forward_availability = 'SKIPPED_NOT_REPLAYABLE') AS skipped_in_current,
       IF(COUNT(*) = COUNT(DISTINCT internal_sku)
          AND COUNTIF(negative_all_available_bases AND NOT forward_best_negative) = 0
          AND COUNTIF(negative_all_available_bases AND realized_contribution_before_ads_rub >= 0) = 0
          AND COUNTIF(run_mode = 'CURRENT' AND forward_availability = 'SKIPPED_NOT_REPLAYABLE') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD`;

-- @check AIE_D01_NO_INCREASE_WHILE_P1_NULL
-- Инвариант владельца: пока P1 не задан, INCREASE не выдаётся ни одной строке.
SELECT COUNTIF(recommendation = 'INCREASE' AND p1_reserve_share IS NULL) AS increase_without_p1,
       COUNTIF(recommendation = 'INCREASE') AS increase_total,
       IF(COUNTIF(recommendation = 'INCREASE' AND p1_reserve_share IS NULL) = 0
          AND COUNTIF(recommendation = 'INCREASE') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D02_EVERY_ROW_HAS_A_PRIMARY_REASON
SELECT COUNTIF(primary_reason_code IS NULL) AS no_primary,
       COUNTIF(ARRAY_LENGTH(reason_codes) = 0) AS no_reasons,
       COUNTIF(primary_reason_code NOT IN UNNEST(reason_codes)) AS primary_not_listed,
       COUNTIF(STRPOS(explanation_ru, 'внутренняя ошибка') > 0) AS internal_error_text,
       IF(COUNTIF(primary_reason_code IS NULL) = 0 AND COUNTIF(ARRAY_LENGTH(reason_codes) = 0) = 0
          AND COUNTIF(primary_reason_code NOT IN UNNEST(reason_codes)) = 0
          AND COUNTIF(STRPOS(explanation_ru, 'внутренняя ошибка') > 0) = 0 AND COUNT(*) > 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D03_DECISION_GRAIN_UNIQUE
SELECT COUNT(*) AS n,
       COUNT(DISTINCT FORMAT('%t|%s|%s|%s|%s', as_of_date, policy_id, marketplace, campaign_id, marketplace_sku)) AS k,
       IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%s|%s|%s', as_of_date, policy_id, marketplace, campaign_id, marketplace_sku)),
          'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D04_OZON_FAIL_CLOSED
-- Ozon: не production-grade; направлений ставки (INCREASE / DECREASE / HOLD) нет.
SELECT COUNTIF(production_grade) AS graded,
       COUNTIF(recommendation IN ('INCREASE', 'DECREASE', 'HOLD')) AS directional,
       COUNTIF('DQ_OZON_P25_WINDOW_BIAS' NOT IN UNNEST(reason_codes)) AS p25_not_flagged,
       IF(COUNTIF(production_grade) = 0 AND COUNTIF(recommendation IN ('INCREASE', 'DECREASE', 'HOLD')) = 0
          AND COUNTIF('DQ_OZON_P25_WINDOW_BIAS' NOT IN UNNEST(reason_codes)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`
WHERE marketplace = 'OZON';

-- @check AIE_D05_EVERY_WB_PAIR_DECIDED_ONCE
-- Каждая пара WB с атрибутированным расходом за 28 суток до as_of получает ровно одно решение.
-- Сверка с FACT_ADS_SKU_DAILY, а не с V_AIE_WB_PAIR_EVIDENCE: вторая встроенная копия превышает лимит
-- планировщика BigQuery; равенство доказательной базы и FACT доказывает AIE_W02.
WITH d AS (SELECT as_of_date, campaign_id, marketplace_sku, COUNT(*) AS n
           FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`
           WHERE marketplace = 'WB' GROUP BY as_of_date, campaign_id, marketplace_sku),
f AS (SELECT CAST(advert_id AS STRING) AS campaign_id, CAST(nm_id AS STRING) AS marketplace_sku
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`
      WHERE `date` <= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)
        AND `date` > DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 29 DAY)
      GROUP BY 1, 2 HAVING COUNTIF(stats_spend_rub > 0) > 0)
SELECT (SELECT COUNT(*) FROM f) AS fact_pairs, (SELECT COUNT(*) FROM d) AS decided_pairs,
       IF((SELECT COUNT(*) FROM f) = (SELECT COUNT(*) FROM d)
          AND (SELECT COUNT(*) FROM f LEFT JOIN d USING (campaign_id, marketplace_sku) WHERE d.n IS NULL OR d.n != 1) = 0,
          'PASS', 'FAIL') AS status;

-- @check AIE_D05B_EVERY_OZON_PAIR_DECIDED_ONCE
WITH d AS (SELECT campaign_id, marketplace_sku, COUNT(*) AS n
           FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`
           WHERE marketplace = 'OZON' GROUP BY campaign_id, marketplace_sku),
r AS (SELECT campaign_id, sku AS marketplace_sku
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`
      WHERE `date` <= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)
        AND `date` > DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 29 DAY)
      GROUP BY 1, 2 HAVING COUNTIF(attributed_spend_rub > 0) > 0)
SELECT (SELECT COUNT(*) FROM r) AS raw_pairs, (SELECT COUNT(*) FROM d) AS decided_pairs,
       IF((SELECT COUNT(*) FROM r) = (SELECT COUNT(*) FROM d)
          AND (SELECT COUNT(*) FROM r LEFT JOIN d USING (campaign_id, marketplace_sku) WHERE d.n IS NULL OR d.n != 1) = 0,
          'PASS', 'FAIL') AS status;

-- @check AIE_D06_DETERMINISTIC
-- Два независимых вычисления в одном запросе дают один и тот же отпечаток результата.
WITH a AS (SELECT BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fp, COUNT(*) AS n
           FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT` t),
b AS (SELECT BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fp, COUNT(*) AS n
      FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT` t)
SELECT a.fp AS fp_a, b.fp AS fp_b, a.n, IF(a.fp = b.fp AND a.n = b.n, 'PASS', 'FAIL') AS status FROM a, b;

-- @check AIE_D07_DELTA_NOT_DERIVABLE
-- Решение владельца P11: величина изменения ставки не выводится.
SELECT COUNTIF(proposed_delta IS NOT NULL) AS delta_set, COUNTIF(delta_basis != 'DELTA_NOT_DERIVABLE') AS basis_other,
       IF(COUNTIF(proposed_delta IS NOT NULL) = 0 AND COUNTIF(delta_basis != 'DELTA_NOT_DERIVABLE') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D08_POLICY_IS_THE_OWNER_DECISION
-- Одна политика — утверждённая владельцем 2026-09-27 (evetis_ref.V_AIE_POLICY): P3 = 0.90, P7 = 3 %, K = 14;
-- P1, P2, P5 (обе границы), P13 — NULL (скрытых значений по умолчанию нет).
SELECT COUNT(DISTINCT policy_id) AS policies,
       COUNTIF(policy_id != 'V1_SHADOW_2026-09-27' OR p3_confidence IS DISTINCT FROM 0.90
               OR p7_price_change_pct IS DISTINCT FROM 3.0 OR k_inactive_days IS DISTINCT FROM 14) AS approved_mismatch,
       COUNTIF(p1_reserve_share IS NOT NULL OR p2_band_pp IS NOT NULL OR p5_low_cover_days IS NOT NULL
               OR p5_overstock_cover_days IS NOT NULL OR p13_cooldown_days IS NOT NULL) AS unapproved_set,
       IF(COUNT(DISTINCT policy_id) = 1
          AND COUNTIF(policy_id != 'V1_SHADOW_2026-09-27' OR p3_confidence IS DISTINCT FROM 0.90
               OR p7_price_change_pct IS DISTINCT FROM 3.0 OR k_inactive_days IS DISTINCT FROM 14) = 0
          AND COUNTIF(p1_reserve_share IS NOT NULL OR p2_band_pp IS NOT NULL OR p5_low_cover_days IS NOT NULL
               OR p5_overstock_cover_days IS NOT NULL OR p13_cooldown_days IS NOT NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D09_STATE_AND_DIRECTION_CONSISTENT
SELECT COUNTIF(recommendation NOT IN ('INCREASE', 'DECREASE', 'HOLD', 'PAUSE_CANDIDATE', 'INSUFFICIENT_DATA', 'BLOCKED_BY_GUARDRAIL')) AS unknown_state,
       COUNTIF(proposed_direction != CASE recommendation WHEN 'INCREASE' THEN 'UP' WHEN 'DECREASE' THEN 'DOWN'
                                                          WHEN 'PAUSE_CANDIDATE' THEN 'PAUSE' ELSE 'NONE' END) AS direction_mismatch,
       IF(COUNTIF(recommendation NOT IN ('INCREASE', 'DECREASE', 'HOLD', 'PAUSE_CANDIDATE', 'INSUFFICIENT_DATA', 'BLOCKED_BY_GUARDRAIL')) = 0
          AND COUNTIF(proposed_direction != CASE recommendation WHEN 'INCREASE' THEN 'UP' WHEN 'DECREASE' THEN 'DOWN'
                                                          WHEN 'PAUSE_CANDIDATE' THEN 'PAUSE' ELSE 'NONE' END) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D10_SAME_DECISION_DATE_EVERYWHERE
-- Одна дата решения на весь результат; в текущем режиме это вчера по МСК (как в V_AIE_WB_PAIR_EVIDENCE).
-- Одна ссылка на представление: две встроенные копии решения превышают лимит планировщика BigQuery.
SELECT COUNT(DISTINCT as_of_date) AS decision_dates, MAX(as_of_date) AS as_of_date,
       IF(COUNT(DISTINCT as_of_date) = 1 AND COUNTIF(run_mode != 'CURRENT') = 0
          AND MAX(as_of_date) = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D11_NO_OPTIMIZATION_CODES_WITHOUT_EVIDENCE
-- Коды L7/L8 появляются только у доказательных строк (ACTIONABLE, эффективное окно > 0).
SELECT COUNTIF(EXISTS (SELECT 1 FROM UNNEST(reason_codes) c
                       WHERE c IN ('WASTE_ZERO_ORDERS_ADS4', 'OZON_NOT_PRODUCTION_GRADE', 'POLICY_P3_NOT_SET',
                                   'POLICY_P3_UNSUPPORTED_VALUE', 'ECON_BASIS_UNAVAILABLE', 'ECON_NO_BUYOUTS_IN_WINDOW',
                                   'EVID_INTERVAL_STRADDLES_BREAKEVEN', 'ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT', 'ALLOCATION_NOT_WORST_PAIR',
                                   'POLICY_P1_NOT_SET', 'EVID_INTERVAL_STRADDLES_TARGET', 'ECON_DRR_WITHIN_TARGET_BAND',
                                   'ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE', 'POLICY_P2_NOT_SET', 'POLICY_P5_NOT_SET',
                                   'INV_COVER_UNAVAILABLE', 'INV_LOW_COVER', 'ECON_DRR_BELOW_TARGET_CONFIDENT', 'INCREASE_CANDIDATE'))
               AND NOT (evidence_status = 'ACTIONABLE' AND effective_days > 0)) AS leaked,
       IF(COUNTIF(EXISTS (SELECT 1 FROM UNNEST(reason_codes) c
                       WHERE c IN ('WASTE_ZERO_ORDERS_ADS4', 'OZON_NOT_PRODUCTION_GRADE', 'POLICY_P3_NOT_SET',
                                   'POLICY_P3_UNSUPPORTED_VALUE', 'ECON_BASIS_UNAVAILABLE', 'ECON_NO_BUYOUTS_IN_WINDOW',
                                   'EVID_INTERVAL_STRADDLES_BREAKEVEN', 'ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT', 'ALLOCATION_NOT_WORST_PAIR',
                                   'POLICY_P1_NOT_SET', 'EVID_INTERVAL_STRADDLES_TARGET', 'ECON_DRR_WITHIN_TARGET_BAND',
                                   'ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE', 'POLICY_P2_NOT_SET', 'POLICY_P5_NOT_SET',
                                   'INV_COVER_UNAVAILABLE', 'INV_LOW_COVER', 'ECON_DRR_BELOW_TARGET_CONFIDENT', 'INCREASE_CANDIDATE'))
               AND NOT (evidence_status = 'ACTIONABLE' AND effective_days > 0)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D12_UNIVERSE_RULE
-- Текущий набор (решение владельца 2026-09-27): активный статус (WB 9/11, Ozon только RUNNING — INACTIVE не доказан) и не больше K суток
-- без расхода. Остальное — историческая база доказательств: строка сохраняется, основная причина — SCOPE_CAMPAIGN_INACTIVE
-- (или более ранний код L0), состояние BLOCKED_BY_GUARDRAIL.
SELECT COUNTIF((universe = 'CURRENT_ACTIONABLE') IS DISTINCT FROM
                 IFNULL(IF(marketplace = 'WB', campaign_status IN ('9', '11'), campaign_status = 'RUNNING')
                  AND days_since_last_spend <= k_inactive_days, FALSE)) AS rule_mismatch,
       COUNTIF((universe = 'HISTORICAL_EVIDENCE_ONLY') IS DISTINCT FROM ('SCOPE_CAMPAIGN_INACTIVE' IN UNNEST(reason_codes))) AS code_mismatch,
       COUNTIF(universe = 'HISTORICAL_EVIDENCE_ONLY' AND recommendation != 'BLOCKED_BY_GUARDRAIL') AS historical_actionable,
       COUNTIF(universe = 'HISTORICAL_EVIDENCE_ONLY') AS historical_rows,
       IF(COUNTIF((universe = 'CURRENT_ACTIONABLE') IS DISTINCT FROM
                 IFNULL(IF(marketplace = 'WB', campaign_status IN ('9', '11'), campaign_status = 'RUNNING')
                  AND days_since_last_spend <= k_inactive_days, FALSE)) = 0
          AND COUNTIF((universe = 'HISTORICAL_EVIDENCE_ONLY') IS DISTINCT FROM ('SCOPE_CAMPAIGN_INACTIVE' IN UNNEST(reason_codes))) = 0
          AND COUNTIF(universe = 'HISTORICAL_EVIDENCE_ONLY' AND recommendation != 'BLOCKED_BY_GUARDRAIL') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D13_PRICE_BELOW_BREAKEVEN_CONSISTENT
-- PRICE_BELOW_BREAKEVEN: прогнозный вклад до рекламы < 0, цена ниже безубыточной (WB), маршрут PRICING_REVIEW;
-- PRICING_REVIEW ставится ровно при отрицательной прогнозной base-экономике.
SELECT COUNTIF(primary_reason_code = 'PRICE_BELOW_BREAKEVEN'
               AND NOT (contribution_before_ads_rub < 0 AND routing = 'PRICING_REVIEW')) AS pause_without_negative_forward,
       COUNTIF(primary_reason_code = 'PRICE_BELOW_BREAKEVEN' AND marketplace = 'WB'
               AND NOT (breakeven_price_rub > current_effective_price_rub)) AS pause_price_not_below_breakeven,
       COUNTIF(IFNULL(routing = 'PRICING_REVIEW', FALSE) IS DISTINCT FROM ('PRICING_REVIEW' IN UNNEST(reason_codes))) AS routing_code_mismatch,
       COUNTIF(marketplace = 'WB' AND run_mode = 'CURRENT'
               AND IFNULL(routing = 'PRICING_REVIEW', FALSE) IS DISTINCT FROM IFNULL(contribution_before_ads_rub < 0, FALSE)) AS routing_rule_mismatch,
       COUNTIF(primary_reason_code = 'PRICE_BELOW_BREAKEVEN') AS pause_rows,
       IF(COUNTIF(primary_reason_code = 'PRICE_BELOW_BREAKEVEN'
               AND NOT (contribution_before_ads_rub < 0 AND routing = 'PRICING_REVIEW')) = 0
          AND COUNTIF(primary_reason_code = 'PRICE_BELOW_BREAKEVEN' AND marketplace = 'WB'
               AND NOT (breakeven_price_rub > current_effective_price_rub)) = 0
          AND COUNTIF(IFNULL(routing = 'PRICING_REVIEW', FALSE) IS DISTINCT FROM ('PRICING_REVIEW' IN UNNEST(reason_codes))) = 0
          AND COUNTIF(marketplace = 'WB' AND run_mode = 'CURRENT'
               AND IFNULL(routing = 'PRICING_REVIEW', FALSE) IS DISTINCT FROM IFNULL(contribution_before_ads_rub < 0, FALSE)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D14_FINANCIAL_DATA_MATURE
-- Контракт FINANCIAL_DATA_MATURE: экономическое окно WB заканчивается не позже последней даты с финансами и не позже
-- as_of; флаг никогда не FALSE; FIN_WINDOW_ENDS_AT_FINANCE — ровно при отставании окна.
SELECT COUNTIF(marketplace = 'WB' AND financial_data_mature IS NOT TRUE) AS not_mature,
       COUNTIF(marketplace = 'WB' AND econ_as_of > as_of_date) AS after_as_of,
       COUNTIF(marketplace = 'WB' AND (econ_window_end_lag_days > 0) IS DISTINCT FROM ('FIN_WINDOW_ENDS_AT_FINANCE' IN UNNEST(reason_codes))) AS ctx_mismatch,
       MAX(econ_window_end_lag_days) AS max_lag_days,
       IF(COUNTIF(marketplace = 'WB' AND financial_data_mature IS NOT TRUE) = 0
          AND COUNTIF(marketplace = 'WB' AND econ_as_of > as_of_date) = 0
          AND COUNTIF(marketplace = 'WB' AND (econ_window_end_lag_days > 0) IS DISTINCT FROM ('FIN_WINDOW_ENDS_AT_FINANCE' IN UNNEST(reason_codes))) = 0,
          'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D15_INCREASE_CANDIDATE_IS_DIAGNOSTIC
-- INCREASE_CANDIDATE — флаг и контекстный код, не состояние; кандидат требует доказательства ниже фактической границы.
SELECT COUNTIF(increase_candidate AND recommendation = 'INCREASE') AS candidate_as_increase,
       COUNTIF(IFNULL(increase_candidate, FALSE) IS DISTINCT FROM ('INCREASE_CANDIDATE' IN UNNEST(reason_codes))) AS code_mismatch,
       COUNTIF(increase_candidate AND NOT (increase_candidate_realized AND conservative_guard = 'PASS')) AS candidate_without_guards,
       COUNTIF(increase_candidate) AS candidates,
       COUNTIF(increase_candidate_realized) AS realized_candidates,
       IF(COUNTIF(increase_candidate AND recommendation = 'INCREASE') = 0
          AND COUNTIF(IFNULL(increase_candidate, FALSE) IS DISTINCT FROM ('INCREASE_CANDIDATE' IN UNNEST(reason_codes))) = 0
          AND COUNTIF(increase_candidate AND NOT (increase_candidate_realized AND conservative_guard = 'PASS')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;

-- @check AIE_D16_INVENTORY_STATES
-- Наборы — всегда INV_COVER_UNAVAILABLE; без покрытия — INV_COVER_UNAVAILABLE; при незаданных порогах P5 —
-- INV_COVER_POLICY_NOT_SET (LOW / NORMAL / OVERSTOCK не выводятся без порогов).
SELECT COUNTIF(is_bundle AND inventory_cover_state != 'INV_COVER_UNAVAILABLE') AS bundle_with_cover,
       COUNTIF(cover_days IS NULL AND inventory_cover_state != 'INV_COVER_UNAVAILABLE') AS null_cover_with_state,
       COUNTIF(p5_low_cover_days IS NULL AND p5_overstock_cover_days IS NULL
               AND inventory_cover_state NOT IN ('INV_COVER_UNAVAILABLE', 'INV_COVER_POLICY_NOT_SET')) AS state_without_policy,
       COUNTIF(inventory_cover_state = 'INV_COVER_UNAVAILABLE') AS unavailable_rows,
       IF(COUNTIF(is_bundle AND inventory_cover_state != 'INV_COVER_UNAVAILABLE') = 0
          AND COUNTIF(cover_days IS NULL AND inventory_cover_state != 'INV_COVER_UNAVAILABLE') = 0
          AND COUNTIF(p5_low_cover_days IS NULL AND p5_overstock_cover_days IS NULL
               AND inventory_cover_state NOT IN ('INV_COVER_UNAVAILABLE', 'INV_COVER_POLICY_NOT_SET')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;
