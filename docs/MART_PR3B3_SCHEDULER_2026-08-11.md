# PR-Mart3b-3 — Cloud Scheduler для витрины (`wb-mart-prod`)

**Дата:** 2026-08-11
**Ветка:** `feat/mart3b-3-scheduler`
**Предшественники:** PR #83/#84 (loader `mart` + `MART_RUNS`), #85 (generic CLI + lease + Job + IAM), #86 (`REF_COST_MAP` `__NULL__+penalty`), #87 (sales retry 429)
**Статус после PR:** планировщик СОЗДАН, но `paused = true`. Снятие паузы — отдельный шаг PR-Mart3b-4.
**Ревизия:** REV2 — учтены оба блокера аудита (fail-fast `wb-mart+shadow`; расписание пересчитано по данным).

## Зачем

`wb-mart-prod` проходит зелёным с 10.08 (прогон `wb-mart-prod-dfq9n`: `MART_RUNS` COMPLETE,
target_date 2026-08-09, mart_rows 7012, `lease_only_no_mart = FALSE`, `exit(0)`), но запускается
**руками**. Пока это так, `MART_SKU_DAILY` свежа ровно до дня последнего ручного запуска — то есть
любой дашборд поверх витрины показывает произвольно устаревшие данные.

## Что делает PR

| Файл | Изменение |
|---|---|
| `infra/terraform/scheduler.tf` | `google_cloud_scheduler_job "wb_mart_prod"` — `0 7,9,12,16 * * *`, `Europe/Moscow`, `paused = true`, `ignore_changes = [paused]`, OAuth от `sa-scheduler-prod`, POST на Run Admin API `…/jobs/wb-mart-prod:run` |
| `infra/terraform/iam.tf` | `google_cloud_run_v2_job_iam_member "scheduler_mart_prod_invoke"` — `roles/run.invoker` для `sa-scheduler-prod` на Job `wb-mart-prod` |
| `infra/terraform/cloud_run_jobs.tf` | снят устаревший комментарий «Планировщик НЕ создаётся здесь» |
| `.github/workflows/scheduler-control.yml` | входы → `type: choice`; **fail-fast первым шагом** на `wb-mart` + не-prod |
| `sql/mart3/pr_mart3b3_freshness_readiness.sql` | **новый** — read-only запросы, которыми выбрано расписание; перепрогонять на rollout |

Новых прав на проект не выдаётся: `roles/cloudscheduler.admin` у `sa-terraform-apply` уже есть,
`sa-scheduler-prod` уже существует. Добавляется ровно один **поресурсный** invoker-биндинг.

## Ключевые проектные решения

### 1. Имя — `wb-mart-prod`, не `mart-daily-prod`

`scheduler-control.yml` собирает имя как `"<loader>-<environment>"`. Любое другое имя сделало бы
планировщик витрины неуправляемым из workflow, и pause/resume пришлось бы делать в Console.
Cloud Scheduler и Cloud Run — разные типы ресурсов, совпадение имён коллизии не создаёт.

### 2. Окна — это попытки ОДНОГО построения

Идемпотентность обеспечивает lease в `LOADER_RUNS` (`cloud/src/bq/runManifest.ts`), ключ —
`(environment, loader_name, logical_period)`, где `logical_period` для mart = **D-1 МСК**.
Поэтому все окна одного дня целятся в одну строку lease:

| Состояние на момент окна | `acquire()` | Поведение |
|---|---|---|
| прошлое окно дало COMPLETE | `active > 0`, `cur_status = COMPLETE` | `guard_skip`, `exit(0)`, `wb_mart` не трогается |
| прошлое окно дало ERROR | ERROR не активен → `active = 0` | новая строка STARTED, **новый run_id** → полноценный ретрай |
| прошлое окно ещё идёт (< 30 мин) | свежий STARTED | `ALREADY_RUNNING`, `exit(0)` |
| STARTED «завис» > 30 мин | stale | recovery новой строкой |

### 3. ⚠️ Расписание `0 7,9,12,16` выбрано по данным (аудит REV2, блокер #2)

Исходное предположение «источники подтягиваются позже, поэтому нужны окна 09/10/11 и, возможно,
поздние фолбэки» **эмпирически неверно**. Почасовой прогон гейта по `V_INGEST_HEARTBEAT`
(`sql/mart3/pr_mart3b3_freshness_readiness.sql`, 7 полных суток 03–09.08.2026):

