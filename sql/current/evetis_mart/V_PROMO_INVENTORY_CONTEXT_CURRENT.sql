-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Сценарии экономики акций PR-PROMO-3 LEFT JOIN контекст запаса. Экономика не меняется:
-- её колонки перенесены дословно из V_PROMO_ECONOMICS_SCENARIO_CURRENT (DQ сверяет по ключу),
-- грейн тот же — одна строка на marketplace × scenario_entity_key.
--
-- Оси не смешиваются и не взвешиваются:
--   required_sales_uplift_pct      — экономический break-even (PR-PROMO-3);
--   required_inventory_uplift_*    — ускорение распродажи к цели (V_SKU_INVENTORY_TARGET_CURRENT).
-- Нет сводного балла, ранжирования и решений ENTER/STAY/EXIT. Давление запаса не меняет
-- экономику и не ослабляет порог маржи: отрицательный вклад остаётся отрицательным рядом с
-- любым избытком запаса.
--
-- Согласование времени: promo_observed_at — время снимка акций (V_PROMO_OBSERVATION_HISTORY),
-- inventory_as_of — момент дневного среза Control Tower. Контекст ALIGNED, если
-- |promo_observed_at − inventory_as_of| ≤ CT_CONFIG.refresh_sla_hours (правило самого
-- Control Tower: без успешной пересборки дольше этого витрина STALE). Иначе значения запаса
-- NULL, статус INVENTORY_CONTEXT_STALE — сценарий остаётся.
--
-- Сущность запаса: физический SKU → его строка V_SKU_SELL_THROUGH_CURRENT и цели;
-- набор → мощность сборки V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT, карточки набора на площадке
-- и самый ранний срок годности среди компонентов. Нет internal_sku — NO_CANONICAL_SKU.
--
-- План запроса: каждое представление читается один раз (одна связка, без повторных ссылок).
--
-- Грейн: marketplace × scenario_entity_key.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT`
OPTIONS (description = "PR-PROMO-4. Текущие сценарии экономики акций PR-PROMO-3 с контекстом запаса: экономика дословно, запас, скорость, покрытие, срок годности, требуемая скорость к цели (для наборов — мощность сборки). Время акции, базиса экономики и запаса раздельно, согласование по CT_CONFIG.refresh_sla_hours. Без балла, ранжирования и решений.")
AS
WITH obs AS (
  SELECT marketplace, observation_id, observed_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
),
sla AS (
  SELECT SAFE_CAST(MAX(config_value) AS INT64) AS refresh_sla_hours
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
  WHERE config_key = 'refresh_sla_hours'
),
pm AS (
  SELECT internal_sku, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
),
t AS (
  SELECT
    internal_sku,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', target_status, NULL)) AS sell_by_target_status,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', required_units_per_day, NULL)) AS sell_by_required_units_per_day,
    MAX(IF(target_type = 'EXPIRY_SELL_BY', required_inventory_uplift_pct, NULL)) AS sell_by_required_inventory_uplift_pct,
    MAX(IF(target_type = 'EXPIRY_DATE', target_status, NULL)) AS expiry_target_status,
    MAX(IF(target_type = 'EXPIRY_DATE', required_units_per_day, NULL)) AS expiry_required_units_per_day,
    MAX(IF(target_type = 'EXPIRY_DATE', required_inventory_uplift_pct, NULL)) AS expiry_required_inventory_uplift_pct,
    COUNTIF(target_basis = 'OWNER_TARGET') AS owner_targets
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`
  GROUP BY internal_sku
),
bc AS (
  SELECT
    bundle_sku,
    ANY_VALUE(capacity_status) AS capacity_status,
    ANY_VALUE(technical_assembly_capacity_units) AS technical_assembly_capacity_units,
    ANY_VALUE(constraining_components) AS constraining_components,
    ANY_VALUE(shares_components_with) AS shares_components_with,
    ANY_VALUE(wb_bundle_cards_available) AS wb_bundle_cards_available,
    ANY_VALUE(ozon_bundle_cards_available) AS ozon_bundle_cards_available,
    ANY_VALUE(bundle_earliest_component_expiry_date) AS earliest_component_expiry_date,
    ANY_VALUE(bundle_earliest_expiry_component) AS earliest_expiry_component,
    ANY_VALUE(bundle_earliest_component_days_to_expiry) AS earliest_component_days_to_expiry,
    ANY_VALUE(bundle_components_without_expiry) AS components_without_expiry,
    ANY_VALUE(inventory_as_of) AS inventory_as_of,
    ANY_VALUE(inventory_freshness_status) AS inventory_freshness_status
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`
  GROUP BY bundle_sku
),
j AS (
  SELECT
    e.*,
    o.observed_at AS promo_observed_at,
    CASE
      WHEN e.internal_sku IS NULL THEN 'NO_CANONICAL_SKU'
      WHEN pm.internal_sku IS NULL THEN 'NOT_IN_PRODUCT_MASTER'
      WHEN pm.is_bundle THEN 'BUNDLE'
      ELSE 'PHYSICAL_SKU'
    END AS inventory_entity_type,
    sla.refresh_sla_hours,
    s AS sk,
    t AS tk,
    bc AS bk
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT` e
  CROSS JOIN sla
  LEFT JOIN obs o ON o.marketplace = e.marketplace AND o.observation_id = e.last_observation_id
  LEFT JOIN pm ON pm.internal_sku = e.internal_sku
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT` s
    ON s.internal_sku = e.internal_sku AND NOT pm.is_bundle
  LEFT JOIN t ON t.internal_sku = e.internal_sku AND NOT pm.is_bundle
  LEFT JOIN bc ON bc.bundle_sku = e.internal_sku AND pm.is_bundle
),
k AS (
  SELECT
    j.*,
    COALESCE(sk.inventory_as_of, bk.inventory_as_of) AS inventory_as_of,
    TIMESTAMP_DIFF(promo_observed_at, COALESCE(sk.inventory_as_of, bk.inventory_as_of), MINUTE) AS inventory_temporal_gap_minutes,
    CASE
      WHEN inventory_entity_type IN ('NO_CANONICAL_SKU', 'NOT_IN_PRODUCT_MASTER') THEN inventory_entity_type
      WHEN COALESCE(sk.inventory_as_of, bk.inventory_as_of) IS NULL THEN 'NO_INVENTORY_CONTEXT'
      WHEN promo_observed_at IS NULL OR refresh_sla_hours IS NULL THEN 'TEMPORAL_ALIGNMENT_UNKNOWN'
      WHEN ABS(TIMESTAMP_DIFF(promo_observed_at, COALESCE(sk.inventory_as_of, bk.inventory_as_of), MINUTE))
           > refresh_sla_hours * 60 THEN 'INVENTORY_CONTEXT_STALE'
      ELSE 'ALIGNED'
    END AS inventory_context_status
  FROM j
),
x AS (
  -- Контекст запаса виден только при согласованном времени; иначе NULL, сценарий остаётся.
  SELECT
    k.* EXCEPT (sk, tk, bk),
    IF(inventory_context_status = 'ALIGNED', sk, NULL) AS sk,
    IF(inventory_context_status = 'ALIGNED', tk, NULL) AS tk,
    IF(inventory_context_status = 'ALIGNED', bk, NULL) AS bk
  FROM k
)
SELECT
  -- Экономика PR-PROMO-3 — дословно
  x.marketplace,
  x.scenario_entity_key,
  x.source_promotion_id,
  x.promotion_type,
  x.promotion_lifecycle_status,
  x.promotion_starts_at,
  x.marketplace_product_id,
  x.offer_id,
  x.internal_sku,
  x.mapping_status,
  x.scenario_type,
  x.scenario_key,
  x.price_interpretation,
  x.price_binding_evidence,
  x.economics_status,
  x.baseline_price_rub,
  x.scenario_price_rub,
  x.baseline_contribution_expected_rub,
  x.promo_contribution_expected_rub,
  x.delta_contribution_expected_rub,
  x.baseline_margin_expected_pct,
  x.promo_margin_expected_pct,
  x.break_even_price_expected_rub,
  x.downside_case,
  x.promo_contribution_downside_rub,
  x.promo_downside_negative,
  x.uplift_status,
  x.required_sales_uplift_pct,
  x.present_in_latest_slot,
  -- Время
  x.last_economics_slot,
  x.promo_observed_at,
  x.last_basis_captured_at AS economics_basis_as_of,
  x.inventory_as_of,
  x.inventory_temporal_gap_minutes,
  x.refresh_sla_hours AS alignment_limit_hours,
  'ABS(promo_observed_at − inventory_as_of) ≤ evetis_ref.CT_CONFIG.refresh_sla_hours' AS alignment_rule,
  x.inventory_context_status,
  x.inventory_entity_type,
  -- Запас физического SKU (NULL, если контекст не ALIGNED или это набор)
  x.sk.inventory_freshness_status,
  x.sk.inventory_freshness_reason,
  x.sk.inventory_position_units,
  x.sk.warehouse_ff_units,
  x.sk.marketplace_available_units,
  CASE x.marketplace WHEN 'WB' THEN x.sk.wb_available_units WHEN 'OZON' THEN x.sk.ozon_available_units END
    AS this_channel_available_units,
  x.sk.in_transit_units,
  IF(x.sk.internal_sku IS NULL, NULL, 'POSITION=GLOBAL_PHYSICAL; FF=WAREHOUSE_SHARED_POOL; CHANNEL=CHANNEL_SPECIFIC')
    AS inventory_scope,
  x.sk.units_per_day_7d,
  x.sk.units_per_day_30d,
  x.sk.units_per_day_90d,
  x.sk.velocity_quality_30d,
  x.sk.cover_days_30d,
  x.sk.cover_status_30d,
  x.sk.expiry_evidence_status,
  x.sk.earliest_expiry_date,
  x.sk.days_to_expiry,
  x.sk.sell_by_date,
  x.sk.ct_policy_status_code,
  -- Требуемая скорость распродажи к цели (ось запаса)
  x.tk.sell_by_target_status,
  x.tk.sell_by_required_units_per_day,
  x.tk.sell_by_required_inventory_uplift_pct,
  x.tk.expiry_target_status,
  x.tk.expiry_required_units_per_day,
  x.tk.expiry_required_inventory_uplift_pct,
  x.tk.owner_targets AS owner_targets_count,
  -- Набор
  x.bk.capacity_status AS bundle_capacity_status,
  x.bk.inventory_freshness_status AS bundle_inventory_freshness_status,
  x.bk.technical_assembly_capacity_units AS bundle_technical_assembly_capacity_units,
  x.bk.constraining_components AS bundle_constraining_components,
  x.bk.shares_components_with AS bundle_shares_components_with,
  CASE x.marketplace WHEN 'WB' THEN x.bk.wb_bundle_cards_available WHEN 'OZON' THEN x.bk.ozon_bundle_cards_available END
    AS this_channel_bundle_cards_available,
  x.bk.earliest_component_expiry_date AS bundle_earliest_component_expiry_date,
  x.bk.earliest_expiry_component AS bundle_earliest_expiry_component,
  x.bk.earliest_component_days_to_expiry AS bundle_earliest_component_days_to_expiry,
  x.bk.components_without_expiry AS bundle_components_without_expiry
FROM x
