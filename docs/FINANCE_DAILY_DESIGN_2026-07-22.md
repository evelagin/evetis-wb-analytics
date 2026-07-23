# Дизайн PR-Fin1: Production Finance Loader (двухслойная модель DAILY/WEEKLY)

Дата: 2026-07-22. **Ревизия 3-final** (аудит rev2: APPROVE WITH CHANGES — grain
manifest + 8 правок; аудит rev3: APPROVE WITH MINOR CHANGES — 4 уточнения + 2 доп.
внесены). Статус: **УТВЕРЖДЁН, к реализации**.
Основание: probe A/B/C 22.07 (WbFinanceDailyProbe.gs v1.1) — критерии D1–D6 закрыты.

Изменения рев.2: legacy-branch в COMPLETE-вью; cutover по границе недели; метрики
list/detailed в manifest NUMERIC + metrics JSON; DISCOVERED + несколько тиков в день;
FINANCE_LOADER_RUNS; weekly discovery незакрытых недель; 07:30 не SLA; raw_json
однострочный; ослаблено row-level равенство; NUMERIC в новых таблицах.

Изменения рев.3: **manifest grain = report_id** (одна mutable-строка на физический
отчёт; discovered_run_id / processing_run_id / attempt_count; discovery никогда не
создаёт второй queue-item); FINANCE_REPORT_ATTEMPTS — опционально, вне MVP;
обязательный preflight-SQL «нет legacy после cutover» (fail-closed); weekly discovery
по календарю недель от cutover (не по строкам пустой WEEK_STATUS); очередь
DISCOVERED/ERROR обрабатывается ДО новых list-запросов; report_type INT64; retry с
rrdId=0 + COMPLETE только после distinct-проверки; recovery stale STARTED;
raw_json = JSON одного detailed-объекта (не HTTP-body).

Финальные уточнения (аудит rev3, APPROVE WITH MINOR CHANGES): attempt_count
инкрементится ровно один раз — при переходе в STARTED (stale-recovery НЕ
инкрементит); перед COMPLETE — post-load SQL по RAW (persisted_rows =
persisted_distinct_rrd = rows_fetched = rows_loaded); daily discovery пропускает
недели с weekly_final=true; COMPLETE-строка manifest immutable (повторный list
только сверяет, расхождение метрик = аномалия fail-closed); PK report_id —
логический (только MERGE + контроль COUNT(*)=COUNT(DISTINCT report_id));
C0-сверка legacy по count + датам + финансовым суммам.

---

## 1. Что доказано probe (эмпирика, не переоткрывать)

| # | Факт | Доказательство |
|---|------|----------------|
| 1 | Ежедневные отчёты реализации существуют в API | `list period='daily'`: 25 отчётов/14 дней, HTTP 200 |
| 2 | Daily type=1 формируется каждый день, лаг 1 день | покрытие 14/14, createDate = день+1 |
| 3 | Daily type=2 («по выкупам») спарсный — только в дни с выкупами | пропуски 13/15/17.07; Σ доступных type2 недели = weekly type2 точно |
| 4 | **Daily-слой ТОЧЕН по проверенным метрикам**: Σ7 daily = weekly | list-суммы недели 13–19.07 Δ=0; detailed дня 16.07 Δ=0 по 12 агрегатам |
| 5 | **Weekly содержит те же rrdId; на проверенном срезе значения совпали** | пересечение: common 95/95 по (rrdId, forPay), changed/only 0; агрегаты дня Δ=0. ⚠️ Probe сравнил rrdId+forPay и агрегаты 12 метрик, НЕ все 89 полей построчно — полное row-level равенство НЕ утверждается |
| 6 | rrdId уникален внутри отчёта; пустых нет | daily 95/95, weekly 586/586 |
| 7 | BigInt: reportId 16 цифр (≈4.1e15 < 2^53), rrdId 13 цифр — потерь пока нет | двухканальная сверка raw/safe/native |
| 8 | Weekly формируется в пн (лаг 1 день); официально возможно до ср | createDate 3 недель подряд = пн |
| 9 | Detailed нового API: 89 полей; одна страница на отчёт | B/C, ответы 0.2–1.1 МБ |

