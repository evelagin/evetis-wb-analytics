# PR-Mart3 — Оркестрация MART (runWbMartDaily). ДИЗАЙН REV4

**Дата:** 2026-07-31  **Статус:** REV4 — вшиты 3 финальных контракта (APPROVE WITH CHANGES по REV3).
Новый аудит дизайна не требуется. **PR-Mart3a — в работе** (код готов: `sql/mart3/*`, `apps-script/IngestRunLog.gs`,
`docs/MART_PR3A_INGEST_RUNS_2026-07-31.md`); PR-Mart3b — после 1–2 суток наблюдения журнала.
**Оркестратор:** Cloud Run Job + Cloud Scheduler, паттерн [[migration-to-cloud-native]].
**Связано:** MART_MART2_CONTRACTS_2026-07-28.md, MIGRATION_CLOUDRUN_DESIGN_2026-07-24_rev2.md, sql/mart/pr_mart2b_sku_daily.sql.

## РЕШЕНИЯ (зафиксированы)
fail-closed · FACT+MART в одном job · один успешный **D-1** build/день (target = вчера, Europe/Moscow) ·
ads freshness через heartbeat, business-лаг маркируется `ADS_LAGGED`.

---

## БЛОК 1 (REV3) — технический журнал загрузчиков вместо RAW.loaded_at

### Почему RAW.loaded_at не heartbeat (подтверждено фактами 31.07)
1. **Zero-row дыра:** при успешном ране с 0 новых строк в RAW ничего не пишется → `MAX(loaded_at)` не двигается → fail-closed
   гейт ложно заблокирует build. Доказывает наличие ДАННЫХ, а не факт УСПЕШНОГО ЗАПУСКА.
2. **Формат:** `loaded_at`/`load_ts` — STRING вида `'2026-07-31 11:31:47'` **без таймзоны** (ISO-парсер даёт NULL;
   TZ неявная, судя по ads-рану 05:12 ≈ триггер ~05:00 МСК — Europe/Moscow). Для гейта нужен настоящий TIMESTAMP.
3. **Журнала нет:** `wb_raw.LOADER_RUNS` содержит ТОЛЬКО `shadow/stocks` (Cloud Run). Apps Script-загрузчики
   orders/sales/ads не логируют раны никуда. `FINANCE_LOADER_RUNS`/`REF_SYNC_RUNS` есть только у finance/REF.

### Новая таблица `wb_raw.INGEST_RUNS` (журнал Apps Script-загрузчиков)
| колонка | тип | смысл |
|---|---|---|
| loader_name | STRING | `orders` \| `sales` \| `ads` (далее — прочие) |
| logical_period | **DATE** | сутки (МСК), которые закрывает ран (тип DATE, не строка — сравнение с `target_date` без парсинга) |
| run_id | STRING | id рана загрузчика |
| status | STRING | `STARTED` \| `COMPLETE` \| `ERROR` |
| started_at / completed_at | TIMESTAMP | **настоящие TIMESTAMP** (UTC), не строки |
| rows_fetched / rows_loaded | INT64 | 0 — допустимо и НЕ влияет на heartbeat |
| trigger_type | STRING | `SCHEDULED` \| `MANUAL` |
| source | STRING | `apps_script` \| `cloud_run` — различать после миграции |
| error_code / error_message | STRING | при ERROR |

### Модель записи `INGEST_RUNS` — одна строка на run [REV4 §2]
`INSERT` (STARTED) → `UPDATE` **той же строки по `run_id`** до `COMPLETE` или `ERROR`. Обязательные свойства:
- **`run_id` уникален** (`INS_<LOADER>_<ts>_<uuid8>`); одна строка на попытку;
- **`COMPLETE`/`ERROR` терминальны:** финализация идёт с условием `WHERE run_id=@run_id AND status='STARTED'`;
- **повторная финализация идемпотентна** (0 affected rows, терминальный статус не перезаписывается);
- **сбой логирования не скрывает исходную ошибку загрузчика:** функции логгера гасят свои исключения
  (`Logger` + возврат `null`/`false`), исходное исключение пробрасывается без подмены;
- физика: `PARTITION BY DATE(started_at)`, `CLUSTER BY loader_name, status, logical_period`.
- реализация: DML через `BigQuery.Jobs.query` (**не** streaming `insertAll` — там `UPDATE` запрещён, пока строка
  в streaming buffer); NAMED-параметры вместо конкатенации (`error_message` с кавычками/переводами строк).
Без этого Apps Script мог бы создать несколько несогласованных строк STARTED/COMPLETE, и heartbeat выбрал бы ложный успех.

**Ключевое:** строка пишется при КАЖДОМ успешном ране, даже если `rows_loaded=0` → zero-актив день проходит гейт.