| Час МСК | Зелёных суток | Кто красный |
|---|---|---|
| 00:00–05:00 | 0 / 7 | ads ещё не отработал |
| **06:00–10:00** | **7 / 7** | — |
| **11:00** | **0 / 7** | **sales** |
| **12:00–23:00** | **7 / 7** | — |

Источники готовы **уже к ~05:11–05:12 МСК** каждый день (лимитирует ads: старт 05:07 → COMPLETE
05:11). Проблема не в поздней готовности, а в **детерминированной регрессии**:

```
05:11  ads COMPLETE            → гейт зелёный
09:22  sales COMPLETE
09:31  orders COMPLETE
10:22  sales ERROR (WB HTTP 429 «Limited by global limiter, per seller»)  → гейт КРАСНЫЙ
10:31  orders COMPLETE         (не помогает: LATEST-ATTEMPT sales = ERROR)
11:22  sales COMPLETE          → гейт снова зелёный
```

Слот sales 10:22 падал **7 раз из 7** — это не случайный transient, а воспроизводимый отказ.
Семантика LATEST-ATTEMPT превращает его в ровно часовое «окно красноты» 10:2x → 11:2x, в которое
окно 11:00 попадает всегда. Поэтому 11:00 из расписания исключено, а окна разнесены по обе стороны
отказа:

- **07:00** — основное. Источники готовы с ~05:11; выбран с зазором от Apps Script-окна триггера
  ads (`atHour(5)` может сработать в любой момент 05:00–06:00), поэтому не 06:00.
- **09:00** — резерв до отказа sales.
- **12:00** — первый безопасный час после восстановления sales в 11:22.
- **16:00** — глубокий фолбэк на случай длительной недоступности WB.

Дополнительная выгода 07:00: витрина за D-1 готова к началу рабочего дня, а не к 09:05.

### 4. Почему `retry_count = 1` у Scheduler безопасен

Вызов `:run` **асинхронный**: `200` означает «execution создан», исход прогона Scheduler не видит.
Значит retry Scheduler повторяет только неудавшийся *вызов API*. Единственный неприятный сценарий —
успешный `:run` с потерянным ответом: повтор создаст второй execution, его отсекает lease по свежему
STARTED (`ALREADY_RUNNING`, `exit(0)`).

