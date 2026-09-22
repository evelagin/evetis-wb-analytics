-- ============================================================================
-- PR-PROMO-1 — Marketplace Promotion Observation Layer (READ-ONLY)
--
-- Назначение: append-only история наблюдений состояния АКЦИЙ маркетплейсов.
-- Слой наблюдения, а не интерпретации: здесь нет ни одного вывода, ни одной
-- рекомендации, ни одной экономической величины.
--
-- Документы: docs/promotions/PR_PROMO_1_IMPLEMENTATION_2026-09-22.md
--            docs/promotions/PROMOTION_DATA_MODEL_2026-09-22.md
--            docs/promotions/PROMOTION_PHASE_0_5_DECISION_GATE_2026-09-22.md
--
-- ЖЁСТКИЕ СВОЙСТВА, которые нельзя нарушать при доработках:
--   1. ТОЛЬКО append. Ни MERGE по дате, ни UPDATE. Схлопывание наблюдений в
--      сутки уничтожает предмет наблюдения: акция меняет состав и цены внутри
--      суток, и четыре снимка в день обязаны остаться четырьмя строками.
--   2. Наблюдение, в котором ничего не изменилось, обязано сохраниться.
--      Отсутствие строки означает «наблюдатель не отработал», а не «не менялось».
--   3. Кандидат и автодобавление — РАЗНЫЕ состояния, а не один булев флаг.
--      «Площадка МОЖЕТ добавить» и «площадка ДОБАВИТ» различаются на 41 % вклада
--      на единицу (замер 2026-09-22, акция Ozon 4253043). Разведены колонками
--      membership и list_kind, и схлопывать их запрещено.
--   4. Уровень акции и уровень SKU не смешиваются. Для автоакций WB состав по
--      SKU недоступен (метод /nomenclatures отвечает 422 по контракту площадки).
--      Агрегат остаётся агрегатом: inPromoActionTotal хранится как факт уровня
--      акции и НИКОГДА не размазывается по товарам.
--   5. NULL — валидное «источник не дал». 0 — валидное значение. Смешивать
--      их запрещено: подстановка нуля вместо NULL исказит будущую экономику.
--   6. Семантика value в marketing_actions.actions[] РАЗНОРОДНА (цена в рублях
--      у ценовых акций, число месяцев у рассрочки, 100 у программ «за 1 рубль»).
--      Нормализация запрещена — величина хранится как есть.
--   7. Персональные данные покупателей в этот слой не попадают. Источник
--      /v1/actions/discounts-task/list НЕ загружается (DEFERRED_PRIVACY_SENSITIVE_SOURCE).
--
-- Откат: sql/promotions/pr_promo1_rollback.sql
-- ============================================================================

-- ════════════════════════════════════════════════════════════════════════════
--  WILDBERRIES  —  wb_raw
-- ════════════════════════════════════════════════════════════════════════════

