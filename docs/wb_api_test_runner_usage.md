# WB API Test Runner — Usage

> Инструкция по безопасному тестовому контуру WB API для проекта **EVETIS WB Analytics**.
> Выполняет тестовые запросы по `docs/wb_api_test_plan.md` (T1–T11), сохраняет raw JSON и контрольные суммы **только в Drive**.
> Не меняет production-код, не пишет в боевые листы, не создаёт production-загрузчиков, не хранит и не логирует токены.

---

## 0. Что это и чем не является

Это **тестовый runner / test harness**, а не система загрузки. Он:

- выполняет тестовые запросы WB API по кабинету EVETIS;
- сохраняет raw JSON-ответы в отдельную Drive-папку;
- считает контрольные суммы (суммы, строки, `nmId`, unmatched и т. д.);
- фиксирует фактические поля, ключи, статусы;
- проставляет каждому тесту статус `PASSED` / `PARTIAL` / `FAILED` / `BLOCKED` / `TBD`.

Он **не**: не меняет существующие функции Apps Script, не меняет структуру Google Sheets, не пишет в `RAW_WB_*`, `UNIT_SKU_DAILY`, `PNL_TOTAL`, не создаёт новых листов, не делает auto-import, не использует Excel/Drive как источник факта, не ищет COGS в WB API, не признаёт рекламу SKU-фактом без сверки T8/T9.

---

## 1. Файлы контура

| Файл | Назначение |
|---|---|
| `apps-script/WbApiTestConfig.gs` | Константы, флаг `WB_API_TEST_MODE`, реестр тестов, имена properties. |
| `apps-script/WbApiTestUtils.gs` | Guard, чтение токенов, HTTP, task-flow, сохранение в Drive, чек-суммы, summary, read-only `SKU_MASTER`. |
| `apps-script/WbApiTestRunner.gs` | Публичные функции `testWbApi…` (T1–T11) и `testWbApiRunAll`. |
| `docs/wb_api_test_runner_usage.md` | Этот документ. |

Все публичные функции начинаются с `testWbApi…` и явно отделены от production.

---

## 2. Предварительная настройка (Script Properties)

В редакторе Apps Script: **Project Settings → Script Properties**. Задать:

| Property | Категория | Назначение |
|---|---|---|
| `WB_TOKEN_STATISTICS` | Statistics | T2 (legacy finance), T3 (orders), T4 (sales) |
| `WB_TOKEN_FINANCE` | Finance | T1 (новый Finance API) |
| `WB_TOKEN_ANALYTICS` | Analytics | T5, T6 (остатки), T10 (хранение), T11 (приёмка) |
| `WB_TOKEN_PROMOTION` | Promotion | T7, T8, T9 (реклама) |
| `WB_API_TEST_RESULTS_FOLDER_ID` | — | ID Drive-папки `EVETIS_WB_API_TEST_RESULTS` для результатов |

Правила по токенам:

- токены **не хардкодятся**, читаются только из `PropertiesService`;
- в логи пишется только `token category: …` и `token present: yes/no`;
- токены **не попадают** в raw JSON и summary;
- если нужного токена нет — соответствующий тест получает статус `BLOCKED` (не падает всё).

Если нет `WB_API_TEST_RESULTS_FOLDER_ID` или папка недоступна — **все** тесты получают `BLOCKED`, потому что результаты сохранять некуда (по согласованному решению Этапа 2).

Создание Drive-папки: вручную создать папку `EVETIS_WB_API_TEST_RESULTS` на Google Drive, скопировать её ID из URL и положить в property `WB_API_TEST_RESULTS_FOLDER_ID`.

---

## 3. Как запускать

Запуск — **вручную из редактора Apps Script** (Run). Меню в Google Sheets на первом этапе **не добавляется**, `Menu.gs::onOpen` не трогается.

Рекомендуемый сценарий — **по одному тесту**:

| Функция | Тест(ы) |
|---|---|
| `testWbApiRunFinanceNew()` | T1 — новый Finance API |
| `testWbApiRunLegacyFinance()` | T2 — legacy finance (сверка) |
| `testWbApiRunOrders()` | T3 — заказы |
| `testWbApiRunSales()` | T4 — продажи/возвраты |
| `testWbApiRunStocks()` | T5 + T6 — остатки (кандидаты A и B) |
| `testWbApiRunAds()` | T7 + T8 + T9 — реклама |
| `testWbApiRunStorage()` | T10 — хранение |
| `testWbApiRunAcceptance()` | T11 — приёмка |

`testWbApiRunAll()` существует, но это **не основной сценарий**: из-за лимита времени Apps Script (≈6 минут) полный прогон может не успеть завершиться — особенно из-за пауз rate-limit у legacy finance (T2, ~1 запрос/мин) и трёх task-based тестов (T5/T10/T11) с опросами статуса. `testWbApiRunAll()` продолжает работу, если отдельный тест `BLOCKED`/`FAILED` (каждый тест в своём `try/catch`), и в конце собирает summary, но рассчитывать на него как на единственный прогон не стоит.

---

## 4. Что и куда сохраняется

Только в Drive-папку `EVETIS_WB_API_TEST_RESULTS`. Никаких новых листов, никакой записи в боевые листы.

Raw JSON по тесту (во всех именах есть `<timestamp>` — история прогонов сохраняется, файлы не перезаписываются):

