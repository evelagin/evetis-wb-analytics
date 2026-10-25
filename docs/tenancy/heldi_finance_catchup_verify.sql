-- Heldi (client_001): проверка дней финансового догона после загрузки. Только SELECT.
-- Запуск: bq --project_id=mpa-t-client-001 query --nouse_legacy_sql < docs/tenancy/heldi_finance_catchup_verify.sql
-- Перед запуском заменить список target на дни авторитета (ниже — контрольные уже принятые дни
-- 07.10 FULL, 20–21.10 CURRENT и один пропуск 22.10, который обязан дать FAIL до догона).
-- Партиционные фильтры по event_date/coverage_date держат объём чтения в пределах нескольких МБ.

-- V1–V6: по каждому дню — дедупликация, покрытие, классификация, связка SKU, провенанс, витрина.
WITH target AS (
  SELECT d FROM UNNEST([DATE '2026-10-07', DATE '2026-10-20', DATE '2026-10-21', DATE '2026-10-22']) d
),
raw AS (
  SELECT r.event_date AS d, COUNT(*) AS raw_rows,
    COUNT(DISTINCT TO_JSON_STRING(STRUCT(r.accrual_id, r.type_id, r.sku))) AS natural_keys,
    COUNTIF(r.operation_name IS NULL OR r.operation_name = 'UNKNOWN') AS unresolved_operations,
    COUNTIF(t.type_id IS NULL) AS unknown_type_rows,
    COUNT(DISTINCT r.ingestion_run_id) AS runs,
    ROUND(SUM(r.amount_rub), 2) AS amount_rub,
    ARRAY_AGG(DISTINCT r.ingestion_run_id IGNORE NULLS) AS run_ids
  FROM `mpa-t-client-001.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` r
  LEFT JOIN `mpa-t-client-001.ozon_mart.DIM_OZON_ACCRUAL_TYPE` t ON t.type_id = r.type_id
  WHERE r.event_date IN (SELECT d FROM target)
  GROUP BY 1
),
catalog AS (
  SELECT DISTINCT sku FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`
  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG` WHERE snapshot_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 14 DAY))
    AND sku IS NOT NULL
),
linkage AS (
  SELECT r.event_date AS d, COUNTIF(r.sku IS NOT NULL AND c.sku IS NULL) AS unlinked_sku_rows
  FROM `mpa-t-client-001.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` r LEFT JOIN catalog c ON c.sku = r.sku
  WHERE r.event_date IN (SELECT d FROM target)
  GROUP BY 1
),
journal AS (
  SELECT j.ingestion_run_id, LOGICAL_AND(j.status = 'OK') AS ok, ANY_VALUE(j.backfill_plan_id) AS plan_id
  FROM `mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS` j
  WHERE j.entity = 'finance_accrual' AND j.started_at >= TIMESTAMP '2026-10-01'
  GROUP BY 1
),
provenance AS (
  SELECT r.d, COUNTIF(j.ingestion_run_id IS NULL OR NOT j.ok OR j.plan_id IS NULL) AS unproven_runs
  FROM raw r, UNNEST(r.run_ids) run LEFT JOIN journal j ON j.ingestion_run_id = run
  GROUP BY 1
),
coverage AS (
  SELECT c.coverage_date AS d, c.status, c.reason FROM `mpa-t-client-001.tenant_ops.V_COVERAGE_DAILY` c
  WHERE c.entity = 'finance_accrual' AND c.coverage_date IN (SELECT d FROM target)
),
loaded AS (
  SELECT coverage_date AS d, MAX(rows_loaded) AS rows_loaded FROM `mpa-t-client-001.tenant_ops.DATA_COVERAGE`
  WHERE entity = 'finance_accrual' AND coverage_date IN (SELECT d FROM target) AND status = 'COMPLETE'
  GROUP BY 1
),
dq AS (
  -- FULL пишет FIN_CLASSIFICATION на окно листа (7 дней), CURRENT и догон — на один день.
  SELECT t.d, ARRAY_AGG(q.status ORDER BY q.evaluated_at DESC LIMIT 1)[OFFSET(0)] AS fin_classification
  FROM target t JOIN `mpa-t-client-001.tenant_ops.DQ_RESULTS` q
    ON q.check_id = 'FIN_CLASSIFICATION' AND t.d BETWEEN q.period_from AND q.period_to
  GROUP BY 1
),
share AS (
  SELECT event_date AS d, COUNT(*) AS store_cost_rows, STRING_AGG(DISTINCT data_status) AS share_status
  FROM `mpa-t-client-001.analytics_share.store_costs_daily`
  WHERE event_date IN (SELECT d FROM target)
  GROUP BY 1
)
SELECT t.d AS day,
  IFNULL(raw.raw_rows, 0) AS raw_rows, raw.natural_keys, raw.amount_rub, raw.runs,
  cov.status AS coverage_status, loaded.rows_loaded, dq.fin_classification,
  raw.unresolved_operations, raw.unknown_type_rows, lk.unlinked_sku_rows, pv.unproven_runs,
  share.store_cost_rows, share.share_status,
  CASE
    WHEN raw.raw_rows IS NULL OR cov.status IS NULL THEN 'FAIL_MISSING'
    WHEN raw.raw_rows != raw.natural_keys THEN 'FAIL_DUPLICATE_NATURAL_KEY'
    WHEN cov.status != 'COMPLETE' OR loaded.rows_loaded IS NULL OR loaded.rows_loaded != raw.raw_rows THEN 'FAIL_COVERAGE'
    WHEN raw.runs != 1 OR pv.unproven_runs > 0 THEN 'FAIL_PROVENANCE'
    WHEN raw.unresolved_operations > 0 OR IFNULL(dq.fin_classification, 'ABSENT') != 'PASS' THEN 'FAIL_CLASSIFICATION'
    WHEN lk.unlinked_sku_rows > 0 THEN 'FAIL_SKU_LINKAGE'
    WHEN share.share_status IS NULL OR share.share_status != 'COMPLETE' THEN 'FAIL_ANALYTICS_SHARE'
    ELSE 'PASS'
  END AS verdict
