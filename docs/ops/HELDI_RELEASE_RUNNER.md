# Серверный исполнитель пакетов выпуска и репетиций Heldi (client_001)

Статус: **дизайн**, Terraform и тесты в PR. **Ничего не применено.** Применение — только по OWNER ACK (§7).
Корень Terraform: `infra/heldi-release-runner/`. Поток E (мандат владельца 09.11, «долговечный runner релизов»).

## 1. Зачем

Исполнители пакетов (`~/heldi-work/package-*/package_*.py`) и репетиции идут на ноутбуке владельца часами:
- умирают при перезапуске сессии Claude и при сне машины;
- с домашнего IP упираются в HTTP 403 защиты Google от частых запросов;
- журнал `OPERATIONS.jsonl` живёт в одном локальном каталоге.

Краткосрочно (сейчас): `~/heldi-work/ops/run_detached.sh` — `nohup caffeinate -i -s` в своей сессии процессов,
журнал, pid и код выхода, отказ второй копии. Сон при закрытой крышке на батарее он не предотвращает.

Среднесрочно (этот документ): то же исполнение в Cloud Run Job проекта `mpa-t-client-001`.

## 2. Ресурсы

| Ресурс | Имя | Назначение |
|---|---|---|
| SA | `sa-heldi-release-exec` | стадии пакета с правом записи (под ACK) |
| SA | `sa-heldi-rehearse` | репетиция на копии, production только читается |
| Роль | `heldiRunnerRead` (проект, обе SA) | `bigquery.jobs.create`, `cloudscheduler.jobs.get/list`, `logging.logEntries.list`, `run.executions.get/list`, `run.jobs.get/list`, `run.operations.get` |
| Роль | `heldiRunnerData` (проект, условие на ozon_raw, ref, tenant_ops, tenant_locks; обе SA) | `bigquery.datasets.get`, `bigquery.tables.get/getData/list` |
| Роль | `heldiRunnerWrite` (проект, условие на tenant_ops, tenant_locks; только exec) | `bigquery.tables.create`, `bigquery.tables.updateData` |
| Роль | `heldiRunnerTick` (проект, только exec) | `cloudscheduler.jobs.pause`, `cloudscheduler.jobs.resume` |
| Роль | `heldiRunnerController` (грант на задании `tenant-backfill-controller`, только exec) | `run.jobs.get`, `run.jobs.run`, `run.jobs.update` |
| Роль | `heldiRunnerActAs` (грант на SA `sa-backfill-controller`, только exec) | `iam.serviceAccounts.actAs` |
| Бакет | `mpa-t-client-001-heldi-release-journal` | журнал и логи, EUROPE-WEST1, только добавление |
| Cloud Run Job | `heldi-release-exec` | образ пакета по digest, `max_retries = 0`, таймаут 6 ч |
| Cloud Run Job | `heldi-release-rehearse` | тот же образ, режим копии, таймаут 24 ч |

Read-only plan 09.11 (пустой локальный state, провайдер читает живой проект):
- первый apply (без пинов пакета): **21 to add, 0 to change, 0 to destroy** (SA, роли, гранты, бакет; заданий нет);
- второй apply (пины + digest-заглушки): **23 to add** от нуля, то есть +2 задания поверх первого.

Чего нет и не будет: Scheduler, SA-будильника, `roles/*` (кроме `storage.objectCreator/objectViewer` на бакете),
грантов на ozon_raw/ref/ozon_mart/analytics_share на запись, `delete`/`setIamPolicy` где-либо, доступа к EVETIS.

## 3. Права: что сужено и что нет

- **BigQuery.** ACL датасетов авторитетны в `infra/tenant` (T3.3), поэтому — уровень проекта с условием IAM на
  датасеты, как `heldiWatchData`. Запись — только создание таблицы и дописывание строк в tenant_ops/tenant_locks:
  нет `tables.delete`, `tables.update`, `datasets.update`.
- **Run.** Гранты на уровне задания `tenant-backfill-controller`. Задания runtime Ozon (`ozon-runtime-*`)
  и `tenant-control` недоступны. `actAs` — ровно на SA контроллера (UpdateJob сохраняет его SA).
- **Scheduler — остаточный риск.** У Cloud Scheduler нет IAM на уровне задания, и он не поддерживает
  `resource.name` в условиях IAM (`docs.cloud.google.com/iam/docs/conditions-resource-attributes`, прочитано через
  Firecrawl 09.11). Поэтому `pause/resume` выдаются на проект: SA технически может поставить на паузу или возобновить
  и `ozon-daily/fast/weekly`. Сужение: код образа вызывает только `…/jobs/tenant-backfill-tick:pause|resume`
  (так уже устроен `send()` пакетов: бюджет запросов на стадию); наблюдатель `heldi-health-eval` даёт FAIL
  `ORDINARY_SCHEDULER_NOT_PAUSED`. `update/delete/create/run` расписаний нет.
- **Почему не SA контроллера.** Исполнитель меняет само задание контроллера; общая личность смешала бы
  «кто менял» в журнале аудита и дала бы контроллеру право на собственный UpdateJob.
