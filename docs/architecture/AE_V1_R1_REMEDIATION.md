# Autonomous Engineering — AE-R1: доработка перед вводом в эксплуатацию

Статус: **код в репозитории; ничего не развёрнуто и не включено.** `AE_ENABLED` и
`AE_PREFLIGHT_ENABLED` не заданы, расписаний нет, федерация Anthropic, IAM, GitHub App и F-18
не тронуты. Основание: аудит повторного входа 2026-10-08 (`AE_REQUIRES_REMEDIATION`) и owner ACK
AE-R1 с решениями B1–B7.

## 1. Схема

```
evetis_health.V_DATA_HEALTH_CURRENT ─┐
wb_ops.OPS_INCIDENT (DRO_PIPELINE) ──┼─► signals.py ─► classifier.py ─► watcher ─► objective (task_class, scope)
evetis_health.V_RUN_FAILURE_LEDGER ──┘     (только SELECT,    (детерминированный,     │
                                            без текста ошибок)  fail closed)           ▼
   prepare(Д) → engineer(Н) → ingest(Д) → test(Н, sa-ae-reader) ┐
                                          retest(Н, БЕЗ учётных данных) ┴► verify(Д: сверка) → review(Н)
   → gate(Д: область, лимит диффа, retest, вердикт) → audit(Д) → publish(Д, GitHub App — B2) → STOP
```

Д — доверенный детерминированный job (код main); Н — недоверенный (модель или код кандидата).
Максимальный автоматический исход — **draft PR**. Слияния, деплоя и записи в облако нет нигде.

## 2. Наблюдатель на канонических сигналах

Наблюдатель здоровье **не вычисляет** (`tools/autonomy/signals.py`). Он читает три источника, все
только `SELECT` от `sa-ae-reader`:

| Источник | Что берётся |
|---|---|
| `evetis_health.V_DATA_HEALTH_CURRENT` | pipeline, serving/data/run status, severity, reason_code, data_class, recoverability, evaluated_at, возраст детектора, открытый инцидент |
| `wb_ops.OPS_INCIDENT` (`scope = 'DRO_PIPELINE'`, OPEN) | id инцидента, первая фиксация |
| `evetis_health.V_RUN_FAILURE_LEDGER` (§3) | отказы прогонов: код, сигнатура, отпечаток, повторяемость, восстановление |

Отказ любого источника, пустой снимок или весь снимок старше `signals.max_detector_age_minutes`
(90) → сигнал `infra_blocked` → класс INFRA_BLOCKED (это **не** здоровье). Конвейеры с
`evaluation_mode <> 'EVALUATED'` пропускаются, кроме `DETECTOR_STALE` / `DETECTOR_NEVER_RAN`.

Старый путь (прогон `run_data_checks.py` + `V_OPS_CURRENT_HEALTH`) остался только для диагностики
владельцем (`watch --live --legacy-checks`). Задачи по нему не создаются: устойчивое падение
проверки без канонической классификации — UNCLASSIFIED. Исключение — синтетические стенды тестов.

**Доступ (B7, отдельные ворота).** У `sa-ae-reader` сейчас нет чтения `evetis_health`. До
применения B7 живой наблюдатель честно возвращает INFRA_BLOCKED по `dro_health` и
`run_failure_ledger`. Представление-журнал — стандартное, читает `wb_raw.*`. Чтение `wb_raw` у
`sa-ae-reader` уже есть (`infra/terraform/autonomy.tf`, `ae_read_datasets`).

## 3. Журнал отказов `evetis_health.V_RUN_FAILURE_LEDGER`

Файл `sql/health/dro1_07_run_failure_ledger.sql` пока только в репозитории. Развёртывание — это
DDL в production, оно проходит отдельными воротами. Откат: `DROP VIEW` первой строкой
`dro1_99_rollback.sql`.

- **Зачем.** DRO-1 смотрит на уровне данных. Дефект кода, после которого данные всё же дошли
  (повтор, ручной перезапуск), DRO не видит. Так было 2026-10-07: `unitka_wb` весь день HEALTHY,
  а прогон 09:30 UTC упал на Sheets 503.
