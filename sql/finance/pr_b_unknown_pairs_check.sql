-- ============================================================================
-- PR-B · Детектор неизвестных денежных пар (op_key × amount_field)
--
-- Назначение: гейт перед снятием аварийных заглушек `__NULL__` и регулярный
--   контроль после. Отвечает на один вопрос: есть ли деньги, которые
--   REF_COST_MAP не умеет классифицировать.
--
-- РЕЖИМ: read-only. Ни одной записи.
-- КРИТЕРИЙ: 0 строк. Любая строка = новая семантика WB, которую нужно
--   классифицировать ЯВНО в seed (sql/mart/pr_mart2a_finance_longform.sql),
--   а НЕ заглушкой `__NULL__`.
--
-- Порядок использования:
--   1) до переключения FACT_FINANCE — прогнать, увидеть будущие пары;
--   2) после переключения и правки seed — прогнать снова, должно быть 0;
--   3) дальше — вместе с sql/finance/pr_a_integrity_checks.sql.
--
-- Прогон 13.08.2026 (семантический слой, seed без `__NULL__`):
--   найдена ровно одна пара — «Коррекция хранения» × storage_fee,
--   8 строк, −288,73 ₽, новый контур, 18.07–05.08.2026.
--   Классифицирована в seed как ADJUSTMENT / storage / +1.
--
-- Спека: docs/FINANCE_PR_B_NORMALIZATION_2026-08-13.md
-- ============================================================================

WITH long AS (
  SELECT
    COALESCE(f.operation_type_normalized, '__NULL__') AS op_key,
    u.amount_field,
    u.amt,
    f.finance_date,
    f.source_layer
  FROM `wb_mart.FACT_FINANCE` f,
  UNNEST([
    STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS amt),
    ('logistics_amount',   f.logistics_amount),
    ('storage_fee',        f.storage_fee),
    ('deduction',          f.deduction),
    ('penalty',            f.penalty),
    ('acceptance',         f.acceptance),
    ('acquiring_fee',      f.acquiring_fee),
    ('additional_payment', f.additional_payment),
    ('other_amount',       f.other_amount)
  ]) u
  -- тот же фильтр, что в V_WB_FINANCE_AMOUNTS_LONG: нули и NULL не деньги
  WHERE u.amt IS NOT NULL AND u.amt <> 0
)

SELECT
  l.op_key,
  l.amount_field,
  COUNT(*)                              AS rows_n,
  ROUND(SUM(l.amt), 2)                  AS sum_rub,
  STRING_AGG(DISTINCT l.source_layer)   AS layers,
  MIN(l.finance_date)                   AS first_seen,
  MAX(l.finance_date)                   AS last_seen,
  '🔴 НЕ КЛАССИФИЦИРОВАНА — внести в seed pr_mart2a, не в заглушку' AS action
FROM long l
LEFT JOIN `wb_mart.REF_COST_MAP` r USING (op_key, amount_field)
WHERE r.op_key IS NULL
GROUP BY 1, 2
ORDER BY rows_n DESC;


-- ============================================================================
-- WATCH-LIST: операции нового контура с нулевыми суммами
-- ----------------------------------------------------------------------------
-- Пары не существует, пока сумма нулевая, поэтому запрос выше их не видит.
-- На 13.08.2026 сюда попадает «Сумма баллов, удержанных в рамках акции
-- "Баллы за отзывы"» — 42 строки, все девять денежных полей нулевые/NULL.
-- Как только WB пришлёт по ней деньги, она всплывёт в основном запросе,
-- и fail-closed ASSERT остановит сборку. Это желаемое поведение.
-- Раскомментировать для ревизии.
-- ----------------------------------------------------------------------------
-- SELECT operation_type_normalized AS op, COUNT(*) rows_n,
--        MIN(finance_date) d1, MAX(finance_date) d2
-- FROM `wb_mart.FACT_FINANCE`
-- WHERE source_layer <> 'LEGACY'
--   AND COALESCE(commission_amount,0)  = 0 AND COALESCE(logistics_amount,0)   = 0
--   AND COALESCE(storage_fee,0)        = 0 AND COALESCE(deduction,0)          = 0
--   AND COALESCE(penalty,0)            = 0 AND COALESCE(acceptance,0)         = 0
--   AND COALESCE(acquiring_fee,0)      = 0 AND COALESCE(additional_payment,0) = 0
--   AND COALESCE(other_amount,0)       = 0
-- GROUP BY op ORDER BY rows_n DESC;
