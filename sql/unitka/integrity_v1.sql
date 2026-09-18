-- ============================================================================
-- UNITKA INTEGRITY GUARD V1 — слой фактов целостности (Phase 1C1 → 1C2A, 18.09.2026).
--
-- СТАТУС: ТОЛЬКО ИСХОДНИК В GIT. В BigQuery НЕ применён. Применение — отдельный
-- гейт владельца (Phase 1C2B), см. docs/UNITKA_INTEGRITY_GUARD_V1.md.
--
-- Архитектура (Phase 1B, вариант C-lite): здесь только ФАКТЫ на грейне SKU × день.
-- Серьёзность (ERROR/WARNING/…) решает TypeScript (cloud/src/loaders/unitka/integrity.ts).
-- Вью ничего не пишут и не создают таблиц.
--
-- 🔴 НИ ОДНА ВЬЮ ЭТОГО ФАЙЛА НЕ ЧИТАЕТ evetis_ref (Phase 1C1B, решение владельца). Runtime-учётки
-- Unitka (sa-loaders-*) читают только wb_raw и wb_mart. Канонический COGS приходит из ФИЗИЧЕСКОЙ
-- копии wb_mart.UNITKA_COGS_EFFECTIVE, которую публикует sa-unitka-cogs-pub
-- (sql/unitka/cogs_publication_v1.sql). Проверяется тестом cloud/test/unitka_integrity_sql.test.ts.
-- Порядок применения: сначала cogs_publication_v1.sql (§1 таблицы), затем этот файл.
--
-- Семантика NULL (НЕ схлопывать в 0):
--   factual_order_price NULL  = в FACT_ORDERS нет строк за день, цены НЕТ (не 0 ₽);
--   canonical_cogs NULL       = в снимке нет ровно одного интервала (0 или >1), а не COGS = 0;
--   orders_funnel NULL        = у воронки НЕТ строки за день (≠ подтверждённый ноль);
--   fact_order_rows/qty NULL  = строк заказов нет; для класса расхождения это 0 заказов в Orders API;
--   storage_value NULL        = дата не покрыта отчётом хранения (GAP); 0 при покрытой дате —
--                               легитимный «хранения не начислено».
--
-- 🔴 observed_price_diagnostic = OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE.
--    Цена продавца из наблюдателя цен (RAW_WB_PRICES.seller_effective_price) — ТОЛЬКО
--    диагностика. Она НИКОГДА не подставляется в фактическую цену заказа и не участвует
--    ни в одном финансовом расчёте.
-- ============================================================================

