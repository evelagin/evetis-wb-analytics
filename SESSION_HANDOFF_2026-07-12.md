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

## 5. Заказы (Фаза D1 — ✅ migration/backfill ПРИНЯТ; операционный инкремент — нет)

Порт `WbOrdersLoader` в BQ по рекламному образцу. Базовый endpoint и общий каркас загрузчика сохранены, но `fetchOrdersApiData_` переписан под контракт WB (курсор = полное `lastChangeDate` последней строки, конец = пустой массив/дренаж/иначе PARTIAL, fail-closed на битом ответе), а нормализация дополнена `last_change_date`.
- Новый `WbOrdersBigQuery.gs`: флаг `WB_ORDERS_BQ_SINK`; `wbOrdersBqInit()` (C0 fail-closed); таблица `RAW_WB_ORDERS` (STRING + `_order_date DATE` партиция по дате заказа, кластер wb_nm_id/srid); вью `V_WB_ORDERS` — дедуп **srid + last-wins** (`ORDER BY SAFE_CAST(last_change_date AS TIMESTAMP) DESC, SAFE_CAST(loaded_at AS TIMESTAMP) DESC, load_id DESC`), т.к. заказы мутируют (заказ→отмена); row_hash как ключ НЕ годится. Есть `wbOrdersBqStats`, `wbOrdersBqAssertViews_`, allowlist, аудит схемы + строгая проверка партиции по `_order_date`.
- Правки под флагом в `WbOrdersLoader`: константа `ORDERS_RAW_HEADERS_` (29 колонок; 29-я — `last_change_date`); sink-ветки в `getRawOrdersSheet_`/`buildOrdersRawHeaderMap_`/`clearOrdersOwnPeriod_` (no-op)/`appendOrdersRows_` (массивы→объекты→BQ); контрольные суммы из памяти `aggregateOrdersRowArray_`.
- Оба файла прошли `node --check`. Меню менять не нужно (C1/backfill — существующий пункт «Заказы WB → Загрузить за период…»).

**ФАКТИЧЕСКИЙ ПРОГОН (12.07, проверен в облаке):**
- **C0** `wbOrdersBqInit()` — таблица (партиция `_order_date`, кластер `wb_nm_id, srid`) и вью созданы, счётчики 0/0.
- **C1** `2026-07-10`: 21 строка, 21 уник. `srid`, 12 `nmId`, 0 отмен, 1 `nmId` не найден в SKU_MASTER; RAW=21, VIEW=21; `last_change_date` парсится; `_order_date`=2026-07-10.
- **Повторный C1** того же дня: RAW=42, VIEW=21, все 21 `srid` ×2 → эмпирически подтверждён ключ `srid` + append-only. ⚠️ Смена состояния `is_cancel=false→true` пока НЕ наблюдалась — проверим на D1.2.
- **Backfill** `importWbOrdersFromApi('2026-04-13','2026-07-12')`: **91 календарный день включительно**; **3500 уникальных строк по `srid`** (товарные единицы, НЕ 3500 покупательских заказов — `gNumber` не проверялся); 1 страница (3552 → слив по `[]`). Облако: RAW=3542 (2 тестовых прогона, 3 `load_id`), VIEW=3500=уник. `srid`, отмен в VIEW=285, покрытие **91/91 без пропусков**, пустых `srid`=0, непарсимых `last_change_date`=0, `nmId`=22. Sink `WB_ORDERS_BQ_SINK` включён.

**D1.2 (watermark-инкремент) — КОД НАПИСАН, ждёт acceptance (branch `phase-d1.2-orders-watermark`).** Функции `wbOrdersIncrementalBootstrap()` / `runWbOrdersIncremental()` / `wbOrdersIncrementalStatus()` + 3 пункта меню; статусы `OK`/`OK_NO_CHANGES`/`PARTIAL`/`ERROR`; ScriptLock на весь цикл (и bootstrap тоже); запрос по точному watermark без фильтра `order_dt`; at-least-once (watermark двигается только после успешного append). **Безопасная граница:** дописываются строки `> watermark` И новые граничные (`== watermark`, пары `srid+row_hash` которых нет в RAW) — новая `srid` на зафиксированной секунде не теряется; watermark растёт только при `candidate > watermark_before`; `candidate < before`/битый → ERROR. Строгий формат watermark (`YYYY-MM-DDThh:mm:ss`), валидация каждой строки и `cursor_end` до записи. `api_rows_received` — реальное число строк ответа; фактический `load_id` в лог. Лог `IMPORT_LOG_ORDERS` +5 колонок. Триггер НЕ создан. Baseline bootstrap (12.07): `MAX(last_change_date)`=`2026-07-12 13:35:15` → API `2026-07-12T13:35:15` (не хардкодится — читается из RAW в момент запуска). Из аудита данных: лаг изменения до 32 дней, 39 `srid` изменились >14 дней после заказа — обоснование отказа от `order_dt`-фильтра.

**СЛЕДУЮЩИЙ ШАГ (за владельцем, после merge D1.2):** bootstrap 1 раз → **2 ручных прогона** `runWbOrdersIncremental()`: (1) watermark установлен, `order_dt`-фильтра нет, RAW вырос при изменениях, `watermark_after` = макс. точный `lastChangeDate` записанного пакета, VIEW = уник. `srid`, старые заказы с поздними изменениями не отброшены; (2) сразу следом — `OK_NO_CHANGES` (RAW/watermark неизменны) ИЛИ `OK` с монотонным ростом watermark. Запрещено: уменьшение watermark, запись при `PARTIAL`/`ERROR`, продвижение без append. Изолированный SQL/CTE-тест подтверждает выбор последнего состояния (`is_cancel=false→true`), природная отмена — постконтроль. Триггер обсуждаем только после этого. Затем **D2 Sales/Returns** по зрелому шаблону. Бэклог **D1.1**: `raw_json` + полная схема, `regionName` вместо `oblastOkrugName`, мск-таймзона. Детали — CHANGELOG 2026-07-12, память [[d1-orders-migration-status]].

