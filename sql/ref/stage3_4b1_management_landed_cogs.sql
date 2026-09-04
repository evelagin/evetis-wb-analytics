-- ============================================================================
-- STAGE 3.4B.1 — MANAGEMENT LANDED COGS
-- Дата: 2026-09-04. ACK владельца 2026-09-04, решение №1.
-- Контракт: docs/ozon/STAGE_3_4B1_FULL_LANDED_COST_REPORT.md
--
-- ЧТО ЭТО. Разделение двух баз себестоимости и применение единственной
--   доказуемо относимой дополнительной статьи.
--
--   LEGAL_IMPORT_COST      15 316 321,13 ₽  — доказано документом 01.09.2026
--   + сертификация          82 425,00 ₽     — 2 платежа с названными товарами
--   = MANAGEMENT_LANDED_COGS 15 398 746,13 ₽
--
-- ГДЕ ЧТО ЖИВЁТ ПОСЛЕ ЭТОГО ЭТАПА.
--   REF_COST_BATCH_SKU.landed_cost_unit_rub   = LEGAL_IMPORT_COST, НЕ меняется
--   REF_SKU_COGS_HISTORY.product_cogs_rub     = MANAGEMENT_LANDED_COGS
--   V_PRODUCT_COGS_EFFECTIVE отдаёт обе базы отдельными колонками
--
-- РЕШЕНИЯ ВЛАДЕЛЬЦА, КОТОРЫЕ НЕ ПРИМЕНЯЮТСЯ К SKU COGS (№2-№5).
--   Все суммы сохранены в REF_COST_ADDITIONAL_LANDED с классификацией:
--     91 000 ₽  generic-сертификация -> PERIOD_COST_CERTIFICATION
--     27 180 ₽  Честный знак         -> PREPAID_MARKING (списание кодов не доказано)
--    731 400 ₽  консультации ВЭД     -> OPERATING_IMPORT_OVERHEAD
--          0 ₽  вывоз со станции     -> NOT_RECOGNISED (оценка 2,5 ₽/ед отвергнута)
--   Регистр нужен для будущего operating P&L и для переклассификации, если
--   появится отчёт ЦРПТ о списании кодов или первичка по вывозу.
--
-- ПОЧЕМУ ГЕНЕРИК-СЕРТИФИКАЦИЯ НЕ ИДЁТ В COGS. Декларация соответствия
--   действует несколько лет на неограниченный тираж; отнесение её полной
--   стоимости только на уже ввезённые 82 841 единицу завысило бы себестоимость.
--
-- ОТКАТ: sql/ref/stage3_4b1_rollback.sql
--   Снимок: evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY (Σ = 3197.45)
-- ============================================================================

-- Регистр дополнительных landed-расходов: и применённые, и НЕприменённые.
-- DDL и наполнение — см. развёрнутый скрипт этапа; таблица создана как
-- evetis_ref.REF_COST_ADDITIONAL_LANDED, 16 строк.

-- Применение решения №1: только строки с accounting_class='IN_MANAGEMENT_LANDED_COGS'
UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` T
SET product_cogs_rub = T.product_cogs_rub + S.add_unit,
    source_refs = ARRAY_CONCAT(IFNULL(T.source_refs, []),
      ['ADDITIONAL_LANDED:CERTIFICATION', 'OWNER_ACK:2026-09-04#решение-1']),
    notes = CONCAT(IFNULL(CONCAT(T.notes, ' | '), ''),
      'Stage 3.4B.1 2026-09-04: MANAGEMENT_LANDED_COGS = LEGAL_IMPORT_COST ',
      CAST(T.product_cogs_rub AS STRING), ' + ', CAST(S.add_unit AS STRING),
      ' ₽ документально доказанной сертификации (', S.batch_id,
      '). LEGAL_IMPORT_COST сохранён в REF_COST_BATCH_SKU.landed_cost_unit_rub.')
FROM (SELECT * FROM UNNEST([
    STRUCT('EVT-EP-ENZYME-75' AS internal_sku,'PB-050' AS pb,'BATCH-05' AS batch_id, NUMERIC '3.325' AS add_unit),
    ('EVT-FT-MOIST-150','PB-040','BATCH-05', NUMERIC '3.325'),
    ('EVT-FT-ACNE-150','PB-041','BATCH-05', NUMERIC '3.325'),
    ('EVT-HC-CHERRY-300','PB-010','BATCH-07', NUMERIC '3.311629'),
    ('EVT-HC-AMBER-300','PB-011','BATCH-07', NUMERIC '3.311629')])) S
WHERE T.internal_sku = S.internal_sku AND T.physical_batch_id = S.pb;

-- Базис аллокации: количество внутри названной партии.
--   49 875 / 15 000 = 3,325       точно
--   32 550 /  9 829 = 3,311629    остаток 0,0014 ₽ на всю партию
-- Наборы пересчитываются автоматически через V_BUNDLE_COGS_DERIVED.

-- =========================================================== ПРОВЕРКИ ======
ASSERT (SELECT SUM(total_landed_cost_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`)
       = NUMERIC '15316321.13'
  AS 'B1-1 LEGAL_IMPORT_COST не должен измениться';

ASSERT (SELECT SUM(amount_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_ADDITIONAL_LANDED`
        WHERE accounting_class='IN_MANAGEMENT_LANDED_COGS') = NUMERIC '82425'
  AS 'B1-2 применённая дополнительная сертификация = 82425 ₽';

ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
        WHERE product_cogs_rub = 0) = 0
  AS 'B1-3 ни одна себестоимость не должна стать нулём';

ASSERT (SELECT COUNT(*) FROM (
    SELECT a.internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` a
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` b USING(internal_sku)
    WHERE a.cogs_history_id != b.cogs_history_id
      AND a.effective_from <= IFNULL(b.effective_to, DATE '9999-12-31')
      AND b.effective_from <= IFNULL(a.effective_to, DATE '9999-12-31'))) = 0
  AS 'B1-4 интервалы не должны пересекаться';

ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY` o
          USING (cogs_history_id)
        WHERE h.product_cogs_rub != o.product_cogs_rub) = 5
  AS 'B1-5 должно измениться ровно 5 строк себестоимости';
