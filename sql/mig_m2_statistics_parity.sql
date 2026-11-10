-- ============================================================================
-- M2 (заказы, продажи, ночная сверка) — сверка shadow (Cloud Run, `__CR`) ↔ prod (Apps Script).
-- ТОЛЬКО ЧТЕНИЕ. Регион EU, dataset wb_raw. Три независимых запроса; запускать по одному.
-- Цена: обе таблицы ~5–8 МБ → минимум тарификации 10 МБ на таблицу, ≈ 40 МБ на запрос.
--
-- ЧТО СРАВНИВАЕТСЯ. Обе стороны append-only, последнее состояние выбирает вью (V_WB_ORDERS —
-- last-wins по srid, V_WB_SALES_RETURNS — по sale_id). Поэтому сравниваются СОСТОЯНИЯ, а не строки:
--   • дубли строк не считаются расхождением (Apps Script при сбое после записи дописывает повторно,
--     Cloud Run читает watermark из таблицы и не дублирует — см. runbook, «Отличия»);
--   • состояние = все колонки строки, кроме load_id / loaded_at / raw_row_number (идентификаторы
--     запуска) и internal_sku / sku_match_status (привязка SKU сравнивается отдельно: Apps Script
--     читает лист SKU_MASTER в момент загрузки, Cloud Run — REF_SKU_MASTER после ночной синхронизации).
-- Сутки состояния — дата его last_change_date (время WB, МСК).
--
-- ВЕРДИКТ ДНЯ (QUERY 1, отдельно для заказов и продаж):
--   EXACT      — множества состояний дня совпали, привязка SKU совпала;
--   DATA_DRIFT — расхождения есть, но каждое ОБЪЯСНЕНО опросом в разные минуты: у стороны, где
--                состояния нет, есть БОЛЕЕ ПОЗДНЕЕ состояние того же ключа (WB отдаёт только текущее
--                состояние, промежуточное видит лишь тот, кто опросил между изменениями); либо
--                разошлась только привязка SKU (лаг REF_SKU_MASTER);
--   FAIL       — необъяснённое: ключ или его последнее состояние дня есть только у одной стороны,
--                и у другой нет более позднего состояния; или одно и то же last_change_date ключа
--                нормализовано по-разному;
--   NO_DATA    — нет строк ни у одной стороны.
-- QUERY 2 — итог окна в семантике вью: последнее состояние каждого ключа (то, что видит витрина).
--   Ключи с последним состоянием старше «общего горизонта» (меньший из MAX(last_change_date) сторон)
--   обязаны совпасть ПОЛНОСТЬЮ. Любое расхождение = FAIL окна.
-- QUERY 3 — разбор: ключи с расхождениями (для ручного анализа; ключи в отчёт PR не копировать).
--
-- ПРИЁМКА (runbook): 5 последовательных дней без FAIL в QUERY 1 по обоим потокам, QUERY 2 = EXACT,
-- из 5 дней не меньше 3 EXACT по каждому потоку.
--
-- ПАРАМЕТРЫ: d_from — первые ПОЛНЫЕ сутки shadow (день после старта); d_to — вчера МСК.
-- Самопроверка логики (выполнена при подготовке PR): если заменить `__CR` на боевые таблицы,
-- все дни дают EXACT, QUERY 2 — EXACT.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — ВЕРДИКТ ПО ДНЯМ (заказы и продажи)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (
  SELECT DATE '2026-11-12' AS d_from, DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS d_to
),
o_all AS (
  SELECT 'prod' AS side, srid AS k, last_change_date AS lcd,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, '')) AS sku,
         TO_JSON_STRING(STRUCT(source_api, row_hash, srid, order_uid, order_dt, cancel_dt, is_cancel, wb_nm_id,
           wb_vendor_code, barcode, category, subject, brand, tech_size, warehouse_name, region_name, country_name,
           price_with_disc, quantity, sticker_id, is_duplicate, processed_status, error_message, last_change_date,
           _order_date, total_price, discount_percent, spp, finished_price)) AS fp
  FROM `wb_raw.RAW_WB_ORDERS` WHERE source_api = 'WB_API_ORDERS'
  UNION ALL
  SELECT 'cr', srid, last_change_date,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, '')),
         TO_JSON_STRING(STRUCT(source_api, row_hash, srid, order_uid, order_dt, cancel_dt, is_cancel, wb_nm_id,
           wb_vendor_code, barcode, category, subject, brand, tech_size, warehouse_name, region_name, country_name,
           price_with_disc, quantity, sticker_id, is_duplicate, processed_status, error_message, last_change_date,
           _order_date, total_price, discount_percent, spp, finished_price))
  FROM `wb_raw.RAW_WB_ORDERS__CR` WHERE source_api = 'WB_API_ORDERS'
),
s_all AS (
  SELECT 'prod' AS side, sale_id AS k, last_change_date AS lcd,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, '')) AS sku,
         TO_JSON_STRING(STRUCT(source_api, row_hash, sale_id, srid, g_number, income_id, sale_dt, last_change_date,
           wb_nm_id, wb_vendor_code, barcode, category, subject, brand, tech_size, warehouse_name, warehouse_type,
           region_name, oblast_okrug_name, country_name, total_price, discount_percent, spp, payment_sale_amount,
           price_with_disc, finished_price, for_pay, is_supply, is_realization, operation_type, is_return,
           sticker_id, processed_status, error_message, TO_HEX(MD5(raw_json)) AS raw_md5, _sale_date)) AS fp
  FROM `wb_raw.RAW_WB_SALES_RETURNS` WHERE source_api = 'WB_API_SALES'
  UNION ALL
  SELECT 'cr', sale_id, last_change_date,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, '')),
         TO_JSON_STRING(STRUCT(source_api, row_hash, sale_id, srid, g_number, income_id, sale_dt, last_change_date,
           wb_nm_id, wb_vendor_code, barcode, category, subject, brand, tech_size, warehouse_name, warehouse_type,
           region_name, oblast_okrug_name, country_name, total_price, discount_percent, spp, payment_sale_amount,
           price_with_disc, finished_price, for_pay, is_supply, is_realization, operation_type, is_return,
           sticker_id, processed_status, error_message, TO_HEX(MD5(raw_json)) AS raw_md5, _sale_date))
  FROM `wb_raw.RAW_WB_SALES_RETURNS__CR` WHERE source_api = 'WB_API_SALES'
),
st AS (
  SELECT DISTINCT 'orders' AS stream, side, k, lcd, sku, fp FROM o_all
  UNION ALL
  SELECT DISTINCT 'sales', side, k, lcd, sku, fp FROM s_all
),
-- Самое позднее состояние ключа на каждой стороне за всё время — «объяснитель» расхождений.
max_lcd AS (
  SELECT stream, k, MAX(IF(side = 'prod', lcd, NULL)) AS p_max, MAX(IF(side = 'cr', lcd, NULL)) AS c_max
  FROM st GROUP BY stream, k
),
st_day AS (
  SELECT st.*, SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(lcd, 1, 10)) AS d FROM st, params
  WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(lcd, 1, 10)) BETWEEN params.d_from AND params.d_to
),
-- (a) состояния дня и «точки» (ключ, last_change_date)
states AS (
  SELECT stream, d, fp, k, lcd,
         LOGICAL_OR(side = 'prod') AS in_p, LOGICAL_OR(side = 'cr') AS in_c
  FROM st_day GROUP BY stream, d, fp, k, lcd
),
points AS (
  SELECT stream, d, k, lcd,
         LOGICAL_OR(in_p) AS p_any, LOGICAL_OR(in_c) AS c_any, LOGICAL_OR(in_p AND in_c) AS common
  FROM states GROUP BY stream, d, k, lcd
),
states_eval AS (
  SELECT s.stream, s.d,
         COUNTIF(s.in_p AND s.in_c) AS states_both,
         COUNTIF(s.in_p AND NOT s.in_c) AS states_prod_only,
         COUNTIF(s.in_c AND NOT s.in_p) AS states_cr_only
  FROM states s GROUP BY s.stream, s.d
),
points_eval AS (
  SELECT p.stream, p.d,
         -- необъяснённое: точка есть только у одной стороны, и у другой нет состояния этого ключа ПОЗЖЕ
         COUNTIF(p.p_any AND NOT p.c_any AND NOT IFNULL(m.c_max > p.lcd, FALSE)) AS prod_only_unexplained,
         COUNTIF(p.c_any AND NOT p.p_any AND NOT IFNULL(m.p_max > p.lcd, FALSE)) AS cr_only_unexplained,
         -- точка есть у обеих сторон, но ни одного общего состояния: одна и та же отметка WB
         -- нормализована по-разному (лишнее состояние при общем — это DATA_DRIFT, не FAIL)
         COUNTIF(p.p_any AND p.c_any AND NOT p.common) AS same_point_diff
  FROM points p JOIN max_lcd m USING (stream, k)
  GROUP BY p.stream, p.d
),
-- (b) привязка SKU у совпавших состояний
sku_eval AS (
  SELECT stream, d,
         COUNTIF(p_sku IS NOT NULL AND c_sku IS NOT NULL AND p_sku != c_sku) AS sku_diff
  FROM (
    SELECT stream, d, fp,
           ANY_VALUE(IF(side = 'prod', sku, NULL)) AS p_sku, ANY_VALUE(IF(side = 'cr', sku, NULL)) AS c_sku
    FROM st_day GROUP BY stream, d, fp
  ) GROUP BY stream, d
),
days AS (
  SELECT stream, d FROM UNNEST(['orders', 'sales']) AS stream,
    UNNEST(GENERATE_DATE_ARRAY((SELECT d_from FROM params), (SELECT d_to FROM params))) AS d
)
SELECT days.stream, days.d AS day,
  IFNULL(e.states_both, 0) AS states_both,
  IFNULL(e.states_prod_only, 0) AS states_prod_only,
  IFNULL(e.states_cr_only, 0) AS states_cr_only,
  IFNULL(pe.prod_only_unexplained, 0) AS prod_only_unexplained,
  IFNULL(pe.cr_only_unexplained, 0) AS cr_only_unexplained,
  IFNULL(pe.same_point_diff, 0) AS same_point_diff,
  IFNULL(k.sku_diff, 0) AS sku_diff,
  CASE
    WHEN e.stream IS NULL THEN 'NO_DATA'
    WHEN pe.prod_only_unexplained > 0 OR pe.cr_only_unexplained > 0 OR pe.same_point_diff > 0 THEN 'FAIL'
    WHEN e.states_prod_only > 0 OR e.states_cr_only > 0 OR IFNULL(k.sku_diff, 0) > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS verdict
