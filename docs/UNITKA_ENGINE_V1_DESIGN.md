# UNITKA ENGINE v1 — дизайн (Stage E1)

Статус: **Stage E1 реализован 12.09.2026** — подготовленный слой применён в BigQuery, Engine
написан и покрыт тестами, инфраструктура описана в Terraform, офлайн SHADOW-прогон выполнен
(`docs/UNITKA_ENGINE_V1_SHADOW_2026-09-12.md`). Облачный запуск ждёт предусловий владельца (§9). September Master (`WB_Юнит_2025`, строки 735–768) — immutable reference
implementation; Engine его **обслуживает**, а не меняет.

Агент claude-fable-5-1, 12.09.2026. Код: `cloud/src/loaders/unitka/`, SQL: `sql/unitka/`,
инфраструктура: `infra/terraform/unitka_engine.tf`.

---

## 0. Что обнаружено при инвентаризации (влияет на дизайн)

1. **Платформа Cloud Run Jobs уже production-grade**: один образ, dispatch по `args[0]`,
   execution-guard `LOADER_RUNS` (lease на `environment × loader × logical_period`),
   `run_id` с `git_sha` и `IMAGE_DIGEST`, деплой по digest через GitHub Actions, shadow/prod
   как отдельные Job'ы и SA, Terraform-owned таблицы. Это готовый фундамент — Engine
   становится **ещё одним загрузчиком `unitka`** на том же образе.
2. **Ни один облачный компонент пока не пишет в Google Sheets.** Sheets API не включён в
   `apis.tf`, у SA нет доступа к книге, зависимости `googleapis` в `cloud/package.json` нет.
   Это три конкретных предусловия (см. §9).
3. **Apps Script не деплоится из git** — нет clasp, нет workflow. Синхронизация делалась
   вручную (последний раз — экспортом проекта через Drive API, коммит `3bf8246`).
   Требование «repo — единственный source of truth» на Apps Script выполнимо только
   организационно, на Cloud Run — технически. Это решающий аргумент §1.
4. **Загрузчик воронки `wb-funnel-prod` не работает по расписанию.** В `LOADER_RUNS` ровно
   один прогон (11.09 06:50 UTC, ручной), Scheduler в Terraform `paused = true`.
   На 12.09 воронка стоит на **10.09**, при том что витрина и заказы — на 11.09.
   Без включения этого Scheduler'а у Engine не будет свежего authoritative-источника.
   **Это блокер acceptance, а не Engine'а** — Engine его честно покажет как `SOURCE_STALE`.
5. Свежесть источников на 12.09: funnel 10.09 · mart/orders 11.09 · stocks 12.09 ·
   storage 09.09 · finance 11.09. То есть источники **закрываются с разной задержкой** —
   `LAST_CLOSED_DATE` нельзя брать как «вчера».
6. Все 16 шагов суточного цикла из брифа уже существуют как проверенные функции Apps Script
   (`s82data`, `s8brates`, `s8rates`, `s8bqa`, `s8qa`, `s82lcd`) и как SQL внутри них.
   Engine переносит **SQL — в подготовленный слой BigQuery**, а **оркестрацию и запись —
   в TypeScript**, с побайтовой сверкой против текущего листа в SHADOW.

## 1. Архитектурное решение

```
WB API → RAW BigQuery (wb_raw) → canonical/mart (wb_mart, evetis_ref)
       → ПОДГОТОВЛЕННЫЙ СЛОЙ wb_mart.V_UNITKA_*  ←── Engine читает ТОЛЬКО его
       → Cloud Run Job `unitka` (shadow / prod)   ──→ Google Sheets (только факт-ячейки)
       → журнал wb_ops.UNITKA_ENGINE_RUNS + V_UNITKA_ENGINE_STATUS
```

**Runtime — Cloud Run Job `unitka` на существующем образе, Cloud Scheduler, тот же
execution-guard.** Отвергнутая альтернатива — Apps Script с time-driven trigger:
он дал бы быстрый старт (код уже есть), но (а) не имеет пути деплоя из git, (б) не
имеет `run_id`/digest/manifest, (в) shadow-режим пришлось бы имитировать, (г) 6-минутный
лимит выполнения уже упирался в OOM на 4 671 правилах УФ. Apps Script Stage-8 код
**остаётся в репозитории как reference implementation и ручной инструмент отката**
(`s82datarollback`, `s8brollback`, `s82r768rollback`), но перестаёт быть механизмом
ежедневного обновления.

