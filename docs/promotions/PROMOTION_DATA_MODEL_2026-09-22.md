# EVETIS — модель данных акций (BigQuery)
# Предложение, 2026-09-22. Ни один объект не создан.

Опирается на факты `PROMOTION_DISCOVERY_2026-09-22.md` и матрицу
`PROMOTION_API_CAPABILITY_MATRIX_2026-09-22.md`.

---

## 0. Принципы, вытекающие из существующей архитектуры

| Принцип | Откуда |
|---|---|
| **Изоляция маркетплейсов.** RAW и канонический слой WB не читает Ozon и наоборот; общее — только `evetis_ref` | правило проекта, нарушение = architecture QA FAIL |
| **Нейтральный кросс-МП слой живёт в `evetis_mart`** | там уже лежит `FACT_SKU_DAILY` — единственный кросс-маркетплейсный факт |
| **Никаких `ACTION` в именах** | `ACTION` в EVETIS = задача владельца Control Tower (`CT_OWNER_ACTION_QUEUE`, `V_CT_ACTION_QUEUE`, `sp_ct_generate_actions`). Акции маркетплейса — `PROMO` |
| **Append-only, состояние выводится из истории, а не хранится перезаписью** | так сделан наблюдатель цен: `RAW_WB_PRICES` + `V_WB_PRICES_CURRENT` |
| **Манифест наблюдений рядом с сырьём** | `WB_PRICES_OBSERVATIONS`, `WB_TARIFF_OBSERVATIONS`, `WB_FUNNEL_OBSERVATIONS` |
| **Сырой payload сохраняется целиком** | `RAW_WB_PRICES.raw_item_json`, `RAW_OZON_PRICES.commissions_json` |
| **Ни одна экономическая формула не форкается** | `V_*_FORWARD_ECONOMICS_CURRENT` — единственный источник вклада и безубыточности |
| **Fail closed** | отсутствие входа = `BLOCKED`, а не ноль |

Партиционирование и кластеризация — как у существующих наблюдателей: партиция по дате
наблюдения, кластер по ключу сущности.

---

## 1. Слой RAW — по одному датасету на маркетплейс

### 1.1 `wb_raw.RAW_WB_PROMO_CALENDAR`

**Грейн:** `observed_at × promotion_id`. Append-only.
Источник: `GET /api/v1/calendar/promotions` + `GET /api/v1/calendar/promotions/details`
(склеиваются в одну строку: список даёт существование, детали — условия).

| Колонка | Тип | Источник / смысл |
|---|---|---|
| `observed_at` | TIMESTAMP | момент наблюдения |
| `observation_date` | DATE | партиция |
| `observation_id` | STRING | идентификатор прогона наблюдения |
| `environment` | STRING | `prod` / `shadow` |
| `run_id` | STRING | прогон загрузчика |
| `promotion_id` | INT64 | `id` |
| `promotion_name` | STRING | `name` |
| `promotion_type` | STRING | `type` — сегодня `auto` / `regular` |
| `starts_at` | TIMESTAMP | `startDateTime` |
| `ends_at` | TIMESTAMP | `endDateTime` |
| `description` | STRING | `details.description` |
| `advantages` | ARRAY&lt;STRING&gt; | `details.advantages[]` |
| `in_promo_total` | INT64 | `inPromoActionTotal` — наших товаров в акции |
| `in_promo_leftovers` | INT64 | `inPromoActionLeftovers` |
| `not_in_promo_total` | INT64 | `notInPromoActionTotal` |
| `not_in_promo_leftovers` | INT64 | `notInPromoActionLeftovers` |
| `participation_pct` | NUMERIC | `participationPercentage` |
| `exception_products_count` | INT64 | `exceptionProductsCount` |
| `ranging_json` | STRING | `ranging[]` целиком |
| `boost_min_pct` / `boost_max_pct` | NUMERIC | производные от `ranging[]`, для удобства фильтра |
| `details_available` | BOOL | FALSE, если `details` вернул пустой массив (завершённая акция) |
| `raw_promotion_json` | STRING | полный элемент списка + деталей |
| `source_endpoint` | STRING | `GET /api/v1/calendar/promotions[/details]` |
| `source_payload_hash` | STRING | sha256 нормализованного payload |
| `ingested_at` | TIMESTAMP | |

Партиция `observation_date`, кластер `promotion_id, promotion_type`.

### 1.2 `wb_raw.RAW_WB_PROMO_NOMENCLATURE`

