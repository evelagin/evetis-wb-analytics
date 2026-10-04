# WB ADS RESUMABLE RECOVERY — OWNER REVIEW

## LOCAL GRAIN SAFETY FIX — 2026-10-01 (awaiting independent review)

Owner ACK: local implementation only. This section supersedes the original implementation claims
only for RAW grain validation. No independent final review or deployment approval is claimed.

Root defect: prepared payload/readback equality did not enforce effective-view business-key
uniqueness. Conflicting rows could pass readback and permit terminal COMPLETE.

Changed code: `WbAdsResume.gs` adds `adsResumeValidateRawGrain_`, invoked before `io.publish`;
its duplicate/payload errors cause FAILED batch + BLOCKED manifest. `WbAdsResumeRuntime.gs`
adds the same guard at publish entry before any RAW destination access, including durable replay.
Exact key: date / advertId / nmId / appType / source_level; only processed_status=raw, matching
Wbadsbigquery.gs V_ADV_CAMPAIGN_STATS. The complete flattened stats array is checked at once.
No date truncation, extra grain fields, response normalization, aggregation or winner selection.

Exact duplicates are rejected too. Existing latest-wins view documents historical selection,
not permission for duplicate keys in one prepared publication. See CONTRACT.md pre-publication
section for STRING/NULL semantics, batch boundaries and evidence. Separate booster destination
is not assigned the stats key. The guard runs before either RAW destination is published.

Inspectable result: RAW_GRAIN_DUPLICATE in batch.error and continuation.reason; batch FAILED,
manifest BLOCKED, continuation.needed=false, no RAW publisher/finalizer invocation, no COMPLETE.
Durable prepared evidence is retained. No cleanup/unblock/rollback is attempted.

Nine added focused tests cover conflicting metrics, nested response portions, exact and semantic
duplicates, unique keys, durable payload replay/distant array boundaries, core publisher exclusion,
direct-adapter guard and exact STRING/NULL/date/source_level grain. Existing normal/crash/resume,
activation, scope, lock and continuation tests still run. Test fixtures now include prepared.stats.

Current local validation:
* Focused resume: 51 passed (42 existing + 9 new).
* Step5A: 17/17 passed.
* Full cloud Vitest: 65 files passed; 1413 tests passed, 19 skipped (1432 total).
  Includes mart_loader 25, martWiring 6, mart_manualGuard 9, mart_targetDate 3 = 43 passed.
* Python tools/tests: 2328 passed, 1 deselected, exit 0 (345.40 s).
* SQL validator using the existing temporary venv: PASS, 53 objects, C1–C18.
  Initial system Python lacked sqlglot; rerun with existing dependencies succeeded; no install.
* Six affected .gs syntax parses and git diff --check: PASS.
* impact_analysis: TIER0, 95 objects from broad ingestion mapping; required tools/tests run.

The credential-boundary test test_gcloud_is_unauthenticated_inside_agent_env remains excluded:
it invokes gcloud auth print-access-token, forbidden by the owner. No token command executed.
Full production verification/remote freshness is not claimed; prohibited live/Git operations were
not used. This task keeps the already-authorized dirty-tree scope and preserves unrelated files.

Schema, four pre-existing hook diffs and all other source remain unchanged versus the preceding
review package. Contract and this report are updated; patch/manifest are rebuilt from exact bytes.
Production, feature flags, triggers, credentials, mart 30.09, Unitka, Sheet/LCD and Tenancy/client_001
were not changed. Full cloud regression uses offline fixtures, not production executions.

---

## HISTORICAL IMPLEMENTATION REPORT (before grain safety fix)

Дата: 2026-10-01. Scope: LOCAL IMPLEMENTATION ONLY.
Base: `recovery/chz-automation-2026-10-01`, HEAD `b94ca335b98ce8146f925295f45d8789db5ed46c`.
Production, Sheet, LCD, Scheduler, triggers, credentials и Git refs/index не изменялись.
Нет deploy, Apps Script push/run, Cloud Run execution, production SQL или recovery по реальным ID.

## FILES CHANGED

Новые source:

* `apps-script/ingestion/WbAdsResume.gs` — state machine, scope/coverage validators.
* `apps-script/ingestion/WbAdsResumeRuntime.gs` — Apps Script/BQ adapter, recovery/daily/continuation entrypoints.

Минимальные opt-in hooks в существующих source:

* `apps-script/ingestion/Wbadsdaily.gs` — daily/catch-up dispatch с сохранением provenance.
* `apps-script/ingestion/WbAdsProbe.gs` — deadline-aware HTTP только при активном resume context.
* `apps-script/ingestion/Wbadsbigquery.gs` — bounded sink и metadata-only schema check в этом context.
* `apps-script/ingestion/WbAdsRawLoader.gs` — безопасный log вместо WB_ADS_STATUS Sheet в этом context.