- **Источники.**
  - `wb_raw.LOADER_RUNS` — Cloud Run: unitka, ozon-unitka, mart, stocks, prices, funnel и другие;
  - `wb_raw.INGEST_RUNS` — Apps Script;
  - `wb_raw.WB_PRICES_OBSERVATIONS`;
  - `wb_raw.WB_TARIFF_OBSERVATIONS`.

  Окно — 30 суток. Отказы Unitka Engine приходят через `LOADER_RUNS`, поэтому
  `UNITKA_ENGINE_RUNS` не дублируется.
- **Колонки.**
  - `source_log, loader_name, environment, error_code`;
  - `failure_signature` — TRANSIENT_UPSTREAM / AUTH / QUOTA / SCHEMA / PARITY_QA / OTHER, вычисляется из текста ошибки внутри представления;
  - `recorded_as_transient`, `message_fingerprint` — 16 hex от sha256 нормализованного текста;
  - `occurrences_7d/30d, first_seen_at, last_seen_at, last_run_id`;
  - `recovery_status, minutes_to_recovery`.
- 🔴 **Текста ошибки в выводе нет.** Ошибки содержат идентификаторы таблиц, диапазоны листов и
  URL, а сигналы AE публичны (B3).
- **Проверка 2026-10-08 (read-only, отрендеренное тело).** 8 групп за 30 суток. Кейс 503:
  `unitka / SHEETS_API / TRANSIENT_UPSTREAM / recorded_as_transient=false / 78fbf2aedfbacf71 /
  RECOVERED через 81 мин`.

## 4. Детерминированный классификатор (`tools/autonomy/classifier.py`)

Задачу инженеру создают только классы с `engineering=true`. NOT_ENGINEERING и UNCLASSIFIED не
создают её никогда: они идут в оповещение.

**Принцип: fail closed по явным спискам.** Инженерный класс по журналу отказов назначается только
кодам из `policy.json → classifier_codes`. Списки выведены из реальных кодов `cloud/src` и журналов
прогонов на 2026-10-08. Любой другой код — UNCLASSIFIED, даже если он повторяется. Так сделано
после двух независимых ревью: первые версии делали повторяющиеся коды и обобщённые обёртки
LOADER_DEFECT, а коды состояния данных и пустого окна — инженерными дефектами.

