-- ============================================================================
-- SKU PERFORMANCE V2 · PHASE B — валидация слоя (запускать после сборки)
-- Все сверки динамические: слой против НЕЗАВИСИМОГО пересчёта из источников, без зашитых сумм.
-- Каждая проверка читает не больше одного тяжёлого источника (иначе «query is too complex»).
-- Окна регрессии: 31.08–13.09, 01–14.09 (proof Phase A), 17–30.08 и 27.07–23.08 (окончательные),
-- 01–17.09 (с предварительными сутками), вся история.
-- ============================================================================
-- V1
ASSERT (
SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)) AND COUNTIF(day IS NULL OR nm_id IS NULL) = 0 FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`
) AS 'V1: грейн day × nm_id';

-- V2
ASSERT (
SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`) AND (SELECT COUNT(*) FROM (SELECT FORMAT('%t|%t', day, nm_id) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` EXCEPT DISTINCT SELECT FORMAT('%t|%t', day, nm_id) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`)) = 0
) AS 'V2: грейн ≠ MART_SKU_DAILY';

-- V3
ASSERT (
SELECT COUNT(DISTINCT nm_id) = (SELECT COUNT(*) FROM (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL)) AND COUNTIF(internal_sku IS NULL OR is_bundle IS NULL OR product_name_short IS NULL) = 0 AND COUNT(DISTINCT IF(is_bundle, nm_id, NULL)) > 0 FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`
) AS 'V3: universe/атрибуты/наборы';

-- V4.orders
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.day, t.nm_id, t.orders_gross_units, t.orders_cancelled_units, t.orders_gross_seller_price_rub, t.orders_cancelled_seller_price_rub,
         IFNULL(o.n, 0) n, IFNULL(o.c, 0) c, IFNULL(o.p, 0) p, IFNULL(o.cp, 0) cp
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t LEFT JOIN (SELECT order_date day, nm_id, COUNT(*) n, COUNTIF(is_cancel) c, SUM(price_with_disc) p, SUM(IF(is_cancel, price_with_disc, 0)) cp
                        FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` GROUP BY 1, 2) o USING (day, nm_id)
  WHERE t.orders_covered)
WHERE orders_gross_units <> n OR orders_cancelled_units <> c OR orders_gross_seller_price_rub <> p OR orders_cancelled_seller_price_rub <> cp) = (0
) AS 'V4.orders: заказы/отмены/цена заказов ≠ FACT_ORDERS';

-- V4.sales
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.*, IFNULL(s.n, 0) n, IFNULL(s.p, 0) p, IFNULL(s.bp, 0) bp, IFNULL(s.bpn, 0) bpn
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t LEFT JOIN (SELECT sale_date day, nm_id, COUNTIF(NOT is_return) n, SUM(IF(NOT is_return, price_with_disc, 0)) p,
                               SUM(IF(NOT is_return AND finished_price IS NOT NULL, finished_price, 0)) bp, COUNTIF(NOT is_return AND finished_price IS NOT NULL) bpn
                        FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES` GROUP BY 1, 2) s USING (day, nm_id)
  WHERE t.sales_covered)
WHERE buyout_units <> n OR buyout_seller_price_rub <> p OR buyout_buyer_paid_rub <> bp OR buyout_buyer_paid_units <> bpn) = (0
) AS 'V4.sales: выкупы/цена/цена покупателя ≠ FACT_SALES';

-- V4.finance
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.*, IFNULL(f.su, 0) su, IFNULL(f.ru, 0) ru, IFNULL(f.sp, 0) sp, IFNULL(f.fp, 0) fp, IFNULL(f.bp, 0) bp
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t LEFT JOIN (SELECT finance_date day, nm_id, COUNTIF(supplier_oper_name = 'Продажа') su, COUNTIF(supplier_oper_name = 'Возврат') ru,
                               SUM(IF(supplier_oper_name = 'Возврат', -1, 1) * seller_price_rub) sp, SUM(IF(supplier_oper_name = 'Возврат', -1, 1) * for_pay_rub) fp,
                               SUM(IF(supplier_oper_name = 'Возврат', -1, 1) * buyer_paid_rub) bp
                        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS` WHERE in_mart_universe GROUP BY 1, 2) f USING (day, nm_id)
  WHERE t.finance_covered)