**Грейн:** `observed_at × promotion_id × nm_id × in_action`.
Источник: `GET /api/v1/calendar/promotions/nomenclatures`.

**Сегодня таблица будет пустой** — у EVETIS нет акций `type != 'auto'`. Создаётся именно
поэтому: без неё первая же появившаяся `regular`-акция пройдёт мимо и её плановая цена
исчезнет безвозвратно. Пустота — наблюдаемое состояние площадки, а не дефект; она
фиксируется в манифесте (`skipped_auto_promotions`, `regular_promotions_seen`).

Колонки: технические как выше, плюс `promotion_id`, `nm_id` (`id`), `internal_sku`
(резолв), `in_action` (`inAction`), `price`, `plan_price` (`planPrice`),
`discount_pct`, `plan_discount_pct` (`planDiscount`), `currency_code`, `raw_item_json`.

### 1.3 `wb_raw.WB_PROMO_OBSERVATIONS`

Манифест прогона, по образцу `WB_PRICES_OBSERVATIONS`:
`observation_id, run_id, environment, started_at, finished_at, status, endpoint_calls,
promotions_listed, promotions_detailed, details_empty, regular_promotions_seen,
skipped_auto_promotions, nomenclature_rows, schema_status, unknown_fields,
missing_required_fields, http_errors, error_message`.

`schema_status ∈ {OK, DRIFT_NEW_FIELDS, DRIFT_MISSING_FIELDS}` — та же дисциплина, что в
`cloud/src/loaders/prices/constants.ts`.

### 1.4 `ozon_raw.RAW_OZON_PROMO_ACTIONS`

**Грейн:** `observed_at × action_id`. Источник: `GET /v1/actions`.

| Колонка | Тип | Источник |
|---|---|---|
| `snapshot_ts` / `snapshot_date` | TIMESTAMP / DATE | как в `RAW_OZON_PRICES` |
| `action_id` | INT64 | `id` |
| `title` | STRING | `title` |
| `action_type` | STRING | `action_type` (`ELASTIC_BOOSTING`, `STOCK_DISCOUNT`, `DISCOUNT`, …) |
| `date_start` / `date_end` | TIMESTAMP | |
| `freeze_date` | TIMESTAMP | пусто → NULL, **не эпоха** |
| `auto_add_dates` | ARRAY&lt;TIMESTAMP&gt; | `auto_add_dates[]` |
| `is_participating` | BOOL | |
| `potential_products_count` | INT64 | |
| `participating_products_count` | INT64 | |
| `banned_products_count` | INT64 | |
| `is_voucher_action` | BOOL | |
| `with_targeting` | BOOL | |
| `order_amount` | NUMERIC | |
| `discount_type` | STRING | |
| `discount_value` | NUMERIC | |
| `description` | STRING | |
| `raw_action_json` | STRING | элемент целиком |
| `source_endpoint`, `ingestion_run_id`, `extracted_at`, `source_payload_hash` | | как в остальных `RAW_OZON_*` |

Ключ слияния как у существующих сущностей Ozon: `["snapshot_date","action_id"]` — повтор
прогона в тот же день не плодит строк. Партиция `snapshot_date`, кластер `action_id, action_type`.

### 1.5 `ozon_raw.RAW_OZON_PROMO_PRODUCTS`

**Грейн:** `observed_at × action_id × product_id × membership`.
Источники: `POST /v1/actions/products` (`membership = 'PARTICIPATING'`) и
`POST /v1/actions/candidates` (`membership = 'CANDIDATE'`). Один объект, а не два: схема
элемента побайтово одинакова, а различие смысла несёт колонка — иначе половина запросов
и половина проверок качества дублировались бы.

Колонки: технические + `action_id`, `product_id` (`id`), `membership`,
`price_rub`, `action_price_rub`, `max_action_price_rub`,
`alert_max_action_price_rub`, `alert_max_action_price_failed`,
`add_mode`, `stock`, `min_stock`,
`current_boost_pct`, `min_boost_pct`, `max_boost_pct`,
`price_min_elastic_rub`, `price_max_elastic_rub`, `raw_item_json`.

Ключ слияния `["snapshot_date","action_id","product_id","membership"]`.

### 1.6 `ozon_raw.RAW_OZON_PROMO_AUTO_ADD`

**Грейн:** `observed_at × action_id × auto_add_date × product_id × list_kind`.
Источники: `/v1/actions/auto-add/products/list` (`list_kind = 'SCHEDULED'`) и
`/v1/actions/auto-add/products/candidates` (`list_kind = 'ELIGIBLE'`).

