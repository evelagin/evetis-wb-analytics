-- ============================================================================
-- EVETIS · Stage B · Operations backend · 02 — настройки и входы посева
-- Идемпотентно (MERGE): повторный прогон не создаёт дублей и не меняет журнал.
-- ============================================================================

-- Настройки. writeback_enabled = 'false' — операции владельца закрыты до Stage E.
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` t
USING (
  SELECT 'writeback_enabled' AS config_key, 'false' AS config_value,
         'Owner operations (reserve / build / cancel / reverse) are refused while false. Stage B: false.' AS note
  UNION ALL SELECT 'max_api_age_hours', '30',
         'API evidence (Ozon supply orders extract, Control Tower marketplace snapshot) older than this is stale -> refuse.'
  UNION ALL SELECT 'ledger_lock', 'init',
         'Writer lock row: every write transaction UPDATEs it first, serializing writers.'
) s ON t.config_key = s.config_key
WHEN NOT MATCHED THEN
  INSERT (config_key, config_value, note, updated_at, updated_by)
  VALUES (s.config_key, s.config_value, s.note, CURRENT_TIMESTAMP(), 'stage-b-deploy');

-- Авторитетный физический срез ФФ на 09.09.2026.
-- Источник: docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1 (ред. 2): «Оперативное = срез ЛК Usend
-- на 09.09 минус состав четырёх поставок Ozon от 03.09, которые уже уехали»; паллеты — там же
-- (крем для лица АКНЕ ≈1 077 после неоформленного добора ~40 ед.). Точность источника ±50 ед.
-- 🔴 Это НЕ CT_STOCK_SNAPSHOT: там полка 610 = 801 − 191 — доля поставок Ozon 07.09, которую
--    сид Control Tower считал уехавшей. Физически 07.09-поставки на 09.09 не уехали (тот же
--    документ §2 и письмо ФФ о нехватке 123 ед.). Доказательство — V_CT_SEED_RECON.
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT` t
USING (
  SELECT * FROM UNNEST([
    STRUCT(DATE '2026-09-09' AS snapshot_date, 'EVT-FS-MOIST-30'   AS internal_sku, 292 AS shelf_units, 3600 AS pallet_units, TRUE AS in_ledger),
    STRUCT(DATE '2026-09-09', 'EVT-HC-AMBER-300',  109, 4400, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-HC-CHERRY-300', 109, 4320, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-EP-ENZYME-75',  107, 3630, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-FT-ACNE-150',    76, 3600, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-FT-MOIST-150',   67, 3600, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-FS-ACNE-30',     23, 5200, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-HC-HAND-300',    16, 2720, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-FC-MOIST-50',     2,    0, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-FC-ACNE-50',      0, 1077, TRUE),
    STRUCT(DATE '2026-09-09', 'EVT-HC-BODY-300',     0,    0, TRUE),
    -- «Плюс ~11 ед в прочих позициях ЛК (16 позиций всего)» — не SKU каталога, не опознаны.
    -- Задокументированы, в журнал не идут: у них нет internal_sku, баланс которого они могли бы составить.
    STRUCT(DATE '2026-09-09', 'NON_CATALOG_LK',     11,    0, FALSE)
  ])
) s ON t.snapshot_date = s.snapshot_date AND t.internal_sku = s.internal_sku
WHEN NOT MATCHED THEN
  INSERT (snapshot_date, internal_sku, shelf_units, pallet_units, in_ledger, source, source_ref, precision_note, loaded_at)
  VALUES (s.snapshot_date, s.internal_sku, s.shelf_units, s.pallet_units, s.in_ledger,
          'FF_OPERATIONAL_STOCK_2026-09-09', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1',
          IF(s.in_ledger, 'LK Usend 09.09, accuracy ±50 units', 'non-catalog LK positions, ~11 units, unidentified'),
          CURRENT_TIMESTAMP());

-- Физический статус заказов Ozon, открытых на 09.09.
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` t
USING (
  SELECT * FROM UNNEST([
    -- Пять поставок от 07.09: на 09.09 НЕ уехали — документ разбирает их исполнимость, ФФ пишет о
    -- нехватке 41 + 43 + 39 = 123 ед. (FF_OPERATIONAL_STOCK_2026-09-09.md §2, «Дефицит по трём позициям»).
    STRUCT('2000065756060' AS order_number, 'ON_FF' AS ff_status_at_opening, 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §2 (Воронеж)' AS source_ref),
    STRUCT('2000065790707', 'ON_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §2 (Красноярск)'),
    STRUCT('2000065740382', 'ON_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §2 (Казань)'),
    STRUCT('2000065751264', 'ON_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §2 (СПб и СЗО)'),
    STRUCT('2000065804615', 'ON_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §2 (Новосибирск)'),
    -- Четыре неотменённые поставки от 03.09: на 09.09 уже уехали — «IN_TRANSIT, 178 ед в компонентах»
    -- (там же §1). 178 = 60 + 28 + 51 + 39 по BOM — совпадает с документом до единицы.
    STRUCT('2000065337049', 'LEFT_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1 (поставки 03.09, 178 ед.)'),
    STRUCT('2000065342295', 'LEFT_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1 (поставки 03.09, 178 ед.)'),
    STRUCT('2000065375639', 'LEFT_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1 (поставки 03.09, 178 ед.)'),
    STRUCT('2000065378095', 'LEFT_FF', 'docs/FF_OPERATIONAL_STOCK_2026-09-09.md §1 (поставки 03.09, 178 ед.)')
  ])
) s ON t.order_number = s.order_number
WHEN NOT MATCHED THEN
  INSERT (order_number, ff_status_at_opening, source_ref, note, loaded_at)
  VALUES (s.order_number, s.ff_status_at_opening, s.source_ref, NULL, CURRENT_TIMESTAMP());
