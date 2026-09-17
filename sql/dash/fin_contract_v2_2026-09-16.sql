-- ============================================================================
-- FIN CONTRACT V2 — управленческий финансовый контракт Executive (2026-09-16)
--
-- Основание: forensic reconciliation WDS × SellMonitor за 31.08–13.09.2026 и
-- решения владельца от 2026-09-16:
--   1. Возмещения «Возмещение издержек по перевозке/…», «…по перемещению и
--      операционной обработке…», «Возмещение за выдачу и возврат на ПВЗ» —
--      техническое перераспределение вознаграждения WB. P&L-эффект = 0.
--   2. Реклама в Executive / управленческом P&L = БИЛЛИНГ ПО ДАТЕ УСЛУГИ.
--      Атрибуция статистики остаётся контрактом SKU Performance и НЕ удаляется.
--   3. Документы «WB Продвижение» из финотчёта — только сверка выплаты.
--   4. «Остаток по минимальному платежу» — расход по тарифной опции
--      «Общий рейтинг по карточке». Остаётся расходом.
--   5. Себестоимость — действующая политика по дате действия (не FIFO). Не меняется.
--
-- Документ контракта: docs/FIN_CONTRACT_V2_2026-09-16.md
-- Валидация:          sql/dash/fin_contract_v2_validation.sql
-- Откат:              sql/rollback/fin_contract_v2_2026-09-16/
--
-- ЗАМЕЩАЕТ определение V_DASH_FINANCE_CORRECTED_DAILY из
-- sql/mart/pr_deductions_direct_labels_v1.sql (раздел 3/3). Повторный прогон
-- того файла откатит этот контракт.
--
-- Порядок внутри скрипта обязателен:
--   §0 предусловия → §1 view с веткой MEMO → §2 REF_COST_MAP → §3 corrected view
--   → §4 приёмка. Ветка MEMO создаётся ДО перевода строк карты: иначе между
--   шагами cost_amount_positive стал бы NULL, а не 0.
-- ============================================================================

-- ── §0. ПРЕДУСЛОВИЯ (fail-closed, до единой записи) ──────────────────────────

