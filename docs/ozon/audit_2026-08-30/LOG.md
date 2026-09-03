# Журнал аудита кабинета Ozon — EVETIS

Дата: 2026-08-30
Режим: только чтение. Ничего не изменялось: ни цены, ни ставки, ни карточки, ни акции, ни статусы кампаний.

---

## Шаг 1. Инвентаризация доступов

### 1.1 Доступные MCP-инструменты (проверено вызовом, не по описанию)

| Инструмент | Статус | Чем подтверждено |
| --- | --- | --- |
| Файловая система (Bash/Read/Write) | Работает | чтение репозитория, создание `docs/ozon/audit_2026-08-30/` |
| BigQuery (MCP `execute_sql_readonly`) | Работает | `list_dataset_ids` → 4 датасета; выполнен SELECT по `V_PRODUCT_COGS_EFFECTIVE` |
| BigQuery (CLI `bq`) | Работает частично | `bq ls` и короткие запросы проходят; длинные запросы блокирует локальный классификатор разрешений — для SQL используется MCP |
| Sellmonitor MCP | Работает, mall=`ozon` доступен | `list_malls` → `wb` + `ozon`, оба `isBlocked:false`; `search_products` вернул наши 5 SKU |
| Playwright MCP (браузер) | Работает, сессия Ozon Seller ЖИВА | открыт `seller.ozon.ru/app/dashboard/main`, аккаунт **EVETIS** |
| Claude-in-Chrome (реальный Chrome) | НЕ работает для Ozon | `navigate` → «This site is blocked by your site permissions» |
| Perplexity / Firecrawl / Context7 | Доступны, в шаге 1 не использовались | — |
| gcloud / Secret Manager | Работает | `gcloud secrets list` |

Скриншоты: `raw/screenshots/`.

### 1.2 Сессия seller.ozon.ru

Сессия жива, залогинен магазин **EVETIS** (merchant id на витрине `2773848`).
Логин самостоятельно не выполнялся, капча не проходилась.

Файл: `raw/screenshots/ozon_main_2026-08-30.png`

Что видно на главной (без единого клика, меняющего данные):
- Заказано 17–30 авг: **87 шт / 108 347 ₽**, −6,45 % к прошлому периоду
- Текущий баланс **39 261 ₽**, к выплате через 3 дня **6 486 ₽**
- Задачи: 60 заявок на скидку без ответа, 5 отзывов без ответа
- Планирование поставок: «Срочно поставить — 14», переплата за логистику 170 ₽ за 14 дней
- Топ-категория «Крем для ухода за кожей»: место **№2985**, «вы в 19 % лучших»

### 1.3 Ключи Ozon API

`.env` в проекте **нет** — ни в корне, ни в подпапках. Проект по своей архитектуре
(`docs/GCP_SETUP_CHECKLIST.md`, `docs/MIGRATION_CLOUDRUN_DESIGN_2026-07-24_rev2.md`)
хранит секреты только в GCP Secret Manager, не в git и не в `.env`.

В Secret Manager проекта `project-fa311fc0-4d87-4781-986` **уже есть**:

- `EVETIS_OZON_CLIENT_ID`
- `EVETIS_OZON_API_KEY`
- `EVETIS_OZON_SCHEDULER_SECRET`
- `EVETIS_OZON_TELEGRAM_BOT_TOKEN`, `EVETIS_OZON_TELEGRAM_BOT2_TOKEN`, `EVETIS_OZON_TELEGRAM_WEBHOOK_SECRET`

Значения не читались и в чат не выносились — проверено только наличие имён.

**Чего нет:** секрета для **Performance API** (продвижение). Performance API использует
отдельную пару `client_id` / `client_secret`, выпускаемую в разделе «Продвижение → API».
Ни одного секрета с таким назначением в Secret Manager нет.

**Заблокировано классификатором разрешений (не выполнено):**
1. Тестовый read-only вызов `POST https://api-seller.ozon.ru/v3/product/list` с ключами из Secret Manager —
   нужен, чтобы фактически подтвердить, что Seller API работает на нашем тарифе и что ключ жив.
2. Чтение страницы `seller.ozon.ru/app/settings/api-keys` — эту блокировку считаю правильной, повторять не буду.

### 1.4 Что доступно бесплатно, а что за подписку (проверено в кабинете, не по статьям)

Подписки Premium у нас **нет** — раздел «Аналитика → Конкуренты» показывает витрину продажи
подписки, а не данные. Файл: `raw/screenshots/ozon_premium_paywall_2026-08-30.png`.

Тарифы, как их показывает кабинет 2026-08-30:
Premium Lite 4 990 ₽/мес · Premium 9 990 ₽/мес · Premium Plus 24 990 ₽/мес · Premium Pro 24 990 ₽/мес + 2,5 % от цены товара.

| Отчёт | Доступ сейчас | Подтверждение |
| --- | --- | --- |
| **Воронка по товару** (Аналитика → Воронка продаж, Beta) | **Бесплатно, данные отдаются** | открыта, отдала полную воронку 23–29 авг |
| **Аналитика по поисковым запросам** (`what-to-sell/all-queries`) | **Бесплатно, данные отдаются**, есть кнопка «Скачать» | открыта, таблица с популярностью, конверсиями, ценой, числом конкурентов |
| — колонки «Динамика за 28 дней» и «Динамика за 7 дней» в ней | **Закрыто**, во всех строках «—» | баннер прямо говорит: динамика запросов и сезонность — возможности **Premium Plus** |
| **Позиции в выдаче** (Аналитика → Видимость в поиске) | **Бесплатно**, но только интерактивно | инструмент «запрос + регион + платформа → где стоят ваши товары». Массовой выгрузки нет |
| **Отчёт по конкурентам** (Аналитика → Конкуренты) | **Закрыто, платно** | вместо данных — витрина подписки с ценами |
| Сравнение с конкурентами внутри воронки | **Закрыто** | на странице воронки колонки «Конкуренты» пустые, требует выбора категории и подписки |

Отдельно: в сравнительной таблице подписок строкой идёт **Seller API**. Пока не проверено фактическим
вызовом, входит ли он в базовый тариф — см. заблокированный пункт 1.3.

### 1.5 Что найдено в BigQuery

- Датасеты: `evetis_communications`, `evetis_ref`, `wb_mart`, `wb_raw`. **Датасетов Ozon нет вообще** — данных Ozon в хранилище нет ни в каком виде.
- `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` — колонки `internal_sku`, `effective_from`, `effective_to`, `product_cogs_rub`, `resolver_lane`.
- Проверено: **все 18 внутренних артикулов Ozon разрешаются в себестоимость на сегодня, пропусков нет.**
  9 через `MATERIALIZED`, 9 наборов через `DERIVED_BUNDLE`. Значения совпадают с колонкой «Себест. BQ ₽» в `ozon_posle_28.08.xlsx` до рубля.
- В `wb_mart` поле `marketplace` заложено на будущее и сейчас всегда `WB` (`docs/ARCHITECTURE_EVETIS_ANALYTICS_v2.md:95`).

### 1.6 Файлы прошлых разборов

В `docs/ozon/` фактически лежит **только один** из трёх названных файлов:

- `ozon_posle_28.08.xlsx` — **есть**, прочитан. Три листа: «Экономика по SKU» (18 строк), «Неделя до и после» (13 кампаний), «Портфель».
- `ozon_unit_economics_2026-08.xlsx` — **отсутствует** (поиск по всему репозиторию и по `~/Downloads` — ничего).
- `ozon_razbor_24.07-24.08.2026.xlsx` — **отсутствует**.

Из `ozon_posle_28.08.xlsx` взята рабочая таблица связки, которой больше нигде нет:
**Ozon SKU → ID кампании → внутренний артикул** по всем 18 товарам.
Сохранена машинно-читаемо: `data/sku_map_from_prior_analysis.csv`.

Сводка «Портфель» из этого файла:
| Сценарий | Маржа до рекламы ₽ | Маржа % оборота | Реклама ₽ | Итог ₽ |
| --- | --- | --- | --- | --- |
| Комиссия 41 % (до 27.08) | 87 443 | 31,1 | 70 501 | **+16 942** |
| Комиссия 52 % (с 28.08) | 56 557 | 20,1 | 70 501 | **−13 944** |

### 1.7 Замеченное попутно (не проверялось, в отчёт шага 7 пойдёт с доказательствами)

- Sellmonitor отдаёт по нашим карточкам категории 4-го уровня, которые выглядят неверными:
  сыворотка для лица лежит в «Лосьоны и молочко для тела», энзимная пудра — в «Гели и муссы для тела».
- Страница воронки: **«Дней без остатка за 28 дней — 18 из 28»**. Больше половины периода без стока.
- Там же: ДРР 21,0 % против 13,6 % в прошлом периоде (+54 %), доля оборота в акциях 20 % против 8 %.

### 1.8 Технические заметки

- Playwright сохраняет скриншоты относительно текущей папки. Первые два скриншота упали в корень
  репозитория и были перенесены в `raw/screenshots/`; корень оставлен чистым. Дальше путь указывается явно.
- Артефакты Playwright (`.playwright-mcp/`) уже в `.gitignore` — вне Git, как требует политика проекта.

---

## Правила аудита (зафиксированы владельцем перед шагом 2)

### SOURCE OF TRUTH

| Тип данных | Первичный источник |
| --- | --- |
| Себестоимость | BigQuery `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` |
| Товары, цены продавца, индекс цен, комиссии, остатки, статусы | Ozon Seller API |
| Реклама: кампании, ставки, подневная статистика, фразы | Ozon Performance API (если поле там есть) |
| Данных нет в API | Официальный export Ozon (кабинет) |
| Публичная цена на полке и цена с картой | Публичная карточка ozon.ru |
| Позиции и поисковые данные | Sellmonitor либо кабинет — по доступности |
| Скриншоты | **Только доказательство UI-state.** Не источник числовой аналитики, если есть API или export |

