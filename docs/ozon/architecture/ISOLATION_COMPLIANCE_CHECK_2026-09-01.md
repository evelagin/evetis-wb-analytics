# Проверка соответствия правилу Marketplace Isolation

Дата: 2026-09-01. Объект проверки: всё, что построено на этапах Stage 1.5–2.

## Итог: **1 нарушение**

| # | Проверка | Результат |
| --- | --- | --- |
| 1 | нет чтения из `wb_raw` | **FAIL** — см. V-01 |
| 2 | нет чтения из `wb_mart` | PASS |
| 3 | только разрешённые справочники из `evetis_ref` | PASS — использован `V_PRODUCT_COGS_EFFECTIVE`, это history себестоимости |
| 4 | нет записи в датасеты WB | PASS — за весь аудит в BigQuery не выполнено ни одной операции записи |
| 5 | факты Ozon имеют свой grain и натуральные ключи | PASS — см. ниже |

## V-01. Домен Ozon читает `wb_raw.REF_SKU_MASTER`

**Где.** Связка Ozon `offer_id` → внутренний артикул строилась запросом к
`wb_raw.REF_SKU_MASTER` (Stage 1.5). Зафиксировано в `LOG.md:252` и в
`data/source_contract.csv`, строка `mapping`.

**Почему это нарушение.** `REF_SKU_MASTER` лежит в `wb_raw`, а не в `evetis_ref`.
По правилу изоляции домен Ozon не имеет права читать из `wb_raw` ни при каких условиях.

**Почему нарушение появилось.** На момент Stage 1.5 правило ещё не было сформулировано,
а таблица оказалась единственным местом, где есть связь `nm_id ↔ internal_sku`.

**Существенная деталь.** `REF_SKU_MASTER` — таблица **смешанная**. В ней одновременно:

| Marketplace-независимое | WB-специфичное | Операционные флаги WB |
| --- | --- | --- |
| `internal_sku`, `product_name_short`, `product_name_full`, `category`, `line`, `product_type`, `is_bundle`, `brand`, `volume_ml`, `barcode`, `status`, `active` | `nm_id`, `wb_vendor_code`, `wb_subject_id`, `wb_subject_name` | `include_in_pnl`, `include_in_ads_analysis`, `include_in_supply_plan`, `include_in_stock_alerts` |

Колонка `marketplace` в ней принимает единственное значение `WB` (25 строк, 25 SKU).
То есть таблица по факту принадлежит домену WB и просто содержит внутри себя общий справочник.

Поэтому «перенести таблицу в `evetis_ref`» — неверное решение: вместе с общими атрибутами
в общий слой уехали бы WB-специфичные поля и операционные флаги WB.

## Предлагаемое устранение

Разделить, а не переносить. Проект это уже предвидел:
`docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md:27` прямо говорит, что при появлении Ozon
«создаётся отдельный `REF_SKU_CHANNEL_MAP`, таблицы себестоимости при этом не меняются».

```
evetis_ref.REF_PRODUCT_MASTER     internal_sku → название, категория, линия,
                                  тип, is_bundle, бренд, объём, штрихкод
                                  (marketplace-независимые атрибуты)

evetis_ref.REF_SKU_CHANNEL_MAP    internal_sku × marketplace × marketplace_sku
                                  ключ: (marketplace, marketplace_sku)
                                  WB   → nm_id
                                  OZON → ozon_sku, offer_id

wb_raw.REF_SKU_MASTER             остаётся у WB: wb_vendor_code, wb_subject_*,
                                  include_in_* — операционные флаги WB
```

После этого домен Ozon читает только `evetis_ref` и правило соблюдается.

**Важная оговорка про природу связки.** То, что на Ozon `offer_id` численно равен
WB `nm_id`, — это **операционное соглашение EVETIS**, а не факт маркетплейса.
В `REF_SKU_CHANNEL_MAP` идентификатор Ozon должен быть записан **явной строкой**,
а не выводиться из WB-данных. Иначе домен Ozon снова окажется зависим от WB,
только неявно. Проверено на 20 из 20 товаров — но это проверка соглашения,
а не основание строить на нём зависимость.

## Grain и натуральные ключи фактов Ozon (проверка 5)

| Датасет | Grain | Натуральный ключ | Пересечение с WB |
| --- | --- | --- | --- |
| `finance_accrual_fact` | операция × тип × SKU | `operation_id` (accrual_id Ozon) | нет |
| `marketing_cost_fact` | дата × тип затрат × кампания/SKU | дата + тип + campaign_id | нет |
| `performance_campaign_daily` | дата × кампания | `campaign_id` Ozon Performance | нет |
| `orders_fbo_fact` | отправление × SKU | `posting_number` Ozon | нет |
| `price_history_from_transactions` | дата × SKU × сделка | дата + ozon_sku | нет |
| `campaign_registry_current` | кампания | `campaign_id` | нет |

Ни один ключ не заимствован из WB. Пересечение только через `internal_sku`,
и только как справочная связь, а не как источник факта.

## Что не нарушено, хотя выглядит похоже

`sql/ref/pr_ref_cogs_history.sql` упоминает Ozon — но только в комментарии о том, что
движения товара через Ozon в исторической реконструкции не восстанавливаются.
Это не зависимость, а оговорка об ограничении. Менять не нужно.

## Статус

V-01 внесено в `data/discrepancies.csv` как **D024**.
Само устранение — предмет проектирования Ozon BigQuery layer, а не текущей правки.
**Ничего в BigQuery не менялось.**
