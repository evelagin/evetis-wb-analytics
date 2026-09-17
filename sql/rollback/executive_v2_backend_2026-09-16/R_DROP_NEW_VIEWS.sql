-- ОТКАТ Executive V2 backend (2026-09-16): удаление трёх НОВЫХ view.
-- Порядок: сначала R_V_DASH_SETTLEMENT_DAILY_BEFORE.sql и
-- R_V_DASH_EXECUTIVE_ECONOMICS_DAILY_BEFORE.sql (они читают V_WB_FINANCE_PRICE_COMPONENTS),
-- затем этот файл. Metabase эти объекты на 2026-09-16 не читает.
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`;
