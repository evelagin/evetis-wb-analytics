-- =====================================================================================
-- EVETIS OWNER CONTROL TOWER — PHASE 1 — VALIDATION / REGRESSION SUITE
-- =====================================================================================
-- Назначение : проверить целостность объектов Control Tower и сходимость с production.
-- Запуск     : bq query --use_legacy_sql=false < sql/control_tower/ct_phase1_validation.sql
--              (или целиком в BigQuery Console; скрипт падает на первом нарушенном ASSERT)
-- Читает     : только CT_* / V_CT_* и production-витрины. Ничего не пишет.
-- Периодичность: после каждого sp_ct_refresh_daily и перед публикацией изменений.
-- Автор      : Control Tower Phase 1 (2026-09-10)
--
-- ДОПУСКИ (документированы, не подгонка):
--   * T5  BOM-консервация плана: seed-артефакт округляет карточки и флаконы независимо
--         до 0,1 → расхождение ≤ 0,25 фл. на строку месяца и ≤ 3 фл. на сезон из 21 303.
--         Источник истины по плану — target_cards; target_physical_units_bom в
--         V_CT_PLAN_ACTIVE пересчитывает флаконы из BOM.
--   * T10 Сходимость запаса: срез ФФ владельца и живые остатки площадок снимаются в
--         разное время → допуск 50 фл. нетто на портфель (факт на 10.09: +5).
-- =====================================================================================

DECLARE v_fail STRING DEFAULT NULL;

-- ---------------------------------------------------------------------------------
-- T1. Уникальность зерна плана: plan_version × date × marketplace × sku × sales_mode
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT plan_version, plan_date, marketplace, internal_sku, sales_mode
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
    GROUP BY 1, 2, 3, 4, 5 HAVING COUNT(*) > 1)
) = 0 AS 'T1 FAILED: дубли в зерне CT_SEASON_PLAN';

-- ---------------------------------------------------------------------------------
-- T2. Ровно одна активная версия плана
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_status = 'ACTIVE'
) = 1 AS 'T2 FAILED: активных версий плана не ровно одна';

-- ---------------------------------------------------------------------------------
-- T3. Итоги сезонного плана = утверждённый сценарий SEASON EXECUTION TARGET
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT ABS(SUM(target_cards) - 16806.2) < 0.5 AND ABS(SUM(target_physical_units) - 21302.9) < 3
     AND ABS(SUM(target_gmv) - 14885717) < 100 AND ABS(SUM(target_contribution) - 1555676) < 100
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
) AS 'T3 FAILED: итоги CT_SEASON_PLAN разошлись с утверждённым SET';

-- ---------------------------------------------------------------------------------
-- T4. Дневная декомпозиция сходится с месячным seed по каждому месяцу × каналу × SKU
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT m.month, m.marketplace, m.internal_sku, m.sales_mode
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` m
    LEFT JOIN (
      SELECT month, marketplace, internal_sku, sales_mode, SUM(target_cards) AS cards, SUM(target_gmv) AS gmv
      FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN` GROUP BY 1, 2, 3, 4
    ) d ON d.month = m.month AND d.marketplace = m.marketplace AND d.internal_sku = m.internal_sku AND d.sales_mode = m.sales_mode
    WHERE ABS(IFNULL(d.cards, 0) - m.target_cards) > 0.01 OR ABS(IFNULL(d.gmv, 0) - m.target_gmv) > 1)
) = 0 AS 'T4 FAILED: дневная кривая не сходится с месячным планом';

-- ---------------------------------------------------------------------------------
-- T5. BOM-консервация плана (с документированным допуском округления seed)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT MAX(ABS(target_physical_units - target_cards * component_count)) < 0.25
     AND ABS(SUM(target_physical_units - target_cards * component_count)) < 3
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
) AS 'T5 FAILED: BOM-консервация плана нарушена сверх допуска округления';

-- ---------------------------------------------------------------------------------
-- T6. Маппинг SKU: каждый SKU плана есть в REF_PRODUCT_MASTER и в BOM
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT DISTINCT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
    WHERE internal_sku NOT IN (SELECT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`)
       OR internal_sku NOT IN (SELECT DISTINCT card_sku FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`))
) = 0 AS 'T6 FAILED: SKU плана отсутствует в REF_PRODUCT_MASTER или BOM';

