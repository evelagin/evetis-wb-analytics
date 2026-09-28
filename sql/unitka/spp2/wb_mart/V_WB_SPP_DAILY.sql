-- ============================================================================
-- SPP-2 (2026-09-28) · wb_mart.V_WB_SPP_DAILY (VIEW) · Git-first, развёртывание tools/spp_daily_view_deploy.py
-- Вне sql/current: wb_mart не канонизирован (docs/architecture/CANONICAL_COVERAGE.md §3) — тот же путь,
-- что у WB-вью PROMO-2 (sql/promotions/pr_promo2/wb_mart). Перенос — после решения владельца по варианту A/B.
-- Грейн: date_msk × nm_id. Строка есть ТОЛЬКО там, где у SKU в этот день есть строки Orders API
-- (wb_raw.V_WB_ORDERS); искусственных строк для дней без заказов нет.
-- Контракт: docs/UNITKA_SPP_2_DAILY_VIEW_2026-09-28.md.
--
-- Две разные величины, их нельзя смешивать:
--   • raw_spp_*        — поле `spp`, которое WB прислал в каждом заказе (целое, %);
--   • effective_spp_pct — экономическая скидка покупателя SKU × день:
--       100 × (1 − Σ finished_price × quantity / Σ price_with_disc × quantity).
--     Именно она соответствует колонке AB Юнитки: AA = Σ price_with_disc·q / Σ q, AC = AA × (1 − AB),
--     и при AB = effective_spp_pct получается Σq × AC = Σ finished_price·q — сумма, уплаченная покупателями.
--
-- Популяция = та же, что у AA/FACT_ORDERS в wb_mart.V_UNITKA_DAILY_FACT: все строки V_WB_ORDERS за день
-- заказа, ВКЛЮЧАЯ отменённые, веса — quantity. date_msk = _order_date (дата заказа WB, МСК).
-- Источник один: spp_source = 'ORDER_SPP_ACTUAL'. Никакого fallback (выкупы, последнее известное,
-- ручной ввод, витрина, оценка). Отрицательная effective_spp_pct (рассрочка с наценкой) НЕ обрезается:
-- правило записи в AB — MAX(effective, 0) — принадлежит будущему writer, не этому слою.
-- internal_sku — атрибут из evetis_ref.REF_SKU_CHANNEL_MAP (действующая на дату привязка, иначе
-- текущая, иначе последняя); ключом не служит, строки без привязки не теряются (internal_sku = NULL).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SPP_DAILY`
OPTIONS (description = "SPP-2. СПП WB по заказам, SKU × день заказа (МСК). raw_spp_* — поле spp из Orders API; effective_spp_pct = 100 × (1 − Σ finished_price / Σ price_with_disc) — скидка покупателя, соответствует AB Юнитки. Популяция как у AA Юнитки: все заказы дня, включая отменённые. Строка только при наличии заказов Orders API. spp_source = ORDER_SPP_ACTUAL, без fallback. spp_status: OK / ZERO_SPP / NEGATIVE_MARKUP / PRICE_DATA_MISSING (price_missing_rows > 0: цена или сумма покупателя отсутствует — значение не подставляется). Отрицательное значение (рассрочка с наценкой) не обрезается.")
AS
WITH o AS (
  SELECT
    _order_date AS date_msk,
    SAFE_CAST(wb_nm_id AS INT64) AS nm_id,
    IFNULL(SAFE_CAST(quantity AS INT64), 0) AS qty,
    LOWER(IFNULL(is_cancel, '')) = 'true' AS is_cancel,
    SAFE_CAST(spp AS FLOAT64) AS spp,
    SAFE_CAST(REPLACE(price_with_disc, ',', '.') AS NUMERIC) AS pwd,
    SAFE_CAST(REPLACE(finished_price, ',', '.') AS NUMERIC) AS fp
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_ORDERS`
  WHERE _order_date IS NOT NULL
),
agg AS (
  SELECT
    date_msk,
    nm_id,
    SUM(qty) AS orders_qty,
    SUM(IF(is_cancel, qty, 0)) AS cancelled_orders_qty,
    SUM(IF(spp IS NOT NULL, qty, 0)) AS orders_with_spp,
    AVG(spp) AS raw_spp_avg_pct,
    ARRAY_AGG(spp IGNORE NULLS ORDER BY spp) AS spp_sorted,
    MIN(spp) AS raw_spp_min_pct,
    MAX(spp) AS raw_spp_max_pct,
    SUM(IF(spp = 0, qty, 0)) AS zero_spp_orders,
    SUM(pwd * qty) AS sum_price_with_disc,
    SUM(fp * qty) AS sum_finished_price,
    SUM(IF(fp > pwd, qty, 0)) AS neg_markup_orders,
    COUNTIF(pwd IS NULL OR pwd <= 0 OR fp IS NULL) AS rows_price_missing
  FROM o
  GROUP BY date_msk, nm_id
),
calc AS (
  SELECT
    agg.*,
    IF(ARRAY_LENGTH(spp_sorted) = 0, NULL,
       (spp_sorted[OFFSET(DIV(ARRAY_LENGTH(spp_sorted) - 1, 2))] + spp_sorted[OFFSET(DIV(ARRAY_LENGTH(spp_sorted), 2))]) / 2
    ) AS raw_spp_median_pct,
    IF(rows_price_missing > 0 OR sum_price_with_disc IS NULL OR sum_price_with_disc <= 0 OR sum_finished_price IS NULL, NULL,
       CAST(100 * (1 - sum_finished_price / sum_price_with_disc) AS FLOAT64)
    ) AS effective_spp_pct
  FROM agg
),
map AS (
  SELECT SAFE_CAST(marketplace_sku AS INT64) AS nm_id, internal_sku, valid_from, valid_to, is_current
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'WB'
),
mapped AS (
  SELECT
    calc.*,
    map.internal_sku
  FROM calc
  LEFT JOIN map ON map.nm_id = calc.nm_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY calc.date_msk, calc.nm_id
    ORDER BY
      IF(calc.date_msk BETWEEN IFNULL(map.valid_from, DATE '1900-01-01') AND IFNULL(map.valid_to, DATE '9999-12-31'), 0, 1),
      IF(map.is_current, 0, 1),
      map.valid_from DESC,
      map.internal_sku
  ) = 1
)
SELECT
  date_msk,
  nm_id,
  internal_sku,
  orders_qty,
  cancelled_orders_qty,
  orders_with_spp,
  raw_spp_avg_pct,
  raw_spp_median_pct,
  raw_spp_min_pct,
  raw_spp_max_pct,
  zero_spp_orders,
  sum_price_with_disc,
  sum_finished_price,
  effective_spp_pct,
  neg_markup_orders,
  rows_price_missing AS price_missing_rows,
  'ORDER_SPP_ACTUAL' AS spp_source,
  CASE
    WHEN effective_spp_pct IS NULL THEN 'PRICE_DATA_MISSING'
    WHEN effective_spp_pct < 0 THEN 'NEGATIVE_MARKUP'
    WHEN ROUND(effective_spp_pct, 1) = 0 THEN 'ZERO_SPP'
    ELSE 'OK'
  END AS spp_status
FROM mapped