**Ключевое следствие (№5):** одна и та же строка приходит дважды под **разными**
`report_id` (daily `409455520260716` и weekly `785600830`). Дедуп `report_id|rrd_id`
их НЕ схлопнет → без замещения по неделе — двойной счёт.

## 2. Модель (принята владельцем + аудитором 22.07)

Два слоя: **DAILY = PROVISIONAL** (оперативка за вчера), **WEEKLY = FINAL**
(платёжный канон). Weekly — FINAL всегда, независимо от совпадения с daily.
Слои никогда не суммируются. Замещение — на уровне **(отчётная неделя × reportType)**.
Daily остаётся в RAW навсегда (аудит/reconciliation). Окна пересверки 60 дней нет:
COMPLETE-отчёт повторно не грузится; deep backfill — отдельная ручная функция.

## 2A. Cutover legacy ↔ новый контур (блокер 2)

Константа **`FIN_CUTOVER_WEEK_START = '2026-07-13'`** (пн, МСК; фиксируется при
внедрении = начало первой недели, которую грузит НОВЫЙ контур).

- **Недели < cutover** — источник: ТОЛЬКО legacy-строки (`run_id IS NULL`,
  вся существующая история 2024-09…12.07, она недельная). Новый загрузчик эти
  недели сам НЕ грузит. Если deep backfill (ручной) перегрузил старую неделю —
  для неё действует приоритет: manifest-confirmed WEEKLY COMPLETE > legacy
  (по той же неделе × типу), детерминированно и без смешивания.
- **Недели ≥ cutover** — источник: ТОЛЬКО manifest-confirmed строки нового контура
  (daily → замещение weekly по §3.6). Legacy-строк этих недель нет по построению
  (история заканчивается 12.07 = вс перед cutover).

Пересечение legacy ↔ новое исключено конструктивно: граница проходит по стыку
недель 06–12.07 / 13–19.07.

**Обязательный preflight (fail-closed):** при C0-инициализации И в установщике
триггеров выполняется SQL-проверка

```sql
SELECT COUNT(*) FROM RAW_WB_FINANCE
WHERE run_id IS NULL
  AND _rr_date >= DATE('<FIN_CUTOVER_WEEK_START>')
```

Ожидание — 0. Если найдены legacy-строки на/после cutover — установка/включение
останавливается с ошибкой (вью не переключаем, триггеры не ставим) до ручного
разбора: либо сдвиг cutover, либо очистка аномальных строк.

## 3. Объекты BigQuery

### 3.1 `RAW_WB_FINANCE` (существующая, append-only) — ALTER ADD COLUMNS (nullable)
Новые колонки: `report_period` STRING ('DAILY'|'WEEKLY'), `report_type` **INT64**
(1|2|3), `run_id` STRING, `raw_json` STRING (**JSON одного detailed-объекта этой
строки**: `JSON.stringify(row)` — один объект из массива ответа, одной строкой,
без pretty-print; НЕ полный HTTP-body). Существующие строки не трогаются: у них
`run_id IS NULL` — это и есть признак legacy (см. 2A, 3.5); `report_period IS NULL`
трактуется как 'WEEKLY'. Типы существующих денежных колонок RAW не меняем
(STRING, историческая конвенция) — числовой слой начинается с manifest/recon (NUMERIC)
и вью. Обязательные метаданные новых строк: report_id, report_period, report_type,
report_date_from, report_date_to, rrd_id, run_id, loaded_at, source_api.

### 3.2 `FINANCE_LOADER_RUNS` — журнал прогонов (отдельно от report-manifest)
`run_id` (PK), started_at, finished_at, status ('OK'|'OK_NO_NEW'|'PARTIAL'|'ERROR'),
trigger_type ('AUTO'|'MANUAL'|'BACKFILL'), reports_discovered, reports_loaded,
reports_errors, requests_made, error_message.

