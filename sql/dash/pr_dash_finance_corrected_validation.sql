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
--   Разница AFTER-BEFORE обязана равняться РОВНО исключённому биллингу.
--   Любое иное значение означало бы, что коррекция задела что-то ещё.
ASSERT (SELECT COUNTIF((period_result_pre_cogs_corrected_rub - period_result_pre_cogs_rub)
                       IS DISTINCT FROM ad_billing_reconstructed_rub)
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
        WHERE period_result_eligible) = 0
  AS 'C-7 FAIL: дельта AFTER-BEFORE не равна исключённому рекламному биллингу';

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
--   Проверка отношением: исправленный итог обязан содержать их целиком.
ASSERT (SELECT COUNTIF(account_level_total_corrected_rub
                       IS DISTINCT FROM (account_level_total_rub - ad_billing_reconstructed_rub))
        FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`) = 0
  AS 'C-12 FAIL: из account-level исключено не только AD_BILLING_RECONSTRUCTED';

-- ── C-13 (A8). Product COGS не участвует ──────────────────────────────────
--   Слой не должен ссылаться ни на один COGS-ОБЪЕКТ Stage 3.1B. Проверяются
--   именно имена объектов, а не подстрока 'COGS': она легитимно встречается
--   в economics_basis и в именах pre-COGS метрик самого слоя.
ASSERT (SELECT COUNTIF(REGEXP_CONTAINS(view_definition,
                r'V_FACT_FINANCE_COGS|V_MART_SKU_DAILY_COGS|V_PRODUCT_COGS_EFFECTIVE|REF_COGS'))
        FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
        WHERE table_name = 'V_DASH_FINANCE_CORRECTED_DAILY') = 0
  AS 'C-13 FAIL: слой ссылается на COGS-объекты — Product COGS подключён вне ACK';

-- ── C-14 (A9). Соседние слои не тронуты ──────────────────────────────────
ASSERT (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.TABLES`) = 4
  AS 'C-14 FAIL: изменился evetis_ref';
ASSERT (SELECT COUNT(*) FROM `wb_raw.INFORMATION_SCHEMA.TABLES`) = 56
  AS 'C-15 FAIL: изменился wb_raw';
ASSERT (SELECT COUNT(*) FROM `wb_mart.REF_COST_MAP`) = 19
  AS 'C-16 FAIL: изменился REF_COST_MAP';

-- ── C-17 (A12). Metabase не переключён: слой ещё никем не читается ────────
--   Ни один V_DASH_* не должен ссылаться на новый объект.
ASSERT (SELECT COUNTIF(REGEXP_CONTAINS(view_definition, r'V_DASH_FINANCE_CORRECTED_DAILY'))
        FROM `wb_mart.INFORMATION_SCHEMA.VIEWS`
        WHERE table_name != 'V_DASH_FINANCE_CORRECTED_DAILY') = 0
  AS 'C-17 FAIL: существующий V_DASH_* уже читает новый слой';

-- ── Итоговый снимок для отчёта (значения, а не гейты) ─────────────────────
--   Проценты считаются ratio-of-sums — в самом view их нет по решению O-1.
SELECT
  MIN(day) AS period_from, MAX(day) AS period_to,
  COUNT(*) AS days, SUM(period_result_eligible_day) AS eligible_days,
  SUM(contribution_pre_cogs_rub)                    AS contribution_pre_cogs_rub,
  SUM(deduction_rub)                                AS deduction_total_rub,
  SUM(ad_billing_reconstructed_rub)                 AS ad_billing_reconstructed_rub,
  SUM(other_wb_deductions_rub)                      AS other_wb_deductions_rub,
  SUM(account_level_total_rub)                      AS account_level_before_rub,
  SUM(account_level_total_corrected_rub)            AS account_level_corrected_rub,
  SUM(period_result_pre_cogs_rub)                   AS period_result_before_rub,
  SUM(period_result_pre_cogs_corrected_rub)         AS period_result_corrected_rub,
  SUM(revenue_base_period_result_rub)               AS revenue_base_aligned_rub,
  SAFE_DIVIDE(SUM(period_result_pre_cogs_corrected_rub),
              SUM(revenue_base_period_result_rub))  AS period_result_pre_cogs_margin_pct,
  SAFE_DIVIDE(SUM(contribution_pre_cogs_rub),
              SUM(revenue_base_contribution_rub))   AS contribution_pre_cogs_margin_pct,
  SUM(ad_spend_attributed_rub)                      AS ad_spend_attributed_rub,
  SUM(ad_spend_billed_rub)                          AS ad_spend_billed_rub,
  SUM(ad_spend_unallocated_rub)                     AS ad_spend_unallocated_rub
FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
WHERE day BETWEEN DATE '2026-08-01' AND DATE '2026-08-26';
