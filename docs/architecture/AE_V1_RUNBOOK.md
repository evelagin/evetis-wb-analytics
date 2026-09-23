# Autonomous Engineering v1 — runbook

Все шаги ниже выполняет **владелец**. Агент их не выполняет: это IAM, Terraform, настройки
репозитория и консоль Anthropic.

## 1. Состояние после слияния PR

Workflows AE лежат в `main`, но ничего не делают: `autonomy-watch` пропускается, пока нет
переменной `AE_READER_SA`; `autonomy-run` не начинается без `AE_ENABLED=true`. Ни одной
production-мутации слияние не вызывает.

## 2. Включение

### 2.1 Закрыть широкие привязки привилегированных SA — фаза A (добавить)

Изменение привязки `terraform_apply` применяется тем же workflow, который этой привязкой
пользуется. Чтобы исключить блокировку, сначала **только добавляются** новые привязки;
старые остаются, пока новые не проверены.

`infra.yml` → `workflow_dispatch`, `action=plan`, `targets`:

```text
google_iam_workload_identity_pool_provider.github,google_service_account_iam_member.deployer_wif["deploy-prod.yml"],google_service_account_iam_member.deployer_wif["deploy-shadow.yml"],google_service_account_iam_member.terraform_apply_wif["infra.yml"],google_service_account_iam_member.terraform_apply_wif["scheduler-control.yml"]
```

Ожидаемый план: провайдер `~ update in-place` (два новых атрибута маппинга), **4 to add,
0 to destroy**. Если план предлагает destroy — остановиться. Затем то же с `action=apply`.

### 2.2 Проверить новые привязки

1. `deploy-shadow` (`workflow_dispatch`) — авторизация `deployer` проходит.
2. `infra.yml`, `action=plan` без целей — авторизация `terraform_plan` проходит.
3. Политика SA — только JSON, `value(bindings.role)` отбрасывает условия:

```bash
gcloud iam service-accounts get-iam-policy "$(gh variable get DEPLOYER_SA)" --format=json
```

В `members` должны быть `attribute.workflow_path/…/deploy-prod.yml` и `…/deploy-shadow.yml`.

### 2.3 Фаза B — убрать широкие привязки

`infra.yml`, `action=plan`, `targets=google_service_account_iam_member.deployer_wif,google_service_account_iam_member.terraform_apply_wif`.
Ожидаемо: **0 to add, 2 to destroy** — старые экземпляры без ключа. Затем `apply`.

**Если после фазы B `infra.yml` не может получить токен** — вернуть привязку вручную (владелец
проекта имеет право):

```bash
gcloud iam service-accounts add-iam-policy-binding "$(gh variable get TERRAFORM_APPLY_SA)" --role=roles/iam.workloadIdentityUser --member="principalSet://iam.googleapis.com/projects/$(gh variable get GCP_PROJECT_NUMBER)/locations/global/workloadIdentityPools/github-pool/attribute.repo_ref/evelagin/evetis-wb-analytics@refs/heads/main"
```

### 2.4 Создать `sa-ae-reader`

`infra.yml`, `targets`:

```text
google_service_account.ae_reader,google_project_iam_member.ae_reader,google_bigquery_dataset_iam_member.ae_reader_read,google_service_account_iam_member.ae_reader_wif
```

Ожидаемо: **14 to add** (SA, 3 проектные роли, 8 датасетов, 2 WIF), 0 to change, 0 to destroy.
Затем переменная репозитория `AE_READER_SA` = email созданного SA.

### 2.5 Claude API без ключа

В Claude Console: два сервисных аккаунта (инженер, ревьюер), issuer GitHub Actions, два правила
федерации по `AE_V1_SECURITY.md` §6, лимит расходов рабочего пространства. Переменные
репозитория: `AE_ANTHROPIC_ORGANIZATION_ID`, `AE_ANTHROPIC_FEDERATION_RULE_ID`,
`AE_ANTHROPIC_SERVICE_ACCOUNT_ID`, `AE_ANTHROPIC_REVIEWER_FEDERATION_RULE_ID`,
`AE_ANTHROPIC_REVIEWER_SERVICE_ACCOUNT_ID`. Секрет `ANTHROPIC_API_KEY` не создавать.

### 2.6 Наблюдение без действий

`autonomy-watch` → `workflow_dispatch`, `allow_dispatch=false`. Ожидаемо: отчёт с
`llm_invocations: 0`, создана ветка `autonomy-state`. Несколько дней наблюдать сигналы.

### 2.7 Включить

`AE_ENABLED=true`. С этого момента наблюдатель по расписанию диспатчит инженера при
устойчивом новом падении.

## 3. Канарейка UBR-011 (первый реальный прогон; НЕ запущена)

Фикстура: `quality/autonomy/examples/objective.ubr011.canary.json` (`execute: false`).

1. Скопировать фикстуру в ветку `autonomy-state` как `objectives/obj-ubr-011-canary.json`,
   заменив `repository_sha` на актуальный `origin/main` и `execute` на `true`.
2. `python -m tools.autonomy.cli validate --kind objective objectives/obj-ubr-011-canary.json`.
3. `gh workflow run autonomy-run.yml --ref main -f objective_path=objectives/obj-ubr-011-canary.json`.
4. Ожидаемый исход — любой из честных: draft PR, если найдено исправление, не меняющее смысл
   условия; или `WAITING_FOR_HUMAN`, если любое исправление меняет утверждённый смысл
   (UBR-011 `why_not_automatable`). Неприемлемый исход — зелёный PR с ослабленной проверкой:
   его должен остановить детектор ослабления ворот (UNSAFE).

## 4. Остановка и откат

| Ситуация | Действие |
|---|---|
| Остановить всё немедленно | `AE_ENABLED` ≠ `true`; активный прогон — `gh run cancel <id>` |
| Остановить наблюдателя | удалить `AE_READER_SA` |
| Убрать AE целиком | revert PR; targeted destroy ресурсов §2.4 |
| Вернуть широкие привязки | revert изменений `wif.tf` и targeted apply — **только осознанно**: это возвращает риск §2 документа безопасности |
| Прогон застрял в BLOCKED/WAITING | прочитать issue; решение — закрыть прогон (перевод в COMPLETED вручную в ветке `autonomy-state`) или ACK плана (`owner_ack.plan_sha256`) и `autonomy-run -f run_id=…` |

## 5. Проверка локально (без production)

```bash
python -m pytest -q tools/tests/test_autonomy_acceptance.py tools/tests/test_autonomy_ci_trust.py tools/tests/test_autonomy_security.py tools/tests/test_autonomy_units.py
```
