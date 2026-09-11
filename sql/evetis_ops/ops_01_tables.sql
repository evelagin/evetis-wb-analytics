-- ============================================================================
-- EVETIS · Stage B · Operations backend · 01 — таблицы
-- Датасет evetis_ops (Terraform: infra/terraform/evetis_ops.tf).
-- Архитектура: docs/control_tower/ARCHITECTURE_DELTA_V1_2026-09-11.md
-- Протокол: docs/ops/STAGE_B_OPS_BACKEND_2026-09-11.md
--
-- ПРИНЦИПЫ
--   * Всё количество в журнале — ФИЗИЧЕСКИЕ единицы (флакон / банка / туба).
--     Карточки, наборы, заказы — единицы продажи; они раскладываются по BOM
--     до того, как коснутся остатков, и никогда не складываются с ними напрямую.
--   * Журнал только дополняется. Исправление — сторно (reversal_of), не UPDATE.
--   * Остатки площадок (в пути, на складе WB/Ozon) — производные от API и в журнал
--     ФФ не пишутся.
--   * Писать в журнал можно только процедурами sp_ops_* (04_procedures).
-- Все CREATE — IF NOT EXISTS: повторный прогон файла ничего не пересоздаёт.
-- ============================================================================

-- Настройки контура. Одна строка = один ключ.
--   writeback_enabled  — 'false': операции владельца (резерв, сборка, отмена, сторно) запрещены.
--   max_api_age_hours  — старше этого возраста данные API считаются устаревшими → отказ.
--   ledger_lock        — строка-замок: каждая пишущая транзакция начинается с UPDATE этой строки,
--                        поэтому две транзакции записи в журнал не идут параллельно.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` (
  config_key   STRING    NOT NULL,
  config_value STRING,
  note         STRING,
  updated_at   TIMESTAMP NOT NULL,
  updated_by   STRING
) OPTIONS (description = 'Operations backend configuration: write-back switch, API staleness threshold, writer lock row.');

-- Настройки логистики по физическому SKU и каналу, версионные.
-- Версия действует с effective_from по effective_to включительно; effective_to IS NULL — действует сейчас.
-- Перекрытие версий одного (SKU, канал) недопустимо — проверяется инвариантом, а запись
-- новой версии (Stage E) закрывает предыдущую.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` (
  internal_sku        STRING  NOT NULL,   -- физический SKU (компонент), не набор
  channel             STRING  NOT NULL,   -- WB | OZON | B2B | FBS
  units_per_box       INT64,
  unit_weight_kg      NUMERIC,
  box_weight_kg       NUMERIC,
  min_shipment_units  INT64,
  shipment_multiple   INT64,
  target_cover_days   INT64,
  safety_stock_days   INT64,
  lead_time_days      INT64,
  fbs_reserve_units   INT64,
  max_lot_weight_kg   NUMERIC,            -- лимит лота канала (WB ПВЗ: 25 кг), если есть
  effective_from      DATE    NOT NULL,
  effective_to        DATE,
  source              STRING  NOT NULL,
  note                STRING,
  created_at          TIMESTAMP NOT NULL,
  created_by          STRING
) OPTIONS (description = 'Versioned logistics settings per physical SKU x channel. Empty until the owner provides values (Stage C/E); no values are inferred.');

-- Авторитетный физический срез ФФ на дату открытия журнала — ВХОД посева.
-- in_ledger = FALSE: количество задокументировано, но в журнал не идёт (нет SKU каталога).
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT` (
  snapshot_date   DATE    NOT NULL,
  internal_sku    STRING  NOT NULL,
  shelf_units     INT64   NOT NULL,       -- оперативное хранение (полка)
  pallet_units    INT64   NOT NULL,       -- длительное хранение (паллеты)
  in_ledger       BOOL    NOT NULL,
  source          STRING  NOT NULL,
  source_ref      STRING,
  precision_note  STRING,
  loaded_at       TIMESTAMP NOT NULL
) OPTIONS (description = 'Authoritative physical FF snapshot used to open the ledger (seed input). Physical units only.');

-- Доказательство физического статуса заказов Ozon на дату открытия — ВХОД посева.
-- Статус API READY_TO_SUPPLY по умолчанию = резерв на ФФ; подтверждённый ФФ выезд сильнее.
-- Посев отказывает, если открытый на дату заказ не имеет строки здесь.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` (
  order_number          STRING NOT NULL,   -- номер поставки Ozon
  ff_status_at_opening  STRING NOT NULL,   -- ON_FF | LEFT_FF
  source_ref            STRING NOT NULL,
  note                  STRING,
  loaded_at             TIMESTAMP NOT NULL
) OPTIONS (description = 'Physical FF status of Ozon supply orders at ledger opening, with documentary source. Seed fails closed on any open order without evidence.');

