-- ============================================================================
-- PR-Mig1c (остатки: T5 + контроль T5/T6 + детектор доступности) — ТОЧНАЯ сверка
-- shadow (Cloud Run) ↔ prod (Apps Script) для пятидневного окна приёмки.
-- Только чтение. Регион EU, dataset wb_raw.
--
-- Отличие от sql/mig1_stocks_parity.sql (Mig1, допуски по суммам): здесь сравнение
-- ПОСТРОЧНОЕ и ПОКОЛОНОЧНОЕ, без допусков. Расхождение возможно только из-за того,
-- что WB изменил остатки между снимками (prod ~06:23, shadow ~06:30 МСК), поэтому
-- вердикт дня различает:
--   EXACT       — все строки T6/T5 и все метрики manifest совпали;
--   DATA_DRIFT  — у совпавших ключей совпали тип строки, SKU и склад, разошлись только
--                 количества (и производные суммы/статусы), либо ключ T5 есть лишь на одной
--                 стороне И это ОБЪЯСНЕНО независимым движением того же товара (см. j5);
--                 число, количество и объяснение каждого такого ключа — в QUERY 3;
--   SOURCE_GAP  — хотя бы на одной стороне T5_UNAVAILABLE: T5 не сравнивается, день не
--                 засчитывается ни как EXACT, ни как FAIL (разбирается вручную);
--   FAIL        — структурное расхождение: ключи T6, атрибуты совпавших ключей T6/T5
--                 (row_type, SKU, source_api, склад), дубли грейна, структурные метрики manifest,
--                 НЕОБЪЯСНЁННЫЙ односторонний ключ T5.
-- Приёмка: 5 последовательных дней без FAIL, из них не меньше 3 EXACT (см.
-- docs/MIG1_STOCKS_PARITY.md, раздел Mig1c).
--
-- Ключи (после обезличивания складов 16.08.2026 warehouse_id = NULL у T6):
--   T6: nm_id | chrt_id | COALESCE(NULLIF(warehouse_code,''), CAST(warehouse_id AS STRING), warehouse_name)
--   T5: nm_id | barcode | tech_size | warehouse_name
-- Снимок дня — последний COMPLETE по period_to на каждой стороне.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — ВЕРДИКТ ПО ДНЯМ
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 7 AS lookback_days),
ps AS (
  SELECT period_to, snapshot_id, started_at FROM `wb_raw.WB_STOCKS_SNAPSHOTS`
  WHERE status = 'COMPLETE' AND SAFE.PARSE_DATE('%Y-%m-%d', period_to)
        >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
),
cs AS (
  SELECT period_to, snapshot_id, started_at FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR`
  WHERE status = 'COMPLETE' AND SAFE.PARSE_DATE('%Y-%m-%d', period_to)
        >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
),
-- T6 построчно
p6 AS (
  SELECT ps.period_to, CONCAT(CAST(r.nm_id AS STRING), '|', CAST(r.chrt_id AS STRING), '|',
         COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING), r.warehouse_name, '')) AS k,
         r.* EXCEPT (load_id, snapshot_id, snapshot_ts, raw_json, _snapshot_date)
  FROM `wb_raw.RAW_WB_STOCKS` r JOIN ps USING (snapshot_id)
),
c6 AS (
  SELECT cs.period_to, CONCAT(CAST(r.nm_id AS STRING), '|', CAST(r.chrt_id AS STRING), '|',
         COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING), r.warehouse_name, '')) AS k,
         r.* EXCEPT (load_id, snapshot_id, snapshot_ts, raw_json, _snapshot_date)
  FROM `wb_raw.RAW_WB_STOCKS__CR` r JOIN cs USING (snapshot_id)
),
t6 AS (
  SELECT period_to,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL) AS t6_matched,
    COUNTIF(c.k IS NULL) AS t6_prod_only,
    COUNTIF(p.k IS NULL) AS t6_cr_only,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL AND (
      p.quantity IS DISTINCT FROM c.quantity OR p.in_way_to_client IS DISTINCT FROM c.in_way_to_client
      OR p.in_way_from_client IS DISTINCT FROM c.in_way_from_client)) AS t6_qty_diff,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL AND (
      p.internal_sku IS DISTINCT FROM c.internal_sku OR p.sku_match_status IS DISTINCT FROM c.sku_match_status
      OR p.source_api IS DISTINCT FROM c.source_api OR p.warehouse_id IS DISTINCT FROM c.warehouse_id
      OR p.warehouse_name IS DISTINCT FROM c.warehouse_name OR p.region_name IS DISTINCT FROM c.region_name
      OR p.is_aggregate_warehouse IS DISTINCT FROM c.is_aggregate_warehouse)) AS t6_attr_diff
  FROM p6 p FULL OUTER JOIN c6 c USING (period_to, k)
  GROUP BY period_to
),
-- T5 построчно
p5 AS (
  SELECT ps.period_to, CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size, '|', r.warehouse_name) AS k,
         CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size) AS item,
         r.* EXCEPT (load_id, snapshot_id, snapshot_ts, _snapshot_date)
  FROM `wb_raw.RAW_WB_STOCKS_T5` r JOIN ps USING (snapshot_id)
),
c5 AS (
  SELECT cs.period_to, CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size, '|', r.warehouse_name) AS k,
         CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size) AS item,
         r.* EXCEPT (load_id, snapshot_id, snapshot_ts, _snapshot_date)
  FROM `wb_raw.RAW_WB_STOCKS_T5__CR` r JOIN cs USING (snapshot_id)
),
-- T6: nm, у которых между снимками что-то изменилось (количество, потоки или состав ключей).
t6_moved AS (
  SELECT DISTINCT period_to, IFNULL(p.nm_id, c.nm_id) AS nm_id
  FROM p6 p FULL OUTER JOIN c6 c USING (period_to, k)
  WHERE p.k IS NULL OR c.k IS NULL
     OR p.quantity IS DISTINCT FROM c.quantity OR p.in_way_to_client IS DISTINCT FROM c.in_way_to_client
     OR p.in_way_from_client IS DISTINCT FROM c.in_way_from_client
),
-- T5 построчно, с объяснением односторонних ключей.
-- Односторонний ключ T5 — товар × склад, который есть только на одной стороне. WB не отдаёт
-- строки с нулевым остатком, поэтому продажа/поступление между снимками МОЖЕТ так выглядеть,
-- но само по себе это не доказательство. Ключ считается ОБЪЯСНЁННЫМ, только если движение
-- того же товара видно независимо от него самого:
--   (a) у того же товара (nm|barcode|techSize) изменилось количество на СОВПАВШЕМ ключе
--       (обычно PSEUDO_TOTAL / AGGREGATE при продаже), или
--   (b) в T6 по тому же nm_id между снимками изменились остатки/потоки.
-- Необъяснённый односторонний ключ — FAIL: структурный пропуск под видом дрейфа не принимаем.
j5 AS (
  SELECT period_to, k, IFNULL(p.item, c.item) AS item, IFNULL(p.nm_id, c.nm_id) AS nm_id,
         IFNULL(p.row_type, c.row_type) AS row_type,
         CASE WHEN c.k IS NULL THEN 'prod_only' WHEN p.k IS NULL THEN 'cr_only' ELSE 'matched' END AS side,
         p.quantity AS prod_qty, c.quantity AS cr_qty,
         (p.k IS NOT NULL AND c.k IS NOT NULL AND p.quantity IS DISTINCT FROM c.quantity) AS qty_changed
  FROM p5 p FULL OUTER JOIN c5 c USING (period_to, k)
),
t5_item_moved AS (SELECT DISTINCT period_to, item FROM j5 WHERE qty_changed),
t5_one_sided AS (
  SELECT j.*, (im.item IS NOT NULL) AS item_moved, (tm.nm_id IS NOT NULL) AS t6_moved,
         (im.item IS NOT NULL OR tm.nm_id IS NOT NULL) AS explained
  FROM j5 j
  LEFT JOIN t5_item_moved im USING (period_to, item)
  LEFT JOIN t6_moved tm USING (period_to, nm_id)
  WHERE j.side != 'matched'
),
t5_os AS (
  SELECT period_to,
    COUNT(*) AS t5_one_sided,
    SUM(ABS(IFNULL(prod_qty, 0) - IFNULL(cr_qty, 0))) AS t5_one_sided_qty,
    COUNTIF(NOT explained) AS t5_one_sided_unexplained
  FROM t5_one_sided GROUP BY period_to
),
t5 AS (
  SELECT period_to,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL) AS t5_matched,
    COUNTIF(c.k IS NULL) AS t5_prod_only,
    COUNTIF(p.k IS NULL) AS t5_cr_only,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL AND p.quantity IS DISTINCT FROM c.quantity) AS t5_qty_diff,
    COUNTIF(p.k IS NOT NULL AND c.k IS NOT NULL AND (
      p.row_type IS DISTINCT FROM c.row_type OR p.internal_sku IS DISTINCT FROM c.internal_sku
      OR p.sku_match_status IS DISTINCT FROM c.sku_match_status OR p.source_api IS DISTINCT FROM c.source_api
      OR p.vendor_code IS DISTINCT FROM c.vendor_code OR p.volume IS DISTINCT FROM c.volume)) AS t5_attr_diff
  FROM p5 p FULL OUTER JOIN c5 c USING (period_to, k)
  GROUP BY period_to
),
dup5 AS (  -- грейн T5 не должен дублироваться ни на одной стороне
  SELECT period_to, SUM(IF(side = 'prod', n - 1, 0)) AS t5_dup_prod, SUM(IF(side = 'cr', n - 1, 0)) AS t5_dup_cr
  FROM (SELECT period_to, k, 'prod' AS side, COUNT(*) AS n FROM p5 GROUP BY 1, 2
        UNION ALL SELECT period_to, k, 'cr', COUNT(*) FROM c5 GROUP BY 1, 2)
  GROUP BY period_to
),
-- manifest поколоночно
pm AS (SELECT m.* FROM `wb_raw.WB_STOCKS_SNAPSHOTS` m JOIN ps USING (snapshot_id)),
cm AS (SELECT m.* FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR` m JOIN cs USING (snapshot_id)),
man AS (
  SELECT p.period_to,
    TIMESTAMP_DIFF(c.started_at, p.started_at, MINUTE) AS gap_min,
    p.control_status AS prod_control, c.control_status AS cr_control,
    p.availability_status AS prod_avail, c.availability_status AS cr_avail,
    -- структурные метрики: обязаны совпасть всегда
    ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST([
      IF(p.expected_rows IS DISTINCT FROM c.expected_rows, 'expected_rows', NULL),
      IF(p.written_rows IS DISTINCT FROM c.written_rows, 'written_rows', NULL),
      IF(p.distinct_keys IS DISTINCT FROM c.distinct_keys, 'distinct_keys', NULL),
      IF(p.duplicate_keys IS DISTINCT FROM c.duplicate_keys, 'duplicate_keys', NULL),
      IF(p.unique_nm_ids IS DISTINCT FROM c.unique_nm_ids, 'unique_nm_ids', NULL),
      IF(p.warehouses_count IS DISTINCT FROM c.warehouses_count, 'warehouses_count', NULL),
      IF(p.aggregate_warehouse_rows IS DISTINCT FROM c.aggregate_warehouse_rows, 'aggregate_warehouse_rows', NULL),
      IF(p.anonymized_warehouse_rows IS DISTINCT FROM c.anonymized_warehouse_rows, 'anonymized_warehouse_rows', NULL),
      IF(p.unmatched_nm_ids IS DISTINCT FROM c.unmatched_nm_ids, 'unmatched_nm_ids', NULL)
    ]) AS x WHERE x IS NOT NULL), ',') AS struct_diff,
    -- количественные метрики и производные статусы: расходятся только вместе с количествами
    ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST([
      IF(p.qty_positive_rows IS DISTINCT FROM c.qty_positive_rows, 'qty_positive_rows', NULL),
      -- число строк T5 зависит от состава ненулевых остатков → дрейф, а не структура
      IF(p.t5_rows_written IS DISTINCT FROM c.t5_rows_written, 't5_rows_written', NULL),
      IF(p.qty_zero_rows IS DISTINCT FROM c.qty_zero_rows, 'qty_zero_rows', NULL),
      IF(p.sum_quantity_all_t6 IS DISTINCT FROM c.sum_quantity_all_t6, 'sum_quantity_all_t6', NULL),
      IF(p.sum_quantity_physical_t6 IS DISTINCT FROM c.sum_quantity_physical_t6, 'sum_quantity_physical_t6', NULL),
      IF(p.t5_control_sum IS DISTINCT FROM c.t5_control_sum, 't5_control_sum', NULL),
      IF(p.t5_wb_rf_sum IS DISTINCT FROM c.t5_wb_rf_sum, 't5_wb_rf_sum', NULL),
      IF(p.t5_named_sum IS DISTINCT FROM c.t5_named_sum, 't5_named_sum', NULL),
      IF(p.t6_comparable IS DISTINCT FROM c.t6_comparable, 't6_comparable', NULL),
      IF(p.control_status IS DISTINCT FROM c.control_status, 'control_status', NULL),
      IF(p.control_delta IS DISTINCT FROM c.control_delta, 'control_delta', NULL),
      IF(p.availability_gap IS DISTINCT FROM c.availability_gap, 'availability_gap', NULL),
      IF(p.availability_gap_prev IS DISTINCT FROM c.availability_gap_prev, 'availability_gap_prev', NULL),
      IF(p.availability_gap_delta IS DISTINCT FROM c.availability_gap_delta, 'availability_gap_delta', NULL),
      IF(ROUND(p.availability_ratio, 9) IS DISTINCT FROM ROUND(c.availability_ratio, 9), 'availability_ratio', NULL),
      IF(p.availability_status IS DISTINCT FROM c.availability_status, 'availability_status', NULL)
    ]) AS x WHERE x IS NOT NULL), ',') AS qty_diff
  FROM pm p JOIN cm c USING (period_to)
)
SELECT
  d.period_to, man.gap_min,
  t6.t6_matched, t6.t6_prod_only, t6.t6_cr_only, t6.t6_qty_diff, t6.t6_attr_diff,
  t5.t5_matched, t5.t5_prod_only, t5.t5_cr_only, t5.t5_qty_diff, t5.t5_attr_diff,
  IFNULL(os.t5_one_sided, 0) AS t5_one_sided, IFNULL(os.t5_one_sided_qty, 0) AS t5_one_sided_qty,
  IFNULL(os.t5_one_sided_unexplained, 0) AS t5_one_sided_unexplained,
  dup5.t5_dup_prod, dup5.t5_dup_cr,
  man.prod_control, man.cr_control, man.prod_avail, man.cr_avail,
  NULLIF(man.struct_diff, '') AS manifest_struct_diff,
  NULLIF(man.qty_diff, '') AS manifest_qty_diff,
  CASE
    WHEN ps.snapshot_id IS NULL OR cs.snapshot_id IS NULL THEN 'MISSING'
    -- T6 и структура manifest проверяются всегда: они не зависят от судьбы T5.
    WHEN IFNULL(t6.t6_prod_only, 0) + IFNULL(t6.t6_cr_only, 0) + IFNULL(t6.t6_attr_diff, 0) > 0
      OR man.struct_diff != '' THEN 'FAIL'
    -- T5_UNAVAILABLE хотя бы на одной стороне — сбой источника в момент снимка, а не алгоритма.
    -- Проверяется ДО сравнения строк T5: иначе пустая детализация одной стороны дала бы FAIL.
    WHEN man.prod_control = 'T5_UNAVAILABLE' OR man.cr_control = 'T5_UNAVAILABLE' THEN 'SOURCE_GAP'
    WHEN IFNULL(t5.t5_attr_diff, 0) > 0
      OR IFNULL(dup5.t5_dup_prod, 0) + IFNULL(dup5.t5_dup_cr, 0) > 0
      OR IFNULL(os.t5_one_sided_unexplained, 0) > 0 THEN 'FAIL'
    WHEN IFNULL(t6.t6_qty_diff, 0) + IFNULL(t5.t5_qty_diff, 0) > 0
      OR IFNULL(t5.t5_prod_only, 0) + IFNULL(t5.t5_cr_only, 0) > 0
      OR man.qty_diff != '' THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS verdict
