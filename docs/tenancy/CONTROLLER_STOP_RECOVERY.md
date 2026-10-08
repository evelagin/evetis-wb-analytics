# Post-reconciliation, pre-source controller recovery

`CONTROLLER_POST_RECONCILIATION_PRE_SOURCE_STOP`, version 1, is an owner-only
attestation for an exact controller STOP after a successful predecessor receipt
was reconciled and before successor source authority existed. It is separate from
failed-before-report-POST recovery and does not exclude reservations or turn a
failed receipt into a successful one.

Every certificate pins tenant/root, exact STOP, predecessor receipt and
reconciliation hashes, successful source execution, historical qualified
controller/runtime sources and digests, checkpoint identity, causal evidence
hashes, exact owner authorization and the recovery implementation SHA. Each
predicate is required to be confirmed; unknown is a hard stop. Missing old
exception text is not a substitute for or obstacle to proven safety boundaries.

The owner writer independently reads fresh bindings, hold, executions, dispatch
fences (including uncommitted ones), source journal, source/persisted natural
keys and exact predecessor terminal lease release before publishing. Older lease
records must have an exact owner release or canonical TTL plus visibility grace
expired before the STOP boundary. Missing/held expiry or conflicting release
ownership fails closed. Expired history is preserved and never reclassified. Only metadata GET/list
and SELECT are used for those checks. It must not invoke ingestion, report POST,
validation, source reconciliation or the coverage writer.

Publication adds an owner-only REF attestation and an immutable orchestration
record `CONTROLLER_STOP_RECOVERED`. The controller append identity cannot create
the owner REF attestation. Original STOPs, receipt, reconciliation, source units,
leases and business data are preserved. Conflicting attestations fail closed.
A certificate covers only its explicitly authorized STOP, never other records
with the same reason. Every future STOP remains a separate blocker.

Cloud consumption reconstructs the records and owner marker, checks exact
historical terminal executions and provenance, then includes only those exact
STOP hashes in the gate's approved set. Successful predecessors retain their
existing reconciliation. New successor work after committed recovery is allowed
only through the ordinary bounded controller protocol; publication itself must
still prove no successor authority exists. This is not FULL_HISTORY GO, lifecycle
activation or permission to enable regular Schedulers.

Operational certificates and tenant accounting evidence belong in private durable
cloud storage. Do not put real tenant counts, source rows, credentials or incident
payloads in this public repository. The local observer is not continuation
authority. Image qualification, reviewed tenant-only deployment, fresh predicates,
readback of preserved records and a separate operational/GO preflight remain
mandatory before source continuation.
