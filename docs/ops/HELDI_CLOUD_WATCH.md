# Облачное наблюдение Heldi (client_001)

Статус: код, тесты и Terraform в PR; **ничего не применено**. Применение — только по OWNER ACK, порядок ниже.
Предложение: `HELDI_CLOUD_WATCH_PROPOSAL.md` (поток E, после S4.1a). Корень Terraform: `infra/tenant-monitoring/`
(файл `watch.tf`), рядом с уже применёнными оповещениями из `TENANT_ALERTS_CLIENT001.md`.

## 1. Зачем

25.10 локальный `observe_loop` владельца умер в 14:56Z: истёк `gcloud`-токен. Реальный STOP `aaf68ede`
(падение `kc4bg` в 20:09Z, STOP в 20:10Z) обнаружили в 04:22Z 26.10.

Важная деталь: облачная политика «контроллер в STOP» в ту ночь уже существовала. Метрика `tenant_controller_stopped`
насчитала 7 записей в окне 20:00–20:30Z и по 10 записей за каждые 30 минут до утра. Значит, условие политики
выполнялось с 20:10Z. Письмо на почту, скорее всего, было отправлено, но ночью его не увидели. Доставку проверить
нельзя: API инцидентов недоступен.

Поэтому слой 2 закрывает не саму эту ночь, а то, что видел только локальный цикл:
- CURRENT за сутки;
- зависшие исполнения;
- отсутствие продвижения FULL;
- Scheduler обычных загрузок.

Ночной канал по-прежнему почта (см. §6).

## 2. Слои

| Сигнал | Источник | Условие | Письмо |
|---|---|---|---|
| H1 STOP | `controller_stopped` (main.tf, **уже применён**) | `status=STOPPED` за 10 мин | на открытие |
| H2 падение | `heldi_controller_crash` → `controller_crash` | Traceback в stderr контроллера за 10 мин | на открытие |
| H3 тишина | `heldi_controller_status` → `controller_silence` | 0 строк `jsonPayload.status` за 20 мин | открытие и закрытие, **второй apply** |
| Наблюдатель FAIL | строка `HEALTH` `health=FAIL` | log-match | каждое новое состояние |
| Наблюдатель: смена состояния | строка `HEALTH` `health=DEGRADED`/`OK` | log-match | каждое новое состояние |
| Наблюдатель не работает | `heldi_watch_heartbeat` | 0 пульсов за 75 мин | открытие и закрытие, **второй apply** |

**Бэктест сигналов по логам 14–27.10 (только чтение):**
- **H2.** Traceback в stderr контроллера встретился ровно один раз — у `kc4bg`. А «Container called exit(1)» за то
  же время записан около 20 раз. Это штатный `WAITING_READ_RETRY`, который завершается с кодом 1. Поэтому exit(1)
  сигналом не считается.
- **H3.** Вне пауз тика разрывы между строками статуса не превышали 17 мин (25.10, 12:00, после resume).
  При окне 15 мин было бы ложное срабатывание.
- **Длительность.** Самое долгое исполнение длилось 940 с, порог — 1500 с.

## 3. Наблюдатель `heldi-health-eval` (слой 2)

Код: `tools/heldi_watch/health.py`. Это стадия `observe` исполнителя S4.1a без журнала и без паузы.
Наблюдатель только сообщает.

