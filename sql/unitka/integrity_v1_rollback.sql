-- ============================================================================
-- UNITKA INTEGRITY GUARD V1 — откат слоя фактов целостности.
-- Удаляет ТОЛЬКО две вью Guard. Бизнес-данных не касается: вью ничего не хранят.
-- Перед откатом: UNITKA_INTEGRITY_MODE=off на Job'ах unitka-engine-* (иначе прогон
-- в режиме observe зафиксирует INTEGRITY_SUBSYSTEM_FAILURE — факт-запись при этом не страдает).
-- Вью читают только wb_raw/wb_mart; физическую копию COGS откатывает отдельно
-- sql/unitka/cogs_publication_v1_rollback.sql (ПОСЛЕ этого файла).
-- ============================================================================
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_COGS_CANONICAL`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_INTEGRITY`;
