-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-3 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md.
-- Нейтральный слой: читает только wb_mart, ozon_mart и evetis_mart. Экономику НЕ считает —
-- она посчитана в представлениях площадок по их каноническим контрактам (WB_FE_V1, Ozon
-- FORWARD_MODELLED). EXPECTED сопоставим между площадками, downside — НЕТ: у WB это
-- стресс p90, у Ozon WORST по максимальной логистике (downside_case на строке).
--
-- Грейн: marketplace × scenario_entity_key. Выводится ТОЛЬКО из истории: строка = последний
-- слот, где сценарий существовал. Последний слот площадки — по снимкам базиса (есть строки
-- BASELINE). Сценарий, которого нет в последнем слоте (акция исчезла, товар вышел из
-- списка), остаётся с present_in_latest_slot = FALSE: его экономика — на last_economics_slot.
-- Порога устаревания нет — в платформе он для этих данных не задан.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`
OPTIONS (description = "PR-PROMO-3. Текущая экономика сценариев акций, выведенная из истории: одна строка на marketplace × scenario_entity_key = последний слот сценария. Свежесть явная: последний слот базиса площадки, присутствие сценария в нём, возраст в минутах. Порога устаревания нет. Без рекомендаций.")
AS
WITH h AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
),
mp AS (
  SELECT marketplace, MAX(economics_slot) AS latest_economics_slot, MAX(basis_captured_at) AS latest_basis_captured_at
  FROM h
  WHERE scenario_type = 'BASELINE' AND basis_captured_at IS NOT NULL
  GROUP BY marketplace
),
agg AS (
  SELECT marketplace, scenario_entity_key, MIN(economics_slot) AS first_economics_slot, COUNT(*) AS slots_count
  FROM h
  GROUP BY marketplace, scenario_entity_key
),
latest AS (
  SELECT *
  FROM h
  QUALIFY ROW_NUMBER() OVER (PARTITION BY marketplace, scenario_entity_key ORDER BY economics_slot DESC) = 1
)
SELECT
  l.marketplace,
  l.scenario_entity_key,
  l.source_promotion_id,
  l.promotion_type,
  l.promotion_lifecycle_status,
  l.promotion_starts_at,
  l.marketplace_product_id,
  l.offer_id,
  l.internal_sku,
  l.mapping_status,
  l.sku_state,
  l.scenario_type,
  l.scenario_key,
  l.price_interpretation,
  l.auto_add_at,
  l.currency,
  l.scenario_price_rub,
  l.price_source,
  l.price_evidence_status,
  l.price_binding_evidence,
  l.promotion_link_evidence,
  l.observed_seller_price_rub,
  l.baseline_price_rub,
  l.baseline_price_source,
  l.baseline_price_observed_at,
  l.baseline_includes_promotion_effect,
  l.cogs_rub,
  l.cogs_effective_from,
  l.economics_model_version,
  l.temporal_basis,
  l.economics_status,
  l.baseline_contribution_expected_rub,
  l.promo_contribution_expected_rub,
  l.delta_contribution_expected_rub,
  l.baseline_margin_expected_pct,
  l.promo_margin_expected_pct,
  l.break_even_price_expected_rub,
  l.promo_price_minus_break_even_rub,
  l.downside_case,
  l.baseline_contribution_downside_rub,
  l.promo_contribution_downside_rub,
  l.promo_downside_negative,
  l.uplift_status,
  l.required_sales_uplift_pct,
  l.uplift_interpretation,
  l.economics_slot AS last_economics_slot,
  l.observation_id AS last_observation_id,
  l.basis_snapshot_id AS last_basis_snapshot_id,
  l.basis_captured_at AS last_basis_captured_at,
  a.first_economics_slot,
  a.slots_count,
  IFNULL(l.economics_slot = mp.latest_economics_slot, FALSE) AS present_in_latest_slot,
  mp.latest_economics_slot AS marketplace_latest_economics_slot,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), mp.latest_basis_captured_at, MINUTE) AS basis_age_minutes
FROM latest l
JOIN agg a ON a.marketplace = l.marketplace AND a.scenario_entity_key = l.scenario_entity_key
LEFT JOIN mp ON mp.marketplace = l.marketplace
