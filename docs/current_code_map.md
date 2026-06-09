# Current Code Map — EVETIS WB Analytics

> Карта текущего кода проекта **EVETIS WB Analytics** после переноса Apps Script в GitHub.  
> Документ фиксирует текущее состояние кода и не является техническим заданием на рефакторинг.  
> Никаких правок, новых функций или новой архитектуры в этом документе не предлагается.

Дата фиксации: 2026-06-09  
Источник: первичный аудит Claude после переноса Google Apps Script в GitHub  
Версия системы из `Config`: `SYSTEM_VERSION = '1.0.3-mvp'`  
Статус: первичная карта текущего кода

---

## 1. Краткое описание текущего состояния

Проект — Google Sheets + Apps Script для управленческой аналитики бренда EVETIS на Wildberries (FBO).

Архитектура заявлена как 4–5 слоёв: RAW → CLEAN → справочники → PNL/юнит → дашборд (см. `ARCHITECTURE.md`, `PROJECT_RULES.md`). Фактически в коде сейчас реально работают: загрузка финансов (API + Drive), импорт хранения, сборка `CLEAN_WB_DAILY`, сборка юнит-экономики `ЮНИТ_MM_YYYY`, справочники SKU/себестоимости и набор аудитов.

Состояние по факту:

| Блок | Состояние в коде |
|---|---|
| Импорт финансов WB (API + XLSX с Drive) | Реализован; требует контрольной сверки на выбранном периоде |
| Импорт хранения WB (API + Drive folder) | Реализован; требует контрольной сверки перед использованием как канонического источника |
| CLEAN-слой (`CLEAN_WB_DAILY`) | Реализован; есть риск рассинхрона схемы 15 колонок vs расширенная схема |
| Юнит-экономика по месяцам | Реализованы **три** параллельные версии (см. §10) |
| Справочники SKU / COST_HISTORY / BUNDLES | Реализованы, есть аудиты |
| Заказы/продажи/реклама/остатки через WB API | В меню — заглушки `notImplementedYet_` |
| PNL_TOTAL / DASHBOARD как отдельные листы | В меню — заглушки, отдельного билдера нет |

Ключевой момент: «юнит-экономика» в коде закрывает роль PNL по кабинету и по SKU. Отдельного модуля `PNL_TOTAL`/`PNL_SKU` как самостоятельного билдера нет — расчёты прибыли живут внутри юнит-отчётов.

---

## 2. Карта основных модулей

Файлы папки `apps-script/` и их роль (по факту кода):

| Файл | Роль |
|---|---|
| `Code.gs` (v1.1.0) | Точки входа сборки книги: `setupWorkbook()`, `createAllSheets_()`, README, `systemHealthCheck`, таймаут-гард |
| `Config` | Центральная конфигурация: `SYSTEM_VERSION`, `COLORS`, `SHEET_NAMES`, `SHEETS_SCHEMA`, `SHEET_ORDER`, `HIDDEN_SHEETS` |
| `Settings` | `createSettingsSheet_()` — лист «Настройки» (key-value: lookback, retry, исключения и пр.) |
| `Formatting` | `applyAllFormatting_`, `applyAllValidations_`, `applyAllConditionalFormatting_`, `protectAllFormulaColumns_`, `reorderSheets_`, `hideRawSheets_` |
| `Menu v2` (v2.0) | `onOpen()` — главное меню «🏷️ EVETIS WB», обёртки команд, `notImplementedYet_` |
| `Wbfinanceloader` | Загрузка финансов через WB Statistics API, маппинг `FINANCE_API_FIELD_MAP_`, лог `IMPORT_LOG_FINANCE` |
| `Wbfinanceimportfromdrive` | Импорт XLSX-финотчётов с Google Drive, `COL_MAPPING_`, меню «📥 Импорт WB Finance» |
| `Wbfinancemonthclose` | Закрытие месяца: `removeAllFinanceApiRows`, Drive-only загрузка, создание пустых листов ЮНИТ |
| `Wbfinance` | Тестовые расходы, `buildCleanWbDailyWithFinance`, `buildMonthlyUnitReportWithFinance`, кабинетные итоги |
| `Cleanwbdaily` | `buildCleanWbDaily()` — RAW → `CLEAN_WB_DAILY` (orders/sales/returns/ads/stocks) |
| `Cleanwbdailyperiod` | Сборка CLEAN за период + контроль из RAW_WB_FINANCE, обрезка вне периода |
| `Testrawtoclean` | `resetCleanWbDailyStructure`, `fillTestRawDataForClean`, тестовый контур RAW→CLEAN |
| `Monthlyunitreport` (v1.2) | Старый юнит-отчёт `buildMonthlyUnitReport()` |
| `MonthlyUnitReport_v10` (v10) | Новый юнит-отчёт `buildMonthlyUnitReportV10()` с хранением и рекламой по SKU |
| `WbStorageImport` | `importWbStorageReport_2026_05_18_24`, меню «📦 Хранение WB» |
| `Wbstoragefolderloader` | Папочный загрузчик хранения, сверка `verifyStorageReconciliation`, `getCleanStorageDailyMap_` |
| `CostManagement` | Себестоимость наборов, `onEditCostTracker` (onEdit-триггер), установка/удаление триггера |
| `Skucostaudit` | Связка SKU_MASTER ↔ COST_HISTORY по `wb_nm_id`, аудит и обновление `current_cogs` |
| `AuditAndNotes` | Полный аудит справочников: `auditSkuAndCost`, `auditSkuMaster_`, `auditCostHistory_`, `crossAudit_` |
| `Patch_v10_Config` | Патч схемы `RAW_WB_STORAGE` (21 колонка) для `Config` |
| `Patch rawskunormalize` | Патч нормализации SKU в RAW |
| `utils` | Общие хелперы (часть хелперов также дублируется в других файлах — см. §10) |