Разделение обязательно: разница между «может добавить» и «добавит» стоит по сегодняшнему
замеру 41 % вклада на единицу (см. `PROMOTION_DISCOVERY_2026-09-22.md` §4.4). Схлопывать их
в одну сущность нельзя.

Колонки: технические + `action_id`, `auto_add_date`, `list_kind`, `product_id`, `offer_id`,
`ozon_sku`, `product_name`, `price_rub`, `base_price_rub`, `max_discount_price_rub`,
`min_seller_price_rub`, `marketplace_seller_price_rub`, `action_price_to_auto_add_rub`,
`min_action_quantity`, `quantity_to_auto_add`, `currency_code`, `add_mode` (NULL у
`ELIGIBLE`), `raw_item_json`.

Ключ слияния `["snapshot_date","action_id","auto_add_date","product_id","list_kind"]`.

### 1.7 Манифест Ozon — **новый объект не нужен**

Прогон пишется в существующий `ozon_raw.OZON_INGESTION_RUNS` как отдельные сущности
`promo_actions`, `promo_products`, `promo_auto_add` в `REGISTRY`
(`pipelines/ozon/runtime/entities.py`). Изоляция отказов, счётчики строк и запись ошибок
уже реализованы — дублировать их незачем.

### 1.8 Отложено до отдельного решения владельца

`ozon_raw.RAW_OZON_PROMO_DISCOUNT_TASK` — «Хочу скидку». Содержит ПДн покупателей.
Не проектируется в этой фазе; если будет принято грузить, поля
`customer_name, first_name, last_name, patronymic, email, user_comment`
**отбрасываются в загрузчике до формирования строки**, а не маскируются вью.

---

## 2. Канонический слой — по датасету маркетплейса

### 2.1 `wb_mart.V_WB_PROMO_CURRENT`

Последнее наблюдение каждой акции WB + вычисленный этап жизненного цикла.
`QUALIFY ROW_NUMBER() OVER (PARTITION BY promotion_id ORDER BY observed_at DESC) = 1`
— ровно тот приём, которым сделан `V_WB_PRICES_CURRENT`.

Добавляет: `lifecycle_stage` (§4 спецификации решений), `days_until_start`,
`days_until_end`, `observation_age_minutes`, `freshness_status`,
`sku_level_data_available = FALSE` для `promotion_type = 'auto'` — **явный флаг слепоты**,
чтобы ни один потребитель не принял отсутствие строк уровня SKU за «мы не участвуем».

### 2.2 `wb_mart.V_WB_PROMO_SKU_CURRENT`

Строится из `RAW_WB_PROMO_NOMENCLATURE`. **Сегодня вернёт 0 строк.** Существует, чтобы
нейтральный слой имел одинаковую форму для обеих площадок и не переписывался в день,
когда WB даст `regular`-акцию.

### 2.3 `ozon_mart.V_OZON_PROMO_CURRENT`

Последнее наблюдение каждой акции Ozon + `lifecycle_stage`, `days_until_start/end`,
`next_auto_add_date` (ближайшая будущая из `auto_add_dates`), `days_until_auto_add`,
`freeze_active`, `freshness_status`.

### 2.4 `ozon_mart.V_OZON_PROMO_SKU_CURRENT`

Последнее наблюдение на паре акция × товар, с резолвом в `internal_sku` через
`evetis_ref.REF_SKU_CHANNEL_MAP` (`marketplace='OZON' AND is_current`,
`SAFE_CAST(marketplace_product_id AS INT64) = product_id`), с запасным путём по `offer_id`
для строк автодобавления.

Объединяет три источника в один грейн `action_id × internal_sku`, добавляя взаимно
исключающие признаки:

| Колонка | Смысл |
|---|---|
| `is_participating` | есть строка `membership = 'PARTICIPATING'` |
| `is_candidate` | есть строка `membership = 'CANDIDATE'` |
| `is_auto_add_scheduled` | есть строка `list_kind='SCHEDULED'` с `add_mode='AUTO'` на будущую дату |
| `is_auto_add_eligible` | есть строка `list_kind='ELIGIBLE'` |
| `required_price_rub` | `action_price` у участника, иначе `action_price_to_auto_add` у запланированного, иначе NULL |
| `max_allowed_price_rub` | `max_action_price` |
| `alert_price_rub`, `alert_failed` | сигнал площадки |
| `min_stock_required` | `min_stock` / `min_action_quantity` |
| `boost_current_pct`, `boost_min_pct`, `boost_max_pct` | |
| `price_at_min_boost_rub`, `price_at_max_boost_rub` | переименование `price_min_elastic` / `price_max_elastic` **по наблюдаемому смыслу**; исходные имена сохраняются рядом, пока O-2 не закрыт |
| `sku_resolution_status` | `RESOLVED_BY_PRODUCT_ID` / `RESOLVED_BY_OFFER_ID` / `UNRESOLVED` |

