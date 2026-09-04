-- =====================================================================
-- Stage 3.4C / 3.4C.3 — канонический P&L Ozon. Production mart.
-- Дата: 2026-09-04. ACK владельца получен (3.4C), закрытие 3.4C.3.
--
-- АРХИТЕКТУРА: все пять объектов — VIEW. Материализованных таблиц нет.
--
-- Почему VIEW, а не TABLE (решение Stage 3.4C.3, вариант A):
--   * полный пересчёт месячного P&L сканирует ~22 МБ и занимает ~3 с —
--     материализация не окупается;
--   * VIEW физически не может устареть относительно ozon_raw, поэтому
--     класс ошибок REFRESH_GAP исчезает целиком, а не «управляется»;
--   * не нужен ни второй планировщик, ни job, ни процедура, ни новые
--     права IAM — а значит нечему молча сломаться;
--   * поздние корректировки финансов (lookback 14 дней) и уточнения
--     рекламы (7 дней) распространяются в отчётность немедленно,
--     без пересчёта исторических месяцев;
--   * гонка «март считается по наполовину обновлённым raw» невозможна:
--     нет момента записи, есть момент чтения.
--
-- Префикс FCT_ здесь означает ЗЕРНО факта, а не способ хранения.
-- Зерно и формулы не изменились: мост L1-L4 заморожен в
-- docs/ozon/OZON_PNL_POLICY_V1.md. Одно имя — одна формула.
--
-- Инварианты:
--   * отсутствующая комиссия и отсутствующий COGS никогда не ноль;
--   * REVENUE_PROPORTIONAL_ALL не применяется;
--   * расходы уровня магазина на SKU не разносятся;
--   * ozon_raw, WB-объекты, runtime и планировщики не изменяются.
--
-- Откат: sql/ozon/stage3_4c_rollback.sql
-- Скрипт идемпотентен: CREATE OR REPLACE, повторный запуск безопасен.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0. Датасет
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart`
OPTIONS (location='EU',
  description='Домен Ozon, аналитический слой P&L. Stage 3.4C. Все объекты - VIEW, материализованных таблиц нет: март не может устареть относительно ozon_raw. Мост L1-L4 заморожен в docs/ozon/OZON_PNL_POLICY_V1.md: одно имя - одна формула. Не читает и не пишет датасеты WB. Отсутствующая комиссия и COGS никогда не подменяются нулём.');

-- Снос прежней материализации Stage 3.4C. Без этого CREATE VIEW под тем же
-- именем не выполнится: BigQuery не заменяет TABLE на VIEW.
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;


-- ---------------------------------------------------------------------
-- 1. FCT_OZON_PNL_MONTHLY — зерно: календарный месяц
--
-- rec_comm: 29 postings, комиссия по которым восстановлена из локальных
-- отчётов Ozon о реализации (9 637,60 ₽). Это факт из документа продавца,
-- а не оценка. Источник — docs/ozon/audit_2026-09-04/pnl/.
-- Оставшиеся 6 postings комиссии не имеют ни в одном источнике и
-- отражены через commission_missing_* и границы неопределённости.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`
OPTIONS(description="Канонический P&L Ozon, зерно = календарный месяц. VIEW, не таблица: пересчитывается при каждом чтении, устареть относительно ozon_raw не может. Мост L1-L4 по OZON_PNL_POLICY_V1. Продажи и себестоимость привязаны к order_date, расходы уровня магазина - к дате начисления. Отсутствующая комиссия НЕ ноль: см. commission_missing_qty и поля uncertainty.")
AS
WITH rec_comm AS (
  SELECT * FROM UNNEST([
    STRUCT('0107987069-0115-1' AS posting_number, NUMERIC '428.40' AS c),('0107987069-0116-1',NUMERIC '346.50'),
    ('0111865653-0053-1',NUMERIC '356.16'),('0140207342-0283-1',NUMERIC '446.46'),('0140207342-0294-1',NUMERIC '391.44'),
    ('0140425762-0336-1',NUMERIC '350.28'),('0143652501-0071-1',NUMERIC '428.40'),('0148296041-0008-1',NUMERIC '346.50'),
    ('0148983889-0086-1',NUMERIC '262.92'),('0174671740-0036-1',NUMERIC '206.79'),('0180844433-0032-1',NUMERIC '222.65'),
    ('0184479194-0054-1',NUMERIC '350.28'),('0198758548-0051-1',NUMERIC '382.20'),('0201540221-0097-1',NUMERIC '341.88'),
    ('0231520423-0001-2',NUMERIC '254.94'),('0233071107-0012-1',NUMERIC '276.36'),('52826697-0003-15',NUMERIC '254.94'),
    ('57794512-0006-2',NUMERIC '264.00'),('59699516-0310-4',NUMERIC '439.12'),('69799830-0276-1',NUMERIC '229.40'),
    ('71080184-0023-1',NUMERIC '262.92'),('81535887-0077-1',NUMERIC '350.28'),('90312383-0011-1',NUMERIC '385.56'),
    ('90831504-0160-1',NUMERIC '352.80'),('92234251-0008-1',NUMERIC '254.94'),('98041754-0004-1',NUMERIC '369.60'),
    ('98041754-0006-1',NUMERIC '344.40'),('99543424-0109-1',NUMERIC '350.28'),('99543424-0131-1',NUMERIC '387.20')])),
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(buyer_paid_price_rub) bp,
         SUM(ozon_bonus_rub) bonus, SUM(ozon_coinvestment_rub) coinv, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.status, p.quantity, p.posting_number,
    -- seller-base: finance где есть, иначе posting.price_rub (RECONSTRUCTED_PROVEN)
    IFNULL(f.sp_unit, p.price_rub) * p.quantity AS seller_base,
    IFNULL(f.bp,0) bp, IFNULL(f.bonus,0) bonus, IFNULL(f.coinv,0) coinv,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) AS commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) AS commission_missing_qty,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.price_rub*p.quantity, NUMERIC '0') AS commission_missing_revenue,
    c.u * p.quantity AS cogs_amt,
    IF(c.u IS NULL, p.quantity, 0) AS cogs_missing_qty
  FROM post p
  LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number = p.posting_number
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (
  SELECT m,
    SUM(quantity) gross_ordered_qty,
    SUM(IF(status='cancelled', quantity, 0)) cancelled_qty,
    SUM(IF(status IN ('delivering','awaiting_deliver','awaiting_packaging'), quantity, 0)) in_transit_qty,
    SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', bp, 0)),2) buyer_paid_revenue_rub,
    ROUND(SUM(IF(status='delivered', bonus, 0)),2) bonus_rub,
    ROUND(SUM(IF(status='delivered', coinv, 0)),2) coinvestment_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', quantity - cogs_missing_qty, 0)) cogs_covered_qty,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_known_rub,
    SUM(IF(status='delivered', quantity - commission_missing_qty, 0)) commission_coverage_qty,
    SUM(IF(status='delivered', commission_missing_qty, 0)) commission_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_missing_revenue, 0)),2) commission_missing_revenue_rub
  FROM sales GROUP BY m),
