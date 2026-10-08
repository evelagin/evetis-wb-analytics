-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_ADS_CPO_PROMOTED_DAILY (VIEW) · sync_state: pending_deploy
-- Phase B (OWNER ACK 2026-10-08). Rules: sql/current/README.md. Док: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md.
--
-- МАРКЕТИНГОВАЯ атрибуция «Оплаты за заказ»: на какой ПРОДВИГАЕМЫЙ товар потрачено и какие заказы он
-- привёл (в т. ч. заказы ДРУГИХ товаров — cross_sku_*). Зерно = business_date (сутки МСК заказа) ×
-- продвигаемый SKU. Для эффективности продвижения, а НЕ для P&L: финансово расход несёт заказанный
-- товар (FCT_OZON_SKU_PNL_DAILY.cpo_expense_rub). Суммы двух атрибуций по всем SKU равны; по одному
-- SKU — нет, и складывать их нельзя.
-- Internal dependencies: V_OZON_ADS_CPO_ORDERS.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_PROMOTED_DAILY`
OPTIONS (description = "Маркетинговая атрибуция Оплаты за заказ (CPO) Ozon, зерно = business_date (сутки МСК заказа) x продвигаемый SKU. Расход и заказы, приведённые продвигаемым товаром, включая заказы других товаров (cross_sku_*). Не для P&L: финансово расход несёт заказанный SKU (FCT_OZON_SKU_PNL_DAILY.cpo_expense_rub). Несопоставленный продвигаемый SKU остаётся строкой с promoted_mapping_status UNMAPPED.")
AS
SELECT
  business_date, promoted_sku, promoted_internal_sku, promoted_offer_id, promoted_mapping_status,
  SUM(expense_rub) cpo_promoted_expense_rub,
  COUNT(DISTINCT CONCAT(report_family, '|', order_id)) orders,
  SUM(quantity) ordered_qty,
  SUM(sale_value_rub) ordered_value_rub,
  SUM(IF(ordered_differs_from_promoted, expense_rub, 0)) cross_sku_expense_rub,
  SUM(IF(ordered_differs_from_promoted, sale_value_rub, 0)) cross_sku_ordered_value_rub,
  SUM(IF(order_cancelled, expense_rub, 0)) cancelled_order_expense_rub,
  COUNTIF(business_date_basis = 'CHARGE_DATE_FALLBACK') charge_date_fallback_rows
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
GROUP BY 1, 2, 3, 4, 5;
