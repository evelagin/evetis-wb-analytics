# Autonomous Engineering v1 — runbook

Все шаги ниже выполняет **владелец**: IAM, Terraform, настройки репозитория, консоль Anthropic.
Агент их не выполняет. Ни один шаг не включает плановое наблюдение и не запускает UBR-011.

## 1. Состояние после слияния PR #165

Workflows AE лежат в `main`, но ничего не делают:

- у `autonomy-watch` **нет расписания**. Запуск только вручную (`workflow_dispatch`) и только при
  заданной переменной `AE_READER_SA`;
- `autonomy-run` не начинается без `AE_ENABLED=true`;
- `ci.yml` и `sql-current.yml` получили триггер `workflow_dispatch` (S8), а `ci.yml` больше не
  читает pip-кэш.

Слияние не меняет IAM и не вызывает ни одной production-мутации. В `main` появятся
`wif.tf`/`autonomy.tf`, которые **не применены**. Полный `terraform apply` из `main` запрещён
`CLAUDE.md` и без этого; применять только целевыми шагами §2.

## 2. Раскатка: две фазы, по одной мутации

Путь применения — одно из двух:

- **(а) после слияния PR #165:** `infra.yml` → `workflow_dispatch`, `action=plan`, затем `action=apply`
  с тем же `targets`. Apply авторизуется только из `main`;
- **(б) до слияния, с машины владельца:** из рабочей копии ветки `arch/autonomous-engineering-v1`,
  с уже инициализированным backend. Команда:
  `terraform plan -target=… -out=p && terraform show p && terraform apply p`.

Ожидаемые diff ниже сняты read-only `terraform plan -lock=false` 2026-09-24 на живом state.
Если фактический план отличается хоть в одном ресурсе — **остановиться**.

### Фаза 1 — закрыть привилегированный WIF (S1)

> **Выполнено 2026-09-24 по ACK владельца: `WIF_HARDENING_VERIFIED`.** Путь (б), сохранённые планы
> M1 (`1 to change`) и M2 (`5 to add, 3 to destroy`). Проверка:
> `wif_check --live --phase wif-hardening` → 25/25, утечек 0. Доказательства —
> [`ae_evidence/wif_hardening_2026-09-24/`](ae_evidence/wif_hardening_2026-09-24/README.md).
> ⚠️ Пока `wif.tf` из PR #165 не в `main`, не применять из `main` провайдер и `*_wif`: откат
> маппинга лишит deploy-* и infra авторизации.

#### M1. Маппинг атрибутов провайдера (только добавление)

| | |
|---|---|
| Текущее | `google.subject`, `attribute.repository`, `attribute.ref`, `attribute.repo_ref` |
| Желаемое | плюс `attribute.workflow_ref`, `attribute.job_workflow_ref`, `attribute.tf_plan_workflow` |
| targets | `google_iam_workload_identity_pool_provider.github` |
| Ожидаемый diff | `1 to change`: провайдер `~ update in-place`, `+` три атрибута, `4 unchanged elements` |
| Проверка | `gcloud iam workload-identity-pools providers describe github-provider --workload-identity-pool=github-pool --location=global --format=json` → 7 ключей `attributeMapping`; `infra.yml action=plan` с feature-ветки проходит авторизацию (привязки не менялись) |
| Откат | `gcloud iam workload-identity-pools providers update-oidc github-provider --workload-identity-pool=github-pool --location=global --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.repo_ref=assertion.repository + \"@\" + assertion.ref"` |

Почему отдельно. Привязки M2 ссылаются на новые атрибуты. Если поменять их без маппинга,
`infra.yml` и `deploy-*` потеряют авторизацию.

#### M2. Привязки `sa-deployer`, `sa-terraform-apply`, `sa-terraform-plan`

| | |
|---|---|
| Текущее | deployer: `attribute.repository/evelagin/evetis-wb-analytics`; apply: `attribute.repo_ref/evelagin/evetis-wb-analytics@refs/heads/main`; plan: `attribute.repository/evelagin/evetis-wb-analytics` |
| Желаемое | deployer: `attribute.workflow_ref/…/deploy-prod.yml@refs/heads/main` и `…/deploy-shadow.yml@refs/heads/main`; apply: `…/infra.yml@refs/heads/main` и `…/scheduler-control.yml@refs/heads/main`; plan: `attribute.tf_plan_workflow/infra.yml` |
| targets | `google_service_account_iam_member.deployer_wif,google_service_account_iam_member.terraform_apply_wif,google_service_account_iam_member.terraform_plan_wif` |
| Ожидаемый diff | `5 to add, 0 to change, 3 to destroy`: 2 новых deployer, 2 новых apply; plan `must be replaced`; старые `deployer_wif` и `terraform_apply_wif` destroyed «because resource uses count or for_each» |
| Проверка | 1) `python -m tools.autonomy.wif_check --live --phase wif-hardening` → **код 0, 25/25 PASS** (до M3; после M3 — без `--phase`, это и есть S1); 2) `infra.yml action=plan` с feature-ветки — авторизация plan; 3) следующий `deploy-shadow` (push в `main`) — авторизация deployer; 4) JSON-политика SA: `gcloud iam service-accounts get-iam-policy sa-deployer@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com --format=json` — `value(bindings.role)` скрывает условия |
| Откат | `gcloud iam service-accounts add-iam-policy-binding <SA> --role=roles/iam.workloadIdentityUser --member=<старый member>` для трёх строк из «Текущее» (префикс `principalSet://iam.googleapis.com/projects/37074083763/locations/global/workloadIdentityPools/github-pool/`). Откат возвращает риск §2 документа безопасности |

