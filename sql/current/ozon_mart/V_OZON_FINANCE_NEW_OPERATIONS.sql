-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3: детектор неразобранных начислений (аналог wb_mart.V_WB_FINANCE_NEW_OPERATIONS). Любая строка
-- здесь переводит месяц в PENDING_CLASSIFICATION (V_OZON_STORE_MONTH_STATE) и блокирует публикацию.
-- Ловит: type_id вне карты; класс PENDING_CLASSIFICATION (синтетический −2); смену operation_name;
-- знак amount_rub против expected_sign; дату вне valid_from/valid_to; строку с месяцем заказа без
-- найденного отправления; экономический блок на типе, который его не несёт; Premium без
-- разбираемого периода; тип 54 с кампанией, которой нет среди CPC/CPO-кампаний; документ выкупа СНГ
-- без отправления, на отправлении с экономическим блоком или на отправлении с несколькими SKU.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS`
OPTIONS (description = "Неразобранные начисления Ozon (D3), зерно = строка RAW_OZON_FINANCE_ACCRUAL или документ выкупа СНГ x причина. issue: UNKNOWN_TYPE, PENDING_CLASS, NAME_CHANGED, SIGN_UNEXPECTED, OUTSIDE_VALID_PERIOD, POSTING_NOT_LINKED, ECON_ON_UNEXPECTED_TYPE, PREMIUM_PERIOD_UNPARSED, CAMPAIGN_UNRESOLVED, CIS_DOC_WITHOUT_POSTING, CIS_DOC_WITH_ECON_BLOCK, CIS_DOC_MULTI_SKU. Пустой результат - условие публикации P&L магазина; строка переводит месяц в PENDING_CLASSIFICATION.")
AS
WITH
map AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_OPERATION_MAP`),
led AS (
  SELECT ledger_key, type_id, pnl_month, posting_link_required, posting_linked, link_method
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  WHERE source = 'RAW'),
camp AS (
  SELECT DISTINCT campaign_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
  WHERE payment_type IN ('CPC', 'CPO')),
r AS (
  SELECT a.event_date, a.accrual_id, a.type_id, a.operation_name, a.sku, a.posting_number, a.unit_number,
    a.amount_rub, a.seller_base_price_rub,
    CONCAT(CAST(a.accrual_id AS STRING), '|', CAST(a.type_id AS STRING), '|', IFNULL(a.sku, '')) row_key,
    m.type_id map_type_id, m.operation_name map_name, m.pnl_class, m.expected_sign, m.valid_from, m.valid_to,
    a.type_id = 54 AND c.campaign_id IS NULL campaign_unresolved
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` a
  LEFT JOIN map m ON m.type_id = a.type_id
  LEFT JOIN camp c ON c.campaign_id = a.unit_number),
issues AS (
  SELECT r.*, l.pnl_month, issue
  FROM r
  LEFT JOIN led l ON l.ledger_key = r.row_key,
  UNNEST([
    IF(r.map_type_id IS NULL, 'UNKNOWN_TYPE', NULL),
    IF(r.pnl_class = 'PENDING_CLASSIFICATION', 'PENDING_CLASS', NULL),
    IF(r.map_type_id IS NOT NULL AND r.operation_name != r.map_name, 'NAME_CHANGED', NULL),
    IF(CASE r.expected_sign
         WHEN 'NEG' THEN r.amount_rub >= 0
         WHEN 'NEG_OR_ZERO' THEN r.amount_rub > 0
         WHEN 'ZERO' THEN r.amount_rub != 0
         WHEN 'POS' THEN r.amount_rub <= 0
         ELSE FALSE END, 'SIGN_UNEXPECTED', NULL),
    IF(r.event_date < IFNULL(r.valid_from, DATE '1900-01-01')
       OR r.event_date > IFNULL(r.valid_to, DATE '9999-12-31'), 'OUTSIDE_VALID_PERIOD', NULL),
    IF(l.posting_link_required AND NOT l.posting_linked, 'POSTING_NOT_LINKED', NULL),
    IF(r.seller_base_price_rub IS NOT NULL AND r.type_id NOT IN (32, 29, 28, -1, -2), 'ECON_ON_UNEXPECTED_TYPE', NULL),
    IF(r.type_id = 52 AND l.ledger_key IS NOT NULL, 'PREMIUM_PERIOD_UNPARSED', NULL),
    IF(r.campaign_unresolved, 'CAMPAIGN_UNRESOLVED', NULL)
  ]) issue
  WHERE issue IS NOT NULL),
post AS (
  SELECT posting_number, MIN(DATE(created_at, 'Europe/Moscow')) od, COUNT(DISTINCT sku) skus
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  GROUP BY 1),
econ AS (
  SELECT DISTINCT posting_number
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL),
cis AS (
  SELECT b.document_period_end, b.posting_number, b.buyout_proceeds_rub, p.od, issue
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
  LEFT JOIN post p USING (posting_number)
  LEFT JOIN econ e USING (posting_number),
  UNNEST([
    IF(p.posting_number IS NULL, 'CIS_DOC_WITHOUT_POSTING', NULL),
    IF(e.posting_number IS NOT NULL, 'CIS_DOC_WITH_ECON_BLOCK', NULL),
    IF(p.skus > 1, 'CIS_DOC_MULTI_SKU', NULL)
  ]) issue
  WHERE issue IS NOT NULL)
SELECT
  event_date, DATE_TRUNC(event_date, MONTH) booking_month, IFNULL(pnl_month, DATE_TRUNC(event_date, MONTH)) pnl_month, accrual_id, type_id, operation_name,
  map_name expected_operation_name, pnl_class, expected_sign, issue,
  sku, posting_number, unit_number, amount_rub, seller_base_price_rub
FROM issues
UNION ALL
SELECT
  document_period_end, DATE_TRUNC(document_period_end, MONTH), DATE_TRUNC(IFNULL(od, document_period_end), MONTH),
  CAST(NULL AS INT64), CAST(NULL AS INT64), 'Выкуп СНГ по документу', CAST(NULL AS STRING), 'DIRECT_SKU',
  CAST(NULL AS STRING), issue, CAST(NULL AS STRING), posting_number, CAST(NULL AS STRING), CAST(NULL AS NUMERIC),
  buyout_proceeds_rub
FROM cis;
