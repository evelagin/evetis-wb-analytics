-- ============================================================================
-- ОТКАТ fin contract v2 (2026-09-16) — возмещения и long-mapped слой.
-- Возвращает состояние ДО миграции sql/dash/fin_contract_v2_2026-09-16.sql.
--
-- 🔴 ПОРЯДОК ОТКАТА:
--   1. Metabase: вернуть карточки 46, 47, 51, 79, 83, 85 из снимка
--      sql/rollback/fin_contract_v2_2026-09-16/metabase_cards_BEFORE/
--      (иначе карточки обратятся к колонкам, которых не станет).
--   2. R1_V_DASH_FINANCE_CORRECTED_DAILY_BEFORE.sql
--   3. этот файл (REF_COST_MAP → CREDIT, затем view без ветки MEMO).
--   Выплата WB (V_DASH_SETTLEMENT_DAILY) от отката не меняется.
-- ============================================================================

UPDATE `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`
SET economic_direction = 'CREDIT',
    note = REGEXP_REPLACE(note, r' \| FIN CONTRACT V2 \(2026-09-16\).*$', '')
WHERE amount_field = 'commission_amount'
  AND economic_direction = 'MEMO'
  AND op_key IN ('Возмещение за выдачу и возврат товаров на ПВЗ',
                 'Возмещение издержек по перевозке/по складским операциям с товаром',
                 'Возмещение издержек по перемещению и операционной обработке товара');

ASSERT (SELECT COUNTIF(economic_direction = 'MEMO') FROM `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP`) = 0
  AS 'rollback fin v2: в REF_COST_MAP осталась строка MEMO';

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` AS
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
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG` l
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.REF_COST_MAP` r USING (op_key, amount_field);