WHERE fin_sale_units <> su OR fin_return_units <> ru OR fin_seller_price_rub <> sp OR credited_for_goods_rub <> fp OR fin_buyer_paid_rub <> bp) = (0
) AS 'V4.finance: финотчёт (нетто) ≠ V_WB_FINANCE_PRICE_COMPONENTS';

-- V4.logistics
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.logistics_rub, IFNULL(l.x, 0) x FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t
  LEFT JOIN (SELECT finance_date day, nm_id, SUM(cost_amount_positive) x FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
             WHERE cost_category = 'logistics' AND is_sku_row AND cost_amount_positive IS NOT NULL GROUP BY 1, 2) l USING (day, nm_id)
  WHERE t.finance_covered) WHERE logistics_rub <> x) = (0
) AS 'V4.logistics: логистика ≠ LONG_MAPPED';

-- V4.storage
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.storage_sku_rub, IFNULL(s.x, 0) x FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t
  LEFT JOIN (SELECT date_msk day, nm_id, SUM(storage_rub_exact) x FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` GROUP BY 1, 2) s USING (day, nm_id)
  WHERE t.storage_sku_covered) WHERE storage_sku_rub <> x) = (0
) AS 'V4.storage: хранение ≠ отчёт платного хранения';

-- V4.ads
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.ads_attributed_rub, IFNULL(a.x, 0) x FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t
  LEFT JOIN (SELECT `date` day, nm_id, SUM(stats_spend_rub) x FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` GROUP BY 1, 2) a USING (day, nm_id)
  WHERE t.ads_covered) WHERE ABS(ads_attributed_rub - x) > 0.000001) = (0
) AS 'V4.ads: реклама ≠ FACT_ADS_SKU_DAILY';

-- V4.cogs
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS` c USING (day, nm_id)
  WHERE t.sales_covered AND t.cogs_rub IS DISTINCT FROM c.net_product_cogs_operational_rub) = (0
) AS 'V4.cogs: себестоимость ≠ V_MART_SKU_DAILY_COGS';

-- V4.cohort
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.day, SUM(t.cohort_orders) o, SUM(t.cohort_buyout_orders) b, SUM(t.cohort_cancelled_orders) c, SUM(t.cohort_unresolved_orders) u, SUM(t.cohort_conflict_orders) x
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t WHERE t.orders_covered GROUP BY t.day) s
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` e ON e.cohort_date = s.day
  WHERE s.o <> e.cohort_orders OR s.b <> e.buyout_orders OR s.c <> e.cancelled_orders OR s.u <> e.unresolved_orders OR s.x <> e.conflict_orders) = (0
) AS 'V4.cohort: Σ когорт по SKU ≠ когорта Executive V2 по суткам';