FROM days
LEFT JOIN states_eval e USING (stream, d)
LEFT JOIN points_eval pe USING (stream, d)
LEFT JOIN sku_eval k USING (stream, d)
ORDER BY days.stream, day;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — ИТОГ ОКНА В СЕМАНТИКЕ ВЬЮ (последнее состояние ключа)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT '2026-11-12' AS lcd_from),  -- = d_from QUERY 1, строкой (сравнение по префиксу даты)
o AS (
  SELECT 'orders' AS stream, side, k, lcd, fp FROM (
    SELECT 'prod' AS side, srid AS k, last_change_date AS lcd,
           TO_JSON_STRING(STRUCT(row_hash, order_dt, cancel_dt, is_cancel, wb_nm_id, price_with_disc, sticker_id,
             warehouse_name, region_name, total_price, discount_percent, spp, finished_price, last_change_date)) AS fp,
           ROW_NUMBER() OVER (PARTITION BY srid ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC, row_hash DESC) AS rn
    FROM `wb_raw.RAW_WB_ORDERS` WHERE source_api = 'WB_API_ORDERS'
    UNION ALL
    SELECT 'cr', srid, last_change_date,
           TO_JSON_STRING(STRUCT(row_hash, order_dt, cancel_dt, is_cancel, wb_nm_id, price_with_disc, sticker_id,
             warehouse_name, region_name, total_price, discount_percent, spp, finished_price, last_change_date)),
           ROW_NUMBER() OVER (PARTITION BY srid ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC, row_hash DESC)
    FROM `wb_raw.RAW_WB_ORDERS__CR` WHERE source_api = 'WB_API_ORDERS'
  ) WHERE rn = 1
),
s AS (
  SELECT 'sales' AS stream, side, k, lcd, fp FROM (
    SELECT 'prod' AS side, sale_id AS k, last_change_date AS lcd, TO_HEX(MD5(raw_json)) AS fp,
           ROW_NUMBER() OVER (PARTITION BY sale_id ORDER BY last_change_date DESC, TO_HEX(MD5(raw_json)) DESC) AS rn
    FROM `wb_raw.RAW_WB_SALES_RETURNS`
    WHERE source_api = 'WB_API_SALES' AND sale_id IS NOT NULL AND TRIM(sale_id) <> '' AND processed_status <> 'MISSING_EVENT_KEY'
    UNION ALL
    SELECT 'cr', sale_id, last_change_date, TO_HEX(MD5(raw_json)),
           ROW_NUMBER() OVER (PARTITION BY sale_id ORDER BY last_change_date DESC, TO_HEX(MD5(raw_json)) DESC)
    FROM `wb_raw.RAW_WB_SALES_RETURNS__CR`
    WHERE source_api = 'WB_API_SALES' AND sale_id IS NOT NULL AND TRIM(sale_id) <> '' AND processed_status <> 'MISSING_EVENT_KEY'
  ) WHERE rn = 1
),
latest AS (SELECT * FROM o UNION ALL SELECT * FROM s),
-- Общий горизонт: меньший из MAX(last_change_date) сторон — дальше одна из сторон ещё не опрашивала.
horizon AS (
  SELECT stream, LEAST(MAX(IF(side = 'prod', lcd, NULL)), MAX(IF(side = 'cr', lcd, NULL))) AS h FROM latest GROUP BY stream
),
cmp AS (
  SELECT stream, k,
         ANY_VALUE(IF(side = 'prod', fp, NULL)) AS p_fp, ANY_VALUE(IF(side = 'cr', fp, NULL)) AS c_fp,
         MAX(lcd) AS lcd_max
  FROM latest GROUP BY stream, k
)
SELECT c.stream,
  COUNTIF(c.p_fp = c.c_fp) AS keys_equal,
  COUNTIF(c.p_fp IS NOT NULL AND c.c_fp IS NOT NULL AND c.p_fp != c.c_fp) AS keys_latest_diff,
  COUNTIF(c.c_fp IS NULL) AS keys_prod_only,
  COUNTIF(c.p_fp IS NULL) AS keys_cr_only,
  IF(COUNTIF(c.p_fp IS NULL OR c.c_fp IS NULL OR c.p_fp != c.c_fp) = 0, 'EXACT', 'FAIL') AS verdict,
  ANY_VALUE(h.h) AS horizon
