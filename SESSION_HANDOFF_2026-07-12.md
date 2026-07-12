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

**СЛЕДУЮЩИЙ ШАГ — D1.2 (watermark-инкремент), ДО D2:** ежедневное обновление по watermark (точный `lastChangeDate` в Script Properties), запрос по watermark (НЕ по `order_dt`), сохранять все изменения включая позднюю отмену, watermark двигать только после полного успешного append (при ERROR/PARTIAL — не двигать), небольшой overlap + дедуп во вью, триггер включать после 2 ручных прогонов. Контрольный тест D1.2 обязан подтвердить last-wins на реальной смене `is_cancel=false→true`. `importWbOrdersFromApiRolling14Days` на триггер ставить НЕЛЬЗЯ (фильтрует `order_dt`). Затем **D2 Sales/Returns** по зрелому шаблону: API change feed → append-only RAW → srid-key/view → watermark → trigger. Бэклог **D1.1**: `raw_json` + полная схема, `regionName` вместо `oblastOkrugName`, мск-таймзона. Детали — CHANGELOG 2026-07-12, память [[d1-orders-migration-status]].

## 6. Осталось по дорожной карте

- **D1 Orders:** прогнать C0/C1, проверить, backfill 90 дней.
- **D2 Sales/Returns** (`WbSalesReturnsLoader`) — тем же приёмом.
- **D3 Storage** (`WbStocksLoader`) — snapshot по snapshot_date+warehouse+nmId.
- **D4 Витрины MART:** ДРР по SKU, связать рекламу с удержанием 4,56 млн, ABC, воронка. Партиции + фильтр по дате (экономия квоты 1 ТБ/мес).
- **D5 Dashboard** (web-app, читает MART).
- **Фаза E:** вывести RAW из Sheets.

## 7. Правила

Не усложнять; минимально/обратимо/проверяемо. Всё пользовательское — по-русски. Новые листы — только с разрешения. Перед кодом: описать что/зачем/риск/контрольные цифры → потом код. Ключ дедупа проверять эмпирически (урок updNum).
