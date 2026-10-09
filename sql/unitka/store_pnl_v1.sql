-- ============================================================================
-- UNITKA WB — PHASE C: P&L МАГАЗИНА WB (OWNER ACK 2026-10-09, C1/C2/C4)
-- ============================================================================
-- Вопрос: сколько РЕАЛЬНО заработал магазин WB. Ответ — УПРАВЛЕНЧЕСКАЯ чистая прибыль магазина:
--   MANAGEMENT NET STORE PROFIT = Σ вклад SKU (лист Юнитки, как есть)
--                               + Σ поправок «факт площадки − представлено в SKU» (знак: + увеличивает прибыль)
--                               − затраты уровня кабинета (по МЕСЯЦУ УСЛУГИ, решение Р1)
--                               + доходы уровня кабинета.
-- «Управленческая», а не бухгалтерская: COGS и налог 2 % — управленческие, УСН/OPEX/фулфилмент не входят.
--
-- Сторона SKU — снимок значений листа (wb_ops.UNITKA_SKU_COMPONENTS_DAILY, загрузчик unitka-store-pnl):
-- исторические ставки комиссии и логистики живут только в листе, BigQuery хранит лишь текущие окна.
-- Финансовая сторона — финотчёт WB, переведённый в ту же когорту заказа (srid → order_dt, сутки МСК) и
-- сверенный на ОДНИХ И ТЕХ ЖЕ единицах. Док: docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md.
--
-- Возмещения WB (перевозка, перемещение, ПВЗ) — MEMO_NON_PNL по доказательству Р2 (09.10, 2 794 строки
-- 08–10.2026): сумма возмещения погашена уменьшением вознаграждения WB (vw + vwNds) до копеек, forPay = 0,
-- итог отчёта WB считает выплату без них. Это уже внутри удержания с продаж (комиссии модели).
-- ============================================================================

-- ─── 1. Утверждённая карта операций финотчёта WB (C4: всё, чего здесь нет, — NEW_FINANCE_OPERATION) ──
-- Утверждено только то, что модель действительно ЧИТАЕТ (V_WB_STORE_FINANCE_COVERAGE доказывает это по рублям).
-- Операция, которую модель не читает, — PENDING_CLASSIFICATION: её появление в окне месяца блокирует закрытие.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` AS
SELECT * FROM UNNEST([
  STRUCT('Продажа' AS supplier_oper_name, 'DIRECT_SKU' AS treatment, 'REVENUE_COMMISSION' AS category, 'цена продавца × кол-во; удержание WB = база − for_pay (комиссия + эквайринг)' AS note),
  ('Возврат', 'DIRECT_SKU', 'RETURNS', 'сторно продажи по srid'),
  ('Коррекция продаж', 'PENDING_CLASSIFICATION', 'REVENUE_COMMISSION', 'историческая (до 2025-07); модель её не читает — появление = разбор'),
  ('Корректировка эквайринга', 'PENDING_CLASSIFICATION', 'REVENUE_COMMISSION', 'историческая (до 2025-01); модель её не читает — появление = разбор'),
  ('Логистика', 'DIRECT_SKU', 'LOGISTICS', 'прямые, обратные плечи и отказы по srid (до 31.08.2026)'),
  ('Доставка', 'DIRECT_SKU', 'LOGISTICS', 'то же с 01.09.2026 (переименование WB)'),
  ('Коррекция логистики', 'DIRECT_SKU', 'LOGISTICS', 'историческая (до 2026-04)'),
  ('Хранение', 'DIRECT_SKU', 'STORAGE', 'факт по nm; Юнитка с 01.09 — тот же факт'),
  ('Коррекция хранения', 'DIRECT_SKU', 'STORAGE', 'сторно хранения'),
  ('Платная приемка', 'ACCOUNT_LEVEL', 'ACCEPTANCE', 'приёмка поставки'),
  ('Пересчет платной приемки', 'ACCOUNT_LEVEL', 'ACCEPTANCE', 'историческая'),
  ('Штраф', 'ACCOUNT_LEVEL', 'PENALTY', 'без nm и заказа (08.2026: «srid» 6144364 — не заказ)'),
  ('Удержание', 'BY_DEDUCTION_CLASS', 'DEDUCTION', 'класс — V_WB_DEDUCTIONS_CLASSIFIED'),
  ('Возмещение издержек по перевозке/по складским операциям с товаром', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: погашено вознаграждением WB, forPay = 0'),
  ('Возмещение издержек по перемещению и операционной обработке товара', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: то же с 01.09.2026'),
  ('Возмещение за выдачу и возврат товаров на ПВЗ', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: ppvzReward погашен вознаграждением WB'),
  ('Компенсация скидки по программе лояльности', 'MEMO_ZERO', 'LOYALTY', 'денежных полей нет (raw_json)'),
  ('Сумма баллов, удержанных в рамках акции "Баллы за отзывы"', 'MEMO_ZERO', 'REVIEW_POINTS', 'денежных полей нет (raw_json)'),
  ('Добровольная компенсация при возврате', 'PENDING_CLASSIFICATION', 'COMPENSATION', 'историческая; поле денег не доказано — появление = разбор, не молчаливый доход'),
  ('Компенсация ущерба', 'PENDING_CLASSIFICATION', 'COMPENSATION', 'историческая; поле денег не доказано — появление = разбор, не молчаливый доход'),
  ('Стоимость участия в программе лояльности', 'PENDING_CLASSIFICATION', 'LOYALTY', '2025-08/09; не встречалась в месяцах Phase C'),
  ('Сумма удержанная за начисленные баллы программы лояльности', 'PENDING_CLASSIFICATION', 'LOYALTY', '2025-08/09; не встречалась в месяцах Phase C')
]);

-- ─── 2. Финансы WB в когорте заказа: сутки МСК заказа × nm (только строки с srid) ──────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` AS
WITH f AS (
  SELECT supplier_oper_name op, finance_status,
    DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow') order_date_msk,
    SAFE_CAST(wb_nm_id AS INT64) nm_id,
    SAFE_CAST(quantity AS NUMERIC) q, SAFE_CAST(retail_price_withdisc_rub AS NUMERIC) pwd,
    SAFE_CAST(for_pay AS NUMERIC) fp, SAFE_CAST(logistics_amount AS NUMERIC) lg
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE srid IS NOT NULL AND srid != ''
    AND supplier_oper_name IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики'))
SELECT order_date_msk, nm_id,
  SUM(CASE op WHEN 'Продажа' THEN q WHEN 'Возврат' THEN -q ELSE 0 END) sold_qty,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q WHEN 'Возврат' THEN -pwd * q ELSE 0 END) seller_base_rub,
  SUM(CASE op WHEN 'Продажа' THEN fp WHEN 'Возврат' THEN -fp ELSE 0 END) for_pay_rub,
  SUM(CASE op WHEN 'Продажа' THEN pwd * q - fp WHEN 'Возврат' THEN -(pwd * q - fp) ELSE 0 END) wb_take_rub,
  SUM(IF(op IN ('Логистика', 'Доставка', 'Коррекция логистики'), IFNULL(lg, 0), 0)) logistics_rub,
  COUNTIF(finance_status = 'PROVISIONAL') provisional_rows