-- ── WB-1. Календарь акций: одна строка на акцию в снимке ────────────────────
-- Грейн: observation_id × promotion_id.
-- Источник: GET /api/v1/calendar/promotions + GET /api/v1/calendar/promotions/details
-- (склеиваются в одну строку: список даёт существование, детали — условия).
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`
(
  observed_at              TIMESTAMP NOT NULL OPTIONS(description="Момент наблюдения (UTC). Один на снимок, одинаков для всех строк."),
  observation_bucket       STRING    NOT NULL OPTIONS(description="Логический период прогона = 6-часовое окно UTC, YYYY-MM-DDTHH:MM. Он же logical_period в LOADER_RUNS."),
  observation_id           STRING    NOT NULL OPTIONS(description="Детерминированный id снимка = f(environment, observation_bucket). Ключ идемпотентности."),
  environment              STRING    NOT NULL OPTIONS(description="shadow | prod"),
  run_id                   STRING    OPTIONS(description="Сквозной id прогона Cloud Run → LOADER_RUNS."),

  promotion_id             INT64     NOT NULL OPTIONS(description="data.promotions[].id — идентификатор акции WB."),
  promotion_name           STRING    OPTIONS(description="data.promotions[].name, дословно."),
  promotion_type           STRING    OPTIONS(description="data.promotions[].type. Наблюдались значения auto и regular. Значение источника, без нормализации."),
  is_auto_promotion        BOOL      OPTIONS(description="promotion_type = 'auto'. Производная удобства; авторитетен promotion_type."),
  starts_at                TIMESTAMP OPTIONS(description="startDateTime."),
  ends_at                  TIMESTAMP OPTIONS(description="endDateTime."),

  details_available        BOOL      OPTIONS(description="FALSE = метод /details вернул 200 и ПУСТОЙ массив для этой акции (наблюдается у завершённых). Отличать от ошибки запроса."),
  description              STRING    OPTIONS(description="details.description — текст условий акции, дословно."),
  advantages_csv           STRING    OPTIONS(description="details.advantages[] через запятую, в порядке источника."),
  in_promo_total           INT64     OPTIONS(description="details.inPromoActionTotal — число НАШИХ товаров В акции. ФАКТ УРОВНЯ АКЦИИ. Не размазывать по SKU."),
  in_promo_leftovers       INT64     OPTIONS(description="details.inPromoActionLeftovers."),
  not_in_promo_total       INT64     OPTIONS(description="details.notInPromoActionTotal — наших товаров ВНЕ акции."),
  not_in_promo_leftovers   INT64     OPTIONS(description="details.notInPromoActionLeftovers."),
  participation_pct        NUMERIC   OPTIONS(description="details.participationPercentage."),
  exception_products_count INT64     OPTIONS(description="details.exceptionProductsCount — товаров, исключённых продавцом из автоакции."),
  ranging_tiers            INT64     OPTIONS(description="Число ступеней в details.ranging[]. Сами ступени — в RAW_WB_PROMO_RANGING."),
  ranging_condition        STRING    OPTIONS(description="ranging[].condition первой ступени. Наблюдались calculateProducts и productsInPromotion. Официального описания различия нет — значение источника."),

  sku_level_data_available BOOL      OPTIONS(description="Доступен ли поимённый состав акции через /nomenclatures. FALSE для автоакций — ограничение контракта WB, а не наш дефект."),
  nomenclature_status      STRING    OPTIONS(description="SKIPPED_AUTO_PROMOTION | FETCHED | UNSUPPORTED_422 | EMPTY | HTTP_ERROR. Почему строк уровня SKU нет или есть."),

  raw_promotion_json       STRING    OPTIONS(description="Элемент списка + элемент деталей, дословно как вернул WB. Форензика и эволюция схемы."),
  source_endpoint          STRING    OPTIONS(description="Endpoint(ы), породившие строку."),
  source_payload_hash      STRING    OPTIONS(description="sha256 от (observation_id, promotion_id) — стабильный ключ строки снимка."),
  ingested_at              TIMESTAMP OPTIONS(description="Момент записи в BigQuery (UTC). Отличается от observed_at при ретрае.")
)
PARTITION BY DATE(observed_at)
CLUSTER BY promotion_id, promotion_type
OPTIONS(
  description="PR-PROMO-1. Append-only наблюдения календаря акций WB. Факты УРОВНЯ АКЦИИ. Поимённый состав акции здесь отсутствует намеренно: для автоакций WB его не отдаёт. Никогда не схлопывать по дате.",
  require_partition_filter=false
);

-- ── WB-2. Лестница бустинга: длинная проекция ranging[] ─────────────────────
-- Грейн: observation_id × promotion_id × tier_ordinal.
-- Отдельная таблица, а не JSON-колонка: лестница — самостоятельная сущность
-- (условие → доля участия → бустинг), по ней будут строиться решения уровня
-- портфеля в PR-PROMO-2. Полный ranging[] дублируется в raw_tier_json.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING`
(
  observed_at         TIMESTAMP NOT NULL,
  observation_bucket  STRING    NOT NULL,
  observation_id      STRING    NOT NULL,
  environment         STRING    NOT NULL,
  run_id              STRING,

  promotion_id        INT64     NOT NULL OPTIONS(description="Акция, которой принадлежит ступень."),
  tier_ordinal        INT64     NOT NULL OPTIONS(description="Позиция ступени в ranging[], начиная с 0. Порядок источника сохраняется."),
  condition           STRING    OPTIONS(description="ranging[].condition — условие ступени, значение источника."),
  participation_rate  NUMERIC   OPTIONS(description="ranging[].participationRate — доля участия, с которой действует ступень, %."),
  boost_pct           NUMERIC   OPTIONS(description="ranging[].boost — бустинг ступени, %. Это заявленный стимул видимости, а не наблюдённый эффект."),

  raw_tier_json       STRING    OPTIONS(description="Элемент ranging[] дословно."),
  source_endpoint     STRING,
  source_payload_hash STRING    OPTIONS(description="sha256 от (observation_id, promotion_id, tier_ordinal)."),
  ingested_at         TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY promotion_id, tier_ordinal
OPTIONS(description="PR-PROMO-1. Append-only лестница бустинга акций WB (ranging[]). Заявленный стимул площадки, не измеренный эффект.");

-- ── WB-3. Состав акции по номенклатурам ─────────────────────────────────────
-- Грейн: observation_id × promotion_id × nm_id × in_action_requested.
-- Источник: GET /api/v1/calendar/promotions/nomenclatures.
--
-- ⚠️ НА МОМЕНТ СОЗДАНИЯ ТАБЛИЦА БУДЕТ ПУСТОЙ. У EVETIS сегодня нет ни одной
-- акции type != 'auto', а для автоакций метод неприменим по официальной
-- документации WB и отвечает 422 (проверено 14 вариантами запроса 2026-09-22).
-- Таблица создаётся именно поэтому: без неё первая же появившаяся regular-акция
-- прошла бы мимо и её плановая цена (planPrice) исчезла бы безвозвратно.
-- Пустота — наблюдаемое состояние площадки, фиксируется в WB_PROMO_OBSERVATIONS.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE`
(
  observed_at            TIMESTAMP NOT NULL,
  observation_bucket     STRING    NOT NULL,
  observation_id         STRING    NOT NULL,
  environment            STRING    NOT NULL,
  run_id                 STRING,

  promotion_id           INT64     NOT NULL,
  in_action_requested    BOOL      NOT NULL OPTIONS(description="Значение параметра inAction в запросе: TRUE — спрашивали участников, FALSE — доступных к участию. Часть грейна: один и тот же nm_id приходит в обоих ответах."),
  nm_id                  INT64     NOT NULL OPTIONS(description="nomenclatures[].id — артикул WB."),
  internal_sku           STRING    OPTIONS(description="Канонический SKU EVETIS из REF_SKU_MASTER. NULL = не разрешён, см. unmapped в манифесте. Строка НЕ отбрасывается."),
  in_action              BOOL      OPTIONS(description="nomenclatures[].inAction — участвует ли товар."),
  price                  NUMERIC   OPTIONS(description="nomenclatures[].price — текущая цена."),
  plan_price             NUMERIC   OPTIONS(description="nomenclatures[].planPrice — ПЛАНОВАЯ акционная цена. Единственный источник требуемой цены WB."),
  discount_pct           NUMERIC   OPTIONS(description="nomenclatures[].discount — текущая скидка, %."),
  plan_discount_pct      NUMERIC   OPTIONS(description="nomenclatures[].planDiscount — плановая скидка акции, %."),
  currency_code          STRING    OPTIONS(description="nomenclatures[].currencyCode."),

  raw_item_json          STRING    OPTIONS(description="Элемент nomenclatures[] дословно."),
  source_endpoint        STRING,
  source_payload_hash    STRING    OPTIONS(description="sha256 от (observation_id, promotion_id, in_action_requested, nm_id)."),
  ingested_at            TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY promotion_id, nm_id
OPTIONS(description="PR-PROMO-1. Append-only состав акций WB по номенклатурам. Заполняется ТОЛЬКО для акций type != 'auto': для автоакций метод неприменим по контракту WB. Пустая таблица — состояние площадки, а не дефект.");

-- ── WB-4. Манифест наблюдений ───────────────────────────────────────────────
-- Одна строка на снимок. Здесь run-level факты, которых нет в RAW.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_OBSERVATIONS`
(
  observation_id            STRING    NOT NULL,
  observation_bucket        STRING    NOT NULL,
  environment               STRING    NOT NULL,
  run_id                    STRING,
  started_at                TIMESTAMP NOT NULL,
  observed_at               TIMESTAMP OPTIONS(description="Момент успешного ответа WB. NULL, если ответа не было."),
  completed_at              TIMESTAMP,
  status                    STRING    NOT NULL OPTIONS(description="STARTED | COMPLETE | ERROR | REUSED."),

  http_status               INT64     OPTIONS(description="Код последнего ответа."),
  http_attempts             INT64     OPTIONS(description="Число HTTP-попыток, включая ретраи 429/5xx."),
  window_from               TIMESTAMP OPTIONS(description="Начало запрошенного окна календаря."),
  window_to                 TIMESTAMP OPTIONS(description="Конец запрошенного окна календаря."),

  promotions_listed         INT64     OPTIONS(description="Акций в ответе /promotions."),
  promotions_detailed       INT64     OPTIONS(description="Акций, по которым /details вернул объект."),
  details_empty             INT64     OPTIONS(description="Акций, по которым /details вернул 200 и пустой массив (штатно у завершённых)."),
  auto_promotions           INT64     OPTIONS(description="Акций type='auto' в снимке."),
  regular_promotions        INT64     OPTIONS(description="Акций type!='auto' в снимке. Пока 0 — состав по SKU недоступен."),
  ranging_rows              INT64     OPTIONS(description="Строк лестницы бустинга."),
  nomenclature_rows         INT64     OPTIONS(description="Строк состава акций. 0 при отсутствии regular-акций — ожидаемо."),
  nomenclature_attempts     INT64     OPTIONS(description="Сколько раз вызывали /nomenclatures."),
  capability_gap_count      INT64     OPTIONS(description="Сколько акций пропущено по известному ограничению контракта (автоакции + наблюдённый 422). НЕ ошибка."),
  unmapped_nm_ids           INT64     OPTIONS(description="nm_id из состава акции, не разрешённые в REF_SKU_MASTER. Не отбрасываются, но обязаны быть видны."),
  mapping_coverage_pct      NUMERIC   OPTIONS(description="Доля разрешённых nm_id, %. NULL при nomenclature_rows = 0."),

  rows_written              INT64     OPTIONS(description="Сумма строк по трём RAW-таблицам WB."),
  schema_status             STRING    OPTIONS(description="OK | DRIFT_NEW_FIELDS | DRIFT_MISSING_FIELDS."),
  schema_unknown_fields     STRING    OPTIONS(description="Поля ответа WB вне нашего контракта. Новое поле не должно раствориться молча."),
  error_code                STRING,
  error_message             STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY environment, observation_bucket
OPTIONS(description="PR-PROMO-1. Манифест наблюдений акций WB: полнота, свежесть, дрейф схемы, известные ограничения контракта, ошибки.");

-- ════════════════════════════════════════════════════════════════════════════
--  OZON  —  ozon_raw
-- ════════════════════════════════════════════════════════════════════════════

-- ── OZ-1. Акции Ozon ────────────────────────────────────────────────────────
-- Грейн: observation_id × action_id.  Источник: GET /v1/actions.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
(
  observed_at                 TIMESTAMP NOT NULL,
  observation_bucket          STRING    NOT NULL OPTIONS(description="6-часовое окно UTC, YYYY-MM-DDTHH:MM."),
  observation_id              STRING    NOT NULL OPTIONS(description="Детерминированный id снимка. Ключ идемпотентности load-джобы."),
  environment                 STRING    NOT NULL,
  run_id                      STRING,

  action_id                   INT64     NOT NULL OPTIONS(description="result[].id."),
  title                       STRING    OPTIONS(description="result[].title, дословно."),
  action_type                 STRING    OPTIONS(description="result[].action_type. Наблюдались ELASTIC_BOOSTING и STOCK_DISCOUNT. Значение источника."),
  date_start                  TIMESTAMP OPTIONS(description="result[].date_start."),
  date_end                    TIMESTAMP OPTIONS(description="result[].date_end."),
  freeze_at                   TIMESTAMP OPTIONS(description="result[].freeze_date. Пустая строка источника → NULL, а не эпоха. Заполнено = нельзя повышать цены, менять состав и уменьшать количество."),
  auto_add_dates_csv          STRING    OPTIONS(description="result[].auto_add_dates[] через запятую, ISO-8601. Даты, в которые площадка меняет состав без нашего участия."),
  auto_add_dates_count        INT64     OPTIONS(description="Число дат автодобавления."),
  is_participating            BOOL      OPTIONS(description="result[].is_participating — участвуем ли мы в акции."),
  potential_products_count    INT64     OPTIONS(description="result[].potential_products_count — товаров, доступных для акции."),
  participating_products_count INT64    OPTIONS(description="result[].participating_products_count — товаров в акции."),
  banned_products_count       INT64     OPTIONS(description="result[].banned_products_count."),
  is_voucher_action           BOOL      OPTIONS(description="result[].is_voucher_action."),
  with_targeting              BOOL      OPTIONS(description="result[].with_targeting."),
  order_amount                NUMERIC   OPTIONS(description="result[].order_amount."),
  discount_type               STRING    OPTIONS(description="result[].discount_type. Значение источника."),
  discount_value              NUMERIC   OPTIONS(description="result[].discount_value."),
  description                 STRING    OPTIONS(description="result[].description — условия акции, дословно (HTML источника)."),

  raw_action_json             STRING    OPTIONS(description="Элемент result[] дословно."),
  source_endpoint             STRING,
  source_payload_hash         STRING    OPTIONS(description="sha256 от (observation_id, action_id)."),
  ingested_at                 TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY action_id, action_type
OPTIONS(
  description="PR-PROMO-1. Append-only наблюдения акций Ozon. Источник: GET /v1/actions. Метод отдаёт только текущие и будущие акции — исторической выгрузки у Ozon нет.",
  require_partition_filter=false
);

-- ── OZ-2. Товары акций: участники и кандидаты ───────────────────────────────
-- Грейн: observation_id × action_id × product_id × membership.
-- Источники: POST /v1/actions/products (PARTICIPATING) и
--            POST /v1/actions/candidates (CANDIDATE).
-- Схема элемента у обоих методов идентична, различие смысла несёт membership.
-- ⚠️ CANDIDATE ≠ автодобавление. «Может быть добавлен» живёт здесь,
-- «будет добавлен» — в RAW_OZON_PROMO_AUTO_ADD с list_kind='SCHEDULED'.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
(
  observed_at                   TIMESTAMP NOT NULL,
  observation_bucket            STRING    NOT NULL,
  observation_id                STRING    NOT NULL,
  environment                   STRING    NOT NULL,
  run_id                        STRING,

  action_id                     INT64     NOT NULL,
  membership                    STRING    NOT NULL OPTIONS(description="PARTICIPATING — из /v1/actions/products; CANDIDATE — из /v1/actions/candidates. Часть грейна. Схлопывать запрещено."),
  product_id                    INT64     NOT NULL OPTIONS(description="products[].id — product_id Ozon."),
  internal_sku                  STRING    OPTIONS(description="Канонический SKU EVETIS через REF_SKU_CHANNEL_MAP.marketplace_product_id. NULL = не разрешён; строка НЕ отбрасывается."),
  sku_resolution_status         STRING    OPTIONS(description="RESOLVED_BY_PRODUCT_ID | UNRESOLVED."),

  price_rub                     NUMERIC   OPTIONS(description="products[].price — текущая цена продавца."),
  action_price_rub              NUMERIC   OPTIONS(description="products[].action_price — акционная цена. У кандидатов источник отдаёт 0: это «не назначена», а не «бесплатно»."),
  max_action_price_rub          NUMERIC   OPTIONS(description="products[].max_action_price — максимально допустимая акционная цена."),
  alert_max_action_price_rub    NUMERIC   OPTIONS(description="products[].alert_max_action_price. Описания подполя в документации Ozon нет — значение источника."),
  alert_max_action_price_failed BOOL      OPTIONS(description="products[].alert_max_action_price_failed. Значение источника без интерпретации."),
  add_mode                      STRING    OPTIONS(description="products[].add_mode: MANUAL | AUTO | NOT_SET. Как товар попал в акцию."),
  stock                         INT64     OPTIONS(description="products[].stock. НЕ складской остаток: наблюдался 0 при ненулевом FBO. Семантика не доказана — значение источника."),
  min_stock                     INT64     OPTIONS(description="products[].min_stock — требование к количеству для участия."),
  current_boost_pct             NUMERIC   OPTIONS(description="products[].current_boost — текущий бустинг, %."),
  min_boost_pct                 NUMERIC   OPTIONS(description="products[].min_boost."),
  max_boost_pct                 NUMERIC   OPTIONS(description="products[].max_boost."),
  price_min_elastic_rub         NUMERIC   OPTIONS(description="products[].price_min_elastic. Наблюдалось соответствие МИНИМАЛЬНОМУ бустингу (цена выше). Имя источника сохранено намеренно: семантика не подтверждена документацией."),
  price_max_elastic_rub         NUMERIC   OPTIONS(description="products[].price_max_elastic. Наблюдалось соответствие МАКСИМАЛЬНОМУ бустингу (цена ниже)."),

  raw_item_json                 STRING    OPTIONS(description="Элемент products[] дословно."),
  source_endpoint               STRING,
  source_payload_hash           STRING    OPTIONS(description="sha256 от (observation_id, action_id, membership, product_id)."),
  ingested_at                   TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY action_id, membership, product_id
OPTIONS(
  description="PR-PROMO-1. Append-only товары акций Ozon. membership различает участников (/products) и кандидатов (/candidates). Кандидат — НЕ автодобавление.",
  require_partition_filter=false
);

-- ── OZ-3. Автодобавление ────────────────────────────────────────────────────
-- Грейн: observation_id × action_id × auto_add_at × product_id × list_kind.
-- Источники: POST /v1/actions/auto-add/products/list       (list_kind='SCHEDULED')
--            POST /v1/actions/auto-add/products/candidates (list_kind='ELIGIBLE')
--
-- ⚠️ Разница между ними — предмет находки фазы 0: по акции 4253043 ELIGIBLE
-- содержал 17 товаров с падением вклада портфеля на 41 %, а SCHEDULED — один
-- товар по цене ВЫШЕ текущей. Схлопывание этих состояний создаёт ложную
-- экономику, поэтому list_kind входит в грейн.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
(
  observed_at                  TIMESTAMP NOT NULL,
  observation_bucket           STRING    NOT NULL,
  observation_id               STRING    NOT NULL,
  environment                  STRING    NOT NULL,
  run_id                       STRING,

  action_id                    INT64     NOT NULL,
  auto_add_at                  TIMESTAMP NOT NULL OPTIONS(description="Дата и время автодобавления из result.auto_add_dates[] метода /v1/actions. Параметр запроса, часть грейна."),
  list_kind                    STRING    NOT NULL OPTIONS(description="SCHEDULED — площадка ДОБАВИТ (/auto-add/products/list); ELIGIBLE — площадка МОЖЕТ добавить (/auto-add/products/candidates). Схлопывать запрещено."),
  product_id                   INT64     NOT NULL OPTIONS(description="products[].product_id."),
  offer_id                     STRING    OPTIONS(description="products[].offer_id — артикул продавца."),
  ozon_sku                     INT64     OPTIONS(description="products[].sku."),
  internal_sku                 STRING    OPTIONS(description="Канонический SKU EVETIS. NULL = не разрешён; строка НЕ отбрасывается."),
  sku_resolution_status        STRING    OPTIONS(description="RESOLVED_BY_PRODUCT_ID | RESOLVED_BY_OFFER_ID | UNRESOLVED."),
  product_name                 STRING    OPTIONS(description="products[].name, дословно."),

  price_rub                    NUMERIC   OPTIONS(description="products[].price — текущая цена продавца."),
  base_price_rub               NUMERIC   OPTIONS(description="products[].base_price."),
  max_discount_price_rub       NUMERIC   OPTIONS(description="products[].max_discount_price."),
  min_seller_price_rub         NUMERIC   OPTIONS(description="products[].min_seller_price."),
  marketplace_seller_price_rub NUMERIC   OPTIONS(description="products[].marketplace_seller_price."),
  action_price_to_auto_add_rub NUMERIC   OPTIONS(description="products[].action_price_to_auto_add — цена, по которой площадка добавит товар."),
  min_action_quantity          INT64     OPTIONS(description="products[].min_action_quantity."),
  quantity_to_auto_add         INT64     OPTIONS(description="products[].quantity_to_auto_add."),
  currency_code                STRING    OPTIONS(description="products[].currency."),
  add_mode                     STRING    OPTIONS(description="products[].add_mode. Возвращается только методом /list; у /candidates отсутствует → NULL."),

  raw_item_json                STRING    OPTIONS(description="Элемент products[] дословно."),
  source_endpoint              STRING,
  source_payload_hash          STRING    OPTIONS(description="sha256 от (observation_id, action_id, auto_add_at, list_kind, product_id)."),
  ingested_at                  TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY action_id, list_kind, product_id
OPTIONS(
  description="PR-PROMO-1. Append-only автодобавление Ozon. list_kind='SCHEDULED' — площадка добавит; 'ELIGIBLE' — может добавить. Это РАЗНЫЕ состояния.",
  require_partition_filter=false
);

-- ── OZ-4. Маркетинговые акции на уровне товара ──────────────────────────────
-- Грейн: observation_id × offer_id.  Источник: POST /v5/product/info/prices.
--
-- Закрывает пробел, зафиксированный фазой 0: блок marketing_actions приходит в
-- боевой загрузчик цен и там отбрасывается, кроме одного флага
-- (pipelines/ozon/runtime/entities.py). Существующая колонка
-- RAW_OZON_PRICES.ozon_actions_exist НЕ меняется и НЕ переиспользуется:
-- её семантика остаётся прежней, а авторитетное наблюдение живёт здесь.
--
-- ⚠️ ozon_actions_exist НЕ является признаком участия в акциях. Замер 2026-09-22:
-- флаг FALSE у 20/20 товаров, при этом у 19 из 20 в том же блоке непустой
-- actions[] (15–19 элементов), а /v1/actions/products показывает 18 участников.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
(
  observed_at                      TIMESTAMP NOT NULL,
  observation_bucket               STRING    NOT NULL,
  observation_id                   STRING    NOT NULL,
  environment                      STRING    NOT NULL,
  run_id                           STRING,

  offer_id                         STRING    NOT NULL OPTIONS(description="items[].offer_id — артикул продавца."),
  product_id                       INT64     OPTIONS(description="items[].product_id."),
  internal_sku                     STRING    OPTIONS(description="Канонический SKU EVETIS. NULL = не разрешён; строка НЕ отбрасывается."),
  sku_resolution_status            STRING    OPTIONS(description="RESOLVED_BY_PRODUCT_ID | RESOLVED_BY_OFFER_ID | UNRESOLVED."),

  source_actions_exist_flag        BOOL      OPTIONS(description="marketing_actions.ozon_actions_exist ДОСЛОВНО. ⚠️ НЕ АВТОРИТЕТЕН для участия в акциях: наблюдался FALSE при 19 непустых элементах actions[] в том же блоке. Хранится как факт источника и только."),
  current_period_from              TIMESTAMP OPTIONS(description="marketing_actions.current_period_from."),
  current_period_to                TIMESTAMP OPTIONS(description="marketing_actions.current_period_to."),
  actions_count                    INT64     OPTIONS(description="Длина marketing_actions.actions[]. Сами элементы — в RAW_OZON_PROMO_PRODUCT_ACTION."),

  price_rub                        NUMERIC   OPTIONS(description="price.price."),
  min_price_rub                    NUMERIC   OPTIONS(description="price.min_price — порог автоприменения скидок. Наблюдался равным price у 20/20."),
  marketing_seller_price_rub       NUMERIC   OPTIONS(description="price.marketing_seller_price — база комиссии в канонической экономике Ozon."),
  old_price_rub                    NUMERIC   OPTIONS(description="price.old_price."),
  auto_action_enabled              BOOL      OPTIONS(description="price.auto_action_enabled, дословно."),
  auto_add_to_ozon_actions_enabled BOOL      OPTIONS(description="price.auto_add_to_ozon_actions_list_enabled, дословно."),

  marketing_actions_json           STRING    OPTIONS(description="Блок marketing_actions целиком, дословно. Гарантия отсутствия потерь при эволюции схемы."),
  source_endpoint                  STRING,
  source_payload_hash              STRING    OPTIONS(description="sha256 от (observation_id, offer_id)."),
  ingested_at                      TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY offer_id, internal_sku
OPTIONS(
  description="PR-PROMO-1. Append-only маркетинговые акции Ozon на уровне товара из /v5/product/info/prices. source_actions_exist_flag сохранён как факт источника и НЕ авторитетен для участия в акциях.",
  require_partition_filter=false
);

-- ── OZ-5. Элементы marketing_actions.actions[] ──────────────────────────────
-- Грейн: observation_id × offer_id × action_ordinal.
--
-- ⚠️ value у Ozon РАЗНОРОДЕН по смыслу: у ценовых акций это акционная цена в
-- рублях (совпадает с action_price из /v1/actions/products), у рассрочки — число
-- месяцев («РК. Рассрочка 0-0-12» → 12), у программ «товар за 1 рубль» — 100.
-- Нормализация запрещена: величина хранится как есть, интерпретация — в
-- PR-PROMO-2 по типу акции.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION`
(
  observed_at         TIMESTAMP NOT NULL,
  observation_bucket  STRING    NOT NULL,
  observation_id      STRING    NOT NULL,
  environment         STRING    NOT NULL,
  run_id              STRING,

  offer_id            STRING    NOT NULL,
  product_id          INT64,
  internal_sku        STRING    OPTIONS(description="Канонический SKU EVETIS. NULL = не разрешён."),
  action_ordinal      INT64     NOT NULL OPTIONS(description="Позиция в actions[], начиная с 0. Порядок источника сохраняется."),

  action_title        STRING    OPTIONS(description="actions[].title дословно. Ozon не отдаёт здесь идентификатор акции — только название."),
  action_value_num    NUMERIC   OPTIONS(description="actions[].value БЕЗ интерпретации единицы. Рубли у ценовых акций, месяцы у рассрочки, 100 у программ «за 1 рубль»."),
  action_date_from    TIMESTAMP OPTIONS(description="actions[].date_from."),
  action_date_to      TIMESTAMP OPTIONS(description="actions[].date_to."),

  raw_item_json       STRING    OPTIONS(description="Элемент actions[] дословно."),
  source_endpoint     STRING,
  source_payload_hash STRING    OPTIONS(description="sha256 от (observation_id, offer_id, action_ordinal)."),
  ingested_at         TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY offer_id, action_ordinal
OPTIONS(
  description="PR-PROMO-1. Append-only длинная проекция marketing_actions.actions[] Ozon. Единица измерения value разнородна — не нормализовать.",
  require_partition_filter=false
);

-- ── OZ-6. Манифест наблюдений Ozon ──────────────────────────────────────────
-- Одна строка на снимок. Дополняет OZON_INGESTION_RUNS, а не заменяет его:
-- в RUNS остаётся изоляция отказов по сущностям, здесь — полнота и покрытие.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_OBSERVATIONS`
(
  observation_id          STRING    NOT NULL,
  observation_bucket      STRING    NOT NULL,
  environment             STRING    NOT NULL,
  run_id                  STRING,
  started_at              TIMESTAMP NOT NULL,
  observed_at             TIMESTAMP,
  completed_at            TIMESTAMP,
  status                  STRING    NOT NULL OPTIONS(description="STARTED | COMPLETE | ERROR | REUSED."),

  requests                INT64     OPTIONS(description="Число HTTP-запросов к Seller API за снимок."),
  retries                 INT64     OPTIONS(description="Число повторов 429/5xx."),
  actions_total           INT64     OPTIONS(description="Акций в /v1/actions."),
  actions_participating   INT64     OPTIONS(description="Акций с is_participating = TRUE."),
  products_participating  INT64     OPTIONS(description="Строк membership='PARTICIPATING'."),
  products_candidate      INT64     OPTIONS(description="Строк membership='CANDIDATE'."),
  auto_add_scheduled_rows INT64     OPTIONS(description="Строк list_kind='SCHEDULED'."),
  auto_add_eligible_rows  INT64     OPTIONS(description="Строк list_kind='ELIGIBLE'."),
  auto_add_pairs          INT64     OPTIONS(description="Пар (акция, дата автодобавления), опрошенных в снимке."),
  marketing_products      INT64     OPTIONS(description="Товаров в /v5/product/info/prices."),
  marketing_action_rows   INT64     OPTIONS(description="Строк длинной проекции actions[]."),

  source_products         INT64     OPTIONS(description="Уникальных product_id/offer_id, встреченных в снимке."),
  mapped_products         INT64     OPTIONS(description="Из них разрешённых в internal_sku."),
  unmapped_products       INT64     OPTIONS(description="Не разрешённых. Не отбрасываются."),
  unmapped_ids            STRING    OPTIONS(description="Список неразрешённых идентификаторов через запятую."),
  mapping_coverage_pct    NUMERIC   OPTIONS(description="mapped / source × 100."),

  rows_written            INT64     OPTIONS(description="Сумма строк по пяти RAW-таблицам Ozon."),
  schema_status           STRING    OPTIONS(description="OK | DRIFT_NEW_FIELDS | DRIFT_MISSING_FIELDS."),
  schema_unknown_fields   STRING    OPTIONS(description="Поля ответа Ozon вне нашего контракта."),
  error_code              STRING,
  error_message           STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY environment, observation_bucket
OPTIONS(description="PR-PROMO-1. Манифест наблюдений акций Ozon: полнота источников, покрытие резолва SKU, дрейф схемы, ошибки.");
