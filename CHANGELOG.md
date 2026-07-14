# CHANGELOG.md

## История изменений

### 2026-07-13 — Фаза D2c: watermark-инкремент продаж + hourly-триггер (ветка phase-d2c-sales-watermark)

Автоматизация уже принятого D2a: продажи/возвраты дособираются ежечасно по образцу боевого D1.2 (заказы). Bootstrap-якорь (контрольная цифра, не хардкод): `MAX(last_change_date)` в RAW = `2026-07-12T20:14:45` (сверено коннектором: 3395 API-строк, 0 пустых, 0 вне формата, min `2026-04-13T07:03:54`). Sales хранит `last_change_date` уже с `T` → на границе сравниваем напрямую (без замены пробела, в отличие от Orders).

Новый файл `WbSalesIncremental.gs`:
- `WB_SALES_LAST_CHANGE_WATERMARK` (Script Property); `salesValidWatermark_` (строгий `YYYY-MM-DDThh:mm:ss[.frac]` + календарная корректность).
- `wbSalesIncrementalBootstrap()` — из `MAX(last_change_date)` RAW под ScriptLock; **не перезаписывает** существующее, пустой RAW/битый формат → ERROR (без fallback).
- `runWbSalesIncremental()` + ядро `wbSalesIncrementalCore_()` — весь цикл под одним `ScriptLock` (`tryLock`→`finally releaseLock`); параллельный запуск = **SKIPPED_LOCKED** (не ошибка данных, watermark не трогается). Требует `WB_SALES_BQ_SINK=ON` и валидный watermark. Один fail-closed запрос `fetchSalesApiData_(token, dateFrom=watermark)` (1 req/min, без пагинации/retry); `noWindow` (без фильтра `sale_dt`).
- Порядок безопасности (правки по 2-му статическому аудиту): (1) **валидация ВСЕХ сырых API-строк ДО нормализации** — `saleID` непустой, `date` валидна, `lastChangeDate` валиден; хоть одна плохая строка → ERROR с её номером, пакет отклонён (нормализатор молча роняет строки без даты `if(!day)continue` — валидация после него пропустила бы дефект и сдвинула watermark); страховка `rows.length === data.length` после нормализации. (2) `candidate` = max `lastChangeDate` пакета, строго валиден и `≥ watermark_before` (иначе ERROR). (3) **STATE-ключ вместо `row_hash`:** граница/дедуп по `sale_id | md5(raw_json)` (полное состояние). `row_hash` считается лишь из `srid/nmId/sale_dt/sale_id/operation_type` и НЕ меняется при изменении цены/склада/скидки → давал бы ложный дубль. MD5 согласован (Apps Script `salesMd5_` lowercase hex ↔ BQ `TO_HEX(MD5(raw_json))` над тем же `raw_json`). (4) **внутрипакетный last-wins по `sale_id`** (max `lastChangeDate`, tie-break по state-hash) вместо слабого `seen[row_hash]`, который схлопывал разные состояния и оставлял первое. Граница: дописываются строки `> watermark` и граничные (`== watermark`) с новым `sale_id|state`, которого ещё нет в RAW. Append → `rows_written`; watermark двигаем **только** если `candidate > watermark_before` и только после успешного append.
- **Контракт гарантий (точный at-least-once) — две РАЗНЫЕ гарантии:**
  - *Граница watermark:* строки `last_change_date == watermark` защищены state-ключом `sale_id|md5(raw_json)` — уже сохранённое состояние повторно не пишется, новое состояние с тем же timestamp не теряется. Здесь state-key даёт физическую идемпотентность.
  - *Ошибка после append:* при сбое после успешного append (напр. `setProperty` упал) RAW уже дополнен, watermark прежний. На повторе строки СТРОГО новее watermark (`> wm`) будут append-нуты СНОВА (в append-only RAW дубли допустимы). Каноническая идемпотентность — во `V_WB_SALES_RETURNS` (last-wins по `sale_id`); успешный повтор двигает watermark. Range-wide дедуп RAW сознательно НЕ делаем (вью уже канонична). Неверная формулировка «те же строки отсекутся по state-key» убрана — state-key отсекает только граничные (`== watermark`).
- **VIEW tie-break (C2):** в `ROW_NUMBER() OVER(PARTITION BY sale_id ORDER BY …)` добавлен финальный `TO_HEX(MD5(raw_json)) DESC` — при равных `last_change_date/loaded_at/load_id` выбор состояния детерминирован и согласован с внутрипакетным last-wins.
- **Boundary query самодостаточен (C3):** `wbSalesBqBoundaryStateKeys_` фильтрует `sale_id IS NOT NULL AND TRIM(sale_id)<>'' AND raw_json IS NOT NULL` (fail-safe к старым/ручным строкам).
- Статусы: `OK` / `OK_NO_CHANGES` / `SKIPPED_LOCKED` / `ERROR` (искусственный PARTIAL не вводим; упор в лимит строк ответа → ERROR, ничего не пишем).
- `wbSalesIncrementalStatus()` — watermark, sink, RAW `MAX(last_change_date)`, `V_WB_SALES_RETURNS` count.
- Идемпотентная установка триггера `wbSalesIncrementalInstallHourlyTrigger()` (0→создать 1; 1→ничего; 2+→удалить дубли, оставить 1) и `wbSalesIncrementalRemoveTrigger()` — трогают ТОЛЬКО обработчик `runWbSalesIncremental` (Orders/Finance/Ads не затрагиваются). Триггер ставит владелец после ручной приёмки.

`WbSalesReturnsBigQuery.gs`: добавлены `wbSalesBqMaxLastChange_()` (MAX last_change_date среди API-строк) и `wbSalesBqBoundaryStateKeys_(watermark)` (`sale_id|TO_HEX(MD5(raw_json))` на границе). `WbSalesReturnsLoader`: `IMPORT_LOG_SALES_HEADERS_` расширен (аддитивно) полями `watermark_before/after`, `api_rows_received`, `rows_after_boundary_dedup`, `rows_written`, `duration_ms`. RAW-схема НЕ менялась (state-hash считается на лету из существующего `raw_json`).

Ключ дедупа состояния — `sale_id | md5(raw_json)`; финальное состояние — во `V_WB_SALES_RETURNS` (last-wins по `sale_id`). Вью получила лишь **финальный детерминированный tie-break** (C2, поведение при неравных ключах не меняется) — нужно один раз пересоздать `wbSalesBqCreateViews()`. НЕ трогалось: consumer adapter (D2b), RAW-схема, Finance, Ads, PNL. `node --check` трёх файлов пройден. Триггер и Apps Script — после PR/merge и ручной приёмки: пересоздать вью → `wbSalesIncrementalBootstrap()` → прогон1 → прогон2 → fail-closed → `wbSalesIncrementalInstallHourlyTrigger()`.

### 2026-07-13 — Read-only аудит ёмкости книги (ветка diag/workbook-cell-audit)

D2b принят: parity зелёный (SHEET 2813 = BQ 2813, keys=933, все нули), флаг `BIGQUERY`, `buildCleanWbDaily()` собран из `V_WB_SALES_RETURNS` (3319 продаж, 0 ошибок). `buildDashboardWb()` упал с `dashRender_:247 — количество ячеек в книге превысит 10 000 000` — это **лимит Google Sheets**, не дефект D2b (произошло уже ПОСЛЕ успешного чтения из BQ). Google Drive-коннектор: книга 13.8 МБ, но `read_file_content` для книги такого размера возвращает пусто и не видит аллокацию сетки (пустые ячейки), которая и упирается в лимит.

Добавлен новый файл `WbWorkbookAudit.gs` — read-only `wbWorkbookCellAudit()` (ничего не пишет/не удаляет): по каждому листу logs сетку `maxRows×maxColumns`, использованный `lastRow×lastCol` и «пустой резерв» (сетка−использовано), сортировка по сетке; итог по книге в % от 10 млн и остаток.

**Результат аудита (41 лист):** книга на **100%** — сетка 9 997 687 / 10 000 000, свободно 2 313 ячеек (потому и упал дашборд). Крупнейший потребитель — `RAW_WB_FINANCE` 6 676 946 (67%, 90228×74, заполнен целиком → структурно уходит только в Фазе E после подтверждения, что PNL читает BQ). **Пустой резерв по книге = 1 628 512 ячеек** (сетка−заполнено) — убирается без потери данных. Отдельный кандидат на удаление — лист-бэкап `BACKUP_PR12_RAW_WB_FINANCE_20260617_163216` (564 916). Вывод: освобождение ячеек — предпосылка и для UNIT/PNL (PNL_TOTAL сейчас пуст, книга у потолка), не только для дашборда.

Добавлена guarded `wbWorkbookTrimEmptyReserve_(opts)` — срезает пустой резерв (строки ниже lastRow / столбцы правее lastCol, запас `margin=2`), в данные не заходит, per-sheet `try/catch`, **по умолчанию dry-run** (только лог). Реальное применение — `{dryRun:false}`; опция `skip:[...]`. Порядок: (1) удалить лист-бэкап + `wbWorkbookTrimEmptyReserve_({dryRun:false})` → ~2.1 млн ячеек (книга ~78%); (2) прогнать UNIT/PNL на BIGQUERY; (3) `buildDashboardWb()`. Долгосрочно Dashboard → D5 (web-app на MART), финансовый RAW из Sheets → Фаза E.

### 2026-07-13 — D2b: parity ограничен честной базой (source_api + boundary) (ветка fix/d2b-diag-date-normalization)

Архитектурное решение владельца: **legacy-import ранних продаж не делаем**. Оперативная история Sales API начинается с **2026-04-13** (нижний край сплошного покрытия BQ), полная финансовая история — в модуле Finance (с 2024-09-05, 201 211 строк). 522 API-строки листа за 01–12.04 — случайный неполный хвост (нет января/февраля/марта/2025/2024), их перенос не делает историю полной и не нужен. 10 строк `source_api=TEST` — не реальные события WB API. 90-дн. лимит — только на ПОЛУЧЕНИЕ из WB; уже сохранённое в BQ не исчезает, история накапливается вперёд через watermark+перехлёст+append+дедуп во VIEW (D2c).

