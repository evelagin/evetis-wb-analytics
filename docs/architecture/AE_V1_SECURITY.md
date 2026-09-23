# Autonomous Engineering v1 — безопасность

## 1. Инвариант

**У AE v1 нет полномочий на изменение production.** Это обеспечивается структурно: правами
job'ов, ролями идентичностей и флагами запуска агента, а не промптом. Фраза агента «я не буду
менять production» границей безопасности не является.

## 2. ⚠️ Находка: до применения WIF-изменений инвариант НЕ держится

Разбор WIF-привязок проекта (`infra/terraform/wif.tf`, 2026-09-23):

| SA | Было | Кто мог выпустить токен |
|---|---|---|
| `deployer` | `attribute.repository/<repo>` | **любой workflow репозитория, с любой ветки** |
| `terraform_apply` (админ) | `attribute.repo_ref/<repo>@refs/heads/main` | **любой workflow на main** |
| `terraform_plan` | `attribute.repository/<repo>` | любой workflow (роль `roles/viewer` — только чтение) |

Пока workflows пишет только владелец, это терпимо. AE добавляет субъект, исполняющий
**произвольный код** (тесты агента, тесты кандидата) внутри job'а с `id-token: write` на `main`.
Внутри job'а процессы одного пользователя читают `/proc/<pid>/environ` друг друга
(docs.github.com, about-github-hosted-runners), значит, код агента может достать
`ACTIONS_ID_TOKEN_REQUEST_TOKEN`, выпустить OIDC-токен и обменять его на `deployer` или
`terraform_apply`. Это путь к развёртыванию и к изменению IAM.

**Исправление подготовлено в Git и НЕ применено** (`wif.tf`): добавлены атрибуты
`workflow_ref` и `workflow_path` (`extract('{path}@')`), и привилегированные SA привязаны к
своим файлам:

- `deployer` → только `deploy-prod.yml`, `deploy-shadow.yml` (любая ветка — сохраняет
  практику деплоя из feature-веток);
- `terraform_apply` → только `infra.yml`, `scheduler-control.yml` и только на `main`.

**Пока это не применено, `AE_ENABLED` обязан оставаться выключенным.** Тест
`test_privileged_service_accounts_are_pinned_to_their_workflow_files` охраняет Git-состояние;
production-состояние проверяется после apply (`AE_V1_RUNBOOK.md` §2.4).

## 3. Где гарантия определена

| Среда | Гарантия |
|---|---|
| GitHub-hosted раннер (CI) | **структурная**: у агента read-only `GITHUB_TOKEN`, read-only SA, нет секретов, нет учётных данных владельца на машине |
| Машина владельца (локально) | **снижение риска, не гарантия**: окружение агента очищено (нет gcloud-конфигурации, git без credential helper, gh без токена), но код теста может прочитать файлы под `$HOME` по абсолютному пути. Локально — только синтетические песочницы и доверенные цели |

Поэтому зависимости от MacBook нет: AE исполняется целиком в Actions, а локальный режим —
инструмент разработки самого AE.

## 4. Модель угроз

| Угроза | Защита |
|---|---|
| Агент пытается записать в main | нет токена записи у агента; публикатор пушит только явным refspec `HEAD:refs/heads/ae/*`, проверяет вердикт сам; тест `test_every_git_push_targets_only_autonomy_state` |
| Агент правит workflow, чтобы получить права | `.github/**` в `forbidden_paths` → UNSAFE; `GITHUB_TOKEN` не может пушить workflow-файлы; идентичности привязаны к `workflow_ref@refs/heads/main` |
| Агент правит скрипт развёртывания, который потом запустит владелец | `tools/*deploy*.py`, `capture_canonical_sql.py`, `scale1_predeploy_render.py` в `forbidden_paths` → UNSAFE: у агента прав на DDL нет, но у владельца, запускающего скрипт, — есть; это путь через цепочку поставки |
| Агент ослабляет ворота | детектор в диффе (удалённые `@check`/`ASSERT`/тесты, skip, xfail, known_failing, override, `gate:false`) + защищённые файлы ворот → UNSAFE, без права переопределения ревьюером |
| Агент подделывает состояние или вердикт | авторитетное состояние пишут только доверенные jobs; недоверенный вывод — с sha256 из outputs; подложенные файлы не читаются |
| Код кандидата подделывает свои тесты | не устраняется в V1 (§5 архитектуры); защита — независимое ревью + слияние человеком |
| Промпт-инъекция из issues, логов, веба, данных | вход агента — только структурированная цель; всё внешнее объявлено данными; `--restricted` игнорирует настройки репозитория; внешнее содержимое не расширяет права — права задаёт job, а не текст |
| Утечка токена Claude API | федерация без ключа; токен одноразовый и короткоживущий; `ANTHROPIC_IDENTITY_TOKEN_FILE` вне рабочего каталога; ущерб ограничен стоимостью → лимит расходов рабочего пространства Anthropic |
| Отравление кэша Actions | привилегированные workflows (`deploy-*`, `infra`, `scheduler-control`) кэш не используют (проверено 2026-09-23) |
| Зацикливание агентов | бюджеты итераций, ревью, времени и проходов; `next_pass` не перезапускает упавший job |
| Повтор после FAILED | cooldown 24 ч; семантический отказ не повторяется автоматически |

