-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3 «Ozon Магазин P&L»: леджер денег Ozon, из которого сверху вниз строится V_OZON_STORE_PNL_MONTHLY.
-- Одна строка = одна строка RAW_OZON_FINANCE_ACCRUAL (ключ accrual_id|type_id|sku) или один документ
-- выкупа СНГ (V_OZON_CIS_BUYOUT × строка отправления). Premium (52) раскладывается на дочерние строки
-- по месяцам периода подписки пропорционально дням; копейки — наибольшим остатком, сумма дочерних
-- строк = родитель до копейки (parent_key).
-- Связь с отправлением (месяц заказа, сутки МСК created_at как в FCT): posting_number строки →
-- unit_number как posting_number → unit_number как order_number + sku. Отправления агрегируются до
-- ключа заранее (одна дата на ключ), карта SKU — только однозначные соответствия, поэтому соединения
-- не размножают строки.
-- Деньги строки: money_rub = seller_base_rub (seller_base_price × количество) + commission_rub + amount_rub —
-- то же тождество, что у D1: total_amount начисления = Σ amount_rub + Σ (sale_amount + commission).
-- Количество: quantity строки (есть у синтетических −1/−2), иначе количество отправления, иначе 1.
-- MEMO (бонус Ozon, соинвестирование, оплачено покупателем) — справочные поля, в деньги не входят.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
OPTIONS (description = "Леджер денег Ozon для P&L магазина (D3), зерно = строка RAW_OZON_FINANCE_ACCRUAL (accrual_id|type_id|sku), дочерняя строка Premium по месяцу услуги или документ выкупа СНГ. money_rub = seller_base_rub + commission_rub + amount_rub. pnl_month по date_basis карты V_OZON_FINANCE_OPERATION_MAP: ORDER_MONTH - месяц заказа отправления (сутки МСК), BOOKING_MONTH/SERVICE_DAY/CHARGE_DATE - месяц начисления, SERVICE_PERIOD - месяц периода Premium. Строка без связи с отправлением при ORDER_MONTH остаётся на месяце начисления и видна как posting_linked = FALSE. MEMO-поля в деньги не входят.")
AS
WITH
map AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_OPERATION_MAP`),
pk AS (
  SELECT posting_number, sku, ANY_VALUE(order_number) order_number,
    MIN(DATE(created_at, 'Europe/Moscow')) od, MAX(quantity) q, MAX(status) status
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  GROUP BY 1, 2),
pp AS (SELECT posting_number, MIN(od) od FROM pk GROUP BY 1),
po AS (SELECT order_number, sku, MIN(od) od, MIN(posting_number) posting_number FROM pk GROUP BY 1, 2),
skumap AS (
  SELECT marketplace_sku, MIN(internal_sku) internal_sku, COUNT(DISTINCT internal_sku) n
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'
  GROUP BY 1),
cpo_camp AS (
  SELECT DISTINCT campaign_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
  WHERE payment_type = 'CPO'),
raw AS (
  SELECT a.*,
    CONCAT(CAST(a.accrual_id AS STRING), '|', CAST(a.type_id AS STRING), '|', IFNULL(a.sku, '')) row_key,
    m.pnl_class, m.date_basis,
    CASE WHEN m.pnl_column = 'L_OR_M' THEN IF(c.campaign_id IS NOT NULL, 'M', 'L') ELSE m.pnl_column END pnl_column,
    CASE WHEN a.posting_number IS NOT NULL AND p1.posting_number IS NOT NULL THEN 'POSTING_NUMBER'
         WHEN a.posting_number IS NOT NULL THEN 'POSTING_NOT_FOUND'
         WHEN p2.posting_number IS NOT NULL THEN 'UNIT_NUMBER_POSTING'
         WHEN p3.order_number IS NOT NULL THEN 'UNIT_NUMBER_ORDER_SKU'
         WHEN a.unit_number_meaning = 'campaign_id' THEN 'CAMPAIGN_ID'
         WHEN a.sku IS NOT NULL THEN 'SKU_ONLY'
         ELSE 'NONE' END link_method,
    COALESCE(p1.posting_number, p2.posting_number, p3.posting_number) posting_resolved,
    COALESCE(p1.od, p2.od, p3.od) order_date_msk,
    COALESCE(a.quantity, q.q, 1) qty_used
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` a
  LEFT JOIN map m ON m.type_id = a.type_id
  LEFT JOIN cpo_camp c ON a.type_id = 54 AND c.campaign_id = a.unit_number
  LEFT JOIN pp p1 ON p1.posting_number = a.posting_number
  LEFT JOIN pp p2 ON a.posting_number IS NULL AND p2.posting_number = a.unit_number
  LEFT JOIN po p3 ON a.posting_number IS NULL AND p2.posting_number IS NULL
                 AND p3.order_number = a.unit_number AND p3.sku = a.sku
  LEFT JOIN pk q ON q.posting_number = a.posting_number AND q.sku = a.sku),
