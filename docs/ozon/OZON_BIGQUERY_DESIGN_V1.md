# OZON_BIGQUERY_DESIGN_V1 — только проектирование

Версия 1, 2026-09-01. **DDL не выполнялся. В BigQuery ничего не создавалось и не менялось.**
Подчиняется `architecture/MARKETPLACE_ISOLATION.md` и `OZON_DATA_CONTRACT_V1.md`.

## Marketplace Isolation

Это обязательный раздел проекта, а не пояснение.

```
evetis_ref                          ОБЩИЙ СЛОЙ, только справочники
   ├── REF_PRODUCT_MASTER           атрибуты товара по internal_sku
   ├── REF_SKU_CHANNEL_MAP          internal_sku × marketplace × marketplace_sku
   ├── REF_SKU_COGS_HISTORY         себестоимость
   ├── REF_BUNDLE_COMPONENTS        состав наборов
   └── V_PRODUCT_COGS_EFFECTIVE     действующая себестоимость

ozon_raw  →  ozon_stg  →  ozon_mart          ДОМЕН OZON
wb_raw    →  wb_stg    →  wb_mart            ДОМЕН WB (не трогаем)
```

Правила, обязательные для каждой таблицы ниже:

1. Ни одна таблица `ozon_*` не читает `wb_raw` и `wb_mart`.
2. Единственная разрешённая внешняя зависимость домена Ozon — `evetis_ref`.
3. Ни одна таблица `ozon_*` не пишет в датасеты WB.
4. Ни одна колонка Ozon не добавляется в витрины WB.
5. Кросс-маркетплейсный слой в этом проекте **не создаётся**.
6. Идентификаторы Ozon разрешаются только через `REF_SKU_CHANNEL_MAP`;
   неявный join `offer_id = nm_id` в production **запрещён**.

### Как проект устраняет D024

Сейчас домен Ozon читает `wb_raw.REF_SKU_MASTER`. В целевой схеме этой зависимости нет:
связка идёт через `evetis_ref.REF_SKU_CHANNEL_MAP`, где идентификатор Ozon хранится
явной строкой. Доказательство воспроизводимости — `data/proposed_ozon_channel_map.csv`:
**20 из 20 товаров Ozon резолвятся в `internal_sku` только по строкам `marketplace='OZON'`**,
ни одна строка OZON не использует `nm_id` в качестве `marketplace_sku`.

## Общий слой `evetis_ref`

| Таблица | Grain | Partition | Cluster | Логический PK | Источник | Update mode |
| --- | --- | --- | --- | --- | --- | --- |
| `REF_PRODUCT_MASTER` | internal_sku | нет | `internal_sku` | `internal_sku` | справочник EVETIS | MERGE |
| `REF_SKU_CHANNEL_MAP` | internal_sku × marketplace × marketplace_sku | нет | `marketplace`, `internal_sku` | (`marketplace`,`marketplace_sku`,`valid_from`) | Ozon Seller API; WB — миграционно | MERGE, SCD2 |

`REF_PRODUCT_MASTER` — 9 полей, классифицированных как общие
(`PRODUCT_REFERENCE_FIELD_CLASSIFICATION.csv`). Операционные флаги WB
(`include_in_pnl`, `include_in_ads_analysis`, `include_in_supply_plan`,
`include_in_stock_alerts`, `data_quality_status`) и поля `wb_vendor_code`,
`wb_subject_id`, `wb_subject_name` **в общий слой не переносятся** и остаются в `wb_raw`.
Три поля (`product_name_full`, `status`, `active`) отложены до уточнения владельцем.

## Слой `ozon_raw`

| Таблица | Grain | Partition | Cluster | Логический PK | Update | Объём/сутки |
| --- | --- | --- | --- | --- | --- | --- |
| `RAW_OZON_CATALOG` | снимок × sku | `snapshot_date` | `sku` | (snapshot_date, sku) | APPEND | ~20 строк |
| `RAW_OZON_PRICES` | снимок × offer_id | `DATE(snapshot_ts)` | `offer_id` | (snapshot_ts, offer_id) | APPEND | ~480 строк (1/час) |
| `RAW_OZON_STOCKS` | снимок × sku × склад | `snapshot_date` | `sku`, `warehouse_id` | (snapshot_date, sku, warehouse_id) | APPEND | ~140 строк |
| `RAW_OZON_POSTINGS_FBO` | posting × sku | `DATE(created_at)` | `posting_number`, `sku` | (posting_number, sku) | MERGE | ~10 строк |
| `RAW_OZON_FINANCE_ACCRUAL` | accrual × type × sku | `event_date` | `type_id`, `sku` | (accrual_id, type_id, sku) | MERGE | ~20 строк |
| `RAW_OZON_ADS_CAMPAIGNS` | снимок × campaign_id | `snapshot_date` | `campaign_id` | (snapshot_date, campaign_id) | APPEND | ~92 строки |
| `RAW_OZON_ADS_EXPENSE_DAILY` | дата × campaign_id | `date` | `campaign_id` | (date, campaign_id) | MERGE | ~12 строк |
| `RAW_OZON_ADS_CAMPAIGN_SKU` | снимок × campaign × sku | `snapshot_date` | `campaign_id`, `sku` | (snapshot_date, campaign_id, sku) | APPEND | ~18 строк |
| `RAW_OZON_ADS_SKU_DAILY` | дата × campaign × sku | `date` | `campaign_id`, `sku` | (date, campaign_id, sku) | MERGE | ~11 строк |
| `RAW_OZON_PUBLIC_SHELF` | момент × sku × регион | `DATE(snapshot_ts)` | `sku`, `region` | (snapshot_ts, sku, region) | APPEND | ~480 строк |
| `RAW_OZON_SEARCH_POSITIONS` | момент × sku × запрос | `snapshot_date` | `sku` | (snapshot_date, sku, query) | APPEND | зависит от числа запросов |

