# CHZ SAFE OPERATIONS V2 — R2 OFFLINE HARDENING: evidence for owner review

Дата: 2026-10-01. CURRENT только для данного локального worktree/review artifact.
Основание: OWNER ACK R2; предыдущий GATE R1: FAIL принят владельцем.
Offline Core **не объявлен принятым**. Повторный review и решение владельца остаются pending.
Production state, remote freshness и production permissions этим отчётом не подтверждаются.

## Результат и reproducibility

Final synthetic result: **67/67 PASS** на Homebrew Python 3.14.6 (89.916s)
и **67/67 PASS** на bundled Python 3.12.14 (89.968s).
0 failures, 0 errors, 0 skips в каждом прогоне. Это validation R2 scope, не owner acceptance.

Runner: существующий stdlib unittest, без pytest/install/venv, без полного repository suite.
Команда из `/private/tmp/evetis-chz-safe-v2`:

```text
python3 -B -W error::ResourceWarning -m unittest tools.tests.chz_v2.test_core tools.tests.chz_v2.test_r2 -v
```

Для второго прогона executable заменён на
`/Users/evgenelagin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`.
67 tests = 36 сохранённых/усиленных D1 + 31 новых R2 regression/adversarial tests.
Python ResourceWarning считается ошибкой. Все test data — synthetic grammar.

Evidence внутри ignored `tools/tests/chz_v2/.runtime/`:

- `r2-review.patch`: diff **PRE-R2 → R2**, без index/staging; 11 прежних files snapshot + новый test_r2.py.
- `r2-before/`: точные reviewed bytes до R2; SHA256 всех 11 совпал с историческим `file-hashes.json`.
- `r2-file-hashes.json`, `r2-evidence.json`: baseline/current hashes, scope, imports, Git state и результаты.
- `r2-tests.txt`, `r2-tests-python312.txt`: полные verbose reports обоих финальных прогонов.
- `r2-impact.json`: существующий impact framework, исходный machine verdict.

Исторические `review.patch`, `test-report.txt`, `evidence.json`, `file-hashes.json` не переписаны;
их прежний результат 36/36 не отменяет обнаруженный R1 FAIL.

## R1 finding → исправление → regression tests

Все short test names ниже находятся в `tools/tests/chz_v2/test_r2.py` с префиксом `test_r1_`.
Полные имена и индивидуальные результаты приведены в verbose report в конце.

| Finding | Конкретное исправление | Regression evidence |
|---|---|---|
| R1-01 | Generic transition только pre-submit controls. Specialized Store APIs проверяют operation/current attempt, typed fake provenance, exact hashes/identity/status/complete/expected ID. Wire/evidence содержат attempt ID; immutable validated_evidence и SQL terminal guard | 01_generic_public_terminal_and_approval_bypass_rejected; 01_wrong_operation_attempt_cannot_resolve_or_mark_unknown; 01_incomplete_contradictory_wrong_type_evidence_fail_closed; 01_manufactured_complete_rejection_has_no_fake_provenance; 01_previous_attempt_evidence_cannot_resolve_current_attempt |
| R1-02 | Каждый public mutation самостоятельно берёт flock/BEGIN IMMEDIATE; вложенный SAVEPOINT rollback. Atomic state+journal, reservation acquisition под тем же lock/transaction | 02_direct_reservation_late_failure_rolls_back_all_items; 02_public_control_and_journal_atomic_on_event_failure; 02_nested_public_failure_savepoint_does_not_leak_reservation; 02_synchronized_public_reservations_one_child_two_sets |
| R1-03 | Schema 2: enums/CHECK, composite exact approval FK, approval one-use, one non-REJECTED attempt, typed owners + PK reservations, holder coupling, membership composition/exclusivity, UNIQUE issue code, immutable committed rows и REPLACE guards; UNKNOWN reservation release запрещён SQL | 03_direct_sql_enums_dangling_owner_and_singleton_rejected; 03_direct_sql_attempt_requires_consumption_and_matching_approval; 03_sql_two_active_attempts_for_one_operation_rejected; 03_direct_sql_terminal_without_evidence_rejected; 03_sql_membership_unique_and_ownership_enforced; 03_sql_code_cannot_participate_in_two_issues; 03_sql_replace_cannot_rewrite_immutable_committed_rows; 03_sql_initial_terminal_state_rejected; 03_unknown_reservation_cannot_be_released_by_sql; 03_old_schema_refused_without_migration |
| R1-04 | Commit сохраняет immutable payload/hash; receipt сверяет committed hash, current rows/artifacts/ownership. Exact item_count и affected-row count; mismatch rollback; orphan не deliverable | 04_committed_composition_and_business_commit_immutable; 04_correlated_artifact_and_row_corruption_cannot_change_receipt; 04_missing_and_partial_reservation_ownership_fail_commit; 04_rowcount_mismatch_rolls_back_commit; 04_sql_null_missing_manifest_fields_fail_closed; 04_os_process_loss_inside_sqlite_commit_recovers; D1 orphan tests |
| R1-05 | external_id меняется только после валидированного evidence; trusted ID immutable SQL. UNKNOWN не записывает untrusted evidence/correlation; expected ID обязателен при известной correlation | 05_bad_evidence_preserves_trusted_external_id_and_history; 05_unknown_correlation_set_only_by_valid_recovery; 05_coordinator_wrong_returned_id_remains_unknown |
| R1-06 | Existing fixed-shape operation/issue references, attempt ownership, typed time, finite transition/outcome, typed details whitelist; SQL reference/details/append-only guards. Exceptions не echo sensitive input | 06_all_public_journal_string_fields_reject_sensitive_values; 06_event_attempt_reference_must_belong_to_operation; 06_sql_unsafe_event_values_and_replacement_rejected; усиленный D1 journal redaction на кодовой операции |

