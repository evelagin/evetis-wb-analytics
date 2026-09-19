-- ОТКАТ sql/unitka/reconcile_v1.sql. Только DROP новых вью: существующие V_UNITKA_DAILY_FACT / V_UNITKA_INTEGRITY /
-- V_UNITKA_COGS_CANONICAL не менялись. Данных и таблиц откат не трогает (таблицы wb_ops — Terraform, deletion_protection).
-- Перед откатом перевести Engine в UNITKA_RECONCILE_MODE=off (иначе прогон упадёт на отсутствующей вью — fail-closed).
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_INTEGRITY_STATUS`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_COGS_CANONICAL`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_INTEGRITY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_FACT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_RECON_WINDOW`;
