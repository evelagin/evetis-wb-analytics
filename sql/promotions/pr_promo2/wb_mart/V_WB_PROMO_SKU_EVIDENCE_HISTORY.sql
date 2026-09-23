-- ============================================================================
-- PR-PROMO-2 · CANONICAL PROMOTION STATE · wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY (VIEW)
-- Грейн: observation_id × evidence_key (= promotion_id × nm_id × запрошенный список inAction).
--
-- Единственный источник SKU-фактов WB — /calendar/promotions/nomenclatures, и только для
-- НЕ-автоакций. Сегодня все акции WB автоматические, поэтому строк 0 — и это правильно:
-- вью НЕ порождает строк из агрегатов акции и не строит декартово произведение
-- «каталог × акция». Отсутствие строки ≠ «не участвует» (см. sku_membership_observability
-- в V_WB_PROMO_HISTORY).
--
-- inAction = TRUE → PARTICIPATING; FALSE → CANDIDATE (документация WB: товары, которые
-- можно добавить в акцию; живыми данными ещё не подтверждено — все акции auto).
-- SKU EVETIS резолвится по evetis_ref.REF_SKU_CHANNEL_MAP (WB, marketplace_sku = nm_id);
-- несопоставленный nm_id остаётся строкой с mapping_status = UNMAPPED.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Свидетельства участия SKU в акциях WB по годным снимкам: одна строка на observation_id × evidence_key. Источник — только /nomenclatures для не-автоакций; для автоакций строк нет намеренно (не наблюдаемо), агрегаты акции на SKU не опускаются. inAction TRUE = PARTICIPATING, FALSE = CANDIDATE. Несопоставленные nm_id не отбрасываются: mapping_status = UNMAPPED.")
AS
WITH obs AS (
  SELECT observation_id
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
),
ref AS (
  SELECT marketplace_sku, COUNT(DISTINCT internal_sku) AS n_sku, MAX(internal_sku) AS internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'WB' AND is_current AND marketplace_sku IS NOT NULL
  GROUP BY marketplace_sku
),
nm AS (
  SELECT observation_id, observation_bucket, observed_at, source_endpoint, promotion_id,
    in_action_requested, nm_id, in_action, price, plan_price, discount_pct, plan_discount_pct, currency_code
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, promotion_id, in_action_requested, nm_id ORDER BY ingested_at, source_payload_hash) = 1
)
SELECT
  'WB' AS marketplace,
  n.observation_id,
  n.observation_bucket AS observation_slot,
  n.observed_at,
  'OBSERVED' AS history_class,
  'PROMOTION_NOMENCLATURES' AS evidence_source,
  n.source_endpoint,
  CASE
    WHEN n.in_action IS TRUE THEN 'PARTICIPATING'
    WHEN n.in_action IS FALSE THEN 'CANDIDATE'
    ELSE 'UNRECOGNISED'
  END AS source_state,
  IF(n.in_action IS NULL, 'UNKNOWN', 'DIRECT_SOURCE') AS evidence_class,
  n.promotion_id,
  'DIRECT_ID' AS promotion_link_method,
  n.nm_id,
  IF(r.n_sku = 1, r.internal_sku, NULL) AS internal_sku,
  CASE
    WHEN r.marketplace_sku IS NULL THEN 'UNMAPPED'
    WHEN r.n_sku = 1 THEN 'MAPPED_BY_NM_ID'
    ELSE 'AMBIGUOUS_MAPPING'
  END AS mapping_status,
  n.in_action_requested,
  n.in_action,
  n.price,
  n.plan_price,
  n.discount_pct,
  n.plan_discount_pct,
  n.currency_code,
  CONCAT('PROMOTION_NOMENCLATURES|', IFNULL(CAST(n.promotion_id AS STRING), '-'), '|',
         IFNULL(CAST(n.nm_id AS STRING), '-'), '|', IFNULL(CAST(n.in_action_requested AS STRING), '-')) AS evidence_key
FROM nm n
JOIN obs o ON o.observation_id = n.observation_id
LEFT JOIN ref r ON r.marketplace_sku = CAST(n.nm_id AS STRING)
