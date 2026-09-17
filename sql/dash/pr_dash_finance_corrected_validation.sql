-- 2026-09-17 EXECUTIVE V2 PHASE C3: C-7, C-12, C-14…C-17 приведены к действующему контракту
--    (FIN CONTRACT V2 от 2026-09-16 и материализованный слой C2 от 2026-09-17). Проверки не
--    ослаблены: формула v1 заменена формулой V2 с тем же точным равенством, снимки количества
--    объектов — инвариантами слоёв и закрытым списком потребителей. Все 17 проверок актуальны.
-- ============================================================================
-- STAGE 3.1C PR2 — ВАЛИДАЦИЯ (A1-A12 + strict non-regression)
-- Дата: 2026-08-27.  Сборка: sql/dash/pr_dash_finance_corrected_v1.sql
--
-- 🔴 ВСЕ ПРОВЕРКИ ДИНАМИЧЕСКИЕ. Ни одно значение снимка (87 471,25 ₽,
--   52 875,00 ₽, 26,98 %) в ASSERT НЕ зашито: FACT_* штатно обновляются
--   ежедневно, и снимок, ставший инвариантом, сломал бы прод на следующий
--   день. Проверяются ОТНОШЕНИЯ между величинами на текущем снимке.
--   Числа снимка живут только в docs/STAGE3_1C_PR2_EXECUTIVE_SEMANTICS_2026-08-27.md.
--
-- 🔴 Структурный baseline (wb_mart = 40) проверяется в
--   sql/ref/pr_ref_cogs_validation.sql, здесь не дублируется.
-- ============================================================================

