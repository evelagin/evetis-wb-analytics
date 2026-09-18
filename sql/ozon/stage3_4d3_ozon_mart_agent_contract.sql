-- ⚠️ ИСТОРИЧЕСКАЯ МИГРАЦИЯ (R2A, 2026-09-18). Файл описывает переход, а не текущее состояние.
--    Авторитетные ТЕКУЩИЕ определения объектов ozon_mart: sql/current/ozon_mart/ (MANIFEST.json).
--    Не применять этот файл повторно как способ «вернуть» текущие витрины.
-- ═══════════════════════════════════════════════════════════════════════
-- Stage 3.4D.3 — закрытие продакшен-контракта перед теневыми агентами.
--
-- Заменяет три объекта Stage 3.4D.2 и добавляет один новый:
--   ЗАМЕНА  V_OZON_SKU_CURRENT_TARIFF          — Premium стал динамическим
--   ЗАМЕНА  V_OZON_TARIFF_SOURCE_HEALTH        — + свежесть seller_info,
--                                                 статус базы сравнения,
--                                                 ворота agent_shadow_ready
--   ЗАМЕНА  V_OZON_SKU_FORWARD_ECONOMICS_CURRENT — переименование поля
--                                                 предохранителя, проброс
--                                                 статусов
--   НОВЫЙ   V_OZON_AGENT_DECISION_INPUT        — контракт входа агента
--
-- V_OZON_TARIFF_CHANGE_LOG и V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT
-- из Stage 3.4D.2 НЕ меняются.
--
-- ЧТО ЗДЕСЬ ГЛАВНОЕ.
--
-- 1. Premium перестал быть константой. До этой стадии в витрине стояло
--    зашитое FALSE, проверенное руками 2026-09-06. Если бы подписку
--    возобновили, слой этого не заметил бы никогда. Теперь статус берётся
--    из RAW_OZON_SELLER_INFO и имеет возраст.
--
--    При этом Premium НЕ входит и не должен входить в формулу вклада SKU:
--    это постоянный расход уровня магазина, и разносить его на товар
--    запрещено политикой (OZON_PNL_POLICY_V1, запрет 5). Поэтому
--    устаревший или неизвестный статус подписки НЕ закрывает аналитику —
--    он закрывает только теневую автоматизацию.
--
-- 2. Появился отдельный статус базы сравнения тарифа. Пока полный снимок
--    блока commissions один, детектор изменений физически не с чем
--    сравнивать. Это НЕ устаревшие данные и НЕ повод прятать витрину:
--    смотреть можно, автоматически решать — нельзя.
--
-- 3. Готовность к теневому режиму и готовность к записи — РАЗНЫЕ ворота.
--    agent_write_ready зашит в FALSE и не выводится ни из чего. Ни одна
--    система не должна уметь получить право записи как следствие того,
--    что данные оказались свежими.
--
-- Откат: sql/ozon/stage3_4d3_rollback.sql
-- ═══════════════════════════════════════════════════════════════════════

-- ---------------------------------------------------------------------
-- 1. V_OZON_SKU_CURRENT_TARIFF (замена) — Premium из ingestion
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`
OPTIONS(description="Действующий тарифный контракт Ozon на уровне internal_sku из последнего снимка /v5/product/info/prices. Без trailing-подстановок: отсутствующее значение = NULL. Статус подписки Premium приходит из RAW_OZON_SELLER_INFO и имеет возраст; в формулу вклада SKU он не входит по политике. Ставка 52% сверена с официальным XLSX от 28.08.2026, границы логистики - с официальной матрицей маршрутов 20/20. ТОЛЬКО АНАЛИТИКА.")
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
  SELECT marketplace_sku, internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current),
cogs AS (
  SELECT internal_sku, product_cogs_rub, cost_basis, cogs_provenance_status
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
  WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')),
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
-- Подписка: последний снимок ingestion + его возраст. Допуск 30 ч —
-- как у остальных суточных сущностей.
-- ⚠️ Скалярные подзапросы вида (SELECT x FROM si) BigQuery вычисляет ЗАНОВО
-- на каждое обращение. При двух десятках обращений витрина перестаёт
-- считаться за разумное время. Поэтому однострочные источники
-- подключаются через LEFT JOIN ... ON TRUE: он сохраняет строки, даже
-- когда снимок подписки ещё не сделан, и вычисляется один раз.
si AS (
  SELECT TRUE AS si_exists, is_premium, premium, premium_plus, subscription_type, tax_system,
         retrieved_at, snapshot_date,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), retrieved_at, HOUR) AS age_hours
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SELLER_INFO`
  QUALIFY ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) = 1),
