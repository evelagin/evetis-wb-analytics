WITH
universe AS (
  SELECT DISTINCT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
),
sales AS (
  SELECT
    NULLIF(TRIM(srid), '')     AS srid,
    COUNTIF(NOT is_return)     AS sale_events,
    COUNTIF(is_return)         AS return_events,
    MIN(IF(NOT is_return, _sale_date, NULL)) AS first_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
  WHERE NULLIF(TRIM(srid), '') IS NOT NULL
  GROUP BY 1
),
orders AS (
  SELECT
    o.order_srid,
    o.order_date,
    o.is_cancel,
    IFNULL(s.sale_events, 0)   AS sale_events,
    IFNULL(s.return_events, 0) AS return_events,
    s.first_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` o
  LEFT JOIN sales s ON s.srid = o.order_srid
  WHERE o.nm_id IN (SELECT nm_id FROM universe)
),
outcome AS (
  SELECT
    *,
    CASE
      WHEN sale_events > 0 AND is_cancel THEN 'CONFLICT'
      WHEN sale_events > 0               THEN 'BUYOUT'
      WHEN is_cancel                     THEN 'CANCELLED'
      ELSE                                    'UNRESOLVED'
    END AS order_outcome
  FROM orders
),
cov AS (
  SELECT day, orders_covered, is_current_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
)
SELECT
  o.order_date                                               AS cohort_date,
  IFNULL(c.orders_covered, FALSE)                            AS orders_covered,
  COUNT(*)                                                   AS cohort_orders,
  COUNTIF(o.order_outcome = 'BUYOUT')                        AS buyout_orders,
  COUNTIF(o.order_outcome = 'CANCELLED')                     AS cancelled_orders,
  COUNTIF(o.order_outcome = 'UNRESOLVED')                    AS unresolved_orders,
  COUNTIF(o.order_outcome = 'CONFLICT')                      AS conflict_orders,
  -- Знаменатель процента выкупа: заказы с известным исходом.
  COUNTIF(o.order_outcome IN ('BUYOUT', 'CANCELLED'))        AS resolved_orders,
  COUNTIF(o.order_outcome = 'BUYOUT' AND o.return_events > 0) AS returned_after_buyout_orders,
  -- Зрелость без порога: когорта окончательна, когда у неё не осталось заказов
  -- без исхода и нет конфликтов. Иначе — предварительна (процент по известным исходам).
  (COUNTIF(o.order_outcome IN ('UNRESOLVED', 'CONFLICT')) = 0
   AND NOT IFNULL(c.is_current_day, FALSE))                  AS cohort_is_final,
  IF(COUNTIF(o.order_outcome IN ('UNRESOLVED', 'CONFLICT')) = 0
     AND NOT IFNULL(c.is_current_day, FALSE), 0, 1)          AS cohort_provisional_day,
  DATE_DIFF(CURRENT_DATE('Europe/Moscow'), o.order_date, DAY) AS cohort_age_days,
  MAX(DATE_DIFF(o.first_sale_date, o.order_date, DAY))       AS max_days_to_buyout,
  'BUYOUT_COHORT'                                            AS economics_basis,
  'Процент выкупа = SUM(buyout_orders) / SUM(resolved_orders) по когортам заказа. Неразрешённые заказы в знаменатель не входят и показываются unresolved_orders; период окончателен, если все его когорты cohort_is_final. Возвраты после выкупа — returned_after_buyout_orders, выкуп не отменяют (gross).'
                                                             AS economics_note,
  CURRENT_TIMESTAMP()                                        AS generated_at
FROM outcome o
LEFT JOIN cov c ON c.day = o.order_date
GROUP BY o.order_date, c.orders_covered, c.is_current_day