**Sheets получает только факт.** Формулы расчётных колонок, УФ, ширины, полосы, MTD-формулы,
сводка — не трогаются никогда (формулы-наследие в факт-колонках — см. §3). Engine пишет ровно те ячейки, значение которых изменилось:
9 факт-колонок закрытых дней (`views, opens, carts, orders, cancels, stock, adsIn, price,
storage`), колонки ставок (`logistics`, `commission`), `REVERSE_LEG_RATE` (`WB737`),
`LAST_CLOSED_DATE` (именованный диапазон + зеркало `WB736`). Итог: все остальные 12 шагов
брифа (unit economics, MTD, сводка) пересчитываются формулами листа — как и задумано в
Master.

## 2. Подготовленный слой BigQuery (`sql/unitka/engine_v1_views.sql`)

Все вью — `wb_mart.V_UNITKA_*`, `CREATE OR REPLACE VIEW`, только чтение production-слоёв.
Engine не содержит бизнес-SQL: `SELECT * FROM view`.

| вью | грейн | что даёт |
|---|---|---|
| `V_UNITKA_SOURCE_FRESHNESS` | 1 строка на источник | `source, max_closed_date, gating (BOOL), observed_at` |
| `V_UNITKA_LAST_CLOSED_DATE` | 1 строка | `last_closed_date = MIN(max_closed_date) по gating-источникам, ≤ D-1 МСК` |
| `V_UNITKA_DAILY_FACT` | `nm_id × date` за текущий месяц | 9 факт-метрик + `orders_source` (`FUNNEL_API` / `XLSX_BACKFILL` / `ORDERS_API`) + `cancels_source` (`XLSX_BACKFILL` / `PROXY_FACT_ORDERS`) |
| `V_UNITKA_LOGISTICS_RATES` | `nm_id` + строка `0` = магазин | популяция `srid IN ('Логистика','Доставка')`, `direct`, `reverse`, `n`, `ref`, `sales`, `nlog`, `ndlv`, `forward_sum`, `reverse_sum`, `delivery_component_sum` |
| `V_UNITKA_COMMISSION_RATES` | `nm_id` + `0` | `commission_rate = ROUND(тариф,6) + ROUND(эквайринг,6)` (как `s8fin_`), `tariff_rate`, `acquiring_rate`, `sales`, `logistics_per_unit` (для правила «своя ставка») |

Gating-источники для `LAST_CLOSED_DATE`: **воронка** (authoritative для opens/carts/orders)
и **витрина** (`MART_RUNS COMPLETE` → views/ads, `FACT_ORDERS` → cancels/price).
Остатки, хранение, финансы не гейтят: их пропуски в Master уже приняты как «GAP — пусто,
не ноль» и дозаполняются следующими прогонами.

Окно факта — **весь текущий месяц до `LAST_CLOSED_DATE`** (30 строк блока, а не D-7):
воронка мутабельна 7 дней, витрина может быть перестроена, а стоимость перечитать
750 ячеек нулевая. Так «исправление задним числом» получается бесплатно — Engine просто
видит другое значение и переписывает ячейку (в офлайн-SHADOW 12.09 так всплыла отмена
`773170315` за 07.09, появившаяся в `FACT_ORDERS` после загрузки листа). Окно ставок —
**30 дней `[LCD−29, LCD]`**, ровно как `s8win_()` в Apps Script (при LCD 10.09 → 12.08–10.09;
первый черновик вью брал D-30…D-1 и был исправлен до применения).

Контракт воронки (§3 решения владельца): `opens / carts / gross orders = FUNNEL_API`;
`01–03.09` — только исторический `RAW_WB_FUNNEL_XLSX_BACKFILL`, в вью помечен источником
и **ограничен датами ≤ 2026-09-03** (production Engine XLSX не использует — он его
просто не увидит за другие даты); `cancels 04.09+ = PROXY_FACT_ORDERS` — так и подписан.

## 3. Суточный цикл Engine (16 шагов брифа → реализация)