prem AS (
  SELECT MAX(event_date) AS last_premium_charge_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE type_id = 52)
SELECT
  mp.internal_sku, cat.offer_id, cat.ozon_sku,
  pm.canonical_product_name AS product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') AS product_type,
  cat.status_name AS catalog_status, cat.stock_present AS fbo_stock,
  cat.description_category_id, cat.type_id,
  p.snapshot_ts AS snapshot_at, p.snapshot_date AS source_snapshot_at,
  DATE '2026-08-28' AS tariff_effective_at,
  p.marketing_seller_price_rub AS seller_base_price,
  p.price_rub AS list_price_rub, p.min_price_rub, p.old_price_rub, p.vat_rate,
  cg.product_cogs_rub AS current_management_cogs_rub,
  cg.cost_basis AS cogs_basis, cg.cogs_provenance_status AS cogs_provenance,
  p.sales_percent_fbo AS commission_fbo_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbo / 100, 2) AS commission_fbo_rub_at_current_price,
  p.sales_percent_fbs AS commission_fbs_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbs / 100, 2) AS commission_fbs_rub_at_current_price,
  p.sales_percent_rfbs AS commission_rfbs_pct, p.sales_percent_fbp AS commission_fbp_pct,
  'SELLER_BASE_PRICE' AS commission_base,
  p.acquiring_rub AS acquiring_max_rub,
  ROUND(SAFE_DIVIDE(p.acquiring_rub, p.marketing_seller_price_rub) * 100, 4) AS acquiring_pct,
  p.volume_weight_l,
  p.fbo_direct_flow_trans_min_rub AS fbo_logistics_min_rub,
  p.fbo_direct_flow_trans_max_rub AS fbo_logistics_max_rub,
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
  NUMERIC '15' AS return_processing_rub,
  p.fbo_return_flow_rub AS fbo_return_logistics_rub,
  p.fbs_return_flow_rub AS fbs_return_logistics_rub,
  'TARIFF_PROVEN_PROBABILITY_NOT_PROVEN' AS return_tariff_status,
  NUMERIC '2.50' AS forced_storage_rub_per_liter_per_day,
  99 AS storage_free_days,
  'EXCLUDED_FROM_ORDER_ECONOMICS_INVENTORY_HOLDING' AS storage_applicability,

  -- --- Premium: из ingestion, а не из константы -------------------------
  -- Устаревший статус НЕ превращается в FALSE. UNKNOWN честнее, чем
  -- «подписки нет», потому что вторая формулировка — утверждение о факте.
  si.is_premium AS premium_active,
  si.premium_plus AS premium_plus_active,
  si.subscription_type AS premium_subscription_type,
  si.retrieved_at AS premium_status_source_at,
  si.age_hours AS premium_status_age_hours,
  CASE
    WHEN si.si_exists IS NOT TRUE THEN 'UNKNOWN_NEVER_INGESTED'
    WHEN si.age_hours > 30 THEN 'UNKNOWN_STALE'
    WHEN si.premium_plus THEN 'PREMIUM_PLUS_ACTIVE'
    WHEN si.is_premium OR si.premium THEN 'ACTIVE'
    ELSE 'INACTIVE' END AS premium_status,
  'PROVEN_CURRENT_API_INGESTED_DAILY' AS premium_status_basis,
  NUMERIC '9990' AS premium_tariff_rub_per_month,
  FALSE AS premium_affects_sku_contribution,
  'STORE_LEVEL_FIXED_COST_NOT_ALLOCATED_TO_SKU (OZON_PNL_POLICY_V1 запрет 5)' AS premium_allocation_rule,
  prem.last_premium_charge_date,
  si.tax_system AS seller_tax_system,

  'PROVEN_OFFICIAL_DOCUMENTATION_XLSX_28082026 + PROVEN_CURRENT_API + REALIZED' AS proof_status_commission,
  'PROVEN_CURRENT_API_BOUNDS_MATCH_OFFICIAL_ROUTE_MATRIX_20_OF_20; EXPECTED_IS_MODELLED' AS proof_status_logistics,
  'PROVEN_CURRENT_API_MAX; OFFICIAL_2.20_RATE_SET_BY_BANK' AS proof_status_acquiring,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.3.2_25082026_MATCHES_API; COURIER_HANDOVER_NOT_PROVEN' AS proof_status_fbs_first_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.7' AS proof_status_last_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.1.3_RATE_AND_TERMS' AS proof_status_storage,
  'PROVEN_CURRENT_API_DAILY_INGESTION' AS proof_status_premium,
  p.commissions_field_count, p.commissions_unknown_fields,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM p
