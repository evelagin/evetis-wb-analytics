# AE v1 — план M6: ввод в эксплуатацию на синтетической цели

Состояние на 2026-09-26. **Не выполняется без отдельного ACK владельца.** UBR-011, плановый
наблюдатель и реальные инциденты в M6 не участвуют.

## 0. Предусловия (все обязательны)

| # | Условие | Как проверить | Статус на 2026-09-26 |
|---|---|---|---|
| P1 | M1–M3 без дрейфа | `wif_check --live` 25/25; `wif_domains --live` оба домена 35/35; `iam_check --live` PASS; targeted plan M1/M2/M3 — No changes | ✅ |
| P2 | M4a/M4b: `AE_READER_SA` и шесть `AE_ANTHROPIC_*` совпадают с живыми значениями; `AE_ENABLED` нет; секретов нет | `gh variable get …` (сравнение без печати значений), `gh secret list` | ✅ |
| P3 | M5: правила, SA, issuer, `check_jti`, 600 с, workspace `evetis-ae`, неизменяемые ID | аттестация владельца (Admin API федерации отвечает 404 — машинной проверки нет) | ✅ по аттестации |
| P4 | Сторож scope в `main` (этот PR): фактический scope и срок токена проверяются до агента | PR слит, тесты `test_autonomy_anthropic_scope.py` | ⏳ PR |
| P5 | Исключение `workspace:developer` явно принято владельцем, срок до 2026-10-09 | ACK владельца на это исключение | ⏳ владелец |
| P6 | **Лимит расходов workspace `evetis-ae`** задан (Console → Workspaces → evetis-ae → Limits → Spend limits) и уведомление о расходах включено | аттестация владельца с суммой | ⏳ владелец (в отчёте M5 не указан) |
| P7 | Ни один workflow AE не запускался, ветки `autonomy-state` нет | `gh run list --workflow=autonomy-*`, `git ls-remote` | ✅ |

## 0.1 Фаза 0 — preflight аутентификации Claude API (ACK владельца 2026-09-25)

Добавлен после попытки №1: сторож ревьюера в обычном прогоне срабатывает только после работы
инженера, а для проверки нужны состояние и `AE_ENABLED`. Preflight проверяет обе идентичности
заранее, без модели, GCP и состояния.

- Запуск: `AE_PREFLIGHT_ENABLED=true` (временно), затем
  `gh workflow run autonomy-run.yml --ref main -f preflight=true`, после — удалить переменную.
- Цепочка та же: `autonomy-run.yml@main → autonomy-{engineer,review}.yml@main`, поэтому claims OIDC
  совпадают с правилами федерации без правок в Console. В reusable-файлах — отдельный job
  `preflight` (вход `mode: preflight` + `AE_PREFLIGHT_ENABLED`); job'ы прохода при этом недостижимы.
- Выключатели независимы: `AE_PREFLIGHT_ENABLED` не открывает ни одного job'а прохода, `AE_ENABLED`
  не открывает preflight, а при `AE_ENABLED=true` preflight не запускается вовсе.
- Job preflight: checkout `main` без сохранения токена, Python (stdlib), затем
  `python -m tools.autonomy.anthropic_scope preflight --role …`. Нет: Claude Code, модели,
  GCP/`sa-ae-reader`, BigQuery, `autonomy-state`, артефактов.
- Проверки: вид ID правила/SA/workspace/организации; у ревьюера правило и SA не инженерские;
  claims OIDC-токена совпадают со спецификацией правила роли (иначе обмена нет); обмен;
  `decide`: `workspace:inference` → PASS, `workspace:developer` → PASS_WITH_EXCEPTION до 2026-10-09,
  прочее (включая `org:admin`, срок > 600 с, отказ обмена) → FAIL.
- Доказательства (лог и итог job'а, через редакцию #194): роль, ЗАПРОШЕННЫЕ ID правила/SA/workspace,
  scope, expires_in, решение, request-id Anthropic, несекретные claims. Ответ обмена ID не
  возвращает, поэтому доказательство идентичности — принятие обмена Anthropic для запрошенных
  rule/SA/workspace; сверка — история аутентификации Console по request-id.
- Диспатчить только при отсутствующем `AE_ENABLED` и без прогона `autonomy-run` в очереди: у группы
  concurrency `autonomy-run` новый запуск вытесняет ожидающий.
- Токен OIDC job'а preflight теоретически обменивается на `sa-ae-reader` (тот же `job_workflow_ref`,
  что у инженера, — это не расширение: инженер уже имеет эту идентичность). В job'е нет шага GCP,
  его код — код `main`; запрет закреплён тестом содержимого job'а.
