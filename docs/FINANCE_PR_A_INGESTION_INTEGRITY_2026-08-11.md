# PR-A · Finance ingestion integrity — спецификация

**Ред. 4** от 11.08.2026 — **APPROVED FOR IMPLEMENTATION**.
Внесена последняя правка: A5 разбит на A5a–A5d, добавлен **условный full-scan fallback**
в pre-load guard с диагностикой в тексте ошибки.
*Ред. 3: разведены гарантии `jobId` и crash-recovery, pre-load orphan guard, эмпирическая природа
сверки `row_hash`, исполнимая форма инварианта RAW. Ред. 2: убрана материализация
`payload_hash`/`source_layer`, `run_id` в `jobId`, логическая ошибка в QC.*
Каждая правка подтверждена прогоном на данных.

**Порядок реализации:** A1 → A2/A3 → A5a → A5b → regression suite.

**Дата:** 11.08.2026 · **Статус:** проектирование, read-only · **Записей в production: ноль**
**Основание:** REV2.4 (артефакт `evetis-wb-finance-rev2`), APPROVED аудитором
**Порядок работ:** PR-A → PR-B (operation semantics) → PR-C (normalization enrichment)

---

## 0. Поправка к диагнозу REV2.4 — читать первым

При разборе кода выяснилось, что **два моих утверждения в REV2.4 были неточны**. На цифры это
не влияет, но меняет состав PR.

### 0.1 `row_hash` считается НЕ с ingestion-метаданными

Я писал: «`row_hash` уникален во всех строках, потому что включает `run_id`/`loaded_at`, и как ключ
дедупликации бесполезен». Проверено по коду и подтверждено на данных.

```js
// apps-script/WbFinanceDaily.gs:528
row_hash: finDailyMd5_(FIN_DAILY_SOURCE_API_ + '|' + ctx.reportId + '|' + rrd),
```

Фактическая формула — `MD5('WB_API_FIN_V1|' || report_id || '|' || rrd_id)`.

Сверка **эмпирическая, по сохранённым в таблице значениям** (не восстановление формулы из кода):
пересчитал хэш в SQL из `report_id` и `rrd_id` и сравнил с колонкой `row_hash`. Совпало на
**205 446 строках из 205 446** — отдельно у legacy-лоадера (201 211 из 201 211) и у нового
(4 235 из 4 235). То есть оба контура пишут `row_hash` по одной и той же формуле, несмотря на
разные функции (`WbFinanceDaily.gs:528` и `WbFinanceApiV1.gs:347` / `WbFinanceBackfillBQ.gs:218`).

Ingestion-метаданных в хэше нет. Уникальность даёт **`report_id`**: daily-отчёт и weekly-отчёт —
это разные отчёты WB, поэтому одна бизнес-строка получает два разных хэша.
**`row_hash` корректен для своего назначения** — он идентифицирует строку *отчёта*.
Менять его не нужно; нужен отдельный `payload_hash` для бизнес-строки.

### 0.2 Дубли — не баг реализации, а следствие задокументированного дизайна

Я назвал это «нарушением идемпотентности». Точнее: **RAW по проекту двухслойный append-only
журнал**, и это записано в проектном документе PR-Fin1:

> `docs/FINANCE_DAILY_DESIGN_2026-07-22.md:41` — «Weekly содержит те же rrdId»
> `:53-57` — «Weekly — FINAL всегда… Daily остаётся в RAW навсегда (аудит/reconciliation)»

Слой daily нужен таблице `FINANCE_WEEK_RECON`, которая сравнивает Σdaily против Σweekly по семи
метрикам. Физическое удаление daily-строк после weekly **сломает эту сверку**.

Собственный ключ RAW при этом **не нарушен**: `(report_id, rrd_id)` уникален на
**205 446 строках из 205 446**. Нарушено только ожидание внешнего потребителя, что уникален
`rrd_id`, — и именно на это ожидание я и наступил в REV1/REV2.

**Отсюда переформулировка дефекта B:** проблема не в том, что дубли есть, а в том, что
двухслойность нигде не объявлена как контракт и ничем не защищена:

| Чего нет | Последствие |
|---|---|
| ASSERT `COUNT(*) = COUNT(DISTINCT rrd_id)` на CANONICAL | если `weekly_final` не проставится, дубли протекут в витрину молча |
| QC на неожиданные дубли в RAW | дубль не-DAILY/WEEKLY природы никто не заметит |
| `payload_hash` без `report_id` | natural key `rrd_id` нечем контролировать |
| Заполненный `is_duplicate` | слой строки не читается из самой таблицы |
| Детерминированный `jobId` при загрузке | сетевой таймаут → третья копия отчёта |

**PR-A делает двухслойность явной и защищённой, а не «убирает дубли».**

---

## 1. Механика возникновения дублей — доказательство

Все **1 710** пар дублей имеют ровно одну природу: `report_period` = `DAILY` + `WEEKLY`,
средний разрыв между загрузками **71 час**, окно 20.07–09.08.2026 (21 день). Иных сочетаний нет.

Цепочка (код):

1. Триггеры 3 раза в день, `WbFinanceDaily.gs:1515-1519` — `hours = [7, 12, 18]`.
2. DAILY discovery: скользящее окно `сегодня−3 … сегодня` (`FIN_DAILY_LOOKBACK_DAYS_ = 3`, `:102`),
   `:1041-1047`.
3. WEEKLY discovery: **весь диапазон от cutover до сегодня, на каждом тике**, пока есть хоть одна
   неделя с `weekly_final = FALSE`, `:1056-1070`.
4. Guard `finDailyRegisterReports_` `:807-832` работает **по `report_id`**. У weekly-отчёта свой
   `report_id`, статуса COMPLETE у него нет → он регистрируется и грузится целиком, включая дни,
   уже загруженные daily.
5. Запись — `bqLoadRows_` (`WbBigQuery.gs:181-223`), load job, `writeDisposition: 'WRITE_APPEND'`,
   `jobId` не задан. Ни MERGE, ни DELETE, ни партиционных перезаписей.
6. Post-load проверка `:733-744` считает `COUNT(*)`/`COUNT(DISTINCT rrd_id)` **внутри одного
   `(report_id, run_id)`** — то есть по построению не видит пересечения с другими отчётами.

Обратного guard'а «weekly пропустить, если дни уже загружены daily» нет и по дизайну быть не должно.

---

## 2. Состав PR-A

Пять пунктов. Ни один не трогает маппинг полей и бизнес-логику.

### A1 · ASSERT уникальности `rrd_id` на CANONICAL — главный

Сегодня **ни один ASSERT в проекте не проверяет уникальность `rrd_id`**: все ключи включают
`report_id` (`finance_row_key = CONCAT(report_id,'#',rrd_id)`, `sql/mart/pr_mart1_facts.sql:204`),
поэтому пара DAILY/WEEKLY проходит все существующие проверки насквозь.

Добавить в `sql/mart/pr_mart1_facts.sql` рядом со строкой 249 и в `pr_mart1_validation.sql`:

```sql
ASSERT (
  SELECT COUNT(*) - COUNT(DISTINCT rrd_id)
  FROM `wb_raw.V_WB_FINANCE_CANONICAL`
) = 0 AS 'PR-A: CANONICAL содержит дубли rrd_id — дедуп weekly/daily не сработал';
```

Это единственная защита от сценария «`weekly_final` не проставился → канон перестал дедуплицировать
→ витрина задвоилась». Fail-closed.

### A2 · QC natural key — БЕЗ материализации `payload_hash`

⚠️ **Изменено по замечанию аудитора.** Первая редакция предлагала добавить `payload_hash`
в маппер. Это было бы **молчаливой потерей данных**: в `RAW_WB_FINANCE` 74 колонки, и `payload_hash`
среди них нет (проверено через `INFORMATION_SCHEMA.COLUMNS`), а загрузчик работает с
`ignoreUnknownValues: true` (`WbBigQuery.gs:181-223`) — поле просто выбросилось бы, а load-job
остался бы зелёным.

**Правильный вариант: не материализовать, считать в SQL на лету.** Тогда:
не нужна миграция схемы; не нужен backfill 205 446 исторических строк; legacy и новый лоадер
проверяются одной формулой; JS- и SQL-реализации хэша не могут со временем разойтись.

