-- ============================================================================
-- EXECUTIVE V2 · PHASE C2 — материализованный дневной слой Executive
--
-- Документ: docs/EXECUTIVE_V2_PHASE_C2_MATERIALIZED_LAYER_2026-09-17.md
-- Валидация: sql/dash/executive_v2_daily_validation.sql
-- Откат:     sql/rollback/executive_v2_daily_2026-09-17/R_DROP_LAYER.sql
--            (канонические V_DASH_* не затрагиваются ни сборкой, ни откатом)
--
-- ЭТО ТОЛЬКО PERFORMANCE REFACTOR. Ни одна формула, знак, правило покрытия или
-- финальности не вводится и не меняется: таблица — pass-through канонических view,
-- а признаки, зависящие от момента чтения, пересчитываются во view ровно теми же
-- выражениями, что в канонических view (см. §4 и ASSERT A6 в процедуре).
--
-- Объекты:
--   wb_mart.EXECUTIVE_V2_DAILY              таблица, грейн day, детерминированные поля
--   wb_mart.EXECUTIVE_V2_BUILD_LOG          журнал сборок (STARTED/SUCCESS/FAILED/SKIPPED_LOCKED)
--   wb_mart._EXECUTIVE_V2_BUILD_LOCK        одна строка: запрет конкурирующих сборок
--   wb_mart.sp_build_executive_v2_daily     сборка: TEMP → ASSERT → атомарная транзакция
--   wb_mart.V_DASH_EXECUTIVE_V2_DAILY       контракт для Metabase
--
-- ЗАВИСИМОСТЬ ОТ МОМЕНТА ЧТЕНИЯ (в таблицу НЕ материализуется):
--   • календарь до CURRENT_DATE('Europe/Moscow')        — V_DASH_COVERAGE_DAILY
--   • is_current_day                                    — V_DASH_COVERAGE_DAILY
--   • финальность биллинга: возраст ≥ contract_settle_days — V_ADV_COSTS_DAY_COVERAGE
--   • executive_incomplete/provisional/final_day,
--     exec_provisional_ads_billing_day                  — V_DASH_EXECUTIVE_ECONOMICS_DAILY
--   • cohort_is_final / cohort_provisional_day          — V_DASH_BUYOUT_COHORT_DAILY
-- ============================================================================

-- ── §1. Журнал сборок и замок ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG` (
  run_id               STRING    NOT NULL,
  trigger_source       STRING,
  started_at           TIMESTAMP NOT NULL,
  finished_at          TIMESTAMP,
  status               STRING    NOT NULL,   -- STARTED | SUCCESS | FAILED | SKIPPED_LOCKED
  rows_built           INT64,
  source_data_through  DATE,                 -- «Данные по»: MIN(data_as_of) слоёв свежести
  mart_built_at        TIMESTAMP,            -- сборка MART_SKU_DAILY, попавшая в слой
  error_message        STRING
)
OPTIONS (description = 'Executive V2 · журнал сборок wb_mart.EXECUTIVE_V2_DAILY (sp_build_executive_v2_daily).');

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK` (
  lock_id      STRING NOT NULL,
  is_running   BOOL   NOT NULL,
  run_id       STRING,
  acquired_at  TIMESTAMP,
  released_at  TIMESTAMP
)
OPTIONS (description = 'Executive V2 · замок сборки: одна строка lock_id = executive_v2_daily.');

INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK` (lock_id, is_running)
SELECT 'executive_v2_daily', FALSE
FROM (SELECT 1)
WHERE NOT EXISTS (SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`
                  WHERE lock_id = 'executive_v2_daily');

