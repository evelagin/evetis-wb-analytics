# Heldi S‴-a — восстановление после STOP `8ad33cc6` и типизация пути записи (2026-10-22)

Аварийный минимальный выпуск контроллера `tenant-backfill-controller` (client_001).
Цель — снять единственный STOP `8ad33cc6…` и вернуть непрерывную загрузку, сохранив
действующий FULL_HISTORY и активный RECENT_180. Финансы CURRENT, рекламный runtime,
FBO V2 и T6 в выпуск не входят.

## Что произошло

Пробуждение `tenant-backfill-controller-qhv6d` (07:03–07:16Z) сверило страницу 103
шарда supplies (лист 465, ограда `RECONCILED` 07:10:06Z, аренда `0345` выпущена
владельцем страницы 07:09:48Z) и завершилось записью `STOPPED` **без закрытого
диагноза** в 07:16:45Z. Полномочий на страницу 104 не возникло: нет аренды `0346`,
нет чекпойнта, нет intent, нет Run POST, нет строк журнала. Claim 1414 закрыт
`STOPPED` с нулём отправок; следующие claims ничего не отправляли. Исключение в логах
отсутствует по дизайну (никаких дампов), поэтому причина остаётся `UNPROVEN`.

Единственные пути без типизированного диагноза в источнике `39bc343`: текст
`pilot lease held` под cadence, `full dispatch receipt unproven` после `start`, и
любое исключение в `close_decision` после ограниченного результата (например
`TransientReadError` при закрытии после `WAITING_READ_RETRY`). Соседние пробуждения
видели `TRANSIENT_READ_TRANSPORT` в 07:16:37Z и 07:21:47Z.

## Что меняется

1. **Новый тип восстановления** `FULL_TERMINAL_GATE_AFTER_RECONCILED_PAGE_V1`
   (`tools/tenancy/full_terminal_gate_recovery.py`, KIND
   `FULL_TERMINAL_GATE_STOP_RECOVERED`, маркеры `BFTG_AUTH_<root>_<stop>` в `ref` и
   `BFTG_<hash>` в `tenant_locks`). Принимает только точный нетипизированный STOP и
   только пока: исполнение контроллера терминально FAILED; его claim закрыт `STOPPED/0`;
   вся серия claims закрыта, после него — 0 отправок; последняя запись шарда — persisted
   `RECONCILED` с оградой раньше STOP; аренда страницы выпущена владельцем страницы до
   STOP; нет записей, оград и аренд дальше страницы; нет runtime-исполнений и строк
   журнала после STOP (во время пробуждения — только run_id страницы); состояние
   страницы не изменилось (`state_hash`); 4/4 Scheduler PAUSED; все прочие STOP покрыты
   действующими сертификатами и карантинами (единый `stop_acceptance`); политика
   RECENT закреплена хэшем и не меняется между наблюдением и публикацией; замена
   квалифицирована (`terminal_gate_recovery_version = 1` + адаптер PASS).
   `historical_cause = UNPROVEN_WRITE_PATH_GATE`, `source_budget = 0`.
2. **Регистрация** в `durable_plan.KINDS`, `stop_acceptance._revalidate`,
   `orchestration_contract.SOURCE_FILES`, поле релиза `terminal_gate_recovery_version`
   (требует `controller_wake_budget_version`) и адаптер `terminal_gate_recovery_adapter`
   в `controller_image_check`.
3. **Диагностика пути записи.** Закрытый журнал записей одной отправки
   (`lease/checkpoint/intent/run/receipt` ∈ `ABSENT/UNKNOWN/PRESENT`) едет внутри
   типизированного STOP (`diagnostic.readback`). После первой отправленной записи любой
   сбой — типизированный STOP, никогда не ожидание и никогда не повторный POST.
   До первой записи `TransientReadError` по-прежнему даёт ограниченный повтор чтения.
   `pilot lease held` в claimed-пробуждении — STOP `LEASE/EVIDENCE`; сбой чтения в
   `close_decision` — STOP `CONTROL_CLOSURE/CLOUD_METADATA`; `receipt unproven` —
   STOP `SOURCE_DISPATCH_BOUNDARY/EVIDENCE`. Новый этап `CONTROL_CLOSURE` в `STAGES`.
4. **Регистрация развёрнутого релиза** `39bc343` (`infra/tenant/releases/backfill/`),
   байт-в-байт равная живому дескриптору `BF_SPEC_39bc343…_ENABLED`.

## Проверка

* `tools/tests/test_full_terminal_gate_recovery.py` — сертификат, 19 негативных
  сценариев полномочий/эффектов за страницей, частичные записи, потерянные ответы,
  отсутствие повторной отправки, сохранение RECENT, типизация `main`.
* `python -m tools.tenancy.full_terminal_gate_image_check` — тот же набор внутри
  образа без сети; агрегируется в `controller_image_check`.
* Фикстура воспроизводит боевую страницу: шард листа 465 =
  `0cbd2884…`, `ack_hash` = `18721b38…`.

## Порядок в production (по отдельному OWNER ACK)

Preflight → сертификат (observe, read-only) → readback → деплой PAUSED-дескриптора →
`publish` (2 маркера + 1 запись корня) → ENABLED → resume только `tenant-backfill-tick`.
Успех — реальная отправка и сверка страницы 104 без нового STOP и сущности текущего дня.
