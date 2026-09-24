-- ============================================================================
-- PR-PROMO-3 · ОТКАТ слоя экономики акций.
-- Удаляет 7 представлений экономики (в порядке, обратном зависимостям) и 2 процедуры снимков.
-- Перед этим поставить на паузу расписания promo-econ-basis-wb и promo-econ-basis-ozon
-- (Terraform: paused = true, целевой apply) — иначе они упадут на отсутствующей процедуре.
--
-- Таблицы снимков wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT и
-- ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT НЕ удаляются намеренно: это единственная
-- запись того, какой была экономика на каждом прошлом слоте, восстановить её нельзя
-- (канонические вью считаются от «сейчас»). Их удаление — отдельное решение владельца.
-- PR-PROMO-1/2 (наблюдения и состояние акций) не затрагиваются.
-- После отката: удалить записи 5 объектов из sql/current/*/MANIFEST.json и их файлы.
-- ============================================================================
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_COVERAGE_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_SCENARIO_HISTORY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.sp_snapshot_ozon_promo_economics_basis`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.sp_snapshot_wb_promo_economics_basis`;
