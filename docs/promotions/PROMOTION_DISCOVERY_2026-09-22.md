# EVETIS — Promotion & Pricing Intelligence Engine
# Фаза 0: форензик-обследование (Wildberries + Ozon)

Дата: 2026-09-22 · Режим: **READ-ONLY**. Ни одной мутирующей операции не выполнено:
ни активации/деактивации акций, ни изменения цен и скидок, ни правки production-объектов
BigQuery, ни правки Scheduler, ни деплоя. Секреты не покидали Secret Manager и не
печатались ни в логах, ни в этом отчёте.

Классы доказательств, используемые ниже:
`CONFIRMED_API` — наблюдено в ответе боевого API на аккаунте EVETIS сегодня ·
`CONFIRMED_DOCS` — официальная документация площадки ·
`CONFIRMED_EXISTING_CODE` — прочитано в репозитории/production BigQuery ·
`LIKELY` — согласованное косвенное свидетельство ·
`UNPROVEN` — гипотеза без подтверждения.

---

## EXECUTIVE VERDICT

**Ozon: движок строится полностью.** Seller API отдаёт всё, что нужно для решения
ENTER/STAY/EXIT по каждому SKU: список акций, кандидатов, участников, требуемую и
максимально допустимую акционную цену, границы эластичного бустинга, требования к остатку,
дату заморозки и — отдельным методом — **точный список товаров, которые площадка
автоматически добавит в акцию, с датой и ценой автодобавления**. Все 18 товаров, которые
сегодня фигурируют в акциях Ozon, резолвятся в `internal_sku` без единого пробела.

**Wildberries: движок строится наполовину, и это не наша вина.** Календарь акций доступен
и богат (включая коэффициенты бустинга по уровням участия и историю за 2 года), но
**на аккаунте EVETIS сегодня нет ни одной акции типа `regular`** — все 10 открытых акций
имеют тип `auto`, а метод выдачи товаров акции официально **не применим к автоакциям**.
Следствие: по WB наблюдаемы акция, её условия, бустинг и **агрегатное число наших товаров
внутри**, но **не наблюдаемы ни конкретные SKU, ни плановая акционная цена**. Экономику
входа по WB детерминированно посчитать нельзя — не из-за нашей архитектуры, а из-за
контракта API.

**Существующая экономика EVETIS готова принять движок без второй формулы прибыли.**
`wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` и `ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`
уже считают вклад, точку безубыточности и предельный ДРР по одной и той же алгебре
`contribution(P) = P·(1 − take) − fixed − COGS`; обе fail-closed по отсутствующим входам.
Движку акций нужно переиспользовать их, подставив акционную цену вместо текущей.

