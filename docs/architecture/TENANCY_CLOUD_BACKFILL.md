# Durable tenant backfill: storage and authority foundation

## Status and authority (2026-10-06)

LOCAL CANDIDATE. This is not a deployed controller, approved full-history plan,
qualified controller image or evidence of unattended execution. PR252 is an
unmerged dependency. No cloud resource or tenant data was changed to install
this foundation. Full-history publication deliberately fails closed until a
qualified gate adapter exists.

The owner ACK of 2026-10-06 authorizes engineering and minimum dedicated,
tenant-scoped orchestration IAM, superseding the no-new-IAM restriction in the
2026-10-05 scope. It does not authorize broadening existing provisioner,
runtime, control, GitHub or legacy EVETIS identities. Ordinary tenant Schedulers
remain PAUSED. Existing source and binding contracts remain authoritative.

## Implemented local foundation

`tools/tenancy/durable_plan.py` uses existing BACKFILL_CHECKPOINTS rows to retain
bounded canonical JSON records: MANIFEST, DISPATCH_INTENT, DISPATCH_RECEIPT,
RECONCILED, WAITING and STOPPED. Root identity includes source SHA, immutable
runtime image, canonical child plans, purpose and UTC creation timestamp.
Every record is addressed and verified by its full SHA256, not by a local path.

`tenant_locks.BFQ_<root>_<sequence>_<kind>` is a consistent create-if-absent
fence. It elects one exact dispatch intent before append or any eventual
Cloud Run POST. A conflicting run ID cannot win the same sequence. A crash
between fence and append permits only identical publication recovery; it must
never produce a replacement run or repeat an uncertain POST.

`tenant_locks.BFR_<record>` commits a record only after SELECT readback verifies
its full JSON digest. Streaming visibility delay is not absence of evidence.
Identical duplicate inserts collapse; contradictory evidence stops the tick.
Marker descriptions retain full hashes; labels contain short lookup prefixes
only, respecting the BigQuery label length limit. Marker/table/partition
retention must be checked explicitly before activation; default expiry is not
assumed safe. No evidence is truncated to meet the 900000-byte guard.

`history(root)` reconstructs committed records from cloud metadata and scoped
SELECT. No local receipt, gcloud process or Mac state is needed by the storage
adapter. The pure tick verdict handles active execution, missing receipt,
reconciliation-before-next-dispatch, quota waiting and sticky security stop.
It does not itself call a marketplace or start a job.

## Cloud authentication and isolation

`tools/tenancy/cloud_access.py` uses the Cloud Run metadata email/token endpoints
and verifies the exact dedicated controller service identity before obtaining
its token. It does not read local OAuth, credential files, environment API
keys or Secret Manager payloads. Cloud Run documents these endpoints at
https://docs.cloud.google.com/run/docs/container-contract#metadata-server.

Reader requests are restricted to tenant BigQuery metadata/data, EU Standard
SQL SELECT, exact tenant Run metadata and Scheduler inventory. SQL is parsed
using the repository-pinned sqlglot dependency. Multiple statements, DDL/DML,
foreign or wildcard tables, indirect sources, unreviewed UDFs and destination
materialization are denied before obtaining a token. One verification query
has a 1GiB maximumBytesBilled ceiling. This is an execution safety ceiling,
not a business threshold.

A separate dedicated append identity receives a delegated 600-second token
scoped to BigQuery. Only insertAll to the three existing orchestration evidence
tables and bounded canonical marker creation can use that token. The append
identity has no jobs.create, RAW/ref writes, schema updates, job invocation or
secret access. The reader cannot append or create tables. HTTP redirects and
automatic mutation retries are forbidden. Error messages contain no credential
or server response payload.

`tenant_tables.Tables` accepts separate reader/write request adapters.
`tenant_backfill.select` accepts a reader request adapter. Existing owner tools
retain their previous defaults; cloud code need not replace module-global
credentials or invoke gcloud. Offline integration tests deny owner fallback and
prove record recovery after constructing a fresh cloud client.

## Proposed exact permission boundary

The machine-readable principal/permission/resource matrix is produced by
`tools.tenancy.orchestration_identity.matrix(tenant)` and checked for exact
equality by `validate_matrix`. It is a proposal, not an effective-IAM readback.
Account names below are dedicated and all resources belong to the registry
project for that tenant.

