# ADR-07. Платформа исполнения Autonomous Engineering v1

Дата решения: 2026-09-23 · Статус: **принято для реализации; включение — решение владельца**

## Решение

AE v1 исполняется на **GitHub Actions (GitHub-hosted, эфемерные раннеры) + Claude Code CLI
в headless-режиме + существующий инструментарий Platform Baseline (BigQuery read-only)**.
Постоянной ВМ, self-hosted раннера и OpenHands нет. Гипотеза задания подтверждена.

## Почему

1. **Изоляция даётся платформой, а не кодом.** Каждый job GitHub-hosted раннера — новая ВМ,
   уничтожаемая после job'а. Это позволяет разнести автора, исполнение кода кандидата,
   ревьюера и решающие шаги по разным машинам без собственной инфраструктуры.
2. **Короткоживущая идентичность без секретов.** И GCP, и Claude API принимают OIDC-токен
   GitHub (Workload Identity Federation). В AE нет ни одного долгоживущего ключа.
3. **Долговременное состояние — Git.** Ветка `autonomy-state` + артефакты Actions. Перезапуск
   не зависит от памяти модели и от живого процесса.
4. **Узкое место проекта — доказуемость, а не пропускная способность** (ADR-04). Сервер
   агентов не добавляет доказательств; он добавляет поверхность атаки и эксплуатацию.

## Почему CLI, а не `claude-code-action` как движок

`anthropics/claude-code-action@v1` — обёртка одного запуска с GitHub-сантехникой
(комментарии, ветки). Оркестратору AE нужны несколько ролей с долговременным состоянием
между ними, а запись в Git — только у детерминированного публикатора. Поэтому движок —
тот же CLI, который обёртывает действие, вызываемый оркестратором; из действия взяты
его механизмы: федерация идентичности (те же переменные `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`, `ANTHROPIC_IDENTITY_TOKEN_FILE`)
и обновление одноразового OIDC-токена каждые 4 минуты. Граница миграции на действие или
на другой рантайм — протокол `AgentAdapter` (`tools/autonomy/agents.py`).

## Результаты исследования (источники проверены 2026-09-23)

