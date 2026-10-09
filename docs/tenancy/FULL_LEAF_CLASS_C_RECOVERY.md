# FULL leaf class C recovery v1

Contract: `FULL_LEAF_POST_DISPATCH_TERMINAL_SUCCESS_UNRECONCILED_STOP_V1`.
This is separate from the qualification-root controller recovery contracts.
Code or this document is not evidence of deployment or production recovery.

## Authority and scope

An owner-only `ref.BFFLR_AUTH_<root>_<controller-source>` marker pins
`client_001` / `mpa-t-client-001`, the exact frozen FULL manifest, unchanged
runtime source/image/implementation, qualified replacement controller artifact,
owner ACK hash, initial incident and permission for subsequent class C incidents.
Cloud append cannot create this marker or a GO marker. The marker authorizes a
controller provenance handoff; it does not replace the manifest/GO, change dates,
reset shards, exempt STOPs or authorize source work by itself.

Initial application is restricted to the owner-pinned STOP/intent/receipt,
lease generation and exact source-unit range. Future automatic application
requires a committed, independently reconstructable initial recovery and a
typed STOP in the checkpoint/lease/terminal/persisted-reconciliation/RECON-append
window, carrying exact index, shard, sequence, intent and receipt hashes.
Generic STOP, binding, quota, dispatch or monitoring failures are not class C.

## Mandatory live predicates

All predicates must be TRUE. UNKNOWN, missing or conflicting evidence blocks:

- Exact tenant, root, manifest, approved T5 plan, GO, leaf/shard and artifact.
- Both bindings BOUND, both credentials PASS, no owner hold, valid BACKFILLING.
- Exact terminal-success child operation/execution, single task, zero task
  retries, canonical job/image/service account, binding/caps and source scope.
- Exact intent/receipt and reconciled predecessor; contiguous terminal-OK source
  units with exact run ownership and no ambiguous transport retries.
- Current runtime state equals the persisted terminal checkpoint, with the
  predecessor checkpoint matching the previous shard RECON.
- Canonical source/persisted counts and natural keys reconcile. This does not
  assert full inventory coverage, financial finality or byte-for-byte API body
  equivalence beyond the existing domain accounting contract.
- Existing exact terminal L/LD generation/owner/ACK/operation chain; no later
  generation in either metadata or checkpoint ledger.
- No successor authority, including uncommitted streaming rows, second child,
  unattributed source activity or active conflicting execution. In a natural
  recovery wake, only its own single controller execution may be active; an
  owner-side recovery requires all jobs inactive.
- Durable incident chronology associates the STOP with the exact controller
  and completed child. The old historical exception need not be guessed.

## Source-free publication

Observation permits tenant metadata GET and bounded EU Standard SQL SELECT only.
No marketplace/secret transport, source dispatch, checkpoint rewrite, lease
release rewrite, coverage/DQ append, data cleanup or runtime replay is available.
Coverage uses the existing canonical verifier through a read-only wrapper; its
optional evidence appends are intercepted without external writes.

After all live predicates pass, append identity creates a non-expiring CAS
certificate fence `tenant_locks.BFFLR_<root>_<stop>` and publishes only:

1. The missing canonical shard `RECONCILED` record.
2. An additive root `FULL_LEAF_STOP_RECOVERED` record linking the certificate.

Original STOP, source records, predecessor RECON, terminal checkpoint and LD
remain unchanged. Source budget is zero. A crash or lost acknowledgement can
finish only the same frozen certificate after fresh predicates; conflicting
evidence blocks. No new source is dispatched in a recovery wake. Subsequent
independent wakes use the normal FULL flow. A partial source state cannot become
COMPLETE; actual completion still requires existing leaf/T5/FULL/DQ gates.

## Diagnostics and stopping

Safe closed gate diagnostics cover manifest/root, binding, quota, lease,
checkpoint, runtime terminal, persisted/source reconciliation, reconciliation
append, monitoring/watermark and source-dispatch boundary. Diagnostics contain
stage/category and non-secret authority hashes, never exception payloads,
headers, credentials or business identifiers.

New unsupported classes, failed/ambiguous runtime, source/persisted mismatch,
duplicates/gaps, binding/security/cross-tenant regression, unknown predicates,
new IAM/credential needs or destructive changes still require owner decision.
Ordinary quota WAIT and healthy reconciled continuation do not require ACK or
an observation pause. Regular daily/fast/weekly Schedulers stay PAUSED.