`wbSalesParityAggregate_` теперь считает **честную общую базу**: пропускает `source_api !== 'WB_API_SALES'` (тест/legacy) и строки `sale_dt < WB_SALES_BQ_BOUNDARY_` (2026-04-13); `is_duplicate`-пропуск и авторитетный `is_return` сохранены; добавлен счётчик `rows`. `wbSalesConsumerParityTest_` вызывает агрегат с boundary и логирует фильтр (`source_api=WB_API_SALES AND sale_dt >= 2026-04-13`), печатает `SHEET/BQ API rows>=boundary`. Константа `WB_SALES_BQ_BOUNDARY_` вынесена к остальным константам вверху файла. Меняется только `wbSalesConsumerSource.gs` (функции parity), reader-контракт и потребители не тронуты.

Ожидание acceptance: SHEET API rows>=boundary = 2813, BQ = 2813, `quantity/money/missing = 0`. Сверено коннектором: `V_WB_SALES_RETURNS` в `[2026-04-13, 2026-06-23]` = **2813 строк, 21 SKU, 0 возвратов**. После зелёного parity → cutover (`wbSalesConsumerUseBigQuery()` → `buildCleanWbDaily()`/`buildDashboardWb()` → UNIT/PNL) → D2c.

### 2026-07-13 — D2b диагностика hotfix: нормализация дат (ветка fix/d2b-diag-date-normalization)

Первый прогон `wbSalesConsumerSourceClassification()` дал ложный `missing unique=0` и пустые бакеты: `getValues()` отдаёт `sale_dt` из листа как JS `Date`, а `String(date).substring(0,10)` = «Sat May 23», что не проходит `^\d{4}-\d{2}-\d{2}$` → все 3345 строк пропускались (дамп `EVT-HC-BODY-300` работал, т.к. без фильтра по дате). Бизнес-факт при этом получен: BODY-300 — `source_api=TEST`, `sale_id` пустой, `last_change_date` пустой, `operation_type=Продажа`, `quantity=2` → сценарий C подтверждён (тестовый/legacy-хвост, не запись WB Sales API).

Фикс только в `wbSalesConsumerSourceClassification()`/`wbSalesDiagDumpRow_`: `sale_dt` и `last_change_date` нормализуются через существующий `wbSalesDateStr_()` (Date → `yyyy-MM-ddTHH:mm:ss`), день = первые 10 символов. Дамп теперь печатает нормализованные даты. Production-reader/parity/flag не тронуты, поведение адаптера не менялось. `WB_SALES_CONSUMER_SOURCE=SHEET`, cutover запрещён. После фикса повторный запуск `wbSalesConsumerSourceClassification()` даст реальные бакеты Период×source_api и долю ранних API-строк vs non-API хвоста.

### 2026-07-13 — D2b диагностика: классификация источников листа (read-only, ветка diag/d2b-sales-source-classification)

Первый runtime-parity после legacy-hotfix показал: гипотеза дублей НЕ подтвердилась (`duplicate rows total=0`), расхождение имеет иную природу. Доказано по BigQuery-коннектору (read-only): BQ RAW = **100% `source_api=WB_API_SALES`** (3395 строк); сплошное покрытие начинается с **2026-04-13** (03-30 = 1 строка `noWindow`-артефакт, 31.03–12.04 = 0); `EVT-HC-BODY-300`/`nmId=252442341` отсутствует в BQ полностью. Лист (3345 строк, 01.04–23.06) содержит раннюю историю (01–12.04, вне backfill и уже вне 90-дн. retention WB API) и строки вида `operation_type=Продажа`/`quantity=2` — вероятные legacy/не-API записи. Корневой разрыв: вью фильтрует `source_api='WB_API_SALES'`, а SHEET-reader/parity — нет (старый `DashboardWb` не-API строки отбрасывал).

Добавлена **только read-only** функция `wbSalesConsumerSourceClassification()` (production-reader/parity/flag НЕ трогает; читает сырой лист напрямую — нужен `sale_id`, которого нет в каноническом контракте). Выводит в лог: бакеты Период(до/с `2026-04-13`) × source_api(`WB_API_SALES`/пусто/иной) с rows/uniq keys/qty/amountKop/мин-макс `sale_dt`/`last_change_date`; агрегаты missing-ключей (до/с boundary, с API/empty/other-строкой); построчный дамп missing (предел 300) и все строки `EVT-HC-BODY-300` с полями `sale_id/source_api/last_change_date/vendor/barcode/oper/qty/fin`. Цель — расклассифицировать 141 missing-ключ для выбора: (1) одноразовый legacy-import подтверждённых API-строк в BQ (`RAW_WB_SALES_RETURNS_LEGACY`), (2) правила для не-API строк, (3) canonical view с provenance (`record_origin`), (4) финальный parity и cutover. Production-cutover запрещён, `WB_SALES_CONSUMER_SOURCE=SHEET`, historical boundary пока не внедряется.

### 2026-07-13 — D2b hotfix: legacy-семантика продаж в parity (ветка fix/d2b-sales-parity-legacy-semantics)

Follow-up после первого runtime-parity: он корректно провалился и остановил cutover. Диагноз подтверждён по BigQuery (`RAW_WB_SALES_RETURNS`/`V_WB_SALES_RETURNS`): по спорным ключам `raw_rows = uniq_sale_id = view`, `missing_key=0` — вью НЕ теряет валидных продаж и не схлопывает их. Лишние строки — на стороне замороженного листа: у него есть колонка `is_duplicate` (loader D2a её сохранял), которой во вью нет концептуально. Прежний SHEET-reader безусловно ставил `is_duplicate=false`/`quantity=1`, из-за чего legacy-дубли (их старый `DashboardWb` исключал по `is_duplicate`) считались продажами → лист давал больше строк на более коротком периоде (SHEET 3345 строк 2026-04-01…06-23 vs BQ 3319 строк 2026-03-30…07-12; примеры: HAND-300 05-23 лист 9 / BQ 8, CHERRY-300 05-23 лист 3 / BQ 2).

Scope только D2b (loader/RAW/VIEW/watermark/триггеры не трогаются). Меняется только `WbSalesConsumerSource.gs`; `DashboardWb.gs`/`Cleanwbdaily` — без изменений. `node --check` пройден.

- **SHEET-reader — реальная legacy-семантика:** `wbSalesReadSheetCanonical_` ищет колонки `quantity`(`quantity`/`qty`) и `is_duplicate`(`is_duplicate`/`isduplicate`) и подставляет их нормализованные значения; синтез `1`/`false` — только при отсутствии колонки. BQ-reader без изменений (во вью полей нет → `quantity=1`, `is_duplicate=false` корректны для дедуп last-state).
- **parity исправлен содержательно:** `wbSalesParityAggregate_` пропускает строки `is_duplicate === true` (сравнение «лист без дублей» vs BQ; на BQ-стороне фильтр безвреден), `qty = Math.abs(quantity) || 1`, возврат — по авторитетному каноническому `is_return` (без повторной деривации из `operation_type`).
- **публичные обёртки (репозиторий = Apps Script):** `wbSalesConsumerParityTest()` → `wbSalesConsumerParityTest_()`; диагностика публична без завершающего `_`.
- **read-only диагностика `wbSalesConsumerParityDiagnostics()`** (флаг/данные не трогает, только console.log): наличие колонок `is_duplicate`/`quantity`, всего дублей и дублей в overlap, non-dup строки, BQ-строки; missing/qty/money **до и после** исключения дублей; до 20 спорных ключей с разбивкой листа raw/dup/non-dup vs BQ; явный дамп исходных строк `EVT-HC-BODY-300` в overlap (для проверки, все ли они дубли).

Acceptance hotfix (флаг остаётся `SHEET`): `wbSalesConsumerParityDiagnostics()` → анализ BODY-300 → `wbSalesConsumerParityTest()`. Ожидание после фикса: `keys>0`, `quantity mismatch=0`, `money mismatch=0`, `missing keys=0`. Если после исключения дублей остаётся только BODY-300 — разобрать его строки: если все дубли → ок; если нет — отдельно выяснить пробел в BQ, cutover не выполнять до объяснения каждой разницы. `WB_SALES_CONSUMER_SOURCE=BIGQUERY` не включаем до зелёного parity.

### 2026-07-13 — Фаза D2b: cutover потребителей продаж на BigQuery (вариант B, код до acceptance)

Снятие полу-переключённого состояния D2a: `WB_SALES_BQ_SINK=ON` пишет в BigQuery, но `DashboardWb`/`Cleanwbdaily` читали замороженный лист `RAW_WB_SALES_RETURNS`. Выбран **вариант B** — прямой cutover потребителей на вью через ЕДИНЫЙ адаптер с feature-flag и мгновенным откатом. Мост BQ→лист (A) и dual-write (C) отклонены. Ветка `phase-d2b-sales-consumer-cutover`; `node --check` трёх файлов пройден; commit/push — за владельцем. Флаг по умолчанию `SHEET` до parity и ручной приёмки.

Границы D2b (НЕ трогалось): loader D2a, watermark, триггеры, RAW-схема, SQL вью `V_WB_SALES_RETURNS`, finance, ads, MART, PNL-формулы, общий `bqQuery_`.

