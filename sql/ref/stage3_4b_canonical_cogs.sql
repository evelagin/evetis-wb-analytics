-- ============================================================================
-- STAGE 3.4B — CANONICAL COGS REGISTRY & HISTORICAL SKU IDENTITY REPAIR
-- Дата: 2026-09-04. База: HEAD 3352d5fd7242cece6438ef67a384390ae133e40a.
-- Контракт: docs/ozon/STAGE_3_4B_CANONICAL_COGS_AND_IDENTITY_REPORT.md
--
-- ЧТО ЭТО. Канонический слой себестоимости, выведенный из семи деклараций
--   на товары, и починка исторических идентичностей Ozon.
--
-- AUTHORITATIVE SOURCE.
--   docs/legal/wb-fire-2026/ПРОВЕРКА_2026-09-01_себестоимость_ДДС_претензия.md §3
--   Первичка: 7 ДТ, 7 инвойсов, выписки четырёх банковских счетов, счета
--   перевозчика и брокера, отчёты предотгрузочной инспекции.
--   Контрольные суммы источника закрываются в ноль:
--     ФТС ↔ графа 47 семи ДТ = 2 999 868,30 ₽
--     платежи поставщикам ↔ семь инвойсов = 839 865,15 CNY
--     FIFO по валютному счёту ВТБ = 0,00 CNY остатка
--
-- 🔴 ИЗМЕНЕНИЕ ЗНАЧЕНИЙ, ВИДИМОЕ ДЛЯ WB — ACK ВЛАДЕЛЬЦА 2026-09-04.
--   V_PRODUCT_COGS_EFFECTIVE читают wb_mart.V_FACT_FINANCE_COGS и
--   wb_mart.V_MART_SKU_DAILY_COGS. Объекты wb_raw/wb_mart НЕ изменяются,
--   но значения себестоимости в них изменятся: документальная себестоимость
--   ниже прежней модельной. Оценка сдвига ~19 000 ₽ в сторону уменьшения
--   COGS WB. Владелец выбрал вариант rebuild осознанно.
--   Карточки Metabase 40-71 читают только V_DASH_* и этих вью не видят.
--
-- МЕТОД УЧЁТА. EFFECTIVE_DATE_COST. Это соглашение учёта, НЕ физическая
--   прослеживаемость партии. batch_traceability = NOT_PROVEN (Stage 3.4A §6).
--   Пул запасов общий с WB, поэтому Ozon-only FIFO запрещён.
--
-- МИГРАЦИЯ. Значения обновляются хирургически (UPDATE), а не DELETE+INSERT:
--   cogs_history_id, границы интервалов и метаданные реконструкции
--   Stage 3.0.3 сохраняются. Меняются product_cogs_rub, confidence,
--   source_refs, notes.
--
-- ОТКАТ: sql/ref/stage3_4b_rollback.sql
--   Снимки: evetis_ref.BAK_20260904_REF_SKU_COGS_HISTORY
--           evetis_ref.BAK_20260904_REF_SKU_CHANNEL_MAP
--
-- ИДЕМПОТЕНТНОСТЬ. CREATE OR REPLACE + MERGE. Повторный прогон даёт тот же
--   результат и не плодит строк.
-- ============================================================================

