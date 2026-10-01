# Reviews & Q&A publication remediation — 2026-10-01

Status: implementation candidate; no deployment authorized. Production is unchanged.

## Evidence and scope

Accepted forensic baseline: production revision `evetis-wb-communications-00034-7v4`,
source `3e0466f597a362fd4972df9c8e58be24c2cb8c4d`, digest
`sha256:dcc5ca5f53c666bb5d220d985c5f93337a4f309530108fbaf27e0f7b3c45a7ef`.
Fresh main base `437536f49c9454ca4b1492d7b27727a7c06d75cd` includes PR #232
(`4bdccd7c1e6b165a09a6e5f20840e5e496c5655e`), which production does not contain.
The forensic example initially associated with the burning review belonged to
another review whose WB answer matched. The architectural defects below are
independent of that association.

Root causes: review POST acceptance immediately set local published/Telegram
success without read-back; the deterministic verifier ran only in shadow or an
optional manual-only gate; old pending drafts bypassed current policy on publish.

## State and policy contract

Before: operator → POST → HTTP acceptance → published.
After: operator → atomic lease → current policy → feedback GET → at most one
possibly delivered POST → bounded GET verification. Only matching identity,
whitespace-normalized answer and `wbRu` state permit `published` with `verified_at`.
This is a conservative visibility criterion, not a claim about undocumented WB
moderation transitions. Missing/unknown states remain unconfirmed.

A matching existing answer permits no POST; a different public answer becomes
`answered_externally`, never overwritten. Acceptance with no proof becomes
`publish_accepted`; ambiguous send/read becomes `publish_unknown`. A durable
`feedback_write_intent` is written transactionally before network I/O. Expired
publishing leases and uncertain intents cannot resend. Definite rejection alone
clears the intent. Existing client retries remain limited to proven pre-send
connection errors and explicit throttling; post-send 5xx/timeouts use read-back.

`validate_for_publication` loads the validated immutable snapshot selected by the
running revision on every operator attempt, resolves product identity, and invokes
only the deterministic verifier. Current means that revision's selected snapshot,
not the draft's generation version or a remote mutable policy. AI, regenerated,
manual and old cards all pass the same gate. Unresolved identity, restricted
components, BLOCK, invalid result or verifier failure prevent WB write. Policy
version, snapshot/hash, text hash and sanitized violations are recorded. Shadow
flags/cache/LLM are never consulted; auto_publish must remain false. The legacy
manual gate env setting remains parseable but does not disable this mandatory gate.
No safety policy, templates, claims or severity calibration changes.

## Reconciliation, storage and UX

Feedback reconciliation runs in the existing natural poll, read-only towards WB:
max 5 claimed documents / 10-second soft budget, one GET each with remaining
request timeout. Metadata enumerates up to 100 candidates per pending state;
within that window least recently checked comes first. This is a bounded backlog
window, not a throughput guarantee for arbitrarily large queues. Active leases
are skipped. Legacy published documents are included only when they reappear in
unanswered ingestion; no mass migration. Null never initiates an automatic POST.

Firestore uses additive schemaless fields and two string statuses (`policy_blocked`,
`policy_check_failed`). Existing documents need no migration or index. Every feedback
state write is fenced by token and unexpired lease in a transaction; memory tests
exercise the shared patch validator. Firestore transaction behavior is not validated
against live production. Existing BQ schema is unchanged: compact trace goes into
existing event payload JSON. Unchanged periodic checks overwrite a compact last
check instead of indefinitely appending identical trace/events.

Telegram acceptance/unknown is neutral and removes the publish button. BLOCK says
“Ответ создан по устаревшей политике и требует обновления”, with regenerate/edit/skip.
Edits and regeneration require policy validation again. Only verified reads show
“Опубликовано — ответ подтверждён на Wildberries”. Trace `telegram_state` records
the intended UI state; it is not a delivery receipt. Telegram/BQ/outbox failures can
lag persisted state, as in the existing question architecture. No customer callbacks
or WB publications were exercised during implementation.

## Verification and intentional test changes

Seven initial tests failed before implementation: FPUB-01, FPUB-02, three stale
sources (FPUB-14/16/17), verifier exception (FPUB-18), unknown identity (FPUB-19).
They then all passed. Full suite includes FPUB-01–23, question and v3 regression,
logging/security, real HTTP transport faults, expired leases, duplicate callbacks,
malformed GET, absent answer state, exact stale document IDs as offline fixtures,
manual/regeneration lifecycle and real mandatory gate on questions.

Intentional old → new expectations:
- Review 204 alone → published: now GET/POST/GET and verified_at are required.
- Reviews excluded from the question reconciler: still true; separate feedback
  reconciliation is now invoked by poll, with dedicated regression coverage.
- Optional manual gate off → bypass: now all sources are validated independently
  of that flag. Transport-oriented existing tests inject an explicit fake PASS;
  dedicated policy tests use the real adapter and current snapshot.
- WBClient raw response return: now acceptance metadata (HTTP code/hash/response)
  is returned without interpreting it as publication proof.

Local suite: 639 passed (Python 3.14; pinned Python 3.12 validation required in CI).
Compileall, offline registry validation and verify-snapshot pass. Snapshot
`ks_v3_20260928T120346_4f4121d4`, policy `v3.policy.2026-09-28.1`, content hash
`66eec390a5af76b80a1ace26bd97b58b41d4ce621117c53540a82447c071215a` unchanged.
Impact: TIER3, communications_current/communication_events, existing package gate.

## Release boundary and rollback

One PR, merge only after green CI. Build the exact merge source and identify image
by immutable digest. Before deployment compare live revision and full env fingerprint,
run deploy/preflight_env.py with the live env snapshot, and present the manifest.
No env, dependency, schema, policy snapshot or stored business-data migration delta.
ODR-07 prompt hotfix already in main ships as defense in depth.
Do not deploy or invoke /poll/callbacks/publication without separate owner ACK.

Rollback traffic target: `evetis-wb-communications-00034-7v4`. This restores the old
unverified publication path and pre-hotfix prompt; rollback is not a safety fix.
Any emergency publication-disable env change requires its own explicit scope.
Do not delete additive fields or backfill historical answers during rollback.

## Backlog — deliberately not implemented

Mild discomfort / burning public-response calibration.
