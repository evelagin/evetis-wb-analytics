-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_SKU_STATE_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × source_promotion_id × marketplace_product_id. Последнее наблюдение
-- пары, выведенное из истории. Если пары нет в последнем годном снимке площадки
-- (акция исчезла или товар выпал из каталога), строка остаётся с
-- present_in_latest_observation = FALSE: её состояние — состояние на last_observed_at,
-- а не «сейчас». Порога устаревания нет (не задан в платформе).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_CURRENT`
OPTIONS (description = "PR-PROMO-2. Текущее разрешённое состояние товара в акции обеих площадок, выведенное из истории: одна строка на marketplace × source_promotion_id × marketplace_product_id = последнее наблюдение пары. Свежесть явная: последний годный снимок площадки, возраст, присутствие пары в нём; отсутствующая пара несёт состояние на last_observed_at. Порога устаревания нет.")
AS
WITH h AS (
  SELECT marketplace, source_promotion_id, observation_id, observed_at, promotion_type, promotion_lifecycle_status,
    marketplace_product_id, offer_id, internal_sku, mapping_status, participation_state,
    participation_evidence_class, auto_add_state, auto_add_evidence_class, resolved_state,
    resolved_state_evidence_class, resolution_rule, in_participants_list, in_candidates_list,
    in_auto_add_scheduled_list, in_auto_add_eligible_list, evidence_conflict, participation_add_mode,
    next_scheduled_auto_add_at, marketing_actions_corroborated
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`
),
mp AS (
  SELECT marketplace, observation_id AS marketplace_latest_observation_id,
    observed_at AS marketplace_latest_observed_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace ORDER BY observed_at DESC, observation_id DESC) = 1
),
agg AS (
  SELECT marketplace, source_promotion_id, marketplace_product_id,
    MIN(observed_at) AS first_observed_at, COUNT(*) AS observations_count
  FROM h
  GROUP BY marketplace, source_promotion_id, marketplace_product_id
),
latest AS (
  SELECT *
  FROM h
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace, source_promotion_id, marketplace_product_id
                             ORDER BY observed_at DESC, observation_id DESC) = 1
)
SELECT
  l.marketplace,
  l.source_promotion_id,
  l.marketplace_product_id,
  l.offer_id,
  l.internal_sku,
  l.mapping_status,
  l.promotion_type,
  l.promotion_lifecycle_status AS promotion_lifecycle_status_at_last_observation,
  l.participation_state,
  l.participation_evidence_class,
  l.auto_add_state,
  l.auto_add_evidence_class,
  l.resolved_state,
  l.resolved_state_evidence_class,
  l.resolution_rule,
  l.in_participants_list,
  l.in_candidates_list,
  l.in_auto_add_scheduled_list,
  l.in_auto_add_eligible_list,
  l.evidence_conflict,
  l.participation_add_mode,
  l.next_scheduled_auto_add_at,
  l.marketing_actions_corroborated,
  l.observation_id AS last_observation_id,
  l.observed_at AS last_observed_at,
  a.first_observed_at,
  a.observations_count,
  IFNULL(l.observation_id = mp.marketplace_latest_observation_id, FALSE) AS present_in_latest_observation,
  mp.marketplace_latest_observation_id,
  mp.marketplace_latest_observed_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), mp.marketplace_latest_observed_at, MINUTE) AS observation_age_minutes,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), l.observed_at, MINUTE) AS last_observed_age_minutes
FROM latest l
JOIN agg a
  ON a.marketplace = l.marketplace AND a.source_promotion_id = l.source_promotion_id
 AND a.marketplace_product_id = l.marketplace_product_id
LEFT JOIN mp ON mp.marketplace = l.marketplace