- Регрессия: `tools/tests/test_autonomy_preflight.py` (перебор всех сочетаний выключателей).

## 1. Bootstrap состояния

1. Создать ветку-сироту `autonomy-state` с одним файлом `objectives/obj-ae-commissioning-001.json`.
   Это копия `quality/autonomy/examples/objective.commissioning.json`, где `execute: true` и
   `repository_sha` = текущий `origin/main` (в нём должен быть `tools/commissioning/ae_canary.py`).
   Проверки: `python -m tools.autonomy.cli validate --kind objective …`; `repository_sha` — предок
   `origin/main`.
2. `gh variable set AE_ENABLED --body true` — только на время прогона.
3. `gh workflow run autonomy-run.yml --ref main -f objective_path=objectives/obj-ae-commissioning-001.json`.
   Наблюдатель **не** запускается.

## 2. Ожидаемый ход (два прохода)

**Проход 1.**
- `prepare` (доверенный, `sa-ae-reader`): доказательства базы, аудит.
- `engineer`: план и реализация — новый файл `tools/tests/test_ae_commissioning_canary.py`.
- `ingest`, `test`, `verify`.
- `review`: протокол ввода — маркера нет, вердикт `CHANGES_REQUIRED`.
- `gate` → `FIXING`, затем `persist` → `next-pass: dispatch`.

**Проход 2.**
- `engineer` (исправление: маркер `AE-COMMISSIONING-ITERATION-OK` в docstring).
- `test`, `verify`, `review` → PASS.
- `gate` → `READY_FOR_PR`.
- `publish`: draft PR в `ae/objective-ae-commissioning-001-<sha8>`, диспатч `sql-current.yml` и `ci.yml`.
- `ci-verify` → `READY_FOR_HUMAN_REVIEW`.
- `persist` → issue с отчётом.

## 3. Идентичности и обмены (что должно и не должно произойти)

| Job | OIDC → GCP | OIDC → Claude API |
|---|---|---|
| prepare / ingest / verify / gate | `sa-ae-reader` (только чтение) | — |
| engineer | `sa-ae-reader` | правило инженера `fdrl_017uB6D6…` → SA `svac_01QHKoVo…`; сторож: `workspace:developer`, 600 с → `PASS_WITH_EXCEPTION` |
| test (код кандидата) | `sa-ae-reader` | **нет**: нет переменных, правило не пропускает `autonomy-test.yml` |
| review | **нет** | правило ревьюера `fdrl_01HCwTPK…` → SA `svac_01BjwPDn…`; сторож — как у инженера |
| publish / ci-verify / persist | — | — |

Никакой job не получает `sa-deployer`, `sa-terraform-*`, `sa-tenant-provisioner`: это проверено
моделью на живой конфигурации обоих доменов.

## 4. Изоляция

- Инженер работает в своей машине и песочнице (`git worktree` от базового SHA), с урезанным окружением агента.
  Токена записи GitHub у него нет. Вывод уходит артефактом, sha256 — через outputs.
- Ревьюер: другая машина, своя песочница с кандидатом, инструменты только Read/Grep/Glob, GCP нет.
  Видит дифф и заявление автора, но не его рассуждения.
- Код кандидата исполняется только в job'ах `engineer` и `test` (оба read-only). В доверенных
  job'ах он не исполняется (F-3).

## 5. Передача артефактов и детерминированное воспроизведение

Для каждого артефакта (`engineer_plan.json`, `engineer_implement.json`, `engineer_implement.patch`,
`evidence.json`, `reviewer.json`) доверенный job сверяет sha256 из outputs job'а-производителя. Без
хеша воспроизведение отказывает (F-4). Патч применяется в песочнице доверенного job'а, и список
файлов пересчитывает git (`--no-renames`). Гейткипер детерминирован, ревьюер может только ужесточить.

## 6. Ожидаемый draft PR

- Ветка `ae/…`, единственный файл `tools/tests/test_ae_commissioning_canary.py`, в нём есть маркер итерации.
- Draft, создан `github-actions[bot]`, тело — отчёт прогона.
- Обязательные workflows по опубликованному SHA: `sql-current.yml` и `ci.yml`, `workflow_dispatch`, success.