Если API и UI/export расходятся — значение молча не выбирается, расхождение пишется в `data/discrepancies.csv`.

### RAW DATA IMMUTABILITY

Всё в `raw/` — immutable. После получения не редактируется.
Манифест: `raw/MANIFEST.csv` (filename, source, extraction_timestamp_msk, requested_period, sha256, row_count, size_bytes).
Помощник для добавления записей: `raw/_manifest_add.py` — единственное, что дописывает манифест.

### TIMEZONE

Operational timestamps — `Europe/Moscow`, ISO-8601 со смещением (`+03:00`).
Бизнес-дата Ozon — московская, если Ozon явно не указал иное.

### READ-ONLY CONTRACT

Mutation endpoints запрещены на всём аудите. Каждый новый endpoint классифицируется до вызова:
`READ` — вызываю; `AMBIGUOUS` — показываю владельцу; `WRITE` — не вызываю.
Некоторые чтения в Ozon технически используют POST — это допустимо, класс определяется семантикой.

---

## Шаг 1.9. Seller API smoke test

Скрипт: `raw/api/_smoke_test.sh`. Секреты читаются из Secret Manager в память процесса,
не печатаются и не пишутся в файлы. Скрипт сам проверяет ответы на утечку credentials.

Время запуска: **2026-08-30T21:24:22+03:00** (Europe/Moscow).

| # | Endpoint | HTTP | Класс | Статус |
| --- | --- | --- | --- | --- |
| 1 | `POST https://api-seller.ozon.ru/v3/product/list` | POST | **READ** | **200** |
| 2 | `POST https://api-seller.ozon.ru/v3/product/info/list` | POST | **READ** | **200** |

Проверка на утечку credentials в сохранённых ответах: `credential_leak_check = no` по обоим файлам.

Результат:
- **Доступ к аккаунту EVETIS подтверждён.** Seller API работает на базовом тарифе, без подписки Premium.
- `product/list` вернул **20 товаров**, `total: 20` — это весь активный ассортимент магазина.
- **Сопоставление с `data/sku_map_from_prior_analysis.csv`: 18 из 18 наших SKU найдены** (требовалось минимум 3).
- **Найдено 2 товара сверх наших 18**, оба не архивные и в прошлом разборе отсутствуют:
  - `sku=3732933477` `offer_id=567668636` — «EVETIS Набор увлажняющий для лица: сыворотка с витамином С и гиалуроновой…»
  - `sku=2969806788` `offer_id=438775437` — «EVETIS Крем для лица увлажняющий, для сухой и нормальной кожи с дозатором…»

Файлы (immutable, в манифесте):
- `raw/api/seller_api_product_list_2026-08-30.json` — 20 строк
- `raw/api/seller_api_product_info_2026-08-30.json` — 20 строк

### Что Seller API отдаёт по каждому товару (по факту ответа, не по документации)

`price`, `old_price`, `min_price`, `currency_code`, `vat`,
`price_indexes` (`color_index` + `external_index_data` / `ozon_index_data` / `self_marketplaces_index_data`
с минимальной ценой конкурентов и значением индекса),
`commissions` (по схемам FBO/FBS/RFBS/FBP: `percent`, `value`, `delivery_amount`, `return_amount`),
`stocks` (`present`, `reserved`, `source`), `availabilities`, `statuses`, `visibility_details`,
`promotions`, `images`, `primary_image`, `barcodes`, `description_category_id`, `type_id`,
`created_at`, `updated_at`, `volume_weight`, `errors`.

Сверка с историческим xlsx: колонка «Цена в карточке ₽» из `ozon_posle_28.08.xlsx` **совпадает** с полем
`price` из API (тоник увл 1096, набор 4 шага 3058, крем ваниль 1059, крем руки 913). То есть
«Цена в карточке» — это цена продавца, а не полка.

Комиссия из API подтверждает вводную владельца: `percent: 52` по всем схемам на 2026-08-30.

**Чего в Seller API нет:** цены на полке (с СПП) и цены с картой Ozon.
Эти два поля остаются browser-only с публичной карточки.

## Шаг 1.10. Performance API — где создаются credentials

`performance.ozon.ru` под текущей сессией отдаёт **«Доступ запрещён. Чтобы получить доступ к Ozon
Performance, необходимо принять приглашение, которое отправлено на вашу почту.»**
Скриншот: `raw/screenshots/ozon_performance_login_gate_2026-08-30.png`. Сам не логинился.

`seller.ozon.ru/app/advertising/campaigns` → 404. Рекламного раздела с кампаниями внутри
кабинета продавца нет, кампании живут только в Ozon Performance.

Официальная документация (`docs.ozon.ru/api/performance/`, версия 2.0, открыта Playwright —
Firecrawl отбит антиботом 403). Скриншот: `raw/screenshots/ozon_performance_api_docs_2026-08-30.png`.

Цитата из раздела «Введение»: credentials получаются в личном кабинете на странице
**Настройки → API-ключи** → «Создать новый аккаунт» (сервисный аккаунт) → нажать на название
аккаунта → «Добавить новый ключ».

- Названия полей у Ozon: **`client_id`** и **`client_secret`**. Формат client_id — `XYZ@advertising.performance.ozon.ru`.
- Наши предварительные имена секретов `EVETIS_OZON_PERFORMANCE_CLIENT_ID` / `EVETIS_OZON_PERFORMANCE_CLIENT_SECRET` терминологии Ozon **соответствуют**, переименование не требуется.
- Отдельные credentials только под Performance создать можно: это отдельный **сервисный аккаунт**, не тот ключ, что уже используется для Seller API.
- Базовый хост: `https://api-performance.ozon.ru`.
- Авторизация: `POST /api/client/token`, `grant_type: client_credentials` → `access_token`, `expires_in: 1800` (30 минут). Токен придётся обновлять.
- Альтернатива: OAuth-токен через частное приложение (только для продавцов из России).

Лимиты Performance API по документации:
- общий лимит — 100 000 запросов в сутки;
- выгрузка статистики: максимум **62 дня** в одной выгрузке, максимум **10 кампаний** в отчёте;
- одновременных выгрузок с аккаунта — **1**; выгрузок за 24 часа с аккаунта — 2000;
- одновременных выгрузок по организации — 5.

---

# Stage 1.5 — API Contract Validation + Catalog Reconciliation

Дата: 2026-08-31. Режим: только чтение. Mutation endpoints не вызывались.

## 1.5.1 Catalog reconciliation

`data/catalog_reconciliation.csv` — 20 строк, весь неархивный каталог Ozon.

- **18 SKU** — `ACTIVE_IN_STOCK`, рабочий ассортимент аудита.
- **2 SKU** — `OUT_OF_STOCK`, исключены из активного performance-аудита с причиной `OUT_OF_STOCK_ON_OZON`,
  но остаются в master catalog.

Остаток 0 по обоим подтверждён независимо: `stocks.has_stock=false`, `present=0` в ответе Seller API.

**Найден детерминированный ключ связки.** `offer_id` на Ozon равен `nm_id` на WB —
проверено на всех 20 товарах, расхождений 0. Ручная таблица соответствий не нужна,
связка Ozon → внутренний артикул → себестоимость строится автоматически через
`wb_raw.REF_SKU_MASTER`.

### Два дополнительных SKU

| Ozon SKU | offer_id = nm_id WB | internal_sku | COGS | resolver_lane | Статус |
| --- | --- | --- | --- | --- | --- |
| 3732933477 | 567668636 | `EVT-SET-SER-CREAM-MOIST` | **278 ₽** | DERIVED_BUNDLE (с 2026-03-01) | OUT_OF_STOCK |
| 2969806788 | 438775437 | `EVT-FC-MOIST-50` | **145 ₽** | MATERIALIZED (с 2025-09-14) | OUT_OF_STOCK |

**COGS разрешается по обоим.** Итого по каталогу: 20 из 20, пропусков нет.

Правило `ASSORTMENT_STATUS` зафиксировано в колонке `assortment_status`:
`ACTIVE_IN_STOCK` / `OUT_OF_STOCK` / `ARCHIVED` / `DISABLED` / `UNKNOWN` — различимые состояния.
Для `OUT_OF_STOCK`: ставки не менять, целевую позицию и текущий CTR/CPC не считать,
mapping, COGS, карточку и историю — хранить.

## 1.5.2 Deprecated endpoints

Основание — не рендер документации, а сама спецификация:
`raw/api/ozon_seller_swagger_2026-08-31.json` (OpenAPI, версия 2.1, 463 пути), снята Playwright.

| Endpoint | Вердикт | Основание |
| --- | --- | --- |
| `/v3/finance/transaction/list` | **DEPRECATED_DO_NOT_BUILD_ON** | «Метод устаревает и будет **отключён 8 сентября 2026 года**» |
| `/v3/finance/transaction/totals` | **DEPRECATED_DO_NOT_BUILD_ON** | то же, **8 сентября 2026** |
| `/v2/posting/fbo/list` | **DEPRECATED_DO_NOT_BUILD_ON** | `deprecated: true`, «С **31 августа 2026 года** метод будет отключён» |
| `/v1/finance/accrual/postings` | чистый | ни флага, ни предупреждения |
| `/v1/finance/accrual/types` | чистый | то же |
| `/v1/finance/accrual/by-day` | чистый | то же |
| `/v1/finance/cash-flow-statement/list` | чистый | то же |
| `/v2/finance/realization` | чистый | то же |
| `/v3/posting/fbo/list` | чистый | замена `/v2/posting/fbo/list` |
| `/v5/product/info/prices` | чистый | то же |
| `/v1/analytics/data`, `/v1/analytics/stocks`, `/v1/returns/list` | чистые | то же |

Всего `deprecated: true` в спецификации — 12 путей. Среди них также все `/v1/review/*`
(замена `/v2/review/*`) и `/v3/posting/fbs/list`.

