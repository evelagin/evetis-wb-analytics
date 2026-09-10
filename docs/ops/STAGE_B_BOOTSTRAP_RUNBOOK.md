# Stage B Recovery — bootstrap рекламного биллинга: инструкция на проходы

**Дата:** 2026-09-08 · **Статус: PHASE A CLOSED (2026-09-10) — GO FOR STAGE B
CUTOVER PRECHECK.** Cutover не выполнен; последовательность — §10
**Журнал проходов и доказательства:** `docs/ops/STAGE_B_BOOTSTRAP_PASSES.md`
**Ревизия:** 2 (уточнены production invariant §5, completion gate §6, порядок проверок §4)
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
поэтому не двигаются от самого прохода — их штатный суточный прирост от ранов
`ADSRAW_` продолжается как обычно и загрязнением не является (§5).

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
проверки. Ровно поэтому `day_lost` после PASS 1 **не обязан** стать 0, а три прохода —
минимум наблюдений, а не доказательство завершения (§6).

🔴 **3. Не менять `WB_ADS_COSTS_OPERATIONAL_DAYS_` до прохождения completion
gate.** Не «до третьего запуска», а именно до гейта — см. §6 и §7.

---

## 4. Последовательность проверок

Порядок фиксирован. Пропущенная проверка — это не сэкономленное время, а
непроверенный проход: доказательства снимаются на том состоянии, которое
существовало сразу после прохода, и восстановить их задним числом нельзя.

| После | Что запускать |
|---|---|
| **PASS 1** | `A1` → `P1` → **production invariant** (§5) |
| **PASS 2** | `A1` → `P1` → **production invariant** (§5) |
| **PASS 3** | полный validation pack: `A1` → `P1` → `I1`–`I9` → `X1` → production invariant (§5) → completion gate (§6) |
| **каждый дополнительный проход** | как PASS 3 — полный pack |

`A1`, `P1`, `I1`–`I9` и `X1` — из `sql/raw/adv_costs_snapshot_validation.sql`,
запускать дословно, без правки тел запросов.

* **`A1`** — монитор Фазы A: помесячные суммы `V_ADV_COSTS` и лаг `FACT`.
  Доказывает две вещи сразу: bootstrap не протёк в бизнес-семантику и штатный
  поток не замер.
* **`P1`** — динамика покрытия: `settled` / `unsettled` / `not_loaded`,
  `stable_reads` (min/avg/max), ревизии, `needs_recheck`, `day_lost` / `day_new`.
  Смысл в сравнении проходов между собой, а не в одном срезе.
* **`I1`–`I9`** — инварианты снапшот-слоя: строки без маркера, единственность
  окна внутри `(run_id, date)`, уникальность `(advertId, updTime)`, единственный
  источник даты, сверка с `FACT` (`I5`), корректность `billed_complete` (`I6`),
  лаг ревизий против `SETTLE_DAYS` (`I7`), нулевые дни без доказательства (`I8`),
  `repo == production` (`I9`).
* **`X1`** — **назначение**: проверка не суммы, а *механизмов*. Она берёт четыре
  даты с известной природой расхождения — `2026-07-10` REVOKED_RECORD (−77 ₽),
  `2026-08-02` OUT_OF_WINDOW (−61 ₽), `2026-08-04` OUT_OF_WINDOW (−4 ₽),
  `2026-08-05` TRANSIENT_SPIKE (−192 ₽) — и показывает, что снапшот-семантика
  лечит каждый из трёх дефектов union'а, а не даёт похожий итог случайно.
  🔴 `matches_expectation = FALSE` **не дефект сам по себе**: величины −334 ₽
  измерены симуляцией 20.08.2026, а bootstrap читает WB заново, и WB вправе
  ответить иначе. Расхождение означает «разобрать причину», а не «переписать
  ожидание». Дефект — если механизм не сработал: старый union по этим датам
  остался равен новому canonical.
* **`needs_recheck`** из `P1` — список дат в Script Property
  `WB_ADS_COSTS_RECHECK_DATES`, затем `loadWbAdsCostsRecheck()` отдельным
  проходом. Это ручной шаг, автоматической обратной связи BQ → Apps Script в
  проекте нет.

Дополнительно после каждого прохода:

```sql
-- новый ран ровно один, все его окна с маркером, статус OK
SELECT run_id, COUNT(*) AS windows, COUNTIF(status='OK' AND http_success='true') AS ok_windows,
       MIN(period_from) AS min_from, MAX(period_to) AS max_to,
       SUM(SAFE_CAST(returned_rows AS INT64)) AS rows_,
       SUM(SAFE_CAST(rows_out_of_window AS INT64)) AS oow
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
WHERE STARTS_WITH(run_id, 'ADSBACKFILL_') GROUP BY run_id ORDER BY run_id;

-- дублей commit-marker быть не может: (run_id, window_index) уникален
SELECT COUNT(*) AS duplicate_markers FROM (
  SELECT run_id, window_index FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS_RUNS`
  GROUP BY 1,2 HAVING COUNT(*) > 1);

-- RAW только растёт: ни одна строка прежних ранов не исчезла
SELECT COUNT(*) AS raw_rows, COUNT(DISTINCT run_id) AS runs
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`;
```

---

## 5. Production invariant — что именно обязано не измениться

🔴 **Числа `2 089 строк` и `536 457 ₽` — это baseline на 2026-09-08, а НЕ
критерий приёмки.** Штатные суточные раны `ADSRAW_` продолжают идти всё время
bootstrap и законно двигают обе величины. Требовать их неизменности — значит
запрограммировать ложную тревогу на второй же день и приучить себя её
игнорировать ровно тогда, когда она понадобится по-настоящему.

Инвариант формулируется так:

> **После каждого bootstrap-прохода production `V_ADV_COSTS` обязана
> по-прежнему читать только `V_ADV_COSTS_UNION_PREBOOTSTRAP`, а строки
> `run_id LIKE 'ADSBACKFILL_%'` — не влиять на рекламную экономику
> до контролируемого cutover Stage 3B.**
>
> Изменение числа строк или суммы `V_ADV_COSTS` относительно предыдущего
> baseline допускается **только** за счёт штатных operational-ранов `ADSRAW_`,
> выполненных после baseline-отметки, и подлежит отдельной сверке.

### 5.1 Жёсткие проверки — ожидается точное значение

```sql
-- И1. Тело production-вью не менялось.
SELECT TO_HEX(SHA256(view_definition)) AS sha256,
       TO_HEX(SHA256(view_definition)) =
       '0699a82ed08d0bc676ef13265fed8eb5e3b88f7bbcf08eb05d6f64075c2968af' AS body_unchanged
FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.VIEWS`
WHERE table_name = 'V_ADV_COSTS';
-- ожидается body_unchanged = TRUE

-- И2. 🔑 ГЛАВНЫЙ ГЕЙТ: вклад небоевых контуров в production canonical = 0.
SELECT
  COUNTIF(STARTS_WITH(run_id,'ADSBACKFILL_')) AS backfill_rows,
  COUNTIF(STARTS_WITH(run_id,'ADSAUDIT_'))    AS audit_rows,
  COUNTIF(STARTS_WITH(run_id,'ADSRECHECK_'))  AS recheck_rows
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`;
-- ожидается 0 / 0 / 0. Любое ненулевое значение = contamination, СТОП.

-- И3. RAW не уменьшилась.
SELECT COUNT(*) AS raw_rows FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_COSTS`;
-- ожидается >= значения предыдущего замера, никогда меньше
```

### 5.2 Сверка прироста — не равенство, а объяснимость

```sql
-- Р1. Разложение canonical по контурам чтения.
SELECT COALESCE(REGEXP_EXTRACT(run_id, r'^(ADS[A-Z]+_)'), '(legacy)') AS contour,
       COUNT(*) AS rows_,
       ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)),2) AS rub,
       MIN(load_ts) AS min_load_ts, MAX(load_ts) AS max_load_ts
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`
GROUP BY 1 ORDER BY 1;

-- Р2. Прирост относительно baseline-отметки.
SELECT
  COUNT(*) AS rows_total,
  ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)),2) AS rub_total,
  COUNTIF(SAFE_CAST(load_ts AS TIMESTAMP) > TIMESTAMP '2026-09-08 05:08:03') AS rows_after_baseline,
  ROUND(SUM(IF(SAFE_CAST(load_ts AS TIMESTAMP) > TIMESTAMP '2026-09-08 05:08:03',
               SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC), 0)),2) AS rub_after_baseline
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`;

-- Р3. Посуточная дельта: какие даты сдвинулись и на сколько.
--     Baseline-срез снять ЭТИМ ЖЕ запросом ДО PASS 1 и сохранить.
SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(updDate,1,10)) AS d,
       COUNT(*) AS rows_,
       ROUND(SUM(SAFE_CAST(REPLACE(updSum,',','.') AS NUMERIC)),2) AS rub
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`
GROUP BY d ORDER BY d;
```