-- V5.chain
ASSERT (
SELECT COUNTIF(price_chain_complete AND ABS((fin_seller_price_rub - credited_for_goods_rub) - (fin_spp_rub + chain_wb_remuneration_rub + chain_wb_remuneration_vat_rub + chain_acquiring_rub + chain_pvz_reward_rub + chain_rounding_rub)) > 0.005) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V5.chain: тождество цепочки цены';

-- V5.spp
ASSERT (
SELECT COUNTIF(finance_covered AND ABS(fin_spp_rub - (fin_seller_price_rub - fin_buyer_paid_rub)) > 0.005) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V5.spp: СПП ≠ цена продавца − цена покупателя';

-- V5.cohort
ASSERT (
SELECT COUNTIF(orders_covered AND (cohort_orders <> orders_gross_units OR cohort_resolved_orders <> cohort_buyout_orders + cohort_cancelled_orders OR cohort_orders <> cohort_resolved_orders + cohort_unresolved_orders + cohort_conflict_orders)) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V5.cohort: разбиение когорты';

-- V5.legs
ASSERT (
SELECT COUNTIF(finance_covered AND ABS(logistics_rub - (logistics_sold_rub + logistics_cancel_to_customer_rub + logistics_cancel_from_customer_rub + logistics_customer_return_rub + logistics_defect_return_rub + logistics_correction_rub + logistics_unlabeled_rub + logistics_other_label_rub)) > 0.005) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V5.legs: виды логистики ≠ итог';

-- V5.contribution
ASSERT (
SELECT COUNTIF(IF(finance_covered AND ads_covered, credited_for_goods_rub - logistics_rub - IFNULL(storage_sku_rub, 0) - ads_attributed_rub, NULL) IS DISTINCT FROM contribution_before_cogs_rub)
     + COUNTIF(IF(finance_covered AND ads_covered AND sales_covered AND cogs_rub IS NOT NULL, contribution_before_cogs_rub - cogs_rub, NULL) IS DISTINCT FROM contribution_after_cogs_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V5.contribution: формула вклада';

-- V6.chain_boundary
ASSERT (
SELECT COUNTIF(day < '2026-07-13' AND finance_covered AND fin_sale_units + fin_return_units > 0 AND chain_wb_remuneration_rub IS NOT NULL)
     + COUNTIF(day >= '2026-07-13' AND finance_covered AND NOT price_chain_complete) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V6.chain_boundary: граница цепочки 13.07.2026';

-- V6.storage_boundary
ASSERT (
SELECT COUNTIF(day < '2026-09-01' AND storage_sku_rub IS NOT NULL) + COUNTIF(storage_sku_covered AND storage_sku_rub IS NULL) + COUNTIF(NOT storage_sku_covered AND storage_sku_rub IS NOT NULL) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V6.storage_boundary: граница хранения 01.09.2026 / NULL вне покрытия';

-- V6.null_zero
ASSERT (
SELECT COUNTIF(NOT orders_covered AND (orders_gross_units IS NOT NULL OR cohort_orders IS NOT NULL))
     + COUNTIF(orders_covered AND (orders_gross_units IS NULL OR cohort_orders IS NULL))
     + COUNTIF(NOT sales_covered AND buyout_units IS NOT NULL) + COUNTIF(sales_covered AND buyout_units IS NULL)
     + COUNTIF(NOT finance_covered AND (credited_for_goods_rub IS NOT NULL OR logistics_rub IS NOT NULL)) + COUNTIF(finance_covered AND credited_for_goods_rub IS NULL)
     + COUNTIF(NOT ads_covered AND ads_attributed_rub IS NOT NULL) + COUNTIF(ads_covered AND ads_attributed_rub IS NULL)
     + COUNTIF(finance_covered AND fin_sale_units + fin_return_units = 0 AND chain_wb_remuneration_rub IS NOT NULL AND chain_wb_remuneration_rub <> 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = (0
) AS 'V6.null_zero: NULL вне покрытия / не NULL в покрытии';

-- V7.bridge
ASSERT (
SELECT COUNTIF(ABS(residual_pre_cogs_rub) > 0.005) + COUNTIF(in_executive_result AND bridge_result_pre_cogs_rub IS NULL) + COUNTIF(ABS(IFNULL(cogs_difference_rub, 0)) > 0.005) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY`) = (0
) AS 'V7.bridge: мост SKU → Executive: остаток ≠ 0';

-- V7.bridge_sum
ASSERT (
SELECT ABS(SUM(residual_pre_cogs_rub)) < 0.01 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY`
) AS 'V7.bridge_sum: накопленный остаток моста';

-- V8.W1.avg
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-31', DATE '2026-09-13') f
  LEFT JOIN (SELECT nm_id, COUNT(*) n, SUM(price_with_disc) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
             WHERE order_date BETWEEN '2026-08-31' AND '2026-09-13' AND nm_id IN (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL) GROUP BY ROLLUP(nm_id)) o ON o.nm_id IS NOT DISTINCT FROM f.nm_id
  WHERE f.cur_avg_seller_price_orders_rub IS DISTINCT FROM SAFE_DIVIDE(o.p, NULLIF(o.n, 0))
     OR f.cur_orders_gross_units IS DISTINCT FROM IFNULL(o.n, IF(f.cur_orders_cov_days > 0, 0, NULL))) = (0
) AS 'V8.W1.avg: 2026-08-31–2026-09-13: средняя цена заказов ≠ SUM/COUNT FACT_ORDERS';

-- V8.W1.total
ASSERT (
SELECT COUNTIF(ABS(IFNULL(t.cur_contribution_after_cogs_rub, 0) - IFNULL(s.x, 0)) > 0.005 OR t.cur_orders_gross_units IS DISTINCT FROM s.o OR t.cur_buyout_units IS DISTINCT FROM s.b
             OR ABS(IFNULL(t.cur_credited_for_goods_rub, 0) - IFNULL(s.c, 0)) > 0.005 OR ABS(IFNULL(t.cur_ads_attributed_rub, 0) - IFNULL(s.a, 0)) > 0.005)
  FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-31', DATE '2026-09-13') WHERE is_portfolio_total) t
  CROSS JOIN (SELECT SUM(cur_contribution_after_cogs_rub) x, SUM(cur_orders_gross_units) o, SUM(cur_buyout_units) b, SUM(cur_credited_for_goods_rub) c, SUM(cur_ads_attributed_rub) a
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-31', DATE '2026-09-13') WHERE NOT is_portfolio_total) s) = (0
) AS 'V8.W1.total: 2026-08-31–2026-09-13: итог портфеля ≠ Σ SKU';

-- V8.W1.ratios
ASSERT (
SELECT COUNTIF(cur_cohort_buyout_rate IS DISTINCT FROM SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))
     OR cur_drr IS DISTINCT FROM SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))
     OR cur_contribution_margin_after_cogs IS DISTINCT FROM SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))
     OR period_days <> DATE_DIFF(period_to, period_from, DAY) + 1 OR DATE_DIFF(prev_to, prev_from, DAY) + 1 <> period_days OR prev_to <> DATE_SUB(period_from, INTERVAL 1 DAY)
     OR low_sample IS DISTINCT FROM (IFNULL(cur_orders_gross_units, 0) < min_reliable_units AND IFNULL(cur_buyout_units, 0) < min_reliable_units)
     OR (low_sample AND cur_orders_gross_units IS NULL AND cur_orders_cov_days > 0))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-31', DATE '2026-09-13')) = (0
) AS 'V8.W1.ratios: 2026-08-31–2026-09-13: ratio-of-sums / окна / малая выборка';

-- V8.W2.avg
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-14') f
  LEFT JOIN (SELECT nm_id, COUNT(*) n, SUM(price_with_disc) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
             WHERE order_date BETWEEN '2026-09-01' AND '2026-09-14' AND nm_id IN (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL) GROUP BY ROLLUP(nm_id)) o ON o.nm_id IS NOT DISTINCT FROM f.nm_id
  WHERE f.cur_avg_seller_price_orders_rub IS DISTINCT FROM SAFE_DIVIDE(o.p, NULLIF(o.n, 0))
     OR f.cur_orders_gross_units IS DISTINCT FROM IFNULL(o.n, IF(f.cur_orders_cov_days > 0, 0, NULL))) = (0
) AS 'V8.W2.avg: 2026-09-01–2026-09-14: средняя цена заказов ≠ SUM/COUNT FACT_ORDERS';

-- V8.W2.total
ASSERT (
SELECT COUNTIF(ABS(IFNULL(t.cur_contribution_after_cogs_rub, 0) - IFNULL(s.x, 0)) > 0.005 OR t.cur_orders_gross_units IS DISTINCT FROM s.o OR t.cur_buyout_units IS DISTINCT FROM s.b
             OR ABS(IFNULL(t.cur_credited_for_goods_rub, 0) - IFNULL(s.c, 0)) > 0.005 OR ABS(IFNULL(t.cur_ads_attributed_rub, 0) - IFNULL(s.a, 0)) > 0.005)
  FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-14') WHERE is_portfolio_total) t
  CROSS JOIN (SELECT SUM(cur_contribution_after_cogs_rub) x, SUM(cur_orders_gross_units) o, SUM(cur_buyout_units) b, SUM(cur_credited_for_goods_rub) c, SUM(cur_ads_attributed_rub) a
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-14') WHERE NOT is_portfolio_total) s) = (0
) AS 'V8.W2.total: 2026-09-01–2026-09-14: итог портфеля ≠ Σ SKU';

-- V8.W2.ratios
ASSERT (
SELECT COUNTIF(cur_cohort_buyout_rate IS DISTINCT FROM SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))
     OR cur_drr IS DISTINCT FROM SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))
     OR cur_contribution_margin_after_cogs IS DISTINCT FROM SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))
     OR period_days <> DATE_DIFF(period_to, period_from, DAY) + 1 OR DATE_DIFF(prev_to, prev_from, DAY) + 1 <> period_days OR prev_to <> DATE_SUB(period_from, INTERVAL 1 DAY)
     OR low_sample IS DISTINCT FROM (IFNULL(cur_orders_gross_units, 0) < min_reliable_units AND IFNULL(cur_buyout_units, 0) < min_reliable_units)
     OR (low_sample AND cur_orders_gross_units IS NULL AND cur_orders_cov_days > 0))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-14')) = (0
) AS 'V8.W2.ratios: 2026-09-01–2026-09-14: ratio-of-sums / окна / малая выборка';