`UNRESOLVED` не отбрасывается: строка остаётся и попадает в health-проверку.

---

## 3. Нейтральный слой — `evetis_mart`

### 3.1 `evetis_mart.V_PROMO_SKU_STATE`

**Грейн: `marketplace × promotion_id × internal_sku`, текущее состояние.**
UNION ALL двух канонических вью. Единственное место, где WB и Ozon встречаются, и делают
они это уже после того, как каждый маркетплейс привёл себя к общей форме.

Колонки — общий минимум: `marketplace, promotion_id, promotion_name, promotion_type,
action_type, internal_sku, marketplace_sku, starts_at, ends_at, freeze_at,
next_auto_add_at, lifecycle_stage, is_participating, is_candidate,
is_auto_add_scheduled, required_price_rub, max_allowed_price_rub, min_stock_required,
boost_current_pct, boost_max_pct, current_seller_price_rub, observed_at,
data_completeness` где
`data_completeness ∈ {SKU_LEVEL, PORTFOLIO_LEVEL_ONLY}`.

WB сегодня поставляет сюда **ноль строк уровня SKU**; его участие представлено отдельной
вью портфельного уровня (ниже), а не строками с NULL-ценой — иначе NULL начал бы
означать одновременно «нет данных» и «цена не требуется».

### 3.2 `evetis_mart.V_PROMO_PORTFOLIO_STATE`

**Грейн: `marketplace × promotion_id`.** То, что WB отдаёт только агрегатами:
`in_promo_total`, `not_in_promo_total`, `participation_pct`, `exception_products_count`,
лестница бустинга, `boost_at_current_participation_pct`. Для Ozon заполняется из
`participating_products_count` / `potential_products_count`.

Эта вью — то место, где по WB вообще возможны решения: «в акции 1 наш товар из 5,
следующая ступень бустинга начинается с 50 % участия».

### 3.3 `evetis_mart.V_PROMO_SKU_SCENARIO`

Экономические сценарии. Формулы и источники — в
`PROMOTION_ECONOMICS_SPEC_2026-09-22.md`. Грейн:
`marketplace × promotion_id × internal_sku × scenario`, где
`scenario ∈ {BASELINE, ENTER, STAY, EXIT, MAX_ALLOWED, AT_MAX_BOOST}`.

### 3.4 `evetis_mart.V_PROMO_SKU_DECISION`

Рекомендация. Грейн тот же минус сценарий. Состояния и reason codes — в
`PROMOTION_DECISION_ENGINE_SPEC_2026-09-22.md`.

### 3.5 `evetis_mart.V_PROMO_RADAR`

Витрина для Metabase. Грейн: `marketplace × internal_sku × promotion_id`, только
релевантные строки (§13 спецификации решений).

### 3.6 `evetis_mart.V_PROMO_HEALTH`

Одна строка — сводное состояние подсистемы, по образцу
`wb_mart.V_WB_PRICING_ECONOMICS_HEALTH`. Проверки перечислены в §17 спецификации решений.

### 3.7 `evetis_mart.FACT_PROMO_SKU_WINDOW`

**Таблица**, а не вью: окна «до / во время / после» считаются дорого и должны быть
воспроизводимы задним числом. Грейн:
`marketplace × promotion_id × internal_sku × window_kind`,
`window_kind ∈ {PRE, DURING, POST}`. Наполняется процедурой при переходе акции в `ENDED`.
Детали — `PROMOTION_ECONOMICS_SPEC_2026-09-22.md` §5.

---

## 4. Справочник — `evetis_ref`

### `evetis_ref.REF_PROMO_POLICY`

Пороги решений **как данные, а не как константы в SQL**, с датированием — по образцу
`REF_MARKETPLACE_COMMISSION_COMPONENT` и `REF_SKU_COGS_HISTORY`.