f AS (
  SELECT DATE_TRUNC(event_date, MONTH) m,
    ROUND(SUM(IF(type_id IN (32,29,28,98,30,1,59,45,78,9,79), -amount_rub, 0)),2) direct_variable_marketplace_costs_rub,
    ROUND(SUM(IF(type_id IN (12,46), -amount_rub, 0)),2) store_level_variable_costs_rub,
    ROUND(SUM(IF(type_id IN (77,76,15,71,39,38,57), -amount_rub, 0)),2) other_marketplace_costs_ex_ads_rub,
    ROUND(SUM(IF(type_id=52, -amount_rub, 0)),2) marketplace_fixed_costs_rub,
    ROUND(SUM(IF(type_id IN (25,10), amount_rub, 0)),2) compensations_rub,
    ROUND(SUM(IF(type_id IN (116,47,96,74,48), -amount_rub, 0)),2) ad_reviews_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` GROUP BY m),
a AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(expense_rub),2) ad_campaign_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` GROUP BY m),
asku AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(attributed_spend_rub),2) ad_sku_attr_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` GROUP BY m),
j AS (
  SELECT COALESCE(s.m, f.m, a.m) month,
    IFNULL(s.gross_ordered_qty,0) gross_ordered_qty, IFNULL(s.cancelled_qty,0) cancelled_qty,
    IFNULL(s.in_transit_qty,0) in_transit_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.buyer_paid_revenue_rub,0) buyer_paid_revenue_rub,
    IFNULL(s.bonus_rub,0) bonus_rub, IFNULL(s.coinvestment_rub,0) coinvestment_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub,
    IFNULL(s.cogs_covered_qty,0) cogs_covered_qty, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_known_rub,0) commission_known_rub,
    IFNULL(s.commission_coverage_qty,0) commission_coverage_qty,
    IFNULL(s.commission_missing_qty,0) commission_missing_qty,
    IFNULL(s.commission_missing_revenue_rub,0) commission_missing_revenue_rub,
    IFNULL(f.direct_variable_marketplace_costs_rub,0) direct_variable_marketplace_costs_rub,
    IFNULL(f.store_level_variable_costs_rub,0) store_level_variable_costs_rub,
    IFNULL(f.other_marketplace_costs_ex_ads_rub,0) other_marketplace_costs_ex_ads_rub,
    IFNULL(f.marketplace_fixed_costs_rub,0) marketplace_fixed_costs_rub,
    IFNULL(f.compensations_rub,0) compensations_rub,
    IFNULL(a.ad_campaign_rub,0) + IFNULL(f.ad_reviews_rub,0) advertising_rub,
    IFNULL(a.ad_campaign_rub,0) ad_campaign_rub,
    IFNULL(asku.ad_sku_attr_rub,0) ad_sku_attributed_rub
  FROM s FULL JOIN f USING (m) FULL JOIN a USING (m) FULL JOIN asku USING (m))
SELECT
  month, gross_ordered_qty, cancelled_qty, in_transit_qty, realized_qty,
  seller_base_revenue_rub, buyer_paid_revenue_rub, bonus_rub, coinvestment_rub,
  product_cogs_rub, cogs_covered_qty, cogs_missing_qty,
  ROUND(SAFE_DIVIDE(cogs_covered_qty, NULLIF(realized_qty,0))*100, 4) cogs_coverage_pct,
  commission_known_rub, commission_coverage_qty, commission_missing_qty, commission_missing_revenue_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.18', 2) commission_uncertainty_lower_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.52', 2) commission_uncertainty_upper_rub,
  direct_variable_marketplace_costs_rub, store_level_variable_costs_rub, other_marketplace_costs_ex_ads_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub, 2) l1_gross_seller_contribution_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, 2) l2_contribution_before_ads_rub,
  advertising_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub, 2) l3_after_ads_before_fixed_rub,
  marketplace_fixed_costs_rub, compensations_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub, 2) l4_marketplace_contribution_profit_known_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.52', 2) l4_profit_uncertainty_lower_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.18', 2) l4_profit_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(advertising_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - marketplace_fixed_costs_rub + compensations_rub,
        NULLIF(seller_base_revenue_rub,0))*100, 4) full_marketplace_break_even_drr_pct,
  NUMERIC '100' finance_classification_coverage_pct,
  NUMERIC '100' ad_total_coverage_pct,
  ROUND(SAFE_DIVIDE(ad_sku_attributed_rub, NULLIF(ad_campaign_rub,0))*100, 4) ad_sku_attribution_coverage_pct,
  CASE WHEN commission_missing_qty > 0 OR cogs_missing_qty > 0 THEN 'KNOWN_GAP' ELSE 'COMPLETE' END data_confidence,
  'PASS_WITH_KNOWN_COMMISSION_GAP' data_status,
  -- VIEW: момент вычисления, а не момент материализации. Устаревания нет.
  CURRENT_TIMESTAMP() computed_at
FROM j WHERE month IS NOT NULL;


-- ---------------------------------------------------------------------
-- 2. FCT_OZON_SKU_PNL_MONTHLY — зерно: месяц x internal_sku
--
-- Иерархия атрибуции прямых расходов:
--   DIRECT_POSTING  — finance-строка несёт posting_number: месяц берётся
--                     от order_date продажи, чтобы расход лёг в тот же
--                     период, что и выручка;
--   DIRECT_SKU      — finance-строка несёт только sku (например тип 78
--                     «Краткосрочное размещение возврата FBS»): месяц
--                     берётся от даты начисления.
-- Оба CTE используют ОДИН и тот же список типов. Расхождение списков
-- роняет 24,00 ₽ типа 78 между ветками — эта ошибка была допущена
-- и исправлена; не восстанавливать разные списки.
--
-- REVENUE_PROPORTIONAL_ALL не применяется.
-- STORE_LEVEL_VARIABLE_COSTS и MARKETPLACE_FIXED_COSTS на SKU не
-- разносятся, поэтому на этом зерне слоя L4 нет — только вклад до и
-- после рекламы.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
OPTIONS(description="P&L Ozon, зерно = месяц x internal_sku. VIEW, не таблица. Атрибуция: DIRECT_POSTING где finance-строка несёт posting_number (привязка к order_date продажи), DIRECT_SKU где несёт только sku (привязка к дате начисления). REVENUE_PROPORTIONAL_ALL не используется. Расходы уровня магазина на SKU НЕ разносятся, поэтому слоя L4 здесь нет.")
AS
WITH rec_comm AS (
  SELECT * FROM UNNEST([
    STRUCT('0107987069-0115-1' AS posting_number, NUMERIC '428.40' AS c),('0107987069-0116-1',NUMERIC '346.50'),
    ('0111865653-0053-1',NUMERIC '356.16'),('0140207342-0283-1',NUMERIC '446.46'),('0140207342-0294-1',NUMERIC '391.44'),
    ('0140425762-0336-1',NUMERIC '350.28'),('0143652501-0071-1',NUMERIC '428.40'),('0148296041-0008-1',NUMERIC '346.50'),
    ('0148983889-0086-1',NUMERIC '262.92'),('0174671740-0036-1',NUMERIC '206.79'),('0180844433-0032-1',NUMERIC '222.65'),
    ('0184479194-0054-1',NUMERIC '350.28'),('0198758548-0051-1',NUMERIC '382.20'),('0201540221-0097-1',NUMERIC '341.88'),
    ('0231520423-0001-2',NUMERIC '254.94'),('0233071107-0012-1',NUMERIC '276.36'),('52826697-0003-15',NUMERIC '254.94'),
    ('57794512-0006-2',NUMERIC '264.00'),('59699516-0310-4',NUMERIC '439.12'),('69799830-0276-1',NUMERIC '229.40'),
    ('71080184-0023-1',NUMERIC '262.92'),('81535887-0077-1',NUMERIC '350.28'),('90312383-0011-1',NUMERIC '385.56'),
    ('90831504-0160-1',NUMERIC '352.80'),('92234251-0008-1',NUMERIC '254.94'),('98041754-0004-1',NUMERIC '369.60'),
    ('98041754-0006-1',NUMERIC '344.40'),('99543424-0109-1',NUMERIC '350.28'),('99543424-0131-1',NUMERIC '387.20')])),
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
pmap AS (SELECT DISTINCT posting_number, sku, order_date FROM post),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.internal_sku, p.status, p.quantity,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity seller_base,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) comm_missing_qty,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number=p.posting_number
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (SELECT m, internal_sku,
    SUM(quantity) gross_qty, SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_rub,
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty
  FROM sales GROUP BY 1,2),
dcost_post AS (
  SELECT DATE_TRUNC(pm.order_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL GROUP BY 1,2),
dcost_sku AS (
  SELECT DATE_TRUNC(f.event_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL GROUP BY 1,2),
ads AS (SELECT DATE_TRUNC(a.date, MONTH) m, mp.internal_sku, ROUND(SUM(a.attributed_spend_rub),2) ad_attr
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=a.sku GROUP BY 1,2),
j AS (SELECT COALESCE(s.m, dp.m, ds.m, ads.m) month,
    COALESCE(s.internal_sku, dp.internal_sku, ds.internal_sku, ads.internal_sku) internal_sku,
    IFNULL(s.gross_qty,0) gross_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_rub,0) commission_rub, IFNULL(s.comm_missing_qty,0) comm_missing_qty,
    IFNULL(dp.direct_var,0) + IFNULL(ds.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dp.other_direct,0) + IFNULL(ds.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dcost_post dp USING (m, internal_sku)
         FULL JOIN dcost_sku ds USING (m, internal_sku)
         FULL JOIN ads USING (m, internal_sku))
SELECT j.month, j.internal_sku, pm.canonical_product_name product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') product_type,
  j.gross_qty, j.realized_qty, j.seller_base_revenue_rub, j.product_cogs_rub,
  CASE WHEN j.cogs_missing_qty > 0 THEN 'MISSING_BOM_INTERVAL' ELSE 'COVERED' END cogs_status,
  j.commission_rub,
  CASE WHEN j.comm_missing_qty > 0 THEN 'MISSING_SOURCE' ELSE 'COVERED' END commission_status,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub, 2) contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, 2) contribution_after_attributed_ads_rub,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_before_ads_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_after_ads_pct,
  ROUND(SAFE_DIVIDE(j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  CASE
    WHEN j.cogs_missing_qty > 0 THEN 'COGS_INCOMPLETE'
    WHEN j.realized_qty = 0 THEN 'INSUFFICIENT_DATA'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub <= 0 THEN 'LOSS_BEFORE_ADS'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
         - j.ad_spend_attributed_rub > 0 THEN 'PROFITABLE_AFTER_ADS'
    ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END profitability_status,
  'DIRECT_POSTING+DIRECT_SKU+ACTUAL_AD_ATTRIBUTION' attribution_level,
  CURRENT_TIMESTAMP() computed_at
FROM j LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = j.internal_sku
WHERE j.month IS NOT NULL AND j.internal_sku IS NOT NULL;


-- ---------------------------------------------------------------------
-- 3. V_OZON_MART_FRESHNESS — контракт свежести
--
-- Марты — представления, поэтому относительно `ozon_raw` они устареть
-- не могут: mart_computed_at всегда равен моменту чтения. Единственный
-- реальный риск — устаревание самого `ozon_raw` относительно Ozon.
-- Именно его и измеряет этот объект.
--
-- Допуски выведены из расписаний OZON_INCREMENTAL_CONTRACT_V1:
--   fbo_postings   ozon-fast,  3 раза в сутки → 12 ч
--   finance_accrual/ads_*  ozon-daily, 1 раз в сутки → 30 ч
--
-- Состояния:
--   REFRESH_FAILED         последняя попытка сущности завершилась не OK
--   STALE                  ни одна из сущностей P&L не свежее допуска
--   PARTIAL_SOURCE_UPDATE  часть сущностей свежая, часть просрочена
--   FRESH                  все сущности P&L в пределах допуска
--
-- ⚠️ Ограничение, которое нельзя забывать. Если планировщик вообще не
-- смог запустить Cloud Run job (например PERMISSION_DENIED), строки
-- в OZON_INGESTION_RUNS не появится вовсе — статуса FAILED не будет.
-- Такой отказ ловится ТОЛЬКО через возраст последнего успеха, поэтому
-- STALE здесь считается по времени, а не по наличию ошибки.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`
OPTIONS(description="Контракт свежести домена Ozon. Марты - VIEW и относительно ozon_raw устареть не могут; здесь измеряется свежесть самого ozon_raw относительно Ozon. Состояния FRESH / PARTIAL_SOURCE_UPDATE / STALE / REFRESH_FAILED. Агент обязан отказываться от решений при STALE и REFRESH_FAILED.")
AS
WITH tol AS (
  SELECT * FROM UNNEST([
    STRUCT('fbo_postings' AS entity, 12 AS tolerance_hours),
    ('finance_accrual', 30), ('ads_expense_daily', 30), ('ads_sku_daily', 30)])),
