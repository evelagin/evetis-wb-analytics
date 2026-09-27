-- ============================================================================
-- AIE V1 (PR-3, финализация 2026-09-27) · ozon_mart.V_AIE_OZON_ECON_GUARD (VIEW) · sync_state: pending_deploy
-- Грейн: as_of_date × internal_sku. Одна дата решения (CTE aie_clock).
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §7.
--
-- Экономический ограничитель SKU Ozon. Переиспользуются:
--   • ozon_mart.V_OZON_AGENT_DECISION_INPUT — прогнозная экономика (best / expected / worst) и гейт
--     решений агента; только текущее состояние, в replay — SKIPPED_NOT_REPLAYABLE;
--   • ozon_mart.V_OZON_MART_FRESHNESS.agent_decision_gate — свежесть (только текущее состояние);
--   • ozon_mart.FCT_OZON_SKU_PNL_DAILY — фактический вклад до рекламы за 28 суток до as_of (история).
-- Структурная пауза (L4) требует отрицательного вклада во ВСЕХ доступных оптимистичных базах:
-- best case прогноза и фактический вклад (если он вычислен). Одна фактическая база паузу не определяет.
-- Разложение цены (fwd_*) — компоненты лучшего сценария: логистика = минимальная по API + последняя миля.
-- Безубыточной цены контракт Ozon не отдаёт — fwd_breakeven_price_rub = NULL, не выдумывается.
-- 🔴 production_grade = FALSE (Ozon до PR-8). 🔴 Не прибыль. 🔴 NULL ≠ 0.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD`
OPTIONS (description = "AIE V1. Экономический ограничитель SKU Ozon на дату решения: прогноз V_OZON_AGENT_DECISION_INPUT (best/expected/worst, гейт агента — только текущее состояние), свежесть V_OZON_MART_FRESHNESS, фактический вклад до рекламы FCT_OZON_SKU_PNL_DAILY за 28 суток. production_grade = FALSE до PR-8. Не прибыль. NULL = не вычислено.")
AS
WITH
-- @aie:clock:begin
aie_clock AS (
  SELECT
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY) AS as_of_date,
    CURRENT_TIMESTAMP() AS knowledge_ts,
    'CURRENT' AS run_mode
),
-- @aie:clock:end
fwd AS (
  SELECT internal_sku, MIN(ozon_sku) AS ozon_sku, COUNT(*) AS fwd_rows,
         MIN(contribution_best_case) AS contribution_best_case,
         MIN(contribution_expected) AS contribution_expected,
         MIN(contribution_worst_case) AS contribution_worst_case,
         MIN(break_even_drr_best_pct) AS break_even_drr_best_pct,
         MIN(break_even_drr_expected_pct) AS break_even_drr_expected_pct,
         MIN(break_even_drr_worst_pct) AS break_even_drr_worst_pct,
         LOGICAL_AND(forward_economics_ready) AS forward_economics_ready,
         MIN(agent_decision_gate) AS agent_decision_gate,
         MIN(mart_computed_at) AS mart_computed_at,
         MIN(seller_base_price) AS seller_base_price,
         MIN(current_commission_rub) AS current_commission_rub,
         MIN(current_commission_pct) AS current_commission_pct,
         MIN(acquiring_rub) AS acquiring_rub,
         MIN(logistics_min_rub) AS logistics_min_rub,
         MIN(last_mile_rub) AS last_mile_rub,
         MIN(management_cogs) AS management_cogs
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_AGENT_DECISION_INPUT`
  GROUP BY internal_sku
),
fresh AS (
  SELECT MIN(agent_decision_gate) AS mart_agent_decision_gate, MIN(freshness_status) AS mart_freshness_status,
         MIN(source_max_ads_sku_date) AS source_max_ads_sku_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`
),
realized AS (
  SELECT d.internal_sku,
         COUNT(*) AS fact_days,
         SUM(d.realized_qty) AS realized_qty,
         SUM(d.seller_base_revenue_rub) AS seller_base_revenue_rub,
         SUM(d.contribution_before_ads_rub) AS contribution_before_ads_rub,
         SUM(d.ad_spend_attributed_rub) AS ad_spend_attributed_rub,
         SUM(d.cogs_missing_qty) AS cogs_missing_qty
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` d
  CROSS JOIN aie_clock c
  WHERE d.fact_date <= c.as_of_date AND d.fact_date > DATE_SUB(c.as_of_date, INTERVAL 28 DAY)
  GROUP BY d.internal_sku
),
skus AS (
  SELECT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current
  GROUP BY internal_sku
)
SELECT
  c.as_of_date,
  c.knowledge_ts,
  c.run_mode,
  s.internal_sku,
  f.ozon_sku,
  CASE
    WHEN c.run_mode != 'CURRENT' THEN 'SKIPPED_NOT_REPLAYABLE'
    WHEN f.internal_sku IS NULL THEN 'MISSING'
    WHEN NOT IFNULL(f.forward_economics_ready, FALSE) THEN 'NOT_READY'
    ELSE 'AVAILABLE'
  END AS forward_availability,
  f.contribution_best_case,
  f.contribution_expected,
  f.contribution_worst_case,
  f.break_even_drr_best_pct,
  f.break_even_drr_expected_pct,
  f.break_even_drr_worst_pct,
  -- ── разложение цены лучшего сценария (диагностика PRICE_BELOW_BREAKEVEN / PRICING_REVIEW) ──
  f.seller_base_price AS fwd_price_rub,
  CAST(NULL AS NUMERIC) AS fwd_breakeven_price_rub,
  f.current_commission_rub AS fwd_commission_rub,
  f.current_commission_pct AS fwd_commission_pct,
  f.acquiring_rub AS fwd_acquiring_rub,
  100 * SAFE_DIVIDE(f.acquiring_rub, f.seller_base_price) AS fwd_acquiring_pct,
  f.logistics_min_rub + IFNULL(f.last_mile_rub, 0) AS fwd_logistics_rub,
  100 * SAFE_DIVIDE(f.logistics_min_rub + IFNULL(f.last_mile_rub, 0), f.seller_base_price) AS fwd_logistics_pct,
  f.management_cogs AS fwd_product_cogs_rub,
  100 * SAFE_DIVIDE(f.management_cogs, f.seller_base_price) AS fwd_product_cogs_pct,
  f.agent_decision_gate AS forward_agent_decision_gate,
  fr.mart_agent_decision_gate,
  fr.mart_freshness_status,
  fr.source_max_ads_sku_date,
  r.fact_days AS realized_fact_days,
  r.realized_qty,
  r.seller_base_revenue_rub,
  -- Себестоимость не покрыта хотя бы для одной единицы — фактический вклад не вычислен (NULL, не 0).
  IF(IFNULL(r.cogs_missing_qty, 0) = 0, r.contribution_before_ads_rub, NULL) AS realized_contribution_before_ads_rub,
  r.ad_spend_attributed_rub AS realized_ad_spend_attributed_rub,
  (IFNULL(r.cogs_missing_qty, 0) = 0 AND r.contribution_before_ads_rub < 0) AS realized_negative_before_ads,
  (c.run_mode = 'CURRENT' AND IFNULL(f.forward_economics_ready, FALSE) AND f.contribution_best_case < 0) AS forward_best_negative,
  CASE
    WHEN c.run_mode = 'CURRENT' AND IFNULL(f.forward_economics_ready, FALSE)
      THEN (f.contribution_best_case < 0
            AND (r.internal_sku IS NULL OR IFNULL(r.cogs_missing_qty, 0) > 0 OR r.contribution_before_ads_rub < 0))
    ELSE FALSE
  END AS negative_all_available_bases,
  (c.run_mode = 'CURRENT'
    AND f.agent_decision_gate = 'DECISIONS_ALLOWED'
    AND fr.mart_agent_decision_gate = 'DECISIONS_ALLOWED') AS agent_gate_open,
  FALSE AS production_grade
FROM skus s
CROSS JOIN aie_clock c
CROSS JOIN fresh fr
LEFT JOIN fwd f ON f.internal_sku = s.internal_sku
LEFT JOIN realized r ON r.internal_sku = s.internal_sku;