Новый файл `WbSalesConsumerSource.gs`:
- feature-flag `WB_SALES_CONSUMER_SOURCE = SHEET | BIGQUERY` (Script Property, **default SHEET**). Функции `wbSalesConsumerUseBigQuery()`/`wbSalesConsumerUseSheet()`/`wbSalesConsumerSourceStatus()`.
- `readCanonicalSalesRows_({fromDate,toDate,allowEmpty})` — единственный слой чтения. Возвращает 2D `[header, ...rows]` (форма `getValues()`), потребители используют именные пикеры без изменений.
- **Единый нормализованный 12-колоночный контракт для ОБОИХ источников** (одинаковые логические типы): `sale_dt`/`last_change_date` string `yyyy-MM-ddTHH:mm:ss`; `internal_sku`/`wb_nm_id`/`wb_vendor_code`/`barcode`/`operation_type`/`source_api` string (**`wb_nm_id` — строка**, INT64 может выходить за Number); `finished_price`/`quantity` number; `is_return`/`is_duplicate` boolean. `quantity=1` и `is_duplicate=false` синтезируются централизованно в обоих режимах (в вью/листе их нет).
- **BIGQUERY:** явный SELECT только нужных полей в каноническом порядке + `1 AS quantity`, `FALSE AS is_duplicate`; обязательный partition-filter `WHERE _sale_date >= DATE '<from>'`; детерминированный `ORDER BY sale_dt, sale_id`. Дата — **валидированный литерал** (regex `^\d{4}-\d{2}-\d{2}$`), НЕ query-параметр (`bqQuery_` не расширялся).
- **Нижняя граница истории — постоянная стартовая дата** `WB_SALES_CONSUMER_FROM` (Script Property, fallback константа `2024-09-01`), НЕ скользящее окно — ранняя история не исчезает из пересчитываемых витрин.
- **FAIL-CLOSED:** в BIGQUERY-режиме ошибка запроса ИЛИ пустой результат при `allowEmpty=false` → исключение с явным префиксом `[SALES ADAPTER]`. Молчаливого отката на SHEET НЕТ. Чтение у потребителей идёт ДО очистки витрин → исключение прерывает сборку, данные целы. `allowEmpty=true` — только для parity/узких проверок.
- `salesHeaderMapFromRow_()` — карта {имя→индекс} из канонической шапки для `Cleanwbdaily` (совместима с общим `findCol_`).
- `wbSalesConsumerParityTest_()` — read-only сверка SHEET vs BIGQUERY по date×SKU на **точной границе пересечения** `[max(min), min(max)]` (печатает `SHEET rows/min/max`, `BQ rows/min/max`, `comparison from/to`). Деньги — целые копейки `Math.round(x*100)`. Критерии приёмки: `quantity mismatch=0`, `money mismatch (копейки)=0`, `missing keys=0`.

Правки потребителей (минимальные, только блок чтения; downstream-логика без изменений):
- `DashboardWb.gs` (`dashBuildSpine_`): вместо `getSheetByName(DASH_SRC_SALES_)`+`getRange().getValues()` → `var sv = readCanonicalSalesRows_({allowEmpty:false})`.
- `Cleanwbdaily` (секция 3): вместо прямого листа+`getHeaderMap_(srSheet)`+`readSheetData_(srSheet)` → `readCanonicalSalesRows_({allowEmpty:false})`, шапка через `salesHeaderMapFromRow_(srValues[0])`, данные `srValues.slice(1)`.

Сверено коннектором на момент правок: `V_WB_SALES_RETURNS` = 3319 строк = 3319 уник. `sale_id`, возвратов 0, SKU 21, диапазон 2026-03-30…2026-07-12; `sale_dt`/`last_change_date` уже в формате `yyyy-MM-ddTHH:mm:ss`.

**Правки по статическому ревью (2026-07-13):**
- **feature flag fail-closed:** `wbSalesConsumerSource_()` возвращает SHEET только при отсутствии/пустом свойстве; любое иное значение (опечатка/повреждение) → исключение, а не молчаливый SHEET (иначе после cutover — внешне корректная устаревшая отчётность).
- **единый контракт fromDate/toDate:** SHEET-режим теперь тоже применяет границы (по первым 10 символам `sale_dt`, симметрично BQ); `from` по умолчанию = `2024-09-01`, не «вся история». Публичный контракт адаптера больше не источник-зависимый.
- **проверка формы ответа BQ:** перед маппингом каждой строки — контроль `Array.isArray(f) && f.length === 12`, иначе контролируемый `[SALES ADAPTER] Invalid BigQuery row shape` (вместо технического TypeError).
- **`Cleanwbdaily`:** каноническое `is_return` — первый источник истины при детекции возврата; `operation_type` и знак — fallback.

**Правки по 2-му раунду ревью (2026-07-13):**
- **`allowEmpty:false` симметричен и в SHEET:** пустой лист/отсутствие листа И пустой результат после фильтрации → исключение `[SALES ADAPTER] empty Sheet …` (раньше SHEET молча возвращал пустое → опасный rollback: `DashboardWb` перестроил бы витрину без продаж). Теперь единый контракт: production-чтение с `allowEmpty=false` не отдаёт пустые продажи ни в одном режиме.
- **`is_return` действительно авторитетно:** в `Cleanwbdaily` при наличии канонической колонки её значение решает полностью (явный `false` не переопределяется `operation_type`/знаком); fallback — только при отсутствии поля (легаси-лист без `is_return`).

Acceptance (порядок, флаг остаётся SHEET): `wbSalesConsumerSourceStatus()` → `wbSalesConsumerParityTest_()` зелёный (проверить ненулевое число comparison keys) → `wbSalesConsumerUseBigQuery()` → `buildCleanWbDaily()` → `buildDashboardWb()` → сверка UNIT/PNL → тест неправильного флага (ожидать исключение) → откат `wbSalesConsumerUseSheet()`. Флаг держать ≥1–2 недели. Отдельно: hotfix D2a `fix/d2a-bq-type-aliases` коммитится независимо.

### 2026-07-12 — Фаза D2a: продажи/возвраты в BigQuery (RAW+view, код до acceptance)

Порт `WbSalesReturnsLoader` в BigQuery по образцу заказов (Фаза D1). Флаг `WB_SALES_BQ_SINK` по умолчанию ВЫКЛ; листовое поведение при выключенном sink сохранено. Триггер, watermark-инкремент и cutover потребителей (DashboardWb/Cleanwbdaily/UNIT/PNL) в этот PR НЕ входят (D2b/D2c). `node --check` обоих файлов пройден в окружении ассистента (на машине владельца node не установлен — синтаксис перепроверяется отдельно).

**Эмпирика (2 read-only probe живого Sales API, 2026-05-28 и 2026-04-13):** 3319 строк/90д, `distinct saleID = distinct srid = raw_rows`, пустых `saleID/srid/lastChangeDate`=0, дублей `saleID`=0, конфликтов `saleID↔srid/nmId/date`=0, cap 80000 не достигнут, префиксы `S=3319/R=0`, `orderType` отсутствует, min `date` (2026-03-30) < `dateFrom` (2026-04-13) → фильтрация по потоку изменений. **Доказано: event_key = `saleID`** (заполнен 100%, уникален, стабилен, без версий). Возвраты у EVETIS ≈0 (в финотчёте 16 за ~22 мес) — return-ветка остаётся валидируемой на входе (постконтроль при первом реальном `R`).

Новый файл `WbSalesReturnsBigQuery.gs`:
- флаг `WB_SALES_BQ_SINK`; `wbSalesBqInitC0()` (C0 fail-closed: preflight→таблица→вью→счётчики, при ошибке sink гасится), `wbSalesBqEnable()/Disable()`, `wbSalesBqStats()` (RAW / MISSING_EVENT_KEY / уник. sale_id во вью), `wbSalesBqValidateViews()`.
- таблица `RAW_WB_SALES_RETURNS` — **типизированная** схема (STRING по умолчанию + `INT64`: raw_row_number/income_id/wb_nm_id; `NUMERIC`: total_price/discount_percent/spp/payment_sale_amount/price_with_disc/finished_price/for_pay; `BOOL`: is_supply/is_realization/is_return) + служебная `_sale_date DATE`. Партиция по `_sale_date`, кластер `sale_id, srid, wb_nm_id`. Аудит схемы типо-чувствительный (несовместимый тип → обрыв), строгая проверка партиции по `_sale_date`; `raw_json` (STRING) хранит оригинал ответа.
- `wbSalesBqAppendRows_` — приведение значений к типам колонки (пустые опускаются → NULL), `_sale_date` из `sale_dt[0:10]`, батч 2000.
- вью `V_WB_SALES_RETURNS` — дедуп **last-wins по `sale_id`**: `PARTITION BY sale_id ORDER BY SAFE_CAST(REPLACE(last_change_date,'T',' ') AS TIMESTAMP) DESC, loaded_at DESC, load_id DESC`; `WHERE source_api='WB_API_SALES' AND TRIM(sale_id)<>'' AND processed_status<>'MISSING_EVENT_KEY'`. Продажа и потенциальный возврат одного `srid` НЕ схлопываются (у них разные `sale_id`); `row_hash` в ключе НЕ используется.
- **Хотфикс (C0 в облаке, canonicalization типов):** первый `wbSalesBqInitC0()` создал `RAW_WB_SALES_RETURNS` корректно, но аудит схемы упал — BigQuery API отдаёт **канонические** имена типов (`INTEGER`/`BOOLEAN`/`FLOAT`/`RECORD`), а сравнение шло с SQL-алиасами (`INT64`/`BOOL`/…): `raw_row_number тип INTEGER, ожидался INT64`. Это один тип. Добавлен `wbSalesBqCanonicalType_()`; в `wbSalesBqEnsureTable_` обе проверки типов (колонки + `_sale_date`) сравнивают канонические формы, текст ошибки показывает исходный тип и canonical. Self-test `wbSalesBqTypeAliasSelfTest()` (без BQ). Таблица была создана пустой, данные НЕ писались, sink fail-closed выключился — повторный `wbSalesBqInitC0()` теперь принимает существующую таблицу, создаёт вью и завершает C0. View SQL / loader / поведение sink не менялись.