**Главный пробел пайплайна — не в акциях, а в том, что мы уже получаем и выбрасываем.**
Подтверждено: `marketing_actions.actions[]` из `/v5/product/info/prices` отбрасывается
([`pipelines/ozon/runtime/entities.py:175`](../../pipelines/ozon/runtime/entities.py#L175)).
Хуже: сохраняемый вместо него флаг `ozon_actions_exist` **систематически лжёт** — сегодня он
`FALSE` у 20 из 20 SKU, тогда как `/v1/actions/products` показывает 18 участвующих товаров.

---

## 1. ТЕКУЩАЯ АРХИТЕКТУРА EVETIS, РЕЛЕВАНТНАЯ АКЦИЯМ

### 1.1 Что реально работает `CONFIRMED_EXISTING_CODE`

| Контур | Реализация | Каденс (Cloud Scheduler, проверено `gcloud scheduler jobs list`) |
|---|---|---|
| WB: заказы, продажи, финансы, реклама, остатки, справочник | Google Apps Script (`apps-script/ingestion/`) — **production**, не legacy | собственные триггеры Apps Script |
| WB: наблюдатель цен | Cloud Run Job `wb-prices-prod` (`cloud/src/loaders/prices/`) | `*/20 * * * *` ENABLED |
| WB: тарифы | `wb-tariffs-prod` | `15 5 * * *` ENABLED |
| WB: воронка | `wb-funnel-prod` | `30 6 * * *` ENABLED |
| WB: витрина | `wb-mart-prod` | `0 7,9,12,16 * * *` ENABLED |
| WB: платное хранение | `wb-paid-storage-prod` | `45 8 * * *` ENABLED |
| Ozon: весь ingestion | Cloud Run Job (`pipelines/ozon/runtime/`, Python) | `ozon-daily 30 6`, `ozon-fast 0 7,13,19`, `ozon-weekly 0 5 * * 1` |
| Слои Executive V2 / SKU V2 | `executive-v2-layer-build 10 7-23`, `sku-performance-v2-layer-build 20 7-23` | ENABLED |
| Здоровье пайплайнов | `wb-ops-health-prod` | `0 */3 * * *` ENABLED |

Датасеты BigQuery (регион EU): `wb_raw`, `wb_mart`, `wb_ops`, `ozon_raw`, `ozon_mart`,
`ozon_stg` (пуст), `evetis_ref`, `evetis_mart`, `evetis_ops`, `evetis_communications`.

### 1.2 Экономический слой, который движок обязан переиспользовать `CONFIRMED_EXISTING_CODE`

| Объект | Роль | Ключевое |
|---|---|---|
| `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | себестоимость, датированная | авторитетный DDL — `sql/ref/stage3_4b1_management_landed_cogs.sql`; **проверено: вью существует** |
| `evetis_ref.V_BUNDLE_COGS_DERIVED` | себестоимость наборов | — |
| `wb_mart.V_WB_SKU_COST_INPUTS` | входы стоимости WB | комиссия base+addon, эквайринг p50/p75/p90, логистика 90 сут |
| `wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` | форвардная экономика WB, модель `WB_FE_V1`, `PRE_TAX` | `contribution_before_ads_pre_tax_rub/pct`, `break_even_before_ads_pre_tax_{base,conservative,stress}`, `max_affordable_drr_pre_tax_pct`, `economics_status`, `blocked_reason` |
| `wb_mart.TVF_WB_FORWARD_ECONOMICS(scenario_price, ad_drr_pct, required_contribution_rub)` | **уже существующая параметризация по цене** | проверено в `INFORMATION_SCHEMA.ROUTINES`; ограничение — одна цена на все SKU сразу |
| `ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` | форвардная экономика Ozon | `contribution_{best,expected,worst}`, `break_even_price_{expected,worst}`, лестница `target_price_{10,15,20,25}pct_*`, `safety_class_worst_case`, `agent_decision_gate` |
| `ozon_mart.V_OZON_AGENT_DECISION_INPUT` | **уже существующий контракт агентных решений** | `agent_decision_gate`, `agent_shadow_ready/gate`, `agent_write_ready/gate` |
| `wb_mart.V_CT_INVENTORY_TRUTH` | канонические остатки, WB+Ozon+ФФ | `sellable_units`, `days_of_stock_*`, `units_per_day_{7,30}d`, `inbound_eta`, `days_to_expiry` |
| `evetis_mart.FACT_SKU_DAILY` | нейтральный суточный факт обоих маркетплейсов | грейн `fact_date × marketplace × internal_sku`; WB с 2024-09-05, OZON с 2025-04-26 |
| `evetis_ref.REF_SKU_CHANNEL_MAP` | резолв идентификаторов | есть колонка `marketplace_product_id` — прямое соответствие `product_id` Ozon |
| `wb_ops.OPS_PIPELINE_REGISTRY` / `OPS_HEALTH_STATE` / `OPS_INCIDENT` / `OPS_ALERT_EVENT` | **готовая инцидентная и алертинговая подсистема** | дедуп по `condition_fingerprint`, `reason_code`, `reminder_bucket`, каналы доставки |

**Алгебра обеих площадок совпадает** `CONFIRMED_EXISTING_CODE`:

```
contribution(P) = P · (1 − take_frac) − fixed_rub − COGS
break_even(P)   = (fixed_rub + COGS) / (1 − take_frac)
```
WB: `take = комиссия% + эквайринг%`, `fixed = E[логистика]`.
Ozon: `take = commission_pct + acquiring_pct`, `fixed = last_mile_rub + logistics_expected_rub`
(проверено по `sql/current/ozon_mart/V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.sql:83`).
Налог не входит ни там, ни там: `tax_model_status = 'PRE_TAX'`.

### 1.3 Именная коллизия, которую нельзя допустить `CONFIRMED_EXISTING_CODE`

В EVETIS слово **ACTION уже занято**: `evetis_ref.CT_ACTION_STATUS_LOG`,
`CT_OWNER_ACTION_QUEUE`, `wb_mart.V_CT_ACTION_CANDIDATES`, `V_CT_ACTION_QUEUE`,
`evetis_ref.sp_ct_generate_actions` — это **задачи владельца Control Tower**, а не акции
маркетплейса. Все новые объекты акций обязаны называться `PROMO`, а не `ACTION`.

---

## 2. ЧТО МЫ УЖЕ ПОЛУЧАЕМ И ВЫБРАСЫВАЕМ

### 2.1 `marketing_actions.actions[]` — подтверждено `CONFIRMED_EXISTING_CODE`

[`pipelines/ozon/runtime/entities.py:175`](../../pipelines/ozon/runtime/entities.py#L175):

```python
ozon_actions_exist=(i.get("marketing_actions") or {}).get("ozon_actions_exist"),
```

Из блока `marketing_actions` ответа `POST /v5/product/info/prices` сохраняется **ровно один
булев флаг**. Массив `actions[]` (`title`, `value`, `date_from`, `date_to` — по описанию
`docs/ozon/audit_2026-08-30/LOG.md:329`) не читается и не пишется никуда. Ни `RAW_OZON_PRICES`,
ни `RAW_OZON_PRICE_COMMISSIONS` колонок под него не имеют. Находка P1-1 из
`docs/pricing/EVETIS_PR0_PRICING_DISCOVERY_2026-09-07.md:714` подтверждается дословно.

Сохраняются при этом два полезных флага автоакций:
`auto_action_enabled` и `auto_add_to_ozon_actions_enabled`
(← `price.auto_add_to_ozon_actions_list_enabled`).

### 2.2 Флаг, который лжёт — новое доказательство `CONFIRMED_API` + `CONFIRMED_EXISTING_CODE`

BigQuery, `ozon_raw.RAW_OZON_PRICES`, каждый снимок 2026-09-11 … 2026-09-22:

| snapshot_date | строк | `ozon_actions_exist = TRUE` | `auto_action_enabled` | `auto_add_to_ozon_actions_enabled` | `min_price = price` |
|---|---|---|---|---|---|
| каждая из 12 дат | 20 | **0** | 0 | **20** | **20** |

Тем же утром `POST /v1/actions/products` вернул **18 участвующих товаров** в девяти акциях.

**Вывод `CONFIRMED_API`:** `marketing_actions.ozon_actions_exist` **не является индикатором
участия в акциях** и не должен использоваться ни в одном решении. Прежняя формулировка
PR-0 («флаг отражает участие в акциях Ozon с явным списком товаров») опровергнута: списки
товаров явные, участие есть, флаг `FALSE`.

**Вывод `CONFIRMED_API`:** `min_price` равна `price` у 20 из 20 SKU, то есть механизм Ozon,
штатно предназначенный ограничивать автоприменение скидок, **не ограничивает ничего**.

### 2.3 Прочие пробелы

| Пробел | Статус |
|---|---|
| Календарь акций WB не вызывается нигде | `CONFIRMED_EXISTING_CODE` — 0 упоминаний `dp-calendar-api` в коде |
| `/v1/actions*` Ozon не вызывается нигде | `CONFIRMED_EXISTING_CODE` — 0 совпадений в `pipelines/`, `cloud/`, `apps-script/` |
| Нет ни одной таблицы истории состояния акций | `CONFIRMED_EXISTING_CODE` — ни одного объекта `*PROMO*` в 10 датасетах |
| `cloud/src/loaders/prices/constants.ts` перечисляет `wholesaleDiscountThreshold` и `isBadTurnover`, которых WB больше не возвращает | `CONFIRMED_API`, дрейф безобидный: детектор пропажи работает только по `REQUIRED_*`, прогон не падает |

---

## 3. WILDBERRIES — ПРОВЕРКА API СЕГОДНЯ

Хост `https://dp-calendar-api.wildberries.ru`, токен категории «Цены и скидки»
(`WB_PRICES_READ_TOKEN`). **Ключевой факт безопасности `CONFIRMED_API`:** в JWT этого токена
установлен **бит 30 (read-only)**, срок действия до 2027-03-08. То есть на WB запись
акций невозможна не политикой, а самим носителем права. `/ping` обоих хостов — 200.

### 3.1 `GET /api/v1/calendar/promotions` — работает `CONFIRMED_API`

Поля: `id`, `name`, `startDateTime`, `endDateTime`, `type`.
Обязательные параметры: `startDateTime`, `endDateTime`, `allPromo`; опциональные `limit` (1..1000), `offset`.

Окно −120 … +180 суток: **70 акций, из них 63 `auto` и 7 `regular`**.
`allPromo=false` и `allPromo=true` вернули **одинаковые 70** — для этого аккаунта флаг
ничего не сужает.

Глубина истории `CONFIRMED_API`:

| запрошенное окно назад | вернулось акций | самая ранняя |
|---|---|---|
| 180 сут | 85 | 2026-03-10 |
| 365 сут | 143 | 2025-09-03 |
| 730 сут | 242 | **2024-10-06** |

То есть **каталог акций WB доступен ретроспективно минимум на 2 года** — историю самих
акций можно забэкфилить одним запросом. Вперёд горизонт публикации — до 2026-10-21
(29 суток от сегодня).

Все 7 `regular` пришлись на май–август 2026 («Экспресс-скидки», «Скидки на товары без
заказов», «Скидки на немаркированные товары»). **В текущем и будущем окне — ноль `regular`.**

### 3.2 `GET /api/v1/calendar/promotions/details` — работает и даёт бустинг `CONFIRMED_API`

Поля: `id`, `name`, `description`, `advantages[]`, `startDateTime`, `endDateTime`,
`inPromoActionLeftovers`, `inPromoActionTotal`, `notInPromoActionLeftovers`,
`notInPromoActionTotal`, `participationPercentage`, `type`, `exceptionProductsCount`,
`ranging[]{condition, participationRate, boost}`.

Срез по 10 актуальным акциям на 2026-09-22:

| id | окно | наших в акции | наших вне акции | участие,% | исключено | лестница бустинга (участие% → бустинг%) |
|---|---|---|---|---|---|---|
| 2940 «Большая распродажа - 2» | 27.09–21.10 | **1** | 4 | 20 | 0 | 1→25, 50→30, 80→35 |
| 2936 «Большая распродажа» | 27.09–21.10 | 0 | 8 | 0 | 0 | 1→25, 50→30, 80→35 |
| 2932 «Бархатные скидки: финал» | 23.09–27.09 | 4 | 9 | 31 | 0 | 1→30, 30→35 |
| 2915 «Бархатные: обновление цен» | 20.09–27.09 | 5 | 13 | 28 | 0 | 1→30, 40→35 |
| 2907 «Бархатные: большой выбор» | 17.09–27.09 | 5 | 13 | 28 | 0 | 1→30, 60→35 |
| 2890 «Бархатные: хиты» | 14.09–27.09 | 4 | 18 | 18 | 0 | 1→25, 20→30, 60→35 |
| 2873 «Осенние скидки» | 11.09–09.10 | 5 | 13 | 28 | 0 | 1→25, 40→30, 70→35 |
| 2866 «Бархатные: доп. бустинг» | 09.09–23.09 | 4 | 14 | 22 | 0 | 1→25, 50→30, 70→35 |
| 2848 «Бархатные скидки - 2» | 03.09–27.09 | 3 | 4 | 43 | 0 | 1→25, 60→30 |
| 2840 «Бархатные скидки» | 03.09–27.09 | 2 | 19 | 10 | 0 | 1→25, 50→30, 80→35 |

**Коммерческий факт, ранее не зафиксированный нигде в репозитории:** товары EVETIS уже
участвуют в восьми автоакциях WB, и один товар уже попал в «Большую распродажу - 2»,
которая стартует 27.09. `exceptionProductsCount = 0` — **ни один SKU не исключён из
автоакций**. Документ `docs/pricing/WB_PRICE_RECOMMENDATIONS_2026-09-21.md` ставил цены,
не учитывая это состояние, потому что оно не наблюдалось.

`details` по завершённым акциям (2728, 2642) возвращает **пустой массив** — детали живут
только для текущих и будущих акций `CONFIRMED_API`.

### 3.3 `GET /api/v1/calendar/promotions/nomenclatures` — недоступен `CONFIRMED_API` + `CONFIRMED_DOCS`

Перебор контракта:

| запрос | ответ |
|---|---|
| без параметров / только `promotionID` / `promotionIDs` / `id` / `promotionID+limit` | `400 Invalid query params` |
| `promotionID` + `inAction` (+`limit`,`offset` в любых сочетаниях, `true/false/True/0`, `Bearer`) | `422 Unprocessable entity` |
| для `auto`-акций 2940, 2873, 2840 | `422` |
| для `regular`-акций 2728, 2730, 2739, 2642, 2423, 2481, 2507 (все завершены) | `422` |
| для несуществующих `promotionID` 1 и 999999 | `422` |

Переход `400 → 422` при добавлении `inAction` доказывает, что **пара `promotionID`+`inAction`
и есть контракт**; отказ содержательный, а не синтаксический. Официальная документация
(`dev.wildberries.ru/docs/openapi/promotion`) прямо оговаривает: **«Данный метод неприменим
для автоакций»** `CONFIRMED_DOCS`.

Схема, которую метод отдал бы на `regular`-акции `CONFIRMED_DOCS`:
`nomenclatures[]{ id, inAction, price, currencyCode, planPrice, discount, planDiscount }`.
Именно `planPrice`/`planDiscount` — недостающая плановая акционная цена.

**Вывод.** Сегодня WB **не отдаёт ни одного поля уровня SKU × акция**. Плановая акционная
цена по WB — `UNPROVEN` величина, которую нельзя ни получить, ни вывести. Как только
появится акция `type = "regular"`, метод заработает, поэтому загрузчик обязан её ждать.

### 3.4 Лимиты и версии `CONFIRMED_API` + `CONFIRMED_DOCS`

- `/api/v2/calendar/promotions` и `/api/v2/.../nomenclatures` → **404** с `origin: ag-dp-calendar`. Актуальна только v1 `CONFIRMED_API`.
- Лимит на аккаунт продавца для **всех** методов категории «Календарь акций»:
  **10 запросов / 6 с, интервал 600 мс, всплеск 5** `CONFIRMED_DOCS`.
- Мутирующий метод категории: `POST /api/v1/calendar/promotions/upload` — **в deny-list**.

### 3.5 СПП — ответ прежний `CONFIRMED_DOCS`

Официального метода Seller API, отдающего текущую СПП до заказа, не существует; календарь
акций её не возвращает. `GET /api/v2/list/goods/filter` сегодня отдаёт по 25 SKU:
`nmID, vendorCode, sizes[], currencyIsoCode4217, discount, clubDiscount, editableSizePrice`,
внутри `sizes[]`: `sizeID, price, discountedPrice, clubDiscountedPrice, techSizeName`.
У всех 25 SKU `discount = 60`, `clubDiscount = 0`, `clubDiscountedPrice = discountedPrice`.

**Значение СПП 32 %, использованное в `docs/pricing/WB_PRICE_RECOMMENDATIONS_2026-09-21.md`
(«Полка WB = цена × 0,68»), — это ДОПУЩЕНИЕ, а не наблюдённый факт площадки.**
Наблюдаемая величина — post-sale `spp_percent` из детализации реализации: за окно с
2026-07-01 средняя 15,65 % по строкам продаж (`docs/pricing/EVETIS_PR0_...:333`). Движок
акций не имеет права материализовать 32 % ни в одной формуле; цена покупателя WB остаётся
ненаблюдаемой до заказа.

---

## 4. OZON — ПРОВЕРКА API СЕГОДНЯ

Хост `https://api-seller.ozon.ru`, ключ `EVETIS_OZON_API_KEY` + `EVETIS_OZON_CLIENT_ID`.
**Риск, который надо назвать прямо:** ключ EVETIS имеет роль Admin, то есть мутирующие
методы акций им технически доступны. На WB запись отрезана самим токеном, на Ozon — нет.
Граница записи для этой подсистемы может быть только программной (см. §16 спецификации).

### 4.1 `GET /v1/actions` `CONFIRMED_API`

Поля: `id`, `title`, `action_type`, `description`, `date_start`, `date_end`,
`auto_add_dates[]`, `freeze_date`, `potential_products_count`, `participating_products_count`,
`is_participating`, `is_voucher_action`, `banned_products_count`, `with_targeting`,
`order_amount`, `discount_type`, `discount_value`.
Пагинации нет — метод отдаёт весь список.

Снимок 2026-09-22 — **12 акций, участвуем в 9**:

| id | тип | окно | участ. | доступно | в акции | заморозка | автодобавление |
|---|---|---|---|---|---|---|---|
| 1977747 «Эластичный бустинг» | `ELASTIC_BOOSTING` | 19.03.25–31.12.26 | да | 2 | **18** | — | 24.09, 01.10 |
| 4113100 «Склады. Москва и МО» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 2 | 14 | — | — |
| 4113126 «Склады. СЗФО» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 4 | 14 | — | — |
| 4113309 «Склады. Поволжье» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 2 | 13 | — | — |
| 4113327 «Склады. Юг» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 4 | 12 | — | — |
| 4113333 «Склады. Урал» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 2 | 10 | — | — |
| 4113338 «Склады. Сибирь» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 5 | 7 | — | — |
| 4113110 «Склады. Центр» | `STOCK_DISCOUNT` | 22.08–30.09 | да | 0 | 5 | — | — |
| 4253043 «Максимальный бустинг» | `STOCK_DISCOUNT` | 08.09–06.10 | да | 17 | 1 | — | 22.09, 29.09 |
| 4273875 «Максимальный бустинг: усиление» | `STOCK_DISCOUNT` | 13.09–06.10 | нет | 18 | 0 | — | 27.09 |
| 4293097 «Распродажа стока. Сентябрь 2.0» | `STOCK_DISCOUNT` | 16.09–28.09 | нет | 4 | 0 | — | — |
| 4113348 «Склады. Дальний Восток» | `STOCK_DISCOUNT` | 22.08–30.09 | нет | 1 | 0 | — | — |

Семантика `freeze_date` `CONFIRMED_DOCS`: «Если поле заполнено, продавец не может повышать
цены, изменять список товаров и уменьшать количество единиц товаров в акции». Сегодня пусто
у всех 12 — но это дедлайн, который обязан входить в жизненный цикл.

### 4.2 `POST /v1/actions/candidates` и `POST /v1/actions/products` `CONFIRMED_API`

Одна и та же схема элемента, разный смысл: `candidates` — доступные, но не участвующие;
`products` — участвующие.

`result{ products[], total, last_id }`, элемент:
`id` (= `product_id`), `price`, `action_price`, `max_action_price`,
`alert_max_action_price`, `alert_max_action_price_failed`, `add_mode`, `stock`, `min_stock`,
`current_boost`, `price_min_elastic`, `price_max_elastic`, `min_boost`, `max_boost`.

Наблюдено:

- **`action_price = price` у всех 18 участников «Эластичного бустинга»** — участие сегодня
  не стоит ни рубля вклада, а бустинг при этом от 15 % до 55 % (`current_boost`: 15 у пяти
  SKU, 55 у двух, промежуточные 15,4–50,2). Это бесплатная видимость.
- `max_action_price` **может быть выше текущей цены** (438775617: цена 1442, максимум 1726).
- `price_min_elastic` > `price_max_elastic` в парах, где есть бустинг (763 / 644 при
  `min_boost` 15, `max_boost` 55): **`*_min_elastic` соответствует минимальному бустингу
  (цена выше), `*_max_elastic` — максимальному (цена ниже)** `LIKELY` — из согласованности
  чисел, официального описания подполей в документации нет.
- `add_mode` у **всех** участников = `MANUAL`. Ни одного `AUTO` сегодня.
- `stock = 0` у всех, при этом FBO-остатки ненулевые → поле `stock` в этом ответе **не есть
  складской остаток** `LIKELY`, вероятно резерв под акцию. `min_stock` заполняется (2, 3, 12
  в «Распродаже стока») — это требование к количеству.
- `alert_max_action_price_failed = false` и `alert_max_action_price = 0` везде — пример из
  документации показывает `true`/`31`, значит поле — сигнал «требуемая цена не проходит»
  `LIKELY`.

Пагинация: `offset` **помечен устаревшим**, актуален `last_id` `CONFIRMED_DOCS`.
Проверено эмпирически: `limit = 1000` → `400`, рабочий потолок 100 `CONFIRMED_API`.

### 4.3 `POST /v1/actions/auto-add/products/candidates` и `.../list` — ключевая пара `CONFIRMED_API`

Обязательные поля: `action_id`, `auto_add_date` (строго из `auto_add_dates` метода
`/v1/actions`), `limit` 1..100, опционально `offset`.

Схема элемента:
`product_id`, `offer_id`, `sku`, `name`, `price`, `base_price`, `max_discount_price`,
`min_seller_price`, `marketplace_seller_price`, `action_price_to_auto_add`,
`min_action_quantity`, `quantity_to_auto_add`, `currency`, `add_mode` (только в `/list`).

**Различие, которое решает всё:**

| метод | смысл | что вернул сегодня |
|---|---|---|
| `/candidates` | **кого площадка МОЖЕТ автодобавить** | 17 SKU для 4253043, 18 для 4273875, 12 для 1977747 |
| `/list` | **кого площадка ДОБАВИТ**, с `add_mode` | 12 SKU `AUTO` на 01.10 (1977747), 1 SKU `AUTO` на 22.09 (4253043), 6 SKU `MANUAL` на 24.09, по 0 на 29.09 и 27.09 |

Фактически запланированное автодобавление (`add_mode = AUTO`) `CONFIRMED_API`:

| дата | акция | offer_id | цена | автоцена | Δ |
|---|---|---|---|---|---|
| 2026-10-01 | 1977747 | 305101272, 305101361, 252442517, 535581674, 535581675, 535580776, 868597351, 910330849, 910584041, 930334395, 593111986, 593111985 | = текущей | = текущей | **0 по всем 12** |
| 2026-09-22 | 4253043 | 868597351 | 3250 | 3279 | **+29** |

**Вывод: ни одно запланированное автодобавление не снижает цену.** 01.10 двенадцать SKU
получат бустинг «Эластичного бустинга» бесплатно.

### 4.4 Почему нельзя было бы остановиться на `/candidates` — демонстрация ценности движка

Если бы мы читали только `/candidates` и считали, что площадка добавит всех кандидатов,
картина по акции 4253043 выглядела бы так (вклад считан по
`ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`, режим expected, формула §1.2):

| offer_id | цена | автоцена | Δ% | вклад/шт сейчас | вклад/шт в акции | вклад,% | флаг | требуемый рост продаж, чтобы сохранить вклад/день |
|---|---|---|---|---|---|---|---|---|
| 930334396 | 1450 | 1130 | −22,1 | 160 | **9** | 0,8 | ниже худшей BE | **+1592 %** |
| 930334395 | 1620 | 1299 | −19,8 | 240 | 89 | 6,8 | ниже худшей BE | +170 % |
| 930334397 | 1570 | 1302 | −17,1 | 272 | 146 | 11,2 | ниже худшей BE | +86 % |
| 567668635 | 1870 | 1234 | −34,0 | 516 | 218 | 17,6 | ниже пола 20 % | +137 % |
| 305101361 | 1448 | 946 | −34,7 | 448 | 212 | 22,4 | — | +111 % |
| 910584041 | 3088 | 2148 | −30,4 | 900 | 458 | 21,3 | — | +96 % |
| 252442517 | 980 | 918 | −6,3 | 130 | 101 | 11,0 | ниже худшей BE | +29 % |
| … 17 SKU всего | | | | **6712** | **3932** | | | **портфель −41,4 % вклада на единицу** |

Эти цифры — не прогноз и не текущее состояние, а **экономика гипотетического входа**
по ценам, которые Ozon сам назвал допустимыми. Она показывает, ради чего строится движок:
разница между «Ozon предлагает» и «Ozon сделает» стоит 41 % вклада на единицу, и сегодня
эту разницу в EVETIS никто не наблюдает — её приходится проверять руками в кабинете.

### 4.5 `POST /v1/actions/discounts-task/list` — работает, но содержит ПДн `CONFIRMED_API`

Параметры: `status`, `page` (≥1), `limit`. Значение `UNKNOWN` роняет соединение (наблюдён
reset peer) — использовать только документированные статусы.

| статус | строк вернулось |
|---|---|
| NEW | 0 |
| SEEN | 0 |
| APPROVED / PARTLY_APPROVED / DECLINED / AUTO_DECLINED / DECLINED_BY_CLIENT | по 50 (первая страница) |

Схема включает **персональные данные покупателей**: `customer_name`, `first_name`,
`last_name`, `patronymic`, `email`, `user_comment`. Экономически полезны:
`requested_price`, `approved_price`, `approved_discount_percent`, `min_auto_price`,
`base_price`, `original_price`, `approved_quantity_min/max`, `edited_till`, `end_at`,
`is_auto_moderated`, `sku`, `offer_id`, `status`.

**Требование:** если «Хочу скидку» вообще будет загружаться, поля ПДн обязаны
отбрасываться в загрузчике до записи в BigQuery — не маскироваться вью, а не попадать
в строку. Это единственный обнаруженный источник ПДн во всём периметре акций.

### 4.6 Что у Ozon отсутствует `CONFIRMED_API` + `CONFIRMED_DOCS`

- `/v1/actions/hotsales/{list,products,activate,deactivate}` → **404**. Методы объявлены
  устаревшими и удалены из документации 11.03.2025. Механики Hot Sale наблюдать нечем.
- `/v1/seller-actions` (без суффикса) → 404; раздел «Акции продавца» состоит из
  `create/*`, `update/*`, `products/add`, `products/candidates` — то есть **чтения списка
  собственных акций продавца отдельным методом нет**, а EVETIS своих акций не ведёт.
- `/v1/actions/auto-add/products` (без `list`/`candidates`) → 404.

### 4.7 Лимиты `CONFIRMED_DOCS`

Общий лимит Seller API: **50 запросов в секунду на один Client-Id**; отдельных лимитов для
методов акций в документации нет. Ошибка `Circle is open` — временная блокировка метода при
большом числе запросов.

---

## 5. РЕЗОЛВ ИДЕНТИФИКАТОРОВ

`CONFIRMED_API` + `CONFIRMED_EXISTING_CODE`: 18 уникальных `product_id`, встречающихся
в акциях Ozon, резолвятся через `evetis_ref.REF_SKU_CHANNEL_MAP` (`marketplace = 'OZON'`,
`is_current`, `marketplace_product_id`) — **18 из 18, ни одного пробела**.
Дополнительно `/v1/actions/auto-add/*` отдаёт `offer_id` и `sku` напрямую, что даёт
второй независимый путь резолва и естественную сверку.

По WB вопрос не стоит: уровня SKU в ответах календаря нет вовсе.

---

## 6. ЧТО ДОСТУПНО ДЛЯ ИСТОРИЧЕСКОЙ ОЦЕНКИ АКЦИЙ

| Источник | Грейн | Глубина `CONFIRMED_EXISTING_CODE` |
|---|---|---|
| `evetis_mart.FACT_SKU_DAILY` | дата × маркетплейс × SKU | **WB с 2024-09-05, OZON с 2025-04-26**; 8127 + 3737 строк; экономика покрыта: OZON 3737/3737, **WB 3840/8127 (47 %)** |
| `wb_mart.SKU_PERFORMANCE_V2_DAILY` | день × nm_id | когортный выкуп, полная цепочка цены |
| `ozon_mart.FCT_OZON_SKU_PNL_DAILY` | день × SKU | вклад до и после рекламы |
| `wb_raw.RAW_WB_PRICES` | nm_id × size × observed_at | **только с 2026-09-07** |
| `ozon_raw.RAW_OZON_PRICES` | offer_id × snapshot_date | **только с 2026-08-31** |
| `wb_raw.V_WB_FUNNEL_DAILY` | день × nm_id | **только 2026-09-04 … 2026-09-21** |
| `wb_mart.FACT_STOCKS_SNAPSHOT` | день × SKU | с 2026-07-16 |
| `ozon_raw.RAW_OZON_STOCKS` | день × SKU | с 2026-08-31 |
| `wb_mart.V_CT_INVENTORY_TRUTH` | SKU | текущее состояние, канонические остатки и покрытие |

**Жёсткое следствие.** Продажи и вклад по дням есть за год и более, но **цена, воронка и
остатки — только с конца августа / начала сентября 2026**. Любая ретроспективная оценка
эффекта акции до сентября 2026 не может контролировать цену, конверсию и наличие, то есть
обязана получать класс `INSUFFICIENT_DATA`. Полноценные до/во время/после окна начнут
накапливаться с момента запуска наблюдателя акций.

---

## 7. ПЕРЕЧЕНЬ WRITE-МЕТОДОВ, ОБНАРУЖЕННЫХ ПРИ ИССЛЕДОВАНИИ (DENY-LIST)

Ни один из них не вызывался. Все обязаны быть запрещены на уровне клиента подсистемы акций.

**Wildberries**
- `POST /api/v1/calendar/promotions/upload` — добавить товар в акцию
- `POST /api/v2/upload/task` и `/api/v2/upload/task/club-discount` — цены и скидки
  (из смежной категории того же токена)

**Ozon**
- `POST /v1/actions/products/activate` · `POST /v1/actions/products/deactivate`
- `POST /v1/actions/auto-add/products/update` · `POST /v1/actions/auto-add/products/delete`
- `POST /v1/actions/discounts-task/approve` · `POST /v1/actions/discounts-task/decline`
- `POST /v1/seller-actions/create/*` (5 механик) · `POST /v1/seller-actions/update/*` (5)
  · `POST /v1/seller-actions/products/add`
- `POST /v1/actions/hotsales/activate` · `/deactivate` (удалены площадкой, но в deny-list)
- `POST /v1/product/import/prices` · `POST /v1/product/update/discount`
- весь `POST /v1/pricing-strategy/*`, кроме `info`/`list` — собственный репрайсер Ozon
  конфликтовал бы с любым нашим ценовым действием

---

## 8. КОНТРОЛЬНЫЕ ЦИФРЫ ДЛЯ ПРОВЕРКИ ЭТОГО ОТЧЁТА

Воспроизводится read-only, без секретов в выводе.

| Проверка | Ожидание |
|---|---|
| `GET /api/v1/calendar/promotions`, окно −120…+180 сут, `allPromo=true` | 70 акций, 63 `auto` + 7 `regular` |
| То же, окно −730 сут | 242 акции, ранняя 2024-10-06 |
| `GET /api/v1/calendar/promotions/details?promotionIDs=2940` | `inPromoActionTotal = 1`, `ranging` из трёх ступеней 25/30/35 |
| `GET /api/v1/calendar/promotions/nomenclatures?promotionID=2873&inAction=true` | `422` |
| `GET /v1/actions` | 12 акций, `is_participating = true` у 9 |
| `POST /v1/actions/products {action_id: 1977747}` | `total = 18`, `action_price = price` у всех |
| `POST /v1/actions/auto-add/products/list {action_id:1977747, auto_add_date:"2026-10-01T21:00:00Z"}` | `total = 12`, `add_mode = AUTO`, `action_price_to_auto_add = price` |
| BigQuery: `SELECT COUNTIF(ozon_actions_exist) FROM ozon_raw.RAW_OZON_PRICES WHERE snapshot_date = CURRENT_DATE()` | `0` при 18 реально участвующих товарах |
| BigQuery: резолв 18 `product_id` через `REF_SKU_CHANNEL_MAP` | 18 из 18 |

---

## 9. ОТКРЫТЫЕ ВОПРОСЫ, КОТОРЫЕ ЗАКРЫВАЮТСЯ ТОЛЬКО НАБЛЮДЕНИЕМ

| # | Вопрос | Класс | Как закроется |
|---|---|---|---|
| O-1 | Смысл `stock` в `/v1/actions/{products,candidates}` (везде 0 при ненулевом FBO) | `UNPROVEN` | сравнение с `RAW_OZON_STOCKS` на горизонте 2 недель после запуска наблюдателя |
| O-2 | Направление `price_min_elastic` / `price_max_elastic` | `LIKELY` | подтвердится, когда бустинг у SKU изменится и пара сдвинется |
| O-3 | Реальная механика перехода `add_mode` `NOT_SET → AUTO` | `LIKELY` | наблюдение перехода 2026-10-01 по 12 SKU |
| O-4 | Частота обновления `max_action_price` | `UNPROVEN` | ряд наблюдений, см. §15 спецификации о каденсе |
| O-5 | Появится ли у EVETIS акция WB `type = "regular"` | `UNPROVEN` | календарь опрашивается ежедневно; при появлении включается ветка `nomenclatures` |
| O-6 | Влияет ли `participationPercentage` (доля наших товаров в акции WB) на фактический бустинг | `CONFIRMED_DOCS` в части лестницы, `UNPROVEN` в части эффекта на продажи | `FACT_PROMO_SKU_WINDOW` после накопления окон |
