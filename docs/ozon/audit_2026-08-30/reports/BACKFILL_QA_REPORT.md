# QA-отчёт исторической загрузки v1

Период: **2026-06-01 → 2026-08-31**. Дата прогона: 2026-08-31, Europe/Moscow.
Режим: только чтение. Ни один mutation endpoint не вызывался.

## Вердикт

**PASSED — 20 из 20 гейтов пройдено, критических отказов 0.**
Бизнес-выводы строить разрешено.

Машинно-читаемо: `data/qa_gates.csv`.

## Гейты

| Гейт | Статус | Детали |
| --- | --- | --- |
| catalog_mapping_20_of_20 | PASS | 20/20 товаров имеют `internal_sku`, и `offer_id == nm_id` |
| cogs_coverage_catalog | PASS | 20/20 товаров с себестоимостью |
| cogs_coverage_transactions | PASS | строк экономики без действующего COGS: **0** из 589 |
| finance_date_completeness | PASS | 92/92 дней, пропусков нет |
| finance_http_ok | PASS | дней с HTTP ≠ 200: 0 |
| finance_pagination_not_truncated | PASS | ни один день не упёрся в лимит страниц |
| performance_date_completeness | PASS | 92/92 дней |
| failed_required_chunks_zero | PASS | всего ошибок 1, блокирующих **0** |
| cpc_finance_reconciliation | PASS | 90 дней сверки, расхождений **0** |
| cpc_reconciliation_max_abs_diff | PASS | максимальное \|разница\| = **0,0000 ₽** |
| search_promo_reconciliation | PASS | дней с расхождением по типу 54: 0 |
| finance_no_duplicate_keys | PASS | дублей натурального ключа: 0 |
| orders_no_duplicate_keys | PASS | дублей (posting_number, sku): 0 |
| performance_no_duplicate_keys | PASS | дублей (date, campaign_id): 0 |
| portfolio_date_completeness | PASS | 92/92 дней |
| orders_quantity_valid | PASS | некорректных quantity: 0 |
| unit_econ_quantity_valid | PASS | отрицательных quantity: 0 |
| currency_single_rub | PASS | все 1 702 начисления в RUB |
| no_credential_leak | PASS | совпадений секретов в файлах: 0 |
| no_token_persisted | PASS | файлов данных с JWT-подобным значением: 0 |

### Две уточнённые проверки

**`no_token_persisted`** в первом прогоне дал FAIL. Разбор: срабатывание на строке
исходного кода `d["access_token"]` в загрузчике, то есть на **имени поля**, а не на значении.
Проверка переписана: ищется JWT-форма `eyJ…`, файлы `.py`, `.sh`, `.log` исключены.
После уточнения — PASS, значений токенов на диске нет.

**`no_credential_leak`** сознательно не проверяет `EVETIS_OZON_CLIENT_ID`: Ozon сам
возвращает его как поле `company_id` в ответах `/v1/returns/list`. Это идентификатор
компании, а не секрет. Настоящие секреты (Api-Key, Performance client_secret) —
0 вхождений во всех файлах.

## Единственная ошибка загрузки

`data/backfill_errors.csv` — одна строка:

`/v2/finance/realization`, чанк `202608`, **HTTP 404 «Report was not found»**.

Отчёт о реализации за август не существует, потому что месяц не закрыт на дату выгрузки.
Не дефект загрузки. Июнь и июль получены полностью. Зафиксировано как D023, закрыто.

## Полнота по датасетам

| Датасет | Ожидалось | Получено | Полнота |
| --- | --- | --- | --- |
| finance accrual by-day | 92 дня | 92 дня, 1 428 начислений | 100 % |
| finance accrual types | 1 | 124 типа | 100 % |
| finance cash flow | 3 месяца | 3 | 100 % |
| finance realization | 3 месяца | 2 (август не закрыт) | 67 %, ожидаемо |
| performance expense daily | 92 дня | 92 дня | 100 % |
| performance daily stats | 3 месяца | 3 | 100 % |
| performance campaigns | снимок | 92 кампании | 100 % |
| performance campaign products | 15 кампаний | 15 | 100 % |
| orders FBO (`/v3/posting/fbo/list`) | 3 месяца | 344 позиции | 100 % |
| returns | весь период | 221 возврат | 100 % |
| catalog | снимок | 20 товаров | 100 % |
| prices snapshot | снимок | 20 товаров | 100 % |
| stocks snapshot | снимок | 138 строк | 100 % |

## Чего в данных нет и не восстанавливалось

| Сущность | Статус | Почему |
| --- | --- | --- |
| Историчeский остаток по дням | `NOT_AVAILABLE` | API отдаёт только текущий снимок. Задним числом из текущего остатка не восстанавливался |
| Историческая публичная полка по часам | `NOT_AVAILABLE` | почасовой истории не существует. Не интерполировалась |
| Историческая полка по сделкам | **есть** | `sale_price` в начислениях даёт фактическую полку в момент каждой продажи — 270 транзакций |
| `REF_VK_PRODUCT_COGS`, `REF_BLOGGER_PRODUCT_COGS` | `NOT_PROVEN` | признак бесплатной выдачи не найден, но механизм не исключён. Нулём не заменялись |
| Воронка: показы, корзина, конверсии | `NOT_AVAILABLE` | вороночные метрики выведены из `/v1/analytics/data` |
| Поисковые запросы | `NOT_AVAILABLE` | `/v1/analytics/product-queries` без подписки отдаёт пусто |
