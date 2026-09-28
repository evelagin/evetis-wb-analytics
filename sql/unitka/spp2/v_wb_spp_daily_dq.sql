-- SPP-2 · DQ wb_mart.V_WB_SPP_DAILY — ТОЛЬКО ЧТЕНИЕ, запуск после развёртывания.
-- Каждая строка результата — проверка: check_name, value, expected, ok.
-- Окно — с 01.09.2026 (старт AUTO для AB); ценовые поля заказов есть с 25.06.2026.
WITH v AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SPP_DAILY` WHERE date_msk >= DATE '2026-09-01'
),
src AS (
  SELECT
    SUM(SAFE_CAST(quantity AS INT64)) AS qty,
    SUM(IF(LOWER(IFNULL(is_cancel, '')) = 'true', SAFE_CAST(quantity AS INT64), 0)) AS cancels,
    SUM(SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) * SAFE_CAST(quantity AS INT64)) AS pwd,
    SUM(SAFE_CAST(REPLACE(finished_price, ',', '.') AS NUMERIC) * SAFE_CAST(quantity AS INT64)) AS fp
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_ORDERS`
  WHERE _order_date >= DATE '2026-09-01'
),
vv AS (
  SELECT SUM(orders_qty) AS qty, SUM(cancelled_orders_qty) AS cancels,
         SUM(sum_price_with_disc) AS pwd, SUM(sum_finished_price) AS fp
  FROM v
),
aa AS (   -- популяция: AA Юнитки (Orders API) = Σ price_with_disc / orders_qty вью
  SELECT COUNTIF(ABS(ROUND(SAFE_DIVIDE(v.sum_price_with_disc, v.orders_qty), 2) - u.price) > 0.01) AS diffs
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT` u
  JOIN v ON v.nm_id = u.nm_id AND v.date_msk = u.date_msk
  WHERE u.price_source = 'ORDERS_API'
)
SELECT 'duplicate_grain' AS check_name, COUNT(*) - COUNT(DISTINCT FORMAT('%t|%d', date_msk, nm_id)) AS value, 0 AS expected FROM v
UNION ALL SELECT 'null_effective_spp', COUNTIF(effective_spp_pct IS NULL AND spp_status != 'PRICE_DATA_MISSING'), 0 FROM v
UNION ALL SELECT 'invalid_effective_spp', COUNTIF(effective_spp_pct < -100 OR effective_spp_pct > 100), 0 FROM v
UNION ALL SELECT 'bad_source', COUNTIF(spp_source != 'ORDER_SPP_ACTUAL'), 0 FROM v
UNION ALL SELECT 'unknown_status', COUNTIF(spp_status NOT IN ('OK', 'ZERO_SPP', 'NEGATIVE_MARKUP', 'PRICE_DATA_MISSING')), 0 FROM v
UNION ALL SELECT 'orders_qty_diff', (SELECT qty FROM src) - (SELECT qty FROM vv), 0
UNION ALL SELECT 'cancels_diff', (SELECT cancels FROM src) - (SELECT cancels FROM vv), 0
UNION ALL SELECT 'price_with_disc_diff_kop', CAST(ROUND(100 * ((SELECT pwd FROM src) - (SELECT pwd FROM vv))) AS INT64), 0
UNION ALL SELECT 'finished_price_diff_kop', CAST(ROUND(100 * ((SELECT fp FROM src) - (SELECT fp FROM vv))) AS INT64), 0
UNION ALL SELECT 'aa_population_diffs', (SELECT diffs FROM aa), 0
UNION ALL SELECT 'unmapped_internal_sku', COUNTIF(internal_sku IS NULL), 0 FROM v
