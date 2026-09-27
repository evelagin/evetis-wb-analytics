-- Начисления уровня магазина и товара без отправления по суткам начисления: подписка,
-- биллинг продвижения, хранение, логистика магазина, компенсации, неклассифицированное.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_STORE_COSTS_DAILY`
OPTIONS(description = 'Начисления без отправления: сутки начисления × класс × SKU (NULL — уровень магазина).')
AS
SELECT a.event_date, a.accrual_class, a.attribution_scope, a.sku,
  SUM(a.amount_rub) AS amount_rub, COUNT(*) AS accruals
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
WHERE a.posting_number IS NULL
GROUP BY a.event_date, a.accrual_class, a.attribution_scope, a.sku;