-- ========================================================== REF_COST_BATCH
-- Grain: одна партия ВВОЗА (одна декларация на товары).
-- Отсутствовавший ранее уровень: партии товара (PB-*) к декларациям не привязаны.
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`
(
  batch_id               STRING  NOT NULL OPTIONS(description="Канонический id партии ввоза, BATCH-01..BATCH-07"),
  batch_number           STRING  NOT NULL OPTIONS(description="Номер декларации на товары"),
  import_date            DATE    NOT NULL OPTIONS(description="Дата декларации"),
  receipt_date           DATE             OPTIONS(description="Дата фактического прихода на фулфилмент. NULL = не подтверждена документами, домысливать запрещено"),
  currency               STRING  NOT NULL OPTIONS(description="Валюта total_landed_cost_rub"),
  source_document        STRING  NOT NULL OPTIONS(description="Документ-источник итоговых чисел"),
  source_type            STRING  NOT NULL OPTIONS(description="Тип источника"),
  total_quantity         INT64   NOT NULL OPTIONS(description="Всего единиц в партии по декларации"),
  total_landed_cost_rub  NUMERIC NOT NULL OPTIONS(description="Полная landed-стоимость партии"),
  calculation_method     STRING  NOT NULL OPTIONS(description="Метод расчёта и распределения"),
  provenance_status      STRING  NOT NULL OPTIONS(description="PROVEN_DOCUMENT | RECONSTRUCTED | NOT_PROVEN"),
  source_reference       STRING  NOT NULL OPTIONS(description="Точная ссылка на раздел источника"),
  created_at             TIMESTAMP NOT NULL OPTIONS(description="Момент создания строки")
)
CLUSTER BY batch_id
OPTIONS(description="Канонические партии ввоза EVETIS. Источник — семь деклараций на товары. Единственный разрешённый источник себестоимости продукта.");

MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH` T
USING (
  SELECT * FROM UNNEST([
    STRUCT('BATCH-01' AS batch_id, '10013160/270824/3261814' AS batch_number, DATE '2024-08-27' AS import_date, 16000 AS total_quantity, NUMERIC '4160956.47' AS total_landed_cost_rub),
    ('BATCH-02', '10013160/250225/5083526', DATE '2025-02-25', 10200, NUMERIC '2360097.06'),
    ('BATCH-03', '10013160/250225/5083193', DATE '2025-02-25',  9800, NUMERIC '1617236.27'),
    ('BATCH-04', '10132160/280825/5101312', DATE '2025-08-28', 10012, NUMERIC '1313764.87'),
    ('BATCH-05', '10132160/311225/5304850', DATE '2025-12-31', 15000, NUMERIC '2574511.57'),
    ('BATCH-06', '10132160/190126/5020158', DATE '2026-01-19', 12000, NUMERIC '1599518.39'),
    ('BATCH-07', '10132160/260326/5122067', DATE '2026-03-26',  9829, NUMERIC '1690236.50')
  ])
) S
ON T.batch_id = S.batch_id
WHEN MATCHED THEN UPDATE SET
  batch_number = S.batch_number, import_date = S.import_date,
  total_quantity = S.total_quantity, total_landed_cost_rub = S.total_landed_cost_rub
WHEN NOT MATCHED THEN INSERT (batch_id, batch_number, import_date, receipt_date, currency,
  source_document, source_type, total_quantity, total_landed_cost_rub,
  calculation_method, provenance_status, source_reference, created_at)
VALUES (S.batch_id, S.batch_number, S.import_date, NULL, 'RUB',
  'ПРОВЕРКА_2026-09-01_себестоимость_ДДС_претензия.md',
  'CUSTOMS_DECLARATION_BANK_VERIFIED', S.total_quantity, S.total_landed_cost_rub,
  'LANDED_COST_ACTUAL_FX_INVOICE_PROPORTIONAL', 'PROVEN_DOCUMENT',
  'docs/legal/wb-fire-2026/ПРОВЕРКА_2026-09-01_себестоимость_ДДС_претензия.md §3',
  CURRENT_TIMESTAMP());