Тесты: `tools/step5a_appsscript_tests/resume.test.js`.
Artifacts: `CONTRACT.md`, `schema.sql`, `REVIEW.md`, `OWN_CODE_DIFF.patch` в этом каталоге.
Другие файлы, включая ранее существовавшие untracked материалы, не редактировались.

## IMPLEMENTED ARCHITECTURE

Новый путь выключен по умолчанию. Existing daily/catch-up → opt-in dispatch → ScriptLock →
immutable scope → verified origin phases → durable prepared batch → RAW publication/readback →
checkpoint → completion validator → NEW terminal heartbeat → terminal manifest.

Исторический recovery не вызывает campaigns/costs, query bids/stats, mart, Unitka и не пишет в Sheet.
Daily режим сохраняет campaigns/costs semantics, но использует ограниченный по времени sink без DDL.
Optional query stages допустимы только после durable COMPLETE для текущего DAILY периода.

## RECOVERY MANIFEST CONTRACT

Полный контракт — CONTRACT.md. Новый объект: `wb_raw.ADS_FULLSTATS_RECOVERY`, только проект
`project-fa311fc0-4d87-4781-986`, EU. schema.sql — предложение, не выполненный DDL.
Таблица хранит immutable MANIFEST revisions и prepared BATCH payloads с SHA-256.
Никаких secrets; подготовленные business rows имеют тот же access boundary, что RAW.

Scope включает logical date, from/to, parent heartbeat, origin RAW run, полный snapshot/hash campaign set,
allowlist. Batches содержат состояние/attempts/классификацию каждого ID/receipts. Manifest содержит
processed/pending/failed/no_stats/skipped, timestamps, continuation state, no-progress, completion lineage.
Активный scope хранится в Script Property; logical date не вычисляется заново после полуночи.

## BATCH/RESUME SEMANTICS

До 50 IDs/batch, штатно до 2 завершённых batches/execution. Source stop оставляет 90 секунд перед
внутренним finish target; finish target на 30 секунд раньше Apps Script hard wall.
HTTP retry/backoff и BQ polling проверяют deadline. Cooldown между fullstats сохраняется между executions.

Prepared response сохраняется до RAW. Если процесс оборвался после принятой RAW записи, следующий
execution проверяет deterministic job и полный RAW readback, не повторяет source fetch и не удваивает строки.
После expiry job metadata сохранённые RAW + prepared payload остаются основанием для readback.
Конфликт payload/job или неполный readback блокирует завершение.

ScriptLock защищает active pointer, manifests, RAW publication и продолжение. Reconcile оставляет не более
одного собственного one-shot trigger. COMPLETE/BLOCKED его прекращают. Три попытки без прогресса или
более пяти admissions одного batch переводят recovery в BLOCKED. Автоматического force-unblock нет.

## COMPLETION SEMANTICS

Все campaigns/costs evidence и все IDs обязательны. `no_stats` допустим только из валидного успешного
ответа либо проверенного legacy marker. Незапрошенный ID, HTTP failure и budget stop не являются no_stats.
Нужны pending=failed=skipped=0 и подтверждённый sink каждого batch.

Исходный `ADS_PARTIAL` остаётся ERROR. Новый `INS_ADS_RESUME_<hash>` вставляется COMPLETE только после
всех проверок, имеет исходный logical date и читается обратно. Новая конкурирующая попытка того же дня
останавливает composite finalization. Existing mart latest-attempt/COMPLETE gate не менялся.

Изменение producer contract показано явно: новый успешный attempt может объединять origin phases и
несколько fullstats executions; lineage находится в новом manifest, схема INGEST_RUNS прежняя.
Успешный heartbeat не использует error_message как поле lineage. WB_ADS_STATUS не является полным
источником наблюдения после activation: новый путь публикует structured logs + durable journal.

## TEST RESULTS

`node --test tools/step5a_appsscript_tests/resume.test.js` — **42/42 PASS**, exit 0.
Реальные .gs загружаются в Node vm; Google/WB транспорт, часы и persistent state подменены.
В интеграционных тестах используется существующий production flattening код, а не его копия.

Покрыты все 18 требуемых категорий: multi-batch, deadline, write-before-checkpoint, repeat batch,
duplicate invocation, ScriptLock, 429/backoff, network/HTTP failure, legitimate no_stats, unprocessed ID,
midnight, snapshot/hash mismatch, no progress, terminal continuation, incomplete coverage,
effective-view key/dedupe, строгий downstream gate, date/ID scope.