## 7. Прерывание (любое из условий — `gh variable delete AE_ENABLED`, `gh run cancel`, M6 FAIL)

- Сторож scope выдал FAIL: `org:admin`, срок больше 600 с или истёкшее исключение.
- Job пытается получить SA вне таблицы §3; у `sa-ae-reader` есть задание BigQuery не-`SELECT`.
- Изменение IAM или WIF в журнале аудита за окно прогона.
- Проходов больше 3, прогон дольше 90 минут, расход сверх лимита (§8).
- Кандидат трогает файлы вне `tools/tests/test_ae_commissioning_canary.py` или TCB.
- Появилась ветка `ae/*`, кроме ожидаемой; есть попытка push в `main`.

## 8. Стоимость

| Ограничитель | Значение | Характер |
|---|---|---|
| лимит расходов workspace `evetis-ae` | задаёт владелец (P6) | структурный (Anthropic) |
| `--max-budget-usd` | инженер $15, ревьюер $5 за вызов | в процессе CLI (обходится кодом с `sudo`) |
| итерации / циклы ревью / проходы | 3 / 3 / 8 | детерминированный (оркестратор, `next-pass`) |
| время | 90 мин на прогон, таймауты job'ов | детерминированный |

Ожидаемо 3 вызова инженера и 2 вызова ревьюера; верхняя граница по бюджетам CLI — около $55.
Рекомендую лимит workspace на время M6 **$75**.

## 9. Доказательства для M6 PASS

1. `python -c "…tools.autonomy.commissioning.assess(…)"` → `PASS` (реальный инженер ≥2 раз, реальный
   ревьюер ≥2 раз на других раннерах, итерация, детерминированный гейт, обязательный CI, финал
   `READY_FOR_HUMAN_REVIEW`, 0 мутаций, область изменений).
2. Итоги job'ов engineer и review: сторож `PASS_WITH_EXCEPTION`, scope `workspace:developer`, `expires_in ≤ 600`.
3. История аутентификации Claude Console за окно (владелец): обмены только по двум правилам, от
   job'ов engineer и review; от `test` — ни одного; отказов `jti_reused` нет.
4. `JOBS_BY_PROJECT` за окно: у `sa-ae-reader` только `SELECT`; журнал аудита IAM и WIF пуст.
5. После прогона: `wif_domains --live`, `wif_check --live`, `iam_check --live` — PASS; Terraform
   M1/M2/M3 — No changes.
6. Draft PR по §6, CI зелёный на опубликованном SHA.
7. Расход workspace за окно не выше лимита.

## 9.1 Диагностика агента (remediation после M6 Phase 1, 2026-09-26)

Попытка Phase 1 (run 36169466331) потеряла причину отказа Claude CLI: артефакт инженера грузился
только при непустых хешах, а ошибка оставалась в черновом состоянии раннера. Теперь:

- каждый вызов агента пишет запись (`tools/autonomy/diagnostics.py`, схема
  `quality/autonomy/agent_diagnostics.schema.json`): стадия и класс отказа, код выхода, тип исключения,
  время, формат вывода, `is_error`/`subtype`/`api_error_status`/тип ошибки API, `duration_api_ms`,
  ходы, usage/стоимость, отредактированные хвосты stdout/stderr, версии Claude Code и Node,
  `pre_invoke`, вывод сторожа scope, ожидаемые и созданные артефакты;
- job инженера/ревьюера ВСЕГДА отдаёт `engineer_diagnostics.json` / `reviewer_diagnostics.json` в хешах
  и выгружает артефакт `if: always()`; код 3 — документированная передача отказа (`::error::` в job'е);
- доверенный ingest/gate: sha256 → размер → схема → роль job'а → run_id → повторный поиск секретов →
  копия в `artifacts/<run>/diagnostics/` → BLOCKED с классом в причине (`[стадия/класс]`, для INFRA —
  `INFRA_FAILURE`). Класс из недоверенного job'а не делает прогон терминальным (FAILED — только по
  transient доверенного адаптера): владелец может возобновить. Подмена — IntegrityError, состояние не
  меняется; плохая диагностика (схема, UTF-8, NaN, секреты, чужой run_id/роль) — отказ без записи;
