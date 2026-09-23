-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Internal dependencies: V_OZON_PROMO_OBSERVATION_HISTORY.
--
-- Грейн: observation_id × evidence_key. КАЖДОЕ наблюдение источника уровня товара, без
-- схлопывания и без разрешения противоречий (это делает V_OZON_PROMO_SKU_STATE_HISTORY).
--
--   evidence_source            endpoint                                   source_state
--   ACTION_PRODUCTS            POST /v1/actions/products                  PARTICIPATING
--   ACTION_CANDIDATES          POST /v1/actions/candidates                CANDIDATE
--   AUTO_ADD_LIST              POST /v1/actions/auto-add/products/list    SCHEDULED_AUTO_ADD
--   AUTO_ADD_CANDIDATES        POST /v1/actions/auto-add/products/candidates  AUTO_ADD_ELIGIBLE
--   PRODUCT_MARKETING_ACTIONS  POST /v5/product/info/prices (marketing_actions.actions[])
--                                                                        MARKETING_ACTION_LISTED
--
-- marketing_actions[] не несёт id акции (L-5). Связь с /v1/actions — только точным
-- совпадением в том же снимке: title побайтно (без TRIM: «Максимальный бустинг » с хвостовым
-- пробелом — реальное название) И date_from = date_start И date_to = date_end, и ровно
-- одна акция с таким title. Иначе AMBIGUOUS (несколько акций с тем же title либо окно не
-- совпало) или UNRESOLVED (такого title нет — программы Ozon вне /v1/actions: рассрочка,
-- семплинг, промокоды). Нечёткого сопоставления нет; неоднозначное не разрешается.
--
-- Семантика не доказана (SEMANTICS_UNRESOLVED, L-3/L-4) и в канон НЕ выносится:
-- stock, min_stock, price_min_elastic, price_max_elastic, alert_max_action_price*,
-- value из marketing_actions (разнородная единица). Они остаются в RAW.
-- SKU EVETIS — через evetis_ref.REF_SKU_CHANNEL_MAP: по product_id, затем по offer_id.
-- Несопоставленный товар остаётся строкой с mapping_status = UNMAPPED.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Все свидетельства уровня товара по акциям Ozon в годных снимках, без схлопывания: участники, кандидаты, запланированное и возможное автодобавление, marketing_actions. Связь marketing_actions с акцией — только точное совпадение title и окна дат при единственной акции с таким title (DETERMINISTIC_EXACT_MATCH), иначе AMBIGUOUS или UNRESOLVED. Поля с недоказанной семантикой (stock, эластичные цены) не выносятся. Несопоставленные товары — mapping_status = UNMAPPED.")
AS
WITH obs AS (
  SELECT observation_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
),
act AS (
  SELECT observation_id, action_id, title, date_start, date_end
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, action_id ORDER BY ingested_at, source_payload_hash) = 1
),
ref_pid AS (
  SELECT marketplace_product_id, COUNT(DISTINCT internal_sku) AS n_sku, MAX(internal_sku) AS internal_sku,
    MAX(offer_id) AS offer_id, MAX(marketplace_sku) AS marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current AND marketplace_product_id IS NOT NULL
  GROUP BY marketplace_product_id
),
ref_offer AS (
  SELECT offer_id, COUNT(DISTINCT internal_sku) AS n_sku, MAX(internal_sku) AS internal_sku,
    MAX(marketplace_sku) AS marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current AND offer_id IS NOT NULL
  GROUP BY offer_id
),
lists AS (
  SELECT observation_id, observation_bucket, observed_at, source_endpoint,
    CASE membership WHEN 'PARTICIPATING' THEN 'ACTION_PRODUCTS' WHEN 'CANDIDATE' THEN 'ACTION_CANDIDATES'
      ELSE 'ACTION_PRODUCT_LISTS' END AS evidence_source,
    CASE membership WHEN 'PARTICIPATING' THEN 'PARTICIPATING' WHEN 'CANDIDATE' THEN 'CANDIDATE'
      ELSE 'UNRECOGNISED' END AS source_state,
    action_id, product_id,
    CAST(NULL AS STRING) AS source_offer_id,
    CAST(NULL AS INT64) AS source_ozon_sku,
    CAST(NULL AS TIMESTAMP) AS auto_add_at,
    add_mode, price_rub, action_price_rub, max_action_price_rub,
    CAST(NULL AS NUMERIC) AS action_price_to_auto_add_rub,
    current_boost_pct, min_boost_pct, max_boost_pct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, action_id, membership, product_id ORDER BY ingested_at, source_payload_hash) = 1
),
auto_add AS (
  SELECT observation_id, observation_bucket, observed_at, source_endpoint,
    CASE list_kind WHEN 'SCHEDULED' THEN 'AUTO_ADD_LIST' WHEN 'ELIGIBLE' THEN 'AUTO_ADD_CANDIDATES'
      ELSE 'AUTO_ADD_LISTS' END AS evidence_source,
    CASE list_kind WHEN 'SCHEDULED' THEN 'SCHEDULED_AUTO_ADD' WHEN 'ELIGIBLE' THEN 'AUTO_ADD_ELIGIBLE'
      ELSE 'UNRECOGNISED' END AS source_state,
    action_id, product_id,
    offer_id AS source_offer_id,
    ozon_sku AS source_ozon_sku,
    auto_add_at,
    add_mode, price_rub,
    CAST(NULL AS NUMERIC) AS action_price_rub,
    CAST(NULL AS NUMERIC) AS max_action_price_rub,
    action_price_to_auto_add_rub,
    CAST(NULL AS NUMERIC) AS current_boost_pct,
    CAST(NULL AS NUMERIC) AS min_boost_pct,
    CAST(NULL AS NUMERIC) AS max_boost_pct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, action_id, auto_add_at, list_kind, product_id ORDER BY ingested_at, source_payload_hash) = 1
),
mk AS (
  SELECT observation_id, observation_bucket, observed_at, source_endpoint, offer_id, product_id,
    action_ordinal, action_title, action_date_from, action_date_to
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, offer_id, action_ordinal ORDER BY ingested_at, source_payload_hash) = 1
),
mk_link AS (
  SELECT m.observation_id, m.observation_bucket, m.observed_at, m.source_endpoint, m.offer_id, m.product_id,
    m.action_ordinal, m.action_title, m.action_date_from, m.action_date_to,
    COUNT(a.action_id) AS title_matches,
    COUNTIF(a.date_start = m.action_date_from AND a.date_end = m.action_date_to) AS window_matches,
    MAX(IF(a.date_start = m.action_date_from AND a.date_end = m.action_date_to, a.action_id, NULL)) AS window_action_id,
    STRING_AGG(CAST(a.action_id AS STRING), ',' ORDER BY a.action_id) AS title_match_action_ids
  FROM mk m
  LEFT JOIN act a ON a.observation_id = m.observation_id AND a.title = m.action_title
  GROUP BY m.observation_id, m.observation_bucket, m.observed_at, m.source_endpoint, m.offer_id, m.product_id,
    m.action_ordinal, m.action_title, m.action_date_from, m.action_date_to
),
u AS (
  SELECT observation_id, observation_bucket, observed_at, source_endpoint, evidence_source, source_state,
    action_id, 'DIRECT_ID' AS promotion_link_method, CAST(NULL AS STRING) AS link_candidate_action_ids,
    product_id, source_offer_id, source_ozon_sku, auto_add_at, add_mode, price_rub, action_price_rub,
    max_action_price_rub, action_price_to_auto_add_rub, current_boost_pct, min_boost_pct, max_boost_pct,
    CAST(NULL AS INT64) AS action_ordinal, CAST(NULL AS STRING) AS source_action_title,
    CAST(NULL AS TIMESTAMP) AS source_date_from, CAST(NULL AS TIMESTAMP) AS source_date_to
  FROM lists
  UNION ALL
  SELECT observation_id, observation_bucket, observed_at, source_endpoint, evidence_source, source_state,
    action_id, 'DIRECT_ID', CAST(NULL AS STRING),
    product_id, source_offer_id, source_ozon_sku, auto_add_at, add_mode, price_rub, action_price_rub,
    max_action_price_rub, action_price_to_auto_add_rub, current_boost_pct, min_boost_pct, max_boost_pct,
    CAST(NULL AS INT64), CAST(NULL AS STRING), CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP)
  FROM auto_add
  UNION ALL
  SELECT observation_id, observation_bucket, observed_at, source_endpoint, 'PRODUCT_MARKETING_ACTIONS',
    'MARKETING_ACTION_LISTED',
    IF(title_matches = 1 AND window_matches = 1, window_action_id, NULL),
    CASE
      WHEN title_matches = 1 AND window_matches = 1 THEN 'DETERMINISTIC_EXACT_MATCH'
      WHEN title_matches = 0 THEN 'UNRESOLVED'
      ELSE 'AMBIGUOUS'
    END,
    IF(title_matches >= 1 AND NOT (title_matches = 1 AND window_matches = 1), title_match_action_ids, NULL),
    product_id, offer_id, CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP), CAST(NULL AS STRING),
    CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
    CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
    action_ordinal, action_title, action_date_from, action_date_to
  FROM mk_link
)
SELECT
  'OZON' AS marketplace,
  u.observation_id,
  u.observation_bucket AS observation_slot,
  u.observed_at,
  'OBSERVED' AS history_class,
  u.evidence_source,
  u.source_endpoint,
  u.source_state,
  IF(u.source_state = 'UNRECOGNISED', 'UNKNOWN', 'DIRECT_SOURCE') AS evidence_class,
  u.action_id,
  u.promotion_link_method,
  u.link_candidate_action_ids,
  u.product_id,
  COALESCE(u.source_offer_id, rp.offer_id) AS offer_id,
  COALESCE(CAST(u.source_ozon_sku AS STRING), rp.marketplace_sku, ro.marketplace_sku) AS ozon_sku,
  CASE
    WHEN rp.n_sku = 1 THEN rp.internal_sku
    WHEN rp.marketplace_product_id IS NULL AND ro.n_sku = 1 THEN ro.internal_sku
  END AS internal_sku,
  CASE
    WHEN rp.n_sku = 1 THEN 'MAPPED_BY_PRODUCT_ID'
    WHEN rp.n_sku > 1 THEN 'AMBIGUOUS_MAPPING'
    WHEN ro.n_sku = 1 THEN 'MAPPED_BY_OFFER_ID'
    WHEN ro.n_sku > 1 THEN 'AMBIGUOUS_MAPPING'
    ELSE 'UNMAPPED'
  END AS mapping_status,
  u.auto_add_at,
  u.add_mode,
  u.price_rub,
  u.action_price_rub,
  u.max_action_price_rub,
  u.action_price_to_auto_add_rub,
  u.current_boost_pct,
  u.min_boost_pct,
  u.max_boost_pct,
  u.action_ordinal,
  u.source_action_title,
  u.source_date_from,
  u.source_date_to,
  CONCAT(u.evidence_source, '|', IFNULL(CAST(u.action_id AS STRING), '-'), '|', IFNULL(CAST(u.product_id AS STRING), '-'),
         '|', IFNULL(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', u.auto_add_at), '-'), '|',
         IF(u.evidence_source = 'PRODUCT_MARKETING_ACTIONS', IFNULL(u.source_offer_id, '-'), '-'), '|',
         IFNULL(CAST(u.action_ordinal AS STRING), '-')) AS evidence_key
FROM u
JOIN obs o ON o.observation_id = u.observation_id
LEFT JOIN ref_pid rp ON rp.marketplace_product_id = CAST(u.product_id AS STRING)
LEFT JOIN ref_offer ro ON ro.offer_id = u.source_offer_id