Дополнительно: kill после landing; kill после второго RAW destination; expired job metadata;
corrupt RAW readback; latest-attempt conflict; historical phase readback/provenance; malformed JSON;
foreign IDs/dates; duplicate own triggers; feature/schema guards; optional work isolation;
catch-up skip COMPLETE/STARTED; ложные DONE/COMPLETE labels; cross-execution cooldown.
Incident allowlist 31 ID находится только в fixture. Остальные 400 IDs fixture синтетические:
это offline proof алгоритма, не копия полного production snapshot.

## REGRESSION RESULTS

| Проверка | Результат |
|---|---|
| `node tools/step5a_appsscript_tests/run.js` | 17/17 PASS, exit 0 |
| 4 существующих mart Vitest files: loader/Wiring/targetDate/manualGuard | 43/43 PASS, exit 0 |
| `python -m pytest -q tools/tests -k 'not test_gcloud_is_unauthenticated_inside_agent_env'` | 2328 PASS, 1 deselected, exit 0 |
| `python tools/validate_current_sql.py` | PASS, 53 objects, C1–C18, exit 0 |
| Syntax parse всех 6 затронутых .gs | PASS |
| ESLint новых .gs и test file: syntax/control-flow/unused-local checks | PASS |
| `git diff --check` | PASS |
| Duplicate resume function declarations | 23 функции, дублей нет |

Один unrelated credential-boundary test намеренно исключён: он запускает команду извлечения access token,
запрещённую owner ACK. Сама команда не запускалась. Это не скрытый PASS этого теста.
Системный Python не имел pytest. Hash-pinned dependencies из tools/requirements-sql-ci.txt установлены
в отдельный временный venv `/private/tmp/evetis-ads-resume-tests-jb6_w4xj`; глобальные packages не менялись.

## OWN DIFF REVIEW

OWN_CODE_DIFF.patch теперь включает весь implementation scope: шесть source files, тест,
schema.sql и CONTRACT.md, а также этот REVIEW.md как сопровождающее evidence/documentation.
REVIEW.md содержит исторический отчёт реализации, а не результат нового независимого review.
PACKAGE_MANIFEST.json находится рядом: hashes всех включённых файлов и самого patch;
manifest не включён в patch во избежание циклического hash. Patch применяется к указанному base HEAD.
Существующие hooks: 17 добавленных строк. Посторонние pre-existing файлы исключены.
Package rebuild не изменяет source/tests/schema/contract.

Review проверил: default OFF; fixed project/EU/BQ sink; no runtime DDL/Sheet status writes; no RAW pre-delete;
immutable dates/IDs; durable prepare-before-publish; full readback; conservative legacy no_stats import;
strict completion; original ERROR immutable; latest-attempt conflict; no optional work before COMPLETE;
scope of trigger cleanup; no credential/payload logging; recovery ID list only in tests.

Framework impact_analysis выполнен read-only. Результат TIER0, 95 affected objects вследствие широкой
карты всего ingestion каталога. Это не означает изменения 95 объектов: financial SQL, mart, Unitka,
Ozon и их schemas не редактировались. Local regressions не доказывают production data contracts.
Полный verify_task/live parity/data-suite запуск не выполнялся: этот этап local-only, его token/Git/live
операции не разрешены. Deployment/merge readiness и freshness относительно remote HEAD не объявляются.
Dirty-tree exception задан текущим owner ACK; исходные untracked файлы сохранены.

## KNOWN LIMITATIONS

* Нужны future schema deployment, actual Apps Script parity и runtime access checks; сейчас UNPROVEN.
* Native Apps Script HTTP/Advanced Service call нельзя прервать JavaScript deadline. Есть reserves и
  crash recovery, но абсолютной гарантии времени отдельного зависшего native call нет.
* Legacy parent/origin связываются summary + RAW timestamps/counts. Перед импортом 30.09 нужно подтвердить
  однозначность этого соответствия и полный snapshot; код не доверяет ручному COMPLETE.
* Один active recovery. BLOCKED требует разбора; периоды без исходного manifest не получают автоматический
  multi-date backlog. Завершение старого периода не доказывает готовность промежуточных пропущенных дней.
* Safety caps: pointer 8 KB, payload 5 MB, <10,000 rows/destination/batch, query cap 100 MB, nonempty snapshot.
  Превышение останавливается fail-closed; автоматического увеличения лимитов нет.
* Optional bids snapshot не восстанавливается за прошлую дату; поздний mandatory completion может его
  пропустить. Admission до optional вызова даёт at-most-once, а не ложную exactly-once гарантию.
