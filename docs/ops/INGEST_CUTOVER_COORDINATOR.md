# Координатор cutover и единый writer-ownership gate WB (этап C, 11.11.2026)

OWNER ACK 11.11.2026, этап C программы переноса загрузки WB из Apps Script в Cloud Run. Предыдущий документ —
`MIG_FINAL_INTEGRATION_GATE_2026-11-10.md` (§A.7, §B.3, §D). **Всё в Draft: ничего не применено и не выкачено.**

## 1. Что и зачем

Сегодня переключение потока зависит от ручного удаления триггера Apps Script и ручного resume Scheduler.
Атомарного механизма между Apps Script и Cloud Scheduler **нет и не будет**. Вместо этого — один fencing-источник
правды, которому подчиняются оба писателя: строка потока в `wb_ops.INGEST_WRITER_OWNERSHIP`.

- Переключение — это смена владельца с доказанным дренажом, а не снятие триггеров.
- Триггеры Apps Script и Scheduler Cloud Run могут оставаться включёнными: неправообладатель ничего не пишет.
- Механизм не перечисляет и не удаляет триггеры. `ScriptApp` видит только триггеры текущего пользователя, поэтому
  fencing не может на них опираться.
- Механизм автономен на стороне сервера: Cloud Run Job + Cloud Scheduler. От открытого Claude Desktop не зависит.

| Компонент | Где |
|---|---|
| DDL и seed | `sql/ops/ingest_writer_ownership.sql` |
| Gate Cloud Run | `cloud/src/ownership/` и вызов в `cloud/src/cli.ts` |
| Координатор | `cloud/src/loaders/cutover/` (загрузчик `cutover`, Job `ingest-cutover-prod`) |
| Gate Apps Script | `apps-script/ingestion/WriterGate.gs` и вызовы в 10 точках входа |
| Terraform | `infra/terraform/ingest_cutover.tf`, флаг `cutover_coordinator_enabled` |
| Выкат | `deploy-prod.yml`, `only_job=ingest-cutover` (вне `all`) |

## 2. Таблицы

- **`INGEST_WRITER_OWNERSHIP`** — одна строка на поток (`stocks`, `statistics`, `ads`, `finance`, `supplies`, `ref`).
  - `owner` ∈ {`APPS_SCRIPT`, `CLOUD_RUN`, `NONE`};
  - `epoch` — fencing-токен, растёт при каждой смене владельца;
  - `revision` растёт при каждом переходе;
  - `legacy_watermark` / `cloud_watermark` — точные отметки переходов;
  - `fenced_at` — начало дренажа;
  - `active_since` — начало мониторинга;
  - `checks_since` — начало окна проверок «писатель без права» (смена владельца или `resolve`).
  - Сверх полей задачи добавлены `revision`, `fenced_at`, `active_since`, `checks_since`, `auto_cutover_enabled`.
    Без `revision` CAS по (epoch, state) уязвим к ABA: путь BLOCKED → SHADOW_VALIDATING возвращает ту же пару.
- **`INGEST_CUTOVER_LOG`** — append-only журнал. Строка пишется в той же транзакции, что и CAS.
  `transition_id = <stream>:r<revision>`, время — `transition_at` (`at` — зарезервированное слово BigQuery).
- **`INGEST_QUALIFICATION`** — вердикты гейта по (поток, сутки, группа). Последняя строка по `verdict_at` заменяет
  прежние. Пишут их проверки паритета. Координатор вердикты только читает (права — dataViewer), см. §11.

Смена владельца — только `MERGE … WHEN MATCHED AND epoch = @e AND state = @s AND revision = @r` в транзакции вместе
со строкой журнала. Повтор той же команды: CAS уже не совпадает, а целевое состояние уже достигнуто → `OK_ALREADY`.

## 3. Машина состояний

```
LEGACY_ACTIVE(AS) → SHADOW_VALIDATING(AS) → QUALIFIED(AS) → LEGACY_FENCED(NONE) → CLOUD_ACTIVE(CR)
  → MONITORING(CR) → COMPLETED(CR)
откат:  CLOUD_ACTIVE | MONITORING | COMPLETED | ROLLBACK_REQUIRED (CR) → CLOUD_FENCED(NONE) → LEGACY_ACTIVE(AS)
отказы: BLOCKED(AS)  STOP(владелец по ситуации)  ROLLBACK_REQUIRED(CR)  UNKNOWN_OUTCOME(NONE)
```

| Переход | Условие |
|---|---|
| LEGACY_ACTIVE → SHADOW_VALIDATING | появились вердикты квалификации |
| SHADOW_VALIDATING → QUALIFIED | `PASS` по всем обязательным группам за окно потока, §3.1 |
| → BLOCKED | недопустимый вердикт в самых свежих сутках — снимается сам, когда свежий день снова чистый. **Или** prod-прогон Cloud Run в `LOADER_RUNS` при владельце не CLOUD_RUN (образ без gate, gate выключен) — код `CUTOVER_WRITER_NOT_GATED`, снимается только командой `resolve` |
| QUALIFIED → LEGACY_FENCED | `auto_cutover_enabled` (колонка или `CUTOVER_AUTO_STREAMS`) **или** команда `fence`; окно свободно, §3.2; **писатели под gate по каждой плановой точке входа** (§3.4). CAS `owner = NONE`, `epoch + 1` |
| LEGACY_FENCED → CLOUD_ACTIVE | доказанный дренаж Apps Script, §3.3; фиксируется legacy watermark. CAS `owner = CLOUD_RUN`, `epoch + 1` |
| CLOUD_ACTIVE → MONITORING | первый COMPLETE Cloud Run; покрытие от legacy watermark подтверждено на данных, §4 |
| MONITORING → COMPLETED | ≥ 3 штатных цикла каждого загрузчика (finance — плюс цикл понедельника), без ERROR/PARTIAL, без дублей, покрытие подтверждено, downstream жив |
| CLOUD_ACTIVE/MONITORING → ROLLBACK_REQUIRED | ERROR/PARTIAL Cloud Run — по **последнему исходу** загрузчика за период (ERROR, закрытый COMPLETE повтором того же периода, не отказ); дубли; разрыв покрытия; нет первого COMPLETE за срок. Владелец остаётся CLOUD_RUN: данные идут, решает человек |
| → STOP | старт писателя после fence + запас gate→журнал; записывающее задание BigQuery, созданное позже, чем мог жить прогон до fence; Apps Script пишет при владельце CLOUD_RUN. Во втором случае Cloud Run тоже отсекается (`NONE`) |
| → UNKNOWN_OUTCOME | у старого писателя есть прогон STARTED/RUNNING без исхода, который **мог быть жив в момент fence** |
| STOP / UNKNOWN_OUTCOME → LEGACY_FENCED или CLOUD_FENCED | команда `resolve` с причиной оператора (`CUTOVER_REASON`) и целью (`CUTOVER_RESOLVE_TO`): новый отсчёт дренажа и проверок, причина — в журнал |
| CLOUD_FENCED → LEGACY_ACTIVE | доказанный дренаж Cloud Run: timeout Job'а + 5 мин, нет STARTED без исхода, нет незавершённых заданий BigQuery |