### Единая вью `wb_raw.V_INGEST_HEARTBEAT` (абстракция источника)
`UNION ALL` из: `INGEST_RUNS` (Apps Script) + `LOADER_RUNS` (Cloud Run, environment='prod') [+ `FINANCE_LOADER_RUNS`,
`REF_SYNC_RUNS` — для наблюдения]. Колонки: `loader_name, logical_period, status, started_at, completed_at, rows_loaded, source`.
**Mart3 freshness читает ТОЛЬКО эту вью** → миграция загрузчика Apps Script → Cloud Run **не потребует менять гейт Mart3**
(меняется лишь `source` строки). Это снимает связность между треком миграции и треком MART.

### Контракт гейта (fail-closed) — LATEST-ATTEMPT за `target_date` [REV5 §1]
Правило: **ПОСЛЕДНЯЯ попытка** каждого обязательного загрузчика за `logical_period = target_date`
(по `started_at`, tie-break `run_id`) обязана быть `COMPLETE`. Проверять «существует хоть один `COMPLETE`»
**нельзя**: ранний ночной успех «заморозил» бы гейт, даже если поздний catch-up упал —
```
00:10 COMPLETE → 08:00 ERROR/PARTIAL → 09:00 build ⇒ EXISTS нашёл бы ранний COMPLETE и собрал витрину на неполных данных.
```
Для hourly orders/sales это критично: ночной успех мог не захватить поздние правки WB, а поздний catch-up упал.
Гейт берёт последнюю попытку и требует, чтобы **именно она** была `COMPLETE`. Обязательный список источников
задаётся слева через `LEFT JOIN`, чтобы полностью отсутствующий загрузчик дал `FALSE`, а не исчез из результата (fail-closed):
```
WITH required AS (SELECT l AS loader_name FROM UNNEST([@loaders]) AS l),
     latest AS (
       SELECT loader_name, status, completed_at,
              ROW_NUMBER() OVER (PARTITION BY loader_name
                                 ORDER BY started_at DESC, run_id DESC) AS rn
       FROM wb_raw.V_INGEST_HEARTBEAT
       WHERE loader_name IN UNNEST([@loaders])
         AND logical_period = @target_date                                        -- попытка закрывает ИМЕННО эти сутки
         AND started_at    >= TIMESTAMP(DATE_ADD(@target_date, INTERVAL 1 DAY), 'Europe/Moscow')  -- и стартовала после их полуночи
     )
SELECT r.loader_name,
       COALESCE(l.status = 'COMPLETE' AND l.completed_at IS NOT NULL, FALSE) AS covers_target
FROM required r
LEFT JOIN (SELECT * FROM latest WHERE rn = 1) l USING (loader_name);
```
Гейт проходит, только если `covers_target = TRUE` для **всех** `@loaders`. Иначе → `MART_FRESHNESS` ERROR + алерт.
`started_at >= полночь(target+1)` ⟹ `completed_at >= полночь` (строже, безопаснее): ран, стартовавший до полуночи,
относится к другому `logical_period` и в набор не попадает.

**Следствие — `logical_period` пишется ДЕТЕРМИНИРОВАННО, даже при нулевых данных** (никогда не выводится из
фактически полученных строк, иначе zero-row контракт снова станет неоднозначным):
- **orders / sales** (hourly incremental): последний **полностью закрытый** день = `DATE(ран, МСК) − 1`
  (любой ран суток X закрывает X−1; ран 31.07 11:31 → `2026-07-30` — ровно то, что спросит гейт при build D-1);
- **ads** (daily): **целевой период API** = `period_to` (эмпирика 31.07: ран 05:12 → `period_to = 2026-07-30` = D-1).
- **Secondary sanity (в лог, НЕ блокирует):** `MAX(business_date)` по RAW с корректным парсингом
  `SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at)` / `SAFE.PARSE_DATE('%Y-%m-%d', ...)`.
- **ADS_LAGGED:** heartbeat ads свежий, но `MAX(ads business date) < target_date` → build ПРОХОДИТ, помечается
  `ads_lagged=TRUE` (рекламные KPI дня трактуются как provisional). Эмпирика 31.07: ран 05:12 покрывает `period_to=2026-07-30`
  = D-1 → **норма = покрытие D-1**; отставание на 1 день → `ADS_LAGGED`; >1 дня → FAIL (порог подтверждается в rollout-окне).
- **finance — вне гейта** (недельный лаг), heartbeat пишется только для наблюдения.