### 3.3 `FINANCE_REPORT_LOADS` — manifest/очередь, **grain = `report_id`**
**Одна mutable-строка на ФИЗИЧЕСКИЙ отчёт** (PK report_id, STRING). Все переходы —
MERGE ON report_id: DISCOVERED → STARTED → COMPLETE | ERROR (и ERROR/stale →
DISCOVERED при ретрае). Discovery, встретив report_id, у которого строка уже есть
в ЛЮБОМ статусе, **никогда не создаёт второй queue-item**. Таблица одновременно
и очередь, и текущее состояние отчёта —
неоднозначность grain `(run_id, report_id)` из рев.2 устранена: незакрытый отчёт
не размножается по новым run_id, JOIN и RECON не задваиваются.
Колонки: report_id (PK), report_period, report_type INT64, date_from, date_to,
status, **discovered_run_id** (кто нашёл), **processing_run_id** (чья загрузка
действительна/идёт), **attempt_count**, discovered_at, started_at, completed_at,
rows_fetched, rows_loaded, **NUMERIC-метрики из list**: list_forpay, list_retail,
list_delivery, list_storage, list_acceptance, list_deduction, list_penalty;
**NUMERIC-метрики из detailed**: det_forpay, det_retail, det_delivery, det_storage,
det_acceptance, det_deduction, det_penalty; `list_metrics_json` (однострочный JSON),
error_message. Достаточно для FINANCE_WEEK_RECON без повторных запросов.
**Валидность RAW-строки** = существует manifest-строка её report_id со status=
COMPLETE **и RAW.run_id = manifest.processing_run_id** — строки неудачных/старых
попыток невидимы по построению.
История попыток: опциональная `FINANCE_REPORT_ATTEMPTS` grain `(run_id, report_id,
attempt_no)` — **в MVP НЕ создаётся** (attempt_count + FINANCE_LOADER_RUNS достаточно).

**Правила целостности manifest:**
- `attempt_count` инкрементится **ровно один раз — при переходе в STARTED**.
  Stale-recovery (STARTED→DISCOVERED) и любые другие переходы счётчик НЕ трогают.
- **COMPLETE-строка immutable.** Discovery, встретив report_id со status=COMPLETE,
  ничего не пишет — только СВЕРЯЕТ свежие list-метрики с сохранёнными; расхождение
  (|Δ| > 0.01 по любой метрике) = аномалия: run завершается ERROR c диагностикой
  в error_message run-журнала, manifest НЕ обновляется (fail-closed, «WB изменил
  закрытый отчёт» — событие для ручного разбора, не для молчаливого UPDATE).
- PK `report_id` — **логический** (BigQuery PK не enforced): все записи ТОЛЬКО
  через MERGE ON report_id; контроль уникальности
  `COUNT(*) = COUNT(DISTINCT report_id)` входит в self-test и приёмку.

### 3.4 `FINANCE_WEEK_STATUS` — grain `week_start × report_type`
week_start (пн, DATE, МСК), report_type INT64, weekly_report_id (STRING), weekly_final BOOL,
finalized_at, daily_days_loaded INT, notes. weekly_final=true ставится ТОЛЬКО после
COMPLETE weekly-отчёта этой недели этого типа. Обновление — MERGE.

### 3.5 `FINANCE_WEEK_RECON` — grain `week_start × report_type × metric`
metric, sum_daily NUMERIC, sum_weekly NUMERIC, delta NUMERIC, recon_status
('OK'|'WARN'), checked_at. Источник — NUMERIC-метрики manifest (COMPLETE-строки),
без дополнительных запросов к API. Считается при финализации недели; после FINAL
пересверка недели прекращается.

### 3.6 `V_WB_FINANCE_COMPLETE` — две явные ветви (блокер 1)
```
SELECT ... FROM RAW
WHERE run_id IS NULL                -- ВЕТВЬ 1: legacy (вся история без manifest)
UNION ALL
SELECT ... FROM RAW r
JOIN manifest m                              -- ВЕТВЬ 2: новый контур
  ON r.report_id = m.report_id
 AND r.run_id    = m.processing_run_id       -- только действительная попытка
WHERE m.status = 'COMPLETE'
```
поверх — last-wins дедуп по `(report_id, rrd_id)` ORDER BY loaded_at DESC,
run_id DESC (legacy: loaded_at). INNER JOIN на manifest применяется ТОЛЬКО ко
второй ветви — legacy-история (201 211 строк) не может быть потеряна по построению;
условие `run_id = processing_run_id` скрывает частичные строки прерванных попыток.