**Гарантии:**
- **`tick` двигает поток максимум на один шаг.** UNKNOWN_OUTCOME, STOP и ROLLBACK_REQUIRED `tick` не трогает:
  слепого повтора нет. Выход из них — командами `resolve` (разбор оператора → снова дренаж), `activate` или
  `rollback`; каждая заново доказывает дренаж.
- **Двух владельцев одновременно не бывает.** Владелец меняется только через `NONE`: прямого перехода
  `APPS_SCRIPT ↔ CLOUD_RUN` нет. Это проверяется кодом (`step`) и исчерпывающим тестом по всем состояниям и действиям.
- **CAS-гонка двух `tick`.** Первый выигрывает, второй получает `OK_ALREADY`; журнал — одна строка.
- **Потерянный ответ CAS.** Если транзакция применилась, а ответ потерян, переход распознаётся по `revision`:
  результат `OK_ALREADY`, второго перехода нет.

### 3.1 Квалификация (`profiles.ts`, декларативно)

| Поток | Окно | Группы |
|---|---|---|
| stocks | 5 сопоставимых суток подряд, **один образ** | `STOCKS_T6`, `STOCKS_T5` — daily; `STOCKS_DIGEST` — квалификация этого же digest |
| statistics | 5 суток | `STATS_ORDERS`, `STATS_SALES`, `STATS_SALES_RECONCILE` (ночная сверка) — daily, допустим `DRIFT_EXPLAINED` (дрейф атрибута SKU, §B.3 п.4) |
| ads | 5 суток | `ADS_COSTS` — только `PASS`; `ADS_STATS`; `ADS_COSTS_AUDIT` — недельный, хотя бы один за 7 суток |
| finance | 5 суток | `FINANCE_DAILY`; `FINANCE_MONEY` (`FAIL` = необъяснённое денежное расхождение); **`FINANCE_WEEKLY` — в понедельник**, обязательно |
| supplies, ref | 5 суток | `SUPPLIES` / `REF` |

Правила:
- Сопоставимые сутки считаются подряд от самых свежих. Первый несопоставимый день обрывает счёт.
- Свежий день без части вердиктов (ещё досчитывается) пропускается.
- Квалификация старше 2 суток устаревает: поток возвращается в `SHADOW_VALIDATING`.

### 3.2 Окно fence

- Fence допустим в момент t, только если интервал [t, t + дренаж + 5 мин] не пересекается ни с одним плановым
  запуском потока — ни Apps Script, ни Cloud Run.
- Занятость запуска: [старт − разброс, старт + разброс + длительность]:
  - Apps Script: ±15 мин (`nearMinute`), длительность 6 мин;
  - Cloud Run: ±1 мин, длительность = timeout Job'а.
- Расписания: Apps Script — снимок живых триггеров 11.11.2026 (страница «Триггеры»), Cloud Run — Terraform потоков
  (`profiles.ts`). Модель ±15 мин вокруг живой минуты исключает окна, перекрывающие живые прогоны.
- Перед `auto_cutover_enabled` владелец сверяет расписания со страницей «Триггеры», см. §9.

### 3.3 Дренаж (доказательно, а не по времени)

1. Ждём не меньше `max(7, CUTOVER_LEGACY_DRAIN_MIN)` минут для Apps Script. Лимит исполнения консьюмерского
   аккаунта — 6 мин; у Workspace — 30, тогда `CUTOVER_LEGACY_DRAIN_MIN=31`. Для Cloud Run — timeout Job'а + 5 мин.
2. **Ни один писатель не стартовал после fence + запас gate→журнал.** Прогон, прошедший gate за мгновение до fence,
   пишет строку журнала уже после него — это прогон «в полёте», не нарушение. Запас: Apps Script 1 мин, Cloud Run
   3 мин (зонд + acquire с одним повтором 30 с). Проверка по журналам потока по времени **старта** прогона
   (`started_at` или штамп в run_id/load_id):

   | Поток | Журналы |
   |---|---|
   | stocks | `WB_STOCKS_SNAPSHOTS` (snapshot_id Apps Script) |
   | statistics | `INGEST_RUNS` (orders, sales) + `ORD_INC_`/`SALE_INC_`/`SALE_RECON_` в RAW |
   | ads | `INGEST_RUNS` (ads) + `RAW_WB_ADV_COSTS_RUNS` без `_CR` |
   | finance | `FINANCE_LOADER_RUNS` (`FINDAILY_…_<цифры>`) |
   | supplies | `run_id DL_` в `RAW_WB_SUPPLIES` |
   | ref | `REF_SYNC_RUNS` |

   Плюс `LOADER_RUNS` prod загрузчиков потока. Старт после fence → **STOP**.
3. **ЗАПИСЫВАЮЩИЕ задания BigQuery в таблицы потока — и выполненные, и незавершённые** —
   `region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT`. Запись в таблицы потоков длится секунды (замер ревьюера за 7 суток:
   ≤ 21 с, p99 ≤ 6 с), поэтому проверки одних незавершённых недостаточно.
   - Какие задания: LOAD/COPY/DML/DDL с `destination_table` — таблица потока; `SCRIPT` с DML в таблицу потока в
     тексте — только незавершённый (его DML — дочерние задания со своим destination). Читающие скрипты не в счёт.
   - Фильтр по таблицам, а не по пользователю; только таблицы, которые не делит другой поток (`INGEST_RUNS` —
     по `loader_name`, п. 2). Shadow `…__CR` не совпадают (имя сравнивается целиком).
   - Чьё задание — по `user_email`: учётная запись владельца (`CUTOVER_APPS_SCRIPT_USER`) = Apps Script, prod-SA
     потока (`CUTOVER_STREAM_PROD_SAS`) = Cloud Run, иначе — посторонний.
   - Запись после fence допустима только как хвост прогона **дренируемого** писателя, начатого до fence:
     Apps Script — до fence + 1 мин, позже — только UPDATE/MERGE своей строки журнала в пределах 7 мин;
     Cloud Run — в пределах timeout Job'а + 5 мин (перечитки перед каждой записью у загрузчиков потоков нет).
     Запись второго (уже отсечённого) писателя или постороннего — **STOP**.
   - Незавершённые задания — ждём DONE.