Retention: `RAW_OZON_PRICES` и `RAW_OZON_PUBLIC_SHELF` — 400 дней (почасовые, растут быстро).
Остальные — бессрочно.

Зависимости `ozon_raw`: **нет ни одной.** Пишется только загрузчиками из API.

## Слой `ozon_stg`

| Таблица | Grain | Partition | Cluster | Источник | Зависимости |
| --- | --- | --- | --- | --- | --- |
| `STG_OZON_FINANCE_ACCRUAL` | accrual × type × sku | `event_date` | `type_id`, `internal_sku` | `RAW_OZON_FINANCE_ACCRUAL` | + `evetis_ref.REF_SKU_CHANNEL_MAP` |
| `STG_OZON_MARKETING_COST` | дата × тип × campaign/sku | `date` | `marketing_cost_type` | `STG_OZON_FINANCE_ACCRUAL`, `RAW_OZON_ADS_EXPENSE_DAILY` | — |
| `STG_OZON_ADS_SKU_DAILY` | дата × campaign × sku | `date` | `internal_sku` | `RAW_OZON_ADS_SKU_DAILY` | + `REF_SKU_CHANNEL_MAP` |
| `STG_OZON_ORDERS` | posting × sku | `order_date` | `internal_sku` | `RAW_OZON_POSTINGS_FBO` | + `REF_SKU_CHANNEL_MAP` |
| `STG_OZON_PRICE_EVENTS` | сделка × sku | `event_date` | `internal_sku` | `RAW_OZON_FINANCE_ACCRUAL` | + `REF_SKU_CHANNEL_MAP` |

`STG_OZON_FINANCE_ACCRUAL` обязана разбирать `unit_number` по типу операции:
типы 41 и 54 → `campaign_id`; категория `POSTING` → `posting_number`; `ITEM` → ссылка на заказ.

## Слой `ozon_mart`

| Таблица | Grain | Partition | Cluster | Зависимости |
| --- | --- | --- | --- | --- |
| `MART_OZON_SKU_DAILY` | дата × internal_sku | `date` | `internal_sku` | `ozon_stg.*` + `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` |
| `MART_OZON_PORTFOLIO_DAILY` | дата | `date` | — | `MART_OZON_SKU_DAILY` |
| `MART_OZON_CAMPAIGN_DAILY` | дата × campaign_id | `date` | `campaign_id` | `ozon_stg.STG_OZON_MARKETING_COST` |
| `V_OZON_CPC_RECONCILIATION` | дата | — | — | `STG_OZON_MARKETING_COST` + `RAW_OZON_ADS_EXPENSE_DAILY` |

`MART_OZON_SKU_DAILY` разделяет `ACTUAL_REALIZED_ECONOMICS` и `FORWARD_UNIT_ECONOMICS`
согласно `data/economics_semantics_contract.csv`. `NOT_PROVEN` компоненты остаются `NULL`.

## Граф зависимостей

```
Ozon Seller API ─┐
Performance API ─┼→ ozon_raw ─→ ozon_stg ─→ ozon_mart
публичная карточка ┤              ↑
Sellmonitor      ─┘              │
                    evetis_ref ──┘   (только справочники)

wb_raw ─→ wb_stg ─→ wb_mart          НИ ОДНОЙ СТРЕЛКИ В ozon_*
```

Проверено: ни одна проектируемая таблица Ozon не имеет зависимости от `wb_raw` или `wb_mart`.

## Ключевые правила загрузки

- `bid`, `weeklyBudget`, `dailyBudget`, `budget` делить на 10⁶ **при загрузке**,
  в BigQuery хранить рубли. К статистике расхода делитель **не применять**.
- `color_index` нормализовать: `COLOR_INDEX_RED` и `RED` — одно значение.
- Схему Performance API не валидировать строго: в ответе 25 полей против 18 объявленных.
- Дедупликация — по логическому PK, при MERGE брать последнюю версию по времени извлечения.
- Окна перезабора: начисления 14 дней, заказы 30 дней (статус меняется после создания).