Эталонная формула — CTE `payload` в `sql/finance/pr_a_regression_check.sql`
(только источниковые поля, без `report_id`, `run_id`, `loaded_at`, `load_id`,
`report_period`, `report_type`).

```sql
-- Ожидание: 0. Срабатывание = WB прислал под одним rrd_id разные суммы,
-- и дедуп начал съедать реальную корректировку.
SELECT rrd_id, COUNT(DISTINCT payload_hash) n
FROM (<CTE payload из pr_a_regression_check.sql>)
GROUP BY rrd_id HAVING n > 1
```

Контракт: `rrd_id` — natural key, **доказанный на наблюдаемой истории**
(203 736 значений, 1 710 повторов, все с идентичным payload, 0 нарушений), а не гарантия WB.

### A3 · Два QC на дубли в RAW

🔴 **Исправлена логическая ошибка первой редакции.** Было:

```sql
GROUP BY rrd_id
HAVING c > 2 OR STRING_AGG(DISTINCT report_period ORDER BY report_period) <> 'DAILY,WEEKLY'
```

Для одиночной строки `c = 1`, `periods = 'DAILY'`, и второе условие истинно → запрос возвращает
каждый нормальный одиночный `rrd_id`. **Прогнано: возвращает 1 007 строк, а не ноль.**
Утверждение «ноль на сегодняшних данных» в первой редакции было ошибочным — я его не проверял.

**QC-1 — настоящий ключ RAW (сильнее и проще):**

```sql
SELECT report_id, rrd_id, COUNT(*) c
FROM `wb_raw.RAW_WB_FINANCE`
GROUP BY 1,2 HAVING c > 1
```

Ожидание всегда 0. Прогнано: **0** и по новому лоадеру, и по всей истории.
Ловит повтор одного и того же отчёта из-за network/crash — то, от чего защищает A5.

**QC-2 — повтор `rrd_id` допустим только как ожидаемая пара DAILY+WEEKLY:**

```sql
SELECT rrd_id, STRING_AGG(DISTINCT report_period ORDER BY report_period) periods, COUNT(*) c
FROM `wb_raw.RAW_WB_FINANCE`
WHERE report_type IS NOT NULL
GROUP BY rrd_id
HAVING COUNT(*) > 1
   AND (COUNT(*) > 2
        OR STRING_AGG(DISTINCT report_period ORDER BY report_period) <> 'DAILY,WEEKLY')
```

Прогнано: **0**. Срабатывание любого из двух → `ingestion alert / failed run`.

### A4 · `is_duplicate` — только с точной семантикой; `source_layer` НЕ материализуем

⚠️ **Сокращено по замечанию аудитора.** `source_layer` в схеме тоже нет, и он **уже
детерминированно вычисляется** в `V_WB_FINANCE_CANONICAL` из `run_id` + `report_period`
(`LEGACY / DAILY / WEEKLY`, `WbFinanceDaily.gs:1367-1436`). Дублировать это физической колонкой
смысла нет.

`is_duplicate` писать можно — колонка существует (`WbBigQuery.gs:114`), сейчас NULL во всех
205 446 строках. Но **обязательно с задокументированной семантикой**:

> `is_duplicate` = повтор внутри одного `(report_id, rrd_id)`, то есть ingestion-дубль.
> Это **НЕ** «`rrd_id` встретился второй раз» — повтор DAILY→WEEKLY ожидаем и дублем не является.

Без этой формулировки имя колонки вводит в заблуждение. Допустимо вынести пункт в отдельную
cleanup-задачу — на инварианты он не влияет.

### A5 · Защита загрузки — четыре части (A5a–A5d)

🔴 **Переработано: первая редакция ломала ownership-контракт.**

`bqLoadRows_` не задаёт `jobId` (`WbBigQuery.gs:181-223`), поэтому потеря ответа на
`Jobs.insert` приводит к повторной попытке. Детерминированный `jobId` это закрывает — но ключ
`fin_<report_id>_<batch_index>` (первая редакция) **привёл бы к тихой потере строк из витрины**.