-- ====================================================== REF_COST_BATCH_SKU
-- Grain: batch_id × internal_sku.
-- factory_cost_unit NULL: авторитетный источник даёт только итоговую landed ₽/ед
-- по SKU. Брать товарную составляющую из документа 31.08 нельзя — там другая
-- итоговая себестоимость, смешивание двух расчётов дало бы несогласованные числа.
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU`
(
  batch_id              STRING  NOT NULL OPTIONS(description="Ссылка на REF_COST_BATCH"),
  internal_sku          STRING  NOT NULL OPTIONS(description="Внутренний артикул EVETIS"),
  quantity              INT64   NOT NULL OPTIONS(description="Единиц данного SKU в партии"),
  factory_cost_unit     NUMERIC          OPTIONS(description="Товарная составляющая ₽/ед. NULL = авторитетный источник даёт только landed"),
  landed_cost_unit_rub  NUMERIC NOT NULL OPTIONS(description="Landed себестоимость ₽/ед по документам"),
  total_landed_cost_rub NUMERIC NOT NULL OPTIONS(description="quantity × landed_cost_unit_rub, производное"),
  effective_from        DATE    NOT NULL OPTIONS(description="Начало действия себестоимости, включительно"),
  effective_to          DATE             OPTIONS(description="Конец действия, включительно. NULL = действует"),
  source_reference      STRING  NOT NULL OPTIONS(description="Ссылка на источник"),
  confidence            STRING  NOT NULL OPTIONS(description="Надёжность ВЕЛИЧИНЫ себестоимости"),
  provenance_status     STRING  NOT NULL OPTIONS(description="PROVEN_DOCUMENT | RECONSTRUCTED | NOT_PROVEN")
)
CLUSTER BY internal_sku, batch_id
OPTIONS(description="Себестоимость по партии ввоза и SKU. Только product landed cost: расходы маркетплейса сюда не входят никогда.");

MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU` T
USING (
  SELECT batch_id, internal_sku, quantity, landed_cost_unit_rub, effective_from, effective_to,
         CAST(quantity AS NUMERIC) * landed_cost_unit_rub AS total_landed_cost_rub
  FROM UNNEST([
    STRUCT('BATCH-01' AS batch_id, 'EVT-HC-BODY-300' AS internal_sku, 6000 AS quantity, NUMERIC '208.05' AS landed_cost_unit_rub, DATE '2024-09-07' AS effective_from, DATE '2025-10-31' AS effective_to),
    ('BATCH-01', 'EVT-HC-HAND-300',    6000, NUMERIC '208.05', DATE '2024-09-07', DATE '2025-01-25'),
    ('BATCH-01', 'EVT-SET-HAND-BODY',  4000, NUMERIC '416.10', DATE '2024-09-10', DATE '2025-04-30'),
    ('BATCH-02', 'EVT-HC-HAND-300',   10200, NUMERIC '231.38', DATE '2025-03-10', NULL),
    ('BATCH-03', 'EVT-FS-ACNE-30',     5000, NUMERIC '168.00', DATE '2025-03-05', DATE '2026-07-31'),
    ('BATCH-03', 'EVT-FS-MOIST-30',    4800, NUMERIC '161.93', DATE '2025-03-05', DATE '2026-02-28'),
    ('BATCH-04', 'EVT-FC-ACNE-50',     4995, NUMERIC '131.66', DATE '2025-09-16', NULL),
    ('BATCH-04', 'EVT-FC-MOIST-50',    5017, NUMERIC '130.78', DATE '2025-09-14', NULL),
    ('BATCH-05', 'EVT-EP-ENZYME-75',   5000, NUMERIC '155.97', DATE '2026-01-09', NULL),
    ('BATCH-05', 'EVT-FT-MOIST-150',   5000, NUMERIC '179.46', DATE '2026-01-08', NULL),
    ('BATCH-05', 'EVT-FT-ACNE-150',    5000, NUMERIC '179.46', DATE '2026-01-10', NULL),
    ('BATCH-06', 'EVT-FS-ACNE-30',     6000, NUMERIC '135.78', DATE '2026-08-01', NULL),
    ('BATCH-06', 'EVT-FS-MOIST-30',    6000, NUMERIC '130.81', DATE '2026-03-01', NULL),
    ('BATCH-07', 'EVT-HC-CHERRY-300',  4859, NUMERIC '171.96', DATE '2026-04-10', NULL),
    ('BATCH-07', 'EVT-HC-AMBER-300',   4970, NUMERIC '171.96', DATE '2026-04-10', NULL)
  ])
) S
ON T.batch_id = S.batch_id AND T.internal_sku = S.internal_sku
WHEN MATCHED THEN UPDATE SET
  quantity = S.quantity, landed_cost_unit_rub = S.landed_cost_unit_rub,
  total_landed_cost_rub = S.total_landed_cost_rub,
  effective_from = S.effective_from, effective_to = S.effective_to
WHEN NOT MATCHED THEN INSERT (batch_id, internal_sku, quantity, factory_cost_unit,
  landed_cost_unit_rub, total_landed_cost_rub, effective_from, effective_to,
  source_reference, confidence, provenance_status)
VALUES (S.batch_id, S.internal_sku, S.quantity, NULL,
  S.landed_cost_unit_rub, S.total_landed_cost_rub, S.effective_from, S.effective_to,
  'ПРОВЕРКА_2026-09-01 §3, таблица «По каждому SKU внутри партии»',
  'PROVEN_DOCUMENT', 'PROVEN_DOCUMENT');

