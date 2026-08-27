-- ============================================================================
-- STAGE 3.1A — PRODUCT COGS REFERENCE LAYER (evetis_ref)
-- Дата: 2026-08-27.  Контракт: docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md
-- Утверждение: Stage 3.0.3B, COGS_CONTRACT_PREIMPLEMENTATION_VALIDATION = PASS.
--
-- ЧТО ЭТО. Независимый справочный слой исторической Product COGS.
--   Product COGS = закупка + расходы в Китае + таможня + доставка ДО фулфилмента.
--   НЕ входят: хранение ФФ, сборка, разборка, маркировка, отгрузка ФФ,
--   доставка ФФ -> склад маркетплейса, расходы кабинета WB, ЗП, аренда, банк, налог.
--
-- ЧТО ЭТО НЕ. Слой НЕ подключён к MART_SKU_DAILY, V_DASH_*, FACT_*, Metabase.
--   Подключение к дашбордам — отдельный этап с отдельным ACK владельца.
--
-- MARKETPLACE-INDEPENDENT. Master key = internal_sku. Идентификаторов канала
--   (wb_nm_id и т.п.) в слое НЕТ. V_PRODUCT_COGS_EFFECTIVE не зависит ни от
--   одного объекта вне evetis_ref. Канальные идентификаторы резолвятся снаружи.
--   Проверка покрытия событий WB — WB-специфична и живёт в
--   sql/ref/pr_ref_cogs_validation.sql, а не внутри evetis_ref.
--
-- ОГРАНИЧЕНИЕ (зафиксировано дословно, Stage 3.0.3 §12):
--   Stage 3.0.3 historical reconstruction is optimized for the WB analytics
--   pipeline. Inventory movements through Ozon, L'Etoile and other channels are
--   not reconstructed in this stage. Current Product COGS remains valid because
--   it is product-level; historical effective-date boundaries remain
--   management-grade estimates where cross-channel movements could matter.
--
-- ИДЕМПОТЕНТНОСТЬ. Скрипт целиком построен на CREATE SCHEMA IF NOT EXISTS и
--   CREATE OR REPLACE. Повторный прогон даёт побайтово тот же результат.
--
-- LOGICAL ROW KEYS. BigQuery не обеспечивает PRIMARY KEY физически, поэтому
--   cogs_history_id и bundle_component_id — logical row keys, а их уникальность
--   гарантируется ASSERT A-1..A-6 ниже, а не декларацией.
--
-- ОТКАТ: bash tools/stage3_1a_cogs_rollback.sh [--dry-run]
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref`
OPTIONS (
  location = 'EU',
  description = 'EVETIS reference layer. Marketplace-independent. Stage 3.1A: Product COGS history and bundle composition. Master key = internal_sku. No channel identifiers, no dependencies outside this dataset.'
);

-- ── 1. REF_SKU_COGS_HISTORY — 17 материализованных строк ────────────────────
--   14 PURCHASE_BATCH + 2 INVENTORY_TRANSFORMATION + 1 IMPORTED_FINISHED_SET.
--   FF-собранные наборы здесь НЕ материализуются (см. §3).
--   Четыре window-поля: from-окно и to-окно раскладывают одну forensic-границу
--   на два конца смежных интервалов. to-окно = from-окно, сдвинутое на -1 день,
--   потому что effective_to объявлена ВКЛЮЧИТЕЛЬНОЙ.
--   Шесть forensic-границ: T1 21-30.01.2025 / T2 03-21.03.2025 / T3 28.09-27.11.2025
--   / T4 07.04-05.06.2025 / T5 01.02-01.04.2026 / T6 01.04-01.09.2026.
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
OPTIONS (description = 'Stage 3.1A. Historical Product COGS intervals. 17 materialized rows. Product COGS = all costs up to arrival at fulfilment. FF operations, WB account costs, OPEX and tax are NOT included. Marketplace-independent: no channel identifiers. FF-assembled bundles are NOT materialized here.')
AS SELECT * FROM UNNEST([
STRUCT(
 'COGS-001' AS cogs_history_id,'EVT-HC-HAND-300' AS internal_sku,'Крем Руки' AS product_name,
 DATE '2024-09-07' AS effective_from, DATE '2025-01-25' AS effective_to,
 FALSE AS effective_from_is_reconstructed, CAST(NULL AS DATE) AS effective_from_window_from, CAST(NULL AS DATE) AS effective_from_window_to, CAST(NULL AS STRING) AS effective_from_transition_id,
 TRUE AS effective_to_is_reconstructed, DATE '2025-01-20' AS effective_to_window_from, DATE '2025-01-29' AS effective_to_window_to, 'T1' AS effective_to_transition_id,
 TRUE AS is_reconstructed, NUMERIC '219.00' AS product_cogs_rub,
 'PURCHASE_BATCH' AS cogs_origin_type,'PB-001' AS physical_batch_id,
 'STRONG_RECONSTRUCTION' AS confidence, TRUE AS owner_confirmed, NUMERIC '219' AS legacy_cogs_rub,
 'Первая закупочная партия 6000 ед. Начало = первое COGS-требующее событие 2024-09-07. Конец реконструирован по расходу (окно T1).' AS evidence_summary,
 ['owner:A2','COST_HISTORY:COST-001','stage:3.0.3#T1'] AS source_refs, CAST(NULL AS STRING) AS notes),
('COGS-002','EVT-HC-HAND-300','Крем Руки',DATE '2025-01-26',DATE '2025-03-09',
 TRUE,DATE '2025-01-21',DATE '2025-01-30','T1',
 TRUE,DATE '2025-03-02',DATE '2025-03-20','T2',
 TRUE,NUMERIC '214.50','INVENTORY_TRANSFORMATION','TR-001','MEDIUM_RECONSTRUCTION',TRUE,NUMERIC '235',
 'Единицы из разборки 1600 импортных наборов (429/2). Обе границы реконструированы: T1 на входе, T2 на выходе.',
 ['owner:A1','owner:A2','stage:3.0.3#TR-001'],
 'Стоимость разборки/перепаковки/маркировки в Product COGS НЕ входит и относится к слою fulfilment. Legacy 235 содержала ~20.50 руб операции ФФ.'),
('COGS-003','EVT-HC-HAND-300','Крем Руки',DATE '2025-03-10',CAST(NULL AS DATE),
 TRUE,DATE '2025-03-03',DATE '2025-03-21','T2',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,NUMERIC '240.00','PURCHASE_BATCH','PB-002','STRONG_RECONSTRUCTION',TRUE,NUMERIC '240',
 'Вторая закупочная партия 10000 ед, контракт RU01. Действует по настоящее время.',
 ['owner:A2','COST_HISTORY:COST-003','stage:3.0.3#T2'],CAST(NULL AS STRING)),
('COGS-004','EVT-HC-BODY-300','Крем Тело',DATE '2024-09-07',DATE '2025-10-31',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,DATE '2025-09-27',DATE '2025-11-26','T3',
 TRUE,NUMERIC '219.00','PURCHASE_BATCH','PB-003','STRONG_RECONSTRUCTION',TRUE,NUMERIC '219',
 'Первая закупочная партия 6000 ед. Конец реконструирован по расходу (окно T3).',
 ['owner:A2','COST_HISTORY:COST-004','stage:3.0.3#T3'],
 'Legacy COST_HISTORY переводила крем тела на 235 руб с 2025-01-15; это опровергнуто расходом: на ту дату из 6000 ед израсходовано около 2400.'),
('COGS-005','EVT-HC-BODY-300','Крем Тело',DATE '2025-11-01',DATE '2026-04-26',
 TRUE,DATE '2025-09-28',DATE '2025-11-27','T3',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,NUMERIC '214.50','INVENTORY_TRANSFORMATION','TR-001','MEDIUM_RECONSTRUCTION',TRUE,NUMERIC '235',
 'Единицы из разборки 1600 импортных наборов (429/2). Интервал закрыт последним COGS-требующим событием 2026-04-26.',
 ['owner:A1','stage:3.0.3#TR-001'],
 'Интервал закрыт сознательно: SKU выбыл, остаток на WB = 0, в остатках фулфилмента позиция отсутствует. Событий после 2026-04-26 нет. Продажа после этой даты получит NULL (fail-closed) — это желаемое поведение.'),
('COGS-006','EVT-SET-HAND-BODY','Набор руки+тело (импорт из Китая)',DATE '2024-09-10',DATE '2025-04-30',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,DATE '2025-04-06',DATE '2025-06-04','T4',
 TRUE,NUMERIC '429.00','IMPORTED_FINISHED_SET','IS-004','OWNER_CONFIRMED',TRUE,NUMERIC '419',
 'Импортированный готовый набор как неделимая товарная единица. Партия 4000 ед: 2400 проданы набором, 1600 разобраны (TR-001).',
 ['owner:A1','stage:3.0.3#T4'],
 'Величина 429 подтверждена владельцем; дата окончания импортного режима реконструирована (окно T4). Суммой компонентов НЕ заменяется. С 2025-05-01 действует derived-режим через REF_BUNDLE_COMPONENTS.'),
('COGS-007','EVT-HC-CHERRY-300','Крем Вишня',DATE '2026-04-10',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '172.00','PURCHASE_BATCH','PB-010','STRONG_RECONSTRUCTION',TRUE,NUMERIC '190',
 'Одна физическая партия 4859 ед за всю историю SKU, не исчерпана (остаток ФФ 4320 ед на 16-17.08.2026). Переходов нет.',
 ['owner:A3','COST_HISTORY:COST-010'],
 'Начало = первое COGS-требующее событие 2026-04-10 (продажа набора вишня+амбра), а не первая продажа самого крема 2026-04-20. Legacy 190 содержала буфер FF/логистики.'),
('COGS-008','EVT-HC-AMBER-300','Крем Амбра',DATE '2026-04-10',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '172.00','PURCHASE_BATCH','PB-011','STRONG_RECONSTRUCTION',TRUE,NUMERIC '190',
 'Одна физическая партия 4970 ед за всю историю SKU, не исчерпана (остаток ФФ 4400 ед). Переходов нет.',
 ['owner:A3','COST_HISTORY:COST-011'],
 'Начало = первое COGS-требующее событие 2026-04-10 (продажа набора вишня+амбра), а не первая продажа самого крема 2026-04-18.'),
('COGS-009','EVT-FS-MOIST-30','Сыворотка УВЛ',DATE '2025-03-05',DATE '2026-02-28',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,DATE '2026-01-31',DATE '2026-03-31','T5',
 TRUE,NUMERIC '165.00','PURCHASE_BATCH','PB-020','CONFIRMED_SYSTEM_RECORD',FALSE,NUMERIC '165',
 'Первая партия 5000 ед, контракт RU02. Величина — запись системы, владельцем отдельно не подтверждалась.',
 ['COST_HISTORY:COST-020','stage:3.0.3#T5'],CAST(NULL AS STRING)),
('COGS-010','EVT-FS-MOIST-30','Сыворотка УВЛ',DATE '2026-03-01',CAST(NULL AS DATE),
 TRUE,DATE '2026-02-01',DATE '2026-04-01','T5',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,NUMERIC '133.00','PURCHASE_BATCH','PB-021','STRONG_RECONSTRUCTION',TRUE,NUMERIC '150',
 'Вторая партия 6000 ед, приход на ФФ конец января 2026. Два независимых метода дали границу в пределах одного месяца.',
 ['owner:A4','owner:A5','stage:3.0.3#T5'],
 'Граница подтверждается сходимостью: выход последней единицы PB-020 с фулфилмента пришёлся на январь 2026 — месяц owner-confirmed прихода PB-021; расход по продажам пересёк 5000 ед 2026-03-09.'),
('COGS-011','EVT-FS-ACNE-30','Сыворотка АКНЕ',DATE '2025-03-05',DATE '2026-07-31',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,DATE '2026-03-31',DATE '2026-08-31','T6',
 TRUE,NUMERIC '165.00','PURCHASE_BATCH','PB-022','CONFIRMED_SYSTEM_RECORD',FALSE,NUMERIC '165',
 'Первая партия 5000 ед, контракт RU02. Величина — запись системы, владельцем отдельно не подтверждалась.',
 ['COST_HISTORY:COST-022','stage:3.0.3#T6'],
 '''2026-08-01 — MANAGEMENT_RECONSTRUCTED_BOUNDARY, а не физическая дата окончательного перехода партии. Forensic Stage 3.0.3 доказал одновременное существование старой и новой партии после этой даты: старый остаток PB-022 сохранялся, в частности, в Казани (~153 ед., отгрузка 13.11.2025), тогда как новая партия PB-023 уже поступала на WB (отгрузки 24.07 и 28.07.2026). Effective-date-модель v1 сознательно аппроксимирует mixed-batch consumption. Опорные свидетельства: разрыв поставок 2025-12-01 -> 2026-05-08 (158 дней); выход последней единицы PB-022 с фулфилмента ~ февраль-апрель 2026; окно выбытия складов WB 18.07 -> 05.08.2026, в котором утрачено 203 ед. (143 старой партии + 60 новой), сошедшихся поштучно с листом потерь. Количественная чувствительность: 812 ед. x 27 руб = 21 924 руб при сдвиге границы по всему окну 2026-04-01 ... 2026-09-01. Batch-level / FIFO-оценка — возможное будущее расширение, для management-grade v1 не требуется.'''),
('COGS-012','EVT-FS-ACNE-30','Сыворотка АКНЕ',DATE '2026-08-01',CAST(NULL AS DATE),
 TRUE,DATE '2026-04-01',DATE '2026-09-01','T6',
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 TRUE,NUMERIC '138.00','PURCHASE_BATCH','PB-023','MEDIUM_RECONSTRUCTION',TRUE,NUMERIC '150',
 'Вторая партия 6000 ед, приход на ФФ конец января 2026. Граница опирается на окно выбытия складов WB и разрыв поставок.',
 ['owner:A4','owner:A6','stage:3.0.3#T6'],
 '''2026-08-01 — MANAGEMENT_RECONSTRUCTED_BOUNDARY, а не физическая дата окончательного перехода партии. Forensic Stage 3.0.3 доказал одновременное существование старой и новой партии после этой даты: старый остаток PB-022 сохранялся, в частности, в Казани (~153 ед., отгрузка 13.11.2025), тогда как новая партия PB-023 уже поступала на WB (отгрузки 24.07 и 28.07.2026). Effective-date-модель v1 сознательно аппроксимирует mixed-batch consumption. Опорные свидетельства: разрыв поставок 2025-12-01 -> 2026-05-08 (158 дней); выход последней единицы PB-022 с фулфилмента ~ февраль-апрель 2026; окно выбытия складов WB 18.07 -> 05.08.2026, в котором утрачено 203 ед. (143 старой партии + 60 новой), сошедшихся поштучно с листом потерь. Количественная чувствительность: 812 ед. x 27 руб = 21 924 руб при сдвиге границы по всему окну 2026-04-01 ... 2026-09-01. Batch-level / FIFO-оценка — возможное будущее расширение, для management-grade v1 не требуется.'''),
('COGS-013','EVT-FC-MOIST-50','Крем УВЛ лицо',DATE '2025-09-14',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '145.00','PURCHASE_BATCH','PB-030','STRONG_RECONSTRUCTION',TRUE,NUMERIC '145',
 'Одна физическая партия 5017 ед, контракт RU03. Переходов нет.',
 ['owner:A2','COST_HISTORY:COST-030'],
 'Партия практически исчерпана: расход 4818 ед, остаток ФФ 0, остаток WB 5 ед. Следующая партия 5000 ед ожидалась не ранее октября 2026 и на момент заморозки контракта не приходовалась.'),
('COGS-014','EVT-FC-ACNE-50','Крем АКНЕ лицо',DATE '2025-09-16',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '145.00','PURCHASE_BATCH','PB-031','STRONG_RECONSTRUCTION',TRUE,NUMERIC '145',
 'Одна физическая партия 4995 ед, контракт RU03. Переходов нет.',
 ['owner:A2','COST_HISTORY:COST-031'],CAST(NULL AS STRING)),
('COGS-015','EVT-FT-MOIST-150','Тоник УВЛ',DATE '2026-01-08',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '182.00','PURCHASE_BATCH','PB-040','STRONG_RECONSTRUCTION',TRUE,NUMERIC '200',
 'Одна физическая партия 5000 ед в составе закупки RU04 (15000 ед). Не исчерпана: остаток ФФ 3600 ед. Переходов нет.',
 ['owner:A3','COST_HISTORY:COST-040'],CAST(NULL AS STRING)),
('COGS-016','EVT-EP-ENZYME-75','Энзимная пудра',DATE '2026-01-09',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '159.00','PURCHASE_BATCH','PB-050','STRONG_RECONSTRUCTION',TRUE,NUMERIC '174',
 'Одна физическая партия 5000 ед в составе закупки RU04. Не исчерпана: остаток ФФ 3630 ед. Переходов нет.',
 ['owner:A3','COST_HISTORY:COST-050'],CAST(NULL AS STRING)),
('COGS-017','EVT-FT-ACNE-150','Тоник АКНЕ',DATE '2026-01-10',CAST(NULL AS DATE),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),
 FALSE,NUMERIC '182.00','PURCHASE_BATCH','PB-041','STRONG_RECONSTRUCTION',TRUE,NUMERIC '200',
 'Одна физическая партия 5000 ед в составе закупки RU04. Не исчерпана: остаток ФФ 3600 ед. Переходов нет.',
 ['owner:A3','COST_HISTORY:COST-041'],CAST(NULL AS STRING))
]);

-- ── 2. REF_BUNDLE_COMPONENTS — 33 строки состава, 14 наборов ────────────────
--   Стоимостных полей НЕТ: Product COGS набора выводится из компонентов.
--   BC-01/BC-02 действуют ТОЛЬКО с 2025-05-01: до этой даты набор руки+тело был
--   импортной неделимой единицей (COGS-006). Периоды взаимоисключающие (D-3).
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
OPTIONS (description = 'Stage 3.1A. Bundle composition for FF-assembled sets. 33 rows / 14 bundles. NO cost columns: bundle Product COGS is derived as SUM(component_qty * component product_cogs on date). Assembly cost is a separate fulfilment layer and is NOT part of Product COGS.')
AS SELECT * FROM UNNEST([
STRUCT(
 'BC-01' AS bundle_component_id,'EVT-SET-HAND-BODY' AS bundle_internal_sku,'EVT-HC-HAND-300' AS component_internal_sku, 1 AS component_qty,
 DATE '2025-05-01' AS effective_from, CAST(NULL AS DATE) AS effective_to,
 TRUE AS effective_from_is_reconstructed, DATE '2025-04-07' AS effective_from_window_from, DATE '2025-06-05' AS effective_from_window_to,'T4' AS effective_from_transition_id,
 FALSE AS effective_to_is_reconstructed, CAST(NULL AS DATE) AS effective_to_window_from, CAST(NULL AS DATE) AS effective_to_window_to, CAST(NULL AS STRING) AS effective_to_transition_id,
 'FF_ASSEMBLED' AS assembly_model,'BUNDLES sheet + owner:A1' AS source_ref,
 'Действует ТОЛЬКО с 2025-05-01. До этой даты набор был импортной неделимой единицей (COGS-006, 429 руб). Периоды взаимоисключающие (инвариант D-3).' AS notes),
('BC-02','EVT-SET-HAND-BODY','EVT-HC-BODY-300',1,DATE '2025-05-01',CAST(NULL AS DATE),TRUE,DATE '2025-04-07',DATE '2025-06-05','T4',FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet + owner:A1','Действует ТОЛЬКО с 2025-05-01, см. BC-01.'),
('BC-03','EVT-SET-SER-CREAM-MOIST','EVT-FS-MOIST-30',1,DATE '2025-10-31',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-04','EVT-SET-SER-CREAM-MOIST','EVT-FC-MOIST-50',1,DATE '2025-10-31',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-05','EVT-SET-SER-CREAM-ACNE','EVT-FS-ACNE-30',1,DATE '2025-11-01',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-06','EVT-SET-SER-CREAM-ACNE','EVT-FC-ACNE-50',1,DATE '2025-11-01',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-07','EVT-SET-ACNE-POWDER-SERUM-CREAM','EVT-EP-ENZYME-75',1,DATE '2026-01-15',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','SKU_MASTER.bundle_components_source','Набор ни разу не отгружался на WB: событий ноль, определение существует, но никогда не вычисляется. DQ: в листе BUNDLES сумма 474, в COST_HISTORY/SKU_MASTER 497 — обе legacy, source of truth не являются.'),
('BC-08','EVT-SET-ACNE-POWDER-SERUM-CREAM','EVT-FS-ACNE-30',1,DATE '2026-01-15',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','SKU_MASTER.bundle_components_source',CAST(NULL AS STRING)),
('BC-09','EVT-SET-ACNE-POWDER-SERUM-CREAM','EVT-FC-ACNE-50',1,DATE '2026-01-15',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','SKU_MASTER.bundle_components_source',CAST(NULL AS STRING)),
('BC-10','EVT-SET-TON-CREAM-MOIST','EVT-FT-MOIST-150',1,DATE '2026-03-19',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-11','EVT-SET-TON-CREAM-MOIST','EVT-FC-MOIST-50',1,DATE '2026-03-19',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-12','EVT-SET-TON-SER-CREAM-MOIST','EVT-FT-MOIST-150',1,DATE '2026-03-20',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet','Определение начинается после границы T5 (2026-03-01), поэтому фазы 492 руб у набора не было никогда — только 460.'),
('BC-13','EVT-SET-TON-SER-CREAM-MOIST','EVT-FS-MOIST-30',1,DATE '2026-03-20',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-14','EVT-SET-TON-SER-CREAM-MOIST','EVT-FC-MOIST-50',1,DATE '2026-03-20',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-15','EVT-SET-4PC-ACNE','EVT-EP-ENZYME-75',1,DATE '2026-03-24',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-16','EVT-SET-4PC-ACNE','EVT-FT-ACNE-150',1,DATE '2026-03-24',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-17','EVT-SET-4PC-ACNE','EVT-FS-ACNE-30',1,DATE '2026-03-24',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-18','EVT-SET-4PC-ACNE','EVT-FC-ACNE-50',1,DATE '2026-03-24',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-19','EVT-SET-CHERRY-AMBER','EVT-HC-CHERRY-300',1,DATE '2026-04-10',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet','Первое COGS-требующее событие бренда для компонентов CHERRY и AMBER — 2026-04-10 (продажа этого набора).'),
('BC-20','EVT-SET-CHERRY-AMBER','EVT-HC-AMBER-300',1,DATE '2026-04-10',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-21','EVT-SET-TON-SER-CREAM-ACNE','EVT-FT-ACNE-150',1,DATE '2026-04-23',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-22','EVT-SET-TON-SER-CREAM-ACNE','EVT-FS-ACNE-30',1,DATE '2026-04-23',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-23','EVT-SET-TON-SER-CREAM-ACNE','EVT-FC-ACNE-50',1,DATE '2026-04-23',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-24','EVT-SET-HAND-AMBER','EVT-HC-HAND-300',1,DATE '2026-04-26',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-25','EVT-SET-HAND-AMBER','EVT-HC-AMBER-300',1,DATE '2026-04-26',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-26','EVT-SET-HAND-CHERRY','EVT-HC-HAND-300',1,DATE '2026-05-01',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-27','EVT-SET-HAND-CHERRY','EVT-HC-CHERRY-300',1,DATE '2026-05-01',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-28','EVT-SET-TON-CREAM-ACNE','EVT-FT-ACNE-150',1,DATE '2026-05-03',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-29','EVT-SET-TON-CREAM-ACNE','EVT-FC-ACNE-50',1,DATE '2026-05-03',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING)),
('BC-30','EVT-SET-ACNE-TONIC-SERUM','EVT-FT-ACNE-150',1,DATE '2026-07-09',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','SKU_MASTER.bundle_components_source','DQ: набор отсутствует в листе BUNDLES, состав взят из SKU_MASTER.'),
('BC-31','EVT-SET-ACNE-TONIC-SERUM','EVT-FS-ACNE-30',1,DATE '2026-07-09',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','SKU_MASTER.bundle_components_source',CAST(NULL AS STRING)),
('BC-32','EVT-SET-MOIST-TONIC-SERUM','EVT-FT-MOIST-150',1,DATE '2026-07-20',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet','Определение начинается после границы T5, поэтому у набора только фаза 315 руб. DQ: в листе BUNDLES сумма 355, в COST_HISTORY 352 — обе legacy.'),
('BC-33','EVT-SET-MOIST-TONIC-SERUM','EVT-FS-MOIST-30',1,DATE '2026-07-20',CAST(NULL AS DATE),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),FALSE,CAST(NULL AS DATE),CAST(NULL AS DATE),CAST(NULL AS STRING),'FF_ASSEMBLED','BUNDLES sheet',CAST(NULL AS STRING))
]);

-- ── 3. V_BUNDLE_COGS_DERIVED — 21 производный интервал ──────────────────────
--   Interval intersection: период действия состава режется граничными точками
--   ценовых интервалов компонентов. Подынтервал, где хотя бы один компонент не
--   разрешается ровно в одну цену, ИСКЛЮЧАЕТСЯ (fail-closed), а не обнуляется.
--   Сборка (assembly cost) в Product COGS не входит.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`
OPTIONS (description = 'Stage 3.1A. Derived Product COGS intervals for FF-assembled bundles, built by interval intersection of component price intervals with the bundle composition period. Fail-closed: a sub-interval where at least one component does not resolve to exactly one price is EXCLUDED, never zero-filled. Assembly cost is not included.')
AS
WITH defs AS (
  SELECT bundle_internal_sku, component_internal_sku, component_qty,
         effective_from AS def_from,
         COALESCE(effective_to, DATE '9999-12-31') AS def_to
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`
),
def_span AS (
  SELECT bundle_internal_sku, MIN(def_from) AS b_from, MAX(def_to) AS b_to, COUNT(*) AS n_components
  FROM defs GROUP BY bundle_internal_sku
),
pts AS (
  SELECT bundle_internal_sku, b_from AS pt FROM def_span
  UNION DISTINCT
  SELECT d.bundle_internal_sku, p AS pt
  FROM defs d
  JOIN def_span s ON s.bundle_internal_sku = d.bundle_internal_sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
    ON h.internal_sku = d.component_internal_sku
  CROSS JOIN UNNEST(ARRAY_CONCAT([h.effective_from],
        IF(h.effective_to IS NULL, [], [DATE_ADD(h.effective_to, INTERVAL 1 DAY)]))) AS p
  WHERE p > s.b_from AND p <= s.b_to
),
ordered AS (
  SELECT bundle_internal_sku, pt,
         LEAD(pt) OVER (PARTITION BY bundle_internal_sku ORDER BY pt) AS next_pt
  FROM pts
),
segs AS (
  SELECT o.bundle_internal_sku, o.pt AS seg_from,
         LEAST(IF(o.next_pt IS NULL, s.b_to, DATE_SUB(o.next_pt, INTERVAL 1 DAY)), s.b_to) AS seg_to,
         s.n_components
  FROM ordered o JOIN def_span s ON s.bundle_internal_sku = o.bundle_internal_sku
),
priced AS (
  SELECT g.bundle_internal_sku, g.seg_from, g.seg_to, g.n_components,
         COUNT(h.cogs_history_id) AS resolved_components,
         SUM(d.component_qty * h.product_cogs_rub) AS bundle_cogs
  FROM segs g
  JOIN defs d ON d.bundle_internal_sku = g.bundle_internal_sku
             AND d.def_from <= g.seg_from AND g.seg_to <= d.def_to
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
    ON h.internal_sku = d.component_internal_sku
   AND h.effective_from <= g.seg_from
   AND g.seg_to <= COALESCE(h.effective_to, DATE '9999-12-31')
  GROUP BY 1,2,3,4
)
SELECT bundle_internal_sku AS internal_sku,
       seg_from AS effective_from,
       IF(seg_to = DATE '9999-12-31', NULL, seg_to) AS effective_to,
       bundle_cogs AS product_cogs_rub,
       n_components AS component_count
FROM priced
WHERE seg_from <= seg_to AND resolved_components = n_components;

-- ── 4. V_PRODUCT_COGS_EFFECTIVE — unified resolver, 38 интервалов ───────────
--   17 materialized + 21 derived. Зависит ТОЛЬКО от объектов внутри evetis_ref.
--   Правило потребителя: на COGS-требующее событие обязан примениться ровно один
--   интервал; ноль или больше одного => NULL (fail-closed), никогда 0.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
OPTIONS (description = 'Stage 3.1A. UNIFIED product-level Product COGS resolver: materialized base/imported COGS UNION ALL derived FF-assembled bundle COGS. Marketplace-independent: depends ONLY on objects inside evetis_ref, no channel identifiers, no dependency on wb_raw / wb_mart. Key = internal_sku + date. Consumer rule: exactly one interval must apply to a COGS-requiring event; zero or more than one => NULL (fail-closed), never 0.')
AS
SELECT internal_sku, effective_from, effective_to, product_cogs_rub,
       'MATERIALIZED' AS resolver_lane, cogs_origin_type, cogs_history_id AS resolver_ref,
       is_reconstructed, confidence, owner_confirmed
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`
UNION ALL
SELECT internal_sku, effective_from, effective_to, product_cogs_rub,
       'DERIVED_BUNDLE' AS resolver_lane, 'FF_ASSEMBLED_DERIVED' AS cogs_origin_type,
       CONCAT('BUNDLE:', internal_sku, ':', FORMAT_DATE('%Y%m%d', effective_from)) AS resolver_ref,
       TRUE AS is_reconstructed, 'DERIVED_FROM_COMPONENTS' AS confidence, FALSE AS owner_confirmed
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`;

-- ============================================================================
-- 5. ИСПОЛНЯЕМЫЕ ASSERT — fail-closed. Прогоняются после загрузки.
--    Блок A — logical row keys. BigQuery НЕ обеспечивает PRIMARY KEY физически,
--    поэтому уникальность гарантируется здесь, а не декларацией.
-- ============================================================================
ASSERT (SELECT COUNTIF(cogs_history_id IS NULL) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'A-1 FAIL: cogs_history_id содержит NULL';
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT cogs_history_id) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`)
  AS 'A-2 FAIL: cogs_history_id не уникален';
