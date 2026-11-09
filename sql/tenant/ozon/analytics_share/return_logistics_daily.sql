-- Обратная логистика по товару и суткам начисления. Это НЕ число возвратов: сторно возврата
-- покупателя платформа не загружает. Начисление обратной логистики по ДОСТАВЛЕННОМУ отправлению
-- (cost_scope = POSTING_DELIVERED) — признак возврата после выкупа; по недоставленному — невыкуп
-- или отмена; POSTING_NOT_LOADED — отправление вне загруженной истории.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.return_logistics_daily`
OPTIONS(description = 'Обратная логистика: сутки начисления × SKU × уровень (доставленное / недоставленное / вне истории). Признак возвратов, не их число; data_status — полнота начислений.')
AS
SELECT f.event_date, f.sku, d.internal_sku, d.product_name, d.offer_id, d.ozon_name, f.cost_scope,
  f.amount_rub AS return_logistics_rub, f.posting_lines AS postings, f.accruals,
  COALESCE(c.status, 'UNKNOWN') AS data_status
FROM `__tenant__.ozon_mart.FACT_OZON_ACCRUAL_DAILY` f
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku_joinable AND d.sku = f.sku
LEFT JOIN `__tenant__.tenant_ops.V_COVERAGE_DAILY` c ON c.entity = 'finance_accrual' AND c.coverage_date = f.event_date
WHERE f.line_class = 'RETURN_LOGISTICS';
