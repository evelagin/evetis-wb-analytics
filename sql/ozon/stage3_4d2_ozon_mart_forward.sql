-- ⚠️ ИСТОРИЧЕСКАЯ МИГРАЦИЯ (R2A, 2026-09-18). Файл описывает переход, а не текущее состояние.
--    Авторитетные ТЕКУЩИЕ определения объектов ozon_mart: sql/current/ozon_mart/ (MANIFEST.json).
--    Не применять этот файл повторно как способ «вернуть» текущие витрины.
--    SUPERSEDED: определения V_OZON_SKU_CURRENT_TARIFF, V_OZON_TARIFF_SOURCE_HEALTH и
--    V_OZON_SKU_FORWARD_ECONOMICS_CURRENT в этом файле устарели (заменены Stage 3.4D.3) и НЕ
--    являются текущими; повторный прогон после 3.4D.3 ломает V_OZON_AGENT_DECISION_INPUT.
-- ═══════════════════════════════════════════════════════════════════════
-- Stage 3.4D.2 — продакшен-слой ДЕЙСТВУЮЩЕГО тарифа и форвардной экономики.
--
-- ГРАНИЦА С ЧУЖИМ СЛОЕМ. V_OZON_SKU_UNIT_ECONOMICS_CURRENT остаётся
-- TRAILING_OBSERVED и здесь НЕ трогается: у неё другой вопрос («что было
-- в среднем за 180 дней»). Объекты ниже отвечают на другой вопрос —
-- «что произойдёт с ОДНИМ новым заказом по действующим условиям».
-- Смешивать их запрещено: комиссия выросла 41 → 52 %, и trailing-числа
-- систематически завышают маржу.
--
-- ЧТО ДОКАЗАНО НА 2026-09-06 (docs/ozon/audit_2026-09-06/tariffs/):
--   • ставка 52 % — официальный XLSX «Таблица категорий … 28082026»,
--     строки Красота и гигиена > Косметика для ухода, все пять type_id
--     EVETIS: FBO 0,52 / FBS 0,52 / rFBS 0,52. Архивная таблица
--     (15.07–27.08) даёт 0,41 / 0,47 — совпадает с наблюдённой историей;
--   • API-границы логистики = официальная матрица: min = универсальный
--     тариф по объёму (лист «Тарифы по умолчанию»), max = максимум по
--     848 маршрутам кластер→кластер. Сверено 20/20 SKU;
--   • последняя миля 25 ₽ — п. 2.7; обработка возврата 15 ₽ — п. 2.32;
--   • обработка отправления FBS: СЦ 0 ₽, ПВЗ/ППЗ 10 ₽ — п. 2.3.2
--     (с 25.08.2026). Вторичное «2 % макс 45 ₽» — пункт ОТМЕНЁННОЙ
--     редакции от 14.07.2026, в действующей его нет;
--   • вынужденное размещение 2,50 ₽/л/день после 99 бесплатных дней
--     (п. 2.1.3 + таблица сроков). В экономику ЗАКАЗА не входит.
--
-- ЧТО НЕ ДОКАЗАНО И ПОЭТОМУ НЕ МОДЕЛИРУЕТСЯ:
--   • курьерская отгрузка FBS — тариф устанавливает партнёр, виден
--     только в кабинете (п. 2.4.1);
--   • вероятность возврата — expected_return_cost = NULL;
--   • маршрут конкретного заказа: ожидаемая логистика — MODELLED_EXPECTED,
--     а не тариф.
--
-- Откат: sql/ozon/stage3_4d2_rollback.sql
-- ═══════════════════════════════════════════════════════════════════════

