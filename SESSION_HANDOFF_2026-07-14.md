# SESSION HANDOFF — 2026-07-14

Снимок состояния EVETIS WB Analytics на конец сессии 14.07.2026.
Главное за сессию: **закрыт весь контур продаж в BigQuery** — D2b (cutover потребителей) и D2c (watermark + hourly-триггер) приняты и в проде; зафиксирована **смена роли Google Sheets** (хранилище → пульт); проведён аудит ёмкости книги (упёрлась в лимит 10 млн ячеек). Следующая задача — **Sales Night Reconciliation**.

---

## 1. Управляющая архитектура (актуальная, решение 13–14.07)

**BigQuery-first, а Google Sheets → «пульт», не хранилище.** Конвейер: `WB API → Apps Script (только загрузчики/оркестрация) → BQ RAW (неизменяемая история) → BQ VIEW (дедуп/последнее состояние) → BQ REFERENCE (справочники из Sheets) → BQ MART (готовые показатели) → Web Dashboard / Telegram / AI`.

Google Sheets остаётся ТОЛЬКО как пульт: `SKU_MASTER`, `COST_HISTORY`, `BUNDLES`, настройки, API-конфиг, журналы загрузок/ошибок, кнопки ручного запуска. **RAW/UNIT/PNL/DASHBOARD_WB в Sheets — уходящий слой, не восстанавливаем.** Расчёты (UNIT-эквивалент день×SKU, PNL, ABC) станут SQL-витринами MART; дашборд строим сразу на BQ (D5). Детали и обязательные условия перехода — память [[evetis-roadmap-vision]].

**Инженерные условия перехода (обязательные):** (1) справочники текут Sheets→BQ (REF-sync), иначе MART не посчитает cogs/маржу/PNL; (2) PNL/UNIT мигрируют витринами с parity против последних верных листовых чисел (CLAUDE.md: PNL без контрольных сумм не трогать); (3) `RAW_WB_FINANCE` и прочие RAW удаляем из Sheets только после аудита зависимостей.

## 2. Продажи/возвраты — ПОЛНОСТЬЮ В BIGQUERY (D2a→D2b→D2c готово)

### D2a (ingestion) — принято ранее
`RAW_WB_SALES_RETURNS` + `V_WB_SALES_RETURNS` (last-wins по `sale_id`), флаг `WB_SALES_BQ_SINK=ON`, `noWindow`. Ключ `sale_id` доказан probe.

### D2b (cutover потребителей на BQ) — ПРИНЯТО и в проде (PR #54, #55, #57)
- Новый `WbSalesConsumerSource.gs` — единый адаптер чтения. Флаг `WB_SALES_CONSUMER_SOURCE = SHEET | BIGQUERY` (**сейчас `BIGQUERY`**), fail-closed (неизвестное значение → исключение). `readCanonicalSalesRows_({fromDate,toDate,allowEmpty})` нормализует ОБА источника в единый 12-колоночный контракт (типы одинаковы; `wb_nm_id` — строка; `quantity=1`/`is_duplicate=false` синтез при отсутствии колонки, но SHEET читает реальные, если есть). Обязательный partition-filter `_sale_date >= '2024-09-01'` (постоянная граница, не скользящая), дата — валидированный литерал (bqQuery_ не расширяли). Пустой BQ при `allowEmpty=false` → исключение. `DashboardWb.gs`/`Cleanwbdaily` читают продажи только через адаптер.
- **Parity доказан:** сверка честной базы `source_api=WB_API_SALES AND sale_dt >= 2026-04-13` → SHEET **2813 = BQ 2813**, keys=933, `quantity/money/missing = 0`.
- **Урок по данным (важно для будущего):** лист — смесь. Реального дубля (`is_duplicate`) нет (0). Ранние продажи 01–12.04 (522 API-строки) — вне backfill и уже вне 90-дн. retention (невосстановимы). `EVT-HC-BODY-300`/`nmId=252442341` — тестовые строки `source_api=TEST`, пустой `sale_id`, `operation_type=Продажа`, `quantity=2` — НЕ события API. Отсюда: **BQ покрывает продажи сплошняком с 2026-04-13** (03-30 — единичный `noWindow`-артефакт); историческая денежная глубина — в Finance (с 05.09.2024), НЕ в Sales API. Legacy-import ранних продаж решили НЕ делать.