## Schema / state model

13 таблиц: metadata, operations, approvals, attempts, validated_evidence, events, codes,
operation_codes, memberships, reservation_owners, reservations, issues, issue_items.
Business operation, exact owner approval и execution attempt — отдельные entities.
Operation identity по explicit demand не заменяется content fingerprint; duplicate content
требует owner review, но разрешает новую явно утверждённую demand.
Один consumed approval не может начать второй attempt. REJECTED → APPROVED требует нового approval.
UNKNOWN → только RECONCILING; никакой automatic mutating retry. PARTIAL удерживает reservations.

Canonical business payload/hash, formatted transport, signed bytes и wire envelope/hash хранятся
отдельно. Fake signed=transport по определению; wire содержит synthetic signature и attempt ID.
SQLite не умеет самостоятельно пересчитывать SHA256 внешнего файла: hash/bytes проверяет Store/Queue;
сохранённые committed hash/payload защищены SQL immutability. Artifact rename не business commit.
Schema 1 не мигрируется: отказ до DDL с regression byte comparison на synthetic DB.

## Качество тестов / adversarial SQLite и Store

Вызовы атак выполняются через public Store без wrapper, либо direct DML на Store connection
с FK ON и recursive_triggers ON. Проверяются реальные exceptions/rollback и persisted rows,
а не только вручную выставленный итоговый state. В частности late item failure оставляет 0 holds;
journal insertion failure откатывает control state; nested caller, поймавший exception, не сохраняет
partial reservations. Direct SQL несовместимых owners/enums/approval/attempt/terminal/membership/
issue composition/REPLACE отклоняется. NULL/missing committed JSON fields fail closed.

Lost-response tests подтверждают remote-side fake SUCCEEDED record, exact operation/hash/attempt,
consumed approval, immutable wire hash после reopen и exact submission_count без роста при retry/recovery.
Fake не deduplicate: каждый лишний submit действительно создал бы новый external ID.
31 R2 tests добавлены к 36 D1; базовый journal leakage test теперь использует actual synthetic code
в UTILISATION и проверяет все event columns. В R2 sensitive fixtures проверяют refs/transition/details,
journal rows и захваченные stdout/stderr; реальные secrets/КИ не используются и не выводятся.

Negative evidence modes выдаются самим fake service с provenance, поэтому тесты incomplete/wrong
hash/environment/kind/participant/status/ID действительно проверяют semantic validation.
Отдельный forged Evidence содержит формально корректные поля, включая current attempt, но не provenance.
Old valid evidence предыдущего attempt той же operation не принимается для нового attempt.

Corruption tests, специально удаляющие trigger, помечены как **fault injection**, не допустимый
runtime workflow: проверяется, что receipt/verify обнаруживает повреждение следующей защитой.
Commit rowcount test внедряет BEFORE UPDATE RAISE(IGNORE): rollback issue/event при неполном обновлении.

## Concurrency / crash results

Все scenarios ниже PASS в обоих финальных прогонах.

Синхронизация — явные pipe handshakes, без sleep-based race. Два однонаправленных anonymous OS pipes
вместо socketpair. Это local IPC; networking не требуется.

| R2 scenario | Проверяемый результат | Финальный статус |
|---|---|---|
| Два queue writers, contention и последующий retry | Второй busy при lock; потом отдельная issue; 2 issues, 4 уникальных codes и 4 reservations | PASS |
| Два public SET reservations с одним child | Второй busy при lock и blocked после release; child у первого, второй parent не partial-reserved; 0 submits | PASS |
| UNKNOWN SET + direct SQL DELETE hold + restart | SQL release отвергнут; 2 holds сохранены, remote success и 1 submit; readback создаёт membership | PASS |
| os._exit(74) внутри issue DB transaction | После reopen: RESERVED/pending recovery, 0 issued rows, прежний journal, 2 holds; explicit commit той же issue/hash | PASS |
| Lost response / before submit / after HTTP | Remote effect проверяется; restart UNKNOWN; 0 либо 1 submit по фактической boundary; повтор не отправляется | PASS |
| Потеря full/partial issue reservation | Commit rejected, issue не COMMITTED | PASS |
| Correlated rows+artifact corruption | Immutable committed fingerprint блокирует receipt | PASS |
| Orphan bundle | NON_DELIVERABLE_ORPHAN, нет adopt/commit/receipt | PASS |
| Повтор evidence старого attempt | UNKNOWN, history/current correlation не испорчены; fresh current evidence завершает ту же attempt, всего 2 approved submits | PASS |

Process crashes и injected fsync/rename failures проверены. Physical power loss, controller-cache
и target production filesystem durability этим набором не доказаны.

## Fault-injection matrix DESIGN D1 (после R2)

Каждая строка ниже — synthetic PASS, не свидетельство поведения сервиса ЧЗ.
Имена относятся к `tools/tests/chz_v2/test_core.py`; полный список запусков ниже.

