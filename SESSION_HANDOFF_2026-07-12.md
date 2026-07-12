# SESSION HANDOFF — 2026-07-12

Снимок состояния EVETIS WB Analytics на конец сессии 12.07.2026.
Главное за сессию: **реклама (Фаза C) полностью в BigQuery и проверена**; **начата Фаза D1 (заказы) — код написан, ещё не прогонялся**.

---

## 1. Управляющая архитектура (директива 12.07)

**BigQuery-first.** Конвейер: `API → BigQuery RAW → Views (дедуп) → MART → Dashboard`. BigQuery — единственный источник истины. Google Sheets — только пульт (меню Apps Script, ручной backfill, конфиг, сервис/логи, диагностика), НЕ хранилище. Новые RAW/CLEAN/UNIT листы не создаём; существующие = legacy, выводим последними (Фаза E).

**Стратегия:** единая платформа EVETIS — со временем в BQ добавляются Ozon, рекламные кабинеты, финансы, CRM, поставки, фулфилмент. Новый источник = новый конвейер API→BQ→MART без переделки. Dashboard ничего не считает, только читает MART; логика в SQL. См. память [[project-architecture-decision]].

**Пер-модульный чек-лист (эталон):** аудит загрузчика → RAW→BQ через флаг-порт (тяга не меняется) → backfill 90 дней → дедуп-вью с ключом, **проверенным эмпирически** → контрольные суммы → следующий модуль.

## 2. Инфраструктура (готово)

- GCP: `project-fa311fc0-4d87-4781-986`, dataset `wb_raw` (EU). Коннектор Google Cloud подключён — Клод сам гоняет SQL (`execute_sql_readonly`, `execute_sql`).
- Слой доступа `WbBigQuery.gs`: `getBqConfig_`, `bqEnsureDataset_`, `bqLoadRows_` (NDJSON load-job), `bqQuery_`, `bqSelfTest`. Переиспользуется всеми модулями.
- Деньги/числа в BQ хранятся STRING → в SQL приводить `SAFE_CAST(REPLACE(REPLACE(col,' ',''),',','.') AS FLOAT64)`.

## 3. Финансы (Фаза B — готово)

`RAW_WB_FINANCE` (69 колонок) + `V_WB_FINANCE` (дедуп report_id|rrd_id). 200 305 строк, 2024-09…2026-07. Комиссия ~27,6%, база = `retail_price_withdisc_rub × quantity`. Рычаги: комиссия → удержание/реклама (4,56 млн) → логистика. Детали — память [[finance-report-economics]], [[finance-v1-migration-status]].

## 4. Реклама (Фаза C — ЗАВЕРШЕНА и проверена 12.07)

5 таблиц `RAW_WB_ADV_*` + 5 вью `V_ADV_*`. Порт флагом `WB_ADS_BQ_SINK` (`WbAdsBigQuery.gs`). Прошёл 3 раунда внешнего аудита (ChatGPT) — все правки внесены (fail-closed init `wbAdsBqInit`, аудит схемы, 5 дедуп-вью, allowlist, preflight, batch, проверка вью).

**Backfill 90 дней (13.04–11.07) проверен в облаке:**
- fullstats: покрытие полное 90/90 дней, расход по SKU ≈ **395 170 ₽**, 24 SKU. Грузили 14-дневными окнами (~200 сек/окно).
- costs (upd): `V_ADV_COSTS` ≈ **425 106 ₽**. Сходится с fullstats (+7,5% — вся реклама с баланса vs привязанное к SKU).
- campaigns: 426 уникальных.
- ⚠️ **Дефект пойман и исправлен:** `updNum` в costs НЕ уникален (2 значения на 272 строки) → ключ `V_ADV_COSTS` = `TO_HEX(SHA256(raw_json))`. См. [[ads-spend-data-model]].
- Не грузили (низкий приоритет): search clusters (это sample; полный сбор — меню «Кластеры за месяц», Фаза D).