FROM (SELECT period_to FROM ps UNION DISTINCT SELECT period_to FROM cs) d
LEFT JOIN ps USING (period_to)
LEFT JOIN cs USING (period_to)
LEFT JOIN t6 USING (period_to)
LEFT JOIN t5 USING (period_to)
LEFT JOIN t5_os os USING (period_to)
LEFT JOIN dup5 USING (period_to)
LEFT JOIN man USING (period_to)
ORDER BY d.period_to DESC;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — СТРОКИ РАСХОЖДЕНИЙ T5 ЗА ДЕНЬ (для разбора FAIL/DATA_DRIFT)
-- Подставить @d = 'YYYY-MM-DD'.
-- ─────────────────────────────────────────────────────────────────────────
WITH ps AS (
  SELECT snapshot_id FROM `wb_raw.WB_STOCKS_SNAPSHOTS` WHERE status = 'COMPLETE' AND period_to = @d
  QUALIFY ROW_NUMBER() OVER (ORDER BY started_at DESC) = 1
),
cs AS (
  SELECT snapshot_id FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR` WHERE status = 'COMPLETE' AND period_to = @d
  QUALIFY ROW_NUMBER() OVER (ORDER BY started_at DESC) = 1
),
p AS (SELECT CONCAT(IFNULL(CAST(nm_id AS STRING), '-'), '|', barcode, '|', tech_size, '|', warehouse_name) AS k, *
      FROM `wb_raw.RAW_WB_STOCKS_T5` WHERE snapshot_id = (SELECT snapshot_id FROM ps)),
c AS (SELECT CONCAT(IFNULL(CAST(nm_id AS STRING), '-'), '|', barcode, '|', tech_size, '|', warehouse_name) AS k, *
      FROM `wb_raw.RAW_WB_STOCKS_T5__CR` WHERE snapshot_id = (SELECT snapshot_id FROM cs))
SELECT k, p.row_type AS prod_type, c.row_type AS cr_type, p.quantity AS prod_qty, c.quantity AS cr_qty,
       p.internal_sku AS prod_sku, c.internal_sku AS cr_sku
FROM p FULL OUTER JOIN c USING (k)
WHERE p.k IS NULL OR c.k IS NULL
   OR p.row_type IS DISTINCT FROM c.row_type OR p.quantity IS DISTINCT FROM c.quantity
   OR p.internal_sku IS DISTINCT FROM c.internal_sku OR p.sku_match_status IS DISTINCT FROM c.sku_match_status
ORDER BY k;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 3 — ОДНОСТОРОННИЕ КЛЮЧИ T5 ЗА ОКНО: что, сколько, чем объяснено
-- Обязательное приложение к отчёту каждого DATA_DRIFT-дня. explained=false → день FAIL.
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 7 AS lookback_days),
ps AS (
  SELECT period_to, snapshot_id FROM `wb_raw.WB_STOCKS_SNAPSHOTS`
  WHERE status = 'COMPLETE' AND SAFE.PARSE_DATE('%Y-%m-%d', period_to)
        >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
),
cs AS (
  SELECT period_to, snapshot_id FROM `wb_raw.WB_STOCKS_SNAPSHOTS__CR`
  WHERE status = 'COMPLETE' AND SAFE.PARSE_DATE('%Y-%m-%d', period_to)
        >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
),
p6 AS (SELECT ps.period_to, CONCAT(CAST(r.nm_id AS STRING), '|', CAST(r.chrt_id AS STRING), '|',
              COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING), r.warehouse_name, '')) AS k, r.*
       FROM `wb_raw.RAW_WB_STOCKS` r JOIN ps USING (snapshot_id)),
c6 AS (SELECT cs.period_to, CONCAT(CAST(r.nm_id AS STRING), '|', CAST(r.chrt_id AS STRING), '|',
              COALESCE(NULLIF(r.warehouse_code, ''), CAST(r.warehouse_id AS STRING), r.warehouse_name, '')) AS k, r.*
       FROM `wb_raw.RAW_WB_STOCKS__CR` r JOIN cs USING (snapshot_id)),
t6_moved AS (
  SELECT DISTINCT period_to, IFNULL(p.nm_id, c.nm_id) AS nm_id
  FROM p6 p FULL OUTER JOIN c6 c USING (period_to, k)
  WHERE p.k IS NULL OR c.k IS NULL
     OR p.quantity IS DISTINCT FROM c.quantity OR p.in_way_to_client IS DISTINCT FROM c.in_way_to_client
     OR p.in_way_from_client IS DISTINCT FROM c.in_way_from_client
),
p5 AS (SELECT ps.period_to, CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size, '|', r.warehouse_name) AS k,
              CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size) AS item, r.*
       FROM `wb_raw.RAW_WB_STOCKS_T5` r JOIN ps USING (snapshot_id)),
c5 AS (SELECT cs.period_to, CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size, '|', r.warehouse_name) AS k,
              CONCAT(IFNULL(CAST(r.nm_id AS STRING), '-'), '|', r.barcode, '|', r.tech_size) AS item, r.*
       FROM `wb_raw.RAW_WB_STOCKS_T5__CR` r JOIN cs USING (snapshot_id)),
j5 AS (
  SELECT period_to, k, IFNULL(p.item, c.item) AS item, IFNULL(p.nm_id, c.nm_id) AS nm_id,
         IFNULL(p.row_type, c.row_type) AS row_type, IFNULL(p.internal_sku, c.internal_sku) AS internal_sku,
         CASE WHEN c.k IS NULL THEN 'prod_only' WHEN p.k IS NULL THEN 'cr_only' ELSE 'matched' END AS side,
         p.quantity AS prod_qty, c.quantity AS cr_qty,
         (p.k IS NOT NULL AND c.k IS NOT NULL AND p.quantity IS DISTINCT FROM c.quantity) AS qty_changed
  FROM p5 p FULL OUTER JOIN c5 c USING (period_to, k)
),
t5_item_moved AS (
  SELECT period_to, item, STRING_AGG(FORMAT('%s %d→%d', row_type, prod_qty, cr_qty), '; ') AS item_changes
  FROM j5 WHERE qty_changed GROUP BY period_to, item
)
SELECT j.period_to, j.side, j.k, j.row_type, j.internal_sku, j.prod_qty, j.cr_qty,
       im.item_changes,                               -- (a) движение того же товара на совпавших ключах
       (tm.nm_id IS NOT NULL) AS t6_moved,            -- (b) движение того же nm в T6
       (im.item IS NOT NULL OR tm.nm_id IS NOT NULL) AS explained
FROM j5 j
LEFT JOIN t5_item_moved im USING (period_to, item)
LEFT JOIN t6_moved tm USING (period_to, nm_id)
WHERE j.side != 'matched'
ORDER BY j.period_to DESC, explained, j.k;