| Сценарий D1 / дополнительный invariant | Verdict | Тест / наблюдение |
|---|---|---|
| Crash before submit | PASS | crash_before_submit_is_conservatively_unknown: approval consumed, 0 submits, UNKNOWN после restart |
| Timeout after remote acceptance | PASS | timeout_after_acceptance_never_auto_retry: 1 submit, readback → SUCCEEDED |
| Crash after HTTP success before journal | PASS | crash_after_http_before_journal: restart → UNKNOWN, readback без повторного submit |
| Crash after queue reservation | PASS | crash_after_queue_reservation: reservation сохранена, receipt запрещён |
| Crash during artifact creation | PASS | artifact_crash_matrix_and_non_deliverable_orphan: частичный staging не deliverable |
| Crash before atomic rename | PASS | тот же test, before_rename: rebuild той же issue |
| Crash after rename / before business commit | PASS | тот же test, after_rename: NON_DELIVERABLE_PENDING_RECOVERY |
| Crash during SQLite issue commit | PASS | issue_commit_crash_and_idempotent_receipt: rollback, receipt запрещён |
| Crash after SQLite issue commit | PASS | тот же test: прежний receipt после reopen, новая issue не создаётся |
| Реальные ошибки fsync / rename (injected OSError) | PASS | filesystem_failures_leave_issue_non_deliverable |
| Duplicate process | PASS | duplicate_process_stale_lock_and_concurrent_issue: второй lock отвергнут |
| Concurrent issue | PASS | тот же test: два OS processes; unique issue_items, минимум одна выдача |
| Stale lock | PASS | тот же test: старый PID в файле не мешает ОС lock |
| OS process loss | PASS | real_process_crash_releases_lock_keeps_reservation: os._exit(73), lock освобождён, reservation сохранена |
| Corrupted pointer | PASS | corrupted_artifact_and_pointer: внешний pointer не влияет на SQL projection |
| Corrupted artifact | PASS | тот же test: receipt запрещён при несовпадении manifest |
| Duplicate КИ | PASS | duplicate_codes_and_extra_forbidden: duplicate list отвергнут |
| Same КИ in two sets / scalar extra / self-parent | PASS | тот же test: все формы отвергнуты до submit |
| Same КИ in concurrent documents | PASS | ledger_blocks_same_code_two_operations: второй документ не отправлен |
| Retry after UNKNOWN | PASS | timeout_after_acceptance_never_auto_retry + crash_before_submit_is_conservatively_unknown |
| Expired approval / changed hash | PASS | expired_approval_and_changed_hash: 0 submits |
| Wrong environment / Store identity | PASS | environment_is_hard_boundary: несовместимые Store/adapter/operation отвергнуты |
| Restart every intermediate operation state | PASS | реальные PREPARED/APPROVED/ACCEPTED/UNKNOWN пути сохранены; SUBMITTING/RECONCILING отдельными crash tests → UNKNOWN |
| Restart terminal/control states | PASS | restart_preserves_each_non_inflight_state: SUCCEEDED/REJECTED/PARTIAL/INVALIDATED/CANCELLED сохранены, submits не растут |
| Одно approval — одна attempt | PASS | attempt_separate_one_approval_one_submission: UNIQUE approval, повтор отвергнут |
| Rejected → новая attempt | PASS | rejection_requires_fresh_explicit_approval: только новый approval, readback правильного external ID |
| Новая законная demand с тем же content | PASS | new_demand_duplicate_review_not_identity: отдельная identity, обязательный review совпадений |
| Late duplicate после approval / forged Prepared | PASS | forged_prepared_identity_and_late_duplicate_blocked |
| Bundle без SQLite issue | PASS | orphan_without_sqlite_issue_never_committed: NON_DELIVERABLE_ORPHAN, commit/receipt запрещены |
| Canonical/transport/signed/wire separation | PASS | three_hashes_and_exact_signed_bytes: отдельные hashes, изменение signed bytes отвергнуто |
| Persisted bytes corruption | PASS | attempt_bytes_immutable_and_corruption_detected: trigger + повторная проверка |
| Journal corruption / redaction | PASS | journal_append_only_chain_and_redaction, journal_corruption_blocks_execute, journal_rejects_secret_in_allowed_field |
| SUZ retrieval recovery старого block | PASS | retrieval_recovery_no_new_codes: один submit, ровно прежние unique codes |
| Closed order / ambiguous blocks / foreign or partial evidence | PASS | retrieval_closed_order_remains_unknown, ambiguous_or_mismatched_retrieval_evidence: UNKNOWN |
| Partial external outcome | PASS | partial_is_not_success_or_retry: PARTIAL, нет автоматического retry |
| Delayed readback | PASS | restart_during_reconciling_and_delayed_readback: UNKNOWN до появления complete evidence |
| Все семь operation kinds | PASS | all_seven_operation_types_terminal_evidence: ORDER/RETRIEVE/UTILISATION/INTRODUCTION/SET/NK_FEED/NK_SIGN |
| Ledger lifecycle/history | PASS | lifecycle_and_operation_participation_history: EMITTED→APPLIED→INTRODUCED, обе participation сохранены |
| Symlink / path traversal | PASS | symlink_artifact_and_path_traversal_rejected: target не изменён |
| Network/auth/Crypto/real input boundary | PASS | no_real_inputs_or_adapters_or_home_paths + audit assertions каждого test |
| Normal stdout | PASS | core_stdout_empty; payload repr/preview masked в identity test |

## Ограничение network/auth/CryptoPro/production effects

1. AST inventory core imports сохранён в r2-evidence.json: stdlib + собственные v2 modules.
   Нет legacy chz.http/imports, HTTP/socket client, subprocess, ctypes, config/credential lookup,
   CryptoPro, real signer, certificate/private-key store или реального endpoint.
   Coordinator принимает только конкретные встроенные fake types; fake storage — memory dictionaries.
