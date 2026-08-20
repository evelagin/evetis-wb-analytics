-- ============================================================================
-- PR-Mart1 — EVETIS WB Analytics MART, слой FACT (8 таблиц). REV3 (аудит) + PR-Mart1.1 (реклама).
-- Дата: 2026-07-28.  Дизайн: docs/MART_DESIGN_2026-07-23_rev2.md; контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md.
-- История правок аудита — docs/MART_PR1_2026-07-28.md; PR-Mart1.1 — docs/MART_PR11_2026-07-28.md.
-- PR-Mart1.1: FACT_ADS_SKU_DAILY получил ads_revenue_raw_rub/_dedup_estimate_rub, ad_orders_raw (переим. orders),
--   ad_orders_dedup_estimate, multitouch_ambiguous_flag, zero_revenue_multiorder_flag + sum_price parse-QC + dynamic acceptance.
--   ⚠️ Требует ПЕРЕ-bootstrap (CALL sp_bootstrap_facts('')) — FACT_ADS_SKU_DAILY пересоберётся с новыми колонками.
--
-- 2026-08-20 (Stage 3B, ads spend semantics): +1.7 FACT_ADS_SPEND_ALLOC_DAILY и
--   +1.8 FACT_ADS_SPEND_UNALLOC_DAILY. Биллинг (FACT_ADS_COSTS_DAILY, грейн date x advert_id)
--   распределяется на SKU пропорционально stats_spend_rub той же пары; нераспределимый остаток
--   выносится ОТДЕЛЬНОЙ таблицей, а не обнуляется и не размазывается. Существующие шесть FACT
--   не изменены ни на байт — миграция строго аддитивная.
--   Инвариант: SUM(billed_alloc_rub) + SUM(unallocated_rub) = SUM(actual_spend_rub), остаток по
--   каждой паре (date, advert_id) ровно 0. Требует ПЕРЕ-bootstrap: CALL sp_bootstrap_facts('').
--
-- 2026-08-17 (fix/mart-stocks-warehouse-key): грейн FACT_STOCKS_SNAPSHOT переведён с
--   `warehouse_id INT64` на `warehouse_key STRING` — WB обезличил склад отгрузки, лоадер
--   с 16.08 пишет warehouse_id = NULL. Подробности и доказательства — в блоке 1.3.
--   Отдельный ПЕРЕ-bootstrap НЕ нужен: FACT-таблицы собираются CREATE OR REPLACE, штатный
--   прогон wb-mart-prod пересоздаст FACT_STOCKS_SNAPSHOT с новой схемой.
--
-- ⚠️ BOOTSTRAP / MANUAL-ONLY. Процедура `sp_bootstrap_facts` — РУЧНОЙ первичный прогон.
--   Публикация выполняется как ПОСЛЕДОВАТЕЛЬНЫЙ publish каждой FACT (8 отдельных
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
    -- 1.3 FACT_STOCKS_SNAPSHOT — грейн: snapshot_date × nm_id × warehouse_key.
    --      Последний COMPLETE-снапшот дня (правка 7), затем сумма складов.
    --
    -- 🔴 warehouse_key (STRING) — канонический ключ склада, введён 17.08.2026.
    --    До 16.08 грейном был `warehouse_id INT64`. 16.08 WB обезличил склад
    --    отгрузки: T6 отдаёт warehouseId = -999999 — это СЕНТИНЕЛ «склад не
    --    раскрываем», а не идентификатор. Лоадер (WbStocksSnapshot.gs, правка
    --    16.08) сознательно кладёт сырое значение в `warehouse_code STRING`, а
    --    `warehouse_id` оставляет NULL — чтобы сентинел не притворялся id склада.
    --    Следствие: 16 и 17.08 дали 46 строк с NULL в старом грейне, и ASSERT
    --    ниже завалил витрину два прогона подряд (см. CHANGELOG 2026-08-17).
    --
    --    warehouse_key = COALESCE(NULLIF(warehouse_code,''), CAST(warehouse_id AS STRING))
    --    покрывает ОБЕ эпохи: ≤15.08 — числовой id при пустом code, ≥16.08 —
    --    наоборот. Проверено read-only на всей истории (30 суток): 5 637 строк,
    --    0 NULL, 5 637 distinct — ключ уникален и не-NULL на обеих эпохах.
    --
    --    `warehouse_id` ОСТАЁТСЯ в таблице как справочная колонка (NULL с 16.08):
    --    история до 15.08 включительно на нём построена. Потребителям склада
    --    брать `warehouse_key`; тип STRING выбран потому, что WB уже прислал
    --    непрозрачное значение и может прислать нечисловое.
    --    ⚠️ Смена типа ключа — изменение структуры колонок FACT (см. CHANGELOG).
    --
    -- ⏭️ Открытый вопрос (аудит 17.08, сознательно НЕ решаем здесь): namespace ключа
    --    вида `id:123456` / `code:-999999`. `warehouse_id` и `warehouse_code` — разные
    --    пространства идентификаторов, и голая строка теоретически допускает коллизию
    --    числового id со строковым code. Сегодня коллизий нет (5 637 = 5 637 distinct),
    --    поэтому в этот фикс не берём: префикс — переписывание ключа во всей истории,
    --    его цена выше, чем польза, пока WB отдаёт один код. Вернуться, если появится
    --    второй непрозрачный код склада.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`
      PARTITION BY snapshot_date
      CLUSTER BY nm_id, warehouse_key AS
      WITH pick AS (
        SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
        FROM `wb_raw.WB_STOCKS_SNAPSHOTS`
        WHERE status = 'COMPLETE' AND period_to IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
      )
      SELECT
        p.snapshot_date,
        r.nm_id,
        COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING)) AS warehouse_key,
        -- однозначность warehouse_id внутри warehouse_key НЕ следует из построения ключа
        -- и доказывается отдельным ASSERT ниже (ремарка аудита 17.08).
        ANY_VALUE(r.warehouse_id)     AS warehouse_id,
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
      GROUP BY snapshot_date, nm_id, warehouse_key
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    -- not-null/not-empty grain (string) + not-null partition — как у FACT_ORDERS/FACT_SALES.
    -- ⚠️ Текст ASSERT изменён вместе с типом грейна: старая сигнатура ошибки была
    --    'FACT_STOCKS_SNAPSHOT: NULL grain/partition'.
    ASSERT (SELECT COUNTIF(snapshot_date IS NULL) + COUNTIF(nm_id IS NULL)
                 + COUNTIF(warehouse_key IS NULL) + COUNTIF(TRIM(warehouse_key) = '')
            FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) = 0
            AS 'FACT_STOCKS_SNAPSHOT: NULL/empty grain or NULL partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t|%t', snapshot_date, nm_id, warehouse_key))
            FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`)
            AS 'FACT_STOCKS_SNAPSHOT: (snapshot_date,nm_id,warehouse_key) not unique';
    -- fail-closed: непустой BUILD + соответствие грейну ВЫБРАННЫХ (последних COMPLETE) снапшотов.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) > 0 AS 'FACT_STOCKS_SNAPSHOT: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD`) = (
              SELECT COUNT(DISTINCT FORMAT('%t|%t|%t', p.snapshot_date, r.nm_id,
                       COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING))))
              FROM (SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
                    FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1) p
              JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id))
            AS 'FACT_STOCKS_SNAPSHOT: BUILD rows != source distinct grain (selected snapshots)';

    -- ──────────────────────────────────────────────────────────────────────
    -- 🔴 ANY_VALUE выше — не предположение, а проверяемый инвариант (ремарка аудита 17.08).
    --    Однозначность warehouse_id внутри warehouse_key НЕ гарантирована построением ключа:
    --    теоретически несколько RAW-строк могут нести один warehouse_code и разные
    --    warehouse_id, и тогда ANY_VALUE молча станет недетерминированным.
    --    Замер 17.08 на всей истории (30 суток, 5 637 групп): нарушений 0; более того,
    --    max RAW-строк на группу = 1 — то есть сегодня GROUP BY не схлопывает ничего.
    --    Но это свойство ДАННЫХ (у наших SKU один chrt_id), а не ключа: появится размерный
    --    ряд — и агрегация заработает по-настоящему. Проверяем на RAW ДО агрегации, потому
    --    что в BUILD нарушение уже неразличимо.
    --    ⚠️ FORMAT('%t', x) обязателен: COUNT(DISTINCT) сам по себе игнорирует NULL, и
    --    группа {NULL, 5} прошла бы как однозначная — а это ровно тот случай, который ловим.
    -- ──────────────────────────────────────────────────────────────────────
    ASSERT (SELECT COUNT(*) FROM (
              SELECT p.snapshot_date, r.nm_id,
                     COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING)) AS warehouse_key
              FROM (SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
                    FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1) p
              JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id)
              GROUP BY 1,2,3
              HAVING COUNT(DISTINCT FORMAT('%t', r.warehouse_id)) > 1)) = 0
            AS 'FACT_STOCKS_SNAPSHOT: multiple warehouse_id per warehouse_key';
    -- то же для остальных ANY_VALUE-колонок: описание склада и привязка SKU обязаны быть
    -- однозначны внутри грейна, иначе факт перестаёт быть воспроизводимым между прогонами.
    -- Разбивку «какая именно колонка сломалась» даёт проверка 4b в pr_mart1_validation.sql.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT p.snapshot_date, r.nm_id,
                     COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING)) AS warehouse_key
              FROM (SELECT snapshot_id, SAFE.PARSE_DATE('%Y-%m-%d', period_to) AS snapshot_date
                    FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status='COMPLETE' AND period_to IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC)=1) p
              JOIN `wb_raw.RAW_WB_STOCKS` r USING (snapshot_id)
              GROUP BY 1,2,3
              HAVING COUNT(DISTINCT FORMAT('%t', r.warehouse_name))   > 1
                  OR COUNT(DISTINCT FORMAT('%t', r.region_name))      > 1
                  OR COUNT(DISTINCT FORMAT('%t', r.internal_sku))     > 1
                  OR COUNT(DISTINCT FORMAT('%t', r.sku_match_status)) > 1)) = 0
            AS 'FACT_STOCKS_SNAPSHOT: multiple descriptive values per warehouse_key (name/region/sku)';

    -- ======================================================================
    -- 1.4 FACT_FINANCE — грейн: finance_row_key = report_id#rrd_id.
    --      PR-B: источник — V_WB_FINANCE_SEMANTIC (семантический слой поверх
    --      V_WB_FINANCE_CANONICAL). Канон не тронут: дедуп и ASSERT PR-A/A1
    --      ниже по-прежнему считаются ПО КАНОНУ.
    --      Целостность ключа проверяем ПО ИСТОЧНИКУ (обе части NOT NULL/empty),
    --      затем строим CONCAT БЕЗ IFNULL (иначе маскирует отсутствие части).
    --      PR-B2: +колонка marketplace_fee_gap_rub (authoritative сбор WB).
    --      Изменение аддитивное — ни одна существующая колонка не тронута.
    -- ======================================================================
    ASSERT (SELECT COUNTIF(report_id IS NULL OR TRIM(report_id) = ''
                        OR rrd_id    IS NULL OR TRIM(rrd_id)    = '')
            FROM `wb_raw.V_WB_FINANCE_SEMANTIC`) = 0
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
        -- PR-B2: authoritative сбор маркетплейса. Уже NUMERIC и уже округлён
        --   в V_WB_FINANCE_SEMANTIC §4.1 — здесь чистый pass-through, без
        --   повторного парсинга. NULL вне «Продажа»/«Возврат» — по контракту.
        marketplace_fee_gap_rub,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_raw.V_WB_FINANCE_SEMANTIC`
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
      -- PR-B2: retail_price_withdisc_rub стал входом authoritative-метрики
      --   marketplace_fee_gap_rub, поэтому попадает под тот же parse-QC.
      --   (for_pay уже покрыт выше как finance_for_pay_accounting.)
      + COUNTIF(retail_price_withdisc_rub IS NOT NULL AND TRIM(retail_price_withdisc_rub) <> '' AND SAFE_CAST(REPLACE(retail_price_withdisc_rub, ',', '.') AS NUMERIC) IS NULL)
      FROM `wb_raw.V_WB_FINANCE_SEMANTIC`) = 0 AS 'FACT_FINANCE: money parse-QC != 0';
    -- PR-B2 fail-closed: у операции с выручкой и выплатой спред обязан быть определён.
    --   Молчаливый NULL здесь занизил бы расход витрины (SUM игнорирует NULL).
    ASSERT (SELECT COUNTIF(supplier_oper_name IN ('Продажа', 'Возврат')
                           AND marketplace_fee_gap_rub IS NULL)
            FROM `wb_raw.V_WB_FINANCE_SEMANTIC`) = 0
            AS 'FACT_FINANCE: marketplace_fee_gap_rub NULL на «Продажа»/«Возврат»';
    ASSERT (SELECT COUNTIF(wb_nm_id IS NOT NULL AND TRIM(wb_nm_id) <> ''
              AND SAFE_CAST(wb_nm_id AS INT64) IS NULL)
            FROM `wb_raw.V_WB_FINANCE_SEMANTIC`) = 0 AS 'FACT_FINANCE: nm_id cast != 0';
    ASSERT (SELECT COUNTIF(finance_row_key IS NULL OR TRIM(finance_row_key) = '') + COUNTIF(finance_date IS NULL)
            FROM `wb_mart.FACT_FINANCE__BUILD`) = 0 AS 'FACT_FINANCE: NULL/empty grain or NULL partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT finance_row_key) FROM `wb_mart.FACT_FINANCE__BUILD`)
            AS 'FACT_FINANCE: finance_row_key not unique';
    -- PR-A/A1: finance_row_key включает report_id, поэтому пара DAILY+WEEKLY по одному
    --   rrd_id проходит проверку выше насквозь. Инвариант CANONICAL — ОДНА строка на
    --   rrd_id; если дедуп weekly/daily не сработал (например, weekly_final не проставлен),
    --   витрина задвоится молча. Fail-closed до подмены FACT_FINANCE.
    --   Спека: docs/FINANCE_PR_A_INGESTION_INTEGRITY_2026-08-11.md §2 A1.
    ASSERT (
      SELECT COUNT(*) - COUNT(DISTINCT rrd_id)
      FROM `wb_raw.V_WB_FINANCE_CANONICAL`
    ) = 0 AS 'PR-A: CANONICAL содержит дубли rrd_id — дедуп weekly/daily не сработал';
    -- fail-closed: непустой BUILD + pass-through объём.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE__BUILD`) > 0 AS 'FACT_FINANCE: empty build (fail-closed)';
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE__BUILD`)
         = (SELECT COUNT(*) FROM `wb_raw.V_WB_FINANCE_SEMANTIC`) AS 'FACT_FINANCE: BUILD rows != source rows';

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
    -- 1.7 FACT_ADS_SPEND_ALLOC_DAILY — грейн: date x advert_id x nm_id.
    --      Stage 3B (20.08.2026): РАСПРЕДЕЛЕНИЕ БИЛЛИНГА НА SKU.
    --
    --      ЧТО ЭТО. FACT_ADS_COSTS_DAILY.actual_spend_rub — то, что WB фактически
    --      списал с баланса за (сутки x кампанию). Это учётная величина, и разреза по
    --      nm_id у неё нет: WB его не публикует. FACT_ADS_SKU_DAILY.stats_spend_rub —
    --      расход, который WB ОТНЁС на карточку в статистике кампании. Это атрибуция,
    --      и её сумма систематически отличается от биллинга. Две разные величины, обе
    --      верные, ни одна не заменяет другую.
    --
    --      ПРАВИЛО РАСПРЕДЕЛЕНИЯ. Внутри (date, advert_id) биллинг делится
    --      пропорционально stats_spend_rub той же пары:
    --          billed_alloc_rub = actual_spend_rub * stats_sku / SUM(stats кампании)
    --      Если SUM(stats) по кампании = 0 (или строк статистики нет вовсе) — сумма
    --      НЕ размазывается ни на кого и целиком уходит в 1.8 как unallocated.
    --      Выдумывать получателя расхода нельзя: это была бы аллокация без основания.
    --
    --      ТОЧНОСТЬ. Построчного округления НЕТ. Деление NUMERIC даёт scale 9, поэтому
    --      SUM долей может разойтись с actual на нано-рубли. Остаток целиком относится
    --      на ЯКОРНУЮ строку (максимальный stats_spend_rub, тай-брейк nm_id ASC):
    --      инвариант SUM(billed_alloc) = actual выполняется ТОЧНО по построению, а
    --      ASSERT ниже проверяет логику распределения, а не арифметику NUMERIC.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`
      PARTITION BY `date`
      CLUSTER BY advert_id, nm_id AS
      WITH stats AS (
        SELECT
          `date`, advert_id, nm_id, stats_spend_rub,
          SUM(stats_spend_rub) OVER (PARTITION BY `date`, advert_id) AS campaign_stats_spend_rub
        FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`
      ),
      j AS (
        SELECT
          c.`date`, c.advert_id, s.nm_id,
          s.stats_spend_rub,
          s.campaign_stats_spend_rub,
          c.actual_spend_rub,
          SAFE_DIVIDE(s.stats_spend_rub, s.campaign_stats_spend_rub) AS alloc_weight,
          SAFE_DIVIDE(c.actual_spend_rub * s.stats_spend_rub, s.campaign_stats_spend_rub) AS alloc_raw
        FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD` c
        JOIN stats s
          ON s.`date` = c.`date` AND s.advert_id = c.advert_id
        WHERE s.campaign_stats_spend_rub > 0
      ),
      anchored AS (
        SELECT
          j.*,
          SUM(alloc_raw) OVER (PARTITION BY `date`, advert_id) AS alloc_sum,
          ROW_NUMBER() OVER (PARTITION BY `date`, advert_id
                             ORDER BY stats_spend_rub DESC, nm_id ASC) AS rn
        FROM j
      )
      SELECT
        `date`, advert_id, nm_id,
        stats_spend_rub,
        campaign_stats_spend_rub,
        actual_spend_rub,
        alloc_weight,
        alloc_raw + IF(rn = 1, actual_spend_rub - alloc_sum, 0) AS billed_alloc_rub,
        'stats_spend_rub' AS alloc_basis,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM anchored
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (SELECT COUNTIF(`date` IS NULL) + COUNTIF(advert_id IS NULL) + COUNTIF(nm_id IS NULL)
            FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: NULL grain/partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t|%t', `date`, advert_id, nm_id))
            FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`)
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: (date,advert_id,nm_id) not unique';
    -- fail-closed: биллинг есть (1.6 непуст), статистика есть (1.5 непуста) — значит и
    --   распределение обязано быть непустым. Пустой BUILD означал бы разрыв ключей.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`) > 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: empty build (fail-closed)';
    ASSERT (SELECT COUNTIF(alloc_weight IS NULL OR alloc_weight < 0 OR alloc_weight > 1)
            FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: alloc_weight вне [0,1]';
    ASSERT (SELECT COUNTIF(billed_alloc_rub < 0) FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: отрицательный billed_alloc_rub';
    -- ГЛАВНЫЙ инвариант распределения: по КАЖДОЙ паре (date, advert_id) остаток ровно 0.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT `date`, advert_id
              FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`
              GROUP BY `date`, advert_id
              HAVING ANY_VALUE(actual_spend_rub) - SUM(billed_alloc_rub) <> 0)) = 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: residual != 0 по (date, advert_id)';
    -- веса — доли одного основания, по кампании-дню обязаны давать единицу.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT `date`, advert_id
              FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`
              GROUP BY `date`, advert_id
              HAVING ABS(SUM(alloc_weight) - 1) > 0.000001)) = 0
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: SUM(alloc_weight) != 1 по кампании-дню';

    -- ======================================================================
    -- 1.8 FACT_ADS_SPEND_UNALLOC_DAILY — грейн: date x advert_id.
    --      Биллинг, который НЕ на кого распределить: у пары нет строк статистики
    --      либо SUM(stats) = 0. Таблица МОЖЕТ быть пустой — это норма, а не сбой,
    --      поэтому fail-closed проверки на непустоту здесь НЕТ.
    --
    --      Эта величина не имеет права исчезнуть: без неё сумма по SKU молча
    --      оказалась бы меньше фактического списания, и сходимость выглядела бы
    --      достигнутой при потерянных деньгах.
    -- ======================================================================
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`
      PARTITION BY `date`
      CLUSTER BY advert_id AS
      WITH camp AS (
        SELECT `date`, advert_id,
               COUNT(*)             AS stats_rows,
               SUM(stats_spend_rub) AS campaign_stats_spend_rub
        FROM `wb_mart.FACT_ADS_SKU_DAILY__BUILD`
        GROUP BY `date`, advert_id
      )
      SELECT
        c.`date`, c.advert_id,
        c.actual_spend_rub,
        c.actual_spend_rub                    AS unallocated_rub,
        IFNULL(k.stats_rows, 0)               AS stats_rows,
        IFNULL(k.campaign_stats_spend_rub, 0) AS campaign_stats_spend_rub,
        IF(k.advert_id IS NULL, 'NO_STATS_ROWS', 'ZERO_STATS_SPEND') AS reason,
        @run_id AS mart_run_id, @built_at AS built_at
      FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD` c
      LEFT JOIN camp k ON k.`date` = c.`date` AND k.advert_id = c.advert_id
      WHERE IFNULL(k.campaign_stats_spend_rub, 0) <= 0
    """ USING v_run_id AS run_id, v_built_at AS built_at;

    ASSERT (SELECT COUNTIF(`date` IS NULL) + COUNTIF(advert_id IS NULL)
            FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SPEND_UNALLOC_DAILY: NULL grain/partition';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', `date`, advert_id))
            FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`)
            AS 'FACT_ADS_SPEND_UNALLOC_DAILY: (date,advert_id) not unique';
    ASSERT (SELECT COUNTIF(reason NOT IN ('NO_STATS_ROWS', 'ZERO_STATS_SPEND'))
            FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`) = 0
            AS 'FACT_ADS_SPEND_UNALLOC_DAILY: неизвестный reason';
    -- ПОЛНОТА БИЛЛИНГА: распределённое + нераспределённое = фактическое списание.
    ASSERT (SELECT
              (SELECT IFNULL(SUM(billed_alloc_rub), 0) FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`)
            + (SELECT IFNULL(SUM(unallocated_rub), 0)  FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`)
            = (SELECT IFNULL(SUM(actual_spend_rub), 0) FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`))
            AS 'ADS_SPEND: SUM(billed_alloc) + SUM(unallocated) != SUM(actual_spend)';
    -- ни одна пара биллинга не потеряна и ни одна не учтена дважды.
    ASSERT (SELECT
              (SELECT COUNT(DISTINCT FORMAT('%t|%t', `date`, advert_id)) FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`)
            + (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`)
            = (SELECT COUNT(*) FROM `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`))
            AS 'ADS_SPEND: пары биллинга потеряны или задвоены между alloc/unalloc';

    -- ======================================================================
    -- 2. PUBLISH — все ASSERT пройдены. ПОСЛЕДОВАТЕЛЬНЫЙ publish каждой FACT
    --    (8 отдельных CREATE OR REPLACE; НЕ атомарно по всему набору — см. шапку).
    --    ПОВТОРЯЕМ PARTITION/CLUSTER (CTAS `SELECT *` их НЕ наследует!).
    -- ======================================================================
    CREATE OR REPLACE TABLE `wb_mart.FACT_ORDERS`
      PARTITION BY order_date CLUSTER BY nm_id, internal_sku AS
      SELECT * FROM `wb_mart.FACT_ORDERS__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_SALES`
      PARTITION BY sale_date CLUSTER BY nm_id, is_return AS
      SELECT * FROM `wb_mart.FACT_SALES__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_STOCKS_SNAPSHOT`
      PARTITION BY snapshot_date CLUSTER BY nm_id, warehouse_key AS
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
    CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
      PARTITION BY `date` CLUSTER BY advert_id, nm_id AS
      SELECT * FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY__BUILD`;
    CREATE OR REPLACE TABLE `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`
      PARTITION BY `date` CLUSTER BY advert_id AS
      SELECT * FROM `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY__BUILD`;

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
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=2
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_ADS_SPEND_ALLOC_DAILY')
            AS 'FACT_ADS_SPEND_ALLOC_DAILY: partition/cluster physics lost';
    ASSERT (SELECT COUNTIF(is_partitioning_column='YES')=1 AND COUNTIF(clustering_ordinal_position IS NOT NULL)=1
            FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='FACT_ADS_SPEND_UNALLOC_DAILY')
            AS 'FACT_ADS_SPEND_UNALLOC_DAILY: partition/cluster physics lost';

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
