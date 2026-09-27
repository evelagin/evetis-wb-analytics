-- ============================================================================
-- AIE V1 (PR-2) · wb_mart.V_AIE_WB_QUERY_CLASS (VIEW)
-- Грейн: policy_id × nm_id × norm_query (окно Ads-4 28 суток на его собственную as_of; policy_id — от экономики
-- SKU, окно которой зависит от порога P7).
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §6.
--
-- ДИАГНОСТИКА, А НЕ ИСТОЧНИК НАПРАВЛЕНИЯ СТАВКИ. Колонок рекомендации здесь нет и не будет:
-- автоматическое изменение ставок кластеров в V1 не проектируется; Q_POSSIBLY_UNDEREXPOSED — гипотеза
-- (доля показов WB не отдаёт), а не повод повышать ставку.
--
-- Переиспользуется wb_mart.V_ADS_FUNNEL_QUERY_28D (Ads-4): метрики окна, гейты доказательности,
-- базовые уровни SKU_EX_SELF → STORE_EX_SELF, ставки последнего снимка. Экономика SKU — из
-- wb_mart.V_AIE_WB_ECON_GUARD (фактическая, окно AIE).
--
-- Экономические потолки (диагностика, PROVISIONAL — калибровка C6):
--   econ_cpo_ceiling_rub = вклад до рекламы / заказанные единицы SKU в окне AIE
--                          (выкупы по дате продажи, заказы по дате заказа — смещение к концу окна);
--   econ_cpc_ceiling_rub = order_cr_clicks × econ_cpo_ceiling_rub  (только при can_judge_order_cr);
--   econ_cpm_ceiling_rub = ctr × econ_cpc_ceiling_rub × 1000        (только при can_compare_ctr).
-- NULL ≠ 0: потолок не вычислен — NULL.
--
-- Множители 0,7 / 1,3 — проектное решение Ads-4 без вывода (P15 не решено, UBR-021): CTE aie_diag_params,
-- replay подставляет туда сетку чувствительности.
-- В запросе нет выручки, поэтому ДРР и ROAS по запросу не считаются (ADS5 §6).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`
OPTIONS (description = "AIE V1. Диагностические классы поисковых запросов WB (nm × norm_query) поверх Ads-4 V_ADS_FUNNEL_QUERY_28D и экономики SKU V_AIE_WB_ECON_GUARD. Не источник направления ставки: колонок рекомендации нет, автоматическое изменение ставок кластеров не проектируется; Q_POSSIBLY_UNDEREXPOSED — гипотеза. Множители 0,7/1,3 — проектное решение Ads-4 (P15 не решено). NULL = не вычислено, не ноль.")
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
-- @aie:diag:begin
aie_diag_params AS (
  SELECT 0.7 AS below_mult, 1.3 AS above_mult, 'ADS4_DESIGN_CHOICE_UNRESOLVED_P15' AS mult_source
),
-- @aie:diag:end
q AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
),
econ AS (
  SELECT policy_id, nm_id, internal_sku, econ_as_of, econ_window_days, economic_state,
         contribution_before_ads_rub, orders_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`
),
ceil AS (
  SELECT
    e.*,
    IF(e.economic_state IN ('BELOW_BREAKEVEN', 'ABOVE_BREAKEVEN', 'NO_AD_SPEND', 'NEGATIVE_BEFORE_ADS')
       AND e.orders_qty > 0,
       SAFE_DIVIDE(e.contribution_before_ads_rub, e.orders_qty), NULL) AS econ_cpo_ceiling_rub
  FROM econ e
),
j AS (
  SELECT
    q.*,
    c.policy_id,
    c.internal_sku,
    c.econ_as_of,
    c.econ_window_days,
    c.economic_state AS sku_economic_state,
    c.econ_cpo_ceiling_rub,
    IF(q.can_judge_order_cr, q.order_cr_clicks * c.econ_cpo_ceiling_rub, NULL) AS econ_cpc_ceiling_rub,
    IF(q.can_judge_order_cr AND q.can_compare_ctr,
       q.ctr * q.order_cr_clicks * c.econ_cpo_ceiling_rub * 1000, NULL) AS econ_cpm_ceiling_rub
  FROM q
  LEFT JOIN ceil c ON c.nm_id = q.nm_id
),
cls AS (
  SELECT
    j.*,
    p.below_mult,
    p.above_mult,
    p.mult_source,
    (j.evidence_status != 'ACTIONABLE') AS q_insufficient,
    (j.evidence_status = 'ACTIONABLE' AND j.funnel_monotonic
      AND (IFNULL(j.orders_sum, 0) = 0
           OR (j.can_judge_cart_cr AND j.cart_cr_vs_baseline < p.below_mult))) AS q_traffic_no_conversion,
    (j.can_compare_cpc
      AND (j.cpc_vs_baseline > p.above_mult
           OR (j.econ_cpc_ceiling_rub IS NOT NULL AND j.cpc_calc > j.econ_cpc_ceiling_rub))) AS q_high_cpc,
    (j.sku_economic_state IN ('ABOVE_BREAKEVEN', 'NEGATIVE_BEFORE_ADS')
      OR (j.evidence_status = 'ACTIONABLE' AND IFNULL(j.orders_sum, 0) > 0
          AND j.econ_cpo_ceiling_rub IS NOT NULL AND j.cpo_ads > j.econ_cpo_ceiling_rub)) AS q_econ_cannot_raise,
    (j.evidence_status = 'ACTIONABLE' AND j.funnel_monotonic AND IFNULL(j.orders_sum, 0) > 0
      AND j.econ_cpo_ceiling_rub IS NOT NULL AND j.cpo_ads <= j.econ_cpo_ceiling_rub) AS q_converting
  FROM j
  CROSS JOIN aie_diag_params p
)
SELECT
  c.as_of_date AS aie_as_of_date,
  c.run_mode,
  x.policy_id,
  x.as_of_date AS query_as_of_date,
  (x.as_of_date = c.as_of_date) AS query_as_of_aligned,
  x.window_days,
  x.nm_id,
  x.internal_sku,
  x.norm_query,
  x.evidence_status,
  x.evidence_reason,
  x.funnel_monotonic,
  x.views_sum,
  x.clicks_sum,
  x.atbs_sum,
  x.orders_sum,
  x.spend_sum AS spend_attributed_rub,
  x.spend_share_window,
  x.ctr,
  x.cpc_calc AS cpc_rub,
  x.cpo_ads AS cpo_rub,
  x.cart_cr_clicks,
  x.order_cr_clicks,
  x.avg_pos_w_views,
  x.baseline_level,
  x.ctr_vs_baseline,
  x.cart_cr_vs_baseline,
  x.cpc_vs_baseline,
  x.cpo_vs_baseline,
  x.bid_min AS bid_min_rub,
  x.bid_max AS bid_max_rub,
  x.bid_snapshot_date,
  x.bid_is_after_stats_as_of,
  x.payment_type,
  x.bid_type,
  x.econ_as_of,
  x.econ_window_days,
  x.sku_economic_state,
  x.econ_cpo_ceiling_rub,
  x.econ_cpc_ceiling_rub,
  x.econ_cpm_ceiling_rub,
  'PROVISIONAL_C6' AS econ_ceiling_status,
  x.q_insufficient,
  x.q_traffic_no_conversion,
  x.q_high_cpc,
  x.q_econ_cannot_raise,
  x.q_converting,
  -- Гипотеза: запрос конвертирует в пределах экономики, ставка ниже экономического потолка CPM.
  -- Долю показов WB не отдаёт, поэтому «недополучает показы» не доказано; только manual CPM имеет ставку.
  (x.q_converting AND x.payment_type = 'cpm' AND x.bid_max IS NOT NULL
    AND x.econ_cpm_ceiling_rub IS NOT NULL AND x.bid_max < x.econ_cpm_ceiling_rub) AS q_possibly_underexposed,
  CASE
    WHEN x.q_insufficient THEN 'Q_INSUFFICIENT'
    WHEN x.q_econ_cannot_raise THEN 'Q_ECON_CANNOT_RAISE'
    WHEN x.q_traffic_no_conversion THEN 'Q_TRAFFIC_NO_CONVERSION'
    WHEN x.q_high_cpc THEN 'Q_HIGH_CPC'
    WHEN x.q_converting AND x.payment_type = 'cpm' AND x.bid_max IS NOT NULL
         AND x.econ_cpm_ceiling_rub IS NOT NULL AND x.bid_max < x.econ_cpm_ceiling_rub THEN 'Q_POSSIBLY_UNDEREXPOSED'
    WHEN x.q_converting THEN 'Q_CONVERTING'
    ELSE 'Q_ACTIONABLE_NO_CLASS'
  END AS primary_query_class,
  x.below_mult AS diag_below_mult,
  x.above_mult AS diag_above_mult,
  x.mult_source AS diag_mult_source,
  FALSE AS is_bid_direction_source
FROM cls x
CROSS JOIN aie_clock c;
