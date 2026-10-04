# CHZ SAFE OPERATIONS V2 — DESIGN D1: offline core

Дата: 2026-10-01. Статус: R2 offline hardening предъявляется для повторного owner review; core не принят.
Основание: DESIGN D1 и OWNER ACK «CHZ SAFE OPERATIONS V2 / R2 OFFLINE HARDENING» после GATE R1: FAIL.
Это описание текущей локальной implementation, не CURRENT production state и не разрешение операций ЧЗ.

## Scope и исполнение

Исполнитель — локальный Python, стандартная библиотека, SQLite, POSIX flock.
База: `b94ca335b98ce8146f925295f45d8789db5ed46c`; owner разрешил исключение fresh-base только для offline этапа.
Рабочее дерево: `/private/tmp/evetis-chz-safe-v2`, ветка `codex/chz-safe-operations-v2`.
Никакой интеграции с legacy imports, config, auth helpers, queue, CSV, PDF, journal или credentials.
Реальные значения запрещены закрытой synthetic grammar: `TEST-GTIN-NNN`, `SYN:...`.
Даже label `PROD` означает лишь тестирование разделения пространств в fake runtime.
Все state directories ограничены `tools/tests/chz_v2/.runtime` данного worktree.

Файлы: `domain.py` — identity/preview; `store.py` + `schema.sql` — каноническое состояние;
`coordinator.py` — одноразовые попытки и recovery; `queue.py` — выдача;
`fakes.py` — SUZ/True API/NK/signer в памяти. Операторский CLI/UI не реализован.

## Identity и immutable preview

`payload_hash = SHA256(canonical_payload)`; canonical JSON имеет сортированные ключи,
закрытую схему и отсортированные множества кодов/составов. Кодировка UTF-8, NaN запрещён.
Состав каждого набора задаётся явной матрицей parent/children; scalar `extra` запрещён.

`fingerprint = SHA256(schema_version, environment, participant, kind, payload_hash)`.
`operation_id = SHA256(те же поля + explicit business demand)`.
Повтор той же demand и content даёт ту же операцию. Новая demand с тем же content —
отдельная операция; approval требует явно перечислить все обнаруженные совпадения.
При появлении нового совпадения между approval и execute отправка блокируется.
Это escalation владельцу, не абсолютный запрет повторной законной business demand.

Prepared — frozen value object с immutable bytes, его identity повторно проверяется при сохранении.
SQLite запрещает изменение identity/payload. Preview возвращает отдельную проекцию:
среда, тип, GTIN, количество, masked aliases КИ, состав наборов, operation ID, hash,
synthetic effect и отсутствие реального rollback. Изменение preview-словаря не меняет canonical payload.
`SYN:...` не является настоящим КИ и никогда не конвертируется в реальный код.

Approval связывает operation ID, exact payload hash, среду, participant, owner,
время создания/истечения и reviewed duplicates. Доверенный synthetic caller передаёт время;
clock authority, owner authentication и UI confirmation в этот этап не входят.
Истёкший/неподходящий approval не расходуется и не отправляет запрос.
Для изменённого content нужна новая prepared identity и отдельное решение.

## State machine: business operation и execution attempt

| Из | Допустимые переходы |
|---|---|
| PREPARED | APPROVED, CANCELLED |
| APPROVED | SUBMITTING, INVALIDATED, CANCELLED |
| SUBMITTING | ACCEPTED, REJECTED, UNKNOWN |
| UNKNOWN | RECONCILING |
| ACCEPTED | RECONCILING |
| RECONCILING | SUCCEEDED, REJECTED, PARTIAL, UNKNOWN |
| REJECTED | APPROVED только по новому explicit approval |
| SUCCEEDED, PARTIAL, INVALIDATED, CANCELLED | Нет автоматического продолжения |

ACCEPTED означает приём, не завершение бизнес-операции. SUCCEEDED требует complete readback
с соответствующими operation ID и payload hash. PARTIAL удерживает scope для ручного решения.
Из UNKNOWN нет execute/retry перехода даже при отсутствии evidence: только readback/reconciliation.
После crash перед фактической отправкой SUBMITTING также становится UNKNOWN — цена безопасности
при невозможности доказать, дошёл ли вызов до внешней стороны.

Одна operation имеет много attempts, но один approval имеет максимум один attempt (UNIQUE).
В одной SQLite transaction фиксируются reservation, consumption approval, attempt с точными
bytes/hash и переход SUBMITTING; только затем вызывается fake submit.
Использованный approval не восстанавливается после crash/ошибки/доказанного отказа.
Доказанный REJECTED допускает новую попытку только после нового approve.
Restart переводит только незавершённые SUBMITTING/RECONCILING attempts в UNKNOWN;
остальные состояния сохраняет. Restart ничего не отправляет.

