-- ============================================================================
-- UNITKA FINANCIAL INTEGRITY V1 — подготовленный слой СВЕРКИ (19.09.2026).
--
-- СТАТУС: ТОЛЬКО ИСХОДНИК В GIT. В BigQuery НЕ применён. Применение — отдельный гейт владельца.
-- Док: docs/UNITKA_FIN_INTEGRITY_V1.md. Откат: sql/unitka/reconcile_v1_rollback.sql (DROP новых вью).
--
-- Существующие вью V_UNITKA_DAILY_FACT / V_UNITKA_INTEGRITY / V_UNITKA_COGS_CANONICAL НЕ меняются:
-- Engine в режиме UNITKA_RECONCILE_MODE=off читает их как раньше. Новый слой читается только в режимах
-- observe / write. Все вью — только чтение; таблиц не создают; evetis_ref НЕ читают (как integrity_v1.sql).
--
-- 1. ОКНО СВЕРКИ — 35 календарных дней [LCD−34, LCD], через границы месяцев (решение владельца), но не раньше
--    ЭПОХИ СВЕРКИ 2026-09-01 (граница миграции: первый месяц под управлением Engine).
--    Константа окна — ОДНА, в V_UNITKA_RECON_WINDOW; остальные вью берут границы оттуда.
--
-- 2. ЦЕНА — детерминированное разрешение с происхождением (решение владельца):
--      ORDERS_API       — FACT_ORDERS дал хотя бы одну строку заказа за SKU-день: средневзвешенная price_with_disc.
--                         ОСНОВНОЙ источник. Остаётся основным и при расхождении счётчиков (расхождение — статус,
--                         а не повод пересчитать цену).
--      FUNNEL_FALLBACK  — Orders API не дал НИ ОДНОЙ строки за SKU-день, счётчик заказов Unitka взят из воронки
--                         (orders_source = FUNNEL_API), воронка даёт orders_count > 0 и orders_sum_rub > 0:
--                         цена = orders_sum_rub / orders_count. Та же ценовая концепция (цена продавца после скидки
--                         продавца, до СПП): на 91 SKU-дне сентября 2026 с равными счётчиками суммы совпали
--                         (134 051,00 ↔ 134 050,46 ₽; воронка округляет заказ до рубля).
--      NULL             — не разрешено. НИКОГДА: соседний день, цена карточки, средняя, MTD, другой SKU, константа.
--    Деление суммы воронки при НЕСОГЛАСНЫХ счётчиках (Orders API дал строки, но их число ≠ воронке) запрещено:
--    в этом случае цена остаётся ценой Orders API, а строка получает статус LATE_DATA / DATA_ERROR (integrity.ts).
--
-- 3. SKU: все WB-SKU справочника, а не только активные: прошлый месяц окна может держать блок SKU, выведенного с
--    1-го числа. sku_active отдаётся явно — правило BLOCK_MISSING месяца LCD Engine применяет по нему.
-- ============================================================================

-- ─── 0. Окно сверки и ЭПОХА СВЕРКИ ──────────────────────────────────────────
-- RECONCILIATION_EPOCH = 2026-09-01 (решение владельца 20.09.2026) — граница миграции домена, которым управляет Engine.
-- Сентябрь 2026 — первый месяц под Engine (Stage E1, 12.09.2026). Август 2026 и раньше писали прежние процессы
-- (Apps Script, ручной ввод) из других источников; новая сверка эти месяцы не меняет НИКОГДА.
-- Эпоха — явный параметр домена, заданный ОДИН раз (CTE cfg), а не дата, зашитая в логику запросов:
--     window_from = max(начало скользящих 35 дней, reconciliation_epoch)
-- Остальные вью слоя берут границы только отсюда. То же значение — в reconcile.ts (RECONCILIATION_EPOCH);
-- Engine сверяет оба при каждом прогоне (RECON_WINDOW_INCONSISTENT) и отвергает строки раньше эпохи (RECON_BEFORE_EPOCH).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW` AS
WITH cfg AS (
  SELECT DATE '2026-09-01' AS reconciliation_epoch, 35 AS window_days
)
SELECT
  l.last_closed_date,
  GREATEST(DATE_SUB(l.last_closed_date, INTERVAL cfg.window_days - 1 DAY), cfg.reconciliation_epoch) AS window_from,
  l.last_closed_date                                                                        AS window_to,
  cfg.window_days,
  cfg.reconciliation_epoch,
  DATE_SUB(l.last_closed_date, INTERVAL cfg.window_days - 1 DAY)                            AS rolling_window_from
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE` AS l
CROSS JOIN cfg;

