-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SALES_PLAN_APPROVED (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- ЕДИНСТВЕННЫЙ контракт бизнес-плана продаж для downstream. Строка есть только если месяц входит
-- в окно действия утверждённой владельцем версии (V_PLAN_VERSION_STATUS): прошлые окна
-- заменённых и отозванных версий остаются историей и не переписываются. Версия с нарушенной
-- целостностью (INTEGRITY_BROKEN) не даёт ни одной строки. MODEL_SCENARIO, DRAFT, PROPOSED —
-- никогда. Пока ничего не утверждено, представление пусто — это правильный ответ.
--
-- Грейн: month × marketplace × internal_sku (продаваемая карточка).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_APPROVED`
OPTIONS (description = "PR-PLAN-1. Утверждённый владельцем план продаж: месяц × площадка × карточка → planned_cards с версией, хешем и временем утверждения. Только окна действия утверждённых версий; модельные, черновые и предложенные версии сюда не попадают. Пусто, пока нет утверждения.")
AS
WITH s AS (
  SELECT plan_version, plan_kind, plan_name, method_id, horizon_from, horizon_to, created_at, created_by,
    lifecycle_status, effective_from_month, effective_to_month_exclusive, approved_at, approved_by,
    approval_reference, approved_content_sha256
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
  WHERE lifecycle_status IN ('APPROVED', 'SUPERSEDED', 'REVOKED')
    AND effective_from_month IS NOT NULL
    AND effective_to_month_exclusive > effective_from_month
)
SELECT
  l.month,
  l.marketplace,
  l.internal_sku,
  l.sales_mode,
  l.planned_cards,
  l.plan_version,
  s.plan_kind,
  s.plan_name,
  s.method_id,
  s.horizon_from,
  s.horizon_to,
  s.created_at AS plan_created_at,
  s.created_by AS plan_created_by,
  s.lifecycle_status AS version_lifecycle_status,
  s.effective_from_month,
  s.effective_to_month_exclusive,
  s.approved_at,
  s.approved_by,
  s.approval_reference,
  s.approved_content_sha256
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL` l
JOIN s ON s.plan_version = l.plan_version
WHERE l.month >= s.effective_from_month AND l.month < s.effective_to_month_exclusive
