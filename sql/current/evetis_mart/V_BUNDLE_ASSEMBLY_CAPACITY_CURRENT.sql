-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Техническая мощность сборки наборов из единиц ФФ по авторитетному BOM.
-- BOM — evetis_ref.REF_BUNDLE_COMPONENTS через wb_mart.V_CT_BOM_CURRENT (тот же BOM, что у
-- Control Tower и evetis_ops; по названиям карточек состав не выводится). Набор без строк
-- BOM — BUNDLE_BOM_UNAVAILABLE, мощность не считается.
--
--   component_capacity_units = FLOOR(ФФ компонента / qty в наборе)
--   technical_assembly_capacity_units = MIN по компонентам
-- Число совпадает с wb_mart.V_CT_BUNDLE_STATUS.assemblable_now_ff (DQ сверяет); здесь добавлено
-- то, чего там нет: детерминированный ограничивающий компонент (все компоненты с минимумом,
-- по алфавиту), мощность каждого компонента, общие компоненты и одиночные продажи.
--
-- 🔴 Мощность НЕ распределена: компоненты общие для нескольких наборов и продаются
-- поодиночке. Мощности разных наборов не складываются; резерв под наборы не вычитается —
-- системы распределения нет. Числитель — ФФ (сборка идёт на ФФ); единицы на полках площадок
-- уже в карточках и в набор не собираются. evetis_ops.V_CT_BUNDLE_CAPACITY считает то же из
-- журнала ФФ, но журнал заморожен на посеве 09.09 (запись владельца выключена) — поэтому
-- здесь числитель Control Tower.
--
-- Срок годности набора — самый ранний среди компонентов (факт, не цель).
-- План запроса: V_SKU_SELL_THROUGH_CURRENT читается один раз; набор собирается в массив
-- компонентов и раскрывается обратно (без повторных ссылок на CTE).
--
-- Грейн: bundle_sku × component_sku (у набора без BOM — одна строка с component_sku NULL).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`
OPTIONS (description = "PR-PROMO-4. Техническая мощность сборки наборов из единиц ФФ по авторитетному BOM: мощность каждого компонента, мощность набора (минимум), детерминированный ограничивающий компонент, общие компоненты и одиночные продажи. Мощность не распределена между наборами и не складывается. Карточки наборов на площадках — справочно из V_CT_BUNDLE_STATUS.")
AS
WITH bom AS (
  SELECT card_sku AS bundle_sku, component_sku, component_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`
  WHERE is_bundle
),
bundle_links AS (
  -- Общие компоненты и число наборов на компонент — из BOM (без запаса).
  SELECT
    a.bundle_sku,
    a.component_sku,
    ARRAY_AGG(DISTINCT o.bundle_sku IGNORE NULLS ORDER BY o.bundle_sku) AS other_bundles,
    COUNT(DISTINCT o.bundle_sku) + 1 AS bundles_using_component
  FROM bom a
  LEFT JOIN bom o ON o.component_sku = a.component_sku AND o.bundle_sku != a.bundle_sku
  GROUP BY a.bundle_sku, a.component_sku
),
standalone AS (
  SELECT DISTINCT internal_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE is_current
),
bom_agg AS (
  SELECT
    b.bundle_sku,
    ARRAY_AGG(STRUCT(b.component_sku, b.component_qty, sa.internal_sku IS NOT NULL AS component_also_sold_standalone,
                     l.other_bundles, l.bundles_using_component) ORDER BY b.component_sku) AS comps
  FROM bom b
  LEFT JOIN standalone sa ON sa.internal_sku = b.component_sku
  LEFT JOIN bundle_links l ON l.bundle_sku = b.bundle_sku AND l.component_sku = b.component_sku
  GROUP BY b.bundle_sku
),
inv AS (
  -- Единственное чтение запаса: строки компонентов + общая свежесть (одинакова для всех SKU).
  SELECT
    ARRAY_AGG(STRUCT(internal_sku, product_name, warehouse_ff_units, inventory_position_units, earliest_expiry_date,
                     days_to_expiry)) AS skus,
    ANY_VALUE(inventory_as_of) AS inventory_as_of,
    ANY_VALUE(inventory_as_of_date) AS inventory_as_of_date,
    ANY_VALUE(inventory_freshness_status) AS inventory_freshness_status,
    ANY_VALUE(inventory_freshness_reason) AS inventory_freshness_reason,
    ANY_VALUE(ff_anchor_date) AS ff_anchor_date,
    ANY_VALUE(ff_anchor_age_days) AS ff_anchor_age_days
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
per_bundle AS (
  SELECT
    p.internal_sku AS bundle_sku,
    COALESCE(p.product_name_short, p.canonical_product_name) AS bundle_name,
    ARRAY(
      SELECT AS STRUCT
        b.component_sku,
        b.component_qty,
        i.product_name AS component_name,
        i.warehouse_ff_units AS component_ff_units,
        i.inventory_position_units AS component_position_units,
        i.earliest_expiry_date AS component_earliest_expiry_date,
        i.days_to_expiry AS component_days_to_expiry,
        IF(b.component_qty > 0 AND i.warehouse_ff_units IS NOT NULL, DIV(i.warehouse_ff_units, b.component_qty), NULL)
          AS component_capacity_units,
        b.component_qty <= 0 AS qty_invalid,
        i.internal_sku IS NULL AS component_missing,
        b.component_also_sold_standalone,
        b.other_bundles,
        b.bundles_using_component
      FROM UNNEST(ba.comps) b
      LEFT JOIN UNNEST(v.skus) i ON i.internal_sku = b.component_sku
      ORDER BY b.component_sku
    ) AS comps,
    v.* EXCEPT (skus)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p
  CROSS JOIN inv v
  LEFT JOIN bom_agg ba ON ba.bundle_sku = p.internal_sku
  WHERE p.is_bundle
),
agg AS (
  SELECT
    pb.*,
    ARRAY_LENGTH(pb.comps) AS components,
    (SELECT SUM(c.component_qty) FROM UNNEST(pb.comps) c) AS units_per_bundle,
    (SELECT MIN(c.component_capacity_units) FROM UNNEST(pb.comps) c) AS capacity_units,
    (SELECT LOGICAL_OR(c.qty_invalid) FROM UNNEST(pb.comps) c) AS any_qty_invalid,
    (SELECT LOGICAL_OR(c.component_missing) FROM UNNEST(pb.comps) c) AS any_component_missing,
    (SELECT ARRAY_AGG(DISTINCT x ORDER BY x) FROM UNNEST(pb.comps) c, UNNEST(c.other_bundles) x) AS shares,
    (SELECT AS STRUCT c.component_earliest_expiry_date AS d, c.component_sku AS sku, c.component_days_to_expiry AS days
       FROM UNNEST(pb.comps) c WHERE c.component_earliest_expiry_date IS NOT NULL
       ORDER BY c.component_earliest_expiry_date, c.component_sku LIMIT 1) AS earliest_expiry,
    (SELECT COUNTIF(c.component_earliest_expiry_date IS NULL) FROM UNNEST(pb.comps) c) AS components_without_expiry
  FROM per_bundle pb
),
st AS (
  SELECT
    agg.*,
    CASE
      WHEN components = 0 THEN 'BUNDLE_BOM_UNAVAILABLE'
      WHEN any_qty_invalid THEN 'BOM_QTY_INVALID'
      WHEN any_component_missing THEN 'COMPONENT_NOT_IN_INVENTORY'
      ELSE 'COMPUTED'
    END AS capacity_status
  FROM agg
),
ct AS (
  SELECT bundle_sku, wb_cards_live, ozon_cards_live, ozon_cards_transit_api, assemblable_now_ff
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS`
)
SELECT
  st.bundle_sku,
  st.bundle_name,
  st.capacity_status,
  st.components,
  st.units_per_bundle,
  c.component_sku,
  c.component_name,
  c.component_qty,
  c.component_ff_units,
  c.component_position_units,
  c.component_capacity_units,
  c.component_earliest_expiry_date,
  c.component_days_to_expiry,
  IF(st.capacity_status = 'COMPUTED', c.component_capacity_units = st.capacity_units, NULL) AS is_constraining_component,
  IF(st.capacity_status = 'COMPUTED', st.capacity_units, NULL) AS technical_assembly_capacity_units,
  IF(st.capacity_status = 'COMPUTED',
     (SELECT STRING_AGG(x.component_sku, ', ' ORDER BY x.component_sku) FROM UNNEST(st.comps) x
       WHERE x.component_capacity_units = st.capacity_units), NULL) AS constraining_components,
  IF(st.capacity_status = 'COMPUTED',
     (SELECT MIN(x.component_sku) FROM UNNEST(st.comps) x WHERE x.component_capacity_units = st.capacity_units), NULL)
    AS first_constraining_component,
  c.bundles_using_component,
  st.earliest_expiry.d AS bundle_earliest_component_expiry_date,
  st.earliest_expiry.sku AS bundle_earliest_expiry_component,
  st.earliest_expiry.days AS bundle_earliest_component_days_to_expiry,
  st.components_without_expiry AS bundle_components_without_expiry,
  c.component_also_sold_standalone,
  NULLIF(ARRAY_TO_STRING(st.shares, ', '), '') AS shares_components_with,
  IFNULL(ARRAY_LENGTH(st.shares), 0) = 0 AS capacity_is_independent,
  'TECHNICAL_ASSEMBLY_CAPACITY_FROM_FF_UNITS_NOT_ALLOCATED' AS capacity_semantics,
  'Компоненты общие для наборов и продаются поодиночке: мощности наборов не складываются, резерв под наборы не вычитается.'
    AS capacity_note,
  ct.wb_cards_live AS wb_bundle_cards_available,
  ct.ozon_cards_live AS ozon_bundle_cards_available,
  ct.ozon_cards_transit_api AS ozon_bundle_cards_in_transit_api,
  ct.assemblable_now_ff AS ct_assemblable_now_ff,
  st.inventory_as_of,
  st.inventory_as_of_date,
  st.inventory_freshness_status,
  st.inventory_freshness_reason,
  st.ff_anchor_date,
  st.ff_anchor_age_days
FROM st
LEFT JOIN UNNEST(st.comps) c
LEFT JOIN ct USING (bundle_sku)