### Следствие по последовательности работ
Журнал живёт в Apps Script → **PR-Mart3a** (INGEST_RUNS + `logIngestRun_()` в orders/sales/ads + вью V_INGEST_HEARTBEAT,
правки в Apps Script вносит владелец) → 1–2 суток наблюдения, что строки COMPLETE пишутся ежедневно →
**PR-Mart3b** (Cloud Run job `mart`). Гейт Mart3b без наполненного журнала работать не может (fail-closed).

---

## БЛОК 2 (REV3) — state machine execution guard (явно)

| Состояние ключа `environment × loader='mart' × logical_period=D-1` | Решение |
|---|---|
| `COMPLETE` | **skip** (EXIT_OK) — день уже собран, повторный фаер ничего не делает |
| `ERROR` | **retry разрешён** — новая попытка захватывает lease (ERROR не активен) |
| `STARTED` свежий (age < TTL) | **lease занят** → skip `ALREADY_RUNNING` (защита от параллельного двойного build) |
| `STARTED` старше TTL (**stale**) | **повторный захват разрешён** — вставляется НОВАЯ строка STARTED со своим run_id (recovery) |

- TTL (`MART_LEASE_TTL`) = **30 мин** (текущий `DEFAULT_STALE_STARTED_MS`). Реализовано в `runManifest.isActive()` — переиспользуем.
- **Инвариант, который обязан выполняться:** `max_build_duration < TTL < scheduler_interval`, здесь `build (~мин) < 30 мин < 60 мин`.
  - `TTL < 60 мин` → если фаер 09:00 умер жёстко (Cloud Run execution убит, строка осталась STARTED), к 10:00 lease протух → **10:00 подхватит**.
  - `build < TTL` → пока build идёт, следующий фаер не начнёт второй параллельный build.
- **Контрольная точка rollout:** замерить фактическую длительность ручного прогона; при росте близко к TTL — поднять TTL, сохранив `TTL < 60 мин`.
- Реальность подтверждает необходимость: в `LOADER_RUNS` уже висит `STARTED` строка от 27.07 (shadow/stocks) без `completed_at` —
  ровно тот случай, который без stale-TTL заблокировал бы все последующие попытки.

---

## БЛОК 3 (REV3) — хранение freshness snapshot / ADS_LAGGED: `wb_mart.MART_RUNS`

`V_MART_RUN_LOG` над текущей схемой действительно недостаточно: в `LOADER_RUNS` нет места для снимка свежести и флагов.
**Решение — отдельная типизированная таблица `wb_mart.MART_RUNS`** (LOADER_RUNS остаётся исключительно для guard/lease):

| колонка | тип | смысл |
|---|---|---|
| run_id | STRING | **сквозной** id (Cloud Run → LOADER_RUNS → FACT → MART → здесь) |
| environment / target_date | STRING / DATE | среда и D-1 |
| status | STRING | `COMPLETE` \| `ERROR` |
| started_at / completed_at / duration_ms | TIMESTAMP / INT64 | тайминги job |
| freshness_json | STRING (JSON) | снимок per-source: `{loader, last_complete_at, covers_target, business_max_date}` |
| ads_lagged | BOOL | флаг provisional-рекламы |
| ads_business_max_date | DATE | фактическое покрытие ads на момент build |
| steps_json | STRING (JSON) | тайминги/статусы шагов (`sp_bootstrap_facts`, `sp_build_mart_sku_daily`, …) |
| mart_rows | INT64 | `COUNT(*) MART_SKU_DAILY` после publish |
| git_sha / image_digest | STRING | версия кода/образа |
| error_code / error_message | STRING | при ERROR |