### 3.7 `V_WB_FINANCE_CANONICAL`
Поверх COMPLETE. Неделя строки = пн(rr_date, МСК). Правила по порядку:
1. week < `FIN_CUTOVER_WEEK_START` → legacy-ветвь; если для (week × type) существует
   manifest-confirmed WEEKLY COMPLETE (ручной deep backfill) → она вместо legacy.
2. week ≥ cutover: `report_period='WEEKLY'` → строка проходит (FINAL);
   `report_period='DAILY'` → проходит только если (week × type) НЕ weekly_final.
Поля: `source_layer` ('LEGACY'|'DAILY'|'WEEKLY'), `finance_status`
('FINAL'|'PROVISIONAL'; legacy = FINAL). Потребители PNL/MART читают ТОЛЬКО canonical.
Существующее `V_WB_FINANCE` не переопределяется в этом PR (читатели живут как жили);
их перевод на canonical — отдельный шаг после приёмки.

## 4. Загрузчик `WbFinanceDaily.gs` (новый файл)

Триггеры: **3 тика в день** (~07:30, ~12:30, ~18:30 МСК; 07:30 — первый ПРОБНЫЙ
запуск, НЕ SLA появления отчётов; фактическое время появления замеряем по run-log
и потом при желании сокращаем до 1–2 тиков). Очередь устойчива к любому числу
тиков: состояние живёт в manifest (DISCOVERED), а не в памяти прогона.

