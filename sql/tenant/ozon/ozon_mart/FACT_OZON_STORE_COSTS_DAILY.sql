-- Начисления, не вошедшие в экономику доставленных заказов, по суткам начисления: уровень
-- магазина и товара (подписка, биллинг продвижения, хранение, компенсации) и начисления по
-- отправлениям, которые не доставлены или не загружены (отмены, невыкупы, отправления вне
-- загруженной истории). Вместе с FACT_OZON_SKU_ECONOMICS_DAILY даёт все начисления без
-- пропусков и повторов. Знак — как у Ozon (расход < 0).
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_STORE_COSTS_DAILY`
OPTIONS(description = 'Начисления вне экономики доставленных заказов: сутки начисления × класс × уровень (cost_scope) × SKU. Вместе с экономикой доставленных — все начисления.')
AS
WITH delivered AS (
  SELECT DISTINCT p.posting_number, p.sku
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  WHERE p.is_delivered
),
postings AS (
  SELECT p.posting_number, LOGICAL_OR(p.is_delivered) AS any_delivered
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  GROUP BY p.posting_number
)
SELECT a.event_date, a.accrual_class,
  CASE
    WHEN a.posting_number IS NULL THEN a.attribution_scope
    WHEN ps.posting_number IS NULL THEN 'POSTING_NOT_LOADED'
    WHEN ps.any_delivered THEN 'POSTING_SKU_UNMATCHED'
    ELSE 'POSTING_NOT_DELIVERED'
  END AS cost_scope,
  a.sku, SUM(a.amount_rub) AS amount_rub, SUM(a.commission_rub) AS commission_rub,
  COUNT(*) AS accruals, COUNTIF(a.is_unresolved) AS unresolved_accruals
FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
LEFT JOIN delivered d ON d.posting_number = a.posting_number AND d.sku = a.sku
LEFT JOIN postings ps ON ps.posting_number = a.posting_number
WHERE d.posting_number IS NULL
GROUP BY a.event_date, a.accrual_class, cost_scope, a.sku;