| # | шаг брифа | где живёт | fail-closed |
|---|---|---|---|
| 1 | LAST_CLOSED_DATE | `V_UNITKA_LAST_CLOSED_DATE` | нет строки / дата раньше зеркала в книге → `LCD_REGRESSION` |
| 2 | rolling windows | вью (месяц; `[LCD−29, LCD]`) | — |
| 3–9 | funnel, orders/cancels, stock, price, ads, storage | `V_UNITKA_DAILY_FACT` | дубль `date×nm` → `DUP_KEY` |
| 9 | commission/acquiring | `V_UNITKA_COMMISSION_RATES` | — |
| 10–11 | direct / reverse logistics | `V_UNITKA_LOGISTICS_RATES` | **инвариант** `n = sales + ref` → `INVARIANT_FAIL` |
| 12–14 | unit economics, MTD, сводка | формулы Master | не пишутся |
| 15 | QA | `qa.ts`: preflight + plan-QA + post-write | любой FAIL → `EXIT_ERROR`, журнал `ERROR` |
| 16 | запись | `sheets.ts`: один `values.batchUpdate` | только после plan-QA PASS |

Preflight (до вычислений): 24 блока найдены по nmID в строке 735; даты блоков и сводки —
дни месяца LCD (иной месяц → `MONTH_ROLLOVER_REQUIRED`, E1.1); в расчётных колонках всех
30 строк и в сводке C..K стоят формулы — иначе `STRUCTURE_DRIFT`; именованный диапазон
`LAST_CLOSED_DATE` и зеркало `WB736` — даты.

**Формулы-наследие в факт-колонках** (обнаружено офлайн-SHADOW): в Master остались
`=stock*0.15` (хранение) и `=prev − orders + cancels` (остаток) в колонках `stock`/`storage`
части блоков — 67 ячеек в закрытых днях, 495 в будущих. Это не дрейф структуры: `s82data`
заменял такие ячейки значениями, когда значения расходились, и 67 уцелели только потому,
что их результат совпал с BigQuery (0 = 0). Engine ведёт их учёт (`legacy_formulas` в журнале)
и **в закрытых днях заменяет значением из BigQuery даже при совпадении результата** — после
первой prod-записи контракт «факт = значение» становится полным. Будущие дни Engine не
трогает; их формулы-проекции считаются отдельно (`LEGACY_FUTURE_FORMULAS`, informational) и
**не** засчитываются как `FUTURE LEAKAGE` — утечкой считается только значение без формулы.

Post-write reconciliation: повторное чтение сетки → `BQ→SHEETS MISMATCH = 0`,
`FORMULA ERRORS = 0` (по `#REF!/#DIV/0!/…` в 735–768), `FUTURE LEAKAGE = 0` (факт-ячейки
за датами > LCD пусты), `SUMMARY RECONCILIATION` (сумма 24 блоков по закрытым дням =
колонки сводки, `max|Δ| = 0`), `LAST_CLOSED_DATE consistent` (имя = зеркало = вычисленное).

## 4. Мутабельность и идемпотентность

Значение — единственный ключ идемпотентности: Engine перечитывает все закрытые дни
месяца и пишет ячейку тогда и только тогда, когда `sheet ≠ bq` (числа сравниваются с
допуском 0.005, пусто ≠ 0). Повтор прогона с теми же источниками даёт **пустой план**
(`cells_changed = 0`) — это тестируется. Отдельного `settle-window` для заказов/финансов
Engine не вводит: он берёт уже устоявшиеся правила источников (витрина строится за D-1,
воронка перечитывается D-7, финансы приходят недельными отчётами и попадают в ставки
через окно D-30).

## 5. QA-гейт публикации (§7 брифа)

```
BLOCKS = 24/24                      preflight
NO DUPLICATE date×nmID              plan-QA (вью + план)
DIRECT LOGISTICS INVARIANT = PASS   plan-QA (n = sales + ref)
LAST_CLOSED_DATE consistent         plan-QA (не раньше зеркала) + post-write
BQ → SHEETS MISMATCH = 0            post-write
FORMULA ERRORS = 0                  post-write
FUTURE LEAKAGE = 0                  post-write
SUMMARY RECONCILIATION = PASS       post-write
```