| Правило | Условие | Класс |
|---|---|---|
| R8 | журнал не `LOADER_RUNS` (Cloud Run; его код в `cloud/`, внутри allowlist): Apps Script `INGEST_RUNS` — доверенная база | NOT_ENGINEERING (Apps Script) / UNCLASSIFIED |
| R1 | код состояния данных или бизнеса — `business_code_patterns`: `*_EMPTY`, `*_MISSING`, `*_AUTH`, `*_STALE`, `FRESHNESS_GATE`, `*_REQUIRES_ACK`, `*_HELD*`, `*_LOCKED`, `MONTH_SECTION_*`, `RECON_*`, `SPP_*`, `LCD_*`, `PRICE_*`, `COGS_*`, `SOURCE_*`, `INTEGRITY_DATA_*` … | NOT_ENGINEERING |
| R6 | сигнатура AUTH / QUOTA | NOT_ENGINEERING (токен или тариф — решение владельца) |
| R2 | сигнатура временного сбоя, код сам объявил временным | NOT_ENGINEERING |
| R3 | сигнатура временного сбоя, а обёрточный код (`retry_wrapper_codes`: `SHEETS_API`, `LOADER_ERROR`, `ENGINE_ERROR`, `FATAL_UNHANDLED`, `*_HTTP_FAILED` …) записан как детерминированный | **RETRY_CLASSIFIER_DEFECT** |
| R3X | временная сигнатура при коде вне списка обёрток | UNCLASSIFIED |
| R4 | код из `schema_codes` (`WB_*_SHAPE`, `WB_*_BAD_JSON`, `WB_T6_PARSE`) ≥ 3 раз за 7 суток | **SCHEMA_DRIFT** (однократно — UNCLASSIFIED: возможен обрезанный ответ). `BQ_SHAPE` исключён: так называются охранники целостности данных Юнитки |
| R5 | `parity_codes` пуст | **PARITY_DEFECT** автоматически не назначается: `INVARIANT_FAIL` (пустое окно), `FUTURE_LEAKAGE` (ручное состояние листа) — UNCLASSIFIED; класс доступен только цели владельца |
| R7 | код из `loader_defect_codes` (`MART_RUNS_DUP`, `STOCKS_POSTCOUNT_DUP` — проверки собственной записи загрузчика) ≥ 3 раз за 7 суток | **LOADER_DEFECT**. `DUP_KEY` (дубли во вью) и обобщённые обёртки без временной сигнатуры — UNCLASSIFIED |
| H1 | DRO: `data_class` или `source_system` = MANUAL (ФФ, план продаж, ручные операции) | NOT_ENGINEERING |
| H2 | DRO: детектор устарел или не запускался | UNCLASSIFIED: причина может быть операционной (планировщик на паузе — документированный откат DRO-1) |
| H3 | DRO: данных нет или они опаздывают (`SLOT_*`, `FRESHNESS_*`, `DATA_LOSS_*`, `NO_DATA_OBSERVED`, `MISSING_DATES_RECOVERABLE`) | NOT_ENGINEERING |
| H4 | DRO: `RUN_FAILED` / `RUN_PARTIAL` | UNCLASSIFIED — диагноз даёт только журнал отказов с кодом |
| R0 / H0 | ни одно правило | UNCLASSIFIED (fail closed) |

**DETECTOR_DEFECT** автоматически не назначается. Класс доступен только цели владельца с явным
`task_class`. Устаревший детектор даёт одно агрегированное событие `dro:detector:DETECTOR_STALE` и
INFRA_BLOCKED, а не сигнал на каждый конвейер. Конвейеры NOT_EVALUATED сигналами не считаются.

**Повторная постановка.** Сигнал журнала отказов — уже случившийся факт, поэтому задача
создаётся сразу. Но одна и та же строка (тот же `last_seen_at`) порождает задачу один раз
(`already_processed`). Новый отказ — новая задача. Ключ сигнала включает сигнатуру:
`runfail:<loader>:<code>:<signature>:<fingerprint>`.

**Временная сигнатура в SQL** опирается только на однозначные признаки: «status code 5xx»,
«HTTP 5xx», «returned code 5xx» (UrlFetch), «503 Service Unavailable», формулировки Google/BigQuery и сетевые коды. Голое «500» в
тексте («получено 500 диапазонов») временной сигнатурой не считается.

## 5. Область задачи и лимит диффа

`policy.json → task_classes`. Область берётся из политики по классу, а **не** из цели: автор цели
allowlist не расширяет.

| Класс | allowed_paths | строк / файлов | профили тестов |
|---|---|---|---|
| RETRY_CLASSIFIER_DEFECT | `cloud/src/failure.ts`, `cloud/src/errors.ts`, `cloud/src/cli.ts`, `cloud/src/http/*.ts`, `cloud/src/bq/client.ts`, `cloud/src/loaders/*/sheets.ts`, `cloud/src/loaders/*/wbApi.ts`, `cloud/src/loaders/unitka/ozon/requests.ts`, `cloud/test/**/*.test.ts` — классификатор и I/O-клиенты, без экономики | 300 / 6 | python, cloud |
| LOADER_DEFECT | загрузчики приёма `cloud/src/loaders/{stocks,prices,funnel,promo,storage,tariffs}/**`, `cloud/src/bq/*.ts`, `cloud/src/http/*.ts`, тесты и JSON-фикстуры cloud, `pipelines/ozon/runtime|tests` | 400 / 8 | python, cloud, ozon |
| SCHEMA_DRIFT | `normalize.ts` загрузчиков приёма, `wbApi.ts`, `cloud/src/http/*.ts`, тесты и JSON-фикстуры cloud, `pipelines/ozon/runtime|tests` | 400 / 8 | python, cloud, ozon |
| PARITY_DEFECT | `sql/current/**/*.sql`, `sql/mart/**/*.sql`, `sql/dash/**/*.sql`, `tools/tests/test_*.py` — все SQL-пути требуют ACK плана | 300 / 6 | python |
| DETECTOR_DEFECT (только цель владельца) | `sql/health/dro1_01…06`, `tools/dro1_health.py`, `tools/tests/test_dro1_health.py` | 300 / 5 | python |
| COMMISSIONING_CANARY | `tools/tests/test_ae_commissioning_canary.py` | 120 / 1 | python |
| SYNTHETIC_FIXTURE | `synthetic/**`, `tests_synthetic/**` (только при `incident.source = synthetic`) | 200 / 4 | python |

