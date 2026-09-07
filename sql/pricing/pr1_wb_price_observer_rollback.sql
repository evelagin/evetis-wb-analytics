-- ============================================================================
-- PR-1 — откат наблюдателя цен WB.
--
-- Порядок: сначала вью (зависимости), затем таблицы.
-- Ничего, созданного до PR-1, здесь не упоминается: наблюдатель ни от чего
-- не наследуется и ни на что не влияет — откат полностью локален.
--
-- ⚠️ Таблицы содержат ЕДИНСТВЕННУЮ существующую историю цен продавца WB.
-- Восстановить её из продаж/реализации нельзя: это разные величины
-- (наблюдённая форвардная цена ≠ исторически реализованная).
-- Удалять только при осознанном отказе от Stage PR-1.
-- ============================================================================

DROP VIEW  IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVER_HEALTH`;
DROP VIEW  IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_OBSERVED_CHANGES`;
DROP VIEW  IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_PRICES_CURRENT`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_PRICES_OBSERVATIONS`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PRICES`;

-- Строка реестра пайплайнов (если была добавлена):
-- DELETE FROM `project-fa311fc0-4d87-4781-986.wb_ops.OPS_PIPELINE_REGISTRY`
-- WHERE pipeline_id = 'wb_prices_observer';
