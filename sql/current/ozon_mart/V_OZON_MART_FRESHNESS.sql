-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_MART_FRESHNESS (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4c_ozon_mart.sql (parity: EXACT_TEXT).
-- Internal dependencies: none.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`
OPTIONS (description = "Контракт свежести домена Ozon. Марты - VIEW и относительно ozon_raw устареть не могут; здесь измеряется свежесть самого ozon_raw относительно Ozon. Состояния FRESH / PARTIAL_SOURCE_UPDATE / STALE / REFRESH_FAILED. Агент обязан отказываться от решений при STALE и REFRESH_FAILED.")
AS
WITH tol AS (
  SELECT * FROM UNNEST([
    STRUCT('fbo_postings' AS entity, 12 AS tolerance_hours),
    ('finance_accrual', 30), ('ads_expense_daily', 30), ('ads_sku_daily', 30)])),
runs AS (
  SELECT entity,
    MAX(IF(status='OK', completed_at, NULL)) last_ok_at,
    MAX(completed_at) last_attempt_at,
    ANY_VALUE(status HAVING MAX completed_at) last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' GROUP BY entity),
e AS (
  SELECT t.entity, t.tolerance_hours, r.last_ok_at, r.last_attempt_status,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.last_ok_at, HOUR) age_hours,
    CASE
      WHEN r.last_ok_at IS NULL THEN 'NEVER_INGESTED'
      WHEN r.last_attempt_status <> 'OK' AND r.last_attempt_at > r.last_ok_at THEN 'FAILED'
      WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.last_ok_at, HOUR) > t.tolerance_hours THEN 'STALE'
      ELSE 'FRESH' END entity_state
  FROM tol t LEFT JOIN runs r USING (entity))
SELECT
  CURRENT_TIMESTAMP() mart_computed_at,
  (SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`) source_max_posting_date,
  (SELECT MAX(event_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`) source_max_finance_date,
  (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`) source_max_ads_date,
  (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`) source_max_ads_sku_date,
  (SELECT MAX(last_ok_at) FROM e) last_successful_ingestion_at,
  (SELECT MAX(age_hours) FROM e) worst_source_age_hours,
  (SELECT STRING_AGG(CONCAT(entity,'=',entity_state,'(',CAST(IFNULL(age_hours,-1) AS STRING),'ч)'), ' | ' ORDER BY entity) FROM e) entity_states,
  CASE
    WHEN (SELECT COUNTIF(entity_state IN ('FAILED','NEVER_INGESTED')) FROM e) > 0 THEN 'REFRESH_FAILED'
    WHEN (SELECT COUNTIF(entity_state='STALE') FROM e) = (SELECT COUNT(*) FROM e) THEN 'STALE'
    WHEN (SELECT COUNTIF(entity_state='STALE') FROM e) > 0 THEN 'PARTIAL_SOURCE_UPDATE'
    ELSE 'FRESH' END freshness_status,
  CASE
    WHEN (SELECT COUNTIF(entity_state IN ('FAILED','NEVER_INGESTED','STALE')) FROM e) > 0
      THEN 'DECISIONS_BLOCKED_SOURCE_NOT_FRESH'
    ELSE 'DECISIONS_ALLOWED' END agent_decision_gate;