-- ── §2. Таблица слоя (схема = тело сборки, без строк) ───────────────────────
--    Тело сборки дублируется в процедуре (§3). Схема фиксируется здесь один раз;
--    INSERT в процедуре идёт по тому же порядку колонок, расхождение упадёт ошибкой.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY`
OPTIONS (description = 'Executive V2 · материализованный дневной слой. Читать через V_DASH_EXECUTIVE_V2_DAILY. Пишет только sp_build_executive_v2_daily.')
AS
WITH
k AS (
  SELECT day, orders_covered, sales_covered, ads_covered, finance_covered, contribution_covered,
         finance_is_final, contains_provisional_finance,
         orders_gross_qty, buyouts_qty, sales_revenue_seller_base_rub, sales_revenue_buyer_paid_rub,
         logistics_rub, storage_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
c AS (
  SELECT day, ads_billing_covered, ad_spend_financial_rub,
         tariff_option_minimum_payment_rub, account_level_total_corrected_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
),
a AS (
  SELECT `date` AS day, (stable_reads >= contract_n_stable) AS ads_billing_stable_ok,
         contract_settle_days AS ads_billing_settle_days
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`
),
e AS (
  SELECT day, period_result_eligible, product_cogs_covered, after_product_cogs_eligible,
         period_result_pre_cogs_corrected_rub, revenue_base_period_result_rub,
         product_cogs_rub, period_result_after_product_cogs_rub, revenue_base_after_product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
),
s AS (
  SELECT day, settlement_eligible,
         settlement_price_chain_covered, settlement_seller_price_rub, settlement_spp_rub,
         settlement_buyer_paid_rub, settlement_wb_remuneration_rub, settlement_wb_remuneration_vat_rub,
         settlement_acquiring_rub, settlement_pvz_reward_rub, settlement_price_chain_rounding_rub,
         settlement_goods_other_operations_rub, settlement_goods_rub,
         post_realization_deductions_rub, wb_payout_rub, post_sale_deductions_rub,
         post_sale_wb_promotion_documents_rub, post_sale_logistics_rub, post_sale_storage_rub,
         post_sale_tariff_option_minimum_payment_rub, post_sale_utilization_rub, post_sale_transit_rub,
         post_sale_penalty_rub, post_sale_acceptance_rub, post_sale_force_majeure_rub,
         post_sale_review_points_refund_rub, post_sale_unclassified_deductions_rub,
         post_sale_loyalty_program_rub, post_sale_loyalty_points_rub, post_sale_other_operations_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`
),
x AS (
  SELECT day, logistics_sold_rub, logistics_cancellation_rub,
         logistics_cancel_to_customer_rub, logistics_cancel_from_customer_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`
),
h AS (
  SELECT cohort_date AS day, cohort_orders, buyout_orders, cancelled_orders, resolved_orders,
         unresolved_orders, conflict_orders, returned_after_buyout_orders
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`
),
f AS (
  SELECT data_as_of_min, mart_build_as_of_date, mart_built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FRESHNESS_HEADER`
)
SELECT
  k.day,
  k.* EXCEPT (day),
  c.* EXCEPT (day),
  a.* EXCEPT (day),
  e.* EXCEPT (day),
  s.* EXCEPT (day),
  x.* EXCEPT (day),
  h.* EXCEPT (day),
  f.data_as_of_min        AS source_data_through,
  f.mart_build_as_of_date,
  f.mart_built_at,
  CAST(NULL AS STRING)    AS layer_run_id,
  CAST(NULL AS TIMESTAMP) AS layer_built_at
FROM k
LEFT JOIN c USING (day)
LEFT JOIN a USING (day)
LEFT JOIN e USING (day)
LEFT JOIN s USING (day)
LEFT JOIN x USING (day)
LEFT JOIN h USING (day)
CROSS JOIN f
WHERE FALSE;

