-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLAN_LINE_MONTHLY_ALL (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Строки всех зарегистрированных версий плана в одном грейне:
--   plan_version × month × marketplace × internal_sku (продаваемая карточка).
-- Собственные версии — evetis_ref.PLAN_LINE_MONTHLY. Legacy-сценарий Control Tower (SET_2026-09-09)
-- не копируется: строки читаются из CT_SEASON_PLAN_MONTHLY по source_ref шапки, planned_cards
-- приводится к NUMERIC с 4 знаками (тот же канон, что у content_sha256).
-- Статус версии здесь не применяется: это содержимое, а не бизнес-план.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL`
OPTIONS (description = "PR-PLAN-1. Строки всех версий плана продаж (собственные и legacy Control Tower через адаптер): plan_version × месяц × площадка × карточка → planned_cards. Содержимое версии без статуса; бизнес-план — только V_SALES_PLAN_APPROVED.")
AS
WITH h AS (
  SELECT plan_version, plan_kind, source_kind, source_ref
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY plan_version ORDER BY created_at) = 1
),
native AS (
  SELECT l.plan_version, h.plan_kind, l.month, l.marketplace, l.internal_sku, l.sales_mode,
    ROUND(l.planned_cards, 4) AS planned_cards, l.line_basis
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_LINE_MONTHLY` l
  JOIN h ON h.plan_version = l.plan_version AND h.source_kind = 'PLAN_LINE_MONTHLY'
),
legacy AS (
  SELECT h.plan_version, ANY_VALUE(h.plan_kind) AS plan_kind, c.month, UPPER(c.marketplace) AS marketplace, c.internal_sku,
    ANY_VALUE(c.sales_mode) AS sales_mode,
    ROUND(CAST(SUM(c.target_cards) AS NUMERIC), 4) AS planned_cards,
    'LEGACY_CT_SEASON_PLAN_MONTHLY' AS line_basis
  FROM h
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` c ON c.plan_version = h.source_ref
  WHERE h.source_kind = 'LEGACY_CT_SEASON_PLAN_MONTHLY'
  GROUP BY h.plan_version, c.month, UPPER(c.marketplace), c.internal_sku
)
SELECT * FROM native
UNION ALL
SELECT * FROM legacy