-- ─── 1. Дневной факт nm_id × дата за окно сверки, с происхождением цены ─────
-- Колонки V_UNITKA_DAILY_FACT сохранены 1-в-1 (views..cancels_source) — Engine читает их теми же именами.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT` AS
WITH w AS (
  SELECT window_from AS d1, window_to AS d2 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`
),
days AS (SELECT d FROM w, UNNEST(GENERATE_DATE_ARRAY(w.d1, w.d2)) AS d),
sku AS (
  SELECT nm_id, LOGICAL_OR(active) AS sku_active
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND nm_id IS NOT NULL
  GROUP BY nm_id
),
g AS (SELECT s.nm_id, s.sku_active, d.d FROM sku s CROSS JOIN days d),
o AS (
  SELECT nm_id, order_date AS d, SUM(quantity) AS gross, SUM(IF(is_cancel, quantity, 0)) AS canc,
         -- Отмена В ДЕНЬ ЗАКАЗА: воронка такой заказ в orders_count не считает (замер 04–18.09.2026: 4 из 4).
         -- Только диагностика расхождения счётчиков; в Q / S / цену листа не входит.
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) = order_date, quantity, 0)) AS same_day_canc,
         SAFE_DIVIDE(SUM(price_with_disc * quantity), NULLIF(SUM(quantity), 0)) AS price,
         MAX(built_at) AS built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, w
  WHERE order_date BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
