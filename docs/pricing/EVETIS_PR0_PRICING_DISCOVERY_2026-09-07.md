# EVETIS — Stage PR-0
# Pricing Intelligence & Repricer Discovery (Wildberries + Ozon)

Дата: 2026-09-07
Статус: DISCOVERY ONLY. Production не изменялся.

```
CODE CHANGES:            0
PRODUCTION DATA CHANGES: 0
MARKETPLACE PRICE CHANGES: 0
API MUTATIONS:           0
Единственный артефакт:   этот файл
```

Легенда классификации:
- **[FACT]** — подтверждено кодом репозитория, живыми данными BigQuery или официальной документацией маркетплейса.
- **[INFERENCE]** — логический вывод из фактов.
- **[RECOMMENDATION]** — предлагаемая архитектура, ещё не согласованная.
- **[NON-AUTH]** — источник неофициальный.

---

## EXECUTIVE VERDICT

### Может ли EVETIS построить собственный репрайсер?

**YES, WITH LIMITATIONS.**

Три утверждения, которые определяют всё остальное:

**1. Экономический фундамент на Ozon уже построен, на WB — нет.** [FACT]
`ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` уже содержит `break_even_price_expected/worst`,
`target_price_{10,15,20,25}pct_*`, коридоры логистики best/expected/worst и гейты
`agent_decision_gate = DECISIONS_ALLOWED`, `agent_shadow_gate = SHADOW_ALLOWED`,
`agent_write_gate = HARDCODED_FALSE_NOT_DERIVABLE_FROM_DATA_REQUIRES_SEPARATE_OWNER_ACK`
для 20 из 20 SKU. Это фактически готовый price floor model для Ozon.
На WB форвардной экономики нет вообще: `wb_mart.V_ADS_SKU_ECONOMIC_LIMITS` считает
ретроспективу по факту реализации, а не «сколько я заработаю, если поставлю цену X».

**2. У WB в системе нет НИ ОДНОГО поля с текущей ценой продавца.** [FACT]
Ни одной таблицы, ни одного пайплайна, ни одного вызова `discounts-prices-api.wildberries.ru`
в репозитории. Цена WB известна только post-factum и только по тем SKU, у которых была продажа
(5–12 nm_id в сутки из 25). Это P0-блокер: репрайсер не может управлять величиной,
которую не наблюдает.

**3. Цена покупателя pre-sale недоступна ни на WB, ни на Ozon. Структурно.** [FACT]
- WB: `GET /api/v2/list/goods/filter` возвращает `price`, `discount`, `discountedPrice`,
  `clubDiscount`, `clubDiscountedPrice` — и ни одного поля СПП.
- Ozon: `price.marketing_price` удалён из `/v5/product/info/prices` 12.11.2025;
  в актуальной схеме ответа его нет.

Это не пробел нашей архитектуры — это осознанная политика обеих площадок.
Любой репрайсер EVETIS будет управлять **ценой продавца** и наблюдать
**цену покупателя с лагом 0–3 суток и разреженным покрытием**.

### Насколько велика дистанция

| Цель | Дистанция |
|---|---|
| A. Price monitoring | Ozon — почти есть (нужен intraday). WB — с нуля. **Ближайший стейдж.** |
| B. Price recommendation | Ozon — 1 стейдж. WB — 2 стейджа (нужны тарифы WB). |
| C. Shadow repricer | 3–4 стейджа. |
| D. Controlled auto repricer | 5–6 стейджей. |
| E. Cross-market autonomous | 7–8 стейджей. |
| F. Profit-optimizing engine | 9+ стейджей, требует накопления истории цен ≥6 месяцев. |

### Открытие, которое перевешивает всю остальную инженерию

**[FACT]** Цена покупателя на WB составляет **38–70 % от цены покупателя на Ozon**
по одному и тому же физическому SKU (медиана ≈ 0,49). Данные: WB `finished_price`
за 14 суток против Ozon FBO posting `price_rub` за те же 14 суток.

| internal_sku | WB цена покупателя | Ozon цена покупателя | WB/Ozon |
|---|---|---|---|
| EVT-HC-AMBER-300 | 443 | 1151 | 0,38 |
| EVT-HC-HAND-300 | 372 | 947 | 0,39 |
| EVT-EP-ENZYME-75 | 485 | 1182 | 0,41 |
| EVT-HC-CHERRY-300 | 453 | 1101 | 0,41 |
| EVT-SET-4PC-ACNE | 1621 | 3627 | 0,45 |
| EVT-FC-ACNE-50 | 634 | 1304 | 0,49 |
| EVT-FT-ACNE-150 | 563 | 1154 | 0,49 |
| EVT-FT-MOIST-150 | 553 | 1113 | 0,50 |
| EVT-FS-ACNE-30 | 681 | 1258 | 0,54 |
| EVT-FS-MOIST-30 | 664 | 1208 | 0,55 |
| EVT-SET-TON-SER-CREAM-ACNE | 1223 | 1937 | 0,63 |
| EVT-SET-MOIST-TONIC-SERUM | 869 | 1299 | 0,67 |
| EVT-SET-SER-CREAM-ACNE | 1053 | 1576 | 0,67 |
| EVT-SET-HAND-CHERRY | 885 | 1267 | 0,70 |

**[INFERENCE]** Это либо крупнейшая недополученная прибыль на WB, либо крупнейший
недополученный объём на Ozon. Ни один из вариантов не был виден системе до сегодняшнего дня,
потому что домены WB и Ozon разделены и общей ценовой витрины не существует.
Разрыв такого масштаба нельзя закрывать автоматикой — сначала нужен ответ владельца,
какая из двух площадок оценена неверно.

---

## 1. CURRENT ARCHITECTURE

### 1.1 Что существует [FACT]

**Проект:** `project-fa311fc0-4d87-4781-986`, BigQuery location `EU`.

**Датасеты (8):**

| Dataset | Назначение | Объектов |
|---|---|---|
| `wb_raw` | RAW WB + справочник SKU + run-логи | 32 таблицы, 26 вью |
| `wb_mart` | FACT/MART/dashboard WB | 17 таблиц, 27 вью |
| `wb_ops` | Мониторинг, health, инциденты | 6 таблиц, 2 вью |
| `evetis_ref` | Кросс-канальные справочники (SKU-мэппинг, COGS, наборы) | 11 таблиц, 2 вью |
| `ozon_raw` | RAW Ozon | 15 таблиц |
| `ozon_mart` | Витрины Ozon, включая форвардную экономику | 11 вью |
| `ozon_stg` | Стейджинг Ozon | — |
| `evetis_communications` | Коммуникации | — |

**Слои исполнения:**
- Google Apps Script — WB loaders (orders, sales, finance, ads, stocks, supplies), ~75 файлов.
- Cloud Run + TypeScript (`cloud/src`) — WB stocks loader, MART builder.
- Cloud Run Job + Python (`pipelines/ozon/runtime`) — весь домен Ozon, 11 сущностей.
- Terraform (`infra/terraform`) — schedulers, IAM, service accounts, Secret Manager.
- Metabase — 57 карточек, 4 дашборда (живёт вне git, в репозитории только описание).

**Изоляция маркетплейсов [FACT]:** `pipelines/ozon/runtime/common.py` явно декларирует,
что домен Ozon не читает и не пишет `wb_raw`/`wb_mart`, а идентификаторы резолвит только
через `evetis_ref.REF_SKU_CHANNEL_MAP`. Любая кросс-маркетплейсная витрина обязана
соблюдать это правило.

### 1.2 Карта фактически работающих пайплайнов

Проверено по `wb_mart.V_DATA_FRESHNESS`, `wb_ops.OPS_PIPELINE_REGISTRY`,
`ozon_raw.OZON_INGESTION_RUNS` (не по наличию кода). [FACT]

#### Wildberries

| Pipeline | Source API | Destination | Каденс | Глубина | Статус |
|---|---|---|---|---|---|
| orders | `GET /api/v1/supplier/orders` | `RAW_WB_ORDERS` | почасовой (:31) | инкремент | OK, data_as_of 2026-09-06 |
| sales | `GET /api/v1/supplier/sales` | `RAW_WB_SALES_RETURNS` | почасовой (:22) | с 2026-03-30 | OK, data_as_of 2026-09-06 |
| finance | `POST /api/finance/v1/sales-reports/detailed` | `RAW_WB_FINANCE` | 3×/сут (07:26/12:24/18:36) | 210 388 строк | OK, лаг 2 сут |
| stocks_snapshot | `warehouse_remains`, `stocks-report/wb-warehouses` | `WB_STOCKS_SNAPSHOTS` | 06:30 МСК | snapshot-only | OK |
| stocks_cloudrun | тот же | `RAW_WB_STOCKS` | 06:30 МСК | snapshot-only | OK |
| ads_daily (+3 дочерних) | `adv/v1/*`, `adv/v3/fullstats` | `RAW_WB_ADV_*` | 05:00 МСК | инкремент | OK |
| ref_sync | Google Sheets | `REF_SKU_MASTER_DATA` | 07:30 МСК | — | OK |
| mart | BigQuery | `MART_SKU_DAILY` | 4×/сут (7,9,12,16) | — | OK |
| **prices** | **—** | **—** | **—** | **—** | **НЕ СУЩЕСТВУЕТ** |
| **tariffs** | **—** | **—** | **—** | **—** | **НЕ СУЩЕСТВУЕТ** |
| **promo calendar** | **—** | **—** | **—** | **—** | **НЕ СУЩЕСТВУЕТ** |

#### Ozon

Три Cloud Run Job'а с фиксированным `ENTITIES` (`infra/terraform/ozon_ingestion.tf`):

| Job | Сущности | Расписание |
|---|---|---|
| `ozon-runtime-fast` | stocks, fbo_postings | 07:00, 13:00, 19:00 |
| `ozon-runtime-daily` | catalog, **prices**, seller_info, finance_accrual, ads_campaigns, ads_expense_daily, ads_sku_daily, supplies | 06:30 |
| `ozon-runtime-weekly` | clusters | пн 05:00 |

| Pipeline | Source API | Destination | Каденс | Глубина | Статус |
|---|---|---|---|---|---|
| prices | `POST /v5/product/info/prices` | `RAW_OZON_PRICES`, `RAW_OZON_PRICE_COMMISSIONS` | 1×/сут | **6 снимков, с 2026-08-31** | OK, но см. 6.2 |
| catalog | `/v3/product/list` + `/v3/product/info/list` | `RAW_OZON_CATALOG` | 1×/сут | — | OK |
| stocks | `/v1/analytics/stocks` | `RAW_OZON_STOCKS` | 3×/сут | — | OK |
| fbo_postings | `/v3/posting/fbo/list` | `RAW_OZON_POSTINGS_FBO` | 3×/сут | 30 сут окно | OK |
| finance_accrual | `/v1/finance/accrual/by-day` | `RAW_OZON_FINANCE_ACCRUAL` | 1×/сут | 14 сут окно | OK |
| ads_* | Performance API | `RAW_OZON_ADS_*` | 1×/сут | 7 сут окно | OK |
| seller_info | `/v1/seller/info`, `/v1/rating/summary` | `RAW_OZON_SELLER_INFO` | 1×/сут | — | OK |
| **actions / promo** | **—** | **—** | **—** | **—** | **НЕ СУЩЕСТВУЕТ** |
| **pricing-strategy** | **—** | **—** | **—** | **—** | **НЕ СУЩЕСТВУЕТ** |

---

## 2. WILDBERRIES PRICING AUDIT

### 2.1 READ-методы категории «Цены и скидки» [FACT, официальная документация]

Хост: `https://discounts-prices-api.wildberries.ru`
Токен: категория **«Цены и скидки»** (у EVETIS такого токена нет — см. §19).

| Метод | Назначение |
|---|---|
| `GET /api/v2/list/goods/filter` | Все товары с ценами, `limit≤1000`, `offset` |
| `POST /api/v2/list/goods/filter` | Товары по списку `nmList` (1..1000) |
| `GET /api/v2/list/goods/size/nm` | Цены по размерам одного товара |
| `GET /api/v2/quarantine/goods` | Товары в ценовом карантине |
| `GET /api/v2/history/tasks` | Состояние обработанной загрузки |
| `GET /api/v2/history/goods/task` | Детали обработанной загрузки, ошибки по товарам |
| `GET /api/v2/buffer/tasks` | Необработанные загрузки (акции) |
| `GET /api/v2/buffer/goods/task` | Детали необработанных загрузок |

**Схема ответа `list/goods/filter`** (дословно из документации):

```json
{"data": {"listGoods": [{
  "nmID": 98486, "vendorCode": "07326060",
  "sizes": [{"sizeID": 3123515574, "price": 500,
             "discountedPrice": 350, "clubDiscountedPrice": 332.5,
             "techSizeName": "42"}],
  "currencyIsoCode4217": "RUB",
  "discount": 30, "clubDiscount": 5,
  "editableSizePrice": true,
  "wholesaleDiscountThreshold": [{"minQuantity": 10, "wholesaleDiscount": 10, "level": 1}],
  "isBadTurnover": true }]}}
```

**Лимиты запросов** (на аккаунт, вся категория «Цены и скидки»):

| Тип токена | Период | Лимит | Интервал | Burst |
|---|---|---|---|---|
| Personal / Service / Base with secret | 6 с | 10 запросов | 600 мс | 5 |
| Base | 1 ч | 4 запроса | 15 мин | 1 |