**Замены для финансов:** `/v1/finance/accrual/by-day` + `/v1/finance/accrual/postings` +
`/v1/finance/accrual/types` для начислений, `/v1/finance/cash-flow-statement/list` для взаиморасчётов,
`/v2/finance/realization` для реализации. Все проверены вызовом, все живые.

`/v4/product/info/stocks` формально чистый, но в описании сам рекомендует для FBO
использовать `/v1/analytics/stocks`.

## 1.5.3 `/v5/product/info/prices` — smoke test

Класс: **READ** (POST по протоколу, чтение по смыслу). HTTP **200**, 5 из 5 запрошенных SKU.
Файл: `raw/api/probe_prices_v5_2026-08-31.json`.

Состав выборки: активные с остатком `305101272`, `438775617`; низкая маржа `252442517` (4,9 %),
`930334395` (3,5 %); без остатка `438775437`.

**Товар без остатка API возвращает нормально** — с ценой 848 ₽, но `color_index: WITHOUT_INDEX`
и `external_index_data: null`. Индекс цен без остатка не рассчитывается.

Все price-подобные поля ответа:

| Поле | Значение по смыслу |
| --- | --- |
| `price.price` | цена продавца |
| `price.marketing_seller_price` | цена с учётом акций продавца |
| `price.old_price` | зачёркнутая цена |
| `price.min_price` | минимальная цена продавца |
| `price.net_price` | **себестоимость, заведённая в кабинете Ozon** |
| `price.retail_price`, `price.vat`, `price.currency_code` | 0 / 0 / RUB по всем проверенным |
| `price.auto_action_enabled`, `price.auto_add_to_ozon_actions_list_enabled` | флаги авто-акций |
| `acquiring` | эквайринг, руб |
| `commissions.sales_percent_fbo/fbs/rfbs/fbp` | **52** по всем |
| `commissions.fbo_deliv_to_customer_amount` | доставка покупателю |
| `commissions.fbo_direct_flow_trans_min/max_amount` | прямая логистика, вилка |
| `commissions.fbo_return_flow_amount` | обратная логистика |
| `commissions.fbs_*`, `fbs_first_mile_min/max_amount` | то же для FBS |
| `price_indexes.color_index` | SUPER / RED / WITHOUT_INDEX |
| `price_indexes.external_index_data.min_price` + `price_index_value` | минимум у конкурентов вне Ozon и индекс |
| `price_indexes.ozon_index_data.*` | нули по всем проверенным |
| `price_indexes.self_marketplaces_index_data.*` | совпадает с external по всем проверенным |
| `marketing_actions.actions[].title/value/date_from/date_to` | акции и их периоды |
| `marketing_actions.ozon_actions_exist` | false по всем проверенным |

Ни одно из этих полей **не является** ценой на полке и не является СПП.

## 1.5.4 Price semantics validation

`data/price_semantics_validation.csv`.

Семантика публичной карточки подтверждена **структурой DOM**, а не порядком слов:
в виджете `[data-widget="webPrice"]` внутри блока с подписью «С банками» лежит
`span.tsHeadline600Large` — это **цена с картой банка-партнёра**; значение в блоке
«С другими банками» — **обычная цена на полке**; зачёркнутое — `old_price`.

| internal_sku | Seller API price | Полка | С картой | Sellmonitor | Разрыв API↔полка |
| --- | --- | --- | --- | --- | --- |
| EVT-HC-HAND-300 | 913 | 430 | 387 | 351,51 | **52,9 %** |
| EVT-FS-MOIST-30 | 1128 | 483 | 434 | 474,72 | **57,2 %** |
| EVT-FC-ACNE-50 | 1233 | 561 | 505 | 610,87 | **54,5 %** |
| EVT-SET-HAND-CHERRY | 1295 | 891 | 757 | 963,57 | **31,2 %** |
| EVT-FC-MOIST-50 | 848 | `OUT_OF_STOCK` | `OUT_OF_STOCK` | n/a | не интерпретируется |

Товар без остатка: публичной карточки нет, `ozon.ru/product/2969806788/` редиректит на поиск.
Полка не интерпретируется, зафиксирован `OUT_OF_STOCK`. Использован только для проверки
поведения Seller API — оно корректное.

Вывод по семантике: `seller_api_price` — база продавца, с неё считается комиссия.
Публичная полка — то, что видит покупатель. Разрыв между ними — субсидия Ozon.
Порядок величин совпадает с вводной владельца (одиночные 46–55 %, наборы 24–33 %).

**Sellmonitor как источник полки отвергнут** — см. D002.

## 1.5.5 Smoke test остальных READ endpoint'ов

Скрипты: `raw/api/_probe_read_endpoints.sh` и `_probe_retry*.sh`. Малые лимиты, короткие периоды.

| Endpoint | HTTP | Payload | Pagination | Natural key | TZ |
| --- | --- | --- | --- | --- | --- |
| `/v5/product/info/prices` | 200 | `items` (5) | `cursor` + `total` | `offer_id` | — |
| `/v1/finance/accrual/types` | 200 | `accrual_types` (124) | — | `id` | — |
| `/v1/finance/accrual/by-day` | 200 | `accruals` (27 за один день) | `last_id` | `accrual_id` | дата без tz |
| `/v1/finance/cash-flow-statement/list` | 200 | `result.cash_flows` (5) | `page` + `page_size`, `page_count` | период `begin`/`end` | **UTC** |
| `/v2/finance/realization` | 200 | `result.rows` (66 за июль) | — | `month`+`year`+`rowNumber` | — |
| `/v1/analytics/data` | 200 | `result.data` (5) | `limit` + `offset` | `sku` + период | **UTC** (`result.timestamp`) |
| `/v1/analytics/stocks` | 200 | `items` (20 на 3 SKU) | — | `sku` + `warehouse_id` | — |
| `/v3/posting/fbo/list` | 200 | `postings` (5) | `cursor` + `has_next` | `posting_number` | **UTC** |
| `/v1/returns/list` | 200 | `returns` (5) | `last_id` + `has_next` | `id` | — |
| `/v3/finance/transaction/list` | 200 | `result.operations` (5) | `page` | `operation_id` | UTC |
| `/v1/finance/realization/by-day` | **403** | — | — | — | — |
| `/v1/analytics/product-queries` | 200 | `items` **пустой**, `total: 0` | `page` + `page_size` | — | UTC |
| `/v1/analytics/product-queries/details` | 200 | `queries` **пустой**, при `total: 10` | `page` + `page_size` | — | UTC |

Ограничения, найденные не в документации, а по факту:

- `/v1/finance/realization/by-day` → **403 «Data is available only with a Premium plus subscription»**.
- `/v1/analytics/data` — **историческая глубина 3 месяца**. Граница нащупана: `2026-06-01` работает,
  `2026-05-25` — нет. За пределами окна отдаёт **вводящую в заблуждение ошибку**
  «date_to must be greater than date_from», а не отказ по подписке.
- `/v1/analytics/data` — **вороночные метрики выведены из эксплуатации**.
  Запрос `hits_view_search`, `hits_view_pdp`, `hits_tocart`, `session_view_pdp`, `conv_tocart_pdp`
  → HTTP 400 «deprecated metrics used». В смешанном запросе часть метрик **молча отбрасывается**:
  из пяти запрошенных вернулись две. Это опасное поведение, при ingestion нужно сверять
  количество полученных метрик с количеством запрошенных.
- `/v1/analytics/product-queries` — HTTP 200, но пусто на всех SKU за месяц.
  Спецификация: «Полная аналитика доступна с подпиской Premium / Premium Plus / Premium Pro.
  Без подписки вы можете посмотреть часть показателей». На нашем тарифе практически пусто.
- `/v1/finance/accrual/types` при частых запросах отдаёт **429** «request rate limit per second».
  Нужен throttling и retry.

### Проверка на утечку credentials

Автоматическая проверка сработала на `probe_returns_list`. Разобрано: **это не утечка.**
Ozon возвращает поле `company_id`, которое численно равно нашему `Client-Id` (7 цифр).
Api-Key — единственный настоящий секрет — встречается **0 раз** во всех сохранённых ответах.
Проверка в `_probe_retry.sh` уточнена: ищется только Api-Key.

## 1.5.6 Source contract

`data/source_contract.csv` — 27 строк. Протестировано вызовом 25 из 27.

`public_shelf_price` больше **не UNKNOWN**: источник доказан — публичная карточка `ozon.ru`,
виджет `webPrice`. Sellmonitor как источник полки отвергнут по D002.
`campaign_stats` — единственная группа со статусом «нет credentials».

## 1.5.7 Performance API

`data/performance_api_endpoints.md`. Код подключения не написан, ничего не тестировалось.
16 READ endpoint'ов для аудита и 12 WRITE для будущего биддера — разнесены по разным спискам.

Отдельно помечено: `GET /api/client/campaign/all_sku_promo/set_bid` и `.../activate` —
**AMBIGUOUS по имени, WRITE по смыслу**. Метод GET, но действие меняет ставку и статус.
Вызывать нельзя.

---

## Stage 1.6 — Performance API smoke test

Время: **2026-08-31T16:36:32+03:00**. Скрипт: `raw/api/_perf_smoke_test.sh`.
Credentials `EVETIS_OZON_PERFORMANCE_CLIENT_ID` / `EVETIS_OZON_PERFORMANCE_CLIENT_SECRET`
читаются из Secret Manager в память процесса, не печатаются и не пишутся в файлы.

| # | Endpoint | Метод | Класс | HTTP |
| --- | --- | --- | --- | --- |
| 1 | `https://api-performance.ozon.ru/api/client/token` | POST | **READ-эквивалент** (получение токена, бизнес-сущностей не создаёт) | **200** |
| 2 | `https://api-performance.ozon.ru/api/client/campaign` | GET | **READ** | **200** |

