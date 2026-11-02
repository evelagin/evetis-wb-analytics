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
-- Заказы слоя = FACT_ORDERS того прогона витрины, что прочитала сборка (срез V4.cohort): живая FACT_ORDERS после
-- пересборки витрины несёт поздние заказы и отмены, которых слой видеть не мог (02.11.2026, 09:33Z против 09:20Z).
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.day, t.nm_id, t.orders_gross_units, t.orders_cancelled_units, t.orders_gross_seller_price_rub, t.orders_cancelled_seller_price_rub,
         IFNULL(o.n, 0) n, IFNULL(o.c, 0) c, IFNULL(o.p, 0) p, IFNULL(o.cp, 0) cp
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t LEFT JOIN (SELECT order_date day, nm_id, COUNT(*) n, COUNTIF(is_cancel) c, SUM(price_with_disc) p, SUM(IF(is_cancel, price_with_disc, 0)) cp
                        -- срез заказов, как в V4.cohort: FACT_ORDERS прогона витрины, который прочитала сборка слоя
                        FROM (
                          SELECT srid AS order_srid, _order_date AS order_date, SAFE_CAST(wb_nm_id AS INT64) AS nm_id,
                                 LOWER(IFNULL(is_cancel, '')) = 'true' AS is_cancel,
                                 SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) AS price_with_disc
                          FROM (
                            SELECT srid, _order_date, wb_nm_id, is_cancel, price_with_disc, ROW_NUMBER() OVER (
                                     PARTITION BY COALESCE(NULLIF(srid, ''), row_hash)
                                     ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC,
                                              SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC) AS rn
                            FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ORDERS`
                            WHERE source_api = 'WB_API_ORDERS'
                              AND PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')
                                  <= (SELECT MAX(r.started_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS` r
                                      WHERE r.environment = 'prod' AND r.status = 'COMPLETE'
                                        AND r.started_at <= (SELECT mart_built_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
                                                             WHERE status = 'SUCCESS' ORDER BY started_at DESC LIMIT 1)))
                          WHERE rn = 1) GROUP BY 1, 2) o USING (day, nm_id)
  WHERE t.orders_covered)
WHERE orders_gross_units <> n OR orders_cancelled_units <> c OR orders_gross_seller_price_rub <> p OR orders_cancelled_seller_price_rub <> cp) = (0
) AS 'V4.orders: заказы/отмены/цена заказов ≠ FACT_ORDERS';