Документация в корне репозитория: `ARCHITECTURE.md`, `DATA_MODEL.md`, `PROJECT_RULES.md`, `CLAUDE.md`, `CHANGELOG.md`, `README.md`.

---

## 3. Импорт данных WB

| Источник | Файл / функции | Целевой лист | Примечание |
|---|---|---|---|
| Финансы (WB Statistics API, `reportDetailByPeriod`) | `Wbfinanceloader`: `importWbFinanceFromApi`, `importWbFinanceFromApiInternal_`, `fetchFinanceApiData_` | `RAW_WB_FINANCE` + `IMPORT_LOG_FINANCE` | Пагинация по `rrdid`, дедуп по `row_hash`, идемпотентная очистка своих API-строк за период |
| Финансы (XLSX с Drive) | `Wbfinanceimportfromdrive`: `importWbFinanceReports_2026_05_18_24`, `listWbFinanceReportsInDrive` | `RAW_WB_FINANCE` | Автодетект заголовков, маппинг через `COL_MAPPING_` |
| Финансы (Drive-only, месяц) | `Wbfinancemonthclose`: `updateWbFinanceMay2026FromDriveOnly`, `previewWbFinanceMay2026DriveFiles` | `RAW_WB_FINANCE` | Период берётся из лога / парсится в память |
| Хранение (XLSX) | `WbStorageImport`: `importWbStorageReport_2026_05_18_24` | `RAW_WB_STORAGE` | Сверка с `storage_fee` из RAW_WB_FINANCE |
| Хранение (API + папка Drive) | `Wbstoragefolderloader` | `RAW_WB_STORAGE` + `IMPORT_LOG_STORAGE` | Дедуп API>DRIVE, сверка по пересечению дат |
| Заказы/продажи/реклама/остатки (WB API) | меню «📡 API WB (в разработке)» | `RAW_WB_ORDERS`, `RAW_WB_SALES_RETURNS`, `RAW_WB_ADS`, `RAW_WB_STOCKS` | **Заглушки** `notImplementedYet_` — автозагрузки в коде нет |

Ключи токенов: `WB_TOKEN_STATISTICS` / `WB_TOKEN_ANALYTICS` (Script Properties).
Параметры API (в `Wbfinanceloader`): `WB_FINANCE_API_LIMIT_=100000`, `WB_FINANCE_API_MAX_PAGES_=12`, `WB_FINANCE_API_PAGE_PAUSE_=21000`, `WB_FINANCE_API_ROLLING_DAYS_=14`.

---

## 4. Финансовые модули