Блокировки себя нет. Job `infra.yml` apply получает токен в начале job'а, а новая привязка его
тоже пропускает (случай A4). Если авторизация всё же пропала, владелец проекта возвращает
привязку командой отката.

### Фаза 2 — идентичности и конфигурация AE

#### M3. `sa-ae-reader` (read-only)

> **Выполнено 2026-09-24 (повторно, исправленный дизайн): `AE_READER_IDENTITY_VERIFIED`.** PR #169 →
> `main` `e5b7bbc`; plan `17 to add`; `iam_check --live` — PASS; строгий `wif_check --live` — 25/25.
> Реальное чтение — `NOT_EXECUTABLE_AT_M3` (первое — в M6). Доказательства —
> [`ae_evidence/ae_reader_m3b_2026-09-24/`](ae_evidence/ae_reader_m3b_2026-09-24/README.md).

> **2026-09-24: первое применение откатано — `AE_READER_IDENTITY_FAILED_ROLLED_BACK`.** По
> эффективным правам `roles/bigquery.jobUser` оказался не только запуском запросов:
> `dataform.repositories.create`, `dataform.folders.create` (Dataform API включён),
> `geminidataanalytics.locations.chat`. WIF-часть работала: строгий `wif_check --live` дал 25/25.
> Дизайн исправлен в Git: пользовательская роль `aeBigQueryJobRunner` (`bigquery.jobs.create`,
> `bigquery.config.get`) вместо `jobUser`. Доказательства —
> [`ae_evidence/ae_reader_m3_2026-09-24/`](ae_evidence/ae_reader_m3_2026-09-24/README.md).

| | |
|---|---|
| Текущее | нет |
| Желаемое | SA, пользовательская роль `aeBigQueryJobRunner` (`bigquery.jobs.create`, `bigquery.config.get`), `bigquery.resourceViewer`, `logging.viewer`, `bigquery.dataViewer` на 8 датасетах, 4 привязки WIF по `job_workflow_ref` (watch, gate, engineer, test — **без** review). **Не** `roles/bigquery.jobUser` |
| targets | `google_service_account.ae_reader,google_project_iam_custom_role.ae_job_runner,google_project_iam_member.ae_reader,google_project_iam_member.ae_reader_job_runner,google_bigquery_dataset_iam_member.ae_reader_read,google_service_account_iam_member.ae_reader_wif` |
| Ожидаемый diff | `17 to add, 0 to change, 0 to destroy` (read-only plan 2026-09-24: `terraform_fixed_design_plan.txt`) |
| Проверка | 1) `python -m tools.autonomy.iam_check --live` → код 0 (эффективные права: ни одного права создания/изменения, кроме `bigquery.jobs.create`; чтение восьми датасетов, `jobs.listAll`, `logEntries.list` есть); 2) `wif_check --live` **без `--phase`** → 25/25: B1/B2 получают только `sa-ae-reader`, C1 — ничего |
| Откат | `terraform destroy` с теми же targets (или удалить SA: `gcloud iam service-accounts delete`, привязки уйдут вместе с ним) |

#### M4. Переменные репозитория

| | |
|---|---|
| Текущее | есть `WIF_PROVIDER`, `GCP_PROJECT_ID`; переменных AE нет |
| Желаемое | `AE_READER_SA=sa-ae-reader@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com` и шесть `AE_ANTHROPIC_*` из `quality/autonomy/anthropic_federation.json → github_variables`. **`AE_ENABLED` не задавать.** Секрет `ANTHROPIC_API_KEY` не создавать |
| Команда | `gh variable set AE_READER_SA --body …` (и так для каждой) |
| Проверка | `gh variable list`; `gh secret list` не содержит `ANTHROPIC*` |
| Откат | `gh variable delete <имя>` |

#### M5. Федерация Anthropic (консоль, роль admin/owner организации)

| | |
|---|---|
| Текущее | нет |
| Желаемое | по `quality/autonomy/anthropic_federation.json`: issuer GitHub Actions (`check_jti` включён), workspace `evetis-ae` с лимитом расходов, аккаунты `evetis-ae-engineer` и `evetis-ae-reviewer`, два правила (claims — точные строки из файла, включая `repository_id` и `repository_owner_id`), `workspace:inference`, 600 с |
| Где | Claude Console → Settings → Workload identity → Connect workload → GitHub Actions; затем «Advanced rule options»: вставить `claims` из файла, `subject_prefix` без `*` |
| Проверка | первый прогон §3: в истории аутентификации два разных правила, у job'а `test` обменов нет |
| Откат | выключить правила или удалить аккаунты в консоли |

#### M6. Ветка состояния

