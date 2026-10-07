# Bounded pre-source failure recovery — owner ACK 2026-10-07

## Scope and observed incident

The owner authorized only the minimum CAPABILITY_PROFILE read grant, generic
pre-intent checks, preserved failure recovery, new qualified images, and continuation
of accepted cloud-native qualification. Ordinary Schedulers remain PAUSED.

The registered PR269 runtime image ended execution `ozon-runtime-daily-nmjzg` with
exit 1. Query `c4825b78-8aad-4093-bb00-edb9b60e0c75` was denied to the expected
runtime service account while reading CAPABILITY_PROFILE. Exact old Git source
places that SELECT before the report POST. Unit15 was an INTENT, without UUID,
submission or new SKU writes. Completed30/90 remained reconciled. This is an
internal dependency failure, not an Ozon quota rejection.

## Minimum IAM

`sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com`
→ custom role `runtimeCapabilityRead` containing only `bigquery.tables.getData`
→ table `mpa-t-client-001.tenant_ops.CAPABILITY_PROFILE`.

Live metadata confirms TABLE/EU, no view or column policy tags. Existing project
jobUser supplies jobs.create. No dataset-wide metadata/data role is added; no
provisioner permission changes. Role creation/binding is owner bootstrap, not a
new delegation to the canonical provisioner. Plan scanner rejects foreign table,
principal, write permission, conditions and unknown IAM scope.

## Generic contract

Internal binding/credential/CAPABILITY_PROFILE/current-day Catalog dependencies
are checked before a new report intent; the existing rolling-budget read follows
before intent publication. Submission-time binding/credential rechecks remain.
`TENANCY_INTERNAL_PREFLIGHT=1` on the qualified canonical SKU runtime performs
only internal reads, no source calls, secret reads or business/journal writes.
It does not authorize ingestion or weaken binding/security gates.

An exact owner audit may attest only CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST.
The audit verifies registered historical source/image, terminal failed operation,
expected runtime identity, exact denied query, original immutable source state,
zero source calls, no UUID/new source unit, zero run-attributed SKU rows and persisted
prefix reconciliation. It reads source from the exact historical commit; absence
of a UUID alone is insufficient proof.

The owner publisher creates an immutable `ref.BFP_<proofhash>` metadata marker
and appends a durable FAILED_PRE_SOURCE record. Runtime/controller can only read
ref; cloud append identity cannot forge this owner authority. Original intent,
failed aggregate, receipt, STOP, reservations and business rows are not rewritten.
Recovery is not RECONCILED/DONE. A new lease CAS generation and source unit record
PRE_SOURCE_RECOVERED precede a new intent. Any new/unreferenced STOP or ambiguous
submission continues to block execution.

Quota accounting preserves historical reservation10 but classifies that exact
attested unsubmitted attempt as not charged. The null-plan FAILED aggregate is
excluded only by its exact verified run/receipt; other unknown exports still
block. The accepted25/+10-per-reconciled-ten guard and frozen root/plan/origin,
cohort≤10, reviewed budgets and source throttling semantics are unchanged.

## Release and rollback

This document describes candidate code. It is not evidence of deployed IAM,
images, recovery publication,90/90, READY, or FULL_HISTORY GO. Record actual PR/CI,
immutable build/digest qualification, reviewed tenant plan and independent cloud
readback before any claim. Never apply an intermediate artifact-staging revision.

Use a deployment pause of only the dedicated controller when necessary to prevent
an intermediate image/recovery state generating an unrelated new STOP; restore
its reviewed ten-minute cadence only after readback and recovery publication.
Ordinary daily/fast/weekly schedules stay PAUSED. On uncertain new submission,
stop source progress; preserve intent/receipt/lease evidence. Rollback is not a
blind old-image restoration after new state, nor deletion of historical rows.

After actual reconciled90/90, the owner's already accepted corrected current-main
probe/release/discovery/Supplies/full-plan path remains subject to its separate
live GO gates. Qualification completeness never implies full-history activation.

## Artifact staging (not deployed)

The runtime candidate is built from the exact reviewed PR271 source commit, not represented
as an already merged main commit. The accepted source and actual image hashes are explicit.
Intermediate branch revisions omitted controller opt-in only to build/qualify its next
immutable artifact without a circular digest dependency. They were never applied.
Qualified controller source `8baef573538cba8ae5ab640f7b8030a317d52fc5` passed source CI `37592378220` and packaged image
checks. Final integration restores the original root and ENABLED ten-minute desired
Scheduler contract; deployment still requires final CI/merge and the reviewed owner
PAUSED stage followed by audited recovery and a separate activation plan.
Source commits are reviewed PR artifacts, not claimed main-at-build or live evidence.

## Owner publication and deployment ordering

Owner recovery publication uses a separate read-only observation preflight, with no
controller execution identity and no active runtime/control/controller execution.
The normal cloud preflight still requires its own visible registered execution.

The reviewed staging plan derives only a PAUSED dedicated Scheduler/controller
descriptor from the same canonical release/root; ordinary schedules stay PAUSED.
After exact IAM/image readback and source-free internal preflight, append the typed
owner attestation preserving sequence15/STOP/receipt/reservation. Publish immutable
PAUSED and ENABLED descriptors from that committed evidence, then review/apply
activation containing only dedicated Scheduler/controller state restoration.
A failed pre-source INTENT is never passed off as a successful reconciled checkpoint.

## CI execution budget

Final candidate SQL workflow37593155744 ran all tools cases to 2712 PASS/1 SKIP
in593.52seconds, then was cancelled before final SQL validation under the ten-minute
job budget. Candidate local full suite passed2713 tests. Increase only the job
timeout to20minutes; retain both original full tests and SQL validation commands.
Cancelled CI is not PASS; final candidate must complete both workflows before merge.