R2: generic public `transition` допускает только CANCELLED/INVALIDATED до submission.
`record_submission`, `begin_reconciliation`, `finish_reconciliation`, `mark_unknown` самостоятельно
проверяют operation/current attempt и владеют transaction. Terminal outcome требует typed evidence,
выданного встроенным fake service, с exact operation, attempt ID, business hash, environment,
participant, kind, complete/status/outcome и ожидаемым external ID. Недостаточное evidence даёт
UNKNOWN без замены trusted correlation/history. SQL дополнительно требует соответствующую
immutable validated_evidence запись текущего attempt. Evidence другого attempt той же operation
не принимается. Manually constructed Evidence и изменение sealed body отвергаются.

Fake provenance — process-local opaque reference → body hash, без credentials, реальной подписи
или external authority. Это защита публичного API от сконструированного ответа в synthetic модели,
не граница против Python reflection/изменения private registry или DB administrator.

## Три представления и bytes

Business canonical payload/hash отделён от transport JSON и подписанных bytes.
Attempt сохраняет `business_hash`, `transport_hash`, `signed_hash`, `wire_hash`, exact `wire`.
Fake transport — форматированный JSON; fake signed bytes совпадают с transport по определению
этого fake signer; wire — envelope с явно synthetic signature. Совпадение двух hashes допустимо,
значения всё равно хранятся в разных полях. R2 wire envelope содержит назначенный Store attempt ID;
этот ID возвращается в fake evidence и проверяется в Python/SQLite. Preview packet без attempt
не отправляется. Persisted wire — точные bytes bound packet, фактически переданного fake service.
Это не CryptoPro и не криптографическая подпись.
Перед execute проверяется соответствие; persisted attempt bytes immutable, при verify/recovery
пересчитываются все четыре hashes. Реальное XML/CMS и transport encoding потребуют отдельного дизайна.

## SQLite schema и ledger

| Таблица | Назначение / ключ |
|---|---|
| metadata | Immutable singleton: среда, participant, schema_version=2; несовпадение закрывает Store |
| operations | PK operation_id; immutable payload/identity, текущее business state |
| approvals | PK approval_id; exact binding, срок, reviewed duplicates, consumed |
| attempts | PK attempt_id; composite FK exact approval binding; UNIQUE approval_id и не более одного non-REJECTED attempt на operation; immutable bytes/hashes |
| validated_evidence | Immutable body/hash, phase, outcome, operation/current attempt binding; full synthetic payload только в private DB |
| events | Append-only sequence и SHA256 chain, переход, среда, время, attempt, safe evidence |
| codes | PK synthetic КИ; уникальная позиция, GTIN, external status/source/time, local allocation/holder |
| operation_codes | PK(operation,code); immutable declared participation: DOCUMENT/PARENT/CHILD |
| memberships | UNIQUE child → parent и операция; child не может входить в два набора |
| reservation_owners | Typed owner namespace: operation XOR issue, FK и exact owner ID |
| reservations | PK code_id; FK owner; глобальная exclusivity и синхронизация codes holder/allocation |
| issues | PK issue_id; RESERVED/COMMITTED, immutable committed payload и manifest hash |
| issue_items | PK(issue,code), UNIQUE code_id; immutable committed composition |

Не смешиваются три измерения:

- External status: EMITTED / APPLIED / INTRODUCED / RETIRED / UNKNOWN. Это статусы synthetic модели,
  не утверждённый mapping enum реального ЧЗ.
- Allocation: QUARANTINED / AVAILABLE / RESERVED / ISSUED_TO_FF + holder.
  ISSUED_TO_FF здесь означает зафиксированное выделение в bundle; получение ФФ всегда UNCONFIRMED.
- Participation: operation_codes + operation/attempt state; parent/child membership отдельно.

Synthetic lifecycle: retrieve даёт EMITTED в quarantine; utilisation допускает EMITTED,
успех даёт APPLIED; introduction допускает APPLIED, успех даёт INTRODUCED/AVAILABLE.
Для synthetic SET требуются INTRODUCED участники. Состав после успеха сохраняет hold;
автоматического разагрегирования/возврата children в queue нет.
Для utilisation/introduction проверяется совпадение GTIN. Реальные правила упаковки, BOM,
GTIN родителей/компонентов и допустимость реальных операций остаются integration blockers.
Повторное включение зарезервированного КИ другой операцией запрещено.
UNKNOWN/PARTIAL удерживают reservation; доказанный отказ освобождает holder в quarantine.
DELETE operation reservation также защищён SQL: inflight/UNKNOWN/PARTIAL release запрещён;
разрешены pre-submit CANCELLED/INVALIDATED либо текущий validated rejection / successful
UTILISATION/INTRODUCTION. Membership/committed issue reservations не освобождаются.
История происхождения и документов доступна через codes, operation_codes, issue_items и events.