runs AS (
  SELECT entity,
    MAX(IF(status='OK', completed_at, NULL)) last_ok_at,
    MAX(completed_at) last_attempt_at,
    ANY_VALUE(status HAVING MAX completed_at) last_attempt_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
  WHERE marketplace='OZON' GROUP BY entity),
e AS (
  SELECT t.entity, t.tolerance_hours, r.last_ok_at, r.last_attempt_status,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.last_ok_at, HOUR) age_hours,
    CASE
      WHEN r.last_ok_at IS NULL THEN 'NEVER_INGESTED'
      WHEN r.last_attempt_status <> 'OK' AND r.last_attempt_at > r.last_ok_at THEN 'FAILED'
      WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.last_ok_at, HOUR) > t.tolerance_hours THEN 'STALE'
      ELSE 'FRESH' END entity_state
  FROM tol t LEFT JOIN runs r USING (entity))
SELECT
  CURRENT_TIMESTAMP() mart_computed_at,
  (SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`) source_max_posting_date,
  (SELECT MAX(event_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`) source_max_finance_date,
  (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`) source_max_ads_date,
  (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`) source_max_ads_sku_date,
  (SELECT MAX(last_ok_at) FROM e) last_successful_ingestion_at,
  (SELECT MAX(age_hours) FROM e) worst_source_age_hours,
  (SELECT STRING_AGG(CONCAT(entity,'=',entity_state,'(',CAST(IFNULL(age_hours,-1) AS STRING),'ч)'), ' | ' ORDER BY entity) FROM e) entity_states,
  CASE
    WHEN (SELECT COUNTIF(entity_state IN ('FAILED','NEVER_INGESTED')) FROM e) > 0 THEN 'REFRESH_FAILED'
    WHEN (SELECT COUNTIF(entity_state='STALE') FROM e) = (SELECT COUNT(*) FROM e) THEN 'STALE'
    WHEN (SELECT COUNTIF(entity_state='STALE') FROM e) > 0 THEN 'PARTIAL_SOURCE_UPDATE'
    ELSE 'FRESH' END freshness_status,
  CASE
    WHEN (SELECT COUNTIF(entity_state IN ('FAILED','NEVER_INGESTED','STALE')) FROM e) > 0
      THEN 'DECISIONS_BLOCKED_SOURCE_NOT_FRESH'
    ELSE 'DECISIONS_ALLOWED' END agent_decision_gate;


