-- ============================================================================
-- STAGE 3.1F — VALIDATION для классификации удержаний по прямым меткам WB
-- Дата: 2026-08-28.  Запускать ПОСЛЕ pr_deductions_direct_labels_v1.sql.
--
-- Проверки динамические: сверяются ОТНОШЕНИЯ и инварианты, а не снимок чисел.
-- Абсолютные величины контрольных окон живут в docs/, а не в ASSERT: FACT_*
-- штатно обновляются, и захардкоженный снимок ломал бы валидацию by design.
--
-- 🔴 F-9 — словарь прямых меток. Проверка живёт ЗДЕСЬ, в приёмке выката,
--   а не в самой вью: новая формулировка WB не должна ронять дашборд.
--   Классификатор в этом случае fail-closed отдаёт UNKNOWN_SEMANTIC, а
--   выкат останавливается и требует расширить словарь осознанно.
-- ============================================================================

-- ── F-1. Грейн не взорвался: строк ровно столько, сколько операций «Удержание». ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`)
       = (SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE` WHERE supplier_oper_name='Удержание')
) AS 'F-1 FAIL: грейн классификатора разошёлся с источником';

-- ── F-2. finance_row_key уникален. ──
ASSERT (
  SELECT COUNT(*) = 0 FROM (
    SELECT finance_row_key FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
    GROUP BY finance_row_key HAVING COUNT(*) > 1)
) AS 'F-2 FAIL: дубликаты finance_row_key в классификаторе';

-- ── F-3. Классифицированная сумма равна каноническому итогу до копейки. ──
ASSERT (
  SELECT ABS(
    (SELECT SUM(deduction_amount_rub) FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`)
  - (SELECT SUM(IFNULL(deduction,0) + IFNULL(additional_payment,0))
       FROM `wb_mart.FACT_FINANCE` WHERE supplier_oper_name='Удержание')) < 0.005
) AS 'F-3 FAIL: сумма классификатора != канонический итог удержаний';

-- ── F-4. Приоритет прямой метки. Непустой bonus_type_name НИКОГДА не
--         классифицируется эвристикой формы. Это ядро контракта DESIGN A. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE bonus_type_name IS NOT NULL
    AND deduction_class_source NOT IN ('DIRECT_WB_LABEL','UNKNOWN_DIRECT_LABEL')
) AS 'F-4 FAIL: строка с прямой меткой WB классифицирована эвристикой формы';

-- ── F-5. Обратное направление: пустая метка НЕ может дать прямой источник. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE bonus_type_name IS NULL
    AND deduction_class_source IN ('DIRECT_WB_LABEL','UNKNOWN_DIRECT_LABEL')
) AS 'F-5 FAIL: строка без метки помечена прямым источником';

-- ── F-6. Нераспознанная непустая метка НИКОГДА не становится рекламой. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE deduction_class_source = 'UNKNOWN_DIRECT_LABEL'
    AND deduction_class <> 'UNKNOWN_SEMANTIC'
) AS 'F-6 FAIL: нераспознанная прямая метка получила класс, отличный от UNKNOWN_SEMANTIC';

-- ── F-7. История не переписана: до первой метки WB классы прежние. ──
--         2024 и 2025 не содержат ни одной строки с прямым источником.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE finance_date < DATE '2026-07-19'
    AND deduction_class_source IN ('DIRECT_WB_LABEL','UNKNOWN_DIRECT_LABEL')
) AS 'F-7 FAIL: историческая строка получила прямую классификацию';

-- ── F-8. uuid36-популяция 2025 года остаётся неопознанной. Stage 3.1F её
--         не переинтерпретирует: прямых меток там нет ни одной. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE EXTRACT(YEAR FROM finance_date) = 2025
    AND REGEXP_CONTAINS(IFNULL(srid,''), r'^[0-9a-f]{8}-[0-9a-f-]{27}$')
    AND deduction_class <> 'UNCLASSIFIED_DEDUCTION'
) AS 'F-8 FAIL: uuid36-популяция 2025 переклассифицирована';

-- ── F-9. Словарь прямых меток полон для текущего корпуса. ──
--         Появление новой формулировки WB останавливает выкат.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  WHERE deduction_class = 'UNKNOWN_SEMANTIC'
) AS 'F-9 FAIL: WB прислал нераспознанное основание платежа — расширьте словарь Stage 3.1F осознанно';

-- ── F-10. Суточная сверка 1:1 к суточному разложению: части = целое. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_ADVERTISING_RECONCILIATION_DAILY`
  WHERE ABS(ad_spend_billed_rub + transit_deduction_rub + unclassified_deduction_rub
          + conflict_deduction_rub + force_majeure_rub + utilization_rub
          + review_points_refund_rub + minimum_payment_adjustment_rub
          + unknown_semantic_rub - total_deduction_rub) > 0.005
) AS 'F-10 FAIL: суточные части удержаний не складываются в итог';

-- ── F-11. Executive-слой 1:1 к KPI-слою. ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
       = (SELECT COUNT(*) FROM `wb_mart.V_DASH_KPI_DAILY`)
) AS 'F-11 FAIL: грейн V_DASH_FINANCE_CORRECTED_DAILY не 1:1 с KPI';

-- ── F-12. Разложение удержаний полно на каждых сутках с непустым итогом. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE deduction_rub IS NOT NULL AND NOT deduction_decomposition_complete
) AS 'F-12 FAIL: разложение удержаний не равно итогу операции WB';

