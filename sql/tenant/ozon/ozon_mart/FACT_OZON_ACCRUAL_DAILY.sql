-- Все начисления Ozon по суткам начисления: сутки × вид строки × уровень (cost_scope) × SKU.
-- Вид строки: SALE_REVENUE — выручка продавца экономического блока (цена продавца за единицу ×
-- количество строки отправления); SALE_COMMISSION — комиссия продажи; иначе — класс таксономии
-- начисления (amount_rub; комиссия продажи в нём не учтена). Знак — как у Ozon (расход < 0).
-- Сумма amount_rub всех строк суток — результат магазина до себестоимости по дате начисления.
-- Выручка блока без загруженной строки отправления неизвестна (количество неизвестно): amount_rub
-- группы NULL, известная часть — amount_known_rub, число неизвестных — unknown_amounts. Нет данных ≠ 0.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_ACCRUAL_DAILY`
OPTIONS(description = 'Все начисления Ozon: сутки начисления × вид строки (выручка, комиссия продажи, класс начисления) × уровень × SKU; результат магазина по дате начисления.')
AS
WITH lines AS (
  SELECT p.posting_number, p.sku, SUM(p.quantity) AS quantity, LOGICAL_OR(p.is_delivered) AS is_delivered
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  GROUP BY p.posting_number, p.sku
),
postings AS (
  SELECT p.posting_number, LOGICAL_OR(p.is_delivered) AS any_delivered
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  GROUP BY p.posting_number
),
scoped AS (
  SELECT a.event_date, a.posting_number, a.sku, a.accrual_class, a.amount_rub, a.commission_rub,
    a.seller_base_price_rub, a.is_economic_block, a.is_unresolved, l.quantity,
    CASE
      WHEN a.posting_number IS NULL THEN a.attribution_scope
      WHEN ps.posting_number IS NULL THEN 'POSTING_NOT_LOADED'
      WHEN l.is_delivered THEN 'POSTING_DELIVERED'
      WHEN ps.any_delivered THEN 'POSTING_SKU_UNMATCHED'
      ELSE 'POSTING_NOT_DELIVERED'
    END AS cost_scope
  FROM `__tenant__.ozon_mart.NORM_OZON_ACCRUAL` a
  LEFT JOIN lines l ON l.posting_number = a.posting_number AND l.sku = a.sku
  LEFT JOIN postings ps ON ps.posting_number = a.posting_number
),
expanded AS (
  SELECT s.event_date, 'SALE_REVENUE' AS line_class, s.cost_scope, s.sku, s.posting_number,
    s.seller_base_price_rub * s.quantity AS amount_rub, s.is_unresolved
  FROM scoped s
  WHERE s.is_economic_block
  UNION ALL
  SELECT s.event_date, 'SALE_COMMISSION' AS line_class, s.cost_scope, s.sku, s.posting_number,
    s.commission_rub AS amount_rub, s.is_unresolved
  FROM scoped s
  WHERE s.is_economic_block
  UNION ALL
  SELECT s.event_date, s.accrual_class AS line_class, s.cost_scope, s.sku, s.posting_number,
    s.amount_rub, s.is_unresolved
  FROM scoped s
)
SELECT e.event_date, e.line_class, e.cost_scope, e.sku,
  IF(COUNTIF(e.amount_rub IS NULL) = 0, SUM(e.amount_rub), NULL) AS amount_rub,
  SUM(e.amount_rub) AS amount_known_rub,
  COUNT(*) AS accruals,
  COUNT(DISTINCT e.posting_number) AS posting_lines,
  COUNTIF(e.amount_rub IS NULL) AS unknown_amounts,
  COUNTIF(e.is_unresolved) AS unresolved_accruals
FROM expanded e
GROUP BY e.event_date, e.line_class, e.cost_scope, e.sku;
