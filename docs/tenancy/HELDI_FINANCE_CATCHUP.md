# Heldi — адресный догон пропущенных дней финансов (2026-10-25)

Поток C. Цель: дозагрузить `finance_accrual` за дни, у которых нет ни одного источника загрузки,
не меняя принятую историю, замороженный манифест FULL и проверки CURRENT.

## 1. Аудит покрытия (только чтение, 2026-10-25)

Источники: `tenant_ops.BACKFILL_CHECKPOINTS` (записи `orchestration`: `CHUNK_COMPLETE`,
`CURRENT_PLAN`/`CURRENT_CERT`), `tenant_ops.DATA_COVERAGE`/`V_COVERAGE_DAILY`,
`ozon_raw.RAW_OZON_FINANCE_ACCRUAL`, журнал `ozon_raw.OZON_INGESTION_RUNS`.

| Дни начислений | Источник | Статус |
|---|---|---|
| 09.04–07.10 | FULL, листы 436–461 (26/26 `CHUNK_COMPLETE`), срез 07.10 | COMPLETE, 182 дня |
| **08.10–12.10** | между срезом FULL и первым циклом CURRENT (14.10 → день 13.10) | **нет данных, нет покрытия** |
| 13–16.10 | циклы CURRENT 14–17.10 (`CURRENT_CERT finance_accrual`) | COMPLETE |
| **17.10** | цикл 18.10 не создан | **нет** |
| 18.10 | цикл 19.10 | COMPLETE |
| **19.10** | цикл 20.10 не создан | **нет** |
| 20–21.10 | циклы 21–22.10 | COMPLETE |
| **22.10, 23.10** | циклы 23–24.10 не созданы | **нет** |
| 24.10 | цикл 25.10 | COMPLETE |
| **25.10** | цикл 26.10 не выполнен: с 20:10Z 25.10 по 26.10 20:49Z контроллер стоял (STOP aaf68ede, транспортный IncompleteRead; исправлено S4.1a) | **нет** |
| 26.10 | цикл 27.10 (после S4.1a, 4672 строки, PROVISIONAL) | COMPLETE |

Итого без покрытия десять закрытых дней (перепроверено 27.10): **08, 09, 10, 11, 12, 17, 19, 22, 23,
25 октября** (перечень владельца 08–12, 17, 19.10 плюс 22, 23 и 25.10). 25.10 допустим в догон с 27.10 МСК
по правилу «день ≤ дата авторитета − 2». По каждому: ноль строк RAW,
ноль строк `DATA_COVERAGE`, ни одного плана FULL/CURRENT, ни одного запуска в журнале — частичных
данных нет. `CURRENT_PARTIAL` нет ни одного.

Влияние на аналитику: `V_FINANCE_WINDOW_STATUS` считает пропуски во всём хвосте, поэтому все окна
от 09.04 до 21.10 сейчас `PARTIAL / TAIL_INCOMPLETE` (missing_days 9…2), а
`analytics_share.profitability_daily` отдаёт `contribution_*` = NULL на всех 3 579 строках.
После догона пропуски исчезнут, но статус станет `UNKNOWN / MATURITY_NOT_CONFIGURED`:
в `ref.REF_TENANT_ECONOMICS` нет `FINANCE_SETTLEMENT_MATURITY_DAYS` — это отдельное решение владельца.

Снимки (восстановить нельзя, только фиксируем) с первого снимка 01.10:

| Сущность | Нет снимка |
|---|---|
| stocks | 02–08, 10–13, 18, 20, 23, 24.10 |
| prices | 02–07, 09–13, 18, 20, 23, 24.10 |
| catalog | 02–04, 18, 20, 23, 24.10 |

Попутно: в принятых днях есть типы начислений вне `DIM_OZON_ACCRUAL_TYPE` — 84 «Дополнительная
упаковка» (10 строк, 30.09–21.10, известное ограничение FULL) и 127 «Аналитика Plus» (1 строка, 01.10).

## 2. Механизм

Новый модуль `tools/tenancy/finance_catchup.py`. Это не лист FULL и не цикл CURRENT: у догона своя
область долговременных записей, CURRENT и FULL не меняются.

