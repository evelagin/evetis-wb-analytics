# Оповещения арендатора client_001 (Heldi)

Корень Terraform: `infra/tenant-monitoring/` (отдельно от `infra/tenant`, см. `versions.tf`).
Проект: `mpa-t-client-001`. Канал: почта владельца (адрес передаётся при apply, в Git его нет).
Автоматического восстановления и автоматической паузы нет.
Облачное наблюдение (H2 падение, H3 тишина, наблюдатель `heldi-health-eval`) — в том же корне, `watch.tf`;
регламент — `docs/ops/HELDI_CLOUD_WATCH.md`.

## Сигналы

| Политика | Условие | Письмо |
|---|---|---|
| Контроллер в STOP | stdout контроллера `status=STOPPED` за 3 ч (с 09.11; было 10 мин) | только на открытие |
| Scheduler не запускает контроллер | ≥ 2 отказа `tenant-backfill-tick` за 15 мин | открытие и закрытие |
| Нет успешных завершений контроллера | 0 успешных завершений за 60 мин (с 09.11; было 30): `(sum(increase(...{result="succeeded"}[60m])) or vector(0)) < 1` | открытие и закрытие |
| Данные сущности не обновлены 27 ч | 0 сертификатов суток (`RECONCILED_BOUNDARY`, `current_complete=true`) по сущности за 24 ч, удержание 3 ч | открытие и закрытие, по инциденту на группу: catalog, stocks, prices, finance, orders (`fbo_postings`), ads (3 сущности) |
| Проверка доставки | запись владельца в журнал `tenant-alert-delivery-test` | только на открытие |

- Одиночный штатный WAIT (`WAITING_READ_RETRY`, `WAIT_ACTIVE_CONTROLLER`) и одиночный 429 инцидента не открывают:
  соседние тики завершаются успешно, а 429 виден только как отсутствие сертификата суток.
- Условия считают события (`increase`), а не наличие точек: Cloud Monitoring пишет нулевые точки рядом с событием,
  и `absent_over_time` принял бы ряд из одних нулей за здоровье (проверено 20.10 на `completed_execution_count`).
  Отсутствие ряда закрывает `or vector(0)`.
- PromQL-инцидент закрывается сам через ~5 мин после того, как условие перестало выполняться, и следующее
  срабатывание открывает **новый** инцидент с новым письмом. Окно условия поэтому должно перекрывать самый длинный
  штатный разрыв сигнала. Замер 09.11 (только чтение, API `projects.alerts` + журнал `ViolationOpenEventv1` + почта):
  за STOP `eca0335a` (05.11 22:01Z → 09.11) политика STOP с окном 10 мин открыла **58 инцидентов и 58 писем** —
  под STOP строки `STOPPED` идут не на каждом тике (между ними `WAIT_ACTIVE_CONTROLLER`), разрывы до 133 мин.
  Окно 3 ч даёт один инцидент. «Нет завершений» с окном 30 мин открылась 25 раз за 30.10–05.11 (разрывы между
  успешными завершениями до 49 мин) — с окном 60 мин ни разу.
- Письма приходили: первое о STOP `eca0335a` — 05.11 22:04:49Z (через 3 мин после первой строки STOPPED), но с темой
  «[ALERT - Critical] Тики завершаются STOPPED on __missing__» во вкладку Gmail «Оповещения», непрочитанным.
  С 09.11 у каждой политики своя тема, начинающаяся с «Heldi».
- Сертификат суток приходит раз в сутки (00:20–01:45 МСК), разрывы 23,6–24,5 ч: окно 24 ч с удержанием 30 мин
  открывало бы ложный инцидент почти каждую ночь. Удержание 3 ч: тревога через 27 ч после последнего сертификата,
  то есть пропущенная загрузка суток видна к ~04:00 МСК следующего дня.
- Окно свежести 24 ч — это `snapshot_sla_seconds = fbo_sla_seconds = 86400` политики суточного контура.
  Пользовательские лог-метрики PromQL видит не дальше 25 ч, поэтому запас дан удержанием (`duration`), а не окном.
- Плановая или аварийная пауза `tenant-backfill-tick` откроет «Нет успешных завершений». Это ожидаемо;
  инцидент закроется после возобновления.
- STOP-инцидент закрывается и при паузе тика, хотя STOP при этом не разрешён. Поэтому письма о его
  закрытии нет. STOP снимается только новым OWNER ACK.

## Локальные состояния A2 (PR #404, `durable.tf`)

