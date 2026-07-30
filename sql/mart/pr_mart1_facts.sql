-- ============================================================================
-- PR-Mart1 — EVETIS WB Analytics MART, слой FACT (6 таблиц). REV3 (аудит) + PR-Mart1.1 (реклама).
-- Дата: 2026-07-28.  Дизайн: docs/MART_DESIGN_2026-07-23_rev2.md; контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md.
-- История правок аудита — docs/MART_PR1_2026-07-28.md; PR-Mart1.1 — docs/MART_PR11_2026-07-28.md.
-- PR-Mart1.1: FACT_ADS_SKU_DAILY получил ads_revenue_raw_rub/_dedup_estimate_rub, ad_orders_raw (переим. orders),
--   ad_orders_dedup_estimate, multitouch_ambiguous_flag, zero_revenue_multiorder_flag + sum_price parse-QC + dynamic acceptance.
--   ⚠️ Требует ПЕРЕ-bootstrap (CALL sp_bootstrap_facts('')) — FACT_ADS_SKU_DAILY пересоберётся с новыми колонками.
--
-- ⚠️ BOOTSTRAP / MANUAL-ONLY. Процедура `sp_bootstrap_facts` — РУЧНОЙ первичный прогон.
--   Публикация выполняется как ПОСЛЕДОВАТЕЛЬНЫЙ publish каждой FACT (6 отдельных
--   CREATE OR REPLACE) — это НЕ атомарно по всему набору: сбой между шагами может
--   оставить смешанный run. Поэтому:
--     • конкурентный запуск ЗАПРЕЩЁН (advisory-lock `_MART_BOOTSTRAP_LOCK`);
--     • ПОТРЕБИТЕЛЕЙ (дашборд/витрины) НЕ подключать к этим FACT до PR-Mart3;
--     • полный staging-набор с единой публикацией + run-log + freshness-gate — в PR-Mart3
--       (Cloud Run Job runWbMartDaily с execution-guard, как у загрузчиков).
--
-- Движок (решение владельца 28.07): engine-agnostic BigQuery (процедура). Оркестратором
--   позже станет Cloud Run Job (`node dist/cli.js mart` → CALL). Apps Script не пишем.
--
-- Паттерн: BUILD → ASSERT(parse-QC + not-null/not-empty grain + дедуп) → publish (с
--   повтором PARTITION/CLUSTER) → ASSERT физики через INFORMATION_SCHEMA.
--
-- Безопасность: скриптовые переменные v_run_id / v_built_at передаются в динамический
--   SQL ТОЛЬКО как query-параметры (EXECUTE IMMEDIATE ... USING v_run_id AS run_id,
--   v_built_at AS built_at) — без строковой интерполяции и без shadowing колонок.
--
-- Проверено read-only на wb_raw 2026-07-28 (см. docs/MART_PR1_2026-07-28.md). Негативные
--   тесты каждого класса каста — sql/mart/pr_mart1_negative_tests.sql.
-- ⚠️ Объекты в BQ создаются ТОЛЬКО после финального APPROVE + запуска владельцем.
-- ============================================================================

-- ── 0. Датасет + lock-таблица (advisory mutex для manual-bootstrap) ───────────
CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

CREATE TABLE IF NOT EXISTS `wb_mart._MART_BOOTSTRAP_LOCK` (
  lock_id     STRING NOT NULL,
  is_running  BOOL,        -- TRUE, пока идёт прогон
  run_id      STRING,      -- run_id ТЕКУЩЕГО держателя lock (NULL, когда свободно)
  last_run_id STRING,      -- последний завершившийся run_id (успех или ошибка) — для аудита
  acquired_at TIMESTAMP,
  released_at TIMESTAMP
);
-- идемпотентность setup: если lock-таблица осталась от более старой версии без last_run_id —
-- CREATE TABLE IF NOT EXISTS колонку не добавит, поэтому явно доводим схему.
ALTER TABLE `wb_mart._MART_BOOTSTRAP_LOCK` ADD COLUMN IF NOT EXISTS last_run_id STRING;

MERGE `wb_mart._MART_BOOTSTRAP_LOCK` T
USING (SELECT 'facts' AS lock_id) S ON T.lock_id = S.lock_id
WHEN NOT MATCHED THEN
  INSERT (lock_id, is_running, run_id, last_run_id, acquired_at, released_at)
  VALUES ('facts', FALSE, NULL, NULL, NULL, NULL);

