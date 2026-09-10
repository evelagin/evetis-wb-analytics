-- =====================================================================================
-- CONTROL TOWER PHASE 1 — BOOTSTRAP (только при развёртывании с нуля)
-- =====================================================================================
-- ПОРЯДОК ПРИМЕНЕНИЯ ВСЕГО КОНТУРА:
--   1. ct_01_reference_tables.sql       — 12 таблиц CT_*
--   2. ct_04a_views_base.sql            — базовые витрины (BOM, план, факт, свежесть)
--                                         V_CT_INVENTORY_TRUTH тут упадёт до шага 4 —
--                                         это ожидаемо, применить файл повторно после шага 4
--   3. ct_02_seed_load.sql              — seed из утверждённых артефактов
--   4. ct_03_daily_curve_and_plan.sql   — кривая и дневной план
--   5. ct_06_bootstrap.sql (этот файл)  — создать CT_INVENTORY_SNAPSHOT_DAILY
--   6. ct_04a повторно + ct_04b_views_owner.sql
--   7. ct_05_procedures.sql
--   8. CALL evetis_ref.sp_ct_refresh_daily();
--   9. ct_phase1_validation.sql          — 22 теста, должны пройти все
-- =====================================================================================

-- Схема наследуется от LIVE-витрины, поэтому таблица создаётся через CTAS.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`
PARTITION BY snapshot_date
CLUSTER BY internal_sku
OPTIONS (description = 'Control Tower: daily materialised inventory truth per component SKU (FF + WB FBO + Ozon FBO + transit + FBS, lost WB stock separate, inbound separate). Written by evetis_ref.sp_ct_refresh_daily() from wb_mart.V_CT_INVENTORY_TRUTH_LIVE; one partition per day (replace on rerun). Additive CT object; rollback = DROP TABLE.')
AS
SELECT CURRENT_DATE() AS snapshot_date, CURRENT_TIMESTAMP() AS snapshot_ts, v.*
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH_LIVE` v;
