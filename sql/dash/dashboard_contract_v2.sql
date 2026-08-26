-- ============================================================================
-- Stage 4A · PR2 — Dashboard Contract v2. Три вью presentation-слоя.
-- Дата: 2026-08-20.  Контракт: docs/STAGE4A_PR2_DESIGN_2026-08-20.md.
-- Предшественник: PR1 (V_DASH_COVERAGE_DAILY, V_DASH_FRESHNESS_HEADER) — принят,
-- закрыт протоколом docs/STAGE4A_PR1_ROLLOUT_2026-08-20.md. PR1 НЕ изменяется.
--
-- ЧТО ЭТО. Первый экран управленческого дашборда: Executive (грейн сутки) и
-- SKU Analytics (грейн сутки × SKU), плюс свежесть В РАЗРЕЗЕ КОНТРАКТА.
--
-- 🔴 ЗАЧЕМ ВООБЩЕ НУЖНО НАСЛЕДОВАНИЕ ПОКРЫТИЯ (замер 20.08.2026)
--   SUM(hybrid_day_contribution_pre_cogs) по MART_SKU_DAILY:
--     где покрытие есть (128 суток)   +1 312 034,95 ₽
--     где покрытия нет  (587 суток)   −9 089 133,91 ₽
--     вся история без гейта           −7 777 098,96 ₽
--   Минус не означает убытка: комиссия и логистика идут с 2024-09, а выкупы —
--   только с 2026. Витрина честно вычитает расходы из выручки, которой в тех
--   сутках физически нет. Без гейта Executive показал бы минус 7,8 млн ₽.
--
-- ЧТО ЭТИ ТРИ ВЬЮ СОЗНАТЕЛЬНО НЕ ДЕЛАЮТ
--   1. Не меняют MART_SKU_DAILY, FACT_*, загрузчики, job'ы, Scheduler, heartbeat.
--   2. Не меняют объекты PR1 — только читают V_DASH_COVERAGE_DAILY.
--   3. Не пересчитывают покрытие. Все флаги — pass-through из PR1. Собственных
--      альтернативных coverage-флагов нет ни одного.
--   4. Не читают V_ADV_COSTS_SNAPSHOT / V_ADV_COSTS_DAY_COVERAGE / FACT_ADS_COSTS_DAILY
--      и не содержат биллинговых колонок: со Stage 3B.1 связи нет ни одной.
--   5. Не трогают V_ADS_SCREEN_SKU / V_ADS_SCREEN_QUERY / V_ADS_FUNNEL_* — у них
--      свой universe и своя приёмочная история; PR2 их не читает вовсе.
--   6. Не содержат НИ ОДНОЙ ratio-колонки. Все ratio — в Looker, SUM(a)/SUM(b).
--   7. Не содержат IFNULL(...,0) / COALESCE(...,0) для метрик.
--   8. Не разносят расходы уровня счёта по SKU никакой аллокацией.
--   9. Не вычисляют profit / margin / unit_profit — нет REF_COGS. Только *_pre_cogs.
--
-- 🔴 ЧЕТЫРЕ ВРЕМЕННЫЕ БАЗЫ В ОДНОЙ СТРОКЕ (контракт, не дефект)
--   ad_*                        — дата рекламной активности
--   orders_*, canceled_*        — дата заказа
--   buyouts_*, returns_*        — дата продажи/возврата
--   *_fee, logistics, storage,  — дата финансовой операции (расчёта)
--   deduction, net_settlement
--   Поэтому contribution_pre_cogs_rub — cross-base величина, а НЕ P&L суток.
--   Управленчески осмысленна на горизонте от недели.
--
-- 🔴 РЕШЕНИЯ ВЛАДЕЛЬЦА, РЕАЛИЗОВАННЫЕ ЗДЕСЬ
--   OPEN-1  PROVISIONAL финансы разрешены в контрибуции, но остаются наблюдаемыми
--           (finance_is_final, contains_provisional_finance, contribution_provisional_days).
--   OPEN-2  Свежесть по контракту — ОТДЕЛЬНАЯ вью, шапка PR1 не расширяется.
--   OPEN-3  sales_revenue_settled_rub строится из FACT_FINANCE СО ЗНАКОМ операции;
--           поведение MART (возврат прибавляется) не наследуется. MART не правится.
--   OPEN-4  «Коррекция продаж» вынесена в отдельный sales_adjustment_rub и в
--           settled revenue НЕ входит.
--
-- Откат: DROP VIEW трёх объектов. Существующие объекты не изменяются.
-- ============================================================================


