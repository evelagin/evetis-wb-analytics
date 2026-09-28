# AE v1 — учётные данные публикатора: least-privilege дизайн (2026-09-28)

Статус: **дизайн, ничего не создано**. App, ключи, секреты и настройки репозитория не менялись.

## Проблема

Публикатор (job `publish` в `autonomy-run.yml`) обязан открыть **draft PR** из проверенной ветки кандидата
`ae/*` в `main`. `GITHUB_TOKEN` этого сделать не может: настройка репозитория
«Allow GitHub Actions to create and approve pull requests» выключена (`can_approve_pull_request_reviews=false`).

Требования: только репозиторий `evelagin/evetis-wb-analytics`; только создание draft PR и чтение нужных
метаданных; не approve, не merge, не менять настройки, не администрировать Actions/secrets/environments,
не обходить защиту веток.

## Сравнение

| | A. `GITHUB_TOKEN` + настройка репозитория | B. GitHub App (installation token) | C. Fine-grained PAT владельца |
|---|---|---|---|
| Идентичность | `github-actions[bot]` | собственный `<app>[bot]` | **человек** (владелец) |
| Срок жизни | до конца job'а | 1 ч, выпускается на каждый прогон | до 366 дней, долгоживущий |
| Область | весь репозиторий | только выбранный репозиторий; токен можно дополнительно сузить по правам | выбранный репозиторий |
| Права для PR | `pull-requests: write` + настройка, которая включает **и создание, и approve** для ВСЕХ job'ов репозитория, объявивших `pull-requests: write` | `pull_requests: write`, `contents: read`, `metadata: read`; approve собственного PR невозможен (автор = App) | `pull_requests: write` и т.д. |
| Merge | нет (если нет `contents: write` у job) | **нет** — `contents: write` не выдаётся | нет, если не выдан |
| Секрет | не нужен | private key App (долгоживущий) | сам токен (долгоживущий) |
| Отзыв | снять галочку | отозвать ключ / удалить установку, мгновенно | отозвать токен |
| Аудит | события от `github-actions[bot]` — неотличимы от прочих workflow | все PR/комментарии от `<app>[bot]` — отдельная идентичность | действия неотличимы от владельца |
| Широта изменения | репозиторная настройка на все workflow | только новый job-шаг, использующий токен | — |

**C отвергнут**: учётные данные человека в автоматизации, долгий срок жизни, неотличимость от владельца.
**A** проще всего, но расширяет права для всех workflow с `pull-requests: write` сразу (создание *и* approve).
**Рекомендация — B**: отдельная бот-идентичность, токен на 1 час только на этот репозиторий, без `contents: write`
(то есть без push и merge), без администрирования.

## Спецификация B

**App** (создаёт владелец вручную: Settings → Developer settings → GitHub Apps → New GitHub App):

- Name: `evetis-ae-publisher` (любое), Homepage: URL репозитория.
- **Webhook: выключен** (`Active` снят) — App ничего не принимает.
- Repository permissions: **Pull requests: Read and write**, **Contents: Read-only**, **Metadata: Read-only** (обязательное).
  Всё остальное — **No access** (Actions, Administration, Secrets, Environments, Workflows, Checks, Issues…).
- Organization/Account permissions: нет. Events: нет.
- Where can this app be installed: **Only on this account**.
- Установка: Install → **Only select repositories** → `evelagin/evetis-wb-analytics`.

**Ключ**: Generate a private key (PEM) → сохранить в **environment secret** `AE_PUBLISHER_APP_KEY` окружения
`ae-publisher` с deployment branch policy «только `main`»; локальную копию ключа удалить.
App ID → repository variable `AE_PUBLISHER_APP_ID`. (Если окружения с ограничением веток недоступны на тарифе —
repository secret; тогда граница «только main» держится кодом job'а, как сейчас для `autonomy-run`.)

**Получение токена в Actions** (будущий PR по отдельному ACK, только job `publish`):

```yaml
- id: app-token
  uses: actions/create-github-app-token@<pinned-sha>
  with:
    app-id: ${{ vars.AE_PUBLISHER_APP_ID }}
    private-key: ${{ secrets.AE_PUBLISHER_APP_KEY }}
    owner: evelagin
    repositories: evetis-wb-analytics
    permission-pull-requests: write
    permission-contents: read
```

Токен передаётся **только** в вызов `gh pr create --draft` / `gh pr list` (`GH_TOKEN` для одного шага); push ветки
`ae/*` по-прежнему делает `GITHUB_TOKEN` с `contents: write` под контролем `publisher.py`. Недоверенные job'ы
(engineer/test/review) ни ключа, ни токена не получают. Токен истекает через час; action отзывает его в post-step.

Побочный эффект: PR от App-токена **запускает** `pull_request`-workflows (в отличие от `GITHUB_TOKEN`). ci-verify
учитывает только прогоны `workflow_dispatch` по опубликованному SHA — поведение проверки не меняется.

**Ротация/отзыв**: новый ключ → обновить секрет → удалить старый ключ в настройках App (раз в 90 дней или при
подозрении). Аварийно: Suspend/Uninstall App — все токены недействительны сразу.

**Аудируемость**: каждый PR/комментарий от `evetis-ae-publisher[bot]`; выдачи токенов видны в журнале App;
ci-verify уже проверяет `author`/draft/base/head PR.

**Модель угроз**: утечка ключа даёт создание/изменение PR и комментариев, запрос ревью и **approve чужих PR**
(branch protection на тарифе недоступна — одобрение ничего не гейтит); нельзя push, merge, менять настройки,
секреты, workflows. Утёкший токен также может снять draft (`ready for review`) и сменить base PR — ci-verify и так проверяет draft/base/head перед `READY_FOR_HUMAN_REVIEW`. Смягчение: секрет окружения только для `main`, короткий токен, отдельная идентичность, отзыв.

**Если окружения с ограничением веток недоступны на тарифе** (вероятно — branch protection тоже недоступна): repository secret допустим только потому, что `GITHUB_TOKEN` не может менять файлы workflow, а `ci.yml`/`sql-current.yml` секретов не используют; публикатор обязан отвергать кандидата, затрагивающего `.github/` (политика `forbidden_paths` это уже запрещает — проверить при внедрении). `gh pr create` с App-токеном — без флагов `@me` (App не пользователь).

## Ручные действия владельца (после ACK на дизайн)

1. Создать App по спецификации выше и установить только на `evetis-wb-analytics`.
2. Сгенерировать private key, положить в секрет (`AE_PUBLISHER_APP_KEY`), App ID — в переменную.
3. Дать ACK на PR, добавляющий шаг `create-github-app-token` в job `publish` (изменение TCB — отдельное ревью).
