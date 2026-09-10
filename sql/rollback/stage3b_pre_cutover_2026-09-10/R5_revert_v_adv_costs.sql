-- ============================================================================
-- EVETIS · Stage 3B ROLLBACK · R5 · вернуть V_ADV_COSTS на UNION_PREBOOTSTRAP (только ПОЛНЫЙ откат)
-- Снято с production 2026-09-10 до шага B1 (INFORMATION_SCHEMA.*.ddl, read-only).
-- Это ТОЧНОЕ состояние до cutover, а не реконструкция из Git.
-- Манифест и порядок: docs/ops/STAGE_B_CUTOVER_ROLLBACK_MANIFEST.md
-- ============================================================================
-- sha256(view_definition) = 0699a82ed08d0bc676ef13265fed8eb5e3b88f7bbcf08eb05d6f64075c2968af
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`
AS SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP`;