### D2c (watermark + hourly-триггер) — ПРИНЯТО и в проде (ветка phase-d2c-sales-watermark)
- Новый `WbSalesIncremental.gs`: `WB_SALES_LAST_CHANGE_WATERMARK` (Script Property); `wbSalesIncrementalBootstrap()` (из `MAX(last_change_date)` RAW, не перезаписывает, пустой RAW→ERROR); `runWbSalesIncremental()`+ядро под одним `ScriptLock` (`tryLock`→`finally`); `wbSalesIncrementalStatus()`; идемпотентный `wbSalesIncrementalInstallHourlyTrigger()`/`...RemoveTrigger()`.
- Один fail-closed запрос `dateFrom=watermark` (1 req/min), `noWindow`. Порядок безопасности: **валидация ВСЕХ сырых API-строк ДО нормализации** (`saleID/date/lastChangeDate`, любая плохая → ERROR с номером) + страховка `rows.length===data.length`. Дедуп/граница по **STATE-ключу `sale_id | md5(raw_json)`** (row_hash не годится — не меняется при изменении цены/склада/скидки); MD5 согласован Apps Script `salesMd5_` (lowercase) ↔ BQ `TO_HEX(MD5(raw_json))`. Внутрипакетный **last-wins по `sale_id`** (max lastChangeDate, tie-break state-hash). watermark двигается только после append и только если `candidate > watermark_before`.
- Статусы: `OK` / `OK_NO_CHANGES` / `SKIPPED_LOCKED` / `ERROR` (cap 80000 → ERROR, PARTIAL не вводим).
- Контракт at-least-once (точная формулировка): на границе (`==watermark`) state-key даёт физическую идемпотентность; строки строго новее watermark после post-append сбоя на повторе могут быть append-нуты снова (в append-only RAW дубли допустимы) — каноническая идемпотентность во VIEW. Range-wide дедуп в hourly сознательно НЕ делаем.
- `WbSalesReturnsBigQuery.gs`: `wbSalesBqMaxLastChange_()`, `wbSalesBqBoundaryStateKeys_(watermark)` (с fail-safe фильтрами `sale_id`/`raw_json`); во вью добавлен финальный tie-break `TO_HEX(MD5(raw_json)) DESC` (C2) — нужно пересоздать `wbSalesBqCreateViews()` при накатке. `WbSalesReturnsLoader`: `IMPORT_LOG_SALES_HEADERS_` расширен (`watermark_before/after`, `api_rows_received`, `rows_after_boundary_dedup`, `rows_written`, `duration_ms`).

**ФАКТИЧЕСКИЙ ПРОГОН (14.07, проверен в облаке):**
- bootstrap → watermark `2026-07-12T20:14:45` (из RAW).
- прогон 1: `OK`, api_rows=26, written=26, watermark → `2026-07-14T14:14:10`.
- прогон 2 сразу: `OK_NO_CHANGES`.
- Облако: RAW 3395→**3421** (+26 `SALE_INC_%`), VIEW 3319→**3345** (26 новых `sale_id`), RAW max lcd = 2026-07-14T14:14:10.
- Триггер `runWbSalesIncremental` установлен (everyHours 1); повторная установка дубля не создаёт.

## 3. Остальные потоки (статус)

- **Заказы (D1/D1.2):** в BQ, watermark + hourly-триггер, боевой. Шаблон, по которому сделан D2c.
- **Финансы:** `RAW_WB_FINANCE`/`V_WB_FINANCE`, полная история 05.09.2024–12.07.2026 (~201k строк), source_api `WB_API_FIN_V1`. Осталось: **daily incremental loader** + скользящая пересверка последних отчётов + контроль новых reportId.
- **Реклама (Фаза C):** 90 дней в BQ, вью `V_ADV_*`. Осталось: регулярная загрузка (каждые ~3ч) + ночная пересверка 7 дней + контроль полноты кампаний.
- **Остатки/склады:** НЕ сделано (следующий крупный ingestion после Finance) — snapshot остатков, история по складам, дни запаса, риск out-of-stock.

## 4. Ёмкость книги Google Sheets — КРИТИЧНО

Аудит `wbWorkbookCellAudit()` (ветка `diag/workbook-cell-audit`, read-only): книга **на 100%** — сетка **9 997 687 / 10 000 000**, свободно 2 313. Из-за этого `buildDashboardWb()` упал (`dashRender_:247`), и UNIT/PNL тоже под угрозой.
- Крупнейший потребитель: `RAW_WB_FINANCE` = **6 676 946 (67%)**, заполнен целиком → структурно уходит только в Фазе E после аудита зависимостей (кто читает лист vs BQ).
- Пустой резерв по книге = **1 628 512 ячеек** (убирается без потери данных).
- Кандидат на удаление: лист-бэкап `BACKUP_PR12_RAW_WB_FINANCE_20260617_163216` (564 916).
- Инструмент: guarded `wbWorkbookTrimEmptyReserve_({dryRun:false})` (срезает строки/столбцы за пределами использованного диапазона + запас; по умолчанию dry-run). Немедленная гигиена: удалить бэкап + trim → книга ~78%.
- ⚠️ Ветка `diag/workbook-cell-audit` НЕ смерджена; её файлы (`WbWorkbookAudit.gs` + запись в CHANGELOG) лежали в рабочем дереве незакоммиченными — коммитить в свою ветку отдельно от D2c.