-- ── C-1 / C-2. Грейн: overlay 1:1 к KPI-слою, day уникален ────────────────
ASSERT (SELECT COUNT(*) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
     = (SELECT COUNT(*) FROM `wb_mart.V_DASH_KPI_DAILY`)
  AS 'C-1 FAIL: число строк не равно V_DASH_KPI_DAILY — overlay стал superset';

ASSERT (SELECT COUNT(DISTINCT day) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
     = (SELECT COUNT(*)            FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
  AS 'C-2 FAIL: day не уникален — join дал fan-out';

-- ── C-3 (A1, A10). BEFORE-величины воспроизводятся символ в символ ────────
--   Слой ничего не «улучшает» в исходных числах: любая правка BEFORE была бы
--   молчаливым изменением действующего экрана.
ASSERT (SELECT COUNT(*)
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` v
        JOIN `wb_mart.V_DASH_KPI_DAILY` k USING (day)
        WHERE v.deduction_rub                 IS DISTINCT FROM k.deduction_rub
           OR v.account_level_total_rub       IS DISTINCT FROM k.account_level_total_rub
           OR v.contribution_pre_cogs_rub     IS DISTINCT FROM k.contribution_pre_cogs_rub
           OR v.period_result_pre_cogs_rub    IS DISTINCT FROM k.period_result_pre_cogs_rub
           OR v.ad_spend_attributed_rub       IS DISTINCT FROM k.ad_spend_attributed_rub
           OR v.sales_revenue_seller_base_rub IS DISTINCT FROM k.sales_revenue_seller_base_rub
           OR v.storage_rub                   IS DISTINCT FROM k.storage_rub
           OR v.penalty_account_rub           IS DISTINCT FROM k.penalty_account_rub
           OR v.acceptance_rub                IS DISTINCT FROM k.acceptance_rub
           OR v.reimbursement_account_rub     IS DISTINCT FROM k.reimbursement_account_rub
           OR v.other_account_rub             IS DISTINCT FROM k.other_account_rub) = 0
  AS 'C-3 FAIL: BEFORE-величины расходятся с V_DASH_KPI_DAILY';

-- ── C-4 (A6). Части складываются в целое. Ни один класс не исчезает ───────
ASSERT (SELECT COUNTIF(NOT deduction_decomposition_complete)
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
        WHERE deduction_rub IS NOT NULL) = 0
  AS 'C-4 FAIL: ad_billing + transit + unclassified + conflict != deduction_rub';

-- ── C-5. Fail-closed: разложение существует ровно там, где существует итог ─
ASSERT (SELECT COUNTIF((deduction_rub IS NULL) != (ad_billing_reconstructed_rub IS NULL))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-5 FAIL: NULL-маска разложения не совпала с маской итога (UNKNOWN стал 0)';

-- ── C-6 (D-1). Предикатная коррекция = перечислительной форме владельца ───
--   Доказывает, что вычитание из предикатного итога не «съело» ни одной
--   статьи уровня счёта и не потеряло знак компенсаций.
ASSERT (SELECT COUNTIF(account_level_total_corrected_rub IS DISTINCT FROM
                (storage_rub + penalty_account_rub + acceptance_rub + other_account_rub
                 + other_wb_deductions_rub + reimbursement_account_rub))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
        WHERE finance_covered) = 0
  AS 'C-6 FAIL: предикатная и перечислительная формы account-level итога разошлись';

-- ── C-7 (A7). Реклама не вычитается дважды и не вычитается трижды ─────────
--   FIN CONTRACT V2: результат = вклад + атрибуция (возврат) − биллинг по дате услуги
--   − уровень счёта без документов «WB Продвижение» и форс-мажора. Разница AFTER-BEFORE
--   обязана равняться РОВНО этим четырём слагаемым. Любое иное значение означало бы,
--   что коррекция задела что-то ещё. Точное равенство, как в v1.
ASSERT (SELECT COUNTIF((period_result_pre_cogs_corrected_rub - period_result_pre_cogs_rub)
                       IS DISTINCT FROM (ad_spend_attributed_rub - ad_spend_financial_rub
                                         + ad_billing_reconstructed_rub + force_majeure_rub))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
        WHERE period_result_eligible) = 0
  AS 'C-7 FAIL: дельта AFTER-BEFORE не равна (атрибуция − биллинг + документы WB Продвижение + форс-мажор)';

-- ── C-8 (A11). Числитель и знаменатель живут на одном множестве суток ─────
ASSERT (SELECT COUNTIF((period_result_pre_cogs_corrected_rub IS NULL)
                       != (revenue_base_period_result_rub IS NULL))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-8 FAIL: numerator/denominator period_result рассогласованы по покрытию';

ASSERT (SELECT COUNTIF((contribution_pre_cogs_rub IS NULL)
                       != (revenue_base_contribution_rub IS NULL))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-9 FAIL: numerator/denominator contribution рассогласованы по покрытию';

-- ── C-10. attributed не подменён и не раздвоился ──────────────────────────
ASSERT (SELECT COUNTIF(ad_spend_attributed_rub IS NOT NULL
                   AND recon_ad_spend_attributed_rub IS NOT NULL
                   AND ad_spend_attributed_rub <> recon_ad_spend_attributed_rub)
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-10 FAIL: attributed из KPI-слоя и из reconciliation-слоя разошлись';

-- ── C-11. unallocated вне покрытия атрибуции = NULL, а не 0 ───────────────
ASSERT (SELECT COUNTIF(NOT IFNULL(ads_attribution_covered, FALSE)
                   AND ad_spend_unallocated_rub IS NOT NULL)
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-11 FAIL: unallocated протёк за пределы окна покрытия атрибуции';

-- ── C-12 (A4, A5). Транзит и неклассифицированное ОСТАЮТСЯ расходом ───────
--   Проверка отношением: исправленный итог обязан содержать их целиком. С Stage 3.1F из
--   уровня счёта исключаются ровно две статьи: документы «WB Продвижение» и форс-мажор.
ASSERT (SELECT COUNTIF(account_level_total_corrected_rub
                       IS DISTINCT FROM (account_level_total_rub - ad_billing_reconstructed_rub - force_majeure_rub))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-12 FAIL: из account-level исключено что-то кроме документов WB Продвижение и форс-мажора';

-- ── C-13 (A8). Product COGS не участвует ──────────────────────────────────
--   Слой не должен ссылаться ни на один COGS-ОБЪЕКТ Stage 3.1B. Проверяются
--   именно имена объектов, а не подстрока 'COGS': она легитимно встречается
--   в economics_basis и в именах pre-COGS метрик самого слоя.
ASSERT (SELECT COUNTIF(REGEXP_CONTAINS(view_definition,
                r'V_FACT_FINANCE_COGS|V_MART_SKU_DAILY_COGS|V_PRODUCT_COGS_EFFECTIVE|REF_COGS'))
        FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
        WHERE table_name = 'V_DASH_FINANCE_CORRECTED_DAILY') = 0
  AS 'C-13 FAIL: слой ссылается на COGS-объекты — Product COGS подключён вне ACK';

-- ── C-14 (A9). Соседние слои не зависят от витрины ─────────────────────────
--   Снимок количества объектов (2026-08-27) заменён инвариантом направления слоёв:
--   evetis_ref — справочник, не читает wb_mart ни во view, ни в процедурах слоя расчётов.
--   (sp_ct_refresh_daily читает V_DASH_SKU_DAILY — утверждённый Control Tower, не этот слой.)
ASSERT (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.VIEWS`
        WHERE REGEXP_CONTAINS(view_definition, r'wb_mart\.')) = 0
   AND (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.ROUTINES`
        WHERE REGEXP_CONTAINS(routine_definition, r'V_DASH_(FINANCE_CORRECTED|EXECUTIVE_ECONOMICS|SETTLEMENT)_DAILY')) = 0
  AS 'C-14 FAIL: evetis_ref зависит от финансового слоя wb_mart';
ASSERT (SELECT COUNT(*) FROM `wb_raw.INFORMATION_SCHEMA.VIEWS`
        WHERE REGEXP_CONTAINS(view_definition, r'wb_mart\.')) = 0
   AND (SELECT COUNT(*) FROM `wb_raw.INFORMATION_SCHEMA.ROUTINES`
        WHERE REGEXP_CONTAINS(routine_definition, r'wb_mart\.')) = 0
  AS 'C-15 FAIL: wb_raw зависит от wb_mart (нарушено направление RAW → MART)';

-- ── C-16. REF_COST_MAP = утверждённое состояние FIN CONTRACT V2 ─────────────
--   21 строка, ключ (op_key, amount_field) уникален, отпечаток смысловых колонок (без note)
--   совпадает с принятым 2026-09-16. Любая правка классификатора — через ACK и обновление
--   отпечатка здесь (production уже правился мимо Git: см. REF_COST_MAP seed drift).
ASSERT (SELECT COUNT(*) = 21
           AND COUNT(DISTINCT CONCAT(op_key, '|', amount_field)) = 21
           AND FARM_FINGERPRINT(STRING_AGG(CONCAT(op_key, '|', amount_field, '|', economic_direction, '|',
                                                  cost_category, '|', CAST(field_normalization_sign AS STRING)),
                                           '#' ORDER BY op_key, amount_field)) = 7478614899318045374
        FROM `wb_mart.REF_COST_MAP`)
  AS 'C-16 FAIL: REF_COST_MAP отличается от утверждённого FIN CONTRACT V2';

-- ── C-17 (A12). Потребители слоя — только утверждённые ──────────────────────
--   С Stage 3.1D слой читает V_DASH_EXECUTIVE_ECONOMICS_DAILY, с Phase C2 —
--   sp_build_executive_v2_daily. Закрытый список: новый потребитель без ACK = FAIL.
ASSERT (SELECT ARRAY_TO_STRING(ARRAY_AGG(table_name ORDER BY table_name), ',')
        FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
        WHERE table_name != 'V_DASH_FINANCE_CORRECTED_DAILY'
          AND REGEXP_CONTAINS(view_definition, r'V_DASH_FINANCE_CORRECTED_DAILY')) = 'V_DASH_EXECUTIVE_ECONOMICS_DAILY'
   AND (SELECT ARRAY_TO_STRING(ARRAY_AGG(routine_name ORDER BY routine_name), ',')
        FROM `wb_mart.INFORMATION_SCHEMA.ROUTINES`
        WHERE REGEXP_CONTAINS(routine_definition, r'V_DASH_FINANCE_CORRECTED_DAILY')) = 'sp_build_executive_v2_daily'
  AS 'C-17 FAIL: слой читает неутверждённый потребитель (view или процедура)';