2. Каждый test включает audit guard socket.*, subprocess.*, os.exec/spawn/system, ctypes.dlopen;
   protected credential paths блокируются. Каждый tearDown требует DENIED=[] и PROTECTED_ACCESSES=[].
   В обоих финальных прогонах все 67 tearDown assertions PASS: DENIED=[] и PROTECTED_ACCESSES=[].
3. В промежуточном test hardening guard заблокировал локальный socket.__new__ от duplex Pipe/socketpair.
   Реального endpoint/network request не было. Harness исправлен на anonymous Pipe(duplex=False).
   Другие промежуточные failure — старые tamper fixtures, остановленные новыми SQL constraints;
   fixture усилены без ослабления guards. Промежуточные failures не переименованы в PASS.
4. DB, bundles, baseline snapshot и reports размещены только внутри synthetic test .runtime worktree.
   Обычный output не содержит full codes/secrets; sensitive test fixtures находятся только в tests/private DB.
5. В target worktree tracked diff и staged diff пусты, HEAD/base/branch сохранены. Изменяются только
   разрешённые V2 files; legacy scripts, production queue/CSV/pointers/PDF не читаются и не пишутся.
   ~/.config/evetis-chz, auth/token refresh, CryptoPro не использовались; install/venv/manifests не менялись.
   Commit/push/PR/merge/deploy/Git ref changes не выполнялись на R2.

Граница доказательства — code structure, конкретные guarded test runs, scope/hashes/Git и выполненные
команды. Это не глобальный OS trace/anti-tamper proof. Реальные credential/runtime files намеренно
не читались и не хешировались. CURRENT production state остаётся UNPROVEN.
Исходный repository имеет прежние 4 modified ingestion paths; текущий untracked inventory шире
исторического снимка из предыдущего этапа, поэтому глобальная неизменность чужого dirty tree не
объявляется доказанной. R2 не выполнял в нём writes; точный наблюдавшийся статус сохранён в evidence.

## C7 / существующий verification framework

impact_analysis explicit --files всех 12 V2 paths: exit 0; machine risk TIER2;
affected_total=0; required_contracts={}; required command `python -m pytest -q tools/tests`.
Inventory 2026-09-22T04:35:09Z. Это не domain safety PASS и не C7 PASS.

**C7 BLOCKED**. Homebrew Python 3.14.6 и bundled Python 3.12.14 не имеют pytest
(find_spec=False); project venv/pyvenv.cfg не найден. Repository dependency уже объявлена:
`tools/requirements-sql-ci.txt`, pytest==8.3.4 с hashes; `.github/workflows/sql-current.yml`
использует Python 3.12, installs этот manifest и запускает полный tools/tests.
Root cause — environment/bootstrap, не отсутствующая repository declaration.

Полный suite не запускался: `tools/tests/test_autonomy_security.py` содержит subprocess
`gcloud auth print-access-token` и `gh auth status` в scrubbed_env, а также suites с Git subprocess
и temporary repositories. Эти effects требуют отдельно reviewed isolation/ACK и отсутствуют в R2.
Минимальный будущий C7 ACK: отдельная изолированная Python 3.12 environment, установка существующего
hash-pinned manifest без правки manifests/system Python, review и ограничение effects полного suite,
запуск в scrubbed среде без production credential mounts. Один targeted pytest V2 подтвердит runner
compatibility, но не закроет требование полного tools/tests. На R2 ничего не устанавливалось.

## Design deviations / оставшиеся blockers / следующий gate

Уточнения R2 в DESIGN: schema 2 без migration; evidence attempt binding и process-local fake provenance;
RECONCILING→ACCEPTED исключён (incomplete evidence → UNKNOWN). Production kinds не добавлялись.
Fake provenance не заменяет owner authentication, trusted clock, signed external evidence или durable
remote service. SQLite DDL/PRAGMA bypass и Python private registry modification вне threat model.
Все прежние synthetic ограничения D1 сохранены: JSON bundle без PDF, NK без real CAS/XML/CMS,
test-only storage, отсутствие approval supersede UI/owner authority, memory fake server.

Оставшиеся blockers:

1. STRICT READ-ONLY GATE R3 review exact PRE-R2 diff, schema/API/test quality и решение владельца;
   успешные synthetic tests не являются принятием Offline Core.
2. Отдельный C7 bootstrap/runner/effects gate; общий repository acceptance pending.
3. Отдельный OWNER ACK на минимальный read-only integration inventory с exact resource/field scope;
   production queue freshness, used codes и historical mapping пока UNPROVEN.
4. Real API evidence/correlation/status/retention/retry/block/version/CAS contracts, GTIN/BOM rules,
   trusted approval/time workflow, secure storage и migration/backup/collision/recovery design.
5. Crypto Acceptance Plan и printable/delivery workflow — отдельные будущие gates, не операции R2.

Рекомендация: следующий gate — **R3 STRICT READ-ONLY review**, без fixes/install/auth/network.
C7 закрывать отдельным последующим ACK; migration/integration автоматически не начинать.

## Git / files

Worktree `/private/tmp/evetis-chz-safe-v2`; branch `codex/chz-safe-operations-v2`.
HEAD `b94ca335b98ce8146f925295f45d8789db5ed46c`, без commit/branch/index changes.
12 new untracked paths vs HEAD; R2 diff изменяет 9 paths относительно PRE-R2.
`domain.py`, `__init__.py`, tests `.gitignore` unchanged vs PRE-R2.
Reports/baseline/DB artifacts ignored; staged/tracked diff пусты.