| Функция | Файл | Что делает |
|---|---|---|
| `importWbFinanceFromApi` / `...Internal_` | `Wbfinanceloader` | Импорт реализации через API, запись в RAW, лог |
| `fetchFinanceApiData_` | `Wbfinanceloader` | Запрос отчёта реализации с пагинацией, обработка 429 |
| `normalizeFinanceApiRows_` | `Wbfinanceloader` | Нормализация JSON → строки RAW, `row_hash`, привязка SKU |
| `aggregateFinanceSums_` | `Wbfinanceloader` | Суммы: реализация, к перечислению, логистика, хранение, удержания, продвижение, транзит, компенсации, «после расходов WB» |
| `importWbFinanceReports_2026_05_18_24` | `Wbfinanceimportfromdrive` | Импорт XLSX-пары отчётов (хардкод периода) |
| `removeAllFinanceApiRows` | `Wbfinancemonthclose` | Удаляет только API-строки (`FIN_API_` / `WB_API_REALIZATION`), пересобирает CLEAN |
| `computeFinanceControl_` | `Cleanwbdailyperiod` | Контрольные суммы за период из RAW_WB_FINANCE |
| `auditRawWbFinanceColumns` | `Wbfinance` | Аудит колонок RAW_WB_FINANCE |

Формула «После расходов WB» (как заложено в коде): `for_pay − логистика − хранение − удержания − приёмка − штрафы + компенсации лояльности + тех.компенсации`. WB-продвижение и транзитная доставка уже входят в удержания (в коде это явно прокомментировано, чтобы не вычитать дважды).

---

## 5. Реклама

| Аспект | Где в коде | Примечание |
|---|---|---|
| Чтение рекламы по SKU | `MonthlyUnitReport_v10`: `readAdsBySkuData_` | Агрегация `RAW_WB_ADS` по `date|nmId` → `spend` |
| Реклама в CLEAN | `Cleanwbdaily` | `RAW_WB_ADS` → `ads_spend` по `date|wb_nm_id` (берётся `Math.abs(spend)`) |
| Реклама в верхнем блоке юнита | `MonthlyUnitReport_v10` | «WB Продвижение» берётся из финотчёта (удержания) |
| Сверка ADS vs финотчёт | `MonthlyUnitReport_v10`: `writeReconciliationErrors_` | Расхождение `sum(RAW_WB_ADS.spend)` vs «WB Продвижение» → `ERRORS_CONTROL` |
| ДРР | юнит-отчёты | `ДРР = расход на рекламу / сумма реализации WB` |
| Загрузка рекламы из WB Ads API | меню | **Заглушка** `notImplementedYet_` — автозагрузки нет |

Соответствует правилу из `PROJECT_RULES.md`: реклама = (1) факт удержаний из финотчёта + (2) статистика кампаний; распределение по SKU делается из `RAW_WB_ADS`, а не выдаётся как точная поартикульная себестоимость.

---

## 6. SKU_MASTER и себестоимость

| Функция | Файл | Что делает |
|---|---|---|
| `auditSkuCostLinks` | `Skucostaudit` | Проверка связки SKU_MASTER ↔ COST_HISTORY по `wb_nm_id` (только точное совпадение) |
| `updateCurrentCogsFromCostHistory` | `Skucostaudit` | Тянет `current_cogs` в SKU_MASTER из `is_current=TRUE`; при дублях/пустых — не обновляет |
| заполнение `wb_nm_id` в COST_HISTORY | `Skucostaudit` | Заполняет колонку по SKU_MASTER, при необходимости создаёт колонку |
| `onEditCostTracker` | `CostManagement` | onEdit-триггер: при правке `current_cogs`/`bundle_build_cost` пишет в COST_HISTORY и снимает `is_current` со старой записи |
| `installCostEditTrigger` / `removeCostEditTrigger` | `CostManagement` | Установка/снятие триггера |
| расчёт себестоимости наборов | `CostManagement` | Компоненты (BUNDLES) + сборка; ручная перезапись подсвечивается жёлтым |
| `auditSkuAndCost`, `auditSkuMaster_`, `auditCostHistory_`, `crossAudit_` | `AuditAndNotes` | Полный аудит справочников и перекрёстные проверки |

Правила связки заложены жёстко: один `is_current=TRUE` на SKU; набор без записи в COST_HISTORY допустим (считается через компоненты); компоненты набора должны быть в SKU_MASTER.

---