## 5. Матрица прав

| Субъект | GitHub | GCP | Claude API |
|---|---|---|---|
| Engineer | `contents: read` | `sa-ae-reader` (read-only) | сервисный аккаунт инженера (WIF) |
| Test (код кандидата) | `contents: read` | `sa-ae-reader` | — |
| Reviewer | `contents: read` | **нет** | отдельный сервисный аккаунт ревьюера (WIF) |
| prepare / ingest / verify / gate | `contents: write` → только `autonomy-state` | `sa-ae-reader` (prepare) | — |
| publish | `contents: write` → только `ae/*`, `pull-requests: write` → только draft | — | — |
| persist | `contents: write` → только `autonomy-state`, `actions: write`, `issues: write` | — | — |
| watch | `contents: write` → только `autonomy-state`, `actions: write`, `issues: write` | `sa-ae-reader` | — |

`sa-ae-reader` (`infra/terraform/autonomy.tf`, не применено): `roles/bigquery.dataViewer` на
восьми датасетах ворот, `roles/bigquery.jobUser`, `roles/bigquery.resourceViewer` (журнал заданий
для аудита), `roles/logging.viewer`. Ни одной роли записи: DDL/DML требует
`tables.create`/`updateData`, которых нет, — мутация падает на стороне BigQuery.

## 6. Аутентификация

- **GitHub → GCP**: существующий пул `github-pool`; `sa-ae-reader` привязан к
  `attribute.workflow_ref` = `…/autonomy-watch.yml@refs/heads/main` и `…/autonomy-run.yml@refs/heads/main`.
  Workflow, изменённый в любой другой ветке, токена не получит.
- **GitHub → Claude API**: Anthropic WIF. В Claude Console владелец создаёт два сервисных
  аккаунта (инженер, ревьюер) и два правила федерации: issuer
  `https://token.actions.githubusercontent.com`, audience `https://api.anthropic.com`,
  claims `repository = evelagin/evetis-wb-analytics`, `ref = refs/heads/main`,
  `workflow_ref = …/autonomy-run.yml@refs/heads/main`, `job_workflow_ref = …/autonomy-engineer.yml@refs/heads/main`
  (для ревьюера — `autonomy-review.yml`). Идентификаторы — переменные репозитория
  (`AE_ANTHROPIC_*`), не секреты. Секрета `ANTHROPIC_API_KEY` быть не должно: он перекрыл бы
  федерацию — workflow проверяет это и падает.
- **Долгоживущих учётных данных в AE нет.** Секретов в workflows AE нет вообще
  (`test_no_secrets_and_no_static_anthropic_key`).

## 7. Доказательства в тестах

`tools/tests/test_autonomy_security.py` (51 проверка): права каждого job'а, отсутствие опасных
триггеров и секретов, отсутствие команд мутации production, запись только в `autonomy-state`,
выключатель, закреплённая версия агента, роли `sa-ae-reader`, привязка привилегированных SA к
файлам, флаги агента (`--restricted`, `dontAsk`, без `bypassPermissions`), очистка окружения,
отсутствие аутентификации gcloud/git/gh внутри окружения агента (живые вызовы), отказ
read-only клиента BigQuery от DDL/DML **до** отправки запроса, отказ политики с правом на
production, отсутствие вызова модели в детерминированных модулях.
