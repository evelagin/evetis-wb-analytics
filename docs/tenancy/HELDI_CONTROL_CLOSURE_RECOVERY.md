# Heldi — восстановление после STOP `eca0335a` (CONTROL_CLOSURE/CLOUD_METADATA, 2026-11-05)

Минимальный выпуск контроллера `tenant-backfill-controller` (client_001). Единственная цель —
дать владельцу точный тип восстановления для STOP `eca0335a6ade182e…`, не трогая логику пробуждения.
Догон финансов, #392 (QUIESCENT_PAUSED), рекламный релиз B, FBO, RECENT и FULL_HISTORY в выпуск не входят.

## Что произошло

- 05.11, 21:54:57Z — аренда `L_…0918` выпущена (`LD_…0918`) владельцем `bf-d6e173f1`;
  21:55:24Z — `CURRENT_CERT` stocks за день 06.11 (маркер `BFR_3c6bebde…`).
- 21:57:02Z — исполнение `tenant-backfill-controller-9sbgd`, claim 2404 в 21:58:40Z.
- 22:01:39Z — `close_decision` не смог закрыть claim: `cloud_controller.main` типизирует
  `TransientReadError` закрытия (а гейт `CONTROL_CLOSURE` — любую другую `TableError` метаданных) как
  `GateFailure('CONTROL_CLOSURE','CLOUD_METADATA')`. Записан STOP с этим диагнозом, затем claim закрыт
  `STOPPED/0`. Код ответа не логируется по дизайну, поэтому причина в сертификате — сам закрытый диагноз
  (`TYPED_CONTROL_CLOSURE_CLOUD_METADATA`), без домысла.
- После сертификата stocks нет ни аренды, ни ограды, ни записи журнала, ни строки `BACKFILL_CHECKPOINTS`,
  ни исполнения runtime, ни строки `OZON_INGESTION_RUNS`, кроме самого STOP.

Ни TG V1 (только нетипизированный STOP после страницы FULL), ни CR v1–v4 (зависимые STOP
`MANIFEST_ROOT/EVIDENCE` при незакрытом claim) этот STOP не принимают.

## Что меняется

1. **Новый тип восстановления** `CONTROL_CLOSURE_METADATA_AFTER_CURRENT_CERT_V1`
   (`tools/tenancy/control_closure_recovery.py`, KIND `CONTROL_CLOSURE_STOP_RECOVERED`, маркеры
   `BFCLX_AUTH_<root>_<stop>` в `ref` и `BFCLX_<hash>` в `tenant_locks`). Принимает только STOP с точной
   полезной нагрузкой `{reason, controller_execution, diagnostic: CONTROL_CLOSURE/CLOUD_METADATA}` и только
   пока доказано всё сразу:
   - исполнение контроллера терминально FAILED (одна задача, без повторов) и соответствует своему
     зарегистрированному релизу и корню в режиме ENABLED;
   - серия claims непрерывна и закрыта; claim терминального пробуждения закрыт `STOPPED/0` **после**
     маркера STOP (раньше закрытие не приземлялось); все последующие claims — 0 отправок;
   - последняя выпущенная аренда (`LD_`, без срока жизни) принадлежит run единственного CURRENT-шарда
     дня; у шарда ровно intent/receipt/RECONCILED (`checkpoint=DONE`, `source_complete`), дальше ничего;
     `CURRENT_CERT` сущности связан с receipt и reconciliation по хэшам, факт проходит `check_fact`,
     состояние источника совпадает с `state_hash` сертификата;
   - порядок: выпуск аренды ≤ маркер сертификата < старт терминального пробуждения;
   - после маркера сертификата в `tenant_locks` созданы только claims корня, маркер этого STOP и маркеры
     самой квалифицированной замены; в `BACKFILL_CHECKPOINTS` после него ровно одна строка — STOP;
     пилотных строк с арендой выше последней нет; исполнений runtime и строк журнала после него нет;
   - активных исполнений нет, 4/4 Scheduler PAUSED; неразрешённый STOP ровно один (единый
     `stop_acceptance`), все прежние сертификаты перепроверяются; политика RECENT закреплена хэшем;
   - замена квалифицирована: `control_closure_recovery_version = 1` и адаптер PASS.
   `source_budget = 0`. Ничего не выдумывается: ни аренды, ни receipt, ни claim, ни закрытия, ни плана CURRENT.
   Таблицы аренд `L_` истекают по дизайну (`L_…0918` — 12.11 23:43Z), поэтому тип опирается на `LD_` и
   журнал чекпойнтов; ещё живая `L_` должна совпадать.
2. **Регистрация** в `durable_plan.KINDS`, `stop_acceptance._revalidate`, `orchestration_contract.SOURCE_FILES`,
   поле релиза `control_closure_recovery_version` (требует `finance_catchup_version`), адаптер
   `control_closure_recovery_adapter` в `controller_image_check`, префикс `BFCLX_` в проверке сроков жизни
   маркеров `full_controller`.
3. **Регистрация развёрнутого релиза** `e82ba56` (`infra/tenant/releases/backfill/`), байт-в-байт равная
   `CANDIDATE_RELEASE.json` выпуска C (sha256 `a0978a27…`, закреплён в OWNER ACK C-R).

TG V1, CR v1–v4, догон финансов и независимость CURRENT (S4) не меняются; образ сохраняет CURRENT,
RECENT_180, FULL_HISTORY и все существующие сертификаты.

## Что не входит: повторяемость

Тот же класс сбоя (429/5xx или обрыв на GET метаданных) бывает 0–4 раза в сутки, но до STOP дошёл
впервые. Превращать его на этапе `CONTROL_CLOSURE` в `WAITING_READ_RETRY` без закрытия нельзя: claim
остаётся открытым, и следующее пробуждение даёт STOP `MANIFEST_ROOT/EVIDENCE`
(`UnclosedTerminalHolder`). Безопасный вариант — повтор закрытия внутри пробуждения с тем же
запечатанным значением (иначе повтор с новым `closed_at` даёт конфликт) — меняет `CC.close` в горячем
пути каждого пробуждения. Он вынесен в отдельную задачу.

## Проверка

* `tools/tests/test_control_closure_recovery.py` — сертификат, 40 негативных сценариев (каждый без
  эффектов), неизвестные предикаты, изменённый сертификат, загрузка только с обоими маркерами, контракт
  релиза, регистрация e82ba56.
* `python -m tools.tenancy.control_closure_image_check` — тот же набор внутри образа без сети;
  агрегируется в `controller_image_check`.

## Порядок в production (по отдельному OWNER ACK)

Как C-R/S4.1a: freeze (пауза tick в безопасном окне) → preflight → 8 PAUSED-маркеров → validate → deploy
PAUSED → readback → recovery (observe ≤ 15 мин, publish ровно 4 записи) → recovery-readback → bootstrap
(`QUIESCENT_PAUSED`, 0 отправок) → bootstrap-readback → enabled-marker → enable-validate → enable →
enabled-readback → resume (только `tenant-backfill-tick`) → observe. Откат на gen 62 (`e82ba56`) — только до
первой записи стадии recovery.
