-- ============================================================================
-- STAGE 3.1C PR2 — EXECUTIVE FINANCIAL SEMANTICS CORRECTION
-- Дата: 2026-08-27.  База: HEAD 9ada18b8a7a85bf407450f2337d4e931644f65fc.
-- Контракт: docs/STAGE3_1C_PR2_EXECUTIVE_SEMANTICS_2026-08-27.md
-- Откат: tools/stage3_1c_pr2_finance_corrected_rollback.sh [--dry-run]
--
-- ЗАЧЕМ. Рекламный расход вычитался в Executive ДВАЖДЫ:
--   1) как attributed advertising внутри contribution_pre_cogs_rub
--      (MART_SKU_DAILY.ad_spend, грейн день x nm_id);
--   2) повторно — как часть account-level операции WB «Удержание»
--      внутри account_level_total_rub, потому что 92,5 % этой операции
--      является рекламным биллингом (Stage 3.1C PR1).
--   Итог: period_result_pre_cogs_rub занижен на величину биллинга.
--
-- ЧТО ДЕЛАЕТ ЭТОТ PR. Создаёт ОДИН overlay-view поверх V_DASH_KPI_DAILY,
--   который рядом с исходными (BEFORE) величинами публикует исправленные:
--   из account-level итога исключается РОВНО класс AD_BILLING_RECONSTRUCTED,
--   и публикуются знаменатели процентов, выровненные по тому же
--   eligible-day universe, что и их числители.
--
-- ЧЕГО ЭТОТ PR НЕ ДЕЛАЕТ (owner ACK).
--   Не изменяет V_DASH_KPI_DAILY, V_DASH_SKU_DAILY, V_DASH_COVERAGE_DAILY,
--   FACT_*, MART_SKU_DAILY, REF_COST_MAP, evetis_ref, объекты Stage 3.1B
--   (V_FACT_FINANCE_COGS, V_MART_SKU_DAILY_COGS) и объекты Stage 3.1C PR1.
--   Не переключает Metabase: до отдельного ACK этот view никем не читается.
--   Не подключает Product COGS. Не начинает Stage 3B.
--
-- 🔴 OD-3. Исключается ТОЛЬКО AD_BILLING_RECONSTRUCTED. TRANSIT_DEDUCTION,
--   UNCLASSIFIED_DEDUCTION и CLASSIFICATION_CONFLICT остаются расходом.
--   uuid36-популяция 2025 года (794 732 ₽) НЕ переклассифицируется и НЕ
--   исключается: неизвестное остаётся расходом до доказательства природы.
--
-- 🔴 D-1 (owner ACK). Коррекция выполняется ВЫЧИТАНИЕМ из предикатного итога:
--       account_level_total_corrected_rub
--         = account_level_total_rub - ad_billing_reconstructed_rub
--   а НЕ пересборкой итога перечислением категорий. Действующий контракт
--   (dashboard_contract_v2.sql, CORRECTION-2) намеренно определяет уровень
--   счёта предикатом NOT is_sku_row: перечисление молча потеряло бы новую
--   категорию WB. Числовая эквивалентность двух форм проверена на всех
--   722 сутках — 0 расхождений (проверка C-6 в validation).
--
-- 🔴 O-1 (owner ACK). Ratio-полей в этом view НЕТ. Хранятся только
--   согласованные numerator и denominator; процент считает потребитель как
--   ratio-of-sums: SAFE_DIVIDE(SUM(numerator), SUM(denominator)).
--   Посуточный процент суммировать нельзя, AVG(ratio) — другая величина.
--
-- 🔴 UNKNOWN != 0. Ни одна NULL-величина не превращается в ноль. Разложение
--   удержаний существует ровно там, где существует их итог (finance_covered);
--   вне покрытия — NULL, а не ноль.
--
-- 🔴 ЭТО НЕ ПРИБЫЛЬ. period_result_pre_cogs_corrected_rub — результат после
--   SKU-level и account-level расходов, но ДО себестоимости товара, FF costs,
--   OPEX и налога. Слова profit / gross_margin / unit_profit в слое запрещены.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/1 · V_DASH_FINANCE_CORRECTED_DAILY — грейн `day`, overlay 1:1 к KPI-слою.
--
--   Overlay, а НЕ superset: строк ровно столько же, сколько в
--   V_DASH_KPI_DAILY, и ни одна из них не создаётся этим view.
--   Источники: V_DASH_KPI_DAILY (BEFORE-величины и гейты покрытия) и
--   V_ADVERTISING_RECONCILIATION_DAILY (разложение удержаний + биллинг).
--   Строковый уровень классификатора здесь НЕ читается: суточные суммы уже
--   агрегированы в PR1, второй агрегат того же источника создавал бы риск
--   расхождения двух «правд».
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` AS
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
  FROM `wb_mart.V_DASH_KPI_DAILY`
),

-- Разложение удержаний. Маска — deduction_rub: разложение обязано
-- существовать ровно там, где существует его итог, и отсутствовать там, где
-- итог неизвестен. IFNULL(...,0) применяется ТОЛЬКО внутри непустого итога:
-- сутки с финансовыми строками, но без удержаний, дают честный ноль.
ded AS (
  SELECT
    k.day,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.ad_spend_billed_rub,        NUMERIC '0')) AS ad_billing_reconstructed_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.transit_deduction_rub,      NUMERIC '0')) AS transit_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.unclassified_deduction_rub, NUMERIC '0')) AS unclassified_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.conflict_deduction_rub,     NUMERIC '0')) AS classification_conflict_rub,
    -- Контрольная величина классификатора (не маскируется): нужна валидации.
    r.total_deduction_rub                                                               AS classifier_total_deduction_rub,
    r.deduction_rows,
    -- Рекламная сверка живёт по СВОЕМУ покрытию (ads_attribution_covered),
    -- а не по finance_covered: это контроль биллинга, а не статья затрат.
    r.ads_attribution_covered,
    r.ad_spend_billed_rub,
    r.ad_spend_unallocated_rub,
    r.ad_spend_attributed_rub AS recon_ad_spend_attributed_rub,
    r.ad_billing_classification_confidence
  FROM kpi k
  LEFT JOIN `wb_mart.V_ADVERTISING_RECONCILIATION_DAILY` r ON r.day = k.day
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

  -- 🔴 ЕДИНЫЙ ELIGIBLE-DAY UNIVERSE (OD-7). Числитель и знаменатель процента
  --    обязаны жить на одном множестве суток. Дефект, который это чинит:
  --    period_result считался по 25 покрытым суткам, а выручка знаменателя
  --    бралась по 26 — процент выходил заниженным механически.
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

  -- ── РАЗЛОЖЕНИЕ УДЕРЖАНИЙ (Stage 3.1C PR1) ──
  -- 🔴 deduction_rub НЕ уменьшается и с экрана не исчезает: он остаётся
  --    полным итогом операции WB. Исключение работает только в расчёте.
  d.ad_billing_reconstructed_rub,
  d.transit_deduction_rub,
  d.unclassified_deduction_rub,
  d.classification_conflict_rub,
  ( d.transit_deduction_rub
  + d.unclassified_deduction_rub
  + d.classification_conflict_rub )                       AS other_wb_deductions_rub,
  d.deduction_rows,
  d.ad_billing_classification_confidence,
  -- Fail-closed индикатор: части обязаны складываться в целое.
  (d.ad_billing_reconstructed_rub
 + d.transit_deduction_rub
 + d.unclassified_deduction_rub
 + d.classification_conflict_rub = k.deduction_rub)       AS deduction_decomposition_complete,

  -- ── ИСПРАВЛЕННЫЙ УРОВЕНЬ СЧЁТА (OD-3 + D-1) ──
  -- Вычитание, а не перечисление. NULL распространяется естественно:
  -- если итог или разложение неизвестны, исправленный итог неизвестен тоже.
  (k.account_level_total_rub - d.ad_billing_reconstructed_rub)
                                                          AS account_level_total_corrected_rub,

  -- ── ЭКОНОМИКА ──
  k.contribution_pre_cogs_rub,
  k.period_result_pre_cogs_rub,
  -- 🔴 Гейт повторяет действующую формулу V_DASH_KPI_DAILY символ в символ,
  --    чтобы BEFORE и AFTER жили на одном множестве суток и разница между
  --    ними равнялась РОВНО исключённому рекламному биллингу.
  IF(k.contribution_covered AND k.finance_covered,
     k.contribution_pre_cogs_rub - (k.account_level_total_rub - d.ad_billing_reconstructed_rub),
     NULL)                                                AS period_result_pre_cogs_corrected_rub,

  -- ── РЕКЛАМНАЯ СВЕРКА (OD-5, Задача 3) ──
  -- 🔴 attributed НЕ подменяется billed. billed — контрольная величина
  --    уровня счёта, unallocated — разрыв сверки, а НЕ статья затрат:
  --    из period_result он не вычитается ни при каких условиях.
  --    Знак не скрывается: ни ABS, ни GREATEST(...,0).
  --    Вне покрытия атрибуции unallocated = NULL, а не ноль.
  k.ad_spend_attributed_rub,
  d.ad_spend_billed_rub,
  d.ad_spend_unallocated_rub,
  d.ads_attribution_covered,

  -- ── Диагностика сверки (не метрики экрана) ──
  d.classifier_total_deduction_rub,
  d.recon_ad_spend_attributed_rub,

  'PRE_COGS_AD_BILLING_CORRECTED'                         AS economics_basis,
  'Результат после SKU-level и account-level расходов, но ДО себестоимости товара, FF, OPEX и налога. Не прибыль. Рекламный биллинг исключён из account-level, потому что реклама уже вычтена как attributed внутри contribution_pre_cogs_rub.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM kpi k
LEFT JOIN ded d ON d.day = k.day;