4. **«Без исхода» — только живые на момент fence.** STARTED/RUNNING считается, если прогон стартовал не раньше
   fence − время жизни писателя (Apps Script — дренаж 7 мин, Cloud Run — timeout + 5). Такой без исхода →
   **UNKNOWN_OUTCOME**. Более старые STARTED — мёртвые прогоны (убиты лимитом до fence): они в доказательствах
   (`deadStartedBeforeFence`), но cutover не блокируют.
5. Только после этого — legacy watermark (MAX по журналу или RAW) и CAS на нового владельца.

### 3.4 Доказательство «писатели под gate» (перед fence)

`JOBS_BY_PROJECT`, выполненные запросы без ошибки, текст **начинается** с метки gate точки входа и `SELECT owner`.
По **каждой плановой точке входа** потока (`profiles.schedules`):
- Apps Script — метка `WRITER_GATE:AS:<поток>:<точка входа>:v1`, пользователь = `CUTOVER_APPS_SCRIPT_USER`;
- Cloud Run — метка `WRITER_GATE:CR:<поток>:<загрузчик>:v1`, пользователь = prod-SA потока
  (`CUTOVER_STREAM_PROD_SAS`);
- окно: ежедневная точка входа — 26 ч, недельная (аудит рекламы) — 8 суток.

Не засчитываются: `wbWriterGateStatus()` (своя метка), перечитка перед записью, ручные запросы других пользователей,
сам координатор (его запрос начинается с `SELECT`). Нет доказательства хотя бы по одной точке входа или не заданы
учётные записи — fence не ставится. Поэтому до fence prod-Scheduler'ы Cloud Run потока должны работать: при
владельце APPS_SCRIPT их прогоны штатно отсекаются (`WRITER_NOT_OWNER`) и тем самым дают доказательство (§7 п. 6).

После активации (CLOUD_ACTIVE/MONITORING) любое записывающее задание в таблицы потока не от prod-SA потока → **STOP**
с отсечением: Apps Script пишет от учётной записи человека, это различимо (проверка раз в час и в моменты решения).

## 4. Gate Cloud Run, commit-маркеры и первый прогон

`cli.ts` проверяет владельца **только при `ENVIRONMENT=prod`** и только для загрузчиков потоков. Проверка идёт
**до** DRY_RUN, lease и handler:

| Ситуация | Поведение |
|---|---|
| владелец `CLOUD_RUN` | прогон идёт, epoch запоминается (`ctx.writerFence`) |
| иначе | `writer_fenced`, код `WRITER_NOT_OWNER`, severity INFO, exit 0. Ни данных, ни строки `LOADER_RUNS` |
| ошибка чтения, нет строки, мусор | `WRITER_GATE_UNAVAILABLE`, exit 1 (fail-closed) |
| перед COMPLETE в `LOADER_RUNS` | повторная проверка. Владелец или epoch сменились → `WRITER_EPOCH_CHANGED`: COMPLETE не пишется, lease закрывается ERROR (это исход, не данные), exit 1 |

Shadow gate не проверяет: он пишет только `__CR`. Карта загрузчик → поток: `cloud/src/ownership/streams.ts`.
Остатки (`stocks`) проверяются только при `WRITER_GATE_STOCKS=1` у Job'а (§8). Запрос gate входа начинается с
метки `/* WRITER_GATE:CR:<поток>:<загрузчик>:v1 */` — доказательство для координатора (§3.4); перечитка перед
COMPLETE — `WRITER_RECHECK:CR:…` (не доказательство).

**Остаточное окно Cloud Run.** Между перечиткой перед COMPLETE и самим UPDATE — миллисекунды. Окно между gate и
записью данных закрывает дренаж Cloud Run при откате: timeout Job'а + 5 мин, задания BigQuery, запас gate→журнал.

**Ограничение — без правок веток потоков.** Общий способ закрывает только финальный COMPLETE в `LOADER_RUNS`. Это
маркер, который видят `V_INGEST_HEARTBEAT` и гейт витрины. Собственные commit-маркеры потоков живут в их ветках,
которые здесь не правятся. Для них есть две готовые точки, обе — по одной строке в ветке потока:
- `await ctx.writerFence?.assertEpoch('<стадия>')` непосредственно перед маркером;
- для DML — `ownershipAssertSql(...)`: ASSERT по владельцу и epoch **в той же транзакции**, перед MERGE/UPDATE.

| Поток | Commit-маркер (ветка потока) | Как первый прогон Cloud Run подхватывает legacy watermark | Почему пропуска нет (проверка в MONITORING) |
|---|---|---|---|
| stocks | `manifestFinalize(COMPLETE)` в `WB_STOCKS_SNAPSHOTS` (`stocks/index.ts`); вью берёт последний COMPLETE | Снимок — полное состояние за сутки; legacy = `MAX(period_to)` COMPLETE | Цепочка сутки COMPLETE от legacy без разрыва > 1; дубль-снимок за сутки → ROLLBACK_REQUIRED. **M1 координатором не ведётся** (§8) |
| statistics | один load job пакета в RAW (`statistics/index.ts` `appendRows`) — сам он и есть коммит; `LOADER_RUNS` COMPLETE | Загрузчик читает watermark из **таблицы**: `MAX(last_change_date)` строк `ORD_INC_`/`SALE_INC_` (`statistics/bq.ts`). Это та же таблица и те же префиксы, что у инкремента Apps Script. Legacy фиксируется тем же выражением после дренажа | (а) отметка не откатилась; (б) Cloud Run не писал строк старше legacy; (в) MAX строк инкремента со стартом **до** первого прогона Cloud Run = legacy, т.е. первый прогон прочитал ровно её; (г) первая ночная сверка (7 суток) накрыла legacy продаж |
| ads | маркер `RAW_WB_ADV_COSTS_RUNS` после данных окна (`ads/sources.ts`); `LOADER_RUNS` 'ads' COMPLETE | Окно от даты запуска: расходы D−14…D−1, fullstats D−7…D−1. Legacy = MAX `logical_period` COMPLETE 'ads' | Разрыв между legacy и COMPLETE-периодами Cloud Run ≤ 7 суток (перекрытие окна) |
| finance | MERGE COMPLETE отчёта в `FINANCE_REPORT_LOADS` (`finance/run.ts` `mergeComplete`), финализация недели | Очередь `FINANCE_REPORT_LOADS` общая: DISCOVERED/ERROR, оставленные Apps Script, Cloud Run дочитывает. Discovery смотрит сегодня−3…сегодня | Разрыв ≤ 3 суток + цикл понедельника |
| supplies | шапка `RAW_WB_SUPPLIES` после goods (`supplies/index.ts`) | Объекты выбираются сравнением списка WB с известным состоянием RAW: новый / не завершён / `updated_dt` новее / окно 14 суток по `factDate` — от даты прошлого прогона не зависит. Отметка — `MAX(updated_dt)` (для журнала) | «Состояние»: разрыва по датам не проверяем — `MAX(snapshot_ts)` устаревает (Apps Script пишет только изменённые объекты и с 31.10 не пишет вовсе, токен), см. §8.1 |
| ref | указатель `REF_ACTIVE_VERSION` — одна DML (`refsync/index.ts` `activate`) | Каждая версия — полное состояние листа | Пропуска быть не может; downstream — указатель переключён версией Cloud Run |