-- ============================== REF_SKU_COGS_HISTORY: значения из канона ====
-- Хирургический UPDATE. Интервалы, cogs_history_id и метаданные реконструкции
-- Stage 3.0.3 сохраняются: неверны были ВЕЛИЧИНЫ, а не границы.
-- TR-001 — разборка импортных наборов, себестоимость = половина набора
-- партии 1: 416.10 / 2 = 208.05. Это INVENTORY_TRANSFORMATION, не закупка.
UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` T
SET product_cogs_rub = S.new_cogs,
    confidence = 'PROVEN_DOCUMENT',
    source_refs = ARRAY_CONCAT(
      IFNULL(T.source_refs, []),
      [CONCAT('COST_BATCH:', S.batch_id), 'DOC:ПРОВЕРКА_2026-09-01#§3']),
    notes = CONCAT(IFNULL(CONCAT(T.notes, ' | '), ''),
      'Stage 3.4B 2026-09-04: величина заменена на документальную из ',
      S.batch_id, ' (было ', CAST(T.product_cogs_rub AS STRING), ' ₽). ',
      'Границы интервала не менялись.')
FROM (
  SELECT * FROM UNNEST([
    STRUCT('EVT-HC-HAND-300' AS internal_sku, 'PB-001' AS pb, 'BATCH-01' AS batch_id, NUMERIC '208.05' AS new_cogs),
    ('EVT-HC-BODY-300',   'PB-003', 'BATCH-01', NUMERIC '208.05'),
    ('EVT-SET-HAND-BODY', 'IS-004', 'BATCH-01', NUMERIC '416.10'),
    ('EVT-HC-HAND-300',   'TR-001', 'BATCH-01', NUMERIC '208.05'),
    ('EVT-HC-BODY-300',   'TR-001', 'BATCH-01', NUMERIC '208.05'),
    ('EVT-HC-HAND-300',   'PB-002', 'BATCH-02', NUMERIC '231.38'),
    ('EVT-FS-ACNE-30',    'PB-022', 'BATCH-03', NUMERIC '168.00'),
    ('EVT-FS-MOIST-30',   'PB-020', 'BATCH-03', NUMERIC '161.93'),
    ('EVT-FC-ACNE-50',    'PB-031', 'BATCH-04', NUMERIC '131.66'),
    ('EVT-FC-MOIST-50',   'PB-030', 'BATCH-04', NUMERIC '130.78'),
    ('EVT-EP-ENZYME-75',  'PB-050', 'BATCH-05', NUMERIC '155.97'),
    ('EVT-FT-MOIST-150',  'PB-040', 'BATCH-05', NUMERIC '179.46'),
    ('EVT-FT-ACNE-150',   'PB-041', 'BATCH-05', NUMERIC '179.46'),
    ('EVT-FS-ACNE-30',    'PB-023', 'BATCH-06', NUMERIC '135.78'),
    ('EVT-FS-MOIST-30',   'PB-021', 'BATCH-06', NUMERIC '130.81'),
    ('EVT-HC-CHERRY-300', 'PB-010', 'BATCH-07', NUMERIC '171.96'),
    ('EVT-HC-AMBER-300',  'PB-011', 'BATCH-07', NUMERIC '171.96')
  ])
) S
WHERE T.internal_sku = S.internal_sku
  AND T.physical_batch_id = S.pb
  AND T.product_cogs_rub != S.new_cogs;

-- ================== REF_SKU_CHANNEL_MAP: исторические идентичности Ozon ====
-- Корень дефекта: карта заполнялась из ТЕКУЩЕГО списка товаров Ozon API,
-- поэтому снятые с продажи карточки в неё не попадали. Историчность в схеме
-- уже была (valid_from/valid_to/is_current), не использовалась только она.
-- Обе связки доказаны дважды и независимо:
--   1) ozon_raw.RAW_OZON_SUPPLY_BUNDLES несёт sku, offer_id и product_name;
--   2) первичные документы Ozon (RealizationReportCIS, DocumentB2BSales).
MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` T
USING (
  SELECT * FROM UNNEST([
    STRUCT('EVT-HC-BODY-300' AS internal_sku, 'OZON' AS marketplace,
           '1997315236' AS marketplace_sku, '252442341' AS offer_id,
           DATE '2025-04-18' AS valid_from, DATE '2026-04-09' AS valid_to),
    ('EVT-SET-HAND-BODY', 'OZON', '2046027307', '252441968',
     DATE '2025-04-18', DATE '2026-04-17')
  ])
) S
ON T.marketplace = S.marketplace AND T.marketplace_sku = S.marketplace_sku
WHEN NOT MATCHED THEN INSERT (internal_sku, marketplace, marketplace_sku,
  marketplace_product_id, offer_id, vendor_code, valid_from, valid_to,
  is_current, mapping_source, mapping_status, verified_at, loaded_at)