SQLite: BEGIN IMMEDIATE, FK ON, journal_mode DELETE, synchronous EXTRA, fullfsync ON.
State directory 0700, DB/lock/artifacts 0600. Один процесс использует advisory flock на стабильном
lock-файле; файл не удаляется. Его старое содержимое/PID не является lock authority.
ОС освобождает flock при завершении процесса. Это протокол для cooperating processes,
не защита от администратора, произвольно меняющего SQLite или подменяющего Python.

Все public Store mutations берут стабильный flock и BEGIN IMMEDIATE; вложенные вызовы используют
SAVEPOINT с самостоятельным rollback, даже если caller ловит exception. State + journal atomic.
Поздний item failure откатывает всю reservation acquisition. CHECK/enum, composite FK, unique
indexes и triggers защищают owners, binding, transitions, memberships и issue composition,
включая INSERT OR REPLACE. JSON missing/NULL при issue commit fail closed.
FK/recursive_triggers включены на Store connection. Privileged DDL/PRAGMA bypass не security boundary.
Schema 1 отклоняется до DDL; ни automatic migration, ни import старых данных не выполняются.

## Transactional queue и recovery

1. Под flock/SQLite transaction выбираются только AVAILABLE/INTRODUCED без holder по position.
2. Создаются RESERVED issue и issue_items; КИ резервируются атомарно, событие фиксируется вместе.
3. В staging создаются synthetic items.json и manifest.sha256, flush/fsync файлов и каталога.
4. rename staging → bundle; fsync родительского каталога. Это только filesystem commit.
5. Проверяются состав и hashes, exact ownership всех items и item_count; отдельная SQLite
   transaction фиксирует immutable COMMITTED payload/hash, ledger allocation и QUEUE_COMMITTED.
   Число обновлённых ledger rows обязано равняться item_count, иначе rollback.
   Только после неё receipt становится deliverable; receipt сравнивает bundle/current rows
   с сохранённым immutable business payload/hash и committed ownership.
6. Повтор build/commit/receipt той же выдачи не создаёт новую выдачу.

| Состояние после сбоя | Recovery |
|---|---|
| Reservation есть, artifacts отсутствуют/неполны | NON_DELIVERABLE_RESERVED; rebuild той же issue |
| Bundle есть, SQLite issue RESERVED | NON_DELIVERABLE_PENDING_RECOVERY; explicit commit той же issue |
| Bundle есть, SQLite issue отсутствует | NON_DELIVERABLE_ORPHAN; только inventory, нет автоматического adopt |
| SQLite COMMITTED, bundle соответствует manifest | DELIVERABLE; прежний receipt |
| SQLite COMMITTED, bundle отсутствует/повреждён | SafetyError; выдача блокируется |

Pointer — SQL projection MIN(position), внешний pointer.txt игнорируется.
Ссылки в state/bundle запрещены, filenames не принимаются от пользователя; issue ID строго hex.
Сбой fsync/rename не фиксирует business issue. Power-loss/controller-cache durability не доказана
процессными synthetic tests. Backup/restore, шифрование реальных данных и disaster-recovery media — будущий этап.

## Duplicate prevention и reconciliation

Общее BEFORE: exact immutable identity, journal/bytes integrity, среда/participant, live local
approval, duplicate review, lifecycle/reservation; AFTER: receipt ID + safe response hash;
timeout: UNKNOWN; retry: только новый approval после доказанного REJECTED. UNKNOWN никогда не retry.

| Тип | Business object/content | Terminal synthetic evidence | Recovery / compensation |
|---|---|---|---|
| ORDER | Demand, GTIN, quantity | Exact order correlation и complete outcome | Fake readback; новый order не создаётся автоматически |
| RETRIEVE | Demand, order ID, GTIN, quantity | Exact block, quantity, unique codes и hash | Сохранённый block ID либо единственный delta от pre-submit blocks; retry старого block |
| UTILISATION | Exact множество codes/GTIN | Exact document outcome | Readback; без повторного документа/авто-компенсации |
| INTRODUCTION | Exact множество codes/GTIN | Exact document outcome | Readback; без автоматического повторного ввода |
| SET | Explicit parent/children matrix | Exact outcome, unique membership | Readback; разагрегирование не реализовано |
| NK_FEED | GTIN, version, value | Exact operation/content complete result | Fake readback; реальный version/CAS mapping не реализован |
| NK_SIGN | GTIN, version, exact synthetic content | Exact operation/content complete result | Fake readback; реальный signed-content/version verification — blocker |