Почему. `V_WB_FINANCE_COMPLETE` пропускает строки нового контура только при совпадении владельца
(определение вью, проверено):

```sql
JOIN FINANCE_REPORT_LOADS m
  ON r.report_id = m.report_id AND r.run_id = m.processing_run_id
WHERE m.status = 'COMPLETE'
```

Сценарий отказа: execution RUN_A вставил строки с `run_id = RUN_A`, ответ потерялся, манифест ушёл
в ERROR/stale. Следующий execution RUN_B видит тот же детерминированный `jobId`, повторного append
не делает — но строки в RAW остались с `RUN_A`, а манифест теперь принадлежит `RUN_B`.
Post-load `WHERE run_id=@run` не находит строк, а вью не пропускает их вообще.
**Данные в таблице есть, в витрине их нет.**

**Правильный ключ:** `fin_<report_id>_<run_id>_<batch_index>` — сохраняет `run_id` ownership.

#### Контракт A5 — что гарантирует каждая часть

⚠️ **Исправлено по замечанию аудитора.** Ред. 2 утверждала, что межпрогонный crash «закрывает
fail-closed guard QC-1». Это было неверно: QC-1 — периодический validator, он **обнаруживает уже
записанный дубль, но не предотвращает второй append**. Более того, RUN_B успел бы получить
`POSTLOAD_MISMATCH → ERROR`, а RAW уже был бы загрязнён.

Две разные гарантии надо развести явно:

| Механизм | Что даёт |
|---|---|
| **A5a** pre-load orphan guard | fail-closed **до** append при межпрогонном crash |
| **A5b** deterministic `jobId` + `run_id` | exactly-once для повторов `Jobs.insert` **внутри** одного run |
| **A5c** post-load validation | обнаружение сразу после загрузки |
| **A5d** регулярный QC | обнаружение постфактум |

⚠️ Ред. 2 утверждала, что A5b сам по себе достаточен, а межпрогонный crash «закрывает QC-1».
Неверно дважды: `jobId` с `run_id` меняется при новом execution, поэтому второй append
по-прежнему возможен; а QC — детектор, не предохранитель. Предотвращает только A5a.

### A5a · Pre-load orphan guard — два шага, дешёвый и условный

Проверяем `report_id`, а не `rrd_id`, поэтому законная двухслойность DAILY/WEEKLY guard'ом
**не затрагивается** вообще.

**Шаг 1 — дешёвый основной guard (выполняется всегда):**

```sql
SELECT COUNTIF(run_id = @run)                AS this_run,
       COUNTIF(run_id IS DISTINCT FROM @run) AS other_run
FROM `wb_raw.RAW_WB_FINANCE`
WHERE report_id = @report_id
  AND _rr_date BETWEEN @date_from AND @date_to   -- ← partition pruning
```

| Исход шага 1 | Действие |
|---|---|
| `this_run > 0` | resume: проверить job, повторно не грузить |
| `other_run > 0` | **ERROR `ORPHANED_RAW_REPORT`** → ручное восстановление |
| обе нули | перейти к шагу 2 |

**Шаг 2 — условный full-scan fallback (только когда шаг 1 дал ноль):**

```sql
SELECT COUNT(*)                        AS full_count,
       COUNTIF(_rr_date IS NULL)       AS null_rr_date,
       COUNTIF(_rr_date < @date_from
            OR _rr_date > @date_to)    AS outside_period,
       COUNT(DISTINCT run_id)          AS run_count
FROM `wb_raw.RAW_WB_FINANCE`
WHERE report_id = @report_id           -- без фильтра по партициям
```

| Исход шага 2 | Действие |
|---|---|
| `full_count = 0` | чисто → **LOAD разрешён** |
| `full_count > 0` | нарушен partition-инвариант → **ERROR `ORPHANED_RAW_OUTSIDE_MANIFEST_PERIOD`**, append НЕ выполняется |

Сообщение об ошибке должно нести диагностику целиком, иначе разбор займёт часы:

```
ORPHANED_RAW_OUTSIDE_MANIFEST_PERIOD: report_id=…, full=95, pruned=0,
outside=95, null_rr_date=0, run_count=2
```

