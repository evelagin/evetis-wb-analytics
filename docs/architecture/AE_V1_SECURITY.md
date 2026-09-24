# Autonomous Engineering v1 — безопасность

Состояние на 2026-09-24. Отчёт об атакующем разборе и результаты S1–S10 —
[AE_V1_SECURITY_COMMISSIONING.md](AE_V1_SECURITY_COMMISSIONING.md).

## 1. Инвариант

**У AE v1 нет полномочий на изменение production.** Граница задаётся правами job'ов GitHub,
ролями идентичностей GCP и правилами федерации Anthropic. Флаги агента и промпт границей не
являются. Агент исполняет произвольный код (`python`, `pytest`), а у кода на раннере GitHub
есть `sudo`: он читает всё, что есть на **своей** машине, включая память процесса раннера.
Поэтому граница — это машина и то, что на ней лежит. Что агент «решит» не делать, значения не
имеет.

## 2. ⚠️ Живая конфигурация WIF нарушает инвариант. Исправление в Git, не применено

Снимок живого провайдера и привязок SA: `quality/autonomy/wif_live_snapshot_2026-09-24.json`.
Проверка `python -m tools.autonomy.wif_check --snapshot …` даёт **24 FAIL из 25**.

| SA | Живая привязка | Кто сегодня получает токен |
|---|---|---|
| `sa-deployer` (`run.developer`, `artifactregistry.writer`) | `attribute.repository/<repo>` | любой workflow репозитория, с любой ветки |
| `sa-terraform-apply` (IAM/BigQuery/Run/Secret Manager admin) | `attribute.repo_ref/<repo>@refs/heads/main` | любой workflow на `main` |
| `sa-terraform-plan` (`roles/viewer`: чтение всех 50 датасетов) | `attribute.repository/<repo>` | любой workflow репозитория |

Job инженера, тестов и даже ревьюера AE исполняется на `main` с `id-token: write`. Значит,
его код может выпустить OIDC-токен с любой audience и получить все три SA. Пока исправление не
применено, `AE_ENABLED` обязан оставаться выключенным.

**Исправление** (`infra/terraform/wif.tf`, `autonomy.tf`). Авторизация идёт по claims, которые
выставляет GitHub, а не содержимое репозитория:

| SA | Claim | Значение |
|---|---|---|
| `sa-deployer` | `workflow_ref` (файл верхнего уровня) | `…/deploy-prod.yml@refs/heads/main`, `…/deploy-shadow.yml@refs/heads/main` |
| `sa-terraform-apply` | `workflow_ref` | `…/infra.yml@refs/heads/main`, `…/scheduler-control.yml@refs/heads/main` |
| `sa-terraform-plan` | вычисленный атрибут `tf_plan_workflow` | `workflow_ref == repository + '/.github/workflows/infra.yml@' + ref` и ref не `refs/heads/ae/*` (plan с feature-веток сохраняется) |
| `sa-ae-reader` | `job_workflow_ref` (файл, где объявлен job) | `autonomy-watch/-gate/-engineer/-test.yml@refs/heads/main`; ревьюера в списке нет |

Почему именно эти claims. `workflow_ref` у job'а переиспользуемого workflow — это файл
**вызывающего**. Поэтому AE-workflow, вызвавший `deploy-prod.yml` как переиспользуемый, deployer
не получит (случай E5). `job_workflow_ref` различает job'ы одного прогона: инженер и ревьюер —
это один `workflow_ref` (`autonomy-run.yml`), но разные `job_workflow_ref`. Сравнение — точное
равенство строки. `extract('{path}@')` не годится: он берёт текст до **первого** «@», и файл
`deploy-prod.yml@x.yml` на любой ветке выдавал бы себя за `deploy-prod.yml` (случай E1). Эту
ошибку содержала первая версия исправления в этом же PR.

Неизменяемость. `workflow_ref` фиксирует путь и ref, но не коммит. Закрепить `workflow_sha`
нельзя: он меняется с каждым коммитом. Целостность держится на том, что `GITHUB_TOKEN` не может
менять `.github/workflows/**` (право `workflows` ему не выдаётся), а ветки кандидатов
исключены (`@refs/heads/main`, `!ae/*`). Защиты ветки `main` на тарифе нет (§6, R-1).

## 3. Классификация границ

`STRUCTURALLY_ENFORCED` — держится правами платформы или машинной границей. `DETECT_ONLY` —
детерминированный код замечает, но не делает невозможным. `PROMPT_ONLY` — держится на
инструкциях модели. `NOT_ENFORCED` — не держится.

