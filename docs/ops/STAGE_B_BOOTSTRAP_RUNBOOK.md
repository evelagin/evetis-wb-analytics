# Stage B Recovery — bootstrap рекламного биллинга: инструкция на три прохода

**Дата:** 2026-09-08 · **Статус: PASS 1 НЕ ВЫПОЛНЕН — BLOCKED, требуется действие владельца**
**Production не менялся. Ни одного DDL/DML в этом этапе не выполнено.**

Контекст и корневая причина: `docs/ops/STAGE_B_CUTOVER_BLOCKED_2026-09-08.md`.
Этот файл — операционная часть: что именно нажать, чего не нажимать и как
доказать результат.

---

## 1. Почему проход не может быть запущен из репозитория

`loadWbAdsCostsBootstrapPass()` — функция Google Apps Script в проекте владельца,
привязанном к таблице (`SpreadsheetApp.getActiveSpreadsheet()` в
`loadWbAdsCostsRaw`). Автоматического пути её вызова отсюда нет, и это проверено,
а не предположено:

| Проверка | Результат |
|---|---|
| `.clasp.json` / script ID в репозитории | отсутствует; ни одного упоминания `script.google.com` |
| Scope'ы активного токена `gcloud` | `cloud-platform`, `compute`, `appengine.admin`, `sqlservice.login`, `openid`, `email` — **ни Drive, ни Sheets, ни Apps Script** |
| Drive API с этим токеном | HTTP 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT` |
| `script.googleapis.com` / `sheets` / `drive` в GCP-проекте | **не включены** — пути `scripts.run` не существует в принципе |
| Альтернативный загрузчик `adv/v1/upd` в `cloud/` или `pipelines/` | нет ни одного |

Ручная эмуляция функции SQL-запросами запрещена по существу, а не формально:
commit-marker, сочинённый в обход загрузчика, — это подделка доказательства
полноты, ровно того свойства, ради которого весь снапшот-слой и построен.

**Проход выполняет владелец. Из репозитория проверяется результат.**

---

## 2. PASS 1 — что сделать

Функции **нет в меню**: `addWbAdsRawLoaderMenu()` выводит только operational и
audit. Bootstrap запускается из редактора Apps Script.

1. Открыть проект Apps Script рабочей таблицы → файл `WbAdsRawLoader.gs`.
2. В выпадающем списке функций выбрать **`loadWbAdsCostsBootstrapPass`**.
3. Нажать «Выполнить». Подтвердить авторизацию, если запросит.
4. Дождаться завершения и **сохранить Execution log целиком** — это первичное
   доказательство прохода.

Что произойдёт: чтение `adv/v1/upd` за 13.04.2026 … D−1 окнами по 30 суток —
**5 окон** (148 суток), пауза 1,2 с между окнами. На каждое окно: данные →
затем строка журнала (commit-marker). Порядок именно такой: окно, упавшее между
данными и маркером, останется невидимым для canonical — это заложенный
fail-closed, а не сбой.

Ожидаемо в логе — по строке на окно:
```
raw_costs 2026-04-13…2026-05-12 [w0] OK | строк N | суток D | вне окна 0 | ...мс
```
и итог `status=OK`, `failed_windows=0`, `runlog_failed=0`.
`status=PARTIAL` — это отказ прохода, а не «почти получилось»: пересчитать
причину по `error_message`, проход повторить в следующие сутки.

### Почему проход безопасен для витрины

Production-canonical `V_ADV_COSTS` = `V_ADV_COSTS_UNION_PREBOOTSTRAP`, и эта вью
**исключает `ADSBACKFILL_` по префиксу run_id** (проверено в живом
`view_definition` 2026-09-08). Bootstrap только дописывает строки; ни одна
существующая не меняется и не удаляется. Экономика, вклад, ДРР и дашборды
поэтому не двигаются — ни во время прохода, ни между проходами.

---

## 3. Три ловушки

🔴 **1. Не запускать `wbAdsBqCreateViews()`.**
Она безусловно делает `CREATE OR REPLACE VIEW V_ADV_COSTS` дедупом по всей
таблице `RAW_WB_ADV_COSTS` **без фильтра по префиксу рана**
(`apps-script/WbAdsBigQuery.gs:331`). Сегодня это безвредно — `UNION_LEGACY` и
`UNION_PREBOOTSTRAP` совпадают (2 089 строк / 536 457 ₽ обе). **С первой же
секунды после PASS 1 — уже нет:** вызов отменит шаг A5 и втянет
bootstrap-строки в production-экономику, молча завысив `FACT_ADS_COSTS_DAILY`.
Защиты в коде нет. Если вью всё же пересоздали — вернуть одной строкой:
```sql
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS` AS
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP`;
```
Контрольная сумма правильного тела:
`0699a82ed08d0bc676ef13265fed8eb5e3b88f7bbcf08eb05d6f64075c2968af`.

🔴 **2. Не запускать два прохода в одни сутки.**
Из docstring функции: проходы внутри одного дня не независимы — WB публикует
списания в течение суток, и три чтения подряд измерили бы один момент, а не три
наблюдения. `billed_complete` требует `stable_ok` — совпадения `N_STABLE`
последовательных чтений; сжать это в один день нельзя, не разрушив смысл
проверки. Ровно поэтому `day_lost` после PASS 1 **не обязан** стать 0.

🔴 **3. Не менять `WB_ADS_COSTS_OPERATIONAL_DAYS_` до конца bootstrap.** См. §5.

---

## 4. Проверка после каждого прохода

Запускать `A1` и `P1` из `sql/raw/adv_costs_snapshot_validation.sql`, плюс:

```sql
-- ран должен быть ровно один новый, все его окна — с маркером
SELECT run_id, COUNT(*) AS windows, COUNTIF(status='OK' AND http_success='true') AS ok_windows,
       MIN(period_from) AS min_from, MAX(period_to) AS max_to,
       SUM(SAFE_CAST(returned_rows AS INT64)) AS rows_, SUM(SAFE_CAST(rows_out_of_window AS INT64)) AS oow
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
WHERE STARTS_WITH(run_id, 'ADSBACKFILL_') GROUP BY run_id ORDER BY run_id;

