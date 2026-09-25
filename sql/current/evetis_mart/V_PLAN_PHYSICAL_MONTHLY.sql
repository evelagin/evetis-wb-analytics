-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLAN_PHYSICAL_MONTHLY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Физическая потребность каждой версии плана по компонентам:
--   physical(c, m) = Σ_s planned_cards(s, m) × qty(s, c | снимок BOM версии, PLAN_BOM_BASIS)
-- Набор — продаваемая карточка, а не запас: его продажа расходует компоненты. Одиночный SKU
-- расходует сам себя (qty 1). Набор без строк в базисе BOM версии не раскладывается выдуманным
-- составом — строка компонента пустая (component_sku NULL, BOM_BASIS_MISSING), это исключение.
-- Базис BOM — снимок версии, поэтому правка REF_BUNDLE_COMPONENTS не меняет смысл версии.
--
-- Текущий месяц (месяц даты запаса — последней партиции среза Control Tower): остаток плана по
-- карточке × площадке = MAX(план месяца − заказано MTD, 0), MTD — V_CT_ACTUAL_DAILY с 1-го числа
-- по sales_as_of. Прошлые месяцы — 0 к списанию, будущие — план целиком. Заказы дня среза
-- (после sales_as_of) могут быть и в позиции запаса, и в остатке плана: это не более суток
-- заказов, консервативно (траектория чуть ниже), отмечено в контракте.
--
-- Грейн: plan_version × month × component_sku.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY`
OPTIONS (description = "PR-PLAN-1. Физическая потребность версий плана по компонентам: карточки × состав по снимку BOM версии; одиночные и наборы раздельно, разбивка по наборам. Для текущего месяца — остаток плана после заказов MTD. Набор без базиса BOM не раскладывается (BOM_BASIS_MISSING).")
AS
WITH cal AS (
  -- Дата запаса — последняя партиция среза Control Tower (как V_INVENTORY_POSITION_HISTORY);
  -- sales_as_of — формула V_SKU_SELL_THROUGH_CURRENT (паритет — DQ P09). Тяжёлые представления
  -- запаса здесь не читаются.
  SELECT
    (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`) AS inv_date,
    (SELECT LEAST(MAX(IF(marketplace = 'WB', d, NULL)), MAX(IF(marketplace = 'OZON', d, NULL)),
                  DATE_SUB(DATE(MAX(refreshed_at)), INTERVAL 1 DAY))
     FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`) AS sales_as_of
),
mtd AS (
  SELECT UPPER(a.marketplace) AS marketplace, a.internal_sku, SUM(a.cards_ordered) AS cards_mtd
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a
  CROSS JOIN cal
  WHERE a.d BETWEEN DATE_TRUNC(cal.inv_date, MONTH) AND cal.sales_as_of
  GROUP BY 1, 2
),
pm AS (
  SELECT internal_sku, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
),
ln AS (
  SELECT
    l.plan_version, l.plan_kind, l.month, l.marketplace, l.internal_sku AS card_sku, l.planned_cards,
    IFNULL(pm.is_bundle, l.sales_mode = 'BUNDLE') AS is_bundle,
    CASE
      WHEN l.month < DATE_TRUNC(cal.inv_date, MONTH) THEN 'PAST'
      WHEN l.month = DATE_TRUNC(cal.inv_date, MONTH) THEN 'CURRENT'
      ELSE 'FUTURE'
    END AS month_position,
    IFNULL(m.cards_mtd, 0) AS cards_mtd
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL` l
  CROSS JOIN cal
  LEFT JOIN pm ON pm.internal_sku = l.internal_sku
  LEFT JOIN mtd m ON m.marketplace = l.marketplace AND m.internal_sku = l.internal_sku
),
ln2 AS (
  SELECT ln.*,
    CASE month_position
      WHEN 'PAST' THEN 0
      WHEN 'CURRENT' THEN GREATEST(planned_cards - cards_mtd, 0)
      ELSE planned_cards
    END AS remaining_cards
  FROM ln
),
ex AS (
  SELECT
    ln2.plan_version, ln2.plan_kind, ln2.month, ln2.month_position, ln2.marketplace, ln2.card_sku, ln2.is_bundle,
    ln2.planned_cards, ln2.remaining_cards,
    IF(ln2.is_bundle, b.component_sku, ln2.card_sku) AS component_sku,
    IF(ln2.is_bundle, b.component_qty, 1) AS component_qty
  FROM ln2
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS` b
    ON ln2.is_bundle AND b.plan_version = ln2.plan_version AND b.bundle_sku = ln2.card_sku
),
bundle_parts AS (
  SELECT plan_version, month, component_sku, card_sku,
    SUM(planned_cards * component_qty) AS units_full, SUM(remaining_cards * component_qty) AS units_remaining
  FROM ex WHERE is_bundle AND component_sku IS NOT NULL
  GROUP BY 1, 2, 3, 4
),
bp AS (
  SELECT plan_version, month, component_sku,
    STRING_AGG(IF(units_remaining > 0, CONCAT(card_sku, ' ', CAST(ROUND(units_remaining, 1) AS STRING)), NULL), '; ' ORDER BY card_sku)
      AS bundle_breakdown_remaining,
    STRING_AGG(IF(units_full > 0, CONCAT(card_sku, ' ', CAST(ROUND(units_full, 1) AS STRING)), NULL), '; ' ORDER BY card_sku)
      AS bundle_breakdown_full
  FROM bundle_parts
  GROUP BY 1, 2, 3
),
agg AS (
  SELECT
    plan_version, ANY_VALUE(plan_kind) AS plan_kind, month, ANY_VALUE(month_position) AS month_position, component_sku,
    SUM(planned_cards * component_qty) AS planned_units_full_month,
    SUM(remaining_cards * component_qty) AS planned_units_remaining,
    SUM(IF(NOT is_bundle, planned_cards * component_qty, 0)) AS standalone_units_full_month,
    SUM(IF(NOT is_bundle, remaining_cards * component_qty, 0)) AS standalone_units_remaining,
    SUM(IF(is_bundle, planned_cards * component_qty, 0)) AS via_bundle_units_full_month,
    SUM(IF(is_bundle, remaining_cards * component_qty, 0)) AS via_bundle_units_remaining,
    SUM(IF(marketplace = 'WB', remaining_cards * component_qty, 0)) AS wb_units_remaining,
    SUM(IF(marketplace = 'OZON', remaining_cards * component_qty, 0)) AS ozon_units_remaining,
    STRING_AGG(DISTINCT IF(component_sku IS NULL, card_sku, NULL), ', ') AS bundles_without_bom_basis
  FROM ex
  GROUP BY plan_version, month, component_sku
)
SELECT
  agg.plan_version,
  agg.plan_kind,
  agg.month,
  agg.month_position,
  agg.component_sku,
  IF(agg.component_sku IS NULL, 'BOM_BASIS_MISSING', 'EXPANDED') AS expansion_status,
  agg.bundles_without_bom_basis,
  agg.planned_units_full_month,
  agg.planned_units_remaining,
  agg.standalone_units_full_month,
  agg.standalone_units_remaining,
  agg.via_bundle_units_full_month,
  agg.via_bundle_units_remaining,
  agg.wb_units_remaining,
  agg.ozon_units_remaining,
  bp.bundle_breakdown_full,
  bp.bundle_breakdown_remaining
FROM agg
LEFT JOIN bp USING (plan_version, month, component_sku)