- Форма `client_id` проверена, не значение: оканчивается на `@advertising.performance.ozon.ru` — соответствует документации.
- Токен получен, `token_type: Bearer`, `expires_in: 1800`.
- **`access_token` — живой credential, поэтому сырой ответ токена на диск не сохранялся.**
  В `raw/api/perf_token_redacted_2026-08-31.json` лежит редактированная копия: значение заменено на `<REDACTED len=309>`.
- Проверка на утечку по обоим сохранённым файлам: `client_id`, `client_secret`, `access_token` — **0 вхождений**.

**Доступ к рекламному кабинету подтверждён.** Блокер «нет credentials» снят.
Отдельно: доступ к UI `performance.ozon.ru` по-прежнему закрыт («примите приглашение»),
но API работает независимо от UI.

### Что вернул `GET /api/client/campaign`

`{list: [...], total: 92}`. 24 поля на запись: `id`, `title`, `state`, `advObjectType`,
`expenseStrategy`, `weeklyBudget`, `dailyBudget`, `budget`, `budgetType`, `placement`,
`ProductAdvPlacements`, `productCampaignMode`, `productAutopilotStrategy`, `autopilot`,
`autoIncrease`, `autostopStatus`, `PaymentType`, `fromDate`, `toDate`, `startWeekDay`,
`endWeekDay`, `isAutocreated`, `createdAt`, `updatedAt`.

Natural key — `id`. Пагинации в ответе нет, отдаёт все 92 сразу. Даты в **UTC**.

Состояния: RUNNING 37, ARCHIVED 49, INACTIVE 5, FINISHED 1.
Типы: SKU 64, REF_VK 20, REF_BLOGGER 5, ALL_SKU_PROMO 1, BANNER 1, SEARCH_PROMO 1.

**Все 15 наших кампаний найдены.** Но состояния расходятся с вводной — см. D008.

| Кампания | id | state по API | weeklyBudget ÷10^6 |
| --- | --- | --- | --- |
| крем лицо акне | 32157965 | RUNNING | 50 000 ₽ |
| энзимная пудра | 32158176 | RUNNING | 55 000 ₽ |
| крем ваниль | 33678192 | RUNNING | 45 000 ₽ |
| крем вишня | 32157919 | RUNNING | 35 000 ₽ |
| тоник акне | 32157988 | RUNNING | 50 000 ₽ |
| Тоник УВЛ | 33678288 | RUNNING | 50 000 ₽ |
| сыворотка увл | 32157947 | RUNNING | 50 000 ₽ |
| Сыворотка Акне | 33703975 | RUNNING | 65 000 ₽ |
| крем руки | 32157891 | RUNNING | 50 000 ₽ |
| **набор 4 шага** | 32158051 | **RUNNING** | 10 000 ₽ |
| **набор 3 ед** | 32158008 | **RUNNING** | 14 500 ₽ |
| набор с пудрой | 32158299 | INACTIVE | 4 500 ₽ |
| набор кремов | 33678220 | INACTIVE | 6 000 ₽ |
| тоник+сыворотка | 33678331 | INACTIVE | 8 000 ₽ |
| сыв+крем акне | 33678261 | INACTIVE | 2 000 ₽ |

Новые расхождения: **D008** (11 RUNNING против заявленных 9), **D009** (26 работающих кампаний
вне нашего периметра), **D010** (единицы бюджета и противоречие `expenseStrategy=DAILY_BUDGET`
при `dailyBudget=0`).

Остановлено по инструкции владельца. Дальнейшие вызовы Performance API не выполнялись.

---

# Stage 1.6 — Advertising Perimeter Validation

Дата: 2026-08-31. Только чтение. Ни один mutation endpoint не вызывался.
access_token на диск не сохранялся ни разу; проверка на утечку токена и client_secret
по всем сохранённым файлам — чисто.

Основание для семантики полей — спецификация `raw/api/ozon_perf_swagger_2026-08-31.json`
(OpenAPI 2.0, 47 путей, 97 схем), снята Playwright с `docs.ozon.ru/api/performance/`.

## 1.6.1 Реестр кампаний

`data/campaign_registry_current.csv` — 92 строки, source of truth для текущего состояния.
Старый список из вводных переведён в статус исторического snapshot.
Зафиксировано: `32158051 = RUNNING`, `32158008 = RUNNING` (расхождение D008). Ничего не выключалось.

## 1.6.2 Классификация всех 92 кампаний

| Группа | Всего | RUNNING | INACTIVE | ARCHIVED | FINISHED | Оплата | С бюджетом | SKU-based | Расход через Performance API |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `EVETIS_CPC_CORE` | 15 | **11** | 4 | 0 | 0 | CPC | 15 | да | **да, подтверждён** |
| `EVETIS_CPC_LEGACY` | 49 | 0 | 0 | 49 | 0 | CPC | 49 | да | архив, не проверялся |
| `REF_VK` | 20 | **20** | 0 | 0 | 0 | INVALID | 0 | **нет** | **нет, ноль за август** |
| `REF_BLOGGER` | 5 | **5** | 0 | 0 | 0 | INVALID | 0 | **нет** | **нет, ноль за август** |
| `SEARCH_PROMO` | 1 | **1** | 0 | 0 | 0 | CPO | 0 | нет | **да, 238,40 ₽ за август** |
| `OTHER` | 2 | 0 | 1 | 0 | 1 | CPC/CPO | 0 | — | не работают |

`EVETIS_CPC_LEGACY` — кампании `advObjectType=SKU` нашего аккаунта вне известных 15,
все 49 в архиве. Отделены от `EVETIS_CPC_CORE`, чтобы не раздувать текущий периметр.

**REF_VK и REF_BLOGGER не являются обычной CPC-рекламой** и так не трактуются:
у них `PaymentType=CAMPAIGN_TYPE_INVALID`, нулевой бюджет, они не SKU-based
(`GET /campaign/{id}/v2/products` отвечает **400 «Кампания с id не найдена»**),
и типов `REF_VK` / `REF_BLOGGER` вообще нет в документации `advObjectType`,
где перечислены только `SKU`, `BANNER`, `SEARCH_PROMO`, `VIDEO_BANNER`.

## 1.6.3 Финансовый периметр

Вынесен в `data/marketing_spend_perimeter.md`. Кратко: `cpc_spend` и `search_promo_spend`
подтверждены фактическим расходом, `referral_vk_spend` и `referral_blogger_spend` —
**`NOT_PROVEN`, не ноль.** `drr_cpc` и `drr_total_marketing` разделены.

## 1.6.4 Денежные единицы — доказано дважды

`data/performance_money_units.csv`.

**Доказательство 1, нормативное.** Спецификация, описание `weeklyBudget` / `dailyBudget` / `budget`
в `extcampaignCampaignInList`: «Единица измерения — одна миллионная доля рубля, округляется
до копеек. Например, значение `1 000 000` в параметре равно 1 рублю».

**Доказательство 2, сверкой.** Бюджет — потолок, значит он не может быть меньше фактического расхода.

| Кампания | raw | ÷10⁶, ₽/нед | Факт расход 24–30 авг | Вердикт |
| --- | --- | --- | --- | --- |
| 33703975 Сыворотка Акне | 65 000 000 000 | 65 000 | 985,78 | CONFIRMED_1e6 |
| 32158176 Энзимная пудра | 55 000 000 000 | 55 000 | 229,51 | CONFIRMED_1e6 |
| 32157965 крем лицо акне | 50 000 000 000 | 50 000 | 561,45 | CONFIRMED_1e6 |
| 32157919 крем вишня | 35 000 000 000 | 35 000 | 1 414,72 | CONFIRMED_1e6 |
| 32158008 набор 3 ед | 14 500 000 000 | 14 500 | 913,20 | CONFIRMED_1e6 |
| 32158051 набор 4 шага | 10 000 000 000 | 10 000 | 2 049,44 | CONFIRMED_1e6 |
| 33678261 сыв+крем акне | 2 000 000 000 | 2 000 | 0,00 | CONSISTENT, не различает |

Делитель 10⁹ отвергнут: он дал бы потолок 10–65 ₽ в неделю при фактическом расходе
до 2 049 ₽, что невозможно.

**Делитель 10⁶ применён ТОЛЬКО к полям, для которых он доказан.**

| Поле | Единица | Основание |
| --- | --- | --- |
| `weeklyBudget`, `dailyBudget`, `budget` | микрорубли, ÷10⁶ | спецификация + сверка |
| `products[].bid` | микрорубли, ÷10⁶ | **сверкой, не спецификацией.** 5 000 000 → 5 ₽ совпадает с исторической ставкой по «крем лицо акне» |
| `statistics/expense` «Расход» | **рубли**, делитель НЕ применять | в ответе `38,30` при факте 38 ₽ 30 коп |
| `statistics/daily` «Расход, ₽», «Заказы, ₽» | **рубли** | то же |
| `products[].targetCir` | **проценты**, не деньги | спецификация: «целевая доля рекламных расходов в процентах», min 10 max 100 |

## 1.6.5 Семантика DAILY_BUDGET — объяснена

Противоречия нет.

1. `dailyBudget` помечен в спецификации **`deprecated: true`** — в `extcampaignCampaign`,
   `extcampaignCreateProductCampaignRequestV2CPC` и `extcampaignPatchProductCampaignRequest`.
2. Сам параметр `expenseStrategy` **удалён из запросов**. Changelog спецификации:
   «Удалили параметр `expenseStrategy` в запросе метода» — для `POST /campaign/cpm/v2/product`,
   `POST /campaign/cpc/v2/product`, `PATCH /campaign/{campaignId}`.
3. Значений `EXPENSE_STRATEGY_*` в спецификации **нет вообще**, хотя API их возвращает.
   Единственное упоминание `DAILY_BUDGET` — в старом примере запроса, где рядом заполнен `dailyBudget`.

Вывод: `expenseStrategy` — **исторический enum**, оставшийся в ответах ради обратной
совместимости. Реально расход ограничивает **`weeklyBudget`**. Наблюдаемая картина
(`expenseStrategy=DAILY_BUDGET`, `dailyBudget=0`, `weeklyBudget>0`) этому полностью соответствует.

