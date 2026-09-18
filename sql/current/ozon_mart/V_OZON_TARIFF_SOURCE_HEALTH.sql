-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d3_ozon_mart_agent_contract.sql (parity: TEXT_DIFFERS_SCHEMA_DATA_EQUIVALENT).
-- Internal dependencies: V_OZON_SKU_CURRENT_TARIFF.
-- Older definition in sql/ozon/stage3_4d2_ozon_mart_forward.sql is SUPERSEDED
-- and must not be applied (it would break dependants; proven in R2 forensic preflight).
-- Live text differs from the historical file but is schema- and data-equivalent
-- (R2A preflight); per owner decision the validated live definition is canonical.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`
OPTIONS (description = "Ворота источника действующего тарифа Ozon и готовности агента. Стоп-краны: возраст снимка prices (30 ч), возраст seller_info (30 ч), неклассифицированная компонента, неполное покрытие тарифа, отсутствие второй базы сравнения. Три уровня готовности: forward_economics_ready (аналитика), agent_shadow_ready (теневой режим), agent_write_ready (всегда FALSE, не выводится ни из чего).")
AS
WITH snap AS (
  SELECT MAX(snapshot_date) AS latest_date, MAX(snapshot_ts) AS latest_ts
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`),
runs AS (
  SELECT MAX(IF(status='OK', completed_at, NULL)) AS last_ok_at, MAX(completed_at) AS last_attempt_at,
         ANY_VALUE(status HAVING MAX completed_at) AS last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' AND entity='prices'),
si_runs AS (
  SELECT MAX(IF(status='OK', completed_at, NULL)) AS last_ok_at, MAX(completed_at) AS last_attempt_at,
         ANY_VALUE(status HAVING MAX completed_at) AS last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' AND entity='seller_info'),
si AS (
  SELECT retrieved_at, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), retrieved_at, HOUR) AS age_hours
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SELLER_INFO`
  QUALIFY ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) = 1),
unk AS (
  SELECT COUNTIF(NOT is_known_component) AS unknown_rows,
         STRING_AGG(DISTINCT IF(is_known_component, NULL, api_field), ', ') AS unknown_fields
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`
  WHERE snapshot_date = (SELECT latest_date FROM snap)),