-- V4.sales
-- Остаточная гонка (B2-D4): FACT_SALES и FACT_ADS_SKU_DAILY пересобирает витрина (07/09/12/16 МСК :00), слой — сборка
-- в :20. Между пересборкой витрины и следующей сборкой слоя V4.sales / V4.ads сравнивают слой с более новым фактом.
-- Запускать файл после сборки слоя и до следующей витрины; FAIL в этом окне перепроверить после :20.
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
-- Остаточная гонка с пересборкой витрины — см. V4.sales (B2-D4).
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
-- Сверка с методикой когорты Executive V2 (V_DASH_BUYOUT_COHORT_DAILY) на тех данных, которые прочитала сборка
-- слоя. Живые источники сравнивать нельзя:
--  * продажа, пришедшая между сборкой и проверкой, разрешает заказ только в живой вью (29.10.2026: nm 535581674,
--    заказ 19.10, загрузка 15:22:59Z при старте сборки 15:20:30Z) → продажи срезаются на старт сборки;
--  * пересборка витрины между сборкой слоя и проверкой приносит в FACT_ORDERS поздние заказы и поздние отмены
--    (02.11.2026: витрина 09:33Z, слой 09:20Z — проверка в 09:40Z падала бы) → заказы берутся как FACT_ORDERS
--    того прогона витрины, что дал mart_built_at сборки: V_WB_ORDERS на старте этого прогона.
-- Допуска нет: любое расхождение со срезом — FAIL. Что срез заказов = FACT_ORDERS своего прогона, доказывает
-- V4.orders_method; что методика = живой вью, доказывает V4.cohort_method.
ASSERT (
SELECT COUNT(*) FROM (
  SELECT t.day, SUM(t.cohort_orders) o, SUM(t.cohort_buyout_orders) b, SUM(t.cohort_cancelled_orders) c, SUM(t.cohort_unresolved_orders) u, SUM(t.cohort_conflict_orders) x
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t WHERE t.orders_covered GROUP BY t.day) s
  LEFT JOIN (SELECT o.order_date AS cohort_date, COUNT(*) AS cohort_orders,
         COUNTIF(o.sale_events > 0 AND NOT o.is_cancel) AS buyout_orders,
         COUNTIF(o.sale_events = 0 AND o.is_cancel)     AS cancelled_orders,
         COUNTIF(o.sale_events = 0 AND NOT o.is_cancel) AS unresolved_orders,
         COUNTIF(o.sale_events > 0 AND o.is_cancel)     AS conflict_orders
  FROM (
    SELECT fo.order_date, fo.is_cancel, IFNULL(sr.sale_events, 0) AS sale_events
    -- Срез заказов: V_WB_ORDERS (та же дедупликация) на старте прогона витрины, построившего mart_built_at сборки.
    FROM (
      SELECT srid AS order_srid, _order_date AS order_date, SAFE_CAST(wb_nm_id AS INT64) AS nm_id,
             LOWER(IFNULL(is_cancel, '')) = 'true' AS is_cancel
      FROM (
        SELECT srid, _order_date, wb_nm_id, is_cancel, ROW_NUMBER() OVER (
                 PARTITION BY COALESCE(NULLIF(srid, ''), row_hash)
                 ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC,
                          SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ORDERS`
        WHERE source_api = 'WB_API_ORDERS'
          AND PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')
              <= (SELECT MAX(r.started_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS` r
                  WHERE r.environment = 'prod' AND r.status = 'COMPLETE'
                    AND r.started_at <= (SELECT mart_built_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
                                         WHERE status = 'SUCCESS' ORDER BY started_at DESC LIMIT 1)))
      WHERE rn = 1) fo
    LEFT JOIN (
      SELECT NULLIF(TRIM(srid), '') AS srid, COUNTIF(NOT is_return) AS sale_events
      FROM (
        SELECT srid, is_return, ROW_NUMBER() OVER (
                 PARTITION BY sale_id
                 ORDER BY SAFE_CAST(REPLACE(last_change_date, 'T', ' ') AS TIMESTAMP) DESC,
                          SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_SALES_RETURNS`
        WHERE source_api = 'WB_API_SALES' AND sale_id IS NOT NULL AND TRIM(sale_id) <> ''
          AND processed_status <> 'MISSING_EVENT_KEY'
          -- Срез на старт сборки, давшей слой: строки продаж, пришедшие позже, слой видеть не мог (гонка 29.10).
          AND PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')
              <= (SELECT MAX(started_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG` WHERE status = 'SUCCESS'))
      WHERE rn = 1 AND NULLIF(TRIM(srid), '') IS NOT NULL
      GROUP BY 1) sr ON sr.srid = fo.order_srid
    WHERE fo.nm_id IN (SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL)
  ) o
  GROUP BY o.order_date) e ON e.cohort_date = s.day
  -- LEFT JOIN: сутки слоя, для которых срез не дал ни одного заказа, сравниваются с нулём, а не выпадают.
  WHERE s.o <> IFNULL(e.cohort_orders, 0) OR s.b <> IFNULL(e.buyout_orders, 0) OR s.c <> IFNULL(e.cancelled_orders, 0)
     OR s.u <> IFNULL(e.unresolved_orders, 0) OR s.x <> IFNULL(e.conflict_orders, 0)) = (0
) AS 'V4.cohort: Σ когорт по SKU ≠ когорта Executive V2 по суткам';

