# AE v1 — учётные данные публикатора: least-privilege дизайн (2026-09-28)

Статус: **дизайн, ничего не создано**. App, ключи, секреты, KMS, IAM и настройки репозитория не менялись.
Исправлено 2026-09-28 (ACK финальной доработки PR #202, только документация): убраны варианты хранения ключа,
невозможные или небезопасные при текущем тарифе (приватный репозиторий на GitHub Free).

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
| Секрет | не нужен | private key App — только в Cloud KMS, неэкспортируемый (см. ниже) | сам токен (долгоживущий) |
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

## Хранение ключа App: только Cloud KMS (неэкспортируемый)

**Факт тарифа** (docs.github.com, *Managing environments for deployment*; проверено 2026-09-28): на GitHub Free
environment secrets, deployment branch policies и required reviewers доступны **только публичным** репозиториям.
Репозиторий приватный. Поэтому:

- **environment secret — невозможен** на текущем тарифе;
- **repository secret — неприемлем** как production-дизайн: доступен любому workflow репозитория, граница
  «только main / только job publish» держалась бы лишь кодом job'а. Этот вариант больше не рассматривается;
- включение настройки «Allow GitHub Actions to create and approve pull requests» (вариант A) — не делается.

**Каноническая схема (B + KMS/WIF):**

- PEM, сгенерированный GitHub для App, один раз импортируется в Cloud KMS (`RSA_SIGN_PKCS1_2048_SHA256`,
  import job; защита SOFTWARE или HSM) и уничтожается локально. Экспорт ключа из KMS невозможен.
- Отдельный SA публикатора с `roles/cloudkms.signerVerifier` **только на эту версию ключа**; WIF-привязка
  этого SA — только к `autonomy-run.yml@refs/heads/main`, job `publish` (как у остальных доверенных job'ов,
  оценщик S1 `wif_check`). Недоверенные job'ы (engineer/test/review) ни SA, ни ключа, ни токена не получают.
- Job `publish` строит JWT App (iss = App ID/Client ID, exp ≤ 10 мин), подписывает его через
  `asymmetricSign` KMS, обменивает на **installation token** с правами только `pull_requests: write`,
  `contents: read`, `metadata: read` и только на этот репозиторий; срок жизни — 1 час; токен отзывается
  (`DELETE /installation/token`) в post-step. Перед выпуском — проверка прав установки
  (`GET /repos/{repo}/installation`): шире перечисленных — отказ.
- Токен передаётся только в `gh pr create --draft` / `gh pr list` (`GH_TOKEN` одного шага); push ветки `ae/*`
  по-прежнему делает `GITHUB_TOKEN` под контролем `publisher.py`; у `GITHUB_TOKEN` публикатора снимается
  `pull-requests: write`.

Побочный эффект: PR от App-токена **запускает** `pull_request`-workflows (в отличие от `GITHUB_TOKEN`). ci-verify
учитывает только прогоны `workflow_dispatch` по опубликованному SHA — поведение проверки не меняется.

**Ротация/отзыв**: новый ключ в App → импорт новой версии в KMS → выключение старой версии KMS → удаление
старого ключа в настройках App (раз в 90 дней или при подозрении). Аварийно: Suspend/Uninstall App — все токены
недействительны сразу; выключение версии ключа KMS — новых токенов нет.

**Аудируемость**: каждый PR/комментарий от `evetis-ae-publisher[bot]`; каждая подпись — запись Data Access
`AsymmetricSign` в Cloud Audit Logs (кто, какой job); выдачи токенов видны в журнале App; ci-verify уже
проверяет `author`/draft/base/head PR.

**Модель угроз**: утечка **токена** (≤ 1 ч) даёт создание/изменение PR и комментариев и approve чужих PR
(branch protection на тарифе недоступна — одобрение ничего не гейтит); нельзя push, merge, менять настройки,
секреты, workflows. Может снять draft и сменить base PR — ci-verify проверяет draft/base/head перед
`READY_FOR_HUMAN_REVIEW`. Утечки **ключа** нет по построению (неэкспортируемый); компрометация job'а `publish`
даёт подписи только пока идёт job и только через WIF-привязку к `main`. `gh pr create` с App-токеном — без
флагов `@me` (App не пользователь).

## Ручные действия владельца (каждое — по отдельному ACK; сейчас ничего не делается)

1. Создать App по спецификации выше и установить только на `evetis-wb-analytics`.
2. ACK на Terraform: keyring/ключ KMS, SA публикатора, `cloudkms.signerVerifier` на версию ключа, WIF-привязка
   к job `publish` на `main` (изменение IAM — отдельное ревью, `wif_check` S1 обязан пройти).
3. Импортировать PEM в KMS, локальную копию уничтожить; App ID/Client ID — в repository variable.
4. ACK на PR, который добавляет подпись через KMS и выпуск/отзыв installation token в job `publish`
   (изменение TCB — отдельное ревью).

До этого публикация остаётся BLOCKED (вариант: владелец сам открывает draft PR из опубликованной ветки `ae/*`).