Экономика Юнитки (`cloud/src/loaders/unitka/**`, кроме I/O-клиента `sheets.ts`) и витрины (`cloud/src/loaders/mart/**`) не входят ни в одну область. Глобальный потолок `diff_limits` — 400 строк, 8 файлов. Строки считаются только внутри ханков `@@`. Бинарные изменения и пути вне `[A-Za-z0-9._/-]` запрещены для любого класса. ACK плана дополнительно требуется для `sql/unitka|promo|pricing|ops|health/**`. Поверх области действуют TCB
(HUMAN_DECISION_REQUIRED) и forbidden_paths (UNSAFE). В TCB добавлены конфиги, которые
исполняются инструментами cloud: `cloud/vitest.config.*`, `cloud/.eslintrc*`,
`cloud/eslint.config.*`, `cloud/tsconfig*.json`, `.nvmrc`, `.node-version`, а также `cloud/src/secrets.ts`, `cloud/src/config.ts` и `cloud/src/**/secret*.ts`. ACK плана для
`sql/{current,mart,dash,ref,control_tower}` действует как прежде.

Где проверяется:
1. **План.** Файлы вне области или класс не определён → WAITING_FOR_HUMAN.
2. **Реализация (до тестов).** Дифф вне области, больше лимита или с неразбираемым путём →
   WAITING_FOR_HUMAN: код кандидата вне области не исполняется ни в одном job'е.
3. **Гейткипер.** `SCOPE_OUT_OF_ALLOWLIST` или `DIFF_TOO_LARGE` → HUMAN_DECISION_REQUIRED.
   Это исправимо инженером: при оставшемся бюджете — FIXING. Класса нет → не исправимо.
4. **Публикатор.** Повторная проверка перед любой записью.

## 6. Инженер и тесты cloud/

Профили — `policy.json → test_profiles`:

- **python:** `pytest tools/tests --junitxml`.
- **ozon:** `pytest pipelines/ozon/tests --junitxml`.
- **cloud:**
  - `npm ci --ignore-scripts --no-audit --no-fund` — с сетью, без lifecycle-скриптов;
  - затем без сети: `npm run typecheck`, `npm run lint`, `vitest run --reporter=junit`.

Правила профилей:
- Статус теста = код выхода **и** валидный непустой JUnit по пути, который задаёт харнесс main. Тест, вызвавший `os._exit(0)`, не оставит отчёта и получит FAIL (`JUNIT_MISSING_OR_EMPTY`).
  - Сам отчёт пишет процесс тестов, т.е. код кандидата может его подделать. Это ограничение, а не гарантия.
- Сеть на время тестов отключается. Это подтверждается пробами: соединение наружу не устанавливается, `sudo` внутри не работает, сокет Docker недоступен.
  - Единственный вариант — `sudo unshare -n`, затем `setpriv` с возвратом к пользователю раннера, `--clear-groups`, `--no-new-privs`, `--inh-caps=-all`, `--bounding-set=-all`. Непривилегированный `unshare -r` не годится: в нём запрещён setgroups.
  - Гейткипер требует изоляцию для CI-доказательств: `test_provenance = RECONCILED` при `network_isolation = NOT_ENFORCED` — INCONCLUSIVE.
  - Изоляция **только сетевая и best-effort**: файловая система раннера не изолирована (`isolation_scope` в доказательстве). Код кандидата может оставить файлы, которые исполнят последующие шаги того же job'а.
  - Если отключить нельзя — `network_isolation: NOT_ENFORCED`.