| Проверка | Уровень |
|---|---|
| Неразрешённый STOP. Набор принятых берётся из `tools.tenancy.stop_acceptance` — тот же, что у шлюза контроллера | FAIL |
| Ошибка доказательств в наборе принятия | FAIL |
| Набор принятия не прочитан | DEGRADED |
| К 09:00 МСК у CURRENT сегодняшних суток нет сертификата `CURRENT_CERT` по catalog, stocks, prices или finance_accrual | FAIL |
| К 09:00 МСК цикл CURRENT сегодняшних суток не начат | FAIL |
| `tenant-backfill-tick` не ENABLED | DEGRADED |
| `tenant-backfill-tick` отсутствует | FAIL |
| Расписание ozon-daily, -fast или -weekly не PAUSED | FAIL |
| Исполнение контроллера идёт дольше 1500 с или уже завершилось так | FAIL |
| Исполнение завершилось ошибкой со статусом STOPPED | FAIL |
| Исполнение завершилось ошибкой без штатного ожидания (WAITING_READ_RETRY или WAITING_LEASE). Так выглядит падение kc4bg | FAIL |
| Три и более штатных ожидания за 2 ч | DEGRADED |
| 6 ч нет ни DISPATCHED, ни RECONCILED*, хотя чанки FULL ещё остались | DEGRADED |
| Образ наблюдателя не собран из живого релиза контроллера (`WATCH_IMAGE_STALE`) | DEGRADED |
| Дрейф env или образа контроллера относительно дескриптора | FAIL |
| Любое чтение не удалось (`READ_FAILED`) | DEGRADED |

Ни одна проверка не превращает сбой чтения в OK.

**Вывод в stdout.**
- `HEARTBEAT` пишется после каждой оценки.
- `HEALTH` пишется, когда меняется отпечаток вердикта (уровень, коды и предметы).
- Если состояние не OK, `HEALTH` повторяется напоминанием раз в 12 ч.
- Прошлое состояние наблюдатель читает из своего же лога, только на чтение.
- Если наблюдатель не может работать (чужая личность или нет metadata), он пишет `UNAVAILABLE` и завершается
  с exit 3, без пульса. В этом случае срабатывает сторож.

**Граница чтения.** Запросы проходят только через читатель `CloudAccess` из `cloud_access`, с полномочием reader.
Ему доступны:
- GET четырёх датасетов;
- метаданные Run и Scheduler;
- один ограниченный SELECT.

Дополнительно разрешён один `entries:list` Logging с фильтром `resource.type="cloud_run_job"`.

Полномочия append нет:
- поставщик токена отказывает в нём;
- в личности metadata должна быть `sa-heldi-watch`, а не SA контроллера;
- `dispatch` и запись — заглушки с отказом.

**Образ.** Его собирает `FROM` точного digest живого контроллера (`tools/heldi_watch/Dockerfile`). Поэтому
принятие STOP, CURRENT и проверка релиза работают тем же кодом, что и сам контроллер. Образ нужно пересобирать
на каждом релизе контроллера. Без пересборки наблюдатель не судит, а сообщает DEGRADED `WATCH_IMAGE_STALE`.

**Права** (все роли пользовательские, `roles/bigquery.jobUser` не используется):

| SA | Роль | Права |
|---|---|---|
| `sa-heldi-watch` | `heldiWatchRead` (проект) | `bigquery.jobs.create`, `cloudscheduler.jobs.list`, `logging.logEntries.list`, `run.executions.get`, `run.executions.list`, `run.jobs.get` |
| `sa-heldi-watch` | `heldiWatchData` (проект, условие IAM) | `bigquery.datasets.get`, `bigquery.tables.get`, `bigquery.tables.getData`, `bigquery.tables.list` — только ozon_raw, ref, tenant_ops, tenant_locks |
| `sa-heldi-watch-wake` | `heldiWatchWake` (только задание `heldi-health-eval`) | `run.jobs.run` |

ACL датасетов в `infra/tenant` авторитетны (T3.3). Если выдать грант на уровне датасета отсюда, следующий apply
арендатора его сотрёт. Поэтому доступ к данным выдан на уровне проекта, с условием IAM на эти четыре датасета.

## 4. Порядок включения (каждый шаг — по OWNER ACK)

1. **Образ.** Сборка в Cloud Build с `CONTROLLER_IMAGE` = digest живого контроллера. На 27.10 это
   `…/tenant-backfill-controller@sha256:f9c56b6a…`, источник `2253a311`. Публикуется в
   `mpa-platform/mpa-runtime/heldi-health-eval`, развёртывание только по digest.
