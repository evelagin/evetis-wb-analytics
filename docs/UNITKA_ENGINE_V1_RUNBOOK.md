# UNITKA ENGINE v1 — runbook

Ежедневное обновление September Master (`WB_Юнит_2025`, строки 735–767) из подготовленного
слоя BigQuery без участия человека. Дизайн: `UNITKA_ENGINE_V1_DESIGN.md`. Код:
`cloud/src/loaders/unitka/`. Инфраструктура: `infra/terraform/unitka_engine.tf`.

## 1. Что делает прогон (и чего не делает)

```
V_UNITKA_SOURCE_FRESHNESS → V_UNITKA_LAST_CLOSED_DATE → снимок листа → preflight
→ V_UNITKA_DAILY_FACT + *_RATES → план (только изменившиеся ячейки)
→ SHADOW: журнал, выход  |  PROD: один values.batchUpdate → повторное чтение → QA-гейт → журнал
```

Пишет: 9 факт-колонок закрытых дней (`views opens carts orders cancels stock adsIn price
storage`), колонки `logistics`/`commission` (30 строк блока), `REVERSE_LEG_RATE` (`WB737`; в Engine 2.0.0 — колонка по имени диапазона),
`LAST_CLOSED_DATE` (имя + зеркало `WB736`). Не пишет: формулы, УФ, ширины, строку 767/768,
будущие дни, блогеров, `spp`. Любой FAIL → ничего не записано (план — один batch), `exit 1`.

## 2. Включение (по порядку, каждый шаг — владелец)

| # | шаг | где | проверка |
|---|---|---|---|
| 1 | targeted plan только по Engine | `infra.yml` action=plan, `targets=` список из `UNITKA_ENGINE_V1_LIVE_SHADOW_E2_2026-09-12.md` §7 | `15 to add, 0 to change, 0 to destroy` — чужой дрейф (`ops_health.tf`) не затрагивается |
| 2 | targeted `infra apply` | `infra.yml` action=apply, те же `targets` | Job'ы `unitka-engine-shadow/prod`, 4 Scheduler'а (paused), таблица `wb_ops.UNITKA_ENGINE_RUNS` |
| 3 | дать доступ к книге `1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg` | Sheets → Настройки доступа | `sa-loaders-shadow@…` Читатель, `sa-loaders-prod@…` Редактор |
| 4 | включить воронку | `scheduler-control.yml`: `wb-funnel`, prod, resume | `RAW_WB_FUNNEL_DAILY` за D-1 появляется ~09:35 МСК |
| 5 | применить вью 6 | `bq query < sql/unitka/engine_v1_views.sql` (только блок 6) | `SELECT * FROM wb_mart.V_UNITKA_ENGINE_STATUS` |
| 6 | собрать образ с Engine | `deploy-shadow.yml` | шаг «Deploy unitka-engine-shadow» выполнен |
| 7 | первый SHADOW | `scheduler-control.yml`: `unitka-engine`, shadow, run-now | журнал: `mode=SHADOW`, `qa_status ∈ {SHADOW_DIFF, SHADOW_MATCH}` |
| 8 | SHADOW по расписанию 2–3 дня | `scheduler-control.yml`: `unitka-engine`, shadow, resume | ежедневно `SHADOW_DIFF` с `cells_planned` ≈ новый день (≈ 200–250) и `SHADOW_MATCH` после ручных загрузок |
| 9 | промоушен в prod | `deploy-prod.yml` с digest из шага 6 | шаг «Deploy unitka-engine-prod» выполнен |
| 10 | **controlled write-day** | `scheduler-control.yml`: `unitka-engine`, prod, run-now (в окно после 09:35 МСК) | журнал `mode=WRITE`, `qa_status=PASS`, `cells_written = cells_planned`; книга: новый день заполнен, MTD/сводка пересчитаны |
| 11 | автоматика | `scheduler-control.yml`: `unitka-engine`, prod, resume | 2–3 дня подряд `PASS` в 10:00 и `PASS` с `cells_written=0` в 12:30 |
| 12 | `UNITKA ENGINE v1 PRODUCTION READY = YES` | — | ручные `s82data/s8rates/s8brates/s82lcd` больше не запускаются |