- Детектор ослабления ворот дополнен: `it/describe/test.skip|only|todo|fails`, `xit` / `fit`, `pytest.skip(`, `importorskip`; удаление блоков `it(` / `test(` / `describe(` — UNSAFE.
- Профили выбираются по изменённым файлам и по `test_profiles` класса.

Инженер получает в промпте область задачи. Его `summary`, `root_cause`, `uncertainty`,
`questions_for_owner` и `commands_run` сохраняются только отпечатком (§9).

## 7. Независимый retest и доверенная сверка

Job `retest` в `autonomy-test.yml` работает без учётных данных: нет id-token (значит, нет
`sa-ae-reader` и федерации Claude), нет токена записи. Он прогоняет те же профили на базовом
коммите и на кандидате, харнесс — код main. Если исполненных тестов (`tests − skipped`) у
кандидата меньше, чем у базы, это FAIL с причиной `TESTS_DISAPPEARED`. Рост числа пропущенных тестов — FAIL `TESTS_SKIPPED_GREW`.

Каждый job выгружает ровно один файл (`evidence.json` / `retest.json`). Гейт скачивает retest в
отдельный каталог и берёт из него только `retest.json`. Из недоверенных документов в состояние
попадают только известные ключи, и только в маскированном виде.

Доверенный шаг `verify` (`ReplayEvidenceRunner` → `reconcile`) проверяет sha256 обоих
артефактов и решает так:
- тесты берутся **из retest**;
- расхождение с job'ом `test` → `evidence_disagreement` → UNSAFE;
- retest нет → `test_provenance = UNTRUSTED_ONLY` → INCONCLUSIVE.

Остаточный риск (назван прямо): злонамеренный код теста исполняется и в retest, так что
доказательство остаётся «двумя независимыми исполнениями с харнессом main», а не формальным.
Следующие рубежи: независимый ревьюер, обязательные `ci.yml` и `sql-current.yml` на опубликованном
SHA и слияние только человеком. Требование владельца «доверенный код независимо проверяет результаты»
выполнено частично: независим исполнитель и харнесс, но не сам отчёт теста. Нестабильный тест, давший
разные статусы в двух job'ах, ведёт к UNSAFE (fail closed, шумно).

## 8. Независимый ревьюер

- **Вход.** Отчёт инженера ревьюер больше не получает (его заявление доказательством не
  является). Вход: цель, область задачи, дифф, анализ влияния, доказательства, включая retest.
- **Вердикты:**
  - `APPROVE` — без находок;
  - `APPROVE_WITH_NITS` — только MINOR;
  - `CHANGES_REQUIRED`;
  - `BLOCK` → гейт `BLOCKED_BY_REVIEW`, не исправимо;
  - `UNPROVEN` → INCONCLUSIVE;
  - `HUMAN_DECISION_REQUIRED`.

  Устаревшие `PASS` → APPROVE, `BLOCKED` → UNPROVEN: нормализуются, схема их принимает.
- **Обязательное поле `test_verification`** (`VERIFIED` / `INSUFFICIENT` / `NOT_APPLICABLE`,
  `relevant_tests`, `basis`). Гейткипер не считает одобрением:
  - отсутствие поля;
  - `INSUFFICIENT`;
  - `NOT_APPLICABLE` при изменении кода;
  - ссылки на тесты, которых нет ни в кандидате, ни в дереве базы.

  Одобрение с находками BLOCKER/MAJOR трактуется как требование изменений.
- **Изоляция.** Отдельный job, отдельное правило федерации, своя песочница. Изменить кандидата
  ревьюер не может: изменение рабочего дерева → UNPROVEN.

