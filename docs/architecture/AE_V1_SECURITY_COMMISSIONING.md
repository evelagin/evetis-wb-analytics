# AE v1 — атакующий разбор безопасности и ввод в эксплуатацию

Дата: 2026-09-24. База: `origin/main` = `4d2f3fa8` (PROMO-2 capture). В ветку AE слит без
конфликтов: PROMO-2 меняет манифесты `sql/current` и тесты промо, файлов AE не касается.
Итог фазы: **AE остаётся выключенным**. Статус — `SECURITY_REVIEWED / FIXES_IN_GIT /
AWAITING_OWNER_MUTATIONS`, а не `READY_FOR_CONTROLLED_CANARY`: S1, S7 и S9 требуют мутаций IAM и
действий в консоли Anthropic, на которые разрешения не было.

Модель противника: Engineer и код кандидата враждебны. У кода на GitHub-hosted раннере есть
`sudo`. Он читает всё на своей машине: окружение и память процесса раннера, а значит,
`ACTIONS_ID_TOKEN_REQUEST_*` (выпуск OIDC с любой audience), `ACTIONS_RUNTIME_TOKEN` (кэш и
артефакты) и `GITHUB_TOKEN` своего job'а.

## 1. Находки

| # | Серьёзность | Находка | Исправление | Доказательство |
|---|---|---|---|---|
| F-1 | CRITICAL, живая | `sa-deployer` и `sa-terraform-plan` доступны любому workflow репозитория, `sa-terraform-apply` — любому workflow на `main`. Job'ы AE (инженер, тесты, **ревьюер**) получают все три | `wif.tf`: точный `workflow_ref@refs/heads/main`, для plan — вычисленный `tf_plan_workflow` | снимок живого провайдера: `wif_check --snapshot` → 24/25 FAIL; Terraform → 25/25 PASS |
| F-2 | HIGH, в PR #165 | Первая версия исправления: deployer по **пути** файла на любой ветке. `extract('{path}@')` принимает файл `deploy-prod.yml@x.yml`; ветка кандидата с копией `deploy-prod.yml` тоже проходила | точное равенство `workflow_ref`, только `main` | `test_previous_path_only_design_would_leak_deployer`, случаи D1, E1, E2 |
| F-3 | CRITICAL, код AE | Доверенный `verify` запускал `python tools/impact_analysis.py` **в песочнице кандидата**. Кандидат подменяет скрипт (или кладёт `tools/json.py`), и его код исполняется рядом с `GH_TOKEN` (`contents: write`) при отсутствии защиты ветки — запись в `main` | анализ влияния только из доверенного checkout (`trusted_repo`); окружение подпроцессов без `GH_*`/`ACTIONS_*`/`ANTHROPIC_*` | `test_trusted_replay_never_executes_candidate_impact_tool` воспроизводит атаку: исполнение в песочнице срабатывает, доверенный путь — нет |
| F-4 | HIGH, код AE | Воспроизведение недоверенного вывода было fail-open: без `--expect` sha256 не сверялся (`ReplayAdapter`, `ReplayEvidenceRunner`) | хеши обязательны, без них отказ | `test_replay_without_declared_hashes_is_refused`, `test_cli_replay_without_expect_fails_closed` |
| F-5 | MEDIUM | `repository_sha` цели не ограничен: доверенный `prepare` исполнил бы pytest произвольного коммита | `--trusted-base-ref origin/main`: sha обязан быть предком | `test_objective_with_base_outside_trusted_branch_is_refused` |
| F-6 | HIGH | `git diff --name-only` с распознаванием переименований показывает только новое имя: `.github/workflows/x.yml → docs/x.yml` обходил запрет | `--no-renames` в песочнице и публикаторе | `test_rename_out_of_tcb_is_still_a_tcb_change` |
| F-7 | MEDIUM | Кэш: недоверенный job (ref `main`) через runtime-токен пишет в кэш области `main`, `ci.yml` на ветке `ae/*` читает pip-кэш без `--require-hashes`. Получается незаметная подмена зависимостей в обязательном CI | pip-кэш убран из `ci.yml` (npm-кэш сверяется с lockfile) | `test_required_workflow_does_not_restore_unverified_caches` |
| F-8 | MEDIUM | `sa-ae-reader` был привязан к `workflow_ref` `autonomy-run.yml`, поэтому ревьюер получал чтение production | привязка по `job_workflow_ref` без `autonomy-review.yml` | случай C1: ревьюер не получает ничего |
| F-9 | MEDIUM | Поверхность инъекции: guard `run-*` пропускал `run-$(…)`; `${{ }}` внутри `run:`, включая сообщение коммита в доверенном job'е | строгая регулярка `^run-\d{8}T\d{6}Z-[0-9a-f]{8}$`, `expect`, `state_sha`; все выражения через `env` | `test_no_expression_interpolation_inside_run_scripts`, `test_run_id_is_strictly_validated…` |
| F-10 | MEDIUM | Сторонние действия по изменяемому тегу в job'ах с токеном записи | закреплены по SHA коммита | `test_third_party_actions_pinned_by_commit_sha` |
| F-11 | HIGH | `READY_FOR_PR → COMPLETED` без CI: PR от `GITHUB_TOKEN` не запускает `pull_request` | S8: диспатч обязательных workflows + `ci-verify` + состояния `AWAITING_VERIFICATION`/`READY_FOR_HUMAN_REVIEW` | `test_autonomy_verification.py` |
| F-12 | MEDIUM | Плановый наблюдатель включался бы сам, как только задан `AE_READER_SA` | `schedule:` удалён | `test_no_scheduled_autonomy` |
| F-13 | MEDIUM | Узкий список запретов (UNSAFE): вне него были `conftest.py`, зависимости, Dockerfile, откаты, Metabase, `skills/`, тесты самих ворот, `quality/**` | TCB из 7 классов, минимум `HUMAN_DECISION_REQUIRED`, не снимается ACK плана | `test_autonomy_tcb.py` |
| F-14 | LOW | `GH_TOKEN` был в окружении всего недоверенного job'а | только на шаге клонирования (через `sudo` он всё равно читаем, но `contents: read`) | workflows |

