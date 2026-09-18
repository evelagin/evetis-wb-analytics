-- ============================================================================
-- SKU PERFORMANCE V2 · PHASE B — материализованный слой day × SKU
--
-- Документ: docs/SKU_PERFORMANCE_V2_PHASE_B_BACKEND_2026-09-18.md
-- Аудит:    docs/SKU_PERFORMANCE_V2_PHASE_A_AUDIT_2026-09-18.md
-- Валидация: sql/dash/sku_performance_v2_daily_validation.sql
-- Откат:     sql/rollback/sku_performance_v2_daily_2026-09-18/R_DROP_LAYER.sql
--
-- Шаблон — Executive V2 Phase C2 (sql/dash/executive_v2_daily_v1.sql): TEMP → ASSERT → транзакция,
-- журнал сборок, замок. Executive V2 не читается напрямую из канонических view результата/экономики/расчётов
-- (их потребители закрыты валидациями C-17/D-12/G-13) — мост к Executive читает V_DASH_EXECUTIVE_V2_DAILY.
--
-- КОНТРАКТ (кратко; полностью — в документе):
--   • в таблице только СУММЫ и КОЛИЧЕСТВА; средние, доли, Δ — ratio-of-sums в TVF/карточках;
--   • 1 строка источника = 1 единица (quantity = 1 доказано в заказах, продажах и финотчёте);
--   • цена продавца = price_with_disc / retail_price_withdisc_rub; цена покупателя = finished_price / retailAmount;
--     начисление = финотчёт for_pay (Statistics for_pay не используется);
--   • строки «Возврат» финотчёта WB отдаёт с положительными суммами — здесь они ВЫЧИТАЮТСЯ (нетто);
--   • компоненты цепочки (vw, vwNds, acquiringFee, ppvzReward) есть с 13.07.2026; при наличии строк
--     без цепочки компонент = NULL, а не 0;
--   • хранение по SKU — отчёт платного хранения (с 01.09.2026); вне покрытия NULL, распределения нет;
--   • реклама — атрибуция (SKU Performance), не биллинг;
--   • себестоимость — операционная (дата выкупа, нетто сторно), как Executive V2; по дате финотчёта — справочно;
--   • расходы уровня счёта (тарифная опция, штрафы, утилизация, прочие удержания, компенсации) по SKU
--     НЕ распределяются — они в мосте V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY.
--
--   Вклад SKU до себестоимости   = начисление за товар (нетто) − логистика SKU − хранение SKU (где покрыто) − реклама (атрибуция)
--   Вклад SKU после себестоимости = вклад до себестоимости − себестоимость
-- ============================================================================

