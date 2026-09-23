-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_STATE_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × source_promotion_id. Выводится ТОЛЬКО из истории — второй
-- изменяемой истины нет. Строка = последнее наблюдение сущности. Последний годный снимок
-- площадки берётся из V_PROMO_OBSERVATION_HISTORY (а не из строк сущностей): иначе пустой
-- снимок или исчезнувшая акция выглядели бы «текущими».
--
-- Свежесть видна всегда: marketplace_latest_observed_at, observation_age_minutes,
-- present_in_latest_observation. Порога «устарело» нет: в платформе он не задан для этих
-- наблюдателей (реестр OPS_PIPELINE_REGISTRY их ещё не содержит, L-6), придумывать нельзя.
--
-- current_lifecycle_status: если акция есть в последнем снимке — её статус в нём
-- (DERIVED_DETERMINISTIC). Если исчезла (Ozon убирает завершённые акции из /v1/actions) —
-- статус по ПОСЛЕДНИМ ИЗВЕСТНЫМ датам на момент последнего снимка площадки, с классом
-- STRONGLY_INFERRED: даты могли измениться после исчезновения. CURRENT_TIMESTAMP в выводе
-- статуса не участвует, только в возрасте наблюдения.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT`
OPTIONS (description = "PR-PROMO-2. Текущее состояние акций обеих площадок, выведенное из истории: одна строка на marketplace × source_promotion_id = последнее наблюдение сущности. Свежесть явная: последний годный снимок площадки, возраст в минутах, присутствие акции в нём. Для исчезнувшей акции current_lifecycle_status выведен из последних известных дат с классом STRONGLY_INFERRED. Порога устаревания нет — в платформе не задан.")
AS
WITH h AS (
  SELECT marketplace, source_promotion_id, observation_id, observed_at, observation_status, promotion_name,
    promotion_type, starts_at, ends_at, starts_on_msk, ends_on_msk, lifecycle_status, lifecycle_evidence_class,
    seller_participation_state, seller_participation_evidence_class, aggregate_state, source_in_promo_count,
    source_not_in_promo_count, source_counter_basis, sku_membership_observability, sku_membership_observability_reason
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`
),
mp AS (
  SELECT marketplace, observation_id AS marketplace_latest_observation_id,
    observed_at AS marketplace_latest_observed_at, observation_status AS marketplace_latest_observation_status
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace ORDER BY observed_at DESC, observation_id DESC) = 1
),
agg AS (
  SELECT marketplace, source_promotion_id, MIN(observed_at) AS first_observed_at, COUNT(*) AS observations_count
  FROM h
  GROUP BY marketplace, source_promotion_id
),
latest AS (
  SELECT *
  FROM h
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace, source_promotion_id ORDER BY observed_at DESC, observation_id DESC) = 1
)
SELECT
  l.marketplace,
  l.source_promotion_id,
  l.promotion_name,
  l.promotion_type,
  l.starts_at,
  l.ends_at,
  l.starts_on_msk,
  l.ends_on_msk,
  l.observation_id AS last_observation_id,
  l.observed_at AS last_observed_at,
  a.first_observed_at,
  a.observations_count,
  l.lifecycle_status AS lifecycle_status_at_last_observation,
  CASE
    WHEN l.observation_id = mp.marketplace_latest_observation_id THEN l.lifecycle_status
    WHEN l.starts_at IS NULL OR l.ends_at IS NULL OR mp.marketplace_latest_observed_at IS NULL THEN 'UNKNOWN'
    WHEN mp.marketplace_latest_observed_at < l.starts_at THEN 'UPCOMING'
    WHEN mp.marketplace_latest_observed_at > l.ends_at THEN 'ENDED'
    ELSE 'ACTIVE'
  END AS current_lifecycle_status,
  CASE
    WHEN l.observation_id = mp.marketplace_latest_observation_id THEN l.lifecycle_evidence_class
    WHEN l.starts_at IS NULL OR l.ends_at IS NULL OR mp.marketplace_latest_observed_at IS NULL THEN 'UNKNOWN'
    ELSE 'STRONGLY_INFERRED'
  END AS current_lifecycle_evidence_class,
  IFNULL(l.observation_id = mp.marketplace_latest_observation_id, FALSE) AS present_in_latest_observation,
  mp.marketplace_latest_observation_id,
  mp.marketplace_latest_observed_at,
  mp.marketplace_latest_observation_status,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), mp.marketplace_latest_observed_at, MINUTE) AS observation_age_minutes,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), l.observed_at, MINUTE) AS last_observed_age_minutes,
  l.observation_status AS last_observation_status,
  l.seller_participation_state,
  l.seller_participation_evidence_class,
  l.aggregate_state,
  l.source_in_promo_count,
  l.source_not_in_promo_count,
  l.source_counter_basis,
  l.sku_membership_observability,
  l.sku_membership_observability_reason
FROM latest l
JOIN agg a ON a.marketplace = l.marketplace AND a.source_promotion_id = l.source_promotion_id
LEFT JOIN mp ON mp.marketplace = l.marketplace
