-- ============================================================================
-- UNITKA ENGINE v1 — подготовленный слой BigQuery (Stage E1, 12.09.2026).
-- Engine (cloud/src/loaders/unitka) читает ТОЛЬКО эти вью и не содержит бизнес-SQL.
-- Все вью — только чтение production-слоёв; таблиц не создают.
-- Док: docs/UNITKA_ENGINE_V1_DESIGN.md.
-- Применение: bq query --use_legacy_sql=false < sql/unitka/engine_v1_views.sql
-- Вью 1–5 применены 12.09.2026 (MCP BigQuery), вью 6 — после infra apply (см. её заголовок).
-- 13.09.2026: слой хранения (раздел 0) и вью 1, 3 переведены на V_WB_STORAGE_DAILY — применено в BQ;
-- этот файл сверен с live-определениями (INFORMATION_SCHEMA.VIEWS) 13.09.2026 — источник правды BigQuery.
-- Production-загрузчик хранения = Stage E4, commit 230c64c: Job wb-paid-storage-prod,
-- infra/terraform/wb_paid_storage_loader.tf (схема RAW_WB_PAID_STORAGE — там же).
-- ============================================================================

-- ============================================================================
-- UNITKA ENGINE v1 — подготовленный слой BigQuery (Stage E1, 12.09.2026).
-- Engine (cloud/src/loaders/unitka) читает ТОЛЬКО эти вью и не содержит бизнес-SQL.
-- Все вью — только чтение production-слоёв; таблиц не создают.
-- Док: docs/UNITKA_ENGINE_V1_DESIGN.md.
-- Применение: bq query --use_legacy_sql=false < sql/unitka/engine_v1_views.sql
-- ============================================================================