**Правило разбора Р3.** Каждая сдвинувшаяся дата обязана попадать в
operational-окно (`D−WB_ADS_COSTS_OPERATIONAL_DAYS_ … D−1`) какого-либо рана
`ADSRAW_`, выполненного **после** baseline-отметки. Такой сдвиг — штатный
прирост биллинга, его записывают в сверку и идут дальше.
Сдвиг даты **вне** этих окон объяснить штатным потоком нельзя: остановиться и
разобрать причину, не продолжая проходы.

**Что НЕ является contamination.** Перетекание строк между контурами при
неизменном итоге — нормальная работа дедупа: ключ union'а
`(advertId, updTime, updSum)`, порядок `load_ts DESC`, поэтому свежее чтение
`ADSRAW_` вытесняет строку `ADSPROBE_` с тем же ключом. Число строк
`ADSPROBE_` при этом уменьшается, `ADSRAW_` растёт, сумма не меняется.
Классифицировать это как загрязнение — ошибка в обратную сторону.

### 5.3 Baseline 2026-09-08 (снят до PASS 1)

| Показатель | Значение |
|---|---|
| baseline-отметка (последний журналированный ран) | `2026-09-08 05:08:03` (`ADSRAW_20260908_050726_303`) |
| `V_ADV_COSTS` тело, sha256 | `0699a82ed08d0bc676ef13265fed8eb5e3b88f7bbcf08eb05d6f64075c2968af` |
| `V_ADV_COSTS` всего | 2 089 строк · 536 457 ₽ · 13.04 … 07.09 |
| — контур `ADSPROBE_` | 1 644 строки · 437 623 ₽ · до `2026-08-20 15:40:32` |
| — контур `ADSRAW_` | 445 строк · 98 834 ₽ · до `2026-09-08 05:08:00` |
| — `ADSBACKFILL_` / `ADSAUDIT_` / `ADSRECHECK_` | **0 / 0 / 0** |
| `RAW_WB_ADV_COSTS` | 5 563 строки (537 с `window_index`, 5 026 без), 57 ранов |
| журнал окон | 20 строк / 20 с маркером; ранов `ADSBACKFILL_` — **0** |
| `V_ADV_COSTS_SNAPSHOT` | 116 строк · 28 684 ₽ · 13.08 … 07.09 |
| `I5` | `day_lost 122 · day_new 0 · day_changed 1 · total_delta −507 773` |
| `I6` | settled 10 · unsettled 138 · not_loaded 122 · violations 0 |
| помесячно `V_ADV_COSTS`, ₽ | 04: 96 009 · 05: 140 347 · 06: 116 970 · 07: 128 653 · 08: 44 842 · 09: 9 636 |

Помесячные суммы `A1` сверяются с этой строкой **с поправкой на штатный прирост
текущего месяца**, а не на точное равенство.

---

## 6. Completion gate — когда bootstrap считается завершённым

🔴 **Три прохода — минимально необходимое число независимых наблюдений, но НЕ
автоматическое доказательство завершения.** «Запустили три раза» и «покрытие
достигнуто» — разные утверждения, и подменять второе первым нельзя: WB мог
ответить неполно, окно могло упасть, сутки могли не набрать `N_STABLE`.

Bootstrap завершён только когда выполнено **всё** перечисленное:

1. PASS 1 / 2 / 3 выполнены в **разные календарные сутки**;
2. полный validation pack выполнен (§4, строка PASS 3);
3. **`I5.day_lost = 0`**;
4. **`I5.day_new = 0`** — либо контракт объясняет иное, и объяснение записано;
5. каждая дата из `day_changed` разобрана и объяснена механизмом;
6. проверки стабильности и покрытия PASS: `I6.violations = 0`,
   `I7.max_revision_lag_days < contract_settle_days`, `I8 = 0`,
   `P1.needs_recheck = 0`, `billed_complete` покрывает расчётный период;
7. аномалий нет: `I1 = 0`, `I2 = 0`, `I3 = 0`, `I4 = 0`, дублей commit-marker 0,
   RAW не уменьшилась, production invariant §5 держится.

### Если после PASS 3 гейт не пройден

`I5.day_lost > 0` или любой другой обязательный пункт не выполнен:

* **Фаза A остаётся OPEN;**
* Stage 3B **не разворачивать**;
* B4a **не выполнять**;
* выполнить **дополнительный bootstrap-проход в следующий календарный день**;
* после него — снова полный pack (§4) и снова §6;
* повторять до фактического прохождения гейта.