## 9. Политика вывода данных (B3, `tools/autonomy/output_policy.py`)

Пока репозиторий публичный, публичным считается всё, что AE пишет:
- `autonomy-state`;
- артефакты Actions;
- issues;
- job summary;
- тела PR.

| Что | Как публикуется |
|---|---|
| Свободный текст инженера (агент с доступом к BigQuery) | только отпечаток `[sha256:<16 hex>;chars:N]`; хеш плана для ACK — по этой форме |
| Вывод CLI инженера в диагностике (`stdout_tail`) | отпечаток |
| Ошибки CLI, итоги, `scratch_reason`, тексты ревьюера (вход ревьюера уже публичен) | маскирование `mask_data` + обрезка |
| Хвосты вывода тестов | только идентификаторы упавших тестов и отпечаток |
| Причины переходов в состоянии | `mask_data` |
| Сигналы наблюдателя, инцидент, цель | только перечисления, счётчики, идентификаторы, отпечатки; текста ошибки нет вообще |
| Итог (issue / тело PR) | метаданные; `ensure_public` — остаток данных = отказ записи |
| **Код кандидата (дифф)** | доверенный ingest проверяет добавленные строки: e-mail, телефоны, URL с параметрами, строки BigQuery и деньги — во всех файлах; длинные числа, строки из трёх и более «реальных» чисел и проценты — в фикстурах, не-кодовых файлах и в тестовом коде (`cloud/test`, `tools/tests`, `pipelines/ozon/tests`). Бинарные изменения запрещены. Совпадение → WAITING_FOR_HUMAN, патч не сохраняется. Публикатор проверяет то же повторно |

`mask_data` заменяет:
- денежные суммы (₽, руб, RUB);
- проценты;
- самостоятельные числа от 6 цифр (nm_id, номера заказов);
- e-mail и телефоны;
- URL с параметрами;
- строки BigQuery REST;
- табличные строки.

Редакция секретов (`redact.py`) остаётся отдельным обязательным слоем.

**Ограничение (принято осознанно).** Текст плана инженера хранится только отпечатком. Владелец,
дающий ACK плана (`sql/current|mart|dash|ref|control_tower`), не может прочитать план в
публичном состоянии. До приватного хранилища (B3) такие цели требуют локального воспроизведения
плана владельцем. ACK привязан к sha256 публичной формы плана.

## 10. Публикатор (B2)

`GitPublisher(identity=…)`. Учётные данные записи даёт только идентичность.

- **`github-app` — по умолчанию.** Выделенный GitHub App с ключом в Cloud KMS через WIF job'а
  publish. Пока App не создан, он отказывает до любой записи (`B2: …`).
- **`actions-token` — только стенды и dry-run.** Широкое право «Actions создаёт PR» владелец не
  включает.

Публикатор открывает только draft PR. Approve, merge и ready он не вызывает никогда.

## 11. Внешние блокеры (не в AE-R1)

| | Блокер | Ворота |
|---|---|---|
| B1 | правило федерации Anthropic → `workspace:inference`; исключение `workspace:developer` истекает 2026-10-09 | действие владельца в Console, затем сверка `anthropic_scope` |
| B2 | GitHub App + ключ KMS + WIF job'а publish | отдельные ворота (дизайн — `AE_V1_PUBLISHER_APP_DESIGN.md`) |
| B4 | `sa-ae-auditor` для аудита уровня организации; `audit.py` сейчас требует режим LIVE от идентичности AE — правка кода в тех же воротах | отдельные ворота |
| B5 | F-18 (`allUsers` на `evetis-wb-communications`) | отдельные ворота |
| B6 | лимит расходов workspace Anthropic | действие владельца |
| B7 | `roles/bigquery.dataViewer` на `evetis_health` для `sa-ae-reader` (Terraform) + DDL `V_RUN_FAILURE_LEDGER` | отдельные ворота |

## 12. Независимое ревью

