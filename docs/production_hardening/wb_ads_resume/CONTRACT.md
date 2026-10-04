# WB Ads bounded resume v1 — local implementation contract

Status: LOCAL IMPLEMENTATION, NOT DEPLOYED. Owner ACK 2026-10-01 permits local source/tests/artifacts only.
Base HEAD: b94ca335b98ce8146f925295f45d8789db5ed46c. No live correctness claim.

## Boundary and activation

Runtime: existing ingestion Apps Script, Google Advanced BigQuery service and existing WB token helper.
Project is fail-closed to `project-fa311fc0-4d87-4781-986`, dataset `wb_raw`, location `EU`, BQ ads sink on.
No migration, new service account, credential handling, marketplace mutation, mart/Unitka/LCD change.

Default is OFF: Script Property `WB_ADS_RESUME_ENABLED` must equal `1`.
`WB_ADS_RESUME_CONTINUATION_ENABLED=1` separately permits creation of a single one-shot continuation.
Neither property nor any trigger was set by local implementation. Legacy dispatch is unchanged with flag OFF.
Daily/catch-up with flag ON first resumes the pinned period. A BLOCKED period blocks new daily recovery;
it is not silently replaced by D−1. One active recovery globally is stricter than one chain per logical date.

## Manifest

`wb_raw.ADS_FULLSTATS_RECOVERY` is a new operational table (DDL proposal in schema.sql).
Rows are append-only. `kind=MANIFEST` holds immutable revisions, `kind=BATCH` a single durable prepared response.
Record key is recovery ID for manifests and deterministic batch ID for batches. SHA-256 validates payloads.
BigQuery job IDs are deterministic for manifest revision, landing batch, each RAW publication and final heartbeat.
Runtime uses CREATE_NEVER and does not deploy schema. Missing/mismatched ledger stops before writes.

Manifest payload:

* version, recovery key, revision, creation/update timestamps, state, attempts;
* immutable scope: logical date, from/to, parent INGEST_RUNS ID, origin RAW run ID, DAILY/RECOVERY;
* sorted unique campaign snapshot, campaign hash, immutable allowlist, scope hash;
* campaigns/costs evidence receipt and imported, proven per-ID coverage;
* batches of up to 50 IDs: deterministic ID, attempts, state, classifications, sink receipt, safe error code;
* counts: processed/no_stats/pending/failed/skipped; completion predicate;
* continuation needed/reason, consecutive no-progress count;
* final heartbeat ID and lineage; optional-stage admission marker for DAILY only.

Script Property `WB_ADS_RESUME_ACTIVE_V1` stores only active key and scope (at most 8 KB; identical
allowlist is omitted). It is written before initial manifest submission. If interrupted, resume resolves
the deterministic initial manifest job or reconstructs from verified origin evidence. BQ is the durable
progress store. ScriptLock serializes pointer, manifest, publication and trigger reconciliation.
Manifest initialization retries do not silently override conflicting initial job contents.

Input limits: nonempty snapshot, at most 2,000 safe integer campaign IDs, maximum 31 inclusive dates,
`to == logicalDate`, closed logical date only. The 8 KB active-pointer limit can be stricter than 2,000 IDs;
oversized input stops before registering the pointer. Prepared payload maximum 5 MB, RAW batch fewer than
10,000 rows per destination. These are safety bounds, not economic thresholds.

## Historical entrypoint

`resumeWbAdsFullstatsRecovery(input)` accepts originRunId, parentRunId, logicalDate, from, to,
campaignIds, optional campaignHash and allowlist. It rejects DAILY impersonation. Subsequent invocations
may use `runWbAdsFullstatsContinuation()` to derive pending work from the manifest without resupplying IDs.
The caller cannot supply phase success, pre-covered IDs, sink receipts or COMPLETE.

Historical bootstrap verifies:

1. Exact parent heartbeat is ERROR/ADS_PARTIAL for the supplied ads logical period/source.
2. Existing parent summary has campaigns=OK and costs=OK. No ERROR row is modified.
3. Origin campaign row count matches the summary; all campaign load timestamps are within that parent
   execution (RAW timestamp resolution has 1-second tolerance); status 7/9/11 snapshot matches the input.
4. Exactly one successful costs window covers the logical date, has zero out-of-window rows, and its
   returned_rows matches RAW readback for that origin/window.
5. Prior fullstats no_stats markers for the exact origin/window are unique and inside the snapshot.
   Other legacy raw IDs are NOT imported as complete: absent a batch receipt, a few rows cannot prove
   complete per-ID coverage. They must be in the allowlist and fetched anew.