-- ── §1. Журнал, замок, конфигурация ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG` (
  run_id               STRING    NOT NULL,
  trigger_source       STRING,
  started_at           TIMESTAMP NOT NULL,
  finished_at          TIMESTAMP,
  status               STRING    NOT NULL,   -- STARTED | SUCCESS | FAILED | SKIPPED_LOCKED
  rows_built           INT64,
  source_data_through  DATE,
  mart_built_at        TIMESTAMP,
  error_message        STRING
)
OPTIONS (description = 'SKU Performance V2 · журнал сборок SKU_PERFORMANCE_V2_DAILY (sp_build_sku_performance_v2_daily).');

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK` (
  lock_id      STRING NOT NULL,
  is_running   BOOL   NOT NULL,
  run_id       STRING,
  acquired_at  TIMESTAMP,
  released_at  TIMESTAMP
)
OPTIONS (description = 'SKU Performance V2 · замок сборки: одна строка lock_id = sku_performance_v2_daily.');

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK` (lock_id, is_running)
SELECT 'sku_performance_v2_daily', FALSE FROM (SELECT 1)
WHERE NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK`
                  WHERE lock_id = 'sku_performance_v2_daily');

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_CONFIG` (
  config_key    STRING NOT NULL,
  config_value  NUMERIC NOT NULL,
  note          STRING,
  updated_at    TIMESTAMP
)
OPTIONS (description = 'SKU Performance V2 · параметры надёжности. Порог — начальный, не вечное бизнес-правило.');

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_CONFIG` (config_key, config_value, note, updated_at)
SELECT k, v, n, CURRENT_TIMESTAMP() FROM UNNEST([
  STRUCT('min_reliable_units' AS k, NUMERIC '10' AS v,
         'Минимум релевантных единиц (заказов / выкупов / завершённых когорт) в КАЖДОМ окне для reliability, сравнения и правил внимания. Фактические значения не скрываются. Решение владельца 2026-09-18, начальное значение.' AS n)
]) WHERE NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_CONFIG` WHERE config_key = k);

-- ── §2. Процедура сборки ────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_sku_performance_v2_daily`(in_trigger STRING)
BEGIN
  DECLARE v_run_id  STRING    DEFAULT GENERATE_UUID();
  DECLARE v_started TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_rows    INT64;
  DECLARE v_through DATE;
  DECLARE v_mart_at TIMESTAMP;
  DECLARE v_in_tx   BOOL DEFAULT FALSE;   -- @@transaction_id недоступен в обработчике исключений
  DECLARE v_exists  BOOL;

  INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
    (run_id, trigger_source, started_at, status)
  VALUES (v_run_id, IFNULL(in_trigger, 'manual'), v_started, 'STARTED');

  UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'sku_performance_v2_daily'
     AND (NOT is_running OR acquired_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 MINUTE));
  IF @@row_count = 0 THEN
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
       SET status = 'SKIPPED_LOCKED', finished_at = CURRENT_TIMESTAMP(), error_message = 'предыдущая сборка ещё выполняется'
     WHERE run_id = v_run_id;
    RETURN;
  END IF;

  BEGIN
    -- 1) Сборка во временную таблицу. Единственный экземпляр тела сборки.
    CREATE TEMP TABLE b AS
    WITH
    u AS (
      SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku, ANY_VALUE(product_name_short) AS product_name_short,
             ANY_VALUE(product_name_full) AS product_name_full, ANY_VALUE(is_bundle) AS is_bundle
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
      WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
      GROUP BY nm_id
    ),
    -- Грейн: плотный календарь MART_SKU_DAILY (universe × дни от первого события SKU до build_as_of).
    spine AS (
      SELECT day, nm_id, ad_spend, views, clicks
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
    ),
    cov AS (
      SELECT day, orders_covered, sales_covered, ads_covered, finance_covered, finance_is_final, contains_provisional_finance
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_COVERAGE_DAILY`
    ),
    ord AS (
      SELECT order_date AS day, nm_id,
             COUNT(*)                               AS gross_units,
             COUNTIF(is_cancel)                     AS cancelled_units,
             SUM(price_with_disc)                   AS gross_price,
             SUM(IF(is_cancel, price_with_disc, 0)) AS cancelled_price
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
      GROUP BY 1, 2
    ),
    -- Когорта: методология V_DASH_BUYOUT_COHORT_DAILY (Executive V2) на грейне SKU.
    srs AS (
      SELECT NULLIF(TRIM(srid), '') AS srid, COUNTIF(NOT is_return) AS sale_events, COUNTIF(is_return) AS return_events
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
      WHERE NULLIF(TRIM(srid), '') IS NOT NULL
      GROUP BY 1
    ),
    coh AS (
      SELECT day, nm_id,
             COUNT(*)                                       AS cohort_orders,
             COUNTIF(oc = 'BUYOUT')                         AS cohort_buyout_orders,
             COUNTIF(oc = 'CANCELLED')                      AS cohort_cancelled_orders,
             COUNTIF(oc = 'UNRESOLVED')                     AS cohort_unresolved_orders,
             COUNTIF(oc = 'CONFLICT')                       AS cohort_conflict_orders,
             COUNTIF(oc = 'BUYOUT' AND return_events > 0)   AS cohort_returned_after_buyout_orders
      FROM (
        SELECT o.order_date AS day, o.nm_id, IFNULL(s.return_events, 0) AS return_events,
               CASE WHEN IFNULL(s.sale_events, 0) > 0 AND o.is_cancel THEN 'CONFLICT'
                    WHEN IFNULL(s.sale_events, 0) > 0                 THEN 'BUYOUT'
                    WHEN o.is_cancel                                  THEN 'CANCELLED'
                    ELSE                                                   'UNRESOLVED' END AS oc
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` o
        LEFT JOIN srs s ON s.srid = o.order_srid
      )
      GROUP BY 1, 2
    ),
    sal AS (
      SELECT sale_date AS day, nm_id,
             COUNTIF(NOT is_return)                                        AS buyout_units,
             SUM(IF(NOT is_return, price_with_disc, 0))                    AS buyout_seller_price,
             SUM(IF(NOT is_return AND finished_price IS NOT NULL, finished_price, 0)) AS buyout_buyer_paid,
             COUNTIF(NOT is_return AND finished_price IS NOT NULL)         AS buyout_buyer_paid_units,
             COUNTIF(is_return)                                            AS return_units
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES`
      GROUP BY 1, 2
    ),
    -- Цепочка цены: финотчёт, дата финотчёта, Продажа (+) / Возврат (−), SKU-строки универсума.
    finr AS (
      SELECT finance_date AS day, nm_id, price_chain_available AS ch,
             IF(supplier_oper_name = 'Возврат', -1, 1) AS sg, supplier_oper_name AS op,
             seller_price_rub, buyer_paid_rub, for_pay_rub, spp_rub,
             wb_remuneration_rub, wb_remuneration_vat_rub, acquiring_rub, pvz_reward_rub, price_chain_rounding_rub,
             marketplace_fee_gap_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`
      WHERE in_mart_universe
    ),
    fin AS (
      SELECT day, nm_id,
             COUNTIF(op = 'Продажа')                      AS fin_sale_units,
             COUNTIF(op = 'Возврат')                      AS fin_return_units,
             COUNTIF(ch)                                  AS fin_chain_units,
             SUM(sg * seller_price_rub)                   AS fin_seller_price_net,
             SUM(sg * buyer_paid_rub)                     AS fin_buyer_paid_net,
             SUM(sg * spp_rub)                            AS fin_spp_net,
             SUM(sg * for_pay_rub)                        AS fin_for_pay_net,
             SUM(IF(op = 'Продажа', seller_price_rub, 0)) AS fin_sale_seller_price,
             SUM(IF(op = 'Возврат', seller_price_rub, 0)) AS fin_return_seller_price,
             SUM(IF(op = 'Возврат', for_pay_rub, 0))      AS fin_return_for_pay,
             SUM(marketplace_fee_gap_rub)                 AS fee_as_reported,
             SUM(IF(ch, sg * wb_remuneration_rub, 0))     AS chain_vw,
             SUM(IF(ch, sg * wb_remuneration_vat_rub, 0)) AS chain_vw_vat,
             SUM(IF(ch, sg * acquiring_rub, 0))           AS chain_acquiring,
             SUM(IF(ch, sg * pvz_reward_rub, 0))          AS chain_pvz,
             SUM(IF(ch, sg * price_chain_rounding_rub, 0)) AS chain_rounding
      FROM finr GROUP BY 1, 2
    ),
    -- Логистика SKU по видам (метки WB только в API-слое с 13.07; LEGACY — «без метки»).
    leg_label AS (
      SELECT CONCAT(report_id, '#', rrd_id) AS row_key, bonus_type_name
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
      WHERE supplier_oper_name IN ('Логистика', 'Доставка', 'Коррекция логистики')
    ),
    logi AS (
      SELECT l.finance_date AS day, l.nm_id,
             SUM(l.cost_amount_positive) AS logistics_total,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name = 'К клиенту при продаже',      l.cost_amount_positive, 0)) AS leg_sold,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name = 'К клиенту при отмене',       l.cost_amount_positive, 0)) AS leg_cancel_to,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name = 'От клиента при отмене',      l.cost_amount_positive, 0)) AS leg_cancel_from,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name = 'От клиента при возврате',    l.cost_amount_positive, 0)) AS leg_customer_return,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name = 'Возврат брака (К продавцу)', l.cost_amount_positive, 0)) AS leg_defect_return,
             SUM(IF(l.op_key = 'Коррекция логистики', l.cost_amount_positive, 0))                                                     AS leg_correction,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name IS NULL, l.cost_amount_positive, 0))                      AS leg_unlabeled,
             SUM(IF(l.op_key <> 'Коррекция логистики' AND b.bonus_type_name IS NOT NULL
                    AND b.bonus_type_name NOT IN ('К клиенту при продаже', 'К клиенту при отмене', 'От клиента при отмене',
                                                  'От клиента при возврате', 'Возврат брака (К продавцу)'),
                    l.cost_amount_positive, 0))                                                                                   AS leg_other_label
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` l
      LEFT JOIN leg_label b ON b.row_key = l.finance_row_key
      WHERE l.cost_category = 'logistics' AND l.is_sku_row AND l.cost_amount_positive IS NOT NULL
      GROUP BY 1, 2
    ),
    sto AS (
      SELECT date_msk AS day, nm_id, SUM(storage_rub_exact) AS storage_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` GROUP BY 1, 2
    ),
    stocov AS (
      SELECT date_msk AS day, LOGICAL_OR(status = 'OK') AS storage_ok
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_COVERAGE` GROUP BY 1
    ),
    cogs AS (
      SELECT day, nm_id, product_cogs_settlement_rub, net_product_cogs_operational_rub,
             cogs_resolution_status, settlement_cogs_covered
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`
    ),
    px AS (
      SELECT DATE(observed_at, 'Europe/Moscow') AS day, nm_id,
             MIN(seller_effective_price) AS set_price_min, MAX(seller_effective_price) AS set_price_max,
             COUNT(*) AS set_price_observations
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`
      WHERE environment = 'prod' AND seller_effective_price IS NOT NULL
      GROUP BY 1, 2
    ),
    fr AS (
      SELECT data_as_of_min, mart_built_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FRESHNESS_HEADER`
    ),
    j AS (
      SELECT
        s.day, s.nm_id,
        u.internal_sku, u.product_name_short, u.product_name_full, u.is_bundle,
        IFNULL(c.orders_covered, FALSE)   AS orders_covered,
        IFNULL(c.sales_covered, FALSE)    AS sales_covered,
        IFNULL(c.ads_covered, FALSE)      AS ads_covered,
        IFNULL(c.finance_covered, FALSE)  AS finance_covered,
        c.finance_is_final,
        IFNULL(c.contains_provisional_finance, FALSE) AS contains_provisional_finance,
        IFNULL(st.storage_ok, FALSE)      AS storage_sku_covered,
        s.ad_spend, s.views, s.clicks,
        o.gross_units, o.cancelled_units, o.gross_price, o.cancelled_price,
        h.cohort_orders, h.cohort_buyout_orders, h.cohort_cancelled_orders, h.cohort_unresolved_orders,
        h.cohort_conflict_orders, h.cohort_returned_after_buyout_orders,
        sl.buyout_units, sl.buyout_seller_price, sl.buyout_buyer_paid, sl.buyout_buyer_paid_units, sl.return_units,
        f.fin_sale_units, f.fin_return_units, f.fin_chain_units, f.fin_seller_price_net, f.fin_buyer_paid_net,
        f.fin_spp_net, f.fin_for_pay_net, f.fin_sale_seller_price, f.fin_return_seller_price, f.fin_return_for_pay,
        f.fee_as_reported, f.chain_vw, f.chain_vw_vat, f.chain_acquiring, f.chain_pvz, f.chain_rounding,
        lg.logistics_total, lg.leg_sold, lg.leg_cancel_to, lg.leg_cancel_from, lg.leg_customer_return,
        lg.leg_defect_return, lg.leg_correction, lg.leg_unlabeled, lg.leg_other_label,
        so.storage_rub,
        cg.product_cogs_settlement_rub, cg.net_product_cogs_operational_rub, cg.cogs_resolution_status, cg.settlement_cogs_covered,
        p.set_price_min, p.set_price_max, p.set_price_observations
      FROM spine s
      JOIN u USING (nm_id)
      LEFT JOIN cov    c  USING (day)
      LEFT JOIN stocov st USING (day)
      LEFT JOIN ord    o  USING (day, nm_id)
      LEFT JOIN coh    h  USING (day, nm_id)
      LEFT JOIN sal    sl USING (day, nm_id)
      LEFT JOIN fin    f  USING (day, nm_id)
      LEFT JOIN logi   lg USING (day, nm_id)
      LEFT JOIN sto    so USING (day, nm_id)
      LEFT JOIN cogs   cg USING (day, nm_id)
      LEFT JOIN px     p  USING (day, nm_id)
    ),
    g AS (
      -- Гейты покрытия (детерминированные флаги). Вне покрытия — NULL, не 0.
      SELECT
        day, nm_id, internal_sku, product_name_short, product_name_full, is_bundle,
        orders_covered, sales_covered, ads_covered, finance_covered, finance_is_final, contains_provisional_finance,
        storage_sku_covered,
        -- ── Заказы (дата заказа) ──
        IF(orders_covered, IFNULL(gross_units, 0), NULL)                          AS orders_gross_units,
        IF(orders_covered, IFNULL(cancelled_units, 0), NULL)                      AS orders_cancelled_units,
        IF(orders_covered, IFNULL(gross_price, 0), NULL)                          AS orders_gross_seller_price_rub,
        IF(orders_covered, IFNULL(cancelled_price, 0), NULL)                      AS orders_cancelled_seller_price_rub,
        -- ── Когорта выкупа (дата заказа) ──
        IF(orders_covered, IFNULL(cohort_orders, 0), NULL)                        AS cohort_orders,
        IF(orders_covered, IFNULL(cohort_buyout_orders, 0), NULL)                 AS cohort_buyout_orders,
        IF(orders_covered, IFNULL(cohort_cancelled_orders, 0), NULL)              AS cohort_cancelled_orders,
        IF(orders_covered, IFNULL(cohort_unresolved_orders, 0), NULL)             AS cohort_unresolved_orders,
        IF(orders_covered, IFNULL(cohort_conflict_orders, 0), NULL)               AS cohort_conflict_orders,
        IF(orders_covered, IFNULL(cohort_buyout_orders, 0) + IFNULL(cohort_cancelled_orders, 0), NULL) AS cohort_resolved_orders,
        IF(orders_covered, IFNULL(cohort_returned_after_buyout_orders, 0), NULL)  AS cohort_returned_after_buyout_orders,
        -- ── Выкупы-события (дата выкупа, Statistics API) ──
        IF(sales_covered, IFNULL(buyout_units, 0), NULL)                          AS buyout_units,
        IF(sales_covered, IFNULL(buyout_seller_price, 0), NULL)                   AS buyout_seller_price_rub,
        IF(sales_covered, IFNULL(buyout_buyer_paid, 0), NULL)                     AS buyout_buyer_paid_rub,
        IF(sales_covered, IFNULL(buyout_buyer_paid_units, 0), NULL)               AS buyout_buyer_paid_units,
        IF(sales_covered, IFNULL(return_units, 0), NULL)                          AS sales_return_units,
        -- ── Финотчёт: цена и начисление (дата финотчёта, нетто возвратов) ──
        IF(finance_covered, IFNULL(fin_sale_units, 0), NULL)                      AS fin_sale_units,
        IF(finance_covered, IFNULL(fin_return_units, 0), NULL)                    AS fin_return_units,
        IF(finance_covered, IFNULL(fin_chain_units, 0), NULL)                     AS fin_chain_units,
        IF(finance_covered, IFNULL(fin_seller_price_net, 0), NULL)                AS fin_seller_price_rub,
        IF(finance_covered, IFNULL(fin_spp_net, 0), NULL)                         AS fin_spp_rub,
        IF(finance_covered, IFNULL(fin_buyer_paid_net, 0), NULL)                  AS fin_buyer_paid_rub,
        IF(finance_covered, IFNULL(fin_for_pay_net, 0), NULL)                     AS credited_for_goods_rub,
        IF(finance_covered, IFNULL(fin_sale_seller_price, 0), NULL)               AS fin_sale_seller_price_rub,
        IF(finance_covered, IFNULL(fin_return_seller_price, 0), NULL)             AS fin_return_seller_price_rub,
        IF(finance_covered, IFNULL(fin_return_for_pay, 0), NULL)                  AS fin_return_for_pay_rub,
        IF(finance_covered, IFNULL(fee_as_reported, 0), NULL)                     AS wb_fee_as_reported_rub,
        -- Компоненты цепочки: NULL, если в сутках есть строки финотчёта без цепочки (до 13.07.2026).
        IF(finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0),
           IFNULL(chain_vw, 0), NULL)                                             AS chain_wb_remuneration_rub,
        IF(finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0),
           IFNULL(chain_vw_vat, 0), NULL)                                         AS chain_wb_remuneration_vat_rub,
        IF(finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0),
           IFNULL(chain_acquiring, 0), NULL)                                      AS chain_acquiring_rub,
        IF(finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0),
           IFNULL(chain_pvz, 0), NULL)                                            AS chain_pvz_reward_rub,
        IF(finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0),
           IFNULL(chain_rounding, 0), NULL)                                       AS chain_rounding_rub,
        (finance_covered AND IFNULL(fin_chain_units, 0) = IFNULL(fin_sale_units, 0) + IFNULL(fin_return_units, 0))
                                                                                  AS price_chain_complete,
        -- ── Логистика SKU (дата финотчёта) ──
        IF(finance_covered, IFNULL(logistics_total, 0), NULL)                     AS logistics_rub,
        IF(finance_covered, IFNULL(leg_sold, 0), NULL)                            AS logistics_sold_rub,
        IF(finance_covered, IFNULL(leg_cancel_to, 0), NULL)                       AS logistics_cancel_to_customer_rub,
        IF(finance_covered, IFNULL(leg_cancel_from, 0), NULL)                     AS logistics_cancel_from_customer_rub,
        IF(finance_covered, IFNULL(leg_customer_return, 0), NULL)                 AS logistics_customer_return_rub,
        IF(finance_covered, IFNULL(leg_defect_return, 0), NULL)                   AS logistics_defect_return_rub,
        IF(finance_covered, IFNULL(leg_correction, 0), NULL)                      AS logistics_correction_rub,
        IF(finance_covered, IFNULL(leg_unlabeled, 0), NULL)                       AS logistics_unlabeled_rub,
        IF(finance_covered, IFNULL(leg_other_label, 0), NULL)                     AS logistics_other_label_rub,
        -- ── Хранение SKU (отчёт платного хранения, дата хранения) ──
        IF(storage_sku_covered, IFNULL(storage_rub, 0), NULL)                     AS storage_sku_rub,
        -- ── Реклама: атрибуция (дата активности) ──
        IF(ads_covered, ad_spend, NULL)                                           AS ads_attributed_rub,
        IF(ads_covered, views, NULL)                                              AS ad_views,
        IF(ads_covered, clicks, NULL)                                             AS ad_clicks,
        -- ── Себестоимость: основная — операционная (дата выкупа, нетто сторно), ровно как
        --    product_cogs_rub экономики Executive V2; по дате финотчёта — справочно ──
        IF(sales_covered, net_product_cogs_operational_rub, NULL)                 AS cogs_rub,
        IF(finance_covered, product_cogs_settlement_rub, NULL)                    AS cogs_settlement_rub,
        cogs_resolution_status,
        settlement_cogs_covered                                                   AS cogs_settlement_covered,
        -- ── Наблюдатель цен (установленная цена продавца, с 07.09.2026) ──
        set_price_min AS set_seller_price_min_rub, set_price_max AS set_seller_price_max_rub, set_price_observations
      FROM j
    )
    SELECT
      g.*,
      -- ── Вклад SKU (только прямые/атрибутированные компоненты) ──
      IF(finance_covered AND ads_covered,
         credited_for_goods_rub - logistics_rub - IFNULL(storage_sku_rub, 0) - ads_attributed_rub, NULL)
                                                                                  AS contribution_before_cogs_rub,
      IF(finance_covered AND ads_covered AND sales_covered AND cogs_rub IS NOT NULL,
         credited_for_goods_rub - logistics_rub - IFNULL(storage_sku_rub, 0) - ads_attributed_rub - cogs_rub, NULL)
                                                                                  AS contribution_after_cogs_rub,
      fr.data_as_of_min AS source_data_through,
      fr.mart_built_at,
      v_run_id  AS layer_run_id,
      v_started AS layer_built_at
    FROM g CROSS JOIN fr;

    -- 2) ASSERT до подмены.
    -- S1 грейн
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)) AND COUNTIF(day IS NULL OR nm_id IS NULL) = 0 FROM b)
      AS 'SKU_V2 S1: грейн day × nm_id не уникален или NULL';
    -- S2 грейн = MART_SKU_DAILY (universe × плотный календарь)
    ASSERT (SELECT COUNT(*) FROM (
              SELECT FORMAT('%t|%t', day, nm_id) k FROM b
              EXCEPT DISTINCT SELECT FORMAT('%t|%t', day, nm_id) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)) = 0
       AND (SELECT COUNT(*) FROM b) = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
      AS 'SKU_V2 S2: грейн слоя не совпал с MART_SKU_DAILY';
    -- S3 universe: все активные SKU, плотный календарь без дыр
    ASSERT (SELECT COUNT(DISTINCT nm_id) FROM b) = (SELECT COUNT(DISTINCT nm_id) FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
                                                    WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL)
       AND (SELECT COUNTIF(cnt <> span) FROM (SELECT nm_id, COUNT(*) cnt, DATE_DIFF(MAX(day), MIN(day), DAY) + 1 span FROM b GROUP BY nm_id)) = 0
      AS 'SKU_V2 S3: universe неполон или в календаре SKU есть дыры';
    -- S4 сверка с MART_SKU_DAILY построчно (заказы нетто/брутто, выкупы, выручка, реклама, логистика, сбор WB)
    ASSERT (
      SELECT COUNTIF(b.orders_gross_units IS DISTINCT FROM IF(b.orders_covered, m.orders_qty + m.canceled_qty, NULL))
           + COUNTIF(b.orders_cancelled_units IS DISTINCT FROM IF(b.orders_covered, m.canceled_qty, NULL))
           + COUNTIF(IF(b.orders_covered, b.orders_gross_seller_price_rub - b.orders_cancelled_seller_price_rub, NULL)
                     IS DISTINCT FROM IF(b.orders_covered, m.orders_rub, NULL))
           + COUNTIF(b.buyout_units IS DISTINCT FROM IF(b.sales_covered, m.buyouts_qty, NULL))
           + COUNTIF(b.buyout_seller_price_rub IS DISTINCT FROM IF(b.sales_covered, m.buyouts_rub, NULL))
           + COUNTIF(b.ads_attributed_rub IS DISTINCT FROM IF(b.ads_covered, m.ad_spend, NULL))
           + COUNTIF(b.logistics_rub IS DISTINCT FROM IF(b.finance_covered, m.logistics_cost_positive, NULL))
           + COUNTIF(b.wb_fee_as_reported_rub IS DISTINCT FROM IF(b.finance_covered, m.marketplace_fee_rub, NULL))
      FROM b JOIN `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` m USING (day, nm_id)
    ) = 0 AS 'SKU_V2 S4: слой разошёлся с MART_SKU_DAILY';
    -- S5 тождества
    ASSERT (
      SELECT
        -- цепочка цены: цена − начисление = СПП + vw + НДС + эквайринг + ПВЗ + округление (где цепочка полная)
        COUNTIF(price_chain_complete AND ABS((fin_seller_price_rub - credited_for_goods_rub)
               - (fin_spp_rub + chain_wb_remuneration_rub + chain_wb_remuneration_vat_rub + chain_acquiring_rub
                  + chain_pvz_reward_rub + chain_rounding_rub)) > 0.005)
        -- нетто = продажи − возвраты
      + COUNTIF(finance_covered AND ABS(fin_seller_price_rub - (fin_sale_seller_price_rub - fin_return_seller_price_rub)) > 0.005)
        -- сбор WB как отчитан = (продажи + возвраты) по цене − (продажи + возвраты) по начислению
      + COUNTIF(finance_covered AND ABS(wb_fee_as_reported_rub
               - ((fin_seller_price_rub + 2 * fin_return_seller_price_rub) - (credited_for_goods_rub + 2 * fin_return_for_pay_rub))) > 0.005)
        -- когорта: разбиение
      + COUNTIF(orders_covered AND cohort_orders <> cohort_buyout_orders + cohort_cancelled_orders + cohort_unresolved_orders + cohort_conflict_orders)
      + COUNTIF(orders_covered AND cohort_orders <> orders_gross_units)
        -- логистика: сумма видов = итог
      + COUNTIF(finance_covered AND ABS(logistics_rub - (logistics_sold_rub + logistics_cancel_to_customer_rub
               + logistics_cancel_from_customer_rub + logistics_customer_return_rub + logistics_defect_return_rub
               + logistics_correction_rub + logistics_unlabeled_rub + logistics_other_label_rub)) > 0.005)
        -- NULL-семантика: компонента цепочки нет ⇔ цепочка неполная
      + COUNTIF(finance_covered AND (chain_wb_remuneration_rub IS NULL) != (NOT price_chain_complete))
        -- вклад
      + COUNTIF(contribution_after_cogs_rub IS NOT NULL
               AND ABS(contribution_after_cogs_rub - (contribution_before_cogs_rub - cogs_rub)) > 0.000001)
      FROM b
    ) = 0 AS 'SKU_V2 S5: нарушено тождество (цепочка, нетто, сбор, когорта, логистика, NULL, вклад)';
    -- S6 независимое повторное чтение источников (итоги по универсуму; по одному источнику на запрос)
    ASSERT (SELECT ROUND(SUM(storage_sku_rub), 6) FROM b)
         = (SELECT ROUND(SUM(s.storage_rub_exact), 6) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` s
            JOIN (SELECT DISTINCT date_msk FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_COVERAGE` WHERE status = 'OK') c USING (date_msk)
            WHERE s.nm_id IN (SELECT nm_id FROM b) AND s.date_msk <= (SELECT MAX(day) FROM b))
      AS 'SKU_V2 S6: хранение по SKU разошлось с отчётом платного хранения';
    ASSERT (SELECT SUM(credited_for_goods_rub) FROM b)
         = (SELECT SUM(IF(p.supplier_oper_name = 'Возврат', -1, 1) * p.for_pay_rub)
            FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS` p
            JOIN (SELECT DISTINCT day FROM b WHERE finance_covered) d ON d.day = p.finance_date
            WHERE p.in_mart_universe)
      AS 'SKU_V2 S6: начисление за товар разошлось с финотчётом';
    ASSERT (SELECT SUM(cogs_rub) FROM b)
         = (SELECT SUM(c.net_product_cogs_operational_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS` c
            JOIN (SELECT DISTINCT day FROM b WHERE sales_covered) d USING (day))
      AS 'SKU_V2 S6: себестоимость разошлась с V_MART_SKU_DAILY_COGS';
    ASSERT (SELECT SUM(orders_gross_units) FROM b)
         = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` o
            JOIN (SELECT DISTINCT day FROM b WHERE orders_covered) d ON d.day = o.order_date
            WHERE o.nm_id IN (SELECT nm_id FROM b))
      AS 'SKU_V2 S6: заказы разошлись с FACT_ORDERS';

    SET v_rows    = (SELECT COUNT(*) FROM b);
    SET v_through = (SELECT ANY_VALUE(source_data_through) FROM b);
    SET v_mart_at = (SELECT ANY_VALUE(mart_built_at) FROM b);

    -- 3) Первая сборка создаёт пустую таблицу по схеме b (единственный источник схемы — тело выше).
    SET v_exists = EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.TABLES`
                           WHERE table_name = 'SKU_PERFORMANCE_V2_DAILY');
    IF NOT v_exists THEN
      CREATE TABLE `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`
      CLUSTER BY nm_id, day
      OPTIONS (description = 'SKU Performance V2 · материализованный слой day × nm_id (только суммы и количества). Читать через V_DASH_SKU_PERFORMANCE_V2_DAILY / TVF_SKU_PERFORMANCE_V2_PERIOD. Пишет только sp_build_sku_performance_v2_daily.')
      AS SELECT * FROM b WHERE FALSE;
    END IF;

    -- 4) Атомарная подмена.
    BEGIN TRANSACTION;
    SET v_in_tx = TRUE;
      DELETE FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` WHERE TRUE;
      INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` SELECT * FROM b;
    COMMIT TRANSACTION;
    SET v_in_tx = FALSE;

    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'sku_performance_v2_daily' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
       SET status = 'SUCCESS', finished_at = CURRENT_TIMESTAMP(), rows_built = v_rows,
           source_data_through = v_through, mart_built_at = v_mart_at
     WHERE run_id = v_run_id;

  EXCEPTION WHEN ERROR THEN
    IF v_in_tx THEN
      ROLLBACK TRANSACTION;
    END IF;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'sku_performance_v2_daily' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
       SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP(), error_message = @@error.message
     WHERE run_id = v_run_id;
    RAISE USING MESSAGE = CONCAT('sp_build_sku_performance_v2_daily FAILED: ', @@error.message);
  END;