Изменения в `WbSalesReturnsLoader` (аддитивно, под флагом):
- константа `SALES_RAW_HEADERS_` (40 колонок, каноническая для BQ; `_sale_date` служебная, добавляется в append) и `WB_SALES_API_ROWS_CAP_=80000`.
- `normalizeSalesApiRows_`: добавлены `g_number, income_id, warehouse_type, payment_sale_amount, price_with_disc, is_supply, is_realization, raw_json`; **раздельные** `region_name`(regionName) и `oblast_okrug_name`(oblastOkrugName) — не смешивать (урок Orders); `operation_type` → `SALE`/`RETURN`; `processed_status='MISSING_EVENT_KEY'` при пустом `saleID`. Листовые поля `order_dt/quantity/is_duplicate` сохранены (в BQ-схеме отсутствуют, при sink игнорируются). `orderType` не добавлен (в контракте нет).
- **`noWindow` (критично, найдено на ревью):** `normalizeSalesApiRows_` получил параметр `opts.noWindow`. Sales API — change-feed по `lastChangeDate`, поэтому поздно изменённая старая продажа приходит с `sale_dt < dateFrom` (probe: `date` 2026-03-30 при `dateFrom` 2026-04-13). При sink ON нормализация вызывается с `{noWindow:true}` — локальный фильтр `sale_dt ∈ [from,to]` снят, весь change-feed сохраняется; при sink OFF (legacy sheet) прежнее окно по `sale_dt` сохранено. Контрольные суммы при sink считаются тем же `noWindow:true` (`aggregateSalesRowArray_(...,{noWindow:true})`), чтобы поздние изменения попадали в диагностику. Будущий watermark (D2c) — тоже всегда noWindow.
- Mock-проверка `wbSalesNoWindowSelfTest()` (без API/листов/BQ): строка `sale_dt=2026-03-30`, `lastChangeDate=2026-04-13T07:03:54`, `dateFrom=2026-04-13` → sink ON нормализуется и в sums (1/1), sink OFF отбрасывается (0).
- `fetchSalesApiData_` переписан: **пагинация снята**, один fail-closed запрос (rate limit Sales API 1 req/min; при объёме EVETIS весь диапазон в одном ответе). HTTP≠200/битый JSON/не-массив → `ERROR`; `arr.length>=80000` → `PARTIAL` (граница обрезана, ничего не пишем); пустой `[]` → `ok` с 0 строк.
- **rate limit (найдено на ревью):** `salesHttpGet_` теперь = **ровно один `UrlFetchApp.fetch` без retry** (`wbFetchWithRetry_` убран — при 1 req/min повтор через 20 сек снова словил бы 429 и жёг лимит выполнения). Удалены неиспользуемые константы `WB_SALES_API_MAX_PAGES_/PAGE_PAUSE_/RETRY_429_/RETRY_PAUSE_MS_` (осталась `ROLLING_DAYS_` для rolling-14). 429/5xx → `ERROR`, повтор — вручную оператором ≥65 сек. Комментарии секции FETCH обновлены (не «пагинация/429-backoff»).
- ядро `importWbSalesReturnsFromApiInternal_`: при `!ok` статус `PARTIAL`/`ERROR` (fail-closed, строки не пишутся); контрольные суммы при sink — из памяти (`aggregateSalesRowArray_`).
- sink-ветки: `getRawSalesSheet_` (заглушка `_bqSink`, `wbSalesBqEnsureTable_`), `buildSalesRawHeaderMap_` (hMap из `SALES_RAW_HEADERS_`), `clearSalesOwnPeriod_` (no-op при sink — append-only), `appendSalesRows_` (массивы→объекты→`wbSalesBqAppendRows_`).

Не включено (следующие фазы): **D2b** — cutover `DashboardWb`/`Cleanwbdaily` на `V_WB_SALES_RETURNS`; **D2c** — `WB_SALES_LAST_CHANGE_WATERMARK`, инкремент (граница `sale_id+row_hash`), hourly-триггер. Флаг `WB_SALES_BQ_SINK` включать вручную только на приёмке.

### 2026-07-12 — Фаза D1.2: watermark-инкремент заказов (код, до acceptance-прогонов)

Отдельная операционная семантика поверх period/backfill. Триггер НЕ создаётся, `runWbDailyRefresh` не трогается. `node --check` обоих файлов — ОК.

Новое (`WbOrdersLoader`):
- `WB_ORDERS_LAST_CHANGE_WATERMARK` (Script Property) — полное `lastChangeDate` в формате WB (RFC3339 с `T`).
- `wbOrdersIncrementalBootstrap()` — берёт `MAX(last_change_date)` из RAW (`source_api='WB_API_ORDERS'`), строго проверяет формат, восстанавливает `T` вместо первого пробела, записывает свойство **только если его нет** (существующее молча не перезаписывает; контрольное значение не хардкодится).
- `runWbOrdersIncremental()` (+ ядро `wbOrdersIncrementalCore_`) — под `LockService.getScriptLock()` на весь цикл; требует `WB_ORDERS_BQ_SINK=1` и валидный watermark; запрос по точному watermark `flag=0`; **без фильтра `order_dt`**; строгая валидация `srid`(trim)/даты/`lastChangeDate` каждой строки И `cursor_end` ДО любой ветки; at-least-once — watermark двигается ТОЛЬКО после полного успешного append; при `PARTIAL`/`ERROR` ничего не пишет и watermark не двигает; overlap арифметически не уменьшается.
- **Безопасная граница watermark (защита от потери строк при секундной точности):** no-change определяется НЕ только по timestamp. Помимо строк `lastChangeDate > watermark`, дописываются граничные строки (`== watermark`), пары `srid+row_hash` которых ещё нет в RAW (helper `wbOrdersBqBoundaryKeys_`). Итог: новая `srid` на уже зафиксированной временной точке НЕ теряется; watermark продвигается только при наличии строк строго новее (`candidate > watermark_before`); при только-граничных новых строках — они пишутся, watermark остаётся (боевого размножения дублей нет, т.к. на след. прогоне пары уже в RAW). `candidate < watermark_before` или невалидный `candidate` → `ERROR`.
- `wbOrdersIncrementalStatus()` — текущий watermark, состояние sink, `RAW MAX(last_change_date)`, `V_WB_ORDERS COUNT` (`wbOrdersBqViewCount_`).
- `ordersValidWatermark_` — строгий формат `YYYY-MM-DDTHH:mm:ss[.fraction]` (дата без времени и пробел отклоняются) **+ календарная корректность** (год 0001–9999, месяц 1–12, день с учётом високосного года, часы 0–23, мин/сек 0–59): отсекает синтаксически похожий мусор вроде `2026-99-99T99:99:99` и `0000-01-01T00:00:00`. Применяется к watermark_before, cursor_end, bootstrap и `lastChangeDate` каждой строки.
- **Инвариант пустого `cursor_end`:** пустой кандидат допустим ТОЛЬКО при настоящем пустом ответе (`data.length===0` и `api_rows_received===0`) → `OK_NO_CHANGES`; непустой пакет без `cursor_end` → `ERROR` (иначе строки писались бы, а watermark стоял → бесконечный повтор диапазона).
- **Диагностика `watermark_after`:** сразу после чтения/валидации watermark ставится `watermark_after = watermark_before`, поэтому при `PARTIAL`/`ERROR` после чтения лог показывает `before == after` (checkpoint не сдвинут), а не пустое `after`; при успешном продвижении перезаписывается на `candidate`.
- `wbOrdersIncrementalBootstrap` — под ScriptLock, требует `WB_ORDERS_BQ_SINK=1`; существующий watermark строго валидируется (валидный → `OK_EXISTS`, битый → `ERROR`, авто-перезаписи нет); записываемое значение проверяется строгим форматом.
- `api_rows_received` = реальная сумма строк ответа API (`fetchOrdersApiData_` считает до дедупа по srid), а не `data.length` после схлопывания. `load_id` (`ORD_INC_yyyyMMdd_HHmmss`) **сквозной** — создаётся в самом начале `runWbOrdersIncremental` (до lock/sink/watermark/API), присутствует в result при всех статусах (`OK`/`OK_NO_CHANGES`/`PARTIAL`/`ERROR`); в лог пишется `r.load_id` без fallback-константы.
- Статусы: `OK` / `OK_NO_CHANGES` / `PARTIAL` / `ERROR`. Поля результата: status, mode, load_id, watermark_before, watermark_candidate, watermark_after, pages_fetched, api_rows_received, rows_appended, unique_srid, started_at, finished_at, duration_ms, error_message.
- В меню «📦 Заказы WB» +3 пункта (bootstrap / инкремент / статус) для ручной приёмки. Rolling-14 по-прежнему помечен ⚠️ и на триггер не ставится.

Изменено аддитивно (поведение period/backfill НЕ меняется):
- `fetchOrdersApiData_` возвращает доп. поля `cursor_start`, `cursor_end` (точное `lastChangeDate` последней строки, формат WB), `pages`, `partial`, `reached_end`, `api_rows_received` (реальная сумма строк ответа до дедупа). HTTP/JSON-ошибка после ≥1 успешной страницы теперь классифицируется `PARTIAL` (на 1-й странице — `ERROR`).
- **Признак завершения при неподвижной границе исправлен (критично для инкремента):** при включительном `>=` WB всегда отдаёт граничную строку с тем же `lastChangeDate`, поэтому `nextCursor === cursor` — норма, а не обрыв. Решение теперь по лимиту строк, НЕ по `progress`: ниже лимита → штатный дренаж (`reachedEnd`, `ok:true`); на лимите → `PARTIAL` (граница могла обрезаться); непустой ответ без `cursor_end` → `PARTIAL`. Раньше `progress>0` на границе (в начале прогона `byKey` пуст → все граничные srid локально «новые») ошибочно давал `PARTIAL`, из-за чего второй прогон не доходил до `OK_NO_CHANGES`, а новая srid на граничной секунде — до `wbOrdersBqBoundaryKeys_`. Проверено через сам `fetchOrdersApiData_`: одна граничная строка / A+B на секунде → `ok:true`; хвост без `lastChangeDate` и 80000 строк на границе → `PARTIAL`.
- `normalizeOrdersApiRows_` получил параметр `opts.noWindow` (пропуск фильтра `order_dt` для инкремента); строка без даты заказа пропускается.
- `IMPORT_LOG_ORDERS_HEADERS_` 14 → 19: аддитивно `mode, watermark_before, watermark_after, pages_fetched, api_rows_received`. Первые 14 колонок не переставлены; `ensureImportLogOrdersSheet_` дописывает новые колонки существующему листу, исторические строки не трогает.

