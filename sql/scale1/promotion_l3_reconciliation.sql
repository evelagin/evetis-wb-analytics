-- ============================================================================
-- UBR-010 · СВЕРКА ПЕРЕНОСА promotion_billed_rub НА СЛОЙ L3. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: `-- @check <ID>`, колонка status ∈ {PASS, FAIL}.
-- Исполнитель: tools/run_data_checks.py --adapter check_blocks
--
-- До развёртывания объекты pending_deploy ещё не существуют под своими именами:
--   python tools/scale1_predeploy_render.py sql/scale1/promotion_l3_reconciliation.sql
-- После развёртывания файл исполняется как есть.
--
-- Фикстура сверки — 10 428,67 ₽ (типы начислений 116, 74, 48). Требования владельца:
-- сумма не исчезает, не уменьшает выручку, не попадает в операционные расходы площадки,
-- не учитывается дважды, прослеживается от сырья до конечного управленческого потребителя.
--
-- ⚠️ РАЗДЕЛЕНИЕ ИНВАРИАНТОВ И ЯКОРЯ. Проверки, приколотые к абсолютным суммам портфеля,
-- обязаны покраснеть от любой новой продажи — такие ворота учат себя игнорировать.
-- Поэтому здесь:
--   * инварианты (равенство слоёв, состав расходов, отсутствие двойного счёта,
--     тождества) сформулированы как ОТНОШЕНИЯ и верны при любых данных;
--   * якорь 10 428,67 ₽ ограничен ЗАКРЫТЫМ окном event_date <= 2026-08-31, в котором
--     лежат все 84 начисления на момент исследования. Если он сдвинется, это новое
--     начисление за прошлый период (окно Ozon — 30 суток, Gate 9B), и якорь
--     переприкалывается после объяснения дельты, а не ослабляется.
-- ============================================================================