Нарушение **до записи** → запись не выполняется, `EXIT_ERROR`, журнал `ERROR` с кодом.
Нарушение **после записи** (единственный batch) → журнал `ERROR` + код `POST_WRITE_*`,
алерт; следующий прогон при тех же источниках не изменит ничего (идемпотентность), а
при исправленных — перепишет. «Частичное обновление» исключено конструктивно: план
пишется одним запросом `values.batchUpdate`, а не поячеечно.

## 6. Наблюдаемость (§8 брифа)

Таблица `wb_ops.UNITKA_ENGINE_RUNS` (Terraform-owned):
`run_id, environment, started_at, completed_at, last_closed_date, source_freshness_json,
rows_read, cells_planned, cells_written, qa_status, qa_json, error_code, error_message,
git_sha, image_digest, engine_version`.

`V_UNITKA_ENGINE_STATUS` — одна строка: `OK` (последний prod-прогон PASS не старше 26 ч) /
`STALE` (нет PASS > 26 ч) / `ERROR` (последний prod-прогон FAIL); применяется после infra apply
(читает журнал). Коды = `error_code`: `SOURCE_STALE`, `SHEETS_API`, `BQ_MISMATCH`,
`FORMULA_ERROR`, `FUTURE_LEAKAGE`, `SUMMARY_MISMATCH`, `LCD_INCONSISTENT`, `PARTIAL_WRITE`,
`INVARIANT_FAIL`, `STRUCTURE_DRIFT`, `LCD_REGRESSION`, `DUP_KEY`, `BLOCK_MISSING`,
`MONTH_ROLLOVER_REQUIRED`, `ENGINE_ERROR`.

**Алерты** — Cloud Monitoring, а не детектор `wb_ops`: тот жёстко привязан к витрине
(`sp_evaluate_pipeline_health` читает `MART_RUNS`) и сам ещё не применён. Любой FAIL Engine =
`exit 1` = execution FAILED + запись `loader_failed` с кодом; log-based alert policy
(`google_monitoring_alert_policy.unitka_engine_failed`) шлёт письмо на `var.unitka_alert_email`
(канал создаётся только при заданном адресе; не чаще раза в час). Это покрывает все шесть
условий брифа: source stale / API failure / BQ mismatch / formula error / partial write /
invariant failure.

## 7. Среды и acceptance (§11 брифа)

| режим | Job | SA | Sheets | что делает |
|---|---|---|---|---|
| SHADOW | `unitka-engine-shadow` | `sa-loaders-shadow` | **Читатель**, scope `spreadsheets.readonly`, `UNITKA_WRITE_ENABLED=0` | считает план, сверяет с листом, пишет журнал `mode=SHADOW` (`SHADOW_MATCH` / `SHADOW_DIFF` / `SHADOW_FAIL`), **не пишет в книгу** |
| PROD | `unitka-engine-prod` | `sa-loaders-prod` | **Редактор**, scope `spreadsheets`, `UNITKA_WRITE_ENABLED=1` | полный цикл; без `UNITKA_WRITE_ENABLED=1` prod тоже работает как SHADOW |