**Почему условный, а не всегда.** В нормальном режиме шаг 2 выполняется только перед новой,
ещё не загруженной сущностью — то есть ровно один раз на отчёт, а не после каждого
положительного pruned-hit.

**Цена, замерено:**

| Запрос | Байт |
|---|---|
| Шаг 1, pruned | **4 448** |
| Шаг 2, full-scan диагностика (4 колонки, фактический прогон) | **181 300** |
| Оценка dry-run для full-scan (верхняя граница) | 2 274 766 |

Фактический прогон дешевле dry-run-оценки, потому что BigQuery колоночный и читает только
`report_id`, `_rr_date`, `run_id`. Даже верхняя граница 2,3 МБ на редком fallback — ничто
против цены тихого задвоения финансового RAW.

**Ложных срабатываний не будет** — проверено на всей истории:

```
report_id всего                                    239   (195 legacy + 44 новых)
report_id, загруженных И legacy, И новым лоадером     0   ← пересечения нет
report_id, загруженных более чем одним run_id         0   ← max runs per report = 1
```

**Partition-инвариант проверен отдельно** — pruned-скан эквивалентен полному:

```
report_id нового лоадера                44
report_id с расхождением full vs pruned  0
строк вне manifest period                0
строк с _rr_date IS NULL                 0   (и по новому контуру, и по всей таблице)
строк без записи в манифесте             0
записей манифеста с NULL в date_from/to  0
строк full-scan / pruned-scan       4 427 / 4 427
```

То есть шаг 2 сегодня всегда возвращал бы `full_count = 0`. Он ставится на случай, если WB
изменит поведение, а не под известную проблему.

### A5b · Детерминированный load-job

`jobId = fin_<report_id>_<run_id>_<batch_index>` — повторяемость `Jobs.insert` внутри одного
execution. Образец обработки «Already Exists / Jobs.get»:
`apps-script/WbStocksBigQuery.gs:170-235`, аналог в Cloud Run —
`cloud/src/loaders/stocks/jobId.ts:1-22` + `postLoad.ts:15-33`.
Сам wrapper хороший, переносить надо только механику, не ключ.

### A5c · Post-load validation

Частично **уже есть** в коде: `WbFinanceDaily.gs:733-744` считает
`COUNT(*) = COUNT(DISTINCT rrd_id)` внутри `(report_id, run_id)` и роняет отчёт с
`POSTLOAD_MISMATCH`. Добавить нужно только проверку `(report_id, rrd_id)` без привязки к run —
она ловит межпрогонное задвоение сразу после загрузки, не дожидаясь периодического QC.

### A5d · Регулярный QC

Это **тот же набор, что A2 и A3**, плюс два пункта из partition-инварианта — не заводить второй
комплект запросов:

| Контроль | Ожидание | Откуда |
|---|---|---|
| duplicate `(report_id, rrd_id)` | 0 | A3 QC-1 |
| повтор `rrd_id` только как пара DAILY+WEEKLY | 0 | A3 QC-2 |
| один `rrd_id` с разным economic payload | 0 | A2 |
| `_rr_date IS NULL` | 0 | новое, A5d |
| строк вне manifest period | 0 | новое, A5d |
| CANONICAL: дубли `rrd_id` | 0 | A1 ASSERT |

#### Пять рубежей защиты

```
pre-load guard (A5a)  →  deterministic job (A5b)  →  post-load validation (A5c)
                      →  RAW QC (A5d)  →  CANONICAL dedup  →  CANONICAL ASSERT (A1)
```

Ошибка должна пройти все рубежи, прежде чем попасть в Mart. Первые три предотвращают,
последние три обнаруживают.

#### Что остаётся вне PR-A

Автоматически «усыновлять» строки прошлого run **не пытаемся** — это redesign manifest semantics.
При срабатывании `ORPHANED_RAW_REPORT` — ручное восстановление.

Образец обработки «Already Exists / Jobs.get»: `apps-script/WbStocksBigQuery.gs:170-235`,
аналог в Cloud Run — `cloud/src/loaders/stocks/jobId.ts:1-22` + `postLoad.ts:15-33`.
Сам wrapper хороший, переносить надо только механику, не ключ.