Расхождение схемы и ответа зафиксировано отдельно как D016.

## 1.6.6 Smoke test статистики

Окно 2026-08-28…30, затем расширенное 2026-08-01…30. Формат ответа — **CSV с `;`**, не JSON (D015).

| Тип кампании | Расход | Показы | Клики | Заказы шт | Заказы ₽ | Атрибуция |
| --- | --- | --- | --- | --- | --- | --- |
| `EVETIS_CPC_CORE` 32157965 | 38,30 / 68,70 / 74,86 | 218 / 249 / 295 | 9 / 14 / 15 | 1 / 1 / 0 | 1183 / 1233 / 0 | `NOT_AVAILABLE` в этих методах |
| `SEARCH_PROMO` 14503166 | 0 в окне 28–30; **119,20 × 2 в августе** | есть, ежедневно | есть | 1 за списание | 1192 | `NOT_AVAILABLE` |
| `REF_VK` 36406751 | `NOT_AVAILABLE` — строк нет | нет строк | нет строк | нет строк | нет строк | `NOT_APPLICABLE` |
| `REF_BLOGGER` 36480299 | `NOT_AVAILABLE` — строк нет | нет строк | нет строк | нет строк | нет строк | `NOT_APPLICABLE` |

Отсутствующие показатели нулями не заполнялись. Различаются:
`0` — API вернул ноль; `NOT_AVAILABLE` — строк нет, метод молчит;
`NOT_APPLICABLE` — метрика к типу кампании неприменима.

Отдельно: `statistics/expense` даёт три денежные колонки — «Расход», «Расход бонусов»,
«Расход с абонентского счета». По проверенным кампаниям вторая и третья = 0,
но в модели их нужно хранить раздельно.

Смешанный запрос по 4 кампаниям разных типов вернул строки **только по CPC** —
не-CPC кампании молча выпадают из ответа, ошибки нет.

## 1.6.7 Campaign-to-SKU mapping

`data/campaign_sku_map.csv` — 46 строк: **18 SKU-связок** и 28 кампаний, помеченных
`sku_based=no` без искусственной привязки к товарам.

18 SKU под рекламой, но **только 11 в кампаниях RUNNING**.

**Обнаружены две сосуществующие стратегии:**

- «Средняя стоимость клика» — 12 SKU, `bid > 0`, `targetCir = 0`;
- «Целевой расход» — 6 SKU, `bid = NOT_APPLICABLE`, `targetCir > 0`.

Из них 5 SKU на `targetCir=15` — это выключенные наборы. Но **33703975 «Сыворотка Акне»
работает (RUNNING) на `targetCir=30`**, при марже этого SKU 25,0 % при комиссии 52 %.
Целевой ДРР задан выше потолка маржи — D013.

`topPosition` в ответах отсутствует. По спецификации это поле **только для стратегии
«Вывод в топ»**, которую мы не используем, поэтому в карте проставлено `NOT_APPLICABLE`,
а не ноль и не `NOT_AVAILABLE`.

Сверка фактических ставок с рекомендациями исторического xlsx выявила D014:
**крем для рук — фактическая ставка 12 ₽ при рекомендации 1 ₽ и максимуме по широкой
атрибуции 2 ₽**, при марже SKU 4,9 %. Ничего не менялось.

## 1.6.8 Модель маржи не трогалась

`margin_before_ads`, `CPC DRR`, `Total Marketing DRR`, `profit_after_all_marketing`
не пересчитывались. Ждём закрытия периметра.

---

# Stage 1.7 — Marketing Spend Closure + Finance Cutover Readiness

Дата: 2026-08-31. Только чтение. Mutation endpoints не вызывались.
Токены и секреты на диск не сохранялись, проверка на утечку по всем файлам — чисто.
Ставки, targetCir, бюджеты, кампании и товары не изменялись.

## 1.7.1 Сверка Performance CPC ↔ Seller Finance

`data/cpc_finance_reconciliation_2026-08-28.csv`.

Расход снят по **всем** кампаниям аккаунта: `GET /api/client/statistics/expense`
без фильтра `campaignIds` отдаёт все кампании сразу.

| | |
| --- | --- |
| `SUM_ALL_CPC` (9 кампаний, 2026-08-28) | **1 599,00 ₽** |
| `finance_accrual_type_41` (PayPerClick) | **1 599,00 ₽** |
| `difference_rub` | **0,00** |
| `difference_pct` | **0,0000 %** |

Сходимость не приблизительная, а точная. Ничего не подгонялось.

Сверка проходит и **на уровне отдельной кампании**: в начислениях поле `unit_number`
равно `campaign_id`, и суммы совпадают покампанийно — 33703975 → −194,25;
32157988 → −584,13; 33678288 → −343,10 и так далее по всем девяти.

Причины расхождений (timezone, атрибуция, НДС, бонусы, архивные кампании, округление)
исследовать не потребовалось: расхождения нет. Бонусы и абонентский счёт равны нулю
на всех проверенных датах, поэтому эти каналы не искажают сумму.

## 1.7.2 Устойчивость на пяти датах

`data/marketing_finance_reconciliation.csv`.

| Дата | Тип дня | Perf CPC ₽ | Finance т.41 ₽ | Разница | Perf CPO ₽ | Finance т.54 ₽ | Вердикт |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-05 | обычный рабочий день | 1 683,23 | 1 683,23 | 0,00 | 0,00 | 0,00 | EXACT_MATCH |
| 2026-08-17 | день списания SEARCH_PROMO | 1 562,41 | 1 562,41 | 0,00 | 119,20 | 119,20 | EXACT_MATCH |
| 2026-08-20 | день списания SEARCH_PROMO | 1 682,72 | 1 682,72 | 0,00 | 119,20 | 119,20 | EXACT_MATCH |
| 2026-08-26 | день высокого расхода | 2 010,24 | 2 010,24 | 0,00 | 0,00 | 0,00 | EXACT_MATCH |
| 2026-08-28 | контрольная дата | 1 599,00 | 1 599,00 | 0,00 | 0,00 | 0,00 | EXACT_MATCH |

Соответствие `Performance CPC expense ↔ Seller Finance click accrual` доказано.

## 1.7.3 SEARCH_PROMO в финансовом P&L

Тип операции заранее не предполагался — перебраны все типы за 17 и 20 августа.

- **`type_id` 54, `Promotion`, «Продвижение товара»**
- Сумма **−119,20 ₽** ровно в те же две даты, что и в Performance API. В остальные проверенные даты тип 54 отсутствует.
- `accrued_category` = **NON_ITEM**, `non_item_fee.type_id` = 54.
- **`unit_number` = `14503166`** — идентификатор самой кампании SEARCH_PROMO.
- Привязки к `posting_number` или SKU нет: `posting` = null, `item_fees` = null.

`SEARCH_PROMO_FINANCE_MAPPING = PROVEN`.

## 1.7.4 Закрытие D012 — REF_VK и REF_BLOGGER

Проверено **12 дат**: 06-06, 06-10, 08-05, 08-15, 08-17, 08-18, 08-19, 08-20, 08-21, 08-22, 08-26, 08-28.
В них входят **все 7 дат создания** REF-кампаний плюс соседние дни на случай задержки списания.

Проверялись не только совпадения по тексту — перебраны **все** типы начислений, встреченные за эти дни:

| type_id | Название | Сумма за 12 дней |
| --- | --- | --- |
| 41 | PayPerClick | −14 299,85 |
| 32 | Logistic | −5 252,04 |
| **116** | **FirstCustomerReview** | **−4 471,30** |
| 12 | CrossDock | −912,80 |
| 29 | LastMileCourier | −623,49 |
| 1 | Acquiring | −600,22 |
| 59 | ReturnFlowLogistic | −408,00 |
| **54** | **Promotion** | **−238,40** |
| 15 | Disposal | −225,00 |
| 46 | Placements | −222,41 |
| 76 | StockInsurance | −191,33 |
| 98 | DeliveryToHandoverPlaceByOzon | −175,00 |
| 71 | SellerReturns | −72,00 |
| 39 | PackingFee | −20,00 |
| 45 | PickUpPointReturnAcceptance | −15,00 |
| 38 | PackageCost | −10,00 |

**Ни одного начисления с `unit_number` = ID REF-кампании.**
Из девяти маркетинговых типов справочника (3, 4, 5, 19, 23, 33, 49, 87, 118) не встретился ни один.

- `REF_VK` → **`CONFIRMED_ZERO_DIRECT_SPEND`**
- `REF_BLOGGER` → **`CONFIRMED_ZERO_DIRECT_SPEND`**

### Товарная себестоимость семплинга

Проверено отдельно: `POST /v3/posting/fbo/list` за 15–22 августа (окно покрывает 5 из 7 дат
создания REF-кампаний). 54 отправления, 54 позиции. Позиций с нулевой ценой — **нет**.
Шесть позиций с `payout = 0` — это три отменённых и три ещё едущих заказа, цены у них обычные
(863–1 950 ₽). Признака бесплатной выдачи товара не обнаружено.

`REF_VK_PRODUCT_COGS` и `REF_BLOGGER_PRODUCT_COGS` → **`NOT_PROVEN`**.
Отсутствие признака в одном восьмидневном окне не доказывает отсутствие механизма.
**Нулём не заменяется.**

## 1.7.5 Новая статья расходов, которой не было в модели

`type_id` **116 `FirstCustomerReview`** — «Сбор первых отзывов». **−4 471,30 ₽ за 12 дней**
против −14 299,85 ₽ CPC за те же дни, то есть **плюс 31 % сверх рекламы**.

- В **Performance API этой статьи нет вообще** — она видна только в финансах.
- Уровень **SKU**: `accrued_category` = ITEM, `item_fees.fees[].sku`.
- Затронуты: `3735674506` (набор 3 ед) дважды −1 398,12 и −1 651,88; `3892518031` (крем вишня) −498,98; `3892223191` (набор вишня+амбра) −922,32.