-- ============================================================================
-- 1/3 · V_DASH_KPI_DAILY — Executive, грейн `day`.
--
-- 🔴 ЛЕВАЯ СТОРОНА — КАЛЕНДАРЬ ИЗ PR1, А НЕ ВИТРИНА. Если строить от
--    MART_SKU_DAILY, сутки без единой строки витрины исчезнут из отчёта, а с ними
--    исчезнет и признак «покрытия не было». Отсутствие суток нельзя показывать
--    отсутствием суток — его надо показывать явным NULL при covered = FALSE.
--
-- 🔴 РАСХОДЫ УРОВНЯ СЧЁТА НЕ СКРЫВАЮТСЯ. Хранение, удержания, приёмка и часть
--    штрафов приходят от WB БЕЗ привязки к SKU (is_sku_row = FALSE). Их нельзя
--    разнести по товарам — источник не даёт связки. Поэтому они живут здесь,
--    на бизнес-уровне, отдельными колонками, и НЕ появляются в V_DASH_SKU_DAILY.
--    Колонка other_account_rub — «всё остальное уровня счёта»: страховка от того,
--    что WB заведёт новую категорию, а она молча выпадет из отчёта.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_DASH_KPI_DAILY` AS
WITH
-- SKU-часть: агрегат витрины по суткам. Universe витрины = активные SKU справочника.
sku_day AS (
  SELECT
    day,
    SUM(orders_qty)                            AS orders_qty,
    SUM(orders_rub)                            AS orders_revenue_rub,
    SUM(canceled_qty)                          AS canceled_qty,
    SUM(canceled_rub)                          AS canceled_rub,
    SUM(buyouts_qty)                           AS buyouts_qty,
    SUM(buyouts_rub)                           AS sales_revenue_seller_base_rub,
    SUM(returns_qty)                           AS returns_qty,
    SUM(returns_rub)                           AS returns_rub,
    SUM(sales_for_pay_operational)             AS sales_for_pay_operational_rub,
    SUM(ad_spend)                              AS ad_spend_attributed_rub,
    SUM(views)                                 AS views,
    SUM(clicks)                                AS clicks,
    SUM(ad_orders_raw)                         AS ad_orders_raw,
    SUM(ads_revenue_raw_rub)                   AS ads_revenue_raw_rub,
    SUM(marketplace_fee_rub)                   AS marketplace_fee_rub,
    SUM(wb_reward_cost_positive)               AS wb_reward_rub,
    SUM(logistics_cost_positive)               AS logistics_rub,
    SUM(finance_for_pay_accounting)            AS net_settlement_rub,
    SUM(hybrid_day_contribution_pre_cogs)      AS contribution_pre_cogs_rub_src,
    SUM(settlement_day_contribution_pre_cogs)  AS settlement_contribution_pre_cogs_rub_src,
    COUNT(*)                                   AS mart_rows
  FROM `wb_mart.MART_SKU_DAILY`
  GROUP BY day
),

-- Universe витрины, воспроизведённый один в один: нужен, чтобы «сколько заплатил
-- покупатель» считалось по ТЕМ ЖЕ строкам, что и база продавца. Разные universe
-- у двух сестринских метрик выручки — прямая дорога к расхождению на экране.
ref_universe AS (
  SELECT DISTINCT nm_id FROM `wb_raw.REF_SKU_MASTER` WHERE active AND nm_id IS NOT NULL
),
buyer_paid_day AS (
  SELECT
    sale_date AS day,
    SUM(IF(NOT is_return, finished_price, -finished_price)) AS sales_revenue_buyer_paid_rub,
    COUNTIF(NOT is_return)                                  AS buyouts_qty_control
  FROM `wb_mart.FACT_SALES`
  WHERE nm_id IN (SELECT nm_id FROM ref_universe)
  GROUP BY sale_date
),

-- 🔴 CORRECTION-2 (найдено при реализации, замер 20.08.2026).
--   Первоначальный дизайн предполагал, что расходы делятся на две части: SKU-уровень
--   целиком лежит в витрине, всё остальное — уровень счёта. ЭТО НЕВЕРНО.
--   Замер по V_WB_FINANCE_AMOUNTS_LONG_MAPPED показал ТРИ группы, а не две:
--     1. SKU-уровень, который витрина несёт: logistics 2 718 375,42 + wb_reward 1 387 245,13
--     2. SKU-уровень, которого в витрине НЕТ ВООБЩЕ — 240 900,49 ₽:
--        acquiring 449 142,73 · loyalty 87,40 · penalty(SKU) 1 322,00 ·
--        reimbursement_logistics(SKU) −209 900,62. Процедура витрины забирает
--        из LONG_MAPPED только 'logistics' и 'wb_reward'.
--     3. SKU-уровень ВНЕ universe витрины — 14 711,02 ₽ на 184 строках:
--        nm_id, которых нет в активном справочнике.
--   Тождество замкнуто: 4 105 620,55 + 240 900,49 + 14 711,02 + 5 298 874,47
--   = 9 660 106,53 = SUM(LONG_MAPPED), остаток 0,00. Гейт D6.
--   Все три обязаны быть видимы: правило владельца — не скрывать деньги.
--   🔴 Группы 2 и 3 в контрибуцию НЕ входят. acquiring и wb_reward сидят ВНУТРИ
--      спреда (контракт v1 REV2.4, PR-B2 §4): вычесть их вторым разом нельзя.
--      Они выводятся как наблюдаемые компоненты, а не как расход в формуле.
--   Уровень счёта по-прежнему определяется ПРЕДИКАТОМ NOT is_sku_row, а не
--   перечислением категорий: перечисление молча потеряло бы новую категорию WB.
--   Замер подтвердил правоту предиката — logistics, wb_reward и acquiring имеют
--   строки на ОБОИХ уровнях (40 173,28 · 183,72 · 29,76 ₽ на уровне счёта).
fin_long_day AS (
  SELECT
    lm.finance_date AS day,
    -- 1. Уровень счёта: по SKU не разносится принципиально.
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category = 'storage',    lm.cost_amount_positive, 0)) AS storage_rub,
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category = 'deduction',  lm.cost_amount_positive, 0)) AS deduction_rub,
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category = 'acceptance', lm.cost_amount_positive, 0)) AS acceptance_rub,
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category = 'penalty',    lm.cost_amount_positive, 0)) AS penalty_account_rub,
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category IN ('reimbursement_logistics', 'reimbursement_pvz'),
           lm.cost_amount_positive, 0))                                                        AS reimbursement_account_rub,
    SUM(IF(NOT lm.is_sku_row AND lm.cost_category NOT IN ('storage', 'deduction', 'acceptance',
             'penalty', 'reimbursement_logistics', 'reimbursement_pvz'),
           lm.cost_amount_positive, 0))                                                        AS other_account_rub,
    SUM(IF(NOT lm.is_sku_row, lm.cost_amount_positive, 0))                                     AS account_level_total_rub,

    -- 2. SKU-уровень внутри universe витрины, которого витрина НЕ несёт.
    --    В контрибуцию не входят — только наблюдение.
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL AND lm.cost_category = 'acquiring',
           lm.cost_amount_positive, 0))                                                        AS acquiring_sku_rub,
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL AND lm.cost_category = 'loyalty',
           lm.cost_amount_positive, 0))                                                        AS loyalty_sku_rub,
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL AND lm.cost_category = 'penalty',
           lm.cost_amount_positive, 0))                                                        AS penalty_sku_rub,
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL AND lm.cost_category = 'reimbursement_logistics',
           lm.cost_amount_positive, 0))                                                        AS reimbursement_sku_rub,
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL
           AND lm.cost_category NOT IN ('logistics', 'wb_reward', 'acquiring', 'loyalty',
                                        'penalty', 'reimbursement_logistics'),
           lm.cost_amount_positive, 0))                                                        AS other_sku_rub,

    -- 3. Reconciliation-индикатор: SKU-расходы вне universe витрины.
    SUM(IF(lm.is_sku_row AND r.nm_id IS NULL, lm.cost_amount_positive, 0))                     AS sku_costs_outside_universe_rub,
    COUNTIF(lm.is_sku_row AND r.nm_id IS NULL)                                                 AS sku_rows_outside_universe,

    -- Контрольная величина для гейта «сумма частей = целое».
    SUM(lm.cost_amount_positive)                                                               AS finance_long_total_rub,
    SUM(IF(lm.is_sku_row AND r.nm_id IS NOT NULL AND lm.cost_category IN ('logistics', 'wb_reward'),
           lm.cost_amount_positive, 0))                                                        AS mart_carried_control_rub
  FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` lm
  LEFT JOIN ref_universe r ON r.nm_id = lm.nm_id
  WHERE lm.cost_amount_positive IS NOT NULL
  GROUP BY lm.finance_date
),