-- дублей commit-marker быть не может: (run_id, window_index) уникален
SELECT COUNT(*) AS duplicate_markers FROM (
  SELECT run_id, window_index FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
  GROUP BY 1,2 HAVING COUNT(*) > 1);

-- бизнес-семантика НЕ сдвинулась: обязано совпасть с baseline до копейки
SELECT COUNT(*) AS rows_, ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)),2) AS rub
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`;
```

**Baseline 2026-09-08 до PASS 1 — с чем сверять:**

| Показатель | Значение |
|---|---|
| `RAW_WB_ADV_COSTS` строк | 5 563 |
| из них с `window_index` | 537 |
| без `window_index` | 5 026 |
| строк журнала / committed windows | 20 / 20 |
| ранов `ADSBACKFILL_` | **0** |
| `V_ADV_COSTS_SNAPSHOT` | 116 строк, 13.08 … 07.09, 28 684 ₽ |
| `V_ADV_COSTS` (production) | 2 089 строк, 13.04 … 07.09, **536 457 ₽** |
| I5 | `day_lost 122 · day_new 0 · day_changed 1 · total_delta −507 773` |
| I6 | settled 10 · unsettled 138 · not_loaded 122 · violations 0 |
| помесячно `V_ADV_COSTS` ₽ | 04: 96 009 · 05: 140 347 · 06: 116 970 · 07: 128 653 · 08: 44 842 · 09: 9 636 |

После каждого прохода **`V_ADV_COSTS` обязана остаться 536 457 ₽** (плюс штатный
суточный прирост от `ADSRAW_`). Любое другое число означает, что bootstrap
протёк в бизнес-семантику — остановиться и разобрать причину.

Растёт от прохода к проходу: `stable_reads` и `settled_days` в P1.
Гейт закрытия — `day_lost = 0` в I5 после третьего прохода.

---

## 5. B4a — окно operational-чтения 7 → 14

| | |
|---|---|
| Где задаётся | `apps-script/WbAdsRawLoader.gs:112` — `var WB_ADS_COSTS_OPERATIONAL_DAYS_ = 7;` |
| Кто читает | `WbAdsRawLoader.gs:333` (`loadWbAdsCostsRawOperational`), `WbAdsDaily.gs:153` |
| Текущее значение | **7** (D−7 … D−1) |
| Предлагаемое | **14** (D−14 … D−1) |
| Зачем | наблюдавшаяся ревизия биллинга пришла на D+7 — ровно на границе окна. «Стабильность» дальше была артефактом того, что мы перестали спрашивать |

**Blast radius, если применить сейчас.** Ежедневные раны `ADSRAW_` попадают в
`V_ADV_COSTS_UNION_PREBOOTSTRAP` — то есть в действующую production-экономику.
Union умеет только расти: удвоение окна перечитывания добавит ключи
`(advertId, updTime, updSum)` за сутки 8–14 и **поднимет суммы `FACT` ещё до
всякого cutover**. Фаза A перестанет быть «без смены бизнес-семантики», а I5
потеряет смысл: она сверяет новый canonical против прежнего FACT, и сдвинуть
прежний FACT посреди проверки — значит сломать сам гейт.

**Ответ: B4a выполняется ПОСЛЕ завершения bootstrap, внутри Фазы B, между
шагами B3 (I1–I9, `day_lost = 0`) и B4b (переключение `V_ADV_COSTS`).**
Не до PASS 2/3 и не между проходами. Так и записано в
`docs/ADS_COSTS_SNAPSHOT_ROLLOUT_2026-08-20.md` §3 и §3.1(2).

---

## 6. График

| Шаг | Когда | Кто |
|---|---|---|
| PASS 1 + A1/P1 | сутки 1 | владелец |
| PASS 2 + A1/P1 | сутки 2 (другой календарный день) | владелец |
| PASS 3 + A1/P1/X1 | сутки 3 | владелец |
| I5: `day_lost` = 0 | после PASS 3 | гейт |
| B4a, затем Фаза B (B1–B10), затем Stage 3B | далее | — |

Пропущенный день не ломает набор — он только сдвигает окно. Повторный проход
в те же сутки его не заменяет.