Новое (`WbOrdersBigQuery.gs`): `wbOrdersBqMaxLastChangeDate_()` — `MAX(last_change_date)` из RAW для bootstrap; `wbOrdersBqBoundaryKeys_(lastChangeStorage)` — DISTINCT `srid,row_hash` на границе для безопасной обработки граничных строк; `wbOrdersBqViewCount_()` — COUNT из `V_WB_ORDERS` для статуса.

Ограничение (документируется): fetch хранит только последнее наблюдённое состояние `srid` за прогон — D1.2 даёт *latest observed state per srid*, полный event log — это D1.1 (`raw_json`, отказ от fetch-схлопывания). Переход `is_cancel=false→true` в рабочей RAW искусственно НЕ провоцируем: до триггера — изолированный SQL/CTE-тест без записи, после запуска — постконтроль на естественной отмене.

### 2026-07-12 — Фаза D1: прогон C0 → C1 → backfill выполнен и проверен в облаке (migration/backfill ПРИНЯТ)

Документальная фиксация фактического прогона (код не менялся). Проверки — через BigQuery-коннектор в `project-fa311fc0-4d87-4781-986.wb_raw`.

**C0 `wbOrdersBqInit()` — успешно.** Создана `RAW_WB_ORDERS` (партиция `_order_date`, кластер `wb_nm_id, srid`), создана и подтверждена вью `V_WB_ORDERS`. Начальные счётчики 0/0.

**C1 за 2026-07-10 — успешно.** Импортировано 21 строка, 21 уникальный `srid`, 12 `nmId`, 0 отмен, 1 `nmId` не найден в SKU_MASTER. В облаке: RAW=21, VIEW=21; `last_change_date` заполнен и парсится в TIMESTAMP (диапазон 2026-07-10…2026-07-11 — изменение может приходить позже `order_dt`); `_order_date` у всех = 2026-07-10.

**Повторный C1 за тот же день — эмпирическое подтверждение ключа.** RAW=42, VIEW осталось 21, все 21 `srid` встретились ровно по два раза (2 `load_id`). Это подтверждает append-only модель и ключ дедупликации `srid` (RAW растёт, VIEW стабильна). ⚠️ Выбор нового состояния при переходе `is_cancel=false → true` пока НЕ наблюдался (в тестовых прогонах состояние не менялось) — проверяется на watermark-инкременте (D1.2).

**Backfill `importWbOrdersFromApi('2026-04-13','2026-07-12')` — успешно.** Период = **91 календарный день включительно**. Импортировано **3500 уникальных строк по `srid`** (товарные строки / единицы заказа; это НЕ 3500 уникальных покупательских заказов — связка по `gNumber` не проверялась). Одна страница ответа (3552 получено, слив по пустому массиву `[]`). В облаке: RAW=3542 (с учётом двух тестовых прогонов, 3 `load_id`); `V_WB_ORDERS`=3500 = число уникальных `srid`; отмен в VIEW=285; покрытие **91/91 день без пропусков**; пустых `srid`=0; непарсимых `last_change_date`=0; уникальных `nmId`=22. Флаг `WB_ORDERS_BQ_SINK` остаётся включён.

**Статус.** D1 принята как **миграция и историческая загрузка**. Операционный ежедневный инкремент ещё НЕ реализован; `importWbOrdersFromApiRolling14Days` на триггер ставить нельзя (фильтрует по `order_dt` → теряет позднюю отмену старого заказа). Следующий технический этап — **D1.2 watermark-инкремент**. Отдельным бэклогом остаётся **D1.1** (`raw_json` + полная схема, `regionName` вместо `oblastOkrugName`, мск-таймзона для аналитики).

### 2026-07-12 — Фаза D1: fail-closed на ошибке ответа WB + PARTIAL в лог (3-й аудит)

3-й аудит подтвердил корректность контракта WB и поймал fail-open блокер целостности.

Блокер (`fetchOrdersApiData_`, `WbOrdersLoader`):
- `JSON.parse` в `catch` возвращал `arr = []`, а пустой массив = штатный конец → повреждённый/не-массивный ответ WB (битый JSON, HTML-ошибка с HTTP 200, объект вместо массива, обрезанный ответ) принимался за **полную** выгрузку с `ok:true`. Исправлено: ошибка разбора → `{ok:false, partial: pages>0}`; ответ не `Array` → `{ok:false}`. Штатным концом остаётся ТОЛЬКО настоящий пустой массив `[]`.

PARTIAL в результат/лог (`importWbOrdersFromApiInternal_`):
- при `!fetched.ok` статус теперь `PARTIAL` (упор в лимит/курсор/битый JSON) vs `ERROR` (жёсткая), в существующей колонке `status` лога `IMPORT_LOG_ORDERS`; в `error_message` добавлено число полученных страниц; в result — поля `partial`, `pages_fetched`. При любом `!ok` строки НЕ записываются (fail-closed). Теперь фраза «увидишь PARTIAL» соответствует интерфейсу.

Синхронизированы 3 устаревших комментария (backfill «окнами» → одним проходом; «lastChangeDate не сохраняется» → сохраняется; «партиция не проверяется» → строгая проверка). `node --check` обоих файлов — ОК.

Отложено в **D1.1** (после успешного C1, отдельным решением — не раздувать патч): RAW не полностью «сырой» — нет `raw_json` (страховка от расширения схемы, разбор спорных строк без повторного запроса), `totalPrice`, `finishedPrice`, `discountPercent`, `spp`, `warehouseType`, `incomeID`, `isSupply`, `isRealization`; `region_name` сейчас заполнен `oblastOkrugName` (федеральный округ), тогда как WB отдаёт отдельно `regionName` (регион) — для витрины продаж по регионам семантически неверно; `last_change_date` хранится как мск-время (UTC+3) без зоны, `SAFE_CAST(... AS TIMESTAMP)` в BQ трактует как UTC — для дедупа неважно (сдвиг одинаков), но для аналитики абсолютное время на 3 ч неверно, приводить через мск-зону.

### 2026-07-12 — Фаза D1: исправление семантики пагинации/backfill заказов (2-й аудит)

Повторный внешний аудит подтвердил предыдущий hardening и выявил архитектурный дефект пагинации. По документации WB (`/api/v1/supplier/orders`, `flag=0`): `dateFrom` = `lastChangeDate`, возвращаются записи с `lastChangeDate >= dateFrom` (лимит ответа ~80 000 строк); пагинация — **полным значением `lastChangeDate` последней строки**; конец — **пустой массив `[]`**; параметра `dateTo` у эндпоинта НЕТ.

`fetchOrdersApiData_` (`WbOrdersLoader`):
- курсор пагинации теперь = точное `lastChangeDate` **последней строки** ответа (было: `max()` по странице с `replace('T',' ').substring(0,19)` — обрезка мс/формата расширяла границу и вызывала повтор страниц).
- сравнение версий last-wins по srid — на сырых строках `lastChangeDate` (без обрезки).
- признаки завершения приведены к контракту: пустой массив → конец; страница без новых/обновлённых srid **и ниже лимита строк** → дренаж; курсор не двигается при непустом ответе **или** упор в лимит страниц → `{ok:false, partial:true}` (не «красивый» OK). Введена `WB_ORDERS_API_ROWS_CAP_ = 80000` для отличия дренажа от упора в лимит строк.

Backfill (инструкция, не код): 90 дней делать **одним проходом** `importWbOrdersFromApi('<начало 90д>','<сегодня>')` — окна по `dateTo` объём ответа WB не уменьшают (эндпоинт всё равно отдаёт всё от `dateFrom` до «сейчас»), только многократно перетягивают историю и повышают риск упора в лимит. Client-side фильтр по `order_dt` в `normalizeOrdersApiRows_` оставлен — он и вырезает целевое окно.

Ежедневный инкремент (помечено в коде, реализация — до включения триггера): `importWbOrdersFromApiRolling14Days` фильтрует по `order_dt` — для инкремента НЕВЕРНО (поздняя отмена заказа старше окна не обновит состояние). Заменить на watermark-режим: `dateFrom` = последний обработанный `lastChangeDate`, без фильтра изменений по `order_dt` (дедуп по srid во вью выберет последнее состояние).

Строгая проверка партиции (`WbOrdersBigQuery.gs`): `wbOrdersBqEnsureTable_` у существующей таблицы теперь падает, если она не партиционирована по `_order_date` (patch колонку добавляет, но партицию не создаёт). Синхронизирован устаревший комментарий шапки (`loaded_at DESC` → `last_change_date`). Оба файла прошли `node --check`.

### 2026-07-12 — Фаза D1: hardening заказов по внешнему аудиту (до C0/C1)

Правки перед первым прогоном. Финансы/реклама/CLEAN/UNIT/PNL не затронуты.

Структура колонок (`WbOrdersLoader`):
- `ORDERS_RAW_HEADERS_`: 28 → **29 колонок**, добавлена `last_change_date` **в конец** (время изменения заказа на стороне WB). Легаси-лист RAW_WB_ORDERS физически имеет 28 колонок — append в конец её игнорирует (в sheet-режиме `set('last_change_date')` = no-op); в BQ схема патчится аддитивно. Порядок первых 28 колонок не тронут.

Устойчивый last-wins (`WbOrdersLoader`):
- `fetchOrdersApiData_` переписан: внутрипакетный дедуп по srid был **first-wins** (`if (!seenSrid[key]) all.push(o)`) → теперь **last-wins** (для каждой srid держим версию с максимальной `lastChangeDate`). Раньше поздняя версия одной srid отбрасывалась ещё до RAW — вью не могла бы это исправить.
- нормализация сохраняет `last_change_date` (`T`→пробел для чистого `SAFE_CAST` в BQ).

Полнота backfill (`WbOrdersLoader`):
- упор в лимит `WB_ORDERS_API_MAX_PAGES_` (30) больше не возвращает `ok:true`. Введён флаг `reachedEnd`, отличающий штатное завершение (пустой ответ / нет прогресса / курсор не двигается) от обрыва по лимиту → `{ok:false, partial:true}` с сообщением «сузьте окно». Иначе обрезанный хвост давал «красивый» OK.

