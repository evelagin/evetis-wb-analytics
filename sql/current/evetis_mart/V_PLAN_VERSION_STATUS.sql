-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLAN_VERSION_STATUS (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Статус каждой версии плана ВЫВОДИТСЯ при чтении из шапки, содержимого и событий
-- evetis_ref.REF_SALES_PLAN_APPROVAL (PROPOSED | APPROVED | REVOKED | WITHDRAWN). Событие,
-- нарушающее правила, не действует, даже если строку вставили в обход процедур:
--   * content_sha256 — SHA-256 канонической строки содержимого:
--       строки «месяц|площадка|карточка|planned_cards(NUMERIC, 4 знака)», сортировка по строке,
--       '\n', затем '\n#BOM\n' и строки базиса BOM «набор|компонент|qty» так же;
--     шапка обязана совпадать с пересчётом (иначе INTEGRITY_BROKEN), событие — с шапкой;
--   * MODEL_SCENARIO нельзя ни предложить, ни утвердить;
--   * APPROVED действует, только если раньше было действующее PROPOSED с тем же хешем, версия не
--     отозвана (WITHDRAWN) и это первое утверждение версии; effective_from_month — первое число
--     месяца, не раньше месяца утверждения (утверждение задним числом невозможно) и в горизонте;
--   * REVOKED действует после действующего APPROVED и снимает версию с месяца, следующего за
--     месяцем отзыва (прошедшие месяцы не переписываются).
-- Окна действия утверждённых версий не пересекаются: версия действует с effective_from_month до
-- effective_from_month следующего (по времени записи) утверждения, отзыва или конца горизонта.
-- SUPERSEDED — выводится, не записывается.
--
-- Грейн: plan_version.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
OPTIONS (description = "PR-PLAN-1. Статус версий плана продаж, выведенный из шапки, пересчитанного content_sha256 и событий REF_SALES_PLAN_APPROVAL: MODEL_SCENARIO | DRAFT | PROPOSED | APPROVED | SUPERSEDED | REVOKED | WITHDRAWN | INTEGRITY_BROKEN. Невалидные события не действуют. Окно действия утверждённой версии по месяцам.")
AS
WITH hdr AS (
  SELECT *, COUNT(*) OVER (PARTITION BY plan_version) AS header_rows
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY plan_version ORDER BY created_at) = 1
),
lk AS (
  SELECT plan_version, COUNT(*) AS line_rows, SUM(planned_cards) AS planned_cards_total, STRING_AGG(k, '\n' ORDER BY k) AS s
  FROM (
    SELECT plan_version, planned_cards,
      CONCAT(CAST(month AS STRING), '|', marketplace, '|', internal_sku, '|', CAST(planned_cards AS STRING)) AS k
    FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL`
  )
  GROUP BY plan_version
),
bk AS (
  SELECT plan_version, COUNT(*) AS bom_rows, STRING_AGG(k, '\n' ORDER BY k) AS s
  FROM (
    SELECT plan_version, CONCAT(bundle_sku, '|', component_sku, '|', CAST(component_qty AS STRING)) AS k
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS`
  )
  GROUP BY plan_version
),
v AS (
  SELECT
    h.*,
    IFNULL(lk.line_rows, 0) AS line_rows_actual,
    lk.planned_cards_total,
    IFNULL(bk.bom_rows, 0) AS bom_rows_actual,
    TO_HEX(SHA256(CONCAT(IFNULL(lk.s, ''), '\n#BOM\n', IFNULL(bk.s, '')))) AS recomputed_sha256
  FROM hdr h
  LEFT JOIN lk USING (plan_version)
  LEFT JOIN bk USING (plan_version)
),
e1 AS (
  SELECT
    e.plan_version, e.approval_status AS event, e.content_sha256 AS event_sha256, e.effective_from_month,
    e.approved_by AS actor, e.approved_at AS event_at, e.approval_reference, e.recorded_at,
    ROW_NUMBER() OVER (PARTITION BY e.plan_version ORDER BY e.recorded_at, e.approved_at, e.approval_status) AS seq,
    v.plan_kind, v.horizon_from, v.horizon_to,
    (v.header_rows = 1 AND v.content_sha256 = v.recomputed_sha256 AND e.content_sha256 = v.content_sha256) AS hash_ok,
    v.plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED') AS approvable_kind,
    (e.effective_from_month IS NOT NULL
     AND e.effective_from_month = DATE_TRUNC(e.effective_from_month, MONTH)
     AND e.effective_from_month >= DATE_TRUNC(DATE(e.approved_at), MONTH)
     AND e.effective_from_month >= DATE_TRUNC(v.horizon_from, MONTH)
     AND e.effective_from_month <= v.horizon_to) AS effective_ok
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL` e
  LEFT JOIN v USING (plan_version)
),
e2 AS (
  SELECT e1.*,
    (event = 'PROPOSED' AND IFNULL(approvable_kind, FALSE) AND IFNULL(hash_ok, FALSE)) AS p_ok
  FROM e1
),
e3 AS (
  SELECT e2.*,
    IFNULL(LOGICAL_OR(p_ok) OVER (PARTITION BY plan_version ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), FALSE)
      AS prior_p_ok
  FROM e2
),
e4 AS (
  SELECT e3.*,
    (event = 'APPROVED' AND IFNULL(approvable_kind, FALSE) AND IFNULL(hash_ok, FALSE) AND effective_ok AND prior_p_ok) AS a_cand
  FROM e3
),
e5 AS (
  SELECT e4.*,
    IFNULL(LOGICAL_OR(a_cand) OVER (PARTITION BY plan_version ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), FALSE)
      AS prior_a_cand
  FROM e4
),
e6 AS (
  SELECT e5.*,
    (event = 'WITHDRAWN' AND prior_p_ok AND NOT prior_a_cand) AS w_ok
  FROM e5
),
e7 AS (
  SELECT e6.*,
    IFNULL(LOGICAL_OR(w_ok) OVER (PARTITION BY plan_version ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), FALSE)
      AS prior_w_ok
  FROM e6
),
e8 AS (
  SELECT e7.*,
    (a_cand AND NOT prior_a_cand AND NOT prior_w_ok) AS a_ok
  FROM e7
),
e9 AS (
  SELECT e8.*,
    (event = 'REVOKED'
     AND IFNULL(LOGICAL_OR(a_ok) OVER (PARTITION BY plan_version ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), FALSE))
      AS r_ok
  FROM e8
),
ev AS (
  SELECT
    plan_version,
    COUNT(*) AS events_total,
    COUNTIF(NOT (p_ok OR a_ok OR w_ok OR r_ok)) AS events_ignored,
    MIN(IF(p_ok, recorded_at, NULL)) AS proposed_at,
    ARRAY_AGG(IF(a_ok, STRUCT(event_at, recorded_at, effective_from_month, actor, approval_reference, event_sha256), NULL)
              IGNORE NULLS ORDER BY seq LIMIT 1)[SAFE_OFFSET(0)] AS appr,
    MIN(IF(w_ok, recorded_at, NULL)) AS withdrawn_at,
    MIN(IF(r_ok, event_at, NULL)) AS revoked_at,
    STRING_AGG(IF(NOT (p_ok OR a_ok OR w_ok OR r_ok),
                  CONCAT(event, '@', FORMAT_TIMESTAMP('%F %T', recorded_at),
                         CASE
                           WHEN plan_kind IS NULL THEN ':NO_SUCH_VERSION'
                           WHEN NOT approvable_kind AND event IN ('PROPOSED', 'APPROVED') THEN ':MODEL_NOT_APPROVABLE'
                           WHEN NOT hash_ok AND event IN ('PROPOSED', 'APPROVED') THEN ':HASH_MISMATCH'
                           WHEN event = 'APPROVED' AND NOT effective_ok THEN ':EFFECTIVE_MONTH_INVALID'
                           WHEN event = 'APPROVED' AND NOT prior_p_ok THEN ':NOT_PROPOSED_BEFORE'
                           WHEN event = 'APPROVED' THEN ':DUPLICATE_OR_WITHDRAWN'
                           ELSE ':OUT_OF_ORDER'
                         END), NULL), '; ' ORDER BY seq) AS ignored_events
  FROM e9
  GROUP BY plan_version
),
appr AS (
  -- Окна действия утверждений: по времени записи; следующее утверждение обрезает предыдущее.
  SELECT
    v.plan_version,
    ev.appr.effective_from_month AS effective_from_month,
    ev.appr.recorded_at AS approved_recorded_at,
    MIN(ev.appr.effective_from_month) OVER (ORDER BY ev.appr.recorded_at ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)
      AS next_approval_from_month,
    IF(ev.revoked_at IS NULL, NULL, DATE_ADD(DATE_TRUNC(DATE(ev.revoked_at), MONTH), INTERVAL 1 MONTH)) AS revoke_end_month,
    DATE_ADD(DATE_TRUNC(v.horizon_to, MONTH), INTERVAL 1 MONTH) AS horizon_end_month
  FROM v
  JOIN ev USING (plan_version)
  WHERE ev.appr IS NOT NULL
),
win AS (
  SELECT plan_version, effective_from_month, next_approval_from_month,
    LEAST(IFNULL(next_approval_from_month, horizon_end_month), IFNULL(revoke_end_month, horizon_end_month), horizon_end_month)
      AS effective_to_month_exclusive
  FROM appr
)
SELECT
  v.plan_version,
  v.plan_kind,
  v.plan_name,
  v.method_id,
  v.method_version,
  v.parent_plan_version,
  v.source_kind,
  v.source_ref,
  v.horizon_from,
  v.horizon_to,
  v.marketplaces,
  v.basis_sales_as_of,
  v.basis_inventory_as_of,
  v.basis_inventory_freshness,
  v.created_at,
  v.created_by,
  v.notes,
  v.content_sha256,
  v.recomputed_sha256,
  v.header_rows,
  v.line_count AS line_count_declared,
  v.line_rows_actual,
  v.bom_basis_rows AS bom_rows_declared,
  v.bom_rows_actual,
  v.planned_cards_total,
  (v.header_rows = 1 AND v.content_sha256 = v.recomputed_sha256
   AND v.line_count = v.line_rows_actual AND v.bom_basis_rows = v.bom_rows_actual) AS integrity_ok,
  IFNULL(ev.events_total, 0) AS events_total,
  IFNULL(ev.events_ignored, 0) AS events_ignored,
  ev.ignored_events,
  ev.proposed_at,
  ev.withdrawn_at,
  ev.appr.event_at AS approved_at,
  ev.appr.actor AS approved_by,
  ev.appr.approval_reference AS approval_reference,
  ev.appr.event_sha256 AS approved_content_sha256,
  ev.revoked_at,
  w.effective_from_month,
  w.effective_to_month_exclusive,
  CASE
    WHEN NOT (v.header_rows = 1 AND v.content_sha256 = v.recomputed_sha256
              AND v.line_count = v.line_rows_actual AND v.bom_basis_rows = v.bom_rows_actual) THEN 'INTEGRITY_BROKEN'
    WHEN v.plan_kind = 'MODEL_SCENARIO' THEN 'MODEL_SCENARIO'
    WHEN ev.revoked_at IS NOT NULL THEN 'REVOKED'
    WHEN ev.appr IS NOT NULL AND w.next_approval_from_month IS NOT NULL
         AND (w.effective_to_month_exclusive <= w.effective_from_month
              OR DATE_TRUNC(CURRENT_DATE(), MONTH) >= w.next_approval_from_month) THEN 'SUPERSEDED'
    WHEN ev.appr IS NOT NULL THEN 'APPROVED'
    WHEN ev.withdrawn_at IS NOT NULL THEN 'WITHDRAWN'
    WHEN ev.proposed_at IS NOT NULL THEN 'PROPOSED'
    ELSE 'DRAFT'
  END AS lifecycle_status,
  (v.plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED') AND ev.proposed_at IS NOT NULL AND ev.appr IS NULL
   AND ev.withdrawn_at IS NULL AND v.content_sha256 = v.recomputed_sha256 AND v.header_rows = 1) AS approvable_now
FROM v
LEFT JOIN ev USING (plan_version)
LEFT JOIN win w USING (plan_version)