-- ── 1. Процедура bootstrap FACT-слоя ─────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `wb_mart.sp_bootstrap_facts`(IN in_run_id STRING)
BEGIN
  DECLARE v_run_id   STRING    DEFAULT IFNULL(NULLIF(in_run_id, ''), GENERATE_UUID());
  DECLARE v_built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP();

  -- --- concurrency guard: занять lock одним UPDATE ... WHERE is_running=FALSE ---
  UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'facts' AND is_running = FALSE;
  IF @@row_count = 0 THEN
    RAISE USING MESSAGE =
      'sp_bootstrap_facts: lock занят (manual-only, конкурентный запуск запрещён). '
      || 'Если ран завис — вручную снять _MART_BOOTSTRAP_LOCK (is_running=FALSE, run_id=NULL).';
  END IF;

  BEGIN  -- защищённый блок: любой сбой → освободить lock и переброс ошибки

    -- ======================================================================
    -- 1.1 FACT_ORDERS — грейн: order_srid (=srid). Дата: order_date.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_ORDERS__BUILD`
      PARTITION BY order_date
      CLUSTER BY nm_id, internal_sku AS
      SELECT
        srid                                                    AS order_srid,
        _order_date                                             AS order_date,
        SAFE_CAST(wb_nm_id AS INT64)                            AS nm_id,
        internal_sku, sku_match_status,
        SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) AS price_with_disc,
        SAFE_CAST(quantity AS INT64)                            AS quantity,
        LOWER(IFNULL(is_cancel, '')) = 'true'                   AS is_cancel,
        cancel_dt,
        warehouse_name, region_name, country_name,
        category, subject, brand, tech_size,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_raw.V_WB_ORDERS`
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    -- parse-QC (источник непуст, но cast → NULL): price, quantity, nm_id.
    ASSERT (SELECT COUNTIF(price_with_disc IS NOT NULL AND TRIM(price_with_disc) <> ''
              AND SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) IS NULL)
            FROM `wb_raw.V_WB_ORDERS`) = 0 AS 'FACT_ORDERS: price_with_disc parse-QC != 0';
    ASSERT (SELECT COUNTIF(quantity IS NOT NULL AND TRIM(quantity) <> ''
              AND SAFE_CAST(quantity AS INT64) IS NULL)
            FROM `wb_raw.V_WB_ORDERS`) = 0 AS 'FACT_ORDERS: quantity parse-QC != 0';
    ASSERT (SELECT COUNTIF(wb_nm_id IS NOT NULL AND TRIM(wb_nm_id) <> ''
              AND SAFE_CAST(wb_nm_id AS INT64) IS NULL)
            FROM `wb_raw.V_WB_ORDERS`) = 0 AS 'FACT_ORDERS: nm_id cast != 0';
    -- not-null/not-empty grain (string) + not-null partition; дедуп.
    ASSERT (SELECT COUNTIF(order_srid IS NULL OR TRIM(order_srid) = '') + COUNTIF(order_date IS NULL)
            FROM `wb_mart.FACT_ORDERS__BUILD`) = 0 AS 'FACT_ORDERS: NULL/empty grain or NULL partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT order_srid) FROM `wb_mart.FACT_ORDERS__BUILD`)
            AS 'FACT_ORDERS: order_srid not unique';
    -- fail-closed: непустой BUILD + соответствие объёма источнику (pass-through: BUILD rows = source rows).
    -- (bootstrap read источника в рантайме; редкий дозаписанный в источник ряд во время прогона → повторить запуск.)
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ORDERS__BUILD`) > 0 AS 'FACT_ORDERS: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ORDERS__BUILD`)
         = (SELECT COUNT(*) FROM `wb_raw.V_WB_ORDERS`) AS 'FACT_ORDERS: BUILD rows != source rows';

    -- ======================================================================
    -- 1.2 FACT_SALES — грейн: sale_id. Деньги уже NUMERIC. for_pay→sales_for_pay_operational.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_SALES__BUILD`
      PARTITION BY sale_date
      CLUSTER BY nm_id, is_return AS
      SELECT
        sale_id,
        _sale_date                                             AS sale_date,
        wb_nm_id                                               AS nm_id,
        internal_sku, sku_match_status,
        is_return, is_realization, operation_type,
        total_price, price_with_disc, finished_price, spp, discount_percent,
        for_pay                                                AS sales_for_pay_operational,
        warehouse_name, warehouse_type, region_name, country_name,
        category, subject, brand, tech_size,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_raw.V_WB_SALES_RETURNS`
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (SELECT COUNTIF(sale_id IS NULL OR TRIM(sale_id) = '') + COUNTIF(sale_date IS NULL)
            FROM `wb_mart.FACT_SALES__BUILD`) = 0 AS 'FACT_SALES: NULL/empty grain or NULL partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT sale_id) FROM `wb_mart.FACT_SALES__BUILD`)
            AS 'FACT_SALES: sale_id not unique';
    -- fail-closed: непустой BUILD + pass-through объём.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_SALES__BUILD`) > 0 AS 'FACT_SALES: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_SALES__BUILD`)
         = (SELECT COUNT(*) FROM `wb_raw.V_WB_SALES_RETURNS`) AS 'FACT_SALES: BUILD rows != source rows';

    -- ======================================================================
    -- 1.3 FACT_STOCKS_SNAPSHOT — грейн: snapshot_date × nm_id × warehouse_id.
    --      Последний COMPLETE-снапшот дня (правка 7), затем сумма складов.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`
      PARTITION BY snapshot_date
      CLUSTER BY nm_id, warehouse_id AS
      WITH pick AS (
        SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
        FROM `wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE status = 'COMPLETE' AND period_to IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
      )
      SELECT
        p.snapshot_date, r.nm_id, r.warehouse_id,
        ANY_VALUE(r.warehouse_name)   AS warehouse_name,
        ANY_VALUE(r.region_name)      AS region_name,
        SUM(r.quantity)               AS quantity,
        SUM(r.in_way_to_client)       AS in_way_to_client,
        SUM(r.in_way_from_client)     AS in_way_from_client,
        ANY_VALUE(r.internal_sku)     AS internal_sku,
        ANY_VALUE(r.sku_match_status) AS sku_match_status,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM pick p
      JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id)
      GROUP BY p.snapshot_date, r.nm_id, r.warehouse_id
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (SELECT COUNTIF(snapshot_date IS NULL) + COUNTIF(nm_id IS NULL) + COUNTIF(warehouse_id IS NULL)
            FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) = 0 AS 'FACT_STOCKS_SNAPSHOT: NULL grain/partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t|%t', snapshot_date, nm_id, warehouse_id))
            FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`)
            AS 'FACT_STOCKS_SNAPSHOT: (snapshot_date,nm_id,warehouse_id) not unique';
    -- fail-closed: непустой BUILD + соответствие грейну ВЫБРАННЫХ (последних COMPLETE) снапшотов.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) > 0 AS 'FACT_STOCKS_SNAPSHOT: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) = (
              SELECT COUNT(DISTINCT FORMAT('%t|%t|%t', p.snapshot_date, r.nm_id, r.warehouse_id))
              FROM (SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
                    FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1) p
              JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id))
            AS 'FACT_STOCKS_SNAPSHOT: BUILD rows != source distinct grain (selected snapshots)';

    -- ======================================================================
    -- 1.4 FACT_FINANCE — грейн: finance_row_key = report_id#rrd_id. Только canonical.
    --      Целостность ключа проверяем ПО ИСТОЧНИКУ (обе части NOT NULL/empty),
    --      затем строим CONCAT БЕЗ IFNULL (иначе маскирует отсутствие части).
    -- ======================================================================
    ASSERT (SELECT COUNTIF(report_id IS NULL OR TRIM(report_id) = ''
                        OR rrd_id    IS NULL OR TRIM(rrd_id)    = '')
            FROM `wb_raw.V_WB_FINANCE_CANONICAL`) = 0
            AS 'FACT_FINANCE: report_id/rrd_id NULL or empty (key integrity)';

    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_FINANCE__BUILD`
      PARTITION BY finance_date
      CLUSTER BY nm_id, finance_status, operation_type_normalized AS
      SELECT
        CONCAT(report_id, '#', rrd_id)                         AS finance_row_key,
        report_id, rrd_id,
        _rr_date                                               AS finance_date,
        SAFE_CAST(wb_nm_id AS INT64)                           AS nm_id,
        internal_sku, sku_match_status,
        source_layer, finance_status, report_type, week_start,
        supplier_oper_name, operation_type_normalized, doc_type_name,
        SAFE_CAST(REPLACE(sale_amount,        ',', '.') AS NUMERIC) AS sale_amount,
        SAFE_CAST(REPLACE(return_amount_rub,  ',', '.') AS NUMERIC) AS return_amount_rub,
        SAFE_CAST(REPLACE(for_pay,            ',', '.') AS NUMERIC) AS finance_for_pay_accounting,
        SAFE_CAST(REPLACE(commission_amount,  ',', '.') AS NUMERIC) AS commission_amount,
        SAFE_CAST(REPLACE(logistics_amount,   ',', '.') AS NUMERIC) AS logistics_amount,
        SAFE_CAST(REPLACE(storage_fee,        ',', '.') AS NUMERIC) AS storage_fee,
        SAFE_CAST(REPLACE(deduction,          ',', '.') AS NUMERIC) AS deduction,
        SAFE_CAST(REPLACE(penalty,            ',', '.') AS NUMERIC) AS penalty,
        SAFE_CAST(REPLACE(acceptance,         ',', '.') AS NUMERIC) AS acceptance,
        SAFE_CAST(REPLACE(acquiring_fee,      ',', '.') AS NUMERIC) AS acquiring_fee,
        SAFE_CAST(REPLACE(additional_payment, ',', '.') AS NUMERIC) AS additional_payment,
        SAFE_CAST(REPLACE(compensation_amount,',', '.') AS NUMERIC) AS compensation_amount,
        SAFE_CAST(REPLACE(other_amount,       ',', '.') AS NUMERIC) AS other_amount,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_raw.V_WB_FINANCE_CANONICAL`
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (
      SELECT
        COUNTIF(sale_amount        IS NOT NULL AND TRIM(sale_amount)        <> '' AND SAFE_CAST(REPLACE(sale_amount,        ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(return_amount_rub  IS NOT NULL AND TRIM(return_amount_rub)  <> '' AND SAFE_CAST(REPLACE(return_amount_rub,  ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(for_pay            IS NOT NULL AND TRIM(for_pay)            <> '' AND SAFE_CAST(REPLACE(for_pay,            ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(commission_amount  IS NOT NULL AND TRIM(commission_amount)  <> '' AND SAFE_CAST(REPLACE(commission_amount,  ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(logistics_amount   IS NOT NULL AND TRIM(logistics_amount)   <> '' AND SAFE_CAST(REPLACE(logistics_amount,   ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(storage_fee        IS NOT NULL AND TRIM(storage_fee)        <> '' AND SAFE_CAST(REPLACE(storage_fee,        ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(deduction          IS NOT NULL AND TRIM(deduction)          <> '' AND SAFE_CAST(REPLACE(deduction,          ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(penalty            IS NOT NULL AND TRIM(penalty)            <> '' AND SAFE_CAST(REPLACE(penalty,            ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(acceptance         IS NOT NULL AND TRIM(acceptance)         <> '' AND SAFE_CAST(REPLACE(acceptance,         ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(acquiring_fee      IS NOT NULL AND TRIM(acquiring_fee)      <> '' AND SAFE_CAST(REPLACE(acquiring_fee,      ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(additional_payment IS NOT NULL AND TRIM(additional_payment) <> '' AND SAFE_CAST(REPLACE(additional_payment, ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(compensation_amount IS NOT NULL AND TRIM(compensation_amount)<> '' AND SAFE_CAST(REPLACE(compensation_amount,',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(other_amount       IS NOT NULL AND TRIM(other_amount)       <> '' AND SAFE_CAST(REPLACE(other_amount,       ',', '.') AS NUMERIC) IS NULL)
      FROM `wb_raw.V_WB_FINANCE_CANONICAL`) = 0 AS 'FACT_FINANCE: money parse-QC != 0';
    ASSERT (SELECT COUNTIF(wb_nm_id IS NOT NULL AND TRIM(wb_nm_id) <> ''
              AND SAFE_CAST(wb_nm_id AS INT64) IS NULL)
            FROM `wb_raw.V_WB_FINANCE_CANONICAL`) = 0 AS 'FACT_FINANCE: nm_id cast != 0';
    ASSERT (SELECT COUNTIF(finance_row_key IS NULL OR TRIM(finance_row_key) = '') + COUNTIF(finance_date IS NULL)
            FROM `wb_mart.FACT_FINANCE__BUILD`) = 0 AS 'FACT_FINANCE: NULL/empty grain or NULL partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT finance_row_key) FROM `wb_mart.FACT_FINANCE__BUILD`)
            AS 'FACT_FINANCE: finance_row_key not unique';
    -- fail-closed: непустой BUILD + pass-through объём.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE__BUILD`) > 0 AS 'FACT_FINANCE: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE__BUILD`)
         = (SELECT COUNT(*) FROM `wb_raw.V_WB_FINANCE_CANONICAL`) AS 'FACT_FINANCE: BUILD rows != source rows';

    -- ======================================================================
    -- 1.5 FACT_ADS_SKU_DAILY — грейн: date × advert_id × nm_id (SUM по appType).
    --      date как КАЛЕНДАРНАЯ дата: PARSE_DATE от SUBSTR (без TZ-каста).
    --      PR-Mart1.1: +ads_revenue_raw_rub / _dedup_estimate_rub, ad_orders_raw (переим. из orders),
    --      ad_orders_dedup_estimate, multitouch_ambiguous_flag, zero_revenue_multiorder_flag
    --      (контракт MART_MART2_CONTRACTS §1). raw = source-faithful; dedup = ОЦЕНКА (не выручка), ВНЕ консервации.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SKU_DAILY__BUILD`
      PARTITION BY `date`
      CLUSTER BY nm_id, advert_id AS
      WITH parsed AS (
        SELECT
          SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(`date`, 1, 10)) AS d,
          SAFE_CAST(advertId AS INT64) AS advert_id,
          SAFE_CAST(nmId AS INT64)     AS nm_id,
          SAFE_CAST(views AS INT64)    AS views,
          SAFE_CAST(clicks AS INT64)   AS clicks,
          SAFE_CAST(orders AS INT64)   AS orders,
          SAFE_CAST(REPLACE(`sum`, ',', '.') AS NUMERIC)     AS spend,
          SAFE_CAST(REPLACE(sum_price, ',', '.') AS NUMERIC) AS revenue
        FROM `wb_raw.V_ADV_CAMPAIGN_STATS`
      ),
      dedup AS (  -- на группе (d,advert,nm): Σ различных ненулевых revenue; Σ MAX(orders) на различное значение
        SELECT d, advert_id, nm_id,
               SUM(rev_val) AS revenue_dedup,
               SUM(max_orders_for_val) AS orders_dedup_nz
        FROM (
          SELECT d, advert_id, nm_id, revenue AS rev_val, MAX(orders) AS max_orders_for_val
          FROM parsed WHERE revenue IS NOT NULL AND revenue <> 0
          GROUP BY d, advert_id, nm_id, revenue
        ) GROUP BY d, advert_id, nm_id
      )
      SELECT
        p.d AS `date`, p.advert_id, p.nm_id,
        SUM(p.views)  AS views,
        SUM(p.clicks) AS clicks,
        SUM(p.spend)  AS stats_spend_rub,
        SUM(p.orders) AS ad_orders_raw,
        SUM(IFNULL(p.revenue, 0)) AS ads_revenue_raw_rub,
        IFNULL(ANY_VALUE(dd.revenue_dedup), 0) AS ads_revenue_dedup_estimate_rub,
        IFNULL(ANY_VALUE(dd.orders_dedup_nz), 0) + SUM(IF(IFNULL(p.revenue, 0) = 0, p.orders, 0)) AS ad_orders_dedup_estimate,
        (COUNTIF(p.revenue IS NOT NULL AND p.revenue <> 0) > COUNT(DISTINCT IF(p.revenue <> 0, p.revenue, NULL))) AS multitouch_ambiguous_flag,
        (COUNTIF(IFNULL(p.revenue, 0) = 0 AND p.orders > 0) >= 2) AS zero_revenue_multiorder_flag,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM parsed p LEFT JOIN dedup dd USING (d, advert_id, nm_id)
      GROUP BY p.d, p.advert_id, p.nm_id
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    -- parse-QC: date, advertId, nmId, views, clicks, orders, sum, sum_price (Mart1.1).
    ASSERT (
      SELECT
        COUNTIF(`date` IS NOT NULL AND TRIM(`date`) <> '' AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(`date`,1,10)) IS NULL)
      + COUNTIF(advertId IS NOT NULL AND TRIM(advertId) <> '' AND SAFE_CAST(advertId AS INT64) IS NULL)
      + COUNTIF(nmId     IS NOT NULL AND TRIM(nmId)     <> '' AND SAFE_CAST(nmId     AS INT64) IS NULL)
      + COUNTIF(views    IS NOT NULL AND TRIM(views)    <> '' AND SAFE_CAST(views    AS INT64) IS NULL)
      + COUNTIF(clicks   IS NOT NULL AND TRIM(clicks)   <> '' AND SAFE_CAST(clicks   AS INT64) IS NULL)
      + COUNTIF(orders   IS NOT NULL AND TRIM(orders)   <> '' AND SAFE_CAST(orders   AS INT64) IS NULL)
      + COUNTIF(`sum`    IS NOT NULL AND TRIM(`sum`)    <> '' AND SAFE_CAST(REPLACE(`sum`, ',', '.') AS NUMERIC) IS NULL)
      + COUNTIF(sum_price IS NOT NULL AND TRIM(sum_price) <> '' AND SAFE_CAST(REPLACE(sum_price, ',', '.') AS NUMERIC) IS NULL)
      FROM `wb_raw.V_ADV_CAMPAIGN_STATS`) = 0 AS 'FACT_ADS_SKU_DAILY: parse-QC != 0';
    ASSERT (SELECT COUNTIF(`date` IS NULL) + COUNTIF(advert_id IS NULL) + COUNTIF(nm_id IS NULL)
            FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`) = 0 AS 'FACT_ADS_SKU_DAILY: NULL grain/partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t|%t', `date`, advert_id, nm_id))
            FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`)
            AS 'FACT_ADS_SKU_DAILY: (date,advert_id,nm_id) not unique';
    -- fail-closed: непустой BUILD + соответствие грейну источника (SUM по appType не теряет комбинаций).
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`) > 0 AS 'FACT_ADS_SKU_DAILY: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`) = (
              SELECT COUNT(DISTINCT FORMAT('%t|%t|%t',
                     SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(`date`,1,10)), SAFE_CAST(advertId AS INT64), SAFE_CAST(nmId AS INT64)))
              FROM `wb_raw.V_ADV_CAMPAIGN_STATS`)
            AS 'FACT_ADS_SKU_DAILY: BUILD rows != source distinct grain';
    -- Mart1.1 ДИНАМИЧЕСКИЙ acceptance: raw ad-revenue FACT == Σ source sum_price (не фикс-сумма).
    ASSERT (SELECT ROUND(SUM(ads_revenue_raw_rub),2) FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`)
         = (SELECT ROUND(SUM(SAFE_CAST(REPLACE(sum_price, ',', '.') AS NUMERIC)),2) FROM `wb_raw.V_ADV_CAMPAIGN_STATS`)
            AS 'FACT_ADS_SKU_DAILY: ads_revenue_raw_rub != Σ source sum_price';
    -- Mart1.1 fail-closed границы estimate: 0 ≤ dedup ≤ raw (оценка не может превышать source-faithful или быть отрицательной).
    ASSERT (SELECT COUNTIF(
              ads_revenue_dedup_estimate_rub < 0 OR ads_revenue_dedup_estimate_rub > ads_revenue_raw_rub
              OR ad_orders_dedup_estimate < 0 OR ad_orders_dedup_estimate > ad_orders_raw)
            FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SKU_DAILY: dedup estimate outside raw bounds';

    -- ======================================================================
    -- 1.6 FACT_ADS_COSTS_DAILY — грейн: date × advert_id (агрегируем updNum-строки).
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`
      PARTITION BY `date`
      CLUSTER BY advert_id AS
      SELECT
        SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate, 1, 10)) AS `date`,
        SAFE_CAST(advertId AS INT64)                        AS advert_id,
        SUM(SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC)) AS actual_spend_rub,
        COUNT(*)                                            AS cost_rows,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_raw.V_ADV_COSTS`
      GROUP BY 1, 2
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (
      SELECT
        COUNTIF(updDate  IS NOT NULL AND TRIM(updDate)  <> '' AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) IS NULL)
      + COUNTIF(advertId IS NOT NULL AND TRIM(advertId) <> '' AND SAFE_CAST(advertId AS INT64) IS NULL)
      + COUNTIF(updSum   IS NOT NULL AND TRIM(updSum)   <> '' AND SAFE_CAST(REPLACE(updSum, ',', '.') AS NUMERIC) IS NULL)
      FROM `wb_raw.V_ADV_COSTS`) = 0 AS 'FACT_ADS_COSTS_DAILY: parse-QC != 0';
    ASSERT (SELECT COUNTIF(`date` IS NULL) + COUNTIF(advert_id IS NULL)
            FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`) = 0 AS 'FACT_ADS_COSTS_DAILY: NULL grain/partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', `date`, advert_id))
            FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`)
            AS 'FACT_ADS_COSTS_DAILY: (date,advert_id) not unique';
    -- fail-closed: непустой BUILD + соответствие грейну источника.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`) > 0 AS 'FACT_ADS_COSTS_DAILY: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`) = (
              SELECT COUNT(DISTINCT FORMAT('%t|%t',
                     SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)), SAFE_CAST(advertId AS INT64)))
              FROM `wb_raw.V_ADV_COSTS`)
            AS 'FACT_ADS_COSTS_DAILY: BUILD rows != source distinct grain';

    -- ======================================================================
    -- 2. PUBLISH — все ASSERT пройдены. ПОСЛЕДОВАТЕЛЬНЫЙ publish каждой FACT
    --    (6 отдельных CREATE OR REPLACE; НЕ атомарно по всему набору — см. шапку).
    --    ПОВТОРЯЕМ PARTITION/CLUSTER (CTAS `SELECT *` их НЕ наследует!).
    -- ======================================================================
    CREATE OR REPLACE TABLE `wb_mart.FACT_ORDERS`
      PARTITION BY order_date CLUSTER BY nm_id, internal_sku AS
      SELECT * FROM `wb_mart.FACT_ORDERS__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_SALES`
      PARTITION BY sale_date CLUSTER BY nm_id, is_return AS
      SELECT * FROM `wb_mart.FACT_SALES__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_STOCKS_SNAPSHOT`
      PARTITION BY snapshot_date CLUSTER BY nm_id, warehouse_id AS
      SELECT * FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_FINANCE`
      PARTITION BY finance_date CLUSTER BY nm_id, finance_status, operation_type_normalized AS
      SELECT * FROM `wb_mart.FACT_FINANCE__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SKU_DAILY`
      PARTITION BY `date` CLUSTER BY nm_id, advert_id AS
      SELECT * FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_COSTS_DAILY`
      PARTITION BY `date` CLUSTER BY advert_id AS
      SELECT * FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`;

    -- ======================================================================
    -- 3. ВАЛИДАЦИЯ ФИЗИКИ финальных FACT через INFORMATION_SCHEMA
    --    (partition-колонка = 1; число cluster-колонок как задумано).
    -- ======================================================================
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=2
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_ORDERS')
            AS 'FACT_ORDERS: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=2
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_SALES')
            AS 'FACT_SALES: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=2
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_STOCKS_SNAPSHOT')
            AS 'FACT_STOCKS_SNAPSHOT: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=3
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_FINANCE')
            AS 'FACT_FINANCE: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=2
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_ADS_SKU_DAILY')
            AS 'FACT_ADS_SKU_DAILY: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=1
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_ADS_COSTS_DAILY')
            AS 'FACT_ADS_COSTS_DAILY: partition/cluster physics lost';

  EXCEPTION WHEN ERROR THEN
    -- освободить lock (run_id=NULL, зафиксировать last_run_id) и пробросить ошибку
    UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
       SET is_running = FALSE, run_id = NULL, last_run_id = v_run_id, released_at = CURRENT_TIMESTAMP()
     WHERE lock_id = 'facts';
    RAISE USING MESSAGE = @@error.message;
  END;

  -- освободить lock при успехе (run_id=NULL, last_run_id=v_run_id)
  UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
     SET is_running = FALSE, run_id = NULL, last_run_id = v_run_id, released_at = CURRENT_TIMESTAMP()
   WHERE lock_id = 'facts';

  SELECT v_run_id AS mart_run_id, v_built_at AS built_at, 'FACTS_BOOTSTRAPPED' AS status;
END;

-- Запуск (ТОЛЬКО после финального APPROVE, владельцем, БЕЗ параллельных запусков):
--   CALL `wb_mart.sp_bootstrap_facts`('');   -- '' → авто-UUID run_id
-- Потребителей (витрины/дашборд) НЕ подключать до PR-Mart3.