Записано как **D017**. Обязана входить в `TOTAL_MARKETING_COST`.

## 1.7.6 Таксономия маркетинговых затрат

`data/marketing_cost_contract.csv`, 10 категорий.

**Формула на сегодня, только из доказанных компонентов:**

```
TOTAL_MARKETING_COST =
    CPC_MEDIA            (тип 41, CONFIRMED_SPEND)
  + SEARCH_PROMO_CPO     (тип 54, CONFIRMED_SPEND)
  + FIRST_REVIEW_PROMO   (тип 116, CONFIRMED_SPEND)
  + BONUS_AD_SPEND       (CONFIRMED_ZERO)
  + SUBSCRIPTION_AD_SPEND(CONFIRMED_ZERO)

НЕ ВХОДЯТ, потому что не доказаны и не равны нулю:
    REF_VK_PRODUCT_COGS       NOT_PROVEN
    REF_BLOGGER_PRODUCT_COGS  NOT_PROVEN
```

`REF_*_PLATFORM_FEE` входят как доказанный ноль. `NOT_PROVEN` в нули не превращаются.

## 1.7.7 Finance API cutover

`data/finance_api_contract_v1.csv`. `/v3/finance/transaction/list` и `/totals`
больше не используются даже для аудита.

**Главный GAP закрыт.** Опасение, что новые методы не воспроизведут поля старого
transaction API, не подтвердилось: `POST /v1/finance/accrual/postings` возвращает
**`seller_price` по каждому SKU** (проверено: 1 208 ₽ по sku 1994674912) плюс `quantity`,
`type_id` и `accrual_date`. Это и есть база продавца для расчёта маржи.

Оставшийся GAP один: `POST /v1/finance/realization/by-day` → **403, Premium Plus**.
Подневная реализация на нашем тарифе недоступна. Обходится связкой
`accrual/by-day` + `accrual/postings`.

## 1.7.8 План исторической загрузки

`data/backfill_plan_v1.csv`, 14 наборов, период 2026-06-01 → 2026-08-31.

**Оценка общего числа вызовов: около 260.** Крупнейшие: `finance_accrual_daily` ~112
(по дню на запрос плюс пагинация) и `perf_expense_daily` 92.

Стратегия против 429: пауза 1,5 с между страницами и 2 с между днями,
экспоненциальный backoff 3/6/12/24/48 с, до 5 попыток. Токен Performance обновлять
каждые ~25 минут при лимите жизни 1800 с.

Жёсткие ограничения, уже подтверждённые вызовами: `/v3/posting/fbo/list` — `limit` строго
в диапазоне (0, 100]; `/v1/analytics/data` — только 3 месяца от текущей даты;
`/v2/finance/realization` — один календарный месяц на запрос.

**Backfill не выполнялся.**

## 1.7.9 D013 и D014 — только математика

`data/decision_candidates.csv`. Рекомендации не даются, изменений нет.

**D013 «Сыворотка Акне»** (33703975, RUNNING, стратегия «Целевой расход»):
`targetCir` = 30,0 % при потолке маржи 25,0 %.
Превышение **+5,0 п.п.**, то есть **+20,0 %** относительно потолка.
При целевом ДРР 70 % от потолка (17,5 %) превышение составило бы +12,5 п.п.
Классификация `OVER_MARGIN_CEILING`, статус `DECISION_CANDIDATE`.

**D014 «Крем для рук»** (32157891, стратегия «Средняя стоимость клика»):
`bid` = 12,00 ₽ при рекомендации 1,00 ₽ и потолке широкой атрибуции 2,00 ₽,
то есть в 6,0 раза выше потолка. Маржа SKU 4,9 %.

**Это не считается автоматической ошибкой.** По крему для рук зафиксировано
бизнес-исключение: товар распродаётся, осознанный минус допустим.
Классификация **`LIQUIDATION_STRATEGY`**, оценивать отдельно от общего правила потолка маржи.

## 1.7.10 Семантика значений

В файлах Stage 1.7 различаются `0`, `NULL`, `NOT_AVAILABLE`, `NOT_APPLICABLE`,
`NOT_PROVEN`, `OUT_OF_STOCK`. Автоматических `IFNULL(...,0)` в raw и audit слое нет.

---

# Stage 2 — Historical Backfill v1

Дата: 2026-08-31. Период 2026-06-01 → 2026-08-31. Только чтение.
Ничего не менялось: ни цены, ни ставки, ни targetCir, ни бюджеты, ни кампании,
ни карточки, ни остатки, ни акции, ни настройки кабинета.

## Source contract freeze

Использованы только endpoint'ы из `data/finance_api_contract_v1.csv` и
`data/source_contract.csv`. Deprecated не вызывались **ни разу, даже как запасной вариант**:
`/v3/finance/transaction/list`, `/v3/finance/transaction/totals`, `/v2/posting/fbo/list`.

Загрузчики: `raw/backfill_v1/_bf_common.py`, `_bf_finance.py`, `_bf_seller.py`,
`_bf_performance.py`. Сборка фактов: `build_facts.py`, `build_economics.py`.
QA: `qa_backfill.py`.

## Что загружено

221 raw-объект, около 260 HTTP-запросов, все в `raw/MANIFEST.csv` (298 записей).
Структура `raw/backfill_v1/{finance,performance,seller,catalog,public_price,sellmonitor}`.
Существующие raw-файлы предыдущих этапов не изменялись.

Финансы 92/92 дня, 1 428 начислений. Реклама 92/92 дня. Заказы 344 позиции.
Возвраты 221. Каталог 20/20. Ошибок загрузки — 1, не блокирующая (D023).

## QA

**PASSED, 20/20 гейтов, критических отказов 0.** `reports/BACKFILL_QA_REPORT.md`,
машинно-читаемо `data/qa_gates.csv`.

Гейт `no_token_persisted` в первом прогоне дал ложный FAIL — срабатывал на строке
исходного кода `d["access_token"]`, то есть на имени поля. Проверка уточнена до поиска
JWT-формы в файлах данных, скрипты исключены. После уточнения PASS.

## Главные находки этапа

**1. В начислениях есть фактическая ставка комиссии и все компоненты цены.**
`posting.products[].commission` содержит `seller_price`, `sale_price`, `bonus`,
`coinvestment`, `sale_commission` и **`commission_ratio`**. Это снимает необходимость
подставлять вводные 41 % / 52 % — комиссия берётся фактическая, на уровне операции.

**2. Формула владельца подтверждена точно.** Тождество
`seller_price = sale_price + bonus + coinvestment` сходится на **270 транзакциях из 270**,
расхождений ноль.

**3. Переход на 52 % в данных периода почти не проявился (D019).** Из 227 операций
226 прошли по ставке 0,41 и только одна по 0,52 (29 августа). Разбиение по дате начисления
не отражает смену комиссии.

**4. СПП выше вводной (D020).** Взвешенная по портфелю 58,9 % против заявленных 44,6 %;
по наборам 51–63 % против 24–33 %.

**5. Продажи по цене около нуля (D021).** 28.07 крем вишня ушёл за 1,00 ₽ при базе 1 101 ₽.

**6. Нераспределённый рекламный расход (D022).** 24 003 ₽ из 82 604 ₽ приходятся на две
мультитоварные кампании. Искусственно не разносилось.

## Методологическая правка по ходу работы

Первая версия юнит-экономики соединяла выручку и рекламу внутренним join по
`(дата, SKU)` и теряла рекламный расход за дни без продаж этого товара: на SKU попадало
лишь 15 % расхода. Исправлено на полное объединение ключей — теперь на SKU отнесено
58 601 ₽ из 82 604 ₽, остальное честно помечено как нераспределяемое.

## Чего нет и не восстанавливалось

`historical_stock = NOT_AVAILABLE` — API отдаёт только текущий снимок, задним числом
не восстанавливался. `historical_public_shelf_price` почасово = `NOT_AVAILABLE`,
не интерполировалось. Но **полка в момент каждой сделки есть** — `sale_price` в начислениях,
270 транзакций, сохранено в `data/price_history_from_transactions.csv`.

`REF_VK_PRODUCT_COGS` и `REF_BLOGGER_PRODUCT_COGS` остаются `NOT_PROVEN` со значением NULL.

## Остановка

Итоговый отчёт: `reports/HISTORICAL_BACKFILL_V1.md`.
Ничего в кабинете Ozon не менялось. К биддеру не переходил. Автоматизация не создавалась.

---

## 2026-09-01 — правило Marketplace Isolation и проверка соответствия

Владелец зафиксировал жёсткое архитектурное правило: WB и Ozon — два изолированных
marketplace domain. Правило записано в `docs/ozon/architecture/MARKETPLACE_ISOLATION.md`.

Проведена проверка всего, что построено на Stage 1.5–2:
`docs/ozon/architecture/ISOLATION_COMPLIANCE_CHECK_2026-09-01.md`.

**Результат: 4 проверки из 5 PASS, одно нарушение.**

**V-01 / D024.** Домен Ozon читает `wb_raw.REF_SKU_MASTER` для связки
`offer_id → internal_sku`. По правилу это запрещено безусловно.

Таблица оказалась смешанной: общие атрибуты товара, WB-специфичные поля
(`nm_id`, `wb_vendor_code`, `wb_subject_*`) и операционные флаги WB (`include_in_*`).
Колонка `marketplace` = `WB` на всех 25 строках. Целиком в `evetis_ref` переносить нельзя.

Предложено разделение, которое проект уже предвидел в
`docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md:27`:
`evetis_ref.REF_PRODUCT_MASTER` + `evetis_ref.REF_SKU_CHANNEL_MAP`, WB-поля остаются в `wb_raw`.

Отдельно зафиксировано: равенство Ozon `offer_id` и WB `nm_id` — **операционное соглашение
EVETIS, а не факт маркетплейса.** В общем маппинге идентификатор Ozon должен храниться
явной строкой, иначе домен Ozon снова станет зависеть от WB, только неявно.

