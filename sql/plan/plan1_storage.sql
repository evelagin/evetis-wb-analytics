-- ============================================================================
-- PR-PLAN-1 · хранилище планирования (evetis_ref). Контракт:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md
--
-- ЧТО ЭТО. Версионированный план продаж и реестр поступлений — только добавление строк:
--   PLAN_VERSION        шапка версии: вид (MODEL_SCENARIO | SYSTEM_PROPOSED | OWNER_AUTHORED),
--                       горизонт, метод, базис данных, content_sha256 содержимого;
--   PLAN_LINE_MONTHLY   строки версии: месяц × площадка × карточка → planned_cards;
--   PLAN_ASSUMPTION     допущения версии с классом доказательности и источником;
--   PLAN_BOM_BASIS      снимок BOM, с которым версия создана (утверждённая история не меняется
--                       при правке REF_BUNDLE_COMPONENTS);
--   INBOUND_LOT_EVENT   события партий поступления (состояние, блокер, ETA, приёмка).
-- Реестр событий плана — существующий evetis_ref.REF_SALES_PLAN_APPROVAL (PR-PROMO-4),
-- расширенный двумя колонками: второго реестра утверждений нет.
--
-- ПИШУТ только процедуры sql/plan/plan1_procedures.sql, запускаемые вручную (без расписаний
-- и сервисных аккаунтов). Изменение содержимого = новая версия. UPDATE/DELETE не предусмотрены;
-- их следы ловит DQ (пересчёт content_sha256 против шапки и утверждений).
-- Legacy SET_2026-09-09 строк не копирует: регистрируется шапкой, строки читаются из
-- CT_SEASON_PLAN_MONTHLY представлением, хеш защищает их от незаметной правки.
--
-- CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS: повторный прогон ничего не меняет.
-- Откат — sql/plan/plan1_rollback.sql (таблицы удаляются только пустыми).
-- ============================================================================

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
(
  plan_version            STRING    NOT NULL OPTIONS(description="Ключ версии. Legacy — ключ CT_PLAN_VERSION"),
  plan_kind               STRING    NOT NULL OPTIONS(description="MODEL_SCENARIO | SYSTEM_PROPOSED | OWNER_AUTHORED"),
  plan_name               STRING    NOT NULL,
  method_id               STRING             OPTIONS(description="OBSERVED_RUN_RATE_30D_V1 | LEGACY_CT_MODEL | OWNER_INPUT"),
  method_version          STRING,
  parent_plan_version     STRING             OPTIONS(description="Версия-источник (для OWNER_AUTHORED на основе другой)"),
  source_kind             STRING    NOT NULL OPTIONS(description="PLAN_LINE_MONTHLY | LEGACY_CT_SEASON_PLAN_MONTHLY"),
  source_ref              STRING             OPTIONS(description="Для legacy — plan_version в CT_SEASON_PLAN_MONTHLY"),
  horizon_from            DATE      NOT NULL,
  horizon_to              DATE      NOT NULL,
  marketplaces            STRING    NOT NULL OPTIONS(description="Через запятую: WB,OZON"),
  basis_sales_as_of       DATE               OPTIONS(description="Последний полный день продаж, на котором построена версия"),
  basis_inventory_as_of   DATE,
  basis_inventory_freshness STRING           OPTIONS(description="FRESH | STALE на момент создания"),
  line_count              INT64     NOT NULL,
  bom_basis_rows          INT64     NOT NULL,
  content_sha256          STRING    NOT NULL OPTIONS(description="SHA-256 строк плана и базиса BOM (канон — V_PLAN_VERSION_STATUS)"),
  created_at              TIMESTAMP NOT NULL,
  created_by              STRING    NOT NULL,
  notes                   STRING
)
OPTIONS(description="PR-PLAN-1: шапки версий плана продаж. Только добавление; изменение содержимого = новая версия.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_LINE_MONTHLY`
(
  plan_version   STRING    NOT NULL,
  month          DATE      NOT NULL OPTIONS(description="Первое число календарного месяца"),
  marketplace    STRING    NOT NULL OPTIONS(description="WB | OZON"),
  internal_sku   STRING    NOT NULL OPTIONS(description="Продаваемая карточка: одиночный SKU или набор"),
  sales_mode     STRING    NOT NULL OPTIONS(description="SOLO | BUNDLE"),
  planned_cards  NUMERIC   NOT NULL OPTIONS(description="Карточки заказа без отмен за весь календарный месяц"),
  line_basis     STRING             OPTIONS(description="Как получено значение строки"),
  created_at     TIMESTAMP NOT NULL
)
OPTIONS(description="PR-PLAN-1: строки плана. Грейн plan_version × month × marketplace × internal_sku. Только добавление.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_ASSUMPTION`
(
  plan_version      STRING    NOT NULL,
  assumption_id     STRING    NOT NULL,
  assumption_type   STRING    NOT NULL OPTIONS(description="METHOD | OBSERVED_RATE | SEASONALITY | CALENDAR | CHANNEL_SCOPE | INBOUND | LEGACY_MULTIPLIER | PROGRAM | ..."),
  scope_marketplace STRING,
  scope_sku         STRING,
  scope_month       DATE,
  value_numeric     NUMERIC,
  value_text        STRING,
  unit              STRING,
  evidence_class    STRING    NOT NULL OPTIONS(description="FACT | INFERENCE | ASSUMPTION | OWNER_DECISION | LEGACY_MODEL"),
  source            STRING    NOT NULL,
  created_at        TIMESTAMP NOT NULL
)
OPTIONS(description="PR-PLAN-1: допущения версии плана с классом доказательности. Только добавление.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS`
(
  plan_version     STRING    NOT NULL,
  bundle_sku       STRING    NOT NULL,
  component_sku    STRING    NOT NULL,
  component_qty    INT64     NOT NULL,
  source_row_id    STRING             OPTIONS(description="REF_BUNDLE_COMPONENTS.bundle_component_id"),
  captured_at      TIMESTAMP NOT NULL
)
OPTIONS(description="PR-PLAN-1: снимок BOM на момент создания версии. Физический смысл утверждённой версии не меняется при правке REF_BUNDLE_COMPONENTS.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.INBOUND_LOT_EVENT`
(
  inbound_id        STRING    NOT NULL OPTIONS(description="Стабильный ключ партии; новое событие = новая строка"),
  internal_sku      STRING    NOT NULL OPTIONS(description="Физический SKU"),
  quantity          INT64     NOT NULL OPTIONS(description="Количество единиц в партии"),
  lot_state         STRING    NOT NULL OPTIONS(description="PLANNED | HYPOTHETICAL | ORDER_CONFIRMED | IN_PRODUCTION | PRODUCED | READY_FOR_SHIPMENT | IN_TRANSIT | RECEIVED | CANCELLED"),
  blocker           STRING             OPTIONS(description="Например AWAITING_PAYMENT; NULL — нет"),
  eta_date          DATE,
  eta_status        STRING    NOT NULL OPTIONS(description="CONFIRMED | ESTIMATED | UNKNOWN"),
  received_date     DATE,
  received_quantity INT64,
  expiry_batch_id   STRING             OPTIONS(description="evetis_ref.CT_EXPIRY_BATCH.batch_id"),
  evidence_class    STRING    NOT NULL OPTIONS(description="OWNER_FACT | DOCUMENT | SUPPLIER_CONFIRMATION | HYPOTHESIS"),
  evidence_ref      STRING    NOT NULL,
  recorded_at       TIMESTAMP NOT NULL,
  recorded_by       STRING    NOT NULL,
  note              STRING
)
OPTIONS(description="PR-PLAN-1: события партий поступления. Действует последнее событие inbound_id. Только добавление.");

-- Реестр событий плана (PR-PROMO-4) расширяется, а не дублируется.
ALTER TABLE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`
  ADD COLUMN IF NOT EXISTS content_sha256 STRING OPTIONS(description="PR-PLAN-1: content_sha256 версии, к которому относится событие"),
  ADD COLUMN IF NOT EXISTS effective_from_month DATE OPTIONS(description="PR-PLAN-1: для APPROVED — первый месяц действия (не раньше месяца утверждения)");
