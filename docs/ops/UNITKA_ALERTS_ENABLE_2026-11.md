# Юнитка: минимальное включение оповещений к переходу месяца 01–02.11.2026 (только по OWNER ACK)

Статус: **НЕ применено.** Ветка `ops/unitka-alerts-enable-month-transition` поверх PR #373.

## Что включается — и только это

| Политика (Terraform-адрес) | Сейчас (live 28.10) | После | Зачем к 01.11 |
|---|---|---|---|
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"]` | есть, OFF | **ON** (update) | LCD WB не сдвинулся 25 ч — ноябрь не создан/не закрыт |
| `google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"]` | есть, OFF | **ON** (update) | то же для Ozon |
| `google_monitoring_alert_policy.unitka_writers_failed` | нет (apply 21.10: метрика создавалась в ту же секунду) | **создаётся ON** | отказ создания месяца 2-го числа и любой отказ писателя |

Не меняются: heartbeat P&L WB/Ozon (OFF), `unitka_publication_blocked` и `unitka_owner_attention` (в файле OFF, не создаются),
`unitka_writer_config` (исправление имени из #373 не применяется этим шагом), пять старых политик (остаются ON — дубль
сигнала «упал» допустим до подтверждения новых), лог-метрики (все 6 уже существуют, no-op).

Предпосылки проверены live 28.10: метрики `unitka_wb_lcd_committed`, `unitka_ozon_lcd_committed`, `unitka_writer_failures`
существуют; события `LCD_COMMITTED` WB и Ozon — ежедневно 21–28.10 (окно 25 ч заполнено, ложного срабатывания при
включении не будет); канал — email владельца (единственный).

## Порядок (каждый шаг — отдельный ACK)

1. Слияние #373, затем этой ветки в `main`.
   ⚠️ Побочный эффект слияния: оба PR меняют `cloud/test/**` → push в main запускает `deploy-shadow` (пересборка
   shadow-образа wb-loader и обновление shadow-заданий). Production не затрагивается; shadow остатков (`wb-stocks-shadow`)
   продолжит работу на коде main. Если это нежелательно до восстановления 28.10 — сначала выполнить восстановление остатков.
2. `infra.yml` → `action=plan`,
   `targets=google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"],google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"],google_monitoring_alert_policy.unitka_writers_failed`,
   `expected_changes=google_monitoring_alert_policy.unitka_heartbeat["unitka_wb_lcd_committed"]=update,google_monitoring_alert_policy.unitka_heartbeat["unitka_ozon_lcd_committed"]=update,google_monitoring_alert_policy.unitka_writers_failed=create`.
   План принимается, только если `tf_plan_guard` подтвердил ровно эти 3 изменения (у двух update — только `enabled`).
3. `infra.yml` → `action=apply` с теми же `targets` и `expected_changes` (применяется сохранённый план).
4. Приёмка (readback): `gcloud alpha monitoring policies list --format=json` — три политики `enabled: true`,
   у `unitka_writers_failed` один канал; остальные 12 политик без изменений (сверить с выгрузкой до шага 3).
5. Доставка: в консоли Monitoring → Notification channels → «UNITKA Engine — владелец» → *Send test notification*;
   владелец подтверждает получение письма. Без этого доставка считается непроверенной.
6. 02.11 после 10:10 МСК — живая проверка перехода месяца по чек-листу `WAVE_2_INTERIM.md` §6; при отказе создания
   ожидается письмо `unitka_writers_failed`, при отсутствии LCD — heartbeat (~08:02Z).

## Откат

Revert этой ветки (enabled → false у двух heartbeat; `unitka_writers_failed` — `enabled = false`), затем targeted
plan/apply по тем же адресам с `expected_changes` `…=update` ×3. Аварийно (break-glass, с последующим выравниванием
Terraform): `gcloud alpha monitoring policies update <id> --no-enabled`.