Это принципиально отличается от `max_retries` у Cloud Run Job, который у `wb-mart-prod` выставлен
в **0** (аудит REV2, блокер #2 предыдущего PR): task-retry повторяет task в ТОМ ЖЕ execution → тот
же `CLOUD_RUN_EXECUTION` → тот же `run_id` → вторая строка `LOADER_RUNS` с тем же `run_id` и
`MART_RUNS_CONFLICT`.

### 5. Fail-fast на `wb-mart` + не-prod (блокер #1)

`mart` помечен `prodOnly` в реестре загрузчиков, Terraform создаёт только `wb_mart_prod` — job
`wb-mart-shadow` не существует никогда. Проверка стоит **первым шагом, до WIF-аутентификации**:
иначе оператор получает невнятный `NOT_FOUND` от `gcloud` уже после того, как workflow взял
привилегированный `sa-terraform-apply`.

## Границы доказательства (читать перед тем, как доверять расписанию)

1. **История heartbeat — 7 полных суток.** `INGEST_RUNS` появился 02.08 (PR#82), запрошенных
   аудитом 14–30 дней физически нет. Расписание нужно перепроверить тем же SQL после 2–3 недель
   работы.
2. **ads делает РОВНО ОДНУ попытку в сутки** (05:07, hourly-ретраев нет — в отличие от
   orders/sales). Если падает ads, **ни одно окно этого дня витрину не спасёт**. Это закрывается
   алертами (PR-Mart3b-5) и/или ретраем самого ads-триггера, но не расписанием mart. За 9 суток
   наблюдения ads не падал ни разу — выборка слишком мала, чтобы считать это гарантией.
3. **Эффект PR #87 ещё не подтверждён в проде.** Фикс ретрая 429 влит в `main` (`9f01ee3`), но
   отказ sales 10:22 воспроизводился и 10.08 — то есть на момент написания PR синхронизация `.gs`
   в Apps Script-проект не подтверждена. Контроль — запрос Q4 в
   `sql/mart3/pr_mart3b3_freshness_readiness.sql`: как только слот 10:2x стабильно зелёный,
   окна 10:00/11:00 можно вернуть, а 16:00 — убрать.

## Критерии приёмки

**После `terraform apply` (планировщик ещё paused):**

1. `terraform plan` идемпотентен — второй прогон даёт `No changes`.
2. `gcloud scheduler jobs describe wb-mart-prod --location <region>` → `state: PAUSED`,
   `schedule: 0 7,9,12,16 * * *`, `timeZone: Europe/Moscow`, `httpTarget.uri` заканчивается на
   `/jobs/wb-mart-prod:run`, `httpTarget.oauthToken.serviceAccountEmail = sa-scheduler-prod@…`.
3. `gcloud run jobs get-iam-policy wb-mart-prod --region <region>` содержит `roles/run.invoker`
   для `sa-scheduler-prod`.
4. Планировщики stocks не изменились (в plan нет их diff).
5. **Fail-fast:** запуск `scheduler-control.yml` с `loader=wb-mart`, `environment=shadow` падает на
   первом шаге с явным сообщением и НЕ доходит до `google-github-actions/auth`.

**Боевой путь БЕЗ снятия паузы** (`scheduler-control.yml` → `run-now`, `wb-mart`, `prod`):

6. `LOADER_RUNS(loader='mart', prod)` → `status = COMPLETE`, `attempt_count = 1`.
7. `MART_RUNS` → `status = COMPLETE`, `target_date = D-1`, `mart_rows > 0`, `error_code IS NULL`.
8. `V_MART_RUN_LOG` → `lease_only_no_mart = FALSE`.
9. **Идемпотентность после успеха:** повторить `run-now`. Ожидается `guard_skip` с
   `reason = COMPLETE`, `exit(0)`, `mart_rows` не меняется, новой строки `MART_RUNS` нет.
10. **Конкурентность (аудит REV2):** пока прогон №1 ещё RUNNING, запустить №2 —
    `gcloud run jobs execute wb-mart-prod --region <region>` **без** `--wait`, затем сразу второй.
    Ожидается: №2 → `guard_skip` с `reason = ALREADY_RUNNING`, `exit(0)`. После завершения №1
    запустить №3 → `guard_skip` с `reason = COMPLETE`. Так проверяются обе блокирующие ветки lease,
    а не только повтор после успеха.
    ⚠️ `run-now` в `scheduler-control.yml` использует `--wait`, поэтому два последовательных вызова
    workflow дадут COMPLETE/COMPLETE — для проверки ALREADY_RUNNING нужен запуск без `--wait`.

## Что НЕ входит в этот PR

- **Снятие паузы** — PR-Mart3b-4 (rollout): `resume` через `scheduler-control.yml`, затем
  наблюдение 3–5 дней с ежедневной сверкой `V_MART_RUN_LOG` и перепрогоном Q2/Q4.
- **Алертинг (ERROR / SLA)** — PR-Mart3b-5. Требует `monitoring.googleapis.com`, notification
  channel с почтой владельца, роли `roles/monitoring.admin` у `sa-terraform-apply` и log-based
  alert policy. **До него слепое пятно:** если падает ads (одна попытка в сутки) или все окна дня
  дают ERROR, никто не узнает без ручного просмотра `V_MART_RUN_LOG`.
- **Ретрай ads-триггера.** Единственная суточная попытка ads — самое узкое место всей цепочки;
  логичный кандидат в PR-Mart3b-6.

## Смежные факты, которые легко забыть

- В Cloud Run мигрированы только `stocks` и `mart`. **Orders, sales, ads, finance по-прежнему на
  Apps Script** и пишут в `wb_raw.INGEST_RUNS` (`source = apps_script`). Поиск по `loader="sales"`
  в логах Cloud Run вернёт ноль. `V_INGEST_HEARTBEAT` = UNION `INGEST_RUNS` + `LOADER_RUNS(prod)`.
- Витрина считает контрибуцию `*_pre_cogs`: **закупочной себестоимости в системе нет**.
  `REF_COST_MAP` — маппинг финансовых операций WB, а не COGS.
- Каденс Apps Script: orders — hourly в :31, sales — hourly в :22, ads — **раз в сутки** ~05:07.