-- 0.1 Три строки карты существуют и находятся в ожидаемом состоянии.
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`
        WHERE amount_field = 'commission_amount'
          AND economic_direction = 'CREDIT'
          AND op_key IN ('Возмещение за выдачу и возврат товаров на ПВЗ',
                         'Возмещение издержек по перевозке/по складским операциям с товаром',
                         'Возмещение издержек по перемещению и операционной обработке товара')) = 3
  AS 'fin v2 §0.1: ожидались ровно 3 строки CREDIT возмещений в REF_COST_MAP';

-- 0.2 Перевод в MEMO допустим только если тождество доказано на ВСЕЙ истории:
--     vw + vwNds + rebillLogisticCost + ppvzReward = 0 (±2 коп.) и
--     ни одного денежного потока продавцу (forPay и поля удержаний = 0).
ASSERT (
  SELECT COUNTIF(
      ABS(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vw') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.vwNds') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.rebillLogisticCost') AS NUMERIC), 0)
        + IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)) > 0.02
    OR IFNULL(SAFE_CAST(REPLACE(for_pay, ',', '.') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.deliveryService') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.paidStorage') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.deduction') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.penalty') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.additionalPayment') AS NUMERIC), 0) <> 0
    OR IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.paidAcceptance') AS NUMERIC), 0) <> 0
    OR raw_json IS NULL)
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
  WHERE supplier_oper_name IN ('Возмещение за выдачу и возврат товаров на ПВЗ',
                               'Возмещение издержек по перевозке/по складским операциям с товаром',
                               'Возмещение издержек по перемещению и операционной обработке товара')
) = 0
  AS 'fin v2 §0.2: тождество технической проводки возмещения нарушено хотя бы в одной строке истории';

-- 0.3 Биллинговый факт согласован со снапшотом покрытия на всех загруженных сутках.
ASSERT (
  SELECT COUNTIF(IFNULL(f.s, NUMERIC '0') <> c.answer_sum_rub)
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` c
  LEFT JOIN (SELECT `date` AS d, SUM(actual_spend_rub) AS s
             FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY` GROUP BY 1) f
         ON f.d = c.`date`
  WHERE c.requested_ok
) = 0
  AS 'fin v2 §0.3: FACT_ADS_COSTS_DAILY расходится со снапшотом V_ADV_COSTS_DAY_COVERAGE — сначала пересобрать FACT';

-- ── §1. LONG_MAPPED: явная ветка MEMO = доказанный ноль ──────────────────────
--    MEMO — «техническая проводка без P&L-эффекта». Ноль, а не NULL: NULL в этом
--    слое означает «неизвестно» и молча выбрасывается фильтрами
--    cost_amount_positive IS NOT NULL; ноль — утверждение, которое видно.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` AS
SELECT
  l.finance_row_key, l.finance_date, l.nm_id, l.sku_match_status,
  l.operation_type_normalized, l.op_key, l.is_sku_row,
  l.amount_field, l.source_signed_amount,
  r.economic_direction, r.cost_category, r.field_normalization_sign,
  CASE r.economic_direction
    WHEN 'COST'       THEN ABS(l.source_signed_amount)
    WHEN 'CREDIT'     THEN -ABS(l.source_signed_amount)
    WHEN 'ADJUSTMENT' THEN l.source_signed_amount * r.field_normalization_sign
    WHEN 'MEMO'       THEN NUMERIC '0'
    ELSE NULL
  END AS cost_amount_positive
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG` l
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP` r USING (op_key, amount_field);

-- ── §2. REF_COST_MAP: возмещения → MEMO ─────────────────────────────────────
--    cost_category и field_normalization_sign НЕ меняются: категория сохраняет
--    наблюдаемость строк (разложение по категориям), а знак в ветке MEMO не
--    участвует. Меняется только экономическое направление.
UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`
SET economic_direction = 'MEMO',
    note = CONCAT(note, ' | FIN CONTRACT V2 (2026-09-16): техническое перераспределение вознаграждения WB',
                        ' (vw + vwNds = −(rebillLogisticCost + ppvzReward), forPay = 0). НЕ доход продавца,',
                        ' P&L-эффект = 0. Решение владельца 2026-09-16.')
WHERE amount_field = 'commission_amount'
  AND economic_direction = 'CREDIT'
  AND op_key IN ('Возмещение за выдачу и возврат товаров на ПВЗ',
                 'Возмещение издержек по перевозке/по складским операциям с товаром',
                 'Возмещение издержек по перемещению и операционной обработке товара');

ASSERT (SELECT COUNTIF(economic_direction = 'MEMO') FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`) = 3
  AS 'fin v2 §2: ожидались ровно 3 строки MEMO';