FROM f
WHERE order_date_msk IS NOT NULL AND nm_id IS NOT NULL AND nm_id > 0
GROUP BY 1, 2;

-- ─── 3. Счета уровня кабинета: дата проводки, месяц услуги, класс ─────────────────────────────────────
-- Р1 (решение владельца): месяц УСЛУГИ, а не дата проводки. Утилизация подписана «за <месяц> <год>»,
-- «Остаток по минимальному платежу» выставляется ~12-го за ПРЕДЫДУЩИЙ месяц (13.08 → июль, 12.09 → август):
-- период выводится правилом (service_period_basis = INFERRED_PREVIOUS_MONTH). Остальное — месяц проводки.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` AS
WITH months AS (
  SELECT * FROM UNNEST(['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь',
                        'октябрь', 'ноябрь', 'декабрь']) name WITH OFFSET i),
ded AS (
  SELECT d.finance_row_key, d.finance_date, d.bonus_type_name label, d.deduction_class,
    d.deduction_amount_rub amount_rub, d.srid, d.transit_supply_id,
    REGEXP_EXTRACT(LOWER(d.bonus_type_name), r'за ([а-я]+) \d{4}') m_name,
    SAFE_CAST(REGEXP_EXTRACT(d.bonus_type_name, r'за [А-Яа-я]+ (\d{4})') AS INT64) m_year
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED` d
  WHERE d.deduction_class != 'AD_BILLING'),
ded2 AS (
  SELECT ded.*, months.i + 1 m_num FROM ded LEFT JOIN months ON months.name = ded.m_name),