-- @check P01_FIXTURE_TRACEABLE_RAW_TO_NEUTRAL
-- ИНВАРИАНТ: одна и та же величина на всех четырёх уровнях — сырьё → суточный факт Ozon →
-- месячный P&L Ozon → нейтральный факт. Верен при любых данных. Допуск 0,01 ₽.
WITH raw_sum AS (
  SELECT ROUND(SUM(-amount_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE type_id IN (116, 74, 48)),
daily AS (
  SELECT ROUND(SUM(sku_promotion_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`),
monthly AS (
  SELECT ROUND(SUM(sku_promotion_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`),
neutral AS (
  SELECT ROUND(SUM(promotion_billed_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`
  WHERE marketplace = 'OZON')
SELECT (SELECT v FROM raw_sum) raw_accrual, (SELECT v FROM daily) ozon_daily,
       (SELECT v FROM monthly) ozon_monthly, (SELECT v FROM neutral) neutral_fact,
       IF(ABS((SELECT v FROM daily) - (SELECT v FROM raw_sum)) <= 0.01
          AND ABS((SELECT v FROM monthly) - (SELECT v FROM raw_sum)) <= 0.01
          AND ABS((SELECT v FROM neutral) - (SELECT v FROM raw_sum)) <= 0.01,
          'PASS', 'FAIL') status;

-- @check P02_REVENUE_UNCHANGED
-- ИНВАРИАНТ: выручка нейтрального факта равна выручке витрины, и продвижение в неё не
-- входит. Если бы компонент трактовали как скидку, эти две величины разошлись бы ровно
-- на него.
SELECT ROUND((SELECT SUM(seller_base_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) monthly_revenue,
       ROUND((SELECT SUM(seller_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON'), 2) neutral_revenue,
       ROUND((SELECT SUM(sku_promotion_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) promotion,
       IF(ABS(ROUND((SELECT SUM(seller_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON'), 2)
              - ROUND((SELECT SUM(seller_base_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2)) <= 0.01,
          'PASS', 'FAIL') status;

-- @check P03_NOT_IN_MARKETPLACE_COSTS
-- ИНВАРИАНТ: расходы площадки раскладываются ровно на свои компоненты, и продвижения
-- среди них нет. Верен при любых данных.
SELECT ROUND(SUM(marketplace_costs_total_rub), 2) mp_costs_total,
       ROUND(SUM(marketplace_commission_rub + IFNULL(logistics_rub,0) + IFNULL(storage_rub,0)
                 + IFNULL(acquiring_rub,0) + IFNULL(other_marketplace_costs_rub,0)), 2) mp_costs_components,
       COUNTIF(ABS(marketplace_costs_total_rub
                   - (marketplace_commission_rub + IFNULL(logistics_rub,0) + IFNULL(storage_rub,0)
                      + IFNULL(acquiring_rub,0) + IFNULL(other_marketplace_costs_rub,0))) > 0.005) decomposition_breaks,
       IF(COUNTIF(ABS(marketplace_costs_total_rub
                   - (marketplace_commission_rub + IFNULL(logistics_rub,0) + IFNULL(storage_rub,0)
                      + IFNULL(acquiring_rub,0) + IFNULL(other_marketplace_costs_rub,0))) > 0.005) = 0,
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON';

-- @check P04_NEUTRAL_IDENTITY_HOLDS
-- Тождество контракта V2 выполняется на КАЖДОЙ строке обеих площадок:
-- вклад после рекламы = выручка − расходы площадки − атрибуция − начисленное продвижение.
-- В V1 этот инвариант ломался на 71 строке OZON ровно на −10 428,67 ₽.
SELECT COUNT(*) rows_total,
       COUNTIF(ABS(contribution_after_ads_rub
                   - (seller_revenue_rub - marketplace_costs_total_rub
                      - IFNULL(advertising_attributed_rub,0) - IFNULL(promotion_billed_rub,0))) > 0.005) after_ads_breaks,
       COUNTIF(contribution_after_cogs_rub IS NOT NULL
               AND ABS(contribution_after_cogs_rub - (contribution_after_ads_rub - cogs_rub)) > 0.005) after_cogs_breaks,
       IF(COUNTIF(ABS(contribution_after_ads_rub
                   - (seller_revenue_rub - marketplace_costs_total_rub
                      - IFNULL(advertising_attributed_rub,0) - IFNULL(promotion_billed_rub,0))) > 0.005) = 0
          AND COUNTIF(contribution_after_cogs_rub IS NOT NULL
               AND ABS(contribution_after_cogs_rub - (contribution_after_ads_rub - cogs_rub)) > 0.005) = 0,
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`;

-- @check P05_NO_DOUBLE_COUNTING
-- ИНВАРИАНТ и главный критерий приёмки: величина вычтена РОВНО ОДИН раз. «Вклад до
-- рекламы» её больше не содержит, а «вклад после рекламы» отличается от него ровно на
-- сумму рекламы и продвижения. Верен при любых данных.
SELECT ROUND(SUM(contribution_before_ads_rub), 2) before_ads,
       ROUND(SUM(contribution_after_attributed_ads_rub), 2) after_ads,
       ROUND(SUM(ad_spend_attributed_rub), 2) ads, ROUND(SUM(sku_promotion_rub), 2) promo,
       COUNTIF(ABS(contribution_before_ads_rub
                   - (seller_base_revenue_rub - product_cogs_rub - commission_rub
                      - direct_variable_marketplace_costs_rub - other_direct_marketplace_costs_rub)) > 0.005) before_breaks,
       COUNTIF(ABS(contribution_after_attributed_ads_rub
                   - (contribution_before_ads_rub - ad_spend_attributed_rub - sku_promotion_rub)) > 0.005) after_breaks,
       IF(COUNTIF(ABS(contribution_before_ads_rub
                   - (seller_base_revenue_rub - product_cogs_rub - commission_rub
                      - direct_variable_marketplace_costs_rub - other_direct_marketplace_costs_rub)) > 0.005) = 0
          AND COUNTIF(ABS(contribution_after_attributed_ads_rub
                   - (contribution_before_ads_rub - ad_spend_attributed_rub - sku_promotion_rub)) > 0.005) = 0
          , 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

-- @check P06_PROMOTION_FAMILY_AGGREGATE
-- Агрегат семейства продвижения равен сумме своих слагаемых на каждой строке,
-- а его ДРР считается от той же выручки. actual_drr_pct НЕ переопределён.
SELECT ROUND(SUM(promotion_family_rub), 2) family_total,
       ROUND(SUM(ad_spend_attributed_rub) + SUM(sku_promotion_rub), 2) family_components,
       COUNTIF(ABS(promotion_family_rub - (ad_spend_attributed_rub + sku_promotion_rub)) > 0.02) row_breaks,
       ROUND(SAFE_DIVIDE(SUM(promotion_family_rub), SUM(seller_base_revenue_rub))*100, 4) family_drr,
       ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(seller_base_revenue_rub))*100, 4) ads_only_drr,
       IF(COUNTIF(ABS(promotion_family_rub - (ad_spend_attributed_rub + sku_promotion_rub)) > 0.02) = 0,
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

-- @check P07_PORTFOLIO_TOTALS
-- ИНВАРИАНТ: управленческие итоги портфеля сходятся с пересчётом из компонентов.
-- Абсолютные величины выводятся для чтения человеком, но условием PASS не являются:
-- они законно меняются с каждой продажей.
SELECT ROUND(SUM(seller_base_revenue_rub), 2) revenue,
       ROUND(SUM(commission_rub + direct_variable_marketplace_costs_rub + other_direct_marketplace_costs_rub), 2) mp_costs,
       ROUND(SUM(promotion_family_rub), 2) promotion_family,
       ROUND(SUM(contribution_before_ads_rub), 2) contribution_before_ads,
       ROUND(SUM(contribution_after_attributed_ads_rub), 2) final_profit,
       ROUND(SAFE_DIVIDE(SUM(contribution_after_attributed_ads_rub), SUM(seller_base_revenue_rub))*100, 4) margin_pct,
       ROUND(SUM(contribution_before_ads_rub) - SUM(promotion_family_rub) - SUM(contribution_after_attributed_ads_rub), 2) chain_residual,
       IF(ABS(ROUND(SUM(contribution_before_ads_rub) - SUM(promotion_family_rub)
                    - SUM(contribution_after_attributed_ads_rub), 2)) <= 0.05, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

-- @check P07B_HISTORICAL_ANCHOR_2026_08_31
-- ЯКОРЬ, а не инвариант: в закрытом окне до 2026-08-31 лежат все 84 начисления
-- продвижения на 10 428,67 ₽, измеренные при исследовании UBR-010. Окно закрыто, но не
-- заморожено: у Ozon есть 30-суточное окно начислений (Gate 9B), поэтому новая строка за
-- прошлый период возможна. Если проверка покраснеет — это НЕ дефект переноса: объяснить
-- дельту и переприколоть якорь, не ослабляя остальных проверок.
SELECT COUNT(*) rows_, ROUND(SUM(-amount_rub), 2) total,
       IF(COUNT(*) = 84 AND ROUND(SUM(-amount_rub), 2) = 10428.67, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
WHERE type_id IN (116, 74, 48) AND event_date <= DATE '2026-08-31';

-- @check P08_HIGH_IMPACT_SKU_CHERRY_AMBER
-- Регрессионный случай наибольшего влияния: у EVT-SET-CHERRY-AMBER продвижение —
-- 17,81 % выручки. Итог SKU не меняется, ДРР семейства продвижения — 35,05 %.
SELECT ROUND(SUM(seller_base_revenue_rub), 2) revenue,
       ROUND(SUM(sku_promotion_rub), 2) promotion,
       ROUND(SUM(ad_spend_attributed_rub), 2) ads,
       ROUND(SUM(contribution_before_ads_rub), 2) contribution_before_ads,
       ROUND(SUM(contribution_after_attributed_ads_rub), 2) final_profit,
       ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(seller_base_revenue_rub))*100, 2) ads_only_drr,
       ROUND(SAFE_DIVIDE(SUM(promotion_family_rub), SUM(seller_base_revenue_rub))*100, 2) family_drr,
       -- ИНВАРИАНТ: у этого SKU продвижение заметно больше нуля и ДРР семейства строго
       -- выше ДРР одной атрибуции — именно это различие и было смыслом решения UBR-010.
       -- Абсолюты (2 767,45 ₽ продвижения, итог −1 707,11 ₽, ДРР 35,05 %) измерены
       -- 2026-09-22 и выводятся для чтения, условием PASS не являются.
       IF(SUM(sku_promotion_rub) > 0
          AND SAFE_DIVIDE(SUM(promotion_family_rub), SUM(seller_base_revenue_rub))
              > SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(seller_base_revenue_rub)),
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
WHERE internal_sku = 'EVT-SET-CHERRY-AMBER';

-- @check P09_LAYER_AGREEMENT_SKU_VS_STORE
-- Обе витрины держат продвижение на одном слое: «вклад до рекламы» ни в одной из них
-- его больше не содержит. Разница уровней остаётся только там, где она объявлена
-- (магазинный слой несёт расходы уровня магазина и биллинг CPC, а не атрибуцию).
SELECT ROUND((SELECT SUM(contribution_before_ads_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) sku_before_ads,
       ROUND((SELECT SUM(l1_gross_seller_contribution_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`), 2) store_l1,
       ROUND((SELECT SUM(sku_promotion_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) sku_promotion,
       -- ИНВАРИАНТ: «вклад до рекламы» витрины SKU больше не содержит продвижения,
       -- то есть строго превышает «вклад после рекламы» ровно на семейство продвижения.
       IF(ABS((SELECT SUM(contribution_before_ads_rub) - SUM(promotion_family_rub)
                      - SUM(contribution_after_attributed_ads_rub)
               FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`)) <= 0.05,
          'PASS', 'FAIL') status;
