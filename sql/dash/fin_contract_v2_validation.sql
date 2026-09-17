-- ============================================================================
-- FIN CONTRACT V2 — валидация (read-only, fail-closed ASSERT).
-- Контракт: docs/FIN_CONTRACT_V2_2026-09-16.md
-- Миграция: sql/dash/fin_contract_v2_2026-09-16.sql
--
-- Запуск целиком как BigQuery script. Любое нарушение останавливает скрипт с
-- именем проверки. Ни одной записи: только SELECT/ASSERT.
-- ============================================================================

-- ── DQ-1. Возмещения: техническое тождество и нулевой P&L-эффект ────────────
-- 1a. Каждая строка операций, отнесённых к MEMO, удовлетворяет тождеству и не
--     несёт денежного потока продавцу (вся история).
ASSERT (
  SELECT COUNTIF(
      ABS(IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.vw') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.vwNds') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.rebillLogisticCost') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.ppvzReward') AS NUMERIC), 0)) > 0.02
    OR IFNULL(SAFE_CAST(REPLACE(s.for_pay, ',', '.') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.deliveryService') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.paidStorage') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.deduction') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.penalty') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.additionalPayment') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(s.raw_json, '$.paidAcceptance') AS NUMERIC), 0) <> 0)
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC` s
  WHERE s.supplier_oper_name IN (
    SELECT op_key FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP` WHERE economic_direction = 'MEMO')
) = 0 AS 'DQ-1a: строка MEMO-операции нарушает техническое тождество или несёт денежный поток';

-- 1b. management_pnl_impact MEMO-строк = 0 (ни ненулевых, ни NULL).
ASSERT (SELECT IFNULL(SUM(ABS(cost_amount_positive)), 0) + COUNTIF(cost_amount_positive IS NULL)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE economic_direction = 'MEMO') = 0
  AS 'DQ-1b: MEMO-строки влияют на P&L';

-- 1c. Возмещения не попадают ни в одну величину экрана.
ASSERT (SELECT COUNTIF(IFNULL(reimbursement_account_rub, 0) <> 0 OR IFNULL(reimbursement_sku_rub, 0) <> 0)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`) = 0
  AS 'DQ-1c: reimbursement_* ненулевой в V_DASH_KPI_DAILY';

-- 1d. Домен направлений карты закрыт.
ASSERT (SELECT COUNTIF(economic_direction IS NULL OR economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT','MEMO'))
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`) = 0
  AS 'DQ-1d: economic_direction вне домена';

-- ── DQ-2. Executive-реклама = SUM(биллинг по дате услуги) ───────────────────
ASSERT (
  SELECT COUNTIF(v.ad_spend_financial_rub IS DISTINCT FROM IFNULL(f.s, NUMERIC '0'))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v
  LEFT JOIN (SELECT `date` AS d, SUM(actual_spend_rub) AS s
             FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` GROUP BY 1) f ON f.d = v.day
  WHERE v.ads_billing_covered
) = 0 AS 'DQ-2a: ad_spend_financial_rub ≠ SUM(FACT_ADS_COSTS_DAILY) на покрытых сутках';

ASSERT (SELECT COUNTIF(NOT ads_billing_covered AND ad_spend_financial_rub IS NOT NULL)
             + COUNTIF(period_result_eligible AND NOT ads_billing_covered)
             + COUNTIF(ads_billing_is_final AND NOT ads_billing_covered)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'DQ-2b: утечка рекламы вне покрытия биллинга';

-- ── DQ-3. Settlement-реклама = документы WB Продвижение; в результат не входит ─
--     Сравнивается ad_billing_reconstructed_rub — величина, которая исключается в
--     формуле. Сырая ad_spend_billed_rub — диагностика и NULL в сутках без строки
--     сверки (например, до начала рекламы).
ASSERT (
  SELECT COUNTIF(v.ad_billing_reconstructed_rub IS DISTINCT FROM IFNULL(c.s, NUMERIC '0'))
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v
  LEFT JOIN (SELECT finance_date AS d, SUM(deduction_amount_rub) AS s
             FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
             WHERE deduction_class = 'AD_BILLING' GROUP BY 1) c ON c.d = v.day
  WHERE v.deduction_rub IS NOT NULL
) = 0 AS 'DQ-3a: ad_spend_billed_rub ≠ документы AD_BILLING классификатора';

-- 3b. Независимая сборка результата: ровно ОДИН рекламный расход (биллинг),
--     документы и форс-мажор исключены, атрибуция не вычитается.
ASSERT (
  SELECT COUNTIF(ABS(
      v.period_result_pre_cogs_corrected_rub
    - ( k.sales_revenue_seller_base_rub - k.marketplace_fee_rub - k.logistics_rub
      - v.ad_spend_financial_rub
      - (k.account_level_total_rub - v.ad_billing_reconstructed_rub - v.force_majeure_rub))) > 0.005)
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY` k USING (day)
  WHERE v.period_result_eligible
) = 0 AS 'DQ-3b: результат не собирается из компонентов с единственным рекламным расходом';

-- ── DQ-4. COGS: ровно одна действующая запись на SKU × дату ────────────────
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` b
    ON a.internal_sku = b.internal_sku
   AND CONCAT(CAST(a.effective_from AS STRING), '|', a.resolver_ref)
     < CONCAT(CAST(b.effective_from AS STRING), '|', b.resolver_ref)
   AND a.effective_from <= COALESCE(b.effective_to, DATE '9999-12-31')
   AND b.effective_from <= COALESCE(a.effective_to, DATE '9999-12-31')
) = 0 AS 'DQ-4a: пересекающиеся интервалы себестоимости';

ASSERT (SELECT COUNTIF(cogs_resolution_status = 'RESOLVED' AND cogs_match_count <> 1)
             + COUNTIF(cogs_resolution_status = 'CONTRACT_VIOLATION_MULTI')
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`) = 0
  AS 'DQ-4b: продажа сопоставлена не ровно одной записи себестоимости';