| Колонка | Смысл |
|---|---|
| `policy_key` | например `CONTRIBUTION_FLOOR_PCT` |
| `marketplace` | `WB` / `OZON` / `ALL` |
| `scope` | `ALL` или конкретный `internal_sku` |
| `value_num` / `value_str` | значение |
| `basis` | `CONTRIBUTION_BEFORE_ADS_PRE_TAX_PCT_OF_SELLER_PRICE` и т. п. |
| `case_basis` | `EXPECTED` / `WORST` |
| `is_hard_floor` | TRUE = запрет, FALSE = цель |
| `effective_from` / `effective_to` | датирование |
| `source` | ссылка на документ решения владельца |
| `notes` | |

Первое наполнение — только после подтверждения владельцем (см. §10 спецификации решений).

---

## 5. Схема связей

```
                    evetis_ref.REF_SKU_CHANNEL_MAP ──┐
                    evetis_ref.V_PRODUCT_COGS_EFFECTIVE
                              │                      │
  wb_raw.RAW_WB_PROMO_CALENDAR│                      │  ozon_raw.RAW_OZON_PROMO_ACTIONS
  wb_raw.RAW_WB_PROMO_NOMENCL.│                      │  ozon_raw.RAW_OZON_PROMO_PRODUCTS
  wb_raw.WB_PROMO_OBSERVATIONS│                      │  ozon_raw.RAW_OZON_PROMO_AUTO_ADD
            │                 │                      │            │
            ▼                 │                      │            ▼
  wb_mart.V_WB_PROMO_CURRENT  │                      │  ozon_mart.V_OZON_PROMO_CURRENT
  wb_mart.V_WB_PROMO_SKU_CURRENT (пусто) ◄───────────┴─►ozon_mart.V_OZON_PROMO_SKU_CURRENT
            │                                                     │
            └──────────────┬──────────────────────────────────────┘
                           ▼
              evetis_mart.V_PROMO_SKU_STATE        evetis_mart.V_PROMO_PORTFOLIO_STATE
                           │
      ┌────────────────────┼─────────────────────────────┐
      ▼                    ▼                             ▼
wb_mart.V_WB_SKU_    ozon_mart.V_OZON_SKU_     wb_mart.V_CT_INVENTORY_TRUTH
FORWARD_ECONOMICS_   FORWARD_ECONOMICS_
CURRENT              CURRENT
      └────────────────────┬─────────────────────────────┘
                           ▼
              evetis_mart.V_PROMO_SKU_SCENARIO
                           │
                           ├──◄── evetis_ref.REF_PROMO_POLICY
                           ├──◄── evetis_mart.FACT_PROMO_SKU_WINDOW
                           ▼
              evetis_mart.V_PROMO_SKU_DECISION
                           │
             ┌─────────────┴──────────────┐
             ▼                            ▼
   evetis_mart.V_PROMO_RADAR    wb_ops.OPS_INCIDENT / OPS_ALERT_EVENT
```

---

## 6. Что НЕ создаётся и почему

| Отвергнутый объект | Причина |
|---|---|
| Отдельная таблица «текущее состояние» с перезаписью | состояние выводится из append-only истории — так уже сделано у цен, тарифов и воронки |
| Отдельный манифест прогонов Ozon | `OZON_INGESTION_RUNS` уже изолирует отказы по сущностям |
| Отдельные таблицы под `products` и `candidates` | идентичная схема элемента; различие несёт колонка `membership` |
| Своя таблица остатков | канонический источник — `wb_mart.V_CT_INVENTORY_TRUTH`, второй истины быть не должно |
| Своя формула вклада | `V_*_FORWARD_ECONOMICS_CURRENT` — единственный источник; сценарии лишь подставляют цену |
| Своя подсистема уведомлений | `wb_ops.OPS_INCIDENT` + `OPS_ALERT_EVENT` с дедупом по `condition_fingerprint` уже есть |
| `RAW_*_PROMO_*` в общем датасете | нарушило бы изоляцию маркетплейсов |
| Материализация СПП 32 % | это допущение владельца, а не наблюдаемая величина |

---

## 7. Влияние на существующие объекты

**Нулевое.** Ни один существующий объект не изменяется. Единственное предлагаемое
изменение в действующем пайплайне — **расширение** записи `prices` в Ozon: сохранять
`marketing_actions.actions[]` (сегодня отбрасывается) в новую колонку
`marketing_actions_json` таблицы `RAW_OZON_PRICES`. Это добавление колонки, а не изменение
семантики существующих; экономика витрин не затрагивается. Выносится отдельным шагом
PR-PROMO-1 с явной контрольной суммой: число строк и все существующие колонки не меняются.