- **Кто запускает.** Только владелец (`roles/owner` проекта): `gcloud run jobs execute … --args=<стадия>`.
  Каждое исполнение — одна стадия ACK. Отдельных invoker-грантов нет.

## 4. Пины: ACK, пакет, исполнитель

Образ `heldi-release-runner@sha256:…` собирается Cloud Build в `mpa-platform` (как контроллер и наблюдатель),
`FROM python:3.12-slim@sha256:<digest>`. Контекст сборки:
1. детерминированный архив каталога пакета (`tar --sort=name --mtime=@0 --owner=0 --group=0`), sha256 =
   `package_sha256`;
2. `git archive <executor_source>` — исходник, против которого пакет квалифицирован;
3. текст OWNER ACK (`OWNER_ACK.txt`), sha256 = `ack_sha256`.

Шаги сборки (fail-closed): сверить три sha256 с подстановками; прогнать офлайн-тесты пакета `--network=none`;
проверить, что без своей личности образ завершается exit 3 до первого запроса.

В Terraform передаются digest образа и `package_pins` (`package`, `ack_sha256`, `package_sha256`,
`executor_source`) — они становятся env задания. При **каждом** старте `heldi_runner.entry` до первого запроса:
- личность metadata = `EXPECTED_IDENTITY` (exec или rehearse; чужая личность — exit 3);
- sha256 вшитого ACK, архива пакета и `CONTROLLER_SOURCE_SHA` исходника = env;
- стадия из `--args` есть в списке стадий ACK; в режиме REHEARSE запись в production запрещена транспортом
  (как `overlay2.Copy` в репетициях: писатель — заглушка с отказом).

Новый пакет = новый образ = plan «0 add, 2 change» (image и env двух заданий). Тег не используется нигде.

Нужная правка пакетов (одна, в шаблоне): `token()` берёт токен из metadata, когда `RUNNER_MODE` задан, вместо
`gcloud auth print-access-token`. Остальной код пакетов не меняется.

## 5. Журнал и логи

- Бакет с политикой хранения 400 сут (**не** заблокирована: снять может только владелец). Пока она действует,
  объект нельзя удалить или перезаписать, в том числе самому исполнителю (`storage.objects.delete` нет).
- Каждое событие `OPERATIONS.jsonl` (STAGE_INTENT/RESULT, REQUEST_INTENT/RESULT) — новый объект
  `journal/<пакет>/<seq:06d>-<тип>.json`, создаётся с `ifGenerationMatch=0`.
- Перед стадией entry восстанавливает `OPERATIONS.jsonl` из объектов по порядку seq: шлюзы пакета
  (`stage_done`, свежесть gate ≤ 15 мин, `unknown_intents`) работают как на ноутбуке.
- **Одна копия.** Два одновременных исполнения посчитают один и тот же следующий seq; второе получит 412 и
  остановится до первого запроса. Найденный `REQUEST_INTENT` без `REQUEST_RESULT` — неизвестный исход: стадия
  не повторяется, нужна readback-стадия (правило мандата).
- stdout/stderr — Cloud Logging (`_Default`, 30 сут); копия лога исполнения — `logs/<execution>.log` в бакете.
- Репетиция пишет только `rehearsal/<пакет>/<run>/` (итоговый `REHEARSAL_*.json`, журнал копии).

## 6. Стоимость (оценка по прайсу Cloud Run Tier 1, не проверялась свежим источником)

| Статья | Расчёт | В месяц |
|---|---|---|
| Cloud Run Jobs, 1 vCPU + 2 GiB | ≈ 0,079 $/ч; репетиция 6 ч + 10 стадий по 10 мин ≈ 7,7 ч | ≈ 0,6 $ (до free tier) |
| Cloud Storage | журнал — единицы МБ | < 0,01 $ |
| Cloud Build | сборка образа ≈ 3 мин на пакет | ≈ 0,02 $ за сборку |
| Cloud Logging | < 1 ГиБ | 0 $ (в бесплатном объёме) |

Free tier Cloud Run (180 000 vCPU-с) считается на платёжный аккаунт и может быть занят другими проектами.

## 7. Порядок включения (каждый шаг — по OWNER ACK)

1. **Первый apply** — личности, роли, гранты, бакет; заданий нет. Ожидается 21 to add, 0 change, 0 destroy.
2. **Код `tools/heldi_runner`** (entry, cloudbuild, Dockerfile, тесты) — отдельный PR, без ACK.
3. **Первый пакет на сервере** — его собственный ACK: сборка образа, второй apply (+2 задания), затем стадии.
   Репетиция идёт первой (`heldi-release-rehearse`), стадии записи — только после её PASS.
4. Откат: plan/apply без `exec_image`/`rehearse_image`/`package_pins` (−2 задания); полностью — destroy корня.
   Бакет с непустым журналом не удаляется (`force_destroy = false`, политика хранения) — это намеренно.
   Пользовательские роли после удаления 7 суток нельзя создать с тем же id (повтор — через undelete).