-- ── DQ-5. Себестоимость набора = сумма компонентов на дату действия ─────────
ASSERT (
  WITH bundles AS (
    SELECT internal_sku, effective_from, product_cogs_rub
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
    WHERE resolver_lane = 'DERIVED_BUNDLE'
  ),
  comp AS (
    SELECT b.internal_sku, b.effective_from, b.product_cogs_rub AS bundle_cogs,
           SUM(c.component_qty * h.product_cogs_rub) AS comp_sum,
           COUNT(*) AS n_comp, COUNT(h.product_cogs_rub) AS n_priced
    FROM bundles b
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` c
      ON c.bundle_internal_sku = b.internal_sku
     AND b.effective_from BETWEEN c.effective_from AND COALESCE(c.effective_to, DATE '9999-12-31')
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` h
      ON h.internal_sku = c.component_internal_sku
     AND b.effective_from BETWEEN h.effective_from AND COALESCE(h.effective_to, DATE '9999-12-31')
    GROUP BY 1, 2, 3
  )
  SELECT COUNTIF(n_comp <> n_priced OR ABS(bundle_cogs - comp_sum) > 0.000001) FROM comp
) = 0 AS 'DQ-5: себестоимость набора ≠ сумма действующих компонентов';

-- ── DQ-6. Строка финотчёта влияет на управленческий P&L не более чем через одну статью ─
--   Статьи результата: сбор маркетплейса (спред Продажа/Возврат, SKU в universe),
--   логистика SKU (universe), уровень счёта (NOT is_sku_row, cost ≠ 0).
--   Задокументированное исключение: документы AD_BILLING и FORCE_MAJEURE_PAYMENT
--   входят в уровень счёта и вычитаются обратно — нетто-эффект 0, поэтому 0 статей.
ASSERT (
  WITH u AS (SELECT DISTINCT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
             WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL),
  net_zero AS (SELECT finance_row_key FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
               WHERE deduction_class IN ('AD_BILLING', 'FORCE_MAJEURE_PAYMENT')),
  hits AS (
    SELECT f.finance_row_key, 1 AS h
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` f
    WHERE f.marketplace_fee_gap_rub IS NOT NULL AND f.marketplace_fee_gap_rub <> 0
      AND COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)
      AND f.nm_id IN (SELECT nm_id FROM u)
    UNION ALL
    SELECT l.finance_row_key, 1
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` l
    WHERE l.cost_amount_positive IS NOT NULL AND l.cost_amount_positive <> 0
      AND ( (l.is_sku_row AND l.cost_category = 'logistics' AND l.nm_id IN (SELECT nm_id FROM u))
         OR (NOT l.is_sku_row AND l.finance_row_key NOT IN (SELECT finance_row_key FROM net_zero)) )
  )
  SELECT COUNT(*) FROM (SELECT finance_row_key FROM hits GROUP BY 1 HAVING SUM(h) > 1)
  -- KNOWN ISSUE KI-2026-09-16-1 (НЕ исправлен, решение владельца не принято):
  --   LEGACY-строка «Продажа» 09.07.2026, nm 1083392113, internal_sku разрешён,
  --   но sku_match_status = 'not_found'. Спред 484,76 ₽ не вычитается, vw −183,72
  --   и эквайринг 29,76 уходят на уровень счёта. Результат 09.07 завышен на 638,72 ₽.
  --   Исключение ограничено ровно одним ключом: любая новая строка уронит проверку.
  WHERE finance_row_key <> '778547028#3129609040166'
) = 0 AS 'DQ-6: строка финотчёта вычитается из результата более чем через одну статью';

