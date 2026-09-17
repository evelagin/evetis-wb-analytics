-- ============================================================================
-- EXECUTIVE V2 BACKEND — валидация (read-only, fail-closed ASSERT) на ВСЕЙ истории.
-- Миграция: sql/dash/executive_v2_backend_2026-09-16.sql
-- Документ:  docs/EXECUTIVE_V2_BACKEND_2026-09-16.md
-- Инварианты FIN CONTRACT V2 проверяет sql/dash/fin_contract_v2_validation.sql —
-- запускать оба файла.
-- ============================================================================

-- ── GAP-01/05. Ценовая цепочка ──────────────────────────────────────────────
ASSERT (SELECT COUNTIF(price_chain_available AND ABS(price_chain_rounding_rub) > 0.01)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`) = 0
  AS 'EV2-A1: тождество цепочки цены нарушено сверх 0,01 ₽ на строке';

ASSERT (SELECT COUNTIF(price_chain_available <> (source_layer IN ('WEEKLY', 'DAILY')))
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`) = 0
  AS 'EV2-A2: доступность цепочки не совпала с API-слоем (LEGACY — нет, WEEKLY/DAILY — да)';

ASSERT (SELECT COUNTIF(ABS(IFNULL(marketplace_fee_gap_rub, 0) - (seller_price_rub - for_pay_rub)) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`) = 0
  AS 'EV2-A3: marketplace_fee_gap_rub ≠ цена продавца − к перечислению';

ASSERT (SELECT COUNTIF(fee_components_covered AND ABS(
            fee_spp_rub + wb_remuneration_rub + wb_remuneration_vat_rub + acquiring_rub + pvz_reward_rub
          + fee_components_rounding_rub - marketplace_fee_rub) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`) = 0
  AS 'EV2-A4: разложение «Удержано WB из цены» не сходится с marketplace_fee_rub';

ASSERT (SELECT COUNTIF(NOT fee_components_covered AND (wb_remuneration_vat_rub IS NOT NULL OR pvz_reward_rub IS NOT NULL))
             + COUNTIF(sales_covered AND spp_rub IS NULL)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`) = 0
  AS 'EV2-A5: компоненты опубликованы вне покрытия или СПП пропущена на покрытых сутках';

ASSERT (SELECT COUNTIF(b.spp_rub IS DISTINCT FROM k.sales_revenue_seller_base_rub - k.sales_revenue_buyer_paid_rub)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` b
        JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY` k USING (day)
        WHERE k.sales_covered) = 0
  AS 'EV2-A6: spp_rub ≠ выручка по цене продавца − оплачено покупателями';

-- ── GAP-02. Плечи логистики ─────────────────────────────────────────────────
ASSERT (SELECT COUNTIF(finance_covered AND ABS(
            logistics_sold_rub + logistics_cancellation_rub + logistics_customer_return_rub
          + logistics_defect_return_to_seller_rub + logistics_correction_rub
          + logistics_unlabeled_rub + logistics_other_label_rub - logistics_rub) > 0.005)
             + COUNTIF(finance_covered AND ABS(logistics_cancellation_rub
          - logistics_cancel_to_customer_rub - logistics_cancel_from_customer_rub) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`) = 0
  AS 'EV2-B1: плечи логистики не складываются в logistics_rub';

ASSERT (SELECT SUM(logistics_other_label_rows)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`) = 0
  AS 'EV2-B2: WB прислал новую метку плеча логистики — расширить категории';

ASSERT (SELECT COUNTIF(day >= DATE '2026-07-13' AND logistics_unlabeled_rows > 0)
             + COUNTIF(logistics_legs_covered <> (finance_covered AND logistics_unlabeled_rows = 0))
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`) = 0
  AS 'EV2-B3: неразмеченная логистика в API-слое или флаг покрытия плеч рассогласован';

-- ── GAP-04 / GAP-01 settlement ──────────────────────────────────────────────
ASSERT (SELECT COUNTIF(settlement_eligible AND (
            ABS(post_sale_logistics_rub + post_sale_storage_rub + post_sale_acceptance_rub + post_sale_penalty_rub
              + post_sale_wb_promotion_documents_rub + post_sale_tariff_option_minimum_payment_rub
              + post_sale_utilization_rub + post_sale_transit_rub + post_sale_force_majeure_rub
              + post_sale_review_points_refund_rub + post_sale_unclassified_deductions_rub
              + post_sale_loyalty_program_rub + post_sale_loyalty_points_rub + post_sale_other_operations_rub
              - post_sale_deductions_rub) > 0.005
         OR ABS(post_sale_deductions_rub + post_realization_deductions_rub) > 0.005
         OR ABS(settlement_goods_rub - post_sale_deductions_rub - wb_payout_rub) > 0.005))
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`) = 0
  AS 'EV2-C1: разложение удержаний после реализации не замыкает мост выплаты';

ASSERT (SELECT COUNTIF(settlement_price_chain_covered AND ABS(
            settlement_seller_price_rub - settlement_spp_rub - settlement_wb_remuneration_rub
          - settlement_wb_remuneration_vat_rub - settlement_acquiring_rub - settlement_pvz_reward_rub
          - settlement_price_chain_rounding_rub + settlement_goods_other_operations_rub
          - settlement_goods_rub) > 0.005)
             + COUNTIF(settlement_price_chain_covered AND ABS(
            settlement_seller_price_rub - settlement_spp_rub - settlement_buyer_paid_rub) > 0.005)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`) = 0
  AS 'EV2-C2: цепочка «цена продавца → к перечислению за товар» не сходится';

ASSERT (SELECT COUNTIF(s.settlement_eligible
                       AND s.post_sale_wb_promotion_documents_rub IS DISTINCT FROM c.ad_billing_reconstructed_rub)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` s
        JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` c USING (day)) = 0
  AS 'EV2-C3: реклама settlement ≠ документы WB Продвижение corrected-слоя';

-- ── GAP-03. Когорта выкупа ──────────────────────────────────────────────────
ASSERT (SELECT COUNTIF(buyout_orders + cancelled_orders + unresolved_orders + conflict_orders <> cohort_orders)
             + COUNTIF(resolved_orders <> buyout_orders + cancelled_orders)
             + COUNTIF(cohort_is_final AND (unresolved_orders > 0 OR conflict_orders > 0))
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`) = 0
  AS 'EV2-D1: исходы когорты не образуют разбиение или финальность нарушена';

ASSERT (SELECT (SELECT SUM(cohort_orders) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`)
             = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
                WHERE nm_id IN (SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
                                WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL)))
  AS 'EV2-D2: когорты потеряли или размножили заказы';