Проверки покрытия statistics читают RAW. Поэтому они идут не на каждом tick, а в момент решения: CLOUD_ACTIVE →
MONITORING и перед COMPLETED. `period_gap` дешёвый (`LOADER_RUNS`) и проверяется на каждом tick.

## 5. Gate Apps Script

**Точки входа.** `wbWriterGate_(stream)` стоит после `ScriptLock` и до любой записи — BigQuery, лист, Script
Properties. Вставлена в 10 точек входа одной строкой, больше в них ничего не менялось:

| Точка входа | Где стоит gate |
|---|---|
| `runWbStocksSnapshot`, `runWbOrdersIncremental`, `runWbSalesIncremental`, `runWbSalesNightReconcile`, `runWbSuppliesDaily`, `runRefSync` | в самой функции |
| `runWbAdsDaily`, `runWbAdsDailyCatchUp` | в общем `runWbAdsDailyCore_`: лок берётся там |
| `runWbFinanceDaily` | в `finDailyRun_`: там лок; это закрывает и ручной `wbFinDailyRunManual` |
| `loadWbAdsCostsAudit` | первой строкой: своего лока нет |

Исход gate:
- владелец ≠ APPS_SCRIPT → `SKIPPED_FENCED` (`console.log`), выход без записи;
- ошибка или нет строки → `SKIPPED_GATE_UNAVAILABLE` (`console.error`), fail-closed.

Запрос gate: `SELECT owner, epoch … WHERE stream = @s`, `maximumBytesBilled` 20 МБ, без кэша.

**Остатки (M1).** Gate потока `stocks` по умолчанию **выключен** (Script Property `WB_WRITER_GATE_STOCKS ≠ '1'`):
ни в точке входа, ни перед записью строка владения не читается. Ручная процедура MIG1B работает как раньше (§8).

**Метки.** Текст запроса к таблице владения начинается с метки:
- `/* WRITER_GATE:AS:<поток>:<точка входа>:v1 */` — **только** gate точки входа (`wbWriterGate_(stream, entry)`,
  точка входа обязательна): доказательство для координатора (§3.4);
- `WRITER_GUARD:AS:<поток>:v1` — перечитка перед записью;
- `WRITER_GATE_STATUS:AS:<поток>:v1` — ручной `wbWriterGateStatus()`.

**Перед каждой записью.** Все записывающие вызовы BigQuery в коде загрузки идут через `wbBqJobsQuery_`,
`wbBqJobsInsert_`, `wbBqTablesInsert_`, `wbBqTablesPatch_`, `wbBqTablesRemove_`. Это 15 файлов, замена одного токена. Статический тест
запрещает прямые вызовы вне `WriterGate.gs`; исключения — Unitka R3 и КИЗ, у них свои таблицы вне потоков.

Если запись идёт в таблицу потока (load, DML, DDL, патч схемы), владелец перечитывается **непосредственно перед**
отправкой задания:

| Результат перечитки | Исход |
|---|---|
| не APPS_SCRIPT | `WRITER_NOT_OWNER` |
| epoch ≠ epoch точки входа | `WRITER_EPOCH_CHANGED` |
| не прочитать | `WRITER_GATE_UNAVAILABLE` |

Задание в BigQuery не уходит. Это закрывает запись после потери владения посреди исполнения, в том числе между
подготовкой load и его отправкой. Цена — один SELECT ~10 МБ на запись.

**Исключение — закрыть свою строку журнала.** UPDATE/MERGE в `INGEST_RUNS`, `FINANCE_LOADER_RUNS`,
`WB_STOCKS_SNAPSHOTS`, `REF_SYNC_RUNS` пропускается, только если одновременно:
- исполнение прошло gate точки входа (есть поколение входа);
- запрос ссылается (в тексте или параметре) на id строки, которую **открыло это исполнение** под владением.
  Id запоминается при INSERT с параметром `run_id`/`snapshot_id` и при load строк журнала;
- в MERGE нет `WHEN NOT MATCHED` (вставки);
- для остатков при включённом gate — только исходом `ERROR` (COMPLETE снимка после потери владения запрещён).

Без исключения строка осталась бы STARTED навсегда и дренаж почти всегда давал бы UNKNOWN_OUTCOME. Новые строки
журналов (INSERT/load), чужие строки и данные — под проверкой.

**Таблицы рекламы — ровно 9 таблиц M3** (`ads/targets.ts`), без префикса. `RAW_WB_ADV_SEARCH_CLUSTERS` M3 не
переносит: кластеры остаются за Apps Script и после cutover рекламы, gate их не отсекает.

**Запрещённые формы.** Статический тест: в путях потоков нет copy-/extract-заданий, `LOAD DATA`,
`EXECUTE IMMEDIATE`, `CALL` — записи мимо обёрток.

**Все legacy-пути записи** проходят через те же обёртки и закрыты, даже без точки входа: тогда проверяется владелец
таблицы. Проверено replay-тестом node:vm при `owner = CLOUD_RUN`:

| Путь | Что пишет | Чем закрыт |
|---|---|---|
| `runWbDailyRefresh`, `backfillWbOperationalPilotLast14Days`/`Last30Days` | заказы/продажи/реклама RAW, `INGEST_RUNS` | обёртки (`bqLoadRows_`, `bqQuery_`, `ingestRunQuery_`) |
| `importWbOrdersFromApiInternal_`, `importWbOrdersFromApi*`, `importWbSalesReturnsFromApiInternal_`, `importWbSalesReturnsFromApi*` | `RAW_WB_ORDERS`, `RAW_WB_SALES_RETURNS` | обёртки |
| `importWbStocksFromApiInternal_`, `importWbStocksFromApi` | остатки | обёртки |
| `importWbFinanceFromApiInternal_`, `importWbFinanceFromApi*`, `importNewWbFinanceReportsFromFolder`, `wbFinanceBackfillBQ`, `wbFinanceBackfillAutoTick` (триггер 5 мин), `wbFinDeepBackfillRegister`, `wbFinDailyRunManual` | `RAW_WB_FINANCE`, `FINANCE_*` | обёртки; `wbFinDailyRunManual` — ещё и gate точки входа |
| Ads RAW меню: `loadWbAdsRawLast7Days`, `loadWbAdsRawPeriod*`, `loadWbAdsCosts*` (Backfill90, Operational, BootstrapPass, Recheck, Last7Days), `loadWbAdsFullstats*`, `loadWbAdsCampaignsRaw`, `loadWbAdsSearchClustersRaw*`, `loadWbAdsQueryBidsRaw`, `loadWbAdsQueryStatsRaw`, `wbAdsQueryStatsBackfill*`, `wbAdsFullstatsMonth*`, `wbAdsClustersJob*` | `RAW_WB_ADV_*` | обёртки (9 таблиц M3). Исключение — `loadWbAdsSearchClustersRaw*`, `wbAdsClustersJob*`: `RAW_WB_ADV_SEARCH_CLUSTERS` M3 не переносит, кластеры остаются за Apps Script |
| `runWbAdsDailyCatchUp` | реклама | gate в `runWbAdsDailyCore_` + обёртки. Чтение журнала до лока — только SELECT |
| `runWbSuppliesBackfill`, `wbSuppliesResetBackfill`, `wbSuppliesEnsureTables` | поставки RAW | обёртки (`wbsExecDml_`, `bqLoadRows_`, `wbBqTablesInsert_`) |
| `refEnsureAll_` (`CREATE OR REPLACE VIEW REF_SKU_MASTER`) | вью справочника | обёртка: `REF_SKU_MASTER` отнесена к потоку ref |
| `wbOrdersIncrementalBootstrap`, `wbSalesIncrementalBootstrap` | только Script Properties | BigQuery не пишут. При возврате владения watermark пересинхронизирует gate, см. ниже |
| `wbAdsBqCreateViews()`, `bqCreateFinanceView()` | вью `V_ADV_*`, `V_WB_FINANCE*` | **не закрыты картой таблиц** — вью не таблицы потоков. Запрет CLAUDE.md «не запускать» остаётся в силе |

Листы (`IMPORT_LOG_*`, `WB_ADS_STATUS`) — диагностика, не данные потоков. Gate точки входа их не трогает при
отсечении. При потере владения посреди исполнения прогон падает с ошибкой, и строку лога в лист он может записать.

**Возврат владения (откат).** Заказы и продажи хранят watermark в Script Properties, а Cloud Run двигал его в
таблице. Без пересинхронизации Apps Script перечитал бы и дописал всё, что уже записал Cloud Run.

Как это работает:
- gate помнит поколение (`WB_WRITER_GATE_EPOCH_<STREAM>`);
- при смене поколения у statistics он ставит `WB_ORDERS_/WB_SALES_LAST_CHANGE_WATERMARK` из таблицы тем же
  выражением, что Cloud Run;
- без отметки поколение считается равным 1, и до первого переключения gate ничего не пишет;
- ошибка пересинхронизации → fail-closed.

**Остаточное окно (честно).** Между перечиткой владельца и приёмом задания BigQuery проходят миллисекунды. Уже
принятое задание дорабатывает после fence. DML Apps Script **не** сделан условным в той же транзакции: код
загрузчиков проверяет `numDmlAffectedRows` одиночного DML, у скрипта его нет, и это сломало бы прежнее поведение.
Окно закрывает дренаж §3.3: 7 мин + отсутствие незавершённых заданий в таблицы потока. Для Cloud Run DML можно
сделать условным через `ownershipAssertSql`. Load-задания условными быть не могут — их закрывает тот же дренаж.

## 6. Координатор: команды и эксплуатация

Job `ingest-cutover-prod`, `args = ["cutover"]`. Это плоскость управления: без lease и без строки `LOADER_RUNS`.

| env | значение |
|---|---|
| `CUTOVER_ACTION` | `tick` (Scheduler) / `status` / `fence` / `activate` / `rollback` / `resolve` |
| `CUTOVER_REASON` | `resolve`: причина оператора, не короче 10 символов — пишется в журнал переходов |
| `CUTOVER_RESOLVE_TO` | `resolve` из STOP/UNKNOWN_OUTCOME: `LEGACY_FENCED` (дренаж к Cloud Run) или `CLOUD_FENCED` (к Apps Script). Из BLOCKED — не нужен (→ SHADOW_VALIDATING) |
| `CUTOVER_STREAM` | поток; пусто — все (только `tick` и `status`) |
| `CUTOVER_AUTO_STREAMS` | потоки, где `tick` сам ставит fence; по умолчанию пусто |
| `CUTOVER_INCLUDE_STOCKS` | `1` — вести и остатки, только после приёмки, §8 |
| `CUTOVER_LEGACY_DRAIN_MIN` | 7…120 |

Команда — разовое исполнение. Пример: `gcloud run jobs execute ingest-cutover-prod
--update-env-vars CUTOVER_ACTION=status,CUTOVER_STREAM=statistics` (делает владелец).

Exit 1 и алерт по `jsonPayload.code` дают:
- переход в STOP, UNKNOWN_OUTCOME, ROLLBACK_REQUIRED или BLOCKED по `CUTOVER_WRITER_NOT_GATED`;
- нарушение инварианта;
- отказ хранилища;
- отклонённая команда.

Scheduler `ingest-cutover-tick` срабатывает каждые 15 мин и создаётся PAUSED. Остатки (`coordinated: false`) tick
пропускает без единого запроса.

Стоимость — по минимуму тарификации 10 МБ на таблицу в запросе (оценка, не замер):

