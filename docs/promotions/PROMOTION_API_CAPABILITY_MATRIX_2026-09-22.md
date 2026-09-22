# EVETIS — матрица возможностей API акций (WB + Ozon)
# Проверено на боевых аккаунтах 2026-09-22, только READ

Все строки «EVETIS проверено = да» означают вызов на аккаунте EVETIS сегодня.
Секреты не печатались. Мутирующие методы **не вызывались** — их строки помечены
`WRITE / DENY` и приведены исключительно для формирования запрета.

Классы доказательств: `CONFIRMED_API` · `CONFIRMED_DOCS` · `CONFIRMED_EXISTING_CODE` ·
`LIKELY` · `UNPROVEN`.

---

## 1. Сводная матрица

| МП | Endpoint | Метод | R/W | Доступен | Область токена | Грейн | Пагинация | Важные поля | Горизонт | Лимиты | EVETIS проверено | Доказательство |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| WB | `dp-calendar-api /api/v1/calendar/promotions` | GET | READ | **да** | «Цены и скидки» (`WB_PRICES_READ_TOKEN`, бит 30 read-only) | акция | `limit` 1..1000 + `offset` | `id, name, startDateTime, endDateTime, type` | история ≥2 лет; вперёд до +29 сут | 10 req/6 s на аккаунт, интервал 600 мс, всплеск 5 (общий для всей категории) | **да**, 200 | `CONFIRMED_API` + `CONFIRMED_DOCS` |
| WB | `dp-calendar-api /api/v1/calendar/promotions/details` | GET | READ | **да** | та же | акция | нет; `promotionIDs` 1..100 за раз | `description, advantages[], inPromoActionTotal/Leftovers, notInPromoActionTotal/Leftovers, participationPercentage, exceptionProductsCount, ranging[]{condition, participationRate, boost}` | только текущие и будущие; по завершённым — пустой массив | та же | **да**, 200 | `CONFIRMED_API` |
| WB | `dp-calendar-api /api/v1/calendar/promotions/nomenclatures` | GET | READ | **нет для EVETIS** | та же | акция × nmID | `limit` 1..1000 + `offset` | `id, inAction, price, planPrice, discount, planDiscount, currencyCode` | — | та же | **да, 422 во всех 14 вариантах** | `CONFIRMED_API` (отказ) + `CONFIRMED_DOCS` («неприменим для автоакций») |
| WB | `dp-calendar-api /api/v2/calendar/promotions*` | GET | READ | **нет** | — | — | — | — | — | — | **да, 404** | `CONFIRMED_API` |
| WB | `dp-calendar-api /api/v1/calendar/promotions/upload` | POST | **WRITE / DENY** | да (не вызывался) | та же категория токена | — | — | добавляет товар в акцию | — | та же | **нет и не будет** | `CONFIRMED_DOCS` |
| WB | `discounts-prices-api /api/v2/list/goods/filter` | GET | READ | да, **уже в проде** | та же | nmID × size | `limit` ≤1000 + `offset` | `discount, clubDiscount, price, discountedPrice, clubDiscountedPrice` | текущее состояние | — | **да**, 200, 25 SKU | `CONFIRMED_EXISTING_CODE` + `CONFIRMED_API` |
| WB | `discounts-prices-api /api/v2/upload/task*` | POST | **WRITE / DENY** | физически заблокирован битом 30 | — | — | — | цены и скидки | — | — | нет | `CONFIRMED_EXISTING_CODE` (`infra/terraform/wb_prices_observer.tf`) |
| OZ | `api-seller /v1/actions` | GET | READ | **да** | Seller API key (роль Admin) | акция | нет | `id, title, action_type, date_start, date_end, auto_add_dates[], freeze_date, potential_products_count, participating_products_count, is_participating, is_voucher_action, banned_products_count, with_targeting, order_amount, discount_type, discount_value` | текущие и будущие; завершённые не отдаются | 50 req/s на Client-Id | **да**, 200, 12 акций | `CONFIRMED_API` + `CONFIRMED_DOCS` |
| OZ | `api-seller /v1/actions/products` | POST | READ | **да** | та же | акция × product_id | `limit` ≤100 + **`last_id`** (`offset` устарел) | `id, price, action_price, max_action_price, alert_max_action_price, alert_max_action_price_failed, add_mode, stock, min_stock, current_boost, price_min_elastic, price_max_elastic, min_boost, max_boost` | текущее участие | 50 req/s | **да**, 200, 18 уникальных SKU в 9 акциях | `CONFIRMED_API` |
| OZ | `api-seller /v1/actions/candidates` | POST | READ | **да** | та же | акция × product_id | то же | те же поля, `action_price = 0`, `add_mode = NOT_SET` | доступность сейчас | 50 req/s | **да**, 200 | `CONFIRMED_API` |
| OZ | `api-seller /v1/actions/auto-add/products/list` | POST | READ | **да** | та же | акция × дата автодобавления × product_id | `limit` 1..100 + `offset` | `product_id, offer_id, sku, name, price, base_price, max_discount_price, min_seller_price, marketplace_seller_price, action_price_to_auto_add, min_action_quantity, quantity_to_auto_add, currency, add_mode` | **будущее: что площадка ДОБАВИТ** | 50 req/s | **да**, 200 | `CONFIRMED_API` + `CONFIRMED_DOCS` |
| OZ | `api-seller /v1/actions/auto-add/products/candidates` | POST | READ | **да** | та же | акция × дата × product_id | `limit` 1..100 + `offset` | те же минус `add_mode` | **будущее: кого МОЖЕТ добавить** | 50 req/s | **да**, 200 | `CONFIRMED_API` |
| OZ | `api-seller /v1/actions/discounts-task/list` | POST | READ | **да, но содержит ПДн** | та же | заявка покупателя | `page` ≥1 + `limit` | полезно: `requested_price, approved_price, approved_discount_percent, min_auto_price, base_price, original_price, approved_quantity_min/max, edited_till, end_at, is_auto_moderated, sku, offer_id, status`; **ПДн: `customer_name, first_name, last_name, patronymic, email, user_comment`** | история заявок | 50 req/s; статус `UNKNOWN` роняет соединение | **да**, 200; NEW=0, SEEN=0, остальные по 50 | `CONFIRMED_API` |
| OZ | `api-seller /v5/product/info/prices` | POST | READ | да, **уже в проде** | та же | offer_id | `cursor` | `marketing_actions{ozon_actions_exist, actions[]}`, `auto_action_enabled`, `auto_add_to_ozon_actions_list_enabled`, `min_price`, `marketing_seller_price` | текущее | 50 req/s | **да**, ежедневно | `CONFIRMED_EXISTING_CODE` |
| OZ | `api-seller /v1/actions/hotsales/*` | POST | — | **нет, удалены площадкой** | — | — | — | — | — | — | **да, 404** | `CONFIRMED_API` + `CONFIRMED_DOCS` (11.03.2025) |
| OZ | `api-seller /v1/seller-actions` | GET | — | **нет такого метода** | — | — | — | — | — | — | **да, 404** | `CONFIRMED_API` |
| OZ | `api-seller /v1/seller-actions/products/candidates` | POST | READ | да (не вызывался) | та же | акция продавца × товар | — | кандидаты в **собственную** акцию продавца | — | 50 req/s | нет — у EVETIS нет своих акций | `CONFIRMED_DOCS` |
| OZ | `/v1/actions/products/activate`, `/deactivate` | POST | **WRITE / DENY** | да (роль Admin позволяет) | та же | — | — | вход/выход из акции | — | — | **нет и не будет** | `CONFIRMED_DOCS` |
| OZ | `/v1/actions/auto-add/products/update`, `/delete` | POST | **WRITE / DENY** | да | та же | — | — | управление автодобавлением | — | — | **нет** | `CONFIRMED_DOCS` |
| OZ | `/v1/actions/discounts-task/approve`, `/decline` | POST | **WRITE / DENY** | да | та же | — | — | согласование скидки покупателю | — | — | **нет** | `CONFIRMED_DOCS` |
| OZ | `/v1/seller-actions/create/*`, `/update/*`, `/products/add` | POST | **WRITE / DENY** | да | та же | — | — | создание акций продавца | — | — | **нет** | `CONFIRMED_DOCS` |
| OZ | `/v1/product/import/prices`, `/v1/product/update/discount` | POST | **WRITE / DENY** | да | та же | — | — | цена, `min_price`, авто-флаги | — | 10 обновлений цены на товар в час | **нет** | `CONFIRMED_DOCS` |
| OZ | `/v1/pricing-strategy/*` кроме `info`/`list` | POST | **WRITE / DENY** | да | та же | — | — | встроенный репрайсер Ozon — конфликтующая система | — | — | **нет** | `CONFIRMED_DOCS` |