Backfill гнать ПО ИСТОЧНИКАМ (не оркестратором): campaigns 1 раз; costs помесячно (`loadWbAdsCostsBackfill90` или меню «RAW: только расходы за период…»); fullstats окнами ≤14 дней (меню «RAW: только fullstats за период…»). У одиночного вызова бюджет 4 мин → чистый PARTIAL, не жёсткий кил.

## 5. Заказы (Фаза D1 — КОД НАПИСАН, НЕ ПРОГНАН) ← продолжаем здесь

Порт `WbOrdersLoader` в BQ по рекламному образцу. Тяга/нормализация не тронуты.
- Новый `WbOrdersBigQuery.gs`: флаг `WB_ORDERS_BQ_SINK`; `wbOrdersBqInit()` (C0 fail-closed); таблица `RAW_WB_ORDERS` (STRING + `_order_date DATE` партиция по дате заказа, кластер wb_nm_id/srid); вью `V_WB_ORDERS` — дедуп **srid + last-wins** (`ORDER BY loaded_at DESC`), т.к. заказы мутируют (заказ→отмена); row_hash как ключ НЕ годится. Есть `wbOrdersBqStats`, `wbOrdersBqAssertViews_`, allowlist, аудит схемы.
- Правки под флагом в `WbOrdersLoader`: константа `ORDERS_RAW_HEADERS_` (28 колонок); sink-ветки в `getRawOrdersSheet_`/`buildOrdersRawHeaderMap_`/`clearOrdersOwnPeriod_` (no-op)/`appendOrdersRows_` (массивы→объекты→BQ); контрольные суммы из памяти `aggregateOrdersRowArray_`.
- Оба файла прошли `node --check`. Меню менять не нужно (C1/backfill — существующий пункт «Заказы WB → Загрузить за период…»).

**СЛЕДУЮЩИЙ ШАГ (за владельцем):** залить оба файла → **C0** `wbOrdersBqInit()` (редактор) → **C1** меню «Заказы WB → за период…» `2026-07-10,2026-07-10` → `wbOrdersBqStats()` → Клод проверит в облаке: покрытие, `COUNT(*)` vs `COUNT(DISTINCT srid)`, отмены, эмпирически ключ (srid с меняющимся is_cancel) → **backfill 90 дней ОДНИМ проходом** `importWbOrdersFromApi('<начало 90д>','<сегодня>')` (НЕ окнами: у эндпоинта нет dateTo, окна объём не режут). ⚠️ Глубина заказов = потолок API ~90 дней (глубже — из финотчёта).

**2-й внешний аудит (12.07, внесён):** исправлена семантика пагинации — курсор = точное `lastChangeDate` последней строки, конец = пустой массив/дренаж/иначе PARTIAL; backfill одним проходом; ежедневный инкремент до триггера переделать на watermark (без фильтра order_dt). Детали — CHANGELOG 2026-07-12 и память [[d1-orders-migration-status]].

## 6. Осталось по дорожной карте

- **D1 Orders:** прогнать C0/C1, проверить, backfill 90 дней.
- **D2 Sales/Returns** (`WbSalesReturnsLoader`) — тем же приёмом.
- **D3 Storage** (`WbStocksLoader`) — snapshot по snapshot_date+warehouse+nmId.
- **D4 Витрины MART:** ДРР по SKU, связать рекламу с удержанием 4,56 млн, ABC, воронка. Партиции + фильтр по дате (экономия квоты 1 ТБ/мес).
- **D5 Dashboard** (web-app, читает MART).
- **Фаза E:** вывести RAW из Sheets.

## 7. Правила

Не усложнять; минимально/обратимо/проверяемо. Всё пользовательское — по-русски. Новые листы — только с разрешения. Перед кодом: описать что/зачем/риск/контрольные цифры → потом код. Ключ дедупа проверять эмпирически (урок updNum).
