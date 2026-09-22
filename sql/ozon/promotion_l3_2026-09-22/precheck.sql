-- ============================================================================
-- UBR-010 · ПРЕДПРОВЕРКА перед развёртыванием. ТОЛЬКО ЧТЕНИЕ.
-- Все блоки обязаны вернуть status = 'PASS'. Исполнитель:
--   python tools/run_data_checks.py --file sql/ozon/promotion_l3_2026-09-22/precheck.sql \
--     --adapter check_blocks --project <PROJECT> --token-command "gcloud auth print-access-token"
-- ============================================================================

-- @check U1_NEW_COLUMNS_ABSENT
-- Развёртывание ещё не выполнялось: новых колонок в production нет.
SELECT
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart`.INFORMATION_SCHEMA.COLUMNS
   WHERE table_name = 'FACT_SKU_DAILY' AND column_name = 'promotion_billed_rub') neutral_new_col,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart`.INFORMATION_SCHEMA.COLUMNS
   WHERE table_name = 'FCT_OZON_SKU_PNL_MONTHLY' AND column_name IN ('promotion_family_rub','promotion_family_drr_pct')) monthly_new_cols,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart`.INFORMATION_SCHEMA.COLUMNS
      WHERE table_name = 'FACT_SKU_DAILY' AND column_name = 'promotion_billed_rub') = 0
     AND (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart`.INFORMATION_SCHEMA.COLUMNS
          WHERE table_name = 'FCT_OZON_SKU_PNL_MONTHLY' AND column_name IN ('promotion_family_rub','promotion_family_drr_pct')) = 0,
     'PASS', 'FAIL') status;

-- @check U2_PREDEPLOY_STATE_MATCHES_ROLLBACK
-- Откат восстанавливает состояние «до». Проверяем, что production ещё в нём:
-- вклад до рекламы у месячного P&L равен 530 505,93 ₽ (с продвижением внутри),
-- а нейтральный факт ломает тождество ровно на 71 строке — это и есть исходный дефект.
SELECT
  ROUND((SELECT SUM(contribution_before_ads_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) before_ads_now,
  (SELECT COUNTIF(contribution_after_cogs_rub IS NOT NULL
      AND ABS(contribution_after_cogs_rub - (contribution_after_ads_rub - cogs_rub)) > 0.005)
   FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON') identity_breaks_now,
  IF(ABS(ROUND((SELECT SUM(contribution_before_ads_rub) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`), 2) - 530505.93) <= 0.02
     AND (SELECT COUNTIF(contribution_after_cogs_rub IS NOT NULL
            AND ABS(contribution_after_cogs_rub - (contribution_after_ads_rub - cogs_rub)) > 0.005)
          FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY` WHERE marketplace = 'OZON') = 71,
     'PASS', 'FAIL') status;

-- @check U3_FIXTURE_UNCHANGED
-- Фикстура сверки не изменилась с момента исследования: 84 начисления на 10 428,67 ₽.
SELECT COUNT(*) rows_, ROUND(SUM(-amount_rub), 2) total,
       IF(COUNT(*) = 84 AND ROUND(SUM(-amount_rub), 2) = 10428.67, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
WHERE type_id IN (116, 74, 48);

-- @check U4_CT_BASELINE
-- Базовое расхождение Control Tower за август до правки: вклад +10 336,14 ₽, выручка +986,04 ₽.
-- После развёртывания останется только разница выручки (UBR-012).
SELECT ROUND(ct.rev - pr.rev, 2) d_rev, ROUND(ct.contrib - pr.contrib, 2) d_contrib,
       IF(ABS(ROUND(ct.rev - pr.rev, 2) - 986.04) <= 0.02
          AND ABS(ROUND(ct.contrib - pr.contrib, 2) - 10336.14) <= 0.02, 'PASS', 'FAIL') status
FROM (SELECT SUM(revenue_seller_base) rev, SUM(contribution) contrib
      FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
      WHERE marketplace = 'OZON' AND d BETWEEN DATE '2026-08-01' AND DATE '2026-08-31') ct,
     (SELECT SUM(seller_base_revenue_rub) rev, SUM(contribution_after_attributed_ads_rub) contrib
      FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
      WHERE month = DATE '2026-08-01') pr;

-- @check U5_DOWNSTREAM_CONSUMERS_PRESENT
-- Четыре потребителя ниже по течению существуют и будут пересобраны вместе с базой.
SELECT COUNT(*) present,
       IF(COUNT(*) = 4, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart`.INFORMATION_SCHEMA.VIEWS
WHERE table_name IN ('V_OZON_LIFETIME_PNL', 'V_OZON_SKU_UNIT_ECONOMICS_CURRENT',
                     'V_OZON_AGENT_DECISION_INPUT', 'V_OZON_SKU_PNL_DAILY_OPERATIONAL');
