-- ОТКАТ SCALE 1 (2026-09-20): удаление трёх НОВЫХ вью. Данных не содержит ни один из них —
-- удаление ничего не теряет, повторное развёртывание восстанавливает всё из sql/current.
-- Существующие объекты (месячные P&L Ozon, весь wb_mart, evetis_ref, RAW) этим PR не менялись
-- и откатом не затрагиваются. Порядок — от потребителя к источнику. На 2026-09-20 Metabase эти
-- объекты не читает; если к моменту отката появились потребители — сначала переключить их.
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_COMMISSION_RECOVERY`;
-- Датасет evetis_mart оставляется пустым намеренно: DROP SCHEMA здесь нет (правило R2D —
-- откат не удаляет датасеты). Пустой датасет безвреден и ничего не стоит.