## 5. СЛЕДУЮЩАЯ ЗАДАЧА — Sales Night Reconciliation (план согласован, код не начат)

Ночная пересверка продаж (закрывает контур: hourly ingestion + ночная пересверка → затем health monitor). **Зачем:** eventual consistency Sales API — строка может всплыть позже с `lastChangeDate < watermark`, hourly её не увидит; плюс пропуски от сбоев/429. Пересверка — правильное место для **range-wide дедупа** (в hourly его нет намеренно).

**Согласованный дизайн:**
- `runWbSalesNightReconcile()` + ядро + ночной триггер `everyDays(1).atHour(4)` (МСК), идемпотентная установка `wbSalesReconcileInstallNightlyTrigger()`.
- Конфиг `WB_SALES_RECONCILE_DAYS` (Script Property, дефолт **7**). `dateFrom` = полночь МСК `(сегодня − N дней)`.
- Один fail-closed запрос, `noWindow`, та же валидация сырых строк + `rows.length===data.length`.
- Новый BQ-хелпер `wbSalesBqStateKeysSince_(fromLcd)` → набор `sale_id|TO_HEX(MD5(raw_json))` где `last_change_date >= fromLcd` (fail-safe фильтры). Внутрипакетно дедуп по `sale_id|state` (НЕ last-wins — латаем все отсутствующие состояния). Append только `sale_id|state`, которых нет в RAW → `gaps_filled`.
- **watermark НЕ трогаем** (им владеет hourly). Общий `ScriptLock` с hourly (`SKIPPED_LOCKED` при занятости).
- Статусы: `OK` / `OK_NO_GAPS` / `SKIPPED_LOCKED` / `ERROR`. Лог в `IMPORT_LOG_SALES_RETURNS` (`load_id` префикс `SALE_RECON_`, `rows_written=gaps_filled`, окно в `period_from`).
- Файлы: `WbSalesIncremental.gs` (или новый `WbSalesReconcile.gs`) + `wbSalesBqStateKeysSince_` в `WbSalesReturnsBigQuery.gs` + CHANGELOG. RAW-схему/вью/adapter/Finance/Ads/PNL не трогаем.

**Решённые параметры:** окно 7 дней; час 04:00 МСК; watermark пересверка не двигает.

**Acceptance:** первый прогон за 7д → `OK_NO_GAPS`/малый `gaps_filled`, watermark неизменен, VIEW = distinct `sale_id`; повтор сразу → `OK_NO_GAPS`, RAW не растёт; fail-closed (битый ответ/sink OFF → ERROR, RAW неизменен). Триггер — после ручной приёмки.

## 6. Дорожная карта после Night Reconciliation

Этап 1 (автоматизация готовых ingestion): Sales hourly ✅ → **Sales Night Reconcile** → Finance daily incremental → Ads-триггер (3ч) → единый health/status monitor → 48–72ч без рук. Этап 2: остатки/склады. Этап 3: справочники Sheets→BQ (REF_*). Этап 4: MART (`MART_SKU_DAILY`, `MART_PNL_SKU_DAILY`, `MART_ADS_SKU_DAILY`, `MART_STOCKS_SKU_DAILY`, затем ABC/ДРР/поисковые запросы). Этап 5: Web Dashboard (обзор/SKU/реклама/остатки/PNL + сценарный калькулятор). Этап 6: Telegram/AI (дайджест/аномалии/рекомендации). Целевой `MART_SKU_DAILY` и состав показателей — в этом хендоффе п.5 предыдущего обсуждения (продажи/реклама/финансы/себестоимость+прибыль/остатки).

## 7. Правила процесса (неизменны)

Не усложнять; минимально/обратимо/проверяемо. Перед кодом: что/зачем/риск/контрольные цифры → потом код. Ключ дедупа доказываем эмпирически. `node --check` + `git diff --check`. Ветку создаёт ассистент, commit/push — владелец через GitHub Desktop; код в Apps Script и триггеры — вручную после PR/merge и ручной приёмки. Всё пользовательское — по-русски. Аудитор плана — ChatGPT (владелец прогоняет план через него перед реализацией).
