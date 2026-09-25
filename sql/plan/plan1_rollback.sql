-- ============================================================================
-- PR-PLAN-1 · ОТКАТ. Запускать только по решению владельца. Контракт:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md §Откат
--
-- Шаг 1 (вне этого файла, ДО него): вернуть два представления PR-PROMO-4 к версии до PR-PLAN-1 —
-- они перестанут читать контракт версий:
--   git show 15b1867:sql/current/evetis_mart/V_SALES_PLAN_MONTHLY_CURRENT.sql
--   git show 15b1867:sql/current/evetis_mart/V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT.sql
-- и выполнить их CREATE OR REPLACE VIEW как есть. Проверка: ни одно представление вне списка
-- ниже не ссылается на V_PLAN_* / V_SALES_PLAN_APPROVED / V_INBOUND_LOT_CURRENT / V_PLANNING_*.
--
-- Шаг 2 (этот файл): удалить процедуры и представления PR-PLAN-1 в обратном порядке зависимостей.
--
-- ДАННЫЕ НЕ УДАЛЯЮТСЯ. Таблицы evetis_ref.PLAN_* и INBOUND_LOT_EVENT содержат решения и факты
-- владельца (версии, поступления) — они остаются как есть; пустую таблицу владелец может удалить
-- отдельной командой. Колонки content_sha256 и effective_from_month реестра утверждений остаются
-- (nullable, представления PR-PROMO-4 до PR-PLAN-1 их не читают).
-- Control Tower, C1 (evetis_ops), Юнитка и экономика откатом не затрагиваются — PR-PLAN-1 их не менял.
-- ============================================================================

DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_submit`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_approve`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_close`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_event_core`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_propose_observed_run_rate`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_register_legacy_ct`;
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.sp_inbound_record`;

DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_HEADER`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_SKU_OVERVIEW`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_EXCEPTIONS`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_APPROVED`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL`;