- отказ редакции — документ REDACTION_BLOCKED без свободного текста;
- отчёт (issue) показывает стадию, класс, код выхода, статус/тип ошибки API и sha диагностики.

Ограничения CLI 2.1.251 (фиксируются в каждом документе): нет request-id Messages API и нет сигнала
об обмене токена самим CLI. «Дошёл ли запрос до Messages API» выводится из `api_error_status`,
`duration_api_ms` и usage; обмен токена CLI — только история аутентификации Console.

`iam_check`: анонимный датасет результатов `_<40 hex>` (OWNER у SA, создаётся BigQuery при первом
SELECT) исключается из «записи» только при доказанных A1–A9 (скрыт, единственный OWNER — SA, нет
настроек, истечение 24 ч, задания SA с назначением в датасет, датасет создан во время задания,
все задания SA — SELECT, регион совпадает). Иначе — прежний FAIL. `_script*` не покрывается.

## 9.2 План следующего M6 (только по отдельному ACK)

Предусловия: этот PR слит; `iam_check --live` PASS с одним распознанным анонимным датасетом;
`wif_check --live`, `wif_domains --live` PASS; Phase 0 preflight повторён (оба PASS_WITH_EXCEPTION).

Ход и остановка — как §1–§8. Дополнительно обязательно сохранить и приложить к отчёту:
1. `engineer_diagnostics.json` и `reviewer_diagnostics.json` каждого прохода (sha из outputs + доверенная
   копия в `autonomy-state`), с версиями CLI/Node и выводом сторожа (request-id);
2. для каждого вызова: стадию, класс, `api_error_status`/тип, `messages_api_reached`,
   `model_response_began`, usage и стоимость;
3. владелец: история аутентификации и логи запросов Console за окно прогона, сопоставленные с
   request-id сторожа и временем вызовов; без этого M6 PASS не засчитывается;
4. `JOBS_BY_PROJECT` за окно (только SELECT у `sa-ae-reader`), журнал IAM/WIF пуст, `iam_check --live` PASS.

## 9.3 Публикация и доверенный аудит (remediation после M6 Phase 1 #3, 2026-09-27)

Прогон `run-20260927T121235Z-ab2e1c53` дошёл до `READY_FOR_PR`; публикация остановилась на
`gh pr create` («GitHub Actions is not permitted to create or approve pull requests»), а приёмка не видела
доказанного нуля мутаций (доверенные replay-шаги писали `audit_status=NOT_APPLICABLE`).

- **Аудит.** `NOT_APPLICABLE` больше не перезаписывает результат; FAIL «липкий»; `audit_evidence` фиксирует
  окно (с `created_at`) и число покрытых вызовов агента. Новый доверенный шаг `audit` (autonomy-gate.yml,
  `sa-ae-reader`, только в `READY_FOR_PR`) между `gate` и `publish`. Публикатор и `commissioning.assess`
  требуют `zero_mutations_proven` (tools/autonomy/audit.py); нет доказательства — нет публикации.
- **Покрытие аудита (источник v4).** Прежний `region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT` с
  `statement_type != 'SELECT'` не видел LOAD/COPY/EXTRACT, SELECT с записью в таблицу и задания вне EU, а его
  задержка не ограничивалась. Теперь PASS — только если чисты все источники: `jobs.list` без location
  (все регионы и типы, `unreachable` — отказ); Admin Activity и Data Access, где AE действует сам ИЛИ стоит в
  начале цепочки делегирования (имперсонация другого SA видна по `serviceAccountDelegationInfo`); маршрутизация
  Data Access — точный фильтр `_Default` по умолчанию, без исключений, хранение дольше окна; самопроверка IAM в
  момент аудита (`testIamPermissions` на проекте, на каждом SA проекта из событий CreateServiceAccount и на SA
  других проектов с ролями здесь, ACL видимых датасетов, `updateData` на КАЖДОЙ существующей таблице проекта —
  перечень по истории создания/удаления в Admin Activity, способ выдачи права не важен); текущие IAM-политики ВСЕХ ресурсов проекта,
  восстановленные по истории SetIamPolicy (публичный/групповой доступ выше чтения, AE на отдельном ресурсе,
  прямой федеративный принципал — отказ); конфигурация WIF, восстановленная по Admin Activity и проверенная тем
  же оценщиком S1, что `wif_check --live` (на 2026-09-27 восстановление совпало с живой конфигурацией, 25/25);
  федеративный участник вне модели S1 (весь пул, другой пул, иная роль, чужой issuer, неизвестный путь
  updateMask) — отказ, и в самом `wif_check` тоже. Уровень организации (наследуемые привязки) аудиту AE не
  виден — его доказывает `iam_check --live` владельца (политика организации без федеративных/групповых/
  публичных участников).
  Полнота окна доказана зондом-отметкой в jobs.list и в Data Access (до 12 попыток), выдержка 300 с от
  `READY_FOR_PR`. Нарушение инварианта или сбой источника — BLOCKED; доказательство иной версии источника нулём
  не считается. Действия других WIF-workflow в окне (деплой) — BLOCKED «атрибуция неоднозначна», не FAIL.
  **Следствие на 2026-09-27:** `evetis-wb-communications` доступен `allUsers` (`roles/run.invoker`, F-18) —
  публичную точку входа журналы аудита не видят, поэтому аудит BLOCKED, пока F-18 не закрыта или владелец не
  примет иное решение. Хранение Admin Activity 400 дней: для проекта старше аудит станет BLOCKED (перечни SA,
  таблиц и политик не доказуемы).