JOIN cat USING (offer_id)
JOIN m mp ON mp.marketplace_sku = cat.ozon_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = mp.internal_sku
LEFT JOIN cogs cg ON cg.internal_sku = mp.internal_sku
LEFT JOIN obs ob ON ob.internal_sku = mp.internal_sku
LEFT JOIN si ON TRUE
LEFT JOIN prem ON TRUE
WHERE NOT cat.is_archived;


-- ---------------------------------------------------------------------
-- 2. V_OZON_TARIFF_SOURCE_HEALTH (замена) — ворота источника и агента
--
-- Три независимых уровня, которые нельзя смешивать:
--   forward_economics_ready — можно ли ВООБЩЕ считать форвардную экономику;
--   agent_shadow_ready      — можно ли пускать агента в теневом режиме;
--   agent_write_ready       — можно ли давать право записи. Всегда FALSE.
--
-- Разница между первым и вторым принципиальна. Отсутствие второго полного
-- снимка тарифа не делает числа неверными — оно лишь означает, что
-- детектор изменений ещё ни разу не отработал. Смотреть можно,
-- автоматически действовать нельзя.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`
OPTIONS(description="Ворота источника действующего тарифа Ozon и готовности агента. Стоп-краны: возраст снимка prices (30 ч), возраст seller_info (30 ч), неклассифицированная компонента, неполное покрытие тарифа, отсутствие второй базы сравнения. Три уровня готовности: forward_economics_ready (аналитика), agent_shadow_ready (теневой режим), agent_write_ready (всегда FALSE, не выводится ни из чего).")
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
si_runs AS (
  SELECT MAX(IF(status='OK', completed_at, NULL)) AS last_ok_at,
         MAX(completed_at) AS last_attempt_at,
         ANY_VALUE(status HAVING MAX completed_at) AS last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' AND entity='seller_info'),
si AS (
  SELECT retrieved_at, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), retrieved_at, HOUR) AS age_hours
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SELLER_INFO`
  QUALIFY ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) = 1),
unk AS (
  SELECT COUNTIF(NOT is_known_component) AS unknown_rows,
         STRING_AGG(DISTINCT IF(is_known_component, NULL, api_field), ', ') AS unknown_fields
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`
  WHERE snapshot_date = (SELECT latest_date FROM snap)),
-- Полные снимки тарифа: только те даты, что писал новый runtime.
-- Старые снимки RAW_OZON_PRICES базой быть не могут — в них есть одна
-- компонента из четырнадцати, и сравнение по ним было бы ложным.
base AS (
  SELECT COUNT(DISTINCT snapshot_date) AS complete_snapshots,
         MIN(snapshot_date) AS first_complete_date,
         MAX(snapshot_date) AS last_complete_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`),
cov AS (
  SELECT COUNT(*) AS offers,
         COUNTIF(sales_percent_fbo IS NOT NULL AND fbo_direct_flow_trans_min_rub IS NOT NULL
                 AND fbo_direct_flow_trans_max_rub IS NOT NULL AND fbo_deliv_to_customer_rub IS NOT NULL
                 AND acquiring_rub IS NOT NULL) AS offers_full_tariff
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
  WHERE snapshot_date = (SELECT latest_date FROM snap)),