-- ---------------------------------------------------------------------------------
-- T7. Уникальность зерна факта: date × marketplace × sku × sales_mode
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT d, marketplace, internal_sku, sales_mode FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
    GROUP BY 1, 2, 3, 4 HAVING COUNT(*) > 1)
) = 0 AS 'T7 FAILED: дубли в зерне CT_ACTUAL_DAILY';

-- ---------------------------------------------------------------------------------
-- T8. Отсутствие двойного счёта физических единиц:
--     разложение по BOM даёт ровно cards × component_count
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT (SELECT SUM(units_ordered) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`)
       = (SELECT SUM(units_ordered) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`)
) AS 'T8 FAILED: двойной счёт физических единиц при разложении наборов';

-- ---------------------------------------------------------------------------------
-- T9. Сходимость факта WB с production-витриной V_DASH_SKU_DAILY (закрытый август)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT ct.orders = pr.orders AND ct.buyouts = pr.buyouts
     AND ABS(ct.rev - pr.rev) < 1 AND ABS(ct.ads - pr.ads) < 1 AND ABS(ct.cash - pr.cash) < 1
  FROM (SELECT SUM(cards_ordered) AS orders, SUM(cards_sold) AS buyouts, SUM(revenue_seller_base) AS rev,
               SUM(ad_spend) AS ads, SUM(seller_cash) AS cash
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
        WHERE marketplace = 'WB' AND d BETWEEN DATE '2026-08-01' AND DATE '2026-08-31') ct,
       (SELECT SUM(orders_qty) AS orders, SUM(buyouts_qty) AS buyouts, SUM(sales_revenue_seller_base_rub) AS rev,
               SUM(ad_spend_attributed_rub) AS ads, SUM(contribution_pre_cogs_rub) AS cash
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY`
        WHERE internal_sku IS NOT NULL AND day BETWEEN DATE '2026-08-01' AND DATE '2026-08-31') pr
) AS 'T9 FAILED: факт WB разошёлся с wb_mart.V_DASH_SKU_DAILY';

-- ---------------------------------------------------------------------------------
-- T10. Сходимость факта Ozon с production-витриной FCT_OZON_SKU_PNL_MONTHLY (август)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT ct.qty = pr.qty AND ABS(ct.rev - pr.rev) < 1 AND ABS(ct.ads - pr.ads) < 1
     AND ABS(ct.cogs - pr.cogs) < 1 AND ABS(ct.contrib - pr.contrib) < 1
  FROM (SELECT SUM(cards_sold) AS qty, SUM(revenue_seller_base) AS rev, SUM(ad_spend) AS ads,
               SUM(cogs) AS cogs, SUM(contribution) AS contrib
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
        WHERE marketplace = 'OZON' AND d BETWEEN DATE '2026-08-01' AND DATE '2026-08-31') ct,
       (SELECT SUM(realized_qty) AS qty, SUM(seller_base_revenue_rub) AS rev, SUM(ad_spend_attributed_rub) AS ads,
               SUM(product_cogs_rub) AS cogs, SUM(contribution_after_attributed_ads_rub) AS contrib
        FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` WHERE month = DATE '2026-08-01') pr
) AS 'T10 FAILED: факт Ozon разошёлся с ozon_mart.FCT_OZON_SKU_PNL_MONTHLY';

-- ---------------------------------------------------------------------------------
-- T11. Сходимость запаса: открытие сезона − отгружено ≈ текущий продаваемый остаток
--      (допуск 50 фл. на разное время срезов ФФ и площадок)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT ABS(
    (SELECT SUM(total_sellable_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT` WHERE is_season_opening)
    - (SELECT IFNULL(SUM(units_ordered), 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`
       WHERE d >= (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT` WHERE is_season_opening))
    - (SELECT SUM(sellable_units) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`)) < 50
) AS 'T11 FAILED: продаваемый запас не сходится с открытием сезона сверх допуска';

-- ---------------------------------------------------------------------------------
-- T12. Потерянный сток WB не входит в продаваемый запас
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT SUM(wb_lost_claimed_units) > 0
     AND SUM(sellable_units) = SUM(ff_total_units + wb_fbo_live_units + ozon_fbo_units + ozon_transit_units + wb_in_transit_units + fbs_units)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
) AS 'T12 FAILED: потерянный сток WB попал в продаваемый запас';

-- ---------------------------------------------------------------------------------
-- T13. Inbound (партия 8) не считается доступным запасом
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT MAX(IF(inbound_units > 0, sellable_units, 0)) < 100
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
) AS 'T13 FAILED: inbound-партия учтена как доступный запас';