other AS (
  SELECT row_hash finance_row_key, _rr_date finance_date, supplier_oper_name label,
    CASE WHEN supplier_oper_name = 'Штраф' THEN 'PENALTY'
         WHEN supplier_oper_name IN ('Платная приемка', 'Пересчет платной приемки') THEN 'ACCEPTANCE' END deduction_class,
    IFNULL(SAFE_CAST(penalty AS NUMERIC), 0) + IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0) amount_rub, srid
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки'))
SELECT finance_row_key, finance_date booking_date, DATE_TRUNC(finance_date, MONTH) booking_month,
  CASE WHEN deduction_class = 'UTILIZATION' AND m_num IS NOT NULL AND m_year IS NOT NULL THEN DATE(m_year, m_num, 1)
       WHEN deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT' THEN DATE_SUB(DATE_TRUNC(finance_date, MONTH), INTERVAL 1 MONTH)
       ELSE DATE_TRUNC(finance_date, MONTH) END service_month,
  CASE WHEN deduction_class = 'UTILIZATION' AND m_num IS NOT NULL THEN 'LABEL_PERIOD'
       WHEN deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT' THEN 'INFERRED_PREVIOUS_MONTH'
       ELSE 'BOOKING_MONTH' END service_period_basis,
  CASE deduction_class
    WHEN 'MINIMUM_PAYMENT_ADJUSTMENT' THEN 'MINIMUM_PAYMENT'
    WHEN 'UTILIZATION' THEN 'UTILIZATION'
    WHEN 'TRANSIT_DEDUCTION' THEN 'TRANSIT'
    WHEN 'FORCE_MAJEURE_PAYMENT' THEN 'COMPENSATION_INCOME'
    WHEN 'REVIEW_POINTS_ADVANCE_REFUND' THEN 'REFUND_INCOME'
    WHEN 'UNCLASSIFIED_DEDUCTION' THEN 'UNKNOWN'
    ELSE deduction_class END category,
  CASE WHEN deduction_class IN ('FORCE_MAJEURE_PAYMENT', 'REVIEW_POINTS_ADVANCE_REFUND') THEN 'ACCOUNT_LEVEL_INCOME'
       -- утилизация без «за <месяц> <год>»: месяц услуги не доказан → не закрывать месяц молча
       WHEN deduction_class = 'UTILIZATION' AND (m_num IS NULL OR m_year IS NULL) THEN 'UNKNOWN'
       WHEN deduction_class = 'UNCLASSIFIED_DEDUCTION' THEN 'UNKNOWN'
       WHEN deduction_class IN ('MINIMUM_PAYMENT_ADJUSTMENT', 'UTILIZATION', 'TRANSIT_DEDUCTION', 'PENALTY', 'ACCEPTANCE') THEN 'ACCOUNT_LEVEL'
       ELSE 'UNKNOWN' END treatment,
  -- знак: + = расход продавца; доходы (компенсации, возвраты аванса) приходят отрицательными
  amount_rub, label, srid, transit_supply_id
FROM (
  SELECT finance_row_key, finance_date, label, deduction_class, amount_rub, srid, transit_supply_id, m_num, m_year FROM ded2
  UNION ALL
  SELECT finance_row_key, finance_date, label, deduction_class, amount_rub, srid, CAST(NULL AS STRING), NULL, NULL FROM other
  WHERE amount_rub != 0);

-- ─── 4. Новые / неутверждённые операции (C4): операция вне карты или с классом PENDING / UNKNOWN ─────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_NEW_OPERATIONS` AS
WITH f AS (
  SELECT supplier_oper_name, _rr_date,
    ABS(IFNULL(SAFE_CAST(retail_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(logistics_amount AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(penalty AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) + ABS(IFNULL(SAFE_CAST(compensation_amount AS NUMERIC), 0))
    + ABS(IFNULL(SAFE_CAST(other_amount AS NUMERIC), 0)) abs_money_rub,
    srid, SAFE_CAST(wb_nm_id AS INT64) nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`)
SELECT f.supplier_oper_name, IFNULL(m.treatment, 'NEW_FINANCE_OPERATION') treatment,
  MIN(f._rr_date) first_seen, MAX(f._rr_date) last_seen, COUNT(*) rows_n, ROUND(SUM(f.abs_money_rub), 2) abs_money_rub,
  COUNTIF(f.srid IS NOT NULL AND f.srid != '') rows_with_srid, COUNTIF(f.nm_id > 0) rows_with_nm,
  STRING_AGG(DISTINCT FORMAT_DATE('%Y-%m', DATE_TRUNC(f._rr_date, MONTH)) ORDER BY FORMAT_DATE('%Y-%m', DATE_TRUNC(f._rr_date, MONTH))) months
FROM f LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m USING (supplier_oper_name)
WHERE m.treatment IS NULL OR m.treatment = 'PENDING_CLASSIFICATION'
GROUP BY 1, 2;