| Dedicated principal | Permission | Resource |
|---|---|---|
| sa-backfill-controller | bigquery.tables.get/getData | tenant ozon_raw, tenant_ops datasets |
| sa-backfill-controller | bigquery.tables.get/getData/list | tenant ref dataset |
| sa-backfill-controller | bigquery.tables.get/list | tenant_locks dataset |
| sa-backfill-controller | bigquery.jobs.create | tenant project |
| sa-backfill-controller | run.jobs.get/run/runWithOverrides; run.executions.get/list | exact three canonical runtime jobs |
| sa-backfill-controller | run.jobs.list; run.operations.get; cloudscheduler.jobs.list | tenant project |
| sa-backfill-controller | iam.serviceAccounts.getAccessToken | dedicated sa-backfill-append only |
| sa-backfill-append | bigquery.tables.updateData | BACKFILL_CHECKPOINTS, DATA_COVERAGE, DQ_RESULTS individually |
| sa-backfill-append | bigquery.tables.create | tenant_locks dataset only |
| sa-backfill-wake | run.jobs.run | dedicated tenant-backfill-controller job only |

No standard broad TokenCreator role is used. No signJwt, signBlob or implicit
delegation, no invocation of tenant-control, no existing-SA delegation and no
Secret Manager permission is proposed. The query reader cannot create tables;
this prevents pairing jobs.create with table creation to evade append-only
storage. Dataset grants must join the canonical authoritative Terraform ACL,
not out-of-band IAM members that a later apply would remove. No IAM change may
be applied without the exact generated resource matrix, effective-binding
readback, CI, image qualification and reviewed tenant-only plan.

## Quota and recovered-generation invariants

The conservative budget is 15 exports per rolling24hours. Reservations are
durable report intents, deduplicated by plan ID/sequence; conflicting counts or
unknown ordinary exports stop work. Repeat acknowledgements conservatively
extend expiry. WAITING is not an Ozon quota rejection or permanent PARTIAL.
A known POLL/download may finish with zero new-export allowance. Ambiguous
REPORT_INTENT cannot become a second POST.

The accepted client_001 SKU generation retains 15 completed and 75 pending
campaigns. Its immutable runtime plan/image and ACK were reconstructed from
cloud journal/checkpoint/execution evidence. This proves recoverability, not
permission to run it using different application code. Its implementation hash
differs from current PR252 because the qualified application changed. No
silent hash substitution, generation reset, completed-result discard or new
remaining-cohort plan merely for convenience is permitted.

## Remaining activation work (not implemented by this foundation)

- Registered dedicated controller/template/Scheduler contract and Terraform
  integration, including authoritative ACL and exact plan-scanner support.
- Qualified cloud entrypoint using canonical preflight/start/reconcile and
  explicit registration of its own job/Scheduler. Unknown resources must not
  be hidden from current strict inventory checks.
- Durable before-dispatch intent and after-dispatch receipt hooks, recovery
  of an uncertain Run POST by exact run ID, and scope lease reconciliation.
- Reviewed compatibility with the retained qualified SKU image/generation;
  current plan validation must not be bypassed.
- Successful PR252 CI, immutable probe image build/qualification/release,
  reviewed tenant deployment and bounded corrected discovery.
- Deployment/readback of minimum dedicated IAM and controller image; prove
  restart recovery with the Mac/Codex offline before full GO.
- Remaining SKU cohort, Supplies scale/state-growth proof, progressive DQ,
  safe snapshot adapters and canonical durable11-domain full-history plan.
- Full-history gate adapter and canonical lifecycle approval. Until these
  gates pass, MANIFEST purpose FULL_HISTORY remains rejected.

Local tests cannot substitute for any of these live/release gates. A Python
module or valid Terraform source is not a deployed/current production fact.

## Data semantics preserved

FBO first confirmed source activity is2023-03-23. Finance2023-03-23 is a sampled
lower bound, not proof of absolute earliest activity. Performance expense first
confirmed activity is2023-03-24; tested2023-03-10..23 was empty. Freeze conservative
queryable boundaries from durable evidence without inventing earlier EMPTY.

Finance COMPLETE means traversal complete as-of observation; economic status
remains PROVISIONAL. Keep30-day refresh and explicit older REOPEN. Type84 source
label is «Дополнительная упаковка на складе Ozon». Its P&L mapping remains UNKNOWN;
this alone does not block RAW traversal. Snapshot-only domains must not acquire
fictional historic coverage. Historical trial data is preserved; this task is
not trial cleanup, Seller policy redesign or identity/binding migration.