```text
?? docs/chz/CHZ_SAFE_OPERATIONS_V2_ACCEPTANCE.md
?? docs/chz/CHZ_SAFE_OPERATIONS_V2_DESIGN.md
?? tools/chz/v2/__init__.py
?? tools/chz/v2/coordinator.py
?? tools/chz/v2/domain.py
?? tools/chz/v2/fakes.py
?? tools/chz/v2/queue.py
?? tools/chz/v2/schema.sql
?? tools/chz/v2/store.py
?? tools/tests/chz_v2/.gitignore
?? tools/tests/chz_v2/test_core.py
?? tools/tests/chz_v2/test_r2.py
```

## Полный synthetic test report

### Homebrew Python 3.14.6

```text
test_all_seven_operation_types_terminal_evidence (tools.tests.chz_v2.test_core.CoreTests.test_all_seven_operation_types_terminal_evidence) ... ok
test_ambiguous_or_mismatched_retrieval_evidence (tools.tests.chz_v2.test_core.CoreTests.test_ambiguous_or_mismatched_retrieval_evidence) ... ok
test_artifact_crash_matrix_and_non_deliverable_orphan (tools.tests.chz_v2.test_core.CoreTests.test_artifact_crash_matrix_and_non_deliverable_orphan) ... ok
test_attempt_bytes_immutable_and_corruption_detected (tools.tests.chz_v2.test_core.CoreTests.test_attempt_bytes_immutable_and_corruption_detected) ... ok
test_attempt_separate_one_approval_one_submission (tools.tests.chz_v2.test_core.CoreTests.test_attempt_separate_one_approval_one_submission) ... ok
test_core_stdout_empty (tools.tests.chz_v2.test_core.CoreTests.test_core_stdout_empty) ... ok
test_corrupted_artifact_and_pointer (tools.tests.chz_v2.test_core.CoreTests.test_corrupted_artifact_and_pointer) ... ok
test_crash_after_http_before_journal (tools.tests.chz_v2.test_core.CoreTests.test_crash_after_http_before_journal) ... ok
test_crash_after_queue_reservation (tools.tests.chz_v2.test_core.CoreTests.test_crash_after_queue_reservation) ... ok
test_crash_before_submit_is_conservatively_unknown (tools.tests.chz_v2.test_core.CoreTests.test_crash_before_submit_is_conservatively_unknown) ... ok
test_duplicate_codes_and_extra_forbidden (tools.tests.chz_v2.test_core.CoreTests.test_duplicate_codes_and_extra_forbidden) ... ok
test_duplicate_process_stale_lock_and_concurrent_issue (tools.tests.chz_v2.test_core.CoreTests.test_duplicate_process_stale_lock_and_concurrent_issue) ... ok
test_environment_is_hard_boundary (tools.tests.chz_v2.test_core.CoreTests.test_environment_is_hard_boundary) ... ok
test_expired_approval_and_changed_hash (tools.tests.chz_v2.test_core.CoreTests.test_expired_approval_and_changed_hash) ... ok
test_filesystem_failures_leave_issue_non_deliverable (tools.tests.chz_v2.test_core.CoreTests.test_filesystem_failures_leave_issue_non_deliverable) ... ok
test_forged_prepared_identity_and_late_duplicate_blocked (tools.tests.chz_v2.test_core.CoreTests.test_forged_prepared_identity_and_late_duplicate_blocked) ... ok
test_identity_and_preview_immutable_no_code_leak (tools.tests.chz_v2.test_core.CoreTests.test_identity_and_preview_immutable_no_code_leak) ... ok
test_issue_commit_crash_and_idempotent_receipt (tools.tests.chz_v2.test_core.CoreTests.test_issue_commit_crash_and_idempotent_receipt) ... ok
test_journal_append_only_chain_and_redaction (tools.tests.chz_v2.test_core.CoreTests.test_journal_append_only_chain_and_redaction) ... ok
test_journal_corruption_blocks_execute (tools.tests.chz_v2.test_core.CoreTests.test_journal_corruption_blocks_execute) ... ok
test_journal_rejects_secret_in_allowed_field (tools.tests.chz_v2.test_core.CoreTests.test_journal_rejects_secret_in_allowed_field) ... ok
test_ledger_blocks_same_code_two_operations (tools.tests.chz_v2.test_core.CoreTests.test_ledger_blocks_same_code_two_operations) ... ok
test_lifecycle_and_operation_participation_history (tools.tests.chz_v2.test_core.CoreTests.test_lifecycle_and_operation_participation_history) ... ok
test_new_demand_duplicate_review_not_identity (tools.tests.chz_v2.test_core.CoreTests.test_new_demand_duplicate_review_not_identity) ... ok
test_no_real_inputs_or_adapters_or_home_paths (tools.tests.chz_v2.test_core.CoreTests.test_no_real_inputs_or_adapters_or_home_paths) ... ok
test_orphan_without_sqlite_issue_never_committed (tools.tests.chz_v2.test_core.CoreTests.test_orphan_without_sqlite_issue_never_committed) ... ok
test_partial_is_not_success_or_retry (tools.tests.chz_v2.test_core.CoreTests.test_partial_is_not_success_or_retry) ... ok
test_real_process_crash_releases_lock_keeps_reservation (tools.tests.chz_v2.test_core.CoreTests.test_real_process_crash_releases_lock_keeps_reservation) ... ok
test_rejection_requires_fresh_explicit_approval (tools.tests.chz_v2.test_core.CoreTests.test_rejection_requires_fresh_explicit_approval) ... ok
test_restart_during_reconciling_and_delayed_readback (tools.tests.chz_v2.test_core.CoreTests.test_restart_during_reconciling_and_delayed_readback) ... ok
test_restart_preserves_each_non_inflight_state (tools.tests.chz_v2.test_core.CoreTests.test_restart_preserves_each_non_inflight_state) ... ok
test_retrieval_closed_order_remains_unknown (tools.tests.chz_v2.test_core.CoreTests.test_retrieval_closed_order_remains_unknown) ... ok
test_retrieval_recovery_no_new_codes (tools.tests.chz_v2.test_core.CoreTests.test_retrieval_recovery_no_new_codes) ... ok
test_symlink_artifact_and_path_traversal_rejected (tools.tests.chz_v2.test_core.CoreTests.test_symlink_artifact_and_path_traversal_rejected) ... ok
test_three_hashes_and_exact_signed_bytes (tools.tests.chz_v2.test_core.CoreTests.test_three_hashes_and_exact_signed_bytes) ... ok
test_timeout_after_acceptance_never_auto_retry (tools.tests.chz_v2.test_core.CoreTests.test_timeout_after_acceptance_never_auto_retry) ... ok
test_r1_01_generic_public_terminal_and_approval_bypass_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_generic_public_terminal_and_approval_bypass_rejected) ... ok
test_r1_01_incomplete_contradictory_wrong_type_evidence_fail_closed (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_incomplete_contradictory_wrong_type_evidence_fail_closed) ... ok
test_r1_01_manufactured_complete_rejection_has_no_fake_provenance (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_manufactured_complete_rejection_has_no_fake_provenance) ... ok
test_r1_01_previous_attempt_evidence_cannot_resolve_current_attempt (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_previous_attempt_evidence_cannot_resolve_current_attempt) ... ok
test_r1_01_wrong_operation_attempt_cannot_resolve_or_mark_unknown (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_wrong_operation_attempt_cannot_resolve_or_mark_unknown) ... ok
test_r1_02_direct_reservation_late_failure_rolls_back_all_items (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_direct_reservation_late_failure_rolls_back_all_items) ... ok
test_r1_02_nested_public_failure_savepoint_does_not_leak_reservation (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_nested_public_failure_savepoint_does_not_leak_reservation) ... ok
test_r1_02_public_control_and_journal_atomic_on_event_failure (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_public_control_and_journal_atomic_on_event_failure) ... ok
test_r1_02_synchronized_public_reservations_one_child_two_sets (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_synchronized_public_reservations_one_child_two_sets) ... ok
test_r1_03_direct_sql_attempt_requires_consumption_and_matching_approval (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_attempt_requires_consumption_and_matching_approval) ... ok
test_r1_03_direct_sql_enums_dangling_owner_and_singleton_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_enums_dangling_owner_and_singleton_rejected) ... ok
test_r1_03_direct_sql_terminal_without_evidence_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_terminal_without_evidence_rejected) ... ok
test_r1_03_old_schema_refused_without_migration (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_old_schema_refused_without_migration) ... ok
test_r1_03_sql_code_cannot_participate_in_two_issues (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_code_cannot_participate_in_two_issues) ... ok
test_r1_03_sql_initial_terminal_state_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_initial_terminal_state_rejected) ... ok
test_r1_03_sql_membership_unique_and_ownership_enforced (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_membership_unique_and_ownership_enforced) ... ok
test_r1_03_sql_replace_cannot_rewrite_immutable_committed_rows (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_replace_cannot_rewrite_immutable_committed_rows) ... ok
test_r1_03_sql_two_active_attempts_for_one_operation_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_two_active_attempts_for_one_operation_rejected) ... ok
test_r1_03_unknown_reservation_cannot_be_released_by_sql (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_unknown_reservation_cannot_be_released_by_sql) ... ok
test_r1_04_committed_composition_and_business_commit_immutable (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_committed_composition_and_business_commit_immutable) ... ok
test_r1_04_correlated_artifact_and_row_corruption_cannot_change_receipt (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_correlated_artifact_and_row_corruption_cannot_change_receipt) ... ok
test_r1_04_missing_and_partial_reservation_ownership_fail_commit (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_missing_and_partial_reservation_ownership_fail_commit) ... ok
test_r1_04_os_process_loss_inside_sqlite_commit_recovers (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_os_process_loss_inside_sqlite_commit_recovers) ... ok
test_r1_04_rowcount_mismatch_rolls_back_commit (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_rowcount_mismatch_rolls_back_commit) ... ok
test_r1_04_sql_null_missing_manifest_fields_fail_closed (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_sql_null_missing_manifest_fields_fail_closed) ... ok
test_r1_05_bad_evidence_preserves_trusted_external_id_and_history (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_bad_evidence_preserves_trusted_external_id_and_history) ... ok
test_r1_05_coordinator_wrong_returned_id_remains_unknown (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_coordinator_wrong_returned_id_remains_unknown) ... ok
test_r1_05_unknown_correlation_set_only_by_valid_recovery (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_unknown_correlation_set_only_by_valid_recovery) ... ok
test_r1_06_all_public_journal_string_fields_reject_sensitive_values (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_all_public_journal_string_fields_reject_sensitive_values) ... ok
test_r1_06_event_attempt_reference_must_belong_to_operation (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_event_attempt_reference_must_belong_to_operation) ... ok
test_r1_06_sql_unsafe_event_values_and_replacement_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_sql_unsafe_event_values_and_replacement_rejected) ... ok

----------------------------------------------------------------------
Ran 67 tests in 89.916s

OK
```

