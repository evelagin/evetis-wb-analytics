# PR-Mart3b-1 — BigQuery-контракты + исполнимый loader `mart`. REV6 (на аудит кода).

**Дата:** 2026-08-03
**Статус:** REV6 — закрыт 1 operational-блокер аудита REV5 (честный failed-step для записи журнала COMPLETE) + нит. Локально зелёный (tsc/eslint 0, vitest 89 passed). В BigQuery НЕ применялось. НЕ мержить/применять до визы.
**План:** `docs/MART_PR3B_PLAN_2026-08-03.md` (REV3 APPROVED), подшаг PR3b-1.

---

## Правки REV6 (по замечанию аудита REV5)

1. **[БЛОКЕР] Сбой записи `MART_RUNS.COMPLETE` больше не маркируется как `mart_snapshot`.** После успешных проверок snapshot шаг закрывается `{step:'mart_snapshot', status:'OK'}`, затем `currentStep='mart_runs_complete'` + новый `stepStartMs` ПЕРЕД `writeMartRun(COMPLETE)`. Теперь при «freshness/FACT/MART/snapshot прошли, запись COMPLETE упала, запись ERROR прошла» журнал честен: `[freshness_gate:OK, sp_bootstrap_facts:OK, sp_build_mart_sku_daily:OK, mart_snapshot:OK, mart_runs_complete:MART_ERROR]`. **Тест именно этого сценария:** фейк с `completeWriteFails` (падает ТОЛЬКО MERGE со status='COMPLETE'; ERROR проходит) → loader падает; одна строка ERROR; `mart_snapshot=OK`; `mart_runs_complete=MART_ERROR`; `mart_snapshot` с ошибочным статусом отсутствует. +тест успеха: `mart_snapshot=OK` ровно один раз.
2. **[нит] SQL-коммент** перед ASSERT: «снимок покрытия рекламы» → «снимок АКТИВНОСТИ рекламы» (документация соответствует контракту activity vs coverage).

---

## Правки REV5 (по замечаниям аудита кода REV4)

1. **[БЛОКЕР 1] Fail-closed guards ручного entry-point** — новый чистый модуль `loaders/mart/manualGuard.ts`, вызывается в `mart_manual.ts` ДО loader:
   - `environment !== 'prod'` → **`MART_MANUAL_ENV`** (процедуры публикуют production `wb_mart`; отдельного shadow-датасета нет — запуск под `ENVIRONMENT=shadow` перезаписал бы прод с меткой 'shadow' в журнале);
   - `isCanonicalDate()` — реальная календарная дата, не только regex (`2026-02-31` → **`MART_MANUAL_DATE`**);
   - `targetDate !== d1Moscow()` → **`MART_MANUAL_TARGET`** (историческая дата откатила бы витрину; строго D-1). Исторический backfill — отдельный будущий явно опасный entry-point со своим контрактом, в production runner НЕ включён.
   Тесты (5): shadow отклонён; историческая дата отклонена; «сегодня» отклонено; `2026-02-31` отклонена; prod+D-1 допущен. +4 unit `isCanonicalDate`.
2. **[БЛОКЕР 2] Честный `steps_json`.** `freshness_gate: OK` пишется ТОЛЬКО после успешных проверок shape И coverage. При ошибке в `steps_json` пишется КОНКРЕТНЫЙ упавший шаг с кодом ошибки (`currentStep`/`stepStartMs`: ctx_invariant / freshness_gate / sp_bootstrap_facts / sp_build_mart_sku_daily / mart_snapshot), а не общий `step=error`. Тесты (4): FRESHNESS_SHAPE и FRESHNESS_GATE → в steps_json НЕТ `freshness_gate=OK`, есть failed-step с кодом; успех → ровно один `freshness_gate=OK`; ошибка build → failed-step `sp_build_mart_sku_daily`, `step=error` отсутствует.
3. **[неблок] NULL-safe единственность снимка**: в ASSERT процедуры и validation §5 `COUNT(DISTINCT col)` → `COUNT(DISTINCT TO_JSON_STRING(STRUCT(col AS value)))` — смесь NULL+значение больше не проходит незамеченной.

---

## Правки REV4 (по замечаниям аудита кода REV3)