Записи в BigQuery не выполнялись ни разу за весь аудит. IAM не менялся.
`data/source_contract.csv` помечен: строка `mapping` не соответствует правилу.

---

# Stage 2.1 — Economic Semantics Closure + Marketplace Reference Design

Дата: 2026-09-01. Только чтение. DDL не выполнялся, `wb_raw.REF_SKU_MASTER` не изменялся,
WB-система не рефакторилась, историческая загрузка повторно не выполнялась,
в Ozon ничего не менялось, IAM не менялся, коммит не выполнялся.

**Architecture QA: 12/12 PASS.** `architecture/STAGE2_1_ARCHITECTURE_QA.csv`.

## Что сделано

- `architecture/PRODUCT_REFERENCE_FIELD_CLASSIFICATION.csv` — 22 поля `wb_raw.REF_SKU_MASTER`
  классифицированы: 9 общих, 5 WB-специфичных, 5 операционных флагов WB, 3 отложены.
- `data/proposed_ozon_channel_map.csv` — 45 строк, доказательство резолва **20/20**
  товаров Ozon без обращения к `wb_raw`.
- `data/commission_effective_date_analysis.csv` — 271 операция, 262 сопоставлены с отправлениями.
- `data/economics_semantics_contract.csv` — 14 метрик, ACTUAL и FORWARD разделены.
- `data/discount_semantics_normalized.csv` — 270 транзакций, нейтральная терминология.
- `data/extreme_buyer_price_events.csv` — 1 событие, классифицировано по evidence.
- `data/campaign_attribution_analysis.csv`, `data/sku_economics_readiness.csv`.
- `OZON_DATA_CONTRACT_V1.md`, `OZON_BIGQUERY_DESIGN_V1.md`,
  `architecture/STAGE2_1_COMMIT_SCOPE.md`.

## Главное за этап

**Атрибуция рекламы решена.** Найден источник, которого не было в предыдущем инвентаре:
асинхронный отчёт `POST /api/client/statistics` возвращает **ZIP с CSV на кампанию,
где есть разрез по SKU** с колонкой «Расход, ₽, с НДС». Сумма по SKU сходится с
`statistics/expense` до копейки. `ATTRIBUTION_LEVEL = SKU_ACTUAL`,
распределено **82 088,43 из 82 604,23 ₽ (99,38 %)**. Остаток 0,62 % не разносился.
D022 и D028 закрыты.

**Выводы изменились.** На фактической атрибуции убыточных SKU стало **9 из 18**, а не 5.
Пример: `EVT-SET-ACNE-POWDER-SERUM-CREAM` был +522 ₽, стал **−3 549 ₽**.

**Найден расход архивных кампаний.** 20 898 ₽ за период потратили 4 кампании
в статусе ARCHIVED. Реестр обязан покрывать архивные, иначе история теряется (D031).

**Моя ошибка в отчёте Stage 2 исправлена.** Опубликованные агрегаты считались до
исправления join. Отчёт помечен `SUPERSEDED_BY_STAGE_2_1`, сырые данные не менялись (D025).

**Терминология СПП исправлена.** Слово «СПП» в спецификации Ozon **отсутствует**
(0 вхождений). `SPP_SEMANTICS = NOT_PROVEN`. Введена нейтральная
`observed_buyer_discount_pct` (D026).

**Событие за 1 ₽ объяснено.** Акция Озон Банка, Ozon покрыл 1 100 из 1 101 ₽,
комиссия удержана с полной базы, выплата продавцу 649,59 ₽. `OZON_SUBSIDIZED_PROMO`.

**Определяющее событие для ставки комиссии.** Дата начисления **отвергнута**
(14 контрпримеров). Дата создания заказа согласуется со всеми 262 операциями,
но различающее наблюдение одно. `COMMISSION_EFFECTIVE_DATE = NOT_PROVEN` (D027).

---

# Stage 3.1 — Shared Reference Migration + Ozon BigQuery Foundation

Дата: 2026-09-01. Ozon API mutations не выполнялись. Bidder не начинался.
WB semantics не менялась. Миграция additive. Коммит не выполнялся.

## Решения владельца, применённые в схеме

`product_name_full` → общий атрибут под именем **`canonical_product_name`**.
`status` и `active` → **не перенесены**, остаются WB_SPECIFIC / CHANNEL_OPERATIONAL.
`automation_ready = FALSE` для всех SKU: уровни `economics_ready`,
`bid_recommendation_ready`, `automation_ready` разделены, порог «8 наблюдений»
доказательством готовности больше не считается.

## Создано в BigQuery

`evetis_ref.REF_PRODUCT_MASTER` — 25 строк, 25 уникальных ключей, 0 NULL.
`evetis_ref.REF_SKU_CHANNEL_MAP` — 45 строк: 20 OZON и 25 WB, неоднозначных связок 0.
Датасеты `ozon_raw`, `ozon_stg`, `ozon_mart` — location **EU**, как у существующих.
Восемь таблиц фундамента в `ozon_raw` с партиционированием и кластеризацией по контракту.

**D024 фактически устранён в схеме:** строки OZON содержат `ozon_sku`, `ozon_product_id`
и `offer_id` явными значениями из Ozon API. Пересечение `marketplace_sku` строк OZON
с `marketplace_sku` строк WB — **0**. Резолв 20/20 без обращения к `wb_raw`.

`wb_raw.REF_SKU_MASTER` не изменялся: 25 строк, 22 колонки до и после.
Счётчики объектов WB совпадают с базовой линией.

## Блокер B-01: загрузка исторических фактов не выполнена

`bigquery.googleapis.com` отдаёт **HTML-страницу HTTP 403** на прямые обращения.
`bq load`, `bq mk`, `bq ls` падают при разборе этого не-JSON ответа; `curl` и с
user-token, и с ADC + `x-goog-user-project` даёт тот же 403.

Это **блокировка egress окружения, а не права IAM**: `gcloud secrets` работает,
MCP-инструмент BigQuery работает. Через MCP прошли DDL и небольшие INSERT.

Массовая загрузка через MCP означала бы провести **1,2 МБ SQL-литералов** через
контекст диалога. Это не production-путь и никогда им не станет, поэтому я
остановился, а не стал имитировать загрузку.

Данные подготовлены и лежат в `audit_2026-08-30/load/`: 8 файлов JSONL и 8 файлов SQL,
**3 977 строк**. Загрузка выполняется одной командой, как только появится доступ.

Правильное решение — то же, что уже используется для WB: загрузка из Cloud Run
под сервисным аккаунтом, либо через GCS и `LOAD DATA`.

## Production QA: 10 PASS, 6 BLOCKED, 0 FAIL

`data/stage3_1_production_qa.csv`. Заблокированы P06–P11 — все шесть требуют
загруженных фактов. Критических отказов нет.

---

# Stage 3.1B — Ozon Production Loader Bootstrap

Дата: 2026-09-03. Scheduler не создавался, почасовой сбор цены не запускался,
bidder не начинался, mutations в Ozon не выполнялись. Коммит не выполнялся.

## Инвентаризация: production-паттерн WB СУЩЕСТВУЕТ

Вопреки ожиданию, что реализации Cloud Run для WB нет, она есть и работает.
`docs/ozon/architecture/OZON_LOADER_INFRA_INVENTORY.md`.

Живые ресурсы: Cloud Run Jobs `wb-stocks-shadow`, `wb-stocks-prod`, `wb-mart-prod`;
Scheduler на них же плюс `evetis-wb-poll`; SA `sa-loaders-*`, `sa-scheduler-*`,
`sa-deployer`, `sa-terraform-*`; Artifact Registry `wb-loaders`; Terraform в `infra/terraform`.

Переиспользован **подход** (Cloud Run Job, `europe-west1`, отдельная runtime identity,
секреты только из Secret Manager). Код `cloud/src/` **не переиспользован**: там
`wbHttp`, `REF_SKU_MASTER`, `RAW_WB_*` — бизнес-логика WB, изоляция это запрещает.

## Уточнение B-01

Проверено по каждому API: `gcloud run`, `scheduler`, `iam`, `storage`, `artifacts`
из этой среды **работают**. Не работает **только `bigquery.googleapis.com`**.
Путь GCS → Cloud Run → BigQuery обходит блокировку полностью.

## Создано

Бакет `evetis-ozon-staging-37074083763` (europe-west1, uniform access, public access prevention).
SA `sa-ozon-ingestion` с правами: `bigquery.jobUser` на проект,
`bigquery.dataEditor` **с условием только на `ozon_raw`**,
`bigquery.dataViewer` **с условием только на `evetis_ref`**, `storage.objectViewer` на бакет.
Широких ролей нет, прав на запись в датасеты WB нет.

Условные привязки вместо dataset-level ACL — вынужденное следствие B-01:
`bq` и REST недоступны, а `GRANT ON SCHEMA` через MCP не разрешает регион датасета.
Область прав при этом эквивалентна.

Cloud Run Job `ozon-bootstrap-load`, `europe-west1`, python:3.12-slim,
код в `pipelines/ozon/bootstrap/`.

## Идемпотентность

Стратегия для всех восьми таблиц: **native load job в staging → MERGE по натуральному
ключу → удаление staging**. Ключи проверены на уникальность до загрузки, дублей 0.
`NULL`-безопасное сравнение ключей нужно для `RAW_OZON_FINANCE_ACCRUAL`, где `sku`
законно бывает пустым (752 строки).

Smoke на каталоге и повторный прогон: 20 строк, 20 уникальных ключей, **0 дублей**.
Полный bootstrap выполнялся несколько раз, суммы не удвоились.

## Дефекты, найденные и исправленные по ходу

1. **Переполнение масштаба NUMERIC.** 193 значения имели больше 9 знаков после запятой
   из-за артефактов float. `orders_fbo` падал на загрузке. Все float округлены до 4 знаков.
