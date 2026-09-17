-- ============================================================================
-- ОТКАТ EXECUTIVE V2 · PHASE C2 (BigQuery)
--
-- ПОРЯДОК:
--   1. Сначала вернуть Dashboard 2 на карточки Phase B:
--        python3 tools/metabase_exec_v2_c2_switch.py --rollback
--      (или tools/metabase_exec_v2_rollback.py для полного возврата к Phase A).
--   2. Выключить расписание: terraform — удалить infra/terraform/executive_v2_layer.tf
--      целевым apply, либо `gcloud scheduler jobs pause executive-v2-layer-build`.
--   3. Только после этого удалять объекты ниже. Ни один канонический V_DASH_* не
--      зависит от слоя — откат их не затрагивает.
--
-- Слой можно и НЕ удалять: изолированная таблица без читателей безвредна.
-- ============================================================================
DROP VIEW      IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_V2_DAILY`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_executive_v2_daily`;
DROP TABLE     IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_DAILY`;
DROP TABLE     IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._EXECUTIVE_V2_BUILD_LOCK`;
-- Журнал сборок оставить как аудиторский след; удалить отдельно при необходимости:
-- DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.EXECUTIVE_V2_BUILD_LOG`;