ASSERT (SELECT COUNTIF(economic_direction = 'CREDIT') FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`) = 0
  AS 'fin v2 §2: осталась строка CREDIT';

-- ── §3. V_DASH_FINANCE_CORRECTED_DAILY — реклама P&L = биллинг по дате услуги ─
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
),
-- FIN CONTRACT V2: реклама управленческого P&L — биллинг WB по дате оказания услуги.
--   Значение — FACT_ADS_COSTS_DAILY (канонический факт из wb_raw.V_ADV_COSTS).
--   Покрытие — V_ADV_COSTS_DAY_COVERAGE: сутки явно запрошены успешным раном
--   (requested_ok) И факт совпадает со снапшотом покрытия до копейки. Второе
--   условие защищает от гонки: снапшот уже обновлён, а FACT ещё не пересобран.
--   Финальность — billed_complete (SLA стабильности 15 сут × 3 чтения).
--   🔴 Прецедент финансов: незакрытые сутки ВХОДЯТ в результат, как
--      contains_provisional_finance, и остаются наблюдаемыми через
--      ads_billing_is_final / ads_billing_provisional_day.
ads_bill AS (
  SELECT
    c.`date` AS day,
    (c.requested_ok AND IFNULL(f.spend, NUMERIC '0') = c.answer_sum_rub)            AS covered,
    (c.requested_ok AND IFNULL(f.spend, NUMERIC '0') = c.answer_sum_rub
       AND c.billed_complete)                                                       AS is_final,
    IFNULL(f.spend, NUMERIC '0')                                                    AS spend_rub,
    IFNULL(f.campaigns, 0)                                                          AS campaigns
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` c
  LEFT JOIN (
    SELECT `date` AS day, SUM(actual_spend_rub) AS spend, COUNT(DISTINCT advert_id) AS campaigns
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`
    GROUP BY `date`
  ) f ON f.day = c.`date`
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

  -- ── FIN CONTRACT V2: покрытие финансовой рекламы ──
  IFNULL(b.covered, FALSE)                                AS ads_billing_covered,
  IFNULL(b.is_final, FALSE)                               AS ads_billing_is_final,
  IF(IFNULL(b.covered, FALSE) AND NOT IFNULL(b.is_final, FALSE), 1, 0)
                                                          AS ads_billing_provisional_day,

  -- 🔴 ЕДИНЫЙ ELIGIBLE-DAY UNIVERSE. Числитель и знаменатель процента обязаны
  --    жить на одном множестве суток. V2: + покрытие биллинга рекламы.
  (k.contribution_covered AND k.finance_covered AND IFNULL(b.covered, FALSE))
                                                          AS period_result_eligible,
  k.contribution_covered                                  AS contribution_eligible,
  IF(k.contribution_covered AND k.finance_covered AND IFNULL(b.covered, FALSE), 1, 0)
                                                          AS period_result_eligible_day,
  IF(k.contribution_covered, 1, 0)                        AS contribution_eligible_day,

  -- ── ВЫРУЧКА: сырая и выровненные знаменатели ──
  k.sales_revenue_seller_base_rub,
  IF(k.contribution_covered AND k.finance_covered AND IFNULL(b.covered, FALSE),
     k.sales_revenue_seller_base_rub, NULL)               AS revenue_base_period_result_rub,
  IF(k.contribution_covered, k.sales_revenue_seller_base_rub, NULL)
                                                          AS revenue_base_contribution_rub,

  -- ── РАСХОДЫ УРОВНЯ СЧЁТА: BEFORE, без единого изменения ──
  --    V2: reimbursement_account_rub = 0 по построению (REF_COST_MAP → MEMO).
  k.storage_rub,
  k.deduction_rub,
  k.acceptance_rub,
  k.penalty_account_rub,
  k.reimbursement_account_rub,
  k.other_account_rub,
  k.account_level_total_rub,

  -- ── РАЗЛОЖЕНИЕ УДЕРЖАНИЙ ПО ПРЯМЫМ МЕТКАМ WB (Stage 3.1F) ──
  d.ad_billing_reconstructed_rub,
  d.transit_deduction_rub,
  d.unclassified_deduction_rub,
  d.classification_conflict_rub,
  d.force_majeure_rub,
  d.utilization_rub,
  d.review_points_refund_rub,
  d.minimum_payment_adjustment_rub,
  -- V2: каноническое имя расхода по тарифной опции (решение владельца 2026-09-16:
  --   «Общий рейтинг по карточке», +0,75 %, минимальный платёж). Та же величина,
  --   что minimum_payment_adjustment_rub; прежняя колонка сохранена для совместимости.
  d.minimum_payment_adjustment_rub                        AS tariff_option_minimum_payment_rub,
  d.unknown_semantic_rub,
  (-d.force_majeure_rub)                                  AS exceptional_compensation_rub,
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
  --    Документы «WB Продвижение» исключаются: реклама входит в результат
  --    отдельной строкой (биллинг по дате услуги), документы — только сверка.
  (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub)
                                                          AS account_level_total_corrected_rub,

  -- ── РЕКЛАМА УПРАВЛЕНЧЕСКОГО P&L (FIN CONTRACT V2) ──
  IF(IFNULL(b.covered, FALSE), b.spend_rub, NULL)         AS ad_spend_financial_rub,
  IF(IFNULL(b.covered, FALSE), b.campaigns, NULL)         AS ad_billing_campaigns,
  -- Сверка контрактов: атрибуция SKU минус финансовый биллинг. НЕ статья затрат.
  IF(IFNULL(b.covered, FALSE) AND k.ad_spend_attributed_rub IS NOT NULL,
     k.ad_spend_attributed_rub - b.spend_rub, NULL)       AS ad_attribution_minus_billing_rub,

  -- ── ЭКОНОМИКА ──
  k.contribution_pre_cogs_rub,
  k.period_result_pre_cogs_rub,
  -- 🔴 FIN CONTRACT V2. contribution_pre_cogs_rub уже вычел АТРИБУЦИЮ рекламы
  --    (контракт SKU). Здесь атрибуция возвращается и вычитается биллинг по дате
  --    услуги — ровно один рекламный расход в результате. Документы WB
  --    Продвижения и форс-мажор исключены из уровня счёта, как в v1.
  IF(k.contribution_covered AND k.finance_covered AND IFNULL(b.covered, FALSE),
     k.contribution_pre_cogs_rub
       + k.ad_spend_attributed_rub
       - b.spend_rub
       - (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub),
     NULL)                                                AS period_result_pre_cogs_corrected_rub,
  IF(k.contribution_covered AND k.finance_covered AND IFNULL(b.covered, FALSE),
     -d.force_majeure_rub, NULL)                          AS exceptional_compensation_eligible_rub,

  -- ── РЕКЛАМНАЯ СВЕРКА (контрольные величины, в результат не входят) ──
  --    ad_spend_billed_rub — документы WB Продвижение в финотчёте (settlement).
  --    ad_spend_unallocated_rub — документы минус атрибуция (прежняя сверка).
  k.ad_spend_attributed_rub,
  d.ad_spend_billed_rub,
  d.ad_spend_unallocated_rub,
  d.ads_attribution_covered,

  -- ── Диагностика сверки (не метрики экрана) ──
  d.classifier_total_deduction_rub,
  d.recon_ad_spend_attributed_rub,

  'PRE_COGS_AD_BILLING_SERVICE_DATE_EXCL_EXCEPTIONAL'     AS economics_basis,
  'FIN CONTRACT V2 (2026-09-16). Результат после SKU-level и account-level расходов WB, но ДО себестоимости товара, FF, OPEX и налога. Не прибыль. Реклама = биллинг WB по дате оказания услуги (ad_spend_financial_rub); атрибуция статистики — контракт SKU Performance, в результат не входит. Документы «WB Продвижение» из финотчёта — сверка выплаты. Возмещения WB (перевозка, ПВЗ) — техническое перераспределение вознаграждения WB, эффект 0. Минимальный платёж по тарифной опции — расход. Форс-мажорные компенсации исключены (OPTION B) и публикуются отдельно.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM kpi k
LEFT JOIN ded d USING (day)
LEFT JOIN ads_bill b USING (day);

-- ── §4. ПРИЁМКА (fail-closed после публикации; при падении — откат по R1/R2) ─

-- 4.1 P&L-эффект MEMO-строк = 0 на всей истории.
ASSERT (SELECT IFNULL(SUM(ABS(cost_amount_positive)), 0) + COUNTIF(cost_amount_positive IS NULL)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE economic_direction = 'MEMO') = 0
  AS 'fin v2 §4.1: MEMO-строки имеют ненулевой или NULL P&L-эффект';

-- 4.2 Неизвестных денежных пар по-прежнему нет.
ASSERT (SELECT COUNTIF(cost_category IS NULL)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`) = 0
  AS 'fin v2 §4.2: появились денежные пары вне REF_COST_MAP';

-- 4.3 Грейн corrected-слоя: одна строка на сутки, 1:1 к KPI.
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT day)
          AND COUNT(*) = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
  AS 'fin v2 §4.3: грейн V_DASH_FINANCE_CORRECTED_DAILY нарушен (fanout или потеря суток)';
