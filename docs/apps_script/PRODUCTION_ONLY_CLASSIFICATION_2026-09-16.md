# INGESTION — production-only файлы: классификация и план lossless-сверки

Дата: 2026-09-16. Всё получено read-only. Ничего не удалено и не изменено в production.

## Метод

Для каждого production-файла, отсутствующего в Git, взяты объявленные символы
(функции и глобальные `var/const/let`) и выполнен поиск их вызовов во ВСЕХ остальных
87 файлах проекта с предварительно вырезанными комментариями. Отдельно проверено
вхождение в 11 триггеров проекта и в пункты меню (`addItem`).

🔴 Все найденные «ссылки» оказались ложными — это совпадения по именам локальных
переменных (`sheet`, `keys`, `rows`, `error`, `total`, `sales`, `attempt`, `dateFrom`).
**Ни один production-only файл не вызывается из другого production-файла.**

## Таблица

| файл | символов | вызывается из production? | триггер / меню | назначение | вердикт |
|---|---|---|---|---|---|
| `Wbmarkingkizloader` | 71 | нет | нет | загрузчик КИЗ поставок в `RAW_WB_KIZ_SUPPLY` (34 поставки, 3068 кодов) — рабочий код, не проба | **KEEP** |
| `Wbfinanceapidiag` | 74 | нет | **2 пункта меню**: `compareWbFinanceApiVsRawXlsx_2026_05_18_24`, `testAuditWbFinanceApi_2026_05_18_24` | диагностика финансового API | **KEEP** |
| `Wbdocumentsprobe` | 69 | нет | нет | проба API документов WB (источник КИЗ, УПД по маркировке) | ARCHIVE |
| `WbSalesProbeD2` | 86 | нет | нет | READ-ONLY проба Sales/Returns | ARCHIVE |
| `WbAdsProbeV2` | 22 | нет | нет | READ-ONLY проба рекламы P1+P2 (V5, V6 уже в Git) | ARCHIVE |
| `WbAdsProbeV3` | 27 | нет | нет | READ-ONLY проба историчности normquery/stats | ARCHIVE |
| `WbAdsProbeV4` | 17 | нет | нет | READ-ONLY проба get-bids на CPC+manual парах | ARCHIVE |
| `Wbincomesshapeprobe` | 33 | нет | нет | shape-проба incomes (Probe2 и Probe3 уже в Git) | ARCHIVE |
| `Wbworkbookaudit` | 12 | нет | нет | аудит книги (лимит ячеек) | ARCHIVE |
| `FindFinanceReturns_TEMP` | 99 | нет | нет | временная диагностика возвратов; в имени `_TEMP` | CANDIDATE_REMOVE |
| `WbApiGateSalesReturns_TEMP` | 119 | нет | нет | временная проба API-шлюза; в имени `_TEMP` | CANDIDATE_REMOVE |
| `UnitkaR7` (в INGESTION) | 1 | нет | нет | пустая заглушка `function myFunction(){}`, 3 строки | CANDIDATE_REMOVE (OWNER DECISION 2: пока НЕ удалять) |

## Что обязано попасть в Git, чтобы первый push был lossless

`clasp push` удаляет из проекта всё, чего нет локально. Поэтому **вердикт KEEP / ARCHIVE /
CANDIDATE_REMOVE не влияет на необходимость импорта** — импортировать нужно ВСЕ 12 файлов
выше, иначе push их сотрёт. Решение об архивации или удалении принимается потом,
отдельным controlled deployment.

Дополнительно к ним — 34 файла в `apps-script/ingestion/`, у которых **нет расширения**
(`AuditAndNotes`, `Cleanwbdaily`, `Config`, `Menu v2`, `Wbfinance`, `loadCostHistory` и др.).
clasp отправляет только `*.gs` / `*.js` / `*.html` / `appsscript.json`, поэтому эти файлы он
не увидит, а их production-двойников удалит. Нужен `git mv <файл> <файл>.gs` для каждого.

### План lossless-сверки INGESTION

| шаг | действие | файлов |
|---|---|---|
| 1 | `git mv` 34 файлов без расширения → `*.gs` | 34 |
| 2 | импорт 12 production-only файлов в `apps-script/ingestion/` побайтово | 12 |
| 3 | привести к байтам production 25 файлов, различающихся только пробелами | 25 |
| 4 | повторный прогон `tools/clasp_prepush_verify.js ingestion` → цель `DELETED = 0` | — |

После шагов 1–3 в MODIFIED останутся только файлы с реальным расхождением: Step 5A
(`IngestRunLog`, `WbAdsDaily`, `WbAdsRawLoader`, `WbAdsQueryBids`, `Wbadsquerystats`),
`UnitkaR3Ingest`, `UnitkaR5`, `UnitkaR6`, `WbBigQuery`, `WbStocksBigQuery`,
`Wbsuppliesinventoryprobe4` — то есть ровно то, что мы осознанно хотим задеплоить.

### UNITKA

В production есть файл `Код` (8470 байт) — сентябрьский QA-скрипт R4 v4.2.0 со сверкой
с BigQuery и независимым пересчётом формул. В Git его нет. Импортировать как
`apps-script/unitka/Код.gs`, иначе push его удалит.

### OPERATIONS

`Код.gs` — монолит-сборка. По OWNER DECISION 4 миграция на модули выполняется отдельным
controlled deployment, поэтому до него `clasp push` в OPERATIONS не выполняется вообще.