VALUES (S.internal_sku, S.marketplace, S.marketplace_sku, NULL, S.offer_id, NULL,
  S.valid_from, S.valid_to, FALSE,
  'ozon_raw.RAW_OZON_SUPPLY_BUNDLES (sku+offer_id+product_name) + первичные документы Ozon: RealizationReportCIS-12363791169560.xlsx, DocumentB2BSales_12297392881090_2773848.xlsx',
  'OZON_PRIMARY_DOCUMENT', DATE '2026-09-04', CURRENT_TIMESTAMP());

-- ============================ V_PRODUCT_COGS_EFFECTIVE: интерфейс сохранён ==
-- Первые десять колонок и их порядок не меняются — оба потребителя в wb_mart
-- выбирают колонки поимённо (SELECT * не используется, проверено).
-- Добавлены поля происхождения в конец.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
OPTIONS(description="Действующая себестоимость продукта на дату. Stage 3.4B: значения выведены из семи деклараций. cost_method=EFFECTIVE_DATE, batch_traceability=NOT_PROVEN — метод является соглашением учёта, а не физической прослеживаемостью партии.")
AS
SELECT
  h.internal_sku,
  h.effective_from,
  h.effective_to,
  h.product_cogs_rub,
  'MATERIALIZED'   AS resolver_lane,
  h.cogs_origin_type,
  h.cogs_history_id  AS resolver_ref,
  h.is_reconstructed,
  h.confidence,
  h.owner_confirmed,
  -- Stage 3.4B: происхождение
  b.batch_id                       AS cost_batch_id,
  b.batch_number                   AS cost_batch_number,
  'EFFECTIVE_DATE'                 AS cost_method,
  'NOT_PROVEN'                     AS batch_traceability,
  IF(b.batch_id IS NULL, 'DERIVED_OR_TRANSFORMED', 'PROVEN_DOCUMENT') AS cogs_provenance_status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU` bs
       ON bs.internal_sku = h.internal_sku
      AND bs.effective_from = h.effective_from
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH` b
       ON b.batch_id = bs.batch_id
UNION ALL
SELECT
  internal_sku,
  effective_from,
  effective_to,
  product_cogs_rub,
  'DERIVED_BUNDLE' AS resolver_lane,
  'FF_ASSEMBLED_DERIVED' AS cogs_origin_type,
  CONCAT('BUNDLE:', internal_sku, ':', FORMAT_DATE('%Y%m%d', effective_from)) AS resolver_ref,
  TRUE  AS is_reconstructed,
  'DERIVED_FROM_COMPONENTS' AS confidence,
  FALSE AS owner_confirmed,
  CAST(NULL AS STRING) AS cost_batch_id,
  CAST(NULL AS STRING) AS cost_batch_number,
  'EFFECTIVE_DATE'     AS cost_method,
  'NOT_PROVEN'         AS batch_traceability,
  'DERIVED_FROM_COMPONENTS' AS cogs_provenance_status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

-- =========================================================== ПРОВЕРКИ ======
-- A-1 семь партий
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`) = 7
  AS 'A-1 REF_COST_BATCH должен содержать ровно 7 партий';

-- A-2 количество единиц сходится точно
ASSERT (SELECT SUM(total_quantity) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`) = 82841
  AS 'A-2 сумма единиц по партиям должна быть ровно 82841';

-- A-3 landed стоимость сходится точно
ASSERT (SELECT SUM(total_landed_cost_rub) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`) = NUMERIC '15316321.13'
  AS 'A-3 сумма landed по партиям должна быть ровно 15316321.13 ₽';

-- A-4 разбивка по SKU совпадает с партиями по количеству
ASSERT (SELECT SUM(quantity) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH_SKU`) = 82841
  AS 'A-4 сумма единиц по batch×SKU должна быть ровно 82841';

-- A-5 интервалы себестоимости не пересекаются внутри SKU
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` a
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` b
      USING (internal_sku)
    WHERE a.cogs_history_id != b.cogs_history_id
      AND a.effective_from <= IFNULL(b.effective_to, DATE '9999-12-31')
      AND b.effective_from <= IFNULL(a.effective_to, DATE '9999-12-31')
  )) = 0
  AS 'A-5 интервалы действия себестоимости не должны пересекаться внутри internal_sku';

