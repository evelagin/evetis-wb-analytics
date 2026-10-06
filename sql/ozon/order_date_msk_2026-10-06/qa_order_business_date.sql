-- ============================================================================
-- QA (read-only) — бизнес-дата заказа Ozon = календарные сутки МСК (2026-10-06).
-- Запускать ПОСЛЕ развёртывания представлений ozon_mart. Каждый блок — один SELECT со столбцом status.
-- Блоки разделены строкой «-- @check <ID>»; запуск по одному (bq query --use_legacy_sql=false).
-- Источник истины для Q2/Q3 — Ozon Seller Analytics /v1/analytics/data (ordered_units, dimension day/sku),
-- снято 2026-10-06 12:51 МСК: 30.09=7, 01.10=26, 02.10=18, 03.10=10, 04.10=23, 05.10=10.
-- ============================================================================

-- @check Q1_FCT_DAY_EQUALS_MSK_DAY_OF_CREATED_AT
-- Для каждых суток: заказано в FCT = сумма quantity отправлений с DATE(created_at, МСК) в эти сутки.
WITH raw AS (
  SELECT DATE(p.created_at, 'Europe/Moscow') d, SUM(p.quantity) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN (SELECT DISTINCT marketplace_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
        WHERE marketplace = 'OZON') m ON m.marketplace_sku = p.sku
  GROUP BY 1),
fct AS (
  SELECT fact_date d, SUM(gross_qty) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` GROUP BY 1)
SELECT COUNT(*) days, COUNTIF(IFNULL(raw.q, 0) <> IFNULL(fct.q, 0)) days_failed,
  IF(COUNTIF(IFNULL(raw.q, 0) <> IFNULL(fct.q, 0)) = 0, 'PASS', 'FAIL') status
FROM raw FULL JOIN fct USING (d);

-- @check Q2_DAILY_TOTALS_MATCH_OZON_ANALYTICS
WITH exp AS (
  SELECT * FROM UNNEST([STRUCT(DATE '2026-09-30' AS d, 7 AS q), (DATE '2026-10-01', 26), (DATE '2026-10-02', 18),
                        (DATE '2026-10-03', 10), (DATE '2026-10-04', 23), (DATE '2026-10-05', 10)])),
fct AS (
  SELECT fact_date d, SUM(gross_qty) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL`
  WHERE fact_date BETWEEN '2026-09-30' AND '2026-10-05' GROUP BY 1)
SELECT STRING_AGG(FORMAT('%t=%d/%d', exp.d, IFNULL(fct.q, 0), exp.q), ', ' ORDER BY exp.d) actual_vs_ozon,
  IF(COUNTIF(IFNULL(fct.q, 0) <> exp.q) = 0, 'PASS', 'FAIL') status
FROM exp LEFT JOIN fct USING (d);

-- @check Q3_BOUNDARY_POSTINGS_LAND_ON_MSK_DAY
-- 0120712550-0195-1: 01:37 МСК 06.10 → 06.10 (не 05.10); 32276726-0432-1 и 31090352-0598-3 — 04.10
-- (не 03.10); 42806693-0193-2: 00:49 МСК 03.10 → 03.10 (не 02.10).
WITH exp AS (
  SELECT * FROM UNNEST([STRUCT('0120712550-0195-1' AS posting_number, DATE '2026-10-06' AS d),
                        ('32276726-0432-1', DATE '2026-10-04'), ('31090352-0598-3', DATE '2026-10-04'),
                        ('42806693-0193-2', DATE '2026-10-03')])),
-- Отправление должно быть в RAW, его МСК-сутки — ожидаемые, а факт на эти сутки × SKU — содержать его штуки.
m AS (SELECT DISTINCT internal_sku, marketplace_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
        WHERE marketplace = 'OZON'),
fct AS (
  SELECT f.fact_date d, m.marketplace_sku sku, SUM(f.gross_qty) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` f JOIN m USING (internal_sku)
  WHERE f.fact_date BETWEEN '2026-10-01' AND '2026-10-07' GROUP BY 1, 2),
lines AS (
  SELECT e.posting_number, e.d, p.sku, p.quantity, p.order_date utc_d, DATE(p.created_at, 'Europe/Moscow') msk_d,
    fct.q fct_qty_on_msk_day
  FROM exp e LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p USING (posting_number)
  LEFT JOIN fct ON fct.d = e.d AND fct.sku = p.sku)
SELECT STRING_AGG(FORMAT('%s: utc=%t msk=%t fct=%d', posting_number, utc_d, msk_d, IFNULL(fct_qty_on_msk_day, 0)), '; ') evidence,
  IF(COUNT(DISTINCT IF(sku IS NOT NULL, posting_number, NULL)) = 4 AND COUNTIF(msk_d <> d) = 0
     AND COUNTIF(IFNULL(fct_qty_on_msk_day, 0) < quantity) = 0, 'PASS', 'FAIL') status
FROM lines;

-- @check Q4_SKU_DAYS_03_05_MATCH_OZON_ANALYTICS
-- Разбивка по SKU за 03.10 и 05.10 из Ozon Analytics (ненулевые позиции); все прочие SKU — 0.
WITH exp AS (
  SELECT * FROM UNNEST([
    STRUCT(DATE '2026-10-03' AS d, '1991772098' AS sku, 1 AS q), (DATE '2026-10-03', '1997079254', 5),
    (DATE '2026-10-03', '2974977988', 2), (DATE '2026-10-03', '3530427102', 1), (DATE '2026-10-03', '3733452761', 1),
    (DATE '2026-10-05', '2974977988', 2), (DATE '2026-10-05', '3530427102', 1), (DATE '2026-10-05', '3733452761', 1),
    (DATE '2026-10-05', '3735183318', 1), (DATE '2026-10-05', '3735674506', 1), (DATE '2026-10-05', '3892223191', 2),
    (DATE '2026-10-05', '3892345516', 1), (DATE '2026-10-05', '3892518031', 1)])),
m AS (SELECT DISTINCT internal_sku, marketplace_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
      WHERE marketplace = 'OZON'),
fct AS (
  SELECT f.fact_date d, m.marketplace_sku sku, SUM(f.gross_qty) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` f JOIN m USING (internal_sku)
  WHERE f.fact_date IN ('2026-10-03', '2026-10-05') AND f.gross_qty > 0 GROUP BY 1, 2)
SELECT COUNT(*) sku_days, COUNTIF(IFNULL(fct.q, 0) <> IFNULL(exp.q, 0)) sku_days_failed,
  IF(COUNTIF(IFNULL(fct.q, 0) <> IFNULL(exp.q, 0)) = 0, 'PASS', 'FAIL') status
FROM exp FULL JOIN fct USING (d, sku);