## 5.1 Продажи/возвраты (Фаза D2a — ✅ КОД написан, до acceptance-прогонов)

Ветка `phase-d2a-sales-bigquery`. Аудит D2 + 2 read-only probe живого Sales API завершены; архитектура принята владельцем.

**Доказано probe (`dateFrom` 2026-05-28 и 2026-04-13):** 3319 строк/90д, `distinct saleID = distinct srid = raw_rows = 3319`, пустых `saleID/srid/lastChangeDate`=0, дублей `saleID`=0, конфликтов `saleID↔srid/nmId/date`=0, cap 80000 недостижим, префиксы `S=3319/R=0`, `orderType` в контракте отсутствует, min `date` (2026-03-30) < `dateFrom` → фильтрация по `lastChangeDate`, а не по дате продажи. **event_key = `saleID`** (100% заполнен, уникален, стабилен, без версий). Возвраты у EVETIS ≈0.

**Что написано (флаг `WB_SALES_BQ_SINK` по умолчанию ВЫКЛ):**
- новый `WbSalesReturnsBigQuery.gs`: `wbSalesBqInitC0` (C0 fail-closed), enable/disable/stats/validate; таблица `RAW_WB_SALES_RETURNS` (типизированная: INT64/NUMERIC/BOOL + `_sale_date DATE`), партиция `_sale_date`, кластер `sale_id, srid, wb_nm_id`; вью `V_WB_SALES_RETURNS` — last-wins по `sale_id` (`ORDER BY REPLACE(last_change_date,'T',' ')::TIMESTAMP DESC, loaded_at DESC, load_id DESC`), `WHERE source_api='WB_API_SALES' AND TRIM(sale_id)<>'' AND processed_status<>'MISSING_EVENT_KEY'`.
- правки под флагом в `WbSalesReturnsLoader`: `SALES_RAW_HEADERS_` (40 колонок), новые API-поля + `raw_json`, раздельные `region_name`/`oblast_okrug_name`, `operation_type=SALE/RETURN`, `MISSING_EVENT_KEY` при пустом `saleID`; `fetchSalesApiData_` → один fail-closed запрос (пагинация снята, cap→PARTIAL); `salesHttpGet_` = ровно один `UrlFetchApp.fetch` **без retry** (rate limit 1 req/min; `wbFetchWithRetry_` и константы MAX_PAGES/PAGE_PAUSE/RETRY_* убраны; 429/5xx→ERROR, повтор вручную ≥65с); sink-ветки (getRawSheet/headerMap/clear no-op/append→BQ); `aggregateSalesRowArray_`.
- **`noWindow` (найдено на ревью, критично):** при sink ON нормализация и суммы идут с `{noWindow:true}` — локальное окно `sale_dt` снято, весь change-feed от `dateFrom` сохраняется (Sales API фильтрует по `lastChangeDate`; `sale_dt` может быть раньше `dateFrom`). Legacy sheet (sink OFF) — прежнее окно. Mock `wbSalesNoWindowSelfTest()` подтверждает (sink ON=1/1, sink OFF=0). `node --check` обоих файлов пройден в окружении ассистента (на машине владельца node не установлен — перепроверить отдельно).

**СЛЕДУЮЩИЙ ШАГ (владелец, D2a acceptance):** `wbSalesBqInitC0()` (RAW=0/VIEW=0) → `importWbSalesReturnsFromApi(d,d)` за 1 день (RAW=строкам, VIEW=distinct saleID, пустых ключей/дат=0, `_sale_date` заполнена) → повтор того же дня (RAW растёт append-only, VIEW не меняется → дедуп по saleID подтверждён) → backfill `importWbSalesReturnsFromApi('2026-04-13','<сегодня>')` (~3319 строк, RAW≥VIEW=distinct saleID, покрытие по дням, MAX(last_change_date)). Проверять `wbSalesBqStats()`. Потребители НЕ трогаем. После приёмки — **D2b** (cutover DashboardWb/Cleanwbdaily на вью), затем **D2c** (watermark `WB_SALES_LAST_CHANGE_WATERMARK`, граница `sale_id+row_hash`, триггер).

## 6. Осталось по дорожной карте

- **D1 Orders:** прогнать C0/C1, проверить, backfill 90 дней.
- **D2 Sales/Returns** (`WbSalesReturnsLoader`): D2a код готов (ветка `phase-d2a-sales-bigquery`) → acceptance → D2b cutover → D2c watermark/триггер.
- **D3 Storage** (`WbStocksLoader`) — snapshot по snapshot_date+warehouse+nmId.
- **D4 Витрины MART:** ДРР по SKU, связать рекламу с удержанием 4,56 млн, ABC, воронка. Партиции + фильтр по дате (экономия квоты 1 ТБ/мес).
- **D5 Dashboard** (web-app, читает MART).
- **Фаза E:** вывести RAW из Sheets.

## 7. Правила

Не усложнять; минимально/обратимо/проверяемо. Всё пользовательское — по-русски. Новые листы — только с разрешения. Перед кодом: описать что/зачем/риск/контрольные цифры → потом код. Ключ дедупа проверять эмпирически (урок updNum).