-- ─── 4б. Покрытие финотчёта (C2/C4): каждый рубль строки финотчёта прочитан моделью ───────────────────────
-- Потребитель строки: COHORT (когорта заказа), STORAGE, DEDUCTION_LEDGER (удержания, сверяются с классификатором
-- по сумме), ACCOUNT_LEDGER (штраф, приёмка), MEMO (возмещения, Р2), MEMO_ZERO. unconsumed_abs_rub — деньги, которые
-- модель НЕ читает: операция без потребителя, когортная строка без srid / даты заказа / nm, деньги в «чужом» поле.
-- retail_amount, acquiring_fee и ppvzReward у продажи/возврата — информационные: тождество расчёта WB
-- retailPriceWithDisc − forPay = СПП + vw + vwNds + эквайринг + ppvzReward, то есть они уже внутри «база − for_pay».
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` AS
WITH f AS (
  SELECT row_hash finance_row_key, _rr_date booking_date, supplier_oper_name op, finance_status,
    srid, SAFE.TIMESTAMP(order_dt) odt, SAFE_CAST(wb_nm_id AS INT64) nm_id,
    ABS(IFNULL(SAFE_CAST(retail_amount AS NUMERIC), 0)) retail, ABS(IFNULL(SAFE_CAST(for_pay AS NUMERIC), 0)) fp,
    ABS(IFNULL(SAFE_CAST(logistics_amount AS NUMERIC), 0)) lg, ABS(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) st,
    ABS(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0)) ded, ABS(IFNULL(SAFE_CAST(penalty AS NUMERIC), 0)) pen,
    ABS(IFNULL(SAFE_CAST(acceptance AS NUMERIC), 0)) acc, ABS(IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0)) addp,
    ABS(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) rb, ABS(IFNULL(SAFE_CAST(compensation_amount AS NUMERIC), 0)) comp,
    ABS(IFNULL(SAFE_CAST(other_amount AS NUMERIC), 0)) oth, ABS(IFNULL(SAFE_CAST(acquiring_fee AS NUMERIC), 0)) acq,
    ABS(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)) ppvz
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
c AS (
  SELECT f.*, IFNULL(m.treatment, 'NEW_FINANCE_OPERATION') treatment,
    CASE WHEN m.treatment IN ('PENDING_CLASSIFICATION') OR m.treatment IS NULL THEN 'NONE'
         WHEN f.op IN ('Продажа', 'Возврат', 'Логистика', 'Доставка', 'Коррекция логистики') THEN 'COHORT'
         WHEN f.op IN ('Хранение', 'Коррекция хранения') THEN 'STORAGE'
         WHEN f.op = 'Удержание' THEN 'DEDUCTION_LEDGER'
         WHEN f.op IN ('Штраф', 'Платная приемка', 'Пересчет платной приемки') THEN 'ACCOUNT_LEDGER'
         WHEN m.treatment = 'MEMO_NON_PNL' THEN 'MEMO'
         WHEN m.treatment = 'MEMO_ZERO' THEN 'MEMO_ZERO'
         ELSE 'NONE' END consumer
  FROM f LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` m ON m.supplier_oper_name = f.op)
SELECT finance_row_key, booking_date, op supplier_oper_name, treatment, consumer, finance_status,
  CASE consumer
    WHEN 'COHORT' THEN IF(srid IS NULL OR srid = '' OR odt IS NULL OR IFNULL(nm_id, 0) <= 0, fp + lg, 0)
                       + st + ded + pen + acc + addp + rb + comp + oth
    WHEN 'STORAGE' THEN retail + fp + lg + ded + pen + acc + addp + rb + comp + oth + acq + ppvz
    WHEN 'DEDUCTION_LEDGER' THEN retail + fp + lg + st + pen + acc + rb + comp + oth + acq + ppvz
    WHEN 'ACCOUNT_LEDGER' THEN retail + fp + lg + st + ded + addp + rb + comp + oth + acq + ppvz
    WHEN 'MEMO' THEN retail + fp + lg + st + ded + pen + acc + addp + comp + oth + acq
    ELSE retail + fp + lg + st + ded + pen + acc + addp + rb + comp + oth + acq + ppvz END unconsumed_abs_rub
FROM c;