-- OPEN-3: реализация из финансов СО ЗНАКОМ операции.
--   Тождество, проверенное на всей истории: retail_price_withdisc_rub
--   = marketplace_fee_gap_rub + finance_for_pay_accounting (fee_gap = srev − for_pay).
--   Поэтому база продавца восстанавливается из FACT_FINANCE без обращения к
--   V_WB_FINANCE_SEMANTIC (STRING-деньги, 47 МБ на запрос).
-- 🔴 Продажа → «+», Возврат → «−». Поведение MART (возврат ПРИБАВЛЯЕТСЯ, потому что
--   finpay-CTE витрины суммирует обе операции без знака) здесь НЕ наследуется.
--   Сам MART в PR2 не правится — расхождение измеряется гейтом D-SETTLED.
-- OPEN-4: «Коррекция продаж» — отдельная метрика. У этих строк fee_gap пуст,
--   базы выручки нет, поэтому в settled revenue они не входят и выручкой не считаются.
settled_day AS (
  SELECT
    finance_date AS day,
    SUM(CASE supplier_oper_name
          WHEN 'Продажа' THEN  (marketplace_fee_gap_rub + finance_for_pay_accounting)
          WHEN 'Возврат' THEN -(marketplace_fee_gap_rub + finance_for_pay_accounting)
        END)                                                             AS sales_revenue_settled_rub,
    SUM(IF(supplier_oper_name = 'Коррекция продаж', finance_for_pay_accounting, NULL))
                                                                         AS sales_adjustment_rub,
    COUNTIF(supplier_oper_name = 'Продажа')                              AS settled_sale_rows,
    COUNTIF(supplier_oper_name = 'Возврат')                              AS settled_return_rows,
    COUNTIF(supplier_oper_name = 'Коррекция продаж')                     AS settled_adjustment_rows
  FROM `wb_mart.FACT_FINANCE`
  WHERE supplier_oper_name IN ('Продажа', 'Возврат', 'Коррекция продаж')
  GROUP BY finance_date
)