Валидация периода (`WbOrdersLoader`):
- добавлена проверка `dateFrom <= dateTo` в ядре и в промпте меню.

Дедуп-вью (`WbOrdersBigQuery.gs`):
- `V_WB_ORDERS`: первичный ключ сортировки `SAFE_CAST(last_change_date AS TIMESTAMP) DESC`, затем `loaded_at DESC, load_id DESC` (tie-break). NULL last_change_date уходит вниз (штатно для BigQuery DESC).
- `wbOrdersBqEnsureTable_`: у существующей таблицы теперь аудируется и служебная `_order_date` (должна быть DATE; если нет — добавляется аддитивно). Раньше проверялись только STRING-колонки из headers → таблица могла существовать без партиционной колонки, append падал бы.

Отложено в бэклог (не блокеры D1): контрольные суммы прогона считаются по пакету, не по дедуп-вью — для acceptance брать `COUNT(*)`/`COUNT(DISTINCT srid)` из `V_WB_ORDERS` в облаке; `IMPORT_LOG_ORDERS` временно в Sheets (диагностика); сопоставление SKU при загрузке через `SKU_MASTER` (стратегически — в SQL-вью, чтобы правка справочника пересчитывала старые RAW). Оба файла прошли `node --check`.

### 2026-07-12 — Фаза D1: Orders → BigQuery (порт)

Аудит `WbOrdersLoader` и порт заказов в BigQuery по образцу рекламы (BigQuery-first).

Новый файл `WbOrdersBigQuery.gs`:
- флаг `WB_ORDERS_BQ_SINK`; `wbOrdersBqEnable()` с preflight (self-test+ensure dataset, fail-closed); `wbOrdersBqDisable()`; `wbOrdersBqInit()` — C0 без WB API (флаг+таблица+вью+счётчики, rollback флага при ошибке).
- `wbOrdersBqEnsureTable_()` — 404-aware + аддитивное расширение схемы; таблица `RAW_WB_ORDERS` = STRING-колонки + `_order_date DATE` (партиция по ДАТЕ ЗАКАЗА, кластер wb_nm_id/srid).
- `wbOrdersBqAppendRows_()` — append-only, `_order_date` из order_dt; batch 2000; allowlist только RAW_WB_ORDERS.
- `wbOrdersBqCreateViews()` — `V_WB_ORDERS`: дедуп по **srid, last-wins** (`ORDER BY loaded_at DESC`), фильтр source_api='WB_API_ORDERS'. Заказы мутируют (заказ→отмена) → нужно последнее состояние; row_hash как ключ НЕ годится (включает is_cancel → задвоение).
- `wbOrdersBqStats()`, `wbOrdersBqAssertViews_()`.

Правки под флагом в `WbOrdersLoader` (тяга/нормализация НЕ тронуты):
- добавлена константа `ORDERS_RAW_HEADERS_` (канонический порядок 28 колонок — при sink листа нет).
- `getRawOrdersSheet_` → при sink возвращает заглушку (`_bqSink`, getName, getLastColumn) и гарантирует BQ-таблицу; `buildOrdersRawHeaderMap_` → из константы; `clearOrdersOwnPeriod_` → no-op (append-only, дедуп во вью); `appendOrdersRows_` → массивы→объекты→BQ; контрольные суммы при sink считаются из памяти (`aggregateOrdersRowArray_`).

Запуск: C0 `wbOrdersBqInit()` (редактор) → C1 `importWbOrdersFromApi` за 1 день (меню «Заказы WB → за период…») → `wbOrdersBqStats()` → проверка в облаке (в т.ч. эмпирическая проверка ключа srid: COUNT vs COUNT DISTINCT srid, srid с меняющимся is_cancel) → backfill 90 дней окнами. ⚠️ Глубина заказов = потолок API ~90 дней. Лист `RAW_WB_ORDERS` остаётся legacy. Финансы/реклама/CLEAN/UNIT/PNL не затронуты.

### 2026-07-11

BigQuery migration — Phase C (реклама), hardening по внешнему аудиту перед запуском.

Что изменено (`apps-script/WbAdsBigQuery.gs`):
- `wbAdvBqEnsureTable_()` переписан: отличает 404 от прочих ошибок (убран «пустой catch»), для существующей таблицы делает аудит схемы и АДДИТИВНОЕ расширение (недостающие колонки → STRING NULLABLE через `Tables.patch`), обрывает запуск при несовместимом типе; выделены `wbAdvBqCreateTable_()` / `wbAdvBqAuditAndExtendSchema_()`;
- `wbAdsBqCreateViews()` теперь создаёт дедуп-вью для ВСЕХ 5 таблиц (было 2): `V_ADV_CAMPAIGNS` (ключ advertId), `V_ADV_CAMPAIGN_STATS` (date+advertId+nmId+appType+source_level, только `processed_status='raw'`), `V_ADV_BOOSTER_STATS` (date+advertId+nmId), `V_ADV_SEARCH_CLUSTERS` (period+связка+norm_query), `V_ADV_COSTS` (updNum, при пустом — составной ключ). Сортировка дедупа: `SAFE_CAST(load_ts AS TIMESTAMP) DESC, run_id DESC`;
- `wbAdsBqEnable()` — preflight (fail-closed): `getBqConfig_` + `bqEnsureDataset_` + `bqSelfTest` ДО установки флага;
- добавлены allowlist `WB_ADS_BQ_TABLES_` и `wbAdsBqAssertTable_()` (вызываются в ensure/append);
- `WB_ADS_BQ_BATCH_` 10000 → 1000 (payload NDJSON с крупным raw_json);
- `wbAdsBqStats()` больше не прячет реальные ошибки под «(нет таблицы)».

Что изменено (`apps-script/WbAdsRawLoader.gs`):
- UI-сообщение оркестратора теперь показывает верное назначение (BigQuery vs листы) при включённом sink;
- `RAW_WB_ADV_SEARCH_CLUSTERS` явно задокументирован как SAMPLE (первые N связок, без ротации) — не источник полноты;
- в `loadWbAdsRawPeriod()` добавлено предупреждение при периоде > 31 дня (backfill — помесячно).

Замечания аудита, ОТЛОЖЕННЫЕ в бэклог (не блокеры первого прогона):
- уникальность `updNum` в `RAW_WB_ADV_COSTS` проверить фактическим запросом (в один день по кампании возможно несколько операций) до утверждения ключа `V_ADV_COSTS`;
- отдельная `V_ADV_CAMPAIGN_NO_STATS` при необходимости (сейчас маркеры остаются только в RAW);
- партиция ingestion-time во вью не используется — для витрин фильтровать по бизнес-дате/`_PARTITIONDATE`.

Порядок запуска (лестница): C0 `wbAdsBqEnable()` (preflight) → C1 один день + `wbAdsBqStats()` + `wbAdsBqCreateViews()` → C2 7 дней → backfill ПОМЕСЯЧНО. Откат: `wbAdsBqDisable()`. Финконтур (RAW_WB_FINANCE/V_WB_FINANCE), CLEAN/UNIT/PNL и daily refresh не затронуты.

Правки по ВТОРОМУ раунду аудита (тот же день):
- **Блокер C0:** `wbAdsBqCreateViews()` падал бы на отсутствующей RAW-таблице (таблицы создаются лениво загрузчиками; при отсутствии кампаний 7/9/11 или связок advertId+nmId часть таблиц не появляется). Добавлен `wbAdsBqEnsureAllTables_()` (гарантирует 5 пустых таблиц из констант заголовков) — вызывается в начале `wbAdsBqCreateViews()`. Добавлен `wbAdsBqInit()` — настоящий C0 БЕЗ WB API (enable+ensure+views+stats).
- **Блокер `V_ADV_CAMPAIGNS`:** свежая строка `count_only` (временный сбой /adverts) вытесняла полноценную `raw` (название/товары/даты). В `makeView()` добавлен параметр `orderPrefix`; для кампаний приоритет `raw` (0) над `count_only` (1) перед сортировкой по load_ts.
- **Backfill по источникам:** инструкция в заголовке и handoff переписана — историю грузить НЕ общим оркестратором (он тратит бюджет на паузы search clusters до fullstats), а по источникам: `loadWbAdsCampaignsRaw()` один раз, `loadWbAdsCostsRaw` помесячно, `loadWbAdsFullstatsRaw` малыми окнами, clusters отдельно.
- Исправлена неверная формулировка: `wbAdsSplitPeriod_()` даёт СМЕЖНЫЕ НЕперекрывающиеся окна; причина PARTIAL — тайм-бюджет и rate-limit, не перекрытие.
- `load_ts` подтверждён: `wbAdsNow_()` → `'yyyy-MM-dd HH:mm:ss'` — валидный timestamp-литерал BigQuery, `SAFE_CAST(load_ts AS TIMESTAMP)` парсит корректно (проверить и на реальных строках в C1).
- Комментарий аудита схемы сужен: проверяются только колонки/типы, партиция и clustering — нет (бэклог).

Утилиты backfill по источникам (для C1/истории, 2026-07-12):
- `WbAdsRawLoader.gs`: `loadWbAdsCostsBackfill90()` (расходы 2026-04-13…2026-07-11, 90 завершённых дней), prompt-обёртки `loadWbAdsCostsRawPeriodPrompt()` и `loadWbAdsFullstatsRawPeriodPrompt()` — вызывают напрямую BQ-совместимые загрузчики (без replace-slice по листам).
- `Menu v2`: в «Реклама WB» добавлены пункты «RAW: только расходы за период…» и «RAW: только fullstats за период…».
- Примечание: пункт «fullstats за месяц» тоже пишет в BQ (внутри зовёт loadWbAdsFullstatsRaw), но перед загрузкой делает лишний deleteRows по старым листам — для backfill предпочтительны новые prompt-пункты «за период».
- Backfill не требует пересоздания вью: V_ADV_* читают живой RAW; `wbAdsBqCreateViews()` нужен только при изменении SQL вью.