-- ─── 5. P&L магазина WB по месяцу (C1/C2): мост «факт площадки ↔ SKU ↔ уровень магазина» ───────────────
-- Знак всех *_adjustment_rub: + увеличивает прибыль магазина относительно Σ вклада SKU.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_PNL_MONTHLY` AS
WITH snap AS (
  -- Окно Phase C — с 2026-08 (решение ACK: сентябрь, октябрь, август для сравнения). Раньше: наследие формул
  -- листа, оценочное хранение, LEGACY-слой финотчёта и неполный биллинг рекламы — такие месяцы P&L не считает.
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`
  WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`)
    AND date_msk >= DATE '2026-08-01'),
sku_day AS (
  SELECT s.date_msk, s.nm_id, s.orders, s.cancels, s.orders - s.cancels realized, s.price, s.commission_rate,
    s.logistics_per_unit, s.reverse_leg_rate, s.storage, s.ads_in, s.ads_out, s.profit, s.lcd, s.model_gap, s.tax_per_unit, s.cogs_per_unit,
    c.sold_qty, c.seller_base_rub, c.wb_take_rub, c.logistics_rub fin_logistics_rub
  FROM snap s LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` c
    ON c.order_date_msk = s.date_msk AND c.nm_id = s.nm_id),
sku AS (
  SELECT DATE_TRUNC(date_msk, MONTH) m, MAX(date_msk) last_day,
    SUM(realized * price) revenue_model_rub,
    SUM(profit) sku_contribution_rub,
    SUM(ads_in) ads_sku_rub, SUM(storage) storage_sku_rub,
    SUM(orders * logistics_per_unit + cancels * reverse_leg_rate) logistics_sku_rub,
    SUM(IFNULL(sold_qty, 0)) sold_qty, SUM(realized) realized_qty,
    SUM(IFNULL(seller_base_rub, 0) - IFNULL(sold_qty, 0) * price) price_adjustment_rub,
    SUM(IFNULL(sold_qty, 0) * price * commission_rate - IFNULL(wb_take_rub, 0)) commission_adjustment_rub,
    SUM(GREATEST(realized - IFNULL(sold_qty, 0), 0)) unsettled_qty,
    SUM(GREATEST(IFNULL(sold_qty, 0) - realized, 0)) oversold_qty,
    SUM(GREATEST(realized - IFNULL(sold_qty, 0), 0) * price) unsettled_revenue_rub,
    -- ожидаемый вклад единиц когорты, так и не ставших продажей (без логистики: её факт — в логистике когорты).
    -- БЕЗ отсечения по нулю: сутки, где продано больше, чем лист насчитал выкупов (заказ, потерянный Orders API),
    -- вычитают отрицательный «непроданный» вклад, то есть добавляют вклад лишних продаж. Так тождество
    -- «сверху вниз» (ниже, double_count_residual_rub) точное.
    SUM((realized - IFNULL(sold_qty, 0)) * (price - price * commission_rate - tax_per_unit - cogs_per_unit)) unsettled_margin_rub,
    -- для тождества «сверху вниз»: финансы когорты и управленческие налог/COGS проданных единиц
    SUM(IFNULL(seller_base_rub, 0)) seller_base_rub, SUM(IFNULL(wb_take_rub, 0)) wb_take_rub,
    SUM(IFNULL(sold_qty, 0) * (tax_per_unit + cogs_per_unit)) sold_tax_cogs_rub,
    SUM(ads_out) ads_out_rub, SUM(model_gap) model_gap_rub, MAX(lcd) snapshot_lcd,
    SUM(IFNULL(fin_logistics_rub, 0)) logistics_finance_rub,
    SUM(ABS(model_gap)) model_gap_abs_rub
  FROM sku_day GROUP BY 1),