-- ---------------------------------------------------------------------
-- 4. V_OZON_LIFETIME_PNL — агрегат за всю историю
--
-- Три ДРР называются полностью и никогда сокращённо. «Безубыточный ДРР»
-- без указания какой из двух — запрещённая формулировка (OZON_PNL_POLICY_V1 §3).
-- data_status фиксирует известный разрыв по комиссии; называть P&L
-- полностью точным без этого флага нельзя.
-- freshness_status приходит из V_OZON_MART_FRESHNESS и относится
-- к источнику, а не к марту.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_LIFETIME_PNL`
OPTIONS(description="Агрегат FCT_OZON_PNL_MONTHLY за всю историю. Мост L1-L4 по OZON_PNL_POLICY_V1. Статус PASS_WITH_KNOWN_COMMISSION_GAP: комиссия по 6 postings неизвестна, диапазон истинного вклада показан явно. Отсутствующая комиссия никогда не ноль. Поля freshness_* относятся к свежести ozon_raw, не к марту.")
AS SELECT
  CAST(MIN(month) AS STRING) period_from, CAST(MAX(month) AS STRING) period_to, COUNT(*) months,
  SUM(gross_ordered_qty) gross_ordered_qty, SUM(cancelled_qty) cancelled_qty,
  SUM(in_transit_qty) in_transit_qty, SUM(realized_qty) realized_qty,
  SUM(seller_base_revenue_rub) seller_base_revenue_rub,
  SUM(buyer_paid_revenue_rub) buyer_paid_revenue_rub,
  SUM(bonus_rub) bonus_rub, SUM(coinvestment_rub) coinvestment_rub,
  SUM(product_cogs_rub) product_cogs_rub,
  SUM(commission_known_rub) commission_known_rub,
  SUM(direct_variable_marketplace_costs_rub) direct_variable_marketplace_costs_rub,
  SUM(l1_gross_seller_contribution_rub) l1_gross_seller_contribution_rub,
  SUM(store_level_variable_costs_rub) store_level_variable_costs_rub,
  SUM(other_marketplace_costs_ex_ads_rub) other_marketplace_costs_ex_ads_rub,
  SUM(l2_contribution_before_ads_rub) l2_contribution_before_ads_rub,
  SUM(advertising_rub) advertising_rub,
  SUM(l3_after_ads_before_fixed_rub) l3_after_ads_before_fixed_rub,
  SUM(marketplace_fixed_costs_rub) marketplace_fixed_costs_rub,
  SUM(compensations_rub) compensations_rub,
  SUM(l4_marketplace_contribution_profit_known_rub) canonical_known_contribution_rub,
  SUM(l4_profit_uncertainty_lower_rub) contribution_uncertainty_lower_rub,
  SUM(l4_profit_uncertainty_upper_rub) contribution_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(SUM(l4_marketplace_contribution_profit_known_rub), SUM(seller_base_revenue_rub))*100, 4) canonical_known_margin_pct,
  ROUND(SAFE_DIVIDE(SUM(advertising_rub), SUM(seller_base_revenue_rub))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(l2_contribution_before_ads_rub), SUM(seller_base_revenue_rub))*100, 4) variable_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(l2_contribution_before_ads_rub) - SUM(marketplace_fixed_costs_rub) + SUM(compensations_rub),
        SUM(seller_base_revenue_rub))*100, 4) full_marketplace_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(SUM(realized_qty), NULLIF(SUM(realized_qty),0))*100, 2) seller_base_coverage_pct,
  SUM(cogs_covered_qty) cogs_covered_qty, SUM(cogs_missing_qty) cogs_missing_qty,
  ROUND(SAFE_DIVIDE(SUM(cogs_covered_qty), SUM(realized_qty))*100, 4) cogs_coverage_pct,
  SUM(commission_coverage_qty) commission_coverage_qty, SUM(commission_missing_qty) commission_missing_qty,
  SUM(commission_missing_revenue_rub) commission_missing_revenue_rub,
  SUM(commission_uncertainty_lower_rub) commission_uncertainty_lower_rub,
  SUM(commission_uncertainty_upper_rub) commission_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(SUM(commission_coverage_qty), SUM(realized_qty))*100, 4) commission_coverage_pct,
  NUMERIC '100' finance_taxonomy_coverage_pct,
  NUMERIC '100' ad_total_coverage_pct,
  ROUND(SAFE_DIVIDE((SELECT SUM(ad_spend_attributed_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`),
        (SELECT SUM(expense_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`))*100, 4) ad_sku_attribution_coverage_pct,
  'PASS_WITH_KNOWN_COMMISSION_GAP' data_status,
  CURRENT_TIMESTAMP() mart_computed_at,
  (SELECT source_max_posting_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_posting_date,
  (SELECT source_max_finance_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_finance_date,
  (SELECT source_max_ads_date FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) source_max_ads_date,
  (SELECT freshness_status FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) freshness_status,
  (SELECT agent_decision_gate FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_MART_FRESHNESS`) agent_decision_gate
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`;


-- ---------------------------------------------------------------------
-- 5. V_OZON_SKU_UNIT_ECONOMICS_CURRENT — юнит-экономика, окно 180 дней
--
-- ⚠️ ЭТО НАБЛЮДЁННАЯ ЗАДНЯЯ ЭКОНОМИКА, А НЕ ПРОГНОЗНАЯ.
--
-- economics_mode = TRAILING_OBSERVED.
-- Все средние — фактические за окно 180 дней. Ни одно поле не является
-- действующим тарифом Ozon. Действующая комиссия документально
-- не подтверждена (в частности, дата вступления ставки 52 % —
-- NOT_PROVEN), и выводить её из средних за 180 дней запрещено.
--
-- forward_pricing_ready = FALSE. Прогнозное ценообразование закрывается
-- отдельно в Stage 3.4D; до этого поля break-even и target price —
-- индикативные на исторической базе, а не цены к установке.
--
-- Имена всех расчётных полей начинаются с trailing_, чтобы читатель
-- (в том числе агент) не мог принять их за действующие тарифы.
-- Имя самого объекта содержит CURRENT по историческим причинам
-- (ACK владельца Stage 3.4C); переименование — решение Stage 3.4D.
--
-- full_marketplace_break_even_drr_pct на уровне SKU намеренно NULL:
-- постоянные расходы магазина на SKU не разносятся.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_UNIT_ECONOMICS_CURRENT`
OPTIONS(description="TRAILING_OBSERVED юнит-экономика SKU Ozon за окно 180 дней. НЕ прогнозная: forward_pricing_ready = FALSE, действующие тарифы Ozon документально не подтверждены. ТОЛЬКО АНАЛИТИКА, цены и ставки не изменяются. Все расчётные поля с префиксом trailing_. full_marketplace_break_even_drr на уровне SKU = NULL: постоянные расходы магазина не разносятся. Прогнозное ценообразование - Stage 3.4D.")
AS
WITH win AS (SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 180 DAY) d0, CURRENT_DATE() d1),
agg AS (
  SELECT f.internal_sku, ANY_VALUE(f.product_name) product_name, ANY_VALUE(f.product_type) product_type,
    SUM(f.realized_qty) realized_qty, SUM(f.seller_base_revenue_rub) rev,
    SUM(f.product_cogs_rub) cogs, SUM(f.commission_rub) comm,
    SUM(f.direct_variable_marketplace_costs_rub + f.other_direct_marketplace_costs_rub) var_cost,
    SUM(f.ad_spend_attributed_rub) ads,
    MIN(f.month) first_month, MAX(f.month) last_month,
    MAX(IF(f.cogs_status='MISSING_BOM_INTERVAL', 1, 0)) cogs_gap,
    MAX(IF(f.commission_status='MISSING_SOURCE', 1, 0)) comm_gap
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` f, win
  WHERE f.month >= DATE_TRUNC(win.d0, MONTH) GROUP BY f.internal_sku
  HAVING SUM(f.realized_qty) > 0)
SELECT a.internal_sku, a.product_name, a.product_type, a.realized_qty,

  -- --- семантика окна ------------------------------------------------
  'TRAILING_OBSERVED' economics_mode,
  180 economics_window_days,
  (SELECT DATE_TRUNC(d0, MONTH) FROM win) window_start,
  (SELECT d1 FROM win) window_end,
  a.first_month first_observed_month,
  a.last_month last_observed_month,

  -- --- база каждой группы величин -------------------------------------
  'TRAILING_OBSERVED_REALIZED_AVG' price_basis,
  'TRAILING_OBSERVED_EFFECTIVE_RATE' commission_basis,
  'TRAILING_OBSERVED_DIRECT_ACTUAL' marketplace_cost_basis,
  'TRAILING_OBSERVED_ATTRIBUTED_ACTUAL' advertising_basis,
  'PROVEN_DOCUMENT_EFFECTIVE_DATE' cogs_basis,
  FALSE forward_pricing_ready,
  'Действующая комиссия Ozon документально не подтверждена (дата ставки 52% NOT_PROVEN); действующие тарифы логистики и последней мили не подтверждены. Выводить их из средних за 180 дней запрещено. Stage 3.4D.' forward_pricing_blockers,

  -- --- наблюдённые средние за окно ------------------------------------
  ROUND(SAFE_DIVIDE(a.rev, a.realized_qty), 2) trailing_avg_seller_base_price_rub,
  ROUND(SAFE_DIVIDE(a.cogs, a.realized_qty), 2) trailing_avg_management_cogs_unit_rub,
  ROUND(SAFE_DIVIDE(a.comm, a.realized_qty), 2) trailing_avg_commission_unit_rub,
  ROUND(SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))*100, 2) trailing_effective_commission_pct,
  ROUND(SAFE_DIVIDE(a.var_cost, a.realized_qty), 2) trailing_avg_variable_mp_cost_unit_rub,
  ROUND(SAFE_DIVIDE(a.ads, a.realized_qty), 2) trailing_ad_cost_per_realized_unit_rub,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_l1_unit_contribution_rub,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_l2_unit_available_for_ads_rub,
  ROUND(SAFE_DIVIDE(a.ads, NULLIF(a.rev,0))*100, 4) trailing_actual_drr_pct,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, NULLIF(a.rev,0))*100, 4) trailing_variable_break_even_drr_pct,
  CAST(NULL AS NUMERIC) full_marketplace_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(a.rev - a.cogs - a.comm - a.var_cost, a.realized_qty), 2) trailing_max_ad_spend_per_unit_rub,

  -- --- индикативные цены НА ИСТОРИЧЕСКОЙ БАЗЕ --------------------------
  -- Это не рекомендованные цены: ставка комиссии в знаменателе —
  -- наблюдённая за окно, а не действующая. Ставить по ним цену нельзя.
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / (1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))), 2) trailing_basis_break_even_price_rub,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.10'), 2) trailing_basis_indicative_price_margin_10pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.15'), 2) trailing_basis_indicative_price_margin_15pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.20'), 2) trailing_basis_indicative_price_margin_20pct,
  ROUND(SAFE_DIVIDE(a.cogs + a.var_cost, a.realized_qty) / ((1 - SAFE_DIVIDE(a.comm, NULLIF(a.rev,0))) - NUMERIC '0.25'), 2) trailing_basis_indicative_price_margin_25pct,

  CASE WHEN a.cogs_gap = 1 THEN 'COGS_INCOMPLETE'
       WHEN a.rev - a.cogs - a.comm - a.var_cost <= 0 THEN 'LOSS_BEFORE_ADS'
       WHEN a.rev - a.cogs - a.comm - a.var_cost - a.ads > 0 THEN 'PROFITABLE_AFTER_ADS'
       ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END trailing_profitability_status,
  CASE WHEN a.cogs_gap = 1 OR a.comm_gap = 1 THEN 'KNOWN_GAP' ELSE 'COMPLETE' END data_confidence,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' usage_note,
  CURRENT_TIMESTAMP() mart_computed_at
FROM agg a ORDER BY a.realized_qty DESC;


-- =====================================================================
-- СНИМОК для сверки арифметики — 2026-09-04 15:05 МСК,
-- после восстановления загрузки (инцидент P0).
--
-- ⚠️ Это НЕ контрольные константы. Объекты выше — представления, они
-- пересчитываются при каждом чтении, и числа меняются с каждой новой
-- загрузкой. Заморожены ФОРМУЛЫ моста L1-L4, а не значения.
-- Снимок нужен, чтобы проверить тождества L1→L4, а не чтобы к нему
-- подгонять. Расхождение = пришли данные, а не ошибка.
--
--   SELLER_BASE_REVENUE            1 780 253,00
--   PRODUCT_COGS                     440 574,72
--   COMMISSION_KNOWN                 648 870,48
--   DIRECT_VARIABLE                  173 861,62
--   L1                               516 946,18
--   STORE_LEVEL_VARIABLE              16 689,67
--   OTHER_MARKETPLACE_EX_ADS          11 629,76
--   L2                               488 626,75
--   ADVERTISING                      495 393,55
--   L3                                -6 766,80
--   MARKETPLACE_FIXED                109 880,00
--   COMPENSATIONS                     +3 896,23
--   L4 (CANONICAL_KNOWN)            -112 750,57
--   realized_qty 1962 / gross 2263 / месяцев 18
--   cogs_missing_qty 1, commission_missing_qty 6 (7 417,00 ₽ выручки)
--
-- Предыдущий снимок (12:00 МСК, до догрузки) и разбор каждой дельты —
-- docs/ozon/STAGE_3_4C_LIFETIME_PNL_REPORT.md §20.
-- =====================================================================