---

## 2. Разбор по девяти требуемым разрезам

### 2.1 Доступные акции

| МП | Есть | Чем |
|---|---|---|
| WB | **да** | `/calendar/promotions` (+ `/details` для условий и бустинга) |
| Ozon | **да** | `/v1/actions` |

### 2.2 Товары, доступные для участия (eligible)

| МП | Есть | Чем |
|---|---|---|
| WB | **нет** — для `auto`-акций метод неприменим, а других акций у EVETIS сегодня нет. Косвенно: `details.notInPromoActionTotal` даёт только **число** | `nomenclatures?inAction=false` — заработает на `regular` |
| Ozon | **да, поимённо** | `/v1/actions/candidates` — с `max_action_price`, `min_stock`, границами эластичности |

### 2.3 Товары, участвующие сейчас

| МП | Есть | Чем |
|---|---|---|
| WB | **нет поимённо**; есть `details.inPromoActionTotal` (наших товаров в акции) и `participationPercentage` | — |
| Ozon | **да, поимённо** | `/v1/actions/products` + `add_mode` (MANUAL/AUTO) |

### 2.4 Будущие акции

| МП | Горизонт |
|---|---|
| WB | до **+29 суток** от сегодня (последняя опубликованная — 2940, до 21.10) |
| Ozon | `date_start` в будущем отдаётся; сегодня самая дальняя граница — 06.10; плюс **`auto_add_dates` до 01.10** |

