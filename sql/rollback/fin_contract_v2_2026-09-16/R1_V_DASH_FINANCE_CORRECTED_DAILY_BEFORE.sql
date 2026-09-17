-- ОТКАТ fin contract v2: определение ДО изменений, снято из production 2026-09-16
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` AS
WITH
kpi AS (
  SELECT
    day,
    sales_covered, ads_covered, finance_covered, contribution_covered,
    finance_is_final, contains_provisional_finance, is_current_day,
    sales_revenue_seller_base_rub,
    ad_spend_attributed_rub,
    storage_rub, deduction_rub, acceptance_rub,
    penalty_account_rub, reimbursement_account_rub, other_account_rub,
    account_level_total_rub,
    contribution_pre_cogs_rub,
    period_result_pre_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
-- Разложение удержаний. Маска — deduction_rub: разложение обязано существовать
-- ровно там, где существует его итог, и отсутствовать там, где итог неизвестен.
ded AS (
  SELECT
    k.day,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.ad_spend_billed_rub,             NUMERIC '0')) AS ad_billing_reconstructed_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.transit_deduction_rub,           NUMERIC '0')) AS transit_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.unclassified_deduction_rub,      NUMERIC '0')) AS unclassified_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.conflict_deduction_rub,          NUMERIC '0')) AS classification_conflict_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.force_majeure_rub,               NUMERIC '0')) AS force_majeure_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.utilization_rub,                 NUMERIC '0')) AS utilization_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.review_points_refund_rub,        NUMERIC '0')) AS review_points_refund_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.minimum_payment_adjustment_rub,  NUMERIC '0')) AS minimum_payment_adjustment_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.unknown_semantic_rub,            NUMERIC '0')) AS unknown_semantic_rub,
    r.total_deduction_rub                                                                     AS classifier_total_deduction_rub,
    r.deduction_rows,
    r.unknown_semantic_rows,
    r.direct_label_rows,
    r.ads_attribution_covered,
    r.ad_spend_billed_rub,
    r.ad_spend_unallocated_rub,
    r.ad_spend_attributed_rub AS recon_ad_spend_attributed_rub,
    r.ad_billing_classification_confidence
  FROM kpi k
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY` r ON r.day = k.day
)