Fake services намеренно не имеют server idempotency: лишний submit создаёт новый ID.
Это позволяет обнаружить повтор вместо сокрытия бага за fake deduplication.
SUZ framework сохраняет baseline block IDs перед первой попыткой, читает старый block;
0/2+ candidates, closed order, неполный/чужой result, неверное quantity или повтор кодов дают UNKNOWN.
Неизвестная доступность настоящего blocks/retry endpoint не заменяется повторным обычным retrieval.
Fake readback умеет exact operation correlation; наличие такой возможности у реальных API UNPROVEN.

## Journal V2 / security / migration

Journal содержит hashes, IDs, время, переходы, безопасный outcome/HTTP status. Все public input
fields проверяются: operation/issue ID должен существовать и иметь fixed hex shape; attempt ID
должен принадлежать operation; timestamp typed; transition/outcome enum; details typed whitelist.
SQL также проверяет references и values. Не принимаются произвольные response/error bodies,
reason strings, codes, tokens, PIN или headers. Canonical code payloads и
wire хранятся только в private synthetic SQLite, кодовые bundles — в private synthetic directories.
Обычный stdout core пуст; repr payload/evidence bytes скрыт. Chain/append-only triggers ловят
проверенные повреждения, но не являются подписанным внешним anti-tamper журналом: DBA может
переписать всю историю. Legacy 376 records не импортировались и не переписывались.

Fake adapters имеют только in-memory dictionaries и методы; не имеют URL/client/session,
credential/config discovery, socket/HTTP, subprocess, ctypes, CryptoPro или real signer.
Coordinator принимает только конкретные встроенные fake types. Tests блокируют socket,
subprocess/exec/spawn/system и ctypes.dlopen audit events, а также обращения к типовым credential paths.
Это доказательство структуры и конкретного прогона, не sandbox для произвольного чужого Python-кода.

Migration не реализована. Historical pointers/journal/ACK не источник CURRENT state.
Legacy extra finding остаётся DEFECT на уровне ранее исследованной логики повторного child;
факт production incident остаётся UNPROVEN. Этот этап не перечитывает реальные документы/очередь.

## Следующие gates (не разрешены этим ACK)

Нужны отдельные reviewed scope/ACK: минимальный read-only inventory production state и разрешённые
пути/поля; mapping legacy→ledger с provenance и collision policy; controlled current readback;
реальные API schemas/idempotency/retention/version constraints; migrations и rollback/backup design;
owner identity/time/approval UX; BOM/status rules; secure code storage, printable artifact renderer,
FF delivery acknowledgement. До этого offline library не подключать к legacy runtime.

Crypto Acceptance Plan остаётся только планом: certificate metadata → thumbprint matching →
container/key availability → offline synthetic signature → verification → отдельно sandbox auth →
отдельно production auth. Ни один из этих шагов не исполнялся.

## Design deviations / уточнения

1. Реализована закрытая synthetic schema вместо принятия production payloads; это ограничение данного ACK.
2. Bundle — synthetic JSON/hash, без PDF/CSV/label renderer и без бизнес-выдачи.
3. NK adapters проверяют framework evidence, а не реальные revisions, XML/CMS или CAS.
4. Store расположен исключительно внутри test directory; deployment/migration не поддерживаются.
5. Истёкший approval/late duplicate блокирует execute. Полноценный workflow supersede/reapprove
   APPROVED операции не реализован; нельзя обходить его ручным SQL или создавать новую demand без решения.
6. Clock/owner — explicit synthetic inputs; durable external fake state переживает reopen локального Store
   в рамках test process, а не полное выключение fake server. Отдельный OS crash test покрывает queue/lock.
7. Общий repository verification не выдаёт PASS: полный C7 pytest отсутствует и не запускался;
   live gates запрещены. Подробности и machine evidence — в ACCEPTANCE.
8. R2 вводит schema 2 и synthetic attempt correlation/provenance. Отсутствие real evidence authority,
   процесса durable fake server и real wire schema остаётся integration blocker; это не auth/signing.
9. Из RECONCILING удалён RECONCILING→ACCEPTED: incomplete receipt не подтверждает reconciliation;
   fail closed в UNKNOWN. Business kinds/production functionality не расширялись.
