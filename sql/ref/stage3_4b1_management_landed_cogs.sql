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
-- ОТКАТ: RETIRED — DO NOT EXECUTE (R2D-3d, 2026-09-18). sql/ref/stage3_4b1_rollback.sql
--   выведен из эксплуатации; исправления — только forward-fix миграцией.
--   Снимок: evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY (Σ = 3197.45)
-- ============================================================================

-- ============================================================ РЕГИСТР ======
-- Регистр дополнительных landed-расходов: и применённые, и НЕприменённые.
--
-- 🔴 STAGE A (2026-09-08). Раньше здесь стояла отсылка «см. развёрнутый скрипт
--    этапа», а самого DDL и наполнения в репозитории не было: authoritative
--    источник таблицы существовал только в production. Ниже — восстановленное
--    определение, снятое с production (INFORMATION_SCHEMA.TABLES.ddl) и
--    сверенное построчно с 16 живыми строками.
--
-- НЕРАЗРУШАЮЩАЯ ИДЕМПОТЕНТНОСТЬ. CREATE TABLE IF NOT EXISTS + MERGE WHEN NOT
--    MATCHED. Повторный прогон НЕ переписывает существующие строки: значения,
--    которые уже приняты владельцем (ACK 2026-09-04), защищены от отката.
--    Именно поэтому здесь НЕТ CREATE OR REPLACE TABLE.
--
-- МАСКИРОВАНИЕ. В source_reference номера расчётных счетов сокращены до
--    последних четырёх цифр. Это единственное отличие сида от production, и оно
--    не может попасть в production: MERGE не обновляет уже существующие строки.
--    Неусечённые реквизиты — в docs/legal/ (на диске, вне репозитория) и в
--    самой production-таблице. На расчёт COGS поле не влияет.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_ADDITIONAL_LANDED`
(
  cost_item_id       STRING  NOT NULL OPTIONS(description="Логический ключ строки"),
  cost_category      STRING  NOT NULL OPTIONS(description="CERTIFICATION | MARKING_CHZ | STATION_REMOVAL | VED_CONSULTING"),
  payment_date       DATE             OPTIONS(description="Дата платежа. NULL для агрегированных строк"),
  counterparty       STRING           OPTIONS(description="Контрагент"),
  amount_rub         NUMERIC NOT NULL OPTIONS(description="Сумма платежа"),
  payment_purpose    STRING           OPTIONS(description="Назначение платежа как в выписке"),
  batch_id           STRING           OPTIONS(description="Партия ввоза, если доказана"),
  internal_sku       STRING           OPTIONS(description="Товар, если доказан"),
  allocated_unit_rub NUMERIC          OPTIONS(description="Аллокация на единицу. NULL = не аллоцировано"),
  allocation_method  STRING  NOT NULL OPTIONS(description="BATCH_QUANTITY | NONE"),
  allocation_basis   STRING           OPTIONS(description="Основание аллокации"),
  attribution_status STRING  NOT NULL OPTIONS(description="PRODUCT_SPECIFIC | AMBIGUOUS | GENERIC_NOT_ATTRIBUTABLE | PREPAYMENT_NOT_CONSUMPTION | NOT_FOUND"),
  accounting_class   STRING  NOT NULL OPTIONS(description="IN_MANAGEMENT_LANDED_COGS | PERIOD_COST_CERTIFICATION | PREPAID_MARKING | OPERATING_IMPORT_OVERHEAD | NOT_RECOGNISED"),
  owner_decision     STRING           OPTIONS(description="Решение владельца ACK 2026-09-04"),
  source_reference   STRING  NOT NULL OPTIONS(description="Где доказано"),
  confidence         STRING  NOT NULL OPTIONS(description="Надёжность"),
  created_at         TIMESTAMP NOT NULL
)
CLUSTER BY cost_category
OPTIONS(
  description="Дополнительные landed-расходы сверх LEGAL_IMPORT_COST. Хранит и применённые, и НЕприменённые категории с классификацией и решением владельца. Stage 3.4B.1."
);

MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_ADDITIONAL_LANDED` T
USING (SELECT * FROM UNNEST([
    STRUCT('CERT-AMB-20251221' AS cost_item_id, 'CERTIFICATION' AS cost_category, DATE '2025-12-21' AS payment_date, 'ООО СТЭП' AS counterparty, NUMERIC '5000' AS amount_rub, 'ДС ТР ТС (крем для тела) ТМ EVETIS' AS payment_purpose, CAST(NULL AS STRING) AS batch_id, CAST(NULL AS STRING) AS internal_sku, CAST(NULL AS NUMERIC) AS allocated_unit_rub, 'NONE' AS allocation_method, CAST(NULL AS STRING) AS allocation_basis, 'AMBIGUOUS' AS attribution_status, 'PERIOD_COST_CERTIFICATION' AS accounting_class, 'ACK 2026-09-04 решение №2: не включать. Не различает какой из трёх кремов' AS owner_decision, 'bank acct ****1818' AS source_reference, 'DOCUMENTED_PRODUCT_AMBIGUOUS' AS confidence, TIMESTAMP '2026-09-04 10:20:41.321495 UTC' AS created_at),
    ('CERT-B05-EP', 'CERTIFICATION', DATE '2026-04-22', 'ООО СТЭП', NUMERIC '16625', 'ДС ТР ТС (энзимная пудра, увлажняющий тоник, тоник акне)', 'BATCH-05', 'EVT-EP-ENZYME-75', NUMERIC '3.325', 'BATCH_QUANTITY', '49875 ₽ / 15000 ед партии 5', 'PRODUCT_SPECIFIC', 'IN_MANAGEMENT_LANDED_COGS', 'ACK 2026-09-04 решение №1: применить', 'bank acct ****1818; Stage 3.4B.1 certification_allocation.csv', 'PROVEN_DOCUMENT', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-B05-FTA', 'CERTIFICATION', DATE '2026-04-22', 'ООО СТЭП', NUMERIC '16625', 'ДС ТР ТС (энзимная пудра, увлажняющий тоник, тоник акне)', 'BATCH-05', 'EVT-FT-ACNE-150', NUMERIC '3.325', 'BATCH_QUANTITY', '49875 ₽ / 15000 ед партии 5', 'PRODUCT_SPECIFIC', 'IN_MANAGEMENT_LANDED_COGS', 'ACK 2026-09-04 решение №1: применить', 'bank acct ****1818; Stage 3.4B.1 certification_allocation.csv', 'PROVEN_DOCUMENT', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-B05-FTM', 'CERTIFICATION', DATE '2026-04-22', 'ООО СТЭП', NUMERIC '16625', 'ДС ТР ТС (энзимная пудра, увлажняющий тоник, тоник акне)', 'BATCH-05', 'EVT-FT-MOIST-150', NUMERIC '3.325', 'BATCH_QUANTITY', '49875 ₽ / 15000 ед партии 5', 'PRODUCT_SPECIFIC', 'IN_MANAGEMENT_LANDED_COGS', 'ACK 2026-09-04 решение №1: применить', 'bank acct ****1818; Stage 3.4B.1 certification_allocation.csv', 'PROVEN_DOCUMENT', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-B07-AMBER', 'CERTIFICATION', DATE '2026-05-28', 'ООО СТЭП', NUMERIC '16457.8', 'ДС ТР ТС HAND & BODY CREAM Lost Cherry и Amber Vanilla', 'BATCH-07', 'EVT-HC-AMBER-300', NUMERIC '3.311629', 'BATCH_QUANTITY', '32550 ₽ / 9829 ед партии 7', 'PRODUCT_SPECIFIC', 'IN_MANAGEMENT_LANDED_COGS', 'ACK 2026-09-04 решение №1: применить', 'bank acct ****1818; Stage 3.4B.1 certification_allocation.csv', 'PROVEN_DOCUMENT', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-B07-CHERRY', 'CERTIFICATION', DATE '2026-05-28', 'ООО СТЭП', NUMERIC '16092.2', 'ДС ТР ТС HAND & BODY CREAM Lost Cherry и Amber Vanilla', 'BATCH-07', 'EVT-HC-CHERRY-300', NUMERIC '3.311629', 'BATCH_QUANTITY', '32550 ₽ / 9829 ед партии 7', 'PRODUCT_SPECIFIC', 'IN_MANAGEMENT_LANDED_COGS', 'ACK 2026-09-04 решение №1: применить', 'bank acct ****1818; Stage 3.4B.1 certification_allocation.csv', 'PROVEN_DOCUMENT', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20240529', 'CERTIFICATION', DATE '2024-05-29', 'ООО АБСОЛЮТ СТАНДАРТ', NUMERIC '6500', 'Консультационные услуги по счету № 1849 от 21.05.2024', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank export.csv acct ****2775', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20240918', 'CERTIFICATION', DATE '2024-09-18', 'ООО АБСОЛЮТ СТАНДАРТ', NUMERIC '15000', 'Консультационные и сопроводительные услуги', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank export.csv acct ****2775', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20241227', 'CERTIFICATION', DATE '2024-12-27', 'ООО СТЭП', NUMERIC '5000', 'Организация работ для получения ДС 353 ПП (косметика)', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS. Не входит в 168425 документа', 'bank acct ****4928', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20250610', 'CERTIFICATION', DATE '2025-06-10', 'ООО СТЭП', NUMERIC '28000', 'Организация работ для получения ДС ТР ТС (косметика)', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank acct ****1818', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20250704', 'CERTIFICATION', DATE '2025-07-04', 'ООО СТЭП', NUMERIC '5000', 'Организация работ для получения ДС ТР ТС (косметика)', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank acct ****1818', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20251030', 'CERTIFICATION', DATE '2025-10-30', 'ООО СТЭП', NUMERIC '16500', 'ДС ТР ТС (Средства ухода за кожей)', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank acct ****1818', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CERT-GEN-20251111', 'CERTIFICATION', DATE '2025-11-11', 'ООО СТЭП', NUMERIC '10000', 'ДС 353 ПП (Средства ухода за кожей)', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'PERIOD_COST_CERTIFICATION', 'ACK 2026-09-04 решение №2: не включать в SKU COGS', 'bank acct ****1818', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('CHZ-TOTAL', 'MARKING_CHZ', CAST(NULL AS DATE), 'ОПЕРАТОР-ЦРПТ ООО', NUMERIC '27180', '8 предоплат за коды маркировки, лицевой счёт 2608486, 16.10.2025..27.07.2026', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'PREPAYMENT_NOT_CONSUMPTION', 'PREPAID_MARKING', 'ACK 2026-09-04 решение №3: не включать до отчёта ЦРПТ о списании кодов', 'bank acct ****1818; Stage 3.4B.1 marking_allocation.csv', 'ALLOCATION_NOT_PROVEN', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('STATION-NOT-FOUND', 'STATION_REMOVAL', CAST(NULL AS DATE), CAST(NULL AS STRING), NUMERIC '0', 'Вывоз со станции по 6 партиям из 7 — платежей не найдено', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'NOT_FOUND', 'NOT_RECOGNISED', 'ACK 2026-09-04 решение №5: принять NOT_FOUND, оценку 2.5 ₽/ед не использовать', 'сплошной поиск по всем банковским счетам; Stage 3.4B.1 station_logistics_evidence.csv', 'NOT_FOUND', TIMESTAMP '2026-09-04 10:20:41.321495 UTC'),
    ('VED-CONSULT-TOTAL', 'VED_CONSULTING', CAST(NULL AS DATE), 'Васильева Елена Леонтьевна', NUMERIC '731400', 'Консультационные услуги ВЭД, 13 платежей', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS NUMERIC), 'NONE', CAST(NULL AS STRING), 'GENERIC_NOT_ATTRIBUTABLE', 'OPERATING_IMPORT_OVERHEAD', 'ACK 2026-09-04 решение №4: не включать в MANAGEMENT_LANDED_COGS, обязательно в operating P&L', 'bank acct ****1818', 'DOCUMENTED', TIMESTAMP '2026-09-04 10:20:41.321495 UTC')
])) S
ON T.cost_item_id = S.cost_item_id
WHEN NOT MATCHED THEN INSERT (cost_item_id, cost_category, payment_date, counterparty, amount_rub,
  payment_purpose, batch_id, internal_sku, allocated_unit_rub, allocation_method, allocation_basis,
  attribution_status, accounting_class, owner_decision, source_reference, confidence, created_at)
