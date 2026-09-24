-- ============================================================================
-- PR-PROMO-4 · ОТКАТ слоя запасов и распродажи.
-- Удаляет 7 представлений evetis_mart (в порядке, обратном зависимостям). Никакой объект
-- Control Tower, evetis_ops, PR-PROMO-1/2/3 не затрагивается: слой их только читает.
--
-- Справочники владельца evetis_ref.REF_SALES_PLAN_APPROVAL и REF_SKU_INVENTORY_TARGET
-- удаляются ТОЛЬКО если пусты: в них могут быть решения владельца, восстановить которые
-- нельзя. Проверка — первые два оператора (ASSERT падает, если строки есть).
-- После отката: удалить записи 7 объектов из sql/current/evetis_mart/MANIFEST.json и их файлы;
-- вернуть evetis_mart в EXTERNAL_DATASET_POLICY без evetis_ops, если он больше не нужен.
-- ============================================================================
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`) = 0
  AS 'REF_SALES_PLAN_APPROVAL содержит решения владельца — удалять только отдельным решением';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_INVENTORY_TARGET`) = 0
  AS 'REF_SKU_INVENTORY_TARGET содержит цели владельца — удалять только отдельным решением';
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_INVENTORY_TARGET`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`;