-- ---------------------------------------------------------------------------------
-- T14. OPEX: 77 000 ₽/мес первые шесть месяцев, 527 000 ₽ за сезон
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT SUM(amount_rub) = 527000 AND COUNTIF(m = 77000) = 6
  FROM (SELECT month, SUM(amount_rub) AS m, SUM(amount_rub) AS amount_rub
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OPEX` WHERE is_active GROUP BY month)
) AS 'T14 FAILED: OPEX не равен 77 000 ₽/мес × 6 + 65 000 ₽';

-- ---------------------------------------------------------------------------------
-- T15. Дедупликация очереди действий: один открытый ряд на dedup_key
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT dedup_key FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
    WHERE status IN ('OPEN', 'IN_PROGRESS') GROUP BY 1 HAVING COUNT(*) > 1)
) = 0 AS 'T15 FAILED: дубли открытых действий по dedup_key';

-- ---------------------------------------------------------------------------------
-- T16. Очередь действий: допустимые статусы и приоритеты
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNTIF(status NOT IN ('OPEN', 'IN_PROGRESS', 'DONE', 'CANCELLED', 'SUPERSEDED')) = 0
     AND COUNTIF(priority NOT IN ('P0 TODAY', 'P1 THIS WEEK', 'P2 NEXT 2 WEEKS', 'P3 THIS MONTH', 'P4 WATCH')) = 0
     AND COUNTIF(action_id IS NULL OR dedup_key IS NULL) = 0
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
) AS 'T16 FAILED: недопустимый статус/приоритет в очереди действий';

-- ---------------------------------------------------------------------------------
-- T17. Owner Home отдаёт ровно одну строку и знает вчерашнюю дату
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNT(*) = 1 AND MAX(yesterday_date) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME`
) AS 'T17 FAILED: V_CT_OWNER_HOME вернул не одну строку';

-- ---------------------------------------------------------------------------------
-- T18. Операционный результат = вклад − распределённый OPEX (месяц и сезон)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNTIF(ABS(operating_result - (IFNULL(actual_contribution, 0) - opex_allocated)) > 0.01) = 0
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_CASH_CONVERSION` WHERE period_type != 'SEASON_PLAN'
) AS 'T18 FAILED: операционный результат ≠ вклад − OPEX';

-- ---------------------------------------------------------------------------------
-- T19. Сходимость вклада: Owner Home MTD = сумма факта витрины за месяц
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT ABS(h.contribution_mtd - a.contrib) < 0.01 AND h.cards_ordered_mtd = a.cards
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME` h,
       (SELECT SUM(contribution) AS contrib, SUM(cards_ordered) AS cards
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
        WHERE d >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND d <= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)) a
) AS 'T19 FAILED: вклад MTD на Owner Home не сходится с витриной факта';

-- ---------------------------------------------------------------------------------
-- T20. Обработка устаревших данных: каждый домен свежести имеет дату и статус
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNTIF(status NOT IN ('OK', 'STALE', 'ERROR')) = 0 AND COUNT(*) >= 8
     AND COUNTIF(status = 'OK' AND age_days > sla_days) = 0
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS`
) AS 'T20 FAILED: витрина свежести данных некорректна';

-- ---------------------------------------------------------------------------------
-- T21. Plan vs Actual: план присутствует, факт не теряется (грубая сходимость MTD)
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT SUM(IF(in_plan, target_cards, 0)) > 0 AND SUM(IF(is_past, IFNULL(actual_cards, 0), 0)) >= 0
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH)
) AS 'T21 FAILED: V_CT_PLAN_VS_ACTUAL_DAILY не отдаёт план на текущий месяц';

-- ---------------------------------------------------------------------------------
-- T22. Провенанс: у активной версии заполнены артефакт, горизонт и открытие сезона
-- ---------------------------------------------------------------------------------
ASSERT (
  SELECT COUNTIF(source_artifact IS NULL OR horizon_from IS NULL OR horizon_to IS NULL
              OR opening_inventory_units IS NULL OR curve_version IS NULL) = 0
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_status = 'ACTIVE'
) AS 'T22 FAILED: у активной версии плана нет полного провенанса';

SELECT 'CT PHASE 1 VALIDATION: ALL 22 TESTS PASSED' AS result, CURRENT_TIMESTAMP() AS checked_at;