FROM target t
LEFT JOIN raw ON raw.d = t.d LEFT JOIN coverage cov ON cov.d = t.d LEFT JOIN loaded ON loaded.d = t.d
LEFT JOIN dq ON dq.d = t.d LEFT JOIN linkage lk ON lk.d = t.d LEFT JOIN provenance pv ON pv.d = t.d
LEFT JOIN share ON share.d = t.d
ORDER BY day;

-- V7: окно финансов в analytics_share — пропусков в хвосте не осталось (после догона всех дней
-- missing_days = 0; статус дальше определяется сроком созревания, а не пропусками).
SELECT from_date, last_evaluated_date, incomplete_days, missing_days, maturity_days, status, reason
FROM `mpa-t-client-001.tenant_ops.V_FINANCE_WINDOW_STATUS`
WHERE from_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-24'
ORDER BY from_date;

-- V8: принятая история не переписана — отпечаток всех дней вне авторитета до и после догона
-- должен совпасть построчно (снять до первого запуска и сравнить после).
SELECT event_date, COUNT(*) AS n, ROUND(SUM(amount_rub), 2) AS amount_rub,
  COUNT(DISTINCT ingestion_run_id) AS runs, MAX(extracted_at) AS max_extracted_at
FROM `mpa-t-client-001.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
WHERE event_date BETWEEN DATE '2026-04-09' AND DATE '2026-10-24'
  AND event_date NOT IN UNNEST([DATE '2026-10-08', DATE '2026-10-09', DATE '2026-10-10', DATE '2026-10-11',
                                DATE '2026-10-12', DATE '2026-10-17', DATE '2026-10-19', DATE '2026-10-22', DATE '2026-10-23'])
GROUP BY 1
ORDER BY 1;
