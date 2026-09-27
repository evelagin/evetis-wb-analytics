-- Расчёты Ozon по отправлению × SKU: экономический блок товара (цена продавца за единицу,
-- комиссия), начисления по классам и признаки неполноты. Сумма колонок классов равна
-- accrual_amount_rub. Комиссия — со знаком Ozon (расход < 0); NULL — блока нет.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_OZON_POSTING_SETTLEMENT`
OPTIONS(description = 'Расчёты по отправлению × SKU: экономический блок, комиссия, начисления по классам, признаки неполноты.')
AS
SELECT
  a.posting_number, a.sku,
  MIN(a.event_date) AS first_settlement_date,
  MAX(a.event_date) AS last_settlement_date,
  COUNTIF(a.is_economic_block) AS economic_blocks,
  COUNTIF(a.is_economic_block AND (a.seller_base_price_rub IS NULL OR a.commission_rub IS NULL)) AS incomplete_economic_blocks,
  MAX(a.seller_base_price_rub) AS seller_price_unit_rub,
  SUM(a.commission_rub) AS commission_rub,
  SUM(IF(a.accrual_class = 'LOGISTICS', a.amount_rub, 0)) AS logistics_rub,
  SUM(IF(a.accrual_class = 'LAST_MILE', a.amount_rub, 0)) AS last_mile_rub,
  SUM(IF(a.accrual_class = 'ACQUIRING', a.amount_rub, 0)) AS acquiring_rub,
  SUM(IF(a.accrual_class = 'RETURN_LOGISTICS', a.amount_rub, 0)) AS return_logistics_rub,
  SUM(IF(a.accrual_class IN ('PROMOTION_BILLING', 'PROMOTION_SERVICES'), a.amount_rub, 0)) AS promotion_rub,
  SUM(IF(a.accrual_class NOT IN ('LOGISTICS', 'LAST_MILE', 'ACQUIRING', 'RETURN_LOGISTICS',
                                 'PROMOTION_BILLING', 'PROMOTION_SERVICES', 'UNCLASSIFIED'), a.amount_rub, 0)) AS other_costs_rub,
  SUM(IF(a.accrual_class = 'UNCLASSIFIED', a.amount_rub, 0)) AS unclassified_rub,
  COUNTIF(a.is_unresolved) AS unresolved_accruals,
  SUM(a.amount_rub) AS accrual_amount_rub
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
WHERE a.posting_number IS NOT NULL
GROUP BY a.posting_number, a.sku;