## 7. PNL / юнит-экономика

Отдельного билдера `PNL_TOTAL`/`PNL_SKU` нет. P&L рассчитывается внутри юнит-отчётов. Существуют **три** реализации:

| Версия | Функция | Файл | Структура строк | Источник расходов по SKU |
|---|---|---|---|---|
| v1.2 (старая) | `buildMonthlyUnitReport` | `Monthlyunitreport` | 21 строка (`UNIT_ROW_LABELS_`) | WB-расходы по факту = 0, прибыль `= netRev − ads − cogs` |
| «с финансами» | `buildMonthlyUnitReportWithFinance` | `Wbfinance` | блок `R_` (21 строка) | расходы из CLEAN_WB_DAILY, кабинетные итоги |
| v10 (новая) | `buildMonthlyUnitReportV10` | `MonthlyUnitReport_v10` | кабинет — 22 строки (`UNIT_TOTAL_LABELS_V10_`), SKU — 18 строк | хранение из `RAW_WB_STORAGE`, реклама из `RAW_WB_ADS` по SKU |

Формулы прибыли по SKU (v10):
`profit_before = for_pay − logistics + comp_direct − cogs`;
`profit_after = profit_before − storage − ads`;
`margin = profit_after / for_pay`.

Кабинетный блок (v10): прибыль `= После расходов WB − себестоимость` (реклама уже внутри удержаний), маржинальность `= прибыль / реализация`.

Все три пишут на листы вида `ЮНИТ_MM_YYYY`.

---

## 8. CLEAN-слой

| Функция | Файл | Что делает |
|---|---|---|
| `buildCleanWbDaily` | `Cleanwbdaily` | RAW_WB_ORDERS / SALES_RETURNS / ADS / STOCKS → `CLEAN_WB_DAILY`, связка по `date|wb_nm_id`, ошибки в `ERRORS_CONTROL` |
| `buildCleanWbDailyWithFinance` | `Wbfinance` | Пересобирает CLEAN с финансовыми расходами; перезаписывает заголовки `CLEAN_HEADERS_FULL_`, чистит данные |
| `buildCleanWbDailyWithFinanceForPeriodInternal_` | `Cleanwbdailyperiod` | Полная сборка + обрезка до периода + контроль из RAW_WB_FINANCE |
| `clearCleanRowsOutsidePeriod_` | `Cleanwbdailyperiod` | Удаляет строки CLEAN вне `[dateFrom..dateTo]` |
| `resetCleanWbDailyStructure` | `Testrawtoclean` | Сброс структуры к 15 колонкам (`CLEAN_HEADERS_NEW_`): `sheet.clear()` + `deleteColumns` |
| `getCleanStorageDailyMap_` | `Wbstoragefolderloader` | Чистая дедуплицированная карта хранения по дням |

Две разные «ширины» CLEAN живут одновременно: 15 колонок (`CLEAN_HEADERS_NEW_`, тестовый сброс) и расширенная `CLEAN_HEADERS_FULL_` (сборка с финансами). Это даёт риск рассинхрона схемы (см. §11).

---

## 9. Меню и интерфейс

Главное меню строится в `Menu v2` → `onOpen()`: «🏷️ EVETIS WB» с подменю Настройка, Финансы WB, API WB (в разработке), Расчёты (заглушки), Обслуживание, Инфо.

Помимо него каждый модуль определяет **своё** меню (вызываются отдельно, не все из `onOpen`):

| Меню | Файл | Функция-конструктор |
|---|---|---|
| 💸 Финансы WB | `Wbfinance` | `addWbFinanceMenu` |
| 📥 Импорт WB Finance | `Wbfinanceimportfromdrive` | `addWbFinanceImportMenu` |
| 📅 Финансы: закрытие месяца | `Wbfinancemonthclose` | `addWbFinanceMonthCloseMenu` |
| 📊 CLEAN | `Cleanwbdaily` | `addCleanWbMenu` |
| 🧪 Тест RAW → CLEAN | `Testrawtoclean` | `addTestRawToCleanMenu` |
| 📈 Юнит-экономика | `Monthlyunitreport` | `addUnitReportMenu` |
| 📈 Юнит-экономика | `MonthlyUnitReport_v10` | `addUnitReportMenuV10` |
| 📦 Хранение WB | `WbStorageImport` | `addStorageImportMenu` |