SELECT
  c.day,

  -- ── Покрытие и свежесть: PASS-THROUGH из PR1. Ничего не пересчитывается. ──
  c.orders_covered,
  c.sales_covered,
  c.ads_covered,
  c.finance_covered,
  c.stocks_covered,
  c.contribution_covered,
  c.finance_is_final,
  c.contains_provisional_finance,
  c.is_current_day,
  c.bounds_complete,
  c.orders_coverage_start,
  c.sales_coverage_start,
  c.ads_coverage_start,
  c.finance_coverage_start,
  c.finance_last_final_date,
  c.mart_build_as_of_date,
  -- Граница СБОРКИ витрины — это не покрытие источника, а окно построения.
  -- Нужна, чтобы отличить «данных нет» от «витрина ещё не собрала эти сутки».
  (c.day <= c.mart_build_as_of_date)                    AS mart_build_covered,
  (s.day IS NOT NULL)                                   AS has_mart_row,

  -- ── Аддитивные счётчики для произвольного Date Range (тоже из PR1) ──
  c.days_total,
  c.orders_uncovered_days,
  c.sales_uncovered_days,
  c.ads_uncovered_days,
  c.finance_uncovered_days,
  c.contribution_uncovered_days,
  c.provisional_finance_days,
  c.contribution_provisional_days,

  -- ── ТОРГОВЛЯ ──
  IF(c.orders_covered, s.orders_qty,                     NULL) AS orders_qty,
  IF(c.orders_covered, s.orders_revenue_rub,             NULL) AS orders_revenue_rub,
  IF(c.orders_covered, s.canceled_qty,                   NULL) AS canceled_qty,
  IF(c.orders_covered, s.canceled_rub,                   NULL) AS canceled_rub,
  IF(c.sales_covered,  s.buyouts_qty,                    NULL) AS buyouts_qty,
  IF(c.sales_covered,  s.sales_revenue_seller_base_rub,  NULL) AS sales_revenue_seller_base_rub,
  IF(c.sales_covered,  b.sales_revenue_buyer_paid_rub,   NULL) AS sales_revenue_buyer_paid_rub,
  IF(c.sales_covered,  s.returns_qty,                    NULL) AS returns_qty,
  IF(c.sales_covered,  s.returns_rub,                    NULL) AS returns_rub,
  IF(c.sales_covered,  s.sales_for_pay_operational_rub,  NULL) AS sales_for_pay_operational_rub,

  -- ── РЕКЛАМА: только атрибуция. Биллинг в PR2 отсутствует (Stage 3B.1). ──
  IF(c.ads_covered, s.ad_spend_attributed_rub, NULL) AS ad_spend_attributed_rub,
  IF(c.ads_covered, s.views,                   NULL) AS views,
  IF(c.ads_covered, s.clicks,                  NULL) AS clicks,
  IF(c.ads_covered, s.ad_orders_raw,           NULL) AS ad_orders_raw,
  IF(c.ads_covered, s.ads_revenue_raw_rub,     NULL) AS ads_revenue_raw_rub,

  -- ── РАСХОДЫ УРОВНЯ SKU (дата расчёта) ──
  IF(c.finance_covered, s.marketplace_fee_rub, NULL) AS marketplace_fee_rub,
  IF(c.finance_covered, s.wb_reward_rub,       NULL) AS wb_reward_rub,
  IF(c.finance_covered, s.logistics_rub,       NULL) AS logistics_rub,

  -- ── РАСХОДЫ УРОВНЯ СЧЁТА (по SKU не разносятся принципиально) ──
  IF(c.finance_covered, a.storage_rub,               NULL) AS storage_rub,
  IF(c.finance_covered, a.deduction_rub,             NULL) AS deduction_rub,
  IF(c.finance_covered, a.acceptance_rub,            NULL) AS acceptance_rub,
  IF(c.finance_covered, a.penalty_account_rub,       NULL) AS penalty_account_rub,
  IF(c.finance_covered, a.reimbursement_account_rub, NULL) AS reimbursement_account_rub,
  IF(c.finance_covered, a.other_account_rub,         NULL) AS other_account_rub,
  IF(c.finance_covered, a.account_level_total_rub,   NULL) AS account_level_total_rub,

  -- ── SKU-УРОВЕНЬ, КОТОРОГО НЕТ В ВИТРИНЕ (CORRECTION-2) ──
  -- 🔴 В контрибуцию НЕ входят: acquiring и wb_reward сидят внутри спреда,
  --    вычитать их вторым разом нельзя (контракт v1 REV2.4 / PR-B2 §4).
  IF(c.finance_covered, a.acquiring_sku_rub,     NULL) AS acquiring_sku_rub,
  IF(c.finance_covered, a.loyalty_sku_rub,       NULL) AS loyalty_sku_rub,
  IF(c.finance_covered, a.penalty_sku_rub,       NULL) AS penalty_sku_rub,
  IF(c.finance_covered, a.reimbursement_sku_rub, NULL) AS reimbursement_sku_rub,
  IF(c.finance_covered, a.other_sku_rub,         NULL) AS other_sku_rub,

  -- ── RECONCILIATION: SKU universe ↔ финансовый universe ──
  IF(c.finance_covered, a.sku_costs_outside_universe_rub, NULL) AS sku_costs_outside_universe_rub,
  a.sku_rows_outside_universe,
  a.finance_long_total_rub,
  a.mart_carried_control_rub,

  -- ── РАСЧЁТ ──
  IF(c.finance_covered, s.net_settlement_rub,        NULL) AS net_settlement_rub,
  IF(c.finance_covered, f.sales_revenue_settled_rub, NULL) AS sales_revenue_settled_rub,
  IF(c.finance_covered, f.sales_adjustment_rub,      NULL) AS sales_adjustment_rub,

  -- ── ЭКОНОМИКА, pre-COGS. NULL, если хоть один компонент неизвестен. ──
  IF(c.contribution_covered, s.contribution_pre_cogs_rub_src,            NULL) AS contribution_pre_cogs_rub,
  IF(c.contribution_covered, s.settlement_contribution_pre_cogs_rub_src, NULL) AS settlement_contribution_pre_cogs_rub,
  'PRE_COGS'                                                                  AS economics_basis,
  'REF_COGS отсутствует в BigQuery: это вклад до себестоимости, не прибыль и не маржа'
                                                                              AS economics_note,

  -- ── Диагностика сверки (не метрики экрана) ──
  s.mart_rows,
  b.buyouts_qty_control,
  f.settled_sale_rows,
  f.settled_return_rows,
  f.settled_adjustment_rows,

  CURRENT_TIMESTAMP() AS generated_at

