-- ============================================================================
-- PR-Mart2a — EVETIS WB Analytics MART. Finance long-form + REF_COST_MAP.
-- Дата: 2026-07-30.  Контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md (§3 REF, §KPI).
-- PR-нота: docs/MART_PR2A_FINANCE_LONGFORM_2026-07-30.md.
-- Аудит: APPROVE (ChatGPT, 30.07). Реконструкция из контракта сверена read-only на проде
--   30.07 — ВСЕ контрольные цифры совпали до копейки (см. validation-файл §сводка).
--
-- Назначение: развернуть FACT_FINANCE 9 денежных полей в длинную форму (1 строка = 1
--   ненулевое денежное значение), приклеить нормализованное направление/категорию из
--   REF_COST_MAP и дать нормализованное cost_amount_positive — БЕЗ потери консервации
--   (source_signed_amount авторитетен; cost_amount_positive — представление).
--
-- Что создаётся (3 объекта в wb_mart, все read-model, обратимо DROP):
--   1) REF_COST_MAP                          — seed-таблица маппинга (operation × field → direction/category)
--   2) V_WB_FINANCE_AMOUNTS_LONG             — чистый source-faithful UNPIVOT 9 полей
--   3) V_WB_FINANCE_AMOUNTS_LONG_MAPPED      — LONG ⨝ REF → direction/category/cost_amount_positive
--
-- Ничего существующего (FACT_*, загрузчики) не меняется. Потребители подключаются с Mart2b+.
--
-- Ключевые контракты (durable):
--   • op_key = COALESCE(operation_type_normalized, '__NULL__')  (NULL не сматчился бы JOIN).
--   • is_sku_row = COALESCE(nm_id > 0 AND sku_match_status = 'matched', FALSE)  (NULL-статус → ACCOUNT).
--   • Деньги в FACT_FINANCE уже NUMERIC (парсинг на слое FACT) — здесь без re-parse.
--   • 9 unpivot-полей: commission_amount, logistics_amount, storage_fee, deduction, penalty,
--     acceptance, acquiring_fee, additional_payment, other_amount.
--     compensation_amount НЕ разворачивается — отдельный guard (§5.1, деньги не несёт).
--   • field_normalization_sign: commission_amount = -1; все прочие = +1.
--   • cost_amount_positive:  COST → +ABS(source);  CREDIT → -ABS(source);
--     ADJUSTMENT → source_signed × field_normalization_sign  (знак сохраняется).
--
-- Гейт (fail-closed): §5.1 compensation guard; §5.2 лемма консервации по-полю (порог 0.005);
--   §5.3 unknown money-pairs = 0; §5.4 нормализация корректна + SKU+ACCOUNT == total;
--   §5.5 REF уникален + домены direction/sign.
-- ⚠️ Объекты создаются после APPROVE (получен). Read-only проверки — validation-файл.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

-- ── 1. REF_COST_MAP — seed маппинга (operation_type_normalized × amount_field) ──
-- 22 денежные пары → 10 категорий. Точные op-строки взяты ИЗ ДАННЫХ (не из сокращений дока).
CREATE OR REPLACE TABLE `wb_mart.REF_COST_MAP`
CLUSTER BY op_key, amount_field AS
SELECT
  op_key, amount_field, economic_direction, cost_category,
  field_normalization_sign, note,
  CURRENT_TIMESTAMP() AS seeded_at