| Попытка кандидата или агента | Класс | Чем обеспечено |
|---|---|---|
| Получить привилегированную идентичность GCP | **NOT_ENFORCED сейчас** → STRUCTURALLY_ENFORCED после применения §2 | STS/IAM GCP по claims GitHub; `wif_check --live` |
| Получить токен записи GitHub | STRUCTURALLY_ENFORCED | у недоверенных job'ов `contents: read`; `GH_TOKEN` только на шаге клонирования (через `sudo` читается и он, но он read-only) |
| Изменить `main` | STRUCTURALLY_ENFORCED для недоверенных job'ов; NOT_ENFORCED платформой для доверенных | доверенные job'ы исполняют только код `main` и пушат явным refspec; защиты ветки на тарифе нет (R-1) |
| Изменить `.github/workflows/**` | STRUCTURALLY_ENFORCED | `GITHUB_TOKEN` не пушит workflow-файлы; путь в TCB |
| Изменить Terraform, IAM или инструменты развёртывания | STRUCTURALLY_ENFORCED для публикации AE; DETECT_ONLY для слияния | TCB → `HUMAN_DECISION_REQUIRED`, публикатор отказывает; слияние — только человек |
| Изменить поведение гейткипера или публикатора | STRUCTURALLY_ENFORCED | доверенные job'ы исполняют `main`, а не кандидата; правка — TCB |
| Изменить генерацию доказательств в доверенном шаге | STRUCTURALLY_ENFORCED (исправлено, F-3) | анализ влияния — из доверенного checkout; тест воспроизводит атаку |
| Подделать собственные доказательства тестов | **NOT_ENFORCED** → DETECT_ONLY | код кандидата с `sudo` в job'е тестов; обязательный CI тоже исполняет кандидата; рубеж — ревью и человек (R-3) |
| Подделать доверенное состояние | STRUCTURALLY_ENFORCED | `autonomy-state` пишут только job'ы с `contents: write`, исполняющие `main` |
| Подделать вердикт ревьюера | STRUCTURALLY_ENFORCED | sha256 из outputs job'а ревьюера; outputs job'а задаёт только он сам |
| Подменить артефакт после хеширования | STRUCTURALLY_ENFORCED | сверка sha256 обязательна; без `--expect` отказ (исправлено, F-4) |
| Отравить `autonomy-state` целью с чужим кодом | STRUCTURALLY_ENFORCED (исправлено, F-5) | `repository_sha` обязан быть предком `origin/main` |
| Злоупотребить переиспользуемыми workflows / `workflow_call` | STRUCTURALLY_ENFORCED после §2 | вызвать их может только workflow репозитория (добавить — только владелец); привилегии по `workflow_ref` вызывающего |
| Злоупотребить `workflow_dispatch` | STRUCTURALLY_ENFORCED для недоверенных; код для доверенных | `actions: write` только у publish, persist, watch; список диспатча — `policy.json`, имя проверяется регуляркой (R-2) |
| Злоупотребить claims OIDC | STRUCTURALLY_ENFORCED после §2 | claims подписывает GitHub; кандидат не меняет workflow-файлы; ветки `ae/*` исключены |
| Злоупотребить `GITHUB_TOKEN` | STRUCTURALLY_ENFORCED | у недоверенных — чтение; PR от него запускает CI через явный диспатч (S8) |
| Отравить артефакты | STRUCTURALLY_ENFORCED | имена уникальны в прогоне, sha256 из outputs производителя — возможен только отказ в обслуживании |
| Отравить кэш Actions | STRUCTURALLY_ENFORCED отсутствием кэша | через `sudo` недоверенный job достаёт runtime-токен и пишет в кэш области `main`; привилегированные и обязательные workflows pip-кэш не читают (исправлено, F-7; тест) |
| Сохранить учётные данные в git-конфигурации | STRUCTURALLY_ENFORCED | `persist-credentials: false`, токен убирается из `.git/config`; окружение агента без credential helper |
| Изменить скрипт, который потом запустит человек с правами | DETECT_ONLY | известные классы — TCB (развёртывание, откат, `conftest.py`, зависимости, Dockerfile, Makefile, инструкции агентам); любой слитый `tools/*.py`, запущенный владельцем, — вектор; рубеж — ревью человека (R-4) |
| Выполнить DDL/DML в BigQuery | STRUCTURALLY_ENFORCED после §2 | у `sa-ae-reader` нет ролей записи; `ReadOnlyBigQuery` — дополнительный DETECT_ONLY |
| Изменить WB/Ozon | STRUCTURALLY_ENFORCED | в AE нет ни секретов, ни доступа к Secret Manager |
| Выйти за рабочий каталог или ограничения инструментов | NOT_ENFORCED как граница | `--restricted`/`allowedTools` — гигиена: агент исполняет Python. Граница — машина и права job'а |
| Вынести прочитанные данные в сеть | NOT_ENFORCED | у раннера есть интернет (R-5) |
| Потратить деньги идентичностью Claude | STRUCTURALLY_ENFORCED лимитом workspace | бюджеты CLI обходятся кодом с `sudo`; предел — лимит расходов workspace `evetis-ae` (R-6) |
| Склонить ревьюера промпт-инъекцией в диффе | PROMPT_ONLY → смягчено | ревьюер может только ужесточить итог; ложный PASS не снимает ни одного детерминированного запрета |

