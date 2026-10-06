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
-- 🔴 UBR-010 (решение владельца 2026-09-22): sku_promotion_rub — слой L3, вычитается вместе с
-- рекламой. operational_contribution_before_ads_rub его больше НЕ содержит;
-- operational_contribution_after_ads_rub содержит, как и прежде — его значение не изменилось.
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
-- Только документированный выкуп СНГ доказывает NOT_APPLICABLE. Структурный кандидат
-- (delivered, нет seller-base finance, payout NULL/0) не доказывает отсутствия комиссии:
-- для операционной модели применяется датированный тариф, выручка остаётся непроверенной.
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
--
-- ЗАКАЗ, А НЕ ТОЛЬКО РЕАЛИЗАЦИЯ (Gate 9). Юнитка — операционная модель ПО ДАТЕ ЗАКАЗА.
-- До Gate 9 экономика считалась только по ДОСТАВЛЕННЫМ единицам, и сутки, где все заказы
-- ещё в пути, выглядели чистым убытком: реклама списана, выручки нет. Замер: 17 строк
-- сутки × SKU в окне 45 суток, 16 из них показывали результат, суммарно −16 676,73 ₽
-- мнимого убытка.
--
-- Поэтому операционная экономика считается на ОЖИДАЕМО РЕАЛИЗОВАННЫХ единицах
-- (`expected_realized_qty` = заказано − отменено), а не только на доставленных. Единица
-- в пути приносит СВОЮ провизорную экономику:
--   цена продавца — `price_rub` отправления, известна в момент заказа (ДЕТЕРМИНИРОВАНА);
--   комиссия      — тариф из V_OZON_COMMISSION_POLICY: `payout_rub` до доставки равен нулю
--                   (0 из 40 отправлений в пути имеют выплату), поэтому точный вывод
--                   «цена − выплата» здесь неприменим;
--   логистика     — SKU_P70_120D;
--   цена покупателя и СПП — НЕ ВЫВОДЯТСЯ. Они существуют только в финансовом начислении
--                   (2003 строки из 2003), то есть после доставки. Пустая ячейка честнее
--                   выдуманной скидки, и пустота СПП не обнуляет известную цену продавца.
--
-- ДВОЙНОГО СЧЁТА НЕТ: отмена и доставка меняют те же сутки заказа, а окно перезаписи
-- (45 суток) длиннее максимального наблюдённого срока пребывания в пути (32 суток).
-- Провизорный вклад отменённой единицы исчезает при следующем же прогоне.
--
-- ФИНАНСЫ СТАРШЕ ЗАСТРЯВШЕГО СТАТУСА (2026-10-06, дефект 20.08 / 930334396 / 77152971-0050-1).
-- Отчёт по отправлениям перечитывается только 30 суток, и статус единицы может навсегда
-- застрять на «delivering», хотя финансовое начисление (seller_base + комиссия) уже пришло.
-- Факт считает реализацией только delivered, а оценка выключается при n_base > 0 — единица
-- выпадала из обеих частей, и комиссия молча становилась нулём (+655,81 ₽ к прибыли).
-- Правило: недоставленная, неотменённая MARKETPLACE_SALE с seller-base начислением — это
-- КОНФЛИКТ ЖИЗНЕННОГО ЦИКЛА. Её комиссия берётся из начисления (как в факте, -commission),
-- в оценку она не входит, строка остаётся провизорной, пока статус не догонит финансы.
-- Разбиение единиц по комиссии полное по построению: факт | конфликт | оценка | выкуп.
-- commission_unaccounted_qty считает реальные дыры (конфликт с NULL-комиссией, статус NULL) и
-- делает состояние UNKNOWN, а строку — не ACTUAL и не PROVISIONAL_COMPLETE, а не нулём.
--
-- ФАКТ НЕ ТРОНУТ: `seller_base_revenue_rub` и `realized_qty` остаются выручкой и
-- количеством ДОСТАВЛЕННЫХ единиц. Провизорная часть лежит в своих колонках.
-- Internal dependencies: FCT_OZON_SKU_PNL_DAILY, V_OZON_CIS_BUYOUT, V_OZON_COMMISSION_POLICY,
-- V_OZON_LOGISTICS_ESTIMATOR. External: evetis_ref.V_PRODUCT_COGS_EFFECTIVE.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL`
OPTIONS (description = "Операционная (провизорная) экономика Ozon, зерно = сутки x internal_sku. Надстройка над FCT_OZON_SKU_PNL_DAILY: денежные поля факта проходят насквозь неизменными, полнота комиссии уточняется по первичному buyout evidence, оценки лежат в отдельных колонках, факт и оценка раздельно аудируемы. Покомпонентное старшинство: ACTUAL > ESTIMATED > NOT_APPLICABLE > UNKNOWN; ноль не ставится только потому, что Ozon ещё не прислал расход. Комиссия оценивается как (price_rub - payout_rub) x quantity из отчёта по отправлениям - тождество проверено на 600 отправлениях из 600 с расхождением 0,00 руб.; резерв - тариф из V_OZON_COMMISSION_POLICY; у документированных выкупов СНГ комиссия NOT_APPLICABLE; структурные кандидаты без документа используют датированный тариф и остаются PROVISIONAL_PARTIAL. Логистика оценивается V_OZON_LOGISTICS_ESTIMATOR (SKU_P70_120D, выбран бэктестом). Хранение, прочие прямые, продвижение и реклама - только факт, оценщика не имеют. Период считается созревшим через 36 суток - максимальный наблюдённый срок видимости начисления. Gate 9: операционная экономика считается на ОЖИДАЕМО реализованных единицах (заказано - отменено), а не только на доставленных: единица в пути приносит свою провизорную экономику (цена продавца из отправления, комиссия по тарифу, логистика по оценщику). Цена покупателя и СПП до доставки не выводятся - они существуют только в финансовом начислении. Факт не тронут: seller_base_revenue_rub и realized_qty остаются величинами доставленных единиц. Финансы старше застрявшего статуса: недоставленная неотменённая продажа с seller-base начислением - конфликт жизненного цикла, её комиссия берётся из начисления (lifecycle_conflict_commission_rub), строка остаётся провизорной; неучтённая единица (commission_unaccounted_qty > 0) делает комиссию UNKNOWN, а не нулём.")
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
         COUNTIF(type_id IN (32, 29, 28, 98, 30, 59, 45, 78, 9)) n_log,
         -- та же агрегация, что fin_econ в FCT: только строки с seller_base
         SUM(IF(seller_base_price_rub IS NOT NULL, seller_base_price_rub, NULL)) fin_sp,
         SUM(IF(seller_base_price_rub IS NOT NULL, commission_rub, NULL)) fin_comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  GROUP BY 1, 2),
-- Уточнение для операционной комиссии: документированный выкуп и структурный кандидат
-- различаются. Денежный факт FCT (включая непроверенную выручку) не переопределяется.
cogs AS (
  SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
cls AS (
  SELECT p.*, IFNULL(a.n_base, 0) n_base, IFNULL(a.n_log, 0) n_log, a.fin_sp, a.fin_comm,
    CASE WHEN b.posting_number IS NOT NULL THEN 'CIS_BUYOUT'
         WHEN p.status = 'delivered' AND IFNULL(a.n_base, 0) = 0
              AND IFNULL(p.payout_rub, NUMERIC '0') = 0 THEN 'CIS_BUYOUT_CANDIDATE'
         ELSE 'MARKETPLACE_SALE' END op_type
  FROM post p
  LEFT JOIN acc a USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number),
-- единицы, чья экономика уже ОЖИДАЕТСЯ: доставленные и находящиеся в пути.
-- Отменённые сюда не входят никогда: у них экономики не будет.
gap AS (
  SELECT c.order_date d, c.internal_sku,
    SUM(IF(c.status <> 'delivered', c.quantity, 0)) in_transit_qty_src,
    -- выручка единиц в пути по цене отправления: seller_base_price появится только
    -- в начислении, но price_rub ей тождественно равен (2002 строки из 2003)
    -- конфликт цикла: seller_base из начисления, как в факте (IFNULL(sp, price_rub))
    SUM(IF(c.status <> 'delivered',
           IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base > 0, IFNULL(c.fin_sp, c.price_rub), c.price_rub)
             * c.quantity, NUMERIC '0')) in_transit_revenue_rub,
    SUM(IF(c.status <> 'delivered' AND c.op_type = 'MARKETPLACE_SALE' AND c.n_base > 0,
           c.quantity, 0)) lifecycle_conflict_qty,
    SUM(IF(c.status <> 'delivered' AND c.op_type = 'MARKETPLACE_SALE' AND c.n_base > 0,
           IFNULL(-c.fin_comm, NUMERIC '0'), NUMERIC '0')) lifecycle_conflict_commission_rub,
    -- страж «UNKNOWN не становится нулём» — реальные дыры, а не тавтология разбиения:
    -- (1) конфликт цикла, у которого seller-base есть, а комиссия в начислении NULL;
    -- (2) отправление с неизвестным статусом (NULL): факт считает его в gross_qty, а ни одна
    --     ветка комиссии его не покрывает.
    SUM(IF(c.status IS NULL
           OR (c.status <> 'delivered' AND c.op_type = 'MARKETPLACE_SALE' AND c.n_base > 0 AND c.fin_comm IS NULL),
           c.quantity, 0)) commission_unaccounted_qty,
    SUM(IF(c.op_type IN ('MARKETPLACE_SALE', 'CIS_BUYOUT_CANDIDATE') AND c.n_base = 0, c.quantity, 0)) commission_gap_qty,
    -- «цена − выплата» применима ТОЛЬКО там, где выплата уже известна, то есть у доставленных
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0 AND IFNULL(c.payout_rub, NUMERIC '0') > 0,
           (c.price_rub - c.payout_rub) * c.quantity, NUMERIC '0')) commission_gap_payout_rub,
    SUM(IF(c.op_type = 'MARKETPLACE_SALE' AND c.n_base = 0 AND IFNULL(c.payout_rub, NUMERIC '0') > 0,
           c.quantity, 0)) commission_gap_payout_qty,
    -- база тарифа — цена тех единиц, где выплата ещё не известна
    SUM(IF(c.op_type IN ('MARKETPLACE_SALE', 'CIS_BUYOUT_CANDIDATE') AND c.n_base = 0 AND IFNULL(c.payout_rub, NUMERIC '0') = 0,
           c.price_rub * c.quantity, NUMERIC '0')) commission_gap_tariff_base_rub,
    SUM(IF(c.n_log = 0, c.quantity, 0)) logistics_gap_qty,
    -- себестоимость единиц В ПУТИ: в факте её нет (там только доставленные), а без неё
    -- провизорная прибыль была бы завышена на всю себестоимость — систематический оптимизм
    SUM(IF(c.status <> 'delivered', IFNULL(k.u, NUMERIC '0') * c.quantity, NUMERIC '0')) in_transit_cogs_rub,
    SUM(IF(c.status <> 'delivered' AND k.u IS NULL, c.quantity, 0)) in_transit_cogs_missing_qty
  FROM cls c
  LEFT JOIN cogs k ON k.internal_sku = c.internal_sku AND c.order_date BETWEEN k.effective_from AND k.et
  WHERE c.status IS NULL OR c.status <> 'cancelled'
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
    IFNULL(g.lifecycle_conflict_qty, 0) lifecycle_conflict_qty,
    IFNULL(g.lifecycle_conflict_commission_rub, NUMERIC '0') lifecycle_conflict_commission_rub,
    IFNULL(g.commission_unaccounted_qty, 0) commission_unaccounted_qty,
    -- выплата известна в момент отправления: комиссия = цена - выплата, точно
    IFNULL(g.commission_gap_payout_rub, NUMERIC '0') gap_payout_rub,
    IFNULL(g.commission_gap_payout_qty, 0) gap_payout_qty,
    -- резерв: тариф на дату заказа от цены продавца тех единиц, где выплата неизвестна
    CAST(IFNULL(g.commission_gap_tariff_base_rub, NUMERIC '0') * IFNULL(pol.commission_rate, 0) AS NUMERIC) gap_tariff_rub,
    IFNULL(g.in_transit_revenue_rub, NUMERIC '0') in_transit_revenue_rub,
    IFNULL(g.in_transit_cogs_rub, NUMERIC '0') in_transit_cogs_rub,
    IFNULL(g.in_transit_cogs_missing_qty, 0) in_transit_cogs_missing_qty,
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
    -- обе части складываются: точная там, где выплата известна, тарифная там, где нет
    j.gap_payout_rub + j.gap_tariff_rub commission_estimated_rub,
    CASE WHEN j.commission_gap_qty = 0 THEN NULL
         WHEN j.gap_payout_qty = j.commission_gap_qty THEN 'POSTING_PAYOUT_EXACT'
         WHEN j.gap_payout_qty > 0 AND j.policy_rate IS NOT NULL THEN 'POSTING_PAYOUT_AND_TARIFF'
         WHEN j.policy_rate IS NOT NULL THEN 'COMMISSION_POLICY_TARIFF'
         ELSE NULL END commission_estimate_method,
    CAST(j.logistics_gap_qty * j.log_per_unit AS NUMERIC) logistics_estimated_rub
  FROM j)
SELECT
  -- Денежный факт проходит насквозь. Только полнота комиссии уточняется: структурный
  -- кандидат без документа — missing actual commission, а не доказанное NOT_APPLICABLE.
  r.fact_date, r.internal_sku,
  r.gross_qty, r.cancelled_qty, r.in_transit_qty, r.realized_qty,
  r.seller_base_revenue_rub, r.commission_rub,
  r.commission_missing_qty + r.buyout_revenue_unproven_qty commission_missing_qty,
  r.commission_not_applicable_qty - r.buyout_revenue_unproven_qty commission_not_applicable_qty,
  r.buyout_revenue_unproven_qty, r.buyout_revenue_unproven_rub,
  r.logistics_rub, r.acquiring_rub, r.storage_rub,
  r.direct_variable_marketplace_costs_rub, r.other_direct_marketplace_costs_rub,
  r.product_cogs_rub, r.cogs_missing_qty, r.sku_promotion_rub,
  r.contribution_before_ads_rub, r.ad_spend_attributed_rub,
  r.contribution_after_attributed_ads_rub, r.fact_date_semantics,
  -- ── ОЦЕНКА: отдельные колонки, факт не затронут ───────────────────────────────────────
  -- ── ОЖИДАЕМАЯ РЕАЛИЗАЦИЯ: база операционной экономики (Gate 9) ────────────────────────
  r.gross_qty - r.cancelled_qty expected_realized_qty,
  r.seller_base_revenue_rub + r.in_transit_revenue_rub provisional_revenue_rub,
  r.product_cogs_rub + r.in_transit_cogs_rub provisional_cogs_rub,
  r.in_transit_revenue_rub, r.in_transit_cogs_rub,
  r.cogs_missing_qty + r.in_transit_cogs_missing_qty provisional_cogs_missing_qty,
  r.commission_gap_qty, r.commission_estimated_rub, r.commission_estimate_method,
  r.logistics_gap_qty, r.logistics_estimated_rub, r.log_method logistics_estimate_method,
  r.log_per_unit logistics_estimated_per_unit_rub,
  r.policy_rate commission_policy_rate,
  -- ── ЭФФЕКТИВНАЯ ВЕЛИЧИНА И СОСТОЯНИЕ ──────────────────────────────────────────────────
  r.commission_rub + r.commission_estimated_rub + r.lifecycle_conflict_commission_rub commission_effective_rub,
  -- база состояния — ОЖИДАЕМАЯ реализация: у единицы в пути комиссия не «неприменима»,
  -- она просто ещё не начислена, и оценка для неё существует
  CASE WHEN r.gross_qty - r.cancelled_qty = 0 THEN 'NOT_APPLICABLE'
       WHEN r.commission_unaccounted_qty > 0 THEN 'UNKNOWN'
       WHEN r.commission_not_applicable_qty = r.realized_qty AND r.realized_qty > 0
            AND r.commission_gap_qty = 0 AND r.lifecycle_conflict_qty = 0 THEN 'NOT_APPLICABLE'
       WHEN r.commission_gap_qty = 0 THEN 'ACTUAL'
       WHEN r.commission_estimate_method IS NOT NULL THEN 'ESTIMATED'
       ELSE 'UNKNOWN' END commission_state,
  r.logistics_rub + r.logistics_estimated_rub logistics_effective_rub,
  CASE WHEN r.gross_qty - r.cancelled_qty = 0 AND r.logistics_rub = 0 THEN 'NOT_APPLICABLE'
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
  -- ── ПОЛНОТА ЭКОНОМИКИ СТРОКИ (Gate 9 §7F) ─────────────────────────────────────────────
  -- Строка не имеет права выглядеть законченным операционным результатом, если существенные
  -- составляющие принятой модели молча отсутствуют. Классификация — в данных, а не в описании.
  CASE
    WHEN r.gross_qty - r.cancelled_qty = 0 THEN 'NO_ECONOMICS'
    WHEN r.buyout_revenue_unproven_qty > 0
         OR r.cogs_missing_qty + r.in_transit_cogs_missing_qty > 0 THEN 'PROVISIONAL_PARTIAL'
    -- Документированный buyout не делает оставшиеся in-transit единицы фактическими.
    WHEN r.in_transit_qty = 0 AND r.lifecycle_conflict_qty = 0 AND r.commission_unaccounted_qty = 0
         AND r.commission_gap_qty = 0 AND r.logistics_gap_qty = 0 THEN 'ACTUAL'
    WHEN (r.seller_base_revenue_rub + r.in_transit_revenue_rub) > 0
         AND r.commission_unaccounted_qty = 0
         AND (r.commission_gap_qty = 0 OR r.commission_estimate_method IS NOT NULL)
         AND (r.logistics_gap_qty = 0 OR r.log_per_unit > 0)
         AND r.cogs_missing_qty + r.in_transit_cogs_missing_qty = 0
      THEN 'PROVISIONAL_COMPLETE'
    ELSE 'PROVISIONAL_PARTIAL'
  END economics_completeness,
  -- ── ОПЕРАЦИОННЫЙ РЕЗУЛЬТАТ: тот же состав, что у факта, но на ОЖИДАЕМОЙ реализации ─────
  r.seller_base_revenue_rub + r.in_transit_revenue_rub
    - (r.product_cogs_rub + r.in_transit_cogs_rub)
    - (r.commission_rub + r.commission_estimated_rub + r.lifecycle_conflict_commission_rub)
    - (r.direct_variable_marketplace_costs_rub + r.logistics_estimated_rub)
    - r.other_direct_marketplace_costs_rub
      operational_contribution_before_ads_rub,
  r.seller_base_revenue_rub + r.in_transit_revenue_rub
    - (r.product_cogs_rub + r.in_transit_cogs_rub)
    - (r.commission_rub + r.commission_estimated_rub + r.lifecycle_conflict_commission_rub)
    - (r.direct_variable_marketplace_costs_rub + r.logistics_estimated_rub)
    - r.other_direct_marketplace_costs_rub - r.sku_promotion_rub
    - r.ad_spend_attributed_rub operational_contribution_after_ads_rub,
  r.age_days,
  r.age_days >= 36 period_matured,
  36 maturity_days,
  -- ── КОНФЛИКТ ЖИЗНЕННОГО ЦИКЛА (2026-10-06): финансы пришли, статус ещё не delivered ──
  r.lifecycle_conflict_qty, r.lifecycle_conflict_commission_rub, r.commission_unaccounted_qty
FROM r