| Состояние потока | Запросов | В сутки |
|---|---|---|
| состояния Apps Script, обычный tick | чтение строки (1) | 96 × 10 МБ ≈ 0,96 ГБ |
| состояния Apps Script, первый tick часа | + `LOADER_RUNS` + квалификация (2) | 24 × 20 МБ ≈ 0,48 ГБ |
| COMPLETED | чтение строки | ≈ 0,96 ГБ |
| переход (дренаж, мониторинг) | 3–7, плюс `JOBS_BY_PROJECT` | несколько ГБ, только дни перехода |

Пять потоков в покое ≈ 7 ГБ/сутки ≈ **0,21 ТиБ/мес**. Плюс Apps Script — один SELECT ~10 МБ на каждую запись
в таблицы потока. После завершения переключений Scheduler можно поставить на паузу.

## 7. Порядок внедрения (каждый шаг — решение владельца)

0. **SQL проверен парсером BigQuery** (только dry-run): `tools/bq_dryrun_cutover_sql.py`, результат —
   `docs/ops/evidence/INGEST_CUTOVER_SQL_DRYRUN_2026-11-11.txt`. Перед применением — повторить на текущем коде.
1. **DDL и seed** (`sql/ops/ingest_writer_ownership.sql`). До этого шага gate нигде не выкатывать: без строки оба
   писателя fail-closed. `stocks_owner` выставить по факту MIG1B (на поведение не влияет, пока gate остатков
   выключен). Readback: 6 строк, `epoch = 1`.
2. **Права и Job.** `CUTOVER_COORDINATOR_ENABLED=true` → `infra.yml` targeted plan:
   - ожидаемо только create (SA, своя роль, IAM таблиц, чтение строки владения у `sa-loaders-prod` и
     `sa-deployer`, Job, Scheduler на паузе, политика алерта);
   - любой update/delete → STOP.
   - Чтение у `sa-deployer` нужно предпроверке `deploy-prod` (п. 5): без него выкат образа в Job'ы потоков
     fail-closed.
3. **Gate в Apps Script** — одно действие через редактор, §9. Readback: `wbWriterGateStatus()`.
4. **Чтение строки владения prod-SA потоков.** `CUTOVER_GATE_READER_STREAMS='["statistics", …]'` — только для
   потоков, чей prod-SA уже создан флагом `mN_prod_enabled`. `sa-loaders-prod` получает чтение на шаге 3.
5. **Образ с gate в Cloud Run** — `deploy-prod` по потокам. Первый шаг после снимка Job'ов —
   **предпроверка** (`tools/writer_gate_deploy_check.py`): если в охвате режима есть Job'ы под gate (Job'ы M2–M5;
   `wb-stocks-prod` — только с `WRITER_GATE_STOCKS=1`), то таблица владения обязана содержать 6 строк, а SA каждого
   такого Job'а — иметь табличное чтение. Иначе выката нет. Только чтение: `bq head`, `bq get-iam-policy`.
   Остатки: gate выключен, выкат образа на поведение M1 не влияет.
6. **Снять паузу prod-Scheduler'ов потока при владельце APPS_SCRIPT** (образ с gate уже выкачен, п. 5).
   В ветках потоков prod-Scheduler'ы создаются `paused = true` с пометкой «снимать на cutover после снятия
   триггера Apps Script». С координатором порядок другой: при владельце APPS_SCRIPT prod-прогоны Cloud Run штатно
   получают `WRITER_NOT_OWNER` (exit 0, ничего не пишут) и дают доказательство gate (§3.4). Без этого fence не
   ставится. Триггеры Apps Script **не снимаются** вовсе.
7. **Проверка: все prod-писатели видят своего владельца.**
   - Apps Script: `wbWriterGateStatus()`.
   - Cloud Run: в логах ближайших штатных прогонов — `writer_gate_allow` / `writer_fenced` с ожидаемым owner и epoch,
     ни одного `WRITER_GATE_UNAVAILABLE`.