Чего делать нельзя ни при каких обстоятельствах: понижать `N_STABLE`, править
timestamps, редактировать журнал, ослаблять правила покрытия, объявлять пункт
гейта неприменимым ради продвижения. Гейт не пройден — значит наблюдений пока
не хватает, а не значит, что гейт неправильный.

---

## 7. B4a — окно operational-чтения 7 → 14

| | |
|---|---|
| Где задаётся | `apps-script/WbAdsRawLoader.gs:112` — `var WB_ADS_COSTS_OPERATIONAL_DAYS_ = 7;` |
| Кто читает | `WbAdsRawLoader.gs:333` (`loadWbAdsCostsRawOperational`), `WbAdsDaily.gs:153` |
| Текущее значение | **7** (D−7 … D−1) |
| Предлагаемое | **14** (D−14 … D−1) |
| Зачем | наблюдавшаяся ревизия биллинга пришла на D+7 — ровно на границе окна. «Стабильность» дальше была артефактом того, что мы перестали спрашивать |

**Blast radius, если применить раньше срока.** Раны `ADSRAW_` попадают в
`V_ADV_COSTS_UNION_PREBOOTSTRAP`, то есть в действующую production-экономику.
Union умеет только расти: удвоение окна перечитывания добавит ключи
`(advertId, updTime, updSum)` за сутки 8–14 и **поднимет суммы `FACT` ещё до
всякого cutover**. Фаза A перестанет быть «без смены бизнес-семантики», а `I5`
потеряет смысл: она сверяет новый canonical против прежнего `FACT`, и сдвинуть
прежний `FACT` посреди проверки — значит сломать сам гейт.

**B4a разрешён только после фактического прохождения completion gate §6** — не
после третьего запуска как такового. Место в порядке работ: Фаза B, между B3
(`I1`–`I9`, `day_lost = 0`) и B4b (переключение `V_ADV_COSTS`). Не до PASS 2/3 и
не между проходами. Так же записано в
`docs/ADS_COSTS_SNAPSHOT_ROLLOUT_2026-08-20.md` §3 и §3.1(2).

---

## 8. График

| Шаг | Когда | Кто | Проверки |
|---|---|---|---|
| PASS 1 | сутки 1 | владелец | A1, P1, invariant §5 |
| PASS 2 | сутки 2 (другой календарный день) | владелец | A1, P1, invariant §5 |
| PASS 3 | сутки 3 | владелец | полный pack + gate §6 |
| PASS N | следующий календарный день, пока гейт §6 не пройден | владелец | полный pack + gate §6 |
| B4a | только после прохождения гейта §6 | — | отдельный коммит |
| Фаза B (B1–B10), затем Stage 3B | далее | — | по роллауту |

Пропущенный день не ломает набор — он только сдвигает окно. Повторный проход в
те же сутки его не заменяет и не засчитывается.

---

## 9. Что запрещено на всё время bootstrap

* запускать `wbAdsBqCreateViews()` (§3, ловушка 1);
* выполнять больше одного bootstrap-прохода в одни календарные сутки;
* выполнять B4a между проходами или до прохождения гейта §6;
* разворачивать Stage 3B до прохождения гейта §6;
* создавать commit-marker вручную;
* делать synthetic backfill через SQL — любой `INSERT`/`MERGE` в
  `RAW_WB_ADV_COSTS` или `RAW_WB_ADV_COSTS_RUNS` мимо загрузчика;
* менять `V_ADV_COSTS` — переключение делается один раз, шагом B4b.


---

## 10. Последовательность cutover — к исполнению, НЕ выполнена

Составлена из `docs/ADS_COSTS_SNAPSHOT_ROLLOUT_2026-08-20.md` §3 «Фаза B» и
`sql/mart/ads_spend_stage3b_validation.sql` §0 (редакция Stage 1.8 от 26.08 —
прежний порядок из пяти шагов устарел и приводил к молчаливой потере колонок
витрины).

Ничего из перечисленного на 2026-09-10 не выполнено.

### Precheck — read-only, ничего не меняет

| # | Что | Ожидание | Замер 2026-09-10 |
|---|---|---|---|
| P-1 | `I1`–`I9`, `P1`, инвариант §5 | всё PASS | **PASS** |
| P-2 | `I5.day_lost` | 0 | **0** |
| P-3 | `_MART_BOOTSTRAP_LOCK` по обоим `lock_id` | `is_running = FALSE` | **FALSE / FALSE** |
| P-4 | последний прогон mart | `COMPLETE` | **COMPLETE, 09.09** |
| P-5 | гейт Stage 3B | `FALSE` до B4b | **FALSE** |
| P-6 | `V_ADV_COSTS` sha256 | `0699a82e…2968af` | **совпал** |
| P-7 | зафиксировать B0-срез canonical и `FACT` | сохранён | **2 105 · 539 193 ₽ · FACT 539 193 ₽** |

