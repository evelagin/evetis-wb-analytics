-- Свежесть и полнота каждой сущности загрузки за последние 30 суток (Москва). Перечислены ВСЕ
-- сущности пакета Ozon, в том числе ни разу не оценённые: у них last_status = UNKNOWN и все 30
-- суток — unknown_days_30. Сутки без оценки — UNKNOWN, а не ноль и не COMPLETE. is_stale —
-- последние полные сутки старше ожидаемого отставания сущности (соглашение витрины, не SLA).
-- as_of_status — статус СЕГОДНЯШНИХ суток: ещё не загруженные сегодня (например, при остановленном
-- контроллере) — UNKNOWN, а не ноль и не перенос вчерашнего COMPLETE.
CREATE OR REPLACE VIEW `__tenant__.analytics_share.data_freshness`
OPTIONS(description = 'Свежесть данных: сущность загрузки × сегодня. Последние полные и последние оценённые сутки, отставание, дни по статусам за 30 суток, UNKNOWN для неоценённых.')
AS
WITH entities AS (
  SELECT 'fbo_postings' AS entity, 'Заказы FBO (отправления)' AS entity_title, 'Продажи и заказы; Остатки' AS dashboard_section, 1 AS expected_lag_days
  UNION ALL SELECT 'stocks', 'Остатки FBO', 'Остатки', 1
  UNION ALL SELECT 'clusters', 'Кластеры и склады', 'Остатки', 7
  UNION ALL SELECT 'supplies', 'Поставки FBO', 'Остатки', 1
  UNION ALL SELECT 'ads_expense_daily', 'Реклама: расходы кампаний', 'Реклама', 2
  UNION ALL SELECT 'ads_sku_daily', 'Реклама: расходы по товарам', 'Реклама', 2
  UNION ALL SELECT 'ads_campaigns', 'Реклама: список кампаний', 'Реклама', 7
  UNION ALL SELECT 'finance_accrual', 'Начисления Ozon', 'Экономика SKU', 2
  UNION ALL SELECT 'prices', 'Цены продавца', 'Экономика SKU', 1
  UNION ALL SELECT 'catalog', 'Каталог товаров', 'Все разделы', 1
  UNION ALL SELECT 'seller_info', 'Сведения о продавце', 'Покрытие данных', 7
),
today AS (SELECT CURRENT_DATE('Europe/Moscow') AS d),
cov AS (
  SELECT c.entity, c.coverage_date, c.status, c.reason
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
),
agg AS (
  SELECT c.entity,
    MIN(IF(c.status = 'COMPLETE', c.coverage_date, NULL)) AS first_complete_date,
    MAX(IF(c.status = 'COMPLETE', c.coverage_date, NULL)) AS last_complete_date,
    MAX(c.coverage_date) AS last_evaluated_date,
    COUNTIF(c.coverage_date > DATE_SUB(t.d, INTERVAL 30 DAY) AND c.coverage_date <= t.d) AS evaluated_days_30,
    COUNTIF(c.coverage_date > DATE_SUB(t.d, INTERVAL 30 DAY) AND c.coverage_date <= t.d AND c.status = 'COMPLETE') AS complete_days_30,
    COUNTIF(c.coverage_date > DATE_SUB(t.d, INTERVAL 30 DAY) AND c.coverage_date <= t.d AND c.status = 'PARTIAL') AS partial_days_30,
    COUNTIF(c.coverage_date > DATE_SUB(t.d, INTERVAL 30 DAY) AND c.coverage_date <= t.d
            AND c.status IN ('FAILED', 'NOT_AVAILABLE', 'UNKNOWN')) AS problem_days_30,
    COUNTIF(c.coverage_date > DATE_SUB(t.d, INTERVAL 30 DAY) AND c.coverage_date <= t.d AND c.status = 'NOT_APPLICABLE') AS not_applicable_days_30
  FROM cov c
  CROSS JOIN today t
  GROUP BY c.entity
),
today_row AS (
  SELECT c.entity, c.status
  FROM cov c
  CROSS JOIN today t
  WHERE c.coverage_date = t.d
),
last_row AS (
  SELECT c.entity, c.status, c.reason
  FROM cov c
  JOIN agg a ON a.entity = c.entity AND a.last_evaluated_date = c.coverage_date
)
SELECT e.entity, e.entity_title, e.dashboard_section, t.d AS as_of_date, e.expected_lag_days,
  a.first_complete_date, a.last_complete_date, a.last_evaluated_date,
  DATE_DIFF(t.d, a.last_complete_date, DAY) AS days_since_last_complete,
  a.last_complete_date IS NULL OR DATE_DIFF(t.d, a.last_complete_date, DAY) > e.expected_lag_days AS is_stale,
  IF(a.entity IS NULL, 0, a.complete_days_30) AS complete_days_30,
  IF(a.entity IS NULL, 0, a.partial_days_30) AS partial_days_30,
  IF(a.entity IS NULL, 0, a.problem_days_30) AS problem_days_30,
  IF(a.entity IS NULL, 0, a.not_applicable_days_30) AS not_applicable_days_30,
  IF(a.entity IS NULL, 30, 30 - a.evaluated_days_30) AS unknown_days_30,
  lr.reason AS last_reason,
  COALESCE(lr.status, 'UNKNOWN') AS last_status,
  COALESCE(tr.status, 'UNKNOWN') AS as_of_status
FROM entities e
CROSS JOIN today t
LEFT JOIN agg a ON a.entity = e.entity
LEFT JOIN last_row lr ON lr.entity = e.entity
LEFT JOIN today_row tr ON tr.entity = e.entity;