**[INFERENCE]** При 25 SKU полный снимок цен = **один** POST-запрос (`nmList` до 1000).
Опрос раз в 5 минут = 288 запросов/сутки против лимита 10 запросов/6 с ≈ 144 000/сутки.
Запас по лимиту — 500×. Частота ограничена не API, а полезностью.

### 2.2 Тарифы WB — существуют и не используются [FACT]

Хост: `https://common-api.wildberries.ru`. Токен **любой категории**.

| Метод | Что даёт | Лимит |
|---|---|---|
| `GET /api/v1/tariffs/commission` | Комиссия WB по предметам: `kgvpMarketplace` (FBO), `kgvpSupplier`, `kgvpBooking`, `kgvpPickup`, `paidStorageKgvp` | 1 req/min |
| `GET /api/v1/tariffs/box` | `boxDeliveryBase`, `boxDeliveryLiter`, `boxStorageBase`, `boxStorageLiter` + коэффициенты | 60 req/min |
| `GET /api/v1/tariffs/pallet` | То же для паллет | 60 req/min |
| `GET /api/v1/tariffs/return` | Стоимость возврата товара продавцу | 60 req/min |
| `GET /api/tariffs/v1/acceptance/coefficients` | **Временно отключён с 15.08.2026** | — |

**[INFERENCE]** Это точный WB-аналог блока `commissions` из Ozon `/v5/product/info/prices`.
Именно его отсутствие делает невозможным расчёт WB price floor вперёд.
Без него `absolute_floor` на WB можно оценить только по исторической реализации,
что нарушает принцип «floor исходит из допустимой экономики».

### 2.3 WRITE-методы (документированы, НЕ вызывались) [FACT]

| Метод | Назначение | Ограничения |
|---|---|---|
| `POST /api/v2/upload/task` | Установить цену и/или скидку | ≤1000 товаров; `price` и `discount` не могут быть пустыми оба |
| `POST /api/v2/upload/task/size` | Цены по размерам | требует `editableSizePrice: true` |
| `POST /api/v2/upload/task/club-discount` | Скидка WB Club | — |
| `POST /api/discounts-prices/v1/upload/task/b2b/wholesale` | Оптовые скидки B2B | — |
| `POST /api/v1/calendar/promotions/upload` | Добавить товар в акцию (`dp-calendar-api`) | — |

Тело запроса: `{"data": [{"nmID": 123, "price": 999, "discount": 30}]}`

**Ценовой карантин [FACT]:** «If the new price with discount is at least 3 times less
than the previous one, the price will go into price quarantine and will not change.»
Ошибка возвращается не в ответе на upload, а в методах статуса загрузки.
Вывод товара из карантина — только вручную в кабинете либо повторной корректной загрузкой.

**Асинхронная модель [FACT]:** upload → `uploadID` → опрос `GET /api/v2/history/tasks`.

| Статус | Значение |
|---|---|
| 1 | загрузка через календарь акций, скидка применится на старте акции |
| 2 | **не существует** |
| 3 | обработано без ошибок, цены обновлены |
| 4 | отменено |
| 5 | обработано, часть товаров с ошибками |
| 6 | обработано, все товары с ошибками |

Ответ `history/tasks` содержит `uploadDate` и **`activationDate`** — момент фактической
активации цены, отличный от момента подтверждения. Это и есть точка, вокруг которой
строится верификация записи.

### 2.4 Календарь акций WB [FACT]

Хост `https://dp-calendar-api.wildberries.ru`, токен «Цены и скидки».

| Метод | Что даёт |
|---|---|
| `GET /api/v1/calendar/promotions` | `id`, `name`, `startDateTime`, `endDateTime`, `type` |
| `GET /api/v1/calendar/promotions/details` | Описание, условия акции |
| `GET /api/v1/calendar/promotions/nomenclatures` | Товары, доступные для участия |
| `POST /api/v1/calendar/promotions/upload` | Добавить товар в акцию (WRITE) |

**[INFERENCE]** Участие в акции WB — состояние, при котором цена управляется календарём,
а не методом `upload/task`. Репрайсер обязан считать это состояние блокирующим.

### 2.5 Риск миграции: Order Feed заменяет Orders и Sales [FACT]

Из официального дайджеста WB API за август 2026:

> «The Order Feed report ... is a replacement for the Orders and Sales methods:
> they keep working, but they will be disabled — the date will be announced in advance.»

Новый метод: `POST /api/analytics/v1/order-feed` (`seller-analytics-api`), лимит **1 запрос/мин**,
окно ≤31 суток, snapshot-based пагинация по `snapshotTime`, ключ `srid`, статусы
`created/buyout/cancel/return/returnDefective`.

Поле цены в нём **одно**:
> «`sellerPrice` is the seller's price with the seller's discount, excluding the WB Club discount
> and the B2B wholesale discount»

**[INFERENCE] Это критический риск именно для ценообразования.** Сегодня `supplier/sales`
отдаёт `spp` и `finishedPrice` — единственный существующий источник цены покупателя WB.
В Order Feed этих полей нет. Отключение старых методов уничтожит post-sale наблюдаемость СПП,
если к тому моменту не будет альтернативы (детализация реализации `spp_percent` останется,
но с лагом 2–3 суток вместо часа).

---

## 3. СПП WILDBERRIES — ОТДЕЛЬНЫЙ РАЗБОР

### 3.1 Можно ли получить текущую СПП до заказа?

**НЕТ. [FACT]**

Официального Seller API метода не существует. Проверено:
- полный перечень методов категории «Цены и скидки» (§2.1) — поля СПП нет;
- `/api/v1/calendar/promotions/*` — СПП не возвращает;
- методы `final-price` (`originalFinalPrice` — сумма, начисленная покупателю со всеми скидками
  и кешбэком) существуют **только для DBS и «Самовывоз из магазина»**, EVETIS работает по FBO;
- официальный форум WB API, тема «Получение СПП»: вопрос задан, рабочего способа
  через Seller API в ответах нет [NON-AUTH: форум].

### 3.2 Что можно получить вместо СПП

| Величина | Метод | Момент | Есть у EVETIS |
|---|---|---|---|
| Цена продавца до скидки | `list/goods/filter`.`price` | pre-sale | НЕТ |
| Скидка продавца, % | `list/goods/filter`.`discount` | pre-sale | НЕТ |
| Цена после скидки продавца | `list/goods/filter`.`discountedPrice` | pre-sale | НЕТ |
| Скидка WB Club, % | `list/goods/filter`.`clubDiscount` | pre-sale | НЕТ |
| Цена для WB Club | `list/goods/filter`.`clubDiscountedPrice` | pre-sale | НЕТ |
| **СПП, %** | `supplier/sales`.`spp` | **post-sale, ~1 ч** | **ДА** |
| **Цена покупателя** | `supplier/sales`.`finishedPrice` | **post-sale, ~1 ч** | **ДА** |
| СПП, % (бухг.) | детализация реализации `ppvz_spp_prc` → `spp_percent` | post-sale, 2–3 сут | ДА |
| Цена реализации | `retail_price_withdisc_rub` | post-sale, 2–3 сут | ДА |

### 3.3 `ppvz_spp_prc` / `spp_percent` — разбор [FACT]

- **Источник:** детализация к отчёту реализации, `POST /api/finance/v1/sales-reports/detailed`,
  колонка `spp_percent` в `wb_raw.RAW_WB_FINANCE` (тип STRING).
- **Смысл:** процент скидки постоянного покупателя, применённой к конкретной строке реализации.
- **Когда появляется:** только вместе со строкой реализации.
- **Заполненность:** из 40 385 строк `supplier_oper_name = 'Продажа'` — 40 203 имеют `spp_percent > 0`
  (99,5 %). Средняя СПП по строкам продаж = **15,65 %**. Максимум по всей таблице — 92,86 %.
  Во всех остальных типах операций (логистика, хранение, возмещения — 170 003 строки) поле = 0.
- **Лаг:** медиана 2 суток, среднее 3,2 суток между `_rr_date` и `loaded_at` (окно с 2026-07-01).
- **Пригодность:** для **historical analysis — да**. Для **monitoring — нет**:
  привязана к факту продажи, покрывает только проданные SKU, приходит с лагом суток.

### 3.4 Разграничение понятий — обязательное [FACT]

Смешивать нельзя:

| Понятие | Кто управляет | Кто платит | Видно pre-sale |
|---|---|---|---|
| `discount` — скидка продавца | продавец | продавец | да (через API цен) |
| `clubDiscount` — скидка WB Club | **продавец** | продавец | да (через API цен) |
| СПП — скидка постоянного покупателя | **WB** | WB | **нет** |
| Скидка акции WB | WB задаёт условия, продавец соглашается | продавец | да (календарь) |
| «Компенсация скидки по программе лояльности» | WB | WB | нет, только в реализации |

**[FACT]** Последняя строка наблюдаема: в `RAW_WB_FINANCE` есть тип операции
«Компенсация скидки по программе лояльности», 219 строк. Это единственный явный
marketplace-funded subsidy, который WB раскрывает.

**[INFERENCE]** `clubDiscount` — это НЕ СПП. Он задаётся продавцом через
`POST /api/v2/upload/task/club-discount` и оплачивается продавцом. Трактовать
`clubDiscountedPrice` как «цену покупателя» нельзя.

---

## 4. OZON PRICING AUDIT

### 4.1 READ-методы [FACT]

Хост `https://api-seller.ozon.ru`, аутентификация `Client-Id` + `Api-Key`.
Лимит: **50 запросов/сек на один Client-Id**.

| Метод | Используется EVETIS |
|---|---|
| `POST /v5/product/info/prices` | **ДА**, 1×/сут |
| `POST /v3/product/list`, `/v3/product/info/list` | ДА |
| `POST /v1/analytics/stocks` | ДА |
| `POST /v3/posting/fbo/list` | ДА, 3×/сут |
| `POST /v1/finance/accrual/by-day` | ДА |
| `GET /v1/actions` | НЕТ |
| `POST /v1/actions/products` | НЕТ |
| `POST /v1/actions/candidates` | НЕТ |
| `POST /v1/actions/discounts-task/list` | НЕТ |
| `POST /v1/pricing-strategy/*` (12 методов) | НЕТ |
| `POST /v1/seller-actions/*` (17 методов) | НЕТ |
| `POST /v1/product/action/timer/status` | НЕТ |

### 4.2 Схема цен `/v5/product/info/prices` [FACT]

Блок `price` по документации: `price`, `old_price`, `min_price`, `net_price`,
`currency_code`, `vat`, `auto_action_enabled`, `auto_add_to_ozon_actions_list_enabled`.

**Проверка живыми данными** (`RAW_OZON_PRICES`, снимок 2026-09-07, 20/20 SKU non-null):
API дополнительно возвращает `marketing_seller_price` и `retail_price` — они заполнены
на 100 % строк, хотя в опубликованном примере схемы отсутствуют. [FACT]

`marketing_price` (цена покупателя) **отсутствует**. По независимым источникам поле
отключено 12.11.2025 [NON-AUTH: cleverence.ru, marketparser.ru]; в актуальной схеме
метода его нет [FACT: документация].

Блок `commissions` разобран нашим загрузчиком в 16 известных компонент
(`COMMISSION_MAP` в `pipelines/ozon/runtime/entities.py`) плюс длинная проекция
`RAW_OZON_PRICE_COMMISSIONS`. Неизвестных компонент на 2026-09-07: **0**. [FACT]

Блок `marketing_actions` возвращает `ozon_actions_exist`, `current_period_from/to`
и массив `actions[] {title, value, date_from, date_to}`.
**Наш загрузчик сохраняет только `ozon_actions_exist`.** Детали акций — название,
величина скидки, период — теряются. [FACT — пробел]

### 4.3 WRITE-методы (документированы, НЕ вызывались) [FACT]

`POST /v1/product/import/prices` — ≤1000 товаров за запрос.

**Ключевое ограничение:** «Цену каждого товара можно обновлять **не больше 10 раз в час**.»

Поля запроса: `offer_id`/`product_id`, `price`, `old_price`, `min_price`, `net_price`,
`currency_code`, `vat`, `quant_size`, `auto_action_enabled`,
`auto_add_to_ozon_actions_list_enabled`, `price_strategy_enabled`,
`min_price_for_auto_actions_enabled`, `manage_elastic_boosting_through_price`.

Ответ: `result[] {product_id, offer_id, updated: bool, errors[]}` — синхронный,
пер-товарный результат внутри общего 200.

**Валидация цен Ozon (полный перечень кодов ошибок) [FACT]:**

| Код | Правило |
|---|---|
| `old_price_less_than_price` | `old_price` должна быть **выше** `price` |
| `discount_for_low_price_is_too_small` | цена после скидки ≤400 ₽ → разница > 20 ₽ |
| `discount_for_average_price_is_too_small` | цена 400–10 000 ₽ → разница > 5 % |
| `discount_for_top_price_is_too_small` | цена >10 000 ₽ → разница > 500 ₽ |
| `discount_too_big` | разница между `old_price` и `price` < 90 % |
| `price_out_of_range` | цена вне порогов категории |
| `price_negative` / `price_is_negative` | отрицательная / отсутствующая цена |
| `min_auto_price_too_big` | цена после автоприменения акций должна быть **меньше** вашей цены |