FROM `wb_mart.V_DASH_COVERAGE_DAILY` c
LEFT JOIN sku_day        s ON s.day = c.day
LEFT JOIN buyer_paid_day b ON b.day = c.day
LEFT JOIN fin_long_day   a ON a.day = c.day
LEFT JOIN settled_day    f ON f.day = c.day;


-- ============================================================================
-- 2/3 · V_DASH_SKU_DAILY — SKU Analytics, грейн `day × nm_id`.
--
-- 🔴 СТРОГИЙ PASS-THROUGH ГРЕЙНА ВИТРИНЫ. Ни одной строки больше, ни одной меньше.
--    Справочник сворачивается до одной строки на nm_id ДО join (приём из
--    V_ADS_SCREEN_SKU) — fan-out структурно невозможен, а ref_rows_for_nm_id
--    оставляет дубль справочника видимым, вместо того чтобы его прятать.
--
-- 🔴 РАСХОДОВ УРОВНЯ СЧЁТА ЗДЕСЬ НЕТ И НЕ БУДЕТ. Хранение, удержания и приёмка
--    приходят без привязки к SKU. Любая «аллокация пропорционально выручке» была бы
--    выдуманным числом. Их место — V_DASH_KPI_DAILY, бизнес-уровень.
--    account_level_excluded_note делает это решение видимым в самих данных.
--
-- 🔴 Ratio-колонок нет. drr_*, roas, acos, ctr, cpc, cpm, cpo, blended_cpo и все
--    *_7d/_14d из витрины НЕ переносятся: посуточное отношение за произвольный
--    диапазон можно только усреднить, а AVG(ratio) — другая величина.
--    Замер 20.08 на покрытом окне: ratio-of-sums 15,37 % против AVG 15,77 %.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_DASH_SKU_DAILY` AS
WITH
ref1 AS (
  SELECT
    nm_id,
    ANY_VALUE(internal_sku)        AS internal_sku,
    ANY_VALUE(product_name_short)  AS product_name_short,
    ANY_VALUE(product_name_full)   AS product_name_full,
    ANY_VALUE(category)            AS category,
    ANY_VALUE(line)                AS line,
    ANY_VALUE(product_type)        AS product_type,
    ANY_VALUE(brand)               AS brand,
    ANY_VALUE(is_bundle)           AS is_bundle,
    ANY_VALUE(status)              AS sku_status,
    ANY_VALUE(active)              AS sku_active,
    ANY_VALUE(include_in_pnl)      AS include_in_pnl,
    ANY_VALUE(volume_ml)           AS volume_ml,
    COUNT(*)                       AS ref_rows_for_nm_id
  FROM `wb_raw.REF_SKU_MASTER`
  WHERE nm_id IS NOT NULL
  GROUP BY nm_id
)
SELECT
  m.day,
  m.nm_id,

  -- ── Измерения ──
  r.internal_sku,
  r.product_name_short,
  r.product_name_full,
  r.category,
  r.line,
  r.product_type,
  r.brand,
  r.is_bundle,
  r.sku_status,
  r.sku_active,
  r.include_in_pnl,
  r.volume_ml,
  IFNULL(r.ref_rows_for_nm_id, 0) AS ref_rows_for_nm_id,   -- счётчик строк справочника, НЕ метрика
  (r.nm_id IS NULL)               AS is_orphan,

  -- ── Покрытие: PASS-THROUGH из PR1 ──
  c.orders_covered,
  c.sales_covered,
  c.ads_covered,
  c.finance_covered,
  c.contribution_covered,
  c.finance_is_final,
  c.contains_provisional_finance,
  c.days_total,
  c.orders_uncovered_days,
  c.sales_uncovered_days,
  c.ads_uncovered_days,
  c.finance_uncovered_days,
  c.contribution_uncovered_days,
  c.provisional_finance_days,
  c.contribution_provisional_days,

  -- ── SALES ──
  IF(c.orders_covered, m.orders_qty,    NULL) AS orders_qty,
  IF(c.orders_covered, m.orders_rub,    NULL) AS orders_revenue_rub,
  IF(c.orders_covered, m.canceled_qty,  NULL) AS canceled_qty,
  IF(c.orders_covered, m.canceled_rub,  NULL) AS canceled_rub,

  -- ── BUYOUTS ──
  IF(c.sales_covered, m.buyouts_qty,                NULL) AS buyouts_qty,
  IF(c.sales_covered, m.buyouts_rub,                NULL) AS sales_revenue_seller_base_rub,
  IF(c.sales_covered, m.returns_qty,                NULL) AS returns_qty,
  IF(c.sales_covered, m.returns_rub,                NULL) AS returns_rub,
  IF(c.sales_covered, m.sales_for_pay_operational,  NULL) AS sales_for_pay_operational_rub,

  -- ── ECONOMICS (уровень SKU; расходов уровня счёта здесь нет) ──
  IF(c.finance_covered, m.marketplace_fee_rub,          NULL) AS marketplace_fee_rub,
  IF(c.finance_covered, m.wb_reward_cost_positive,      NULL) AS wb_reward_rub,
  IF(c.finance_covered, m.logistics_cost_positive,      NULL) AS logistics_rub,
  IF(c.finance_covered, m.finance_for_pay_accounting,   NULL) AS net_settlement_rub,

  -- ── ADVERTISING: только атрибуция ──
  IF(c.ads_covered, m.ad_spend,                      NULL) AS ad_spend_attributed_rub,
  IF(c.ads_covered, m.views,                         NULL) AS views,
  IF(c.ads_covered, m.clicks,                        NULL) AS clicks,
  IF(c.ads_covered, m.ad_orders_raw,                 NULL) AS ad_orders_raw,
  IF(c.ads_covered, m.ads_revenue_raw_rub,           NULL) AS ads_revenue_raw_rub,
  IF(c.ads_covered, m.ads_revenue_dedup_estimate_rub, NULL) AS ads_revenue_dedup_estimate_rub,
  IF(c.ads_covered, m.ad_orders_dedup_estimate,      NULL) AS ad_orders_dedup_estimate,

  -- ── CONTRIBUTION, pre-COGS ──
  IF(c.contribution_covered, m.hybrid_day_contribution_pre_cogs,     NULL) AS contribution_pre_cogs_rub,
  IF(c.contribution_covered, m.settlement_day_contribution_pre_cogs, NULL) AS settlement_contribution_pre_cogs_rub,
  'PRE_COGS' AS economics_basis,
  'REF_COGS отсутствует: вклад до себестоимости, не прибыль и не маржа' AS economics_note,
  'storage / deduction / acceptance приходят от WB без привязки к SKU (is_sku_row = FALSE) и находятся только в V_DASH_KPI_DAILY'
             AS account_level_excluded_note,

  m.build_as_of_date,
  m.built_at AS mart_built_at,
  CURRENT_TIMESTAMP() AS generated_at

FROM `wb_mart.MART_SKU_DAILY` m
LEFT JOIN ref1 r                          ON r.nm_id = m.nm_id
LEFT JOIN `wb_mart.V_DASH_COVERAGE_DAILY` c ON c.day  = m.day;


-- ============================================================================
-- 3/3 · V_DASH_FRESHNESS_BY_CONTRACT — свежесть В РАЗРЕЗЕ КОНТРАКТА (OBSERVATION-1).
--
-- 🔴 ЗАЧЕМ. V_DASH_FRESHNESS_HEADER даёт worst-case по ВСЕМУ контуру: сегодня он
--    говорит «данные актуальны на 17.08», и эту дату задаёт ads_costs — рекламный
--    биллинг, который на экран не выводится вовсе (Stage 3B заблокирован).
--    Слой, не участвующий в KPI, не должен старить пользовательские показатели.
--
-- 🔴 ЧЕМ ЭТО НЕ ЯВЛЯЕТСЯ. Заменой системного минимума. V_DASH_FRESHNESS_HEADER
--    остаётся как есть и НЕ расширяется (решение владельца OPEN-2, вариант A).
--    Два значения имеют разный смысл и выводятся на экран рядом.
--
-- 🔴 КАРТА «КОНТРАКТ → СЛОИ» ЖИВЁТ ЗДЕСЬ, В SQL. Она попадает в view_definition,
--    проверяется K9 и гейтами. Никакой фильтрации required layers в Looker:
--    интерфейс читает готовую строку, а не собирает список сам.
--    PR3–PR5 добавляют СТРОКИ в эту карту, а не колонки в вью.
--
-- 🔴 СВЕЖЕСТЬ ≠ ПОКРЫТИЕ. data_as_of_min_used отвечает «насколько свежи данные
--    контракта», V_DASH_COVERAGE_DAILY — «за какие сутки они вообще есть».
--
-- 🔴 ДВА FAIL-CLOSED ПРАВИЛА
--    · Слой без data_as_of (измерения и QC: ref_sku_master, sku_orphans) в минимум
--      НЕ входит — но обязан быть НАЗВАН в layers_used_without_date_list.
--      Иначе он либо испортит дату, либо исчезнет молча; оба варианта неприемлемы.
--    · Слой, объявленный в карте, но отсутствующий в V_DATA_FRESHNESS, получает
--      статус 'UNKNOWN' с наивысшим приоритетом и попадает в
--      layers_declared_but_absent_list. Опечатка в карте обязана краснеть.
--
-- Нормализация статуса и приоритет — те же, что в PR1 (FIX-1): IFNULL(status,'UNKNOWN'),
-- OK < STALE < ERROR < прочее. Статус НЕ пересчитывается, берётся готовый.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT` AS
WITH
-- КАРТА КОНТРАКТОВ. Единственное место, где она определена.
contract_layers AS (
  SELECT 'KPI_DAILY' AS contract_code, layer_code
  FROM UNNEST(['mart_sku_daily', 'finance', 'orders', 'sales', 'fact_ads_sku_daily']) AS layer_code
  UNION ALL
  SELECT 'SKU_DAILY', layer_code
  FROM UNNEST(['mart_sku_daily', 'finance', 'orders', 'sales', 'fact_ads_sku_daily',
               'ref_sku_master']) AS layer_code
),
src AS (
  SELECT
    layer_code,
    data_as_of,
    success_age_is_sla,
    IFNULL(status, 'UNKNOWN') AS status,
    CASE IFNULL(status, 'UNKNOWN')
      WHEN 'OK' THEN 0 WHEN 'STALE' THEN 1 WHEN 'ERROR' THEN 2 ELSE 3 END AS status_prio
  FROM `wb_mart.V_DATA_FRESHNESS`
),
j AS (
  SELECT
    cl.contract_code,
    cl.layer_code,
    s.data_as_of,
    s.success_age_is_sla,
    IFNULL(s.status, 'UNKNOWN')  AS status,
    IFNULL(s.status_prio, 3)     AS status_prio,
    (s.layer_code IS NULL)       AS layer_absent
  FROM contract_layers cl
  LEFT JOIN src s ON s.layer_code = cl.layer_code
)
SELECT
  contract_code,
  COUNT(*)                                                                  AS layers_used_count,
  STRING_AGG(layer_code, ', ' ORDER BY layer_code)                          AS layers_used_list,
  MIN(data_as_of)                                                           AS data_as_of_min_used,
  MAX(data_as_of)                                                           AS data_as_of_max_used,
  ARRAY_AGG(status ORDER BY status_prio DESC, layer_code LIMIT 1)[OFFSET(0)] AS worst_status_used,
  COUNTIF(data_as_of IS NULL)                                               AS layers_used_without_date_count,
  STRING_AGG(IF(data_as_of IS NULL, layer_code, NULL), ', ' ORDER BY layer_code)
                                                                            AS layers_used_without_date_list,
  COUNTIF(success_age_is_sla IS NOT TRUE)                                   AS layers_without_sla_used_count,
  STRING_AGG(IF(success_age_is_sla IS NOT TRUE, layer_code, NULL), ', ' ORDER BY layer_code)
                                                                            AS layers_without_sla_used_list,
  COUNTIF(layer_absent)                                                     AS layers_declared_but_absent_count,
  STRING_AGG(IF(layer_absent, layer_code, NULL), ', ' ORDER BY layer_code)  AS layers_declared_but_absent_list,
  FORMAT('Данные контракта %s актуальны на %s',
         contract_code,
         COALESCE(FORMAT_DATE('%d.%m.%Y', MIN(data_as_of)), '—'))           AS header_text_used,
  CURRENT_TIMESTAMP()                                                       AS generated_at
FROM j
GROUP BY contract_code;