1. **[ДЕФЕКТ 1] `CTX_INVARIANT` теперь пишет терминальную строку `MART_RUNS.ERROR`.** Проверка инварианта `logicalPeriod===targetDate` перенесена ВНУТРЬ общего `try` (после `startedAtIso`/`startMs`/`steps`), поэтому рассинхрон контекста → `catch` → одна строка `MART_RUNS` ERROR (`error_code=CTX_INVARIANT`), FACT/MART не вызываются. Иначе был бы `LOADER_RUNS`=ERROR без строки `MART_RUNS` (нарушение «одна терминальная строка на запуск»). Тест обновлён.
2. **[ДЕФЕКТ 2] Validation выбирает последний COMPLETE по ЗАВЕРШЕНИЮ, не старту.** Во всех §4/§6/§7/§8 `ORDER BY started_at DESC` → **`ORDER BY completed_at DESC, run_id DESC`** (без lease в PR3b-1 возможен started-раньше-но-completed-позже). +§3b: у `COMPLETE` обязателен `completed_at` (ноль строк).
3. **[нит] §6 — NULL-safe сравнение** `IS NOT DISTINCT FROM` (nullable `ads_activity_max_date`).
4. **[нит] `mart_manual.ts` — полный `randomUUID()`** в run_id (не `slice(0,8)`): не сокращаем ключ, вокруг которого построен MERGE.

---

## Правки REV3 (по замечаниям аудита кода REV2)

1. **[БЛОКЕР 1] read-back MART_RUNS сверяет ИДЕНТИЧНОСТЬ, не только статус.** После MERGE read-back по run_id возвращает `status, environment, target_date, git_sha` и требует совпадения с записываемыми. Иначе fail-closed: `MART_RUNS_DUP` (≠1 строка) / `MART_RUNS_CONFLICT` (иной статус) / **`MART_RUNS_IDENTITY_CONFLICT`** (тот же run_id, но другая дата/среда/версия кода — коллизия, НЕ идемпотентный повтор). +3 теста (другая target_date/environment/git_sha).
2. **[БЛОКЕР 2] MERGE = mutating DML — синхронизирован IAM-контракт.** DDL-комментарий, заголовок `bq.ts` («SELECT/CALL/MERGE») и эта нота: **Runtime SA нужны `bigquery.tables.getData` И `bigquery.tables.updateData`** на `wb_mart.MART_RUNS` (dataset-level `BigQuery Data Editor` для wb_mart или эквивалент). Insert-only доступ упадёт на первом MERGE — учесть в Terraform PR3b-2.
3. **[БЛОКЕР 3] ads: честно развели freshness и активность.** Поля переименованы `ads_business_max_date`→**`ads_activity_max_date`**, `ads_lagged`→**`ads_activity_lagged`** (в MART_SKU_DAILY, MART_RUNS, V_MART_RUN_LOG, процедуре, коде, validation). Это ДИАГНОСТИКА активности, а НЕ индикатор покрытия/полноты — комментарии DDL/процедуры про «покрытие рекламы» исправлены. Полнота гарантируется heartbeat-гейтом; `ads_activity_lagged` означает лишь «в последний(е) день(дни) не было рекламных строк». BI не должен показывать это как «лаг данных».
4. **[обяз. синк validation]** +§8: опубликованная витрина ПРИНАДЛЕЖИТ последнему COMPLETE-журналу (`wrong_run=0`, `wrong_date=0` относительно последнего `MART_RUNS` COMPLETE prod).

---

## Правки REV2 (по замечаниям аудита кода REV1)

