-- Расчёты Ozon по отправлению × SKU: цена продавца за единицу (экономический блок товара),
-- комиссия и привязанные к отправлению начисления по классам.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_POSTING_SETTLEMENT`
OPTIONS(description = 'Расчёты по отправлению × SKU: цена продавца за единицу, комиссия, начисления по классам.')
AS
SELECT
  a.posting_number, a.sku,
  MIN(a.event_date) AS first_settlement_date,
  MAX(a.event_date) AS last_settlement_date,
  ARRAY_AGG(a.seller_base_price_rub IGNORE NULLS ORDER BY a.event_date, a.accrual_id LIMIT 1)[SAFE_OFFSET(0)] AS seller_price_unit_rub,
  SUM(IFNULL(a.commission_rub, 0)) AS commission_rub,
  SUM(IF(a.accrual_class = 'LOGISTICS', a.amount_rub, 0)) AS logistics_rub,
  SUM(IF(a.accrual_class = 'LAST_MILE', a.amount_rub, 0)) AS last_mile_rub,
  SUM(IF(a.accrual_class = 'ACQUIRING', a.amount_rub, 0)) AS acquiring_rub,
  SUM(IF(a.accrual_class = 'RETURN_LOGISTICS', a.amount_rub, 0)) AS return_logistics_rub,
  SUM(IF(a.accrual_class IN ('CANCELLATION_COST', 'OTHER_MARKETPLACE_COST'), a.amount_rub, 0)) AS other_costs_rub,
  SUM(IF(a.accrual_class = 'UNCLASSIFIED', a.amount_rub, 0)) AS unclassified_rub,
  SUM(a.amount_rub) AS accrual_amount_rub
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
WHERE a.posting_number IS NOT NULL
GROUP BY a.posting_number, a.sku;