ASSERT (SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)
             = (SELECT COUNT(DISTINCT order_srid) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)
           AND (SELECT COUNT(*) FROM (
                  SELECT srid FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
                  WHERE NOT is_return AND NULLIF(TRIM(srid), '') IS NOT NULL
                  GROUP BY srid HAVING COUNT(*) > 1)) = 0
           AND (SELECT SUM(conflict_orders) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`) = 0)
  AS 'EV2-D3: связь заказ → исход перестала быть 1:1 (дубли srid, мульти-продажи или конфликты)';

-- ── STATUS ──────────────────────────────────────────────────────────────────
ASSERT (SELECT COUNTIF(executive_incomplete_day + executive_provisional_day + executive_final_day <> 1)
             + COUNTIF(executive_data_status <> CASE WHEN executive_incomplete_day = 1 THEN 'INCOMPLETE'
                                                     WHEN executive_provisional_day = 1 THEN 'PROVISIONAL'
                                                     ELSE 'FINAL' END)
             + COUNTIF(executive_final_day = 1 AND (exec_provisional_finance_day = 1 OR exec_provisional_ads_billing_day = 1))
             + COUNTIF(executive_incomplete_day = 0 AND period_result_after_product_cogs_rub IS NULL)
             + COUNTIF(executive_incomplete_day = 1 AND NOT after_product_cogs_eligible
                       AND exec_missing_sales_day + exec_missing_ads_attribution_day + exec_missing_finance_day
                         + exec_missing_ads_billing_day + exec_missing_cogs_day = 0)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`) = 0
  AS 'EV2-E1: статус данных Executive рассогласован с флагами';

-- ── REPORT ──────────────────────────────────────────────────────────────────
SELECT
  w.window_name,
  SUM(b.spp_rub)                          AS spp_rub,
  SUM(b.fee_spp_rub)                      AS fee_spp_rub,
  SUM(b.wb_remuneration_rub)              AS wb_remuneration_rub,
  SUM(b.wb_remuneration_vat_rub)          AS wb_remuneration_vat_rub,
  SUM(b.acquiring_rub)                    AS acquiring_rub,
  SUM(b.pvz_reward_rub)                   AS pvz_reward_rub,
  SUM(b.marketplace_fee_rub)              AS marketplace_fee_rub,
  COUNTIF(b.fee_components_covered)       AS fee_components_covered_days,
  SUM(b.logistics_sold_rub)               AS logistics_sold_rub,
  SUM(b.logistics_cancellation_rub)       AS logistics_cancellation_rub,
  SUM(b.logistics_unlabeled_rub)          AS logistics_unlabeled_rub,
  SUM(b.logistics_rub)                    AS logistics_rub,
  SUM(e.executive_final_day)              AS final_days,
  SUM(e.executive_provisional_day)        AS provisional_days,
  SUM(e.executive_incomplete_day)         AS incomplete_days,
  SUM(e.period_result_after_product_cogs_rub) AS result_after_cogs_rub,
  SUM(s.wb_payout_rub)                    AS wb_payout_rub,
  SUM(s.post_sale_deductions_rub)         AS post_sale_deductions_rub
FROM UNNEST([
  STRUCT('CONTROL 31.08–13.09.2026' AS window_name, DATE '2026-08-31' AS d1, DATE '2026-09-13' AS d2),
  ('HISTORICAL 27.07–23.08.2026', DATE '2026-07-27', DATE '2026-08-23'),
  ('FULL HISTORY', DATE '2000-01-01', DATE '2100-01-01')
]) w
JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` b ON b.day BETWEEN w.d1 AND w.d2
JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` e USING (day)
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` s USING (day)
GROUP BY w.window_name
ORDER BY w.window_name;