## 4. Доверенная вычислительная база (TCB)

Определение: `quality/autonomy/policy.json` → `trusted_computing_base.classes`.

| Класс | Пути |
|---|---|
| CI и доставка | `.github/**` |
| сам AE | `tools/autonomy/**`, `quality/autonomy/**` (политика, схемы, федерация, снимок WIF) |
| ворота и доказательства | `quality/**`, `tools/lib/**` (включая `bq_readonly.py`), `run_data_checks.py`, `verify_task.py`, `validate_current_sql.py`, `verify_current_sql_live.py`, `check_runtime_access.py`, `impact_analysis.py`, `architecture_baseline.py`, `build_contract_registry.py`, их тесты, `tools/requirements-sql-ci.txt` |
| IAM и инфраструктура | `infra/**`, `**/*.tf`, `quality/runtime_identities.json` |
| развёртывание и откат | `tools/*deploy*`, `tools/*rollback*`, канонический рендер, `tools/apply_*`, `tools/metabase_*`, `tools/clasp_*`, `**/*rollback*`, `**/rollback/**`, `**/deploy/**`, `**/Dockerfile*`, `cloudbuild*`, `metabase/**` |
| запись во внешний мир | `apps-script/**`, `appsscript.json`, `services/wb-communications/**` |
| автоисполняемое и цепочка поставок | `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.*`, `tox.ini`, `sitecustomize.py`, `*.pth`, `Makefile`, `requirements*.txt`, `package*.json`, `.npmrc`, `.pre-commit-config.yaml`, `.envrc`, `.vscode/`, `.devcontainer/`, `.gitattributes`, `.gitmodules` |
| инструкции агентам | `.claude/**`, `CLAUDE.md`, `AGENTS.md`, `skills/**` |

Кандидат, затронувший TCB, получает **минимум `HUMAN_DECISION_REQUIRED`** и паркуется в
`WAITING_FOR_HUMAN`. Проверка стоит в трёх местах: в плане (до реализации), в фактическом диффе
до ACK плана и в публикаторе. ACK плана (`owner_ack.plan_sha256`) её не снимает, PASS ревьюера
тоже. Переименование считается изменением обоих путей (`--no-renames`). Секретный материал
(`*.tfvars`, `*.pem`, `.env*`, ключи SA) — `UNSAFE`. Защищённые файлы ворот и признаки их
ослабления остаются `UNSAFE`, как и раньше. Тесты: `tools/tests/test_autonomy_tcb.py`.

## 5. Матрица идентичностей

| Роль | GitHub | GCP | Claude API |
|---|---|---|---|
| Watcher (`autonomy-watch`) | `contents: write` → только `autonomy-state`, `actions: write`, `issues: write` | `sa-ae-reader` | — |
| prepare / ingest / verify / gate (`autonomy-gate`) | `contents: write` → только `autonomy-state` | `sa-ae-reader` (prepare: доказательства базы и аудит) | — |
| Engineer (`autonomy-engineer`) | `contents: read` | `sa-ae-reader` | `evetis-ae-engineer` |
| Test (`autonomy-test`, код кандидата) | `contents: read` | `sa-ae-reader` | **нет** (правило не пропускает) |
| Reviewer (`autonomy-review`) | `contents: read` | **нет** (шага auth нет, привязка не пропускает) | `evetis-ae-reviewer` — другой аккаунт и правило |
| Publisher (`publish`) | `contents: write` → только `ae/*` (refspec в коде), `pull-requests: write` → только draft, `actions: write` → только диспатч обязательных workflows | — | — |
| ci-verify | `contents: write` → только `autonomy-state`, `actions: read` | — | — |
| persist | `contents: write` → только `autonomy-state`, `actions: write` → следующий проход, `issues: write` | — | — |

`sa-ae-reader`: `bigquery.dataViewer` на восьми датасетах ворот, `bigquery.jobUser`,
`bigquery.resourceViewer` (журнал заданий для аудита), `logging.viewer`. Ролей записи нет, доступа
к Secret Manager нет.

Ограничение тарифа. Репозиторий приватный на Free: защита ветки и rulesets недоступны (HTTP 403),
у environments нет правил защиты. Поэтому «запись только в `ae/*`» и «только draft» обеспечивает
детерминированный код доверенного job'а. Платформа ограничить `GITHUB_TOKEN` веткой не может.

