-- Экономика доставленных заказов: сутки заказа (Москва) × SKU. Все суммы — со знаком Ozon:
-- выручка > 0, комиссия и расходы < 0, результат = их сумма.
-- Отправление рассчитано (settled), если у него ровно один полный экономический блок (цена
-- продавца и комиссия). Пока хоть одно отправление строки не рассчитано, есть нераспознанное
-- начисление или выручка неизвестна — результат NULL (не ноль и не оценка). Комиссия и
-- расходы строки с нерассчитанным отправлением тоже NULL: отсутствие начисления до расчёта —
-- не ноль.
-- Возвраты покупателей платформа пока не моделирует (сторно возврата не загружается): доставленное
-- отправление с расходами обратной логистики — признак возврата, и результат строки NULL.
-- Себестоимость — только из справочника продавца на дату заказа; ровно один действующий
-- интервал, иначе (нет или пересекаются) — нет себестоимости. Никаких умолчаний.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY`
OPTIONS(description = 'Доставленные заказы: сутки заказа (Москва) × SKU; выручка продавца, комиссия и расходы Ozon (со знаком Ozon), себестоимость продавца, результат только при полных данных.')
AS
WITH lines AS (
  SELECT p.order_date_msk, p.sku, p.posting_number, p.quantity, p.price_rub,
    s.seller_price_unit_rub, s.commission_rub,
    IFNULL(s.economic_blocks, 0) = 1 AND IFNULL(s.incomplete_economic_blocks, 0) = 0 AS is_settled,
    IFNULL(s.logistics_rub, 0) + IFNULL(s.last_mile_rub, 0) AS logistics_rub,
    IFNULL(s.acquiring_rub, 0) AS acquiring_rub,
    IFNULL(s.return_logistics_rub, 0) AS return_logistics_rub,
    IFNULL(s.promotion_rub, 0) AS promotion_rub,
    IFNULL(s.other_costs_rub, 0) AS other_costs_rub,
    IFNULL(s.unclassified_rub, 0) AS unclassified_rub,
    IFNULL(s.unresolved_accruals, 0) AS unresolved_accruals,
    IFNULL(s.return_logistics_rub, 0) != 0 AS has_return_costs
  FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_LINE` p
  LEFT JOIN `__tenant__.ozon_mart.NORM_OZON_POSTING_SETTLEMENT` s
    ON s.posting_number = p.posting_number AND s.sku = p.sku
  WHERE p.is_delivered
),
line_cogs AS (
  SELECT l.posting_number, l.sku, ANY_VALUE(d.internal_sku) AS internal_sku,
    COUNT(c.internal_sku) AS cogs_intervals, MAX(c.product_cogs_rub) AS unit_cogs_rub
  FROM lines l
  LEFT JOIN `__tenant__.ozon_mart.DIM_OZON_PRODUCT` d ON d.sku = l.sku
  LEFT JOIN `__tenant__.ozon_mart.ECON_TENANT_COGS` c
    ON c.internal_sku = d.internal_sku AND l.order_date_msk BETWEEN c.effective_from AND c.effective_to
  GROUP BY l.posting_number, l.sku
),
costed AS (
  SELECT l.order_date_msk, l.sku, l.posting_number, l.quantity, l.is_settled, l.commission_rub,
    l.logistics_rub, l.acquiring_rub, l.return_logistics_rub, l.promotion_rub, l.other_costs_rub,
    l.unclassified_rub, l.unresolved_accruals, l.has_return_costs,
    IF(l.is_settled, l.seller_price_unit_rub, l.price_rub) * l.quantity AS seller_revenue_rub,
    lc.internal_sku, lc.cogs_intervals = 1 AS has_cogs,
    IF(lc.cogs_intervals = 1, lc.unit_cogs_rub * l.quantity, NULL) AS product_cogs_rub
  FROM lines l
  JOIN line_cogs lc ON lc.posting_number = l.posting_number AND lc.sku = l.sku
),
agg AS (
  SELECT x.order_date_msk, x.sku, ANY_VALUE(x.internal_sku) AS internal_sku,
    COUNT(DISTINCT x.posting_number) AS delivered_postings,
    SUM(x.quantity) AS delivered_units,
    IF(COUNTIF(x.seller_revenue_rub IS NULL) = 0, SUM(x.seller_revenue_rub), NULL) AS seller_revenue_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.commission_rub), NULL) AS commission_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.logistics_rub), NULL) AS logistics_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.acquiring_rub), NULL) AS acquiring_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.return_logistics_rub), NULL) AS return_logistics_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.promotion_rub), NULL) AS promotion_rub,
    IF(COUNTIF(NOT x.is_settled) = 0, SUM(x.other_costs_rub), NULL) AS other_costs_rub,
    SUM(x.unclassified_rub) AS unclassified_rub,
    SUM(x.unresolved_accruals) AS unresolved_accruals,
    COUNTIF(NOT x.is_settled) AS unsettled_postings,
    COUNTIF(x.has_return_costs) AS postings_with_return_costs,
    COUNTIF(x.is_settled) AS settled_postings,
    COUNTIF(NOT x.has_cogs) AS lines_without_cogs,
    COUNTIF(x.has_cogs) AS lines_with_cogs,
    SUM(x.product_cogs_rub) AS product_cogs_sum_rub
  FROM costed x
  GROUP BY x.order_date_msk, x.sku
),
judged AS (
  SELECT g.*,
    g.unsettled_postings = 0 AND g.unresolved_accruals = 0 AND g.postings_with_return_costs = 0
      AND g.seller_revenue_rub IS NOT NULL AS result_ok,
    g.seller_revenue_rub + g.commission_rub + g.logistics_rub + g.acquiring_rub
      + g.return_logistics_rub + g.promotion_rub + g.other_costs_rub AS result_sum_rub
  FROM agg g
)
SELECT j.order_date_msk, j.sku, j.internal_sku, j.delivered_postings, j.delivered_units,
  j.seller_revenue_rub, j.commission_rub, j.logistics_rub, j.acquiring_rub, j.return_logistics_rub,
  j.promotion_rub, j.other_costs_rub, j.unclassified_rub, j.unresolved_accruals, j.unsettled_postings,
  j.postings_with_return_costs,
  IF(j.unsettled_postings = 0, 'SETTLED', IF(j.settled_postings = 0, 'NOT_SETTLED', 'PARTIALLY_SETTLED')) AS revenue_basis,
  IF(j.unresolved_accruals = 0, 'CLASSIFIED', 'UNRESOLVED') AS taxonomy_status,
  IF(j.result_ok, j.result_sum_rub, NULL) AS contribution_pre_cogs_rub,
  IF(j.lines_without_cogs = 0, j.product_cogs_sum_rub, NULL) AS product_cogs_rub,
  IF(j.lines_without_cogs = 0, 'COMPLETE', IF(j.lines_with_cogs = 0, 'NOT_AVAILABLE', 'PARTIAL')) AS cogs_coverage,
  IF(j.result_ok AND j.lines_without_cogs = 0, j.result_sum_rub - j.product_cogs_sum_rub, NULL) AS contribution_after_cogs_rub
FROM judged j;