* Alert routing не активирован. WB_ADS_STATUS alone после opt-in недостаточен.
* Повтор fullstats может изменить effective historical values внутри approved window; no_stats не удаляет
  старые значения. Physical RAW rollback не предусмотрен автоматическим DELETE.

## PROPOSED DEPLOY/ACTIVATION PLAN

Всё ниже — будущие действия, не выполнены:

1. Owner reviews exact source diff, schema/producer contract, flags, operational caps и monitoring change.
2. Отдельный ACK на deployment: установить actual Apps Script project/container/source parity, существующие
   permissions и отсутствие второго fullstats writer; сохранить before-image source/config/triggers.
3. Создать только новую recovery table по reviewed schema.sql, проверить schema/location/access реальной
   runtime identity. Не запускать wbAdsBqCreateViews, не менять существующие RAW/heartbeat schemas.
4. Deploy шести reviewed source files с flags OFF; подтвердить отсутствие activation и старые entrypoints.
5. Отдельный bounded recovery ACK: разрешить flag, точный historical spec, journal/RAW/heartbeat/property
   writes, before-image, abort conditions. Continuation activation — отдельная явная часть ACK.
6. Проверить approved bounded recovery, затем owner решает permanent daily routing/continuation activation.
   Контроль: pending снижается, batches подтверждены, один chain, budget сохраняется, false COMPLETE нет.
7. Подключить alerting на ADS_PARTIAL/BLOCKED, отсутствие прогресса/следующего execution и aging logical date.
   Расписание mart/Unitka сейчас не менять; их запуск требует собственного readiness/owner gate.

Abort: schema/access/parity mismatch, conflicting writer/latest attempt, scope/hash mismatch, unknown sink
outcome, bad readback, no progress/limits. Остановить dependent actions, сохранить evidence.
Rollback activation: по заранее одобренному scope выключить оба flags и удалить только own continuation;
не удалять durable journal/RAW. Возврат source — только reviewed before-image. Legacy daily не является
rollback данных. Исправляющий recovery разрешается отдельно, без ручного COMPLETE или LCD advancement.

## PROPOSED 30.09 RECOVERY PLAN

1. Read-only preflight повторно проверяет effective account/project/EU, текущий live state и отсутствие
   уже успешного superseding recovery. Сохранить before-images approved RAW keys/effective values,
   INGEST_RUNS, relevant phases и source/config. Сейчас recovery specification против production не применялся.
2. Из origin `ADSRAW_20261001_050732_749` и parent `INS_ADS_20261001050734_5253f5ba` получить полный 431-ID snapshot,
   вычислить hash и подтвердить 400 unique legitimate no_stats + exact 31 pending IDs из incident evidence.
   Проверить phase summary/time association, costs commit marker и RAW count. Любое расхождение — STOP.
3. После deployment и recovery ACK вызвать `resumeWbAdsFullstatsRecovery` с logicalDate/to=2026-09-30,
   from=2026-09-24, exact parent/origin, full snapshot/hash и allowlist из 31 ID. Не refresh campaigns/costs.
4. Expected writes: новая journal/pointer/cooldown state; только fullstats/booster для allowlist/окна;
   новый COMPLETE heartbeat исключительно после verified composite coverage. Никаких других logical dates.
   Число rows определяется WB response и ограничено safety caps, заранее выдуманный row count не задаётся.
5. Validation: 431/431 coverage, pending/failed/skipped=0, подтверждённые batches и RAW readback,
   исходный parent ERROR неизменён, новый latest COMPLETE того же logical date, отсутствие вне-scope writes.
6. STOP. Mart 30.09 и Unitka/Sheet/LCD остаются отдельными owner-gated этапами ранее подготовленного плана.

Rollback/recovery: сначала выяснить фактически подтверждённые jobs/rows; повторить только pending,
используя durable payload. Не удалять RAW для «очистки», не переписывать origin heartbeat. Если опубликованные
effective values неверны, подготовить bounded corrective plan с отдельным ACK и before/after diff.

## NEXT OWNER ACK REQUIRED

Сначала owner review этого локального patch и producer/schema contract. Ни review approval сам по себе,
ни зелёные тесты не являются ACK на deploy/trigger activation/production recovery. Следующее разрешение
должно назвать exact reviewed artifact/version, Apps Script target, новую таблицу, flags, recovery dates/IDs,
before-image/abort/rollback и допустимые validation reads. Mart/Unitka/LCD в него автоматически не входят.

WB ADS RESUMABLE RECOVERY IMPLEMENTATION COMPLETE — WAITING FOR OWNER REVIEW