VALUES (S.cost_item_id, S.cost_category, S.payment_date, S.counterparty, S.amount_rub,
  S.payment_purpose, S.batch_id, S.internal_sku, S.allocated_unit_rub, S.allocation_method,
  S.allocation_basis, S.attribution_status, S.accounting_class, S.owner_decision,
  S.source_reference, S.confidence, S.created_at);

-- 🔴 STAGE A (2026-09-08): ГЕЙТ ПОВТОРНОГО ПРОГОНА — fail-closed.
--    UPDATE ниже АДДИТИВЕН (product_cogs_rub + add_unit) и НЕ идемпотентен:
--    второй прогон файла прибавил бы сертификацию ещё раз и завысил бы
--    себестоимость пяти SKU вдвое по этой статье. ASSERT B1-5 такой откат НЕ
--    ловит — он проверяет «изменилось ровно 5 строк», что верно и после
--    двойного начисления.
--    Условие открытия: ни одна строка ещё не расходится со снимком
--    BAK_20260904B_REF_SKU_COGS_HISTORY, то есть аллокация не применена.
--    Восстанавливать историю из снимка ради повторного прогона нельзя (R2D-3d):
--    повтор не выполняется, исправления — отдельной forward-fix миграцией.
ASSERT (SELECT COUNT(*)
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY` o
          USING (cogs_history_id)
        WHERE h.product_cogs_rub != o.product_cogs_rub) = 0
  AS 'STAGE 3.4B.1 RE-RUN BLOCKED: аллокация сертификации уже применена к REF_SKU_COGS_HISTORY (расхождение со снимком BAK_20260904B). Повторный прогон UPDATE удвоил бы дополнительную landed-стоимость. Повторный прогон не выполняется; восстановление из снимка выведено (R2D-3d) — исправление отдельной forward-fix миграцией.';

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

-- ================================ V_PRODUCT_COGS_EFFECTIVE: ред. 3.4B.1 =====
-- 🔴 STAGE A (2026-09-08). AUTHORITATIVE SOURCE ЭТОГО VIEW ТЕПЕРЬ ЗДЕСЬ.
--    До Stage A определение существовало ТОЛЬКО в production: файл этапа
--    описывал контракт в комментариях, но CREATE OR REPLACE VIEW не содержал.
--    Текст ниже снят с production (INFORMATION_SCHEMA.VIEWS.view_definition,
--    SHA-256 191f9baa1421fd05f5d9cc47532aadaadc9085c5f93b8c81a4d959a216ab5f37,
--    2216 байт) и приведён к репозиторному оформлению без изменения семантики.
--
-- ЧТО ДОБАВЛЯЕТ 3.4B.1 К РЕДАКЦИИ 3.4B (15 колонок → 18):
--    legal_import_cost_unit_rub      LEGAL_IMPORT_COST на единицу, из
--                                    REF_COST_BATCH_SKU.landed_cost_unit_rub.
--                                    NULL для наборов (DERIVED_BUNDLE).
--    additional_documented_unit_rub  сумма аллокаций из
--                                    REF_COST_ADDITIONAL_LANDED, только строки
--                                    accounting_class='IN_MANAGEMENT_LANDED_COGS'.
--                                    0 (не NULL), если аллокаций нет.
--    cost_basis                      LEGAL_IMPORT_COST_ONLY | MANAGEMENT_LANDED_COGS
--                                    | MANAGEMENT_LANDED_COGS_DERIVED (наборы).
--
-- ⚠️ product_cogs_rub НЕ пересчитывается этим view: он уже равен
--    MANAGEMENT_LANDED_COGS, потому что UPDATE выше прибавил аллокацию прямо в
--    REF_SKU_COGS_HISTORY. Прибавлять additional_documented_unit_rub повторно
--    ЗАПРЕЩЕНО — это дало бы двойной счёт сертификации.
--
-- ПОТРЕБИТЕЛИ cost_basis (проверено 2026-09-08):
--    wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT  (sql/pricing/pr2_wb_forward_economics.sql)
--    ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT (sql/ozon/stage3_4d2_ozon_mart_forward.sql)
--    ozon_mart.V_OZON_AGENT_DECISION_INPUT       (sql/ozon/stage3_4d3_ozon_mart_agent_contract.sql)
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
OPTIONS(description="Действующая себестоимость продукта на дату. Stage 3.4B.1: product_cogs_rub = MANAGEMENT_LANDED_COGS; отдельными колонками публикуются LEGAL_IMPORT_COST и документально доказанная дополнительная аллокация. cost_method=EFFECTIVE_DATE, batch_traceability=NOT_PROVEN — метод является соглашением учёта, а не физической прослеживаемостью партии.")
AS
SELECT
  h.internal_sku,
  h.effective_from,
  h.effective_to,
  h.product_cogs_rub,
  'MATERIALIZED'     AS resolver_lane,
  h.cogs_origin_type,
  h.cogs_history_id  AS resolver_ref,
  h.is_reconstructed,
  h.confidence,
  h.owner_confirmed,
  b.batch_id         AS cost_batch_id,
  b.batch_number     AS cost_batch_number,
  'EFFECTIVE_DATE'   AS cost_method,
  'NOT_PROVEN'       AS batch_traceability,
  IF(b.batch_id IS NULL, 'DERIVED_OR_TRANSFORMED', 'PROVEN_DOCUMENT') AS cogs_provenance_status,
  bs.landed_cost_unit_rub                       AS legal_import_cost_unit_rub,
  IFNULL(ad.additional_unit_rub, NUMERIC '0')   AS additional_documented_unit_rub,
  IF(ad.additional_unit_rub IS NULL, 'LEGAL_IMPORT_COST_ONLY', 'MANAGEMENT_LANDED_COGS') AS cost_basis
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU` bs
       ON bs.internal_sku = h.internal_sku AND bs.effective_from = h.effective_from
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH` b
       ON b.batch_id = bs.batch_id
LEFT JOIN (
  SELECT internal_sku, SUM(allocated_unit_rub) additional_unit_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_ADDITIONAL_LANDED`
  WHERE accounting_class = 'IN_MANAGEMENT_LANDED_COGS' AND internal_sku IS NOT NULL
  GROUP BY internal_sku) ad ON ad.internal_sku = h.internal_sku
