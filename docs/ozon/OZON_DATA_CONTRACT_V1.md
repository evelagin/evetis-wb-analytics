# OZON_DATA_CONTRACT_V1

Версия 1, 2026-09-01. Домен **Ozon**. Подчиняется `architecture/MARKETPLACE_ISOLATION.md`.
Всё подтверждено вызовами на Stage 1.5–2.1, если не помечено иначе.

## Правила домена

- Денежные единицы: **рубли** во всех finance-полях и в статистике рекламы;
  **микрорубли, ÷10⁶** только в `weeklyBudget`, `dailyBudget`, `budget`, `bid` Performance API.
  Доказано спецификацией и сверкой с фактическим расходом.
- Валюта `RUB` — проверено на 1 702 начислениях.
- Operational timestamps — `Europe/Moscow`, ISO-8601 со смещением. Ответы API приходят в UTC.
- Значения `0`, `NULL`, `NOT_AVAILABLE`, `NOT_APPLICABLE`, `NOT_PROVEN`, `COGS_MISSING`
  различаются. Автоматический `IFNULL(...,0)` запрещён на всех слоях.

## Реестр сущностей

| # | Сущность | Домен | Endpoint / источник | Grain | Natural key |
| --- | --- | --- | --- | --- | --- |
| 1 | `product_reference` | `evetis_ref` | справочник EVETIS | `internal_sku` | `internal_sku` |
| 2 | `sku_channel_map` | `evetis_ref` | Seller API `/v3/product/list`, `/v3/product/info/list` | `internal_sku` × marketplace × marketplace_sku | (marketplace, marketplace_sku, valid_from) |
| 3 | `catalog` | `ozon_raw` | `/v3/product/list` + `/v3/product/info/list` | снимок × sku | (snapshot_date, sku) |
| 4 | `seller_price_snapshot` | `ozon_raw` | `/v5/product/info/prices` | снимок × offer_id | (snapshot_ts, offer_id) |
| 5 | `stock_snapshot` | `ozon_raw` | `/v1/analytics/stocks` | снимок × sku × склад | (snapshot_date, sku, warehouse_id) |
| 6 | `orders_fbo` | `ozon_raw` | `/v3/posting/fbo/list` | posting × sku | (posting_number, sku) |
| 7 | `finance_accrual` | `ozon_raw` | `/v1/finance/accrual/by-day`, `/accrual/postings` | accrual × type × sku | (accrual_id, type_id, sku) |
| 8 | `marketing_cost` | `ozon_stg` | производная от 7 и 10 | дата × тип × campaign/sku | (date, cost_type, campaign_id, sku) |
| 9 | `campaign` | `ozon_raw` | `GET /api/client/campaign` | снимок × campaign_id | (snapshot_date, campaign_id) |
| 10 | `campaign_daily` | `ozon_raw` | `statistics/expense` + `statistics/daily` | дата × campaign_id | (date, campaign_id) |
| 11 | `campaign_sku` | `ozon_raw` | `/campaign/{id}/v2/products` | снимок × campaign × sku | (snapshot_date, campaign_id, sku) |
| 12 | `campaign_sku_daily` | `ozon_raw` | асинхронный отчёт `POST /api/client/statistics` | дата × campaign × sku | (date, campaign_id, sku) |
| 13 | `cogs_effective` | `evetis_ref` | `V_PRODUCT_COGS_EFFECTIVE` | internal_sku × интервал | (internal_sku, effective_from) |
| 14 | `public_shelf_snapshot` | `ozon_raw` | публичная карточка, виджет `webPrice` | момент × sku × регион | (snapshot_ts, sku, region) |
| 15 | `search_position_snapshot` | `ozon_raw` | Sellmonitor MCP | момент × sku × запрос | (snapshot_date, sku, query) |

## Время, единицы, пагинация, частота

| # | Event time | TZ ответа | Monetary unit | Pagination | Cadence | Historical depth |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `active_from`/`active_to` | MSK | н/п | нет | по изменению | SCD2 |
| 2 | `valid_from`/`valid_to` | MSK | н/п | нет | 1/сутки | SCD2 |
| 3 | `updated_at` | UTC | руб | `last_id` | 1/сутки | только снимки вперёд |
| 4 | момент снимка | — | руб | `cursor` | **1/час** | только снимки вперёд |
| 5 | момент снимка | — | н/п | нет | 1/сутки | **ретроспективы нет** |
| 6 | `created_at` | UTC | руб | `cursor` + `has_next`, `limit` ≤ 100 | 1/сутки | полная |
| 7 | `date`, бизнес-дата | без tz, московская | руб | `last_id`, 1 день на запрос | 1/сутки | 92 дня проверено без пропусков |
| 8 | дата начисления | MSK | руб | — | 1/сутки | как у 7 |
| 9 | `updatedAt` | UTC | **микрорубли** | нет | 1/сутки | только снимки |
| 10 | дата | MSK | **руб** | нет | 1/сутки | полная |
| 11 | момент снимка | — | `bid` микрорубли, `targetCir` % | `page`+`pageSize` | 1/сутки | только снимки |
| 12 | дата | MSK | руб, с НДС | ZIP с CSV на кампанию | 1/сутки | ≤ 62 дня на отчёт |
| 13 | `effective_from`/`to` | MSK | руб | нет | по изменению | полная |
| 14 | момент снимка | MSK | руб | нет | **1/час** | **SNAPSHOT SERIES** |
| 15 | момент снимка | MSK | н/п | `limit` | 1/сутки | **SNAPSHOT SERIES**, окно 14 дней |

