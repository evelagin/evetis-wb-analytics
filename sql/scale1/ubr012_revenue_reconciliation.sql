-- ============================================================================
-- UBR-012 · СВЕРКА КАНОНИЧЕСКОГО ИСТОЧНИКА ВЫРУЧКИ OZON. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: `-- @check <ID>`, колонка status ∈ {PASS, FAIL}.
-- Исполнитель: tools/run_data_checks.py --adapter check_blocks
--
-- До развёртывания объекты pending_deploy ещё не существуют под своими именами:
--   python tools/scale1_predeploy_render.py sql/scale1/ubr012_revenue_reconciliation.sql
--
-- Решение владельца 2026-09-22: это не новое бизнес-правило, а дефект потребителя
-- уже действующего контракта Gate 5K/5M. Канон:
--   * обычная реализация — утверждённая каноническая модель выручки;
--   * CIS_BUYOUT — сумма подтверждённого первичного документа;
--   * цена заказа НЕ является fallback для фактической выручки;
--   * BUYOUT без доказанного документа остаётся UNPROVEN и даёт выручку 0.
--
-- ⚠️ РАЗДЕЛЕНИЕ ИНВАРИАНТОВ И ЯКОРЯ. Инварианты (U01–U03, U06–U12) сформулированы как
-- ОТНОШЕНИЯ и верны при любых данных. Якоря U04/U05 приколоты к величине САМОЙ
-- ПОПРАВКИ и ограничены закрытым окном order_date <= 2026-08-31, где лежат все 35
-- выкупов на момент исследования. Новый выкуп сдвинет якорь — это не ослабление
-- ворот, а сигнал переприколоть его после объяснения дельты.
--
-- U01 — ДЕТЕКЦИОННЫЙ КОНТРАКТ ручного реестра V_OZON_CIS_BUYOUT. Реестр заполняется
-- руками и сам не расширяется. Новый выкуп проявится комбинацией «delivered + нет
-- начисления + нет доказанного документа», которой сегодня не существует ни одной
-- строкой. U01 ловит её и роняет CRITICAL-ворота, вместо того чтобы молча подставить
-- цену заказа.
-- ============================================================================