1. **[БЛОКЕР 1] `MART_RUNS` — идемпотентная запись + защита от cross-state.** Обычный INSERT заменён на **`MERGE ON run_id WHEN NOT MATCHED THEN INSERT`** + **read-back**: после записи читаем `COUNT(*)`/`status` по run_id и требуем ровно 1 строку с ОЖИДАЕМЫМ терминальным статусом. Иначе RAISE `MART_RUNS_DUP` / `MART_RUNS_CONFLICT`. Повтор джобы или ambiguous write не плодит дубли и не создаёт пару COMPLETE+ERROR. **Cross-state тесты** добавлены (повторный COMPLETE идемпотентен; ERROR→COMPLETE даёт CONFLICT, ERROR не перезаписан).
2. **[БЛОКЕР 2] `readMartSnapshot(run_id, target_date)` привязан к прогону.** Перед COMPLETE доказываем: витрина непуста, `COUNT(DISTINCT mart_run_id)=1`, `COUNT(DISTINCT build_as_of_date)=1`, ноль строк с чужим `mart_run_id`/`build_as_of_date`. Иначе fail-closed: `MART_EMPTY` / `MART_RUN_MISMATCH` / `MART_DATE_MISMATCH`.
3. **[БЛОКЕР 3] generic-cli для mart убран из PR3b-1.** `mart` НЕ регистрируется в `registry.ts` (иначе cli считал бы current-day, а нужен D-1, и run-lease не доказан). Всё wiring mart в общий CLI (+D-1, +`LOADER_RUNS`) перенесено в PR3b-2. В PR3b-1 mart запускается ТОЛЬКО через `mart_manual.ts`. D-1 вынесен в чистый `loaders/mart/targetDate.ts` (`d1Moscow`), покрыт тестом Europe/Moscow (утро/пересечение суток/переход месяца). Feature-flag (`MART_CLI_ENABLED`/`cliGuard`) удалён как более не нужный.
4. **[БЛОКЕР 4] ADS: coverage (heartbeat) ≠ activity (FACT).** Убран `ADS_TOO_STALE` fail-closed по `MAX(FACT_ADS.date)` — успешный zero-row день (ран OK, рекламных строк нет) давал бы ложный stale. **Fail-closed ПОКРЫТИЕ рекламы за target_date доказывает heartbeat-гейт loader'а** (`ads covers_target`). В процедуре `ads_business_max_date`/`ads_lagged` — ОПИСАТЕЛЬНЫЕ метаданные активности (не гейт): `ads_lagged = (ads_business_max_date IS NULL OR < build_as_of)`.
5. **[extra] Инвариант `ctx.logicalPeriod === ctx.targetDate`** проверяется в начале handler (`CTX_INVARIANT`).
6. **[extra] freshness-результат — РОВНО уникальные orders/sales/ads** (иначе `FRESHNESS_SHAPE`, build не запускается).

> Примечание: правка #4 уточняет план §5.3 (там был трёхуровневый `ADS_TOO_STALE` по FACT). Намерение плана (не публиковать на устаревшей рекламе) сохранено, но реализуется через heartbeat-покрытие, а не через FACT-активность.

---

## Файлы PR (16)

**SQL:** `sql/mart/pr_mart2b_sku_daily.sql` (правки процедуры REV6); новые `sql/mart3/pr_mart3b_mart_runs.sql` (DDL `MART_RUNS`+`V_MART_RUN_LOG`), `sql/mart3/pr_mart3b_validation.sql`.
**TS (`cloud/`):** `loaders/types.ts` (+`runId`/+`targetDate`), `config.ts` (WB-опциональность; martCliEnabled удалён), `cli.ts` (проброс `runId`/`targetDate` для stocks/noop; mart недоступен намеренно), `loaders/registry.ts` (без mart), новые `loaders/mart/{bq,index,targetDate,manualGuard}.ts`, `mart_manual.ts`, `package.json` (скрипт `mart:manual`), тесты `mart_loader.test.ts`+`mart_targetDate.test.ts`+`mart_manualGuard.test.ts`.

---

## Изменения процедуры `sp_build_mart_sku_daily` (REV6)

