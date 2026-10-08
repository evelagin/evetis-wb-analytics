# Terminal-success post-dispatch controller recovery

This contract is authority bookkeeping for completed source work, not a retry
policy. It does not change the older pre-source or post-reconciliation recovery.
Code, a certificate, or a historical success alone never proves live recovery.

`CONTROLLER_POST_DISPATCH_TERMINAL_SUCCESS_UNRECONCILED_STOP`, version 1,
uses a closed owner-authorized certificate and an owner-only `ref.BFDR_*` marker.
The certificate names an exact tenant/root/STOP/intent/receipt, successful child,
source range, predecessor reconciliation, lease generation, immutable releases,
source-state hash and resulting reconciliation hash. Unknown predicates block.

The source-free observer reads terminal operations/executions, bindings, frozen
scheduler authority, source units and request accounting, exact natural keys,
checkpoint/lease history and committed/uncommitted successor fences. A successor,
unattributed source activity, conflicting owner or changed prefix blocks publication.
Only the qualified Supplies qualification scope is supported.

Application preserves original evidence. Canonical `tenant_backfill.reconcile`
appends the checkpoint and creates its exact terminal lease marker. The resulting
`RECONCILED` record is then committed, followed by a separate
`CONTROLLER_DISPATCH_STOP_RECOVERED` record. Traversal remains PARTIAL; recovery
neither publishes COMPLETE nor manufactures coverage. No marketplace transport,
source dispatch, coverage write or automatic recovery is part of this path.

A crash may leave additive partial bookkeeping. Only the same owner certificate
may finish that exact chain. It cannot repeat source work or release another
lease. A later normal successor is allowed when consuming a previously committed
recovery, but never while publishing one. Cloud readers verify the owner marker,
original chain, successful historical execution and terminal release; cloud append
identity cannot create the owner attestation. Another STOP remains blocking.

## Monitoring observation watermark

Each ordered scope read has start/end observations. Clock samples must remain
monotonic across reads. Success/failure timestamps are bounded by the clock sampled
AFTER that scope's reads complete, rather than a stale timestamp sampled before
all reads. A timestamp beyond the completed observation remains invalid; negative
clock movement fails closed. Stale results retain their actual age. Monitoring
projection neither reconciles receipts nor exempts STOPs.

## Qualification and rollout

Use synthetic adversarial tests and exact packaged controller qualification.
Keep all schedulers paused during image-only staging and exact recovery. Preserve
runtime image, IAM, secrets, business data and all original records. Require fresh
source-free observation immediately before application and independent readback
of reconciliation, checkpoint, release, preserved source units and zero source
calls. Resume the same plan only after all operational gates pass. Full-history GO
remains a separate owner-authorized contract, never inferred from this recovery.

Rollback pauses the dedicated authority chain and restores a reviewed qualified
controller digest if necessary. Never erase recovery evidence, regress accepted
checkpoints, delete source rows or replay completed units.