Precheck повторяется **непосредственно перед** B1: между сегодняшним замером и
окном cutover пройдут штатные раны, и `_MART_BOOTSTRAP_LOCK` может быть занят.

### Фаза B — короткое окно, витрина на паузе

| # | Действие |
|---|---|
| **B1** | пауза загрузчика: workflow `scheduler-control.yml` → `environment: prod`, `loader: wb-mart`, `action: pause` |
| **B2** | убедиться: оба `_MART_BOOTSTRAP_LOCK.is_running = FALSE`, последний `mart` = `COMPLETE` |
| **B3** | `I1`–`I9` целиком, `I5.day_lost = 0` на `V_ADV_COSTS_SNAPSHOT` |
| **B4a** | `WB_ADS_COSTS_OPERATIONAL_DAYS_` 7 → 14 в `apps-script/WbAdsRawLoader.gs:112` — **отдельный коммит**, затем перенос файла в проект Apps Script |
| **B4b** | `CREATE OR REPLACE VIEW wb_raw.V_ADV_COSTS AS SELECT * FROM wb_raw.V_ADV_COSTS_SNAPSHOT;` |
| **B5** | `CALL wb_mart.sp_bootstrap_facts('');` — пересборка `FACT_ADS_COSTS_DAILY` |
| **B6** | сверка коррекции против B0 **на одной и той же сборке**: ожидается −300 ₽ ровно на `2026-07-12` (−108) и `2026-08-05` (−192) |
| **B7** | `X1` — механизмы. 🔴 Ожидания −334 ₽ переизмерить: они сняты симуляцией 20.08 на узких окнах, bootstrap читал WB окнами по 30 суток |
| **B8** | `CALL wb_mart.sp_build_mart_sku_daily(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');` |
| **B9** | `K9` по всем шести объектам + повтор `I4`–`I9` на пересобранном `FACT` |
| **B10** | снять паузу: `scheduler-control.yml` → `resume` |

### Stage 3B — переход экономики на биллинг

| # | Действие |
|---|---|
| **C** | проверить, что гейт открылся сам: `SELECT IFNULL(LOGICAL_OR(REGEXP_CONTAINS(view_definition, r'V_ADV_COSTS_SNAPSHOT')), FALSE) FROM wb_raw.INFORMATION_SCHEMA.VIEWS WHERE table_name='V_ADV_COSTS'` → `TRUE`. Файл `pr_mart1_facts.sql` **не править** |
| **D** | deploy `sql/mart/pr_mart1_facts.sql` — пересоздание `sp_bootstrap_facts` |
| **E** | `CALL wb_mart.sp_bootstrap_facts('');` — убедиться, что `FACT_ADS_SPEND_ALLOC_DAILY` и `FACT_ADS_SPEND_UNALLOC_DAILY` созданы и непусты, 15 fail-closed ASSERT §1.7/§1.8 прошли |
| **F** | восстановить Stage 3B блок в `sql/mart/pr_mart2b_sku_daily.sql` из `e30f668` **поверх** guard `fix #5` — guard обязан сохраниться |
| **G** | deploy `pr_mart2b_sku_daily.sql`, затем `CALL wb_mart.sp_build_mart_sku_daily(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');` |
| **H** | deploy `sql/mart/ads_spend_reconciliation_v1.sql` |
| **I** | deploy `sql/mart/ads4_funnel_v1.sql`, **сразу за ним** `sql/mart/dashboard_layer_v1.sql` — строго в этом порядке и в одной сессии: они переименовывают `mart_ad_spend_rub` → `mart_ad_spend_attributed_rub`, и порознь ломаются оба |
| **J** | приёмка: `sql/mart/ads_spend_stage3b_validation.sql` целиком |

🔴 Компиляцию проверять **постейтментно**: `dry_run` по целому файлу даёт ложный
успех — `dashboard_layer_v1.sql` проходит его целиком, но падает на изолированном
`CREATE OR REPLACE VIEW`.

### Откат

Одна строка в обе стороны:

```sql
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS` AS
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP`;
```
затем `CALL wb_mart.sp_bootstrap_facts('');`. Контрольная сумма правильного тела —
`0699a82ed08d0bc676ef13265fed8eb5e3b88f7bbcf08eb05d6f64075c2968af`.