**Уточнение мотивации.** Сегодня повторная вставка в рамках одного run **не портит данные**:
`V_WB_FINANCE_COMPLETE` дедуплицирует `ROW_NUMBER() OVER (PARTITION BY report_id, rrd_id ...)`,
а post-load проверка `COUNT(*) = COUNT(DISTINCT rrd_id)` внутри `(report_id, run_id)` уронит отчёт
с `POSTLOAD_MISMATCH`. То есть deterministic `jobId` защищает не от порчи данных, а от **ложного
ERROR и подвисания отчёта в манифесте**. Реальную защиту RAW даёт именно pre-load guard.

---

## 3. Что НЕ входит в PR-A

| Не делаем | Почему |
|---|---|
| Физически удалять daily-строки после weekly | Сломает `FINANCE_WEEK_RECON` (Σdaily vs Σweekly) — слой задуман для сверки |
| Делать RAW идемпотентным по `rrd_id` | То же: уничтожит provisional/final |
| Менять `row_hash` | Он корректен для своего назначения (ключ строки отчёта). Замена потребовала бы backfill 205 446 строк |
| Материализовать `payload_hash` и `source_layer` | Колонок нет в схеме, `ignoreUnknownValues: true` выбросит молча. `payload_hash` считаем в SQL, `source_layer` уже вычисляется в канон-вью |
| «Усыновлять» строки прошлого `run_id` при crash-recovery | Redesign manifest semantics. В PR-A только fail-closed pre-load guard + ручное восстановление по `ORPHANED_RAW_REPORT` |
| Снимать дедуп из `V_WB_FINANCE_CANONICAL` | Второй рубеж защиты, остаётся навсегда (требование аудитора) |
| Восстанавливать `commission_amount` | Поле признано непригодным (это `ppvz_vw`) — PR-C пишет `commission_native_rub` и `marketplace_fee_gap_rub` |
| Трогать `REF_COST_MAP`, нормализацию, SKU-обогащение | Это PR-B и PR-C |

---

## 4. Целевые инварианты после PR-A

```sql
-- RAW: ключ строки отчёта (истина уже сейчас) — QC-1 из A3
COUNT(*) = COUNT(DISTINCT CONCAT(report_id,'|',rrd_id))
-- CANONICAL: один rrd_id — ASSERT, A1
COUNT(*) = COUNT(DISTINCT rrd_id)
-- NATKEY: rrd_id с > 1 payload_hash = 0 — QC, A2 (SQL на лету)
-- DUPES:  повтор rrd_id только как пара DAILY+WEEKLY — QC-2 (A3)
-- ORPHAN: строки report_id от чужого run_id = 0 — pre-load guard, A5
-- LOGISTICS: класс UNKNOWN = 0 — fail-closed, см. ниже
```

⚠️ Записывать инвариант RAW как `COUNT(DISTINCT report_id, rrd_id)` нельзя — BigQuery не
поддерживает двухаргументную форму. Предложенный на ревью вариант
`COUNT(DISTINCT STRUCT(report_id, rrd_id))` **тоже не работает**, проверено:
`Aggregate functions with DISTINCT cannot be used with arguments of type STRUCT`.
Исполнимые формы — только `CONCAT` (как выше, используется в регрессионном SQL)
либо `GROUP BY report_id, rrd_id HAVING COUNT(*) > 1`.

Три уровня ключей, чтобы не путать:

```
RAW  · ключ строки отчёта      = (report_id, rrd_id)   -- уникален, 205 446 из 205 446
     · ключ бизнес-события     = rrd_id + payload QC   -- повтор DAILY→WEEKLY ожидаем
CANON· инвариант               = один rrd_id
```

### Требование аудитора к классификации логистики

При переносе правила forward/return в Mart **не кодировать `ELSE → RETURN`**. Пять классов,
пятый — fail-closed:

```sql
CASE
  WHEN есть SALE   с тем же (srid, sale_dt)   THEN 'FORWARD'
  WHEN есть RETURN с тем же (srid, sale_dt)   THEN 'RETURN_AFTER_SALE'
  WHEN у srid вообще нет строки «Продажа»     THEN 'NON_BUYOUT_LOGISTICS'
  WHEN logistics.sale_dt < first_sale.sale_dt THEN 'CANCELLED_DELIVERY'
  ELSE 'UNKNOWN'   -- ASSERT / QC, молча никуда не включать
END
```