-- ─── 0. Слой платного хранения WB (wb_raw) ────────────────────────────────
-- Источник: wb_raw.RAW_WB_PAID_STORAGE (пишет Job wb-paid-storage-prod, Stage E4 / 230c64c).
-- V_WB_STORAGE_DAILY — на каждую дату берётся последнее наблюдение (observation_id по
-- MAX(observed_at)); с atomic window replace E4 на дату остаётся одно наблюдение — вью
-- совместима. storage_rub_exact — без округления (для V_UNITKA_DAILY_FACT), storage_rub — 4 знака.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` AS
WITH obs AS (
  SELECT date_msk, observation_id, MAX(observed_at) AS observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`
  GROUP BY date_msk, observation_id
),
latest AS (
  SELECT date_msk, observation_id FROM obs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY date_msk ORDER BY observed_at DESC, observation_id DESC) = 1
)
SELECT
  r.date_msk,
  r.nm_id,
  SUM(r.warehouse_price)           AS storage_rub_exact,
  ROUND(SUM(r.warehouse_price), 4) AS storage_rub,
  SUM(r.barcodes_count)            AS units_stored,
  COUNT(DISTINCT r.warehouse)      AS warehouses,
  MAX(r.observed_at)               AS observed_at,
  l.observation_id
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` r
JOIN latest l USING (date_msk, observation_id)
GROUP BY r.date_msk, r.nm_id, l.observation_id;
-- Покрытие по датам от первой загруженной до D-1 МСК: MISSING = пропуск (GAP), не ноль.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_COVERAGE` AS
WITH cal AS (
  SELECT d AS date_msk
  FROM UNNEST(GENERATE_DATE_ARRAY(
    (SELECT MIN(date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`),
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY))) AS d
)
SELECT
  c.date_msk,
  COUNT(DISTINCT s.nm_id)                    AS nm_with_data,
  ROUND(SUM(s.storage_rub_exact), 2)         AS storage_total_rub,
  ANY_VALUE(s.observation_id)                AS observation_id,
  MAX(s.observed_at)                         AS last_observed_at,
  IF(COUNT(s.nm_id) = 0, 'MISSING', 'OK')    AS status
FROM cal c
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY` s USING (date_msk)
GROUP BY c.date_msk;
-- Сверка суммы по SKU с фактом WB из финансового отчёта (storage_fee), допуск 1 ₽.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_RECONCILIATION` AS
WITH ps AS (
  SELECT date_msk, SUM(storage_rub_exact) AS storage_api
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`
  GROUP BY date_msk
),
fin AS (
  SELECT _rr_date AS date_msk,
         SUM(SAFE_CAST(REPLACE(REPLACE(storage_fee, ' ', ''), ',', '.') AS FLOAT64)) AS storage_finance
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  GROUP BY _rr_date
)
SELECT
  COALESCE(ps.date_msk, fin.date_msk)                       AS date_msk,
  ROUND(ps.storage_api, 2)                                  AS storage_api,
  ROUND(fin.storage_finance, 2)                             AS storage_finance,
  ROUND(IFNULL(ps.storage_api,0) - IFNULL(fin.storage_finance,0), 2) AS delta,
  CASE
    WHEN ps.storage_api IS NULL  THEN 'NO_API_DATA'
    WHEN fin.storage_finance IS NULL THEN 'NO_FINANCE_DATA'
    WHEN ABS(ps.storage_api - fin.storage_finance) <= 1 THEN 'OK'
    ELSE 'MISMATCH'
  END AS status
FROM ps FULL OUTER JOIN fin USING (date_msk);

-- ─── 1. Свежесть источников ────────────────────────────────────────────────
-- gating = TRUE у источников, без которых закрытый день не считается закрытым:
-- воронка (authoritative для переходов/корзин/заказов) и витрина (показы/реклама,
-- FACT_ORDERS → отмены-прокси и цена). Остатки, хранение, финансы не гейтят:
-- их пропуски в Master приняты как «GAP — пусто, не ноль» и дозаполняются позже.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_SOURCE_FRESHNESS` AS
SELECT 'funnel' AS source, MAX(date_msk) AS max_closed_date, TRUE AS gating,
       MAX(observed_at) AS observed_at
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`
UNION ALL
SELECT 'mart', MAX(target_date), TRUE, MAX(completed_at)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_RUNS`
WHERE status = 'COMPLETE' AND environment = 'prod'
UNION ALL
SELECT 'orders', MAX(order_date), FALSE, MAX(built_at)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
UNION ALL
SELECT 'stocks', MAX(snapshot_date), FALSE, MAX(built_at)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`
UNION ALL
SELECT 'storage', MAX(date_msk), FALSE, MAX(observed_at)
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`
UNION ALL
SELECT 'finance', MAX(_rr_date), FALSE, MAX(SAFE_CAST(loaded_at AS TIMESTAMP))
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`;
-- ─── 2. LAST_CLOSED_DATE ───────────────────────────────────────────────────
-- MIN по гейтящим источникам, но не позже D-1 МСК (сегодняшний день закрытым не бывает).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE` AS
SELECT
  LEAST(MIN(max_closed_date), DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)) AS last_closed_date,
  DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS d1_msk,
  ARRAY_AGG(STRUCT(source, max_closed_date) ORDER BY source) AS gating_sources
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_SOURCE_FRESHNESS`
WHERE gating;
-- ─── 3. Дневной факт nm_id × дата за текущий месяц до LAST_CLOSED_DATE ─────
-- Контракт воронки (решение владельца 11.09):
--   opens / carts / gross orders  = FUNNEL_API (authoritative);
--   01-03.09 — только исторический XLSX-backfill, ЖЁСТКО ограничен датами ≤ 2026-09-03,
--              чтобы production Engine не мог использовать XLSX ни за какие другие даты;
--   cancels 04.09+                = PROXY_FACT_ORDERS, ТОЛЬКО отмены СЛЕДУЮЩИХ дней (см. ниже).
--
-- 🔴 КОНТРАКТ ОТМЕН (доказан 20.09.2026 на официальном экспорте воронки WB за 01–19.09.2026,
--    466 строк SKU×день, 129 SKU-дней с заказами; артефакт сверки — scratch, в Git не коммитится):
--      * отмена В ДЕНЬ ЗАКАЗА: воронка НЕ считает такой заказ в «Заказали товаров» И НЕ показывает его
--        в «Отменили, шт» — событие исчезает из воронки целиком (5 из 5 случаев сентября);
--      * отмена СЛЕДУЮЩИХ дней: воронка оставляет заказ в счётчике дня заказа И показывает отмену
--        в «Отменили, шт» той же даты заказа (128 из 129 SKU-дней; единственное исключение —
--        05.09/305101361, где отмену не отдал Orders API, а не воронка).
--    Q листа берётся из воронки, значит заказ, отменённый в день заказа, в Q НЕ входит.
--    Поэтому вычитать его через S — ДВОЙНОЙ УЧЁТ: формула W уже не добавляла его прибыль.
--    Одно бизнес-событие влияет на финансовый результат ровно один раз ⇒ S = отмены СЛЕДУЮЩИХ дней.
--    Отмены дня заказа остаются видимыми диагностикой (same_day_cancel_qty в слое сверки), но в S не входят.
--
-- 🔴 КОНТРАКТ ЦЕНЫ — ОДИН НА ДВА СЛОЯ (введён 20.09.2026 после регрессии production).
--    Суточный писатель строит план по ЭТОЙ вью, а сверка — по V_UNITKA_RECON_FACT. Пока подстановка
--    цены из воронки жила только в слое сверки, суточный прогон видел здесь NULL и планировал СТЕРЕТЬ
--    цену, которую ремонт только что записал (наблюдение unitka-engine-prod-sj7z6: 5 ячеек «806→»).
--    Разные контракты цены в двух слоях = разрушение отремонтированных данных.
--    Поэтому выражения `funnel_fallback_ok`, `price` и `price_source` ниже ДОСЛОВНО совпадают
--    с reconcile_v1.sql; тест unitka_recon_sql.test.ts сравнивает их текст в обоих файлах и падает
--    при расхождении. Меняешь здесь — меняй там же, одним PR.
--    Приоритет: FACT_ORDERS (авторитет) → FUNNEL_FALLBACK по шести условиям → NULL (не выдумываем цену).
--    Подстановка допускается ТОЛЬКО когда Orders API не дал по этому SKU-дню НИ ОДНОГО заказа
--    (fact_order_qty = 0), счётчик Unitka взят из воронки, он положителен, равен orders и сумма воронки
--    положительна. Это исключает приём расхождения счётчиков, отмены Orders API, цену в день без заказов
--    и деление на ноль. Окно этой вью — текущий месяц до LCD, то есть не раньше эпохи сверки 2026-09-01.
-- Строки эмитируются ТОЛЬКО за даты ≤ LAST_CLOSED_DATE — «утечка будущего» отсекается
-- на уровне источника. NULL = пропуск источника (Engine пишет пустую ячейку, не ноль).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_DAILY_FACT` AS
WITH lcd AS (
  SELECT last_closed_date AS d2, DATE_TRUNC(last_closed_date, MONTH) AS d1
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
days AS (SELECT d FROM lcd, UNNEST(GENERATE_DATE_ARRAY(lcd.d1, lcd.d2)) AS d),
sku AS (
  SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active
),
g AS (SELECT s.nm_id, d.d FROM sku s CROSS JOIN days d),
o AS (
  SELECT nm_id, order_date AS d, SUM(quantity) AS gross,
         -- Только отмены СЛЕДУЮЩИХ дней: отмену дня заказа воронка уже исключила из Q (см. контракт выше).
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS canc,
         SAFE_DIVIDE(SUM(price_with_disc * quantity), NULLIF(SUM(quantity), 0)) AS price
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, lcd
  WHERE order_date BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
m AS (
  SELECT nm_id, day AS d, SUM(views) AS views, SUM(ad_spend) AS ads
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`, lcd
  WHERE day BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
f AS (
  -- fsum — сумма заказов воронки; нужна ТОЛЬКО как делимое подстановки цены (см. контракт цены выше).
  SELECT nm_id, date_msk AS d, MAX(open_card_count) AS opens, MAX(add_to_cart_count) AS carts,
         MAX(orders_count) AS forders, MAX(orders_sum_rub) AS fsum
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, lcd
  WHERE date_msk BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
bf AS (
  SELECT nm_id, date_msk AS d, open_card_count AS opens, add_to_cart_count AS carts,
         orders_count AS forders, cancel_count AS canc
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`
  WHERE date_msk <= DATE '2026-09-03'
),
sd AS (SELECT DISTINCT snapshot_date AS d FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, lcd WHERE snapshot_date BETWEEN lcd.d1 AND lcd.d2),
st AS (
  SELECT nm_id, snapshot_date AS d, SUM(quantity) AS stock
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`, lcd
  WHERE snapshot_date BETWEEN lcd.d1 AND lcd.d2
  GROUP BY 1, 2
),
pd AS (SELECT DISTINCT date_msk AS d FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, lcd WHERE date_msk BETWEEN lcd.d1 AND lcd.d2),
ps AS (
  SELECT nm_id, date_msk AS d, storage_rub_exact AS storage
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STORAGE_DAILY`, lcd
  WHERE date_msk BETWEEN lcd.d1 AND lcd.d2
),
x AS (
  SELECT
    g.nm_id,
    g.d,
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
    f.forders                                            AS funnel_orders,
    f.fsum                                               AS funnel_orders_sum
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
  END                                                    AS price_source
FROM p;
-- ─── 4. Ставки логистики, модель B, окно 30 дней [LCD−29, LCD], как s8win_() в Apps Script ──────
-- Популяция прямых отправлений — уникальные srid с операцией IN ('Логистика','Доставка')
-- (решение владельца 11.09). Первое плечо srid — прямое, второе — обратное.
-- Строка nm_id = 0 — магазин (fallback). Инвариант n = sales + ref проверяет Engine.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LOGISTICS_RATES` AS
WITH w AS (
  SELECT DATE_SUB(last_closed_date, INTERVAL 29 DAY) AS d1, last_closed_date AS d2
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
b AS (
  SELECT srid, SAFE_CAST(wb_nm_id AS INT64) AS nm, supplier_oper_name AS son,
         SAFE_CAST(logistics_amount AS NUMERIC) AS amt
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`, w
  WHERE _rr_date BETWEEN w.d1 AND w.d2 AND wb_nm_id IS NOT NULL AND srid IS NOT NULL
),
sale AS (SELECT COUNT(DISTINCT srid) AS n FROM b WHERE son = 'Продажа'),
l AS (
  SELECT srid, nm, son, amt,
         ROW_NUMBER() OVER (PARTITION BY srid ORDER BY amt DESC) AS rn,
         COUNT(*) OVER (PARTITION BY srid) AS legs
  FROM b WHERE son IN ('Логистика', 'Доставка')
),
p AS (
  SELECT srid, ANY_VALUE(nm) AS nm, ANY_VALUE(son) AS son,
         SUM(IF(rn = 1, amt, 0)) AS fwd, SUM(IF(rn > 1, amt, 0)) AS rev, MAX(legs) AS legs
  FROM l GROUP BY srid
),
a AS (
  SELECT nm, COUNT(*) AS n, SUM(fwd) AS fwd, COUNTIF(legs >= 2) AS ref, SUM(rev) AS rev,
         COUNTIF(son = 'Логистика') AS nlog, COUNTIF(son = 'Доставка') AS ndlv,
         SUM(IF(son = 'Доставка', fwd, 0)) AS dlvfwd
  FROM p GROUP BY nm
)
SELECT nm AS nm_id, n AS shipments, ROUND(SAFE_DIVIDE(fwd, NULLIF(n, 0)), 4) AS direct_rate,
       ref AS refusals, ROUND(SAFE_DIVIDE(rev, NULLIF(ref, 0)), 4) AS reverse_rate,
       ROUND(fwd, 2) AS forward_sum, ROUND(rev, 2) AS reverse_sum,
       nlog AS n_logistics, ndlv AS n_delivery, ROUND(dlvfwd, 2) AS delivery_component_sum,
       CAST(NULL AS INT64) AS sales, (SELECT d1 FROM w) AS window_from, (SELECT d2 FROM w) AS window_to
FROM a
UNION ALL
SELECT 0, SUM(n), ROUND(SAFE_DIVIDE(SUM(fwd), NULLIF(SUM(n), 0)), 4),
       SUM(ref), ROUND(SAFE_DIVIDE(SUM(rev), NULLIF(SUM(ref), 0)), 4),
       ROUND(SUM(fwd), 2), ROUND(SUM(rev), 2),
       SUM(nlog), SUM(ndlv), ROUND(SUM(dlvfwd), 2),
       (SELECT n FROM sale), (SELECT d1 FROM w), (SELECT d2 FROM w)
FROM a;
-- ─── 5. Комиссия WB + эквайринг, то же окно [LCD−29, LCD] ────────────────────
-- Точная копия s8finsql_()/s8fin_() из Apps Script: тариф ROUND(cpw/base/100, 6)
-- + эквайринг ROUND(acq/base, 6) — округление КАЖДОГО слагаемого отдельно, чтобы ячейки
-- сошлись побайтово. logistics_per_unit нужен Engine только для правила «своя ставка»
-- (n >= 10 AND logistics_per_unit > 0 AND commission_rate > 0), как в s8fin_().
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_COMMISSION_RATES` AS
WITH w AS (
  SELECT DATE_SUB(last_closed_date, INTERVAL 29 DAY) AS d1, last_closed_date AS d2
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`
),
f AS (
  SELECT SAFE_CAST(wb_nm_id AS INT64) AS nm, supplier_oper_name AS son,
         SAFE_CAST(quantity AS NUMERIC) AS q, SAFE_CAST(logistics_amount AS NUMERIC) AS lg,
         SAFE_CAST(acquiring_fee AS NUMERIC) AS acq, SAFE_CAST(retail_price_withdisc_rub AS NUMERIC) AS rev,
         SAFE_CAST(commission_percent AS NUMERIC) AS cp
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`, w
  WHERE _rr_date BETWEEN w.d1 AND w.d2
),
s AS (
  SELECT nm,
         SUM(IF(son = 'Продажа', q, 0)) AS n,
         SUM(IF(son IN ('Логистика', 'Доставка'), lg, 0)) AS lg,
         SUM(IF(son = 'Продажа', acq, 0)) AS acq,
         SUM(IF(son = 'Продажа', rev * q, 0)) AS base,
         SUM(IF(son = 'Продажа', cp * rev * q, 0)) AS cpw
  FROM f WHERE nm IS NOT NULL AND nm > 0 GROUP BY nm
)
SELECT nm AS nm_id, CAST(n AS INT64) AS sales,
       ROUND(SAFE_DIVIDE(lg, NULLIF(n, 0)), 4) AS logistics_per_unit,
       ROUND(SAFE_DIVIDE(cpw, NULLIF(base, 0)) / 100, 6) AS tariff_rate,
       ROUND(SAFE_DIVIDE(acq, NULLIF(base, 0)), 6) AS acquiring_rate,
       ROUND(SAFE_DIVIDE(cpw, NULLIF(base, 0)) / 100, 6) + ROUND(SAFE_DIVIDE(acq, NULLIF(base, 0)), 6) AS commission_rate,
       (SELECT d1 FROM w) AS window_from, (SELECT d2 FROM w) AS window_to
FROM s
UNION ALL
SELECT 0, CAST(SUM(n) AS INT64),
       ROUND(SAFE_DIVIDE(SUM(lg), NULLIF(SUM(n), 0)), 4),
       ROUND(SAFE_DIVIDE(SUM(cpw), NULLIF(SUM(base), 0)) / 100, 6),
       ROUND(SAFE_DIVIDE(SUM(acq), NULLIF(SUM(base), 0)), 6),
       ROUND(SAFE_DIVIDE(SUM(cpw), NULLIF(SUM(base), 0)) / 100, 6) + ROUND(SAFE_DIVIDE(SUM(acq), NULLIF(SUM(base), 0)), 6),
       (SELECT d1 FROM w), (SELECT d2 FROM w)
FROM s;

-- ─── 6. Статус Engine для дашборда/владельца (применять ПОСЛЕ infra apply, ─────
--        когда таблица wb_ops.UNITKA_ENGINE_RUNS создана Terraform'ом) ───────────
-- OK    — последний prod-прогон PASS (или пустой план) не старше 26 ч;
-- STALE — нет COMPLETE-прогона prod > 26 ч (Scheduler на паузе / Job не запускался);
-- ERROR — последний prod-прогон FAIL (код в error_code).
-- Shadow-прогоны показываются отдельными полями, в статус не входят.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_ENGINE_STATUS` AS
WITH p AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_ENGINE_RUNS`
  WHERE environment = 'prod' AND mode = 'WRITE' ORDER BY started_at DESC LIMIT 1
),
ok AS (
  SELECT MAX(completed_at) AS last_ok_at FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_ENGINE_RUNS`
  WHERE environment = 'prod' AND mode = 'WRITE' AND qa_status = 'PASS'
),
s AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_ENGINE_RUNS`
  WHERE mode = 'SHADOW' ORDER BY started_at DESC LIMIT 1
)
SELECT
  CASE
    WHEN (SELECT qa_status FROM p) = 'FAIL' THEN 'ERROR'
    WHEN (SELECT last_ok_at FROM ok) IS NULL
      OR TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT last_ok_at FROM ok), HOUR) > 26 THEN 'STALE'
    ELSE 'OK'
  END AS status,
  (SELECT last_ok_at FROM ok)            AS last_ok_at,
  (SELECT last_closed_date FROM p)       AS prod_last_closed_date,
  (SELECT completed_at FROM p)           AS prod_last_run_at,
  (SELECT qa_status FROM p)              AS prod_qa_status,
  (SELECT error_code FROM p)             AS prod_error_code,
  (SELECT cells_written FROM p)          AS prod_cells_written,
  (SELECT completed_at FROM s)           AS shadow_last_run_at,
  (SELECT qa_status FROM s)              AS shadow_qa_status,
  (SELECT cells_planned FROM s)          AS shadow_cells_planned,
  (SELECT error_code FROM s)             AS shadow_error_code,
  (SELECT last_closed_date FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_LAST_CLOSED_DATE`) AS bq_last_closed_date;