| | |
|---|---|
| Текущее | ветки `autonomy-state` нет |
| Команда | `gh workflow run autonomy-watch.yml --ref main -f allow_dispatch=false` |
| Проверка | отчёт прогона: `llm_invocations: 0`; ветка `autonomy-state` создана; диспатча нет (`AE_ENABLED` не задан) |
| Откат | `git push origin --delete autonomy-state` |

#### M7. Workflows

Слияние PR #165 (§1). Проверка: `autonomy-run` без `AE_ENABLED` пропускает все job'ы. Откат — revert PR.

Рекомендуемый порядок: **M7 → M1 → M2 (S1 живьём) → M3 → M4 → M5 → M6 → §3**. Фазу 1 (M1–M2)
имеет смысл применить независимо от AE: живой провайдер отдаёт `sa-deployer` любому workflow
репозитория.

## 3. Ввод в эксплуатацию (S9): синтетическая цель, не UBR-011

Предусловия: M1–M7 выполнены, `wif_check --live` и `wif_domains --live` дают код 0.

1. Взять `quality/autonomy/examples/objective.commissioning.json`. Задать `repository_sha` = текущий
   `origin/main`: в нём должен лежать `tools/commissioning/ae_canary.py`. Задать `execute: true` и
   положить файл в ветку `autonomy-state` как `objectives/obj-ae-commissioning-001.json`.
2. `gh variable set AE_ENABLED --body true`.
3. `gh workflow run autonomy-run.yml --ref main -f objective_path=objectives/obj-ae-commissioning-001.json`.
   Протокол ревьюера гарантирует итерацию. Первый вердикт — `CHANGES_REQUIRED` (маркера нет),
   затем `persist` диспатчит второй проход, инженер исправляет, ревьюер даёт `PASS`, гейткипер —
   `READY_FOR_PR`, публикатор создаёт draft PR и запускает обязательные workflows, а `ci-verify`
   ставит `READY_FOR_HUMAN_REVIEW`.
4. Сразу после финала: `gh variable delete AE_ENABLED`.
5. Вердикт S9 — детерминированно, `tools/autonomy/commissioning.py`:

   ```bash
   python - <<'PY'
   import json, subprocess
   from tools.autonomy.commissioning import assess
   run = json.load(open("state/runs/<run_id>.json")); art = "state/artifacts/<run_id>"
   jobs = []
   for rid in ["<id прохода 1>", "<id прохода 2>"]:
       jobs += json.loads(subprocess.check_output(["gh", "api", f"repos/evelagin/evetis-wb-analytics/actions/runs/{rid}/jobs", "--jq", ".jobs"]))
   print(assess(run, json.load(open(f"{art}/gate.json")), open(f"{art}/candidate.patch").read(), jobs,
                "AE-COMMISSIONING-ITERATION-OK"))
   PY
   ```

6. Аудит мутаций (S10): `INFORMATION_SCHEMA.JOBS_BY_PROJECT` за окно прогона. Для
   `sa-ae-reader` допустим только `statement_type = 'SELECT'`.
7. Уборка: закрыть draft PR и удалить ветку `ae/…`. Прогон, артефакты Actions (30 дней) и
   `autonomy-state` остаются как доказательства.

Любой FAIL оставляет AE выключенным.

## 4. Канарейка UBR-011 (НЕ запускается в этой фазе)

Фикстура: `quality/autonomy/examples/objective.ubr011.canary.json` (`execute: false`). Запуск —
отдельное решение владельца после S9 PASS, по той же схеме §3 шаги 1–4. Честные исходы: draft PR
с исправлением, не меняющим смысла условия, или `WAITING_FOR_HUMAN`. Зелёный PR с ослабленной
проверкой должен остановить детектор (UNSAFE).

## 5. Остановка и откат

| Ситуация | Действие |
|---|---|
| Остановить всё немедленно | `gh variable delete AE_ENABLED`; активный прогон — `gh run cancel <id>` |
| Остановить наблюдателя | `gh variable delete AE_READER_SA` (расписания нет и так) |
| Убрать AE целиком | revert PR; targeted destroy M3; удалить правила M5 |
| Вернуть широкие привязки | только осознанно, командами отката M2: это возвращает риск S1 |
| Прогон в BLOCKED/WAITING | прочитать issue; закрыть прогон вручную (COMPLETED в `autonomy-state`) или дать ACK плана (`owner_ack.plan_sha256`) и `autonomy-run -f run_id=…`. Кандидата с TCB ACK не публикует — PR открывает человек |

## 6. Включение планового наблюдения (НЕ в этой фазе)

Отдельный PR владельца возвращает `schedule:` в `autonomy-watch.yml` и снимает тест
`test_no_scheduled_autonomy`. Условие: S1–S10 PASS и канарейка UBR-011 с честным исходом.

## 7. Проверка локально (без production)

```bash
python -m pytest -q tools/tests/test_autonomy_*.py
python -m tools.autonomy.wif_check            # желаемое состояние Terraform, код 0
python -m tools.autonomy.wif_check --snapshot quality/autonomy/wif_live_snapshot_2026-09-24.json   # код 1: живое состояние до исправления
```