-- ЖУРНАЛ ДВИЖЕНИЙ ФИЗИЧЕСКОГО ЗАПАСА. Только добавление.
-- Движение переносит qty единиц internal_sku из бакета from_* в бакет to_*.
-- Бакет = (state, location, channel, container, doc). from_state IS NULL — приход извне
-- (посев / приёмка); to_state IS NULL — выбытие наружу (только сторно прихода).
-- Состояния ФФ: AVAILABLE, RESERVED, ASSEMBLED, FBS_READY. Вне ФФ: SHIPPED, WRITTEN_OFF.
-- container — SKU набора, если единица физически внутри собранного набора.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` (
  movement_id    STRING    NOT NULL,     -- '<request_id>#<seq>'
  request_id     STRING    NOT NULL,
  seq            INT64     NOT NULL,
  event_date     DATE      NOT NULL,     -- дата физического события
  recorded_at    TIMESTAMP NOT NULL,
  internal_sku   STRING    NOT NULL,     -- физический SKU (компонент)
  qty            INT64     NOT NULL,     -- > 0, физические единицы
  from_state     STRING,
  from_location  STRING,
  from_channel   STRING,
  from_container STRING,
  from_doc       STRING,
  to_state       STRING,
  to_location    STRING,
  to_channel     STRING,
  to_container   STRING,
  to_doc         STRING,
  doc_type       STRING    NOT NULL,     -- SEED | SHIPMENT_RESERVE | BUILD | REVERSAL | ...
  doc_id         STRING,
  reversal_of    STRING,                 -- movement_id сторнируемого движения
  source         STRING    NOT NULL,
  changed_by     STRING,
  note           STRING
) CLUSTER BY internal_sku
OPTIONS (description = 'Append-only ledger of PHYSICAL FF stock movements. Written only by evetis_ops.sp_ops_* procedures. Corrections are reversals.');

-- Отгрузки: журнал событий статуса (только добавление); текущий статус — V_CT_SHIPMENT_CURRENT.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT` (
  shipment_id           STRING    NOT NULL,
  event_seq             INT64     NOT NULL,
  status                STRING    NOT NULL,   -- RESERVED | ASSEMBLED | SHIPPED | CANCELLED | CLOSED
  channel               STRING    NOT NULL,   -- WB | OZON | B2B
  counterparty          STRING,
  destination           STRING,
  marketplace_order_id  STRING,
  marketplace_ref       STRING,               -- номер поставки WB / Ozon
  planned_date          DATE,
  event_at              TIMESTAMP NOT NULL,
  request_id            STRING    NOT NULL,
  source                STRING    NOT NULL,
  changed_by            STRING,
  note                  STRING
) OPTIONS (description = 'Shipment status events (append-only). IN_TRANSIT / ACCEPTED are derived from API, never stored.');

-- Строки отгрузки в единицах продажи (карточках) + их физическое разложение на момент создания.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE` (
  shipment_id     STRING    NOT NULL,
  line_no         INT64     NOT NULL,
  card_sku        STRING    NOT NULL,   -- соло-SKU или SKU набора
  marketplace_sku STRING,
  qty_cards       INT64     NOT NULL,   -- единицы продажи
  is_bundle       BOOL      NOT NULL,
  physical_units  INT64     NOT NULL,   -- qty_cards × Σ component_qty
  created_at      TIMESTAMP NOT NULL,
  request_id      STRING    NOT NULL
) OPTIONS (description = 'Shipment lines in selling units with their BOM-expanded physical units. Immutable.');

-- Сборки наборов: события (только добавление).
--   FBO_SHIPMENT — набор собирается под конкретную отгрузку (компоненты RESERVED → ASSEMBLED);
--   FBS_STOCK    — набор собирается в запас FBS (AVAILABLE → FBS_READY).
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD` (
  build_id              STRING    NOT NULL,
  event                 STRING    NOT NULL,   -- BUILD | REVERSAL
  bundle_sku            STRING    NOT NULL,
  qty_bundles           INT64     NOT NULL,
  purpose               STRING    NOT NULL,   -- FBO_SHIPMENT | FBS_STOCK
  shipment_id           STRING,
  channel               STRING,
  reversal_of_build_id  STRING,
  event_at              TIMESTAMP NOT NULL,
  request_id            STRING    NOT NULL,
  source                STRING    NOT NULL,
  changed_by            STRING,
  note                  STRING
) OPTIONS (description = 'Bundle build events. FBO builds are tied to a shipment; FBS builds are standing FBS-ready stock.');

-- Журнал запросов записи: идемпотентность по request_id и аудит отказов.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG` (
  request_id      STRING    NOT NULL,
  procedure_name  STRING    NOT NULL,
  status          STRING    NOT NULL,   -- OK | REJECTED
  message         STRING,
  doc_id          STRING,
  movements       INT64,
  payload         STRING,
  logged_at       TIMESTAMP NOT NULL,
  source          STRING,
  changed_by      STRING
) OPTIONS (description = 'Write-request log: OK rows make requests idempotent by request_id; REJECTED rows audit fail-closed refusals.');
