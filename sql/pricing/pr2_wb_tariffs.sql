-- ============================================================================
-- PR-2 Phase B — приём тарифов WB (READ-ONLY) и конфигурация Конструктора.
--
-- Источник: GET https://common-api.wildberries.ru/api/v1/tariffs/{commission,box,return,pallet}
-- Каденс: 1 раз в сутки. Ставки меняются реже раза в месяц; 20-минутный опрос,
-- как у наблюдателя цен, был бы расходом без информации.
--
-- ЖЁСТКИЕ СВОЙСТВА:
--   1. Append-only. Ответ на вопрос «какой тариф система считала действующим
--      на дату T» невозможен без истории наблюдений.
--   2. Длинный формат (одна строка на метрику). Новое поле тарифа не ломает
--      схему и не теряется молча — оно просто появляется новой метрикой.
--   3. Сентинелы WB (`-`, `не принимает`) НИКОГДА не превращаются в 0.
--      value_num = NULL, value_raw хранит исходную строку, is_parsed = FALSE.
--
-- Откат: sql/pricing/pr2_wb_tariffs_rollback.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_TARIFFS`
(
  observed_at        TIMESTAMP NOT NULL OPTIONS(description="Момент наблюдения (UTC)."),
  observation_date   DATE      NOT NULL OPTIONS(description="Логический период = сутки UTC. Он же logical_period в LOADER_RUNS."),
  observation_id     STRING    NOT NULL OPTIONS(description="Детерминированный id снимка = f(environment, дата)."),
  environment        STRING    NOT NULL,
  run_id             STRING,

  tariff_kind        STRING    NOT NULL OPTIONS(description="COMMISSION | BOX | RETURN | PALLET"),
  entity_key         STRING    OPTIONS(description="subjectID для COMMISSION; warehouseName для складских тарифов."),
  entity_name        STRING    OPTIONS(description="subjectName / warehouseName как отдаёт WB."),
  parent_key         STRING    OPTIONS(description="parentID для COMMISSION; geoName для складских."),
  parent_name        STRING,

  metric             STRING    NOT NULL OPTIONS(description="Имя поля тарифа как в ответе WB: kgvpMarketplace, paidStorageKgvp, boxDeliveryBase, …"),
  value_num          NUMERIC   OPTIONS(description="Разобранное число. NULL = WB не дал значения. НИКОГДА не 0-подстановка."),
  value_raw          STRING    OPTIONS(description="Исходная строка WB: '62,1', '-', 'не принимает'. Форензика."),
  is_parsed          BOOL      OPTIONS(description="FALSE = сентинел или неразбираемое значение; экономика обязана это учитывать."),

  effective_next     DATE      OPTIONS(description="dtNextBox / dtNextPallet — дата вступления следующего тарифа."),
  effective_till_max DATE      OPTIONS(description="dtTillMax — предел действия текущего тарифа."),

  source_endpoint    STRING,
  raw_row_json       STRING    OPTIONS(description="Строка ответа WB дословно."),
  ingested_at        TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY tariff_kind, entity_key, metric
OPTIONS(description="PR-2. Append-only наблюдения тарифов WB. Длинный формат: одна строка на метрику. Сентинелы не приводятся к нулю.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_TARIFF_OBSERVATIONS`
(
  observation_id   STRING    NOT NULL,
  observation_date DATE      NOT NULL,
  environment      STRING    NOT NULL,
  run_id           STRING,
  started_at       TIMESTAMP NOT NULL,
  observed_at      TIMESTAMP,
  completed_at     TIMESTAMP,
  status           STRING    NOT NULL OPTIONS(description="STARTED | COMPLETE | ERROR | REUSED"),
  kinds_requested  STRING,
  kinds_ok         STRING,
  kinds_failed     STRING    OPTIONS(description="Виды тарифов, которые не удалось получить. Пустой ≠ NULL."),
  rows_written     INT64,
  http_status      INT64,
  schema_status    STRING    OPTIONS(description="OK | DRIFT_NEW_FIELDS | DRIFT_MISSING_FIELDS"),
  schema_unknown_fields STRING,
  error_code       STRING,
  error_message    STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY environment, observation_date
OPTIONS(description="PR-2. Манифест наблюдений тарифов WB: покрытие видов, дрейф схемы, ошибки.");

-- ── Конфигурация Конструктора тарифов ───────────────────────────────────────
-- Официальный машиночитаемый источник существует
-- (GET /api/common/v1/tariff-constructor/options), но недоступен ни одному
-- текущему токену EVETIS: 403 «token does not satisfy additional requirements».
-- Проверено на WB_PRICES_READ_TOKEN, WB_TOKEN_ANALYTICS, EVETIS_WB_API_TOKEN.
--
-- Поэтому надбавка живёт здесь как effective-dated конфигурация, а НЕ константой
-- в SQL экономики. Смена 0.75 → 0 выполняется вставкой новой строки с новой датой;
-- формулы репрайсера при этом не переписываются.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_MARKETPLACE_COMMISSION_COMPONENT`
(
  marketplace     STRING  NOT NULL OPTIONS(description="WB | OZON"),
  component       STRING  NOT NULL OPTIONS(description="TARIFF_CONSTRUCTOR | иная надбавка/скидка к базовой комиссии"),
  component_name  STRING  OPTIONS(description="Человекочитаемое название опции кабинета."),
  addon_pct       NUMERIC NOT NULL OPTIONS(description="Надбавка в процентных пунктах к базовой комиссии. 0 = опция отключена."),
  effective_from  DATE    NOT NULL,
  effective_to    DATE    OPTIONS(description="NULL = действует сейчас."),
  source          STRING  NOT NULL OPTIONS(description="OWNER_VERIFIED | WB_API | RECONCILED_FROM_SETTLEMENT"),
  confidence      STRING  NOT NULL OPTIONS(description="HIGH | MEDIUM | LOW"),
  note            STRING,
  updated_at      TIMESTAMP NOT NULL,
  updated_by      STRING
)
CLUSTER BY marketplace, component
OPTIONS(description="PR-2. Компоненты эффективной комиссии сверх базового тарифа маркетплейса, с датировкой. Конструктор тарифов WB — динамическая коммерческая конфигурация кабинета, а не константа.");
