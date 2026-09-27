-- Начисление Ozon с классом из таксономии. Знак amount_rub и commission_rub — как у Ozon
-- (расход < 0). Комиссия продажи в amount_rub НЕ входит: она в commission_rub экономического
-- блока товара. Одна строка на ключ записи RAW (accrual_id, type_id, sku) — последняя выгрузка.
-- is_unresolved: сумма неизвестна (NULL) или тип вне таксономии с ненулевой суммой —
-- такое начисление блокирует финансовый результат.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_ACCRUAL`
OPTIONS(description = 'Начисление Ozon с классом таксономии; UNCLASSIFIED — тип вне таксономии; is_unresolved блокирует результат.')
AS
SELECT
  a.event_date, a.accrual_id, a.type_id, a.operation_name, a.accrued_category,
  a.posting_number, a.sku, a.amount_rub, a.commission_rub, a.seller_base_price_rub,
  a.buyer_paid_price_rub, a.ozon_bonus_rub, a.ozon_coinvestment_rub,
  COALESCE(t.accrual_class, 'UNCLASSIFIED') AS accrual_class,
  IF(a.posting_number IS NOT NULL, 'POSTING', IF(a.sku IS NOT NULL, 'SKU', 'STORE')) AS attribution_scope,
  t.type_id IS NULL AS is_unclassified,
  a.amount_rub IS NULL OR (t.type_id IS NULL AND a.amount_rub != 0) AS is_unresolved,
  a.seller_base_price_rub IS NOT NULL OR a.commission_rub IS NOT NULL AS is_economic_block,
  a.extracted_at, a.ingestion_run_id
FROM `__tenant__.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` a
LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_ACCRUAL_TYPE` t ON t.type_id = a.type_id
QUALIFY ROW_NUMBER() OVER (PARTITION BY a.accrual_id, a.type_id, a.sku ORDER BY a.extracted_at DESC) = 1;