- **Авторитет владельца** — маркер `ref.BFFINCATCHUP_<policy16>_<NN>` типа
  `OWNER_FINANCE_CATCHUP_V1` (закрытый набор полей, `hash` = sha256 канонического JSON):
  `tenant`, `project`, `historical_root` (корень FULL), `policy_hash` (неизменная политика CURRENT,
  поэтому маркер переживает смену контроллера через `BFCURHANDOFF`), `serial`/`previous` (цепочка),
  `owner_ack_sha256`, `created_at`, `entity=finance_accrual`, `days`, `source_concurrency=1`,
  `finance_finality=PROVISIONAL`.
- **Допустимый день**: строго после среза FULL (07.10); не позже `created_at` (МСК) − 2 дня —
  естественный цикл CURRENT для него уже не может быть создан; не покрыт ни одним планом
  `CURRENT finance_accrual`; не входит в более ранний авторитет цепочки; ≤ 31 дня, по возрастанию.
- **Долговременные записи**: корень дня = digest(авторитет, день): `CATCHUP_PLAN` → шард плана
  `DISPATCH_INTENT`/`DISPATCH_RECEIPT`/`RECONCILED` (тот же протокол, что у CURRENT) →
  `CATCHUP_CERT` (сертификат CURRENT: полная датированная сверка, натуральные ключи, DQ,
  `DATA_COVERAGE COMPLETE`) или `CATCHUP_PARTIAL`.
- **План дня**: однодневный план `finance_accrual` (`from = to = день`, `max_units = 20`,
  поколение `fcu-<hash16>-<YYYYMMDD>`, origin = `created_at` авторитета) через единственный путь
  `tenant_backfill.start` и общую пилотную аренду.
- **Порядок в пробуждении** (`current_controller.wake`): открытый receipt догона сверяется раньше
  любой новой работы CURRENT (два открытых полномочия — отказ); новый день догона выбирается только
  в самом конце, когда у сегодняшнего CURRENT больше нечего делать: `catalog` сертифицирован,
  каждая независимая сущность сертифицирована или PARTIAL. Одно пробуждение — не более одной
  отправки источника.
- **Сертификат только при сегодняшнем каталоге**: сверка связывает SKU с сегодняшним снимком
  каталога, поэтому receipt, сверенный после полуночи МСК, ждёт `CURRENT_CERT catalog` текущего дня
  (`FINANCE_CATCHUP_AWAITS_TODAYS_CATALOG`), а не проваливает связку.
- **Защита принятой истории**: перед первой отправкой дня читается RAW и `DATA_COVERAGE` за день;
  если там что-то есть — `CATCHUP_PARTIAL ACCEPTED_DATA_PRESENT` без обращения к источнику.
- **PARTIAL вместо глобального STOP**: неподтверждённая полнота (пустой ответ) или вердикт сверки
  (`PILOT_DATA_RECONCILIATION_FAILED`, `PILOT_SKU_LINKAGE_UNPROVEN`, `PILOT_CATALOG_SKU_JOIN_AMBIGUOUS`)
  закрывает день как PARTIAL и останавливает авторитет и все последующие серии; CURRENT продолжает
  работать. Любая другая ошибка — как раньше, fail-closed.
- **S4**: `current_independence.current_receipt` признаёт держателем общей аренды и receipt
  открытого дня догона (ровно один держатель на всё).
- **Выключение**: нет маркера — одно чтение метаданных за пробуждение, поведение прежнее; маркер
  есть, но релиз без `finance_catchup_version` — маркер игнорируется.

Изменённые файлы: `finance_catchup.py`, `finance_catchup_image_check.py` (новые, внесены в
`orchestration_contract.SOURCE_FILES`), `current_controller.py` (два крючка в `wake`),
`current_independence.py` (держатель аренды), `durable_plan.py` (три вида записей),
`orchestration_contract.py` (флаг релиза `finance_catchup_version: 1`, требует
`current_independence_version`, адаптер `finance_catchup_adapter`), `controller_image_check.py`,
`current_independence_image_check.py` (швы догона в фикстурах), CI.