Предикат `CANCELLED_DELIVERY` проверяемый, а не «известный паттерн»: логистика на srid с продажей,
чей `sale_dt` раньше `sale_dt` первой продажи = плечи предыдущей неудавшейся попытки доставки.

Прогнано на всей истории: FORWARD 39 139 / 2 399 116,99 · NON_BUYOUT 5 837 / 354 467,49 ·
RETURN_AFTER_SALE 9 / 426,00 · CANCELLED_DELIVERY 4 / 241,49 · **UNKNOWN 0 / 0,00**.

Для P&L агрегировать:
`return_logistics = RETURN_AFTER_SALE + NON_BUYOUT_LOGISTICS + CANCELLED_DELIVERY`.

---

## 5. Регрессионный контроль

Файл: **`sql/finance/pr_a_regression_check.sql`** — 20 проверок, read-only.
Прогнать **до** правки и **после**; критерий приёмки PR-A — 20/20 `PASS`.
Проверено 11.08.2026: **20/20 PASS**.

Эталон зафиксирован на срезе по 10.08.2026 включительно, поэтому все денежные проверки ограничены
`_rr_date <= '2026-08-10'` — иначе свежие дни сдвинут накопительные итоги.

Контрольные величины: канон 203 736 строк = 203 736 `rrd_id` · единиц 39 139 ·
реализация 26 674 088,76 · `for_pay` 18 777 190,75 · эквайринг 444 751,92 ·
логистика gross 2 754 251,97 = FORWARD 2 399 116,99 + RETURN 355 134,98 ·
хранение 409 307,65 · приёмка 116 587,80 · штрафы 28 314,00 ·
удержания 4 599 011,91 (368 строк) · rebill 377 374,39.

---

## 6. Порядок выполнения

1. Прогнать `pr_a_regression_check.sql` → зафиксировать baseline 20/20 PASS.
2. **A1** (ASSERT на CANONICAL) — отдельным коммитом, самый ценный и самый дешёвый.
3. **A2 + A3** — три SQL QC (natural key, `(report_id,rrd_id)`, пара DAILY+WEEKLY).
   Кода Apps Script не трогают вообще. Решить, куда вешать: шаг в `wbFinDailyStatus()`
   или sql-валидатор рядом с `pr_mart1_validation.sql`.
4. **A5a** — pre-load orphan guard: pruned-шаг + условный full-scan fallback.
5. **A5b** — deterministic wrapper `fin_<report_id>_<run_id>_<batch_index>`.
   A5c дополняет существующую post-load проверку, A5d = набор из A2/A3 + два новых пункта.
6. **A4** — опционально, можно вынести в cleanup-задачу.
7. Повторный прогон регрессии → 20/20 PASS.
8. Только после этого PR-B.

Шаги 2–3 не требуют изменений в Apps Script вообще — это чистый SQL. Их можно слить первыми
и получить fail-closed защиту до того, как кто-то тронет загрузчик.

**Файлы:** `sql/mart/pr_mart1_facts.sql` (~`:249`), `sql/mart/pr_mart1_validation.sql`,
`sql/finance/pr_a_regression_check.sql` (новый), новый sql-файл с тремя QC;
`apps-script/WbBigQuery.gs` (`bqLoadRows_` `:181-223`) и `WbFinanceDaily.gs`
(`finDailyLoadReport_` `:720-744`, точка вызова guard'а) — только для A5.
Маппер `finDailyMapRow_` в PR-A **не трогаем** (A4 опционален и добавляет только один ключ).

**Дальше без параллельной работы над Dashboard Blueprint** — сначала доказать, что канон
не сдвинулся.

---

Связанные документы: артефакт `evetis-wb-finance-rev2` (REV2.4),
`docs/FINANCE_DAILY_DESIGN_2026-07-22.md`, память проекта:
`finance-rev2-2026-08-11`, `finance-rev2-normalization-rootcause`,
`finance-logistics-forward-return-rule`.