-- DQ-6b. Строки «Продажа/Возврат» без SKU-сопоставления — наблюдаемость KI-2026-09-16-1.
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`
        WHERE supplier_oper_name IN ('Продажа', 'Возврат')
          AND NOT COALESCE(nm_id > 0 AND sku_match_status = 'matched', FALSE)
          AND finance_row_key <> '778547028#3129609040166') = 0
  AS 'DQ-6b: новая строка продажи/возврата без SKU-сопоставления (её спред выпадет из результата)';

-- ── DQ-7. Отсутствие fanout на слоях Executive ──────────────────────────────
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT `date`) FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE`)
  AS 'DQ-7a: покрытие биллинга не уникально по дате';
ASSERT (SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
             = (SELECT COUNT(DISTINCT day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
           AND (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`)
             = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`))
  AS 'DQ-7b: fanout или потеря суток в corrected/economics';

-- ── DQ-8. SKU-атрибуция сохранена и равна витрине ───────────────────────────
ASSERT (SELECT ABS(
          (SELECT IFNULL(SUM(ad_spend_attributed_rub), 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SKU_DAILY` WHERE ads_covered)
        - (SELECT IFNULL(SUM(ad_spend_attributed_rub), 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`)) < 0.01)
  AS 'DQ-8: SKU-атрибуция рекламы расходится с агрегатом витрины';

-- ── DQ-9. Выплата WB: мост замкнут ──────────────────────────────────────────
ASSERT (SELECT COUNTIF(ABS(settlement_goods_rub + post_realization_deductions_rub - wb_payout_rub) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` WHERE settlement_eligible)
  = 0 AS 'DQ-9: мост выплаты не замкнут';

-- ── DQ-10. Минимальный платёж по тарифной опции остаётся расходом ───────────
ASSERT (SELECT COUNTIF(deduction_rub IS NOT NULL AND NOT deduction_decomposition_complete)
             + COUNTIF(tariff_option_minimum_payment_rub IS DISTINCT FROM minimum_payment_adjustment_rub)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'DQ-10a: разложение удержаний неполно или алиас расхода по опции расходится';
-- Удержание входит в account_level_total_corrected; оттуда в результат вычитаются
-- только документы рекламы и форс-мажор — минимальный платёж остаётся внутри.
ASSERT (SELECT COUNTIF(ABS(account_level_total_corrected_rub
          - (storage_rub + acceptance_rub + penalty_account_rub + reimbursement_account_rub + other_account_rub
             + deduction_rub - ad_billing_reconstructed_rub - force_majeure_rub)) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
        WHERE period_result_eligible) = 0
  AS 'DQ-10b: уровень счёта собран не из удержаний за вычетом только рекламы и форс-мажора';

-- ── REPORT. Итоги контрольных окон (не ASSERT: печать для отчёта) ───────────
SELECT
  w.window_name,
  COUNTIF(v.period_result_eligible)                 AS eligible_days,
  SUM(v.ads_billing_provisional_day)                AS ads_billing_provisional_days,
  SUM(v.revenue_base_period_result_rub)             AS revenue_rub,
  SUM(IF(v.period_result_eligible, v.ad_spend_financial_rub, NULL))  AS ad_spend_financial_rub,
  SUM(IF(v.period_result_eligible, v.ad_spend_attributed_rub, NULL)) AS ad_spend_attributed_rub,
  SUM(IF(v.period_result_eligible, v.ad_spend_billed_rub, NULL))     AS ad_spend_settlement_documents_rub,
  SUM(IF(v.period_result_eligible, v.tariff_option_minimum_payment_rub, NULL)) AS tariff_option_minimum_payment_rub,
  SUM(IF(v.period_result_eligible, v.reimbursement_account_rub, NULL)) AS reimbursement_pnl_impact_rub,
  SUM(v.period_result_pre_cogs_corrected_rub)       AS result_pre_cogs_rub,
  SUM(IF(x.after_product_cogs_eligible, x.product_cogs_rub, NULL)) AS product_cogs_rub,
  SUM(x.period_result_after_product_cogs_rub)       AS result_after_cogs_rub,
  SUM(s.wb_payout_rub)                              AS wb_payout_rub
FROM UNNEST([
  STRUCT('CONTROL 31.08–13.09.2026' AS window_name, DATE '2026-08-31' AS d1, DATE '2026-09-13' AS d2),
  ('HISTORICAL 27.07–23.08.2026', DATE '2026-07-27', DATE '2026-08-23'),
  ('FULL HISTORY', DATE '2000-01-01', DATE '2100-01-01')
]) w
JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v ON v.day BETWEEN w.d1 AND w.d2
JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` x USING (day)
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` s USING (day)
GROUP BY w.window_name
ORDER BY w.window_name;
