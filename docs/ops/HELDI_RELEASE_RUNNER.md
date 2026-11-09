# Серверный исполнитель пакетов выпуска и репетиций Heldi (client_001)

Статус: **дизайн**, Terraform и тесты в PR. **Ничего не применено.** Применение — только по OWNER ACK (§7).

Решение владельца 09.11: разработка одобрена при УЗКИХ правах и СТРОГОЙ безопасной паузе. Постоянного права
менять production нет: в покое исполнитель только читает; права на изменение выдаёт ACK конкретного выпуска
на окно ≤ 8 ч, и они гаснут сами (условие IAM `request.time`).
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
| Роль | `heldiRunnerWrite` (проект, условие: tenant_ops, tenant_locks **и окно выпуска**; только exec, только в окне) | `bigquery.tables.create`, `bigquery.tables.updateData` |
| Роль | `heldiRunnerTick` (проект, условие: окно выпуска **и безопасная минута цикла */3**; только exec, только в окне) | `cloudscheduler.jobs.pause`, `cloudscheduler.jobs.resume` |
| Роль | `heldiRunnerController` (грант на задании `tenant-backfill-controller`, условие: окно выпуска; только exec, только в окне) | `run.jobs.get`, `run.jobs.run`, `run.jobs.update` |
| Роль | `heldiRunnerActAs` (грант на SA `sa-backfill-controller`, условие: окно выпуска; только exec, только в окне) | `iam.serviceAccounts.actAs` |
| Бакет | `mpa-t-client-001-heldi-release-journal` | журнал и логи, EUROPE-WEST1, только добавление |
| Cloud Run Job | `heldi-release-exec` | образ пакета по digest, `max_retries = 0`, таймаут 6 ч |
| Cloud Run Job | `heldi-release-rehearse` | тот же образ, режим копии, таймаут 24 ч |

Read-only plan 09.11 (пустой локальный state, провайдер читает живой проект):
- покой (первый apply, без окна и пинов): **17 to add, 0 to change, 0 to destroy** — SA, 6 ролей, только читающие
  гранты, бакет; ни одной привязки на изменение production;
- окно выпуска (`release_window`): **21 to add** от нуля = +4 привязки с условием времени поверх покоя;
- окно + задания (пины и digest-заглушки): **23 to add** от нуля = ещё +2 задания.

## 2а. Окно выпуска и безопасная пауза

- `release_window = {ack_sha256, not_before, expires}` (UTC, не длиннее 8 ч; иначе plan отклоняет переменную).
  Четыре привязки на изменение создаются только с окном, и у каждой условие
  `request.time >= timestamp(not_before) && request.time < timestamp(expires)` — по истечении права гаснут сами,
  даже если закрывающий apply не сделан. Закрывающий apply ACK (без окна) убирает привязки: −4.
- `pause/resume` tick дополнительно разрешены IAM только во 2-й минуте каждого трёхминутного цикла (минуты UTC
  1, 4, …, 58): не раньше 60 с после срабатывания `*/3` и не позже 60 с до следующего. Вызов на границе тика
  невозможен на уровне IAM, а не только по договорённости кода.
- Код образа (контракт `heldi_runner.entry`, следующий PR) перед pause: ни одного исполнения
  `tenant-backfill-controller` без `completionTime`; после pause — readback: tick PAUSED и за это время не
  появилось новых исполнений. Иначе стадия кончается без дальнейших записей (как freeze пакетов C-R/eca0335a).
  Пауза посреди пробуждения (STOP bf7d1370, 28.10) так невозможна дважды: и по времени, и по исполнениям.
- Запись журнала в свой бакет (journal/, logs/) остаётся и в покое: это не production, объекты только
  добавляются.

Чего нет и не будет: Scheduler, SA-будильника, `roles/*` (кроме `storage.objectCreator/objectViewer` на бакете),
грантов на ozon_raw/ref/ozon_mart/analytics_share на запись, `delete`/`setIamPolicy` где-либо, доступа к EVETIS.

## 3. Права: что сужено и что нет

- **BigQuery.** ACL датасетов авторитетны в `infra/tenant` (T3.3), поэтому — уровень проекта с условием IAM на
  датасеты, как `heldiWatchData`. Запись — только создание таблицы и дописывание строк в tenant_ops/tenant_locks:
  нет `tables.delete`, `tables.update`, `datasets.update`.
- **Run.** Гранты на уровне задания `tenant-backfill-controller`. Задания runtime Ozon (`ozon-runtime-*`)
  и `tenant-control` недоступны. `actAs` — ровно на SA контроллера (UpdateJob сохраняет его SA).
- **Scheduler — остаточный риск (сужен временем).** У Cloud Scheduler нет IAM на уровне задания, и он не
  поддерживает `resource.name` в условиях IAM (`docs.cloud.google.com/iam/docs/conditions-resource-attributes`,
  Firecrawl 09.11). Поэтому `pause/resume` — на проекте, но только в окне выпуска и только в безопасную минуту
  цикла: в этих пределах SA технически может поставить на паузу или возобновить и `ozon-daily/fast/weekly`. Сужение: код образа вызывает только `…/jobs/tenant-backfill-tick:pause|resume`
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

1. **Первый apply (покой)** — личности, роли, только читающие гранты, бакет; заданий и прав на изменение нет.
   Ожидается 17 to add, 0 change, 0 destroy.
2. **Код `tools/heldi_runner`** (entry, cloudbuild, Dockerfile, тесты) — отдельный PR, без ACK.
3. **Каждый выпуск на сервере** — собственный ACK пакета: сборка образа; apply с пинами (+2 задания, если их ещё нет)
   и репетиция (`heldi-release-rehearse`, только чтение); после её PASS — apply с `release_window` (+4 привязки,
   окно ≤ 8 ч), стадии записи; закрывающий apply без окна (−4). Если закрывающий apply не сделан — права всё равно
   гаснут в `expires`.
4. Откат: plan/apply без `exec_image`/`rehearse_image`/`package_pins` (−2 задания); полностью — destroy корня.
   Бакет с непустым журналом не удаляется (`force_destroy = false`, политика хранения) — это намеренно.
   Пользовательские роли после удаления 7 суток нельзя создать с тем же id (повтор — через undelete).