```
T1_finance_detailed_2026-05-18_2026-05-24_<timestamp>.json
T2_legacy_finance_2026-05-18_2026-05-24_<timestamp>.json
T3_orders_2026-05-18_2026-05-24_<timestamp>.json
T4_sales_2026-05-18_2026-05-24_<timestamp>.json
T5_warehouse_remains_snapshot_<timestamp>.json
T6_stocks_report_wb_warehouses_snapshot_<timestamp>.json
T7_ads_campaigns_<timestamp>.json
T8_ads_fullstats_2026-05-18_2026-05-24_<timestamp>.json
T9_ads_upd_2026-05-18_2026-05-24_<timestamp>.json
T10_paid_storage_2026-05-18_2026-05-24_<timestamp>.json
T11_acceptance_report_2026-05-18_2026-05-24_<timestamp>.json
```

Каждый файл содержит обёртку без токена: `endpoint`, `method`, `requestParams` (без токена), `httpStatus`, `timestamp`, `rowsCount`, `fields[]`, `firstRows` (первые 3), `checksums`, `errors`, `decision`, `rawResponse`.

Summary (формируется `testWbApiRunAll`, в имени тоже `<timestamp>`):

```
wb_api_test_summary_2026-05-18_2026-05-24_<timestamp>.md
wb_api_test_summary_2026-05-18_2026-05-24_<timestamp>.json
```

---

## 5. Контрольные суммы и решения

Для каждого теста считаются: число строк, уникальные `nmId`, строки без `nmId`, денежные суммы по своим полям (для нового Finance API — парсинг string-полей), unmatched `nmId` относительно `SKU_MASTER` (read-only; `-1`, если лист недоступен).

Кросс-сверки (в summary, если есть оба результата):

- **Реклама T8 / T9:** `T8 fullstats sum` — основной управленческий источник рекламного расхода (P&L, SKU-аналитика, рекламная воронка). `T9 upd updSum` — контрольный источник списаний WB. Коэффициент приведения T8 к T9 **не применяется**, SKU-расходы на `T9/T8` не умножаются. В summary показываются `T8 total spend`, `T9 total updSum`, `delta amount`, `deltaPercent`, `status`: если `deltaPercent <= 5%` (т.е. `|T8 − T9| ≤ 5%` от T8) → `OK`, иначе `WARNING` (ручная сверка с кабинетом WB). Формулировка: реклама для управленческого P&L и SKU-воронки берётся из T8 fullstats; T9 используется как контроль списаний WB; расхождение допустимо из-за временного лага между рекламной активностью и финансовым списанием.
- **Хранение T10 ↔ finance (T1/T2):** `Σ warehousePrice` против `storage`. Канон **не закрепляется** автоматически.
- **Приёмка T11 ↔ finance (T1/T2):** `Σ total` против `acceptance`. Канон **не закрепляется** автоматически.

Статусы решения: `PASSED`, `PARTIAL`, `FAILED`, `BLOCKED`, `TBD`. Candidate B остатков (T6) и новый Finance API (T1)/`adv/v3/fullstats` (T8) имеют неподтверждённые контракты — при отличии схемы тест честно проставляет `PARTIAL`/`TBD`/`FAILED` и сохраняет raw для анализа.

Параметры некоторых запросов:

- **T8 (`adv/v3/fullstats`)** использует query-параметры `beginDate=2026-05-18` и `endDate=2026-05-24` (не `from/to`), и получает только `statsAdvertIds` из T7 (кампании в статусах `7`, `9`, `11`).
- **T3 / T4** дополнительно фильтруют строки по полю `date` в пределах `2026-05-18 00:00:00 — 2026-05-24 23:59:59`; в результат пишутся `rawRowsCount`, `filteredRowsCount`, `filterInfo`.

---

## 6. Summary

`testWbApiRunAll()` формирует summary по результатам текущего прогона. При запуске тестов по одному raw JSON сохраняются отдельно, а summary может быть собран отдельным последующим анализом raw JSON.

---

## 7. Известные ограничения и риски

- **Лимит времени Apps Script (~6 мин):** запускать тесты по одному.
- **Rate limits:** legacy finance ~1 запрос/мин (паузы ~61 c); рекламные эндпоинты требуют разбивки `advertId` пачками до 50.
- **Task-based отчёты (T5/T10/T11):** отчёт хранится ~2 часа; возможны статусы `purged`/`canceled`/таймаут → `PARTIAL`/`FAILED`.
- **T6 (candidate B):** body/schema не подтверждены — вероятный исход `TBD`.
- **T8 (`adv/v3/fullstats`):** реализован методом `GET` с `beginDate/endDate` по плану и получает только `statsAdvertIds` (статусы 7/9/11); если WB требует `POST` с body — это будет видно по `httpStatus`/`errors` в сохранённом результате, метод корректируется отдельным шагом.
- Контур ничего не закрепляет как канон и ничего не пишет в боевые листы — только тестирует и сохраняет.

---

## 8. Будущая опция (НЕ включена на первом этапе)

В дальнейшем можно добавить отдельный пункт меню `testWbApiAddMenu()` (своё меню, без правки `onOpen`). На первом этапе это **не реализуется и не подключается** — запуск только из редактора.

---

*Тестовый контур соответствует политике проекта: production-код не меняется, боевые листы не затрагиваются, raw JSON — только в Drive. Реклама для управленческого P&L и SKU-воронки берётся из T8 fullstats; T9 — контроль списаний WB. COGS в WB API не ищется, Excel/Drive не используется как источник факта.*
