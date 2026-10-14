-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3: детектор неразобранных начислений (аналог wb_mart.V_WB_FINANCE_NEW_OPERATIONS). Любая строка
-- здесь переводит месяц в PENDING_CLASSIFICATION (V_OZON_STORE_MONTH_STATE) и блокирует публикацию.
-- Ловит: type_id вне карты; класс PENDING_CLASSIFICATION (синтетический −2); смену operation_name;
-- знак amount_rub против expected_sign; дату вне valid_from/valid_to; строку с месяцем заказа без
-- найденного отправления; экономический блок на типе, который его не несёт; Premium без
-- разбираемого периода.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS`
OPTIONS (description = "Неразобранные начисления Ozon (D3), зерно = строка RAW_OZON_FINANCE_ACCRUAL x причина. issue: UNKNOWN_TYPE, PENDING_CLASS, NAME_CHANGED, SIGN_UNEXPECTED, OUTSIDE_VALID_PERIOD, POSTING_NOT_LINKED, ECON_ON_UNEXPECTED_TYPE, PREMIUM_PERIOD_UNPARSED. Пустой результат - условие публикации P&L магазина; строка переводит месяц в PENDING_CLASSIFICATION.")
AS
WITH
map AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_OPERATION_MAP`),
led AS (
  SELECT ledger_key, type_id, pnl_month, posting_link_required, posting_linked, link_method
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  WHERE source = 'RAW'),
r AS (
  SELECT a.event_date, a.accrual_id, a.type_id, a.operation_name, a.sku, a.posting_number, a.unit_number,
    a.amount_rub, a.seller_base_price_rub,
    CONCAT(CAST(a.accrual_id AS STRING), '|', CAST(a.type_id AS STRING), '|', IFNULL(a.sku, '')) row_key,
    m.type_id map_type_id, m.operation_name map_name, m.pnl_class, m.expected_sign, m.valid_from, m.valid_to
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` a
  LEFT JOIN map m ON m.type_id = a.type_id),
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
    IF(r.type_id = 52 AND l.ledger_key IS NOT NULL, 'PREMIUM_PERIOD_UNPARSED', NULL)
  ]) issue
  WHERE issue IS NOT NULL)
SELECT
  event_date, DATE_TRUNC(event_date, MONTH) booking_month, IFNULL(pnl_month, DATE_TRUNC(event_date, MONTH)) pnl_month, accrual_id, type_id, operation_name,
  map_name expected_operation_name, pnl_class, expected_sign, issue,
  sku, posting_number, unit_number, amount_rub, seller_base_price_rub
FROM issues;