-- V8.W3.avg
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-17', DATE '2026-08-30') f
  LEFT JOIN (SELECT nm_id, COUNT(*) n, SUM(price_with_disc) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
             WHERE order_date BETWEEN '2026-08-17' AND '2026-08-30' AND nm_id IN (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL) GROUP BY ROLLUP(nm_id)) o ON o.nm_id IS NOT DISTINCT FROM f.nm_id
  WHERE f.cur_avg_seller_price_orders_rub IS DISTINCT FROM SAFE_DIVIDE(o.p, NULLIF(o.n, 0))
     OR f.cur_orders_gross_units IS DISTINCT FROM IFNULL(o.n, IF(f.cur_orders_cov_days > 0, 0, NULL))) = (0
) AS 'V8.W3.avg: 2026-08-17–2026-08-30: средняя цена заказов ≠ SUM/COUNT FACT_ORDERS';

-- V8.W3.total
ASSERT (
SELECT COUNTIF(ABS(IFNULL(t.cur_contribution_after_cogs_rub, 0) - IFNULL(s.x, 0)) > 0.005 OR t.cur_orders_gross_units IS DISTINCT FROM s.o OR t.cur_buyout_units IS DISTINCT FROM s.b
             OR ABS(IFNULL(t.cur_credited_for_goods_rub, 0) - IFNULL(s.c, 0)) > 0.005 OR ABS(IFNULL(t.cur_ads_attributed_rub, 0) - IFNULL(s.a, 0)) > 0.005)
  FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-17', DATE '2026-08-30') WHERE is_portfolio_total) t
  CROSS JOIN (SELECT SUM(cur_contribution_after_cogs_rub) x, SUM(cur_orders_gross_units) o, SUM(cur_buyout_units) b, SUM(cur_credited_for_goods_rub) c, SUM(cur_ads_attributed_rub) a
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-17', DATE '2026-08-30') WHERE NOT is_portfolio_total) s) = (0
) AS 'V8.W3.total: 2026-08-17–2026-08-30: итог портфеля ≠ Σ SKU';

-- V8.W3.ratios
ASSERT (
SELECT COUNTIF(cur_cohort_buyout_rate IS DISTINCT FROM SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))
     OR cur_drr IS DISTINCT FROM SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))
     OR cur_contribution_margin_after_cogs IS DISTINCT FROM SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))
     OR period_days <> DATE_DIFF(period_to, period_from, DAY) + 1 OR DATE_DIFF(prev_to, prev_from, DAY) + 1 <> period_days OR prev_to <> DATE_SUB(period_from, INTERVAL 1 DAY)
     OR low_sample IS DISTINCT FROM (IFNULL(cur_orders_gross_units, 0) < min_reliable_units AND IFNULL(cur_buyout_units, 0) < min_reliable_units)
     OR (low_sample AND cur_orders_gross_units IS NULL AND cur_orders_cov_days > 0))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-17', DATE '2026-08-30')) = (0
) AS 'V8.W3.ratios: 2026-08-17–2026-08-30: ratio-of-sums / окна / малая выборка';

