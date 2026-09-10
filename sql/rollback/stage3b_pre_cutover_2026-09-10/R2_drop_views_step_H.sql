-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R2 · удалить вью шага H (их не было до cutover)
-- Снято с production 2026-09-10 до шага B1 (INFORMATION_SCHEMA.*.ddl, read-only).
-- Это ТОЧНОЕ состояние до cutover, а не реконструкция из Git.
-- Манифест и порядок: docs/ops/STAGE_B_CUTOVER_ROLLBACK_MANIFEST.md
-- ============================================================================
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION_DAILY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION`;
