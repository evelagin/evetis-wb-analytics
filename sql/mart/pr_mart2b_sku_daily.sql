-- ============================================================================
-- PR-Mart2b (PR#81) — EVETIS WB Analytics MART. Витрина #1: MART_SKU_DAILY (day × nm_id). REV2 (аудит).
-- Дата: 2026-07-30.  Контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md (§4 spine, §KPI).
-- PR-нота: docs/MART_PR2B_SKU_DAILY_2026-07-30.md.  Зависит от PR-Mart2a (LONG_MAPPED в проде).
--
-- REV2 по замечаниям аудитора (REQUEST CHANGES, PR#81):
--   #1 Fail-closed guard: в V_WB_FINANCE_AMOUNTS_LONG_MAPPED (finance_date<=build_as_of) НЕТ строк
--      с cost_category IS NULL — неизвестные денежные пары не должны молча исчезать из витрины.
--   #2 Полнота universe: guard global_start<=build_as_of; BUILD rows>0;
--      (active REF nm_id) EXCEPT DISTINCT (BUILD nm_id) = 0 — ловит ПОЛНОСТЬЮ отсутствующий SKU.
--   #3 Все source-CTE и reconciliation ограничены source_date<=build_as_of (воспроизводимость на дату;
--      особенно finance/LONG_MAPPED — finance исключён из max_required_source_date).
--   #4 Убрана NULL-ловушка GREATEST/LEAST: max/min через MAX/MIN(d) FROM UNNEST(dates) WHERE d IS NOT NULL
--      + явный RAISE, если обязательные даты определить невозможно (пустые источники).
--   (+ добавлен KPI cpm = spend/views×1000 — был в списке KPI аудита.)
-- REV3 (re-audit): остаточный fail-closed blocker — NULL-safe UNNEST молча терпел пустой ОТДЕЛЬНЫЙ
--   обязательный источник (max_required считался по остальным). Теперь per-source max-date по
--   FACT_ORDERS/FACT_SALES/FACT_ADS_SKU_DAILY + явный RAISE на КАЖДЫЙ пустой; отдельная проверка непустоты
--   finance LONG_MAPPED (в max_required НЕ входит); только затем GREATEST(orders_max, sales_max, ads_max).
--   CPM входит в витрину (не deferred).
-- REV4 (re-audit): finance-guard считал ВСЕ строки LONG_MAPPED, а витрина потребляет только is_sku_row.
--   При сбое reference-мэппинга (все строки → ACCOUNT) guard прошёл бы, а fin оказался пуст → нулевая витрина
--   опубликовалась бы. Теперь считаем ТОЛЬКО is_sku_row + active universe (реальный источник витрины).
-- REV5 (re-audit): guard is_sku_row всё ещё шире, чем два выводимых показателя (commission_cost_positive,
--   logistics_cost_positive). Развёл на ДВА счётчика по cost_category='commission' и ='logistics' с RAISE каждый —
--   при сбое mapping конкретной категории общий is_sku_row-guard прошёл бы, а метрика занулилась.
--
-- Грейн: day × nm_id, DENSE spine. Universe = REF_SKU_MASTER (WB, active, nm_id NOT NULL).
--   start_date(nm)=LEAST(MIN order/sale/ads/finance по nm, все <=build_as_of); fallback=mart_global_start_date.
--   GENERATE_DATE_ARRAY(start, build_as_of) → пропуски=0. Rolling: RANGE 6/13 PRECEDING по UNIX_DATE(day).
--   Заказы: orders_qty/rub ЧИСТЫЕ (is_cancel=FALSE) + canceled_*. Выкупы: is_return=FALSE + returns_*.
--   Вклад-до-COGS cross-base (НЕ P&L дня); основной управленческий = rolling. Маржа (−COGS) — MART v2.
--
-- Паттерн Mart1: BUILD → ASSERT → publish → ASSERT физики. BOOTSTRAP/MANUAL-ONLY, lock 'mart_sku_daily'.
-- ⚠️ Витрина создаётся ТОЛЬКО после merge PR#81. Оркестрация (runWbMartDaily) — PR-Mart3.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

CREATE TABLE IF NOT EXISTS `wb_mart._MART_BOOTSTRAP_LOCK` (
  lock_id STRING NOT NULL, is_running BOOL, run_id STRING, last_run_id STRING,
  acquired_at TIMESTAMP, released_at TIMESTAMP
);
MERGE `wb_mart._MART_BOOTSTRAP_LOCK` T
USING (SELECT 'mart_sku_daily' AS lock_id) S ON T.lock_id = S.lock_id
WHEN NOT MATCHED THEN INSERT (lock_id, is_running) VALUES ('mart_sku_daily', FALSE);

CREATE OR REPLACE PROCEDURE `wb_mart.sp_build_mart_sku_daily`(
  IN in_build_as_of_date DATE,
  IN in_global_start_date DATE
)
BEGIN
  DECLARE v_run_id       STRING    DEFAULT GENERATE_UUID();
  DECLARE v_built_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_build_as_of  DATE      DEFAULT in_build_as_of_date;
  DECLARE v_global_start DATE;
  DECLARE v_max_required DATE;
  DECLARE v_orders_max   DATE;
  DECLARE v_sales_max    DATE;
  DECLARE v_ads_max                 DATE;
  DECLARE v_finance_commission_rows INT64;
  DECLARE v_finance_logistics_rows  INT64;

  -- build_as_of обязателен раньше всего (используется в проверке непустоты finance ниже).
  IF v_build_as_of IS NULL THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: build_as_of_date IS NULL (нужен явный DATE)'; END IF;

  -- REV3 (re-audit): ОТДЕЛЬНАЯ max-date по каждому обязательному источнику + явный RAISE на пустой.
  --   NULL-safe UNNEST раньше молча терпел пустой отдельный источник (max_required считался по остальным).
  SET v_orders_max = (SELECT MAX(order_date) FROM `wb_mart.FACT_ORDERS`);
  SET v_sales_max  = (SELECT MAX(sale_date)  FROM `wb_mart.FACT_SALES`);
  SET v_ads_max    = (SELECT MAX(`date`)     FROM `wb_mart.FACT_ADS_SKU_DAILY`);
  IF v_orders_max IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_ORDERS пуст'; END IF;
  IF v_sales_max  IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_SALES пуст'; END IF;
  IF v_ads_max    IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_ADS_SKU_DAILY пуст'; END IF;

  -- finance НЕ входит в max_required (лагает недельно), но ДВА обязательных финансовых входа витрины —
  --   SKU commission и SKU logistics (cost_category IN ('commission','logistics'), is_sku_row, active universe,
  --   cost_amount_positive IS NOT NULL) — обязаны быть непусты. REV5 (аудит): счётчики ПО КАЖДОЙ категории отдельно —
  --   иначе при сбое mapping конкретной категории (commission/logistics исчезли, прочие SKU-строки остались) общий
  --   guard прошёл бы, а соответствующая метрика витрины занулилась, и reconciliation сравнил бы ноль с нулём.
  SET v_finance_commission_rows = (
    SELECT COUNT(*) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
    WHERE finance_date <= v_build_as_of AND is_sku_row
      AND cost_category = 'commission' AND cost_amount_positive IS NOT NULL
      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL));
  SET v_finance_logistics_rows = (
    SELECT COUNT(*) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
    WHERE finance_date <= v_build_as_of AND is_sku_row
      AND cost_category = 'logistics' AND cost_amount_positive IS NOT NULL
      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL));
  IF v_finance_commission_rows = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: SKU commission finance пуст (is_sku_row, active universe, <=build_as_of)'; END IF;
  IF v_finance_logistics_rows = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: SKU logistics finance пуст (is_sku_row, active universe, <=build_as_of)'; END IF;

  -- Все три обязательных источника непусты → GREATEST безопасен (нет NULL-аргументов).
  SET v_max_required = GREATEST(v_orders_max, v_sales_max, v_ads_max);

  -- global_start: fallback для «мёртвых» SKU (обязательные источники непусты → MIN определён; finance тоже проверен).
  SET v_global_start = IFNULL(in_global_start_date, (
    SELECT MIN(d) FROM UNNEST([
      (SELECT MIN(order_date)   FROM `wb_mart.FACT_ORDERS`),
      (SELECT MIN(sale_date)    FROM `wb_mart.FACT_SALES`),
      (SELECT MIN(`date`)       FROM `wb_mart.FACT_ADS_SKU_DAILY`),
      (SELECT MIN(finance_date) FROM `wb_mart.FACT_FINANCE`)
    ]) AS d WHERE d IS NOT NULL));

  -- --- остальные guards (fail-closed) ---
  IF v_build_as_of > CURRENT_DATE('Europe/Moscow') THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: build_as_of_date в будущем (> сегодня МСК)'; END IF;
  IF v_build_as_of < v_max_required THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: build_as_of_date < max_required_source_date (orders/sales/ads свежее границы)'; END IF;
  IF v_global_start IS NULL THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: mart_global_start_date не определён'; END IF;
  IF v_global_start > v_build_as_of THEN                                            -- fix #2
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: global_start > build_as_of (пустой spine)'; END IF;

  -- fix #1: неизвестные денежные пары (cost_category IS NULL) в окне <=build_as_of ЗАПРЕЩЕНЫ.
  --   Иначе fin-CTE (cost_amount_positive IS NOT NULL) молча уронил бы их из витрины.
  ASSERT (SELECT COUNTIF(cost_category IS NULL)
          FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
          WHERE finance_date <= v_build_as_of) = 0
    AS 'SKU_DAILY §pre: unknown money-pair (cost_category IS NULL) в LONG_MAPPED — расширить REF_COST_MAP';

  -- --- concurrency guard ---
  UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'mart_sku_daily' AND is_running = FALSE;
  IF @@row_count = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: lock занят (manual-only). Снять _MART_BOOTSTRAP_LOCK при зависшем ране.'; END IF;

  BEGIN
    -- fix #3: ВСЕ source-CTE ограничены <= @build_as_of (воспроизводимость на дату).
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.MART_SKU_DAILY__BUILD`
      PARTITION BY day CLUSTER BY nm_id AS
      WITH universe AS (
        SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL GROUP BY nm_id),
      ads AS (
        SELECT nm_id, `date` d, SUM(stats_spend_rub) ad_spend, SUM(views) views, SUM(clicks) clicks,
          SUM(ad_orders_raw) ad_orders_raw, SUM(ads_revenue_raw_rub) ads_revenue_raw_rub,
          SUM(ads_revenue_dedup_estimate_rub) ads_revenue_dedup_estimate_rub,
          SUM(ad_orders_dedup_estimate) ad_orders_dedup_estimate
        FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE `date` <= @build_as_of GROUP BY nm_id, `date`),
      ord AS (
        SELECT nm_id, order_date d, COUNTIF(NOT is_cancel) orders_qty,
          SUM(IF(NOT is_cancel, price_with_disc, 0)) orders_rub,
          COUNTIF(is_cancel) canceled_qty, SUM(IF(is_cancel, price_with_disc, 0)) canceled_rub
        FROM `wb_mart.FACT_ORDERS` WHERE order_date <= @build_as_of GROUP BY nm_id, order_date),
      sal AS (
        SELECT nm_id, sale_date d, COUNTIF(NOT is_return) buyouts_qty,
          SUM(IF(NOT is_return, price_with_disc, 0)) buyouts_rub,
          SUM(IF(NOT is_return, sales_for_pay_operational, 0)) sales_for_pay_operational,
          COUNTIF(is_return) returns_qty, SUM(IF(is_return, price_with_disc, 0)) returns_rub
        FROM `wb_mart.FACT_SALES` WHERE sale_date <= @build_as_of GROUP BY nm_id, sale_date),
      fin AS (
        SELECT nm_id, finance_date d,
          SUM(IF(cost_category='commission', cost_amount_positive, 0)) commission_cost_positive,
          SUM(IF(cost_category='logistics',  cost_amount_positive, 0)) logistics_cost_positive
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE is_sku_row AND cost_amount_positive IS NOT NULL AND finance_date <= @build_as_of
        GROUP BY nm_id, finance_date),
      finpay AS (
        SELECT nm_id, finance_date d, SUM(finance_for_pay_accounting) finance_for_pay_accounting
        FROM `wb_mart.FACT_FINANCE`
        WHERE COALESCE(nm_id > 0 AND sku_match_status='matched', FALSE) AND finance_date <= @build_as_of
        GROUP BY nm_id, finance_date),
      findt AS (SELECT nm_id, MIN(finance_date) d FROM `wb_mart.FACT_FINANCE`
                WHERE nm_id > 0 AND finance_date <= @build_as_of GROUP BY nm_id),
      firstev AS (
        SELECT nm_id, MIN(d) first_d FROM (
          SELECT nm_id, d FROM ads UNION ALL SELECT nm_id, d FROM ord
          UNION ALL SELECT nm_id, d FROM sal UNION ALL SELECT nm_id, d FROM findt
        ) GROUP BY nm_id),
      spine AS (
        SELECT u.nm_id, day
        FROM universe u LEFT JOIN firstev f USING (nm_id),
        UNNEST(GENERATE_DATE_ARRAY(IFNULL(f.first_d, @global_start), @build_as_of)) day),
      joined AS (
        SELECT s.nm_id, s.day,
          IFNULL(a.ad_spend,0) ad_spend, IFNULL(a.views,0) views, IFNULL(a.clicks,0) clicks,
          IFNULL(a.ad_orders_raw,0) ad_orders_raw, IFNULL(a.ads_revenue_raw_rub,0) ads_revenue_raw_rub,
          IFNULL(a.ads_revenue_dedup_estimate_rub,0) ads_revenue_dedup_estimate_rub,
          IFNULL(a.ad_orders_dedup_estimate,0) ad_orders_dedup_estimate,
          IFNULL(o.orders_qty,0) orders_qty, IFNULL(o.orders_rub,0) orders_rub,
          IFNULL(o.canceled_qty,0) canceled_qty, IFNULL(o.canceled_rub,0) canceled_rub,
          IFNULL(sl.buyouts_qty,0) buyouts_qty, IFNULL(sl.buyouts_rub,0) buyouts_rub,
          IFNULL(sl.sales_for_pay_operational,0) sales_for_pay_operational,
          IFNULL(sl.returns_qty,0) returns_qty, IFNULL(sl.returns_rub,0) returns_rub,
          IFNULL(fn.commission_cost_positive,0) commission_cost_positive,
          IFNULL(fn.logistics_cost_positive,0) logistics_cost_positive,
          IFNULL(fp.finance_for_pay_accounting,0) finance_for_pay_accounting
        FROM spine s
        LEFT JOIN ads a  ON a.nm_id=s.nm_id  AND a.d=s.day
        LEFT JOIN ord o  ON o.nm_id=s.nm_id  AND o.d=s.day
        LEFT JOIN sal sl ON sl.nm_id=s.nm_id AND sl.d=s.day
        LEFT JOIN fin fn ON fn.nm_id=s.nm_id AND fn.d=s.day
        LEFT JOIN finpay fp ON fp.nm_id=s.nm_id AND fp.d=s.day),
      rolled AS (
        SELECT *,
          SUM(ad_spend) OVER w7 ad_spend_7d, SUM(ad_spend) OVER w14 ad_spend_14d,
          SUM(ads_revenue_raw_rub) OVER w7 ads_revenue_raw_7d, SUM(ads_revenue_raw_rub) OVER w14 ads_revenue_raw_14d,
          SUM(ads_revenue_dedup_estimate_rub) OVER w7 ads_revenue_dedup_estimate_7d,
          SUM(ads_revenue_dedup_estimate_rub) OVER w14 ads_revenue_dedup_estimate_14d,
          SUM(ad_orders_raw) OVER w7 ad_orders_raw_7d, SUM(ad_orders_raw) OVER w14 ad_orders_raw_14d,
          SUM(ad_orders_dedup_estimate) OVER w7 ad_orders_dedup_estimate_7d,
          SUM(ad_orders_dedup_estimate) OVER w14 ad_orders_dedup_estimate_14d,
          SUM(buyouts_rub) OVER w7 buyouts_rub_7d, SUM(buyouts_rub) OVER w14 buyouts_rub_14d,
          SUM(orders_rub) OVER w7 orders_rub_7d, SUM(orders_rub) OVER w14 orders_rub_14d,
          SUM(orders_qty) OVER w7 orders_qty_7d, SUM(orders_qty) OVER w14 orders_qty_14d
        FROM joined
        WINDOW
          w7  AS (PARTITION BY nm_id ORDER BY UNIX_DATE(day) RANGE BETWEEN 6  PRECEDING AND CURRENT ROW),
          w14 AS (PARTITION BY nm_id ORDER BY UNIX_DATE(day) RANGE BETWEEN 13 PRECEDING AND CURRENT ROW))
      SELECT
        day, nm_id,
        ad_spend, views, clicks, ad_orders_raw, ads_revenue_raw_rub, ads_revenue_dedup_estimate_rub, ad_orders_dedup_estimate,
        orders_qty, orders_rub, canceled_qty, canceled_rub,
        buyouts_qty, buyouts_rub, sales_for_pay_operational, returns_qty, returns_rub,
        commission_cost_positive, logistics_cost_positive, finance_for_pay_accounting,
        SAFE_DIVIDE(clicks, views)                       AS ctr,
        SAFE_DIVIDE(ad_spend, views) * 1000              AS cpm,
        SAFE_DIVIDE(ad_spend, clicks)                    AS cpc,
        SAFE_DIVIDE(ad_spend, ad_orders_raw)             AS cpo_attributed,
        SAFE_DIVIDE(ad_spend, orders_qty)                AS blended_cpo,
        SAFE_DIVIDE(ad_spend, orders_rub)                AS drr_orders,
        SAFE_DIVIDE(ad_spend, buyouts_rub)               AS drr_buyouts,
        SAFE_DIVIDE(ads_revenue_raw_rub, ad_spend)       AS roas,
        SAFE_DIVIDE(ad_spend, ads_revenue_raw_rub)       AS acos,
        buyouts_rub - commission_cost_positive - logistics_cost_positive - ad_spend
                                                         AS hybrid_day_contribution_pre_cogs,
        finance_for_pay_accounting - ad_spend            AS settlement_day_contribution_pre_cogs,
        ad_spend_7d, ad_spend_14d, ads_revenue_raw_7d, ads_revenue_raw_14d,
        ads_revenue_dedup_estimate_7d, ads_revenue_dedup_estimate_14d,
        ad_orders_raw_7d, ad_orders_raw_14d, ad_orders_dedup_estimate_7d, ad_orders_dedup_estimate_14d,
        buyouts_rub_7d, buyouts_rub_14d, orders_rub_7d, orders_rub_14d, orders_qty_7d, orders_qty_14d,
        SAFE_DIVIDE(ad_spend_7d,  buyouts_rub_7d)        AS drr_buyouts_7d,
        SAFE_DIVIDE(ad_spend_14d, buyouts_rub_14d)       AS drr_buyouts_14d,
        SAFE_DIVIDE(ads_revenue_raw_7d,  ad_spend_7d)    AS roas_7d,
        SAFE_DIVIDE(ads_revenue_raw_14d, ad_spend_14d)   AS roas_14d,
        SAFE_DIVIDE(ad_spend_7d,  orders_qty_7d)         AS blended_cpo_7d,
        SAFE_DIVIDE(ad_spend_14d, orders_qty_14d)        AS blended_cpo_14d,
        @build_as_of AS build_as_of_date,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM rolled
    """ USING v_build_as_of AS build_as_of, v_global_start AS global_start, v_run_id AS run_id, v_built_at AS built_at;

    -- --- ASSERT-гейт на __BUILD ---
    -- fix #2: непустой BUILD.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY__BUILD`) > 0
      AS 'SKU_DAILY: пустой BUILD (fail-closed)';
    -- грейн (day, nm_id) уникален + not-null.
    ASSERT (SELECT COUNTIF(day IS NULL OR nm_id IS NULL) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: NULL day/nm_id';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
      AS 'SKU_DAILY: грейн (day,nm_id) не уникален';
    -- fix #2: полнота universe — КАЖДЫЙ активный SKU обязан присутствовать в BUILD (ловит отсутствующий nm).
    ASSERT (SELECT COUNT(*) FROM (
              SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
              WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL
              EXCEPT DISTINCT
              SELECT nm_id FROM `wb_mart.MART_SKU_DAILY__BUILD`)) = 0
      AS 'SKU_DAILY: активный SKU отсутствует в BUILD (universe неполон)';
    -- плотность spine: у каждого nm число дней == (MAX-MIN+1), пропусков нет.
    ASSERT (SELECT COUNTIF(cnt <> span) FROM (
              SELECT nm_id, COUNT(*) cnt, DATE_DIFF(MAX(day), MIN(day), DAY) + 1 span
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY nm_id)) = 0
      AS 'SKU_DAILY: разрывы в dense-spine';
    -- граница: MAX(day) == build_as_of.
    ASSERT (SELECT MAX(day) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = v_build_as_of
      AS 'SKU_DAILY: MAX(day) != build_as_of_date';

    -- fix #3: reconciliation ограничен <= build_as_of (совпадает с bounding source-CTE).
    ASSERT (SELECT ABS((SELECT SUM(ad_spend) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                     - (SELECT SUM(stats_spend_rub) FROM `wb_mart.FACT_ADS_SKU_DAILY`
                        WHERE `date` <= v_build_as_of
                          AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: ad_spend != FACT (universe, <=build_as_of)';
    ASSERT (SELECT (SELECT SUM(orders_qty) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                 = (SELECT COUNTIF(NOT is_cancel) FROM `wb_mart.FACT_ORDERS`
                    WHERE order_date <= v_build_as_of
                      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL)))
      AS 'SKU_DAILY: orders_qty != FACT (universe, <=build_as_of)';
    ASSERT (SELECT ABS((SELECT SUM(buyouts_rub) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                     - (SELECT SUM(IF(NOT is_return, price_with_disc, 0)) FROM `wb_mart.FACT_SALES`
                        WHERE sale_date <= v_build_as_of
                          AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: buyouts_rub != FACT (universe, <=build_as_of)';
    ASSERT (SELECT ABS(
              (SELECT SUM(commission_cost_positive + logistics_cost_positive) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
            - (SELECT SUM(cost_amount_positive) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
               WHERE is_sku_row AND cost_category IN ('commission','logistics') AND finance_date <= v_build_as_of
                 AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                               WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: finance commission+logistics != LONG_MAPPED (universe SKU, <=build_as_of)';

    -- --- publish ---
    CREATE OR REPLACE TABLE `wb_mart.MART_SKU_DAILY`
      PARTITION BY day CLUSTER BY nm_id AS
      SELECT * FROM `wb_mart.MART_SKU_DAILY__BUILD`;

    ASSERT (SELECT COUNTIF(is_partitioning_column='YES') FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name='MART_SKU_DAILY') = 1 AS 'SKU_DAILY: partition != 1';
    ASSERT (SELECT COUNTIF(clustering_ordinal_position IS NOT NULL) FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name='MART_SKU_DAILY') = 1 AS 'SKU_DAILY: cluster != 1';

    DROP TABLE IF EXISTS `wb_mart.MART_SKU_DAILY__BUILD`;

    UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
       SET is_running=FALSE, run_id=NULL, last_run_id=v_run_id, released_at=CURRENT_TIMESTAMP()
     WHERE lock_id='mart_sku_daily';

  EXCEPTION WHEN ERROR THEN
    UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
       SET is_running=FALSE, run_id=NULL, last_run_id=v_run_id, released_at=CURRENT_TIMESTAMP()
     WHERE lock_id='mart_sku_daily';
    RAISE USING MESSAGE = FORMAT('sp_build_mart_sku_daily FAILED: %s', @@error.message);
  END;
END;

-- Ручной прогон (владелец, после APPROVE + merge):
--   CALL `wb_mart.sp_build_mart_sku_daily`(CURRENT_DATE('Europe/Moscow'), NULL);