sku AS (
  SELECT COUNT(*) AS sku_rows,
         COUNTIF(internal_sku IS NOT NULL AND ozon_sku IS NOT NULL AND offer_id IS NOT NULL) AS sku_identity_ok,
         COUNTIF(current_management_cogs_rub IS NOT NULL AND current_management_cogs_rub > 0) AS sku_cogs_ok,
         COUNTIF(premium_status LIKE 'UNKNOWN%') AS sku_premium_unknown
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
calc AS (
  SELECT
    (SELECT latest_date FROM snap) AS tariff_snapshot_date,
    (SELECT latest_ts FROM snap) AS tariff_snapshot_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) AS tariff_age_hours,
    (SELECT last_ok_at FROM runs) AS prices_last_ok_at,
    (SELECT last_attempt_status FROM runs) AS prices_last_attempt_status,
    (SELECT last_attempt_at FROM runs) AS prices_last_attempt_at,
    (SELECT retrieved_at FROM si) AS seller_info_at,
    (SELECT age_hours FROM si) AS seller_info_age_hours,
    (SELECT last_ok_at FROM si_runs) AS seller_info_last_ok_at,
    (SELECT offers FROM cov) AS offers_in_snapshot,
    (SELECT offers_full_tariff FROM cov) AS offers_with_full_tariff,
    IFNULL((SELECT unknown_rows FROM unk), 0) AS unknown_component_rows,
    (SELECT unknown_fields FROM unk) AS unknown_component_fields,
    (SELECT complete_snapshots FROM base) AS complete_tariff_snapshots,
    (SELECT first_complete_date FROM base) AS first_complete_tariff_date,
    (SELECT sku_rows FROM sku) AS sku_rows,
    (SELECT sku_identity_ok FROM sku) AS sku_identity_ok,
    (SELECT sku_cogs_ok FROM sku) AS sku_cogs_ok,
    (SELECT sku_premium_unknown FROM sku) AS sku_premium_unknown),
st AS (
  SELECT c.*,
    CASE
      WHEN c.prices_last_ok_at IS NULL THEN 'NEVER_INGESTED'
      WHEN c.prices_last_attempt_status <> 'OK' AND c.prices_last_attempt_at > c.prices_last_ok_at THEN 'REFRESH_FAILED'
      WHEN c.tariff_age_hours > 30 THEN 'STALE'
      ELSE 'FRESH' END AS tariff_freshness_status,
    CASE
      WHEN c.seller_info_at IS NULL THEN 'NEVER_INGESTED'
      WHEN c.seller_info_age_hours > 30 THEN 'STALE'
      ELSE 'FRESH' END AS seller_info_freshness_status,
    IF(c.complete_tariff_snapshots >= 2, 'BASELINE_OK', 'FIRST_BASELINE_PENDING') AS tariff_change_baseline_status
  FROM calc c)
SELECT
  CURRENT_TIMESTAMP() AS checked_at,
  st.tariff_snapshot_date, st.tariff_snapshot_at, st.tariff_age_hours,
  30 AS tariff_tolerance_hours,
  st.prices_last_ok_at, st.prices_last_attempt_status,
  st.seller_info_at, st.seller_info_age_hours, st.seller_info_last_ok_at,
  30 AS seller_info_tolerance_hours,
  st.offers_in_snapshot, st.offers_with_full_tariff,
  st.sku_rows, st.sku_identity_ok, st.sku_cogs_ok, st.sku_premium_unknown,
  st.unknown_component_rows, st.unknown_component_fields,
  st.complete_tariff_snapshots, st.first_complete_tariff_date,
  st.tariff_freshness_status,
  st.seller_info_freshness_status,
  st.tariff_change_baseline_status,
  st.unknown_component_rows > 0 AS unknown_tariff_component_detected,

  -- Аналитическая готовность. Свежесть подписки сюда НЕ входит:
  -- Premium не участвует в формуле вклада SKU, поэтому его неизвестность
  -- не делает форвардные числа неверными.
  (   st.tariff_freshness_status = 'FRESH'
   AND st.unknown_component_rows = 0
   AND st.offers_with_full_tariff = st.offers_in_snapshot
  ) AS forward_economics_ready,
  CASE
    WHEN st.unknown_component_rows > 0 THEN 'DECISIONS_BLOCKED_UNKNOWN_TARIFF_COMPONENT'
    WHEN st.tariff_freshness_status <> 'FRESH' THEN 'DECISIONS_BLOCKED_SOURCE_NOT_FRESH'
    WHEN st.offers_with_full_tariff < st.offers_in_snapshot THEN 'DECISIONS_BLOCKED_INCOMPLETE_TARIFF_COVERAGE'
    ELSE 'DECISIONS_ALLOWED' END AS agent_decision_gate,

  -- Готовность к теневому режиму. Строже аналитической на три условия:
  -- свежая подписка, состоявшееся сравнение тарифа, полнота COGS и связки.
  (   st.tariff_freshness_status = 'FRESH'
   AND st.seller_info_freshness_status = 'FRESH'
   AND st.unknown_component_rows = 0
   AND st.offers_with_full_tariff = st.offers_in_snapshot
   AND st.sku_rows > 0
   AND st.sku_identity_ok = st.sku_rows
   AND st.sku_cogs_ok = st.sku_rows
   AND st.tariff_change_baseline_status = 'BASELINE_OK'
  ) AS agent_shadow_ready,
  CASE
    WHEN st.tariff_change_baseline_status <> 'BASELINE_OK' THEN 'SHADOW_BLOCKED_FIRST_BASELINE_PENDING'
    WHEN st.seller_info_freshness_status <> 'FRESH' THEN 'SHADOW_BLOCKED_SELLER_INFO_NOT_FRESH'
    WHEN st.tariff_freshness_status <> 'FRESH' THEN 'SHADOW_BLOCKED_TARIFF_NOT_FRESH'
    WHEN st.unknown_component_rows > 0 THEN 'SHADOW_BLOCKED_UNKNOWN_TARIFF_COMPONENT'
    WHEN st.offers_with_full_tariff < st.offers_in_snapshot THEN 'SHADOW_BLOCKED_INCOMPLETE_TARIFF_COVERAGE'
    WHEN st.sku_identity_ok < st.sku_rows THEN 'SHADOW_BLOCKED_IDENTITY_INCOMPLETE'
    WHEN st.sku_cogs_ok < st.sku_rows THEN 'SHADOW_BLOCKED_COGS_INCOMPLETE'
    ELSE 'SHADOW_ALLOWED' END AS agent_shadow_gate,

  -- Право записи не выводится ни из чего и не зависит от данных.
  FALSE AS agent_write_ready,
  'HARDCODED_FALSE_NOT_DERIVABLE_FROM_DATA_REQUIRES_SEPARATE_OWNER_ACK' AS agent_write_gate
