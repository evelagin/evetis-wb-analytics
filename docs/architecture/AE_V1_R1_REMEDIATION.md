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
после независимого ревью: первая версия делала любой повторяющийся код LOADER_DEFECT и
классифицировала коды состояния данных как дефекты.

| Правило | Условие | Класс |
|---|---|---|
| R1 | код состояния данных или бизнеса — `business_code_patterns`: `*_EMPTY`, `*_MISSING`, `*_AUTH`, `*_STALE`, `FRESHNESS_GATE`, `*_REQUIRES_ACK`, `*_HELD*`, `*_LOCKED`, `MONTH_SECTION_*`, `RECON_*`, `SPP_*`, `LCD_*`, `PRICE_*`, `COGS_*`, `SOURCE_*`, `INTEGRITY_DATA_*` … | NOT_ENGINEERING |
| R6 | сигнатура AUTH / QUOTA | NOT_ENGINEERING (токен или тариф — решение владельца) |
| R2 | сигнатура временного сбоя, код сам объявил временным | NOT_ENGINEERING |
| R3 | сигнатура временного сбоя, а обёрточный код (`retry_wrapper_codes`: `SHEETS_API`, `LOADER_ERROR`, `ENGINE_ERROR`, `FATAL_UNHANDLED`, `*_HTTP_FAILED` …) записан как детерминированный | **RETRY_CLASSIFIER_DEFECT** |
| R3X | временная сигнатура при коде вне списка обёрток | UNCLASSIFIED |
| R4 | код из `schema_codes` (`*_SHAPE`, `*_BAD_JSON`, `WB_T6_PARSE`, `*_SCHEMA_MISSING`, `BQ_SHAPE`) | **SCHEMA_DRIFT** |
| R5 | код из `parity_codes` (`INVARIANT_FAIL`, `POST_COMMIT_QA_FAILED`, `FUTURE_LEAKAGE`) ≥ 3 раз за 7 суток | **PARITY_DEFECT** (однократно — UNCLASSIFIED) |
| R7 | код из `loader_defect_codes` (`LOADER_ERROR`, `MART_ERROR`, `DUP_KEY`, `*_DUP`, `MANIFEST_FINALIZE_FAILED` …) ≥ 3 раз за 7 суток | **LOADER_DEFECT** (однократно — UNCLASSIFIED) |
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
«HTTP 5xx», «503 Service Unavailable», формулировки Google/BigQuery и сетевые коды. Голое «500» в
тексте («получено 500 диапазонов») временной сигнатурой не считается.

## 5. Область задачи и лимит диффа

`policy.json → task_classes`. Область берётся из политики по классу, а **не** из цели: автор цели
allowlist не расширяет.

| Класс | allowed_paths | строк / файлов | профили тестов |
|---|---|---|---|
| RETRY_CLASSIFIER_DEFECT | `cloud/src/failure.ts`, `cloud/src/errors.ts`, `cloud/src/cli.ts`, `cloud/src/loaders/**/*.ts`, `cloud/test/**/*.test.ts` | 300 / 6 | python, cloud |
| LOADER_DEFECT | `cloud/src/**/*.ts`, `cloud/test/**/*.test.ts`, `cloud/test/fixtures/**/*.json`, `pipelines/ozon/runtime/**/*.py`, `pipelines/ozon/tests/**/*.py` | 400 / 8 | python, cloud, ozon |
| SCHEMA_DRIFT | загрузчики/нормализация cloud и ozon + их тесты, JSON-фикстуры | 400 / 8 | python, cloud, ozon |
| PARITY_DEFECT | `sql/**/*.sql`, `tools/tests/test_*.py` | 300 / 6 | python |
| DETECTOR_DEFECT (только цель владельца) | `sql/health/dro1_01…06`, `tools/dro1_health.py`, `tools/tests/test_dro1_health.py` | 300 / 5 | python |
| COMMISSIONING_CANARY | `tools/tests/test_ae_commissioning_canary.py` | 120 / 1 | python |
| SYNTHETIC_FIXTURE | `synthetic/**`, `tests_synthetic/**` (только при `incident.source = synthetic`) | 200 / 4 | python |

Глобальный потолок `diff_limits` — 400 строк, 8 файлов. Строки считаются только внутри ханков `@@`. Бинарные изменения запрещены для любого класса. Поверх области действуют TCB
(HUMAN_DECISION_REQUIRED) и forbidden_paths (UNSAFE). В TCB добавлены конфиги, которые
исполняются инструментами cloud: `cloud/vitest.config.*`, `cloud/.eslintrc*`,
`cloud/eslint.config.*`, `cloud/tsconfig*.json`, `.nvmrc`, `.node-version`, а также `cloud/src/secrets.ts`, `cloud/src/config.ts` и `cloud/src/**/secret*.ts`. ACK плана для
`sql/{current,mart,dash,ref,control_tower}` действует как прежде.

Где проверяется:
1. **План.** Файлы вне области или класс не определён → WAITING_FOR_HUMAN.
2. **Гейткипер.** `SCOPE_OUT_OF_ALLOWLIST` или `DIFF_TOO_LARGE` → HUMAN_DECISION_REQUIRED.
   Это исправимо инженером: при оставшемся бюджете — FIXING. Класса нет → не исправимо.
3. **Публикатор.** Повторная проверка перед любой записью.

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
- Сеть на время тестов отключается, и это подтверждается двумя пробами: соединение наружу не устанавливается, и `sudo` внутри не работает.
  - Варианты: `unshare -rn` или `sudo unshare -n`, затем `setpriv` с возвратом к пользователю раннера, `--no-new-privs`, `--inh-caps=-all`, `--bounding-set=-all`.
  - Если отключить нельзя — `network_isolation: NOT_ENFORCED`.
- Детектор ослабления ворот дополнен: `it/describe/test.skip|only|todo`, `xit` / `fit`, `pytest.skip(`, `importorskip`.
- Профили выбираются по изменённым файлам и по `test_profiles` класса.

Инженер получает в промпте область задачи. Его `summary`, `root_cause`, `uncertainty`,
`questions_for_owner` и `commands_run` сохраняются только отпечатком (§9).

## 7. Независимый retest и доверенная сверка

Job `retest` в `autonomy-test.yml` работает без учётных данных: нет id-token (значит, нет
`sa-ae-reader` и федерации Claude), нет токена записи. Он прогоняет те же профили на базовом
коммите и на кандидате, харнесс — код main. Если исполненных тестов (`tests − skipped`) у
кандидата меньше, чем у базы, это FAIL с причиной `TESTS_DISAPPEARED`.

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
SHA и слияние только человеком.

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
| **Код кандидата (дифф)** | доверенный ingest проверяет добавленные строки: e-mail, телефоны, URL с параметрами, строки BigQuery и деньги — во всех файлах; длинные числа, табличные строки и проценты — в фикстурах и не-кодовых файлах. Бинарные изменения запрещены. Совпадение → WAITING_FOR_HUMAN, патч не сохраняется. Публикатор проверяет то же повторно |

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

Дорожная карта ввода в эксплуатацию — AE-C0 (shadow) … AE-C5, по отдельным ACK. Первый реальный
кейс: Sheets 503 (фикстура `quality/autonomy/examples/signals.sheets_503_2026-10-07.json`).
