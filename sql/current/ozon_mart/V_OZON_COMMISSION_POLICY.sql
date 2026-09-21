-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_COMMISSION_POLICY (VIEW)
-- Git-first object (Gate 8, 2026-09-21): not in production until deployed. Rules:
-- sql/current/README.md. Metadata: MANIFEST.json.
--
-- ДЕЙСТВУЮЩИЙ ТАРИФ КОМИССИИ Ozon, датированный интервалами. Нужен там, где фактического
-- начисления ЕЩЁ НЕТ: Ozon публикует финансовую операцию ровно через 14 суток после её
-- даты (замер Gate 8: p50=p95=max=14), а сама операция возникает через 4-25 суток после
-- заказа. Итого продажа становится видимой в среднем через 18 суток, максимум 36. До этого
-- момента комиссия отсутствует НЕ потому, что её не будет, а потому, что её ещё не напечатали.
--
-- Тариф — детерминированная величина площадки, а не статистика. Поэтому здесь НЕТ средних:
-- ставка берётся из двух свидетельств, и оба проверяемы.
--   ACCRUAL_OBSERVED    — ставка, фактически применённая к заказам этих суток
--                         (RAW_OZON_FINANCE_ACCRUAL.commission_ratio по дате заказа).
--                         Историческая правда, но отстаёт на срок публикации.
--   PRICE_API_SNAPSHOT  — sales_percent_fbo из ежедневного снимка тарифов продавца.
--                         Смотрит ВПЕРЁД: знает сегодняшнюю ставку до первого начисления.
-- Снимок имеет приоритет над наблюдением: он свежее. Там, где нет ни одного свидетельства,
-- ставка тянется вперёд от последнего известного (LAST_VALUE IGNORE NULLS).
--
-- Сверка двух источников на пересечении (с 2026-09-06): обе дают 0,52. Лестница ставок по
-- дате ЗАКАЗА: 0,18 → 0,23 → 0,275 → 0,30 → 0,34 → 0,39 → 0,41 → 0,52. Последний переход
-- 2026-08-27/28: 27.08 смешанные сутки (6 заказов по 0,41, 1 по 0,52), с 28.08 — только 0,52.
--
-- БАЗА — ЦЕНА ПРОДАВЦА (seller_base_price_rub), доказано на 2002 строках из 2003:
-- commission_rub / seller_base_price_rub = -commission_ratio. Цена покупателя базой НЕ является.
--
-- Ставка едина для всех SKU (одна категория): из 456 суток только 3 содержат более одной
-- ставки, и все три — сутки перехода. Зерно всё равно оставлено поSKU-ным: так тариф,
-- разошедшийся по товарам, будет виден сразу, а не спрятан в среднем.
--
-- ВЫКУПЫ СНГ здесь не обслуживаются: у выкупа агентского вознаграждения не существует как
-- факта, и применять к нему тариф запрещено (см. FCT_OZON_SKU_PNL_DAILY, Gate 5K).
-- Internal dependencies: none.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_COMMISSION_POLICY`
OPTIONS (description = "Действующий тариф комиссии Ozon, датированный интервалами (effective_from/effective_to) по internal_sku. Детерминированный тариф площадки, НЕ статистика. Источники: PRICE_API_SNAPSHOT (sales_percent_fbo из ежедневного снимка тарифов, смотрит вперёд) имеет приоритет над ACCRUAL_OBSERVED (commission_ratio фактических начислений по дате заказа, отстаёт на срок публикации). База ставки - цена продавца seller_base_price_rub (доказано на 2002 строках из 2003). Нужен для провизорной оценки комиссии там, где начисление ещё не опубликовано: Ozon публикует операцию через 14 суток после её даты, операция возникает через 4-25 суток после заказа. К выкупам СНГ тариф НЕ применяется: агентского вознаграждения у выкупа не существует.")
AS
WITH map AS (
  SELECT DISTINCT internal_sku, offer_id, marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'),
snap AS (
  SELECT c.snapshot_date d, m.internal_sku, c.value_num / 100 rate
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS` c
  JOIN map m ON m.offer_id = c.offer_id
  WHERE c.sale_scheme = 'FBO' AND c.commission_component = 'SALES_PERCENT'
    AND c.unit = 'PERCENT' AND c.value_num IS NOT NULL
  GROUP BY 1, 2, 3),
