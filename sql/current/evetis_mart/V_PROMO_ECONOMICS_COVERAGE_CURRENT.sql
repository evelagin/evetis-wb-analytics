-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_ECONOMICS_COVERAGE_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-3 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md.
-- Нейтральный слой: читает только wb_mart, ozon_mart и evetis_mart. Экономику НЕ считает —
-- она посчитана в представлениях площадок по их каноническим контрактам (WB_FE_V1, Ozon
-- FORWARD_MODELLED). EXPECTED сопоставим между площадками, downside — НЕТ: у WB это
-- стресс p90, у Ozon WORST по максимальной логистике (downside_case на строке).
--
-- Грейн: marketplace × source_promotion_id. Почему по акции есть или нет экономики. Здесь
-- видно, что у автоакций WB экономики по SKU нет потому, что состав не наблюдаем, а не
-- потому, что её забыли посчитать. Строк уровня SKU для таких акций не создаётся.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_COVERAGE_CURRENT`
OPTIONS (description = "PR-PROMO-3. Покрытие экономикой по каждой акции в текущем состоянии: одна строка на marketplace × source_promotion_id. Сколько сценариев есть и сколько из них вычислимы, по смыслу цены; для акций с ненаблюдаемым составом — PROMO_MEMBERSHIP_NOT_OBSERVABLE без выдуманных строк SKU.")
AS
WITH p AS (
  SELECT marketplace, source_promotion_id, promotion_name, promotion_type, current_lifecycle_status,
    present_in_latest_observation, sku_membership_observability, last_observed_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT`
),
s AS (
  SELECT marketplace, source_promotion_id,
    COUNT(*) AS scenarios_total,
    COUNTIF(economics_status = 'COMPUTABLE') AS scenarios_computable,
    COUNTIF(price_interpretation = 'CURRENT_PARTICIPATION_ACTION_PRICE') AS participation_scenarios,
    COUNTIF(price_interpretation = 'CANDIDATE_MAX_QUALIFYING_ENTRY_PRICE') AS candidate_scenarios,
    COUNTIF(price_interpretation = 'SCHEDULED_AUTO_ADD_PRICE') AS scheduled_auto_add_scenarios,
    COUNTIF(price_interpretation = 'AUTO_ADD_ELIGIBLE_PRICE') AS eligible_auto_add_scenarios,
    STRING_AGG(DISTINCT IF(economics_status != 'COMPUTABLE', economics_status, NULL), ',' ORDER BY IF(economics_status != 'COMPUTABLE', economics_status, NULL)) AS non_computable_statuses,
    MAX(last_economics_slot) AS last_economics_slot
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`
  WHERE scenario_type = 'PROMO' AND present_in_latest_slot
  GROUP BY marketplace, source_promotion_id
)
SELECT
  p.marketplace,
  p.source_promotion_id,
  p.promotion_name,
  p.promotion_type,
  p.current_lifecycle_status,
  p.present_in_latest_observation,
  p.sku_membership_observability,
  IFNULL(s.scenarios_total, 0) AS scenarios_total,
  IFNULL(s.scenarios_computable, 0) AS scenarios_computable,
  IFNULL(s.participation_scenarios, 0) AS participation_scenarios,
  IFNULL(s.candidate_scenarios, 0) AS candidate_scenarios,
  IFNULL(s.scheduled_auto_add_scenarios, 0) AS scheduled_auto_add_scenarios,
  IFNULL(s.eligible_auto_add_scenarios, 0) AS eligible_auto_add_scenarios,
  s.non_computable_statuses,
  CASE
    WHEN p.sku_membership_observability = 'NOT_OBSERVABLE' THEN 'PROMO_MEMBERSHIP_NOT_OBSERVABLE'
    WHEN IFNULL(s.scenarios_total, 0) = 0 THEN 'NO_SKU_PRICE_EVIDENCE'
    WHEN s.scenarios_computable = s.scenarios_total THEN 'FULLY_COMPUTABLE'
    WHEN s.scenarios_computable = 0 THEN 'NOT_COMPUTABLE'
    ELSE 'PARTIALLY_COMPUTABLE'
  END AS economics_coverage_status,
  s.last_economics_slot,
  p.last_observed_at
FROM p
LEFT JOIN s ON s.marketplace = p.marketplace AND s.source_promotion_id = p.source_promotion_id