## 3. Авторитет для текущего пропуска (OWNER ACK)

Один маркер серии 01: `days = [2026-10-08, 2026-10-09, 2026-10-10, 2026-10-11, 2026-10-12,
2026-10-17, 2026-10-19, 2026-10-22, 2026-10-23, 2026-10-25]`, `created_at` — момент публикации
(не раньше 2026-10-27 МСК, иначе 25.10 будет отклонён). Публикует владелец:
`finance_catchup.authority(...)` и `finance_catchup.publish(owner_backend, a)` — только при
`current_execution is None` и `phase_b_ack_sha256 == owner_ack_sha256`; перед созданием маркера
повторно проверяются цепочка, отсутствие планов CURRENT на эти дни и отсутствие строк RAW/покрытия.
Перед публикацией список заново сверяется запросом V1 (`FAIL_MISSING` по каждому дню). Новые
пропуски после публикации идут серией 02.

## 4. Проверка после загрузки

`docs/tenancy/heldi_finance_catchup_verify.sql` — только SELECT:

- V1–V6 по каждому дню: строки RAW = натуральные ключи `(accrual_id, type_id, sku)`;
  `V_COVERAGE_DAILY = COMPLETE` и `rows_loaded` = строкам RAW; один запуск, OK в журнале и с планом
  backfill; `FIN_CLASSIFICATION = PASS`, нет `UNKNOWN`-операций; нет SKU вне каталога;
  `analytics_share.store_costs_daily` со статусом COMPLETE. Итог `verdict`.
- V7: `V_FINANCE_WINDOW_STATUS.missing_days = 0` для окон с 01.10.
- V8: отпечаток всех принятых дней вне авторитета (строки, сумма, запуски, `max(extracted_at)`)
  до и после догона совпадает построчно.

Прогон 2026-10-25 на принятых днях: 07.10 (FULL), 20.10, 21.10 (CURRENT) — `PASS`; 22.10 —
`FAIL_MISSING`; V7 показывает missing_days 9…2. Базовый отпечаток V8 (190 дней, 09.04–24.10 без
девяти дней) снят до любых изменений: sha256 CSV
`0ae7ba9ab1d69a472a294e2a482430fbb478e85c9f415c3480555dc20d3ad8b2`.

## 5. Порядок выпуска

Один образ контроллера за раз. S4.1a (PR #379, main 18665db, release 2253a311, gen 60) уже в
production; ветка догона перебазирована на 18665db, `cloud_access.wire` догон не трогает. Запись релиза
догона строится от `2253a311` (квалификационная база) с добавлением `finance_catchup_version: 1` и
`verification.finance_catchup_adapter: PASS`, с новым `controller_implementation_hash`.
Совместимость с сертификатом v4 (fef95622, `TERMINAL_FAILED_CONTROLLER_PENDING_RECEIPT_CLOSURE_V4`):
его перепроверка на каждом пробуждении касается только шарда листа FULL; при открытом receipt FULL
`CURRENT.wake` (а с ним и догон) уступает до сверки, порядок сохраняется. Догон меняет `controller_implementation_hash`, поэтому его выпуск
и выпуск потока B (реклама/runtime) последовательны: второй перебазируется на первый и заново
квалифицируется; допустимо и объединить оба в один образ. Без маркера код догона поведение не
меняет, поэтому порядок определяется готовностью, а не зависимостью. Обязательные условия:

1. Маркер догона публикуется только после того, как живой образ несёт `finance_catchup_version`
   и принят `BFCURHANDOFF` для этого источника.
2. Пока день догона открыт (есть `CATCHUP_PLAN` без `CATCHUP_CERT`/`CATCHUP_PARTIAL`), не менять
   образ runtime: план дня содержит образ runtime, и после смены он не пройдёт проверку (как и план
   CURRENT). Если поток B меняет runtime, выпускать его до публикации маркера или после завершения
   всех девяти дней.
3. Не откатываться на образ без догона, пока открыт intent догона: старый код не видит этого
   держателя аренды.
