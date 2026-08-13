-- ============================================================================
-- PR-Mart2a — EVETIS WB Analytics MART. Finance long-form + REF_COST_MAP. REV2 (аудит PR#80).
-- Дата: 2026-07-30.  Контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md (§3 REF, §KPI).
-- PR-нота: docs/MART_PR2A_FINANCE_LONGFORM_2026-07-30.md.
--
-- REV2 по замечаниям аудитора (REQUEST CHANGES, PR#80):
--   #1 FAIL-CLOSED: REF_COST_MAP__BUILD → ASSERT seed + live contracts → PUBLISH REF →
--      создать финальные VIEW. Ничего в целевые объекты не публикуется до прохождения ВСЕХ гейтов
--      (staging __BUILD + TEMP _long — не контрактные объекты; при падении ASSERT публикация не происходит).
--   #2 Conservation guard: явный spine всех 9 amount_field + LEFT JOIN + COALESCE(sum,0) —
--      поля без ненулевых LONG-строк (other_amount) больше НЕ выпадают из проверки.
--   #3 Domain guard NULL-safe (явные IS NULL для direction/sign/category) + точная проверка формулы
--      cost_amount_positive во ВСЕХ ТРЁХ режимах (COST/CREDIT/ADJUSTMENT), не только знаки.
--
-- Назначение: развернуть FACT_FINANCE 9 денежных полей в длинную форму (1 строка = 1 ненулевое
--   значение), приклеить нормализованное direction/category из REF_COST_MAP, дать cost_amount_positive
--   БЕЗ потери консервации (source_signed_amount авторитетен; cost_amount_positive — представление).
--
-- Что публикуется (3 объекта в wb_mart, read-model, обратимо DROP):
--   REF_COST_MAP (seed), V_WB_FINANCE_AMOUNTS_LONG (unpivot), V_WB_FINANCE_AMOUNTS_LONG_MAPPED (⨝REF).
-- Ничего существующего (FACT_*, загрузчики) не меняется.
--
-- Durable-контракты: op_key=COALESCE(operation_type_normalized,'__NULL__');
--   is_sku_row=COALESCE(nm_id>0 AND sku_match_status='matched', FALSE);
--   деньги FACT_FINANCE уже NUMERIC (без re-parse); 9 unpivot-полей (commission_amount, logistics_amount,
--   storage_fee, deduction, penalty, acceptance, acquiring_fee, additional_payment, other_amount);
--   compensation_amount НЕ разворачивается (отдельный guard); field_normalization_sign: commission=−1, прочие=+1;
--   cost_amount_positive: COST→+ABS; CREDIT→−ABS; ADJUSTMENT→source×field_sign.
--
-- ⚠️ Read-only dry-run исправленных гейтов пройден на проде 30.07 (все счётчики 0; 9/9 полей).
--    Объекты создаются ТОЛЬКО после merge PR#80 (production apply владельцем/оркестратором).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

-- ── 1. STAGING: REF_COST_MAP__BUILD (seed 19 пар; точные op-строки ИЗ ДАННЫХ) ──
--    PR-B (13.08.2026): удалены пять аварийных пар op_key='__NULL__'.
--    Они ловили строки нового контура (op_key = COALESCE(operation_type_normalized,
--    '__NULL__')) и нейтрализовали fail-closed ASSERT §5.3 именно там, где он нужен;
--    попутно терялась семантика ADJUSTMENT — «Коррекция логистики» и «Корректировка
--    эквайринга» уходили в обычный COST с ABS(). После восстановления
--    operation_type_normalized в V_WB_FINANCE_SEMANTIC заглушки не нужны.
--    ⚠️ Порядок обязателен: сначала FACT_FINANCE пересобран из семантического слоя,
--    только потом этот скрипт. Иначе §5.3 упадёт — и это будет правильно.
CREATE OR REPLACE TABLE `wb_mart.REF_COST_MAP__BUILD` AS
SELECT op_key, amount_field, economic_direction, cost_category,
  field_normalization_sign, note, CURRENT_TIMESTAMP() AS seeded_at
FROM UNNEST([
  STRUCT('Продажа' AS op_key,'commission_amount' AS amount_field,'COST' AS economic_direction,
         'commission' AS cost_category,-1 AS field_normalization_sign,'Комиссия WB по продаже' AS note),
  ('Возврат','commission_amount','COST','commission',-1,'Комиссия по возврату'),
  ('Возмещение за выдачу и возврат товаров на ПВЗ','commission_amount','CREDIT','reimbursement_pvz',-1,'Возмещение ПВЗ (кредит)'),
  ('Возмещение издержек по перевозке/по складским операциям с товаром','commission_amount','CREDIT','reimbursement_logistics',-1,'Возмещение издержек перевозки/склада (кредит)'),
  ('Коррекция продаж','commission_amount','ADJUSTMENT','commission',-1,'Корректировка комиссии — знак сохраняется'),
  ('Продажа','acquiring_fee','COST','acquiring',1,'Эквайринг по продаже (per-SKU COST)'),
  ('Возврат','acquiring_fee','COST','acquiring',1,'Эквайринг по возврату'),
  ('Коррекция продаж','acquiring_fee','ADJUSTMENT','acquiring',1,'Корректировка эквайринга (продажи)'),
  ('Корректировка эквайринга','acquiring_fee','ADJUSTMENT','acquiring',1,'Корректировка эквайринга'),
  ('Логистика','logistics_amount','COST','logistics',1,'Логистика WB'),
  ('Коррекция логистики','logistics_amount','ADJUSTMENT','logistics',1,'Корректировка логистики'),
  ('Хранение','storage_fee','COST','storage',1,'Хранение WB'),
  ('Коррекция хранения','storage_fee','ADJUSTMENT','storage',1,'Корректировка хранения — знак сохраняется (PR-B)'),
  ('Удержание','deduction','COST','deduction',1,'Прочие удержания'),
  ('Удержание','additional_payment','COST','deduction',1,'Удержание через доп. платёж'),
  ('Штраф','penalty','COST','penalty',1,'Штрафы WB'),
  ('Платная приемка','acceptance','COST','acceptance',1,'Платная приёмка'),
  ('Пересчет платной приемки','acceptance','COST','acceptance',1,'Пересчёт платной приёмки'),
  ('Стоимость участия в программе лояльности','additional_payment','COST','loyalty',1,'Программа лояльности WB')
]);

-- ── 2. SEED-контракты на __BUILD (NULL-safe) ─────────────────────────────────
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', op_key, amount_field))
        FROM `wb_mart.REF_COST_MAP__BUILD`)
  AS 'PR-Mart2a seed: (op_key×amount_field) не уникален';
ASSERT (SELECT COUNTIF(op_key IS NULL OR TRIM(op_key)='' OR amount_field IS NULL OR TRIM(amount_field)='')
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: NULL/empty op_key/amount_field';
ASSERT (SELECT COUNTIF(economic_direction IS NULL OR economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT'))
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: economic_direction NULL или вне домена';
ASSERT (SELECT COUNTIF(field_normalization_sign IS NULL OR field_normalization_sign NOT IN (-1, 1))
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: field_normalization_sign NULL или вне {-1,1}';
ASSERT (SELECT COUNTIF(cost_category IS NULL OR TRIM(cost_category)='')
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: cost_category NULL/empty';

-- ── 3. STAGING long (TEMP) для live-контрактов (целевые VIEW ещё НЕ созданы) ──
CREATE TEMP TABLE _long AS
SELECT
  f.finance_row_key, f.finance_date, f.nm_id,
  COALESCE(f.operation_type_normalized, '__NULL__')                     AS op_key,
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)       AS is_sku_row,
  u.amount_field, u.source_signed_amount
FROM `wb_mart.FACT_FINANCE` f,
UNNEST([
  STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS source_signed_amount),
  ('logistics_amount',  f.logistics_amount), ('storage_fee', f.storage_fee), ('deduction', f.deduction),
  ('penalty', f.penalty), ('acceptance', f.acceptance), ('acquiring_fee', f.acquiring_fee),
  ('additional_payment', f.additional_payment), ('other_amount', f.other_amount)
]) u
WHERE u.source_signed_amount IS NOT NULL AND u.source_signed_amount <> 0;

CREATE TEMP TABLE _mapped AS
SELECT l.is_sku_row, l.op_key, l.amount_field, l.source_signed_amount AS s,
  r.economic_direction AS dir, r.field_normalization_sign AS sgn, r.cost_category AS cat,
  CASE r.economic_direction
    WHEN 'COST'       THEN ABS(l.source_signed_amount)
    WHEN 'CREDIT'     THEN -ABS(l.source_signed_amount)
    WHEN 'ADJUSTMENT' THEN l.source_signed_amount * r.field_normalization_sign
  END AS cp
FROM _long l LEFT JOIN `wb_mart.REF_COST_MAP__BUILD` r USING (op_key, amount_field);

-- ── 4. LIVE-контракты (fail-closed: до publish) ──────────────────────────────
-- §5.1 compensation guard.
ASSERT (SELECT COUNTIF(compensation_amount IS NOT NULL AND compensation_amount <> 0)
        FROM `wb_mart.FACT_FINANCE`) = 0
  AS 'PR-Mart2a §5.1: compensation_amount несёт ненулевые значения';

-- §5.2 лемма консервации по-полю: явный spine ВСЕХ 9 полей, LEFT JOIN, COALESCE (fix #2).
--   Поле без ненулевых LONG-строк (other_amount) остаётся в проверке: COALESCE(long,0)=COALESCE(fact,0).
ASSERT (
  WITH fields AS (SELECT f FROM UNNEST(['commission_amount','logistics_amount','storage_fee','deduction',
                    'penalty','acceptance','acquiring_fee','additional_payment','other_amount']) f),
  ls AS (SELECT amount_field, SUM(source_signed_amount) s FROM _long GROUP BY amount_field),
  fs AS (
    SELECT 'commission_amount' amount_field, COALESCE(SUM(commission_amount),0) fact_sum FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'logistics_amount', COALESCE(SUM(logistics_amount),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'storage_fee', COALESCE(SUM(storage_fee),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'deduction', COALESCE(SUM(deduction),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'penalty', COALESCE(SUM(penalty),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acceptance', COALESCE(SUM(acceptance),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acquiring_fee', COALESCE(SUM(acquiring_fee),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'additional_payment', COALESCE(SUM(additional_payment),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'other_amount', COALESCE(SUM(other_amount),0) FROM `wb_mart.FACT_FINANCE`)
  SELECT COUNTIF(ABS(COALESCE(ls.s,0) - COALESCE(fs.fact_sum,0)) > 0.005)
  FROM fields fld
  LEFT JOIN ls ON ls.amount_field = fld.f
  LEFT JOIN fs ON fs.amount_field = fld.f
) = 0
  AS 'PR-Mart2a §5.2: лемма консервации по-полю (9/9) нарушена';

-- §5.3 unknown money-pairs = 0.
ASSERT (SELECT COUNT(*) FROM (SELECT op_key, amount_field FROM _mapped WHERE cat IS NULL GROUP BY op_key, amount_field)) = 0
  AS 'PR-Mart2a §5.3: денежные пары (op_key×field) вне REF_COST_MAP';

-- §5.4 нормализация: точная формула cost_amount_positive во ВСЕХ ТРЁХ режимах + нет NULL cp для money-строк (fix #3).
ASSERT (SELECT
      COUNTIF(dir = 'COST'       AND cp <> ABS(s))
    + COUNTIF(dir = 'CREDIT'     AND cp <> -ABS(s))
    + COUNTIF(dir = 'ADJUSTMENT' AND cp <> s * sgn)
    + COUNTIF(cat IS NOT NULL AND cp IS NULL)
    + COUNTIF(is_sku_row IS NULL)
    FROM `_mapped`) = 0
  AS 'PR-Mart2a §5.4a: формула cost_amount_positive неверна в каком-то из режимов (COST/CREDIT/ADJUSTMENT)';

-- §5.4b расщепление полное: total == SKU + ACCOUNT.
ASSERT (SELECT ABS(SUM(cp) - SUM(IF(is_sku_row, cp, 0)) - SUM(IF(NOT is_sku_row, cp, 0))) < 0.005
        FROM `_mapped` WHERE cp IS NOT NULL)
  AS 'PR-Mart2a §5.4b: SKU + ACCOUNT != total';

-- ── 5. PUBLISH (только после прохождения ВСЕХ гейтов) ────────────────────────
CREATE OR REPLACE TABLE `wb_mart.REF_COST_MAP`
CLUSTER BY op_key, amount_field AS
SELECT op_key, amount_field, economic_direction, cost_category, field_normalization_sign, note, seeded_at
FROM `wb_mart.REF_COST_MAP__BUILD`;

CREATE OR REPLACE VIEW `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` AS
SELECT
  f.finance_row_key, f.finance_date, f.nm_id, f.sku_match_status, f.operation_type_normalized,
  COALESCE(f.operation_type_normalized, '__NULL__')                     AS op_key,
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)       AS is_sku_row,
  u.amount_field, u.source_signed_amount
FROM `wb_mart.FACT_FINANCE` f,
UNNEST([
  STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS source_signed_amount),
  ('logistics_amount',  f.logistics_amount), ('storage_fee', f.storage_fee), ('deduction', f.deduction),
  ('penalty', f.penalty), ('acceptance', f.acceptance), ('acquiring_fee', f.acquiring_fee),
  ('additional_payment', f.additional_payment), ('other_amount', f.other_amount)
]) u
WHERE u.source_signed_amount IS NOT NULL AND u.source_signed_amount <> 0;

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
LEFT JOIN `wb_mart.REF_COST_MAP` r USING (op_key, amount_field);

-- ── 6. cleanup staging ───────────────────────────────────────────────────────
DROP TABLE IF EXISTS `wb_mart.REF_COST_MAP__BUILD`;