Свежее ревью отдельным агентом по `b35b958...d2c481f` вернуло CHANGES_REQUIRED: 3 HIGH, 5 MEDIUM,
6 LOW. Исправлено в этом же PR:

| Находка | Исправление |
|---|---|
| H1 — бизнесовые коды классифицировались как инженерные | явные списки кодов; голое «500» не временная сигнатура |
| H2 — `error_code`, NULL в `ARRAY_AGG` | `* EXCEPT`, `IGNORE NULLS`, `SAFE_OFFSET`; представление повторно выполнено read-only на живых данных |
| H3 — данные в коде кандидата | скан диффа и запрет бинарных изменений |
| M1 — побег из сетевой изоляции | флаги `setpriv` и проба sudo |
| M2 — недоверенные доказательства в публичном состоянии | allowlist ключей, однофайловые артефакты, отдельный каталог retest |
| M3 — ослабление тестов | новые шаблоны; учёт skipped; формулировка про JUnit исправлена |
| M4 — устаревший детектор как множество задач | агрегирование, UNCLASSIFIED |
| M5 — повторная постановка | дедупликация по `last_seen_at`, сигнатура в ключе |
| L1 | `diff_size` только внутри ханков |
| L2 | секреты и конфиг cloud — в TCB |
| L3 | гейткипер fail closed без контекста |
| L4 | идентификаторы vitest |
| L6 | `test_verification` не обязателен в схеме, но без него одобрения нет |
| L5 | оставлен как названное ограничение (§9) |

Второе свежее ревью по `b35b958...865e7d0` вернуло CHANGES_REQUIRED. Обходов гейткипера до
READY_FOR_PR не найдено, но выявлены ещё 1 HIGH и 4 MEDIUM. Исправлено:

| Находка | Исправление |
|---|---|
| HIGH-1 — обёртки, `INVARIANT_FAIL`, Apps Script и обрезанные ответы давали инженерные задачи | только `LOADER_RUNS`; обёртки — только как RETRY; без `INVARIANT_FAIL`; схема — только при повторяемости; UrlFetch «returned code» |
| M-2 — путь с пробелом обходил скан данных | неразбираемый путь — находка, его строки проверяются строже всего; `PATH_NOT_ALLOWED` |
| M-3 — выход через сокет Docker | `--clear-groups`, проба сокета Docker; изоляция названа best-effort |
| M-4 — код вне области исполнялся в тестах | область проверяется до TESTING |
| M-5 — экономический SQL без ACK | allowlist PARITY сужен; ACK для `sql/unitka|promo|pricing|ops|health` |
| L-6 — ключ восстановления в журнале | полный ключ группы; `REUSED` — успех |
| L-8 — ослабление тестов | `it.fails`, удаление блоков тестов |
| L-7 | названо ограничением (§7) |

Третье свежее ревью по `b35b958...044b2d4` вернуло CHANGES_REQUIRED. Обходов гейткипера и
публикатора не найдено, целевой кейс 503 подтверждён, найдены 3 MEDIUM и 3 LOW. Исправлено:

| Находка | Исправление |
|---|---|
| M-1 — `FUTURE_LEAKAGE`, `BQ_SHAPE`, `DUP_KEY` | исключены; PARITY_DEFECT автоматически не назначается |
| M-2 — экономика загрузчиков в allowlist без ACK | области сужены до классификатора, I/O-клиентов и загрузчиков приёма |
| M-3 — данные в тестовом коде | тестовый код сканируется как данные |
| L-1 | `concurrent/sequential/each.skip`, `it['skip']`, рост пропусков — FAIL |
| L-2 | путь `unshare -r` удалён; CI-доказательства без изоляции — INCONCLUSIVE |
| L-3 | `forbidden_paths` проверяется раньше скана данных: UNSAFE не понижается до решения человека |

Дорожная карта ввода в эксплуатацию — AE-C0 (shadow) … AE-C5, по отдельным ACK. Первый реальный
кейс: Sheets 503 (фикстура `quality/autonomy/examples/signals.sheets_503_2026-10-07.json`).