FROM cmp c JOIN horizon h USING (stream), params
WHERE c.lcd_max >= params.lcd_from AND c.lcd_max < h.h
GROUP BY c.stream
ORDER BY c.stream;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 3 — РАЗБОР: состояния, которые есть только у одной стороны (дни окна)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT '2026-11-12' AS lcd_from),
st AS (
  SELECT DISTINCT 'orders' AS stream, 'prod' AS side, srid AS k, last_change_date AS lcd, row_hash AS h,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, '')) AS sku
  FROM `wb_raw.RAW_WB_ORDERS` WHERE source_api = 'WB_API_ORDERS'
  UNION ALL
  SELECT DISTINCT 'orders', 'cr', srid, last_change_date, row_hash,
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, ''))
  FROM `wb_raw.RAW_WB_ORDERS__CR` WHERE source_api = 'WB_API_ORDERS'
  UNION ALL
  SELECT DISTINCT 'sales', 'prod', sale_id, last_change_date, TO_HEX(MD5(raw_json)),
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, ''))
  FROM `wb_raw.RAW_WB_SALES_RETURNS` WHERE source_api = 'WB_API_SALES'
  UNION ALL
  SELECT DISTINCT 'sales', 'cr', sale_id, last_change_date, TO_HEX(MD5(raw_json)),
         CONCAT(IFNULL(internal_sku, ''), '|', IFNULL(sku_match_status, ''))
  FROM `wb_raw.RAW_WB_SALES_RETURNS__CR` WHERE source_api = 'WB_API_SALES'
)
SELECT stream, k, lcd, h,
       STRING_AGG(DISTINCT side ORDER BY side) AS sides,
       STRING_AGG(DISTINCT CONCAT(side, ':', sku) ORDER BY CONCAT(side, ':', sku)) AS sku_by_side
FROM st, params
WHERE lcd >= params.lcd_from
GROUP BY stream, k, lcd, h
HAVING COUNT(DISTINCT side) = 1 OR COUNT(DISTINCT sku) > 1
ORDER BY stream, k, lcd
LIMIT 500;
