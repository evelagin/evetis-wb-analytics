-- Строка отправления FBO (отправление × SKU) в текущем состоянии. Сутки заказа — по времени
-- Москвы (сутки площадки), а не по UTC-дате из RAW. Статус — текущий: история статусов
-- через API не восстанавливается. Отмена — любой статус Ozon, начинающийся с cancelled.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE`
OPTIONS(description = 'Отправление FBO × SKU, текущее состояние; order_date_msk — сутки заказа по Москве.')
AS
SELECT
  p.posting_number, p.sku, p.order_id, p.order_number, p.status, p.substatus,
  STARTS_WITH(p.status, 'cancelled') AS is_cancelled,
  p.status = 'delivered' AS is_delivered,
  p.created_at, DATE(p.created_at, 'Europe/Moscow') AS order_date_msk, p.in_process_at,
  p.cancel_reason_id, p.quantity, p.price_rub, p.old_price_rub, p.total_discount_value_rub,
  p.commission_amount_rub, p.payout_rub, p.warehouse_id, p.warehouse_name, p.city,
  p.extracted_at, p.ingestion_run_id
FROM `__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO` p
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.posting_number, p.sku ORDER BY p.extracted_at DESC, p.ingestion_run_id DESC) = 1;
