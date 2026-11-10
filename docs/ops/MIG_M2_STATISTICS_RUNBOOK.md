# M2 — перенос заказов, продаж и ночной сверки продаж WB из Apps Script в Cloud Run

Статус: **КОД И ИСХОДНИКИ, НИЧЕГО НЕ ПРИМЕНЕНО.** Ни Terraform, ни деплой, ни Scheduler не трогались.
Каждый шаг с эффектом на облако выполняется только по ACK владельца: **M2-SHADOW** (теневой контур и паритет)
и **M2-CUTOVER** (переключение). План переноса в целом: `docs/ops/WB_APPS_SCRIPT_TO_CLOUD_RUN_PLAN_2026-11-10.md` (PR #415).

| Поток | Apps Script (эталон) | Cloud Run | Таблица | `loader_name` |
|---|---|---|---|---|
| Заказы | `runWbOrdersIncremental` | `orders` → Job `wb-orders-<env>` | `RAW_WB_ORDERS` | `orders` |
| Продажи и возвраты | `runWbSalesIncremental` | `sales` → Job `wb-sales-<env>` | `RAW_WB_SALES_RETURNS` | `sales` |
| Ночная сверка продаж | `runWbSalesNightReconcile` | `sales-reconcile` → Job `wb-sales-reconcile-<env>` | `RAW_WB_SALES_RETURNS` | `sales-reconcile` |

Код: `cloud/src/loaders/statistics/*`. Terraform: `infra/terraform/wb_statistics_loaders.tf`.
Сверка паритета: `sql/mig_m2_statistics_parity.sql`.

---

## 1. Семантика (общая для Apps Script и Cloud Run)

### 1.1 Watermark и окна запроса
- **Заказы.** `GET statistics-api /api/v1/supplier/orders?dateFrom=<watermark>&flag=0`. WB трактует `dateFrom` как
  `lastChangeDate` и отдаёт записи с `lastChangeDate ≥ dateFrom`, до ~80 000 строк за ответ, без `dateTo`.
  Пагинация: следующий `dateFrom` — точное `lastChangeDate` последней строки ответа. Конец: пустой массив; или
  неподвижный курсор ниже лимита строк; или курсор сдвинулся, но новых/обновлённых srid нет. Упор в лимит строк на
  неподвижной границе, пустой `lastChangeDate` у непустой страницы, лимит 30 страниц — **PARTIAL**. Внутри прохода —
  last-wins по `srid` (больший `lastChangeDate`).
- **Продажи.** `GET /api/v1/supplier/sales?dateFrom=<watermark>&flag=0` — **ровно один запрос**. ≥ 80 000 строк — граница
  обрезана, ничего не пишем.
- **Сверка.** `dateFrom` = полночь МСК (сегодня − N суток), N = `SALES_RECONCILE_DAYS` (у Apps Script — Script Property
  `WB_SALES_RECONCILE_DAYS`), 1..90, иначе 7. Watermark не трогает.
- Watermark хранится в форме API (`YYYY-MM-DDTHH:mm:ss[.дробь]`), проверяется строго (формат + календарь).
  В RAW заказов `last_change_date` хранится **с пробелом** вместо `T` (первое вхождение), у продаж — с `T`.
- **Fail-closed, at-least-once.** До записи любая ошибка (нет watermark, ошибка API, битый JSON, не массив, невалидная
  строка — `srid`/`saleID` пусты после trim, нет даты, `lastChangeDate` не по формату, кандидат < watermark) → ничего
  не пишется, watermark не двигается. Watermark двигается только после успешной записи и только если есть строки
  **строго новее** (`candidate > watermark`). Кандидат: у заказов — `lastChangeDate` последней строки последней непустой
  страницы, у продаж — максимум по пакету.

### 1.2 Дедупликация и граничные ключи
- **Заказы:** строка пишется, если `last_change_date > watermark`; если `= watermark` — только когда пары
  `srid|row_hash` на этой границе ещё нет в RAW (`wbOrdersBqBoundaryKeys_`). Внутри пакета — дедуп по `row_hash`.
- **Продажи:** внутрипакетный last-wins по `sale_id` (больший `lastChangeDate`, при равенстве — больший
  `md5(raw_json)`); граница — по состоянию `sale_id|md5(raw_json)` (`wbSalesBqBoundaryStateKeys_`), а не по `row_hash`
  (тот не меняется при смене цены/склада).
- **Сверка:** внутрипакетный дедуп по `sale_id|md5(raw_json)` (все различные состояния, не last-wins); пишутся только
  состояния, которых нет в RAW с `last_change_date ≥ fromLcd` (`wbSalesBqStateKeysSince_`).
- Каноническое состояние выбирают вью: `V_WB_ORDERS` (last-wins по `srid`), `V_WB_SALES_RETURNS` (по `sale_id`).
  RAW append-only, дубли строк допустимы.

### 1.3 Нормализация и `raw_json`
- Заказы: все колонки STRING; пустое значение не пишется (NULL). `is_cancel` = `'true'/'false'`, `quantity='1'`,
  `price_with_disc = Number(priceWithDisc) || 0`, SPP-поля (`total_price`, `discount_percent`, `spp`, `finished_price`)
  — **сырые** значения API, отсутствие → NULL, 0 остаётся `'0'`. `region_name` = `oblastOkrugName`.
  `raw_json` у заказов нет. `row_hash = md5(srid|nm|order_day|is_cancel|sticker_id)`. `_order_date` — первые 10
  символов `order_dt`.
- Продажи: типизированная схема (INT64 `raw_row_number/income_id/wb_nm_id`, NUMERIC деньги/проценты, BOOL флаги).
  Деньги округляются до копеек (`Math.round(x·100)/100`), `discount_percent`/`spp` = `Number(x) || 0`.
  Возврат — по контракту WB: `saleID` начинается с `R` → `operation_type='RETURN'`, `is_return=true`.
  `raw_json = JSON.stringify(строки API)` — тот же V8, порядок ключей и форма чисел совпадают с Apps Script, поэтому
  `md5(raw_json)` — общий ключ состояния обеих сторон (`TO_HEX(MD5(raw_json))` в BigQuery).
  `row_hash = md5(srid|nm|sale_day|sale_id|operation_type)`. Пустой `saleID` → `processed_status='MISSING_EVENT_KEY'`.
- `load_id`: `ORD_INC_` / `SALE_INC_` / `SALE_RECON_` + `yyyyMMdd_HHmmss` МСК начала прогона; `loaded_at` — то же время
  `yyyy-MM-dd HH:mm:ss` МСК; `raw_row_number` — номер строки в пакете (1..).

### 1.4 Привязка SKU (замораживается при загрузке)
`sku_match_status` и `internal_sku` ставятся при загрузке и больше не пересчитываются никем (витрина копирует их как
есть). Правило: сначала `nmId` (нормализованный `normalizeNmIdFinance_`), иначе `barcode` (trim); совпало →
`matched` и `internal_sku`; иначе `not_found` и `internal_sku` пуст. Vendor code, предмет и бренд не участвуют.

### 1.5 Схема RAW (живые таблицы, `tables.get` 10.11.2026)
- `RAW_WB_ORDERS`: 33 STRING-колонки + `_order_date DATE`; партиция `_order_date`, кластер `wb_nm_id, srid`.
  Порядок: … `last_change_date`, `_order_date`, `total_price`, `discount_percent`, `spp`, `finished_price`.
- `RAW_WB_SALES_RETURNS`: 40 колонок + `_sale_date DATE`; партиция `_sale_date`, кластер `sale_id, srid, wb_nm_id`.
- Теневые `__CR` (Terraform) — зеркало, включая порядок колонок, партицию и кластер.

### 1.6 Журнал прогона и гейт витрины
- Apps Script: `INGEST_RUNS` (`ingestRunStart_` → STARTED, `ingestFinalizeByStatus_`: `OK`/`OK_NO_CHANGES` → COMPLETE,
  прочее → ERROR с кодом `ORDERS_PARTIAL`, `ORDERS_ERROR`, `SALES_ERROR`, `SALES_SKIPPED_*`). `logical_period` =
  `ingestClosedDayMsk_()` = вчера МСК. Сверка в `INGEST_RUNS` не пишет.
- Cloud Run: `LOADER_RUNS` пишет `cli.ts`. Handler вернул результат → COMPLETE (`rows_fetched` = строк ответа WB,
  `rows_loaded` = записано), бросил `LoaderError` → ERROR с теми же кодами. `logical_period` = D-1 МСК.
  `V_INGEST_HEARTBEAT` берёт строки `environment='prod'`; гейт `cloud/src/loaders/mart/bq.ts` требует, чтобы
  **последняя** попытка `orders` и `sales` за D-1, начатая после полуночи МСК, была COMPLETE.

### 1.7 Повторы, 429 и лимиты WB
- HTTP (`wbFetchWithRetry_`): повтор 429/500/502/503/504 и сетевых исключений, до 3 повторов; пауза — `Retry-After`
  (секунды или HTTP-дата), иначе 20/40/80 с, не больше 60 с. Исчерпано → последний ответ → ERROR/PARTIAL.
- Лимит statistics-api — 1 запрос в минуту (в коде Apps Script: «Sales API 1 req/min, burst 1»). 429 бывает
  «Limited by global limiter, per seller» (`docs/MART_PR3B3_SCHEDULER_2026-08-11.md`) — то есть лимит может быть общим
  на продавца, а не только на метод. Официальную формулировку WB в этой сессии проверить не удалось (страница
  документации WB недоступна для автоматического чтения) — **открытый вопрос**.

---

## 2. Эквивалентность Apps Script → TypeScript

| Apps Script | Cloud Run |
|---|---|
| `runWbOrdersIncremental` / `wbOrdersIncrementalCore_` | `runOrders` (`statistics/index.ts`) + `precheckOrders`, `selectOrdersToAppend` (`orders.ts`) |
| `fetchOrdersApiData_` | `fetchOrders` (`wbApi.ts`) |
| `normalizeOrdersApiRows_`, `computeOrdersRowHash_`, `ordersRawApiValue_` | `normalizeOrdersRows`, `computeOrdersRowHash` |
| `appendOrdersRows_` + `wbOrdersBqAppendRows_` + `bqLoadRows_` | `ordersBqRow` + `StatsBq.appendRows` (один load job) |
| `wbOrdersBqMaxLastChangeDate_` + `wbOrdersBqBoundaryKeys_` + Script Property watermark | `StatsBq.ordersBoundary` (один запрос) |
| `runWbSalesIncremental` / `wbSalesIncrementalCore_` | `runSales` + `precheckSales`, `selectSalesToAppend` (`sales.ts`) |
| `fetchSalesApiData_` / `salesHttpGet_` | `fetchSales` |
| `normalizeSalesApiRows_`, `computeSalesRowHash_`, `salesRound_`, `salesMd5_` | `normalizeSalesRows`, `computeSalesRowHash`, `md5Hex` |
| `wbSalesBqAppendRows_` (типизация) | `salesBqRow` |
| `wbSalesBqMaxLastChange_` + `wbSalesBqBoundaryStateKeys_` | `StatsBq.salesBoundary` |
| `runWbSalesNightReconcile` / `wbSalesNightReconcileCore_` | `runSalesReconcile` + `selectReconcileGaps` |
| `wbSalesReconcileFromLcd_`, `wbSalesReconcileDays_` | `reconcileFromLcd`, `config.salesReconcileDays` |
| `wbSalesBqStateKeysSince_` | `StatsBq.salesStateKeysSince` |
| `wbFetchWithRetry_`, `wbRetryAfterMs_` | `statsGet`, `retryAfterMs` |
| `normalizeDateKey_`, `normalizeNmIdFinance_`, `ordersValidWatermark_`/`salesValidWatermark_` | `normalizeDateKey`, `normalizeNmId`, `isValidWatermark` (`common.ts`) |
| `buildSkuIndex_` (лист `SKU_MASTER`) | `buildSkuIndex` над `wb_raw.REF_SKU_MASTER` |
| `ingestRunStart_` / `ingestFinalizeByStatus_` (`INGEST_RUNS`) | `cli.ts` → `LOADER_RUNS` |
| `ingestClosedDayMsk_` | `d1Moscow` (registry `logicalPeriod`) |
| `LockService.getScriptLock` | lease `LOADER_RUNS` (одна строка STARTED на loader/период/слот) |
| `wbOrdersBqAssertTable_` / `wbSalesBqAssertTable_` (allowlist) | `assertStatsTarget` (`targets.ts`) |

Доказательство эквивалентности — `cloud/test/statistics_apps_script_parity.test.ts`: **боевой код** `.gs` исполняется в
`node:vm` на тех же синтетических сценариях, что и TS; сравниваются байт-в-байт строки для BigQuery, итог журнала,
watermark после прогона и сами URL запросов в WB (21 сценарий: граница, last-wins, привязка SKU, пустые SPP-поля,
возвраты, округление, пустой ответ, невалидные строки, 500 после повторов, 429 с `Retry-After`, битый JSON, не массив,
сверка с пропусками/без пропусков).

---

## 3. Отличия от Apps Script — каждое с обоснованием

1. **Watermark читается из целевой таблицы** (`MAX(last_change_date)` API-строк), а не из Script Property.
   Требование брифа. При штатной работе это одно и то же: свойство двигается только после записи и ровно до
   максимума записанного. Следствия:
   - Cloud Run **не дублирует** строки там, где Apps Script дублировал: при сбое `setProperty` после записи и после
     ночной сверки, дописавшей состояния новее свойства (часовой прогон Apps Script берёт их повторно). Поэтому паритет
     сравнивает состояния, а не строки.
   - **После переключения ручной импорт периода** (`importWbOrdersFromApi` / `importWbSalesReturnsFromApi` в Apps
     Script) **запрещён**: он пишет строки с `last_change_date` до «сейчас», отфильтрованные по дате заказа/продажи,
     и поднял бы MAX таблицы выше реально обработанного — изменения прочих заказов в этом промежутке были бы пропущены.
2. **Один load job на пакет** вместо пачек по 2000 строк: пакет ложится целиком или не ложится — «полузаписи» нет,
   commit-маркер не нужен (watermark = сам факт записи). Число записанных строк сверяется с `statistics.load.outputRows`;
   потерянный ответ load job опознаётся по детерминированному `jobId`.
3. **Пауза между страницами заказов 61 с** вместо 2 с: при лимите 1 запрос/мин вторая страница у Apps Script почти
   всегда получала 429 и ждала повтора. Логика страниц та же, запросов к WB меньше. Job timeout 1800 с.
4. **Справочник SKU — `wb_raw.REF_SKU_MASTER`** (BigQuery-копия листа `SKU_MASTER`, `runRefSync` ~08:22 МСК), а не лист в
   момент загрузки: у Cloud Run нет доступа к книге (M5 переносит и REF). Правило привязки то же. **Риск:** новый SKU,
   добавленный в лист после синхронизации REF, до следующей синхронизации получит `not_found` — и это замораживается
   (витрина копирует статус как есть; потребители связывают продажи по `nm_id`, поэтому деньги от статуса
   не зависят — это метаданные). Мелочи: у REF `internal_sku` обрезан (trim), в листе — нет; дубликат `nm_id` в листе Apps Script
   берёт последним, REF такую публикацию отвергает валидацией.
   Справочник читается **только когда есть что писать** (экономия 2 таблиц × 10 МБ на пустом прогоне).
5. **Guard прогона — слот, а не ScriptLock.** `logical_period` = D-1 (как Apps Script), но guard `LOADER_RUNS` по одному
   периоду пропустил бы все прогоны суток после первого COMPLETE. Поэтому используется существующий механизм
   source-aware guard витрины, где «версия» = часовой слот МСК (`stats-slot:YYYY-MM-DDTHH`, пишется в
   `LOADER_RUNS.source_watermark`): повтор в том же часу после COMPLETE пропускается, следующий слот — новый прогон того же
   D-1. ERROR повтор не блокирует. `cli.ts` и `runManifest.ts` не менялись.
6. **Нет общего 65-секундного cooldown продаж ↔ сверки** (Apps Script: ScriptLock + Script Property
   `WB_SALES_API_LAST_REQUEST_AT_MS`). Lease `LOADER_RUNS` раздельный по `loader_name`, а общий слот потребовал бы
   отдельного BigQuery-запроса на каждый прогон. Замена: расписание разнесено (продажи xx:22, сверка 04:20 — ≥ 1 ч 58 мин)
   и 429 обрабатывается `Retry-After`. Одновременный ручной запуск `sales` и `sales-reconcile` может дописать одно
   состояние дважды — вью канонична, вреда нет.
7. **Сверка пишет `LOADER_RUNS`** (`loader_name='sales-reconcile'`), Apps Script её в `INGEST_RUNS` не писал. Имя
   отдельное, чтобы ERROR сверки не стал последней попыткой `sales` в гейте витрины.
8. **Лог прогона — Cloud Logging** (`orders_increment`, `sales_increment`, `sales_reconcile` с полями
   `watermark_before/candidate/after`, `api_rows_received`, `rows_appended`/`rows_written`, `rows_after_boundary_dedup`,
   `gaps_filled`), а не листы `IMPORT_LOG_ORDERS` / `IMPORT_LOG_SALES_RETURNS`.
9. **Load job без `ignoreUnknownValues`**: колонка, которой нет в таблице, — ошибка, а не тихая потеря поля.
10. **Один секрет** `WB_STATISTICS_SECRET` (по умолчанию `WB_TOKEN_STATISTICS`) без запасного `WB_TOKEN_ANALYTICS`.
    Значение секрета обрезается (trim).
11. **Зависший прогон.** Reaper Apps Script (`ingestReapStaleRuns_`, 15 мин) Cloud Run не касается; у `LOADER_RUNS` свой
    lease — STARTED старше 30 мин не блокирует новый прогон.
12. **Стартовый watermark теневой таблицы** (`ORDERS_BOOTSTRAP_WATERMARK`, `SALES_BOOTSTRAP_WATERMARK`) — только shadow и
    только пока в `__CR` нет API-строк. В prod пустая таблица = ERROR (как `wb*IncrementalBootstrap` без строк).
13. **`DRY_RUN=1`**: план считается, в BigQuery не пишется (Apps Script такого режима не имел).
14. Ветка fallback `normalizeDateKey_` (`new Date(s)` для нестандартных строк) считается в Europe/Moscow явно (Cloud Run
    живёт в UTC). WB отдаёт ISO-даты, ветка в бою не срабатывает.

Наблюдение вне задачи: живая вью `V_WB_SALES_RETURNS` не содержит tie-break `TO_HEX(MD5(raw_json)) DESC`, который есть в
`wbSalesBqCreateViews()` в Git (production снова отличается от репозитория). На загрузчик не влияет; вью не менялась.

---

## 4. Расписание (Europe/Moscow)

Число запусков = Apps Script после PR #414: заказы 4/сут, продажи 4/сут, сверка 1/сут.

| Job | prod | shadow (паритет) | Почему |
|---|---|---|---|
| `wb-sales` | 00:22, 06:22, 12:22, 18:22 | 00:52, 06:52, 12:52, 18:52 | 06:22 — последняя попытка перед витриной 09:00; витрина 12:00 видит её же |
| `wb-orders` | 00:41, 06:41, 12:41, 18:41 | 01:08, 07:08, 13:08, 19:08 | 19 мин после продаж: «global limiter, per seller» |
| `wb-sales-reconcile` | 04:20 | 04:50 | как `wbSalesReconcileInstallNightlyTrigger`; ≥ 1 ч 58 мин от продаж |

Shadow во время паритета ходит в WB тем же токеном, что Apps Script, поэтому его минуты подальше от минут Apps Script
(:22 продажи, :31 заказы — по журналу `INGEST_RUNS`; точные минуты 6-часовых триггеров — вопрос к владельцу).
429 Apps Script после запроса shadow он переживает своим повтором (до 3 раз, пауза до 60 с).

Риск, общий с Apps Script: упавшая попытка 06:xx (LATEST-ATTEMPT = ERROR) блокирует витрины 09:00 и 12:00 до
следующего слота 12:22/12:41. Повтор транзиентного отказа BigQuery/сети делается внутри прогона (`retryTransient`).

---

## 5. Terraform (только исходники; `terraform validate -backend=false` — OK)

**M2-SHADOW** (`plan` → просмотр → `apply` с теми же target'ами):
```
-target=google_bigquery_table.raw_wb_orders_cr
-target=google_bigquery_table.raw_wb_sales_returns_cr
-target=google_bigquery_table_iam_member.shadow_write_orders_cr
-target=google_bigquery_table_iam_member.shadow_write_sales_cr
-target=google_secret_manager_secret_iam_member.shadow_access_wb_token_statistics
-target=google_cloud_run_v2_job.wb_statistics_shadow
-target=google_cloud_run_v2_job_iam_member.scheduler_statistics_shadow_invoke
-target=google_cloud_scheduler_job.wb_statistics_shadow
```
Ожидание плана: 2 таблицы, 3 IAM BigQuery/секрета, 3 Job, 3 invoker, 3 Scheduler (paused) — **все create, prod 0**.
Переменные `statistics_shadow_orders_bootstrap_watermark` / `statistics_shadow_sales_bootstrap_watermark` — стартовый
watermark (см. §6.1).

**M2-CUTOVER** (вечер переключения):
```
-target=google_secret_manager_secret_iam_member.prod_access_wb_token_statistics
-target=google_bigquery_table_iam_member.prod_write_orders
-target=google_bigquery_table_iam_member.prod_write_sales
-target=google_cloud_run_v2_job.wb_statistics_prod
-target=google_cloud_run_v2_job_iam_member.scheduler_statistics_prod_invoke
-target=google_cloud_scheduler_job.wb_statistics_prod
```
Prod Job создаётся на bootstrap-образе (`var.container_image`), env фиксируется при CREATE (дальше `ignore_changes`).
Образ продвигает `deploy-prod.yml` **только в свой Job**: нужен вход `only_job` из PR #416 (Mig1b-0) — после его слияния
добавить `wb-orders-prod`, `wb-sales-prod`, `wb-sales-reconcile-prod` в список допустимых. В этом PR `deploy-prod.yml`
не меняется; `deploy-shadow.yml` обновляет образ трёх shadow-Job, если они уже созданы.

Права: shadow-SA — `dataEditor` только на две `__CR`; prod-SA — только на `RAW_WB_ORDERS` и `RAW_WB_SALES_RETURNS`;
`LOADER_RUNS` и чтение `REF_SKU_MASTER` (+ `REF_SKU_MASTER_DATA`, `REF_ACTIVE_VERSION`) у обоих уже есть (`bigquery.tf`);
`secretAccessor` — поресурсно на `WB_TOKEN_STATISTICS`.

---

## 6. Порядок выпуска

### 6.1 Старт shadow (ACK M2-SHADOW)
1. Владелец: проверить актуальность `WB_TOKEN_STATISTICS` (план §2: v1 от 28.09, вероятно истёк) — обновить версию
   секрета или выбрать `WB_STATISTICS_SECRET` другого токена с категорией «Статистика».
2. Слить PR → `deploy-shadow` соберёт образ D (shadow-Job ещё нет — шаг пропустится).
3. Стартовый watermark: `SELECT MAX(last_change_date)` по `RAW_WB_ORDERS` (заменить пробел на `T`) и по
   `RAW_WB_SALES_RETURNS` в момент старта → в tfvars обеих переменных.
4. `terraform plan/apply` (§5, M2-SHADOW). Затем `deploy-shadow` вручную (`workflow_dispatch`) — образ D в shadow-Job.
5. `scheduler-control.yml`: `run-now` по очереди `wb-sales`, `wb-orders`, `wb-sales-reconcile` (shadow) с интервалом ≥ 2 мин,
   проверить `LOADER_RUNS` (environment='shadow', COMPLETE) и строки в `__CR`; затем `resume` трёх shadow-Scheduler.

### 6.2 Паритет 5 дней
- Ежедневно после 01:30 МСК: `sql/mig_m2_statistics_parity.sql`, `d_from` = первые полные сутки shadow.
- Зачёт: 5 последовательных дней без FAIL по обоим потокам, из них ≥ 3 EXACT по каждому; QUERY 2 = EXACT.
  DATA_DRIFT разбирается QUERY 3 (ожидаемо: промежуточное состояние, которое видел только один опрос; лаг REF).

### 6.3 Переключение вечером (ACK M2-CUTOVER)
Предусловия: паритет подписан; **DRO-1 и детекторы читают `V_INGEST_HEARTBEAT`, а не `INGEST_RUNS`** (иначе после снятия
триггеров DRO потеряет orders/sales — см. §8); `deploy-prod` умеет `only_job`.
1. После вечернего прогона Apps Script 18:xx (в `INGEST_RUNS` COMPLETE для orders и sales) владелец **снимает три триггера**
   в редакторе Apps Script: `runWbOrdersIncremental`, `runWbSalesIncremental`, `runWbSalesNightReconcile`.
2. Сверить watermark: Script Property (`wbOrdersIncrementalStatus` / `wbSalesIncrementalStatus`, только чтение) = MAX
   таблицы. Если MAX таблицы **больше** свойства (ручной импорт периода) — STOP, разбор.
3. `terraform plan/apply` (§5, M2-CUTOVER). Prod Scheduler создаются на паузе.
4. `deploy-prod` с `only_job` для трёх prod-Job — digest D′, проверенный в shadow.
5. `run-now` prod: `wb-sales`, через ≥ 2 мин `wb-orders`. Проверить: `LOADER_RUNS` prod COMPLETE за D-1,
   `V_INGEST_HEARTBEAT` показывает `cloud_run`, `rows_loaded` в разумных пределах (обычно 0–10).
6. `resume` трёх prod-Scheduler. Shadow-Scheduler — `pause` (shadow больше не нужен; удаление — отдельным PR).
7. Утро: гейт витрины 09:00 зелёный, `wb-mart-prod` COMPLETE.

---

## 7. STOP и откат

**STOP** (немедленно `pause` prod-Scheduler):
- любой prod-прогон с `STATS_TARGET_MISMATCH` или с ERROR два слота подряд;
- в `V_WB_ORDERS`/`V_WB_SALES_RETURNS` появились ключи, которых нет у WB (рост числа заказов дня скачком);
- `rows_loaded` prod-прогона ≫ обычного (тысячи строк — признак сброса watermark);
- гейт витрины красный из-за orders/sales.

**Откат:** `pause` prod-Scheduler → владелец ставит обратно три триггера Apps Script (как были: раз в 6 ч; сверка
`wbSalesReconcileInstallNightlyTrigger`). Свойства watermark Apps Script остались на значении момента переключения:
первый прогон Apps Script заново возьмёт изменения, уже записанные Cloud Run, и допишет их ещё раз — дубли строк в
append-only RAW безвредны (вью last-wins), потерь нет. Боевые таблицы и схема не менялись, поэтому другого отката нет.

---

## 8. Что нужно от владельца / открытые вопросы

1. **Токен:** актуален ли `WB_TOKEN_STATISTICS`; если нет — обновить версию или указать другой секрет.
2. **Точные минуты** 6-часовых триггеров Apps Script (для разнесения shadow).
3. **DRO-1 читает `INGEST_RUNS` напрямую** (`sql/health/dro1_02_probe.sql`, `dro1_07_run_failure_ledger.sql`), так же
   `sql/ops/step5a_detectors.sql` и `sql/ops/phase2_health/d2_health_ext_procedure.sql`. После снятия триггеров они
   перестанут видеть orders/sales. Перевод на `V_INGEST_HEARTBEAT` — этап 3 общего плана, предусловие переключения.
4. **Лаг справочника SKU** (§3 п. 4): принимаем «новый SKU до ночной синхронизации REF = `not_found` навсегда» или
   договариваемся, что новый SKU сначала проходит `runRefSync`, а потом появляется в продаже.
5. **Лимит WB:** общий ли лимит statistics-api на продавца или на метод — официально не подтверждено (§1.7).
6. **`deploy-prod.yml`:** добавить три prod-Job в `only_job` после слияния PR #416.
7. **Риск витрины** при упавшей попытке 06:xx (§4) — принять как сейчас у Apps Script или добавить резервный слот
   (это +1 запуск в сутки, против условия PR #414).
