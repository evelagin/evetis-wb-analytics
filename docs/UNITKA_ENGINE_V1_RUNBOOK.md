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
storage`), колонки `logistics`/`commission` (30 строк блока), `REVERSE_LEG_RATE` (`WB737`),
`LAST_CLOSED_DATE` (имя + зеркало `WB736`). Не пишет: формулы, УФ, ширины, строку 767/768,
будущие дни, блогеров, `spp`. Любой FAIL → ничего не записано (план — один batch), `exit 1`.

## 2. Включение (по порядку, каждый шаг — владелец)

| # | шаг | где | проверка |
|---|---|---|---|
| 1 | развести чужой дрейф плана (5 in-place изменений, `ops_health.tf`) | `infra.yml` action=plan | план = только добавления Engine |
| 2 | `infra apply` | `infra.yml` action=apply | Job'ы `unitka-engine-shadow/prod`, 4 Scheduler'а (paused), таблица `wb_ops.UNITKA_ENGINE_RUNS` |
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

## 3. Ежедневная норма (что видно в журнале)

```sql
SELECT started_at, environment, mode, last_closed_date, cells_planned, cells_written, qa_status, error_code
FROM `wb_ops.UNITKA_ENGINE_RUNS` ORDER BY started_at DESC LIMIT 10;
SELECT * FROM `wb_mart.V_UNITKA_ENGINE_STATUS`;
```

| окно МСК | ожидание |
|---|---|
| 10:00 | `PASS`, `last_closed_date = D-1`, `cells_planned` ≈ 24 блока × 9 факт + ставки, если окно сдвинуло их |
| 12:30 | `PASS`, `cells_planned = 0` (те же источники) — либо закрытие дня, если утром воронка опоздала |
| любое | `lag_days = 1` штатно (воронка догружается 09:30); `2` — витрина или воронка опоздали на сутки |

Отставание LCD > 2 суток → `SOURCE_STALE` и алерт — это сигнал про источник, не про Engine.

## 4. Коды ошибок и действия

| `error_code` | что случилось | действие |
|---|---|---|
| `SOURCE_STALE` | LCD отстаёт от D-1 > `UNITKA_MAX_LAG_DAYS` | проверить `wb-funnel-prod` и `wb-mart-prod` (`LOADER_RUNS`, `MART_RUNS`); Engine ничего не пишет, догонит сам |
| `SHEETS_API` | 403/404/5xx от Sheets | 403 — доступ SA к книге / Sheets API не включён; 404 — id книги/имя листа; повтор — следующее окно |
| `STRUCTURE_DRIFT` | нет 24 nmID в строке 735, даты не совпадают, пропали формулы расчётных колонок или сводки | кто-то менял Master; восстановить структуру, не «подстраивать» Engine |
| `MONTH_ROLLOVER_REQUIRED` | LCD в другом месяце, чем блоки | ожидаемо с 02.10 — Engine v1.1 |
| `LCD_REGRESSION` | BQ LCD раньше, чем в книге | источник откатился (перестроена витрина/воронка) — разобраться с источником |
| `DUP_KEY` / `BLOCK_MISSING` | вью отдала дубль `nm×date` / у блока нет строк | `REF_SKU_MASTER` (active), состояние вью |
| `INVARIANT_FAIL` | `shipments ≠ sales + refusals` или пустое окно финансов | `V_WB_FINANCE_CANONICAL` за окно `[LCD−29, LCD]`; смешанные схемы внутри `srid` |
| `FUTURE_LEAKAGE` | значение (не формула) в факт-ячейке за датой > LCD | кто-то внёс факт руками в будущий день — очистить или дождаться закрытия дня |
| `BQ_MISMATCH` / `FORMULA_ERROR` / `SUMMARY_MISMATCH` / `LCD_INCONSISTENT` / `PARTIAL_WRITE` | пост-записной QA не сошёлся | запись уже применена одним batch; `qa_json` показывает ячейки; следующий прогон при тех же источниках ничего не меняет, при исправленных — перепишет |
| `ENGINE_ERROR` | всё остальное | логи execution (`loader_failed`) |

## 5. Откат и стоп

* Стоп автоматики: `scheduler-control.yml` → `unitka-engine`, prod, pause (оба окна).
* Откат значений: Engine пишет только значения факт-ячеек закрытых дней, ставок и LCD; их
  можно перезаписать повторным прогоном (источники) или вручную. Apps Script-функции отката
  Stage 8/8.2 (`s82datarollback`, `s8brollback`, `s82lcdrollback`) остаются в репозитории.
* Engine никогда не трогает формулы расчётных колонок, УФ и структуру — откатывать там нечего.

## 6. Офлайн SHADOW без облака

```
cd cloud && npm run build
node scripts/unitka_offline_shadow.mjs <snapshot.json> <bq.json>
```
Формат входов и пример результата — `UNITKA_ENGINE_V1_SHADOW_2026-09-12.md`.

## 7. Что не входит в v1

Октябрь и rollover (E1.1), планирование (строка 768), XLSX в production, изменение Master.