prem AS (
  SELECT row_key, amount_rub,
    SAFE.PARSE_DATE('%d.%m.%y', SPLIT(unit_number, '-')[SAFE_OFFSET(0)]) p0,
    SAFE.PARSE_DATE('%d.%m.%y', SPLIT(unit_number, '-')[SAFE_OFFSET(1)]) p1
  FROM raw WHERE type_id = 52),
prem_days AS (
  SELECT row_key, DATE_TRUNC(d, MONTH) m, COUNT(*) days
  FROM prem, UNNEST(GENERATE_DATE_ARRAY(p0, DATE_SUB(p1, INTERVAL 1 DAY))) d
  WHERE p0 IS NOT NULL AND p1 > p0
  GROUP BY 1, 2),
prem_exact AS (
  SELECT d.row_key, d.m, d.days, p.p0, p.p1,
    CAST(ROUND(p.amount_rub * 100) AS INT64) total_cents,
    CAST(ROUND(p.amount_rub * 100) AS NUMERIC) * d.days / SUM(d.days) OVER (PARTITION BY d.row_key) exact_cents
  FROM prem_days d JOIN prem p USING (row_key)),
prem_base AS (
  SELECT *, CAST(TRUNC(exact_cents) AS INT64) base_cents,
    ABS(exact_cents - TRUNC(exact_cents)) frac
  FROM prem_exact),
prem_alloc AS (
  SELECT *, total_cents - SUM(base_cents) OVER (PARTITION BY row_key) rem,
    ROW_NUMBER() OVER (PARTITION BY row_key ORDER BY frac DESC, m) rn
  FROM prem_base),
prem_child AS (
  SELECT row_key, m, p0, p1,
    CAST(base_cents + IF(rn <= ABS(rem), SIGN(rem), 0) AS NUMERIC) / 100 amount_child
  FROM prem_alloc),
raw_rows AS (
  SELECT
    r.row_key ledger_key, CAST(NULL AS STRING) parent_key, 'RAW' source,
    r.accrual_id, r.type_id, r.operation_name, r.accrued_category,
    r.event_date booking_date, r.pnl_class, r.pnl_column, r.date_basis, r.link_method,
    r.posting_resolved posting_number, r.order_date_msk, r.sku, r.unit_number,
    r.qty_used, r.seller_base_price_rub, r.commission_rub, r.amount_rub,
    r.buyer_paid_price_rub, r.ozon_bonus_rub, r.ozon_coinvestment_rub,
    CAST(NULL AS DATE) service_period_from, CAST(NULL AS DATE) service_period_to,
    r.ingestion_run_id
  FROM raw r
  LEFT JOIN (SELECT DISTINCT row_key FROM prem_child) sp USING (row_key)
  WHERE sp.row_key IS NULL),
prem_rows AS (
  SELECT
    CONCAT(r.row_key, '|', FORMAT_DATE('%Y-%m', c.m)) ledger_key, r.row_key parent_key, 'RAW_PREMIUM_SPLIT' source,
    r.accrual_id, r.type_id, r.operation_name, r.accrued_category,
    r.event_date booking_date, r.pnl_class, r.pnl_column, r.date_basis, r.link_method,
    CAST(NULL AS STRING) posting_number, CAST(NULL AS DATE) order_date_msk, r.sku, r.unit_number,
    r.qty_used, CAST(NULL AS NUMERIC) seller_base_price_rub, CAST(NULL AS NUMERIC) commission_rub,
    c.amount_child amount_rub,
    CAST(NULL AS NUMERIC) buyer_paid_price_rub, CAST(NULL AS NUMERIC) ozon_bonus_rub,
    CAST(NULL AS NUMERIC) ozon_coinvestment_rub,
    GREATEST(c.p0, c.m) service_period_from,
    LEAST(DATE_SUB(c.p1, INTERVAL 1 DAY), LAST_DAY(c.m)) service_period_to,
    r.ingestion_run_id
  FROM prem_child c JOIN raw r USING (row_key)),