`runWbFinanceDaily()` (каждый тик, ScriptLock):
1. **Открыть run** в FINANCE_LOADER_RUNS.
2. **Recovery stale STARTED:** manifest-строки со status='STARTED' и
   `started_at < now − FIN_STALE_STARTED_MIN_` (по умолчанию 120 мин — заведомо
   больше любого живого прогона) → MERGE обратно в DISCOVERED
   (**attempt_count НЕ инкрементится** — единственная точка инкремента это
   STARTED). Их частичные RAW-строки остаются невидимыми (processing_run_id
   ещё не подтверждён COMPLETE'ом).
3. **Обработка очереди — ДО любых новых list-запросов:** все DISCOVERED и ERROR
   (attempt_count < FIN_MAX_ATTEMPTS_, по умолчанию 5) из manifest, старые сначала;
   61с пауза между ЛЮБЫМИ запросами; бюджет ~4.5 мин.
4. **Загрузка отчёта (retry-семантика):** MERGE→STARTED (processing_run_id =
   текущий run; **attempt_count+1 — здесь и только здесь**) → detailed
   **всегда с rrdId=0** (полная перезагрузка отчёта, никаких дозагрузок с
   середины) → маппинг + raw_json → append в RAW батчами →
   **обязательный post-load SQL по RAW перед COMPLETE** для текущих
   `(report_id, processing_run_id)`:

   ```sql
   SELECT COUNT(*) AS persisted_rows,
          COUNT(DISTINCT rrd_id) AS persisted_distinct_rrd
   FROM RAW_WB_FINANCE
   WHERE report_id = @rid AND run_id = @processing_run_id
   ```

   требование: `persisted_rows = persisted_distinct_rrd = rows_fetched =
   rows_loaded`; любое неравенство = ERROR (не COMPLETE). MERGE→COMPLETE
   (rows, NUMERIC-суммы detailed, list-метрики). Fail-closed: любой сбой →
   MERGE→ERROR; строки этой попытки невидимы (нет COMPLETE с этим
   processing_run_id). Следующий ретрай начинает заново с rrdId=0.
5. **Discovery — при остатке бюджета (иначе целиком на следующий тик):**
   (a) **Daily:** `list(period='daily', dateFrom=сегодня−3)` — lookback 2–3 дня.
   Отчёт, чья неделя (по date_from) уже `weekly_final=true` для его report_type,
   **НЕ регистрируется вовсе** (замещённый слой не догружаем). Иначе: отчёт без
   строки в manifest → MERGE ON report_id → DISCOVERED (discovered_run_id =
   текущий run); существующая COMPLETE-строка — только сверка метрик (см. 3.3).
   Второй queue-item не создаётся никогда.
   (b) **Weekly:** список недель строится **ПО КАЛЕНДАРЮ** — от
   `FIN_CUTOVER_WEEK_START` до последней завершённой недели включительно
   (НЕ по строкам FINANCE_WEEK_STATUS — на пустой таблице это дало бы пустой
   список); для недель без weekly_final → один `list(period='weekly',
   dateFrom=cutover, dateTo=сегодня)` диапазоном; найденные отчёты → DISCOVERED;
   строки WEEK_STATUS создаются MERGE'м для всех календарных недель.
   (c) Свежеоткрытые отчёты обрабатываются в этом же тике при остатке
   бюджета, иначе следующим.
6. **Финализация недели:** weekly COMPLETE → MERGE в FINANCE_WEEK_STATUS
   (weekly_final=true) + расчёт FINANCE_WEEK_RECON из manifest-метрик.
   Daily финализированной недели больше не грузим и не открываем.
7. **Закрыть run:** статус по иерархии ошибка > неполнота (осталась очередь =
   PARTIAL) > OK_NO_NEW/OK.

Триггеры ставятся отдельной функцией ПОСЛЕ ручной приёмки (fail-closed проверка
таймзоны — как в WbAdsDaily). **Deep backfill** — отдельная ручная функция:
произвольный период → та же очередь/manifest, trigger_type='BACKFILL'
(для недель < cutover действует приоритет 3.7-п.1).

## 5. BigInt / парсинг (обязательные правила)

- reportId живёт СТРОКОЙ от list до path detailed; native JSON.parse для ID не
  используется: сырой body → quoted-transform (`"reportId|rrdId|giId|shkId": N` → "N")
  → parse; сверка количества regex-ID и объектов → PARSE_MAPPING_ERROR → ERROR.
- Курсор пагинации rrdId — строка цифр вербатим в payload.
- `raw_json` — результат `JSON.stringify` строки ответа: один объект, одна строка,
  без pretty-print (переносов строк внутри значения нет).
- **Фикс существующего кода:** `wbFinV1FetchDetailedAll_` (WbFinanceApiV1.gs) шлёт
  `rrdid` вместо `rrdId` — исправить; там же native parse (для weekly сейчас
  безопасно, но унифицировать). Правка минимальная, помеченная.

## 6. Затрагиваемые файлы/объекты

Новые: `apps-script/WbFinanceDaily.gs`; BQ: FINANCE_LOADER_RUNS, FINANCE_REPORT_LOADS
(grain report_id), FINANCE_WEEK_STATUS, FINANCE_WEEK_RECON, V_WB_FINANCE_COMPLETE,
V_WB_FINANCE_CANONICAL (FINANCE_REPORT_ATTEMPTS — вне MVP); ALTER RAW_WB_FINANCE
(+4 nullable колонки, report_type INT64). Деньги в новых таблицах — **NUMERIC**
(RAW остаётся STRING по исторической конвенции; преобразование — на границе
manifest/вью). Правка: WbFinanceApiV1.gs (rrdId, 2 строки). Листы Sheets не трогаются;
WbFinanceBackfillBQ.gs не трогается (легаси-бэкфилл, пометить deprecated).

## 7. Риски

| Риск | Митигация |
|------|-----------|
| Потеря legacy-истории вью | ветвь `run_id IS NULL` в COMPLETE (3.6) + приёмочный count-инвариант |
| Пересечение legacy ↔ новое | cutover по границе недель 12/13.07 (2A); deep backfill старых недель — явный приоритет manifest>legacy |
| Двойной счёт daily+weekly | canonical-замещение по (неделя×тип); тест «Σ недели == weekly, не ×2» |
| reportId > 2^53 | строковый парсер повсюду + PARSE_MAPPING_ERROR |
| Weekly задерживается (до ср и позже) | discovery всех незакрытых недель каждый тик; неделя остаётся PROVISIONAL — честный статус |
| Отсутствие daily type=2 в день без выкупов | НЕ пробел (probe п.3); WEEK_STATUS учитывает только фактические отчёты |
| Rate limit 1 req/мин | единый пейсер 61с; бюджет 4.5 мин/тик; очередь в manifest переживает тики |
| Хвост очереди при всплеске отчётов | 3 тика/день; DISCOVERED не теряется; очередь обрабатывается до discovery; PARTIAL-статус сигналит |
| Дублирование queue-items одного отчёта | grain manifest = report_id, MERGE ON report_id — второй item невозможен по построению |
| Зависший STARTED (убитый прогон) | recovery: STARTED старше 120 мин → DISCOVERED (без инкремента attempt_count); частичные строки скрыты правилом processing_run_id |
| Частично загруженный отчёт | ретрай всегда с rrdId=0; COMPLETE только после post-load SQL: persisted_rows=persisted_distinct_rrd=rows_fetched=rows_loaded |
| «WB изменил закрытый отчёт» | COMPLETE immutable; сверка list-метрик при повторном discovery; расхождение → run ERROR, ручной разбор |
| Дубли report_id в manifest (PK логический) | только MERGE; self-test COUNT(*)=COUNT(DISTINCT report_id) |
| Бесконечные ретраи битого отчёта | attempt_count ≥ FIN_MAX_ATTEMPTS_ (5) → остаётся ERROR, run сигналит PARTIAL/ERROR |
| Legacy-строки после cutover (аномалия) | обязательный preflight-SQL в C0 и установщике триггеров; fail-closed |
| Новые sellerOperName | маппинг расширен (6 операций из probe); UNMAPPED-диагностика |

## 8. Контрольные цифры приёмки

1. **C0 (init):** ALTER прошёл; таблицы/вью созданы; **preflight-SQL: 0 legacy-строк
   с `_rr_date >= cutover`** (иначе стоп); **сверка legacy до/после** не только по
   count (`V_WB_FINANCE_COMPLETE` == `V_WB_FINANCE_CANONICAL` == `V_WB_FINANCE` ==
   201 211 на 22.07), но и по **MIN/MAX `_rr_date` и Σ `for_pay` / Σ `retail_amount`**
   (SAFE_CAST, канонический парс) — все три пары значений идентичны до и после.
2. **Прогон 1 (daily):** отчёты за вчера DISCOVERED→COMPLETE; в manifest ровно
   ОДНА строка на report_id; RAW +строки; canonical показывает вчера со
   status=PROVISIONAL, source_layer=DAILY; det_forpay == list_forpay в manifest.
3. **Идемпотентность:** немедленный повтор тика → OK_NO_NEW, 0 загрузок, RAW не
   растёт, в manifest НЕ появилось новых строк по тем же report_id;
   COMPLETE-строки не изменились (immutable); manifest:
   COUNT(*) == COUNT(DISTINCT report_id); daily-отчёты финализированных
   недель не регистрируются.
4. **Очередь/retry:** искусственно оборвать прогон после 2 отчётов → остаток
   DISCOVERED/STARTED; следующий тик: stale-recovery → перезагрузка с rrdId=0 →
   дозагрузка без потерь; canonical не содержит строк прерванной попытки
   (проверка по processing_run_id); дублей queue-item нет.
5. **Прогон в пн:** weekly прошлой недели COMPLETE → weekly_final=true; canonical:
   строки недели только WEEKLY (FINAL), daily исключены; Σ forPay недели ==
   weekly list_forpay (НЕ ×2); RECON: delta=0 по всем метрикам из manifest.
6. **Негативные:** HTTP≠200/обрыв → ERROR в manifest (attempt_count+1), canonical
   не изменился; провал distinct-контроля → ERROR, не COMPLETE;
   PARSE_MAPPING_ERROR → ERROR run; чужой ScriptLock → пропуск тика;
   preflight с подложной legacy-строкой после cutover → установка остановлена.

## 9. Вне объёма PR-Fin1

FACT_FINANCE / MART_PNL (после reconciliation for_pay, архитектура §8); перевод
существующих читателей V_WB_FINANCE на canonical; Health Monitor; удаление
легаси-листа RAW_WB_FINANCE из Sheets; Ozon.
