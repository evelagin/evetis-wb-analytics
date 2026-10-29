# Юнитка: минимальное включение оповещений к переходу месяца 01–02.11.2026 (только по OWNER ACK)

Статус: **НЕ применено.** Ветка `ops/unitka-alerts-enable-month-transition` поверх PR #373.

## Что включается — и только это

| Политика (Terraform-адрес) | Сейчас (live 28.10) | После | Зачем к 01.11 |
|---|---|---|---|
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"]` | есть, OFF | **ON** (update) | LCD WB не сдвинулся 25 ч — ноябрь не создан/не закрыт |
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"]` | есть, OFF | **ON** (update) | то же для Ozon |
| `google_monitoring_alert_policy.unitka_writers_failed[0]` | нет (apply 21.10: метрика создавалась в ту же секунду) | **создаётся ON** | отказ создания месяца 2-го числа и любой отказ писателя |

Не меняются: heartbeat P&L WB/Ozon (OFF), `unitka_publication_blocked` и `unitka_owner_attention` (в файле OFF, не создаются),
`unitka_writer_config` (исправление имени из #373 не применяется этим шагом), пять старых политик (остаются ON — дубль
сигнала «упал» допустим до подтверждения новых), лог-метрики (все 6 уже существуют, no-op).

Предпосылки проверены live 28.10: метрики `unitka_wb_lcd_committed`, `unitka_ozon_lcd_committed`, `unitka_writer_failures`
существуют; события `LCD_COMMITTED` WB и Ozon — ежедневно 21–28.10 (окно 25 ч заполнено, ложного срабатывания при
включении не будет); канал — email владельца (единственный).

## Проверенный план (локально, 28.10.2026, `terraform plan -lock=false`, state `evetis/wb-cloud`, только чтение)

Targeted plan этой ветки (41f27667) по трём адресам — `Plan: 1 to add, 2 to change, 0 to destroy`:

| Адрес | Действие | Изменение |
|---|---|---|
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"]` | update | только `enabled: false → true` |
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"]` | update | только `enabled: false → true` |
| `google_monitoring_alert_policy.unitka_writers_failed[0]` | create | ON, 1 канал (email владельца); PromQL `sum by (job_name)(increase(user/unitka_writer_failures[24h30m])) > 0` |

`tools/tf_plan_guard.py` с `expected_changes` ниже — `OK — 3 изменений ровно как ожидалось`.
⚠️ У `unitka_writers_failed` есть `count`: адрес в `expected_changes` — **`[0]`**; без индекса guard отказывает в apply
(проверено на этом же плане).

Полный (не targeted) plan той же ветки — 24 изменения, из них 21 посторонний дрейф, который этот шаг **не трогает**:
`retry_count None→0` у 16 Scheduler, метки `client` у `wb_mart_prod`/`wb_stocks_prod`, создание трёх политик в OFF и
расписание `wb_comms_poll` (live `0 8-23 * * *`, в Git `0 8,11,14,17,20 * * *` — расхождение production и Git, отдельная задача).

Ложного срабатывания при включении нет (Monitoring API, 50 ч): `LCD_COMMITTED` WB — 27.10 10:13Z и 28.10 07:02Z,
Ozon — 27.10 07:02Z и 28.10 07:02Z; `unitka_writer_failures` — одна точка 27.10 07:01Z (INVARIANT_FAIL #384), из окна
24h30m вышла 28.10 07:31Z.

## Порядок (каждый шаг — отдельный ACK)

1. **Слияние.** #373 → `main` обычным merge commit (не squash), затем #389: GitHub перенацелит его на `main` после
   удаления ветки #373 (иначе сменить base вручную), дождаться CI 11/11 на новой базе, merge commit.
   Побочный эффект каждого слияния: `cloud/test/**` → `deploy-shadow` пересобирает образ из main и обновляет
   `wb-stocks-shadow` и `unitka-engine-shadow` (+ env `IMAGE_DIGEST`, `GIT_SHA`, `UNITKA_INTEGRITY_MODE=observe`).
   Код образа не меняется: shadow сейчас на `d2d4972c` (сборка c40c7e45, #384), между c40c7e45 и main изменений в
   `cloud/` нет, а `cloud/test` в образ не входит (`.dockerignore`). Production-задания не трогаются.
   Восстановление остатков 28.10 читает `__CR`-таблицы уже записанного снимка и от пересборки shadow не зависит.
2. `infra.yml` (ref `main`) → `action=plan`,
   `targets=google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"],google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"],google_monitoring_alert_policy.unitka_writers_failed`,
   `expected_changes=google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"]=update,google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"]=update,google_monitoring_alert_policy.unitka_writers_failed[0]=create`.
   Принимается, только если guard OK и у двух update меняется только `enabled` (сверить с таблицей выше).
3. `infra.yml` → `action=apply` с теми же `targets` и `expected_changes` (применяется сохранённый план).
   Срок: до 31.10 включительно.
4. **Приёмка конфигурации** (readback Monitoring API): «UNITKA WB: LCD не сдвигался 25 ч», «UNITKA Ozon: LCD не
   сдвигался 25 ч», «ЮНИТКА/P&L: сбой прогона или источника» — `enabled: true`, канал `6592765745436218275`
   («UNITKA Engine — владелец», email); остальные политики проекта без изменений (выгрузка до/после).
   Открытых инцидентов по трём политикам после apply нет.
5. **Приёмка доставки.** Тот же канал уже доставляет пять включённых политик Юнитки. Владелец подтверждает, что письма
   Cloud Monitoring с этого канала приходят и не попадают в спам (например, по сбою unitka-engine-prod 27.10 07:01Z).
   Если подтверждения нет — синтетическая проверка отдельным ACK, не позже 30.10: одна запись `gcloud logging write`
   под фильтр `unitka_writer_failures` → письмо «ЮНИТКА/P&L: сбой…» в течение ~5 мин. Инцидент держится 24 ч 30 мин
   и закроется до 01.11. Без одного из этих подтверждений доставка считается непроверенной.
6. 02.11 после 10:10 МСК — живая проверка перехода месяца по чек-листу `WAVE_2_INTERIM.md` §6; при отказе создания
   ожидается письмо `unitka_writers_failed`, при отсутствии LCD — heartbeat (~08:02Z).

## Откат

Revert этой ветки (enabled → false у двух heartbeat; `unitka_writers_failed` — `enabled = false`), затем targeted
plan/apply по тем же адресам с `expected_changes` `…=update` ×3 (у `unitka_writers_failed[0]` — тоже update: enabled → false). Аварийно (break-glass, с последующим выравниванием
Terraform): `gcloud alpha monitoring policies update <id> --no-enabled`.