FROM UNNEST([
  -- commission_amount (field_normalization_sign = -1)
  STRUCT('Продажа' AS op_key, 'commission_amount' AS amount_field, 'COST' AS economic_direction,
         'commission' AS cost_category, -1 AS field_normalization_sign,
         'Комиссия WB по продаже' AS note),
  ('Возврат', 'commission_amount', 'COST', 'commission', -1, 'Комиссия по возврату'),
  ('Возмещение за выдачу и возврат товаров на ПВЗ', 'commission_amount', 'CREDIT',
     'reimbursement_pvz', -1, 'Возмещение ПВЗ (кредит)'),
  ('Возмещение издержек по перевозке/по складским операциям с товаром', 'commission_amount', 'CREDIT',
     'reimbursement_logistics', -1, 'Возмещение издержек перевозки/склада (кредит)'),
  ('Коррекция продаж', 'commission_amount', 'ADJUSTMENT', 'commission', -1,
     'Корректировка комиссии — знак сохраняется'),
  -- acquiring_fee (+1)
  ('Продажа', 'acquiring_fee', 'COST', 'acquiring', 1, 'Эквайринг по продаже (per-SKU COST)'),
  ('__NULL__', 'acquiring_fee', 'COST', 'acquiring', 1, 'Эквайринг, операция не размечена'),
  ('Возврат', 'acquiring_fee', 'COST', 'acquiring', 1, 'Эквайринг по возврату'),
  ('Коррекция продаж', 'acquiring_fee', 'ADJUSTMENT', 'acquiring', 1, 'Корректировка эквайринга (продажи)'),
  ('Корректировка эквайринга', 'acquiring_fee', 'ADJUSTMENT', 'acquiring', 1, 'Корректировка эквайринга'),
  -- logistics_amount (+1)
  ('Логистика', 'logistics_amount', 'COST', 'logistics', 1, 'Логистика WB'),
  ('__NULL__', 'logistics_amount', 'COST', 'logistics', 1, 'Логистика, операция не размечена'),
  ('Коррекция логистики', 'logistics_amount', 'ADJUSTMENT', 'logistics', 1, 'Корректировка логистики'),
  -- storage_fee (+1)
  ('Хранение', 'storage_fee', 'COST', 'storage', 1, 'Хранение WB'),
  ('__NULL__', 'storage_fee', 'COST', 'storage', 1, 'Хранение, операция не размечена'),
  -- deduction (+1)
  ('Удержание', 'deduction', 'COST', 'deduction', 1, 'Прочие удержания'),
  ('__NULL__', 'deduction', 'COST', 'deduction', 1, 'Удержание, операция не размечена'),
  ('Удержание', 'additional_payment', 'COST', 'deduction', 1, 'Удержание через доп. платёж'),
  -- penalty (+1)
  ('Штраф', 'penalty', 'COST', 'penalty', 1, 'Штрафы WB'),
  -- acceptance (+1)
  ('Платная приемка', 'acceptance', 'COST', 'acceptance', 1, 'Платная приёмка'),
  ('Пересчет платной приемки', 'acceptance', 'COST', 'acceptance', 1, 'Пересчёт платной приёмки'),
  -- loyalty (+1)
  ('Стоимость участия в программе лояльности', 'additional_payment', 'COST', 'loyalty', 1,
     'Программа лояльности WB')
]);

-- ── 2. V_WB_FINANCE_AMOUNTS_LONG — source-faithful UNPIVOT 9 денежных полей ──────
-- 1 строка = 1 ненулевое денежное значение. Нули/NULL отброшены. compensation НЕ входит.
CREATE OR REPLACE VIEW `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` AS
SELECT
  f.finance_row_key,
  f.finance_date,
  f.nm_id,
  f.sku_match_status,
  f.operation_type_normalized,
  COALESCE(f.operation_type_normalized, '__NULL__')                        AS op_key,
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)          AS is_sku_row,
  u.amount_field,
  u.source_signed_amount
FROM `wb_mart.FACT_FINANCE` f,
UNNEST([
  STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS source_signed_amount),
  ('logistics_amount',  f.logistics_amount),
  ('storage_fee',       f.storage_fee),
  ('deduction',         f.deduction),
  ('penalty',           f.penalty),
  ('acceptance',        f.acceptance),
  ('acquiring_fee',     f.acquiring_fee),
  ('additional_payment', f.additional_payment),
  ('other_amount',      f.other_amount)
]) u
WHERE u.source_signed_amount IS NOT NULL AND u.source_signed_amount <> 0;