Запись включается только сочетанием `ENVIRONMENT=prod` **и** `UNITKA_WRITE_ENABLED=1`
(выставлено Terraform'ом у `unitka-engine-prod`). Shadow-Job получает readonly-scope Sheets
и физически не может писать.

## 2a. Типы изменений в журнале и diff plan

`qa_json.plan.by_change_type`: `FACT_CHANGE` (новый закрытый день), `LATE_SOURCE_CORRECTION`
(источник пересчитал уже закрытый в книге день), `MODEL_PARAMETER_REFRESH` (ставки rolling-окна),
`LCD_ADVANCE`, `NO_CHANGE` (формула-наследие → то же значение). Штатное утро: `FACT_CHANGE` ≈ 200,
`LCD_ADVANCE` 2, остальное 0; `LATE_SOURCE_CORRECTION` > 0 — WB пересчитал прошлые дни, это
нормально, но каждую такую ячейку видно в `sample`/diff.

**Формат (E3, `unitka-engine/1.1.0`):** `plan.format_cells_planned` / `format_cells_written` —
ячейки закрытых дней, приведённые к контракту формата (эталон — строка 737 того же блока:
заливка, шрифт, числовой формат; будущие дни и GAP не трогаются). Штатное утро: новый день ×
11 колонок × 24 блока (за вычетом GAP) — так закрытая дата теряет «будущий» серый вид. Гейт
`CLOSED_FORMAT_CONTRACT` после записи должен быть 0.

## 3. Ежедневная норма (что видно в журнале)

```sql
SELECT started_at, environment, mode, last_closed_date, cells_planned, cells_written, qa_status, error_code
FROM `wb_ops.UNITKA_ENGINE_RUNS` ORDER BY started_at DESC LIMIT 10;
SELECT * FROM `wb_mart.V_UNITKA_ENGINE_STATUS`;
```

| окно МСК | ожидание |
|---|---|
| 10:00 | `PASS`, `last_closed_date = D-1`, `cells_planned` ≈ 24 блока × 9 факт + ставки, если окно сдвинуло их; `format_cells_written` ≈ 24 × 11 минус GAP |
| 12:30 | `PASS`, `cells_planned = 0` (те же источники) — либо закрытие дня, если утром воронка опоздала |
| любое | `lag_days = 1` штатно (воронка догружается 09:30); `2` — витрина или воронка опоздали на сутки |

Отставание LCD > 2 суток → `SOURCE_STALE` и алерт — это сигнал про источник, не про Engine.

⚠ Хранение (`storage`) не гейтит LCD. Production-загрузчик — **Stage E4, commit `230c64c`**
(`wb-paid-storage-prod`: окно 8 закрытых суток, атомарная замена в `RAW_WB_PAID_STORAGE`); Engine
читает `V_WB_STORAGE_DAILY`. До его первого прогона дни после 09.09 остаются GAP (пусто, не ноль).

## 4. Коды ошибок и действия

| `error_code` | что случилось | действие |
|---|---|---|
| `SOURCE_STALE` | LCD отстаёт от D-1 > `UNITKA_MAX_LAG_DAYS` | проверить `wb-funnel-prod` и `wb-mart-prod` (`LOADER_RUNS`, `MART_RUNS`); Engine ничего не пишет, догонит сам |
| `SHEETS_API` | 403/404/5xx от Sheets | 403 — доступ SA к книге / Sheets API не включён; 404 — id книги/имя листа; повтор — следующее окно |
| `STRUCTURE_DRIFT` | нет 24 nmID в строке 735, даты не совпадают, пропали формулы расчётных колонок или сводки | кто-то менял Master; восстановить структуру, не «подстраивать» Engine |
| `MONTH_SECTION_MISSING` | секции месяца LCD нет в колонке A (Engine 2.0.0) | подготовить месяц: `unitka-month-prep` (§5c); Engine месяц не создаёт |
| `MONTH_SECTION_AMBIGUOUS` | заголовок месяца встречается дважды | убрать дубль заголовка вручную; записи не было |
| `MONTH_SECTION_INVALID` | секция не проходит контракт (даты, шапка, MTD, nmID, сетка) | сверить секцию с `docs/UNITKA_CALENDAR_V2.md` §2; не чинить Engine'ом |
| `MONTH_ROLLOVER_REQUIRED` | (история, Engine ≤ 1.2.0) LCD в другом месяце, чем блоки | в 2.0.0 не возникает — см. `MONTH_SECTION_*` |
| `LCD_REGRESSION` | BQ LCD раньше, чем в книге | источник откатился (перестроена витрина/воронка) — разобраться с источником |
| `DUP_KEY` / `BLOCK_MISSING` | вью отдала дубль `nm×date` / у блока нет строк | `REF_SKU_MASTER` (active), состояние вью |
| `INVARIANT_FAIL` | `shipments ≠ sales + refusals` или пустое окно финансов | `V_WB_FINANCE_CANONICAL` за окно `[LCD−29, LCD]`; смешанные схемы внутри `srid` |
| `FUTURE_LEAKAGE` | непустой факт за датой > LCD (кроме формульной проекции остатка — KEEP) | кто-то внёс факт руками в будущий день или формула хранения даёт значение — очистить или дождаться закрытия дня |
| `BQ_MISMATCH` / `FORMULA_ERROR` / `SUMMARY_MISMATCH` / `LCD_INCONSISTENT` / `PARTIAL_WRITE` | пост-записной QA не сошёлся | запись уже применена одним batch; `qa_json` показывает ячейки; следующий прогон при тех же источниках ничего не меняет, при исправленных — перепишет |
| `FORMAT_CONTRACT` | после записи закрытые ячейки со значением отформатированы не как эталон строки 737 | `qa_json` показывает ячейки; чаще всего кто-то переформатировал строку 737 или закрытые дни вручную — вернуть эталон, следующий прогон приведёт остальное |
| `ENGINE_ERROR` | всё остальное | логи execution (`loader_failed`) |

## 5. Откат и стоп

* Стоп автоматики: `scheduler-control.yml` → `unitka-engine`, prod, pause (оба окна).
* Откат значений: Engine пишет только значения факт-ячеек закрытых дней, ставок и LCD; их
  можно перезаписать повторным прогоном (источники) или вручную. Apps Script-функции отката
  Stage 8/8.2 (`s82datarollback`, `s8brollback`, `s82lcdrollback`) остаются в репозитории.
* Engine никогда не трогает формулы расчётных колонок, УФ и структуру — откатывать там нечего.

## 5a. ⚠ DRY_RUN=1 не означает «без записи»

`unitka` — не `prodOnly`-загрузчик: `DRY_RUN=1` пропускает lease, но вызывает настоящий handler. На
`unitka-engine-prod` (`ENVIRONMENT=prod`, `UNITKA_WRITE_ENABLED=1`) такой прогон **пишет в книгу**. Прогон без
записи — только `unitka-engine-shadow` (readonly-scope Sheets) или `UNITKA_WRITE_ENABLED=0`.

## 5b. Integrity Guard V1 (`UNITKA_INTEGRITY_MODE`)

Полный контракт — `UNITKA_INTEGRITY_GUARD_V1.md`. Кратко для оператора:

* **DATA_ERROR ≠ падение Job'а.** Смотреть `JSON_VALUE(qa_json, '$.integrity.status')` в `UNITKA_ENGINE_RUNS`
  и лог-событие `unitka_integrity`; `qa_status` по-прежнему про запись и сверку.
* Режим задаёт **deploy-workflow**: `deploy-shadow.yml` → shadow `observe`; prod не задаётся → `off`.
  Terraform env у Job'ов игнорирует (`ignore_changes`) — правка `unitka_engine.tf` режим не меняет.
* Канон COGS Guard читает из физической копии `wb_mart.UNITKA_COGS_EFFECTIVE`; её публикует
  `sa-unitka-cogs-pub` (Scheduler `unitka-cogs-publication`, :50 07–23 МСК). Штатная свежесть ≈ 1 ч;
  `COGS_SNAPSHOT_STALE` — после 26 ч без успешной публикации; `COGS_SNAPSHOT_UNAVAILABLE` — копию не прочитать.
  Оба — WARNING, вердиктов COGS в прогоне нет. Проверка публикации:
  ```sql
  SELECT started_at, status, rows_published, error_message FROM `wb_mart.UNITKA_COGS_PUBLISH_LOG`
  ORDER BY started_at DESC LIMIT 5;
  ```
* COGS 252442517: канон 231,38 ₽; 240 ₽ в листе — известное неверное значение, исправление отложено.
* Журнал issue, алерты, УФ — отложены; следующий приоритет после 1C3 — октябрьский rollover.

## 5c. Calendar V2: подготовка месяца (`unitka-month-prep`) — НЕ активировано

Документ: `docs/UNITKA_CALENDAR_V2.md`. Суточный Engine месяц не создаёт; за ≤ 5 дней до конца месяца он пишет
предупреждение `NEXT_MONTH_SECTION_MISSING`, если следующего месяца нет.

* План (без записи): `node dist/cli.js unitka-month-prep` — лог `unitka_month_prep_plan` (статус, строки, слоты,
  COGS новых блоков с происхождением).
* Запись — только решение владельца: `ENVIRONMENT=prod`, `UNITKA_MONTH_PREP_WRITE=1`,
  `UNITKA_MONTH_PREP_TARGET=YYYY-MM`. Повтор — `NO_CHANGE`.
* Отказы: `COGS_MISSING`/`COGS_STALE`/`COGS_UNAVAILABLE` (канон COGS нового SKU), `TEMPLATE_MISMATCH`,
  `PREDECESSOR_*`, `SHEET_TAIL_NOT_EMPTY`, `MONTH_SECTION_PARTIAL/INVALID` — 0 изменений листа.
* SKU выводить из `REF_SKU_MASTER` только с 1-го числа: посреди месяца Engine упадёт `BLOCK_MISSING` (V1).
* Внешний вид нового месяца — визуальный контракт `visual.ts` (`docs/UNITKA_CALENDAR_V2.md` §8): пустые будущие
  дни без заливки, заливка закрытого дня и выходные — правилами УФ месяца; правила прошлого месяца не копируются.
* Откат созданного месяца: `planMonthRollback` (`monthprep_struct.ts`) — удалить правила УФ новой секции, добавленные
  колонки (если пусты выше секции) и строки секции; затем сверить книгу с предснимком (Phase 2C).
* Дополнительные отказы Phase 2C: `UNSUPPORTED_LOCALE`, `STRUCTURE_UNAVAILABLE`, `TEMPLATE_FORMATS_UNAVAILABLE`.
* Отказы hardening 2: `CHAIN_GAP` (выбывший SKU оставил бы дыру в сплошной цепочке блоков — решение владельца),
  `CF_TRIM_UNSAFE` (правило УФ до последней колонки блоков ссылается на другой лист — не переиздаём),
  `ANCHOR_UNRESOLVED` (именованный диапазон `REVERSE_LEG_RATE` не одна ячейка строки 737 листа Unitka; Engine и
  подготовка месяца находят колонку якорей только по нему — после вставки блока 25 это WZ, а не WB).

## 6. Офлайн SHADOW без облака

```
cd cloud && npm run build
node scripts/unitka_offline_shadow.mjs <snapshot.json> <bq.json>
```
Формат входов и пример результата — `UNITKA_ENGINE_V1_SHADOW_2026-09-12.md`.

## 7. Что не входит в v1

Создание октября в живой книге (Phase 2C/2D), планирование (строка 768), XLSX в production, изменение Master.
