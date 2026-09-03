# Performance API — списки endpoint'ов

Источник: `docs.ozon.ru/api/performance/` версия 2.0, снято 2026-08-30.
Credentials нет — **код подключения не пишется, ничего не тестируется.**
Хост: `https://api-performance.ozon.ru`. Токен: `POST /api/client/token`, живёт 1800 с.

## READ endpoints required for audit

| Endpoint | Метод | Что даёт | Для какого шага |
| --- | --- | --- | --- |
| `/api/client/campaign` | GET | список кампаний: статус, размещения, бюджет, стратегия | шаг 2, шаг 4 |
| `/api/client/campaign/{campaignId}/objects` | GET | объекты кампании | шаг 4 |
| `/api/client/campaign/{campaignId}/v2/products` | GET | товары кампании и **текущие ставки**, `topPosition` | шаг 4, шаг 5 |
| `/api/client/statistics/daily` | GET | **подневная статистика** по кампаниям | шаг 2, шаг 4 |
| `/api/client/statistics/expense` | GET | расход | шаг 4, контрольные суммы |
| `/api/client/statistics/campaign/product` | GET | статистика по товарам кампании | шаг 4 |
| `/api/client/statistics/products/sku` | POST (READ) | статистика по товарам, оплата за клик | шаг 4 |
| `/api/client/statistics/phrases` | POST (READ) | **статистика по поисковым фразам в рекламе** | шаг 4, шаг 5 |
| `/api/client/statistics/attribution` | POST (READ) | атрибуция — нужна для вилки ДРР узкий/широкий | шаг 4, шаг 6 |
| `/api/client/statistics` → `/api/client/statistics/{UUID}` | POST → GET | заказ отчёта и скачивание | шаг 2 |
| `/api/client/statistics/list` | GET | список сформированных отчётов | служебное |
| `/api/client/campaign/{campaignId}/products/bids/competitive` | GET | **конкурентные ставки** — ключ к связке ставка → позиция | шаг 5 |
| `/api/client/search_promo/bids/recommendation` | POST (READ) | рекомендация ставок | шаг 4 |
| `/api/client/min/sku` | POST (READ) | минимальные ставки по SKU | шаг 4 |
| `/api/client/search_promo/get_cpo_min_bids` | POST (READ) | минимальные ставки CPO | шаг 4 |
| `/api/client/limits/list` | GET | лимиты аккаунта | служебное |

## WRITE endpoints required later for bidder

**Не тестировать. На аудите не вызывать.** Понадобятся только когда система перейдёт из режима
предложений в режим применения — и только после отдельного разрешения владельца.

| Endpoint | Метод | Что меняет |
| --- | --- | --- |
| `/api/client/campaign/search_promo/v2/bids/set` | POST | установка ставок |
| `/api/client/campaign/search_promo/v2/bids/delete` | POST | удаление ставок |
| `/api/client/campaign/all_sku_promo/set_bid` | GET (по факту WRITE) | ставка в Оплате за заказ |
| `/api/client/campaign/{campaignId}/activate` | POST | включение кампании |
| `/api/client/campaign/{campaignId}/deactivate` | POST | выключение кампании |
| `/api/client/campaign/{campaignId}` | PATCH | изменение параметров кампании |
| `/api/client/campaign/{campaignId}/products` | POST / PUT | состав товаров и ставки |
| `/campaign/{campaignId}/daily_budget` | PUT | дневной бюджет |
| `/campaign/{campaignId}/period` | PUT | период работы кампании |
| `/api/client/search_promo/product/enable` | POST | включение товара в продвижение |
| `/api/client/search_promo/product/disable` | POST | выключение товара |
| `/api/client/campaign/{campaignId}/search_promo/bids/reset` | POST | сброс ставок |

Отдельно: `GET /api/client/campaign/all_sku_promo/set_bid` и `/activate` — **AMBIGUOUS по имени, WRITE по смыслу.**
Метод GET, но действие меняет ставку и статус. Классифицированы как WRITE, вызывать нельзя.