fin_extra AS (
  -- финансы когорты на сутки × nm, которых нет в снимке листа (SKU вне блоков) — должны быть 0
  SELECT DATE_TRUNC(c.order_date_msk, MONTH) m, SUM(c.seller_base_rub) orphan_base_rub, SUM(c.logistics_rub) orphan_logistics_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY` c
  LEFT JOIN snap s ON s.date_msk = c.order_date_msk AND s.nm_id = c.nm_id
  WHERE s.nm_id IS NULL AND c.order_date_msk <= (SELECT MAX(lcd) FROM snap)
  GROUP BY 1),
fin_status AS (
  SELECT MAX(IF(finance_status = 'FINAL', _rr_date, NULL)) final_through, MAX(_rr_date) any_through
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`),
-- Полнота финотчёта — по ПОКРЫТИЮ суток периодами FINAL-отчётов, а не по MAX(даты): пропавшая неделя видна.
fin_days AS (
  SELECT DISTINCT d
  FROM (SELECT DISTINCT SAFE_CAST(report_period_from AS DATE) pf, SAFE_CAST(report_period_to AS DATE) pt
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` WHERE finance_status = 'FINAL'),
    UNNEST(GENERATE_DATE_ARRAY(pf, pt)) d
  WHERE pf IS NOT NULL AND pt IS NOT NULL AND pt >= pf),
cov AS (
  SELECT s.m,
    COUNTIF(fd.d BETWEEN s.m AND LAST_DAY(s.m)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 1 final_month,
    COUNTIF(fd.d BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 20 DAY)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 21 final_m20,
    COUNTIF(fd.d BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 46 DAY)) = DATE_DIFF(LAST_DAY(s.m), s.m, DAY) + 47 final_m46
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s CROSS JOIN fin_days fd
  GROUP BY 1),
-- Деньги финотчёта, которые модель не читает, и неразобранные операции — в окне месяца и 20 суток после
-- (счета и сторно за месяц приходят в следующем): иначе операция, проведённая 12-го, блокировала бы не тот месяц.
uncovered AS (
  SELECT s.m, SUM(c.unconsumed_abs_rub) unconsumed_finance_rub,
    COUNTIF(c.treatment IN ('NEW_FINANCE_OPERATION', 'PENDING_CLASSIFICATION')) pending_rows
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s
  JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_FINANCE_COVERAGE` c
    ON c.booking_date BETWEEN s.m AND DATE_ADD(LAST_DAY(s.m), INTERVAL 20 DAY)
  GROUP BY 1),
-- Удержания: классификатор (FACT_FINANCE) против финотчёта (canonical) — суммы по месяцу проводки должны совпасть,
-- иначе источник счетов кабинета отстал или разошёлся (месяц и следующий за ним).
ded_rec AS (
  SELECT m, ABS(IFNULL(canon, 0) - IFNULL(cls, 0)) gap
  FROM (SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(deduction AS NUMERIC), 0) + IFNULL(SAFE_CAST(additional_payment AS NUMERIC), 0)) canon
        FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL` WHERE supplier_oper_name = 'Удержание' GROUP BY 1)
  FULL JOIN (SELECT DATE_TRUNC(finance_date, MONTH) m, SUM(deduction_amount_rub) cls
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED` GROUP BY 1) USING (m)),
ded_gap AS (
  SELECT s.m, SUM(r.gap) deduction_source_gap_rub
  FROM (SELECT DISTINCT DATE_TRUNC(date_msk, MONTH) m FROM snap) s
  JOIN ded_rec r ON r.m IN (s.m, DATE_ADD(s.m, INTERVAL 1 MONTH)) GROUP BY 1),
storage_fin AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m, SUM(IFNULL(SAFE_CAST(storage_fee AS NUMERIC), 0)) storage_finance_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name IN ('Хранение', 'Коррекция хранения') GROUP BY 1),
ads AS (
  SELECT DATE_TRUNC(day, MONTH) m, SUM(ad_spend_financial_rub) ads_billing_rub,
    COUNTIF(ads_billing_is_final) ads_final_days, COUNT(*) ads_days
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` GROUP BY 1),
acc AS (
  SELECT service_month m,
    SUM(IF(category = 'MINIMUM_PAYMENT', amount_rub, 0)) minimum_payment_rub,
    SUM(IF(category = 'UTILIZATION', amount_rub, 0)) utilization_rub,
    SUM(IF(category = 'PENALTY', amount_rub, 0)) penalty_rub,
    SUM(IF(category = 'TRANSIT', amount_rub, 0)) transit_rub,
    SUM(IF(category = 'ACCEPTANCE', amount_rub, 0)) acceptance_rub,
    SUM(IF(treatment = 'ACCOUNT_LEVEL' AND category NOT IN ('MINIMUM_PAYMENT', 'UTILIZATION', 'PENALTY', 'TRANSIT', 'ACCEPTANCE'), amount_rub, 0)) other_marketplace_cost_rub,
    -SUM(IF(treatment = 'ACCOUNT_LEVEL_INCOME', amount_rub, 0)) marketplace_income_rub,
    SUM(IF(treatment = 'UNKNOWN', ABS(amount_rub), 0)) unknown_rub,
    COUNTIF(category = 'MINIMUM_PAYMENT') minimum_payment_invoices,
    COUNTIF(category = 'UTILIZATION') utilization_invoices,
    STRING_AGG(DISTINCT CONCAT(category, ':', CAST(booking_date AS STRING)), ', ') account_bookings
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_STORE_ACCOUNT_LEDGER` GROUP BY 1),
memo AS (
  SELECT DATE_TRUNC(_rr_date, MONTH) m,
    SUM(IFNULL(SAFE_CAST(rebill_logistics AS NUMERIC), 0)) + SUM(IFNULL(SAFE_CAST(JSON_VALUE(raw_json, '$.ppvzReward') AS NUMERIC), 0)) memo_reimbursement_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE supplier_oper_name LIKE 'Возмещение%' GROUP BY 1),