Порядок: SHADOW несколько дней (`cells_planned` должен совпадать с ожиданием: 0 — если
Apps Script уже загрузил день, ~120–150 — новый день) → один controlled write-day
(owner снимает паузу prod-Scheduler'а на одно окно) → 2–3 автоматических прогона →
`UNITKA ENGINE v1 PRODUCTION READY = YES`. Октябрь (E1.1) — только после этого.

## 8. Расписание

**10:00 и 12:30 МСК** (`07:00` / `09:30 UTC`) для обеих сред — после витрины (07:04–07:06,
резерв 12:00) и после воронки (09:30 МСК по её Scheduler'у). Утреннее окно 07:30 из первого
черновика отвергнуто: воронка к нему ещё не догружена, и каждый день начинался бы с отставания.
Второе окно не пересобирает ничего: при тех же источниках план пуст. Отставание LCD от D-1 на
1 сутки — штатно (`lag_days` в журнале), `> UNITKA_MAX_LAG_DAYS = 2` → `SOURCE_STALE`.
`max_retries = 0` (повтор = новый execution). Логический период guard'а — **часовой слот МСК**
(`YYYY-MM-DDTHH`), а не сутки: суточный ключ подавил бы второе окно; внутри слота повтор
подавляется, ERROR-прогон переигрывается.

## 9. Предусловия, которые выполняет владелец (Engine их проверяет, не обходит)

1. `infra apply` (`infra.yml`, action=apply): `sheets.googleapis.com` + `monitoring.googleapis.com`
   в `apis.tf`, `wb_ops.UNITKA_ENGINE_RUNS`, два Job'а, четыре Scheduler'а (paused), IAM
   (shadow: dataViewer на `wb_raw`/`wb_mart`; обе среды: dataEditor на журнал; run.invoker).
   ⚠ В плане уже есть **чужой дрейф** (5 in-place изменений, описаны в `ops_health.tf`) —
   развести его до apply, иначе включение Engine потянет откат схемы остатков и Ozon-релиза.
   Опционально `unitka_alert_email` в `terraform.tfvars` — без него алерт-политика не создаётся.
2. Открыть книгу `1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg` для
   `sa-loaders-shadow@…` (Читатель) и `sa-loaders-prod@…` (Редактор).
3. **Снять паузу с `wb-funnel-prod`** (`scheduler-control.yml`, loader=`wb-funnel`, prod,
   resume) — иначе LCD навсегда 10.09 и с 13.09 Engine честно падает `SOURCE_STALE`.
4. `deploy-shadow.yml` (собирает образ с Engine, обновляет `unitka-engine-shadow`) →
   `scheduler-control.yml` loader=`unitka-engine`, shadow, run-now → журнал
   `wb_ops.UNITKA_ENGINE_RUNS` → при `SHADOW_DIFF/SHADOW_MATCH` снять паузу shadow-Scheduler'ов.
5. После infra apply применить вью 6 из `sql/unitka/engine_v1_views.sql` (`V_UNITKA_ENGINE_STATUS`).

## 10. Что НЕ делается в E1

Октябрь и любой rollover (Engine на октябрьском LCD останавливается кодом
`MONTH_ROLLOVER_REQUIRED`); правка Master; XLSX в production (вью ограничена `≤ 2026-09-03`);
замена Apps Script загрузчиков RAW; изменение витрины; автоматическое включение
prod-Scheduler'а; чистка формул-наследия в будущих днях (см. §3 — OWNER DECISION).

## 11. Реализация (что лежит в репозитории)

```
cloud/src/loaders/unitka/model.ts   геометрия Master (GRID/OFFSET/FACT_KEYS), A1, серийные даты, сравнения
cloud/src/loaders/unitka/bq.ts      UnitkaBq: 5 вью → типизированные строки; журнал INSERT
cloud/src/loaders/unitka/sheets.ts  SheetsRest: values:batchGet / FORMULA / values:batchUpdate (RAW), ADC
cloud/src/loaders/unitka/plan.ts    preflight, buildPlan (fail-closed коды), toWriteRanges
cloud/src/loaders/unitka/qa.ts      evaluate: 8 проверок гейта + LEGACY_FUTURE_FORMULAS
cloud/src/loaders/unitka/index.ts   unitkaLoader: 5 фаз, SHADOW/WRITE, журнал, ENGINE_VERSION
cloud/src/loaders/unitka/slot.ts    часовой слот МСК (logical_period)
cloud/scripts/unitka_offline_shadow.mjs  офлайн SHADOW на экспорте книги + JSON вью
cloud/test/unitka_*.test.ts         30 тестов (фикстура — синтетический Master 24×30)
sql/unitka/engine_v1_views.sql      6 вью (1–5 применены 12.09, 6 — после infra apply)
infra/terraform/unitka_engine.tf    журнал, IAM, 2 Job, 4 Scheduler (paused), алерт
.github/workflows/*                 deploy-shadow/prod (guarded), scheduler-control (unitka-engine, wb-funnel)
```

Зависимость: `google-auth-library` (уже была транзитивной у BigQuery-клиента) — Sheets REST
вызывается напрямую, пакет `googleapis` не добавлялся. Секретов у Engine нет: доступ к книге —
через IAM сервисного аккаунта Cloud Run.