-- @check U01_DETECTION_UNREGISTERED_BUYOUT
-- ДЕТЕКЦИЯ: доставленная строка без начисления выручки и без первичного документа.
-- Это либо новый выкуп CIS, либо неизвестный класс. И то и другое требует решения
-- владельца, а не автоматической оценки. Ожидание: 0 строк.
WITH acc AS (
  SELECT DISTINCT posting_number, sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
undocumented AS (
  SELECT p.posting_number, p.sku, p.order_date, p.price_rub * p.quantity AS order_price
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  LEFT JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number
  WHERE p.status = 'delivered' AND a.posting_number IS NULL AND b.posting_number IS NULL)
SELECT (SELECT COUNT(*) FROM undocumented) AS undocumented_lines,
       ROUND(IFNULL((SELECT SUM(order_price) FROM undocumented), 0), 2) AS order_price_at_risk,
       IF((SELECT COUNT(*) FROM undocumented) = 0, 'PASS', 'FAIL') AS status;

-- @check U02_BUYOUT_REVENUE_IS_PRIMARY_DOCUMENT
-- ИНВАРИАНТ: выручка класса выкупа равна сумме первичных документов, а не цене заказа.
-- Верен при любых данных: обе стороны считаются по одному и тому же множеству.
WITH acc AS (
  SELECT DISTINCT posting_number, sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
buyout AS (
  SELECT p.posting_number, p.internal_sku, p.order_date, p.quantity, p.price_rub, b.buyout_proceeds_rub
  FROM (SELECT p.*, m.internal_sku
        FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
          ON m.marketplace = 'OZON' AND m.marketplace_sku = p.sku) p
  LEFT JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number
  WHERE p.status = 'delivered' AND a.posting_number IS NULL)
SELECT COUNT(*) AS buyout_lines,
       ROUND(SUM(buyout_proceeds_rub), 2) AS by_document,
       ROUND(SUM(price_rub * quantity), 2) AS by_order_price,
       IF(COUNT(*) > 0 AND ABS(SUM(buyout_proceeds_rub) - SUM(price_rub * quantity)) > 0.01,
          'PASS', 'FAIL') AS status
FROM buyout;

-- @check U03_CT_REVENUE_MATCHES_CANONICAL
-- ИНВАРИАНТ (AC3): выручка Ozon в Control Tower тождественна канонической витрине.
-- После UBR-012 CT её не реконструирует, а читает, поэтому равенство точное.
WITH ct AS (
  SELECT ROUND(SUM(revenue_seller_base), 2) v
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE`
  WHERE marketplace = 'OZON'),
mart AS (
  SELECT ROUND(SUM(seller_base_revenue_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`)
SELECT (SELECT v FROM ct) AS ct_revenue, (SELECT v FROM mart) AS canonical_revenue,
       ROUND((SELECT v FROM ct) - (SELECT v FROM mart), 2) AS delta,
       IF(ABS((SELECT v FROM ct) - (SELECT v FROM mart)) <= 0.01, 'PASS', 'FAIL') AS status;

-- @check U04_ANCHOR_HISTORICAL_CORRECTION
-- ЯКОРЬ (AC1): величина самой поправки за закрытое окно — 12 788,84 ₽.
-- Измеряется как «цена заказа минус документ» по классу выкупа, а не как CT-vs-витрина:
-- после исправления второе равно нулю, а поправка остаётся проверяемой величиной.
WITH acc AS (
  SELECT DISTINCT posting_number, sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
buyout AS (
  SELECT p.order_date, p.quantity, p.price_rub, b.buyout_proceeds_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  LEFT JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number
  WHERE p.status = 'delivered' AND a.posting_number IS NULL
    AND p.order_date <= DATE '2026-08-31')
SELECT COUNT(*) AS lines,
       ROUND(SUM(price_rub * quantity) - SUM(buyout_proceeds_rub), 2) AS correction_rub,
       IF(COUNT(*) = 35
          AND ABS((SUM(price_rub * quantity) - SUM(buyout_proceeds_rub)) - 12788.84) <= 0.01,
          'PASS', 'FAIL') AS status
FROM buyout;

-- @check U05_ANCHOR_AUGUST_CORRECTION
-- ЯКОРЬ (AC2): августовская часть поправки — 986,04 ₽.
WITH acc AS (
  SELECT DISTINCT posting_number, sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
buyout AS (
  SELECT p.quantity, p.price_rub, b.buyout_proceeds_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  LEFT JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number
  WHERE p.status = 'delivered' AND a.posting_number IS NULL
    AND p.order_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31')
SELECT COUNT(*) AS lines,
       ROUND(SUM(price_rub * quantity) - SUM(buyout_proceeds_rub), 2) AS correction_rub,
       IF(ABS((SUM(price_rub * quantity) - SUM(buyout_proceeds_rub)) - 986.04) <= 0.01,
          'PASS', 'FAIL') AS status
FROM buyout;

-- @check U06_BUYOUT_CLASSIFICATION_COMPLETE
-- ИНВАРИАНТ (AC4): структурная сигнатура и доказанный реестр описывают одно множество.
-- Расхождение в любую сторону означает либо новый выкуп, либо мёртвую запись реестра.
WITH acc AS (
  SELECT DISTINCT posting_number, sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
sig AS (
  SELECT DISTINCT p.posting_number
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  LEFT JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  WHERE p.status = 'delivered' AND a.posting_number IS NULL
    AND IFNULL(p.payout_rub, NUMERIC '0') = 0),
reg AS (SELECT DISTINCT posting_number FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT`)
SELECT (SELECT COUNT(*) FROM sig) AS by_signature, (SELECT COUNT(*) FROM reg) AS in_registry,
       (SELECT COUNT(*) FROM sig WHERE posting_number NOT IN (SELECT posting_number FROM reg)) AS sig_not_registered,
       (SELECT COUNT(*) FROM reg WHERE posting_number NOT IN (SELECT posting_number FROM sig)) AS registered_not_sig,
       IF((SELECT COUNT(*) FROM sig WHERE posting_number NOT IN (SELECT posting_number FROM reg)) = 0
          AND (SELECT COUNT(*) FROM reg WHERE posting_number NOT IN (SELECT posting_number FROM sig)) = 0,
          'PASS', 'FAIL') AS status;

-- @check U07_NO_DOUBLE_COUNTING
-- ИНВАРИАНТ (AC4): ни одно отправление не несёт одновременно начисление реализации
-- и первичный документ выкупа. Иначе выручка была бы признана дважды.
WITH acc AS (
  SELECT DISTINCT posting_number
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL)
SELECT COUNT(*) AS both_sources,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM acc JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b USING (posting_number);

-- @check U08_ORDINARY_SALES_UNCHANGED
-- ИНВАРИАНТ (AC5): выручка обычных продаж равна сумме начислений реализации.
-- Экономика обычных продаж изменением не затронута.
WITH acc AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1, 2),
ordinary AS (
  SELECT ROUND(SUM(a.sp * p.quantity), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN acc a ON a.posting_number = p.posting_number AND a.sku = p.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace = 'OZON' AND m.marketplace_sku = p.sku
  WHERE p.status = 'delivered'),
mart AS (
  SELECT ROUND(SUM(seller_base_revenue_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`),
buyout AS (
  SELECT ROUND(IFNULL(SUM(b.buyout_proceeds_rub), 0), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number = p.posting_number
  WHERE p.status = 'delivered')
SELECT (SELECT v FROM ordinary) AS ordinary_revenue, (SELECT v FROM buyout) AS buyout_revenue,
       (SELECT v FROM mart) AS mart_total,
       IF(ABS((SELECT v FROM ordinary) + (SELECT v FROM buyout) - (SELECT v FROM mart)) <= 0.01,
          'PASS', 'FAIL') AS status;

-- @check U09_NON_DELIVERED_NO_REVENUE
-- ИНВАРИАНТ (AC6): отменённые и недоставленные заказы не создают выручку.
-- Витрина признаёт выручку только по status = 'delivered'.
WITH not_delivered AS (
  SELECT SUM(p.quantity) qty, ROUND(SUM(p.price_rub * p.quantity), 2) order_price
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace = 'OZON' AND m.marketplace_sku = p.sku
  WHERE p.status != 'delivered'),
mart AS (
  SELECT SUM(realized_qty) realized, SUM(cancelled_qty + in_transit_qty) not_realized
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`)
SELECT (SELECT qty FROM not_delivered) AS not_delivered_qty,
       (SELECT order_price FROM not_delivered) AS order_price_excluded,
       (SELECT not_realized FROM mart) AS mart_not_realized_qty,
       IF((SELECT qty FROM not_delivered) = (SELECT not_realized FROM mart), 'PASS', 'FAIL') AS status;

-- @check U10_UNPROVEN_IS_FAIL_CLOSED
-- ИНВАРИАНТ (AC7): недоказанный выкуп не превращается в выручку. Величина названа
-- в buyout_revenue_unproven_rub, но в seller_base_revenue_rub не входит.
-- Сегодня недоказанных нет; проверка охраняет будущее состояние.
SELECT SUM(buyout_revenue_unproven_qty) AS unproven_qty,
       ROUND(SUM(buyout_revenue_unproven_rub), 2) AS unproven_rub,
       SUM(commission_not_applicable_qty) AS buyout_qty,
       IF(SUM(buyout_revenue_unproven_qty) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`;

-- @check U11_CT_ROW_SET_IDENTICAL
-- ИНВАРИАНТ: множества строк Control Tower и витрины тождественны. Это условие
-- корректности LEFT JOIN за выручкой: иначе строка витрины потеряла бы выручку молча.
WITH ct AS (
  SELECT d, internal_sku FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY_LIVE`
  WHERE marketplace = 'OZON'),
f AS (
  SELECT fact_date d, internal_sku FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`)
SELECT (SELECT COUNT(*) FROM ct) AS ct_rows, (SELECT COUNT(*) FROM f) AS mart_rows,
       (SELECT COUNT(*) FROM f WHERE (d, internal_sku) NOT IN (SELECT (d, internal_sku) FROM ct)) AS mart_not_in_ct,
       (SELECT COUNT(*) FROM ct WHERE (d, internal_sku) NOT IN (SELECT (d, internal_sku) FROM f)) AS ct_not_in_mart,
       IF((SELECT COUNT(*) FROM f WHERE (d, internal_sku) NOT IN (SELECT (d, internal_sku) FROM ct)) = 0
          AND (SELECT COUNT(*) FROM ct WHERE (d, internal_sku) NOT IN (SELECT (d, internal_sku) FROM f)) = 0,
          'PASS', 'FAIL') AS status;

-- @check U12_PROMOTION_L3_NO_REGRESSION
-- РЕГРЕССИЯ (AC9): UBR-010 не откатился. Продвижение по-прежнему вне marketplace_costs
-- и вне ad_spend, а вклад витрины не включает его до слоя L3.
WITH raw_sum AS (
  SELECT ROUND(SUM(-amount_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE type_id IN (116, 74, 48)),
daily AS (
  SELECT ROUND(SUM(sku_promotion_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`),
neutral AS (
  SELECT ROUND(SUM(promotion_billed_rub), 2) v
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`
  WHERE marketplace = 'OZON')
SELECT (SELECT v FROM raw_sum) AS raw_accrual, (SELECT v FROM daily) AS ozon_daily,
       (SELECT v FROM neutral) AS neutral_fact,
       IF(ABS((SELECT v FROM daily) - (SELECT v FROM raw_sum)) <= 0.01
          AND ABS((SELECT v FROM neutral) - (SELECT v FROM raw_sum)) <= 0.01, 'PASS', 'FAIL') AS status;