2. **Первый apply** (Scheduler на паузе, H3 и сторож выключены):

   ```text
   terraform init -backend-config=bucket=mpa-platform-tfstate-777428383056 -backend-config=prefix=monitoring/tenants/client_001
   terraform plan -var tenant_id=client_001 -var project_id=mpa-t-client-001 -var alert_email=<почта> \
     -var watch_image=<digest> -out=watch.plan
   ```

   Ожидается 18 to add, 0 to change, 0 to destroy (состав — в описании PR; read-only plan 27.10 против
   живого state с `-lock=false` и digest-заглушкой показал ровно это), затем `terraform apply watch.plan`.
3. **Доверенный прогон.** `gcloud run jobs execute heldi-health-eval --region europe-west1 --project mpa-t-client-001 --wait`.
   - В логе задания должны быть `HEALTH` и `HEARTBEAT`.
   - Исполнение должно идти под `sa-heldi-watch`.
   - В журнале аудита не должно быть ни одной записи этой SA.
4. **Синтетический FAIL** — путь «задание → лог → политика → письмо» без STOP в production:

   ```text
   gcloud run jobs execute heldi-health-eval --region europe-west1 --project mpa-t-client-001 --wait \
     --args=--synthetic-fail,syn-<YYYYMMDD-HHMM>
   ```

   Владелец подтверждает письмо «Heldi FAIL: SYNTHETIC FAIL syn-…». Следующий плановый прогон пришлёт
   «смена состояния» обратно.
5. **Синтетический STOP на копии.** Оценка `evaluate()` с транспортом копии `overlay2.Copy` (как в репетиции
   пакета S4.1a), в которую в памяти добавлена запись STOPPED. Ожидается FAIL `UNRESOLVED_STOP`. Офлайн это же
   проверяет тест `test_synthetic_unresolved_stop_is_fail`, а сквозной вывод — `test_synthetic_stop_end_to_end…`.
6. **Второй apply**, не раньше чем через 20 мин после шага 2:
   - `-var watch_active=true` — Scheduler `*/30` и сторож;
   - `-var silence_alerts_enabled=true` — H3.

   Ожидается 0 to add, 3 to change.
7. Сутки без ложных срабатываний. После этого локальный `observe_loop` выключается и остаётся только
   инструментом пакетов выпуска.

**Владелец сам, в любой момент:** `--owner-token-command "gcloud auth print-access-token"` запускает ту же оценку
локально. Граница чтения та же, без сравнения с прошлым состоянием.

**Время оценки.** Прогон владельцем 27.10 с ноутбука, только чтение, вердикт OK:

| Проверка | Время |
|---|---|
| Принятие STOP (476 записей корня, 316 STOP; повторная проверка всех сертификатов восстановления) | 507 с |
| CURRENT | 12 с |
| Остальные проверки | меньше 3 с |

Время уходит на сеть: CPU занят на 4 %, каждый GET открывает новое TLS-соединение (`cloud_access.wire`).
Таймаут задания — 1500 с. Фактическое время в Cloud Run фиксируется при доверенном прогоне в `facts.seconds`.

## 5. Откат

Plan и apply без `-var watch_image`: значение по умолчанию null, и слой 2 удаляется целиком.
`deletion_protection = false` у задания допускает удаление. Слой 1 (H2, H3)
снимается удалением ресурсов.

Контроллер, его Scheduler, датасеты и ACL этот корень не затрагивает.

## 6. Чего здесь нет

- **Telegram.** Исключение из F-18 не дано (как у DRO-1). Ночной канал — та же почта. Если нужен будящий канал
  (SMS или push мобильного приложения Cloud Console), это отдельное решение владельца.
- **Автоматическая пауза.** Её нет и не будет: аварийная пауза остаётся решением владельца или пакета под ACK.