FROM st;


-- ---------------------------------------------------------------------
-- 3. V_OZON_SKU_FORWARD_ECONOMICS_CURRENT (замена)
--
-- Изменения против Stage 3.4D.2:
--   • safe_ad_capacity_15pct_worst_case_rub → safe_ad_spend_15pct_worst_case_rub
--     (каноническое имя ACK владельца; переименование сделано ДО заморозки
--      контракта, потребителей у прежнего имени не было);
--   • добавлены premium_status, tariff_change_baseline_status,
--     agent_shadow_ready, agent_write_ready.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`
OPTIONS(description="FORWARD_MODELLED юнит-экономика одного нового заказа Ozon по ДЕЙСТВУЮЩЕМУ тарифу. Три сценария логистики: best (API min) / expected (MODELLED, не тариф) / worst (API max). Классификация безопасности - по worst case. Premium в формулу вклада SKU не входит. НЕ путать с V_OZON_SKU_UNIT_ECONOMICS_CURRENT (TRAILING_OBSERVED, 180 дней). ТОЛЬКО АНАЛИТИКА.")
AS
WITH t AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
health AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`),
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
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_min_rub - t.last_mile_rub, 2) AS contribution_best_case,
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_expected_rub - t.last_mile_rub, 2) AS contribution_expected,
    ROUND(t.seller_base_price - t.current_management_cogs_rub - t.commission_fbo_rub_at_current_price
          - t.acquiring_max_rub - t.fbo_logistics_max_rub - t.last_mile_rub, 2) AS contribution_worst_case
  FROM t)