-- A-6 ни одной незакрытой исторической идентичности Ozon
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT DISTINCT p.sku
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
      ON m.marketplace = 'OZON' AND m.marketplace_sku = p.sku
    WHERE m.internal_sku IS NULL
  )) = 0
  AS 'A-6 все Ozon SKU из postings должны резолвиться в internal_sku';

-- A-7 одна идентичность Ozon — ровно один internal_sku
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT marketplace_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
    WHERE marketplace = 'OZON'
    GROUP BY marketplace_sku HAVING COUNT(DISTINCT internal_sku) > 1
  )) = 0
  AS 'A-7 один Ozon SKU не может указывать на несколько internal_sku';

-- A-8 расходы маркетплейса в product COGS отсутствуют по построению:
-- REF_COST_BATCH_SKU питается только из деклараций и счетов перевозки/брокера.
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_COST_BATCH`
  WHERE calculation_method != 'LANDED_COST_ACTUAL_FX_INVOICE_PROPORTIONAL'
  ) = 0
  AS 'A-8 метод расчёта должен быть единым landed-методом без расходов площадки';

-- ============ ЗАКРЫТИЕ 3.4B: историческая граница BOM по фактам Ozon =======
-- Три набора продавались на Ozon РАНЬШЕ, чем начинается effective_from их
-- состава. Причина: интервалы BOM реконструированы в Stage 3.0.3 по движениям
-- WB; в шапке sql/ref/pr_ref_cogs_history.sql записано дословно, что движения
-- через Ozon в той реконструкции не восстанавливались.
--
-- Себестоимость КОМПОНЕНТОВ на эти даты доступна у всех трёх. NULL возникал
-- только из-за границы интервала состава.
--
-- Расширяем ТОЛЬКО там, где состав доказан независимым источником Ozon.
-- Состав НЕ меняется. Период раньше доказательства не выдумывается.
--
--   EVT-SET-MOIST-TONIC-SERUM   PROVEN_SAME_BOM
--     «Отчет о реализации товара_20260430»: «Набор для лица увлажняющий:
--      сыворотка и тоник...» — состав назван поимённо, совпадает с BOM.
--   EVT-SET-TON-SER-CREAM-ACNE  PROVEN_SAME_BOM
--     тот же отчёт: «Набор для лица от прыщей и акне: тоник, сыворотка, крем».
--   EVT-SET-HAND-CHERRY         PARTIAL — НЕ расширяется.
--     Поставка 2026-04-13 доказывает существование карточки, но её название
--     «Набор парфюмированных кремов для рук и тела с церамидами, 2 шт 300 мл»
--     ПОБАЙТОВО совпадает с карточкой EVT-SET-HAND-AMBER и не различает
--     Lost Cherry от Amber Vanilla. Атрибуция состава опиралась бы на тот же
--     источник, что и исправляемая граница. COGS остаётся NULL.
--
-- Самая ранняя доказанная дата существования состава — 2026-03-24
-- (первый заказ поставки обоих наборов).
UPDATE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
SET effective_from = DATE '2026-03-24',
    effective_from_is_reconstructed = TRUE,
    effective_from_window_from = DATE '2026-03-24',
    effective_from_window_to = effective_from,
    source_ref = CONCAT(IFNULL(CONCAT(source_ref, ' | '), ''),
      'OZON_PRIMARY_DOCUMENT: Отчет о реализации товара_20260430 (состав назван в наименовании карточки) + ozon_raw.RAW_OZON_SUPPLY_BUNDLES первая поставка 2026-03-24'),
    notes = CONCAT(IFNULL(CONCAT(notes, ' | '), ''),
      'Stage 3.4B 2026-09-04: историческая граница расширена назад с ',
      CAST(effective_from AS STRING), ' до 2026-03-24. Основание — независимые ',
      'production-факты Ozon, которых не было при реконструкции Stage 3.0.3 (она ',
      'строилась только по движениям WB). Состав НЕ менялся, расширен только интервал.')
WHERE bundle_internal_sku IN ('EVT-SET-MOIST-TONIC-SERUM','EVT-SET-TON-SER-CREAM-ACNE')
  AND effective_from > DATE '2026-03-24';

-- A-9 состав не изменился: те же компоненты и количества, что в снимке
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT bundle_internal_sku, component_internal_sku, component_qty
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
    EXCEPT DISTINCT
    SELECT bundle_internal_sku, component_internal_sku, component_qty
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.BAK_20260904_REF_BUNDLE_COMPONENTS`)
) = 0 AS 'A-9 состав наборов не должен меняться, расширяется только интервал';