В меню «📡 API WB» и «📊 Расчёты» пункты ведут на `notImplementedYet_`.

---

## 10. Потенциальные дубли

| Дубль | Где |
|---|---|
| Три билдера юнит-отчёта | `buildMonthlyUnitReport` (`Monthlyunitreport`), `buildMonthlyUnitReportWithFinance` (`Wbfinance`), `buildMonthlyUnitReportV10` (`MonthlyUnitReport_v10`) |
| Объект индексов строк `R_` | определён и в `Monthlyunitreport`, и в `Wbfinance` |
| Метки строк юнита | `UNIT_ROW_LABELS_`, `UNIT_TOTAL_LABELS_`, `UNIT_TOTAL_LABELS_V10_`, `UNIT_SKU_LABELS_SKELETON_` — в разных файлах |
| Два конструктора меню «📈 Юнит-экономика» | `addUnitReportMenu` и `addUnitReportMenuV10` |
| Схема `RAW_WB_STORAGE` | в `Patch_v10_Config` (рабочая) и как комментарий в `MonthlyUnitReport_v10` |
| Заголовки CLEAN | `CLEAN_HEADERS_NEW_` (15) и `CLEAN_HEADERS_FULL_` (расширенная) |
| Общие хелперы | `getHeaderMap_`, `readSheetData_`, `findCol_`, `parseDate_`, `formatDate_`, `pad2_`, `roundTwo_`, `sec_`, `setIfExists_`, `normalizeDateKey_`, `normalizeNmId_` / `normalizeNmIdFinance_` — разбросаны по `utils`, `Skucostaudit`, `Cleanwbdaily`, `Wbfinance` |
| XLSX-импорт (конверсия, детект заголовков, очистка temp) | повторяется между финансами и хранением |
| Несколько `addWbFinance*Menu` | `Wbfinance`, `Wbfinanceimportfromdrive`, `Wbfinancemonthclose` |

Это наблюдения, а не план рефакторинга.

---

## 11. Потенциально опасные функции

| Функция | Файл | Риск |
|---|---|---|
| `resetCleanWbDailyStructure` | `Testrawtoclean` | `sheet.clear()` + `deleteColumns` на `CLEAN_WB_DAILY` — полная потеря данных листа |
| `fillTestRawDataForClean` | `Testrawtoclean` | Пишет тестовые данные в RAW (через `writeTestRows_`, сначала чистит диапазон) — затирает боевой RAW |
| `fillTestFinanceRawData` | `Wbfinance` | Тестовые финрасходы в `RAW_WB_FINANCE` |
| `buildCleanWbDailyWithFinance` | `Wbfinance` | Перезаписывает заголовки + `deleteColumns` на CLEAN; при рассинхроне схем — потеря колонок |
| `removeAllFinanceApiRows` | `Wbfinancemonthclose` | Массовое удаление API-строк RAW_WB_FINANCE + пересборка CLEAN |
| `clearCleanRowsOutsidePeriod_` | `Cleanwbdailyperiod` | Удаляет все строки CLEAN вне периода |
| `clearFinanceOwnPeriod_` | `Wbfinanceloader` | Удаление строк за период |
| `onEditCostTracker` | `CostManagement` | Авто-запись в COST_HISTORY при каждой правке SKU_MASTER; при ошибке тихо засоряет историю |
| `setupWorkbook` → `createAllSheets_` | `Code.gs` | `deleteColumns` лишних колонок — на заполненной книге может срезать колонки с данными |

---

## 12. Функции с хардкодом дат / file_id / периода

| Что захардкожено | Где |
|---|---|
| `FINANCE_REPORTS_FOLDER_ID_ = '1pLgyjHvy4jB5GnaTwVkrMTuCsw2zyXOd'` | `Wbfinanceloader` / импорт с Drive |
| `importWbFinanceReports_2026_05_18_24` (период 18–24.05.2026) | `Wbfinanceimportfromdrive` |
| `importWbStorageReport_2026_05_18_24` (период 18–24.05.2026) | `WbStorageImport` |
| `updateWbFinanceMay2026FromDriveOnly`, `previewWbFinanceMay2026DriveFiles` (май 2026) | `Wbfinancemonthclose` |
| `createUnitSheetJune2026IfNeeded` → `createMonthlyUnitSheetIfNeeded(2026, 6)` | `Wbfinancemonthclose` |
| `WB_FINANCE_API_ROLLING_DAYS_=14`, `WB_FINANCE_API_LIMIT_=100000`, `WB_FINANCE_API_MAX_PAGES_=12`, `WB_FINANCE_API_PAGE_PAUSE_=21000` | `Wbfinanceloader` |

