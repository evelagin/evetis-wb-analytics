-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL (VIEW)
-- Git-first object (Gate 8, 2026-09-21): not in production until deployed. Rules:
-- sql/current/README.md. Metadata: MANIFEST.json.
--
-- ОПЕРАЦИОННАЯ (ПРОВИЗОРНАЯ) ЭКОНОМИКА Ozon. Надстройка над FCT_OZON_SKU_PNL_DAILY, которая
-- НЕ ТРОГАЕТ факт: все канонические поля проходят насквозь неизменными, а оценки лежат в
-- СВОИХ колонках рядом. Факт и оценка остаются раздельно аудируемыми в одной строке.
--
-- ЗАЧЕМ. Ozon публикует финансовую операцию ровно через 14 суток после её даты, а сама
-- операция возникает через 4-25 суток после заказа: продажа становится видимой в среднем
-- через 18 суток, максимум 36 (замер Gate 8). До этого момента комиссия и логистика в факте
-- равны нулю — не потому, что их не будет, а потому, что их ещё не напечатали. Лист показывал
-- за эти сутки мнимую прибыль: выручка есть, расходов нет.
--
-- ПРАВИЛО СТАРШИНСТВА, покомпонентно (D):
--   есть факт                                  -> effective = факт,   state = ACTUAL
--   расход ожидается и оценщик доказан         -> effective = оценка, state = ESTIMATED
--   доказано, что расхода не существует        -> effective = 0,      state = NOT_APPLICABLE
--   иначе                                      -> effective = NULL,   state = UNKNOWN
-- Ноль НИКОГДА не ставится только потому, что Ozon ещё не прислал расход.
--
-- КОМИССИЯ. Оценка не статистическая: (price_rub - payout_rub) x quantity из отчёта по
-- отправлениям. Тождество проверено на 600 урегулированных отправлениях из 600 — расхождение
-- 0,00 ₽. Ozon сообщает будущую выплату в момент отправления, то есть комиссия известна
-- ТОЧНО задолго до финансового документа. Состояние всё равно ESTIMATED: источник — отчёт по
-- отправлениям, а не финансовый документ, и подмена факта оценкой запрещена. Резервный
-- источник, если payout отсутствует, — тариф из V_OZON_COMMISSION_POLICY.
-- У ВЫКУПОВ СНГ комиссии не существует как факта: NOT_APPLICABLE, тариф не применяется.
--
-- ЛОГИСТИКА. Детерминированного источника нет, поэтому оценщик статистический и выбран
-- бэктестом: V_OZON_LOGISTICS_ESTIMATOR (SKU_P70_120D). Логистика начисляется и выкупу.
--
-- ХРАНЕНИЕ, ПРОЧИЕ ПРЯМЫЕ, ПРОДВИЖЕНИЕ, РЕКЛАМА — ТОЛЬКО ФАКТ. Это событийные начисления
-- (утилизация, вывоз, упаковка, сбор отзывов), а не переменная на единицу: предсказывать их
-- нечем, и выдуманный ноль здесь так же запрещён, как и выдуманная оценка. Их состояние —
-- ACTUAL там, где начисление есть, и UNKNOWN там, где его нет, но период ещё не созрел.
--
-- ЗРЕЛОСТЬ. Период считается созревшим через MATURITY_DAYS (36) суток после даты факта —
-- это максимальный наблюдённый срок видимости. До этого отсутствие расхода ничего не
-- доказывает; после — отсутствие становится содержательным.
-- Internal dependencies: FCT_OZON_SKU_PNL_DAILY, V_OZON_CIS_BUYOUT, V_OZON_COMMISSION_POLICY,
-- V_OZON_LOGISTICS_ESTIMATOR.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL`
OPTIONS (description = "Операционная (провизорная) экономика Ozon, зерно = сутки x internal_sku. Надстройка над FCT_OZON_SKU_PNL_DAILY: канонические поля факта проходят насквозь неизменными, оценки лежат в отдельных колонках, факт и оценка раздельно аудируемы. Покомпонентное старшинство: ACTUAL > ESTIMATED > NOT_APPLICABLE > UNKNOWN; ноль не ставится только потому, что Ozon ещё не прислал расход. Комиссия оценивается как (price_rub - payout_rub) x quantity из отчёта по отправлениям - тождество проверено на 600 отправлениях из 600 с расхождением 0,00 руб.; резерв - тариф из V_OZON_COMMISSION_POLICY; у выкупов СНГ комиссия NOT_APPLICABLE. Логистика оценивается V_OZON_LOGISTICS_ESTIMATOR (SKU_P70_120D, выбран бэктестом). Хранение, прочие прямые, продвижение и реклама - только факт, оценщика не имеют. Период считается созревшим через 36 суток - максимальный наблюдённый срок видимости начисления.")
AS
WITH map AS (
  SELECT DISTINCT internal_sku, marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'),
post AS (
  SELECT DISTINCT p.posting_number, p.sku, p.status, p.order_date, p.quantity,
         p.price_rub, p.payout_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN map m ON m.marketplace_sku = p.sku),
acc AS (
  SELECT posting_number, sku,
         COUNTIF(seller_base_price_rub IS NOT NULL) n_base,
         COUNTIF(type_id IN (32, 29, 28, 98, 30, 59, 45, 78, 9)) n_log
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  GROUP BY 1, 2),
-- тип операции выводится ТЕМ ЖЕ правилом, что в FCT_OZON_SKU_PNL_DAILY: второго классификатора
-- в системе быть не должно, иначе факт и операционный слой однажды разойдутся молча
cls AS (
  SELECT p.*, IFNULL(a.n_base, 0) n_base, IFNULL(a.n_log, 0) n_log,
    CASE WHEN b.posting_number IS NOT NULL THEN 'CIS_BUYOUT'
         WHEN p.status = 'delivered' AND IFNULL(a.n_base, 0) = 0
              AND IFNULL(p.payout_rub, NUMERIC '0') = 0 THEN 'CIS_BUYOUT'
         ELSE 'MARKETPLACE_SALE' END op_type
  FROM post p
  LEFT JOIN acc a USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number),
gap AS (
  SELECT c.order_date d, c.internal_sku,
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0, c.quantity, 0)) commission_gap_qty,
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0,
           (c.price_rub - IFNULL(c.payout_rub, NUMERIC '0')) * c.quantity, NUMERIC '0')) commission_gap_payout_rub,
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0 AND IFNULL(c.payout_rub, NUMERIC '0') > 0,
           c.quantity, 0)) commission_gap_payout_qty,
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0,
           c.price_rub * c.quantity, NUMERIC '0')) commission_gap_base_rub,
    SUM(IF(c.n_log = 0, c.quantity, 0)) logistics_gap_qty
  FROM cls c
  WHERE c.status = 'delivered'
  GROUP BY 1, 2),
pol AS (
  SELECT internal_sku, effective_from, effective_to, commission_rate
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_COMMISSION_POLICY`),
est AS (
  SELECT internal_sku, logistics_per_unit_rub, estimator_method
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_LOGISTICS_ESTIMATOR`),
j AS (
  SELECT f.*,
    IFNULL(g.commission_gap_qty, 0) commission_gap_qty,
    IFNULL(g.logistics_gap_qty, 0) logistics_gap_qty,
    -- выплата известна в момент отправления: комиссия = цена - выплата, точно
    IFNULL(g.commission_gap_payout_rub, NUMERIC '0') gap_payout_rub,
    IFNULL(g.commission_gap_payout_qty, 0) gap_payout_qty,
    -- резерв: тариф на дату заказа от цены продавца
    CAST(IFNULL(g.commission_gap_base_rub, NUMERIC '0') * IFNULL(pol.commission_rate, 0) AS NUMERIC) gap_tariff_rub,
    pol.commission_rate policy_rate,
    IFNULL(e.logistics_per_unit_rub, NUMERIC '0') log_per_unit,
    e.estimator_method log_method,
    DATE_DIFF(CURRENT_DATE('Europe/Moscow'), f.fact_date, DAY) age_days
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` f
  LEFT JOIN gap g ON g.d = f.fact_date AND g.internal_sku = f.internal_sku
  LEFT JOIN pol ON pol.internal_sku = f.internal_sku
                AND f.fact_date BETWEEN pol.effective_from AND pol.effective_to
  LEFT JOIN est e ON e.internal_sku = f.internal_sku),