SELECT
  b.internal_sku, b.offer_id, b.ozon_sku, b.product_name, b.product_type, b.catalog_status, b.fbo_stock,
  'FORWARD_MODELLED' AS economics_mode,
  b.snapshot_at AS tariff_snapshot_at, b.tariff_effective_at,
  'MODELLED_EXPECTED' AS expected_case_label_basis,
  b.seller_base_price, b.current_management_cogs_rub, b.cogs_basis,
  b.commission_fbo_pct AS commission_pct,
  b.commission_fbo_rub_at_current_price AS commission_rub,
  b.acquiring_max_rub AS acquiring_rub, b.last_mile_rub,
  b.fbo_logistics_min_rub AS logistics_min_rub,
  b.fbo_logistics_expected_rub AS logistics_expected_rub,
  b.fbo_logistics_max_rub AS logistics_max_rub,
  b.fbo_logistics_expected_basis AS logistics_expected_basis,
  b.fbo_logistics_observations,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_min_rub + b.last_mile_rub, 2) AS mandatory_costs_min_logistics_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_expected_rub + b.last_mile_rub, 2) AS mandatory_costs_expected_rub,
  ROUND(b.commission_fbo_rub_at_current_price + b.acquiring_max_rub + b.fbo_logistics_max_rub + b.last_mile_rub, 2) AS mandatory_costs_max_logistics_rub,
  b.contribution_best_case, b.contribution_expected, b.contribution_worst_case,
  b.contribution_best_case AS fbo_contribution_min_logistics_case,
  b.contribution_expected AS fbo_contribution_expected_case,
  b.contribution_worst_case AS fbo_contribution_max_logistics_case,
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS margin_best_case_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS margin_worst_case_pct,
  ROUND(SAFE_DIVIDE(b.contribution_best_case,  b.seller_base_price) * 100, 2) AS break_even_drr_best_pct,
  ROUND(SAFE_DIVIDE(b.contribution_expected,   b.seller_base_price) * 100, 2) AS break_even_drr_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case, b.seller_base_price) * 100, 2) AS break_even_drr_worst_pct,
  b.contribution_best_case AS max_ad_spend_at_0_margin_best,
  b.contribution_expected AS max_ad_spend_at_0_margin_expected,
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
  ROUND(SAFE_DIVIDE(b.contribution_expected   - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_expected_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS drr_limit_at_15_margin_worst_pct,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.20', b.seller_base_price) * 100, 2) AS drr_limit_at_20_margin_worst_pct,
  -- канонические предохранители будущего рекламного агента
  ROUND(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', 2) AS safe_ad_spend_15pct_worst_case_rub,
  ROUND(SAFE_DIVIDE(b.contribution_worst_case - b.seller_base_price * NUMERIC '0.15', b.seller_base_price) * 100, 2) AS safe_drr_15pct_worst_case_pct,
  ROUND(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, 2) AS route_sensitivity_rub,
  ROUND(SAFE_DIVIDE(b.fbo_logistics_max_rub - b.fbo_logistics_min_rub, b.seller_base_price) * 100, 2) AS route_sensitivity_pp,
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
  cn.cancellation_rate_pct, cn.orders_in_window AS cancellation_window_orders,
  IF(cn.orders_in_window IS NULL, 'NO_ORDERS_IN_WINDOW', 'PROVEN_STATUS_BASED') AS cancellation_probability_status,
  'NOT_PROVEN_RETURN_QTY' AS return_probability_status,
  CAST(NULL AS NUMERIC) AS expected_return_cost_rub,
  ROUND(b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_expected_rub,
  ROUND(b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub, 2) AS cancel_cost_if_shipped_worst_rub,
  NUMERIC '0.558' AS p_shipped_given_cancel_portfolio,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_expected
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_expected_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2) AS expected_contribution_per_created_order_expected_rub,
  ROUND((1 - SAFE_DIVIDE(cn.cancellation_rate_pct, 100)) * b.contribution_worst_case
        - SAFE_DIVIDE(cn.cancellation_rate_pct, 100) * NUMERIC '0.558'
          * (b.fbo_logistics_max_rub + b.fbo_return_logistics_rub + b.return_processing_rub), 2) AS expected_contribution_per_created_order_worst_rub,
  'CANCEL_ONLY_RETURNS_EXCLUDED' AS expected_mode_status,
  CASE
    WHEN b.current_management_cogs_rub IS NULL OR b.seller_base_price IS NULL
      OR b.commission_fbo_pct IS NULL OR b.fbo_logistics_max_rub IS NULL
      OR NOT hh.forward_economics_ready THEN 'BLOCKED_BY_DATA'
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
  -- состояние магазина: видно, но на вклад SKU не влияет
  b.premium_status, b.premium_active, b.premium_plus_active,
  b.premium_status_source_at, b.premium_affects_sku_contribution,
  hh.forward_economics_ready, hh.agent_decision_gate,
  hh.tariff_freshness_status, hh.seller_info_freshness_status,
  hh.tariff_change_baseline_status, hh.unknown_tariff_component_detected,
  hh.agent_shadow_ready, hh.agent_write_ready,
  b.proof_status_commission, b.proof_status_logistics, b.proof_status_acquiring,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM b LEFT JOIN canc cn USING (internal_sku)
CROSS JOIN health hh          -- ровно одна строка, вычисляется один раз
ORDER BY margin_worst_case_pct DESC;


-- ---------------------------------------------------------------------
-- 4. V_OZON_AGENT_DECISION_INPUT (новый) — контракт входа агента
--
-- Одна строка на текущий продаваемый SKU. Только проверенные входы:
-- ни одной рекомендации, ни одного поля, которое можно принять за
-- команду. Наблюдённые метрики продаж и рекламы берутся из канонической
-- FCT_OZON_SKU_PNL_MONTHLY и несут префикс trailing_, чтобы их нельзя
-- было спутать с действующим тарифом.
--
-- Права записи в этом контракте нет и быть не может: agent_write_ready
-- приходит константой FALSE из ворот и не вычисляется из данных.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_AGENT_DECISION_INPUT`
OPTIONS(description="READ-ONLY контракт входа для будущих теневых агентов Ozon. Одна строка на текущий продаваемый SKU. Содержит только проверенные текущие входы и наблюдённые канонические метрики с префиксом trailing_. Рекомендаций нет. agent_write_ready всегда FALSE и не выводится из данных.")
AS
WITH f AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`),
h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`),
obs AS (
  SELECT internal_sku,
         SUM(realized_qty) AS trailing_90d_realized_qty,
         ROUND(SUM(seller_base_revenue_rub), 2) AS trailing_90d_revenue_rub,
         ROUND(SUM(ad_spend_attributed_rub), 2) AS trailing_90d_ad_spend_rub,
         ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), NULLIF(SUM(seller_base_revenue_rub), 0)) * 100, 2) AS trailing_90d_actual_drr_pct
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
  WHERE month >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY), MONTH)
  GROUP BY 1)
SELECT
  f.internal_sku, f.offer_id, f.ozon_sku, f.product_name, f.product_type,
  f.catalog_status, f.fbo_stock,
  IF(f.fbo_stock > 0, 'IN_STOCK', 'OUT_OF_STOCK') AS stock_status,

  -- вход: действующий тариф
  f.seller_base_price, f.current_management_cogs_rub AS management_cogs,
  f.commission_pct AS current_commission_pct, f.commission_rub AS current_commission_rub,
  f.acquiring_rub, f.last_mile_rub,
  f.logistics_min_rub, f.logistics_expected_rub, f.logistics_max_rub,
  f.logistics_expected_basis,

  -- вход: экономика по трём сценариям
  f.contribution_best_case, f.contribution_expected, f.contribution_worst_case,
  f.margin_best_case_pct, f.margin_expected_pct, f.margin_worst_case_pct,
  f.break_even_drr_best_pct, f.break_even_drr_expected_pct, f.break_even_drr_worst_pct,

  -- предохранители
  f.safe_ad_spend_15pct_worst_case_rub, f.safe_drr_15pct_worst_case_pct,
  f.max_ad_spend_at_20_margin_worst, f.drr_limit_at_20_margin_worst_pct,
  f.route_sensitivity_pp,

  -- флаги безопасности
  f.safety_class_worst_case, f.expected_case_label,
  f.pricing_review_required, f.loss_risk_worst_case,

  -- наблюдённое, каноническое, ЯВНО не текущее
  IFNULL(o.trailing_90d_realized_qty, 0) AS trailing_90d_realized_qty,
  IFNULL(o.trailing_90d_revenue_rub, 0) AS trailing_90d_revenue_rub,
  IFNULL(o.trailing_90d_ad_spend_rub, 0) AS trailing_90d_ad_spend_rub,
  o.trailing_90d_actual_drr_pct,
  f.cancellation_rate_pct AS trailing_cancellation_rate_pct,
  f.cancellation_probability_status,

  -- состояние источника и ворота
  f.tariff_snapshot_at, f.tariff_freshness_status, f.seller_info_freshness_status,
  f.tariff_change_baseline_status, f.premium_status,
  h.unknown_component_rows AS unknown_tariff_component_count,
  f.forward_economics_ready, f.agent_decision_gate,
  h.agent_shadow_ready, h.agent_shadow_gate,
  FALSE AS agent_write_ready,
  'HARDCODED_FALSE_NOT_DERIVABLE_FROM_DATA_REQUIRES_SEPARATE_OWNER_ACK' AS agent_write_gate,
  'READ_ONLY_INPUT_NO_RECOMMENDATIONS_NO_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM f LEFT JOIN obs o USING (internal_sku)
CROSS JOIN h                  -- ровно одна строка
ORDER BY f.margin_worst_case_pct;