Сами по себе данные они не разрушают, но это хрупкие точки (привязка к конкретной папке/периоду).

---

## 13. Функции, которые нельзя запускать на боевой таблице без проверки

| Функция | Файл | Почему опасно на проде |
|---|---|---|
| `resetCleanWbDailyStructure` | `Testrawtoclean` | Полностью очищает `CLEAN_WB_DAILY` |
| `fillTestRawDataForClean` | `Testrawtoclean` | Перезаписывает RAW тестовыми данными |
| `fillTestFinanceRawData` | `Wbfinance` | Перезаписывает финансовый RAW тестовыми расходами |
| `removeAllFinanceApiRows` | `Wbfinancemonthclose` | Удаляет API-строки и пересобирает CLEAN |
| `buildCleanWbDailyWithFinance` | `Wbfinance` | Перезапись заголовков + удаление колонок CLEAN |
| `setupWorkbook` / `createAllSheets` | `Code.gs` | Пересоздание/обновление структуры всех листов, удаление лишних колонок |
| `installCostEditTrigger` | `CostManagement` | Включает авто-запись в COST_HISTORY по каждому редактированию |

Перед запуском любой из них на боевой таблице нужны контрольные суммы до/после (механизм частично уже есть: `IMPORT_LOG_*`, `ERRORS_CONTROL`, контроль в `Cleanwbdailyperiod`).

---

## 14. Что позже можно вынести в отдельные файлы

Только наблюдения, без плана переписывания:

| Кандидат | Обоснование |
|---|---|
| Разделение `Wbfinance` | Несёт минимум 4 роли: нормализация дат/nmId, билдер CLEAN с финансами, генератор тестовых данных, билдер ЮНИТ + кабинетные итоги |
| Единый слой утилит | `getHeaderMap_`, `readSheetData_`, `findCol_`, `parseDate_`, `formatDate_`, `pad2_`, `roundTwo_`, `sec_`, `setIfExists_`, `normalizeDateKey_`, `normalizeNmId_`/`normalizeNmIdFinance_` разбросаны по нескольким файлам |
| Общий «importer core» | XLSX-импорт (конверсия, детект заголовков, очистка temp) дублируется между финансами и хранением |
| Изоляция тестовых генераторов | `Testrawtoclean`, `fillTestFinanceRawData` логично отделить от боевого кода |

---

## 15. Что нельзя трогать без контрольных сумм

Согласно `CLAUDE.md` / `PROJECT_RULES.md` и текущей логике кода:

| Объект | Почему нужна контрольная сверка |
|---|---|
| `RAW_WB_FINANCE | Главный источник финансового факта WB для текущей модели P&L |
| `PNL_TOTAL` / итоги юнит-отчётов | Прямой запрет менять без контрольных сумм; внутри уже есть сверка CLEAN → ЮНИТ |
| `CLEAN_WB_DAILY` | Промежуточный слой; ширина схемы (15 vs full) хрупкая, любая перезапись заголовков должна сверяться |
| `COST_HISTORY` (`is_current`, `cogs_per_unit`) | От него зависит вся себестоимость; дубли `is_current` ломают P&L |
| `SKU_MASTER` (`wb_nm_id`, `current_cogs`) | Ключ связки со всеми RAW и расходами |
| Структура колонок любого листа | Менять только с описанием в `CHANGELOG.md` |

Существующие механизмы контроля, на которые можно опираться: `IMPORT_LOG_FINANCE`, `IMPORT_LOG_STORAGE`, `ERRORS_CONTROL`, сверка хранения по пересечению дат (`verifyStorageReconciliation`), сверка ADS vs финотчёт (`writeReconciliationErrors_`), контроль за период (`computeFinanceControl_`).

---

*Документ фиксирует текущее состояние кода и не предлагает изменений. Все правки структуры или функций — только после отдельного согласования и записи в `CHANGELOG.md`.*
