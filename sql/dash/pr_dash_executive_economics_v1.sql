-- ============================================================================
-- STAGE 3.1D — PRODUCT COGS -> EXECUTIVE ECONOMICS
-- Дата: 2026-08-28.  База: HEAD 32d230c9cdada219b0b6d6d3a3bce57cc9d03814.
-- Контракт: docs/STAGE3_1D_EXECUTIVE_PRODUCT_COGS_2026-08-28.md
-- Откат: tools/stage3_1d_executive_economics_rollback.sh [--dry-run]
--
-- ЗАЧЕМ. Executive показывает результат ДО себестоимости товара. Stage 3.1A дал
--   marketplace-independent reference-слой Product COGS, Stage 3.1B — consumer-слой,
--   но к управленческой экономике Executive себестоимость подключена не была.
--   Этот PR добавляет следующую ступень лестницы:
--
--     выручка
--       -> contribution_pre_cogs
--         -> period_result_pre_cogs_corrected      (Stage 3.1C PR2)
--           -> Product COGS                        (ЭТОТ PR)
--             -> period_result_after_product_cogs  (ЭТОТ PR)
--
-- 🔴 ЭТО НЕ ЧИСТАЯ ПРИБЫЛЬ. В показатель НЕ входят: fulfilment/FF costs, OPEX,
--   ЗП, аренда, банковские расходы и налог. Слова «чистая прибыль», «net profit»,
--   «финальная прибыль бизнеса», «gross margin», «EBITDA» к нему неприменимы.
--
-- 🔴 ВЫБОР CONSUMER (owner ACK). Executive использует OPERATIONAL-серию
--   V_MART_SKU_DAILY_COGS.net_product_cogs_operational_rub, потому что числитель
--   и знаменатель Executive построены на базе ВЫКУПОВ:
--     contribution_pre_cogs_rub    = SUM(hybrid_day_contribution_pre_cogs)
--     sales_revenue_seller_base_rub = SUM(buyouts_rub)
--   Себестоимость обязана относиться к ТЕМ ЖЕ реализованным единицам.
--   SETTLEMENT-серия (события FACT_FINANCE, полная история с 2024-09-07)
--   остаётся reconciliation/control и прямой статьёй расхода Executive НЕ является:
--   подстановка её под buyouts-выручку воспроизвела бы ровно тот дефект
--   рассогласования числителя и знаменателя, который чинил Stage 3.1C PR2.
--   Факт: lifetime operational 936 990,50 руб. против settlement 8 689 493 руб.,
--   расхождение на 586 из 722 суток. На контрольном окне 01-26.08.2026 обе серии
--   совпадают посуточно (0 расхождений), поэтому выбор не подгоняет числа.
--
-- 🔴 ГРЕЙН. Product COGS агрегируется до грейна `day` ДО join к суточному слою.
--   Прямой join day x nm_id к Executive daily запрещён: он размножил бы суточные
--   строки по числу SKU. Новый view — overlay 1:1 к V_DASH_FINANCE_CORRECTED_DAILY.
--
-- 🔴 UNKNOWN != ZERO. Суточный агрегат COGS выдаётся ТОЛЬКО если все строки
--   витрины этих суток разрешены (RESOLVED или NOT_APPLICABLE). Иначе NULL.
--   Ноль допустим единственным способом — как доказуемое отсутствие
--   COGS-требующих событий (NOT_APPLICABLE), и он доказуем счётчиками
--   product_cogs_units / cogs_rows. IFNULL(...,0) для маскировки неизвестного
--   COGS не применяется нигде.
--
-- ЧЕГО ЭТОТ PR НЕ ДЕЛАЕТ (owner ACK).
--   Не изменяет V_DASH_FINANCE_CORRECTED_DAILY, V_FACT_FINANCE_COGS,
--   V_MART_SKU_DAILY_COGS, объекты Stage 3.1A (evetis_ref), FACT_FINANCE,
--   MART_SKU_DAILY, REF_COST_MAP, ни одну существующую V_DASH_*.
--   Не переоткрывает семантику возврата Stage 3.1B (OD-1 = B: возврат сторнирует
--   Product COGS ИСХОДНОЙ продажи). Не пересчитывает bundle COGS: наборы
--   приходят только через существующий resolver evetis_ref.V_PRODUCT_COGS_EFFECTIVE.
--   Не переключает Metabase. Не подключает fulfilment/OPEX/налог.
--   Не начинает Stage 3B. Не трогает SKU Performance.
--
-- 🔴 RATIO-КОЛОНОК НЕТ — правило контракта дашборда v2. Процент считает
--   потребитель как ratio-of-sums:
--     SAFE_DIVIDE(SUM(period_result_after_product_cogs_rub),
--                 SUM(revenue_base_after_product_cogs_rub))
--   Посуточный процент суммировать нельзя, AVG(ratio) — другая величина.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/1 · V_DASH_EXECUTIVE_ECONOMICS_DAILY — грейн `day`, overlay 1:1 к
--       V_DASH_FINANCE_CORRECTED_DAILY. Строк ровно столько же; ни одна строка
--       этим view не создаётся и не удаляется.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
OPTIONS (description = 'Stage 3.1D. Executive economics after Product COGS, grain day, 1:1 overlay on V_DASH_FINANCE_CORRECTED_DAILY. Product COGS comes from V_MART_SKU_DAILY_COGS OPERATIONAL series (buyout units, reversal at original-sale cost per Stage 3.1B OD-1=B), aggregated to day BEFORE the join. Settlement COGS series is reconciliation/control only and is NOT an Executive expense series. Fail-closed: an unresolved COGS row on a day makes the whole day NULL, never zero. NOT net profit: fulfilment/FF cost, OPEX, payroll, bank fees and tax are NOT included.')
AS
WITH
-- Суточный агрегат Product COGS. Fail-closed выполняется ЗДЕСЬ, до join:
-- достаточно одной неразрешённой строки витрины, чтобы суточная величина
-- стала неизвестной. Счётчики выводятся всегда — они делают ноль доказуемым.
cogs_day AS (
  SELECT
    day,
    COUNT(*)                                                                   AS cogs_rows,
    COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE'))      AS cogs_unresolved_rows,
    SUM(buyouts_qty)                                                           AS product_cogs_units,
    SUM(reversal_events)                                                       AS product_cogs_reversal_events,
    SUM(reversal_resolved_events)                                              AS product_cogs_reversal_resolved_events,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(net_product_cogs_operational_rub),      NULL)                       AS product_cogs_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_operational_rub),          NULL)                       AS product_cogs_gross_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_reversal_operational_rub), NULL)                       AS product_cogs_reversal_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`
  GROUP BY day
)

SELECT
  f.day,

  -- ── PASS-THROUGH Stage 3.1C PR2. Значения не пересчитываются. ──
  f.period_result_eligible,
  f.contribution_pre_cogs_rub,
  f.sales_revenue_seller_base_rub,
  f.period_result_pre_cogs_corrected_rub,
  f.revenue_base_period_result_rub,

  -- ── PRODUCT COGS (operational). NULL = неизвестно, 0 = доказуемо нет событий ──
  d.product_cogs_rub,
  d.product_cogs_gross_rub,
  d.product_cogs_reversal_rub,
  IFNULL(d.product_cogs_units, 0)                          AS product_cogs_units,
  IFNULL(d.cogs_rows, 0)                                   AS cogs_rows,
  IFNULL(d.cogs_unresolved_rows, 0)                        AS cogs_unresolved_rows,
  IFNULL(d.product_cogs_reversal_events, 0)                AS product_cogs_reversal_events,
  IFNULL(d.product_cogs_reversal_resolved_events, 0)       AS product_cogs_reversal_resolved_events,

  -- ── ПОКРЫТИЕ. Сутки без строк витрины — НЕ покрыты (d.cogs_rows IS NULL),
  --    а не «ноль себестоимости». Fail-closed по умолчанию.
  (d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS product_cogs_covered,
  (f.period_result_eligible
   AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS after_product_cogs_eligible,

  -- ── ЭКОНОМИКА ПОСЛЕ СЕБЕСТОИМОСТИ ТОВАРА ──
  -- 🔴 Числитель и знаменатель живут на ОДНОЙ маске after_product_cogs_eligible.
  --    Разные маски — это дефект, который чинил Stage 3.1C PR2; повторять нельзя.
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.period_result_pre_cogs_corrected_rub - d.product_cogs_rub,
     NULL)                                                 AS period_result_after_product_cogs_rub,
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.revenue_base_period_result_rub,
     NULL)                                                 AS revenue_base_after_product_cogs_rub,

  'AFTER_PRODUCT_COGS'                                     AS economics_basis,
  'Результат после SKU-level и account-level расходов WB и после себестоимости товара (Product COGS). НЕ ВКЛЮЧЕНЫ: fulfilment/FF costs, OPEX, ЗП, аренда, банковские расходы и налог. Это НЕ чистая прибыль и НЕ валовая маржа. Product COGS взят по OPERATIONAL-серии (единицы выкупа), возврат сторнирует себестоимость исходной продажи.'
                                                           AS economics_note,
  CURRENT_TIMESTAMP()                                      AS generated_at

FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` f
LEFT JOIN cogs_day d USING (day);