**Почему отдельная таблица, а не `metadata_json` в LOADER_RUNS:** (1) не трогаем общую схему манифеста, которой владеет трек
миграции (её меняют контракты Mig); (2) типизированные колонки (`ads_lagged BOOL`, `target_date DATE`) пригодны для алертов и BI
без JSON-парсинга; (3) mart-специфичные поля не засоряют контракт загрузчиков.
`wb_mart.V_MART_RUN_LOG` = `LOADER_RUNS (loader='mart')` **JOIN** `MART_RUNS` **USING (run_id)` — одно окно наблюдения
(guard-состояние + богатая метадата). Запись в MART_RUNS — из handler'а, в обеих ветках (COMPLETE и ERROR).

### `ADS_LAGGED` обязан быть виден потребителю витрины [REV4 §3]
Хранить флаг только в `MART_RUNS` **недостаточно**: BI (или человек), читающий `MART_SKU_DAILY`, увидит день
с незавершённой рекламой как «расход рекламы = 0» и примет это за факт, а не за незрелые данные.
**Решение (рекомендация аудитора принята): два поля в самой витрине** `MART_SKU_DAILY`:
| поле | тип | смысл |
|---|---|---|
| `ads_lagged` | BOOL | TRUE, если реклама за `day` ещё не догружена (KPI рекламы дня — provisional) |
| `ads_business_max_date` | DATE | фактическое покрытие рекламы на момент build |

**Вычисляются внутри процедуры витрины, а не прокидываются оркестратором** — тогда поля корректны и при ручном `CALL`,
и при бэкфилле:
```sql
ads_cov AS (SELECT MAX(`date`) AS ads_max FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE `date` <= @build_as_of)
-- ads_business_max_date = ads_cov.ads_max
-- ads_lagged            = (ads_cov.ads_max IS NULL) OR (ads_cov.ads_max < day)
```
Флаг ставится **по строке** (`day`), а не только по build: дни внутри одного build имеют разную зрелость рекламы.
Снимок в `MART_RUNS` остаётся для наблюдения; на приёмке сверяем, что он согласован с полями витрины.
**Дашборд/BI:** день с `ads_lagged = TRUE` маркируется как provisional (не интерпретировать как «расход = 0»).

---

## Прочие уточнения (REV3)
- **Cron:** ОДИН Cloud Scheduler job `mart-daily-prod`, `schedule = "0 9,10,11 * * *"`, `time_zone = "Europe/Moscow"`
  (вместо трёх расписаний и ручного пересчёта в UTC; DST-безопасно).
- **IAM (уточнено по датасетам):**
  - `wb_raw`: **READ** — источники (`RAW_WB_*`, `V_WB_*`, `V_ADV_*`), `REF_SKU_MASTER`, `INGEST_RUNS`, `V_INGEST_HEARTBEAT`;
    **WRITE** — `LOADER_RUNS` (guard/lease пишет именно сюда, датасет `wb_raw`).
  - `wb_mart`: **READ+WRITE** — `FACT_*`, `MART_*`, `*__BUILD`, `REF_COST_MAP`, `V_WB_FINANCE_AMOUNTS_LONG*`, `MART_RUNS`, `_MART_BOOTSTRAP_LOCK`.
  - проект: `roles/bigquery.jobUser` (jobs.create), location **EU**.
- **D-1 (из REV2):** `target_date = logical_period = build_as_of = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)`.
- **Сквозной run_id (из REV2):** один `ctx.runId` в обе процедуры.

## Сопутствующие SQL-правки витрины (в этом же PR)
1. `sp_build_mart_sku_daily`: **+параметр `in_run_id`** (используется как `mart_run_id`; fallback GENERATE_UUID при ''/NULL);
   **убрать guard `build_as_of >= max_required_source_date`** (несовместим с D-1 и бэкфиллом) → оставить как sanity в лог;
   верхний guard `build_as_of <= CURRENT_DATE('Europe/Moscow')` сохраняется. Источники витрины уже bounded `<= build_as_of`.
2. **`sp_build_mart_sku_daily`: +колонки `ads_lagged BOOL`, `ads_business_max_date DATE`** в `MART_SKU_DAILY`
   (вычисляются внутри процедуры, по строке — см. REV4 §3). Соответственно расширяется ASSERT-гейт витрины:
   `COUNTIF(ads_business_max_date > build_as_of) = 0` (покрытие не может быть «из будущего» относительно границы build).
3. `sp_bootstrap_facts`: без изменений (уже принимает `in_run_id`).
4. Validation витрины: проверка «в штатном прогоне `build_as_of == CURRENT_DATE('Europe/Moscow') − 1`»
   и согласованность `ads_lagged` витрины со снимком в `MART_RUNS`.

## Rollout (из REV2, без изменений)
Scheduler **PAUSED** → ручной прод-run → validation (консервация=0, плотность spine, грейн, один run_id по цепочке) →
**same-day skip-тест** → **freshness-failure тест** (ERROR + алерт, витрина не тронута) → **resume** → наблюдение N дней.
Дополнительно на rollout: замер `duration_ms` (инвариант блока 2) и подтверждение порога `ADS_LAGGED`.

## План работ
**PR-Mart3a:** `INGEST_RUNS` DDL + `logIngestRun_()` в Apps Script orders/sales/ads + `V_INGEST_HEARTBEAT` → наблюдение 1–2 суток.
**PR-Mart3b:** `src/loaders/mart/*` (handler + freshness по вью), registry, `ctx.runId` в LoaderContext/cli, config-опциональность
WB/stocks-переменных, `MART_RUNS` DDL + запись, `V_MART_RUN_LOG`, правки витрины (§выше), Terraform (Job + 1 Scheduler + IAM), тесты
(freshness ok/fail, zero-row день, skip COMPLETE, stale-lease recovery, ADS_LAGGED) → PR → merge → infra apply → rollout.