8. **Координатор и квалификация.**
   - **Email учётной записи, от которой работает Apps Script, — персональные данные, репозиторий публичный.**
     Владелец заводит **GitHub Secret** `CUTOVER_APPS_SCRIPT_USER` (Settings → Secrets and variables → Actions →
     Secrets), не Variable: значения Variables в логах Actions не маскируются, Secrets — маскируются.
   - `infra.yml` передаёт его как `TF_VAR_cutover_apps_script_user: ${{ secrets.CUTOVER_APPS_SCRIPT_USER }}`.
     В Terraform переменная `sensitive`: в plan — «(sensitive value)». В env Job'а `ingest-cutover-prod` идёт
     отдельным блоком (sensitive-значение нельзя класть в `for_each`). Env Job'а под `ignore_changes`: значение
     ставится при создании Job'а. Смена значения — пересоздание Job'а или `gcloud run jobs update
     --update-env-vars` владельцем, без печати значения.
   - Координатор email не печатает: в лог, журнал переходов (`INGEST_CUTOVER_LOG.evidence`) и доказательства идёт
     только `apps-script-owner:<sha256, 12 hex>`; посторонний человек — `user:<хэш>`. Сервисные аккаунты видны как
     есть. Это закреплено тестами (vitest и pytest).
   - Порядок: secret → apply → `deploy-prod only_job=ingest-cutover` (оба Job'а) → разовый `CUTOVER_ACTION=status` и
     разовый `ingest-qualify-prod` с `QUALIFY_DRY_RUN=1` → resume `ingest-qualify` и `ingest-cutover-tick`.
9. **Только потом автоматика:** для конкретного потока `auto_cutover_enabled = true` (колонка) или
   `CUTOVER_AUTO_STREAMS`. До этого fence — только командой `fence` по ACK cutover потока.

## 8. Остатки (M1) и потоки M2–M5

### 8.0 Что поменять в runbook'ах M2–M5 (ветки потоков здесь не правятся)
| Runbook | Сейчас | С координатором |
|---|---|---|
| `MIG_M2_STATISTICS_RUNBOOK.md`, `MIG_ADS_RUNBOOK.md`, `MIG_FINANCE_RUNBOOK.md`, `MIG_SUPPLIES_REF_RUNBOOK.md` — cutover | снять триггер Apps Script → resume prod-Scheduler | resume prod-Scheduler'ов потока **заранее**, при владельце APPS_SCRIPT (они отсекаются gate и дают доказательство); переключение — `fence` координатора (или `auto_cutover_enabled`), триггеры Apps Script не снимать |
| те же — откат | pause prod-Scheduler → вернуть триггер | `CUTOVER_ACTION=rollback`: дренаж Cloud Run и возврат Apps Script; Scheduler и триггеры не трогать |
| те же — приёмка паритета | ручной запуск `sql/mig_*_parity.sql`, отчёт в PR | вердикты пишет `ingest-qualify-prod` в `INGEST_QUALIFICATION` (§13); ручной запуск — для разбора |
| те же — Terraform prod | prod-SA без чтения таблицы владения | `CUTOVER_GATE_READER_STREAMS` += поток (чтение строки владения), иначе Job fail-closed |
| те же — выкат prod | — | предпроверка `deploy-prod` (§7 п. 5) требует таблицу и чтение у SA Job'а |
| `MIG_SUPPLIES_REF_RUNBOOK.md` | журнала прогонов Apps Script у поставок нет | см. §8.1 |

### 8.1 Поставки: журнал прогонов Apps Script (описание, не сделано)
У `runWbSuppliesDaily` нет журнала прогонов: дренаж видит Apps Script только по `run_id DL_…` в RAW и по заданиям
BigQuery, а «STARTED без исхода» у поставок не бывает. Нужно (отдельная правка Apps Script по ACK, вне этой ветки):
- в `runWbSuppliesDaily` после gate — `ingestRunStart_('supplies', <сегодня МСК>, 'SCHEDULED')` и в конце
  `ingestFinalizeByStatus_` (тот же `INGEST_RUNS`, что у заказов/продаж/рекламы); `run_id` = `DL_…` прогона;
- `ingestFinalizeByStatus_` по исходу: COMPLETE, если все объекты списка обработаны, иначе ERROR с кодом
  (`SUPPLIES_TOKEN`, `SUPPLIES_PARTIAL`);
- координатор: `INGEST_RUNS (loader_name = 'supplies')` добавить в журналы поставок (§3.3 п. 2), а легаси-отметкой
  поставок брать время последнего COMPLETE прогона из журнала (не `MAX(snapshot_ts)`).
До этого отметка поставок — `MAX(updated_dt)` (только журнал), а непрерывность — «состояние» (§4).

- **M1: выбран вариант «gate остатков выключен по умолчанию в обоих писателях».** До приёмки координатора:
  - Apps Script не читает строку `stocks` (Script Property `WB_WRITER_GATE_STOCKS` не задана);
  - Cloud Run не читает её (`WRITER_GATE_STOCKS` у `wb-stocks-prod` не задан);
  - координатор поток не ведёт (`coordinated: false`), tick пропускает его без запросов;
  - остатки переключаются утверждённой процедурой MIG1B — снятие триггера с readback и resume Scheduler (§A.7),
    и выкат образа с gate её не ломает.
- **Перевод остатков под gate** — отдельное решение после приёмки координатора, всё в одном окне:
  1. строку `stocks` привести к факту (после MIG1B — `CLOUD_RUN`). Это пока единственная ручная DML строки владения;
     команды `set-owner` нет сознательно;
  2. `WB_WRITER_GATE_STOCKS = '1'` в Script Properties;
  3. `WRITER_GATE_STOCKS=1` у `wb-stocks-prod`;
  4. `CUTOVER_INCLUDE_STOCKS=1` у координатора.
- Подготовку shadow M2–M4 координатор не блокирует:
  - gate действует только в prod;
  - ветки потоков от этой ветки не зависят;
  - координатор живёт в своей ветке.

## 9. Пакет одноразовых действий владельца (Apps Script)

Одно действие через редактор `evetis-wb-analitics`, после шага 1 §7:
1. Добавить файл `WriterGate.gs` (из `apps-script/ingestion/`).
2. Обновить 15 изменённых файлов. Изменения точечные: строка gate в 10 точках входа и замена прямых вызовов
   BigQuery на обёртки:
   - `IngestRunLog.gs`, `Refsync.gs`, `WbAdsRawLoader.gs`;
   - `WbBigQuery.gs`, `WbOrdersBigQuery.gs`, `WbOrdersLoader.gs`;
   - `WbSalesIncremental.gs`, `WbSalesReconcile.gs`, `WbSalesReturnsBigQuery.gs`;
   - `WbStocksBigQuery.gs`, `WbStocksSnapshot.gs`, `Wbadsbigquery.gs`;
   - `Wbadsdaily.gs`, `Wbfinancedaily.gs`, `Wbsuppliesrawloader.gs`.
3. Запустить `wbWriterGateStatus()`. Он только читает. Ожидаемо: у всех потоков владелец как в seed и «Apps Script
   пишет» там, где APPS_SCRIPT.

Триггеры **не трогать**. Ни снимать, ни ставить — механизм на них не опирается. Новых OAuth-scopes нет: advanced
service BigQuery уже подключён.

## 10. Откат механизма

1. Пауза `ingest-cutover-tick`. Строки владения остаются как есть, писатели продолжают им подчиняться.
2. Вернуть владение Apps Script — командами `rollback` по потокам. Ручной DML строки владения не делается.
3. Gate Apps Script: вернуть прежние версии 15 файлов и удалить `WriterGate.gs`. Только после пункта 2 — иначе
   Apps Script начнёт писать при владельце CLOUD_RUN.
4. Gate Cloud Run: выкатить предыдущий digest. Без строки владения новый образ fail-closed, поэтому таблицы
   удалять **после** отката образов.
5. Terraform: `CUTOVER_COORDINATOR_ENABLED=false` → plan: только destroy ресурсов файла. Своя роль
   `ingestCutoverBigQuery` и `ingestQualifyBigQuery` при удалении уходят в soft-delete на 7 дней (37 дней до окончательного удаления):
   повторное включение флага в это время либо восстанавливает её (`gcloud iam roles undelete`, делает владелец),
   либо требует нового `role_id`. Учитывать перед повторным включением.
6. Таблицы `wb_ops.INGEST_*` удаляются вручную и последними. Журнал переходов — сохранить экспортом.

## 11. Чего механизм НЕ гарантирует

- **Атомарности между Apps Script и Scheduler нет.** Есть fencing (`epoch`) + перечитка перед каждой записью + дренаж
  с доказательствами.
  - Задание, принятое BigQuery за миллисекунды до fence, дорабатывает после него. Его ждёт дренаж.
  - Запись не в BigQuery (лист, Script Properties) посреди исполнения не fencится. Это не данные потоков.
- Собственные commit-маркеры потоков в Cloud Run не проверяют epoch, пока в ветках потоков не добавлены
  `assertEpoch` / `ownershipAssertSql` (§4). Окно закрывает дренаж Cloud Run при откате: timeout Job'а + 5 мин +
  задания BigQuery.
- Квалификацию координатор только читает (dataViewer). Пишет её загрузчик `qualify` (§13): его вердикты не
  сильнее файлов паритета потоков — их логика проверена в ветках потоков, а не здесь.
- Доказательство gate — по меткам в начале текста запросов `JOBS_BY_PROJECT` от своих пользователей. Его может
  подделать только тот, кто выполнит запрос **от учётной записи владельца или prod-SA** с точной меткой точки
  входа. Оно подтверждает, что код gate исполнялся по каждой плановой точке входа, но не что **каждый** путь
  записи обновлён — это закрывают обёртки Apps Script и статический тест.
- Закрытие своей строки журнала после потери владения разрешено (§5): у журналов это исход, не данные.
- Предпроверка `deploy-prod` видит только табличные права на строку владения. Чтение через права на датасет она
  не засчитывает (ложный отказ — в безопасную сторону).
- В MONITORING Apps Script отличим от Cloud Run не для всех потоков. У поставок и REF одинаковый формат `run_id` у
  обоих писателей, поэтому после активации «второй писатель» там виден только через gate, а не через журналы.
  У ночной сверки продаж журнала нет вовсе.
- Расписания в окне fence — снимок живых триггеров 11.11.2026: orders 00/06/12/18:44, sales 00/06/12/18:17,
  reconcile 04:18, stocks 06:23, ads 05:07, catch-up 06:08 и 08:18, finance 07:26/12:24/18:35, ref 08:22,
  supplies 02:29. Модель ±15 мин. Живые триггеры меняются вручную — перед `auto_cutover_enabled` снимок сверить заново.

## 12. Проверка SQL парсером BigQuery (dry-run)

`tools/bq_dryrun_cutover_sql.py` — ручной, в CI не запускается: нужен токен владельца. Отправляет запросы
**только** с `"dryRun": true` на `www.googleapis.com/bigquery/v2/projects/<p>/queries`.

Что проверяется:
- весь SQL `store.ts`, `gate.ts`, `WriterGate.gs` (запрос gate и пересинхронизация watermark) — выгрузка
  `cloud/scripts/emit_cutover_sql.ts`;
- DDL целиком и по операторам.

Таблиц `wb_ops.INGEST_*` ещё нет, поэтому режимы такие:

| Режим | Что проверяет |
|---|---|
| FULL | запрос как есть (`wb_raw`, `INFORMATION_SCHEMA`) — синтаксис и семантика |
| CTE | `wb_ops.INGEST_*` заменены производной таблицей той же схемы (из DDL) |
| SCRIPT | синтаксис многооператорного скрипта |
| PROXY | выражения DML (условие CAS, SET, VALUES) как SELECT над производной таблицей |
| SCHEMA | имена колонок INSERT/SET есть в схеме DDL |

Результат 11.11.2026: `docs/ops/evidence/INGEST_CUTOVER_SQL_DRYRUN_2026-11-11.txt`. Ошибки ревью B1 (`at`) и B2
(UNION DATE/STRING) этим прогоном ловятся; проверено на старом тексте CAS: `Unexpected keyword AT`.

## 13. Писатель квалификации — загрузчик `qualify`

Job `ingest-qualify-prod` (тот же образ, `args = ["qualify"]`, плоскость управления: без lease и `LOADER_RUNS`),
Scheduler `ingest-qualify` 10:40 МСК, создаётся PAUSED. Под тем же флагом `cutover_coordinator_enabled`.

- **SQL паритета** — файлы потоков `sql/mig_m2_statistics_parity.sql`, `mig_ads_parity.sql`, `mig_finance_parity.sql`,
  `mig_supplies_parity.sql`, `mig_ref_parity.sql` (в ветках потоков). `deploy-shadow` копирует `sql/mig_*_parity.sql`
  в `cloud/parity-sql/` перед сборкой, образ — `/app/parity-sql`. **Нет файла — поток пропускается**
  (`MISSING_PARITY_SQL`, warn): потоки будут слиты позже.
- Исполняются только операторы `SELECT`/`WITH` (иначе отказ), по порядку `QUERY 1…`. Статистика: первые полные
  сутки shadow — `QUALIFY_D_FROM_STATISTICS` (подставляется в `DATE '…' AS d_from`).
- **Группы — ровно группы профиля координатора** (тест):

| Поток | Группа | Источник |
|---|---|---|
| statistics | `STATS_ORDERS`, `STATS_SALES` | QUERY 1 (`stream`, `day`, `verdict`) |
| statistics | `STATS_SALES_RECONCILE` | журнал shadow `sales-reconcile`: COMPLETE за сутки → PASS, только ERROR → FAIL |
| ads | `ADS_COSTS` / `ADS_STATS` | QUERY 1 `costs_verdict` / `verdict` |
| ads | `ADS_COSTS_AUDIT` | журнал shadow `ads-costs-audit` |
| finance | `FINANCE_DAILY` | QUERY 1 `verdict` |
| finance | `FINANCE_MONEY` | QUERY 1: `rows_mismatch + manifest_diff + dup_rrd > 0` → **FAIL** (необъяснённое денежное расхождение); отчёт только на одной стороне → DRIFT_EXPLAINED; иначе PASS |
| finance | `FINANCE_WEEKLY` | QUERY 3: неделя × тип, худший по типам; сутки = понедельник после недели (`week_start + 7`) |
| supplies / ref | `SUPPLIES` / `REF` | QUERY 1 `verdict` |

- Вердикты: EXACT → PASS, DATA_DRIFT → DRIFT_EXPLAINED, FAIL → FAIL; NO_DATA, NO_RUN, MISSING, SOURCE_GAP,
  PENDING, REVIEW → MISSING (день не засчитывается).
- `image_digest` — образ последнего shadow-прогона потока за сутки (`LOADER_RUNS`, environment = 'shadow').
- **Запись** — один `INSERT … SELECT … FROM UNNEST(JSON_QUERY_ARRAY(@payload))` в `INGEST_QUALIFICATION`, только
  добавление. `QUALIFY_DRY_RUN=1` — посчитать и записать в лог, без INSERT.
- **Права** `sa-ingest-qualify`: своя роль (`bigquery.jobs.create`, `config.get`); чтение `wb_raw` целиком (таблицы
  `__CR` появляются и исчезают с флагами shadow — табличную привязку к ним не выдать); запись — только
  `INGEST_QUALIFICATION`. Ни Scheduler, ни Run, ни секретов.
- Отказ запроса одного потока не мешает остальным; итог — `QUALIFY_FAILED` (exit 1, алерт).
- Стоимость: ~0,1 ГБ на поток в сутки (минимум 10 МБ на таблицу).
