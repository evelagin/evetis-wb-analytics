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

## Независимое ревью (22.10, READY_WITH_NOTES)

Исправлено в выпуске: типизированный STOP пути записи без контекста RECONCILE (удержанная аренда,
потерянная запись) больше не попадает в автоматическое восстановление класса C — иначе следующее
пробуждение писало бы второй нетипизированный STOP (`full_leaf_recovery._recover_pending`);
после отправленной записи типизируется любое исключение; предикат журнала защищён от насыщения
`LIMIT 1000`; ограда страницы должна лежать внутри терминального пробуждения, оград дальше
страницы быть не должно; журнал чекпойнтов `BACKFILL_CHECKPOINTS` опрошен напрямую (не зависит
от срока жизни таблиц аренды: `L_0345` истекает ≈ 29.10 09:00Z, после этого `observe` откажет по
`lease generation beyond reconciled page`). Наблюдение на production: ~100 с после прогрева,
прогрев claims (2×1415 GET) — в пакете параллельный.

## Порядок в production (по отдельному OWNER ACK, исполнитель `package_s3a.py`)

Предусловия `observe`/`publish` нового типа: замена уже **развёрнута PAUSED** (PAUSED-дескриптор
`BF_SPEC_<new>_<root>_PAUSED`, `BFCURHANDOFF_<new>` и 6 ассоциаций установлены, Job переведён на новый
образ с `HISTORICAL_SCHEDULER_STATE=PAUSED`), релиз замены зарегистрирован в репо (`terminal_gate_recovery_version`
+ адаптер PASS), 4/4 Scheduler PAUSED, ни одного активного исполнения, исполнение `qhv6d` сохранено
в Cloud Run, рабочее дерево владельца = квалифицированный source. Поэтому сертификат не может быть
выписан до PAUSED-деплоя.

1. `merge` — слияние PR на квалифицированном head (образ собран из него).
2. `preflight` (чтение) — живой Job = gen 54/`39bc343`, маркеры S‴-a отсутствуют, замороженный срез
   корня и шарда листа 465 совпадает с `inputs/FROZEN_BOUNDARY.json`, `qhv6d` сохранён.
3. `markers` — 8 маркеров PAUSED-стадии (create-only).
4. `validate` → `deploy` — `validateOnly`, затем UpdateJob на образ S‴-a с PAUSED.
5. `readback` (чтение) — Job gen 55 на новом образе, маркеры точны, ENABLED-дескриптора нет.
6. `recovery` — одна стадия: `TG.observe` (read-only, ≤15 мин) → проверка свежести → `TG.publish`
   (повторное наблюдение, повторная проверка свежести перед первой записью, 4 HTTP-записи: маркер
   полномочия в `ref`, сертификат в `tenant_locks`, строка `BACKFILL_CHECKPOINTS`, маркер `BFR_`).
   Closure claim 1414 не переписывается.
7. `recovery-readback` (чтение) — сертификат принят единым `stop_acceptance`, нерешённых STOP нет,
   claim 1414 по-прежнему `STOPPED/0`.
8. `bootstrap` → `bootstrap-readback` — одно исполнение PAUSED-контроллера, ожидается
   `QUIESCENT_PAUSED` без отправок.
9. `enabled-marker` → `enable-validate` → `enable` → `enabled-readback` — ENABLED-дескриптор и Job gen 56.
10. `resume` — только `tenant-backfill-tick`; `ozon-daily/fast/weekly` остаются PAUSED.
11. `observe` — реальные циклы: успех = отправка и сверка страницы 104 без нового STOP и сущности
    текущего дня; любое нарушение журналируется как `PAUSE_CONDITION` → `emergency-pause`.