-- ── 3. V_WB_FINANCE_AMOUNTS_LONG_MAPPED — LONG ⨝ REF → direction/category/cost_positive ──
-- UNKNOWN (пара нет в REF) → cost_category/economic_direction NULL, cost_amount_positive NULL
--   (в суммы НЕ попадает; §5.3 гарантирует таких строк 0 для денежных пар).
CREATE OR REPLACE VIEW `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` AS
SELECT
  l.finance_row_key, l.finance_date, l.nm_id, l.sku_match_status,
  l.operation_type_normalized, l.op_key, l.is_sku_row,
  l.amount_field, l.source_signed_amount,
  r.economic_direction, r.cost_category, r.field_normalization_sign,
  CASE r.economic_direction
    WHEN 'COST'       THEN ABS(l.source_signed_amount)
    WHEN 'CREDIT'     THEN -ABS(l.source_signed_amount)
    WHEN 'ADJUSTMENT' THEN l.source_signed_amount * r.field_normalization_sign
    ELSE NULL
  END AS cost_amount_positive
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` l
LEFT JOIN `wb_mart.REF_COST_MAP` r
  USING (op_key, amount_field);

-- ============================================================================
-- ГЕЙТ (fail-closed). Любой ASSERT падает → артефакт считать НЕ принятым.
-- ============================================================================

-- §5.1 compensation guard: compensation_amount не несёт денег (NULL-safe).
ASSERT (SELECT COUNTIF(compensation_amount IS NOT NULL AND compensation_amount <> 0)
        FROM `wb_mart.FACT_FINANCE`) = 0
  AS 'PR-Mart2a §5.1: compensation_amount несёт ненулевые значения — расширить модель';

-- §5.2 лемма консервации по-полю: Σ source_signed в LONG == Σ поля в FACT (порог 0.005).
--   (LONG отбрасывает только нули/NULL → суммы обязаны совпасть.)
ASSERT (
  SELECT COUNTIF(ABS(long_sum - fact_sum) > 0.005)
  FROM (
    SELECT amount_field, SUM(source_signed_amount) AS long_sum
    FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` GROUP BY amount_field
  ) L
  JOIN (
    SELECT 'commission_amount' AS amount_field, SUM(commission_amount) AS fact_sum FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'logistics_amount',   SUM(logistics_amount)   FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'storage_fee',        SUM(storage_fee)        FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'deduction',          SUM(deduction)          FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'penalty',            SUM(penalty)            FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acceptance',         SUM(acceptance)         FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acquiring_fee',      SUM(acquiring_fee)      FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'additional_payment', SUM(additional_payment) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'other_amount',       SUM(other_amount)       FROM `wb_mart.FACT_FINANCE`
  ) F USING (amount_field)
) = 0
  AS 'PR-Mart2a §5.2: лемма консервации по-полю нарушена (LONG != FACT)';

-- §5.3 unknown money-pairs = 0: каждая денежная (op_key × field) обязана быть в REF.
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT op_key, amount_field
    FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
    WHERE cost_category IS NULL
    GROUP BY op_key, amount_field
  )
) = 0
  AS 'PR-Mart2a §5.3: есть денежные пары (op_key×field) вне REF_COST_MAP — сначала расширить REF';

-- §5.4 нормализация корректна: COST → >=0; CREDIT → <=0; и SKU + ACCOUNT == total (расщепление полное).
ASSERT (SELECT COUNTIF(economic_direction = 'COST'   AND cost_amount_positive < 0)
             + COUNTIF(economic_direction = 'CREDIT' AND cost_amount_positive > 0)
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`) = 0
  AS 'PR-Mart2a §5.4a: знак cost_amount_positive не соответствует направлению';

ASSERT (
  SELECT ABS(
      SUM(cost_amount_positive)
    - SUM(IF(is_sku_row, cost_amount_positive, 0))
    - SUM(IF(NOT is_sku_row, cost_amount_positive, 0))
  ) < 0.005
  FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
  WHERE cost_amount_positive IS NOT NULL
) AS 'PR-Mart2a §5.4b: SKU + ACCOUNT != total (расщепление неполное)';

-- §5.5 REF уникален + домены direction/sign.
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', op_key, amount_field))
        FROM `wb_mart.REF_COST_MAP`)
  AS 'PR-Mart2a §5.5a: REF_COST_MAP (op_key×field) не уникален';
ASSERT (SELECT COUNTIF(economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT'))
             + COUNTIF(field_normalization_sign NOT IN (-1, 1))
        FROM `wb_mart.REF_COST_MAP`) = 0
  AS 'PR-Mart2a §5.5b: домен economic_direction / field_normalization_sign нарушен';
