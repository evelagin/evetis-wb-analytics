# Инвентарь endpoint'ов под шаг 2

Источник: официальные списки методов `docs.ozon.ru/api/seller/` и `docs.ozon.ru/api/performance/`,
открытые Playwright 2026-08-30. **Наличие метода в списке ≠ проверено вызовом.**
Фактически проверены только два, отмеченные ✅ (smoke test).

Класс: READ — вызываю; AMBIGUOUS — показываю владельцу; WRITE — не вызываю.

## Seller API (`api-seller.ozon.ru`) — доступ подтверждён

| Пункт шага 2 | Endpoint | Класс | Статус |
| --- | --- | --- | --- |
| Товары, цены продавца, комиссии, остатки | `POST /v3/product/list`, `POST /v3/product/info/list` | READ | ✅ HTTP 200 |
| Цены + индекс цен | `POST /v5/product/info/prices` | READ | не проверен |
| Финансы → Начисления, детализация | `POST /v3/finance/transaction/list` | READ | не проверен |
| Начисления, итоги | `POST /v3/finance/transaction/totals` | READ | не проверен |
| Начисления по дням / типам | `POST /v1/finance/accrual/by-day`, `/v1/finance/accrual/types`, `/v1/finance/accrual/postings` | READ | не проверен |
| Отчёт о реализации товаров | `POST /v1/report/realization/posting/create`, `POST /v1/finance/realization/posting` | READ | не проверен |
| Взаиморасчёты / выплаты | `POST /v1/finance/cash-flow-statement/list` | READ | не проверен |
| Выкупы | `POST /v1/finance/products/buyout` | READ | не проверен |
| Воронка по товару | `POST /v1/analytics/data` | READ | не проверен |
| Остатки FBO по SKU | `POST /v4/product/info/stocks`, `POST /v1/analytics/stocks` | READ | не проверен |
| Заказы FBO, отмены | `POST /v2/posting/fbo/list` | READ | не проверен |
| Возвраты | `POST /v1/returns/list` | READ | не проверен |
| Заявки на скидку | `POST /v2/actions/discounts-task/list` | READ | не проверен |
| — | `/v1/product/import/prices`, `/v1/seller-actions/create/*`, `/v1/actions/discounts-task/approve`, `/v1/product/stairway-discount/*/set` | **WRITE** | **не вызывать** |

## Performance API (`api-performance.ozon.ru`) — доступа пока нет

| Что нужно из шага 4 | Endpoint | Класс |
| --- | --- | --- |
| Список кампаний, статус, размещения, бюджет | `GET /api/client/campaign` | READ |
| Товары в кампании и текущие ставки, `topPosition` | `GET /api/client/campaign/{campaignId}/v2/products` | READ |
| Объекты кампании | `GET /api/client/campaign/{campaignId}/objects` | READ |
| **Подневная статистика** | `GET /api/client/statistics/daily` | READ |
| Расход | `GET /api/client/statistics/expense` | READ |
| Статистика по товарам кампании | `GET /api/client/statistics/campaign/product` | READ |
| Статистика по товарам, оплата за клик | `POST /api/client/statistics/products/sku` | READ |
| **Статистика по поисковым фразам** | `POST /api/client/statistics/phrases` | READ |
| Атрибуция | `POST /api/client/statistics/attribution` | READ |
| Заказ отчёта → UUID → скачивание | `POST /api/client/statistics` → `GET /api/client/statistics/{UUID}` | READ |
| **Конкурентные ставки** | `GET /api/client/campaign/{campaignId}/products/bids/competitive` | READ |
| Рекомендация ставок | `POST /api/client/search_promo/bids/recommendation` | READ |
| Минимальные ставки | `POST /api/client/min/sku`, `POST /api/client/search_promo/get_cpo_min_bids` | READ |
| Лимиты | `GET /api/client/limits/list` | READ |
| — | `activate`, `deactivate`, `PATCH /campaign/{id}`, `PUT .../products`, `PUT .../daily_budget`, `PUT .../period`, `bids/set`, `bids/delete`, `product/enable`, `product/disable`, `all_sku_promo/set_bid` | **WRITE** |

## Чего нет ни в одном API — остаётся браузером или экспортом

| Данные | Почему | Как брать |
| --- | --- | --- |
| **Цена на полке (с СПП) и цена с картой Ozon** | Seller API отдаёт только цену продавца | публичная карточка `ozon.ru`, Sellmonitor как перекрёстная проверка |
| **Размер СПП** | производная от полки и цены продавца | считаем сами |
| Аналитика по поисковым запросам | нет метода в Seller API | кабинет, кнопка «Скачать» на `what-to-sell/all-queries` |
| Позиции в выдаче | нет метода | Sellmonitor `get_product_search_query_positions`; кабинет «Видимость в поиске» — только поштучно |
| Контент карточки: фото, видео, rich, характеристики | частично в API, полнее у Sellmonitor | Sellmonitor `content{images,videos,characteristics,descriptionWords}` + публичная карточка |
| Отзывы, рейтинг, дата последнего отзыва | Seller API даёт рейтинг, не даёт ленту | Sellmonitor `get_product_reviews` + публичная карточка |
| Отчёт по конкурентам | закрыт подпиской Premium | Sellmonitor |
| Скриншоты настроек кампаний | UI Performance | требует доступа к `performance.ozon.ru` |