END;

-- ── §3. Первая сборка (создаёт таблицу; view и функции ниже ссылаются на неё) ──
-- Схема таблицы выводится из тела сборки. При изменении схемы миграция пересоздаёт ПРОИЗВОДНУЮ таблицу
-- (данные полностью восстанавливаются сборкой). Плановая сборка при расхождении схемы падает fail-closed
-- на INSERT, прежняя таблица остаётся.
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`;
CALL `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_sku_performance_v2_daily`('migration');

-- ── §4. Тонкий view: признаки момента чтения + свежесть ─────────────────────
--    Суммы — из таблицы как есть. Здесь только то, что зависит от CURRENT_DATE:
--    текущие сутки, зрелость когорты, свежесть слоя. Выражения зрелости — как в V_DASH_BUYOUT_COHORT_DAILY.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY`
OPTIONS (description = 'SKU Performance V2 · day × nm_id поверх SKU_PERFORMANCE_V2_DAILY. Только суммы; средние и доли считать ratio-of-sums (TVF_SKU_PERFORMANCE_V2_PERIOD).')
AS
WITH meta AS (
  SELECT ANY_VALUE(layer_run_id) AS layer_run_id, ANY_VALUE(layer_built_at) AS layer_started_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY`
),
build AS (
  SELECT
    (SELECT finished_at FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG` l, meta WHERE l.run_id = meta.layer_run_id) AS layer_built_at,
    (SELECT AS STRUCT started_at, status, error_message FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`
     WHERE status <> 'SKIPPED_LOCKED' ORDER BY started_at DESC LIMIT 1) AS last_attempt
)
SELECT
  t.* EXCEPT (layer_run_id, layer_built_at),
  (t.day >= CURRENT_DATE('Europe/Moscow') OR t.day >= DATE(meta.layer_started_at, 'Europe/Moscow')) AS is_current_day,
  DATE_DIFF(CURRENT_DATE('Europe/Moscow'), t.day, DAY)                                             AS day_age_days,
  -- Когорта окончательна, когда у неё нет заказов без исхода и конфликтов (методология Executive V2).
  IF(t.orders_covered,
     t.cohort_unresolved_orders + t.cohort_conflict_orders = 0
     AND NOT (t.day >= CURRENT_DATE('Europe/Moscow') OR t.day >= DATE(meta.layer_started_at, 'Europe/Moscow')),
     NULL)                                                                                          AS cohort_is_final,
  -- ── Дневной ряд «цена ↔ спрос» (SKU Detail). ТОЛЬКО для посуточных графиков: за период НЕ усреднять,
  --    периодные значения — ratio-of-sums в TVF_SKU_PERFORMANCE_V2_PERIOD. Наблюдение, не причинность. ──
  SAFE_DIVIDE(t.orders_gross_seller_price_rub, NULLIF(t.orders_gross_units, 0))           AS avg_seller_price_orders_day_rub,
  SAFE_DIVIDE(t.buyout_seller_price_rub, NULLIF(t.buyout_units, 0))                       AS avg_seller_price_buyouts_day_rub,
  SAFE_DIVIDE(t.buyout_buyer_paid_rub, NULLIF(t.buyout_buyer_paid_units, 0))              AS avg_buyer_paid_price_day_rub,
  SAFE_DIVIDE(t.cohort_buyout_orders, NULLIF(t.cohort_resolved_orders, 0))                AS cohort_buyout_rate_day,
  SAFE_DIVIDE(t.ads_attributed_rub, NULLIF(t.buyout_seller_price_rub, 0))                 AS drr_day,
  meta.layer_run_id,
  build.layer_built_at,
  build.last_attempt.status        AS last_build_status,
  build.last_attempt.started_at    AS last_build_started_at,
  build.last_attempt.error_message AS last_build_error,
  CASE
    WHEN build.last_attempt.status = 'FAILED' THEN 'последняя сборка не удалась'
    WHEN build.last_attempt.status = 'STARTED'
     AND build.last_attempt.started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 MINUTE) THEN 'сборка зависла'
  END AS layer_build_alert
FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t CROSS JOIN meta CROSS JOIN build;

-- ── §5. Мост SKU → Executive (по суткам; суммируется за любой период) ─────────
--    Σ вклада SKU до себестоимости
--    + хранение SKU (вычтено в SKU, в Executive его заменяет хранение финотчёта)
--    − хранение уровня счёта (финотчёт)
--    − тарифная опция / минимальный платёж
--    − прочие расходы уровня счёта (штрафы, утилизация, прочие удержания и т. п.)
--    + (атрибуция рекламы − биллинг WB)
--    + (выручка по дате выкупа − цена продавца по финотчёту)          — база дат выручки Executive
--    + (2 × начисление по возвратам − цена продавца по возвратам)     — Executive считает «Возврат» поступлением (KI-2)
--    = результат Executive до себестоимости; остаток — residual (не скрывается).
--    Строки только на сутках, где Executive-результат определён; вклад SKU вне этих суток — отдельной колонкой.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY`
OPTIONS (description = 'SKU Performance V2 · мост Σ вклада SKU → результат Executive V2 по суткам. Расходы уровня счёта по SKU не распределяются.')
AS
WITH s AS (
  SELECT day,
         SUM(contribution_before_cogs_rub) AS sku_contribution_before_cogs_rub,
         SUM(contribution_after_cogs_rub)  AS sku_contribution_after_cogs_rub,
         SUM(storage_sku_rub)              AS sku_storage_rub,
         SUM(ads_attributed_rub)           AS sku_ads_attributed_rub,
         SUM(buyout_seller_price_rub)      AS sku_revenue_sale_date_rub,
         SUM(fin_sale_seller_price_rub)    AS sku_fin_sale_seller_price_rub,
         SUM(fin_return_seller_price_rub)  AS sku_fin_return_seller_price_rub,
         SUM(fin_return_for_pay_rub)       AS sku_fin_return_for_pay_rub,
         SUM(cogs_rub)                     AS sku_cogs_rub,
         SUM(logistics_rub)                AS sku_logistics_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` GROUP BY day
),
e AS (
  SELECT day, period_result_pre_cogs_corrected_rub, period_result_after_product_cogs_rub, product_cogs_rub,
         storage_rub, tariff_option_minimum_payment_rub, account_level_total_corrected_rub, ad_spend_financial_rub,
         sales_revenue_seller_base_rub, logistics_rub, executive_final_day, executive_provisional_day, executive_incomplete_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
),
j AS (
  SELECT
    e.day,
    e.executive_final_day, e.executive_provisional_day, e.executive_incomplete_day,
    (e.period_result_pre_cogs_corrected_rub IS NOT NULL)                              AS in_executive_result,
    IF(e.period_result_pre_cogs_corrected_rub IS NULL, s.sku_contribution_before_cogs_rub, NULL)
                                                                                      AS sku_contribution_outside_executive_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, s.sku_contribution_before_cogs_rub, NULL) AS l1_sku_contribution_before_cogs_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, IFNULL(s.sku_storage_rub, 0), NULL)       AS l2_add_back_sku_storage_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, -e.storage_rub, NULL)                     AS l3_account_storage_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, -e.tariff_option_minimum_payment_rub, NULL) AS l4_tariff_option_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL,
       -(e.account_level_total_corrected_rub - e.storage_rub - e.tariff_option_minimum_payment_rub), NULL) AS l5_other_account_level_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, s.sku_ads_attributed_rub - e.ad_spend_financial_rub, NULL) AS l6_attribution_minus_billing_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, s.sku_revenue_sale_date_rub - s.sku_fin_sale_seller_price_rub, NULL) AS l7_revenue_date_basis_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, 2 * s.sku_fin_return_for_pay_rub - s.sku_fin_return_seller_price_rub, NULL) AS l8_returns_sign_ki2_rub,
    e.period_result_pre_cogs_corrected_rub                                            AS executive_result_pre_cogs_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, s.sku_cogs_rub, NULL)      AS sku_cogs_rub,
    e.product_cogs_rub                                                                AS executive_cogs_rub,
    e.period_result_after_product_cogs_rub                                            AS executive_result_after_cogs_rub,
    IF(e.period_result_pre_cogs_corrected_rub IS NOT NULL, s.sku_contribution_after_cogs_rub, NULL) AS sku_contribution_after_cogs_rub
  FROM e LEFT JOIN s USING (day)
)
SELECT
  j.*,
  l1_sku_contribution_before_cogs_rub + l2_add_back_sku_storage_rub + l3_account_storage_rub + l4_tariff_option_rub
    + l5_other_account_level_rub + l6_attribution_minus_billing_rub + l7_revenue_date_basis_rub + l8_returns_sign_ki2_rub
                                                                                      AS bridge_result_pre_cogs_rub,
  executive_result_pre_cogs_rub
    - (l1_sku_contribution_before_cogs_rub + l2_add_back_sku_storage_rub + l3_account_storage_rub + l4_tariff_option_rub
       + l5_other_account_level_rub + l6_attribution_minus_billing_rub + l7_revenue_date_basis_rub + l8_returns_sign_ki2_rub)
                                                                                      AS residual_pre_cogs_rub,
  IF(executive_result_after_cogs_rub IS NOT NULL, executive_cogs_rub - sku_cogs_rub, NULL) AS cogs_difference_rub
FROM j;


-- ── §6. Период + предыдущее сопоставимое окно (для Portfolio и SKU Detail) ────
--    Окна: [p_from, p_to] и [p_from − len, p_from − 1], len = число суток текущего окна.
--    Все средние и доли — ratio-of-sums. Абсолютные величины сравниваются в % (Δ% = Δ / |prev|),
--    доли — в процентных пунктах. Сравнение метрики допускается при одинаковом покрытии окон этой метрики;
--    надёжность — отдельным флагом (порог SKU_PERFORMANCE_V2_CONFIG.min_reliable_units), значения не скрываются.
--    Строка с nm_id = NULL — итог портфеля (все 25 SKU).
CREATE OR REPLACE TABLE FUNCTION `project-fa311fc0-4d87-4781-986.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(p_from DATE, p_to DATE)
OPTIONS (description = 'SKU Performance V2 · период и предыдущее сопоставимое окно по SKU и портфелю: суммы, ratio-of-sums, Δ% / п.п., покрытие, надёжность, статус данных.')
AS
WITH
cfg AS (
  SELECT MAX(IF(config_key = 'min_reliable_units', config_value, NULL)) AS min_units
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_CONFIG`
),
bnd AS (
  SELECT p_from AS d1, p_to AS d2, DATE_DIFF(p_to, p_from, DAY) + 1 AS len,
         DATE_SUB(p_from, INTERVAL DATE_DIFF(p_to, p_from, DAY) + 1 DAY) AS p1, DATE_SUB(p_from, INTERVAL 1 DAY) AS p2
),
r AS (
  SELECT v.*, IF(v.day >= bnd.d1, 'cur', 'prv') AS w
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY` v, bnd
  WHERE v.day BETWEEN bnd.p1 AND bnd.d2
),
a AS (
  SELECT w, nm_id,
    SUM(orders_gross_units) AS orders_gross_units,
    SUM(orders_cancelled_units) AS orders_cancelled_units,
    SUM(orders_gross_seller_price_rub) AS orders_gross_seller_price_rub,
    SUM(cohort_orders) AS cohort_orders,
    SUM(cohort_buyout_orders) AS cohort_buyout_orders,
    SUM(cohort_cancelled_orders) AS cohort_cancelled_orders,
    SUM(cohort_unresolved_orders) AS cohort_unresolved_orders,
    SUM(cohort_conflict_orders) AS cohort_conflict_orders,
    SUM(cohort_resolved_orders) AS cohort_resolved_orders,
    SUM(buyout_units) AS buyout_units,
    SUM(buyout_seller_price_rub) AS buyout_seller_price_rub,
    SUM(buyout_buyer_paid_rub) AS buyout_buyer_paid_rub,
    SUM(buyout_buyer_paid_units) AS buyout_buyer_paid_units,
    SUM(fin_sale_units) AS fin_sale_units,
    SUM(fin_return_units) AS fin_return_units,
    SUM(fin_seller_price_rub) AS fin_seller_price_rub,
    SUM(fin_spp_rub) AS fin_spp_rub,
    SUM(fin_buyer_paid_rub) AS fin_buyer_paid_rub,
    SUM(credited_for_goods_rub) AS credited_for_goods_rub,
    SUM(logistics_rub) AS logistics_rub,
    SUM(logistics_sold_rub) AS logistics_sold_rub,
    SUM(logistics_cancel_to_customer_rub) AS logistics_cancel_to_customer_rub,
    SUM(logistics_cancel_from_customer_rub) AS logistics_cancel_from_customer_rub,
    SUM(logistics_unlabeled_rub) AS logistics_unlabeled_rub,
    SUM(storage_sku_rub) AS storage_sku_rub,
    SUM(ads_attributed_rub) AS ads_attributed_rub,
    SUM(cogs_rub) AS cogs_rub,
    SUM(cogs_settlement_rub) AS cogs_settlement_rub,
    SUM(contribution_before_cogs_rub) AS contribution_before_cogs_rub,
    SUM(contribution_after_cogs_rub) AS contribution_after_cogs_rub,
    SUM(logistics_customer_return_rub + logistics_defect_return_rub + logistics_correction_rub + logistics_other_label_rub) AS logistics_other_rub,
    IF(COUNTIF(finance_covered AND NOT price_chain_complete) = 0, SUM(chain_wb_remuneration_rub), NULL) AS chain_wb_remuneration_rub,
    IF(COUNTIF(finance_covered AND NOT price_chain_complete) = 0, SUM(chain_wb_remuneration_vat_rub), NULL) AS chain_wb_remuneration_vat_rub,
    IF(COUNTIF(finance_covered AND NOT price_chain_complete) = 0, SUM(chain_acquiring_rub), NULL) AS chain_acquiring_rub,
    IF(COUNTIF(finance_covered AND NOT price_chain_complete) = 0, SUM(chain_pvz_reward_rub), NULL) AS chain_pvz_reward_rub,
    COUNT(DISTINCT day) AS days_present,
    COUNT(DISTINCT IF(orders_covered, day, NULL)) AS orders_cov_days,
    COUNT(DISTINCT IF(sales_covered, day, NULL)) AS sales_cov_days,
    COUNT(DISTINCT IF(ads_covered, day, NULL)) AS ads_cov_days,
    COUNT(DISTINCT IF(finance_covered, day, NULL)) AS finance_cov_days,
    COUNT(DISTINCT IF(finance_covered AND (contains_provisional_finance OR NOT IFNULL(finance_is_final, FALSE)), day, NULL)) AS finance_provisional_days,
    COUNT(DISTINCT IF(is_current_day, day, NULL)) AS current_days,
    COUNT(DISTINCT IF(finance_covered AND NOT price_chain_complete, day, NULL)) AS price_chain_missing_days,
    COUNT(DISTINCT IF(storage_sku_covered, day, NULL)) AS storage_cov_days,
    COUNTIF(sales_covered AND cogs_rub IS NULL AND IFNULL(buyout_units, 0) + IFNULL(sales_return_units, 0) > 0) AS cogs_missing_sku_days,
    COUNTIF(orders_covered AND NOT IFNULL(cohort_is_final, FALSE) AND cohort_orders > 0) AS cohort_nonfinal_sku_days,
    MIN(IF(is_bundle, 1, 0)) = 1 AS all_bundle
  FROM r
  GROUP BY GROUPING SETS ((w, nm_id), (w))
),
ref AS (
  SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku, ANY_VALUE(product_name_short) AS product_name_short,
         ANY_VALUE(is_bundle) AS is_bundle
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY` GROUP BY nm_id
),
p AS (
  SELECT nm_id,
    MAX(IF(w = 'cur', orders_gross_units, NULL)) AS cur_orders_gross_units, MAX(IF(w = 'prv', orders_gross_units, NULL)) AS prv_orders_gross_units,
    MAX(IF(w = 'cur', orders_cancelled_units, NULL)) AS cur_orders_cancelled_units, MAX(IF(w = 'prv', orders_cancelled_units, NULL)) AS prv_orders_cancelled_units,
    MAX(IF(w = 'cur', orders_gross_seller_price_rub, NULL)) AS cur_orders_gross_seller_price_rub, MAX(IF(w = 'prv', orders_gross_seller_price_rub, NULL)) AS prv_orders_gross_seller_price_rub,
    MAX(IF(w = 'cur', cohort_orders, NULL)) AS cur_cohort_orders, MAX(IF(w = 'prv', cohort_orders, NULL)) AS prv_cohort_orders,
    MAX(IF(w = 'cur', cohort_buyout_orders, NULL)) AS cur_cohort_buyout_orders, MAX(IF(w = 'prv', cohort_buyout_orders, NULL)) AS prv_cohort_buyout_orders,
    MAX(IF(w = 'cur', cohort_cancelled_orders, NULL)) AS cur_cohort_cancelled_orders, MAX(IF(w = 'prv', cohort_cancelled_orders, NULL)) AS prv_cohort_cancelled_orders,
    MAX(IF(w = 'cur', cohort_unresolved_orders, NULL)) AS cur_cohort_unresolved_orders, MAX(IF(w = 'prv', cohort_unresolved_orders, NULL)) AS prv_cohort_unresolved_orders,
    MAX(IF(w = 'cur', cohort_conflict_orders, NULL)) AS cur_cohort_conflict_orders, MAX(IF(w = 'prv', cohort_conflict_orders, NULL)) AS prv_cohort_conflict_orders,
    MAX(IF(w = 'cur', cohort_resolved_orders, NULL)) AS cur_cohort_resolved_orders, MAX(IF(w = 'prv', cohort_resolved_orders, NULL)) AS prv_cohort_resolved_orders,
    MAX(IF(w = 'cur', buyout_units, NULL)) AS cur_buyout_units, MAX(IF(w = 'prv', buyout_units, NULL)) AS prv_buyout_units,
    MAX(IF(w = 'cur', buyout_seller_price_rub, NULL)) AS cur_buyout_seller_price_rub, MAX(IF(w = 'prv', buyout_seller_price_rub, NULL)) AS prv_buyout_seller_price_rub,
    MAX(IF(w = 'cur', buyout_buyer_paid_rub, NULL)) AS cur_buyout_buyer_paid_rub, MAX(IF(w = 'prv', buyout_buyer_paid_rub, NULL)) AS prv_buyout_buyer_paid_rub,
    MAX(IF(w = 'cur', buyout_buyer_paid_units, NULL)) AS cur_buyout_buyer_paid_units, MAX(IF(w = 'prv', buyout_buyer_paid_units, NULL)) AS prv_buyout_buyer_paid_units,
    MAX(IF(w = 'cur', fin_sale_units, NULL)) AS cur_fin_sale_units, MAX(IF(w = 'prv', fin_sale_units, NULL)) AS prv_fin_sale_units,
    MAX(IF(w = 'cur', fin_return_units, NULL)) AS cur_fin_return_units, MAX(IF(w = 'prv', fin_return_units, NULL)) AS prv_fin_return_units,
    MAX(IF(w = 'cur', fin_seller_price_rub, NULL)) AS cur_fin_seller_price_rub, MAX(IF(w = 'prv', fin_seller_price_rub, NULL)) AS prv_fin_seller_price_rub,
    MAX(IF(w = 'cur', fin_spp_rub, NULL)) AS cur_fin_spp_rub, MAX(IF(w = 'prv', fin_spp_rub, NULL)) AS prv_fin_spp_rub,
    MAX(IF(w = 'cur', fin_buyer_paid_rub, NULL)) AS cur_fin_buyer_paid_rub, MAX(IF(w = 'prv', fin_buyer_paid_rub, NULL)) AS prv_fin_buyer_paid_rub,
    MAX(IF(w = 'cur', credited_for_goods_rub, NULL)) AS cur_credited_for_goods_rub, MAX(IF(w = 'prv', credited_for_goods_rub, NULL)) AS prv_credited_for_goods_rub,
    MAX(IF(w = 'cur', logistics_rub, NULL)) AS cur_logistics_rub, MAX(IF(w = 'prv', logistics_rub, NULL)) AS prv_logistics_rub,
    MAX(IF(w = 'cur', logistics_sold_rub, NULL)) AS cur_logistics_sold_rub, MAX(IF(w = 'prv', logistics_sold_rub, NULL)) AS prv_logistics_sold_rub,
    MAX(IF(w = 'cur', logistics_cancel_to_customer_rub, NULL)) AS cur_logistics_cancel_to_customer_rub, MAX(IF(w = 'prv', logistics_cancel_to_customer_rub, NULL)) AS prv_logistics_cancel_to_customer_rub,
    MAX(IF(w = 'cur', logistics_cancel_from_customer_rub, NULL)) AS cur_logistics_cancel_from_customer_rub, MAX(IF(w = 'prv', logistics_cancel_from_customer_rub, NULL)) AS prv_logistics_cancel_from_customer_rub,
    MAX(IF(w = 'cur', logistics_unlabeled_rub, NULL)) AS cur_logistics_unlabeled_rub, MAX(IF(w = 'prv', logistics_unlabeled_rub, NULL)) AS prv_logistics_unlabeled_rub,
    MAX(IF(w = 'cur', storage_sku_rub, NULL)) AS cur_storage_sku_rub, MAX(IF(w = 'prv', storage_sku_rub, NULL)) AS prv_storage_sku_rub,
    MAX(IF(w = 'cur', ads_attributed_rub, NULL)) AS cur_ads_attributed_rub, MAX(IF(w = 'prv', ads_attributed_rub, NULL)) AS prv_ads_attributed_rub,
    MAX(IF(w = 'cur', cogs_rub, NULL)) AS cur_cogs_rub, MAX(IF(w = 'prv', cogs_rub, NULL)) AS prv_cogs_rub,
    MAX(IF(w = 'cur', cogs_settlement_rub, NULL)) AS cur_cogs_settlement_rub, MAX(IF(w = 'prv', cogs_settlement_rub, NULL)) AS prv_cogs_settlement_rub,
    MAX(IF(w = 'cur', contribution_before_cogs_rub, NULL)) AS cur_contribution_before_cogs_rub, MAX(IF(w = 'prv', contribution_before_cogs_rub, NULL)) AS prv_contribution_before_cogs_rub,
    MAX(IF(w = 'cur', contribution_after_cogs_rub, NULL)) AS cur_contribution_after_cogs_rub, MAX(IF(w = 'prv', contribution_after_cogs_rub, NULL)) AS prv_contribution_after_cogs_rub,
    MAX(IF(w = 'cur', logistics_other_rub, NULL)) AS cur_logistics_other_rub, MAX(IF(w = 'prv', logistics_other_rub, NULL)) AS prv_logistics_other_rub,
    MAX(IF(w = 'cur', chain_wb_remuneration_rub, NULL)) AS cur_chain_wb_remuneration_rub, MAX(IF(w = 'prv', chain_wb_remuneration_rub, NULL)) AS prv_chain_wb_remuneration_rub,
    MAX(IF(w = 'cur', chain_wb_remuneration_vat_rub, NULL)) AS cur_chain_wb_remuneration_vat_rub, MAX(IF(w = 'prv', chain_wb_remuneration_vat_rub, NULL)) AS prv_chain_wb_remuneration_vat_rub,
    MAX(IF(w = 'cur', chain_acquiring_rub, NULL)) AS cur_chain_acquiring_rub, MAX(IF(w = 'prv', chain_acquiring_rub, NULL)) AS prv_chain_acquiring_rub,
    MAX(IF(w = 'cur', chain_pvz_reward_rub, NULL)) AS cur_chain_pvz_reward_rub, MAX(IF(w = 'prv', chain_pvz_reward_rub, NULL)) AS prv_chain_pvz_reward_rub,
    MAX(IF(w = 'cur', days_present, NULL)) AS cur_days_present, MAX(IF(w = 'prv', days_present, NULL)) AS prv_days_present,
    MAX(IF(w = 'cur', orders_cov_days, NULL)) AS cur_orders_cov_days, MAX(IF(w = 'prv', orders_cov_days, NULL)) AS prv_orders_cov_days,
    MAX(IF(w = 'cur', sales_cov_days, NULL)) AS cur_sales_cov_days, MAX(IF(w = 'prv', sales_cov_days, NULL)) AS prv_sales_cov_days,
    MAX(IF(w = 'cur', ads_cov_days, NULL)) AS cur_ads_cov_days, MAX(IF(w = 'prv', ads_cov_days, NULL)) AS prv_ads_cov_days,
    MAX(IF(w = 'cur', finance_cov_days, NULL)) AS cur_finance_cov_days, MAX(IF(w = 'prv', finance_cov_days, NULL)) AS prv_finance_cov_days,
    MAX(IF(w = 'cur', finance_provisional_days, NULL)) AS cur_finance_provisional_days, MAX(IF(w = 'prv', finance_provisional_days, NULL)) AS prv_finance_provisional_days,
    MAX(IF(w = 'cur', current_days, NULL)) AS cur_current_days, MAX(IF(w = 'prv', current_days, NULL)) AS prv_current_days,
    MAX(IF(w = 'cur', price_chain_missing_days, NULL)) AS cur_price_chain_missing_days, MAX(IF(w = 'prv', price_chain_missing_days, NULL)) AS prv_price_chain_missing_days,
    MAX(IF(w = 'cur', storage_cov_days, NULL)) AS cur_storage_cov_days, MAX(IF(w = 'prv', storage_cov_days, NULL)) AS prv_storage_cov_days,
    MAX(IF(w = 'cur', cogs_missing_sku_days, NULL)) AS cur_cogs_missing_sku_days, MAX(IF(w = 'prv', cogs_missing_sku_days, NULL)) AS prv_cogs_missing_sku_days,
    MAX(IF(w = 'cur', cohort_nonfinal_sku_days, NULL)) AS cur_cohort_nonfinal_sku_days, MAX(IF(w = 'prv', cohort_nonfinal_sku_days, NULL)) AS prv_cohort_nonfinal_sku_days
  FROM a GROUP BY nm_id
),
m AS (
  SELECT p.*,
    SAFE_DIVIDE(cur_orders_cancelled_units, NULLIF(cur_orders_gross_units, 0))                        AS cur_cancellation_rate,
    SAFE_DIVIDE(cur_orders_gross_seller_price_rub, NULLIF(cur_orders_gross_units, 0))                  AS cur_avg_seller_price_orders_rub,
    SAFE_DIVIDE(cur_buyout_seller_price_rub, NULLIF(cur_buyout_units, 0))                              AS cur_avg_seller_price_buyouts_rub,
    SAFE_DIVIDE(cur_buyout_buyer_paid_rub, NULLIF(cur_buyout_buyer_paid_units, 0))                     AS cur_avg_buyer_paid_price_rub,
    SAFE_DIVIDE(cur_credited_for_goods_rub, NULLIF(cur_fin_sale_units - cur_fin_return_units, 0))      AS cur_credited_per_unit_rub,
    SAFE_DIVIDE(cur_cohort_buyout_orders, NULLIF(cur_cohort_resolved_orders, 0))                       AS cur_cohort_buyout_rate,
    SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0))                        AS cur_drr,
    SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))                  AS cur_contribution_margin_after_cogs,
    SAFE_DIVIDE(cur_contribution_before_cogs_rub, NULLIF(cur_fin_seller_price_rub, 0))                 AS cur_contribution_margin_before_cogs,
    SAFE_DIVIDE(cur_contribution_after_cogs_rub, NULLIF(cur_fin_sale_units - cur_fin_return_units, 0)) AS cur_contribution_after_cogs_per_buyout_rub,
    SAFE_DIVIDE(prv_orders_cancelled_units, NULLIF(prv_orders_gross_units, 0))                        AS prv_cancellation_rate,
    SAFE_DIVIDE(prv_orders_gross_seller_price_rub, NULLIF(prv_orders_gross_units, 0))                  AS prv_avg_seller_price_orders_rub,
    SAFE_DIVIDE(prv_buyout_seller_price_rub, NULLIF(prv_buyout_units, 0))                              AS prv_avg_seller_price_buyouts_rub,
    SAFE_DIVIDE(prv_buyout_buyer_paid_rub, NULLIF(prv_buyout_buyer_paid_units, 0))                     AS prv_avg_buyer_paid_price_rub,
    SAFE_DIVIDE(prv_credited_for_goods_rub, NULLIF(prv_fin_sale_units - prv_fin_return_units, 0))      AS prv_credited_per_unit_rub,
    SAFE_DIVIDE(prv_cohort_buyout_orders, NULLIF(prv_cohort_resolved_orders, 0))                       AS prv_cohort_buyout_rate,
    SAFE_DIVIDE(prv_ads_attributed_rub, NULLIF(prv_buyout_seller_price_rub, 0))                        AS prv_drr,
    SAFE_DIVIDE(prv_contribution_after_cogs_rub, NULLIF(prv_fin_seller_price_rub, 0))                  AS prv_contribution_margin_after_cogs,
    SAFE_DIVIDE(prv_contribution_before_cogs_rub, NULLIF(prv_fin_seller_price_rub, 0))                 AS prv_contribution_margin_before_cogs,
    SAFE_DIVIDE(prv_contribution_after_cogs_rub, NULLIF(prv_fin_sale_units - prv_fin_return_units, 0)) AS prv_contribution_after_cogs_per_buyout_rub
  FROM p
)
SELECT
  m.nm_id,
  (m.nm_id IS NULL)                                   AS is_portfolio_total,
  IFNULL(ref.product_name_short, 'Портфель')          AS product_name_short,
  ref.internal_sku, ref.is_bundle,
  bnd.d1 AS period_from, bnd.d2 AS period_to, bnd.p1 AS prev_from, bnd.p2 AS prev_to, bnd.len AS period_days,
  cfg.min_units AS min_reliable_units,
  m.* EXCEPT (nm_id),
  -- ── Сопоставимость (одинаковое покрытие окон по метрике) ──
  (IFNULL(m.cur_orders_cov_days, 0) = IFNULL(m.prv_orders_cov_days, 0) AND IFNULL(m.cur_orders_cov_days, 0) = bnd.len)   AS orders_comparable,
  (IFNULL(m.cur_sales_cov_days, 0) = IFNULL(m.prv_sales_cov_days, 0) AND IFNULL(m.cur_sales_cov_days, 0) = bnd.len)       AS sales_comparable,
  (IFNULL(m.cur_ads_cov_days, 0) = IFNULL(m.prv_ads_cov_days, 0) AND IFNULL(m.cur_ads_cov_days, 0) = bnd.len
   AND IFNULL(m.cur_sales_cov_days, 0) = bnd.len AND IFNULL(m.prv_sales_cov_days, 0) = bnd.len)                          AS drr_comparable,
  (IFNULL(m.cur_finance_cov_days, 0) = IFNULL(m.prv_finance_cov_days, 0) AND IFNULL(m.cur_finance_cov_days, 0) = bnd.len
   AND IFNULL(m.cur_ads_cov_days, 0) = bnd.len AND IFNULL(m.prv_ads_cov_days, 0) = bnd.len)                              AS economics_comparable,
  (IFNULL(m.cur_storage_cov_days, 0) = IFNULL(m.prv_storage_cov_days, 0))                                                 AS storage_basis_comparable,
  -- ── Надёжность (порог из конфигурации; фактические значения не скрываются) ──
  (IFNULL(m.cur_orders_gross_units, 0) >= cfg.min_units)                                    AS cur_orders_reliable,
  (IFNULL(m.prv_orders_gross_units, 0) >= cfg.min_units)                                    AS prv_orders_reliable,
  (IFNULL(m.cur_buyout_units, 0) >= cfg.min_units)                                          AS cur_buyouts_reliable,
  (IFNULL(m.prv_buyout_units, 0) >= cfg.min_units)                                          AS prv_buyouts_reliable,
  (IFNULL(m.cur_cohort_resolved_orders, 0) >= cfg.min_units)                                AS cur_cohort_reliable,
  (IFNULL(m.prv_cohort_resolved_orders, 0) >= cfg.min_units)                                AS prv_cohort_reliable,
  (IFNULL(m.cur_orders_gross_units, 0) < cfg.min_units AND IFNULL(m.cur_buyout_units, 0) < cfg.min_units) AS low_sample,
  -- ── Изменения: абсолютные — Δ и Δ%, доли — п.п. ──
  m.cur_orders_gross_units - m.prv_orders_gross_units                                                     AS orders_delta,
  SAFE_DIVIDE(m.cur_orders_gross_units - m.prv_orders_gross_units, NULLIF(ABS(m.prv_orders_gross_units), 0)) AS orders_delta_pct,
  m.cur_buyout_units - m.prv_buyout_units                                                                 AS buyouts_delta,
  SAFE_DIVIDE(m.cur_buyout_units - m.prv_buyout_units, NULLIF(ABS(m.prv_buyout_units), 0))                AS buyouts_delta_pct,
  SAFE_DIVIDE(m.cur_avg_seller_price_orders_rub - m.prv_avg_seller_price_orders_rub, NULLIF(m.prv_avg_seller_price_orders_rub, 0)) AS avg_seller_price_orders_delta_pct,
  SAFE_DIVIDE(m.cur_avg_seller_price_buyouts_rub - m.prv_avg_seller_price_buyouts_rub, NULLIF(m.prv_avg_seller_price_buyouts_rub, 0)) AS avg_seller_price_buyouts_delta_pct,
  SAFE_DIVIDE(m.cur_avg_buyer_paid_price_rub - m.prv_avg_buyer_paid_price_rub, NULLIF(m.prv_avg_buyer_paid_price_rub, 0)) AS avg_buyer_paid_price_delta_pct,
  m.cur_ads_attributed_rub - m.prv_ads_attributed_rub                                                     AS ads_delta_rub,
  SAFE_DIVIDE(m.cur_ads_attributed_rub - m.prv_ads_attributed_rub, NULLIF(ABS(m.prv_ads_attributed_rub), 0)) AS ads_delta_pct,
  m.cur_contribution_before_cogs_rub - m.prv_contribution_before_cogs_rub                                 AS contribution_before_cogs_delta_rub,
  m.cur_contribution_after_cogs_rub - m.prv_contribution_after_cogs_rub                                   AS contribution_after_cogs_delta_rub,
  SAFE_DIVIDE(m.cur_contribution_after_cogs_rub - m.prv_contribution_after_cogs_rub,
              NULLIF(ABS(m.prv_contribution_after_cogs_rub), 0))                                          AS contribution_after_cogs_delta_pct,
  (m.cur_cancellation_rate - m.prv_cancellation_rate) * 100                                               AS cancellation_rate_delta_pp,
  (m.cur_cohort_buyout_rate - m.prv_cohort_buyout_rate) * 100                                             AS cohort_buyout_rate_delta_pp,
  (m.cur_drr - m.prv_drr) * 100                                                                           AS drr_delta_pp,
  (m.cur_contribution_margin_after_cogs - m.prv_contribution_margin_after_cogs) * 100                     AS contribution_margin_after_cogs_delta_pp,
  -- ── Новый SKU: в предыдущем окне покрытие было, активности не было ──
  (IFNULL(m.prv_orders_cov_days, 0) > 0 AND IFNULL(m.prv_orders_gross_units, 0) = 0 AND IFNULL(m.prv_buyout_units, 0) = 0
   AND (IFNULL(m.cur_orders_gross_units, 0) > 0 OR IFNULL(m.cur_buyout_units, 0) > 0))                    AS is_new_in_period,
  -- ── Статус данных текущего окна ──
  CASE
    WHEN IFNULL(m.cur_days_present, 0) < bnd.len OR IFNULL(m.cur_current_days, 0) > 0
      OR IFNULL(m.cur_orders_cov_days, 0) < bnd.len OR IFNULL(m.cur_sales_cov_days, 0) < bnd.len
      OR IFNULL(m.cur_ads_cov_days, 0) < bnd.len OR IFNULL(m.cur_finance_cov_days, 0) < bnd.len   THEN 'INCOMPLETE'
    WHEN IFNULL(m.cur_finance_provisional_days, 0) > 0 THEN 'PROVISIONAL'   -- зрелость когорты — отдельный флаг cohort_mature
    ELSE 'FINAL'
  END AS data_status,
  ARRAY(SELECT x FROM UNNEST([
    IF(IFNULL(m.cur_days_present, 0) < bnd.len, FORMAT('Слой SKU покрывает %d из %d сут.', IFNULL(m.cur_days_present, 0), bnd.len), NULL),
    IF(IFNULL(m.cur_current_days, 0) > 0, 'Окно включает текущие сутки', NULL),
    IF(IFNULL(m.cur_finance_cov_days, 0) < bnd.len, FORMAT('Финансы: %d из %d сут.', IFNULL(m.cur_finance_cov_days, 0), bnd.len), NULL),
    IF(IFNULL(m.cur_finance_provisional_days, 0) > 0, FORMAT('Финансы предварительные: %d сут.', m.cur_finance_provisional_days), NULL),
    IF(IFNULL(m.cur_cohort_unresolved_orders, 0) + IFNULL(m.cur_cohort_conflict_orders, 0) > 0,
       FORMAT('Выкуп предварительный: %d заказов без исхода', m.cur_cohort_unresolved_orders + m.cur_cohort_conflict_orders), NULL),
    IF(IFNULL(m.cur_storage_cov_days, 0) < bnd.len, FORMAT('Хранение SKU: %d из %d сут. (отчёт с 01.09.2026)', IFNULL(m.cur_storage_cov_days, 0), bnd.len), NULL),
    IF(IFNULL(m.cur_price_chain_missing_days, 0) > 0, FORMAT('Состав удержаний из цены: нет за %d сут. (до 13.07.2026)', m.cur_price_chain_missing_days), NULL),
    IF(IFNULL(m.cur_cogs_missing_sku_days, 0) > 0, FORMAT('Себестоимость не определена: %d SKU-сут.', m.cur_cogs_missing_sku_days), NULL),
    IF(IFNULL(m.cur_orders_gross_units, 0) < cfg.min_units AND IFNULL(m.cur_buyout_units, 0) < cfg.min_units,
       FORMAT('Малая выборка: < %d шт.', CAST(cfg.min_units AS INT64)), NULL)
  ]) AS x WHERE x IS NOT NULL) AS data_status_reasons,
  (IFNULL(m.cur_storage_cov_days, 0) = bnd.len)       AS storage_complete,
  (IFNULL(m.cur_price_chain_missing_days, 0) = 0)     AS price_chain_complete,
  (IFNULL(m.cur_ads_cov_days, 0) = bnd.len)           AS ads_complete,
  (IFNULL(m.cur_cogs_missing_sku_days, 0) = 0)        AS cogs_complete,
  (IFNULL(m.cur_cohort_unresolved_orders, 0) + IFNULL(m.cur_cohort_conflict_orders, 0) = 0) AS cohort_mature
FROM m
LEFT JOIN ref USING (nm_id)
CROSS JOIN bnd
CROSS JOIN cfg;