-- ── F-13. OPTION B: форс-мажор НЕ улучшает операционный результат.
--          Операционный итог уровня счёта обязан быть равен предикатному
--          итогу минус реклама минус форс-мажор — символ в символ. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE account_level_total_corrected_rub IS DISTINCT FROM
        (account_level_total_rub - ad_billing_reconstructed_rub - force_majeure_rub)
) AS 'F-13 FAIL: операционный итог уровня счёта не соответствует контракту OPTION B';

-- ── F-14. Знак исключительной компенсации инвертирован ровно один раз. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE exceptional_compensation_rub IS DISTINCT FROM (-force_majeure_rub)
) AS 'F-14 FAIL: знак exceptional_compensation_rub не равен -force_majeure_rub';

-- ── F-15. Формула результата: after = contribution - операционный итог. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE period_result_eligible
    AND period_result_pre_cogs_corrected_rub IS DISTINCT FROM
        (contribution_pre_cogs_rub - account_level_total_corrected_rub)
) AS 'F-15 FAIL: операционный результат не равен contribution - account_level_corrected';

-- ── F-16. Реклама не вычитается дважды: исключена ровно она. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE period_result_eligible
    AND (period_result_pre_cogs_corrected_rub - period_result_pre_cogs_rub)
        IS DISTINCT FROM (ad_billing_reconstructed_rub + force_majeure_rub)
) AS 'F-16 FAIL: дельта корректировки != реклама + форс-мажор';

-- ── F-17. Числитель и знаменатель процента живут на одной маске. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
  WHERE (period_result_pre_cogs_corrected_rub IS NULL)
     <> (revenue_base_period_result_rub IS NULL)
) AS 'F-17 FAIL: числитель и знаменатель на разных масках';

-- ── F-18. Объекты Stage 3.1A / 3.1B / 3.1D не тронуты (структурный контроль). ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.TABLES`) = 4
     AND (SELECT COUNT(*) FROM `wb_mart.INFORMATION_SCHEMA.TABLES`)    = 41
     AND (SELECT COUNT(*) FROM `wb_raw.INFORMATION_SCHEMA.TABLES`)     = 56
) AS 'F-18 FAIL: состав датасетов отличается от ожидаемого (4 / 41 / 56)';

-- ── F-19. Ratio-колонок в слое нет (правило контракта дашборда v2). ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name IN ('V_DASH_FINANCE_CORRECTED_DAILY','V_ADVERTISING_RECONCILIATION_DAILY')
    AND (column_name LIKE '%_pct%' OR column_name LIKE '%margin%' OR column_name LIKE '%ratio%')
) AS 'F-19 FAIL: в слое появилась ratio-колонка';

-- ── F-20. Сырой settlement не тронут: выплата WB не может измениться. ──
--          Проверяется инвариантно: классификатор не участвует в её расчёте.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
  WHERE table_name = 'V_WB_DEDUCTIONS_CLASSIFIED'
    AND (view_definition LIKE '%for_pay%' OR view_definition LIKE '%cashback%')
) AS 'F-20 FAIL: классификатор ссылается на поля выплаты — риск влияния на settlement';