-- ---------------------------------------------------------------------
-- 1. V_OZON_TARIFF_SOURCE_HEALTH — ворота источника
--
-- Форвардной экономике мало свежести P&L: комиссия и логистика меняются
-- быстрее продаж, и решение по цене на позавчерашнем тарифе хуже, чем
-- отсутствие решения. Здесь два независимых стоп-крана:
--
--   1. СВЕЖЕСТЬ. Сущность prices идёт в ozon-runtime-daily (30 6 * * *
--      МСК). Допуск 30 ч — тот же, что у остальных суточных сущностей
--      в V_OZON_MART_FRESHNESS. Ровно 24 ч брать нельзя: следующий
--      прогон приходит на 24-м часу, и любая минута задержки давала бы
--      ложный STALE.
--
--   2. НЕИЗВЕСТНАЯ КОМПОНЕНТА. Если Ozon добавит поле в блок
--      commissions, runtime положит его строкой с is_known_component =
--      FALSE. Ни одна формула его не учитывает, поэтому витрина
--      закрывается: молча absorbировать новый тариф запрещено.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`
OPTIONS(description="Ворота источника действующего тарифа Ozon. Два стоп-крана: возраст снимка prices (допуск 30 ч, согласован с суточной каденцией ozon-runtime-daily) и появление неклассифицированной компоненты в блоке commissions. forward_economics_ready = FALSE закрывает V_OZON_SKU_FORWARD_ECONOMICS_CURRENT для решений.")
AS
WITH snap AS (
  SELECT MAX(snapshot_date) AS latest_date, MAX(snapshot_ts) AS latest_ts
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`),
runs AS (
  SELECT MAX(IF(status='OK', completed_at, NULL)) AS last_ok_at,
         MAX(completed_at) AS last_attempt_at,
         ANY_VALUE(status HAVING MAX completed_at) AS last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' AND entity='prices'),
unk AS (
  SELECT COUNTIF(NOT is_known_component) AS unknown_rows,
         STRING_AGG(DISTINCT IF(is_known_component, NULL, api_field), ', ') AS unknown_fields
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`
  WHERE snapshot_date = (SELECT latest_date FROM snap)),
-- покрытие: у скольких товаров последнего снимка тарифный блок полон
cov AS (
  SELECT COUNT(*) AS offers,
         COUNTIF(sales_percent_fbo IS NOT NULL AND fbo_direct_flow_trans_min_rub IS NOT NULL
                 AND fbo_direct_flow_trans_max_rub IS NOT NULL AND fbo_deliv_to_customer_rub IS NOT NULL
                 AND acquiring_rub IS NOT NULL) AS offers_full_tariff
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
  WHERE snapshot_date = (SELECT latest_date FROM snap))
SELECT
  CURRENT_TIMESTAMP() AS checked_at,
  (SELECT latest_date FROM snap) AS tariff_snapshot_date,
  (SELECT latest_ts FROM snap) AS tariff_snapshot_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) AS tariff_age_hours,
  30 AS tariff_tolerance_hours,
  (SELECT last_ok_at FROM runs) AS prices_last_ok_at,
  (SELECT last_attempt_status FROM runs) AS prices_last_attempt_status,
  (SELECT offers FROM cov) AS offers_in_snapshot,
  (SELECT offers_full_tariff FROM cov) AS offers_with_full_tariff,
  (SELECT unknown_rows FROM unk) AS unknown_component_rows,
  (SELECT unknown_fields FROM unk) AS unknown_component_fields,
  CASE
    WHEN (SELECT last_ok_at FROM runs) IS NULL THEN 'NEVER_INGESTED'
    WHEN (SELECT last_attempt_status FROM runs) <> 'OK'
         AND (SELECT last_attempt_at FROM runs) > (SELECT last_ok_at FROM runs) THEN 'REFRESH_FAILED'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) > 30 THEN 'STALE'
    ELSE 'FRESH' END AS tariff_freshness_status,
  IFNULL((SELECT unknown_rows FROM unk), 0) > 0 AS unknown_tariff_component_detected,
  -- итоговые ворота
  (   (SELECT last_ok_at FROM runs) IS NOT NULL
   AND TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) <= 30
   AND NOT ((SELECT last_attempt_status FROM runs) <> 'OK'
            AND (SELECT last_attempt_at FROM runs) > (SELECT last_ok_at FROM runs))
   AND IFNULL((SELECT unknown_rows FROM unk), 0) = 0
   AND (SELECT offers_full_tariff FROM cov) = (SELECT offers FROM cov)
  ) AS forward_economics_ready,
  CASE
    WHEN IFNULL((SELECT unknown_rows FROM unk), 0) > 0
      THEN 'DECISIONS_BLOCKED_UNKNOWN_TARIFF_COMPONENT'
    WHEN (SELECT last_ok_at FROM runs) IS NULL
      OR TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) > 30
      OR ((SELECT last_attempt_status FROM runs) <> 'OK'
          AND (SELECT last_attempt_at FROM runs) > (SELECT last_ok_at FROM runs))
      THEN 'DECISIONS_BLOCKED_SOURCE_NOT_FRESH'
    WHEN (SELECT offers_full_tariff FROM cov) < (SELECT offers FROM cov)
      THEN 'DECISIONS_BLOCKED_INCOMPLETE_TARIFF_COVERAGE'
    ELSE 'DECISIONS_ALLOWED' END AS agent_decision_gate;


-- ---------------------------------------------------------------------
-- 2. V_OZON_TARIFF_CHANGE_LOG — детектор изменения тарифа
--
-- Зерно: offer_id × схема × компонента, последний снимок против
-- предыдущего, в котором эта компонента вообще была. Ловит четыре
-- события: смену значения, появление новой компоненты, исчезновение
-- старой и неклассифицированную компоненту.
--
-- ⚠️ Пока в источнике один снимок с полным блоком commissions, вся
-- таблица вернёт NO_BASELINE. Это не ошибка: сравнивать не с чем,
-- потому что до 2026-09-06 runtime сохранял только sales_percent_fbo.
-- Первая настоящая сверка появится со вторым суточным прогоном.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_CHANGE_LOG`
OPTIONS(description="Детектор изменения действующего тарифа Ozon: значение компоненты в последнем снимке против предыдущего. change_type = VALUE_CHANGED / NEW_COMPONENT / COMPONENT_DISAPPEARED / UNKNOWN_COMPONENT / NO_BASELINE / UNCHANGED. Материальное изменение обязано быть классифицировано человеком до возобновления решений.")
AS
WITH c AS (
  SELECT snapshot_date, snapshot_ts, offer_id, sale_scheme, commission_component,
         api_field, value_num, unit, is_known_component
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`),
ranked AS (
  SELECT *, DENSE_RANK() OVER (ORDER BY snapshot_date DESC) AS rk FROM c),
cur AS (SELECT * FROM ranked WHERE rk = 1),
prv AS (
  -- предыдущее известное значение той же компоненты, каким бы старым оно ни было
  SELECT * EXCEPT(rn) FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY offer_id, sale_scheme, commission_component
                                   ORDER BY snapshot_date DESC) AS rn
    FROM ranked r WHERE rk > 1) WHERE rn = 1)
SELECT
  CURRENT_TIMESTAMP() AS detected_at,
  COALESCE(cur.offer_id, prv.offer_id) AS offer_id,
  COALESCE(cur.sale_scheme, prv.sale_scheme) AS sale_scheme,
  COALESCE(cur.commission_component, prv.commission_component) AS commission_component,
  COALESCE(cur.api_field, prv.api_field) AS api_field,
  COALESCE(cur.unit, prv.unit) AS unit,
  prv.snapshot_date AS previous_snapshot_date,
  prv.value_num AS previous_value,
  cur.snapshot_date AS current_snapshot_date,
  cur.value_num AS current_value,
  ROUND(cur.value_num - prv.value_num, 4) AS delta,
  CASE
    WHEN cur.offer_id IS NOT NULL AND NOT cur.is_known_component THEN 'UNKNOWN_COMPONENT'
    WHEN prv.offer_id IS NULL THEN 'NO_BASELINE'
    WHEN cur.offer_id IS NULL THEN 'COMPONENT_DISAPPEARED'
    WHEN prv.value_num IS NULL AND cur.value_num IS NOT NULL THEN 'NEW_COMPONENT'
    WHEN cur.value_num <> prv.value_num THEN 'VALUE_CHANGED'
    ELSE 'UNCHANGED' END AS change_type,
  CASE
    WHEN cur.offer_id IS NOT NULL AND NOT cur.is_known_component THEN TRUE
    WHEN prv.offer_id IS NULL OR cur.offer_id IS NULL THEN TRUE
    WHEN cur.value_num <> prv.value_num THEN TRUE
    ELSE FALSE END AS tariff_change_detected
FROM cur FULL JOIN prv USING (offer_id, sale_scheme, commission_component);


-- ---------------------------------------------------------------------
-- 3. V_OZON_SKU_CURRENT_TARIFF — действующий тариф на SKU
--
-- Зерно: одна строка на internal_sku текущего каталога Ozon.
-- Никакого замещения средними за 180 дней здесь нет и быть не может:
-- каждое поле либо пришло из последнего снимка API, либо равно NULL.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`
OPTIONS(description="Действующий тарифный контракт Ozon на уровне internal_sku из последнего снимка /v5/product/info/prices. Без trailing-подстановок: отсутствующее значение = NULL. Ставка 52% сверена с официальным XLSX от 28.08.2026; границы логистики API сверены с официальной матрицей маршрутов 20/20. ТОЛЬКО АНАЛИТИКА.")
AS
WITH snap AS (
  SELECT MAX(snapshot_date) AS d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`),
csnap AS (
  SELECT MAX(snapshot_date) AS d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG`),
cat AS (
  SELECT c.offer_id, c.sku AS ozon_sku, c.name, c.status_name, c.is_archived,
         c.stock_present, c.description_category_id, c.type_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG` c, csnap
  WHERE c.snapshot_date = csnap.d),
p AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`, snap
  WHERE snapshot_date = snap.d),
m AS (
  -- только действующие идентичности: из 22 исторических пар сегодня
  -- живы 20, две карточки выведены из каталога весной 2026
  SELECT marketplace_sku, internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current),
cogs AS (
  SELECT internal_sku, product_cogs_rub, cost_basis, cogs_provenance_status
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
  WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')),
-- наблюдённая логистика ПОСЛЕ вступления действующего тарифа: только она
-- имеет право уточнять ожидание внутри доказанного коридора
obs AS (
  SELECT mp.internal_sku,
         APPROX_QUANTILES(-f.amount_rub, 2)[OFFSET(1)] AS median_logistics_rub,
         COUNT(*) AS n_obs
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` po
    ON po.posting_number = f.posting_number AND po.sku = f.sku
  JOIN m mp ON mp.marketplace_sku = f.sku
  WHERE f.type_id = 32
    AND po.created_at >= TIMESTAMP('2026-08-28 00:00:00', 'Europe/Moscow')
  GROUP BY 1),
-- подписка Premium: наблюдение по начислениям. Точечная проверка API
-- 2026-09-06 (is_premium=false, premium=false, premium_plus=false)
-- зафиксирована в docs/ozon/audit_2026-09-04/forward_economics/
-- api_seller_subscription_2026-09-06.json; в ingestion её пока нет.
prem AS (
  SELECT MAX(event_date) AS last_premium_charge_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE type_id = 52)
SELECT
  mp.internal_sku,
  cat.offer_id,
  cat.ozon_sku,
  pm.canonical_product_name AS product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') AS product_type,
  cat.status_name AS catalog_status,
  cat.stock_present AS fbo_stock,
  cat.description_category_id,
  cat.type_id,

  -- --- моменты ------------------------------------------------------
  p.snapshot_ts AS snapshot_at,
  p.snapshot_date AS source_snapshot_at,
  DATE '2026-08-28' AS tariff_effective_at,

  -- --- цена и себестоимость -----------------------------------------
  p.marketing_seller_price_rub AS seller_base_price,
  p.price_rub AS list_price_rub,
  p.min_price_rub,
  p.old_price_rub,
  p.vat_rate,
  cg.product_cogs_rub AS current_management_cogs_rub,
  cg.cost_basis AS cogs_basis,
  cg.cogs_provenance_status AS cogs_provenance,

  -- --- комиссия -----------------------------------------------------
  p.sales_percent_fbo AS commission_fbo_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbo / 100, 2) AS commission_fbo_rub_at_current_price,
  p.sales_percent_fbs AS commission_fbs_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbs / 100, 2) AS commission_fbs_rub_at_current_price,
  p.sales_percent_rfbs AS commission_rfbs_pct,
  p.sales_percent_fbp AS commission_fbp_pct,
  'SELLER_BASE_PRICE' AS commission_base,

  -- --- эквайринг ----------------------------------------------------
  p.acquiring_rub AS acquiring_max_rub,
  ROUND(SAFE_DIVIDE(p.acquiring_rub, p.marketing_seller_price_rub) * 100, 4) AS acquiring_pct,

  -- --- логистика ----------------------------------------------------
  p.volume_weight_l,
  p.fbo_direct_flow_trans_min_rub AS fbo_logistics_min_rub,
  p.fbo_direct_flow_trans_max_rub AS fbo_logistics_max_rub,
  -- ожидание: медиана факта после 28.08 при n >= 2, иначе универсальный
  -- тариф × 1,20 (медиана отношения факт/минимум по 14 доставкам).
  -- Всегда зажато в доказанный коридор [min, max].
  ROUND(LEAST(GREATEST(
      IF(ob.n_obs >= 2, ob.median_logistics_rub, p.fbo_direct_flow_trans_min_rub * NUMERIC '1.20'),
      p.fbo_direct_flow_trans_min_rub), p.fbo_direct_flow_trans_max_rub), 2) AS fbo_logistics_expected_rub,
  IF(ob.n_obs >= 2, 'MODELLED_EXPECTED_OBSERVED_MEDIAN_POST_20260828',
                    'MODELLED_EXPECTED_API_MIN_x1.20_INFERRED') AS fbo_logistics_expected_basis,
  IFNULL(ob.n_obs, 0) AS fbo_logistics_observations,
  p.fbs_direct_flow_trans_min_rub AS fbs_logistics_min_rub,
  p.fbs_direct_flow_trans_max_rub AS fbs_logistics_max_rub,
  p.fbs_first_mile_min_rub AS fbs_first_mile_sc_rub,
  p.fbs_first_mile_max_rub AS fbs_first_mile_pvz_rub,
  CAST(NULL AS NUMERIC) AS fbs_first_mile_courier_rub,
  p.fbo_deliv_to_customer_rub AS last_mile_rub,

  -- --- возвраты -----------------------------------------------------
  NUMERIC '15' AS return_processing_rub,
  p.fbo_return_flow_rub AS fbo_return_logistics_rub,
  p.fbs_return_flow_rub AS fbs_return_logistics_rub,
  'TARIFF_PROVEN_PROBABILITY_NOT_PROVEN' AS return_tariff_status,

  -- --- хранение (вне экономики заказа) --------------------------------
  NUMERIC '2.50' AS forced_storage_rub_per_liter_per_day,
  99 AS storage_free_days,
  'EXCLUDED_FROM_ORDER_ECONOMICS_INVENTORY_HOLDING' AS storage_applicability,

  -- --- Premium ------------------------------------------------------
  FALSE AS premium_active,
  pr.last_premium_charge_date,
  'PROVEN_CURRENT_API_POINT_CHECK_2026-09-06T10:03:50Z + NO_CHARGE_SINCE_2026-05-11' AS premium_status_basis,

  -- --- статусы доказанности -------------------------------------------
  'PROVEN_OFFICIAL_DOCUMENTATION_XLSX_28082026 + PROVEN_CURRENT_API + REALIZED' AS proof_status_commission,
  'PROVEN_CURRENT_API_BOUNDS_MATCH_OFFICIAL_ROUTE_MATRIX_20_OF_20; EXPECTED_IS_MODELLED' AS proof_status_logistics,
  'PROVEN_CURRENT_API_MAX; OFFICIAL_2.20_RATE_SET_BY_BANK' AS proof_status_acquiring,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.3.2_25082026_MATCHES_API; COURIER_HANDOVER_NOT_PROVEN' AS proof_status_fbs_first_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.7' AS proof_status_last_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.1.3_RATE_AND_TERMS' AS proof_status_storage,

  p.commissions_field_count,
  p.commissions_unknown_fields,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM p
JOIN cat USING (offer_id)
JOIN m mp ON mp.marketplace_sku = cat.ozon_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = mp.internal_sku
LEFT JOIN cogs cg ON cg.internal_sku = mp.internal_sku
LEFT JOIN obs ob ON ob.internal_sku = mp.internal_sku
CROSS JOIN prem pr
WHERE NOT cat.is_archived;


-- ---------------------------------------------------------------------
-- 4. V_OZON_SKU_FORWARD_ECONOMICS_CURRENT — форвардная экономика заказа
--
-- Три сценария отличаются ТОЛЬКО логистикой:
--   BEST_CASE     = минимум API  = универсальный тариф по объёму;
--   EXPECTED_CASE = MODELLED_EXPECTED, НЕ тариф;
--   WORST_CASE    = максимум API = самый дальний маршрут матрицы.
--
-- ⚠️ Именование обратное: «минимальная логистика» даёт МАКСИМАЛЬНЫЙ
-- вклад, «максимальная логистика» — МИНИМАЛЬНЫЙ. Классификация
-- безопасности считается по WORST_CASE и только по нему.
--
-- Целевая цена решается алгеброй, а не подбором:
--   P = (COGS + логистика + последняя миля) / (1 − c − a − m − d),
-- где c — доля комиссии, a — доля эквайринга, m — целевая маржа,
-- d — целевой ДРР. Знаменатель ≤ 0 ⇒ цена недостижима ⇒ NULL.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`
OPTIONS(description="FORWARD_MODELLED юнит-экономика одного нового заказа Ozon по ДЕЙСТВУЮЩЕМУ тарифу. Три сценария логистики: best (API min) / expected (MODELLED, не тариф) / worst (API max). Классификация безопасности - по worst case. НЕ путать с V_OZON_SKU_UNIT_ECONOMICS_CURRENT (TRAILING_OBSERVED, окно 180 дней). ТОЛЬКО АНАЛИТИКА: цены, ставки, кампании не изменяются.")
AS
WITH t AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
health AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`),
-- вероятность отмены: статусы отправлений, окно 01.06.2026 → минус 12 дней
-- (последние 12 дней исключены: заказ ещё может быть отменён)
canc AS (
  SELECT mp.internal_sku,
         ROUND(SAFE_DIVIDE(COUNTIF(po.status='cancelled'), COUNT(*)) * 100, 2) AS cancellation_rate_pct,
         COUNT(*) AS orders_in_window
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` po
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku = po.sku AND mp.is_current
  WHERE po.order_date BETWEEN DATE '2026-06-01' AND DATE_SUB(CURRENT_DATE(), INTERVAL 12 DAY)
  GROUP BY 1),
b AS (
  SELECT t.*,
    SAFE_DIVIDE(t.commission_fbo_pct, 100) AS c_frac,
    SAFE_DIVIDE(t.acquiring_pct, 100) AS a_frac,
    t.current_management_cogs_rub + t.last_mile_rub AS fixed_ex_logistics,
    -- вклады
    ROUND(t.seller_base_price - t.current_management_cogs_rub
          - t.commission_fbo_rub_at_current_price - t.acquiring_max_rub
          - t.fbo_logistics_min_rub - t.last_mile_rub, 2) AS contribution_best_case,
    ROUND(t.seller_base_price - t.current_management_cogs_rub
          - t.commission_fbo_rub_at_current_price - t.acquiring_max_rub
          - t.fbo_logistics_expected_rub - t.last_mile_rub, 2) AS contribution_expected,
    ROUND(t.seller_base_price - t.current_management_cogs_rub
          - t.commission_fbo_rub_at_current_price - t.acquiring_max_rub
          - t.fbo_logistics_max_rub - t.last_mile_rub, 2) AS contribution_worst_case
  FROM t)
SELECT
  b.internal_sku, b.offer_id, b.ozon_sku, b.product_name, b.product_type,
  b.catalog_status, b.fbo_stock,

  -- --- семантика ------------------------------------------------------
  'FORWARD_MODELLED' AS economics_mode,
  b.snapshot_at AS tariff_snapshot_at,
  b.tariff_effective_at,
  'MODELLED_EXPECTED' AS expected_case_label_basis,

  -- --- вход -----------------------------------------------------------
  b.seller_base_price,
  b.current_management_cogs_rub,
  b.cogs_basis,
  b.commission_fbo_pct AS commission_pct,
  b.commission_fbo_rub_at_current_price AS commission_rub,
  b.acquiring_max_rub AS acquiring_rub,
  b.last_mile_rub,
  b.fbo_logistics_min_rub AS logistics_min_rub,
  b.fbo_logistics_expected_rub AS logistics_expected_rub,
  b.fbo_logistics_max_rub AS logistics_max_rub,
  b.fbo_logistics_expected_basis AS logistics_expected_basis,
  b.fbo_logistics_observations,

  -- --- обязательные расходы -------------------------------------------
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub
        + b.fbo_logistics_min_rub + b.last_mile_rub, 2) AS mandatory_costs_min_logistics_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub
        + b.fbo_logistics_expected_rub + b.last_mile_rub, 2) AS mandatory_costs_expected_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub
        + b.fbo_logistics_max_rub + b.last_mile_rub, 2) AS mandatory_costs_max_logistics_rub,

  -- --- вклад ----------------------------------------------------------
  b.contribution_best_case,
  b.contribution_expected,
  b.contribution_worst_case,
  b.contribution_best_case  AS fbo_contribution_min_logistics_case,
  b.contribution_expected   AS fbo_contribution_expected_case,
  b.contribution_worst_case AS fbo_contribution_max_logistics_case,

  -- --- маржа ----------------------------------------------------------
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS margin_best_case_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS margin_worst_case_pct,

  -- --- безубыточный ДРР по переменным ---------------------------------
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS break_even_drr_best_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS break_even_drr_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS break_even_drr_worst_pct,

  -- --- ёмкость рекламы -------------------------------------------------
  b.contribution_best_case  AS max_ad_spend_at_0_margin_best,
  b.contribution_expected   AS max_ad_spend_at_0_margin_expected,
  b.contribution_worst_case AS max_ad_spend_at_0_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.10', 2) AS max_ad_spend_at_10_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', 2) AS max_ad_spend_at_15_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.20', 2) AS max_ad_spend_at_20_margin_worst,
  ROUND(b.contribution_best_case  - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_best,
  ROUND(b.contribution_expected   - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_expected,
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.25', 2) AS max_ad_spend_at_25_margin_worst,
  -- предельный ДРР при целевой марже = (вклад − маржа·цена) / цена
  ROUND(SAFE_DIVIDE(b.contribution_expected   - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_worst_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.20', b.seller_base_price) * 100, 2) AS drr_limit_at_20_margin_worst_pct,
  -- предохранитель рекламного агента
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', 2) AS safe_ad_capacity_15pct_worst_case_rub,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS safe_drr_15pct_worst_case_pct,

  -- --- чувствительность к маршруту -------------------------------------
  ROUND(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, 2) AS route_sensitivity_rub,
  ROUND(SAFE_DIVIDE(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, b.seller_base_price) * 100, 2) AS route_sensitivity_pp,

  -- --- целевые цены: алгебра, без рекламы -------------------------------
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac, 0)), 2) AS break_even_price_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac, 0)), 2) AS break_even_price_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.10, 0)), 2) AS target_price_10pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.15, 0)), 2) AS target_price_15pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.20, 0)), 2) AS target_price_20pct_no_ads_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_min_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_best,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub,      NULLIF(1 - b.c_frac - b.a_frac - 0.25, 0)), 2) AS target_price_25pct_no_ads_worst,

  -- --- целевые цены при ДРР (ожидаемый и худший случай) -----------------
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.05, 0)), 2) AS target_price_15pct_at_drr5_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.10, 0)), 2) AS target_price_15pct_at_drr10_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.15, 0)), 2) AS target_price_15pct_at_drr15_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.20, 0)), 2) AS target_price_15pct_at_drr20_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.25, 0)), 2) AS target_price_15pct_at_drr25_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.05, 0)), 2) AS target_price_15pct_at_drr5_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.10, 0)), 2) AS target_price_15pct_at_drr10_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.15 - 0.15, 0)), 2) AS target_price_15pct_at_drr15_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.05, 0)), 2) AS target_price_20pct_at_drr5_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.10, 0)), 2) AS target_price_20pct_at_drr10_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.15, 0)), 2) AS target_price_20pct_at_drr15_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.20, 0)), 2) AS target_price_20pct_at_drr20_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_expected_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.25, 0)), 2) AS target_price_20pct_at_drr25_expected,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.05, 0)), 2) AS target_price_20pct_at_drr5_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.10, 0)), 2) AS target_price_20pct_at_drr10_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.20 - 0.15, 0)), 2) AS target_price_20pct_at_drr15_worst,
  ROUND(SAFE_DIVIDE(b.fixed_ex_logistics + b.fbo_logistics_max_rub, NULLIF(1 - b.c_frac - b.a_frac - 0.25 - 0.10, 0)), 2) AS target_price_25pct_at_drr10_worst,

  -- --- MODE 2: ожидание на созданный заказ ------------------------------
  cn.cancellation_rate_pct,
  cn.orders_in_window AS cancellation_window_orders,
  IF(cn.orders_in_window IS NULL, 'NO_ORDERS_IN_WINDOW', 'PROVEN_STATUS_BASED') AS cancellation_probability_status,
  'NOT_PROVEN_RETURN_QTY' AS return_probability_status,
  CAST(NULL AS NUMERIC) AS expected_return_cost_rub,
  ROUND(b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_expected_rub,
  ROUND(b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_worst_rub,
  NUMERIC '0.558' AS p_shipped_given_cancel_portfolio,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_expected
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2)
    AS expected_contribution_per_created_order_expected_rub,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_worst_case
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2)
    AS expected_contribution_per_created_order_worst_rub,
  'CANCEL_ONLY_RETURNS_EXCLUDED' AS expected_mode_status,

  -- --- классификация безопасности: ТОЛЬКО по worst case ------------------
  CASE
    WHEN b.current_management_cogs_rub IS NULL OR b.seller_base_price IS NULL
      OR b.commission_fbo_pct IS NULL OR b.fbo_logistics_max_rub IS NULL
      OR NOT (SELECT forward_economics_ready FROM health) THEN 'BLOCKED_BY_DATA'
    WHEN b.contribution_worst_case < 0 THEN 'LOSS_RISK_WORST_CASE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 25 THEN 'SCALE_SAFE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 20 THEN 'HEALTHY_SAFE'
    WHEN SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 >= 10 THEN 'WATCH'
    ELSE 'RESTRICT_ADS' END AS safety_class_worst_case,
  CASE
    WHEN b.contribution_expected < 0 THEN 'LOSS_MAKING_BEFORE_ADS'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 30 THEN 'SCALE_CANDIDATE'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 20 THEN 'HEALTHY'
    WHEN SAFE_DIVIDE(b.contribution_expected, b.seller_base_price) * 100 >= 10 THEN 'WATCH'
    ELSE 'RESTRICT_ADS_PRICE_REVIEW' END AS expected_case_label,
  b.contribution_worst_case < 0 AS loss_risk_worst_case,
  SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100 < 10 AS pricing_review_required,

  -- --- ворота -----------------------------------------------------------
  (SELECT forward_economics_ready FROM health) AS forward_economics_ready,
  (SELECT agent_decision_gate FROM health) AS agent_decision_gate,
  (SELECT tariff_freshness_status FROM health) AS tariff_freshness_status,
  (SELECT unknown_tariff_component_detected FROM health) AS unknown_tariff_component_detected,
  b.proof_status_commission, b.proof_status_logistics, b.proof_status_acquiring,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM b LEFT JOIN canc cn USING (internal_sku)