m AS (
  SELECT nm_id, day AS d, SUM(views) AS views, SUM(ad_spend) AS ads
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`, w
  WHERE day BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
f AS (
  SELECT nm_id, date_msk AS d, MAX(open_card_count) AS opens, MAX(add_to_cart_count) AS carts,
         MAX(orders_count) AS forders, MAX(orders_sum_rub) AS fsum, MAX(observed_at) AS observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, w
  WHERE date_msk BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
-- Та же жёсткая граница XLSX-бэкфилла, что в V_UNITKA_DAILY_FACT (решение владельца 11.09).
bf AS (
  SELECT nm_id, date_msk AS d, open_card_count AS opens, add_to_cart_count AS carts,
         orders_count AS forders, cancel_count AS canc
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
sd AS (SELECT DISTINCT snapshot_date AS d FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, w WHERE snapshot_date BETWEEN w.d1 AND w.d2),
st AS (
  SELECT nm_id, snapshot_date AS d, SUM(quantity) AS stock
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, w
  WHERE snapshot_date BETWEEN w.d1 AND w.d2
  GROUP BY 1, 2
),
pd AS (SELECT DISTINCT date_msk AS d FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, w WHERE date_msk BETWEEN w.d1 AND w.d2),
ps AS (
  SELECT nm_id, date_msk AS d, storage_rub_exact AS storage, observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, w
  WHERE date_msk BETWEEN w.d1 AND w.d2
),
x AS (
  SELECT
    g.nm_id, g.sku_active, g.d,
    IFNULL(m.views, 0)                                   AS views,
    COALESCE(f.opens, bf.opens)                          AS opens,
    COALESCE(f.carts, bf.carts)                          AS carts,
    COALESCE(f.forders, bf.forders, o.gross, 0)          AS orders,
    COALESCE(bf.canc, o.canc, 0)                         AS cancels,
    IF(sd.d IS NULL, NULL, IFNULL(st.stock, 0))          AS stock,
    ROUND(IFNULL(m.ads, 0), 2)                           AS ads_in,
    IF(pd.d IS NULL, NULL, ROUND(IFNULL(ps.storage, 0), 2)) AS storage,
    CASE WHEN f.forders IS NOT NULL THEN 'FUNNEL_API'
         WHEN bf.forders IS NOT NULL THEN 'XLSX_BACKFILL'
         ELSE 'ORDERS_API' END                           AS orders_source,
    CASE WHEN bf.canc IS NOT NULL THEN 'XLSX_BACKFILL'
         ELSE 'PROXY_FACT_ORDERS' END                    AS cancels_source,
    o.price                                              AS orders_api_price,
    IFNULL(o.gross, 0)                                   AS fact_order_qty,
    IFNULL(o.same_day_canc, 0)                           AS same_day_cancel_qty,
    f.forders                                            AS funnel_orders,
    f.fsum                                               AS funnel_orders_sum,
    f.observed_at                                        AS funnel_observed_at,
    o.built_at                                           AS orders_built_at,
    ps.observed_at                                       AS storage_observed_at
  FROM g
  LEFT JOIN o  ON o.nm_id  = g.nm_id AND o.d  = g.d
  LEFT JOIN m  ON m.nm_id  = g.nm_id AND m.d  = g.d
  LEFT JOIN f  ON f.nm_id  = g.nm_id AND f.d  = g.d
  LEFT JOIN bf ON bf.nm_id = g.nm_id AND bf.d = g.d
  LEFT JOIN st ON st.nm_id = g.nm_id AND st.d = g.d
  LEFT JOIN ps ON ps.nm_id = g.nm_id AND ps.d = g.d
  LEFT JOIN sd ON sd.d = g.d
  LEFT JOIN pd ON pd.d = g.d
),
p AS (
  SELECT
    x.*,
    -- FUNNEL_FALLBACK разрешён ТОЛЬКО так: Orders API пуст, счётчик Unitka — воронка, сумма воронки положительна.
    (x.orders_api_price IS NULL AND x.fact_order_qty = 0 AND x.orders_source = 'FUNNEL_API'
      AND IFNULL(x.funnel_orders, 0) > 0 AND x.funnel_orders = x.orders AND IFNULL(x.funnel_orders_sum, 0) > 0) AS funnel_fallback_ok
  FROM x
)
SELECT
  nm_id,
  d AS date_msk,
  views, opens, carts, orders, cancels, stock, ads_in,
  CASE
    WHEN orders_api_price IS NOT NULL THEN ROUND(orders_api_price, 2)
    WHEN funnel_fallback_ok THEN ROUND(SAFE_DIVIDE(funnel_orders_sum, funnel_orders), 2)
  END                                                    AS price,
  storage,
  orders_source,
  cancels_source,
  CASE
    WHEN orders_api_price IS NOT NULL THEN 'ORDERS_API'
    WHEN funnel_fallback_ok THEN 'FUNNEL_FALLBACK'
  END                                                    AS price_source,
  fact_order_qty,
  funnel_orders,
  funnel_orders_sum,
  sku_active,
  FORMAT_DATE('%Y-%m', d)                                AS month_key,
  funnel_observed_at,
  orders_built_at,
  storage_observed_at,
  same_day_cancel_qty
FROM p;

-- ─── 2. Факты целостности SKU × день за окно сверки ─────────────────────────
-- Надмножество V_UNITKA_INTEGRITY: те же колонки + происхождение цены, сумма воронки, покрытие снимка остатков.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_INTEGRITY` AS
WITH
lcd AS (SELECT last_closed_date, window_from, window_to FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`),
f AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT`),
ref AS (
  SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku, ANY_VALUE(product_name_short) AS product_name_short
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' GROUP BY nm_id
),
fo AS (
  SELECT nm_id, order_date AS day, COUNT(*) AS fact_order_rows
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, lcd
  WHERE order_date BETWEEN lcd.window_from AND lcd.window_to
  GROUP BY 1, 2
),
-- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE: последнее prod-наблюдение за сутки МСК. ТОЛЬКО диагностика.
obs AS (
  SELECT nm_id, DATE(observed_at, 'Europe/Moscow') AS day,
         ARRAY_AGG(STRUCT(seller_effective_price AS price, observed_at) ORDER BY observed_at DESC LIMIT 1)[OFFSET(0)] AS o
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`, lcd
  WHERE environment = 'prod'
    AND observed_at >= TIMESTAMP(lcd.window_from, 'Europe/Moscow')
    AND observed_at <  TIMESTAMP(DATE_ADD(lcd.window_to, INTERVAL 1 DAY), 'Europe/Moscow')
  GROUP BY 1, 2
)
SELECT
  'WB'                          AS marketplace,
  f.nm_id,
  ref.internal_sku,
  ref.product_name_short        AS product_name,
  f.date_msk                    AS day,
  lcd.last_closed_date,
  f.orders                      AS orders_unitka,
  f.cancels                     AS cancels_unitka,
  f.orders_source,
  f.price                       AS factual_order_price,   -- NULL ≠ 0
  f.price_source,                                         -- ORDERS_API | FUNNEL_FALLBACK | NULL
  f.funnel_orders               AS orders_funnel,         -- NULL = строки воронки нет (бэкфилл — отдельно, в orders_source)
  fo.fact_order_rows,
  NULLIF(f.fact_order_qty, 0)   AS fact_order_qty,
  f.funnel_orders_sum,
  f.same_day_cancel_qty,                                  -- отмены дня заказа: воронка их в orders_count не считает
  obs.o.price                   AS observed_price_diagnostic,  -- OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE
  obs.o.observed_at             AS observed_price_at,
  f.storage                     AS storage_value,
  f.storage IS NOT NULL         AS storage_date_covered,
  f.stock IS NOT NULL           AS stock_date_covered,
  f.sku_active,
  CASE
    WHEN f.price IS NOT NULL THEN 'PRESENT'
    WHEN IFNULL(f.orders, 0) > 0 OR IFNULL(f.cancels, 0) > 0 THEN 'MISSING_WITH_ACTIVITY'
    ELSE 'MISSING_NO_ACTIVITY'
  END                           AS price_state,
  CASE
    WHEN f.orders_source != 'FUNNEL_API' THEN 'NO_FUNNEL_ROW'
    WHEN f.funnel_orders = f.fact_order_qty THEN 'EXACT'
    WHEN f.funnel_orders > 0 AND f.fact_order_qty = 0 THEN 'ONLY_FUNNEL'
    WHEN f.funnel_orders = 0 AND f.fact_order_qty > 0 THEN 'ONLY_FACT'
    WHEN f.funnel_orders > f.fact_order_qty THEN 'FUNNEL_GT_FACT'
    ELSE 'FACT_GT_FUNNEL'
  END                           AS divergence_class
FROM f
CROSS JOIN lcd
LEFT JOIN ref USING (nm_id)
LEFT JOIN fo  ON fo.nm_id = f.nm_id AND fo.day = f.date_msk
LEFT JOIN obs ON obs.nm_id = f.nm_id AND obs.day = f.date_msk;

-- ─── 3. Канонический COGS на SKU × день за окно сверки — из ФИЗИЧЕСКОЙ копии (без evetis_ref) ───
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_COGS_CANONICAL` AS
WITH
b AS (SELECT window_from AS d1, window_to AS d2 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`),
ref AS (
  SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND nm_id IS NOT NULL GROUP BY nm_id
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

-- ─── 4. Наблюдаемость: состояние целостности по двум последним прогонам ─────
-- Применять ПОСЛЕ infra apply (таблицы wb_ops.UNITKA_INTEGRITY_ISSUES и wb_ops.UNITKA_REPAIR_LEDGER — Terraform).
-- Состояния не схлопываются: DATA_ERROR / LATE_DATA / MANUAL_REQUIRED / NOT_AVAILABLE / WARNING считаются отдельно;
-- AUTO_REPAIRED — из журнала ремонта (только status = 'REPAIRED': лист подтвердил запись и проверка пройдена).
-- Финансовая действительность — ОТДЕЛЬНО от состояния: financially_invalid_sku_days считает financial_valid = FALSE
-- при любом состоянии (в т.ч. LATE_DATA с ещё не подтверждёнными деньгами).
-- Сколько бы ни было ожидаемых NOT_AVAILABLE и MANUAL_REQUIRED, автоматический контур «сломанным» они не делают:
-- financial_health смотрит ТОЛЬКО на DATA_ERROR; ручной ввод — отдельным флагом manual_input_pending.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_INTEGRITY_STATUS` AS
WITH runs AS (
  SELECT run_id, MAX(evaluated_at) AS evaluated_at
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_INTEGRITY_ISSUES`
  WHERE environment = 'prod' AND evaluated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 40 DAY)
  GROUP BY run_id
),
ranked AS (SELECT run_id, evaluated_at, ROW_NUMBER() OVER (ORDER BY evaluated_at DESC) AS rn FROM runs),
cur AS (
  SELECT i.* FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_INTEGRITY_ISSUES` i
  JOIN ranked r ON r.run_id = i.run_id AND r.rn = 1 WHERE i.issue_key IS NOT NULL
),
prev AS (
  SELECT i.issue_key FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_INTEGRITY_ISSUES` i
  JOIN ranked r ON r.run_id = i.run_id AND r.rn = 2 WHERE i.issue_key IS NOT NULL
),
rep AS (
  SELECT COUNT(*) AS repaired_cells, COUNT(DISTINCT CONCAT(CAST(nm_id AS STRING), '|', CAST(business_date AS STRING))) AS repaired_sku_days
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_REPAIR_LEDGER`
  WHERE status = 'REPAIRED' AND run_id = (SELECT run_id FROM ranked WHERE rn = 1)
),
-- Попытки ремонта без подтверждения за 7 дней (WRITE_FAILED — запись листа упала; APPLIED_UNVERIFIED — записано,
-- но не проверено). Это НЕ ремонты: в auto_repaired_* они не входят никогда.
unconf AS (
  SELECT COUNT(*) AS attempts
  FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_REPAIR_LEDGER`
  WHERE status IN ('WRITE_FAILED', 'APPLIED_UNVERIFIED') AND detected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
)
SELECT
  (SELECT run_id FROM ranked WHERE rn = 1)                                         AS run_id,
  (SELECT evaluated_at FROM ranked WHERE rn = 1)                                   AS evaluated_at,
  COUNTIF(cur.state = 'DATA_ERROR')                                                AS data_error,
  COUNTIF(cur.state = 'LATE_DATA')                                                 AS late_data,
  COUNTIF(cur.state = 'MANUAL_REQUIRED')                                           AS manual_required,
  COUNTIF(cur.state = 'NOT_AVAILABLE')                                             AS not_available,
  COUNTIF(cur.state = 'WARNING')                                                   AS warning,
  (SELECT repaired_cells FROM rep)                                                 AS auto_repaired_cells,
  (SELECT repaired_sku_days FROM rep)                                              AS auto_repaired_sku_days,
  COUNT(DISTINCT IF(cur.business_date IS NOT NULL AND cur.state IN ('DATA_ERROR', 'LATE_DATA', 'MANUAL_REQUIRED'),
                    CONCAT(CAST(cur.nm_id AS STRING), '|', CAST(cur.business_date AS STRING)), NULL)) AS affected_sku_days,
  COUNT(DISTINCT IF(cur.financial_valid = FALSE AND cur.business_date IS NOT NULL,
                    CONCAT(CAST(cur.nm_id AS STRING), '|', CAST(cur.business_date AS STRING)), NULL)) AS financially_invalid_sku_days,
  MIN(IF(cur.state = 'DATA_ERROR', cur.business_date, NULL))                       AS oldest_unresolved_data_error,
  MIN(IF(cur.state IN ('DATA_ERROR', 'LATE_DATA', 'MANUAL_REQUIRED'), cur.business_date, NULL)) AS oldest_unresolved_any,
  COUNTIF(cur.issue_key NOT IN (SELECT issue_key FROM prev))                       AS new_since_previous_run,
  (SELECT COUNT(*) FROM prev WHERE issue_key NOT IN (SELECT issue_key FROM cur))   AS resolved_since_previous_run,
  COUNTIF(cur.code = 'PRICE_FUNNEL_FALLBACK')                                      AS prices_from_funnel_fallback,
  COUNTIF(cur.code = 'PRICE_NOT_ON_SHEET')                                         AS repair_available_not_written,
  (SELECT attempts FROM unconf)                                                    AS repair_attempts_unconfirmed_7d,
  COUNTIF(cur.state = 'MANUAL_REQUIRED') > 0                                       AS manual_input_pending,
  IF(COUNTIF(cur.state = 'DATA_ERROR') = 0, 'OK', 'DATA_ERROR')                    AS financial_health
FROM cur;
