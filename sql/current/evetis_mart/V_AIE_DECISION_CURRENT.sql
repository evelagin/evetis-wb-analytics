-- ============================================================================
-- AIE V1 (PR-4, финализация 2026-09-27) · evetis_mart.V_AIE_DECISION_CURRENT (VIEW) · sync_state: pending_deploy
-- Грейн: as_of_date × policy_id × marketplace × campaign_id × marketplace_sku.
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md; коды — sql/ads_intel/aie_reason_codes_v1.json
-- (rank и state сверяет tools/tests/test_aie.py); политика — evetis_ref.V_AIE_POLICY.
--
-- Что сегодня сделать с рекламой каждой пары и почему. РЕКОМЕНДАЦИЯ, НЕ ДЕЙСТВИЕ: записи в кабинеты
-- и цены нет, F-18 остаётся блокером любого write-пути.
--
-- Решения владельца 2026-09-27 (Shadow V1):
--   • текущий набор рекомендаций (universe = CURRENT_ACTIONABLE): статус кампании активен (WB 9/11, Ozon
--     только RUNNING — INACTIVE не доказан и исключён, fail closed) и не больше K = 14 суток с последнего расхода; остальные пары остаются в исторической
--     базе доказательств (HISTORICAL_EVIDENCE_ONLY) с основной причиной SCOPE_CAMPAIGN_INACTIVE — в т. ч.
--     кампания, перешедшая в статус 7 в день с расходом: день сохраняется в истории, рекомендаций больше нет;
--   • P3 = 0.90; P7 = 3 % (в V_AIE_POLICY);
--   • P4: без жёстких исключений SKU; структурная пауза при отрицательной прогнозной base-экономике —
--     PAUSE_CANDIDATE с основной причиной PRICE_BELOW_BREAKEVEN, контекст PRICING_REVIEW и разложение цены;
--   • P6 (роли баз): realized — граница доказательности DECREASE; forward base — структурный ценовой
--     ограничитель; forward conservative — ограничитель против INCREASE; forward stress — флаг риска;
--   • P1 = NULL → INCREASE недостижим; INCREASE_CANDIDATE — только диагностика, не рекомендация;
--   • P2, P5 (нижняя и верхняя границы покрытия), P13 не утверждены — NULL, скрытых значений нет;
--   • P15: множители Ads-4 0,7/1,3 — только диагностика запросов, здесь не участвуют;
--   • FINANCIAL_DATA_MATURE: конец фактического окна не позже последней даты с финансами (флаг колонкой).
-- 🔴 FIN CONTRACT V2: ДРР = атрибутированный расход / выкупы SKU; биллинг не читается.
-- 🔴 Ozon: production_grade = FALSE, направлений ставки нет, автопилот — P9.
-- Интервал Пуассона для выкупов — Вильсон–Хилферти (дизайн §4); z — для поддерживаемых P3 {0.80, 0.90, 0.95}.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`
OPTIONS (description = "AIE V1. Рекомендация по рекламе для каждой пары площадка × кампания × SKU на дату решения: INCREASE / DECREASE / HOLD / PAUSE_CANDIDATE / INSUFFICIENT_DATA / BLOCKED_BY_GUARDRAIL, основная причина, все причины, объяснение; набор CURRENT_ACTIONABLE или HISTORICAL_EVIDENCE_ONLY. Рекомендация, не действие. Политика — evetis_ref.V_AIE_POLICY (P3 = 0.90, P7 = 3 %, K = 14; P1 не задан — INCREASE недостижим, INCREASE_CANDIDATE — диагностика). ДРР — атрибуция / выкупы, не биллинг. Ozon не production-grade.")
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
pol AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`
),
fresh AS (
  -- Только режим CURRENT: слои, от которых зависит решение, в статусе OK.
  SELECT STRING_AGG(layer_code, ',' ORDER BY layer_code) AS layers_not_ok
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DATA_FRESHNESS`
  WHERE layer_code IN ('ads_fullstats', 'fact_ads_sku_daily', 'mart_sku_daily', 'orders', 'sales', 'stocks', 'finance')
    AND status != 'OK'
),
bundles AS (
  SELECT internal_sku, LOGICAL_OR(is_bundle) AS is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
  GROUP BY internal_sku
),
cover AS (
  SELECT h.internal_sku, h.snapshot_date AS cover_snapshot_date, h.ct_cover_days_30d_at_snapshot AS cover_days
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY` h
  JOIN (
    SELECT i.internal_sku, MAX(i.snapshot_date) AS md
    FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY` i
    CROSS JOIN aie_clock c
    WHERE i.snapshot_date <= c.as_of_date
    GROUP BY i.internal_sku
  ) m ON m.internal_sku = h.internal_sku AND m.md = h.snapshot_date
),
wb AS (
  SELECT
    w.policy_id, 'WB' AS marketplace,
    w.internal_sku, CAST(w.nm_id AS STRING) AS marketplace_sku, CAST(w.advert_id AS STRING) AS campaign_id,
    w.sku_mapping_count, w.include_in_ads_analysis,
    w.campaign_status_raw AS campaign_status, w.campaign_type_raw AS campaign_type, w.payment_type_raw AS payment_type,
    w.bid_type_raw AS bid_type, CAST(NULL AS STRING) AS autopilot_strategy, FALSE AS autopilot_controls_bids,
    (w.campaign_status_raw IN ('9', '11')) AS campaign_active_status,
    w.config_observed, w.campaign_config_ts, w.search_bid_rub AS current_bid_search_rub,
    w.recommendations_bid_rub AS current_bid_recommendations_rub, w.bid_config_ts AS bid_observed_at,
    w.last_spend_date, w.days_since_last_spend,
    w.ads_data_through, w.stock_global_snapshot_date, w.stock_status, w.stock_units, 0 AS p26_suspect_days,
    w.window_start_nominal, w.effective_window_start, w.campaign_regime_start, w.bid_regime_start,
    w.price_regime_start, CAST(NULL AS DATE) AS promo_regime_start, w.price_unobserved_part,
    w.effective_days, w.stockout_days_excluded, w.spend_28d_attributed_rub,
    w.eff_views, w.eff_clicks, w.eff_orders_raw AS eff_orders, w.eff_spend_attributed_rub, w.eff_cpo_rub,
    w.evidence_status, w.waste_zero_orders_ads4,
    e.econ_as_of, e.econ_window_start, e.economic_state, e.finance_known_through,
    e.financial_data_mature, e.econ_window_end_lag_days,
    e.buyouts_qty AS sku_buyouts_qty, e.buyouts_rub AS sku_buyouts_rub,
    e.ad_spend_attributed_rub AS sku_ad_spend_attributed_rub, e.actual_ad_drr AS sku_drr_point,
    CAST(e.max_ad_drr_breakeven AS FLOAT64) AS limit_realized,
    e.forward_max_drr_conservative AS limit_forward_conservative, e.forward_max_drr_stress AS limit_forward_stress,
    e.forward_availability, e.realized_negative_before_ads, e.forward_base_negative, e.negative_all_available_bases,
    CAST(e.forward_price_rub AS FLOAT64) AS fwd_price_rub, e.fwd_breakeven_price_rub,
    e.forward_contribution_before_ads_rub AS fwd_contribution_before_ads_rub,
    CAST(e.fwd_commission_rub AS FLOAT64) AS fwd_commission_rub, CAST(e.fwd_commission_pct AS FLOAT64) AS fwd_commission_pct,
    e.fwd_acquiring_rub, e.fwd_acquiring_pct,
    CAST(e.fwd_logistics_rub AS FLOAT64) AS fwd_logistics_rub, e.fwd_logistics_pct,
    CAST(e.fwd_product_cogs_rub AS FLOAT64) AS fwd_product_cogs_rub, e.fwd_product_cogs_pct,
    TRUE AS agent_gate_open, TRUE AS production_grade_data
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE` w
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD` e
    ON e.nm_id = w.nm_id AND e.policy_id = w.policy_id
),
oz AS (
  SELECT
    o.policy_id, 'OZON' AS marketplace,
    o.internal_sku, o.sku AS marketplace_sku, o.campaign_id,
    o.sku_mapping_count, CAST(NULL AS BOOL) AS include_in_ads_analysis,
    o.campaign_state AS campaign_status, o.adv_object_type AS campaign_type, o.payment_type,
    CAST(NULL AS STRING) AS bid_type, o.product_autopilot_strategy AS autopilot_strategy,
    IFNULL(o.autopilot_controls_bids, FALSE) AS autopilot_controls_bids,
    -- Только RUNNING (fail closed): семантика состояния INACTIVE в проекте не доказана — в аудите 2026-08-30
    -- INACTIVE-кампании отнесены к «не работают»; до доказательства пара уходит в историческую базу.
    (o.campaign_state = 'RUNNING') AS campaign_active_status,
    (o.campaign_snapshot_date IS NOT NULL) AS config_observed,
    CAST(NULL AS TIMESTAMP) AS campaign_config_ts, CAST(NULL AS FLOAT64) AS current_bid_search_rub,
    CAST(NULL AS FLOAT64) AS current_bid_recommendations_rub, CAST(NULL AS TIMESTAMP) AS bid_observed_at,
    o.last_spend_date, o.days_since_last_spend,
    o.ads_data_through, o.stock_global_snapshot_date, o.stock_status, o.stock_units, o.p26_suspect_days,
    o.window_start_nominal, o.effective_window_start, CAST(NULL AS DATE) AS campaign_regime_start,
    CAST(NULL AS DATE) AS bid_regime_start, o.price_regime_start, o.promo_regime_start, o.price_unobserved_part,
    o.effective_days, o.stockout_days_excluded, CAST(o.spend_28d_attributed_rub AS NUMERIC) AS spend_28d_attributed_rub,
    o.eff_impressions AS eff_views, o.eff_clicks, o.eff_orders, CAST(o.eff_spend_attributed_rub AS NUMERIC) AS eff_spend_attributed_rub,
    CAST(o.eff_cpo_rub AS NUMERIC) AS eff_cpo_rub,
    o.evidence_status, FALSE AS waste_zero_orders_ads4,
    CAST(NULL AS DATE) AS econ_as_of, CAST(NULL AS DATE) AS econ_window_start, CAST(NULL AS STRING) AS economic_state,
    CAST(NULL AS DATE) AS finance_known_through, CAST(NULL AS BOOL) AS financial_data_mature,
    CAST(NULL AS INT64) AS econ_window_end_lag_days,
    CAST(NULL AS INT64) AS sku_buyouts_qty, CAST(NULL AS NUMERIC) AS sku_buyouts_rub,
    CAST(NULL AS NUMERIC) AS sku_ad_spend_attributed_rub, CAST(NULL AS NUMERIC) AS sku_drr_point,
    CAST(NULL AS FLOAT64) AS limit_realized,
    CAST(NULL AS FLOAT64) AS limit_forward_conservative, CAST(NULL AS FLOAT64) AS limit_forward_stress,
    oe.forward_availability, oe.realized_negative_before_ads, oe.forward_best_negative AS forward_base_negative,
    oe.negative_all_available_bases,
    CAST(oe.fwd_price_rub AS FLOAT64) AS fwd_price_rub, CAST(oe.fwd_breakeven_price_rub AS FLOAT64) AS fwd_breakeven_price_rub,
    CAST(oe.contribution_best_case AS FLOAT64) AS fwd_contribution_before_ads_rub,
    CAST(oe.fwd_commission_rub AS FLOAT64) AS fwd_commission_rub, CAST(oe.fwd_commission_pct AS FLOAT64) AS fwd_commission_pct,
    CAST(oe.fwd_acquiring_rub AS FLOAT64) AS fwd_acquiring_rub, CAST(oe.fwd_acquiring_pct AS FLOAT64) AS fwd_acquiring_pct,
    CAST(oe.fwd_logistics_rub AS FLOAT64) AS fwd_logistics_rub, CAST(oe.fwd_logistics_pct AS FLOAT64) AS fwd_logistics_pct,
    CAST(oe.fwd_product_cogs_rub AS FLOAT64) AS fwd_product_cogs_rub, CAST(oe.fwd_product_cogs_pct AS FLOAT64) AS fwd_product_cogs_pct,
    IFNULL(oe.agent_gate_open, FALSE) AS agent_gate_open, FALSE AS production_grade_data
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE` o
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD` oe ON oe.internal_sku = o.internal_sku
),
pairs AS (
  SELECT * FROM wb
  UNION ALL
  SELECT * FROM oz
),
base AS (
  SELECT
    c.as_of_date, c.knowledge_ts, c.run_mode,
    p.p1_reserve_share, p.p2_band_pp, p.p3_confidence, p.p5_low_cover_days, p.p5_overstock_cover_days,
    p.p7_price_change_pct, p.p13_cooldown_days, p.k_inactive_days,
    x.*,
    b.is_bundle,
    cv.cover_days, cv.cover_snapshot_date,
    fr.layers_not_ok,
    -- Текущий набор рекомендаций (решение владельца: активный статус и не больше K суток без расхода).
    -- Без даты последнего расхода пара в текущий набор не попадает (NULL ≠ «расход был недавно»).
    IFNULL(x.campaign_active_status IS TRUE
      AND (p.k_inactive_days IS NULL OR x.days_since_last_spend <= p.k_inactive_days), FALSE) AS in_current_universe,
    CASE p.p3_confidence
      WHEN 0.80 THEN 1.2815515655446004
      WHEN 0.90 THEN 1.6448536269514722
      WHEN 0.95 THEN 1.959963984540054
    END AS z,
    -- P6: DECREASE и HOLD — только против фактической границы (realized).
    x.limit_realized AS limit_drr
  FROM pairs x
  CROSS JOIN aie_clock c
  JOIN pol p ON p.policy_id = x.policy_id
  CROSS JOIN fresh fr
  LEFT JOIN bundles b ON b.internal_sku = x.internal_sku
  LEFT JOIN cover cv ON cv.internal_sku = x.internal_sku
),
ivl AS (
  SELECT
    b.*,
    -- Границы Пуассона для числа выкупов n (Вильсон–Хилферти); нижняя не меньше 0.
    IF(IFNULL(b.sku_buyouts_qty, 0) = 0 OR b.z IS NULL, 0,
       b.sku_buyouts_qty * POW(GREATEST(0, 1 - 1 / (9 * b.sku_buyouts_qty) - b.z / (3 * SQRT(b.sku_buyouts_qty))), 3)) AS n_low,
    IF(b.z IS NULL, NULL,
       (IFNULL(b.sku_buyouts_qty, 0) + 1)
       * POW(1 - 1 / (9 * (IFNULL(b.sku_buyouts_qty, 0) + 1)) + b.z / (3 * SQRT(IFNULL(b.sku_buyouts_qty, 0) + 1)), 3)) AS n_high,
    SAFE_DIVIDE(CAST(b.sku_buyouts_rub AS FLOAT64), b.sku_buyouts_qty) AS avg_buyout_price
  FROM base b
),
drr AS (
  SELECT
    i.*,
    SAFE_DIVIDE(CAST(i.sku_ad_spend_attributed_rub AS FLOAT64), i.avg_buyout_price * i.n_high) AS drr_low,
    IF(i.n_low > 0, SAFE_DIVIDE(CAST(i.sku_ad_spend_attributed_rub AS FLOAT64), i.avg_buyout_price * i.n_low), NULL) AS drr_high,
    i.limit_drr * (1 - i.p1_reserve_share) AS target_drr
  FROM ivl i
),
flags AS (
  SELECT
    d.*,
    (d.evidence_status = 'ACTIONABLE' AND d.effective_days > 0) AS reaches_l7,
    (d.marketplace = 'WB' AND d.evidence_status = 'ACTIONABLE' AND d.effective_days > 0
      AND NOT d.waste_zero_orders_ads4 AND d.z IS NOT NULL
      AND d.limit_drr IS NOT NULL AND IFNULL(d.sku_buyouts_qty, 0) > 0 AND IFNULL(d.sku_buyouts_rub, 0) > 0) AS l7_testable,
    -- P6: forward conservative — ограничитель против INCREASE (только текущий режим).
    CASE
      WHEN d.run_mode != 'CURRENT' THEN 'NOT_REPLAYABLE'
      WHEN d.limit_forward_conservative IS NULL THEN 'UNAVAILABLE'
      ELSE 'EVALUATED'
    END AS conservative_guard_mode,
    -- P5: состояние покрытия; наборы — всегда INV_COVER_UNAVAILABLE до покрытия с учётом компонентов.
    CASE
      WHEN d.is_bundle IS TRUE OR d.cover_days IS NULL THEN 'INV_COVER_UNAVAILABLE'
      WHEN d.p5_low_cover_days IS NULL AND d.p5_overstock_cover_days IS NULL THEN 'INV_COVER_POLICY_NOT_SET'
      WHEN d.p5_low_cover_days IS NOT NULL AND d.cover_days < d.p5_low_cover_days THEN 'INV_LOW_COVER'
      WHEN d.p5_overstock_cover_days IS NOT NULL AND d.cover_days > d.p5_overstock_cover_days THEN 'INV_OVERSTOCK_CONTEXT'
      WHEN d.p5_low_cover_days IS NOT NULL THEN 'INV_NORMAL_COVER'
      ELSE 'INV_COVER_POLICY_NOT_SET'
    END AS inventory_cover_state
  FROM drr d
),
outcome AS (
  SELECT
    f.*,
    (f.l7_testable AND f.drr_low > f.limit_drr) AS c_decrease,
    (f.l7_testable AND NOT (f.drr_low > f.limit_drr) AND (f.drr_high IS NULL OR f.drr_high > f.limit_drr)) AS c_straddle_breakeven,
    (f.l7_testable AND f.drr_high IS NOT NULL AND f.drr_high <= f.limit_drr) AS c_below_breakeven,
    (f.l7_testable AND f.drr_high IS NOT NULL AND f.drr_high <= f.limit_drr AND f.p1_reserve_share IS NOT NULL
      AND f.drr_high < f.target_drr) AS c_below_target_raw,
    CASE
      WHEN f.conservative_guard_mode != 'EVALUATED' THEN f.conservative_guard_mode
      WHEN f.drr_high IS NULL THEN 'NO_INTERVAL'
      WHEN f.drr_high < f.limit_forward_conservative THEN 'PASS'
      ELSE 'FAIL'
    END AS conservative_guard
  FROM flags f
),
alloc AS (
  -- При нескольких кампаниях SKU понижать предлагается кампанию с худшим CPO (без заказов — худшая).
  SELECT
    o.*,
    IF(o.c_decrease,
       ROW_NUMBER() OVER (PARTITION BY o.policy_id, o.marketplace, o.internal_sku, o.c_decrease, o.in_current_universe
                          ORDER BY IF(IFNULL(o.eff_orders, 0) = 0, 1, 0) DESC, o.eff_cpo_rub DESC, o.campaign_id),
       NULL) AS decrease_rank,
    -- Диагностика: статистически ниже фактической границы (P3) и, в текущем режиме, ниже осторожного прогноза.
    -- Без P1 это не рекомендация INCREASE.
    (o.c_below_breakeven AND o.conservative_guard = 'PASS') AS increase_candidate,
    o.c_below_breakeven AS increase_candidate_realized
  FROM outcome o
),
coded AS (
  SELECT
    a.*,
    ARRAY(
      SELECT AS STRUCT r.code, r.rank, r.state
      FROM UNNEST([
        STRUCT('ID_UNMAPPED_SKU' AS code, 100 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state, a.internal_sku IS NULL AS fired),
        STRUCT('ID_AMBIGUOUS_SKU' AS code, 101 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state, a.sku_mapping_count > 1 AS fired),
        STRUCT('SCOPE_CAMPAIGN_TYPE_OUT_OF_SCOPE' AS code, 102 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               (a.marketplace = 'WB' AND a.campaign_type IS NOT NULL AND a.campaign_type != '9')
               OR (a.marketplace = 'OZON' AND a.campaign_type IS NOT NULL
                   AND (a.campaign_type != 'SKU' OR IFNULL(a.payment_type, '') != 'CPC')) AS fired),
        STRUCT('SCOPE_CAMPAIGN_INACTIVE' AS code, 103 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               NOT a.in_current_universe AS fired),
        STRUCT('DQ_FRESHNESS_LAYER_NOT_OK' AS code, 200 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND a.run_mode = 'CURRENT' AND a.layers_not_ok IS NOT NULL AS fired),
        STRUCT('DQ_ADS_DATA_BEHIND' AS code, 201 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND (a.ads_data_through IS NULL OR a.ads_data_through < a.as_of_date) AS fired),
        STRUCT('DQ_STOCK_SNAPSHOT_MISSING' AS code, 202 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.stock_global_snapshot_date IS NULL OR a.stock_global_snapshot_date < a.as_of_date AS fired),
        -- Срок V_DATA_FRESHNESS.finance: data_age_ok_days = 3 от текущей даты (as_of + 1).
        STRUCT('DQ_FINANCE_BEHIND_SLA' AS code, 203 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND (a.finance_known_through IS NULL
                                         OR a.finance_known_through < DATE_SUB(a.as_of_date, INTERVAL 2 DAY)) AS fired),
        STRUCT('DQ_OZON_FRESHNESS_GATE_CLOSED' AS code, 204 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'OZON' AND a.run_mode = 'CURRENT' AND NOT a.agent_gate_open AS fired),
        STRUCT('DQ_OZON_ADS_DATA_BEHIND' AS code, 205 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'OZON' AND (a.ads_data_through IS NULL OR a.ads_data_through < a.as_of_date) AS fired),
        STRUCT('DQ_OZON_P26_SUSPECTED' AS code, 206 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'OZON' AND a.p26_suspect_days > 0 AS fired),
        STRUCT('CTRL_OZON_AUTOPILOT' AS code, 301 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'OZON' AND a.autopilot_controls_bids AS fired),
        STRUCT('INV_ZERO_STOCK' AS code, 400 AS rank, 'PAUSE_CANDIDATE' AS state,
               a.stock_status = 'OBSERVED' AND a.stock_units = 0 AS fired),
        STRUCT('ECON_MISSING_COGS' AS code, 500 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND a.economic_state = 'MISSING_COGS' AS fired),
        STRUCT('PRICE_BELOW_BREAKEVEN' AS code, 501 AS rank, 'PAUSE_CANDIDATE' AS state,
               IFNULL(a.negative_all_available_bases, FALSE)
               AND (a.marketplace = 'WB' OR a.agent_gate_open) AS fired),
        STRUCT('EVID_NO_EFFECTIVE_WINDOW' AS code, 700 AS rank, 'INSUFFICIENT_DATA' AS state,
               a.effective_days = 0 AS fired),
        STRUCT('EVID_INSUFFICIENT' AS code, 701 AS rank, 'INSUFFICIENT_DATA' AS state,
               a.effective_days > 0 AND a.evidence_status = 'INSUFFICIENT' AS fired),
        STRUCT('EVID_OBSERVATIONAL_ONLY' AS code, 702 AS rank, 'INSUFFICIENT_DATA' AS state,
               a.effective_days > 0 AND a.evidence_status = 'OBSERVATIONAL' AS fired),
        STRUCT('WASTE_ZERO_ORDERS_ADS4' AS code, 800 AS rank, 'PAUSE_CANDIDATE' AS state,
               a.marketplace = 'WB' AND a.reaches_l7 AND a.waste_zero_orders_ads4 AS fired),
        STRUCT('OZON_NOT_PRODUCTION_GRADE' AS code, 801 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'OZON' AND a.reaches_l7 AS fired),
        STRUCT('POLICY_P3_NOT_SET' AS code, 803 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND a.reaches_l7 AND NOT a.waste_zero_orders_ads4 AND a.p3_confidence IS NULL AS fired),
        STRUCT('POLICY_P3_UNSUPPORTED_VALUE' AS code, 804 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND a.reaches_l7 AND NOT a.waste_zero_orders_ads4
               AND a.p3_confidence IS NOT NULL AND a.z IS NULL AS fired),
        STRUCT('ECON_BASIS_UNAVAILABLE' AS code, 805 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.marketplace = 'WB' AND a.reaches_l7 AND NOT a.waste_zero_orders_ads4
               AND a.z IS NOT NULL AND a.limit_drr IS NULL AS fired),
        STRUCT('ECON_NO_BUYOUTS_IN_WINDOW' AS code, 806 AS rank, 'INSUFFICIENT_DATA' AS state,
               a.marketplace = 'WB' AND a.reaches_l7 AND NOT a.waste_zero_orders_ads4
               AND (IFNULL(a.sku_buyouts_qty, 0) = 0 OR IFNULL(a.sku_buyouts_rub, 0) <= 0) AS fired),
        STRUCT('EVID_INTERVAL_STRADDLES_BREAKEVEN' AS code, 807 AS rank, 'INSUFFICIENT_DATA' AS state,
               a.c_straddle_breakeven AS fired),
        STRUCT('ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT' AS code, 808 AS rank, 'DECREASE' AS state,
               a.c_decrease AND a.decrease_rank = 1 AS fired),
        STRUCT('ALLOCATION_NOT_WORST_PAIR' AS code, 809 AS rank, 'HOLD' AS state,
               a.c_decrease AND a.decrease_rank > 1 AS fired),
        STRUCT('POLICY_P1_NOT_SET' AS code, 810 AS rank, 'HOLD' AS state,
               a.c_below_breakeven AND a.p1_reserve_share IS NULL AS fired),
        STRUCT('EVID_INTERVAL_STRADDLES_TARGET' AS code, 811 AS rank, 'HOLD' AS state,
               a.c_below_breakeven AND a.p1_reserve_share IS NOT NULL
               AND ((NOT a.c_below_target_raw AND a.drr_low < a.target_drr)
                    OR (a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NOT NULL
                        AND a.drr_high >= a.target_drr - a.p2_band_pp)) AS fired),
        STRUCT('ECON_DRR_WITHIN_TARGET_BAND' AS code, 812 AS rank, 'HOLD' AS state,
               a.c_below_breakeven AND a.p1_reserve_share IS NOT NULL AND NOT a.c_below_target_raw
               AND a.drr_low >= a.target_drr AS fired),
        STRUCT('ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE' AS code, 899 AS rank, 'HOLD' AS state,
               a.c_below_target_raw AND a.conservative_guard != 'PASS' AS fired),
        STRUCT('POLICY_P2_NOT_SET' AS code, 900 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NULL AS fired),
        STRUCT('POLICY_P5_NOT_SET' AS code, 901 AS rank, 'BLOCKED_BY_GUARDRAIL' AS state,
               a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NOT NULL
               AND a.drr_high < a.target_drr - a.p2_band_pp
               AND a.inventory_cover_state = 'INV_COVER_POLICY_NOT_SET' AS fired),
        STRUCT('INV_COVER_UNAVAILABLE' AS code, 902 AS rank, 'HOLD' AS state,
               a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NOT NULL
               AND a.drr_high < a.target_drr - a.p2_band_pp
               AND a.inventory_cover_state = 'INV_COVER_UNAVAILABLE' AS fired),
        STRUCT('INV_LOW_COVER' AS code, 903 AS rank, 'HOLD' AS state,
               a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NOT NULL
               AND a.drr_high < a.target_drr - a.p2_band_pp
               AND a.inventory_cover_state = 'INV_LOW_COVER' AS fired),
        STRUCT('ECON_DRR_BELOW_TARGET_CONFIDENT' AS code, 904 AS rank, 'INCREASE' AS state,
               a.c_below_target_raw AND a.conservative_guard = 'PASS' AND a.p2_band_pp IS NOT NULL
               AND a.drr_high < a.target_drr - a.p2_band_pp
               AND a.inventory_cover_state IN ('INV_NORMAL_COVER', 'INV_OVERSTOCK_CONTEXT') AS fired),
        STRUCT('DQ_OZON_P25_WINDOW_BIAS' AS code, 1000 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'OZON' AS fired),
        STRUCT('DQ_OZON_NOT_POINT_IN_TIME' AS code, 1001 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'OZON' AND a.run_mode != 'CURRENT' AS fired),
        STRUCT('CTRL_OZON_NO_BID_OBSERVED' AS code, 1002 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'OZON' AS fired),
        STRUCT('CTRL_NO_CONFIG_OBSERVED' AS code, 1003 AS rank, CAST(NULL AS STRING) AS state,
               NOT a.config_observed AS fired),
        STRUCT('CTX_CONFIG_SNAPSHOT_BEHIND' AS code, 1004 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'WB' AND a.config_observed
               AND DATE(a.campaign_config_ts, 'Europe/Moscow') < a.as_of_date AS fired),
        STRUCT('CTX_WB_STATUS_PAUSED_WITH_SPEND' AS code, 1005 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'WB' AND a.campaign_status = '11' AND IFNULL(a.spend_28d_attributed_rub, 0) > 0 AS fired),
        STRUCT('CTX_INCLUDE_IN_ADS_ANALYSIS_FALSE' AS code, 1006 AS rank, CAST(NULL AS STRING) AS state,
               a.include_in_ads_analysis IS FALSE AS fired),
        STRUCT('ECON_FORWARD_SKIPPED_NOT_REPLAYABLE' AS code, 1007 AS rank, CAST(NULL AS STRING) AS state,
               a.forward_availability = 'SKIPPED_NOT_REPLAYABLE' AS fired),
        STRUCT('ECON_FORWARD_UNAVAILABLE' AS code, 1008 AS rank, CAST(NULL AS STRING) AS state,
               a.forward_availability IN ('MISSING', 'BLOCKED', 'NOT_READY') AS fired),
        STRUCT('ECON_REALIZED_NEGATIVE_BEFORE_ADS' AS code, 1009 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.realized_negative_before_ads, FALSE) AS fired),
        STRUCT('PRICING_REVIEW' AS code, 1010 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.forward_base_negative, FALSE) AS fired),
        STRUCT('INCREASE_CANDIDATE' AS code, 1011 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.increase_candidate, FALSE) AS fired),
        STRUCT('ECON_STRESS_RISK_FLAG' AS code, 1012 AS rank, CAST(NULL AS STRING) AS state,
               a.run_mode = 'CURRENT' AND a.limit_forward_stress IS NOT NULL
               AND (a.limit_forward_stress < 0 OR CAST(a.sku_drr_point AS FLOAT64) > a.limit_forward_stress) AS fired),
        STRUCT('INV_OVERSTOCK_CONTEXT' AS code, 1013 AS rank, CAST(NULL AS STRING) AS state,
               a.inventory_cover_state = 'INV_OVERSTOCK_CONTEXT' AS fired),
        STRUCT('FIN_WINDOW_ENDS_AT_FINANCE' AS code, 1014 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'WB' AND IFNULL(a.econ_window_end_lag_days, 0) > 0 AS fired),
        STRUCT('REGIME_BID_CHANGE' AS code, 1100 AS rank, CAST(NULL AS STRING) AS state,
               GREATEST(IFNULL(a.bid_regime_start, DATE '1970-01-01'), IFNULL(a.campaign_regime_start, DATE '1970-01-01'))
               > a.window_start_nominal AS fired),
        STRUCT('REGIME_PRICE_CHANGE' AS code, 1101 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.price_regime_start, DATE '1970-01-01') > a.window_start_nominal AS fired),
        STRUCT('REGIME_PRICE_UNOBSERVED_PART' AS code, 1102 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.price_unobserved_part, FALSE) AS fired),
        STRUCT('REGIME_PROMO_CHANGE' AS code, 1103 AS rank, CAST(NULL AS STRING) AS state,
               IFNULL(a.promo_regime_start, DATE '1970-01-01') > a.window_start_nominal AS fired),
        STRUCT('REGIME_PROMO_UNOBSERVABLE_WB' AS code, 1104 AS rank, CAST(NULL AS STRING) AS state,
               a.marketplace = 'WB' AS fired),
        STRUCT('INV_STOCKOUT_DAYS_EXCLUDED' AS code, 1105 AS rank, CAST(NULL AS STRING) AS state,
               a.stockout_days_excluded > 0 AS fired)
      ]) AS r
      WHERE r.fired
      ORDER BY r.rank
    ) AS reasons
  FROM alloc a
),
decided AS (
  SELECT
    k.*,
    (SELECT AS STRUCT r.code, r.rank, r.state FROM UNNEST(k.reasons) AS r
     WHERE r.state IS NOT NULL ORDER BY r.rank LIMIT 1) AS primary_reason
  FROM coded k
)
SELECT
  d.as_of_date,
  d.knowledge_ts,
  d.run_mode,
  d.policy_id,
  d.marketplace,
  d.internal_sku,
  d.marketplace_sku,
  d.campaign_id,
  IF(d.in_current_universe, 'CURRENT_ACTIONABLE', 'HISTORICAL_EVIDENCE_ONLY') AS universe,
  d.campaign_status,
  d.campaign_type,
  d.payment_type,
  d.bid_type,
  d.autopilot_strategy,
  d.last_spend_date,
  d.days_since_last_spend,
  d.current_bid_search_rub,
  d.current_bid_recommendations_rub,
  d.bid_observed_at,
  IFNULL(d.primary_reason.state, 'BLOCKED_BY_GUARDRAIL') AS recommendation,
  d.primary_reason.code AS primary_reason_code,
  d.primary_reason.rank AS primary_reason_rank,
  ARRAY(SELECT r.code FROM UNNEST(d.reasons) AS r ORDER BY r.rank) AS reason_codes,
  CASE IFNULL(d.primary_reason.state, 'BLOCKED_BY_GUARDRAIL')
    WHEN 'INCREASE' THEN 'UP'
    WHEN 'DECREASE' THEN 'DOWN'
    WHEN 'PAUSE_CANDIDATE' THEN 'PAUSE'
    ELSE 'NONE'
  END AS proposed_direction,
  CAST(NULL AS FLOAT64) AS proposed_delta,
  'DELTA_NOT_DERIVABLE' AS delta_basis,
  CONCAT(
    IFNULL(d.primary_reason.state, 'BLOCKED_BY_GUARDRAIL'), ' — ',
    CASE d.primary_reason.code
      WHEN 'ID_UNMAPPED_SKU' THEN 'SKU площадки не сопоставлен с internal_sku'
      WHEN 'ID_AMBIGUOUS_SKU' THEN 'SKU площадки сопоставлен с несколькими internal_sku'
      WHEN 'SCOPE_CAMPAIGN_TYPE_OUT_OF_SCOPE' THEN FORMAT('тип кампании %s / %s вне V1', IFNULL(d.campaign_type, '—'), IFNULL(d.payment_type, '—'))
      WHEN 'SCOPE_CAMPAIGN_INACTIVE' THEN FORMAT('кампания вне текущего набора: статус %s%s, последний расход %s (%s сут назад, допустимо %s) — остаётся в исторической базе доказательств', IFNULL(d.campaign_status, '—'), IF(d.marketplace = 'OZON' AND d.campaign_status = 'INACTIVE', ' (семантика INACTIVE не подтверждена — исключено, fail closed)', ''), IFNULL(CAST(d.last_spend_date AS STRING), '—'), IFNULL(CAST(d.days_since_last_spend AS STRING), '—'), IFNULL(CAST(d.k_inactive_days AS STRING), 'без ограничения'))
      WHEN 'DQ_FRESHNESS_LAYER_NOT_OK' THEN FORMAT('слои не в статусе OK: %s', d.layers_not_ok)
      WHEN 'DQ_ADS_DATA_BEHIND' THEN FORMAT('статистика рекламы есть только по %s, решение на %s', IFNULL(CAST(d.ads_data_through AS STRING), '—'), CAST(d.as_of_date AS STRING))
      WHEN 'DQ_STOCK_SNAPSHOT_MISSING' THEN FORMAT('последний снимок остатков %s старше даты решения %s', IFNULL(CAST(d.stock_global_snapshot_date AS STRING), '—'), CAST(d.as_of_date AS STRING))
      WHEN 'DQ_FINANCE_BEHIND_SLA' THEN FORMAT('финансы известны только по %s — позже срока 3 суток', IFNULL(CAST(d.finance_known_through AS STRING), '—'))
      WHEN 'DQ_OZON_FRESHNESS_GATE_CLOSED' THEN 'гейт свежести витрины Ozon закрыт'
      WHEN 'DQ_OZON_ADS_DATA_BEHIND' THEN FORMAT('статистика Ozon есть только по %s', IFNULL(CAST(d.ads_data_through AS STRING), '—'))
      WHEN 'DQ_OZON_P26_SUSPECTED' THEN FORMAT('сигнатура дефекта P2-6: %d дн. с расходом кампании без строк SKU', d.p26_suspect_days)
      WHEN 'CTRL_OZON_AUTOPILOT' THEN FORMAT('ставками управляет автопилот Ozon (%s), V1 не рекомендует CPC — P9', IFNULL(d.autopilot_strategy, '—'))
      WHEN 'INV_ZERO_STOCK' THEN FORMAT('остаток карточки на площадке 0 (снимок %s): продажа невозможна', IFNULL(CAST(d.stock_global_snapshot_date AS STRING), '—'))
      WHEN 'ECON_MISSING_COGS' THEN 'себестоимость в экономическом окне покрыта не полностью: предел не вычисляется'
      WHEN 'PRICE_BELOW_BREAKEVEN' THEN FORMAT('цена %s ₽ ниже безубыточной %s ₽: вклад до рекламы %s ₽ (комиссия %s %%, эквайринг %s %%, логистика %s %%, себестоимость %s %% цены) — вопрос цены, см. PRICING_REVIEW', IFNULL(FORMAT('%.2f', d.fwd_price_rub), '—'), IFNULL(FORMAT('%.2f', d.fwd_breakeven_price_rub), '—'), IFNULL(FORMAT('%.2f', d.fwd_contribution_before_ads_rub), '—'), IFNULL(FORMAT('%.1f', d.fwd_commission_pct), '—'), IFNULL(FORMAT('%.1f', d.fwd_acquiring_pct), '—'), IFNULL(FORMAT('%.1f', d.fwd_logistics_pct), '—'), IFNULL(FORMAT('%.1f', d.fwd_product_cogs_pct), '—'))
      WHEN 'EVID_NO_EFFECTIVE_WINDOW' THEN FORMAT('после смены режима с %s и без дней без остатка не осталось дней', CAST(d.effective_window_start AS STRING))
      WHEN 'EVID_INSUFFICIENT' THEN FORMAT('%d кликов и %d показов за %d сут. с %s — меньше порогов Ads-4 (10 кликов / 1000 показов)', IFNULL(d.eff_clicks, 0), IFNULL(d.eff_views, 0), d.effective_days, CAST(d.effective_window_start AS STRING))
      WHEN 'EVID_OBSERVATIONAL_ONLY' THEN FORMAT('%d кликов за %d сут. с %s — меньше 40: выводы о конверсии запрещены (Ads-4)', IFNULL(d.eff_clicks, 0), d.effective_days, CAST(d.effective_window_start AS STRING))
      WHEN 'WASTE_ZERO_ORDERS_ADS4' THEN FORMAT('%d кликов и %.2f ₽ атрибутированного расхода за %d сут. без единого заказа (Ads-4 POTENTIAL_WASTE)', IFNULL(d.eff_clicks, 0), CAST(IFNULL(d.eff_spend_attributed_rub, 0) AS FLOAT64), d.effective_days)
      WHEN 'OZON_NOT_PRODUCTION_GRADE' THEN 'доказательная строка Ozon: направления ставок не выдаются до PR-8 (P2-5, P2-6, нет снимка ставок)'
      WHEN 'POLICY_P3_NOT_SET' THEN FORMAT('ДРР SKU %.1f %% (выкупов %d); уровень доверия (P3) не задан', 100 * CAST(IFNULL(d.sku_drr_point, 0) AS FLOAT64), IFNULL(d.sku_buyouts_qty, 0))
      WHEN 'POLICY_P3_UNSUPPORTED_VALUE' THEN 'значение P3 вне набора {0.80, 0.90, 0.95}'
      WHEN 'ECON_BASIS_UNAVAILABLE' THEN 'фактическая граница ДРР (realized) не вычислена для строки'
      WHEN 'ECON_NO_BUYOUTS_IN_WINDOW' THEN FORMAT('в экономическом окне с %s нет выкупов: интервал ДРР не определён', IFNULL(CAST(d.econ_window_start AS STRING), '—'))
      WHEN 'EVID_INTERVAL_STRADDLES_BREAKEVEN' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %s] при фактическом лимите %.1f %% — интервал пересекает лимит (выкупов %d)', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, IFNULL(FORMAT('%.1f', 100 * d.drr_high), '∞'), 100 * d.limit_drr, d.sku_buyouts_qty)
      WHEN 'ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %.1f] выше фактического лимита %.1f %% с доверием %.0f %%', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, 100 * d.drr_high, 100 * d.limit_drr, 100 * d.p3_confidence)
      WHEN 'ALLOCATION_NOT_WORST_PAIR' THEN 'SKU выше лимита, но понижать предлагается другую кампанию этого SKU с худшим CPO'
      WHEN 'POLICY_P1_NOT_SET' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %.1f] ниже фактического лимита %.1f %%; целевой ДРР (P1) не задан — повышения нет%s', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, 100 * d.drr_high, 100 * d.limit_drr, IF(d.increase_candidate, '; диагностика INCREASE_CANDIDATE', ''))
      WHEN 'EVID_INTERVAL_STRADDLES_TARGET' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %.1f] при цели %.1f %%: повышение не доказано', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, 100 * d.drr_high, 100 * d.target_drr)
      WHEN 'ECON_DRR_WITHIN_TARGET_BAND' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %.1f] между целью %.1f %% и лимитом %.1f %%', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, 100 * d.drr_high, 100 * d.target_drr, 100 * d.limit_drr)
      WHEN 'ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE' THEN FORMAT('кандидат на повышение, но осторожный прогноз (conservative) не подтверждён: %s', d.conservative_guard)
      WHEN 'POLICY_P2_NOT_SET' THEN 'кандидат на повышение, но полоса гистерезиса (P2) не задана'
      WHEN 'POLICY_P5_NOT_SET' THEN 'кандидат на повышение, но границы покрытия запасом (P5) не заданы'
      WHEN 'INV_COVER_UNAVAILABLE' THEN 'кандидат на повышение, но покрытие запасом не наблюдается (для наборов — до покрытия с учётом компонентов)'
      WHEN 'INV_LOW_COVER' THEN FORMAT('кандидат на повышение, но покрытие %.0f сут. ниже нижней границы P5 %.0f', d.cover_days, d.p5_low_cover_days)
      WHEN 'ECON_DRR_BELOW_TARGET_CONFIDENT' THEN FORMAT('ДРР SKU %.1f %% [%.1f; %.1f] ниже цели %.1f %% минус полоса, осторожный прогноз подтверждён, покрытие %.0f сут.', 100 * CAST(d.sku_drr_point AS FLOAT64), 100 * d.drr_low, 100 * d.drr_high, 100 * d.target_drr, d.cover_days)
      ELSE 'внутренняя ошибка: нет основной причины'
    END
  ) AS explanation_ru,
  -- ── доказательность ──
  d.evidence_status,
  d.effective_window_start AS evidence_window_start,
  d.as_of_date AS evidence_window_end,
  d.effective_days,
  d.eff_views,
  d.eff_clicks,
  d.eff_orders,
  d.eff_spend_attributed_rub,
  d.eff_cpo_rub,
  d.spend_28d_attributed_rub,
  -- ── экономика SKU (атрибуция / выкупы; граница — realized) ──
  d.economic_state,
  d.econ_as_of,
  d.econ_window_start,
  d.financial_data_mature,
  d.econ_window_end_lag_days,
  d.sku_buyouts_qty,
  d.sku_buyouts_rub,
  d.sku_ad_spend_attributed_rub,
  d.sku_drr_point,
  d.drr_low,
  d.drr_high,
  d.limit_drr AS econ_limit_drr,
  d.target_drr,
  d.forward_availability,
  d.limit_forward_conservative,
  d.limit_forward_stress,
  d.conservative_guard,
  d.increase_candidate,
  d.increase_candidate_realized,
  -- ── ценовой ограничитель (forward base; диагностика PRICING_REVIEW, цена не меняется) ──
  d.fwd_price_rub AS current_effective_price_rub,
  d.fwd_breakeven_price_rub AS breakeven_price_rub,
  d.fwd_contribution_before_ads_rub AS contribution_before_ads_rub,
  d.fwd_commission_rub AS commission_rub,
  d.fwd_commission_pct AS commission_pct,
  d.fwd_acquiring_rub AS acquiring_rub,
  d.fwd_acquiring_pct AS acquiring_pct,
  d.fwd_logistics_rub AS logistics_rub,
  d.fwd_logistics_pct AS logistics_pct,
  d.fwd_product_cogs_rub AS product_cogs_rub,
  d.fwd_product_cogs_pct AS product_cogs_pct,
  IF(IFNULL(d.forward_base_negative, FALSE), 'PRICING_REVIEW', NULL) AS routing,
  -- ── запасы ──
  d.stock_status,
  d.stock_units,
  d.stock_global_snapshot_date,
  d.is_bundle,
  d.cover_days,
  d.cover_snapshot_date,
  d.inventory_cover_state,
  -- ── политика этой строки (evetis_ref.V_AIE_POLICY) ──
  d.p1_reserve_share,
  d.p2_band_pp,
  d.p3_confidence,
  d.p5_low_cover_days,
  d.p5_overstock_cover_days,
  d.p7_price_change_pct,
  d.p13_cooldown_days,
  d.k_inactive_days,
  (d.marketplace = 'WB' AND d.production_grade_data
    AND NOT EXISTS (SELECT 1 FROM UNNEST(d.reasons) AS r WHERE STARTS_WITH(r.code, 'DQ_'))) AS production_grade,
  IF(d.run_mode = 'CURRENT',
     'ads:FACT_CURRENT;config:SNAPSHOT_TS;prices:OBSERVED_AT;stocks:SNAPSHOT_DATE;finance:REPORT_LOADS+FACT;sales:MART_CURRENT;cogs:CURRENT_REFERENCE;forward:CURRENT;freshness:V_DATA_FRESHNESS;ozon_raw:LATEST;inventory_cover:SNAPSHOT_DATE',
     'ads:PIT_RAW_LOAD_TS;config:PIT_SNAPSHOT_TS;prices:PIT_OBSERVED_AT_FROM_2026-09-07;stocks:PIT_SNAPSHOT_DATE;finance:DATE_CUTOFF_REPORT_LOADS(DAILY=WEEKLY_VERIFIED);sales:DATE_CUTOFF+RESTATEMENT_RISK;cogs:CURRENT_REFERENCE_RETROACTIVE;forward:SKIPPED_NOT_REPLAYABLE;freshness:SKIPPED_NOT_REPLAYABLE;ozon_raw:NOT_POINT_IN_TIME;inventory_cover:PIT_SNAPSHOT_FROM_2026-09-10'
  ) AS input_manifest
FROM decided d;