### 2.5 Ограничения акционной цены

| Величина | WB | Ozon |
|---|---|---|
| требуемая акционная цена | **нет** (`planPrice` только для `regular`) | `action_price` (участники), `action_price_to_auto_add` (автодобавление) |
| максимально допустимая | **нет** | `max_action_price` |
| рекомендованная/алерт | **нет** | `alert_max_action_price` + `alert_max_action_price_failed` |
| минимальная цена продавца | `discountedPrice` — это факт, не ограничение | `min_seller_price`, `max_discount_price` |
| границы эластичности | **нет** | `price_min_elastic` / `price_max_elastic` |

### 2.6 Бустинг и ранжирование

| МП | Что видно |
|---|---|
| WB | **лестница `ranging[]`**: `participationRate` → `boost` %, плюс `condition` (`productsInPromotion` / `calculateProducts`). Сегодня диапазон 25–35 % |
| Ozon | `current_boost`, `min_boost`, `max_boost` по каждому SKU. Сегодня у «Эластичного бустинга» 15–55 % |

Это редкий случай, когда **обе площадки раскрывают стимул видимости численно**. Ни одна не
раскрывает, во что бустинг превращается в заказах.

### 2.7 Автодобавление

| МП | Что видно |
|---|---|
| WB | тип акции `auto` и `exceptionProductsCount`; **какие именно наши SKU будут добавлены — нет** |
| Ozon | **полный контракт**: `auto_add_dates[]` → `/auto-add/products/candidates` (кого может) → `/auto-add/products/list` (кого добавит, с `add_mode` и ценой) |