### Bundled Python 3.12.14

```text
test_all_seven_operation_types_terminal_evidence (tools.tests.chz_v2.test_core.CoreTests.test_all_seven_operation_types_terminal_evidence) ... ok
test_ambiguous_or_mismatched_retrieval_evidence (tools.tests.chz_v2.test_core.CoreTests.test_ambiguous_or_mismatched_retrieval_evidence) ... ok
test_artifact_crash_matrix_and_non_deliverable_orphan (tools.tests.chz_v2.test_core.CoreTests.test_artifact_crash_matrix_and_non_deliverable_orphan) ... ok
test_attempt_bytes_immutable_and_corruption_detected (tools.tests.chz_v2.test_core.CoreTests.test_attempt_bytes_immutable_and_corruption_detected) ... ok
test_attempt_separate_one_approval_one_submission (tools.tests.chz_v2.test_core.CoreTests.test_attempt_separate_one_approval_one_submission) ... ok
test_core_stdout_empty (tools.tests.chz_v2.test_core.CoreTests.test_core_stdout_empty) ... ok
test_corrupted_artifact_and_pointer (tools.tests.chz_v2.test_core.CoreTests.test_corrupted_artifact_and_pointer) ... ok
test_crash_after_http_before_journal (tools.tests.chz_v2.test_core.CoreTests.test_crash_after_http_before_journal) ... ok
test_crash_after_queue_reservation (tools.tests.chz_v2.test_core.CoreTests.test_crash_after_queue_reservation) ... ok
test_crash_before_submit_is_conservatively_unknown (tools.tests.chz_v2.test_core.CoreTests.test_crash_before_submit_is_conservatively_unknown) ... ok
test_duplicate_codes_and_extra_forbidden (tools.tests.chz_v2.test_core.CoreTests.test_duplicate_codes_and_extra_forbidden) ... ok
test_duplicate_process_stale_lock_and_concurrent_issue (tools.tests.chz_v2.test_core.CoreTests.test_duplicate_process_stale_lock_and_concurrent_issue) ... ok
test_environment_is_hard_boundary (tools.tests.chz_v2.test_core.CoreTests.test_environment_is_hard_boundary) ... ok
test_expired_approval_and_changed_hash (tools.tests.chz_v2.test_core.CoreTests.test_expired_approval_and_changed_hash) ... ok
test_filesystem_failures_leave_issue_non_deliverable (tools.tests.chz_v2.test_core.CoreTests.test_filesystem_failures_leave_issue_non_deliverable) ... ok
test_forged_prepared_identity_and_late_duplicate_blocked (tools.tests.chz_v2.test_core.CoreTests.test_forged_prepared_identity_and_late_duplicate_blocked) ... ok
test_identity_and_preview_immutable_no_code_leak (tools.tests.chz_v2.test_core.CoreTests.test_identity_and_preview_immutable_no_code_leak) ... ok
test_issue_commit_crash_and_idempotent_receipt (tools.tests.chz_v2.test_core.CoreTests.test_issue_commit_crash_and_idempotent_receipt) ... ok
test_journal_append_only_chain_and_redaction (tools.tests.chz_v2.test_core.CoreTests.test_journal_append_only_chain_and_redaction) ... ok
test_journal_corruption_blocks_execute (tools.tests.chz_v2.test_core.CoreTests.test_journal_corruption_blocks_execute) ... ok
test_journal_rejects_secret_in_allowed_field (tools.tests.chz_v2.test_core.CoreTests.test_journal_rejects_secret_in_allowed_field) ... ok
test_ledger_blocks_same_code_two_operations (tools.tests.chz_v2.test_core.CoreTests.test_ledger_blocks_same_code_two_operations) ... ok
test_lifecycle_and_operation_participation_history (tools.tests.chz_v2.test_core.CoreTests.test_lifecycle_and_operation_participation_history) ... ok
test_new_demand_duplicate_review_not_identity (tools.tests.chz_v2.test_core.CoreTests.test_new_demand_duplicate_review_not_identity) ... ok
test_no_real_inputs_or_adapters_or_home_paths (tools.tests.chz_v2.test_core.CoreTests.test_no_real_inputs_or_adapters_or_home_paths) ... ok
test_orphan_without_sqlite_issue_never_committed (tools.tests.chz_v2.test_core.CoreTests.test_orphan_without_sqlite_issue_never_committed) ... ok
test_partial_is_not_success_or_retry (tools.tests.chz_v2.test_core.CoreTests.test_partial_is_not_success_or_retry) ... ok
test_real_process_crash_releases_lock_keeps_reservation (tools.tests.chz_v2.test_core.CoreTests.test_real_process_crash_releases_lock_keeps_reservation) ... ok
test_rejection_requires_fresh_explicit_approval (tools.tests.chz_v2.test_core.CoreTests.test_rejection_requires_fresh_explicit_approval) ... ok
test_restart_during_reconciling_and_delayed_readback (tools.tests.chz_v2.test_core.CoreTests.test_restart_during_reconciling_and_delayed_readback) ... ok
test_restart_preserves_each_non_inflight_state (tools.tests.chz_v2.test_core.CoreTests.test_restart_preserves_each_non_inflight_state) ... ok
test_retrieval_closed_order_remains_unknown (tools.tests.chz_v2.test_core.CoreTests.test_retrieval_closed_order_remains_unknown) ... ok
test_retrieval_recovery_no_new_codes (tools.tests.chz_v2.test_core.CoreTests.test_retrieval_recovery_no_new_codes) ... ok
test_symlink_artifact_and_path_traversal_rejected (tools.tests.chz_v2.test_core.CoreTests.test_symlink_artifact_and_path_traversal_rejected) ... ok
test_three_hashes_and_exact_signed_bytes (tools.tests.chz_v2.test_core.CoreTests.test_three_hashes_and_exact_signed_bytes) ... ok
test_timeout_after_acceptance_never_auto_retry (tools.tests.chz_v2.test_core.CoreTests.test_timeout_after_acceptance_never_auto_retry) ... ok
test_r1_01_generic_public_terminal_and_approval_bypass_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_generic_public_terminal_and_approval_bypass_rejected) ... ok
test_r1_01_incomplete_contradictory_wrong_type_evidence_fail_closed (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_incomplete_contradictory_wrong_type_evidence_fail_closed) ... ok
test_r1_01_manufactured_complete_rejection_has_no_fake_provenance (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_manufactured_complete_rejection_has_no_fake_provenance) ... ok
test_r1_01_previous_attempt_evidence_cannot_resolve_current_attempt (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_previous_attempt_evidence_cannot_resolve_current_attempt) ... ok
test_r1_01_wrong_operation_attempt_cannot_resolve_or_mark_unknown (tools.tests.chz_v2.test_r2.R2Tests.test_r1_01_wrong_operation_attempt_cannot_resolve_or_mark_unknown) ... ok
test_r1_02_direct_reservation_late_failure_rolls_back_all_items (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_direct_reservation_late_failure_rolls_back_all_items) ... ok
test_r1_02_nested_public_failure_savepoint_does_not_leak_reservation (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_nested_public_failure_savepoint_does_not_leak_reservation) ... ok
test_r1_02_public_control_and_journal_atomic_on_event_failure (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_public_control_and_journal_atomic_on_event_failure) ... ok
test_r1_02_synchronized_public_reservations_one_child_two_sets (tools.tests.chz_v2.test_r2.R2Tests.test_r1_02_synchronized_public_reservations_one_child_two_sets) ... ok
test_r1_03_direct_sql_attempt_requires_consumption_and_matching_approval (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_attempt_requires_consumption_and_matching_approval) ... ok
test_r1_03_direct_sql_enums_dangling_owner_and_singleton_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_enums_dangling_owner_and_singleton_rejected) ... ok
test_r1_03_direct_sql_terminal_without_evidence_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_direct_sql_terminal_without_evidence_rejected) ... ok
test_r1_03_old_schema_refused_without_migration (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_old_schema_refused_without_migration) ... ok
test_r1_03_sql_code_cannot_participate_in_two_issues (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_code_cannot_participate_in_two_issues) ... ok
test_r1_03_sql_initial_terminal_state_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_initial_terminal_state_rejected) ... ok
test_r1_03_sql_membership_unique_and_ownership_enforced (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_membership_unique_and_ownership_enforced) ... ok
test_r1_03_sql_replace_cannot_rewrite_immutable_committed_rows (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_replace_cannot_rewrite_immutable_committed_rows) ... ok
test_r1_03_sql_two_active_attempts_for_one_operation_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_sql_two_active_attempts_for_one_operation_rejected) ... ok
test_r1_03_unknown_reservation_cannot_be_released_by_sql (tools.tests.chz_v2.test_r2.R2Tests.test_r1_03_unknown_reservation_cannot_be_released_by_sql) ... ok
test_r1_04_committed_composition_and_business_commit_immutable (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_committed_composition_and_business_commit_immutable) ... ok
test_r1_04_correlated_artifact_and_row_corruption_cannot_change_receipt (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_correlated_artifact_and_row_corruption_cannot_change_receipt) ... ok
test_r1_04_missing_and_partial_reservation_ownership_fail_commit (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_missing_and_partial_reservation_ownership_fail_commit) ... ok
test_r1_04_os_process_loss_inside_sqlite_commit_recovers (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_os_process_loss_inside_sqlite_commit_recovers) ... ok
test_r1_04_rowcount_mismatch_rolls_back_commit (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_rowcount_mismatch_rolls_back_commit) ... ok
test_r1_04_sql_null_missing_manifest_fields_fail_closed (tools.tests.chz_v2.test_r2.R2Tests.test_r1_04_sql_null_missing_manifest_fields_fail_closed) ... ok
test_r1_05_bad_evidence_preserves_trusted_external_id_and_history (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_bad_evidence_preserves_trusted_external_id_and_history) ... ok
test_r1_05_coordinator_wrong_returned_id_remains_unknown (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_coordinator_wrong_returned_id_remains_unknown) ... ok
test_r1_05_unknown_correlation_set_only_by_valid_recovery (tools.tests.chz_v2.test_r2.R2Tests.test_r1_05_unknown_correlation_set_only_by_valid_recovery) ... ok
test_r1_06_all_public_journal_string_fields_reject_sensitive_values (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_all_public_journal_string_fields_reject_sensitive_values) ... ok
test_r1_06_event_attempt_reference_must_belong_to_operation (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_event_attempt_reference_must_belong_to_operation) ... ok
test_r1_06_sql_unsafe_event_values_and_replacement_rejected (tools.tests.chz_v2.test_r2.R2Tests.test_r1_06_sql_unsafe_event_values_and_replacement_rejected) ... ok

----------------------------------------------------------------------
Ran 67 tests in 89.968s

OK
```

STOP: R2 deliverables предъявляются владельцу. Ждать отдельного OWNER ACK.