## Дедупликация, поздние данные, сверка

| # | Dedup | Late arrival | Reconciliation |
| --- | --- | --- | --- |
| 2 | по натуральному ключу | — | **20/20 товаров Ozon резолвятся без обращения к `wb_raw`** |
| 3 | (snapshot_date, sku) | — | `total` из `/v3/product/list` = число строк |
| 6 | ключ + последняя версия по `snapshot_ts` | статус меняется после создания, перезабирать окно 30 дней | сумма позиций = число товаров в отправлениях |
| 7 | по натуральному ключу | окно перезабора 14 дней | **тип 41 = сумма Performance CPC, расхождение 0,00 ₽ на 90 днях** |
| 8 | по натуральному ключу | как у 7 | сумма = сумма типов 41, 54, 116 |
| 10 | (date, campaign_id) | — | сумма расхода = тип 41 начислений |
| 12 | (date, campaign_id, sku) | — | **сумма по SKU = расход кампании, расхождение 0,01 ₽** |
| 13 | (internal_sku, effective_from) | — | покрытие 20/20 SKU |

## Известные ограничения по сущностям

**2 `sku_channel_map`.** Равенство Ozon `offer_id` и WB `nm_id` — это
`EVETIS_CURRENT_CATALOG_INVARIANT`, **не** правило идентичности площадок.
Идентификатор Ozon хранится явной строкой и никогда не выводится из WB-данных.

**3 `catalog`.** `price_indexes` = `null` у товаров без остатка. `color_index` приходит
как `COLOR_INDEX_RED` в `/v3` и как `RED` в `/v5` — нормализовать при загрузке.

**4 `seller_price_snapshot`.** Цены на полке здесь **нет**. `net_price` — себестоимость
из кабинета Ozon, только для сверки; source of truth по себестоимости — `evetis_ref`.

**5 `stock_snapshot`.** Исторический остаток `NOT_AVAILABLE`. Восстановление задним
числом из текущего остатка **запрещено**.

**6 `orders_fbo`.** `/v2/posting/fbo/list` **запрещён**, отключён 31.08.2026.
Фактической даты доставки в API нет, есть только плановое окно `client_delivery_date_*`.

**7 `finance_accrual`.** **`unit_number` многозначен**: для типов 41 и 54 это `campaign_id`,
для категории `POSTING` — `posting_number`, для `ITEM` — ссылка на заказ.
Трактовать строго по типу операции. `/v3/finance/transaction/*` **запрещены**, отключение 08.09.2026.
`/v1/finance/realization/by-day` — 403, требует Premium Plus.

**8 `marketing_cost`.** Тип 116 «Сбор первых отзывов» в Performance API **отсутствует**,
виден только в финансах. `REF_VK_PRODUCT_COGS` и `REF_BLOGGER_PRODUCT_COGS` = **NULL**,
статус `NOT_PROVEN`, нулём не заменяются.

**9 `campaign`.** **Дрейф схемы**: в ответе 25 полей против 18 объявленных,
`paymentType` в схеме против `PaymentType` в ответе. Валидацию по схеме строить нельзя.
`expenseStrategy` — исторический enum, `dailyBudget` помечен `deprecated`;
расход реально ограничивает `weeklyBudget`.

**10 `campaign_daily`.** Формат ответа — **CSV с `;`** и десятичной запятой,
заголовок `Accept: application/json` игнорируется. Три денежные колонки — «Расход»,
«Расход бонусов», «Расход с абонентского счета» — хранить раздельно.

**11 `campaign_sku`.** `bid` и `targetCir` взаимоисключающи: у стратегии «Целевой расход»
`bid` = `NOT_APPLICABLE`. `topPosition` = `NOT_APPLICABLE`, поле существует только
для стратегии «Вывод в топ». Не-SKU кампании отвечают HTTP 400.

**12 `campaign_sku_daily`.** Асинхронный: `NOT_STARTED` → `OK`/`ERROR`, формирование
занимает минуты. Лимиты: ≤ 62 дня и ≤ 10 кампаний на отчёт, 1 одновременная выгрузка
на аккаунт, 2 000 выгрузок в сутки. Ответ — ZIP.

**13 `cogs_effective`.** **Forward-fill запрещён.** Нет действующего интервала на дату
события → `COGS_MISSING`, расчёт прибыли по строке останавливается. В периоде
6 из 20 SKU меняли себестоимость 2026-08-01.

**14 `public_shelf_snapshot`.** Зависит от региона и сессии. У товара без остатка
публичной карточки нет. Sellmonitor как первичный источник полки **отвергнут**:
не совпадает ни с полкой, ни с ценой по карте.
Отдельно: фактическая цена покупателя в момент **сделки** доступна ретроспективно —
`sale_price` в `finance_accrual`. Это другая сущность, не снимок полки.

**15 `search_position_snapshot`.** `/v1/analytics/product-queries` без подписки Premium
отдаёт пустой результат при HTTP 200.