1. **+`IN in_run_id STRING`** (третий параметр). `v_run_id = COALESCE(NULLIF(in_run_id,''), GENERATE_UUID())`.
2. **Снят guard `build_as_of < max_required`** (несовместим с D-1/бэкфиллом; роль — внешний heartbeat-гейт). `build_as_of > сегодня(МСК)` СОХРАНЁН. Проверки непустоты FACT_* сохранены.
3. **ADS — ДИАГНОСТИКА активности, без гейта (блокеры #4/#3-REV3).** `ads_activity_max_date = MAX(FACT_ADS.date <= build_as_of)` (может быть NULL/раньше build_as_of — норма). `ads_activity_lagged = (ads_activity_max_date IS NULL OR < build_as_of)` — «в последний(е) день(дни) не было рекламных строк», НЕ индикатор полноты. RAISE `ADS_TOO_STALE` УБРАН. Покрытие доказывает heartbeat.
4. **+колонки `ads_activity_max_date DATE`, `ads_activity_lagged BOOL`** — build-level, ЕДИНЫ во всех строках. **+ASSERT:** `ads_activity_max_date > build_as_of = 0`; `COUNT(DISTINCT ads_activity_max_date)<=1`; `COUNT(DISTINCT ads_activity_lagged)<=1`.
5. Lock `_MART_BOOTSTRAP_LOCK` — без изменений.

---

## Loader `mart` — поток (fail-closed)

1. инвариант `logicalPeriod===targetDate`;
2. freshness-gate LATEST-ATTEMPT по `V_INGEST_HEARTBEAT` — форма (ровно orders/sales/ads) + `covers_target` для всех (иначе `FRESHNESS_GATE`);
3. `CALL sp_bootstrap_facts(@run_id)` → `CALL sp_build_mart_sku_daily(@target_date, NULL, @run_id)`;
4. `readMartSnapshot(@run_id,@target_date)` — привязка публикации (блокер #2);
5. одна терминальная запись в `MART_RUNS` (MERGE+read-back); ERROR-запись не маскирует исходную ошибку (try/catch, rethrow исходного).

---

## Ручной controlled-run (DoD PR3b-1)

```
cd cloud && npm run build
GCP_PROJECT_ID=project-fa311fc0-4d87-4781-986 BQ_RAW_DATASET=wb_raw ENVIRONMENT=prod \
  GIT_SHA=$(git rev-parse --short HEAD) \
  npm run mart:manual -- --target-date=2026-08-02
```

Без `--target-date` — D-1 Europe/Moscow. loader `mart` НЕ читает `WB_TOKEN_ANALYTICS`. IAM: `bigquery.jobUser` (jobs.create) + READ+WRITE `wb_mart` (CALL процедур; **MERGE+SELECT** в MART_RUNS — нужны `tables.getData`+`tables.updateData`, dataset-level BigQuery Data Editor или эквивалент) + чтение `wb_raw.V_INGEST_HEARTBEAT`.

---

## Проверки (локально, зелёные)

- `npm run typecheck` 0 · `npm run lint` 0 · `npm test` **89 passed** · `npm run build` эмитит `dist/mart_manual.js`, `dist/loaders/mart/{targetDate,manualGuard}.js`.
- Тесты покрывают: happy (1 строка COMPLETE, rows проброшены); ads activity-лаг = COMPLETE (не ошибка); `CTX_INVARIANT` (→ одна строка MART_RUNS ERROR, FACT/MART не вызваны); `FRESHNESS_SHAPE` (нехватка/дубль); `FRESHNESS_GATE`; ошибка build → `MART_ERROR`; запись MART_RUNS не маскирует исходную ошибку; `MART_EMPTY`/`MART_RUN_MISMATCH`/`MART_DATE_MISMATCH`; MERGE идемпотентность + cross-state (`MART_RUNS_CONFLICT`) + **identity-conflict** (другая target_date/environment/git_sha → `MART_RUNS_IDENTITY_CONFLICT`); `d1Moscow` Europe/Moscow.
- **SQL в BigQuery НЕ применялся.**

---

## Порядок применения (после merge + визы)

1. `sql/mart3/pr_mart3b_mart_runs.sql` (DDL, до первого прогона).
2. правки процедуры `sql/mart/pr_mart2b_sku_daily.sql`.
3. ручной прогон `npm run mart:manual`.
4. `sql/mart3/pr_mart3b_validation.sql` §1–§8 (§8 — витрина принадлежит последнему COMPLETE-журналу).

## Открытое допущение
`sp_bootstrap_facts(in_run_id STRING)` предполагается существующей (один STRING-параметр) — проверить сигнатуру в проде до ручного прогона.

## Дальше (PR3b-2)
Доказать 4-state `runManifest` (+stale-recovery без перезаписи), зарегистрировать mart в CLI с D-1 (`d1Moscow`) + `LOADER_RUNS` run-lease, Cloud Run Job + Terraform/IAM, расширить `V_MART_RUN_LOG` до JOIN.