### 2.8 Заявки покупателей на скидку

| МП | Есть |
|---|---|
| WB | нет такой механики в API |
| Ozon | `/v1/actions/discounts-task/list` — **с ПДн, требует фильтрации полей в загрузчике** |

### 2.9 История акций

| МП | Что можно забэкфилить |
|---|---|
| WB | **каталог акций за 2 года** (242 записи до 2024-10-06) — но без состояния наших товаров: `details` по завершённым акциям пуст |
| Ozon | **ничего**: `/v1/actions` отдаёт только текущие и будущие. История состояния возможна только накоплением снимков с момента запуска |

---

## 3. Слепые зоны

| # | Слепая зона | МП | Класс | Последствие для движка |
|---|---|---|---|---|
| B-1 | Какие именно SKU участвуют в акции | **WB** | `CONFIRMED_API` | по WB невозможны решения уровня SKU; возможен только контроль портфеля и предупреждения |
| B-2 | Плановая акционная цена | **WB** | `CONFIRMED_API` | экономику входа в акцию WB посчитать нечем |
| B-3 | Цена покупателя до заказа (СПП) | **WB** | `CONFIRMED_DOCS` | 32 % — допущение владельца, не факт; в формулы не материализуется |
| B-4 | Скидки за счёт площадки, баллы, округление до заказа | **Ozon** | `CONFIRMED_EXISTING_CODE` (PR-0: разрыв до −29 % в `postings.actions`) | выручка на единицу по `price` систематически завышена |
| B-5 | История состояния акций до момента запуска наблюдателя | оба | `CONFIRMED_API` | ретроспективные окна «до/во время/после» невозможны до накопления |
| B-6 | Цена/воронка/остатки до конца августа–сентября 2026 | оба | `CONFIRMED_EXISTING_CODE` | контроль конфаундеров в прошлом невозможен → класс `INSUFFICIENT_DATA` |
| B-7 | Конверсия и показы по SKU на Ozon | **Ozon** | `CONFIRMED_EXISTING_CODE` | воронка есть только по WB (и та с 04.09.2026) |
| B-8 | Механики Hot Sale | **Ozon** | `CONFIRMED_API` | методы удалены площадкой; наблюдать нечем |
| B-9 | Фактический эффект бустинга на заказы | оба | — | измеряется только эмпирически, окнами |
| B-10 | Смысл `stock` в ответах акций Ozon | **Ozon** | `UNPROVEN` | требование к остатку берём из `min_stock`, а `stock` не используем в решениях до выяснения |

---

## 4. Практические следствия для загрузчика

1. **WB**: три GET, общий лимит 10 запросов / 6 с. Полный снимок = 1 запрос списка +
   `ceil(N/100)` запросов деталей. При 12 актуальных акциях это 2 запроса — с запасом.
   Ветка `nomenclatures` вызывается **только** для акций `type != 'auto'`; её отсутствие
   сегодня не ошибка, а состояние площадки, и должно фиксироваться как таковое.
2. **Ozon**: 1 запрос списка + 2 запроса на акцию (`products`, `candidates`) + по одному на
   каждую пару (акция, `auto_add_date`) в `list` и `candidates`. Сегодня это
   1 + 24 + 10 = **35 запросов** при лимите 50 в секунду. Пагинация — `last_id`, не `offset`;
   `limit` ≤ 100.
3. **ПДн**: `discounts-task/list` выносится в отдельный PR и грузится только после явного
   решения владельца, с вырезанием ПДн-полей в загрузчике.
4. **Дрейф схемы**: контракт полей фиксируется списками констант (как
   `cloud/src/loaders/prices/constants.ts`); новое поле → `DRIFT_NEW_FIELDS` и не роняет
   прогон, пропажа обязательного → отказ прогона без подстановки нуля.