6. Every uncovered ID is inside the allowlist. Omitted IDs cannot become no_stats.

Legacy parent/origin linking is temporal plus summary/RAW evidence, since legacy heartbeat has no
origin column. Exact IDs, times and absence of ambiguous overlapping legacy runs must be reviewed
before production bootstrap. This is not an instruction to edit the old heartbeat.

## Batch state and ambiguity

PENDING/FAILED → durable prepared BATCH → PREPARED → RAW readback → DONE.

Before requesting WB, stage looks for an existing prepared record and/or landing job. No re-fetch is
allowed for PREPARED. Responses must be a valid array (or data array); campaign IDs/dates/nested shape
are checked. Missing IDs become legitimate no_stats only after a valid successful response for that
requested batch. A network error, invalid JSON or budget stop never produces no_stats.

Prepared payload contains normalized rows from the existing fullstats flattening code, explicit
classifications and frozen load timestamps. No credentials are stored. Publication writes only those
rows to RAW_WB_ADV_CAMPAIGN_STATS / RAW_WB_ADV_BOOSTER_STATS. There is no pre-delete, UPDATE, MERGE,
campaigns refresh or costs refresh in the historical recovery path.

Before each append, RAW is checked by deterministic batch run_id and dates. After append, normalized
full-row content is compared with the prepared payload, including raw_json. Successful load job status
and outputRows are additionally checked. Receipt is not granted for partial or conflicting content.
Crash after RAW load but before checkpoint: use same prepared payload; discover existing RAW/job;
confirm instead of appending again. This also works after job metadata expiry while durable RAW exists.
An indeterminate submission is not replaced by a random new job ID. Collision/error is fail-closed.

Business dedupe remains `(date, advertId, nmId, appType, source_level)` with latest load_ts/run_id and
processed_status=raw. No new view definitions and no invocation of wbAdsBqCreateViews.
Recovery may revise effective values in its approved historical window; no_stats is not a tombstone.

## Pre-publication RAW grain guard — local safety fix, 2026-10-01

The exact existing `V_ADV_CAMPAIGN_STATS` partition key is
`date, advertId, nmId, appType, source_level`, for `processed_status = 'raw'`.
Evidence: `Wbadsbigquery.gs` makeView / V_ADV_CAMPAIGN_STATS definition, and
`WbAdsRawLoader.gs` wbAdvFlattenFullstats_ destination mapping. This fix does not change either.
Keys use the existing RAW STRING representation; omitted/null/empty fields become SQL NULL
under the existing adapter normalization. Dates are not truncated or reinterpreted.

Every complete `prepared.stats` array is checked before the core calls `io.publish`, and again
at the adapter's publish entry before either RAW destination is accessed. This includes durable
prepared payloads recovered from a previous execution. The array combines all campaigns/days/apps/nm
portions of the source response, so validation is not reset at nested boundaries or row chunks.
A recovery batch is one prepared publication unit: at most one stats array and one separate booster
array. The stats business key must not be applied across different destination tables. Different
batches have disjoint advertIds (manifest validator), a component of this key.

Any duplicate key is rejected, including exact and semantically identical duplicate business rows.
The repository defines latest-wins across RAW history, but does not authorize duplicate keys within
one prepared publication. Identical duplicates might leave the same selected business value, yet
this does not prove a valid producer payload or safe row-count/idempotency semantics. Therefore
there is no deduplication, aggregation, winner selection or source-response rewrite.
The existing no_stats marker rows are excluded, matching the view predicate exactly.

Rejection code: `RAW_GRAIN_DUPLICATE` (invalid/missing stats array: `RAW_GRAIN_PAYLOAD`).
The batch becomes FAILED, the manifest becomes BLOCKED, continuation.needed=false; batch.error and
continuation.reason retain the safe code. The durable BATCH record remains available for inspection.
No RAW writer/finalizer is invoked for the rejected payload, no DONE or COMPLETE is issued.
This is fail-closed terminal handling of an immutable invalid payload, not an automatic cleanup or
unblock. Previously written valid batches are retained; this fix does not roll them back.
If checkpoint persistence itself fails, no RAW publication has happened and the same durable payload
is revalidated on retry. No production deployment or recovery is implied by this contract update.

## Completion and consumers

COMPLETE requires successful verified campaigns/costs, every snapshot ID processed or legitimate
no_stats, pending=failed=skipped=0, and confirmed receipts for all new batches. A DONE label alone
is insufficient. Before finalizing, latest same-date Apps Script ads attempt must still be the parent
or this recovery's deterministic final heartbeat. A newer unrelated attempt blocks completion.