ORDER BY margin_worst_case_pct DESC;


-- ---------------------------------------------------------------------
-- 5. V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT — сравнение схем
--
-- FBS остаётся PARTIAL и после того, как тарифный конфликт разрешён:
-- у EVETIS нет ни одного реализованного FBS-заказа за всю историю,
-- FBS-склад не настроен, а курьерская отгрузка тарифицируется партнёром
-- и в API не приходит. Считать сравнение окончательным нельзя.
--
-- Внешний фулфилмент — не тариф Ozon, поэтому его стоимость NULL,
-- а не ноль: сценарии 50/75/100/125/150 ₽ показаны отдельными колонками.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT`
OPTIONS(description="Сравнение FBO и FBS по действующему тарифу на уровне одного заказа. Две модели отгрузки FBS: СЦ (обработка 0 руб) и ПВЗ/ППЗ (10 руб); курьерская отгрузка NULL - тариф партнёра виден только в кабинете. fbs_status = PARTIAL: реализованных FBS-заказов нет, склад не настроен. Рекомендация схемы PROVISIONAL.")
AS
WITH t AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
f AS (SELECT internal_sku, contribution_expected, contribution_worst_case
      FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`),
b AS (
  SELECT t.*, f.contribution_expected AS fbo_contribution_expected,
         f.contribution_worst_case AS fbo_contribution_worst,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_expected_rub - t.last_mile_rub - t.fbs_first_mile_sc_rub, 2) AS fbs_sc_expected,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_max_rub - t.last_mile_rub - t.fbs_first_mile_sc_rub, 2) AS fbs_sc_worst,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_expected_rub - t.last_mile_rub - t.fbs_first_mile_pvz_rub, 2) AS fbs_pvz_expected,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_max_rub - t.last_mile_rub - t.fbs_first_mile_pvz_rub, 2) AS fbs_pvz_worst
  FROM t JOIN f USING (internal_sku))