-- ── §3. Процедура сборки ────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_executive_v2_daily`(in_trigger STRING)
BEGIN
  DECLARE v_run_id  STRING    DEFAULT GENERATE_UUID();
  DECLARE v_started TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_rows    INT64;
  DECLARE v_through DATE;
  DECLARE v_mart_at TIMESTAMP;
  DECLARE v_in_tx   BOOL DEFAULT FALSE;   -- @@transaction_id недоступен в обработчике

  INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`
    (run_id, trigger_source, started_at, status)
  VALUES (v_run_id, IFNULL(in_trigger, 'manual'), v_started, 'STARTED');

  -- Замок. Конкурирующая сборка не перестраивает таблицу, а пишет SKIPPED_LOCKED.
  -- Замок старше 90 минут считается зависшим (штатная сборка ~1 мин).
  UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'executive_v2_daily'
     AND (NOT is_running OR acquired_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 MINUTE));
  IF @@row_count = 0 THEN
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`
       SET status = 'SKIPPED_LOCKED', finished_at = CURRENT_TIMESTAMP(),
           error_message = 'предыдущая сборка ещё выполняется'
     WHERE run_id = v_run_id;
    RETURN;
  END IF;

  BEGIN
    -- 1) Сборка во временную таблицу сессии. Продукционная таблица не затронута.
    CREATE TEMP TABLE b AS
    WITH
    k AS (
      SELECT day, orders_covered, sales_covered, ads_covered, finance_covered, contribution_covered,
             finance_is_final, contains_provisional_finance,
             orders_gross_qty, buyouts_qty, sales_revenue_seller_base_rub, sales_revenue_buyer_paid_rub,
             logistics_rub, storage_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
    ),
    c AS (
      SELECT day, ads_billing_covered, ad_spend_financial_rub,
             tariff_option_minimum_payment_rub, account_level_total_corrected_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
    ),
    a AS (
      SELECT `date` AS day, (stable_reads >= contract_n_stable) AS ads_billing_stable_ok,
             contract_settle_days AS ads_billing_settle_days
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`
    ),
    e AS (
      SELECT day, period_result_eligible, product_cogs_covered, after_product_cogs_eligible,
             period_result_pre_cogs_corrected_rub, revenue_base_period_result_rub,
             product_cogs_rub, period_result_after_product_cogs_rub, revenue_base_after_product_cogs_rub,
             -- канонические статусы — только для ASSERT A6, в таблицу не пишутся
             executive_incomplete_day AS canon_executive_incomplete_day,
             executive_provisional_day AS canon_executive_provisional_day,
             executive_final_day AS canon_executive_final_day,
             exec_provisional_ads_billing_day AS canon_exec_provisional_ads_billing_day
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
    ),
    s AS (
      SELECT day, settlement_eligible,
             settlement_price_chain_covered, settlement_seller_price_rub, settlement_spp_rub,
             settlement_buyer_paid_rub, settlement_wb_remuneration_rub, settlement_wb_remuneration_vat_rub,
             settlement_acquiring_rub, settlement_pvz_reward_rub, settlement_price_chain_rounding_rub,
             settlement_goods_other_operations_rub, settlement_goods_rub,
             post_realization_deductions_rub, wb_payout_rub, post_sale_deductions_rub,
             post_sale_wb_promotion_documents_rub, post_sale_logistics_rub, post_sale_storage_rub,
             post_sale_tariff_option_minimum_payment_rub, post_sale_utilization_rub, post_sale_transit_rub,
             post_sale_penalty_rub, post_sale_acceptance_rub, post_sale_force_majeure_rub,
             post_sale_review_points_refund_rub, post_sale_unclassified_deductions_rub,
             post_sale_loyalty_program_rub, post_sale_loyalty_points_rub, post_sale_other_operations_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`
    ),
    x AS (
      SELECT day, logistics_sold_rub, logistics_cancellation_rub,
             logistics_cancel_to_customer_rub, logistics_cancel_from_customer_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`
    ),
    h AS (
      SELECT cohort_date AS day, cohort_orders, buyout_orders, cancelled_orders, resolved_orders,
             unresolved_orders, conflict_orders, returned_after_buyout_orders,
             cohort_provisional_day AS canon_cohort_provisional_day
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`
    ),
    f AS (
      SELECT data_as_of_min, mart_build_as_of_date, mart_built_at
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FRESHNESS_HEADER`
    ),
    cal AS (
      SELECT day, is_current_day AS canon_is_current_day
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
    )
    SELECT
      k.day,
      k.* EXCEPT (day),
      c.* EXCEPT (day),
      a.* EXCEPT (day),
      e.* EXCEPT (day, canon_executive_incomplete_day, canon_executive_provisional_day,
                  canon_executive_final_day, canon_exec_provisional_ads_billing_day),
      s.* EXCEPT (day),
      x.* EXCEPT (day),
      h.* EXCEPT (day, canon_cohort_provisional_day),
      f.data_as_of_min        AS source_data_through,
      f.mart_build_as_of_date,
      f.mart_built_at,
      v_run_id                AS layer_run_id,
      v_started               AS layer_built_at,
      -- служебные канонические статусы (отбрасываются перед записью)
      cal.canon_is_current_day,
      e.canon_executive_incomplete_day, e.canon_executive_provisional_day,
      e.canon_executive_final_day, e.canon_exec_provisional_ads_billing_day,
      h.canon_cohort_provisional_day
    FROM k
    LEFT JOIN c   USING (day)
    LEFT JOIN a   USING (day)
    LEFT JOIN e   USING (day)
    LEFT JOIN s   USING (day)
    LEFT JOIN x   USING (day)
    LEFT JOIN h   USING (day)
    LEFT JOIN cal USING (day)
    CROSS JOIN f;

    -- 2) ASSERT до подмены. Любой отказ → EXCEPTION → таблица не тронута.
    -- A1 грейн
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT day) AND COUNTIF(day IS NULL) = 0 FROM b)
      AS 'EXEC_V2_DAILY A1: day не уникален или NULL';
    -- A2 календарь без дыр
    ASSERT (SELECT COUNT(*) = DATE_DIFF(MAX(day), MIN(day), DAY) + 1 FROM b)
      AS 'EXEC_V2_DAILY A2: в календаре есть дыры';
    -- A3 календарный контракт: ровно сутки канонического календаря, последний день = сегодня (МСК)
    ASSERT (SELECT COUNT(*) > 0
               AND MAX(day) = CURRENT_DATE('Europe/Moscow')
               AND COUNTIF(canon_is_current_day) = 1
               AND COUNTIF(canon_is_current_day AND day <> CURRENT_DATE('Europe/Moscow')) = 0
            FROM b)
      AS 'EXEC_V2_DAILY A3: календарь не совпал с каноническим (последний день ≠ сегодня)';
    -- A4 бухгалтерские тождества
    ASSERT (SELECT
              COUNTIF(after_product_cogs_eligible
                      AND ABS(period_result_after_product_cogs_rub
                              - (period_result_pre_cogs_corrected_rub - product_cogs_rub)) > 0.000001)
            + COUNTIF(settlement_eligible
                      AND ABS(settlement_goods_rub + post_realization_deductions_rub - wb_payout_rub) > 0.005)
            + COUNTIF(settlement_eligible
                      AND ABS(post_sale_wb_promotion_documents_rub + post_sale_logistics_rub + post_sale_storage_rub
                            + post_sale_tariff_option_minimum_payment_rub + post_sale_utilization_rub
                            + post_sale_transit_rub + post_sale_penalty_rub + post_sale_acceptance_rub
                            + post_sale_force_majeure_rub + post_sale_review_points_refund_rub
                            + post_sale_unclassified_deductions_rub + post_sale_loyalty_program_rub
                            + post_sale_loyalty_points_rub + post_sale_other_operations_rub
                            - post_sale_deductions_rub) > 0.005)
            + COUNTIF(settlement_price_chain_covered
                      AND ABS(settlement_seller_price_rub - settlement_spp_rub - settlement_wb_remuneration_rub
                            - settlement_wb_remuneration_vat_rub - settlement_acquiring_rub
                            - settlement_pvz_reward_rub - settlement_price_chain_rounding_rub
                            + settlement_goods_other_operations_rub - settlement_goods_rub) > 0.005)
            + COUNTIF(ABS(IFNULL(logistics_cancellation_rub, 0)
                          - IFNULL(logistics_cancel_to_customer_rub, 0)
                          - IFNULL(logistics_cancel_from_customer_rub, 0)) > 0.005)
            + COUNTIF(cohort_orders IS NOT NULL
                      AND (resolved_orders <> buyout_orders + cancelled_orders
                           OR cohort_orders <> buyout_orders + cancelled_orders + unresolved_orders + conflict_orders))
            FROM b) = 0
      AS 'EXEC_V2_DAILY A4: нарушено бухгалтерское тождество';
    -- A5 канонические значения: построчное повторное чтение каждого канонического view
    --    (одно view на запрос — иначе «query is too complex»). Сравниваются все колонки,
    --    взятые из view, включая одноимённые колонки, которые слой хранит одной копией
    --    (карточки 54/168/175/176 читают их из CORRECTED, 50 — из ECONOMICS).
    --    Расхождение = данные изменились во время сборки или слой разошёлся с контрактом.
    ASSERT (SELECT COUNTIF(v.orders_covered IS DISTINCT FROM b.orders_covered)
                 + COUNTIF(v.sales_covered IS DISTINCT FROM b.sales_covered)
                 + COUNTIF(v.ads_covered IS DISTINCT FROM b.ads_covered)
                 + COUNTIF(v.finance_covered IS DISTINCT FROM b.finance_covered)
                 + COUNTIF(v.contribution_covered IS DISTINCT FROM b.contribution_covered)
                 + COUNTIF(v.finance_is_final IS DISTINCT FROM b.finance_is_final)
                 + COUNTIF(v.contains_provisional_finance IS DISTINCT FROM b.contains_provisional_finance)
                 + COUNTIF(v.orders_gross_qty IS DISTINCT FROM b.orders_gross_qty)
                 + COUNTIF(v.buyouts_qty IS DISTINCT FROM b.buyouts_qty)
                 + COUNTIF(v.sales_revenue_seller_base_rub IS DISTINCT FROM b.sales_revenue_seller_base_rub)
                 + COUNTIF(v.sales_revenue_buyer_paid_rub IS DISTINCT FROM b.sales_revenue_buyer_paid_rub)
                 + COUNTIF(v.logistics_rub IS DISTINCT FROM b.logistics_rub)
                 + COUNTIF(v.storage_rub IS DISTINCT FROM b.storage_rub)
                 + COUNTIF(b.day IS NULL OR v.day IS NULL)
            FROM b FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY` v ON v.day = b.day) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_KPI_DAILY';
    ASSERT (SELECT COUNTIF(v.ads_billing_covered IS DISTINCT FROM b.ads_billing_covered)
                 + COUNTIF(v.ad_spend_financial_rub IS DISTINCT FROM b.ad_spend_financial_rub)
                 + COUNTIF(v.tariff_option_minimum_payment_rub IS DISTINCT FROM b.tariff_option_minimum_payment_rub)
                 + COUNTIF(v.account_level_total_corrected_rub IS DISTINCT FROM b.account_level_total_corrected_rub)
                 + COUNTIF(v.sales_revenue_seller_base_rub IS DISTINCT FROM b.sales_revenue_seller_base_rub)
                 + COUNTIF(v.storage_rub IS DISTINCT FROM b.storage_rub)
                 + COUNTIF(v.period_result_eligible IS DISTINCT FROM b.period_result_eligible)
                 + COUNTIF(v.period_result_pre_cogs_corrected_rub IS DISTINCT FROM b.period_result_pre_cogs_corrected_rub)
                 + COUNTIF(v.revenue_base_period_result_rub IS DISTINCT FROM b.revenue_base_period_result_rub)
                 + COUNTIF(b.day IS NULL OR v.day IS NULL)
            FROM b FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v ON v.day = b.day) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_FINANCE_CORRECTED_DAILY';
    ASSERT (SELECT COUNTIF(v.period_result_eligible IS DISTINCT FROM b.period_result_eligible)
                 + COUNTIF(v.product_cogs_covered IS DISTINCT FROM b.product_cogs_covered)
                 + COUNTIF(v.after_product_cogs_eligible IS DISTINCT FROM b.after_product_cogs_eligible)
                 + COUNTIF(v.period_result_pre_cogs_corrected_rub IS DISTINCT FROM b.period_result_pre_cogs_corrected_rub)
                 + COUNTIF(v.revenue_base_period_result_rub IS DISTINCT FROM b.revenue_base_period_result_rub)
                 + COUNTIF(v.product_cogs_rub IS DISTINCT FROM b.product_cogs_rub)
                 + COUNTIF(v.period_result_after_product_cogs_rub IS DISTINCT FROM b.period_result_after_product_cogs_rub)
                 + COUNTIF(v.revenue_base_after_product_cogs_rub IS DISTINCT FROM b.revenue_base_after_product_cogs_rub)
                 + COUNTIF(v.sales_revenue_seller_base_rub IS DISTINCT FROM b.sales_revenue_seller_base_rub)
                 + COUNTIF(b.day IS NULL OR v.day IS NULL)
            FROM b FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` v ON v.day = b.day) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_EXECUTIVE_ECONOMICS_DAILY';
    ASSERT (SELECT COUNTIF(v.settlement_eligible IS DISTINCT FROM b.settlement_eligible)
                 + COUNTIF(v.settlement_price_chain_covered IS DISTINCT FROM b.settlement_price_chain_covered)
                 + COUNTIF(v.settlement_seller_price_rub IS DISTINCT FROM b.settlement_seller_price_rub)
                 + COUNTIF(v.settlement_spp_rub IS DISTINCT FROM b.settlement_spp_rub)
                 + COUNTIF(v.settlement_buyer_paid_rub IS DISTINCT FROM b.settlement_buyer_paid_rub)
                 + COUNTIF(v.settlement_wb_remuneration_rub IS DISTINCT FROM b.settlement_wb_remuneration_rub)
                 + COUNTIF(v.settlement_wb_remuneration_vat_rub IS DISTINCT FROM b.settlement_wb_remuneration_vat_rub)
                 + COUNTIF(v.settlement_acquiring_rub IS DISTINCT FROM b.settlement_acquiring_rub)
                 + COUNTIF(v.settlement_pvz_reward_rub IS DISTINCT FROM b.settlement_pvz_reward_rub)
                 + COUNTIF(v.settlement_price_chain_rounding_rub IS DISTINCT FROM b.settlement_price_chain_rounding_rub)
                 + COUNTIF(v.settlement_goods_other_operations_rub IS DISTINCT FROM b.settlement_goods_other_operations_rub)
                 + COUNTIF(v.settlement_goods_rub IS DISTINCT FROM b.settlement_goods_rub)
                 + COUNTIF(v.post_realization_deductions_rub IS DISTINCT FROM b.post_realization_deductions_rub)
                 + COUNTIF(v.wb_payout_rub IS DISTINCT FROM b.wb_payout_rub)
                 + COUNTIF(v.post_sale_deductions_rub IS DISTINCT FROM b.post_sale_deductions_rub)
                 + COUNTIF(v.post_sale_wb_promotion_documents_rub IS DISTINCT FROM b.post_sale_wb_promotion_documents_rub)
                 + COUNTIF(v.post_sale_logistics_rub IS DISTINCT FROM b.post_sale_logistics_rub)
                 + COUNTIF(v.post_sale_storage_rub IS DISTINCT FROM b.post_sale_storage_rub)
                 + COUNTIF(v.post_sale_tariff_option_minimum_payment_rub IS DISTINCT FROM b.post_sale_tariff_option_minimum_payment_rub)
                 + COUNTIF(v.post_sale_utilization_rub IS DISTINCT FROM b.post_sale_utilization_rub)
                 + COUNTIF(v.post_sale_transit_rub IS DISTINCT FROM b.post_sale_transit_rub)
                 + COUNTIF(v.post_sale_penalty_rub IS DISTINCT FROM b.post_sale_penalty_rub)
                 + COUNTIF(v.post_sale_acceptance_rub IS DISTINCT FROM b.post_sale_acceptance_rub)
                 + COUNTIF(v.post_sale_force_majeure_rub IS DISTINCT FROM b.post_sale_force_majeure_rub)
                 + COUNTIF(v.post_sale_review_points_refund_rub IS DISTINCT FROM b.post_sale_review_points_refund_rub)
                 + COUNTIF(v.post_sale_unclassified_deductions_rub IS DISTINCT FROM b.post_sale_unclassified_deductions_rub)
                 + COUNTIF(v.post_sale_loyalty_program_rub IS DISTINCT FROM b.post_sale_loyalty_program_rub)
                 + COUNTIF(v.post_sale_loyalty_points_rub IS DISTINCT FROM b.post_sale_loyalty_points_rub)
                 + COUNTIF(v.post_sale_other_operations_rub IS DISTINCT FROM b.post_sale_other_operations_rub)
                 + COUNTIF(b.day IS NULL)
            FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` v LEFT JOIN b ON b.day = v.day) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_SETTLEMENT_DAILY';
    ASSERT (SELECT COUNTIF(v.logistics_sold_rub IS DISTINCT FROM b.logistics_sold_rub)
                 + COUNTIF(v.logistics_cancellation_rub IS DISTINCT FROM b.logistics_cancellation_rub)
                 + COUNTIF(v.logistics_cancel_to_customer_rub IS DISTINCT FROM b.logistics_cancel_to_customer_rub)
                 + COUNTIF(v.logistics_cancel_from_customer_rub IS DISTINCT FROM b.logistics_cancel_from_customer_rub)
                 + COUNTIF(b.day IS NULL OR v.day IS NULL)
            FROM b FULL JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` v ON v.day = b.day) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_EXECUTIVE_BREAKDOWN_DAILY';
    ASSERT (SELECT COUNTIF(v.cohort_orders IS DISTINCT FROM b.cohort_orders)
                 + COUNTIF(v.buyout_orders IS DISTINCT FROM b.buyout_orders)
                 + COUNTIF(v.cancelled_orders IS DISTINCT FROM b.cancelled_orders)
                 + COUNTIF(v.resolved_orders IS DISTINCT FROM b.resolved_orders)
                 + COUNTIF(v.unresolved_orders IS DISTINCT FROM b.unresolved_orders)
                 + COUNTIF(v.conflict_orders IS DISTINCT FROM b.conflict_orders)
                 + COUNTIF(v.returned_after_buyout_orders IS DISTINCT FROM b.returned_after_buyout_orders)
                 + COUNTIF(b.day IS NULL)
            FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` v LEFT JOIN b ON b.day = v.cohort_date) = 0
      AS 'EXEC_V2_DAILY A5: слой разошёлся с V_DASH_BUYOUT_COHORT_DAILY';
    -- A6 признаки момента чтения: выражения §4 на момент сборки = канонические статусы.
    --    🔴 Выражения ОБЯЗАНЫ совпадать с V_DASH_EXECUTIVE_V2_DAILY (§4).
    ASSERT (
      WITH r AS (
        SELECT
          day,
          (day >= CURRENT_DATE('Europe/Moscow')) AS is_current_day,
          (IFNULL(ads_billing_covered, FALSE) AND IFNULL(ads_billing_stable_ok, FALSE)
           AND DATE_DIFF(CURRENT_DATE('Europe/Moscow'), day, DAY) >= ads_billing_settle_days) AS ads_billing_is_final,
          *
        FROM b
      ),
      st AS (
        SELECT
          r.*,
          IF(IFNULL(ads_billing_covered, FALSE) AND NOT ads_billing_is_final, 1, 0) AS exec_provisional_ads_billing_day,
          IF(NOT IFNULL(after_product_cogs_eligible, FALSE) OR is_current_day, 1, 0) AS executive_incomplete_day,
          IF(IFNULL(after_product_cogs_eligible, FALSE) AND NOT is_current_day
             AND (contains_provisional_finance OR NOT ads_billing_is_final), 1, 0) AS executive_provisional_day,
          IF(IFNULL(after_product_cogs_eligible, FALSE) AND NOT is_current_day
             AND NOT contains_provisional_finance AND ads_billing_is_final, 1, 0) AS executive_final_day,
          IF(cohort_orders IS NULL, NULL,
             IF(unresolved_orders + conflict_orders = 0 AND NOT is_current_day, 0, 1)) AS cohort_provisional_day
        FROM r
      )
      SELECT COUNTIF(is_current_day IS DISTINCT FROM canon_is_current_day)
           + COUNTIF(executive_incomplete_day IS DISTINCT FROM canon_executive_incomplete_day)
           + COUNTIF(executive_provisional_day IS DISTINCT FROM canon_executive_provisional_day)
           + COUNTIF(executive_final_day IS DISTINCT FROM canon_executive_final_day)
           + COUNTIF(exec_provisional_ads_billing_day IS DISTINCT FROM canon_exec_provisional_ads_billing_day)
           + COUNTIF(cohort_provisional_day IS DISTINCT FROM canon_cohort_provisional_day)
      FROM st
    ) = 0 AS 'EXEC_V2_DAILY A6: пересчёт статусов разошёлся с каноническими view';

    SET v_rows    = (SELECT COUNT(*) FROM b);
    SET v_through = (SELECT ANY_VALUE(source_data_through) FROM b);
    SET v_mart_at = (SELECT ANY_VALUE(mart_built_at) FROM b);

    -- 3) Атомарная подмена: читатели видят либо старую, либо новую версию целиком.
    BEGIN TRANSACTION;
    SET v_in_tx = TRUE;
      DELETE FROM `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY` WHERE TRUE;
      INSERT INTO `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY`
      SELECT * EXCEPT (canon_is_current_day, canon_executive_incomplete_day, canon_executive_provisional_day,
                       canon_executive_final_day, canon_exec_provisional_ads_billing_day,
                       canon_cohort_provisional_day)
      FROM b;
    COMMIT TRANSACTION;
    SET v_in_tx = FALSE;

    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'executive_v2_daily' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`
       SET status = 'SUCCESS', finished_at = CURRENT_TIMESTAMP(), rows_built = v_rows,
           source_data_through = v_through, mart_built_at = v_mart_at
     WHERE run_id = v_run_id;

  EXCEPTION WHEN ERROR THEN
    IF v_in_tx THEN
      ROLLBACK TRANSACTION;
    END IF;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`
       SET is_running = FALSE, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'executive_v2_daily' AND run_id = v_run_id;
    UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`
       SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP(), error_message = @@error.message
     WHERE run_id = v_run_id;
    RAISE USING MESSAGE = CONCAT('sp_build_executive_v2_daily FAILED: ', @@error.message);
  END;
END;

-- ── §4. Контракт для Metabase ──────────────────────────────────────────────
--    Календарь и признаки момента чтения считаются здесь. Выражения совпадают с
--    каноническими view и с ASSERT A6 процедуры.
--    is_current_day: сутки = сегодня ИЛИ сутки не раньше даты сборки слоя. Второе
--    условие — только для строки, снятой до окончания суток: если слой собран днём
--    и после полуночи ещё не пересобран, вчерашние неполные данные не выдаются за
--    полные. При сборке «сегодня» условие совпадает с каноническим день-в-день.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
OPTIONS (description = 'Executive V2 · контракт Metabase поверх EXECUTIVE_V2_DAILY. Значения = канонические V_DASH_*; признаки момента чтения пересчитываются здесь.')
AS
WITH
meta AS (
  SELECT ANY_VALUE(layer_run_id) AS layer_run_id, ANY_VALUE(layer_built_at) AS layer_built_at,
         MIN(day) AS first_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY`
),
build AS (
  SELECT
    (SELECT AS STRUCT finished_at, status FROM `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG` l, meta
     WHERE l.run_id = meta.layer_run_id) AS layer_build,
    (SELECT AS STRUCT started_at, finished_at, status, error_message
     FROM `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`
     WHERE status <> 'SKIPPED_LOCKED'
     ORDER BY started_at DESC LIMIT 1) AS last_attempt
),
cal AS (
  SELECT day
  FROM meta, UNNEST(GENERATE_DATE_ARRAY(meta.first_day, CURRENT_DATE('Europe/Moscow'))) AS day
),
r AS (
  SELECT
    cal.day,
    (cal.day >= CURRENT_DATE('Europe/Moscow')
     OR cal.day >= DATE(meta.layer_built_at, 'Europe/Moscow'))                      AS is_current_day,
    (IFNULL(t.ads_billing_covered, FALSE) AND IFNULL(t.ads_billing_stable_ok, FALSE)
     AND DATE_DIFF(CURRENT_DATE('Europe/Moscow'), cal.day, DAY) >= t.ads_billing_settle_days) AS ads_billing_is_final,
    t.* EXCEPT (day, layer_run_id, layer_built_at, source_data_through, mart_build_as_of_date, mart_built_at)
  FROM cal
  CROSS JOIN meta
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY` t USING (day)
)
SELECT
  r.day,
  r.day AS cohort_date,
  r.* EXCEPT (day),
  IF(IFNULL(r.ads_billing_covered, FALSE) AND NOT r.ads_billing_is_final, 1, 0)     AS exec_provisional_ads_billing_day,
  IF(NOT IFNULL(r.after_product_cogs_eligible, FALSE) OR r.is_current_day, 1, 0)   AS executive_incomplete_day,
  IF(IFNULL(r.after_product_cogs_eligible, FALSE) AND NOT r.is_current_day
     AND (r.contains_provisional_finance OR NOT r.ads_billing_is_final), 1, 0)     AS executive_provisional_day,
  IF(IFNULL(r.after_product_cogs_eligible, FALSE) AND NOT r.is_current_day
     AND NOT r.contains_provisional_finance AND r.ads_billing_is_final, 1, 0)      AS executive_final_day,
  IF(r.cohort_orders IS NULL, NULL,
     IF(r.unresolved_orders + r.conflict_orders = 0 AND NOT r.is_current_day, 0, 1)) AS cohort_provisional_day,
  -- ── Свежесть (одинакова во всех строках) ──
  f.source_data_through,                                  -- «Данные по»
  f.mart_built_at,                                        -- сборка MART_SKU_DAILY внутри слоя
  meta.layer_run_id,
  build.layer_build.finished_at                           AS layer_built_at,    -- «Витрина собрана»
  build.last_attempt.status                               AS last_build_status,
  build.last_attempt.started_at                           AS last_build_started_at,
  build.last_attempt.error_message                        AS last_build_error,
  CASE
    WHEN build.last_attempt.status = 'FAILED' THEN 'последняя сборка не удалась'
    WHEN build.last_attempt.status = 'STARTED'
     AND build.last_attempt.started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 MINUTE) THEN 'сборка зависла'
  END                                                     AS layer_build_alert  -- NULL = норма
FROM r
CROSS JOIN meta
CROSS JOIN build
CROSS JOIN (SELECT ANY_VALUE(source_data_through) AS source_data_through, ANY_VALUE(mart_built_at) AS mart_built_at
            FROM `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY`) f;