base AS (
  SELECT COUNT(DISTINCT snapshot_date) AS complete_snapshots, MIN(snapshot_date) AS first_complete_date
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`),
cov AS (
  SELECT COUNT(*) AS offers,
         COUNTIF(sales_percent_fbo IS NOT NULL AND fbo_direct_flow_trans_min_rub IS NOT NULL
                 AND fbo_direct_flow_trans_max_rub IS NOT NULL AND fbo_deliv_to_customer_rub IS NOT NULL
                 AND acquiring_rub IS NOT NULL) AS offers_full_tariff
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
  WHERE snapshot_date = (SELECT latest_date FROM snap)),
sku AS (
  SELECT COUNT(*) AS sku_rows,
         COUNTIF(internal_sku IS NOT NULL AND ozon_sku IS NOT NULL AND offer_id IS NOT NULL) AS sku_identity_ok,
         COUNTIF(current_management_cogs_rub IS NOT NULL AND current_management_cogs_rub > 0) AS sku_cogs_ok,
         COUNTIF(premium_status LIKE 'UNKNOWN%') AS sku_premium_unknown
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
calc AS (
  SELECT
    (SELECT latest_date FROM snap) AS tariff_snapshot_date,
    (SELECT latest_ts FROM snap) AS tariff_snapshot_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), (SELECT latest_ts FROM snap), HOUR) AS tariff_age_hours,
    (SELECT last_ok_at FROM runs) AS prices_last_ok_at,
    (SELECT last_attempt_status FROM runs) AS prices_last_attempt_status,
    (SELECT last_attempt_at FROM runs) AS prices_last_attempt_at,
    (SELECT retrieved_at FROM si) AS seller_info_at,
    (SELECT age_hours FROM si) AS seller_info_age_hours,
    (SELECT last_ok_at FROM si_runs) AS seller_info_last_ok_at,
    (SELECT offers FROM cov) AS offers_in_snapshot,
    (SELECT offers_full_tariff FROM cov) AS offers_with_full_tariff,
    IFNULL((SELECT unknown_rows FROM unk), 0) AS unknown_component_rows,
    (SELECT unknown_fields FROM unk) AS unknown_component_fields,
    (SELECT complete_snapshots FROM base) AS complete_tariff_snapshots,
    (SELECT first_complete_date FROM base) AS first_complete_tariff_date,
    (SELECT sku_rows FROM sku) AS sku_rows,
    (SELECT sku_identity_ok FROM sku) AS sku_identity_ok,
    (SELECT sku_cogs_ok FROM sku) AS sku_cogs_ok,
    (SELECT sku_premium_unknown FROM sku) AS sku_premium_unknown),
st AS (
  SELECT c.*,
    CASE
      WHEN c.prices_last_ok_at IS NULL THEN 'NEVER_INGESTED'
      WHEN c.prices_last_attempt_status <> 'OK' AND c.prices_last_attempt_at > c.prices_last_ok_at THEN 'REFRESH_FAILED'
      WHEN c.tariff_age_hours > 30 THEN 'STALE' ELSE 'FRESH' END AS tariff_freshness_status,
    CASE
      WHEN c.seller_info_at IS NULL THEN 'NEVER_INGESTED'
      WHEN c.seller_info_age_hours > 30 THEN 'STALE' ELSE 'FRESH' END AS seller_info_freshness_status,
    IF(c.complete_tariff_snapshots >= 2, 'BASELINE_OK', 'FIRST_BASELINE_PENDING') AS tariff_change_baseline_status
  FROM calc c)
SELECT
  CURRENT_TIMESTAMP() AS checked_at,
  st.tariff_snapshot_date, st.tariff_snapshot_at, st.tariff_age_hours, 30 AS tariff_tolerance_hours,
  st.prices_last_ok_at, st.prices_last_attempt_status,
  st.seller_info_at, st.seller_info_age_hours, st.seller_info_last_ok_at, 30 AS seller_info_tolerance_hours,
  st.offers_in_snapshot, st.offers_with_full_tariff,
  st.sku_rows, st.sku_identity_ok, st.sku_cogs_ok, st.sku_premium_unknown,
  st.unknown_component_rows, st.unknown_component_fields,
  st.complete_tariff_snapshots, st.first_complete_tariff_date,
  st.tariff_freshness_status, st.seller_info_freshness_status, st.tariff_change_baseline_status,
  st.unknown_component_rows > 0 AS unknown_tariff_component_detected,
  (   st.tariff_freshness_status = 'FRESH' AND st.unknown_component_rows = 0
   AND st.offers_with_full_tariff = st.offers_in_snapshot) AS forward_economics_ready,
  CASE
    WHEN st.unknown_component_rows > 0 THEN 'DECISIONS_BLOCKED_UNKNOWN_TARIFF_COMPONENT'
    WHEN st.tariff_freshness_status <> 'FRESH' THEN 'DECISIONS_BLOCKED_SOURCE_NOT_FRESH'
    WHEN st.offers_with_full_tariff < st.offers_in_snapshot THEN 'DECISIONS_BLOCKED_INCOMPLETE_TARIFF_COVERAGE'
    ELSE 'DECISIONS_ALLOWED' END AS agent_decision_gate,
  (   st.tariff_freshness_status = 'FRESH' AND st.seller_info_freshness_status = 'FRESH'
   AND st.unknown_component_rows = 0 AND st.offers_with_full_tariff = st.offers_in_snapshot
   AND st.sku_rows > 0 AND st.sku_identity_ok = st.sku_rows AND st.sku_cogs_ok = st.sku_rows
   AND st.tariff_change_baseline_status = 'BASELINE_OK') AS agent_shadow_ready,
  CASE
    WHEN st.tariff_change_baseline_status <> 'BASELINE_OK' THEN 'SHADOW_BLOCKED_FIRST_BASELINE_PENDING'
    WHEN st.seller_info_freshness_status <> 'FRESH' THEN 'SHADOW_BLOCKED_SELLER_INFO_NOT_FRESH'
    WHEN st.tariff_freshness_status <> 'FRESH' THEN 'SHADOW_BLOCKED_TARIFF_NOT_FRESH'
    WHEN st.unknown_component_rows > 0 THEN 'SHADOW_BLOCKED_UNKNOWN_TARIFF_COMPONENT'
    WHEN st.offers_with_full_tariff < st.offers_in_snapshot THEN 'SHADOW_BLOCKED_INCOMPLETE_TARIFF_COVERAGE'
    WHEN st.sku_identity_ok < st.sku_rows THEN 'SHADOW_BLOCKED_IDENTITY_INCOMPLETE'
    WHEN st.sku_cogs_ok < st.sku_rows THEN 'SHADOW_BLOCKED_COGS_INCOMPLETE'
    ELSE 'SHADOW_ALLOWED' END AS agent_shadow_gate,
  FALSE AS agent_write_ready,
  'HARDCODED_FALSE_NOT_DERIVABLE_FROM_DATA_REQUIRES_SEPARATE_OWNER_ACK' AS agent_write_gate
FROM st;
