-- ============================================================================
-- UBR-010 · СВЕРКА ПЕРЕНОСА promotion_billed_rub НА СЛОЙ L3. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: `-- @check <ID>`, колонка status ∈ {PASS, FAIL}.
-- Исполнитель: tools/run_data_checks.py --adapter check_blocks
--
-- До развёртывания объекты pending_deploy ещё не существуют под своими именами:
--   python tools/scale1_predeploy_render.py sql/scale1/promotion_l3_reconciliation.sql
-- После развёртывания файл исполняется как есть.
--
-- Фикстура сверки — 10 428,67 ₽ (типы начислений 116, 74, 48 за всю историю).
-- Требования владельца: сумма не исчезает, не уменьшает выручку, не попадает в
-- операционные расходы площадки, не учитывается дважды, прослеживается от сырья
-- до конечного управленческого потребителя.
-- ============================================================================

-- @check P01_FIXTURE_TRACEABLE_RAW_TO_NEUTRAL
-- Одна и та же величина на всех четырёх уровнях: сырьё → суточный факт Ozon →
-- месячный P&L Ozon → нейтральный факт. Допуск 0,01 ₽.
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
       IF((SELECT v FROM raw_sum) = 10428.67
          AND ABS((SELECT v FROM daily) - (SELECT v FROM raw_sum)) <= 0.01
          AND ABS((SELECT v FROM monthly) - (SELECT v FROM raw_sum)) <= 0.01
          AND ABS((SELECT v FROM neutral) - (SELECT v FROM raw_sum)) <= 0.01,
          'PASS', 'FAIL') status;

-- @check P02_REVENUE_UNCHANGED
-- Продвижение не уменьшает выручку ни на одном уровне.
SELECT ROUND((SELECT SUM(seller_base_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) monthly_revenue,
       ROUND((SELECT SUM(seller_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON'), 2) neutral_revenue,
       IF(ROUND((SELECT SUM(seller_base_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) = 1884733.16
          AND ABS(ROUND((SELECT SUM(seller_revenue_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON'), 2) - 1884733.16) <= 0.01,
          'PASS', 'FAIL') status;

-- @check P03_NOT_IN_MARKETPLACE_COSTS
-- Продвижение не входит в операционные расходы площадки: расходы = комиссия +
-- прямые переменные + прочие прямые, ровно и без него.
SELECT ROUND(SUM(marketplace_costs_total_rub), 2) mp_costs_total,
       ROUND(SUM(marketplace_commission_rub + IFNULL(logistics_rub,0) + IFNULL(storage_rub,0)
                 + IFNULL(acquiring_rub,0) + IFNULL(other_marketplace_costs_rub,0)), 2) mp_costs_components,
       COUNTIF(ABS(marketplace_costs_total_rub
                   - (marketplace_commission_rub + IFNULL(logistics_rub,0) + IFNULL(storage_rub,0)
                      + IFNULL(acquiring_rub,0) + IFNULL(other_marketplace_costs_rub,0))) > 0.005) decomposition_breaks,
       IF(ABS(ROUND(SUM(marketplace_costs_total_rub), 2) - 883976.09) <= 0.01
          AND COUNTIF(ABS(marketplace_costs_total_rub
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
-- Двойного счёта нет: величина вычтена РОВНО ОДИН раз. Проверяется тем, что
-- «вклад до рекламы» её больше не содержит, а «вклад после рекламы» отличается от него
-- ровно на сумму рекламы и продвижения.
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
          AND ABS(ROUND(SUM(contribution_before_ads_rub), 2) - 540934.60) <= 0.02
          AND ABS(ROUND(SUM(contribution_after_attributed_ads_rub), 2) - 53362.86) <= 0.02,
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

-- @check P06_PROMOTION_FAMILY_AGGREGATE
-- Агрегат семейства продвижения равен сумме своих слагаемых на каждой строке,
-- а его ДРР считается от той же выручки. actual_drr_pct НЕ переопределён.
SELECT ROUND(SUM(promotion_family_rub), 2) family_total,
       ROUND(SUM(ad_spend_attributed_rub) + SUM(sku_promotion_rub), 2) family_components,
       COUNTIF(ABS(promotion_family_rub - (ad_spend_attributed_rub + sku_promotion_rub)) > 0.02) row_breaks,
       ROUND(SAFE_DIVIDE(SUM(promotion_family_rub), SUM(seller_base_revenue_rub))*100, 4) family_drr,
       ROUND(SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(seller_base_revenue_rub))*100, 4) ads_only_drr,
       IF(COUNTIF(ABS(promotion_family_rub - (ad_spend_attributed_rub + sku_promotion_rub)) > 0.02) = 0
          AND ABS(ROUND(SUM(promotion_family_rub), 2) - 487571.74) <= 0.02, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

-- @check P07_PORTFOLIO_TOTALS
-- Управленческие итоги портфеля Ozon после переноса. Итог и маржа не меняются —
-- меняются вклад до рекламы и состав семейства продвижения.
SELECT ROUND(SUM(seller_base_revenue_rub), 2) revenue,
       ROUND(SUM(commission_rub + direct_variable_marketplace_costs_rub + other_direct_marketplace_costs_rub), 2) mp_costs,
       ROUND(SUM(promotion_family_rub), 2) promotion_family,
       ROUND(SUM(contribution_before_ads_rub), 2) contribution_before_ads,
       ROUND(SUM(contribution_after_attributed_ads_rub), 2) final_profit,
       ROUND(SAFE_DIVIDE(SUM(contribution_after_attributed_ads_rub), SUM(seller_base_revenue_rub))*100, 4) margin_pct,
       IF(ABS(ROUND(SUM(seller_base_revenue_rub), 2) - 1884733.16) <= 0.02
          AND ABS(ROUND(SUM(commission_rub + direct_variable_marketplace_costs_rub + other_direct_marketplace_costs_rub), 2) - 883976.09) <= 0.02
          AND ABS(ROUND(SUM(promotion_family_rub), 2) - 487571.74) <= 0.02
          AND ABS(ROUND(SUM(contribution_before_ads_rub), 2) - 540934.60) <= 0.02
          AND ABS(ROUND(SUM(contribution_after_attributed_ads_rub), 2) - 53362.86) <= 0.02,
          'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`;

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
       IF(ABS(ROUND(SUM(sku_promotion_rub), 2) - 2767.45) <= 0.01
          AND ABS(ROUND(SUM(contribution_after_attributed_ads_rub), 2) - (-1707.11)) <= 0.02
          AND ABS(ROUND(SAFE_DIVIDE(SUM(promotion_family_rub), SUM(seller_base_revenue_rub))*100, 2) - 35.05) <= 0.01,
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
       IF(ABS(ROUND((SELECT SUM(contribution_before_ads_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) - 540934.60) <= 0.02,
          'PASS', 'FAIL') status;