A NEW terminal INGEST_RUNS row is inserted idempotently, then read back:
`INS_ADS_RESUME_<scope hash>`, loader ads, source apps_script, trigger_type CATCHUP, same logical date,
status COMPLETE, non-null completed_at. started_at is the final validation execution time, so the
latest-attempt consumer observes the newly proven attempt. Existing parent ERROR/PARTIAL is immutable.
rows_fetched/rows_loaded use campaigns + costs + fullstats data row counts (no no_stats markers or
booster duplication), consistent with daily summary accounting. Completion lineage lives in the
new manifest, not an error_message on a successful heartbeat. Existing INGEST_RUNS schema is unchanged.

This is an explicit producer-contract extension: a successful attempt can be a validated composite of
origin phases and durable fullstats batches. Unlike existing logger STARTED→terminal transitions, this
final row is inserted terminal only AFTER all evidence exists. The original failed attempt stays failed.

Consumer compatibility:

| Consumer | Behavior |
|---|---|
| INGEST_RUNS / V_INGEST_HEARTBEAT | Existing columns/enums; a new attempt, never rewriting ERROR |
| mart checkFreshness | Latest started_at/run_id for exact date; still requires COMPLETE + completed_at |
| ads status whitelist | Unchanged; PARTIAL remains ERROR; no force-complete branch |
| WB_ADS_STATUS | Legacy OFF path unchanged. ON path uses structured logs + durable journal, no Sheet append |
| effective fullstats view | Same RAW fields, grain, status filtering and latest-wins semantics |

WB_ADS_STATUS alone is therefore not a complete monitor after activation. Observers must include
WB_ADS_RESUME events / journal; this monitoring change requires review before activation.

## Deadline and continuation

Execution finish target = start + 330 s (30 s before platform hard wall). Source requests stop another
90 s earlier. A batch starts only with >120 s remaining. Default at most 2 completed batches/execution;
core validates any supplied bound in 1..4. HTTP attempts max 3, 30 s admission allowance, Retry-After
and backoff checked against absolute deadline before sleeping/retrying. BQ polls also check deadline;
query jobs use EU, Standard SQL and a 100 MB billing cap. No unbounded polling.
The last fullstats request time is persisted in `WB_ADS_RESUME_LAST_HTTP_AT` so a manual/duplicate
execution also respects the 21-second cooldown across execution boundaries.

The native Apps Script HTTP/Advanced Service calls cannot be interrupted mid-call by JavaScript.
Reserves reduce risk; durable deterministic jobs handle an actual platform kill. No claim of an absolute
wall-clock guarantee is made for a stalled native call. Campaigns/costs keep their semantics, but enabled
path uses bounded BQ load and metadata-only schema checks rather than automatic schema extension.

At most one own one-shot continuation is kept, under ScriptLock, only for pending/READY work. Terminal
COMPLETE/BLOCKED deletes only own continuation triggers. Other handlers are untouched. Three consecutive
no-progress attempts or more than five admissions for one batch stop the chain at BLOCKED. Progress
includes a newly durable prepared response, not merely a retry. Hard-kill attempts are persisted before I/O.
No automatic unblock/reset is provided. Diagnosed corrections/new approval are required for BLOCKED.

Optional query bids/stats are admitted only after durable COMPLETE, only for DAILY/current D−1, at most
once and with spare budget. Historical recovery never runs them. The admission marker is saved before
execution; crash after admission is reported as ambiguous, not automatically replayed. Optional errors
cannot alter the successful mandatory heartbeat. A late completion may miss the nonhistorical bids
snapshot: this existing data limitation is visible and not disguised as reconstructed history.

## Observability and limitations

Events carry origin/parent/recovery IDs, campaign count/hash, logical date, batch ID, safe error code,
coverage, state, elapsed/remaining time, continuation reason and no-progress count. No source payload,
request URL/header, token or raw exception is logged by resume. Alert routing is not activated here.
Monitor BLOCKED, no progress, missing next execution and aging pinned dates. Evidence table contains
prepared business payloads and must retain the same access controls as wb_raw.

No production IAM/schema/Apps Script source parity or live round-trip was tested in this local task.
No guarantee of delivery of Google time triggers; existing daily/catch-up handlers also pick up pending
work. Origin phases interrupted before manifest initialization still need normal loader retry/reaper;
this change persists fullstats progress, not a new general-purpose campaigns/costs workflow.
All supplied caps fail closed; changing them requires reviewed implementation/config changes.
There is no general multi-date backlog queue: the active fullstats period is pinned, and new daily
initialization waits until it is complete. Missed periods that never had a manifest require separate
bounded recovery assessment. This implementation does not invent successful coverage for them.
