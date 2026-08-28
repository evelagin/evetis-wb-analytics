-- ============================================================================
-- STAGE 3.1G — VALIDATION для V_DASH_SETTLEMENT_DAILY
-- Дата: 2026-08-28.  Запускать ПОСЛЕ pr_dash_settlement_v1.sql.
-- Проверки динамические: сверяются инварианты, а не снимок чисел.
-- ============================================================================

-- ── G-1. Грейн: одна строка на финансовые сутки, дубликатов нет. ──
ASSERT (
  SELECT COUNT(*) = 0 FROM (
    SELECT day FROM `wb_mart.V_DASH_SETTLEMENT_DAILY` GROUP BY day HAVING COUNT(*) > 1)
) AS 'G-1 FAIL: дубликаты finance-суток в слое расчётов';

-- ── G-2. Универсум совпадает с финансовыми сутками canonical-слоя. ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`)
       = (SELECT COUNT(DISTINCT _rr_date) FROM `wb_raw.V_WB_FINANCE_SEMANTIC`)
) AS 'G-2 FAIL: универсум суток разошёлся с canonical-слоем финотчётов';

-- ── G-3. Мост сходится на КАЖДОЙ строке: goods + deductions = payout. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
  WHERE settlement_eligible
    AND ABS(settlement_goods_rub + post_realization_deductions_rub - wb_payout_rub) > 0.005
) AS 'G-3 FAIL: мост не сходится (goods + deductions != payout)';

-- ── G-4. Удержания после реализации выдаются ОТРИЦАТЕЛЬНЫМИ и равны
--         сумме компонентов со знаками источника. Единственная инверсия. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
  WHERE settlement_eligible
    AND ABS(post_realization_deductions_rub
            + (logistics_rub + storage_rub + deductions_rub + penalty_rub
             + additional_payment_rub + acceptance_rub + loyalty_points_rub)) > 0.005
) AS 'G-4 FAIL: post_realization_deductions_rub != -(сумма компонентов)';

-- ── G-5. Гейт закрытости: денежные поля существуют ровно там, где
--         сутки покрыты И закрыты. Открытая неделя не является кэшем. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
  WHERE settlement_eligible <> (wb_payout_rub IS NOT NULL)
) AS 'G-5 FAIL: денежные поля не совпадают с гейтом закрытых суток';

-- ── G-6. UNKNOWN != ZERO: вне гейта поля NULL, а не ноль. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
  WHERE NOT settlement_eligible
    AND (settlement_goods_rub IS NOT NULL
      OR post_realization_deductions_rub IS NOT NULL
      OR wb_payout_rub IS NOT NULL)
) AS 'G-6 FAIL: незакрытые сутки отдают значение вместо NULL';

-- ── G-7. Гейт заимствован, а не изобретён: он обязан совпадать
--         с finance_covered AND finance_is_final из KPI-слоя. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY` s
  LEFT JOIN `wb_mart.V_DASH_KPI_DAILY` k USING (day)
  WHERE s.settlement_eligible
     <> (IFNULL(k.finance_covered, FALSE) AND IFNULL(k.finance_is_final, FALSE))
) AS 'G-7 FAIL: гейт расчётов не совпадает с финансовым гейтом KPI-слоя';

-- ── G-8. Оба типа еженедельного отчёта присутствуют в универсуме. ──
ASSERT (
  SELECT SUM(finance_rows_type_main) > 0 AND SUM(finance_rows_type_buyouts) > 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
) AS 'G-8 FAIL: один из типов еженедельного отчёта отсутствует';

-- ── G-9. Строки разложены полностью. reportType WB передаёт с 2026-07-13,
--         у исторических строк он пуст — они учитываются отдельным счётчиком,
--         поэтому разложение обязано быть полным на ВСЕЙ истории. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_SETTLEMENT_DAILY`
  WHERE finance_rows_type_main + finance_rows_type_buyouts + finance_rows_type_unknown
        <> finance_rows
) AS 'G-9 FAIL: типы отчётов не покрывают все строки суток';

-- ── G-10. Слой независим от управленческой семантики: он не должен
--          ссылаться ни на COGS, ни на attributed-рекламу, ни на
--          seller-base выручку, ни на marketplace_fee. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
  WHERE table_name = 'V_DASH_SETTLEMENT_DAILY'
    AND (view_definition LIKE '%cogs%' OR view_definition LIKE '%COGS%'
      OR view_definition LIKE '%ad_spend%' OR view_definition LIKE '%marketplace_fee%'
      OR view_definition LIKE '%sales_revenue_seller_base%')
) AS 'G-10 FAIL: слой расчётов ссылается на управленческие метрики';

-- ── G-11. Ratio-колонок нет (правило контракта дашборда v2). ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name = 'V_DASH_SETTLEMENT_DAILY'
    AND (column_name LIKE '%_pct%' OR column_name LIKE '%margin%' OR column_name LIKE '%ratio%')
) AS 'G-11 FAIL: в слое расчётов появилась ratio-колонка';

-- ── G-12. Запрещённая терминология в именах колонок. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name = 'V_DASH_SETTLEMENT_DAILY'
    AND (column_name LIKE '%profit%' OR column_name LIKE '%received%'
      OR column_name LIKE '%cash_flow%' OR column_name LIKE '%bank%')
) AS 'G-12 FAIL: имя колонки заявляет больше, чем есть в данных';

-- ── G-13. Управленческие слои не тронуты (структурный контроль). ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.INFORMATION_SCHEMA.TABLES`)    = 42
     AND (SELECT COUNT(*) FROM `wb_raw.INFORMATION_SCHEMA.TABLES`)     = 56
     AND (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.TABLES`) = 4
) AS 'G-13 FAIL: состав датасетов отличается от ожидаемого (42 / 56 / 4)';

-- ── G-14. Грейн управленческих слоёв не изменился. ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_DASH_KPI_DAILY`)
       = (SELECT COUNT(*) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
     AND (SELECT COUNT(*) FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`)
       = (SELECT COUNT(*) FROM `wb_mart.V_DASH_KPI_DAILY`)
) AS 'G-14 FAIL: грейн управленческих слоёв Executive изменился';