UNION ALL
SELECT
  internal_sku, effective_from, effective_to, product_cogs_rub,
  'DERIVED_BUNDLE' AS resolver_lane,
  'FF_ASSEMBLED_DERIVED' AS cogs_origin_type,
  CONCAT('BUNDLE:', internal_sku, ':', FORMAT_DATE('%Y%m%d', effective_from)) AS resolver_ref,
  TRUE AS is_reconstructed,
  'DERIVED_FROM_COMPONENTS' AS confidence,
  FALSE AS owner_confirmed,
  CAST(NULL AS STRING) AS cost_batch_id,
  CAST(NULL AS STRING) AS cost_batch_number,
  'EFFECTIVE_DATE' AS cost_method,
  'NOT_PROVEN' AS batch_traceability,
  'DERIVED_FROM_COMPONENTS' AS cogs_provenance_status,
  CAST(NULL AS NUMERIC) AS legal_import_cost_unit_rub,
  CAST(NULL AS NUMERIC) AS additional_documented_unit_rub,
  'MANAGEMENT_LANDED_COGS_DERIVED' AS cost_basis
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

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

-- ── Stage A: контракт редакции 3.4B.1 должен присутствовать в resolver ──────
--    Гейт ловит откат к 15- или 10-колоночной редакции, если кто-то прогонит
--    более ранний файл этапа поверх.
ASSERT (SELECT COUNTIF(column_name IN ('legal_import_cost_unit_rub',
                                       'additional_documented_unit_rub',
                                       'cost_basis'))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = 'V_PRODUCT_COGS_EFFECTIVE') = 3
  AS 'B1-6 FAIL: V_PRODUCT_COGS_EFFECTIVE потерял колонки редакции 3.4B.1';

ASSERT (SELECT COUNTIF(cost_basis NOT IN ('LEGAL_IMPORT_COST_ONLY',
                                          'MANAGEMENT_LANDED_COGS',
                                          'MANAGEMENT_LANDED_COGS_DERIVED'))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 0
  AS 'B1-7 FAIL: недопустимое значение cost_basis';

ASSERT (SELECT COUNTIF(additional_documented_unit_rub IS NULL AND resolver_lane = 'MATERIALIZED')
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 0
  AS 'B1-8 FAIL: additional_documented_unit_rub обязан быть 0, а не NULL, в MATERIALIZED-ветке';

ASSERT (SELECT COUNTIF(product_cogs_rub IS NULL OR product_cogs_rub <= 0)
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 0
  AS 'B1-9 FAIL: resolver отдаёт NULL или неположительную себестоимость';