Backfill рекламы 90 дней ЗАВЕРШЁН и проверен (2026-07-12):
- fullstats: покрытие 13.04–11.07 полное, 90/90 дней, пропусков нет; расход по SKU ≈ 395 170 ₽, 24 SKU, 7995 строк во `V_ADV_CAMPAIGN_STATS`. Грузили 14-дневными окнами (~200 сек каждое, OK).
- costs (upd): `V_ADV_COSTS` ≈ 425 106 ₽ за период. Сходимость с fullstats: costs на ~7,5% больше (вся реклама с баланса vs привязанное к SKU) — ожидаемо, связывает рекламу с удержанием 4,56 млн из финотчёта.
- campaigns: 426 уникальных. Осталось (низкий приоритет): search clusters (sample) — не грузили.

Результат C1 (первый реальный прогон, проверено в облаке 2026-07-12):
- Все 5 RAW-таблиц и 5 вью созданы; sink работает. Прогон был 7-дневный оркестратором (2 запуска: первый отменён после campaigns+costs, второй прошёл до fullstats PARTIAL).
- Дедуп подтверждён: CAMPAIGNS 852→426 (2×426), CAMPAIGN_STATS фильтр `raw` 256 против 363 no_stats-маркеров.
- `load_ts` (`'yyyy-MM-dd HH:mm:ss'`): `SAFE_CAST(... AS TIMESTAMP)` = 0 NULL — сортировка дедупа корректна.
- **НАЙДЕН и ИСПРАВЛЕН дефект `V_ADV_COSTS`:** `updNum` НЕ уникален (2 различных значения на 272 строки — это номер документа, общий для многих кампаний), ключ на updNum схлопывал 272→2. Заменено на `TO_HEX(SHA256(COALESCE(raw_json,'')))` → 154 строки (честная гранулярность, расход upd ≈ 38 324 ₽ за неделю). Живая вью пересоздана; код синхронизирован. updNum как ключ НЕ использовать.
- **fullstats PARTIAL** (пропущено 26 advertId по тайм-бюджету) — подтверждает: историю грузить ПО ИСТОЧНИКАМ малыми окнами, не оркестратором.

Правки по ТРЕТЬЕМУ раунду аудита (тот же день):
- **C0 fail-closed:** `wbAdsBqInit()` обёрнут в try/catch — при ошибке на любом шаге ПОСЛЕ включения флага (частичное создание таблиц/вью) вызывается `wbAdsBqDisable()` (rollback), чтобы загрузчик не писал в недоинициализированный контур.
- **C0 проверка вью:** добавлен `wbAdsBqAssertViews_()` — подтверждает, что все 5 объектов существуют и являются VIEW (не просто «вызов не бросил исключение»).
- **`V_ADV_COSTS` NULL-safe:** append пишет пустые значения как NULL, а `CONCAT` с NULL даёт NULL → разные операции схлопнулись бы в одну. Резервный ключ заменён на `TO_HEX(SHA256(COALESCE(raw_json,'')))` — сохраняет фактическую гранулярность до подтверждения уникальности `updNum` в C1.

### 2026-07-10

BigQuery migration — Phase A.

Что изменено:
- добавлен новый файл `apps-script/WbBigQuery.gs`;
- добавлен базовый слой доступа к BigQuery: конфиг через Script Properties, создание датасета, создание `RAW_WB_FINANCE`, batch load через `NEWLINE_DELIMITED_JSON`, SQL query helper;
- добавлен `bqSelfTest()` — безопасная проверка доступа через временную таблицу `_selftest`;
- добавлена документация `docs/bigquery_migration_phase_a.md`.

Правила безопасности:
- реальный GCP Project ID не коммитится в репозиторий;
- существующие Google Sheets RAW-листы и WB-загрузчики на этой фазе не меняются;
- `bqSelfTest()` удаляет только временную таблицу `_selftest`;
- `RAW_WB_FINANCE` создаётся только отдельным запуском `bqCreateFinanceTable()`.

Следующий шаг: после успешного `bqSelfTest()` перейти к Phase B — переносу финансового RAW/backfill в BigQuery.

### 2026-06-28

Фаза 0, шаг 1 — миграция финансов на API «Финансы»: файл-разведчик (только чтение).

Что изменено:
- добавлен новый файл `apps-script/WbFinanceApiV1.gs` — диагностический модуль БЕЗ записи в листы;
- `showWbFinanceV1ReportsList()` — POST `sales-reports/list`, выводит в лог фактические периоды всех доступных `reportId` (проверка реальной глубины по кабинету);
- `wbFinanceV1DetailedSample()` — POST `sales-reports/detailed/{reportId}`, выводит реальные имена полей (camelCase) и первую строку для проверки string-сумм;
- предусловие: токен категории «Финансы» в Script Property `WB_TOKEN_FINANCE`;
- база `finance-api.wildberries.ru`, пагинация detailed по `rrdid` (как в старом методе).

Что НЕ менялось:
- `RAW_WB_FINANCE` и любые другие листы (модуль только читает API и пишет в логи);
- старый загрузчик `Wbfinanceloader` (reportDetailByPeriod) — работает до 15.07.2026, выводим из использования позже;
- CLEAN/UNIT/PNL, заказы, продажи, реклама, остатки, хранение.

Подтверждено разведкой (29.06): `list` отдал 189 недельных отчётов с глубиной **2024-09-02 … 2026-06-21**; `detailed/{reportId}` отдаёт строки и за сентябрь 2024. Снята полная camelCase-схема полей.

Добавлен продакшн-загрузчик (в том же файле, переиспользует конвейер `Wbfinanceloader`):
- `wbFinV1AdaptRow_` — новые camelCase-поля → старые имена `FINANCE_API_FIELD_MAP_` + парсинг string-сумм в числа (логистика-деньги = `deliveryService`, не `deliveryAmount`);
- `wbFinV1FetchDetailedAll_` — пагинация detailed по `rrdId`;
- `wbFinV1ImportReport_` — запись одного reportId в `RAW_WB_FINANCE` через `normalizeFinanceApiRows_`, с **replace-slice ПО reportId** (`wbFinV1ClearOwnReport_` удаляет только строки `source_api=WB_API_FIN_V1` И `report_id=reportId`, затем пишет fresh — не по периоду, т.к. на неделю бывает несколько reportId);
- `wbFinanceV1ImportOneReportTest` — импорт ОДНОГО отчёта + контрольные суммы рядом с недельными итогами из `list` (самопроверка маппинга); UNMAPPED-диагностика по `sellerOperName`.

Правила безопасности: токен строго `WB_TOKEN_FINANCE` (без fallback); запись запрещена, если в RAW нет колонок `source_api`/`report_id` (иначе откат невозможен); `row_hash` детерминированный = `WB_API_FIN_V1|reportId|rrdId`; HTTP 204 в пагинации = штатное завершение. Запись additive и обратима (откат — удалить строки с `source_api=WB_API_FIN_V1`). Старый загрузчик не тронут.

Проверка на одном отчёте (757272781, 15–21.06.2026): наши detailed-суммы совпали с недельными итогами `list` точь-в-точь (forPay 4396.37, retail 4849.01, логистика 943.99), UNMAPPED нет.

Добавлен резюмируемый бэкфилл по всем reportId (сен 2024 → сейчас):
- `wbFinV1ListAll_`, `wbFinanceV1Backfill` (бюджет времени 4.5 мин, прогресс в Script Property `WB_FIN_V1_DONE` после каждого отчёта, запуск повторно до «ЗАВЕРШЁН»), `wbFinanceV1BackfillStatus`, `wbFinanceV1BackfillReset`.
- `wbFinV1ImportReport_` принимает готовый `skuIndex` (строится 1 раз на прогон).

Принятая стратегия источника финансов: **единый API**. После бэкфилла — сверка недель перекрытия Excel↔API, затем удаление Excel-строк (`source_api=DRIVE_XLSX_REPORT`) → один источник. Шаг сверки/удаления — отдельно. См. `docs/phase0_finance_migration_tz.md`.

### 2026-06-29 (исправление бэкфилла)

Прогон бэкфилла падал: per-report `deleteRow`-очистка по всему листу на разросшемся RAW давала `IllegalStateException` и «Exceeded maximum execution time» (один большой отчёт не укладывался в 6 мин).

Переписан `wbFinanceV1Backfill`: **один скан листа за прогон** (`wbFinV1BuildSeenRrdSet_`), дедуп строго по безопасному ключу **`reportId|rrdId`**, запись только новых строк, БЕЗ очистки по периоду. Это убирает таймаут, `IllegalStateException` и риск задвоения при повторных/прерванных запусках. `row_hash` теперь = `WB_API_FIN_V1|reportId|rrdId`. Добавлена read-only диагностика `wbFinanceV1CheckDuplicates` (строк/уник. reportId|rrdId/дубли/суммы). Старый `wbFinV1ImportReport_` (single-report тест с replace-slice по report_id) оставлен.

### 2026-06-24

Рекламный дашборд ADS_WB v1 (по API fullstats).

Что изменено:
- добавлен новый файл `AdsDashboardWb.gs` с функцией `buildAdsDashboardWb()` (зависит от хелперов `DashboardWb.gs`);
- лист `ADS_WB` (был пустой) наполняется: фильтр периода, итог по кабинету, разрез по каждому SKU, разрез по каждой кампании;
- источники только API: `RAW_WB_ADV_CAMPAIGN_STATS` (дедуп `date+advertId+nmId+appType`, last-row-wins, площадки суммируются), `RAW_WB_ADV_CAMPAIGNS` (название/статус/площадки из raw_json), `SKU_MASTER`;
- метрики: расход, показы, клики, CTR, CPC, корзины, заказы, CR, CPO, выручка с рекламы, ДРР; целевой ДРР и флаг «резать/усиливать/ок»;
- на таблице кампаний включён нативный фильтр (отбор по SKU кликом);
- формулы локаль-safe (через `dashArgSep_`);
- перед построением `ADS_WB` очищаются проверки данных по всему листу, чтобы старые validations в дальних скрытых колонках не блокировали запись служебного массива.

