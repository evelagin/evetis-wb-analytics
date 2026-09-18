-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_TARIFF_CHANGE_LOG (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d2_ozon_mart_forward.sql (parity: COMMENTS_WHITESPACE_ONLY).
-- Internal dependencies: none.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_CHANGE_LOG`
OPTIONS (description = "Детектор изменения действующего тарифа Ozon: значение компоненты в последнем снимке против предыдущего. change_type = VALUE_CHANGED / NEW_COMPONENT / COMPONENT_DISAPPEARED / UNKNOWN_COMPONENT / NO_BASELINE / UNCHANGED. Материальное изменение обязано быть классифицировано человеком до возобновления решений.")
AS
WITH c AS (
  SELECT snapshot_date, snapshot_ts, offer_id, sale_scheme, commission_component,
         api_field, value_num, unit, is_known_component
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`),
ranked AS (
  SELECT *, DENSE_RANK() OVER (ORDER BY snapshot_date DESC) AS rk FROM c),
cur AS (SELECT * FROM ranked WHERE rk = 1),
prv AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY offer_id, sale_scheme, commission_component
                                   ORDER BY snapshot_date DESC) AS rn
    FROM ranked r WHERE rk > 1) WHERE rn = 1)
SELECT
  CURRENT_TIMESTAMP() AS detected_at,
  COALESCE(cur.offer_id, prv.offer_id) AS offer_id,
  COALESCE(cur.sale_scheme, prv.sale_scheme) AS sale_scheme,
  COALESCE(cur.commission_component, prv.commission_component) AS commission_component,
  COALESCE(cur.api_field, prv.api_field) AS api_field,
  COALESCE(cur.unit, prv.unit) AS unit,
  prv.snapshot_date AS previous_snapshot_date,
  prv.value_num AS previous_value,
  cur.snapshot_date AS current_snapshot_date,
  cur.value_num AS current_value,
  ROUND(cur.value_num - prv.value_num, 4) AS delta,
  CASE
    WHEN cur.offer_id IS NOT NULL AND NOT cur.is_known_component THEN 'UNKNOWN_COMPONENT'
    WHEN prv.offer_id IS NULL THEN 'NO_BASELINE'
    WHEN cur.offer_id IS NULL THEN 'COMPONENT_DISAPPEARED'
    WHEN prv.value_num IS NULL AND cur.value_num IS NOT NULL THEN 'NEW_COMPONENT'
    WHEN cur.value_num <> prv.value_num THEN 'VALUE_CHANGED'
    ELSE 'UNCHANGED' END AS change_type,
  CASE
    WHEN cur.offer_id IS NOT NULL AND NOT cur.is_known_component THEN TRUE
    WHEN prv.offer_id IS NULL OR cur.offer_id IS NULL THEN TRUE
    WHEN cur.value_num <> prv.value_num THEN TRUE
    ELSE FALSE END AS tariff_change_detected
FROM cur FULL JOIN prv USING (offer_id, sale_scheme, commission_component);