j AS (
  SELECT sku.*, fs.final_through, fs.any_through,
    LAST_DAY(sku.m) month_end,
    IFNULL(sf.storage_finance_rub, 0) storage_finance_rub,
    IFNULL(ads.ads_billing_rub, 0) ads_billing_rub, IFNULL(ads.ads_final_days, 0) ads_final_days, IFNULL(ads.ads_days, 0) ads_days,
    IFNULL(acc.minimum_payment_rub, 0) minimum_payment_rub, IFNULL(acc.utilization_rub, 0) utilization_rub,
    IFNULL(acc.penalty_rub, 0) penalty_rub, IFNULL(acc.transit_rub, 0) transit_rub, IFNULL(acc.acceptance_rub, 0) acceptance_rub,
    IFNULL(acc.other_marketplace_cost_rub, 0) other_marketplace_cost_rub, IFNULL(acc.marketplace_income_rub, 0) marketplace_income_rub,
    IFNULL(acc.unknown_rub, 0) unknown_rub, IFNULL(acc.minimum_payment_invoices, 0) minimum_payment_invoices,
    IFNULL(acc.utilization_invoices, 0) utilization_invoices, acc.account_bookings,
    IFNULL(memo.memo_reimbursement_rub, 0) memo_reimbursement_rub, IFNULL(u.pending_rows, 0) pending_rows,
    IFNULL(u.unconsumed_finance_rub, 0) unconsumed_finance_rub, IFNULL(dg.deduction_source_gap_rub, 0) deduction_source_gap_rub,
    IFNULL(cov.final_month, FALSE) final_month, IFNULL(cov.final_m20, FALSE) final_m20, IFNULL(cov.final_m46, FALSE) final_m46,
    IFNULL(fe.orphan_base_rub, 0) orphan_finance_base_rub, IFNULL(fe.orphan_logistics_rub, 0) orphan_finance_logistics_rub
  FROM sku CROSS JOIN fin_status fs
  LEFT JOIN storage_fin sf ON sf.m = sku.m LEFT JOIN ads ON ads.m = sku.m LEFT JOIN acc ON acc.m = sku.m
  LEFT JOIN memo ON memo.m = sku.m LEFT JOIN uncovered u ON u.m = sku.m LEFT JOIN fin_extra fe ON fe.m = sku.m
  LEFT JOIN cov ON cov.m = sku.m LEFT JOIN ded_gap dg ON dg.m = sku.m),
k AS (
  SELECT j.*,
    j.ads_sku_rub - j.ads_billing_rub ads_adjustment_rub,
    j.storage_sku_rub - j.storage_finance_rub storage_adjustment_rub,
    j.logistics_sku_rub - j.logistics_finance_rub logistics_adjustment_rub,
    -- Все флаги — по ДАННЫМ (покрытие суток FINAL-отчётами), не по календарю, и NULL = FALSE (не «закрыт»):
    -- когорта созрела — FINAL покрывает месяц + 46 суток (срок второго плеча отказа),
    j.final_m46 cohort_mature,
    -- окно счетов кабинета закрыто — FINAL покрывает месяц + 20 суток (мин. платёж и утилизация проводятся ~10–13-го),
    j.final_m20 account_invoice_window_closed,
    j.final_month month_finance_final,
    IFNULL(j.snapshot_lcd >= j.month_end, FALSE) sku_month_closed
  FROM j),
n AS (
  SELECT k.*,
    minimum_payment_rub + utilization_rub + penalty_rub + transit_rub + acceptance_rub + other_marketplace_cost_rub marketplace_costs_rub,
    ads_adjustment_rub + storage_adjustment_rub + commission_adjustment_rub + price_adjustment_rub reconciliation_adjustments_rub,
    IF(cohort_mature, logistics_adjustment_rub - unsettled_margin_rub, 0) mature_cohort_adjustment_rub
  FROM k)
