-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_STORE_FINANCE_COVERAGE (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3: доказательство «каждый рубль ровно один раз» по месяцу начисления (аналог
-- wb_mart.V_WB_STORE_FINANCE_COVERAGE). Деньги RAW считаются здесь независимо от леджера тем же
-- тождеством начисления; остаток RAW − леджер обязан быть 0,00. unconsumed_rub — деньги строк без
-- колонки P&L (тип вне карты или PENDING), unlinked_posting_rub — строки с месяцем заказа без
-- найденного отправления: любое ≠ 0 переводит месяц в PARTIAL_SOURCE_MISSING.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_FINANCE_COVERAGE`
OPTIONS (description = "Покрытие денег Ozon леджером P&L магазина (D3), зерно = месяц начисления. raw_money_rub - деньги RAW_OZON_FINANCE_ACCRUAL (seller_base x количество + commission + amount_rub), посчитанные независимо; ledger_money_rub - деньги V_OZON_STORE_ACCOUNT_LEDGER без документов СНГ; conservation_residual_rub обязан быть 0,00. unconsumed_rub (нет колонки P&L) и unlinked_posting_rub (месяц заказа без отправления) обязаны быть 0, иначе месяц PARTIAL_SOURCE_MISSING. pending_issue_rows - строки V_OZON_FINANCE_NEW_OPERATIONS.")
AS
WITH
q AS (
  SELECT posting_number, sku, MAX(quantity) q
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  GROUP BY 1, 2),
raw AS (
  SELECT DATE_TRUNC(a.event_date, MONTH) m, COUNT(*) raw_rows,
    SUM(IFNULL(a.seller_base_price_rub * COALESCE(a.quantity, q.q, 1), 0)
        + IFNULL(a.commission_rub, 0) + IFNULL(a.amount_rub, 0)) raw_money_rub,
    SUM(IFNULL(a.ozon_bonus_rub, 0)) memo_ozon_bonus_rub,
    SUM(IFNULL(a.ozon_coinvestment_rub, 0)) memo_ozon_coinvestment_rub,
    SUM(IFNULL(a.buyer_paid_price_rub, 0)) memo_buyer_paid_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` a
  LEFT JOIN q ON q.posting_number = a.posting_number AND q.sku = a.sku
  GROUP BY 1),
led AS (
  SELECT booking_month m,
    COUNTIF(parent_key IS NULL) ledger_root_rows,
    SUM(money_rub) ledger_money_rub,
    SUM(IF(pnl_column IS NULL, money_rub, 0)) unconsumed_rub,
    SUM(IF(posting_link_required AND NOT posting_linked, money_rub, 0)) unlinked_posting_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`
  WHERE source != 'CIS_DOCUMENT'
  GROUP BY 1),
pend AS (
  SELECT booking_month m, COUNT(*) pending_issue_rows
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS`
  GROUP BY 1)
SELECT
  COALESCE(raw.m, led.m) booking_month,
  IFNULL(raw.raw_rows, 0) raw_rows,
  IFNULL(led.ledger_root_rows, 0) ledger_root_rows,
  IFNULL(raw.raw_money_rub, 0) raw_money_rub,
  IFNULL(led.ledger_money_rub, 0) ledger_money_rub,
  IFNULL(raw.raw_money_rub, 0) - IFNULL(led.ledger_money_rub, 0) conservation_residual_rub,
  IFNULL(led.unconsumed_rub, 0) unconsumed_rub,
  IFNULL(led.unlinked_posting_rub, 0) unlinked_posting_rub,
  IFNULL(pend.pending_issue_rows, 0) pending_issue_rows,
  IFNULL(raw.memo_ozon_bonus_rub, 0) memo_ozon_bonus_rub,
  IFNULL(raw.memo_ozon_coinvestment_rub, 0) memo_ozon_coinvestment_rub,
  IFNULL(raw.memo_buyer_paid_rub, 0) memo_buyer_paid_rub
FROM raw
FULL JOIN led USING (m)
LEFT JOIN pend ON pend.m = COALESCE(raw.m, led.m);