SELECT
  k.day,

  -- ── Гейты покрытия: PASS-THROUGH. Ничего не пересчитывается. ──
  k.sales_covered,
  k.ads_covered,
  k.finance_covered,
  k.contribution_covered,
  k.finance_is_final,
  k.contains_provisional_finance,
  k.is_current_day,

  -- 🔴 ЕДИНЫЙ ELIGIBLE-DAY UNIVERSE. Числитель и знаменатель процента обязаны
  --    жить на одном множестве суток.
  (k.contribution_covered AND k.finance_covered)          AS period_result_eligible,
  k.contribution_covered                                  AS contribution_eligible,
  IF(k.contribution_covered AND k.finance_covered, 1, 0)  AS period_result_eligible_day,
  IF(k.contribution_covered, 1, 0)                        AS contribution_eligible_day,

  -- ── ВЫРУЧКА: сырая и выровненные знаменатели ──
  k.sales_revenue_seller_base_rub,
  IF(k.contribution_covered AND k.finance_covered, k.sales_revenue_seller_base_rub, NULL)
                                                          AS revenue_base_period_result_rub,
  IF(k.contribution_covered, k.sales_revenue_seller_base_rub, NULL)
                                                          AS revenue_base_contribution_rub,

  -- ── РАСХОДЫ УРОВНЯ СЧЁТА: BEFORE, без единого изменения ──
  k.storage_rub,
  k.deduction_rub,
  k.acceptance_rub,
  k.penalty_account_rub,
  k.reimbursement_account_rub,
  k.other_account_rub,
  k.account_level_total_rub,

  -- ── РАЗЛОЖЕНИЕ УДЕРЖАНИЙ ПО ПРЯМЫМ МЕТКАМ WB (Stage 3.1F) ──
  -- 🔴 deduction_rub НЕ уменьшается и с экрана не исчезает: он остаётся
  --    полным итогом операции WB. Исключения работают только в расчёте.
  d.ad_billing_reconstructed_rub,
  d.transit_deduction_rub,
  d.unclassified_deduction_rub,
  d.classification_conflict_rub,
  d.force_majeure_rub,
  d.utilization_rub,
  d.review_points_refund_rub,
  d.minimum_payment_adjustment_rub,
  d.unknown_semantic_rub,
  -- Исключительная компенсация для человеческого чтения: положительная = выгода.
  (-d.force_majeure_rub)                                  AS exceptional_compensation_rub,
  -- Операционные удержания, остающиеся в результате (без рекламы и форс-мажора).
  ( d.transit_deduction_rub
  + d.unclassified_deduction_rub
  + d.classification_conflict_rub
  + d.utilization_rub
  + d.review_points_refund_rub
  + d.minimum_payment_adjustment_rub
  + d.unknown_semantic_rub )                              AS other_wb_deductions_rub,
  d.deduction_rows,
  d.unknown_semantic_rows,
  d.direct_label_rows,
  d.ad_billing_classification_confidence,
  -- Fail-closed индикатор: части обязаны складываться в целое.
  (d.ad_billing_reconstructed_rub
 + d.transit_deduction_rub
 + d.unclassified_deduction_rub
 + d.classification_conflict_rub
 + d.force_majeure_rub
 + d.utilization_rub
 + d.review_points_refund_rub
 + d.minimum_payment_adjustment_rub
 + d.unknown_semantic_rub = k.deduction_rub)              AS deduction_decomposition_complete,

  -- ── ОПЕРАЦИОННЫЙ УРОВЕНЬ СЧЁТА (OPTION B) ──
  -- Вычитание, а не перечисление. NULL распространяется естественно.
  (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub)
                                                          AS account_level_total_corrected_rub,

  -- ── ЭКОНОМИКА ──
  k.contribution_pre_cogs_rub,
  k.period_result_pre_cogs_rub,
  -- 🔴 Гейт повторяет действующую формулу V_DASH_KPI_DAILY символ в символ.
  IF(k.contribution_covered AND k.finance_covered,
     k.contribution_pre_cogs_rub
       - (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub),
     NULL)                                                AS period_result_pre_cogs_corrected_rub,
  -- Исключительная компенсация в разрезе того же гейта — для отдельной строки
  -- экрана. В операционный результат выше она НЕ входит.
  IF(k.contribution_covered AND k.finance_covered, -d.force_majeure_rub, NULL)
                                                          AS exceptional_compensation_eligible_rub,

  -- ── РЕКЛАМНАЯ СВЕРКА ──
  -- 🔴 attributed НЕ подменяется billed. billed — контрольная величина
  --    уровня счёта, unallocated — разрыв сверки, а НЕ статья затрат.
  k.ad_spend_attributed_rub,
  d.ad_spend_billed_rub,
  d.ad_spend_unallocated_rub,
  d.ads_attribution_covered,

  -- ── Диагностика сверки (не метрики экрана) ──
  d.classifier_total_deduction_rub,
  d.recon_ad_spend_attributed_rub,

  'PRE_COGS_AD_BILLING_CORRECTED_EXCL_EXCEPTIONAL'        AS economics_basis,
  'Результат после SKU-level и account-level расходов, но ДО себестоимости товара, FF, OPEX и налога. Не прибыль. Рекламный биллинг исключён, потому что реклама уже вычтена как attributed внутри contribution_pre_cogs_rub. Форс-мажорные компенсации исключены из ОПЕРАЦИОННОГО результата (owner OPTION B) и публикуются отдельно как exceptional_compensation_rub.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM kpi k
LEFT JOIN ded d USING (day);