После A2 временные сбои не дают глобальный STOP, а становятся локальными состояниями. Лог-метрики создаются
сразу (до выпуска #404 считают ноль), политики включаются `-var durable_daily_alerts=true` только после выпуска
образа #404 (0 add / 7 change: шесть политик + фильтр метрики STOP).

| Политика | Условие (поля #404) | Письмо |
|---|---|---|
| Сущность CURRENT держится UNKNOWN | `current_entities.<e> = UNKNOWN` ≥ 5 раз за 90 мин и ни одного COMPLETE/PARTIAL | открытие и закрытие, на сущность |
| Сущность CURRENT частична | `current_entities.<e> = PARTIAL` за 3 ч (FBO SOURCE_DISAPPEARED приходит так) | открытие, на сущность |
| Чтение повторяется, нет решений | ≥ 5 `WAITING_READ_RETRY` за 90 мин и ни одного другого решения, кроме ожиданий | открытие и закрытие; почта + push |
| Локальный STOP листа FULL | `STOPPED` со `diagnostic.stage = PERSISTED_SOURCE_RECONCILIATION` и контекстом листа | открытие, на лист |
| FULL закрыт: больше 3 локальных STOP | `basis = FULL_HISTORY_LOCAL_STOP` | открытие; почта + push |
| SOURCE_DISAPPEARED | значение `SOURCE_DISAPPEARED` в строке контроллера (контракт потока B) | открытие |

- Решений контроллера в среднем 5,2 в час (медианный разрыв 12 мин, p95 18 мин; перекрытые пробуждения пишут
  `WAIT_ACTIVE_CONTROLLER`), поэтому «N пробуждений подряд» = N раз за 90 мин. Бэктест 26.10–05.11 (4675 строк):
  подряд не больше 3 `WAITING_READ_RETRY`, при пороге 5 за 90 мин — 0 срабатываний.
- С включением A2 метрика STOP перестаёт считать локальный STOP листа (`NOT jsonPayload.diagnostic.context.index:*`):
  письмо «контроллер в STOP» означает только глобальный STOP.



1. `terraform init -backend-config=bucket=mpa-platform-tfstate-777428383056 -backend-config=prefix=monitoring/tenants/client_001`
2. `terraform plan -var tenant_id=client_001 -var project_id=mpa-t-client-001 -var alert_email=<почта владельца> -out=alerts.plan`
   Ожидается: 11 to add (guard, канал, 4 лог-метрики, 5 политик), 0 to change, 0 to destroy.
3. `terraform apply alerts.plan`
4. Проверка доставки:
   `gcloud logging write tenant-alert-delivery-test '{"test_id":"<YYYYMMDD-HHMM>"}' --payload-type=json --project=mpa-t-client-001`
   Доставка доказана только подтверждением владельца: пришло письмо с этим `test_id`.
   Само создание правила доставку не доказывает.
5. Не раньше чем через 24 ч после шага 3 чтением убедиться, что у метрики `tenant_current_certified`
   есть точки для catalog, stocks, prices, finance_accrual. Затем тот же plan/apply с `-var freshness_enabled=true`
   (ожидается 0 to add, 1 to change).
   При включении сразу откроются инциденты по сущностям, которые сейчас действительно не обновляются:
   ads_campaigns, ads_expense_daily, ads_sku_daily, fbo_postings.

## Push в приложении Google Cloud (решение владельца 27.10)

STOP и проверка доставки уходят на почту и push приложения Google Cloud. Остальные политики шлют только почту.

Канал push нельзя создать через API или Terraform. Его создаёт само приложение: после установки и выбора проекта
через несколько минут канал появляется в разделе «Mobile Devices» страницы Notification channels. Источник:
docs.cloud.google.com/monitoring/support/notification-options, раздел «Google Cloud console Mobile App». Встроенного
теста канала в Cloud Monitoring нет — доставку проверяет политика «проверка доставки».

1. Владелец на телефоне: установить «Google Cloud» из App Store или Google Play, войти той учётной записью, под которой
   работает с mpa-t-client-001, выбрать проект mpa-t-client-001 и разрешить уведомления приложению.
2. Через несколько минут прочитать имя канала (только чтение):
   `curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" https://monitoring.googleapis.com/v3/projects/mpa-t-client-001/notificationChannels`
   Нужна новая запись не типа `email`, с именем устройства или пользователя. Её `name` — `projects/mpa-t-client-001/notificationChannels/<id>`.
3. Применить план с этим каналом:

   ```text
   terraform plan … -var owner_push_channel=<name> -out=push.plan
   terraform apply push.plan
   ```

   Ожидается 0 to add, 2 to change, 0 to destroy: `notification_channels` у политик STOP и проверки доставки.
4. Проверка доставки — шаг 4 «Порядка включения» выше: письмо и push с тем же `test_id`. Push доказан только подтверждением
   владельца.

Откат push: тот же plan/apply без `-var owner_push_channel` (2 to change обратно). Канал устройства удаляется
отдельно в консоли, если он больше не нужен.

Откат: `terraform destroy` этого корня. Загрузку данных, контроллер и расписания он не затрагивает.