SELECT
  FORMAT_DATE('%Y-%m', m) month, m service_month, last_day sku_last_day,
  -- Выручка: до созревания — ожидаемая (выкупы листа по цене продавца, поправленной на факт); после —
  -- база финотчёта WB по проданным единицам когорты (непроданные единицы сняты и из прибыли).
  IF(cohort_mature, seller_base_rub, revenue_model_rub + price_adjustment_rub) revenue_rub,
  sku_contribution_rub,
  minimum_payment_rub, utilization_rub, penalty_rub, transit_rub, acceptance_rub, other_marketplace_cost_rub,
  marketplace_costs_rub, marketplace_income_rub,
  ads_adjustment_rub, storage_adjustment_rub, commission_adjustment_rub, price_adjustment_rub, reconciliation_adjustments_rub,
  -- Логистика и непроданные единицы: пока когорта НЕ созрела — только раскрытие (timing bridge), в прибыль
  -- не входит: разница там — ещё не выставленные плечи и ещё не проданные единицы, а не экономия. После
  -- созревания — остаток (логистика когорты по факту, ожидаемый вклад непроданных единиц снимается).
  mature_cohort_adjustment_rub,
  IF(cohort_mature, 0, logistics_adjustment_rub) timing_bridge_logistics_rub,
  IF(cohort_mature, 0, unsettled_margin_rub) timing_bridge_unsettled_margin_rub,
  sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
    + marketplace_income_rub management_net_store_profit_rub,
  SAFE_DIVIDE(sku_contribution_rub + reconciliation_adjustments_rub + mature_cohort_adjustment_rub - marketplace_costs_rub
    + marketplace_income_rub, IF(cohort_mature, seller_base_rub, revenue_model_rub + price_adjustment_rub)) management_net_store_margin,
  CASE WHEN unknown_rub > 0.5 OR orphan_finance_base_rub != 0 OR orphan_finance_logistics_rub != 0
         OR unconsumed_finance_rub > 0.5 OR deduction_source_gap_rub > 0.5 THEN 'UNKNOWN_COST_PRESENT'
       WHEN pending_rows > 0 THEN 'PENDING_CLASSIFICATION'
       -- месяц ещё идёт в листе (или снимок отстал) — счета за него заведомо впереди
       WHEN NOT sku_month_closed THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT account_invoice_window_closed AND (minimum_payment_invoices = 0 OR utilization_invoices = 0)
         THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT month_finance_final OR ads_days = 0 OR ads_final_days < ads_days THEN 'PARTIAL_AWAITING_ACCOUNT_INVOICE'
       WHEN NOT cohort_mature THEN 'FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE'
       ELSE 'FINANCIAL_COMPLETE' END financial_state,
  -- Тождество ФОРМЫ (C2): та же прибыль, собранная сверху вниз (финотчёт когорты, биллинг рекламы, налог/COGS проданных
  -- единиц, разрыв модели листа). Алгебраически равна полной прибыли (чистая + мост сроков) — это проверка, что ни
  -- одна поправка не вошла в формулу дважды и не выпала при правке вью, а НЕ проверка полноты данных. Полноту данных
  -- доказывают: покрытие финотчёта (unconsumed_finance_rub), сверка источников удержаний (deduction_source_gap_rub),
  -- финансы без SKU листа (orphan_*) и сверка снимка с итоговой колонкой листа (загрузчик, parse.ts).
  seller_base_rub - wb_take_rub - logistics_finance_rub - storage_finance_rub - ads_billing_rub - sold_tax_cogs_rub
    + ads_out_rub + model_gap_rub - marketplace_costs_rub + marketplace_income_rub topdown_net_rub,
  (sku_contribution_rub + reconciliation_adjustments_rub + logistics_adjustment_rub - unsettled_margin_rub
    - marketplace_costs_rub + marketplace_income_rub)
  - (seller_base_rub - wb_take_rub - logistics_finance_rub - storage_finance_rub - ads_billing_rub - sold_tax_cogs_rub
    + ads_out_rub + model_gap_rub - marketplace_costs_rub + marketplace_income_rub) double_count_residual_rub,
  -- раскрытие и QA
  revenue_model_rub, seller_base_rub, wb_take_rub, sold_tax_cogs_rub, ads_out_rub, model_gap_rub,
  ads_sku_rub, ads_billing_rub, ads_final_days, ads_days,
  storage_sku_rub, storage_finance_rub, logistics_sku_rub, logistics_finance_rub,
  logistics_adjustment_rub, realized_qty, sold_qty, unsettled_qty, oversold_qty, unsettled_revenue_rub, unsettled_margin_rub,
  memo_reimbursement_rub, unknown_rub, pending_rows, unconsumed_finance_rub, deduction_source_gap_rub,
  orphan_finance_base_rub, orphan_finance_logistics_rub,
  minimum_payment_invoices, utilization_invoices, account_bookings,
  cohort_mature, account_invoice_window_closed, month_finance_final, sku_month_closed, final_through, snapshot_lcd,
  model_gap_abs_rub,
  (SELECT MAX(snapshot_id) FROM `project-fa311fc0-4d87-4781-986.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`) snapshot_id
FROM n;
