-- Полнота начислений для суток заказа d. Начисления по заказу приходят позже заказа, поэтому
-- важен хвост после d и срок созревания:
--   COMPLETE — каждые сутки [d, последние оценённые] оценены COMPLETE или NOT_APPLICABLE без
--              пропусков, и с d прошло не меньше срока созревания;
--   PARTIAL  — в хвосте есть пропуск или неполные сутки (TAIL_INCOMPLETE) либо срок ещё не
--              прошёл (NOT_MATURE);
--   UNKNOWN  — срок созревания не задан продавцом (MATURITY_NOT_CONFIGURED) или задан
--              неоднозначно. Умолчания нет: срок — параметр арендатора
--              FINANCE_SETTLEMENT_MATURITY_DAYS в ref.REF_TENANT_ECONOMICS (scope STORE).
-- Сутки без оценки здесь отсутствуют (у потребителя — UNKNOWN).
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_FINANCE_WINDOW_STATUS`
OPTIONS(description = 'Полнота начислений Ozon для суток заказа: хвост до последних оценённых суток и срок созревания продавца. COMPLETE / PARTIAL / UNKNOWN.')
AS
WITH cov AS (
  SELECT c.coverage_date, c.status
  FROM `__tenant__.tenant_ops.V_COVERAGE_DAILY` c
  WHERE c.entity = 'finance_accrual'
),
tail AS (
  SELECT v.coverage_date AS from_date,
    COUNT(*) OVER (ORDER BY v.coverage_date ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS evaluated_days,
    SUM(IF(v.status IN ('COMPLETE', 'NOT_APPLICABLE'), 0, 1))
      OVER (ORDER BY v.coverage_date ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS incomplete_days,
    MAX(v.coverage_date) OVER () AS last_evaluated_date
  FROM cov v
),
maturity_rows AS (
  SELECT e.value_num
  FROM `__tenant__.ref.REF_TENANT_ECONOMICS` e
  WHERE e.parameter = 'FINANCE_SETTLEMENT_MATURITY_DAYS' AND e.scope = 'STORE' AND e.value_num IS NOT NULL
    AND e.effective_from <= CURRENT_DATE('Europe/Moscow')
    AND (e.effective_to IS NULL OR e.effective_to >= CURRENT_DATE('Europe/Moscow'))
  QUALIFY RANK() OVER (ORDER BY e.effective_from DESC, e.loaded_at DESC) = 1
),
maturity AS (
  SELECT IF(COUNT(DISTINCT m.value_num) = 1, MAX(m.value_num), NULL) AS maturity_days
  FROM maturity_rows m
),
judged AS (
  SELECT t.from_date, t.last_evaluated_date, t.incomplete_days,
    DATE_DIFF(t.last_evaluated_date, t.from_date, DAY) + 1 - t.evaluated_days AS missing_days,
    DATE_DIFF(t.last_evaluated_date, t.from_date, DAY) AS tail_days,
    mt.maturity_days
  FROM tail t
  CROSS JOIN maturity mt
)
SELECT j.from_date, j.last_evaluated_date, j.incomplete_days, j.missing_days, j.tail_days, j.maturity_days,
  CASE
    WHEN j.incomplete_days > 0 OR j.missing_days > 0 THEN 'PARTIAL'
    WHEN j.maturity_days IS NULL THEN 'UNKNOWN'
    WHEN j.tail_days < j.maturity_days THEN 'PARTIAL'
    ELSE 'COMPLETE'
  END AS status,
  CASE
    WHEN j.incomplete_days > 0 OR j.missing_days > 0 THEN 'TAIL_INCOMPLETE'
    WHEN j.maturity_days IS NULL THEN 'MATURITY_NOT_CONFIGURED'
    WHEN j.tail_days < j.maturity_days THEN 'NOT_MATURE'
  END AS reason
FROM judged j;