2. **Размазанный экономический блок.** Цена, комиссия и баллы приписывались каждой
   строке услуги внутри одного отправления, из-за чего выручка удваивалась.
   Исправлено: блок остаётся ровно на одной детерминированной строке пары
   (accrual_id, sku). После правки сумма базы продавца = 338 856,00 ₽.

## QA: 12 PASS, 5 FAIL

`data/stage3_1b_production_qa.csv`.

**P06–P16 все PASS.** Ключевое: маркетинг сошёлся до копейки — 93 744,11 ₽;
тип 41 против Performance 0,00 разницы; тип 54 против SEARCH_PROMO 0,00;
тип 116 ровно 9 862,48; архивные кампании 4 штуки на 20 898,32 ₽ по фактическим данным.

**Пять FAIL — на сверке канона Stage 2.1 с BigQuery.** Расследовано, причины найдены:

| Показатель | Канон 2.1 | BigQuery | Причина |
| --- | --- | --- | --- |
| Выручка | 337 755,00 | **338 856,00** | в локальной сборке потерялась сделка 2026-08-05 sku 3892518031 на 965 ₽. BigQuery согласуется с независимым `price_history_from_transactions.csv` |
| Логистика | 26 675,79 | **30 635,36** | локальный расчёт брал только услуги, привязанные к товару в отправлении |
| Прочие расходы МП | 120,00 | **4 423,63** | хранение и подобные сборы приходят как `NON_ITEM` и в портфель не попадали |
| Прибыль до маркетинга | 101 229,76 | **93 899,67** | следствие трёх строк выше |
| Прибыль после маркетинга | 7 485,65 | **155,56** | то же |

Это дефекты **локальных производных артефактов**, а не загрузки: маркетинговые
контрольные суммы совпали точно. BigQuery-значения полнее и должны стать новым каноном.

**Содержательный вывод меняется: портфель за период фактически около нуля
(+155,56 ₽), а не +7 485,65 ₽.** ДРР всего маркетинга 27,66 %, ДРР CPC 24,38 %.

---

# Stage 3.2A — Ozon Supply & Inventory Production Layer

Дата: 2026-09-03. Только чтение из Ozon. Scheduler не создавался, рекомендаций по
поставкам и автопополнения нет, bidder не начинался. Коммит не выполнялся.

## Терминология исправлена

Остаток 198 единиц на 2026-08-31 больше не называется «текущим». Введены
`latest_known_stock` и `stock_snapshot_at`. `RAW_OZON_STOCKS` стала настоящей серией:
два снимка, 2026-08-31 и 2026-09-03, 284 строки. Предыдущий снимок не перезаписан.

## Главная находка: warehouse_id был в данных всё время

Спецификация объявляет `warehouse_id` в `analytics_data` того же
`/v3/posting/fbo/list`, который уже выгружался. В сырых файлах он присутствует
**в 344 из 344 отправлений**. В Stage 3.1B я его просто не перенёс в загрузку —
это моя недоработка, а не ограничение API.

Следствие: **`SALES_GEO_MAPPING = ID_BASED`**, 27 из 27 складов продаж резолвятся
по идентификатору. Три ранее «потерянных» склада нашлись:
`РОСТОВ-НА-ДОНУ_РФЦ` → Ростов, `ЕКАТЕРИНБУРГ_РФЦ_НОВЫЙ` → Екатеринбург,
`КАЗАНЬ_РФЦ_НОВЫЙ` → Казань. Ранее не атрибутированные 14 % выручки закрыты.
Колонка `warehouse_id` добавлена в `RAW_OZON_POSTINGS_FBO` через `ADD COLUMN IF NOT EXISTS`.

## Семантика выручки доказана

`financial_data.products[].price` равен `seller_base_price` из начислений
на **260 из 261** сопоставленных позиций. Единственное расхождение — сторнирующая
строка по акции за 1 ₽. Контракт: `seller_base_revenue` = `SUM(price × quantity)`,
`sales_units` = `SUM(quantity)`. `buyer_paid_revenue` **не доступен на уровне
отправления**, только через начисления и только по доставленным.

## Созданные объекты BigQuery

`ozon_raw.RAW_OZON_CLUSTERS` 840 строк, `RAW_OZON_SUPPLY_ORDERS` 65,
`RAW_OZON_SUPPLIES` 160, `RAW_OZON_SUPPLY_BUNDLES` 556.
`RAW_OZON_POSTINGS_FBO` расширена колонкой `warehouse_id`.
Загрузчик `pipelines/ozon/bootstrap/loader.py` расширен с 8 до 13 таблиц.

## Покрытие истории поставок

`orders_discovered = 65`, `orders_detailed = 65`, `coverage_pct = 100.0`,
`supplies_inside_orders = 160`. Глубина истории **2025-04-18 … 2026-09-03**.
Статистика 50/65 более не экстраполируется: пересчитано на полном наборе.

## Таймлайн поставки

`created_at` 65 из 65, `planned_arrival_at` (timeslot.from) 62 из 65,
`state_updated_at` 65 из 65, **`actual_arrival_at` 0 из 160**.

`ACTUAL_RECEIPT_TIME = NOT_PROVEN`. Время завершения заявки как дату приёмки
не подставляли. `ACTUAL_RECEIPT_QUANTITY = NOT_PROVEN`: в составе поставки есть
только заявленное `quantity`, поля фактической приёмки нет ни в одной из 556 позиций.

## QA географии: 8 PASS, 2 NOT_PROVEN

`data/stage3_2a_geography_qa.csv`. Резолв складов остатка 25/25, поставок 150/150,
продаж 27/27, неоднозначных связок 0, нерезолвленных продаж 0.
Кластер определялся только по официальному `/v1/cluster/list`.

## Диагностические флаги (НЕ рекомендации)

Stockout при наличии спроса: **Ростов** 0 на складе при 53 шт и 64 939 ₽ за 90 дней,
**Самара** 0 при 19 шт и 26 948 ₽, Воронеж 0 при 4 шт, Омск 0 при 2 шт.
Высокий остаток при низком спросе: **Красноярск** 27 единиц при 1 продаже за 90 дней.
Фрагментация: `EVT-HC-CHERRY-300` 14 единиц на 7 складах.
Реклама на нулевом остатке: `EVT-HC-HAND-300` ушёл в 0, кампания 32157891 RUNNING со ставкой 12 ₽.

Это наблюдения. Никаких предложений по поставкам и ставкам не формировалось.

---

# Stage 3.3 — Production Incremental Ingestion + Audit Freeze

Дата: 2026-09-03. Только чтение из Ozon. Дашборды, витрины, биддер и автоматизация
цен не создавались. Коммит не выполнялся.

## Разделение bootstrap и runtime

`ozon-bootstrap-load` оставлен как механизм исторического восстановления из файлов GCS.
Ежедневным загрузчиком не стал. Создан отдельный `ozon-runtime-ingest`
(`pipelines/ozon/runtime/`), собирающий данные напрямую из Ozon API.

## Уточнение глубины истории

Проверено вызовами: `/v3/posting/fbo/list` отдаёт продажи примерно с апреля 2025,
`/v1/finance/accrual/by-day` — финансы примерно с сентября 2025.
В BigQuery загружено с 2026-06-01: это граница выбранного окна, а не предел API.
История поставок с 2025-04-18 к истории продаж отношения не имеет — сущности разные.

## Инфраструктура

Job `ozon-runtime-ingest`, `europe-west1`, python:3.12-slim, SA `sa-ozon-ingestion`.
Секреты — через клиент Secret Manager, доступ выдан точечно на четыре секрета.
Токен Performance API эфемерный: в памяти, на диск и в логи не попадает.
Планировщик — отдельная идентичность `sa-ozon-scheduler` с ролью `run.invoker`.

Расписания: `ozon-fast` 07/13/19 МСК (остатки, заказы), `ozon-daily` 06:30
(каталог, цены, финансы, реклама, поставки), `ozon-weekly` понедельник 05:00 (кластеры).

## Реестр прогонов

`ozon_raw.OZON_INGESTION_RUNS` — marketplace-независимый, не зависит от слоя фактов WB.
На сущность пишется строка: `ingestion_run_id`, `entity`, окно, запросы, строки,
ретраи, статус. 20 строк за два прогона, 19 со статусом OK.

## Изоляция отказов подтверждена практикой

В первом прогоне `fbo_postings` упал на переполнении масштаба NUMERIC, **девять других
сущностей завершились штатно** и записали данные. Статус прогона `PARTIAL`.
После централизованного округления float до 4 знаков второй прогон дал **10 из 10, `OK`**.

## Late arrival: lookback оправдал себя в первый же день

Runtime добавил начисления за **2026-08-31 на 1 726,67 ₽**, которых не было в backfill
от 01.09 — они ещё не были проведены. Итог: CPC за июнь–август вырос с 82 604,23
до **84 330,90 ₽**.

Это не ошибка загрузки, а поздние данные. Канонический показатель Stage 2.1
следует считать снимком на 01.09, а не окончательной величиной.

## Идемпотентность

Два последовательных прогона: дубли по натуральным ключам **0** во всех сущностях —
финансы, заказы, реклама по кампаниям, реклама по SKU, снимки остатков.
Снимки имеют логическим ключом дату, поэтому повтор в тот же день обновляет строку,
а не создаёт вторую копию.

## QA

Q01 каталог 20/20 · Q04 постингов без `warehouse_id` 0 · Q05 складов продаж без
кластера 0 · Q06–Q10 дублей 0 · Q12 тип 54 = 1 277,40 · Q13 тип 116 = 9 862,48 ·
Q14 архивные кампании 20 898,32 сохранены · Q16 скан секретов чист ·
Q17 deprecated endpoints 0 · Q18 регрессия WB: `wb_raw` 56 объектов, `wb_mart` 42,
`REF_SKU_MASTER` 25 строк — совпадает с базовой линией.

Q11 формально отличается от канона на +1 726,67 — это поздние начисления, разобрано выше.
