-- ============================================================================
-- PR-PROMO-2 · CANONICAL PROMOTION STATE · wb_mart.V_WB_PROMO_HISTORY (VIEW)
-- Грейн: observation_id × promotion_id. Состояние акции WB в момент каждого годного снимка.
--
-- Жизненный цикл выводится ТОЛЬКО из дат источника относительно observed_at снимка,
-- а не относительно CURRENT_TIMESTAMP(): состояние в момент T воспроизводимо всегда.
-- WB отдаёт endDateTime как последнюю секунду (20:59:59Z = 23:59:59 МСК), граница
-- включительная: observed_at <= ends_at → ACTIVE.
--
-- Состав автоакции по SKU НЕ наблюдаем (метод /nomenclatures к type='auto' неприменим,
-- PR-PROMO-1): sku_membership_observability = NOT_OBSERVABLE. Строк уровня SKU для таких
-- акций нет нигде в каноническом слое — отсутствие строки здесь НЕ означает «не участвует».
-- Агрегаты details (inPromoActionTotal и др.) остаются фактами уровня акции.
--
-- NULL ≠ 0: у завершённой акции /details отдаёт пустой массив → details_available = FALSE,
-- счётчики NULL, seller_participation_state = UNKNOWN. Явный 0 → NOT_PARTICIPATING.
-- Дедупликация: одна строка на (observation_id, promotion_id) по ingested_at; дубли RAW
-- невозможны по контракту load-джобы и ловятся DQ, а не прячутся.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`
OPTIONS (description = "PR-PROMO-2. Каноническая история акций WB: одна строка на годный снимок × promotion_id. lifecycle_status выведен из дат источника относительно observed_at снимка (не текущего времени). Для автоакций sku_membership_observability = NOT_OBSERVABLE: состав по SKU источник не отдаёт, строк SKU нет, отсутствие строки не означает неучастие. NULL счётчиков = не отдано источником, не ноль. Без экономики.")
AS
WITH obs AS (
  SELECT observation_id, observation_status
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
),
cal AS (
  SELECT observation_id, observation_bucket, observed_at, promotion_id, promotion_name, promotion_type,
    is_auto_promotion, starts_at, ends_at, details_available, in_promo_total, in_promo_leftovers,
    not_in_promo_total, not_in_promo_leftovers, participation_pct, exception_products_count,
    ranging_tiers, ranging_condition, nomenclature_status, source_payload_hash
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, promotion_id ORDER BY ingested_at, source_payload_hash) = 1
)
SELECT
  'WB' AS marketplace,
  c.observation_id,
  c.observation_bucket AS observation_slot,
  c.observed_at,
  'OBSERVED' AS history_class,
  o.observation_status,
  c.promotion_id,
  c.promotion_name,
  c.promotion_type,
  c.is_auto_promotion,
  c.starts_at,
  c.ends_at,
  DATE(c.starts_at, 'Europe/Moscow') AS starts_on_msk,
  DATE(c.ends_at, 'Europe/Moscow') AS ends_on_msk,
  CASE
    WHEN c.starts_at IS NULL OR c.ends_at IS NULL OR c.observed_at IS NULL THEN 'UNKNOWN'
    WHEN c.observed_at < c.starts_at THEN 'UPCOMING'
    WHEN c.observed_at > c.ends_at THEN 'ENDED'
    ELSE 'ACTIVE'
  END AS lifecycle_status,
  IF(c.starts_at IS NULL OR c.ends_at IS NULL OR c.observed_at IS NULL, 'UNKNOWN', 'DERIVED_DETERMINISTIC') AS lifecycle_evidence_class,
  c.details_available,
  CASE
    WHEN c.details_available IS TRUE THEN 'RETURNED_BY_SOURCE'
    WHEN c.details_available IS FALSE THEN 'NOT_RETURNED_BY_SOURCE'
    ELSE 'UNKNOWN'
  END AS aggregate_state,
  c.in_promo_total,
  c.in_promo_leftovers,
  c.not_in_promo_total,
  c.not_in_promo_leftovers,
  c.participation_pct,
  c.exception_products_count,
  c.ranging_tiers,
  c.ranging_condition,
  CASE
    WHEN c.in_promo_total IS NULL THEN 'UNKNOWN'
    WHEN c.in_promo_total > 0 THEN 'PARTICIPATING'
    ELSE 'NOT_PARTICIPATING'
  END AS seller_participation_state,
  IF(c.in_promo_total IS NULL, 'UNKNOWN', 'DERIVED_DETERMINISTIC') AS seller_participation_evidence_class,
  c.nomenclature_status,
  CASE c.nomenclature_status
    WHEN 'FETCHED' THEN 'OBSERVED'
    WHEN 'EMPTY' THEN 'OBSERVED_EMPTY'
    WHEN 'SKIPPED_AUTO_PROMOTION' THEN 'NOT_OBSERVABLE'
    WHEN 'UNSUPPORTED_422' THEN 'NOT_OBSERVABLE'
    ELSE 'UNKNOWN'
  END AS sku_membership_observability,
  CASE c.nomenclature_status
    WHEN 'FETCHED' THEN 'SOURCE_ENUMERATED_NOMENCLATURES'
    WHEN 'EMPTY' THEN 'SOURCE_RETURNED_EMPTY_LIST'
    WHEN 'SKIPPED_AUTO_PROMOTION' THEN 'AUTO_PROMOTION_SKU_LIST_NOT_PROVIDED_BY_SOURCE'
    WHEN 'UNSUPPORTED_422' THEN 'SOURCE_ANSWERED_HTTP_422'
    ELSE 'UNRECOGNISED_NOMENCLATURE_STATUS'
  END AS sku_membership_observability_reason,
  c.source_payload_hash
FROM cal c
JOIN obs o ON o.observation_id = c.observation_id