-- V8.W4.avg
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-07-27', DATE '2026-08-23') f
  LEFT JOIN (SELECT nm_id, COUNT(*) n, SUM(price_with_disc) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
             WHERE order_date BETWEEN '2026-07-27' AND '2026-08-23' AND nm_id IN (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL) GROUP BY ROLLUP(nm_id)) o ON o.nm_id IS NOT DISTINCT FROM f.nm_id
  WHERE f.cur_avg_seller_price_orders_rub IS DISTINCT FROM SAFE_DIVIDE(o.p, NULLIF(o.n, 0))
     OR f.cur_orders_gross_units IS DISTINCT FROM IFNULL(o.n, IF(f.cur_orders_cov_days > 0, 0, NULL))) = (0
) AS 'V8.W4.avg: 2026-07-27–2026-08-23: средняя цена заказов ≠ SUM/COUNT FACT_ORDERS';

-- V8.W4.total
ASSERT (
SELECT COUNTIF(ABS(IFNULL(t.cur_contribution_after_cogs_rub, 0) - IFNULL(s.x, 0)) > 0.005 OR t.cur_orders_gross_units IS DISTINCT FROM s.o OR t.cur_buyout_units IS DISTINCT FROM s.b
             OR ABS(IFNULL(t.cur_credited_for_goods_rub, 0) - IFNULL(s.c, 0)) > 0.005 OR ABS(IFNULL(t.cur_ads_attributed_rub, 0) - IFNULL(s.a, 0)) > 0.005)
  FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-07-27', DATE '2026-08-23') WHERE is_portfolio_total) t
  CROSS JOIN (SELECT SUM(cur_contribution_after_cogs_rub) x, SUM(cur_orders_gross_units) o, SUM(cur_buyout_units) b, SUM(cur_credited_for_goods_rub) c, SUM(cur_ads_attributed_rub) a
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-07-27', DATE '2026-08-23') WHERE NOT is_portfolio_total) s) = (0
) AS 'V8.W4.total: 2026-07-27–2026-08-23: итог портфеля ≠ Σ SKU';

-- V8.W4.ratios
ASSERT (
SELECT COUNTIF(cur_cohort_buyout_rate IS DISTINCT FROM SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))
     OR cur_drr IS DISTINCT FROM SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))
     OR cur_contribution_margin_after_cogs IS DISTINCT FROM SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))
     OR period_days <> DATE_DIFF(period_to, period_from, DAY) + 1 OR DATE_DIFF(prev_to, prev_from, DAY) + 1 <> period_days OR prev_to <> DATE_SUB(period_from, INTERVAL 1 DAY)
     OR low_sample IS DISTINCT FROM (IFNULL(cur_orders_gross_units, 0) < min_reliable_units AND IFNULL(cur_buyout_units, 0) < min_reliable_units)
     OR (low_sample AND cur_orders_gross_units IS NULL AND cur_orders_cov_days > 0))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-07-27', DATE '2026-08-23')) = (0
) AS 'V8.W4.ratios: 2026-07-27–2026-08-23: ratio-of-sums / окна / малая выборка';