SELECT
  b.internal_sku, b.product_name, b.product_type, b.seller_base_price,
  b.commission_fbo_pct, b.commission_fbs_pct,
  b.fbo_contribution_expected, b.fbo_contribution_worst,
  b.fbs_first_mile_sc_rub, b.fbs_first_mile_pvz_rub, b.fbs_first_mile_courier_rub,
  b.fbs_sc_expected  AS fbs_contribution_sc_expected,
  b.fbs_sc_worst     AS fbs_contribution_sc_worst,
  b.fbs_pvz_expected AS fbs_contribution_pvz_expected,
  b.fbs_pvz_worst    AS fbs_contribution_pvz_worst,
  ROUND(b.fbs_sc_expected  - b.fbo_contribution_expected, 2) AS fbs_max_external_ff_to_match_fbo_sc,
  ROUND(b.fbs_pvz_expected - b.fbo_contribution_expected, 2) AS fbs_max_external_ff_to_match_fbo_pvz,
  -- условная скидка −3 % комиссии за отгрузку в первый рекомендованный слот
  -- (официальный раздел 1). Не в базовой модели: зависит от операционной
  -- дисциплины, а не от тарифа.
  ROUND(b.seller_base_price * NUMERIC '0.03', 2) AS fbs_early_shipment_discount_max_rub,
  ROUND(b.fbs_sc_expected - b.fbo_contribution_expected + b.seller_base_price * NUMERIC '0.03', 2)
    AS fbs_max_external_ff_to_match_fbo_sc_with_early_discount,
  CAST(NULL AS NUMERIC) AS external_fbs_fulfillment_cost_rub,
  ROUND(b.fbs_pvz_expected - 50,  2) AS fbs_contribution_ff_50,
  ROUND(b.fbs_pvz_expected - 75,  2) AS fbs_contribution_ff_75,
  ROUND(b.fbs_pvz_expected - 100, 2) AS fbs_contribution_ff_100,
  ROUND(b.fbs_pvz_expected - 125, 2) AS fbs_contribution_ff_125,
  ROUND(b.fbs_pvz_expected - 150, 2) AS fbs_contribution_ff_150,
  IF(b.fbs_pvz_expected - 50  > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_50,
  IF(b.fbs_pvz_expected - 75  > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_75,
  IF(b.fbs_pvz_expected - 100 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_100,
  IF(b.fbs_pvz_expected - 125 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_125,
  IF(b.fbs_pvz_expected - 150 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_150,
  'PARTIAL' AS fbs_forward_economics_ready,
  'PROVISIONAL' AS fbs_scheme_recommendation,
  'NO_REALIZED_FBS_ORDERS_EVER; FBS_WAREHOUSE_NOT_CONFIGURED; COURIER_HANDOVER_TARIFF_NOT_PROVEN' AS fbs_blockers,
  b.proof_status_fbs_first_mile,
  'ANALYTICAL_ONLY_NO_SCHEME_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM b ORDER BY b.internal_sku;
