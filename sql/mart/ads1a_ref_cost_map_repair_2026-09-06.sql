-- ============================================================================
-- Stage ADS-1A · АВАРИЙНОЕ ВОССТАНОВЛЕНИЕ wb_mart.REF_COST_MAP
-- Дата: 06.09.2026
--
-- ЗАЧЕМ. При применении Stage ADS-1A был выполнен authoritative seed-скрипт
-- sql/mart/pr_mart2a_finance_longform.sql. Файл оказался РАСХОЖДЕНИЕМ с production:
-- Stage 1.5 (26.08.2026) правил REF_COST_MAP прямым UPDATE четырёх строк и в seed
-- НЕ вернулся (тот же класс дефекта, что Stage 1.6 нашёл в sp_build_mart_sku_daily).
-- Прогон вернул эти четыре пары в дофиксовое состояние COST/ABS.
--
-- ЧТО НЕ ПОСТРАДАЛО. MART_SKU_DAILY НЕ пересобиралась: guard fix #5 в
-- sp_build_mart_sku_daily упал в секции §pre — до lock 'mart_sku_daily' и до
-- любой записи в витрину. Она осталась в состоянии build_as_of = 2026-09-01,
-- собранном на КОРРЕКТНОЙ карте (last_modified 2026-09-02, 7 627 строк). RAW не трогался.
--
-- ⚠️ УТОЧНЕНИЕ (проверено по wb_mart.__TABLES__ после факта). Шесть таблиц FACT_*
-- БЫЛИ пересозданы 06.09 15:53–15:54 UTC — это ШАГ 1 того же Cloud Run job
-- (sp_bootstrap_facts выполняется до sp_build_mart_sku_daily), а не побочный эффект
-- испорченной карты. Контаминация исключена по построению: sp_bootstrap_facts
-- НЕ читает ни REF_COST_MAP, ни V_WB_FINANCE_AMOUNTS_LONG_MAPPED (проверено поиском
-- по телу процедуры) — REF применяется СТРОГО ниже по потоку, во вью LONG_MAPPED.
-- Тот же bootstrap отрабатывал в этот день и на плановых прогонах 07/09/12/16 МСК.
--
-- ЧТО ПОСТРАДАЛО. Вью V_WB_FINANCE_AMOUNTS_LONG_MAPPED считает
-- cost_amount_positive на лету → dashboard_contract_v2 → V_DASH_* → Metabase
-- Executive показывают дофиксовые величины. Замер на контрольном окне
-- 27.07–16.08.2026 (то же, что в CHANGELOG Stage 1.5):
--   удержания          сейчас 79 168,68 ₽   должно 33 312,68 ₽
--   wb_reward Продажа  сейчас 47 452,07 ₽   должно 42 584,09 ₽
-- Обе величины совпадают с задокументированными ДОфиксовыми значениями Stage 1.5,
-- что подтверждает и диагноз, и цель восстановления.
--
-- ЭТОТ СКРИПТ. Ровно тот же UPDATE четырёх строк, что делал Stage 1.5.
-- Новые пары Stage ADS-1A («Доставка», «Возмещение издержек по перемещению
-- и операционной обработке товара») НЕ трогаются — они уже корректны.
-- Скрипт идемпотентен: WHERE отбирает только строки в неверном состоянии.
-- ============================================================================

UPDATE `wb_mart.REF_COST_MAP`
SET economic_direction       = 'ADJUSTMENT',
    field_normalization_sign = IF(op_key = 'Продажа' AND amount_field = 'commission_amount',
                                  1, field_normalization_sign),
    note = CONCAT(note, ' | Stage 1.5 (2026-08-26): знакопеременное поле — знак СОХРАНЯЕТСЯ.'
                     || ' Построчный ABS() отражал возвраты/кредиты как расход.'
                     || ' [восстановлено Stage ADS-1A 06.09.2026]')
WHERE economic_direction = 'COST'
  AND ((op_key = 'Продажа'                  AND amount_field = 'commission_amount')
    OR (op_key = 'Удержание'                AND amount_field = 'deduction')
    OR (op_key = 'Штраф'                    AND amount_field = 'penalty')
    OR (op_key = 'Пересчет платной приемки' AND amount_field = 'acceptance'));

-- ── Приёмка восстановления ───────────────────────────────────────────────────
-- 1) Четыре пары обязаны быть ADJUSTMENT; Продажа/commission_amount — со знаком +1.
ASSERT (SELECT COUNTIF(economic_direction <> 'ADJUSTMENT')
        FROM `wb_mart.REF_COST_MAP`
        WHERE (op_key='Продажа'                  AND amount_field='commission_amount')
           OR (op_key='Удержание'                AND amount_field='deduction')
           OR (op_key='Штраф'                    AND amount_field='penalty')
           OR (op_key='Пересчет платной приемки' AND amount_field='acceptance')) = 0
  AS 'ADS-1A repair: не все четыре пары Stage 1.5 переведены в ADJUSTMENT';

ASSERT (SELECT field_normalization_sign FROM `wb_mart.REF_COST_MAP`
        WHERE op_key='Продажа' AND amount_field='commission_amount') = 1
  AS 'ADS-1A repair: Продажа/commission_amount должен иметь field_normalization_sign = +1';

-- 2) Зеркало guard fix #5: знакопеременных пар в ветке COST/CREDIT быть не должно.
ASSERT (SELECT COUNT(*) FROM (
          SELECT l.op_key, l.amount_field
          FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` l
          WHERE l.economic_direction IN ('COST','CREDIT')
          GROUP BY l.op_key, l.amount_field
          HAVING COUNTIF(l.source_signed_amount > 0) > 0
             AND COUNTIF(l.source_signed_amount < 0) > 0)) = 0
  AS 'ADS-1A repair: осталась знакопеременная пара в ветке COST/CREDIT';

-- 3) Неизвестные денежные пары по всей истории = 0 (эффект самого Stage ADS-1A).
ASSERT (SELECT COUNTIF(cost_category IS NULL)
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`) = 0
  AS 'ADS-1A repair: остались денежные пары вне REF_COST_MAP';

-- 4) Контрольные величины Stage 1.5 на окне 27.07–16.08.2026 обязаны вернуться.
--    Ожидание: удержания 33 312,68 ₽ · wb_reward по продажам 42 584,09 ₽.
ASSERT (SELECT ROUND(SUM(IF(cost_category='deduction', cost_amount_positive, 0)),2)
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE finance_date BETWEEN '2026-07-27' AND '2026-08-16') = 33312.68
  AS 'ADS-1A repair: удержания за 27.07–16.08 не равны контрольным 33 312,68 ₽';

ASSERT (SELECT ROUND(SUM(IF(cost_category='wb_reward' AND op_key='Продажа', cost_amount_positive, 0)),2)
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE finance_date BETWEEN '2026-07-27' AND '2026-08-16') = 42584.09
  AS 'ADS-1A repair: wb_reward по продажам за 27.07–16.08 не равен контрольным 42 584,09 ₽';