Что НЕ менялось:
- RAW-листы;
- CLEAN/UNIT/PNL;
- финансы, заказы, продажи, остатки, хранение;
- существующие загрузчики и DashboardWb.gs.

Контроль (весь период 01.04–23.06.2026): расход ≈ 380к (сверка с кабинетным «История затрат» 380 532).

### 2026-06-24

Новый управленческий дашборд DASHBOARD_WB только по API-данным.

Что изменено:
- добавлен новый файл `DashboardWb.gs` с функцией `buildDashboardWb()`;
- лист `DASHBOARD_WB` (был пустой) наполняется: фильтр периода (выпадающий список), итог по магазину, таблица по SKU, таблица по дням;
- источники строго API: `RAW_WB_ORDERS` (WB_API_ORDERS), `RAW_WB_SALES_RETURNS` (WB_API_SALES), `RAW_WB_ADV_CAMPAIGN_STATS` (fullstats, дедуп date+advertId+nmId), `SKU_MASTER`;
- расход рекламы дедуплицируется по `date+advertId+nmId`, при дубле берётся ПОСЛЕДНЯЯ строка (last row wins) — новые месячные прогоны fullstats лежат ниже старых недельных, поэтому перекрытие июньских прогонов не задваивается;
- служебный массив день×SKU пишется в скрытые колонки того же листа (расчётные данные, не RAW);
- формулы собираются под локаль таблицы: разделитель аргументов (`,`/`;`) определяется пробной формулой `dashArgSep_`, десятичные литералы убраны (`*0.6` → `*60/100`) — иначе в ru-локали все формулы давали `#ERROR!`;
- ключ дедупа рекламы расширен до `date+advertId+nmId+appType`: fullstats отдаёт расход по площадкам (appType 1/32/64) отдельными строками, их надо суммировать — без appType расход занижался (~130к вместо ~471к);
- «Цель ДРР» (число) и «Флаг» пишутся отдельными `setValue`/`setFormula`, а не в общий `setFormulas` с числом — иначе колонки J/K давали `#ERROR!`.

Что НЕ менялось:
- RAW-листы (заказы/продажи/реклама/финансы/остатки/хранение);
- CLEAN_WB_DAILY, UNIT_SKU_DAILY, PNL;
- Excel-реализации;
- меню (точка входа `addDashboardWbMenu` не подключается автоматически).

Контрольные суммы (весь период 01.04–23.06.2026): заказы 3558 шт / 3 098 389 ₽, выкупы 3335 / 1 746 992 ₽, отмены 276, реклама (дедуп) ≈ 470 904 ₽.

### 2026-06-24

Исправлен replace-slice в месячных рекламных RAW-загрузчиках.

Что изменено:
- в `WbAdsClustersJob.gs` replace-slice теперь удаляет строки через `deleteRows`, а не очищает ячейки через `clearContent`;
- в `WbAdsFullstatsMonth.gs` replace-slice также переведён на `deleteRows`;
- RAW-листы больше не получают пустые разрывы внутри данных;
- CSV/gviz-чтение больше не должно обрываться на первой пустой строке;
- данные других периодов и TEST-строки не трогаются.

Что НЕ менялось:
- структура RAW-листов;
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение.

### 2026-06-24

Месячный WB Ads fullstats с идемпотентным пересбором периода.

Что изменено:
- добавлен новый файл `WbAdsFullstatsMonth.gs`;
- добавлены точки входа fullstats за текущий месяц и выбранный период;
- перед сбором выполняется replace-slice по `period_from`/`period_to` для fullstats-строк;
- чистятся только строки `adv/v3/fullstats` и `no_stats` за выбранный период;
- данные других периодов и TEST-строки не трогаются;
- добавлены пункты меню в «🏷️ EVETIS WB → 📊 Реклама WB → 📈 fullstats за месяц».

Что НЕ менялось:
- существующий сборщик `loadWbAdsFullstatsRaw`;
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение;
- структура RAW-листов.

### 2026-06-24

Полная пакетная загрузка WB Ads search clusters за месяц.

Что изменено:
- добавлен новый загрузчик `WbAdsClustersJob.gs`;
- search clusters теперь можно собирать по всем advertId+nmId парам, а не только sample 20;
- добавлен служебный скрытый лист `_ADS_CLUSTERS_JOB` для хранения прогресса;
- добавлен cursor/progress: сбор можно продолжать пачками без упора в 6-минутный лимит Apps Script;
- добавлен replace-slice по `period_from`/`period_to` для `RAW_WB_ADV_SEARCH_CLUSTERS`, чтобы повторный сбор месяца не плодил дубли;
- добавлены пункты меню в «🏷️ EVETIS WB → 📊 Реклама WB → 🧩 Кластеры за месяц».

Что НЕ менялось:
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение;
- структура `RAW_WB_ADV_SEARCH_CLUSTERS`.

### 2026-06-23

Консолидация меню в единое «🏷️ EVETIS WB» (только UI, без бизнес-логики, листов и данных).

Что изменено:
- `onOpen()` (Menu v2) теперь строит ОДНО top-level меню с подменю: 💰 Финансы WB, 📦 Заказы WB, 💳 Продажи WB, 📦 Остатки WB, 📦 Хранение WB, 📊 Реклама WB, 📊 Расчёты, ⚙️ Обслуживание + быстрые пункты «🔧 Полная настройка», «🔄 Обновить WB API (WB Daily)», «🩺 Диагностика», «ℹ️ О системе»;
- убраны вызовы отдельных строителей top-level меню (`addWbDailyRefreshMenu`, `addWbOrdersLoaderMenu`, `addWbSalesReturnsLoaderMenu`, `addUnitSkuDailyMenu`, `addWbOperationalPilotMenu`, `addStorageFolderLoaderMenu`) — их пункты перенесены в подменю. Сами функции оставлены в файлах;
- меню «📦 Хранение WB» теперь доступно как подменю (раньше уезжало в «…» и было не видно);
- исправлено битое подменю «💰 Финансы WB»: прежние пункты ссылались на несуществующие функции (`previewWbFinanceApi`, `loadWbFinanceLastWeek`/`Period`/`FullHistory`/`HistoryChunked`, `auditRawWbFinance`); перевязано на реальные функции (`importWbFinanceFromApiRolling14Days`, `listWbFinanceReportsInDrive`, `buildCleanWbDailyWithFinance`, `buildMonthlyUnitReportWithFinance`, `auditRawWbFinanceColumns`, `verifyRawWbFinanceImport`).

Зачем:
- множество отдельных top-level меню переполняли панель Google Sheets и прятались в «…» (из-за этого кнопка хранения была недоступна);
- часть пунктов «Финансы WB» не работала (ссылки на отсутствующие функции).

Какие листы затронуты:
- никакие. Изменения только в построении меню. Все целевые функции существуют (проверено: 48 пунктов, 0 битых ссылок), синтаксис проверен `node --check`.

Как проверить:
- перезагрузить таблицу → в шапке одно меню «🏷️ EVETIS WB»; отдельных WB Daily/Заказы/Продажи/Хранение/Pilot/UNIT больше нет;
- открыть «📦 Хранение WB → 🔄 Обновить хранение WB» — запускается `updateWbStorageData`;
- кликнуть пункты «💰 Финансы WB» — ошибки «функция не найдена» больше нет.

### 2026-06-22

Техническая стабильность загрузчиков WB API (без изменения бизнес-логики, структуры листов и данных).

Что изменено:
- добавлен единый HTTP-helper `wbFetchWithRetry_` (+ разбор `Retry-After` в `wbRetryAfterMs_`) в `utils.gs`: ретраи на HTTP 429 и 5xx (500/502/503/504), экспоненциальный backoff с верхней границей, уважение заголовка `Retry-After`, ограничение числа повторов, логирование попыток без вывода токена;
- загрузчики переведены на общий helper: заказы (`fetchOrdersApiData_`), продажи/возвраты (`salesHttpGet_`), остатки (`stocksFetch429_` → делегирует helper), реклама (`wbAdsHttp_`), хранение (`Wbstoragefolderloader`: create/status/download). Прежние внешние контракты функций и форматы строк сохранены;
- устранён дубль глобальной функции `importWbStorageFromApiRolling7Days`: диагностическая копия в `Wbstorageapidiag` переименована в приватную `importWbStorageFromApiRolling7DaysDiag_`; боевая реализация в `Wbstoragefolderloader` и пункт меню не изменены;
- исправлен debug-lookup в `utils.gs` (`debugSkuMatching`): `skuIndex.byNmId` → `skuIndex.byNm` в соответствии с контрактом `buildSkuIndex_` (`byNm`/`byBarcode`/`byVendor`). Сам `buildSkuIndex_` не менялся.

Зачем:
- раньше обработка 429/5xx была непоследовательной (у заказов и рекламы ретраев почти не было) — заказы и реклама могли «тихо» недогружаться при лимитах WB;
- дубль имени функции в общем глобальном пространстве Apps Script приводил к тому, что одна реализация перетирала другую;
- debug-функция обращалась к несуществующему ключу индекса и всегда показывала «NOT FOUND».

Какие листы затронуты:
- никакие. Изменения только в коде загрузки/ретраев и отладки. Имена колонок, формат записываемых строк, логика replace-slice и `source_api` не менялись. RAW/CLEAN/UNIT/PNL и справочники не затрагиваются.

Как проверить:
- запустить «📦 Заказы WB → Обновить заказы (rolling 14)»: в логах — строки `[Orders] …` при лимитах; число строк `WB_API_ORDERS` за период не растёт при повторном прогоне (идемпотентность сохранена);
- прогнать загрузчики продаж/остатков/рекламы/хранения — данные пишутся как прежде, при 429/5xx в логах видны паузы и повторы;
- проверить, что пункт меню «🌐 API rolling 7 days» по-прежнему запускает боевую загрузку хранения.

### 2026-06-04

Создана базовая структура проекта EVETIS WB Analytics:
- добавлены проектные инструкции;
- описана архитектура;
- создана папка apps-script;
- создана папка skills;
- зафиксированы базовые правила работы с Claude и Codex.