-- V4.orders_method
-- Срез заказов V4.cohort воспроизводит FACT_ORDERS построчно: V_WB_ORDERS на старте прогона витрины, который
-- построил живую FACT_ORDERS, = сама FACT_ORDERS (srid, сутки, nmID, отмена, цена). Без этого срез V4.cohort не доказан.
ASSERT (
SELECT COUNT(*) FROM (
      SELECT srid AS order_srid, _order_date AS order_date, SAFE_CAST(wb_nm_id AS INT64) AS nm_id,
             LOWER(IFNULL(is_cancel, '')) = 'true' AS is_cancel,
             SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) AS price_with_disc
      FROM (
        SELECT srid, _order_date, wb_nm_id, is_cancel, price_with_disc, ROW_NUMBER() OVER (
                 PARTITION BY COALESCE(NULLIF(srid, ''), row_hash)
                 ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC,
                          SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ORDERS`
        WHERE source_api = 'WB_API_ORDERS'
          AND PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')
              <= (SELECT started_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS`
                  WHERE run_id = (SELECT ANY_VALUE(mart_run_id) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)))
      WHERE rn = 1) r
  FULL JOIN (SELECT order_srid, order_date, nm_id, is_cancel, price_with_disc, TRUE AS present
             FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`) f USING (order_srid)
  WHERE f.present IS NULL OR r.order_date IS NULL
     OR r.order_date IS DISTINCT FROM f.order_date OR r.nm_id IS DISTINCT FROM f.nm_id
     OR r.is_cancel IS DISTINCT FROM f.is_cancel OR r.price_with_disc IS DISTINCT FROM f.price_with_disc) = (0
) AS 'V4.orders_method: срез заказов ≠ FACT_ORDERS своего прогона витрины';

-- V4.cohort_method
-- Та же методика без среза = живая V_DASH_BUYOUT_COHORT_DAILY по всем суткам и всем исходам (один запрос — один снимок).
ASSERT (
SELECT COUNT(*) FROM (SELECT o.order_date AS cohort_date, COUNT(*) AS cohort_orders,
         COUNTIF(o.sale_events > 0 AND NOT o.is_cancel) AS buyout_orders,
         COUNTIF(o.sale_events = 0 AND o.is_cancel)     AS cancelled_orders,
         COUNTIF(o.sale_events = 0 AND NOT o.is_cancel) AS unresolved_orders,
         COUNTIF(o.sale_events > 0 AND o.is_cancel)     AS conflict_orders
  FROM (
    SELECT fo.order_date, fo.is_cancel, IFNULL(sr.sale_events, 0) AS sale_events
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` fo
    LEFT JOIN (
      SELECT NULLIF(TRIM(srid), '') AS srid, COUNTIF(NOT is_return) AS sale_events
      FROM (
        SELECT srid, is_return, ROW_NUMBER() OVER (
                 PARTITION BY sale_id
                 ORDER BY SAFE_CAST(REPLACE(last_change_date, 'T', ' ') AS TIMESTAMP) DESC,
                          SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC) AS rn
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_SALES_RETURNS`
        WHERE source_api = 'WB_API_SALES' AND sale_id IS NOT NULL AND TRIM(sale_id) <> ''
          AND processed_status <> 'MISSING_EVENT_KEY')
      WHERE rn = 1 AND NULLIF(TRIM(srid), '') IS NOT NULL
      GROUP BY 1) sr ON sr.srid = fo.order_srid
    WHERE fo.nm_id IN (SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL)
  ) o
  GROUP BY o.order_date) r
  FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` e USING (cohort_date)
  WHERE r.cohort_orders IS DISTINCT FROM e.cohort_orders OR r.buyout_orders IS DISTINCT FROM e.buyout_orders
     OR r.cancelled_orders IS DISTINCT FROM e.cancelled_orders OR r.unresolved_orders IS DISTINCT FROM e.unresolved_orders
     OR r.conflict_orders IS DISTINCT FROM e.conflict_orders) = (0
) AS 'V4.cohort_method: методика среза когорты ≠ V_DASH_BUYOUT_COHORT_DAILY';

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
-- Окно из данных, а не из дат: от первых неокончательных суток слоя до последних. Прежнее окно
-- 01–17.09.2026 стало окончательным 22.09 (UBR-011) и падало на любом слое. Нет ни одних предварительных суток
-- (недельный отчёт закрыл всё) — окна нет, проверять нечего; окончательную сторону держит V9.final_window_is_final.
ASSERT (
SELECT (SELECT COUNTIF(NOT IFNULL(finance_is_final, FALSE)) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) = 0
    OR (SELECT LOGICAL_AND(data_status <> 'FINAL') FROM `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(
          (SELECT MIN(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` WHERE NOT IFNULL(finance_is_final, FALSE)),
          (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`)) WHERE is_portfolio_total)
) AS 'V9.provisional_window: окно с предварительными сутками помечено FINAL';

-- V10.price_pairs
-- Цены набора на парах слоя = независимый пересчёт RAW_WB_PRICES (prod, цена продавца задана).
ASSERT (
SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t
  LEFT JOIN (SELECT DATE(observed_at, 'Europe/Moscow') AS day, nm_id, COUNT(*) n,
                    MIN(seller_effective_price) mn, MAX(seller_effective_price) mx
             FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES` WHERE environment = 'prod' AND seller_effective_price IS NOT NULL
             GROUP BY 1, 2) p USING (day, nm_id)
  WHERE IFNULL(t.set_price_observations, 0) <> IFNULL(p.n, 0)
     OR t.set_seller_price_min_rub IS DISTINCT FROM p.mn OR t.set_seller_price_max_rub IS DISTINCT FROM p.mx) = (0
) AS 'V10.price_pairs: цены набора по SKU-дню ≠ RAW_WB_PRICES';

-- V10.price_calendar
-- Наблюдение цены вне строк слоя допустимо только до первого дня календаря SKU (товар выставлен, событий ещё нет)
-- или после окна сборки. Внутри календаря SKU цена выпасть не может. 29.10.2026: 909951444 — 41 сутки
-- 07.09–17.10 (2 907 наблюдений, ≈ 71 в сутки) до первого события 18.10; календарь MART начинается с него.
ASSERT (
SELECT COUNT(*) FROM (SELECT DISTINCT DATE(observed_at, 'Europe/Moscow') AS day, nm_id
                      FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES` WHERE environment = 'prod' AND seller_effective_price IS NOT NULL) p
  JOIN (SELECT nm_id, MIN(day) AS first_day, MAX(day) AS last_day FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` GROUP BY nm_id) f USING (nm_id)
  LEFT JOIN (SELECT day, nm_id FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`) k USING (day, nm_id)
  WHERE k.nm_id IS NULL AND p.day BETWEEN f.first_day AND f.last_day) = (0
) AS 'V10.price_calendar: наблюдение цены внутри календаря SKU выпало из слоя';