- **F-18 — атрибуция публичного входа (источник v5, D-19b).** `allUsers → run.invoker` на
  `evetis-wb-communications` перестаёт быть отказом ТОЛЬКО при полной машинной цепочке за окно: журнал запроса
  Cloud Run (платформа) → `auth_ok` приложения (`wbc-audit/1`) → `mutation_attempt` → `mutation_success|failure`,
  всё по одному trace и одному серверному `request_id`, на ревизиях с `instrumentation_ready`, в интервале запроса,
  на том же экземпляре. Изменение WB — только из webhook после секрета Telegram И allow-list с `result=ok`,
  без `auth_denied` в trace; `/poll` и `/admin` разрешают только вызовы Telegram. SA/ingress сервиса и права
  runtime SA сверяются с ожидаемыми по Admin Activity. Нет цепочки, неоднозначность, `outcome_unknown`, окно без
  инструментирования — BLOCKED; изменение без аутентификации или вне политики — FAIL. IP Telegram и HTTP-код
  доказательством не считаются. **Историческое окно 27.09 (ревизии без инструментирования) этим не доказывается.**
- **Повтор временных ошибок источника**: 429/5xx/сеть — до 4 попыток с паузами 2/4/8 с; исчерпание — BLOCKED.
- **Привязка ci-verify.** `READY_FOR_HUMAN_REVIEW` — только если PR прогона открыт, draft, base `main`, не из
  форка, ветка = ветка кандидата и head = опубликованный SHA (`gh pr view`, `pull-requests: read`); иначе
  BLOCKED (UNSAFE).
- **Публикация.** Идемпотентна: существующая ветка `ae/*` переиспользуется без push, только если её коммит —
  ровно проверенный кандидат (родитель = `repository_sha`, то же дерево); иначе отказ, force-push нет.
  Открытый PR переиспользуется только как draft в `main`. `gh` — только `pr list`, `pr create --draft`,
  `workflow run`.
- **Право создавать PR** — решение владельца (вариант A: настройка репозитория; B: GitHub App).

**Продолжение того же прогона без inference** (после слияния исправления и решения по праву на PR, по
отдельному ACK): временно `AE_ENABLED=true`, затем
`gh workflow run autonomy-run.yml --ref main -f run_id=run-20260927T121235Z-ab2e1c53`.
`prepare` в `READY_FOR_PR` ничего не делает; job'ы инженера и ревьюера выходят на проверке состояния ДО
сторожа и модели; `audit` пишет доверенное доказательство; `publish` переиспользует `d221e72` без изменения,
создаёт draft PR и запускает обязательный CI; `ci-verify` → `READY_FOR_HUMAN_REVIEW`; `persist` → отчёт.

## 10. Уборка и выключение

- `gh variable delete AE_ENABLED` сразу после финального прохода.
- Закрыть draft PR, удалить ветку `ae/…`.
- `autonomy-state`, артефакты Actions (30 дней) и issue остаются как доказательства.
- Исключение scope истекает 2026-10-09. До этой даты либо Console начнёт предлагать `workspace:inference`
  и правила пересоздаются, либо владелец явно продлевает исключение.
