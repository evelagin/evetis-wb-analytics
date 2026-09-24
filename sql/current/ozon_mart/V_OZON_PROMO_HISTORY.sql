-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Internal dependencies: V_OZON_PROMO_OBSERVATION_HISTORY.
--
-- Грейн: observation_id × action_id. Состояние акции Ozon в момент годного снимка.
-- lifecycle_status — из date_start/date_end относительно observed_at снимка (не
-- CURRENT_TIMESTAMP): воспроизводимо для любого исторического T. date_end — последняя
-- секунда (20:59:59Z), граница включительная.
-- sku_membership_observability = OBSERVED только если перечисленные строки /products и
-- /candidates совпали с объявленными participating_products_count и
-- potential_products_count; иначе ENUMERATION_INCOMPLETE — и тогда отсутствие товара в
-- списках НЕ превращается в NOT_ELIGIBLE (см. V_OZON_PROMO_SKU_STATE_HISTORY).
-- freeze_at сохраняется как факт источника; отдельного состояния жизненного цикла из него
-- не выводится: семантика «заморозки» не наблюдалась (NULL у всех акций).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`
OPTIONS (description = "PR-PROMO-2. Каноническая история акций Ozon: одна строка на годный снимок × action_id. lifecycle_status выведен из дат источника относительно observed_at снимка. sku_membership_observability = OBSERVED, только если перечисленные участники и кандидаты совпали с объявленными счётчиками акции, иначе ENUMERATION_INCOMPLETE. is_participating — прямой факт источника. Без экономики.")
AS
WITH obs AS (
  SELECT observation_id, observation_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
),
act AS (
  SELECT observation_id, observation_bucket, observed_at, action_id, title, action_type, date_start, date_end,
    freeze_at, auto_add_dates_csv, auto_add_dates_count, is_participating, potential_products_count,
    participating_products_count, banned_products_count, is_voucher_action, with_targeting,
    discount_type, discount_value, source_payload_hash
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, action_id ORDER BY ingested_at, source_payload_hash) = 1
),
enumerated AS (
  SELECT observation_id, action_id,
    COUNT(DISTINCT IF(membership = 'PARTICIPATING', product_id, NULL)) AS enumerated_participating,
    COUNT(DISTINCT IF(membership = 'CANDIDATE', product_id, NULL)) AS enumerated_candidates
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
  WHERE environment = 'prod'
  GROUP BY observation_id, action_id
)
SELECT
  'OZON' AS marketplace,
  a.observation_id,
  a.observation_bucket AS observation_slot,
  a.observed_at,
  'OBSERVED' AS history_class,
  o.observation_status,
  a.action_id,
  a.title AS promotion_name,
  a.action_type AS promotion_type,
  a.date_start AS starts_at,
  a.date_end AS ends_at,
  DATE(a.date_start, 'Europe/Moscow') AS starts_on_msk,
  DATE(a.date_end, 'Europe/Moscow') AS ends_on_msk,
  CASE
    WHEN a.date_start IS NULL OR a.date_end IS NULL OR a.observed_at IS NULL THEN 'UNKNOWN'
    WHEN a.observed_at < a.date_start THEN 'UPCOMING'
    WHEN a.observed_at > a.date_end THEN 'ENDED'
    ELSE 'ACTIVE'
  END AS lifecycle_status,
  IF(a.date_start IS NULL OR a.date_end IS NULL OR a.observed_at IS NULL, 'UNKNOWN', 'DERIVED_DETERMINISTIC') AS lifecycle_evidence_class,
  a.freeze_at,
  a.auto_add_dates_count,
  (SELECT MIN(t)
   FROM UNNEST(SPLIT(a.auto_add_dates_csv, ',')) AS s, UNNEST([SAFE_CAST(s AS TIMESTAMP)]) AS t
   WHERE t >= a.observed_at) AS next_auto_add_at,
  a.is_participating,
  CASE
    WHEN a.is_participating IS TRUE THEN 'PARTICIPATING'
    WHEN a.is_participating IS FALSE THEN 'NOT_PARTICIPATING'
    ELSE 'UNKNOWN'
  END AS seller_participation_state,
  IF(a.is_participating IS NULL, 'UNKNOWN', 'DIRECT_SOURCE') AS seller_participation_evidence_class,
  CASE
    WHEN a.participating_products_count IS NOT NULL AND a.potential_products_count IS NOT NULL THEN 'RETURNED_BY_SOURCE'
    WHEN a.participating_products_count IS NULL AND a.potential_products_count IS NULL THEN 'NOT_RETURNED_BY_SOURCE'
    ELSE 'PARTIALLY_RETURNED_BY_SOURCE'
  END AS aggregate_state,
  a.participating_products_count,
  a.potential_products_count,
  a.banned_products_count,
  IFNULL(e.enumerated_participating, 0) AS enumerated_participating,
  IFNULL(e.enumerated_candidates, 0) AS enumerated_candidates,
  CASE
    WHEN a.participating_products_count = IFNULL(e.enumerated_participating, 0)
     AND a.potential_products_count = IFNULL(e.enumerated_candidates, 0) THEN 'OBSERVED'
    ELSE 'ENUMERATION_INCOMPLETE'
  END AS sku_membership_observability,
  CASE
    WHEN a.participating_products_count IS NULL OR a.potential_products_count IS NULL THEN 'DECLARED_COUNTS_NOT_RETURNED'
    WHEN a.participating_products_count = IFNULL(e.enumerated_participating, 0)
     AND a.potential_products_count = IFNULL(e.enumerated_candidates, 0) THEN 'ENUMERATION_MATCHES_DECLARED_COUNTS'
    ELSE 'ENUMERATED_ROWS_DIFFER_FROM_DECLARED_COUNTS'
  END AS sku_membership_observability_reason,
  a.discount_type,
  a.discount_value,
  a.is_voucher_action,
  a.with_targeting,
  a.source_payload_hash
FROM act a
JOIN obs o ON o.observation_id = a.observation_id
LEFT JOIN enumerated e ON e.observation_id = a.observation_id AND e.action_id = a.action_id
