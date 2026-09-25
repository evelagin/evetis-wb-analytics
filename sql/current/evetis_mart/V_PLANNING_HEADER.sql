-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLANNING_HEADER (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Шапка экрана планирования (одна строка): даты данных, свежесть запаса, какой план действует,
-- какая версия предложена владельцу (с хешем, который он утверждает), модельный сценарий
-- Control Tower и счётчики исключений. Путь утверждения — текстом: система не утверждает.
--
-- Грейн: одна строка.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_HEADER`
OPTIONS (description = "PR-PLAN-1. Шапка экрана планирования: даты запаса и продаж, свежесть, действующий утверждённый план (или его отсутствие), предложенная версия с content_sha256 и готовностью к утверждению, модельный сценарий Control Tower, счётчики исключений.")
AS
WITH d AS (
  SELECT
    (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`) AS inventory_as_of_date,
    (SELECT LEAST(MAX(IF(marketplace = 'WB', d, NULL)), MAX(IF(marketplace = 'OZON', d, NULL)),
                  DATE_SUB(DATE(MAX(refreshed_at)), INTERVAL 1 DAY))
     FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`) AS sales_as_of
),
st AS (
  SELECT ARRAY_AGG(STRUCT(plan_version, plan_kind, plan_name, lifecycle_status, content_sha256, integrity_ok, approvable_now,
                          horizon_from, horizon_to, created_at, created_by, approved_by, approved_at, approval_reference,
                          effective_from_month, effective_to_month_exclusive, line_rows_actual, planned_cards_total)) AS v
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
),
ex AS (
  SELECT
    COUNTIF(severity = 'BLOCKER') AS blockers,
    COUNTIF(severity = 'WARNING') AS warnings,
    COUNTIF(severity = 'INFO') AS infos,
    COUNTIF(exception_code = 'INVENTORY_STALE') AS stale_skus,
    STRING_AGG(DISTINCT exception_code, ', ' ORDER BY exception_code) AS exception_codes,
    ARRAY_AGG(IF(exception_code = 'INVENTORY_STALE', detail, NULL) IGNORE NULLS LIMIT 1)[SAFE_OFFSET(0)] AS stale_reason_example
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_EXCEPTIONS`
),
j AS (
  SELECT d.*, ex.*,
    (SELECT AS STRUCT x.* FROM UNNEST(st.v) x
     WHERE x.lifecycle_status = 'APPROVED' AND DATE_TRUNC(d.inventory_as_of_date, MONTH) >= x.effective_from_month
       AND DATE_TRUNC(d.inventory_as_of_date, MONTH) < x.effective_to_month_exclusive
     ORDER BY x.effective_from_month DESC LIMIT 1) AS a,
    (SELECT AS STRUCT x.* FROM UNNEST(st.v) x
     WHERE x.plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED') AND x.lifecycle_status NOT IN ('WITHDRAWN', 'INTEGRITY_BROKEN')
     ORDER BY x.created_at DESC LIMIT 1) AS p,
    (SELECT AS STRUCT x.* FROM UNNEST(st.v) x WHERE x.plan_kind = 'MODEL_SCENARIO' ORDER BY x.created_at DESC LIMIT 1) AS m,
    (SELECT COUNT(*) FROM UNNEST(st.v)) AS versions_total
  FROM d CROSS JOIN st CROSS JOIN ex
)
SELECT
  CURRENT_TIMESTAMP() AS rendered_at,
  inventory_as_of_date,
  sales_as_of,
  IF(stale_skus > 0, 'STALE', 'FRESH') AS inventory_freshness_status,
  stale_skus,
  stale_reason_example AS inventory_freshness_reason,
  IF(a.plan_version IS NULL, 'NO_APPROVED_PLAN', 'APPROVED') AS approved_plan_status,
  a.plan_version AS approved_plan_version,
  a.content_sha256 AS approved_content_sha256,
  a.approved_by,
  a.approved_at,
  a.approval_reference,
  a.effective_from_month AS approved_effective_from_month,
  p.plan_version AS proposed_plan_version,
  p.plan_name AS proposed_plan_name,
  p.plan_kind AS proposed_plan_kind,
  p.lifecycle_status AS proposed_lifecycle_status,
  p.content_sha256 AS proposed_content_sha256,
  p.approvable_now AS proposed_approvable_now,
  p.horizon_from AS proposed_horizon_from,
  p.horizon_to AS proposed_horizon_to,
  p.line_rows_actual AS proposed_lines,
  p.planned_cards_total AS proposed_cards_total,
  p.created_at AS proposed_created_at,
  m.plan_version AS model_plan_version,
  m.lifecycle_status AS model_lifecycle_status,
  m.content_sha256 AS model_content_sha256,
  versions_total,
  blockers,
  warnings,
  infos,
  exception_codes,
  'Утверждение только владельцем: содержимое версии → ACK на её content_sha256 → процедура evetis_ref.sp_plan_approve (версия, хеш, месяц начала, кто, основание). Система не утверждает; модельный сценарий утвердить нельзя.'
    AS approval_path
FROM j