-- ─── 1. Факты целостности SKU × день (месяц LAST_CLOSED_DATE, до LCD включительно) ───
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_INTEGRITY` AS
WITH
lcd AS (
  SELECT last_closed_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
b AS (
  -- Границы берутся ТАК ЖЕ, как в V_UNITKA_DAILY_FACT: месяц LCD. Своих календарных констант нет.
  SELECT DATE_TRUNC(last_closed_date, MONTH) AS d1, last_closed_date AS d2 FROM lcd
),
-- Ровно то, что Engine пишет в лист: Guard проверяет записываемые факты, а не свою копию логики.
f AS (
  SELECT nm_id, date_msk AS day, orders, cancels, price, storage, orders_source
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT`
),
ref AS (
  SELECT nm_id, internal_sku, product_name_short
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active
),
fu AS (
  SELECT nm_id, date_msk AS day, MAX(orders_count) AS orders_count
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, b
  WHERE date_msk BETWEEN b.d1 AND b.d2
  GROUP BY 1, 2
),
-- Та же жёсткая граница XLSX-бэкфилла, что в V_UNITKA_DAILY_FACT (решение владельца 11.09).
bf AS (
  SELECT nm_id, date_msk AS day, orders_count
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
fo AS (
  SELECT nm_id, order_date AS day, COUNT(*) AS fact_order_rows, SUM(quantity) AS fact_order_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, b
  WHERE order_date BETWEEN b.d1 AND b.d2
  GROUP BY 1, 2
),
-- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE: последнее prod-наблюдение за сутки МСК.
obs AS (
  SELECT nm_id, DATE(observed_at, 'Europe/Moscow') AS day,
         ARRAY_AGG(STRUCT(seller_effective_price AS price, observed_at) ORDER BY observed_at DESC LIMIT 1)[OFFSET(0)] AS o
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`, b
  WHERE environment = 'prod'
    AND observed_at >= TIMESTAMP(b.d1, 'Europe/Moscow')
    AND observed_at <  TIMESTAMP(DATE_ADD(b.d2, INTERVAL 1 DAY), 'Europe/Moscow')
  GROUP BY 1, 2
),
sd AS (
  SELECT DISTINCT date_msk AS day
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, b
  WHERE date_msk BETWEEN b.d1 AND b.d2
),
x AS (
  SELECT
    f.*,
    COALESCE(fu.orders_count, bf.orders_count) AS orders_funnel,   -- NULL = строки воронки нет
    fo.fact_order_rows, fo.fact_order_qty,
    obs.o.price AS observed_price_diagnostic,
    obs.o.observed_at AS observed_price_at,
    sd.day IS NOT NULL AS storage_date_covered
  FROM f
  LEFT JOIN fu  USING (nm_id, day)
  LEFT JOIN bf  USING (nm_id, day)
  LEFT JOIN fo  USING (nm_id, day)
  LEFT JOIN obs USING (nm_id, day)
  LEFT JOIN sd  USING (day)
)
SELECT
  'WB'                          AS marketplace,
  x.nm_id,
  ref.internal_sku,
  ref.product_name_short        AS product_name,
  x.day,
  lcd.last_closed_date,
  x.orders                      AS orders_unitka,        -- Q, как пишет Engine
  x.cancels                     AS cancels_unitka,       -- S, как пишет Engine
  x.orders_source,
  x.price                       AS factual_order_price,  -- NULL ≠ 0
  x.orders_funnel,
  x.fact_order_rows,
  x.fact_order_qty,
  x.observed_price_diagnostic,                           -- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE
  x.observed_price_at,
  x.storage                     AS storage_value,
  x.storage_date_covered,
  CASE
    WHEN x.price IS NOT NULL THEN 'PRESENT'
    WHEN IFNULL(x.orders, 0) > 0 OR IFNULL(x.cancels, 0) > 0 THEN 'MISSING_WITH_ACTIVITY'
    ELSE 'MISSING_NO_ACTIVITY'
  END                           AS price_state,
  CASE
    WHEN x.orders_funnel IS NULL THEN 'NO_FUNNEL_ROW'
    WHEN x.orders_funnel = IFNULL(x.fact_order_qty, 0) THEN 'EXACT'
    WHEN x.orders_funnel > 0 AND IFNULL(x.fact_order_qty, 0) = 0 THEN 'ONLY_FUNNEL'
    WHEN x.orders_funnel = 0 AND IFNULL(x.fact_order_qty, 0) > 0 THEN 'ONLY_FACT'
    WHEN x.orders_funnel > IFNULL(x.fact_order_qty, 0) THEN 'FUNNEL_GT_FACT'
    ELSE 'FACT_GT_FUNNEL'
  END                           AS divergence_class
FROM x
CROSS JOIN lcd
LEFT JOIN ref USING (nm_id);

-- ─── 2. Канонический COGS на SKU × день — из ФИЗИЧЕСКОЙ копии (без evetis_ref) ───────
-- Источник: wb_mart.UNITKA_COGS_EFFECTIVE (публикует sp_publish_unitka_cogs раз в час).
-- Контракт резолвера (sql/ref/pr_ref_cogs_history.sql): событию должен подходить РОВНО один
-- интервал; 0 или >1 ⇒ canonical_cogs NULL (fail-closed), НИКОГДА 0.
-- snapshot_published_at / snapshot_run_id — одинаковы у всех строк; по ним Engine судит о свежести
-- (COGS_SNAPSHOT_STALE при возрасте > 26 ч). Пустая копия ⇒ snapshot_published_at NULL ⇒
-- COGS_SNAPSHOT_UNAVAILABLE, а НЕ «канона нет»: «канона нет» бывает только при свежей копии.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_COGS_CANONICAL` AS
WITH
b AS (
  SELECT DATE_TRUNC(last_closed_date, MONTH) AS d1, last_closed_date AS d2
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
ref AS (
  SELECT nm_id, internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active
),
days AS (SELECT d AS day FROM b, UNNEST(GENERATE_DATE_ARRAY(b.d1, b.d2)) AS d),
c AS (
  SELECT internal_sku, effective_from, effective_to, product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE`
),
snap AS (
  SELECT MAX(published_at) AS published_at, ANY_VALUE(publish_run_id) AS run_id
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE`
)
SELECT
  'WB'                                                   AS marketplace,
  ref.nm_id,
  ref.internal_sku,
  days.day,
  COUNT(c.internal_sku)                                  AS cogs_interval_count,
  IF(COUNT(c.internal_sku) = 1, MAX(c.product_cogs_rub), NULL) AS canonical_cogs,  -- NULL ≠ 0
  ANY_VALUE(snap.published_at)                           AS snapshot_published_at,
  ANY_VALUE(snap.run_id)                                 AS snapshot_run_id
FROM ref
CROSS JOIN days
CROSS JOIN snap
LEFT JOIN c
  ON c.internal_sku = ref.internal_sku
 AND days.day >= c.effective_from
 AND (c.effective_to IS NULL OR days.day <= c.effective_to)
GROUP BY ref.nm_id, ref.internal_sku, days.day;