**[INFERENCE]** Эти правила образуют жёсткий внешний коридор, который репрайсер обязан
воспроизвести на своей стороне до отправки запроса. Иначе каждый третий вызов будет 400.

Сопутствующие методы: `POST /v1/product/action/timer/update` и
`POST /v1/product/action/timer/status` — таймер актуальности минимальной цены.

---

## 5. OZON MARKETPLACE DISCOUNTS — ОТДЕЛЬНЫЙ РАЗБОР

### 5.1 Матрица понятий Ozon

| Понятие | Управляет | Видно pre-sale | Видно post-sale | Есть у нас |
|---|---|---|---|---|
| `price` — цена продавца | продавец | да | да (posting) | да |
| `old_price` — зачёркнутая цена | продавец | да | да | да |
| `min_price` — минимальная цена | продавец | да | — | да |
| `net_price` — себестоимость для Ozon | продавец | да | — | да |
| `marketing_seller_price` | Ozon (расчёт) | да | — | да |
| `retail_price` | Ozon | да | — | да |
| `marketing_price` — цена покупателя | Ozon | **НЕТ (удалено)** | — | нет |
| Акции Ozon | Ozon | частично (`actions[]`) | да (`actions` в posting) | **нет** |
| Акции продавца | продавец | `/v1/seller-actions/*` | да | **нет** |
| «Скидка за счёт Озон» | **Ozon** | **нет** | **да (posting `actions`)** | частично |
| Оплата Баллами | Ozon/покупатель | нет | да (posting `actions`) | частично |
| Эластичный бустинг | Ozon | флаг в import/prices | да (posting `actions`) | частично |
| Цена конкурента | Ozon | `/v1/pricing-strategy/product/info` | — | **нет** |

### 5.2 Наблюдаемая реальность: субсидии Ozon видны только после заказа [FACT]

`RAW_OZON_POSTINGS_FBO.actions` за период с 2026-08-01:

| Название механики | Строк |
|---|---|
| Системная виртуальная скидка селлера Россия (RUB) | 234 |
| Товарная скидка на доставку (Сквозная экономика 5) | 228 |
| Округление | 217 |
| **Скидка (за счет Озон) — DD by AI benefit system (Mesh)** | 177 |
| **Скидка (за счет Озон) — OA by AI benefit system (Mesh)** | 159 |
| **Скидка (за счет Озон) — DD by AI benefit system** | 57 |
| **Скидка (за счет Озон) — OA by AI benefit system** | 53 |
| Эластичный бустинг. Без ограничения срока действия | 40 |
| [Оплата Баллами] Стандартные условия (до 90%) | 11 |
| Распродажа стока. Август 3.0 | 9 |
| Максимальный бустинг: усиление | 9 |
| Акция на списание индивидуальных бонусов для селлера 2773848 | 2 |
| Прочее (Казахстан, Белоруссия) | 3 |

**[FACT]** При этом `ozon_actions_exist` в `RAW_OZON_PRICES` = FALSE для 20/20 SKU
во всех 6 снимках. То есть флаг `marketing_actions.ozon_actions_exist` отражает
участие в **акциях Ozon с явным списком товаров**, а не наличие механик
«Скидка за счёт Озон» / бустинг / баллы, которые применяются автоматически.

**[FACT]** Цена покупателя в posting систематически ниже текущей цены продавца:

| internal_sku | Цена продавца (07.09) | Цена покупателя (posting) | Разрыв |
|---|---|---|---|
| EVT-SET-TON-SER-CREAM-ACNE | 2735 | 1937 | −29,2 % |
| EVT-SET-HAND-CHERRY | 1620 | 1288 | −20,5 % |
| EVT-SET-MOIST-TONIC-SERUM | 1527 | 1309 | −14,3 % |
| EVT-HC-CHERRY-300 | 1209 | 1101 | −8,9 % |
| EVT-FT-ACNE-150 | 1263 | 1164 | −7,9 % |
| EVT-FS-ACNE-30 | 1258 | 1258 | 0,0 % |

**[INFERENCE]** Разрыв — это Ozon-funded скидки, оплата баллами и округление.
Он достигает 29 % и **не наблюдаем до заказа**. Любой расчёт маржи по цене
`RAW_OZON_PRICES.price_rub` систематически завышает выручку.
Заметим: `price_rub` был неизменен во всех 6 снимках (1 уникальное значение на SKU),
поэтому объяснение «цена изменилась после заказа» отпадает для этого окна.

### 5.3 `min_price` сегодня не является экономическим полом [FACT]

Для 20 из 20 SKU: `min_price_rub = price_rub` в точности.
`old_price_rub = 2,5 × price_rub` ровно для всех 20 SKU.

**[INFERENCE]** `min_price` заполнена механически (равна цене), а не рассчитана из экономики.
Она не защищает ни от чего. `old_price` = 2,5× — маркетинговая конструкция.
Это одновременно и риск (нет реальной защиты), и возможность:
`min_price` — штатный механизм Ozon для ограничения автоприменения акций,
и его правильное заполнение из `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.break_even_price_worst`
даст защиту раньше, чем появится репрайсер.

### 5.4 Собственный репрайсер Ozon — конфликтующая система [FACT]

`/v1/pricing-strategy/*` — встроенный механизм Ozon, который **сам меняет цену**
по цене конкурента. `POST /v1/pricing-strategy/product/info` возвращает
`strategy_product_price`, `strategy_competitor_product_url`, `price_downloaded_at`.

**[INFERENCE]** Если товар включён в стратегию Ozon, наш репрайсер и стратегия Ozon
будут перезаписывать друг друга. `price_strategy_enabled` в `import/prices` и
`is_enabled` в `pricing-strategy/product/info` — обязательный pre-flight guard.
Сегодня мы не собираем ни то, ни другое.

---

## 6. BIGQUERY PRICING INVENTORY

### 6.1 Инвентарь price-related полей — Wildberries [FACT]

| Таблица | Поле | Тип | Смысл | Grain | Pre/Post | Пригодно для репрайсера |
|---|---|---|---|---|---|---|
| `RAW_WB_ORDERS` | `price_with_disc` | STRING | цена продавца после скидки продавца | заказ | post-order | косвенно (прокси цены продавца) |
| `RAW_WB_SALES_RETURNS` | `total_price` | NUMERIC | цена до скидок | продажа | post-sale | да, historical |
| `RAW_WB_SALES_RETURNS` | `discount_percent` | NUMERIC | скидка продавца, % | продажа | post-sale | да, historical |
| `RAW_WB_SALES_RETURNS` | **`spp`** | NUMERIC | **СПП, %** | продажа | post-sale | **да, единственный оперативный источник СПП** |
| `RAW_WB_SALES_RETURNS` | `price_with_disc` | NUMERIC | цена продавца после скидки | продажа | post-sale | да |
| `RAW_WB_SALES_RETURNS` | **`finished_price`** | NUMERIC | **цена покупателя** | продажа | post-sale | **да** |
| `RAW_WB_SALES_RETURNS` | `payment_sale_amount` | NUMERIC | сумма оплаты | продажа | post-sale | ограниченно |
| `RAW_WB_SALES_RETURNS` | `for_pay` | NUMERIC | предварительная сумма к выплате | продажа | post-sale | **нет** (в коде помечено «для P&L НЕ использовать») |
| `RAW_WB_FINANCE` | `retail_price` | STRING | цена до скидок | строка реализации | post-sale +2–3 сут | да |
| `RAW_WB_FINANCE` | `retail_price_withdisc_rub` | STRING | цена реализации со скидками | строка реализации | post-sale | да, база расчёта |
| `RAW_WB_FINANCE` | `retail_amount` | STRING | сумма продажи | строка реализации | post-sale | да |
| `RAW_WB_FINANCE` | **`spp_percent`** | STRING | **СПП, % (`ppvz_spp_prc`)** | строка реализации | post-sale | да, historical |
| `RAW_WB_FINANCE` | `sale_percent` | STRING | скидка, % | строка реализации | post-sale | да |
| `RAW_WB_FINANCE` | `product_discount_for_report` | STRING | скидка товара для отчёта | строка реализации | post-sale | требует проверки семантики |
| `RAW_WB_FINANCE` | `supplier_promo` | STRING | промо продавца | строка реализации | post-sale | требует проверки семантики |
| `RAW_WB_FINANCE` | `commission_percent` | STRING | комиссия, % | строка реализации | post-sale | да, но только по факту |
| `RAW_WB_FINANCE` | `for_pay` | STRING | к перечислению | строка реализации | post-sale | да, settlement base |
| `RAW_WB_FINANCE` | `compensation_amount` | STRING | компенсации WB | строка реализации | post-sale | да, subsidy |
| `MART_SKU_DAILY` | — | — | **цен нет вообще**, только `orders_rub`/`orders_qty` | nm_id × сутки | — | только implied ASP |

**Потери на входе [FACT]:** `apps-script/WbOrdersLoader` сохраняет из ответа orders API
только `priceWithDisc`; в шапке файла зафиксировано: «`totalPrice` / `finishedPrice`
пока НЕ сохраняются (нет колонок) — см. долг D1.1».
`apps-script/WbSalesReturnsLoader` сохраняет полный стек цен.

### 6.2 Инвентарь price-related полей — Ozon [FACT]

`ozon_raw.RAW_OZON_PRICES`, ключ MERGE = (`snapshot_date`, `offer_id`), 45 колонок:

| Поле | Смысл | Заполнено 07.09 |
|---|---|---|
| `price_rub` | цена продавца | 20/20 |
| `old_price_rub` | зачёркнутая цена | 20/20 |
| `min_price_rub` | минимальная цена | 20/20 (= price) |
| `marketing_seller_price_rub` | цена с учётом скидок продавца | 20/20 |
| `retail_price_rub` | розничная цена | 20/20 |
| `net_price_rub` | себестоимость для Ozon | 20/20 |
| `acquiring_rub` | эквайринг | 20/20 |
| `vat_rate` | НДС | 20/20 |
| `auto_action_enabled` | автоприменение акций | 20/20 |
| `auto_add_to_ozon_actions_enabled` | автодобавление в акции Ozon | 20/20 |
| `price_index_color` | индекс цены (GREEN/YELLOW/RED) | 20/20 |
| `external_min_price_rub`, `external_index_value` | индекс к внешним площадкам | 19/20 |
| `ozon_index_min_price_rub`, `ozon_index_value` | индекс к Ozon | 19/20 |
| `self_marketplaces_index_*` | индекс к своим ценам на др. площадках | — |
| `ozon_actions_exist` | флаг участия в акциях Ozon | 20/20 (все FALSE) |
| `sales_percent_{fbo,fbs,rfbs,fbp}` | комиссии | 20/20 |
| `fbo_*`, `fbs_*` (10 полей) | логистика, last mile, возвратный поток | 20/20 |
| `commissions_unknown_fields` | детектор новых тарифов | 0 неизвестных |
| **`marketing_price`** | **цена покупателя** | **ОТСУТСТВУЕТ В API** |

`ozon_raw.RAW_OZON_POSTINGS_FBO` — post-sale источник цены покупателя:
`price_rub`, `old_price_rub`, `total_discount_value_rub`, `commission_amount_rub`,
`payout_rub`, `actions ARRAY<STRING>`, grain = posting × sku, каденс 3×/сут, окно 30 суток.

`ozon_raw.RAW_OZON_PRICE_COMMISSIONS` — длинная проекция тарифного контракта,
ключ (`snapshot_date`, `offer_id`, `sale_scheme`, `commission_component`),
флаг `is_known_component`.

### 6.3 Витрины, релевантные ценообразованию [FACT]

