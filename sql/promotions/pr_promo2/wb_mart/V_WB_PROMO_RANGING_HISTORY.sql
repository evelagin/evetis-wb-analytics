-- ============================================================================
-- PR-PROMO-2 · CANONICAL PROMOTION STATE · wb_mart.V_WB_PROMO_RANGING_HISTORY (VIEW)
-- Грейн: observation_id × promotion_id × tier_ordinal. Лестница бустинга акции WB
-- (условие → доля участия → бустинг) в момент каждого годного снимка.
-- Факт уровня АКЦИИ: ступени не распределяются по SKU и не означают наблюдённый эффект.
-- condition — значение источника без толкования (calculateProducts / productsInPromotion:
-- официального описания различия нет).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_RANGING_HISTORY`
OPTIONS (description = "PR-PROMO-2. Лестница бустинга акций WB по годным снимкам: одна строка на observation_id × promotion_id × tier_ordinal. Факт уровня акции, заявленный стимул, а не наблюдённый эффект; condition — значение источника без толкования.")
AS
WITH obs AS (
  SELECT observation_id
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
),
tiers AS (
  SELECT observation_id, observation_bucket, observed_at, promotion_id, tier_ordinal, condition,
    participation_rate, boost_pct
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING`
  WHERE environment = 'prod'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id, promotion_id, tier_ordinal ORDER BY ingested_at, source_payload_hash) = 1
)
SELECT
  'WB' AS marketplace,
  t.observation_id,
  t.observation_bucket AS observation_slot,
  t.observed_at,
  'OBSERVED' AS history_class,
  t.promotion_id,
  t.tier_ordinal,
  t.condition AS ranging_condition,
  t.participation_rate,
  t.boost_pct
FROM tiers t
JOIN obs o ON o.observation_id = t.observation_id