-- V8.W5.avg
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-17') f
  LEFT JOIN (SELECT nm_id, COUNT(*) n, SUM(price_with_disc) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
             WHERE order_date BETWEEN '2026-09-01' AND '2026-09-17' AND nm_id IN (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace='WB' AND active AND nm_id IS NOT NULL) GROUP BY ROLLUP(nm_id)) o ON o.nm_id IS NOT DISTINCT FROM f.nm_id
  WHERE f.cur_avg_seller_price_orders_rub IS DISTINCT FROM SAFE_DIVIDE(o.p, NULLIF(o.n, 0))
     OR f.cur_orders_gross_units IS DISTINCT FROM IFNULL(o.n, IF(f.cur_orders_cov_days > 0, 0, NULL))) = (0
) AS 'V8.W5.avg: 2026-09-01–2026-09-17: средняя цена заказов ≠ SUM/COUNT FACT_ORDERS';

-- V8.W5.total
ASSERT (
SELECT COUNTIF(ABS(IFNULL(t.cur_contribution_after_cogs_rub, 0) - IFNULL(s.x, 0)) > 0.005 OR t.cur_orders_gross_units IS DISTINCT FROM s.o OR t.cur_buyout_units IS DISTINCT FROM s.b
             OR ABS(IFNULL(t.cur_credited_for_goods_rub, 0) - IFNULL(s.c, 0)) > 0.005 OR ABS(IFNULL(t.cur_ads_attributed_rub, 0) - IFNULL(s.a, 0)) > 0.005)
  FROM (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-17') WHERE is_portfolio_total) t
  CROSS JOIN (SELECT SUM(cur_contribution_after_cogs_rub) x, SUM(cur_orders_gross_units) o, SUM(cur_buyout_units) b, SUM(cur_credited_for_goods_rub) c, SUM(cur_ads_attributed_rub) a
              FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-17') WHERE NOT is_portfolio_total) s) = (0
) AS 'V8.W5.total: 2026-09-01–2026-09-17: итог портфеля ≠ Σ SKU';

-- V8.W5.ratios
ASSERT (
SELECT COUNTIF(cur_cohort_buyout_rate IS DISTINCT FROM SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))
     OR cur_drr IS DISTINCT FROM SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))
     OR cur_contribution_margin_after_cogs IS DISTINCT FROM SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))
     OR period_days <> DATE_DIFF(period_to, period_from, DAY) + 1 OR DATE_DIFF(prev_to, prev_from, DAY) + 1 <> period_days OR prev_to <> DATE_SUB(period_from, INTERVAL 1 DAY)
     OR low_sample IS DISTINCT FROM (IFNULL(cur_orders_gross_units, 0) < min_reliable_units AND IFNULL(cur_buyout_units, 0) < min_reliable_units)
     OR (low_sample AND cur_orders_gross_units IS NULL AND cur_orders_cov_days > 0))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-17')) = (0
) AS 'V8.W5.ratios: 2026-09-01–2026-09-17: ratio-of-sums / окна / малая выборка';

-- V9.status
ASSERT (
SELECT COUNTIF(data_status NOT IN ('FINAL', 'PROVISIONAL', 'INCOMPLETE'))
     + COUNTIF(data_status = 'FINAL' AND ARRAY_LENGTH(data_status_reasons) > 0 AND NOT low_sample AND storage_complete)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-08-17', DATE '2026-08-30')) = (0
) AS 'V9.status: статус данных';

-- V9.final_window_is_final
ASSERT (
SELECT LOGICAL_AND(data_status = 'FINAL') FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-07-27', DATE '2026-08-23') WHERE is_portfolio_total
) AS 'V9.final_window_is_final: окончательный период не FINAL';

-- V9.provisional_window
ASSERT (
SELECT LOGICAL_AND(data_status <> 'FINAL') FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '2026-09-01', DATE '2026-09-17') WHERE is_portfolio_total
) AS 'V9.provisional_window: окно с предварительными сутками помечено FINAL';