cis_rows AS (
  SELECT
    CONCAT('CIS|', b.posting_number, '|', k.sku) ledger_key, CAST(NULL AS STRING) parent_key, 'CIS_DOCUMENT' source,
    CAST(NULL AS INT64) accrual_id, CAST(NULL AS INT64) type_id,
    CONCAT('Выкуп СНГ по документу ', b.document_number) operation_name, 'CIS_BUYOUT' accrued_category,
    b.document_period_end booking_date, 'DIRECT_SKU' pnl_class, 'B' pnl_column, 'ORDER_MONTH' date_basis,
    'POSTING_NUMBER' link_method, b.posting_number, k.od order_date_msk, k.sku, CAST(NULL AS STRING) unit_number,
    IFNULL(k.q, 1) qty_used, b.buyout_proceeds_rub seller_base_price_rub, CAST(0 AS NUMERIC) commission_rub,
    CAST(0 AS NUMERIC) amount_rub,
    CAST(NULL AS NUMERIC) buyer_paid_price_rub, CAST(NULL AS NUMERIC) ozon_bonus_rub,
    CAST(NULL AS NUMERIC) ozon_coinvestment_rub,
    CAST(NULL AS DATE) service_period_from, CAST(NULL AS DATE) service_period_to,
    CAST(NULL AS STRING) ingestion_run_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
  JOIN pk k ON k.posting_number = b.posting_number),
u AS (
  SELECT * FROM raw_rows UNION ALL SELECT * FROM prem_rows UNION ALL SELECT * FROM cis_rows)
SELECT
  u.ledger_key, u.parent_key, u.source, u.accrual_id, u.type_id, u.operation_name, u.accrued_category,
  u.booking_date, DATE_TRUNC(u.booking_date, MONTH) booking_month,
  CASE
    WHEN u.source = 'RAW_PREMIUM_SPLIT' THEN DATE_TRUNC(u.service_period_from, MONTH)
    WHEN u.date_basis = 'ORDER_MONTH' THEN DATE_TRUNC(IFNULL(u.order_date_msk, u.booking_date), MONTH)
    ELSE DATE_TRUNC(u.booking_date, MONTH) END pnl_month,
  u.pnl_class, u.pnl_column, u.date_basis, u.link_method,
  u.date_basis = 'ORDER_MONTH' posting_link_required,
  u.order_date_msk IS NOT NULL posting_linked,
  u.posting_number, u.order_date_msk, u.sku,
  IF(s.n = 1, s.internal_sku, NULL) internal_sku,
  u.unit_number, u.qty_used quantity,
  u.seller_base_price_rub IS NOT NULL is_econ_block,
  CASE WHEN u.seller_base_price_rub > 0 THEN 'SALE'
       WHEN u.seller_base_price_rub < 0 THEN 'REVERSAL' END econ_direction,
  IFNULL(u.seller_base_price_rub * u.qty_used, 0) seller_base_rub,
  IFNULL(u.commission_rub, 0) commission_rub,
  IFNULL(u.amount_rub, 0) amount_rub,
  IFNULL(u.seller_base_price_rub * u.qty_used, 0) + IFNULL(u.commission_rub, 0) + IFNULL(u.amount_rub, 0) money_rub,
  u.buyer_paid_price_rub memo_buyer_paid_rub, u.ozon_bonus_rub memo_ozon_bonus_rub,
  u.ozon_coinvestment_rub memo_ozon_coinvestment_rub,
  u.service_period_from, u.service_period_to, u.ingestion_run_id
FROM u
LEFT JOIN skumap s ON s.marketplace_sku = u.sku;