## 2. Что осталось (не исправлено в Git)

R-1…R-7 — [AE_V1_SECURITY.md §8](AE_V1_SECURITY.md#8-остаточные-риски). Главные: запись в `main` ограничена
кодом, а не платформой (тариф); `actions: write` у доверенных job'ов запускает любой workflow;
злонамеренный кандидат подделывает свои тесты и обязательный CI; вынос прочитанных данных.

## 3. S1–S10

| | Требование | Результат | Машинная проверка |
|---|---|---|---|
| S1 | Привилегированный WIF недоступен job'ам AE | **FAIL живьём / PASS в Git** — не применено (M1–M2) | `wif_check --live` сейчас код 1 (24/25 FAIL); Terraform — код 0 (25/25); 55 тестов |
| S2 | Изменения TCB требуют решения человека | **PASS** | 40 тестов TCB + 8 классов end-to-end |
| S3 | Кандидат не меняет production | **PASS в Git**, живьём держится на S1 | роли `sa-ae-reader` только на чтение, секретов нет, `ReadOnlyBigQuery` |
| S4 | Кандидат не пишет в `main` | **PASS** на границе job'ов; платформенной защиты ветки нет (R-1) | права job'ов, refspec, F-3 |
| S5 | Недоверенный job не подделывает доверенное состояние | **PASS** | sha256 обязательны (F-4), доверенный шаг не исполняет кандидата (F-3), база в истории `main` (F-5) |
| S6 | Engineer и Reviewer — независимые вызовы | **PASS структурно** (разные job'ы и машины, песочницы, экземпляры адаптера, идентичности); живьём — в S9 | `test_autonomy_ci_trust.py`, матрица федерации |
| S7 | Anthropic WIF без статического ключа | **BLOCKED** — конфигурация готова и проверена (18 тестов), обмен не выполнялся: нужна консоль Anthropic (M5) | `test_autonomy_anthropic_federation.py` |
| S8 | Автономный PR получает фактическую проверку | **PASS** реализовано и проверено; живьём — в S9 | `test_autonomy_verification.py` |
| S9 | Живой цикл Engineer → Reviewer → Gatekeeper | **NOT RUN / BLOCKED** — нужны M1–M7 (IAM, консоль Anthropic, workflows в ветке по умолчанию) | цель и детерминированный вердикт готовы: `commissioning.py`, 16 тестов |
| S10 | Аудит production-мутаций = 0 | **PASS** | см. §4 |

## 4. Аудит production-мутаций

`region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT`, 2026-09-24 00:00 UTC — момент снятия:

- идентичности AE: заданий **0** (`sa-ae-reader` не существует);
- этой фазы: только `SELECT` (аудит-запросы с меткой `purpose=ae-security-commissioning-audit`),
  метаданные датасетов через REST (`datasets.get`, не задания), `gcloud … describe/get-iam-policy`;
- не-`SELECT` под учётной записью владельца (`INSERT/UPDATE wb_raw.INGEST_RUNS` в :23/:31 каждого
  часа, `MERGE wb_raw.FINANCE_*` в 07:26 МСК, `CREATE OR REPLACE VIEW wb_raw.REF_SKU_MASTER` +
  `DELETE wb_raw.REF_SKU_MASTER_DATA` в 08:22 МСК) — штатные загрузчики и синхронизация
  справочника Apps Script, исполняются под учётной записью владельца. К AE отношения не имеют;
- IAM, Terraform, расписания, переменные GitHub — не менялись. `terraform plan` выполнен с
  `-lock=false`, без apply.

## 5. Проверки

- `tools/tests`: **679 passed**, из них AE — 294 (acceptance, ci_trust, security, units, tcb,
  verification, wif, anthropic_federation, commissioning);
- `validate_current_sql`: C1–C18 OK, 29 объектов; `actionlint`: все workflows чисты;
- `terraform fmt`/`validate`: OK; read-only targeted plan: `21 to add, 1 to change, 3 to destroy`,
  все изменения — WIF и `sa-ae-reader`, чужого дрейфа (`sa-ct-refresh`) нет.