r AS (
  SELECT j.*,
    CASE WHEN j.commission_gap_qty = 0 THEN NUMERIC '0'
         WHEN j.gap_payout_qty = j.commission_gap_qty THEN j.gap_payout_rub
         ELSE j.gap_tariff_rub END commission_estimated_rub,
    CASE WHEN j.commission_gap_qty = 0 THEN NULL
         WHEN j.gap_payout_qty = j.commission_gap_qty THEN 'POSTING_PAYOUT_EXACT'
         WHEN j.policy_rate IS NOT NULL THEN 'COMMISSION_POLICY_TARIFF'
         ELSE NULL END commission_estimate_method,
    CAST(j.logistics_gap_qty * j.log_per_unit AS NUMERIC) logistics_estimated_rub
  FROM j)
SELECT
  -- ── ФАКТ: канонические поля проходят насквозь, ни одно не переопределяется ──────────────
  r.fact_date, r.internal_sku,
  r.gross_qty, r.cancelled_qty, r.in_transit_qty, r.realized_qty,
  r.seller_base_revenue_rub, r.commission_rub, r.commission_missing_qty,
  r.commission_not_applicable_qty, r.buyout_revenue_unproven_qty, r.buyout_revenue_unproven_rub,
  r.logistics_rub, r.acquiring_rub, r.storage_rub,
  r.direct_variable_marketplace_costs_rub, r.other_direct_marketplace_costs_rub,
  r.product_cogs_rub, r.cogs_missing_qty, r.sku_promotion_rub,
  r.contribution_before_ads_rub, r.ad_spend_attributed_rub,
  r.contribution_after_attributed_ads_rub, r.fact_date_semantics,
  -- ── ОЦЕНКА: отдельные колонки, факт не затронут ───────────────────────────────────────
  r.commission_gap_qty, r.commission_estimated_rub, r.commission_estimate_method,
  r.logistics_gap_qty, r.logistics_estimated_rub, r.log_method logistics_estimate_method,
  r.log_per_unit logistics_estimated_per_unit_rub,
  r.policy_rate commission_policy_rate,
  -- ── ЭФФЕКТИВНАЯ ВЕЛИЧИНА И СОСТОЯНИЕ ──────────────────────────────────────────────────
  r.commission_rub + r.commission_estimated_rub commission_effective_rub,
  CASE WHEN r.realized_qty = 0 THEN 'NOT_APPLICABLE'
       WHEN r.commission_not_applicable_qty = r.realized_qty THEN 'NOT_APPLICABLE'
       WHEN r.commission_gap_qty = 0 THEN 'ACTUAL'
       WHEN r.commission_estimate_method IS NOT NULL THEN 'ESTIMATED'
       ELSE 'UNKNOWN' END commission_state,
  r.logistics_rub + r.logistics_estimated_rub logistics_effective_rub,
  CASE WHEN r.realized_qty = 0 AND r.logistics_rub = 0 THEN 'NOT_APPLICABLE'
       WHEN r.logistics_gap_qty = 0 THEN 'ACTUAL'
       WHEN r.log_per_unit > 0 THEN 'ESTIMATED'
       ELSE 'UNKNOWN' END logistics_state,
  -- хранение и прочие прямые оценщика НЕ имеют: событийные начисления, предсказывать нечем
  CASE WHEN r.storage_rub <> 0 THEN 'ACTUAL'
       WHEN r.age_days >= 36 THEN 'NOT_APPLICABLE'
       ELSE 'UNKNOWN' END storage_state,
  CASE WHEN r.other_direct_marketplace_costs_rub <> 0 OR r.sku_promotion_rub <> 0 THEN 'ACTUAL'
       WHEN r.age_days >= 36 THEN 'NOT_APPLICABLE'
       ELSE 'UNKNOWN' END other_direct_state,
  -- ── ОПЕРАЦИОННЫЙ РЕЗУЛЬТАТ: тот же состав, что у факта, но с эффективными величинами ───
  r.seller_base_revenue_rub - r.product_cogs_rub
    - (r.commission_rub + r.commission_estimated_rub)
    - (r.direct_variable_marketplace_costs_rub + r.logistics_estimated_rub)
    - r.other_direct_marketplace_costs_rub - r.sku_promotion_rub
      operational_contribution_before_ads_rub,
  r.seller_base_revenue_rub - r.product_cogs_rub
    - (r.commission_rub + r.commission_estimated_rub)
    - (r.direct_variable_marketplace_costs_rub + r.logistics_estimated_rub)
    - r.other_direct_marketplace_costs_rub - r.sku_promotion_rub
    - r.ad_spend_attributed_rub operational_contribution_after_ads_rub,
  r.age_days,
  r.age_days >= 36 period_matured,
  36 maturity_days
FROM r