| # | Тема | Установлено | Источник |
|---|---|---|---|
| 1 | Claude Code Action v1 | GA; режим выбирается автоматически: с `prompt` — automation, без — `@claude`; опции CLI через `claude_args`; действие проверяет права актёра, но `workflow_dispatch`/`schedule` не проверяет отдельно (GitHub сам требует write для dispatch); `claude-code-base-action` проверок актёра и восстановления конфигурации из base не делает | github.com/anthropics/claude-code-action/blob/main/docs/security.md (коммит 2026-08-05) |
| 2 | Automation mode | `claude -p` — один запуск без REPL, код выхода; `--output-format json` | `claude --help`, Claude Code 2.1.251 (локально, 2026-09-23) |
| 3 | Structured outputs | `--json-schema` → поле `structured_output`; в действии — output `structured_output`. **Валидатор CLI не принимает URI черновика 2020-12 в `$schema`** — найдено живым запуском, аннотации снимаются (`ClaudeCliAdapter.cli_schema`) | docs/usage.md действия; живой запуск 2026-09-24 |
| 4 | Permissions / tools | `--tools` сужает сам набор; `--allowedTools`/`--disallowedTools` — правила; `--permission-mode dontAsk` отказывает во всём неразрешённом; **`--restricted`** убирает Bash/WebFetch, если `--tools` их не назвал, игнорирует файлы настроек репозитория, ограничивает файловые инструменты рабочими каталогами, отказывает в `bypassPermissions`; `--max-budget-usd`, `--max-turns`, `--no-session-persistence`, `--strict-mcp-config` | `claude --help`, 2.1.251 |
| 5 | Anthropic WIF | GA 2026-06-17; OIDC GitHub → короткоживущий токен Claude API, сервисные аккаунты Claude Platform с аудитом; в действии — входы `anthropic_federation_rule_id` и др., нужен `id-token: write`; **статический ключ имеет приоритет над федерацией**; OIDC-токен одноразовый (`jti_reused` при повторном обмене) | claude.com/blog/workload-identity-federation; github.com/anthropics/claude-code-action/pull/1344, /1378, /1407; docs/setup.md |
| 6 | GitHub Actions security | события `GITHUB_TOKEN` новых запусков не создают, **кроме `workflow_dispatch` и `repository_dispatch`**; `GITHUB_TOKEN` не может пушить файлы `.github/workflows`; права задаются на job | docs.github.com/en/actions/concepts/security/github_token |
| 7 | Эфемерные раннеры | новая ВМ на каждый job, уничтожается после него (кроме `ubuntu-slim`); внутри job процессы одного пользователя видят `/proc/<pid>/environ` друг друга | docs.github.com/en/actions/using-github-hosted-runners/using-github-hosted-runners/about-github-hosted-runners |
| 8 | GitHub OIDC → GCP WIF | в проекте уже есть пул `github-pool`; claim `workflow_ref` = `owner/repo/.github/workflows/<файл>@<ref>`; в маппинге атрибутов GCP поддержан CEL `extract()`; значения со `/` в `principalSet` допустимы | docs.github.com/en/actions/reference/security/oidc; docs.cloud.google.com/iam/docs/workload-identity-federation |
| 9 | Environments / approvals | репозиторий приватный, владелец — пользователь на Free: **защита ветки и rulesets недоступны (HTTP 403)**; environments `infra`/`production` существуют, но без правил защиты; обязательные ревьюеры на Free для приватных репозиториев недоступны | `gh api …/branches/main/protection` и `…/rulesets` → 403 (2026-09-23); docs.github.com/…/manage-environments |
| 10 | OpenHands Agent Server | часть `OpenHands/software-agent-sdk`; Agent Canvas 1.20.0 (2026-09-17); self-host через Docker/npm или Cloud | github.com/OpenHands/OpenHands releases v1.17.0–v1.20.0 |
| 11 | OpenHands Automation | cron, вебхуки, история прогонов; 1.17 разделила права автоматизаций; 1.18 — только создатель включает обратно; 1.20 — автоматизации под сохранённым профилем с выбранными секретами; **1.17 автоматически исполняет `.openhands/hooks.json` из рабочего каталога** | github.com/OpenHands/docs/issues/785; релиз-ноты 1.17–1.20 |
| 12 | ACP / делегирование | Agent Canvas запускает Claude Code, Codex, Gemini как ACP-агентов; дочерние разговоры для делегирования; 1.18 требует явного решения для каждого ACP-харнесса | README OpenHands; блог openhands.dev 2026-09 |

Факты 1–9 проверены по первичным источникам или локальным вызовом. Сведения о версиях
OpenHands (10–12) — из релиз-нот и вторичных обзоров; для решения V1 их достаточно, для
внедрения в V2 — перепроверить.

## Решение по OpenHands

1. **Что добавил бы OpenHands сверх GitHub Actions + Claude Code?** Постоянные разговоры с
   возобновлением, собственный планировщик и вебхуки, UI наблюдения за прогонами, запуск
   разных агентных рантаймов через ACP, песочницы-контейнеры с управлением ресурсами.
2. **Нужно ли это V1?** Нет. Состояние V1 намеренно живёт в Git, а не в разговоре; планировщик
   и вебхуки даёт Actions; изоляцию — эфемерные ВМ. Постоянный сервер противоречит принципу
   «без постоянной машины» и добавляет компонент с правами к данным.
3. **Что оправдало бы внедрение в V2?** Одновременно: длинные расследования, которые не
   укладываются в лимиты job'а; потребность в нескольких рантаймах с разной ценой; большой
   поток задач, где UI очереди экономит время владельца.
4. **Граница миграции.** Слой доказательств (`gatekeeper`, `policy`, `state`, `watcher`,
   `evidence`, `publisher`) не знает о рантайме. OpenHands подключается как ещё один
   `AgentAdapter` (роль → промпт → рабочий каталог → структурированный ответ). Ни схемы,
   ни ворота, ни машина состояний при этом не меняются. Хуки `.openhands/hooks.json` при
   таком подключении обязаны быть выключены: они исполняют содержимое репозитория.