## 6. Аутентификация

**GitHub → GCP.** Пул `github-pool`, условие провайдера `assertion.repository == <repo>`, привязки
по §2. После применения проверка `python -m tools.autonomy.wif_check --live` должна давать
код 0: это та же модель, что в тестах, но на живом провайдере.

**GitHub → Claude API.** Anthropic WIF, спецификация — `quality/autonomy/anthropic_federation.json`:

- issuer `https://token.actions.githubusercontent.com`, `check_jti = true`: повторный обмен одного
  JWT отвергается;
- workspace `evetis-ae` с лимитом расходов;
- два сервисных аккаунта (`evetis-ae-engineer`, `evetis-ae-reviewer`) и два правила. Условия:
  точный `sub` (`repo:<repo>:ref:refs/heads/main`), audience `https://api.anthropic.com`, claims
  `repository`, `repository_owner`, `ref`, `event_name = workflow_dispatch`,
  `runner_environment = github-hosted`, `workflow_ref = …/autonomy-run.yml@main`,
  `job_workflow_ref = …/autonomy-engineer.yml@main` (у ревьюера — `autonomy-review.yml`);
- `oauth_scope = workspace:developer`, `token_lifetime_seconds = 600`. Итоговый срок не больше
  удвоенного остатка жизни JWT GitHub (около 5 минут), а файл токена обновляется каждые 240 с.

Правило клиент выбирает по ID, а ID лежат в переменных репозитория, то есть не секретны. Поэтому
job инженера может назвать ID правила ревьюера, но условие `job_workflow_ref` его не пропустит.
Job тестов, где исполняется кандидат, не проходит ни одно правило. Статический ключ перекрыл бы
федерацию (он выше по порядку разрешения SDK), поэтому job'ы инженера и ревьюера падают, если
заданы `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_PROFILE` или
`CLAUDE_CODE_OAUTH_TOKEN`. Секретов в workflows AE нет вообще. Тесты:
`tools/tests/test_autonomy_anthropic_federation.py`.

## 7. Обязательная проверка автономного PR (S8)

PR, созданный `GITHUB_TOKEN`, не запускает `pull_request`-CI. Исключения GitHub для этого
токена — только `workflow_dispatch` и `repository_dispatch`. Решение не требует нового секрета:

1. `publish` после draft PR выполняет `workflow_dispatch` для `sql-current.yml` и `ci.yml`
   (`policy.json → required_verification`) на ветке кандидата. У этих workflows только
   `contents: read`, нет секретов и OIDC, и они не читают pip-кэш.
2. `ci-verify` (`actions: read`) решает по API Actions. Прогон засчитывается, только если совпадают
   `head_sha` опубликованного коммита, ветка кандидата, событие `workflow_dispatch`, путь файла и
   время не раньше диспатча. Статусы коммита не читаются вовсе: выставить их проще, чем запуск.
3. `READY_FOR_PR` — это вердикт до публикации. Дальше идёт `AWAITING_VERIFICATION`, и
   `READY_FOR_HUMAN_REVIEW` бывает только при success **всех** обязательных workflows. Провал
   даёт `BLOCKED` (`BLOCKED_BY_TEST`), таймаут в 45 минут — `BLOCKED` (`INCONCLUSIVE`).

Тесты: `tools/tests/test_autonomy_verification.py`.

## 8. Остаточные риски

| # | Риск | Почему не закрыт | Что держит |
|---|---|---|---|
| R-1 | Доверенный job с `contents: write` технически может запушить в `main` | защиты ветки на тарифе нет | в доверенных job'ах нет кода агента или кандидата; действия закреплены по SHA; зависимости с `--require-hashes` |
| R-2 | `actions: write` позволяет запустить любой workflow, в том числе `deploy-prod` и `infra` apply | GitHub не ограничивает диспатч файлом; environments без правил | право только у трёх детерминированных job'ов; цели диспатча из кода и политики |
| R-3 | Злонамеренный кандидат подделывает свои тесты и обязательный CI | его код исполняется там же, где проверяется | TCB, детектор ослабления ворот, независимый ревьюер, слияние только человеком |
| R-4 | Слитый код, который владелец запустит со своими правами | после слияния он становится доверенным | TCB для известных классов, ревью человеком |
| R-5 | Вынос прочитанных данных production в сеть | у раннера есть интернет | чтение ограничено восемью датасетами; VPC-SC вне объёма V1 |
| R-6 | Расход идентичностью инженера сверх бюджета прогона | бюджет CLI обходится кодом с `sudo` | лимит расходов workspace Anthropic (решение владельца) |
| R-7 | Промпт-инъекция через данные (сообщения проверок) и дифф | модели читают внешний текст | права задаёт job, а не текст; ревьюер только ужесточает |