ASSERT (SELECT COUNTIF(bundle_component_id IS NULL) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 0
  AS 'A-3 FAIL: bundle_component_id содержит NULL';
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT bundle_component_id) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`)
  AS 'A-4 FAIL: bundle_component_id не уникален';
ASSERT (SELECT COUNT(*) FROM (SELECT internal_sku, effective_from FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` GROUP BY 1,2 HAVING COUNT(*)>1)) = 0
  AS 'A-5 FAIL: нарушена уникальность (internal_sku, effective_from)';
ASSERT (SELECT COUNT(*) FROM (SELECT bundle_internal_sku, component_internal_sku, effective_from FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` GROUP BY 1,2,3 HAVING COUNT(*)>1)) = 0
  AS 'A-6 FAIL: нарушена уникальность component-definition rows';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 17
  AS 'A-7 FAIL: REF_SKU_COGS_HISTORY не равен 17 строкам';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 33
  AS 'A-8a FAIL: REF_BUNDLE_COMPONENTS не равен 33 строкам';
ASSERT (SELECT COUNT(DISTINCT bundle_internal_sku) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 14
  AS 'A-8b FAIL: наборов не 14';

-- Блок B — значения, типы, отсутствие запрещённых колонок
ASSERT (SELECT COUNTIF(product_cogs_rub IS NULL OR product_cogs_rub <= 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'B-1 FAIL: product_cogs_rub NULL или не положителен';
ASSERT (SELECT COUNTIF(cogs_origin_type NOT IN ('PURCHASE_BATCH','INVENTORY_TRANSFORMATION','IMPORTED_FINISHED_SET')) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'B-2 FAIL: недопустимый cogs_origin_type (FF_ASSEMBLED_DERIVED в этой таблице запрещён)';
ASSERT (SELECT COUNTIF(effective_to IS NOT NULL AND effective_to < effective_from) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'B-3a FAIL: effective_to < effective_from в COGS history';
ASSERT (SELECT COUNTIF(effective_to IS NOT NULL AND effective_to < effective_from) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 0
  AS 'B-3b FAIL: effective_to < effective_from в bundle components';
ASSERT (SELECT COUNTIF(component_qty <= 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 0
  AS 'B-4 FAIL: component_qty не положителен';
ASSERT (SELECT COUNTIF(LOWER(column_name) IN ('wb_nm_id','nm_id','channel_sku_id','marketplace'))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='REF_SKU_COGS_HISTORY') = 0
  AS 'B-5 FAIL: в REF_SKU_COGS_HISTORY появился идентификатор канала';
ASSERT (SELECT COUNTIF(LOWER(column_name) IN ('component_cost','bundle_total_cost','bundle_build_cost','product_cogs_rub'))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='REF_BUNDLE_COMPONENTS') = 0
  AS 'B-6 FAIL: в REF_BUNDLE_COMPONENTS появилась стоимостная колонка';

-- R4 — сентинел открытого интервала не протекает в данные
ASSERT (SELECT COUNTIF(effective_to = DATE '9999-12-31' OR effective_from = DATE '9999-12-31') FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'R4a FAIL: сентинел 9999-12-31 попал в COGS history';
ASSERT (SELECT COUNTIF(effective_to = DATE '9999-12-31' OR effective_from = DATE '9999-12-31') FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 0
  AS 'R4b FAIL: сентинел 9999-12-31 попал в bundle components';
ASSERT (SELECT COUNTIF(effective_to = DATE '9999-12-31') FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 0
  AS 'R4c FAIL: сентинел 9999-12-31 протёк наружу через resolver';

-- Блок C — boundary lineage (исправленный I-7 из Stage 3.0.3B)
ASSERT (SELECT COUNTIF(effective_from_is_reconstructed AND (effective_from_window_from IS NULL OR effective_from_window_to IS NULL
        OR NOT (effective_from_window_from <= effective_from AND effective_from <= effective_from_window_to)))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'C-1 FAIL: from-окно отсутствует или не содержит effective_from';
ASSERT (SELECT COUNTIF(effective_to_is_reconstructed AND (effective_to_window_from IS NULL OR effective_to_window_to IS NULL
        OR effective_to IS NULL OR NOT (effective_to_window_from <= effective_to AND effective_to <= effective_to_window_to)))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'C-2 FAIL: to-окно отсутствует или не содержит effective_to';
ASSERT (SELECT COUNTIF(NOT effective_from_is_reconstructed AND (effective_from_window_from IS NOT NULL OR effective_from_window_to IS NOT NULL OR effective_from_transition_id IS NOT NULL))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'C-3 FAIL: у не-реконструированной начальной границы заполнено окно';
ASSERT (SELECT COUNTIF(NOT effective_to_is_reconstructed AND (effective_to_window_from IS NOT NULL OR effective_to_window_to IS NOT NULL OR effective_to_transition_id IS NOT NULL))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'C-4 FAIL: у не-реконструированной конечной границы заполнено окно';
ASSERT (SELECT COUNTIF(is_reconstructed <> (effective_from_is_reconstructed OR effective_to_is_reconstructed))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) = 0
  AS 'C-5 FAIL: is_reconstructed не равен OR по двум концам';
ASSERT (SELECT COUNTIF( e.effective_to_window_from <> DATE_SUB(l.effective_from_window_from, INTERVAL 1 DAY)
                     OR e.effective_to_window_to   <> DATE_SUB(l.effective_from_window_to,   INTERVAL 1 DAY)
                     OR e.effective_to_transition_id <> l.effective_from_transition_id )
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` e
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` l
          ON l.internal_sku = e.internal_sku AND l.effective_from = DATE_ADD(e.effective_to, INTERVAL 1 DAY)
        WHERE e.effective_to_is_reconstructed AND l.effective_from_is_reconstructed) = 0
  AS 'C-6 FAIL: окна смежных строк рассогласованы';
ASSERT (SELECT COUNTIF(effective_from_is_reconstructed AND (effective_from_window_from IS NULL OR effective_from_window_to IS NULL
        OR NOT (effective_from_window_from <= effective_from AND effective_from <= effective_from_window_to)))
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS`) = 0
  AS 'C-7 FAIL: bundle components — from-окно не содержит effective_from';

-- Блок D — temporal. НЕПРЕРЫВНОЕ КАЛЕНДАРНОЕ ПОКРЫТИЕ НЕ ТРЕБУЕТСЯ:
--   даты без событий вне интервалов допустимы (выбывший SKU), UNKNOWN != 0.
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` a
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` b
          ON a.internal_sku = b.internal_sku AND a.cogs_history_id <> b.cogs_history_id
         AND a.effective_from <= COALESCE(b.effective_to, DATE '9999-12-31')
         AND b.effective_from <= COALESCE(a.effective_to, DATE '9999-12-31')) = 0
  AS 'D-1 FAIL: пересекающиеся интервалы у одного internal_sku';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` a
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` b
          ON a.bundle_internal_sku = b.bundle_internal_sku AND a.component_internal_sku = b.component_internal_sku
         AND a.bundle_component_id <> b.bundle_component_id
         AND a.effective_from <= COALESCE(b.effective_to, DATE '9999-12-31')
         AND b.effective_from <= COALESCE(a.effective_to, DATE '9999-12-31')) = 0
  AS 'D-2 FAIL: пересекающиеся определения состава';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY` h
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` c
          ON c.bundle_internal_sku = h.internal_sku
        WHERE h.cogs_origin_type = 'IMPORTED_FINISHED_SET'
          AND h.effective_from <= COALESCE(c.effective_to, DATE '9999-12-31')
          AND c.effective_from <= COALESCE(h.effective_to, DATE '9999-12-31')) = 0
  AS 'D-3 FAIL: режимы IMPORTED_FINISHED_SET и FF_ASSEMBLED пересекаются по времени';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` c
        LEFT JOIN (SELECT DISTINCT internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_COGS_HISTORY`) h
          ON h.internal_sku = c.component_internal_sku
        WHERE h.internal_sku IS NULL) = 0
  AS 'D-4 FAIL: компонент отсутствует в REF_SKU_COGS_HISTORY';

-- E-4 + контроль состава resolver
ASSERT (SELECT COUNTIF(product_cogs_rub IS NULL) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 0
  AS 'E-4 FAIL: в resolver попала строка с NULL-стоимостью';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`) = 21
  AS 'R5a FAIL: производных интервалов не 21';
ASSERT (SELECT COUNT(DISTINCT internal_sku) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_BUNDLE_COGS_DERIVED`) = 14
  AS 'R5b FAIL: derived-вью потеряла набор';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`) = 38
  AS 'R5c FAIL: unified resolver не равен 38 интервалам';

SELECT 'STAGE 3.1A — ASSERT A/B/C/D/E-4/R4/R5 PASS' AS result;
