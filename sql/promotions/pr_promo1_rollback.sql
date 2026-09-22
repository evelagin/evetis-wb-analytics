-- ============================================================================
-- PR-PROMO-1 — ОТКАТ слоя наблюдения акций.
--
-- Удаляет ТОЛЬКО объекты, созданные sql/promotions/pr_promo1_raw.sql.
-- Ни один существующий production-объект здесь не упомянут: PR-PROMO-1 не
-- изменял ни одной существующей таблицы и ни одной вью.
--
-- ⚠️ ПЕРЕД ВЫПОЛНЕНИЕМ. Откат уничтожает накопленную историю наблюдений,
-- которую нельзя восстановить: у Ozon исторической выгрузки акций нет вовсе,
-- у WB есть каталог акций, но нет ни состава, ни цен прошлых наблюдений.
-- Если цель — остановить наблюдение, а не стереть историю, достаточно
-- поставить планировщики на паузу:
--   gcloud scheduler jobs pause wb-promo-prod  --location=europe-west1
--   gcloud scheduler jobs pause ozon-promo     --location=europe-west1
--
-- Порядок отката PR целиком:
--   1. пауза планировщиков (команды выше);
--   2. terraform destroy -target для job'ов и планировщиков PR-PROMO-1;
--   3. этот скрипт;
--   4. revert кода (cloud/src/loaders/promo, pipelines/ozon/runtime/promo.py).
-- ============================================================================

DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_OBSERVATIONS`;

DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_OBSERVATIONS`;