| Объект | Что даёт | Отношение к репрайсеру |
|---|---|---|
| `ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` | `break_even_price_expected/worst`, `target_price_{10,15,20,25}pct_*`, `target_price_*_at_drr{5..25}_*`, коридоры логистики, `safety_class_worst_case` | **готовая основа price floor для Ozon** |
| `ozon_mart.V_OZON_AGENT_DECISION_INPUT` | тот же набор + гейты `agent_decision_gate` / `agent_shadow_gate` / `agent_write_gate` | **готовый контракт для агента** |
| `ozon_mart.V_OZON_SKU_CURRENT_TARIFF`, `V_OZON_TARIFF_CHANGE_LOG`, `V_OZON_TARIFF_SOURCE_HEALTH` | текущий тариф, лог изменений, здоровье источника | входы guardrails |
| `wb_mart.V_ADS_SKU_ECONOMIC_LIMITS` | `max_ad_drr_breakeven`, `contribution_after_ads_and_product_cogs_rub`, `economic_state` — **по факту окна**, не вперёд | не заменяет floor |
| `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | `product_cogs_rub`, `confidence`, `cogs_provenance_status` | COGS для обоих каналов |
| `evetis_ref.REF_SKU_CHANNEL_MAP` | связка internal_sku ↔ WB/Ozon | product identity |
| `wb_ops.OPS_PIPELINE_REGISTRY` | контракт каденса/SLA/snapshot-only для каждого пайплайна | точка подключения observability |

**Замечание по стоимости [FACT]:** запрос к `V_OZON_AGENT_DECISION_INPUT` (20 строк)
обошёлся в 157 МБ billed и ~1000 с slot-времени. Витрина тяжёлая; в интрадей-цикле
её нельзя вызывать на каждый снимок.

---

## 7. DATA FRESHNESS & HISTORICAL COVERAGE

### 7.1 Ответ на главный вопрос §6 ТЗ

**Wildberries.** Можем ли восстановить 08:00 / 08:30 / 09:00 …?

**НЕТ. И даже дневного состояния нет. [FACT]**
Цены продавца WB не сохраняются вообще — ни intraday, ни daily.
Есть только цена, зафиксированная в момент заказа (`price_with_disc`, почасовой инкремент)
и в момент продажи (`finished_price`, `spp`).

**Ozon.** **НЕТ. Только суточное состояние. [FACT]**
Ключ MERGE `RAW_OZON_PRICES` = (`snapshot_date`, `offer_id`). Повторный запуск в те же сутки
**перезаписывает** строку. Intraday невозможен по построению схемы, даже если запускать job чаще.

> **Current architecture is insufficient for intraday repricing.**

### 7.2 Фактическое покрытие

**Ozon `RAW_OZON_PRICES`:** 120 строк, 20 offer_id, 6 суток (2026-08-31 … 2026-09-07).
**Пропуски: 2026-09-01 и 2026-09-02.** Времена первых снимков (МСК): 01.09 14:52, 03.09 13:41,
04.09 15:56, 05.09 06:30, 06.09 13:48, 07.09 06:30. По расписанию — 06:30;
остальные — ручные прогоны. [FACT]

**WB продажи:** 4 272 строки, 2026-03-30 … 2026-09-06.
Покрытие по дням (последние 13 суток): **5–12 уникальных nm_id в сутки из 25**. [FACT]

| Дата | nm_id с продажами | Строк |
|---|---|---|
| 2026-08-28 | 12 | 24 |
| 2026-08-30 | 11 | 19 |
| 2026-08-25 | 10 | 18 |
| 2026-09-02 | 10 | 21 |
| 2026-09-05 | 7 | 14 |
| 2026-09-06 | 7 | 13 |
| 2026-09-03 | 5 | 13 |

**[INFERENCE]** Post-sale наблюдение цены покупателя WB покрывает 20–48 % ассортимента в сутки.
Для SKU без продаж цена покупателя неизвестна вообще. Это делает post-sale источник
непригодным как основной сигнал для репрайсера — он годится только для калибровки модели СПП.

### 7.3 Свежесть пайплайнов на 2026-09-07 [FACT]

Все контракты `wb_mart.V_DATA_FRESHNESS` — статус **OK**:
orders/sales/mart/ads — data_as_of 2026-09-06 (возраст 1 сут при SLA 1–3);
finance — 2026-09-05 (возраст 2 при SLA 3); stocks — 2026-09-07 (0);
sku_orphans = 0.

Ozon `OZON_INGESTION_RUNS` за 10 суток: все 11 сущностей отработали;
2 изолированных отказа (ads_sku_daily ×1, fbo_postings ×1) при 100 % последних прогонов OK.

---

## 8. PRICE SEMANTICS MATRIX (WB × Ozon)

| Концепт | WB — поле/источник | Ozon — поле/источник | Pre-sale | Post-sale |
|---|---|---|---|---|
| base seller price | `list/goods/filter`.`price` | `/v5/.../prices`.`price.price` | WB ✅* / OZ ✅ | WB ✅ (`total_price`) / OZ ✅ (`old_price` posting) |
| seller discount | `list/goods/filter`.`discount` (%) | производная `old_price` → `price` | WB ✅* / OZ ✅ | WB ✅ (`discount_percent`) / OZ ✅ |
| seller effective price | `discountedPrice` | `price.price` | WB ✅* / OZ ✅ | WB ✅ (`price_with_disc`) / OZ ✅ |
| marketplace discount | **СПП — нет метода** | **нет (marketing_price удалён)** | ❌ / ❌ | WB ✅ (`spp`, `spp_percent`) / OZ ⚠️ (`actions[]` — названия без сумм) |
| consumer shelf price | **нет** | **нет** | ❌ / ❌ | WB ✅ (`finished_price`) / OZ ✅ (posting `price_rub`) |
| club / premium price | `clubDiscountedPrice` (**оплачивает продавец**) | Premium — расход магазина, не цена SKU | WB ✅* / OZ ⚠️ | WB ⚠️ / OZ ⚠️ |
| promo price | `calendar/promotions/nomenclatures` | `/v1/actions/products`.`action_price`, `max_action_price` | WB ✅* / OZ ✅* | WB ⚠️ / OZ ✅ (`actions[]`) |
| minimum allowed price | **No semantic equivalent** — у WB нет поля min_price | `price.min_price` | — / OZ ✅ | — |
| marketplace subsidy | «Компенсация скидки по программе лояльности» в реализации | «Скидка (за счет Озон)» в posting `actions` — **без суммы** | ❌ / ❌ | WB ✅ (сумма) / OZ ⚠️ (только факт) |
| seller settlement base | `for_pay` (реализация) | `payout_rub` (posting), `/v1/finance/accrual/by-day` | ❌ / ❌ | ✅ / ✅ |
| actual realized sale price | `retail_price_withdisc_rub` | posting `price_rub` | ❌ / ❌ | ✅ / ✅ |
| ценовой индекс к конкурентам | **No semantic equivalent** | `price_index_color`, `external_index_value`, `ozon_index_value` | — / OZ ✅ | — |
| quarantine / защита от обвала | ценовой карантин (×3) | `discount_too_big`, `price_out_of_range` | ✅ / ✅ | — |

`*` — метод существует, но EVETIS его не вызывает.

**Асимметрии, которые нельзя унифицировать [INFERENCE]:**
1. У WB нет `min_price`. Пол цены на WB может существовать только в нашей системе.
2. У Ozon нет процента marketplace-скидки — есть только факт применения механики.
3. `clubDiscount` WB оплачивается продавцом, Premium Ozon — это подписка магазина.
   Это не одно и то же и не сопоставимо.
4. Индекс цен есть только у Ozon.

---

## 9. CRITICAL DATA GAPS

### P0 — блокеры. Без них репрайсер невозможен

| # | Пробел | Доказательство | Последствие |
|---|---|---|---|
| P0-1 | Нет наблюдения цены продавца WB | В репозитории 0 упоминаний `discounts-prices-api`; в `wb_raw` нет ни одной ценовой таблицы | Нельзя ни рекомендовать, ни верифицировать цену WB |
| P0-2 | Нет токена WB категории «Цены и скидки» | Секреты: `WB_TOKEN_{ANALYTICS,ADVERT,FINANCE,PROMOTION,STANDARD,STATISTICS,SUPPLIES}` | Методы цен WB физически недоступны |
| P0-3 | Нет форвардных тарифов WB | `/api/v1/tariffs/*` не вызывается нигде | `absolute_floor` WB нельзя вычислить из экономики |
| P0-4 | Ozon-цены схлопнуты в сутки ключом MERGE | `merge_rows("RAW_OZON_PRICES", ..., ["snapshot_date","offer_id"])` | Intraday невозможен без смены модели хранения |
| P0-5 | Нет канонической ценовой модели и parity-витрины | В `evetis_ref` нет ни одного ценового объекта | Разрыв WB/Ozon ×2 был невидим |

### P1 — обязательны до контролируемой автоматики

| # | Пробел | Доказательство |
|---|---|---|
| P1-1 | Не собирается состояние акций (WB календарь, Ozon `/v1/actions`) | нет вызовов; `marketing_actions.actions[]` отбрасывается |
| P1-2 | Не собирается статус стратегий ценообразования Ozon | `/v1/pricing-strategy/*` не вызывается |
| P1-3 | COGS отсутствует у 2 SKU из 25 | `V_PRODUCT_COGS_EFFECTIVE`: 23 текущих строки |
| P1-4 | `min_price` Ozon = `price` для 20/20 — фиктивная защита | живые данные |
| P1-5 | Нет разделения read/write кредов; Ozon — один `EVETIS_OZON_API_KEY` | `common.py` |
| P1-6 | Риск отключения `supplier/orders` + `supplier/sales` | дайджест WB API, август 2026 |
| P1-7 | `RAW_WB_ORDERS` теряет `totalPrice`/`finishedPrice` (долг D1.1) | шапка `WbOrdersLoader` |

### P2 — оптимизация после MVP

- Ozon posting `actions[]` — только названия, без сумм → нельзя атрибутировать субсидию по механикам.
- `V_OZON_AGENT_DECISION_INPUT` дорогая (157 МБ на 20 строк) → нужен материализованный слой.
- Нет истории `activationDate` записей цены → нет измеренной propagation delay.
- Нет `wholesaleDiscountThreshold` (B2B WB) в модели.

### P3 — продвинутая аналитика

- Эластичность спроса — требует ≥6 мес истории цен, которой пока нет ни одного дня.
- Конкурентные цены: Ozon даёт их **только** при включении товара в собственную
  стратегию ценообразования, которая сама меняет цену. У WB официального источника нет.
- Атрибуция «падение рекламы vs изменение цены».

---

## 10. CANONICAL PRICE MODEL [RECOMMENDATION]

Предлагается **после** установленных фактов, а не до них.

### 10.1 `PRICE_SNAPSHOT_RAW` — сырой снимок, append-only

Grain: **marketplace × marketplace_product_id × observed_at**. Никакого MERGE по дате.

```
marketplace              STRING   -- 'WB' | 'OZON'
marketplace_product_id   STRING   -- nmID | offer_id
observed_at              TIMESTAMP-- момент нашего наблюдения
source_endpoint          STRING
source_payload_hash      STRING
payload_json             STRING   -- сырой ответ, чтобы новые поля не терялись молча
ingestion_run_id         STRING
```
Партиционирование: `DATE(observed_at)`. Кластеризация: `marketplace, marketplace_product_id`.
Retention: 90 суток (сырьё), далее только canonical.

### 10.2 `PRICE_SNAPSHOT_CANONICAL` — нормализованный снимок

```
marketplace                    STRING
product_key                    STRING   -- internal_sku
marketplace_product_id         STRING
observed_at                    TIMESTAMP
seller_list_price              NUMERIC  -- WB price | OZ old_price? см. примечание
seller_discount_pct            NUMERIC  -- WB discount | OZ производная
seller_effective_price         NUMERIC  -- WB discountedPrice | OZ price
club_or_premium_price          NUMERIC  -- WB clubDiscountedPrice | OZ NULL
min_price_declared             NUMERIC  -- WB NULL (нет поля) | OZ min_price
old_price_display              NUMERIC  -- WB price | OZ old_price
promo_state                    STRING   -- NONE | CANDIDATE | PARTICIPATING | FROZEN | UNKNOWN
promo_id                       STRING
promo_price                    NUMERIC
auto_promo_enabled             BOOL     -- OZ auto_action_enabled | WB NULL
external_price_index           NUMERIC  -- OZ only
consumer_price_observed        NUMERIC  -- ВСЕГДА NULL в pre-sale слое
marketplace_discount_pct       NUMERIC  -- ВСЕГДА NULL в pre-sale слое
currency                       STRING
source                         STRING
source_timestamp               TIMESTAMP
ingested_at                    TIMESTAMP
confidence                     STRING   -- OBSERVED | DERIVED | UNAVAILABLE
```

**Критично [RECOMMENDATION]:** поля `consumer_price_observed` и `marketplace_discount_pct`
в pre-sale слое **обязаны быть NULL**, а не заполняться прокси. Иначе система начнёт
считать цену продавца ценой покупателя — ровно та ошибка, которая сейчас
завышает выручку Ozon на величину до 29 %.

### 10.3 `PRICE_CONSUMER_FACT` — post-sale факт цены покупателя

Отдельный объект, отдельный grain, отдельный лаг.

```
marketplace, product_key, marketplace_product_id
event_at                  TIMESTAMP -- WB sale_dt | OZ order_date
order_ref                 STRING    -- WB srid | OZ posting_number
seller_effective_price    NUMERIC   -- WB price_with_disc | OZ вычисляемо
consumer_price            NUMERIC   -- WB finished_price | OZ posting price_rub
marketplace_discount_pct  NUMERIC   -- WB spp | OZ вычисляемо
subsidy_mechanics         ARRAY<STRING> -- OZ actions[] | WB тип операции
settlement_base           NUMERIC   -- WB for_pay | OZ payout_rub
observation_lag_hours     INT64
confidence                STRING
```

### 10.4 Почему не единый объект

**[INFERENCE]** Смешивание pre-sale снимка и post-sale факта в одной таблице —
это ровно то смешивание RAW и расчётных данных, которое запрещено PROJECT_RULES.
У них разный grain (SKU×время vs заказ), разный лаг (минуты vs сутки),
разное покрытие (100 % SKU vs 20–48 % SKU) и разная достоверность.

---

## 11. UNIT ECONOMICS READINESS

### 11.1 Компоненты по каналам [FACT]

| Компонент | WB | Ozon |
|---|---|---|
| COGS | ✅ `V_PRODUCT_COGS_EFFECTIVE`, 23/25 SKU | ✅ то же, `management_cogs` в витрине |
| Комиссия | ⚠️ только post-factum (`commission_percent`) | ✅ форвардно (`sales_percent_fbo/fbs/rfbs/fbp`) |
| Логистика | ⚠️ только post-factum (`logistics_amount`) | ✅ форвардно, коридор min/expected/max |
| Last mile | ⚠️ внутри логистики | ✅ `last_mile_rub` |
| Fulfillment | ⚠️ | ✅ `fbo_fulfillment_amount` |
| Хранение | ⚠️ post-factum (`storage_fee`) | ⚠️ не в форвардной модели |
| Возвраты | ⚠️ post-factum | ✅ `expected_return_cost_rub`, `return_probability_status` |
| Отмены | ⚠️ | ✅ `cancellation_rate_pct`, `cancel_cost_if_shipped_*` |
| Эквайринг | ⚠️ post-factum (`acquiring_fee`) | ✅ `acquiring_rub` |
| Налоги | ❌ вне модели | ❌ вне модели |
| Реклама | ✅ `FACT_ADS_*`, DRR | ✅ `trailing_90d_ad_spend_rub`, DRR-лимиты |
| Прочие удержания | ✅ `V_WB_DEDUCTIONS_CLASSIFIED` | ⚠️ `finance_accrual` |
| Premium/подписка | н/д | ✅ `premium_status` из `seller_info` |

### 11.2 Классификация расходов [INFERENCE]

| Тип | WB | Ozon |
|---|---|---|
| deterministic (известны до продажи) | **нет ни одного** | комиссия %, эквайринг, last mile |
| SKU-dependent | COGS | COGS, объёмный вес, логистика |
| order/region-dependent | логистика, last mile | логистика (коридор min/expected/max, `route_sensitivity_pp`) |
| probabilistic | возвраты, отмены | возвраты, отмены — **смоделированы** |
| historical average | всё остальное | хранение |
| dynamic | реклама | реклама |

### 11.3 Вывод

**[INFERENCE]** Ozon готов к price floor модели сегодня. WB — нет.
Разрыв закрывается ровно одним пайплайном: `/api/v1/tariffs/{commission,box,return}`.
Это самый дешёвый по инженерии и самый высокий по отдаче элемент всей программы.

---

## 12. PRODUCT MAPPING READINESS

**[FACT]** `evetis_ref.REF_SKU_CHANNEL_MAP`, ключ `internal_sku`:

| marketplace | mapping_status | строк | is_current |
|---|---|---|---|
| WB | MIGRATED_FROM_WB_REFERENCE | 25 | 25 |
| OZON | RESOLVED_FROM_OZON_API | 20 | 20 |
| OZON | OZON_PRIMARY_DOCUMENT | 2 | 0 (историческое) |

**20 SKU присутствуют на обеих площадках**, 5 — только WB
(EVT-HC-BODY-300, EVT-SET-HAND-BODY, EVT-SET-TON-CREAM-ACNE,
EVT-SET-TON-CREAM-MOIST, EVT-SET-TON-SER-CREAM-MOIST).

Идентификаторы: `internal_sku`, `marketplace_sku` (WB nm_id / Ozon SKU),
`marketplace_product_id`, `offer_id`, `vendor_code`, SCD-2 через `valid_from`/`valid_to`/`is_current`.

**Ответ на вопрос ТЗ:** канонический `product_key` **существует** — это `internal_sku`.
Мэппинг сделан не по названию, а через `/v3/product/list` + первичные документы.
Это **не пробел**. Это единственный полностью готовый фундамент репрайсера.

Единственное замечание [FACT]: `offer_id` на Ozon численно равен WB `nm_id`
(например `1083392113` = WB nm_id того же SKU). Это соглашение продавца, а не факт платформы;
резолв обязан идти через `REF_SKU_CHANNEL_MAP`, а не через равенство идентификаторов.

---

## 13. INTRADAY OBSERVER ARCHITECTURE [RECOMMENDATION]

### 13.1 Частота

| Вариант | За | Против | Вывод |
|---|---|---|---|
| 5 мин | ловит всё | 288 прогонов/сут, шум | избыточно |
| **15 мин** | 96 снимков/сут, покрывает суточную динамику СПП | — | **рекомендуется** |
| 30 мин | дешевле | может пропустить окно акции | приемлемо на старте |
| 60 мин | минимум | теряет внутричасовую динамику | недостаточно |

Ограничения не биндят: WB — 1 запрос на полный снимок при лимите 10/6 с;
Ozon — 1 запрос при лимите 50/с.

**[RECOMMENDATION]** Стартовать с **30 минут**, поднять до 15 после первой недели
измерения фактической частоты изменений. Решение принимать по данным, а не заранее.

### 13.2 Объём и стоимость

45 marketplace-SKU (25 WB + 20 Ozon).

| Каденс | Строк/сут | Строк/мес | Строк/год | Размер/год (≈300 Б/строка) |
|---|---|---|---|---|
| 15 мин | 4 320 | ~131 тыс. | ~1,58 млн | ~470 МБ |
| 30 мин | 2 160 | ~66 тыс. | ~790 тыс. | ~235 МБ |

**[INFERENCE]** Объём пренебрежимо мал. Реальная стоимость BigQuery здесь —
не хранение, а **вью**: `V_OZON_AGENT_DECISION_INPUT` уже стоит 157 МБ на один вызов.
При 96 вызовах/сут это 15 ГБ/сут = 450 ГБ/мес только на чтение решений.
Обязателен материализованный слой между наблюдением и решением.

### 13.3 Архитектура [RECOMMENDATION]

```
Cloud Scheduler (30 мин)
   ├── ozon-price-observer  (Cloud Run Job, ENTITIES=prices_intraday)
   │      POST /v5/product/info/prices  →  PRICE_SNAPSHOT_RAW (append)
   └── wb-price-observer    (Cloud Run Job, новый)
          POST /api/v2/list/goods/filter →  PRICE_SNAPSHOT_RAW (append)
                                   ↓
                        PRICE_SNAPSHOT_CANONICAL (материализованная)
                                   ↓
                        PRICE_MARKETPLACE_PARITY (материализованная)
```

Изоляция маркетплейсов сохраняется: сырьё пишется в `wb_raw` / `ozon_raw` раздельно,
кросс-канальная витрина живёт в `evetis_ref` и читает оба домена только через `REF_SKU_CHANNEL_MAP`.

**Дедупликация [RECOMMENDATION]:** писать строку только при изменении `source_payload_hash`
относительно предыдущего снимка + принудительный «heartbeat»-снимок раз в сутки.
Это снижает объём на порядок и даёт бесплатный change-log.

---

## 14. REQUIRED BIGQUERY OBJECTS [RECOMMENDATION]

| Объект | Grain | Writer | Reader | Партиция | Кластер | Retention |
|---|---|---|---|---|---|---|
| `wb_raw.RAW_WB_PRICES` | nmID × observed_at | wb-price-observer | canonical | DATE(observed_at) | nm_id | 90 сут |
| `wb_raw.RAW_WB_TARIFFS` | subject × date | wb-tariff-loader | canonical | date | subject_id | вечно |
| `wb_raw.RAW_WB_PROMO_CALENDAR` | promo × nmID × date | wb-promo-loader | guardrails | date | nm_id | 2 года |
| `ozon_raw.RAW_OZON_PRICES_INTRADAY` | offer_id × observed_at | ozon-price-observer | canonical | DATE(observed_at) | offer_id | 90 сут |
| `ozon_raw.RAW_OZON_ACTIONS` | action × offer_id × date | ozon-actions-loader | guardrails | date | offer_id | 2 года |
| `evetis_ref.PRICE_SNAPSHOT_CANONICAL` | mp × product_key × observed_at | SQL | всё | DATE(observed_at) | marketplace, product_key | 2 года |
| `evetis_ref.PRICE_CONSUMER_FACT` | mp × order_ref × sku | SQL | аналитика | DATE(event_at) | marketplace, product_key | вечно |
| `evetis_ref.PRICE_POLICY` | mp × product_key × valid_from | владелец (руками) | движок | — | product_key | вечно |
| `evetis_ref.PRICE_RECOMMENDATION` | decision_id | движок | shadow/dashboard | DATE(created_at) | marketplace, product_key | 2 года |
| `evetis_ref.PRICE_CHANGE_EVENT` | change_id | execution service | верификация | DATE(created_at) | marketplace, product_key | вечно |
| `evetis_ref.PRICE_CHANGE_VERIFICATION` | change_id × attempt | верификатор | ops | DATE(checked_at) | change_id | вечно |
| `evetis_ref.PRICE_GUARDRAIL_EVENT` | event_id | движок | ops | DATE(created_at) | reason_code | 2 года |
| `evetis_ref.V_PRICE_MARKETPLACE_PARITY` | product_key × date | SQL (вью) | dashboard | — | — | — |
| `wb_ops.OPS_PIPELINE_REGISTRY` | +4 строки | владелец | health | — | — | — |

---

## 15. REPRICING ARCHITECTURE [RECOMMENDATION]

### 15.1 Модель ценового пола — многоуровневая

Плоский `min_price` недостаточен. Предлагается пять уровней:

```
absolute_ceiling   ← максимум, за которым спрос гарантированно умирает / нарушены правила МП
normal_ceiling     ← target + допустимое отклонение вверх
target_price       ← цена целевой маржи (для Ozon уже есть: target_price_20pct_expected)
normal_floor       ← минимальная цена при штатной рекламной нагрузке
defensive_floor    ← минимум для защиты доли/оборачиваемости, разрешён только по явной политике
absolute_floor     ← ЖЁСТКИЙ. Ниже — никогда, ни при каких сигналах
```

### 15.2 Формула `absolute_floor`

```
contribution =
      consumer_revenue_basis            -- settlement base, НЕ витринная цена
    + marketplace_compensation          -- WB: компенсация лояльности; OZ: не наблюдаема → 0
    − commission
    − logistics(worst_case)             -- worst, не expected
    − fulfillment
    − last_mile
    − acquiring
    − storage_allocated
    − COGS
    − expected_return_cost
    − expected_cancel_cost
    − tax
    − advertising_allowance
absolute_floor := min{P : contribution(P) ≥ hard_threshold}
```

**Три обязательных принципа [RECOMMENDATION]:**
1. **Считать от worst-case логистики**, не от expected. Для Ozon `route_sensitivity_pp`
   уже показывает чувствительность.
2. **`marketplace_compensation = 0` по умолчанию.** Субсидия не гарантирована и не наблюдаема
   до продажи. Класть её в пол — значит строить экономику на подарке площадки.
3. **База выручки — settlement base, а не витринная цена.** На Ozon расхождение
   достигает 29 % (§5.2), на WB — величина СПП.

### 15.3 Граница AI и детерминированного движка

```
данные → аналитика → AI-рекомендация → ДЕТЕРМИНИРОВАННЫЙ rule engine
                                              ↓
                                        HARD GUARDRAILS
                                              ↓
                                       execution service
                                              ↓
                                        marketplace API
```

LLM/AI **не может**: отменить `absolute_floor`, игнорировать guardrail,
изменить `PRICE_POLICY`, записать произвольную цену, вызвать write-endpoint.
Прямой путь «LLM → marketplace write API» запрещён архитектурно, а не соглашением:
execution service принимает только строки из `PRICE_RECOMMENDATION`
со статусом, проставленным детерминированным движком.

### 15.4 Машина состояний

```
OBSERVE ──► HOLD ──► RECOMMEND_UP / RECOMMEND_DOWN ──► EXECUTE ──► VERIFY ──► OBSERVE
   │          │                   │                       │           │
   │          │                   ▼                       ▼           ▼
   │          └──────────► MANUAL_REVIEW              FAILED   ROLLBACK_REQUIRED
   │                              ▲                       │           │
   └──────────► BLOCKED ──────────┘                       └───────────┘
```

Переходы:
- `OBSERVE → HOLD` — сигнал внутри deadband либо активен cooldown.
- `OBSERVE → BLOCKED` — сработал fail-closed (см. §16).
- `RECOMMEND_* → EXECUTE` — только при `automation_enabled` и пройденных guardrails;
  иначе `MANUAL_REVIEW`.
- `EXECUTE → VERIFY` — всегда. HTTP 200 состоянием «изменено» не является.
- `VERIFY → ROLLBACK_REQUIRED` — наблюдаемая цена не совпала с ожидаемой после
  истечения окна propagation.

### 15.5 Propagation delay

**WB [FACT]:** `POST upload/task` → `uploadID`; `GET history/tasks` возвращает
`uploadDate` **и** `activationDate` — цена активируется отдельным моментом.
При загрузке через календарь акций статус остаётся `1`, и скидка применится
только на старте акции. Фактическую задержку в проекте измерить не на чем —
записей не было. Обязателен verification polling.

**Ozon [FACT]:** `import/prices` отдаёт `result[].updated` синхронно.
Однако документация фиксирует статус товара `price_sent` как промежуточный
(«Дождитесь статуса `price_sent` и попробуйте ещё раз») — то есть eventual consistency есть.

**[RECOMMENDATION]** Ни один канал не считается изменённым по коду ответа.
Цепочка: `DECISION → WRITE → ACKNOWLEDGED → PROPAGATING → VERIFIED`,
где `VERIFIED` подтверждается **следующим снимком наблюдателя**, а не ответом API.
Именно поэтому наблюдатель (PR-1) обязан появиться раньше исполнителя.

---

## 16. SAFETY ARCHITECTURE [RECOMMENDATION]

### 16.1 Обязательные hard guards

**Финансовые**
- `price ≥ absolute_floor` — иначе BLOCK, без исключений.
- `contribution_margin ≥ minimum_margin_pct` при worst-case логистике.
- `price` проходит валидацию площадки локально (правила §4.3 для Ozon; правило ×3 для WB).

**Качество данных**
- Возраст ценового снимка ≤ 2 × каденс.
- `product_key` разрешён в `REF_SKU_CHANNEL_MAP` с `is_current`.
- COGS присутствует и `confidence ∈ {PROVEN_DOCUMENT, DERIVED_FROM_COMPONENTS}`.
- Тарифная модель свежая: `tariff_freshness_status = OK`, `unknown_tariff_component_count = 0`.

**Маркетплейс**
- API здоров (последний прогон OK).
- WB: товар не в ценовом карантине (`GET /api/v2/quarantine/goods`).
- WB: товар не участвует в акции календаря; акция не в `freeze` — при `freeze_date`
  цену повышать нельзя.
- Ozon: `ozon_actions_exist = FALSE` **и** товар отсутствует в `/v1/actions/products`.
- Ozon: `price_strategy_enabled = FALSE` и `pricing-strategy/product/info.is_enabled = FALSE`.
- Ozon: `auto_action_enabled` учтён — при включённом автоприменении фактическая цена
  будет ниже установленной.

**Операционные**
- Нет активного `manual_override`.
- Ozon: ≤10 изменений цены товара в час (жёсткий лимит API) — закладывать ≤4.
- `|Δprice| ≤ max_change_pct`, `|Δprice| ≥ min_change_pct` (иначе HOLD).
- Прошло ≥ `minimum_hold_minutes` с последнего изменения.
- Не превышен `max_changes_per_day`.

**Кросс-маркетплейс**
- Отклонение parity сверх `parity_max_pct` не допускается, если политика этого не разрешает.
- Изменение на одной площадке не выводит другую за её собственный `absolute_floor`.

**Системные**
- Нет stale-пайплайна среди зависимостей (проверка через `wb_ops.V_OPS_CURRENT_HEALTH`
  и `ozon_mart.V_OZON_MART_FRESHNESS`).
- Нет частичного ingestion.
- Идемпотентность: `decision_id` уникален; повторная отправка того же решения — no-op.

### 16.2 Fail-closed

| Условие | Действие |
|---|---|
| COGS отсутствует | BLOCK |
| Ценовой снимок устарел | BLOCK |
| Ошибка API маркетплейса | HOLD |
| Состояние акции неизвестно | BLOCK |
| Мэппинг отсутствует | BLOCK |
| Комиссия/тариф неизвестны | BLOCK |
| Обнаружена неизвестная тарифная компонента | BLOCK |
| Наблюдаемая цена ≠ ожидаемой после записи | ROLLBACK_REQUIRED + глушение SKU |

**[INFERENCE]** Этот принцип в проекте уже реализован дважды и работает:
`WbSalesConsumerSource.gs` («FAIL-CLOSED: молчаливого отката на SHEET НЕТ»)
и `commissions_unknown_fields` в Ozon-загрузчике («новый тариф Ozon не должен
раствориться молча»). Репрайсер должен наследовать эту дисциплину, а не изобретать свою.

### 16.3 Kill switch и manual override [RECOMMENDATION]

Управление **без redeploy** — через строки в BigQuery, а не через переменные окружения:

```
evetis_ref.PRICE_KILL_SWITCH
  scope        STRING   -- GLOBAL | WB | OZON | SKU
  scope_value  STRING   -- NULL | internal_sku
  enabled      BOOL
  reason, operator, created_at
```

Execution service читает эту таблицу **перед каждым** write и падает закрыто,
если таблица недоступна. Плюс — пауза Cloud Scheduler как второй, независимый рубильник
(механизм `scheduler-control.yml` в проекте уже есть).

```
evetis_ref.PRICE_MANUAL_OVERRIDE
  marketplace, product_key, fixed_price, valid_until, reason, operator
```
При активном override автоматика переходит в `MANUAL_REVIEW` и не пишет ничего.

### 16.4 Гистерезис

| Параметр | Назначение | Стартовое значение [RECOMMENDATION] |
|---|---|---|
| `min_change_pct` | deadband — ниже не двигаемся | 2 % |
| `minimum_hold_minutes` | cooldown после изменения | 360 (6 ч) |
| `max_changes_per_day` | защита от осцилляции | 2 |
| `signal_persistence` | сигнал должен держаться N снимков | 3 подряд |
| `max_change_pct` | ограничение шага | 10 % |

Это предотвращает картину `499 → 519 → 499 → 509 → 499`.
Значения — стартовая гипотеза; калибруются на shadow-этапе.

### 16.5 Reason codes [RECOMMENDATION]

```
MARKETPLACE_DISCOUNT_CHANGE     PRICE_PARITY_GAP
MARGIN_PROTECTION               FLOOR_BREACH_PREVENTED
LOW_STOCK                       OVERSTOCK
SALES_VELOCITY_LOW              SALES_VELOCITY_HIGH
PROMOTION_STATE_CHANGE          PROMOTION_BLOCK
COMPETITOR_PRICE_CHANGE         PRICE_INDEX_DEGRADED
MANUAL_POLICY                   MANUAL_OVERRIDE_ACTIVE
PRICE_ELASTICITY_OPTIMIZATION   DATA_STALE_BLOCK
COGS_MISSING_BLOCK              TARIFF_UNKNOWN_BLOCK
QUARANTINE_RISK_BLOCK           RATE_LIMIT_HOLD
COOLDOWN_HOLD                   DEADBAND_HOLD
```

### 16.6 Аудит

`PRICE_RECOMMENDATION` + `PRICE_CHANGE_EVENT` + `PRICE_CHANGE_VERIFICATION`
хранят полный след: `decision_id`, старые/новые цены (продавца и покупателя),
`target/floor/ceiling`, наблюдаемая marketplace-скидка, ожидаемая маржа и прибыль,
`reason_code`, `rule_version`, `input_snapshot_id`,
метки времени `created_at / approved_at / executed_at / verified_at`,
`execution_status`, `verification_status`, `manual_or_automatic`.

**[RECOMMENDATION]** `input_snapshot_id` обязателен: без ссылки на конкретный снимок
решение невоспроизводимо, а значит неаудируемо.

---

## 17. SHADOW MODE ARCHITECTURE [RECOMMENDATION]

Shadow-репрайсер пишет в `PRICE_RECOMMENDATION` и **не имеет доступа**
к write-эндпоинтам вообще — не по флагу, а по IAM: у service account'а shadow-режима
нет секрета с write-токеном.

Что нужно для оценки shadow-периода:
- полная история наблюдений цен обеих площадок за период (PR-1/PR-2);
- сгенерированные рекомендации с `reason_code` и ожидаемой маржой;
- фактические цены и продажи за тот же период;
- доля решений, заблокированных guardrails, с разбивкой по причинам;
- расхождение «ожидаемая цена покупателя» vs «фактическая цена покупателя»
  (это ключевая метрика: она измеряет, насколько наша модель СПП/скидок Ozon адекватна).

Минимальная длительность: **4 недели**, чтобы захватить недельную сезонность
и хотя бы одну акцию площадки.

**Backtesting [INFERENCE]:** сегодня невозможен — истории цен нет ни одного дня на WB
и 6 суток на Ozon. Он станет возможен через ~60 суток после запуска PR-1.
Строго разделять: `historical simulation` (что бы предложил движок) ≠
`counterfactual estimate` (что было бы с продажами) ≠ `causal experiment` (A/B).
Утверждать прирост выручки можно только по третьему.

---

## 18. CROSS-MARKET PRICING ARCHITECTURE [RECOMMENDATION]

### 18.1 Что именно сравнивать

**Не** `WB seller price` vs `Ozon seller price`. Сравнивать
**фактическую цену покупателя** — а она pre-sale недоступна.

**[RECOMMENDATION]** Двухслойная модель паритета:

**Слой A — «наблюдаемый паритет» (post-sale, лаг 0–3 сут, покрытие 20–48 %):**
```
consumer_parity_ratio = WB_consumer_price / OZON_consumer_price
```
Считается по `PRICE_CONSUMER_FACT`. Это правда, но редкая и запаздывающая.

**Слой B — «моделируемый паритет» (pre-sale, покрытие 100 %):**
```
expected_WB_consumer   = WB_discountedPrice × (1 − SPP_model)
expected_OZON_consumer = OZON_price × (1 − OZON_discount_model)
```
где `SPP_model` и `OZON_discount_model` — оценки, откалиброванные на слое A
per-SKU за скользящее окно. Обязательное поле `confidence`.

**[INFERENCE]** Только слой B пригоден для оперативных решений, и только слой A
даёт право ему доверять. Строить репрайсер на одном из них нельзя.

### 18.2 Показатели

```
absolute_spread     = WB_consumer − OZON_consumer
spread_pct          = spread / OZON_consumer
wb_ozon_index       = WB_consumer / OZON_consumer
parity_corridor     = [1 − parity_max_pct, 1 + parity_max_pct]
priority_marketplace= канал с большей contribution на единицу
```

### 18.3 Когда паритет НЕ требуется

**[RECOMMENDATION]** Паритет ≠ равенство цен. Он не требуется, когда:
- различаются комиссия и логистика настолько, что равная цена даёт разную маржу;
- SKU присутствует только на одной площадке (5 из 25 — только WB);
- на одной площадке идёт акция с фиксированной ценой;
- каналы намеренно позиционируются по-разному (решение владельца, не движка);
- различаются CPA/CPC и конверсия настолько, что оптимальные цены расходятся.

Правильная формулировка цели — не «одинаковая цена», а **«отсутствие необъяснённого разрыва»**.
Разрыв ×2, обнаруженный сегодня, необъяснён.

---

## 19. REQUIRED API PERMISSIONS

### 19.1 Текущее состояние [FACT]

**WB** (Secret Manager, значения владелец кладёт руками; Terraform токены не хранит):
`WB_TOKEN_ANALYTICS`, `WB_TOKEN_ADVERT`, `WB_TOKEN_FINANCE`, `WB_TOKEN_PROMOTION`,
`WB_TOKEN_STANDARD`, `WB_TOKEN_STATISTICS`, `WB_TOKEN_SUPPLIES`.
**Токена категории «Цены и скидки» нет.**

**Ozon:** `EVETIS_OZON_CLIENT_ID`, `EVETIS_OZON_API_KEY`,
`EVETIS_OZON_PERFORMANCE_CLIENT_ID`, `EVETIS_OZON_PERFORMANCE_CLIENT_SECRET`.
Один Seller-ключ на всё.

**Service accounts:** `sa-loaders-{shadow,prod}`, `sa-scheduler-{shadow,prod}`,
`sa-ozon-ingestion`, `sa-ozon-scheduler`, `sa-deployer`, `sa-terraform-{plan,apply}`.

### 19.2 Что требуется [RECOMMENDATION]

| Секрет | Категория | Права | Когда |
|---|---|---|---|
| `WB_TOKEN_PRICES_READ` | Цены и скидки | read-only использование | **PR-1, обязательно** |
| `WB_TOKEN_PRICES_WRITE` | Цены и скидки | отдельный токен, тот же скоуп | не ранее PR-8 |
| `EVETIS_OZON_API_KEY_WRITE` | Seller API | отдельный ключ | не ранее PR-9 |

**[FACT]** WB не позволяет выдать токен «только чтение цен» — категория «Цены и скидки»
включает и запись. **[RECOMMENDATION]** Поэтому разделение обеспечивается не платформой,
а нашей инфраструктурой: два разных секрета, два разных service account'а,
и у reader-SA нет `secretmanager.versions.access` на write-секрет.
То же для Ozon: ключи не скоупятся, изоляция — на уровне IAM и разных Cloud Run Job'ов.

**Service accounts [RECOMMENDATION]:**
- `sa-price-observer` — read-секреты, write в `*_raw`, **без** доступа к write-секретам.
- `sa-price-executor` — write-секреты, write в `PRICE_CHANGE_EVENT`,
  **read-only** на `PRICE_RECOMMENDATION` и `PRICE_KILL_SWITCH`.

---

## 20. STOCK-AWARE PRICING И SALES VELOCITY

### 20.1 Что доступно [FACT]

| Метрика | WB | Ozon |
|---|---|---|
| units on hand | ✅ `WB_STOCKS_SNAPSHOTS`, 1×/сут | ✅ `RAW_OZON_STOCKS`, 3×/сут |
| units in transit | ✅ `V_WB_SUPPLIES_*` | ✅ `RAW_OZON_SUPPLY_ORDERS` |
| units reserved | ⚠️ | ⚠️ |
| available stock | ✅ | ✅ `fbo_stock` в витрине |
| orders/day, units/day | ✅ `MART_SKU_DAILY` | ✅ `RAW_OZON_POSTINGS_FBO` |
| **orders/hour, units/hour** | ⚠️ данные почасовые в RAW, витрины суточные | ⚠️ posting `created_at` есть, витрина суточная |
| days of cover | ⚠️ считается ad-hoc | ✅ входит в форвардную модель |
| conversion | ✅ через `V_ADS_FUNNEL_*` | ⚠️ |

**[FACT] Ограничение наблюдаемости складов WB:** с 2026-08-15 WB не раскрывает движение
по поимённым складам РФ; наблюдаемы только агрегаты (`Склад WB РФ`, `warehouseId: -999999`).
Формулировка из `DATA_MODEL.md` обязательна к использованию:
«named warehouse movement is not observable after WB anonymization; totals remain observable».

**[INFERENCE]** Минимальная реально доступная гранулярность скорости продаж — **сутки**
на уровне витрин, при том что RAW WB собирается почасово. Часовая velocity технически
достижима без новых источников — нужен только новый уровень агрегации.
Это делает stock-aware ценообразование возможным на суточном горизонте
и невозможным на внутридневном (что для 45 SKU и не требуется).

### 20.2 Как остатки должны влиять [RECOMMENDATION]

Концептуально, без реализации:
```
days_of_cover < X_low   → давление вверх на цену, давление вниз на рекламу
days_of_cover > X_high  → давление вниз на цену (но НЕ ниже absolute_floor), вверх на рекламу
stockout_risk высокий   → HOLD, менять цену бессмысленно
```
Остатки — **модификатор** внутри коридора `[normal_floor, normal_ceiling]`,
и никогда не основание для пробития `absolute_floor`.

### 20.3 Реклама и цена [RECOMMENDATION]

Данные для связки уже есть: `MART_SKU_DAILY` (ad_spend, views, clicks, ctr, cpc, drr, roas),
`V_ADS_FUNNEL_*`, `V_ADS_SKU_ECONOMIC_LIMITS` (WB); `RAW_OZON_ADS_SKU_DAILY`,
`break_even_drr_*`, `drr_limit_at_*` (Ozon).

Чего не хватает — **цены в том же гране**. Как только `PRICE_SNAPSHOT_CANONICAL`
появится, вопрос «падение рекламной эффективности — это реклама или цена?»
станет разрешимым простым join'ом по (product_key, date). Сегодня он неразрешим в принципе.

Никаких изменений рекламы в рамках ценовой программы не предполагается.

---

## 21. DASHBOARD REQUIREMENTS — EVETIS PRICE CONTROL CENTER [RECOMMENDATION]

Реализация — Metabase (живёт вне git; в репозитории только описание).

**Заголовок (KPI):**
индекс цены покупателя WB · индекс цены покупателя Ozon · паритет WB/Ozon ·
средняя marketplace-субсидия · contribution margin · выручка · единицы ·
изменений цены за период · заблокированных решений · свежесть ценового снимка.

**Таблица SKU:**
`SKU | WB цена покупателя | Ozon цена покупателя | спред | цель | пол | потолок |
маржа WB | маржа Ozon | покрытие запасом | скорость продаж | рекомендация | причина | статус гейта`

**Графики:**
история цены покупателя · история цены продавца · история marketplace-субсидии ·
цена vs продажи · цена vs конверсия · цена vs ДРР · цена vs позиция · паритет во времени.

**Обязательный блок «Почему не меняем»** — распределение `PRICE_GUARDRAIL_EVENT`
по `reason_code`. **[INFERENCE]** Без него владелец увидит бездействующую систему
и не поймёт, работает она или сломана. На shadow-этапе это главный экран.

---

## 22. CURRENT STATE SCORECARD

Фактические значения на 2026-09-07.

| Capability | WB | Ozon |
|---|---|---|
| Current seller price (в системе) | ❌ | ✅ 1×/сут |
| Current seller price (доступен по API) | ✅ метод есть, токена нет | ✅ |
| Current consumer price | ❌ нет метода | ❌ поле удалено 12.11.2025 |
| Intraday snapshots | ❌ | ❌ (схлопнуто ключом MERGE) |
| Historical buyer price | ⚠️ post-sale, покрытие 20–48 %/сут | ⚠️ post-sale, окно 30 сут |
| Marketplace subsidy | ⚠️ `spp` post-sale + компенсация лояльности | ⚠️ только названия механик, без сумм |
| Promotion state | ❌ не собирается (метод есть) | ⚠️ только флаг `ozon_actions_exist` |
| Price index / конкуренты | ❌ нет источника | ⚠️ индексы есть, цена конкурента — только через стратегию Ozon |
| Product mapping | ✅ 25 SKU | ✅ 20 SKU, 20 общих |
| COGS | ✅ 23/25 | ✅ 23/25 |
| Commission (форвардно) | ❌ | ✅ |
| Logistics (форвардно) | ❌ | ✅ коридор min/exp/max |
| Tariffs API | ❌ не используется (существует) | ✅ используется |
| Stock | ✅ 1×/сут | ✅ 3×/сут |
| Sales velocity | ✅ суточная | ✅ суточная |
| Advertising | ✅ | ✅ |
| Forward unit economics | ❌ | ✅ 20/20 SKU |
| Price floor model | ❌ | ✅ `break_even_price_*` |
| Price write API | ✅ документирован | ✅ документирован |
| Verification | ❌ | ❌ |
| Shadow repricer | ❌ | ❌ (гейт `SHADOW_ALLOWED` уже открыт) |
| Auto repricer | ❌ | ❌ (гейт write = HARDCODED_FALSE) |
| Kill switch | ❌ | ❌ |
| Audit trail решений | ❌ | ❌ |
| Cross-market parity | ❌ | ❌ |
| Observability (OPS) | ✅ развитая | ✅ отдельная |

---

## 23. PROPOSED ROADMAP

Предложенный в ТЗ маршрут PR-0…PR-12 проверен. **Он корректен по направлению,
но его нужно перекроить в трёх местах.**

### Что меняется относительно исходной модели

| Исходно | Предложение | Причина |
|---|---|---|
| PR-1 Canonical Product + Price Data Model | **Убрать как отдельный стейдж** | Канонический `product_key` уже существует (`REF_SKU_CHANNEL_MAP`, 20/25 общих SKU). Ценовая модель без единого дня наблюдений будет спроектирована вслепую. Модель рождается вместе с первым наблюдателем. |
| PR-2 WB Observer, PR-3 Ozon Observer | **Оставить раздельными, но WB первым** | На Ozon наблюдатель уже есть (нужна только смена модели хранения — это S). На WB его нет вообще (это L). Разный масштаб — разные стейджи. |
| — | **Добавить PR-2: WB Tariffs & Forward Economics** | Это P0-3. Без него WB-пол невычислим, и PR-5 не сможет стартовать по WB. В исходном маршруте этого стейджа нет вообще. |
| PR-4 Cross-Market Price Intelligence | Сдвинуть **после** обоих наблюдателей | Паритет нельзя строить на одной наблюдаемой стороне. |
| PR-5 Unit Economics + Policy Engine | **Разделить**: экономика WB уходит в PR-2, движок политик остаётся | Смешаны разнородные задачи с разным риском. |
| — | **Добавить PR-6.5: Promotions & Conflict State** | Состояние акций — hard guardrail. Без него shadow даст рекомендации, невыполнимые в реальности. |
| PR-8/PR-9 WB и Ozon auto-repricing | **Поменять местами: Ozon первым** | На Ozon уже есть форвардная экономика и гейты. Автоматика на WB требует ещё и доверия к модели СПП. |
| PR-10 Cross-Market Coordination | Оставить | — |
| PR-11 Stock + Ads Aware | Оставить | — |
| PR-12 Elasticity | Оставить, но не раньше 6 мес истории цен | Данных нет и не будет раньше. |

### Итоговый маршрут

| Стейдж | Содержание | Зависит от |
|---|---|---|
| **PR-0** | Discovery (этот документ) | — |
| **PR-1** | **WB Price Observer (read-only)** + первая версия `PRICE_SNAPSHOT_RAW/CANONICAL` | токен WB «Цены и скидки» |
| **PR-2** | **WB Tariffs & Forward Economics** — `/api/v1/tariffs/*` → WB-аналог `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` | PR-1 |
| **PR-3** | **Ozon Intraday Price Observer** — смена ключа хранения на append-only, подключение к canonical | PR-1 |
| **PR-4** | **Cross-Market Price Intelligence** — parity слой A+B, витрина, дашборд | PR-1, PR-3 |
| **PR-5** | **Price Policy Engine** — `PRICE_POLICY`, шестиуровневый коридор, floor из экономики | PR-2, PR-4 |
| **PR-6** | **Promotions & Conflict State** — WB календарь, Ozon `/v1/actions`, `pricing-strategy` | PR-1, PR-3 |
| **PR-7** | **Shadow Repricer** — рекомендации без записи, audit trail, дашборд «почему не меняем» | PR-5, PR-6 |
| **PR-8** | **Backtest & Validation** — historical simulation, калибровка гистерезиса | PR-7 + ≥4 нед данных |
| **PR-9** | **Execution & Verification Service** — write-инфраструктура, kill switch, verification loop. **Без включения автоматики.** | PR-8 |
| **PR-10** | **Controlled Ozon Auto-Repricing** — включение на 3–5 SKU | PR-9 + ACK владельца |
| **PR-11** | **Controlled WB Auto-Repricing** | PR-10 |
| **PR-12** | **Cross-Market Coordination** — согласованные решения по обеим площадкам | PR-10, PR-11 |
| **PR-13** | **Stock + Ads Aware Pricing** | PR-12 |
| **PR-14** | **Elasticity / Profit Optimization** | ≥6 мес истории цен |

---

## 24. COMPLEXITY MAP

| Стейдж | Сложность | Риск | Влияние на production | Основные неизвестные |
|---|---|---|---|---|
| PR-1 WB Price Observer | **M** | LOW | новая таблица, новый job; существующее не трогается | реальная частота изменения цен WB |
| PR-2 WB Tariffs & Economics | **M** | LOW | новые таблицы + вью | соответствие `kgvpMarketplace` фактической комиссии в реализации |
| PR-3 Ozon Intraday | **S** | LOW | смена ключа хранения — новая таблица, старая живёт | нужна ли частота >1/сут на практике |
| PR-4 Cross-Market Intelligence | **M** | LOW | только чтение | точность модели СПП при покрытии 20–48 % |
| PR-5 Price Policy Engine | **L** | MEDIUM | новые таблицы; решения ещё не исполняются | какой `hard_threshold` маржи считать допустимым — вопрос владельца |
| PR-6 Promotions & Conflict | **M** | LOW | только чтение | семантика `freeze_date` WB на практике |
| PR-7 Shadow Repricer | **L** | MEDIUM | пишет только рекомендации | доля решений, блокируемых guardrails |
| PR-8 Backtest | **M** | LOW | только чтение | достаточность 4 недель |
| PR-9 Execution & Verification | **L** | **HIGH** | появляется код, способный писать цены | фактическая propagation delay, поведение карантина WB |
| PR-10 Ozon Auto (3–5 SKU) | **M** | **HIGH** | **реальные цены меняются** | реакция Ozon на частые изменения, лимит 10/час |
| PR-11 WB Auto | **M** | **HIGH** | **реальные цены меняются** | карантин WB, реакция СПП на изменение цены продавца |
| PR-12 Cross-Market Coordination | **L** | HIGH | обе площадки одновременно | взаимовлияние каналов |
| PR-13 Stock + Ads Aware | **L** | MEDIUM | — | связь цены и рекламной эффективности |
| PR-14 Elasticity | **XL** | MEDIUM | — | достаточность вариации цен для идентификации модели |

---

## 25. ПРЯМЫЕ ОТВЕТЫ НА КОНТРОЛЬНЫЕ ВОПРОСЫ

### Wildberries

1. **Какая price data хранится?** Только post-factum: `RAW_WB_SALES_RETURNS`
   (`total_price`, `discount_percent`, `spp`, `price_with_disc`, `finished_price`, `for_pay`),
   `RAW_WB_ORDERS.price_with_disc`, `RAW_WB_FINANCE` (`retail_price`,
   `retail_price_withdisc_rub`, `spp_percent`, `sale_percent`). Цены продавца — нет.
2. **Как часто обновляется?** Заказы и продажи — почасово; реализация — 3×/сут с лагом 2–3 сут.
3. **Видим ли current seller price?** **Нет.** Метод есть, токена и пайплайна нет.
4. **Видим ли current buyer price?** **Нет.** Метода не существует.
5. **Видим ли СПП pre-sale?** **Нет.**
6. **Видим ли СПП post-sale?** Да: `sales.spp` (~1 ч) и `spp_percent` (2–3 сут), покрытие 20–48 % SKU/сут.
7. **Видим ли WB Club?** Через `list/goods/filter`: `clubDiscount`, `clubDiscountedPrice`. Не собираем. Это скидка **продавца**, не WB.
8. **Видим ли promotion price?** Через календарь акций. Не собираем.
9. **Как API меняет price?** `POST /api/v2/upload/task`, ≤1000 товаров, асинхронно, `uploadID`.
10. **Ограничения?** 10 req/6 с; карантин при падении цены ≥×3; статусы 1/3/4/5/6.
11. **Как проверить применение?** `GET /api/v2/history/tasks` (+`activationDate`),
    `history/goods/task`, `quarantine/goods`, и обязательно — следующий снимок наблюдателя.
12. **Как безопасно автоматизировать?** Только после PR-1…PR-9: наблюдение → тарифы →
    политика → shadow → верификация. Не раньше.

### Ozon

13. **Какая price data хранится?** `RAW_OZON_PRICES` (45 полей, 1 снимок/сут, 6 суток),
    `RAW_OZON_PRICE_COMMISSIONS`, `RAW_OZON_POSTINGS_FBO` (post-sale цена покупателя).
14. **Видим ли seller price?** Да, суточно.
15. **Видим ли buyer price?** Pre-sale — **нет** (`marketing_price` удалён).
    Post-sale — да, `posting.price_rub`, лаг до 8 ч.
16. **Видим ли Ozon-funded discounts?** Только post-sale и только названиями:
    «Скидка (за счет Озон) — DD/OA by AI benefit system». Сумм по механикам нет.
17. **Что значит `min_price`?** Минимальная цена, ниже которой Ozon не опускает товар
    при автоприменении акций (`min_price_for_auto_actions_enabled`).
    У нас = `price` для 20/20 → защиты фактически нет.
18. **Что значит `old_price`?** Зачёркнутая цена. Должна быть выше `price`;
    разница ограничена правилами 20 ₽ / 5 % / 500 ₽ / <90 %. У нас = 2,5×`price` для 20/20.
19. **Как акции влияют на цену?** При `auto_action_enabled` Ozon сам применяет акции,
    опуская цену до `min_price`. Плюс автоматические механики (AI benefit, бустинг, баллы),
    невидимые до заказа.
20. **Как API меняет price?** `POST /v1/product/import/prices`, ≤1000 товаров,
    результат пер-товарно в `result[].updated`.
21. **Ограничения?** 50 req/с на Client-Id; **≤10 изменений цены товара в час**;
    8 правил валидации скидки; пороги категории.
22. **Как проверить изменение?** По `result[].updated` + `errors[]`, затем — следующим снимком.
    Статус `price_sent` подтверждает eventual consistency.

### Cross-marketplace

23. **Есть ли canonical SKU mapping?** **Да.** `internal_sku` в `REF_SKU_CHANNEL_MAP`,
    20 из 25 SKU присутствуют на обеих площадках.
24. **Можно ли сравнить один товар?** Да, технически. Сегодня — только post-sale.
25. **Какую цену сравнивать?** Фактическую цену покупателя, а pre-sale — её модельную оценку
    с явным `confidence`. Не цену продавца.
26. **Как строить parity?** Двухслойно (§18.1): наблюдаемый слой A калибрует моделируемый слой B.
27. **Когда parity не требуется?** SKU на одной площадке (5 из 25); акция; разная экономика
    каналов; намеренное позиционирование. Цель — отсутствие **необъяснённого** разрыва.

### Economics

28. **Достаточна ли unit economics?** Для Ozon — да. Для WB — нет: нет форвардных
    комиссий, логистики, хранения, возвратов.
29. **Можно ли вычислить floor?** Ozon — да, уже есть `break_even_price_expected/worst`.
    WB — нет, до PR-2.
30. **Какие расходы отсутствуют?** WB: все форвардные. Обе площадки: налоги,
    аллокация хранения на SKU. Ozon: хранение в форвардной модели.
31. **Какие расходы динамические?** Логистика (регион/маршрут — на Ozon смоделирована
    коридором), реклама, возвраты и отмены (вероятностные), хранение (от оборачиваемости).

### System

32. **Какие новые pipelines нужны?** WB prices, WB tariffs, WB promo calendar,
    Ozon intraday prices, Ozon actions, Ozon pricing-strategy state. Итого 6.
33. **Какие таблицы нужны?** См. §14 — 13 объектов + 4 строки в `OPS_PIPELINE_REGISTRY`.
34. **Как часто собирать snapshots?** Старт 30 мин, цель 15 мин. Лимиты не биндят.
35. **Какие guardrails нужны?** См. §16.1 — 6 групп, ~20 проверок, все fail-closed.
36. **Нужен ли отдельный execution service?** **Да.** Это единственный компонент
    с доступом к write-секретам; отдельный SA, отдельный Cloud Run, отдельный деплой.
37. **Как разделить read/write?** Не платформой (WB и Ozon этого не умеют),
    а инфраструктурой: два секрета, два SA, у reader'а нет доступа к write-секрету.
38. **Как строить shadow mode?** Рекомендации в `PRICE_RECOMMENDATION`,
    write-секрет физически недоступен SA. Минимум 4 недели.
39. **Как строить audit trail?** `PRICE_RECOMMENDATION` + `PRICE_CHANGE_EVENT` +
    `PRICE_CHANGE_VERIFICATION` + `PRICE_GUARDRAIL_EVENT`, обязательный `input_snapshot_id`.
40. **Какова длина пути до production repricer?** 9 стейджей до контролируемой
    автоматики на Ozon (PR-1…PR-10), 10 — до WB. Из них 4 первых — LOW risk и без записи.

---

## 26. DATA AVAILABILITY ≠ DATA QUALITY

Оценка критичных входов по семи измерениям. [FACT для колонок 2–5]

| Вход | Availability | Freshness | Completeness | Semantic certainty | Latency | History | Reliability |
|---|---|---|---|---|---|---|---|
| Ozon `price_rub` | есть | 1×/сут | 100 % | высокая | ~сутки | 6 сут | высокая |
| Ozon `min_price_rub` | есть | 1×/сут | 100 % | **высокая, но значение фиктивно (=price)** | сутки | 6 сут | **низкая как защита** |
| Ozon commissions | есть | 1×/сут | 100 %, 0 неизвестных | высокая | сутки | 6 сут | высокая |
| Ozon posting `price_rub` | есть | 3×/сут | 100 % заказов | **средняя** (цена покупателя vs цена на момент заказа) | до 8 ч | 30 сут | средняя |
| Ozon `actions[]` | есть | 3×/сут | 100 % заказов | **низкая** (названия без сумм) | до 8 ч | 30 сут | низкая |
| Ozon `ozon_actions_exist` | есть | 1×/сут | 100 % | **низкая** — FALSE при фактически применённых механиках | сутки | 6 сут | **низкая** |
| WB `spp` (sales) | есть | почасово | **20–48 % SKU/сут** | высокая | ~1 ч | с 2026-03-30 | средняя |
| WB `spp_percent` (реализация) | есть | 3×/сут | 99,5 % строк продаж | высокая | 2–3 сут | полная | высокая |
| WB `finished_price` | есть | почасово | 20–48 % SKU/сут | высокая | ~1 ч | с 2026-03-30 | средняя |
| WB `for_pay` (sales) | есть | почасово | 100 % продаж | **низкая** — в коде помечено «для P&L НЕ использовать» | ~1 ч | полная | **не использовать** |
| WB цена продавца | **НЕТ** | — | — | — | — | — | — |
| WB комиссия форвардно | **НЕТ** | — | — | — | — | — | — |
| COGS | есть | по изменению | 23/25 SKU | высокая (PROVEN_DOCUMENT / DERIVED) | — | SCD-2 | высокая |
| SKU mapping | есть | 1×/сут | 25 WB / 20 OZ | высокая | — | SCD-2 | высокая |

**[INFERENCE]** Три поля выглядят пригодными, но не пригодны, и это ловушки:
`ozon_actions_exist` (FALSE при работающих механиках), `min_price_rub` (равна цене),
`for_pay` из sales (предварительная сумма). Каждое из них при наивном использовании
даст неверный ценовой пол.

---

## 27. SOURCE DISCIPLINE

**Официальная документация Wildberries** (dev.wildberries.ru):
методы «Цены и скидки», лимиты запросов, статусы загрузок, правило карантина,
схема `list/goods/filter`, методы «Тарифы», календарь акций,
дайджест WB API за август 2026 (Order Feed, `sellerPrice`, `final-price` для DBS).

**Официальная документация Ozon** (docs.ozon.ru/api/seller):
прямой доступ из окружения заблокирован (HTTP 403 / anti-bot). Использовано
публичное зеркало документации `github.com/etozhearut/ozon-seller-api` —
методы `/v5/product/info/prices`, `/v1/product/import/prices`, `/v1/actions/*`,
`/v1/seller-actions/*`, `/v1/pricing-strategy/*`, полный перечень кодов ошибок,
лимиты. **[NON-AUTH: зеркало официальной документации, не сам первоисточник.]**
Каждое критичное утверждение по Ozon перепроверено живыми данными BigQuery
(наличие и заполненность полей, состав `commissions`, содержимое `actions[]`).

**Удаление `marketing_price`:** дата 12.11.2025 взята из
`cleverence.ru` и `marketparser.ru` — **[NON-AUTH]**. Подтверждено косвенно:
поля нет в актуальной схеме метода и нет в нашем загрузчике.

**Репозиторий и BigQuery** — первичные источники для всех утверждений
о текущем состоянии EVETIS.

Форум WB API использован только для одного утверждения (отсутствие рабочего
способа получить СПП) — **[NON-AUTH]**, и оно согласуется с отсутствием поля
в официальной схеме.

---

## RECOMMENDED NEXT STAGE

```
RECOMMENDED NEXT STAGE:
  PR-1 — Wildberries Price Observer (read-only)

WHY:
  Это единственный P0-блокер, который блокирует всё остальное.
  Сегодня система не наблюдает цену продавца WB ни в каком виде.
  Пока её нет:
    • нельзя построить паритет (одна сторона слепа);
    • нельзя верифицировать запись (не с чем сравнить);
    • нельзя откалибровать модель СПП (нет знаменателя);
    • нельзя объяснить разрыв WB/Ozon ×2, найденный в этом стейдже.
  Стоимость минимальна: один POST-запрос на полный снимок 25 SKU
  при лимите 10 запросов / 6 секунд. Запас по лимиту — 500×.
  Ozon-наблюдатель уже работает, поэтому WB — правильная точка входа.

PRECONDITIONS:
  1. Владелец создаёт токен WB категории «Цены и скидки» и кладёт значение
     в Secret Manager под именем WB_TOKEN_PRICES_READ.
     Terraform значения токенов не хранит — это ручное действие владельца.
  2. Владелец подтверждает (ACK), что PR-1 не содержит write-кода
     и что write-токен на этом этапе НЕ создаётся.
  3. Владелец принимает решение по разрыву WB/Ozon ×2: это ошибка
     ценообразования на WB, на Ozon, или намеренное позиционирование.
     Ответ определяет target_price в PR-5 и не может быть выведен из данных.

PRODUCTION RISK:
  LOW.
  • Существующие пайплайны, таблицы и витрины не изменяются.
  • Новые объекты: 1 таблица (wb_raw.RAW_WB_PRICES), 1 Cloud Run Job,
    1 Cloud Scheduler, 1 service account, 1 строка в OPS_PIPELINE_REGISTRY.
  • Вызывается ровно один endpoint, и он read-only.
  • Запись цен архитектурно невозможна: у service account'а нет write-секрета.
  • Откат: удалить job, scheduler и таблицу. Ничто от них не зависит.

EXPECTED OUTPUT:
  • wb_raw.RAW_WB_PRICES — append-only снимки цен WB
    (nmID, price, discount, discountedPrice, clubDiscount, clubDiscountedPrice,
     editableSizePrice, wholesaleDiscountThreshold, isBadTurnover, observed_at),
    партиция по DATE(observed_at), кластер по nm_id.
  • Первая версия PRICE_SNAPSHOT_CANONICAL на двух источниках.
  • Измеренная фактическая частота изменения цен WB — фактическое основание
    для выбора каденса intraday (сейчас это гипотеза, а не знание).
  • Первый в истории проекта ряд «цена продавца WB во времени».
  • Данные для калибровки модели СПП: пара (цена продавца, цена покупателя)
    на тех SKU, где были продажи.
```

**Переход к PR-1 не выполняется.** Требуется решение владельца по трём preconditions.

---

## КОНТРОЛЬНЫЕ ЦИФРЫ ДЛЯ ПРОВЕРКИ ЭТОГО ОТЧЁТА

Любое утверждение ниже воспроизводится одним запросом.

| Проверка | Ожидаемое значение |
|---|---|
| Датасетов в проекте (region-eu) | 8 |
| Ценовых таблиц в `wb_raw` | 0 |
| `COUNT(*) FROM ozon_raw.RAW_OZON_PRICES` | 120 |
| Уникальных `snapshot_date` там же | 6 (пропуски 01.09, 02.09) |
| `COUNTIF(min_price_rub = price_rub)` на 07.09 | 20 из 20 |
| `COUNTIF(ozon_actions_exist)` за всё время | 0 |
| `COUNT(DISTINCT internal_sku)` в `REF_SKU_CHANNEL_MAP WHERE is_current` | 25 |
| Из них с обеими площадками | 20 |
| `COUNT(DISTINCT internal_sku)` в `V_PRODUCT_COGS_EFFECTIVE` (текущие) | 23 |
| Строк `supplier_oper_name='Продажа'` в `RAW_WB_FINANCE` | 40 385 |
| Из них `spp_percent > 0` | 40 203 |
| Средняя `spp_percent` по продажам | 15,65 % |
| Медианный лаг реализации (`_rr_date` → `loaded_at`), с 01.07 | 2 суток |
| Строк в `V_WB_SALES_RETURNS` | 4 272 |
| Уникальных nm_id с продажами 06.09 | 7 |
| `agent_write_ready` в `V_OZON_AGENT_DECISION_INPUT` | 0 из 20 |
| `agent_shadow_ready` там же | 20 из 20 |
| Медиана WB/Ozon по цене покупателя (14 сут) | ≈ 0,49 |
| Секретов WB категории «Цены и скидки» | 0 |