posting AS (
  SELECT DISTINCT posting_number, sku, order_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`),
acc_raw AS (
  SELECT p.order_date d, m.internal_sku, f.commission_ratio rate, COUNT(*) n
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN posting p USING (posting_number)
  JOIN map m ON m.marketplace_sku = f.sku
  WHERE f.commission_ratio IS NOT NULL
  GROUP BY 1, 2, 3),
-- сутки перехода содержат обе ставки: берём преобладающую, а не первую попавшуюся
acc AS (
  SELECT d, internal_sku, rate FROM (
    SELECT d, internal_sku, rate,
           ROW_NUMBER() OVER (PARTITION BY d, internal_sku ORDER BY n DESC, rate DESC) rn
    FROM acc_raw)
  WHERE rn = 1),
-- ставка площадки этих суток: тариф единый (из 456 суток только 3 содержат более одной
-- ставки, и все три - сутки перехода), поэтому модальная ставка дня - это тариф дня
mkt AS (
  SELECT d, rate, src FROM (
    SELECT d, rate, src, ROW_NUMBER() OVER (PARTITION BY d ORDER BY pri, n DESC, rate DESC) rn
    FROM (
      SELECT d, rate, 'PRICE_API_SNAPSHOT' src, 0 pri, COUNT(*) n FROM snap GROUP BY 1, 2
      UNION ALL
      SELECT d, rate, 'ACCRUAL_OBSERVED' src, 1 pri, SUM(n) n FROM acc_raw GROUP BY 1, 2))
  WHERE rn = 1),
cal AS (
  SELECT d, m.internal_sku
  FROM UNNEST(GENERATE_DATE_ARRAY(
        (SELECT LEAST(MIN(d), (SELECT MIN(d) FROM snap)) FROM acc),
        GREATEST(CURRENT_DATE('Europe/Moscow'), (SELECT MAX(d) FROM snap)))) d
  CROSS JOIN map m),
obs AS (
  SELECT c.d, c.internal_sku,
    COALESCE(s.rate, a.rate) own_rate,
    CASE WHEN s.rate IS NOT NULL THEN 'PRICE_API_SNAPSHOT'
         WHEN a.rate IS NOT NULL THEN 'ACCRUAL_OBSERVED' END own_src,
    mk.rate mkt_rate, mk.src mkt_src
  FROM cal c
  LEFT JOIN snap s USING (d, internal_sku)
  LEFT JOIN acc  a USING (d, internal_sku)
  LEFT JOIN mkt mk ON mk.d = c.d),
-- сутки без свидетельства наследуют последнее известное: отдельно по SKU, отдельно по площадке
filled AS (
  SELECT d, internal_sku,
    LAST_VALUE(own_rate IGNORE NULLS) OVER w own_rate,
    LAST_VALUE(own_src  IGNORE NULLS) OVER w own_src,
    LAST_VALUE(IF(own_rate IS NULL, NULL, d) IGNORE NULLS) OVER w own_at,
    LAST_VALUE(mkt_rate IGNORE NULLS) OVER w mkt_rate,
    LAST_VALUE(mkt_src  IGNORE NULLS) OVER w mkt_src,
    LAST_VALUE(IF(mkt_rate IS NULL, NULL, d) IGNORE NULLS) OVER w mkt_at
  FROM obs
  WINDOW w AS (PARTITION BY internal_sku ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)),
-- побеждает БОЛЕЕ СВЕЖЕЕ свидетельство. Иначе редкий SKU, не продававшийся с мая и не попавший
-- в снимок, навсегда застрял бы на ставке 0,41 и занижал бы оценку комиссии после перехода.
best AS (
  SELECT d, internal_sku,
    IF(own_at IS NOT NULL AND (mkt_at IS NULL OR own_at >= mkt_at), own_rate, mkt_rate) rate,
    IF(own_at IS NOT NULL AND (mkt_at IS NULL OR own_at >= mkt_at), own_src,  mkt_src)  src,
    IF(own_at IS NOT NULL AND (mkt_at IS NULL OR own_at >= mkt_at), 'SKU',    'MARKETPLACE') scope,
    IF(own_at IS NOT NULL AND (mkt_at IS NULL OR own_at >= mkt_at), own_at,   mkt_at)   evidence_date
  FROM filled),
marked AS (
  SELECT *, IF(rate IS DISTINCT FROM LAG(rate) OVER (PARTITION BY internal_sku ORDER BY d), 1, 0) chg
  FROM best WHERE rate IS NOT NULL),
grp AS (
  SELECT *, SUM(chg) OVER (PARTITION BY internal_sku ORDER BY d ROWS UNBOUNDED PRECEDING) g
  FROM marked)
SELECT
  internal_sku,
  MIN(d) effective_from,
  IF(MAX(d) = MAX(MAX(d)) OVER (PARTITION BY internal_sku), DATE '9999-12-31', MAX(d)) effective_to,
  ANY_VALUE(rate) commission_rate,
  'SELLER_BASE_PRICE' basis,
  -- источник и охват берутся у САМОГО СВЕЖЕГО свидетельства интервала, а не произвольного:
  -- интервал постоянной ставки может начаться наблюдением начисления и продолжиться снимком
  ARRAY_AGG(src ORDER BY d DESC LIMIT 1)[OFFSET(0)] rate_source,
  ARRAY_AGG(scope ORDER BY d DESC LIMIT 1)[OFFSET(0)] rate_scope,
  CONCAT('свидетельство ', CAST(MAX(evidence_date) AS STRING), '; ',
         ARRAY_AGG(src ORDER BY d DESC LIMIT 1)[OFFSET(0)], '/',
         ARRAY_AGG(scope ORDER BY d DESC LIMIT 1)[OFFSET(0)],
         '; ставка ', CAST(ROUND(ANY_VALUE(rate) * 100, 3) AS STRING), '%') evidence
FROM grp
GROUP BY internal_sku, g
