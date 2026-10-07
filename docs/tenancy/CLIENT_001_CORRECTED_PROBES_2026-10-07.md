# client_001 corrected capability probes — release candidate

Owner ACK 2026-10-07 accepts the frozen SKU90 evidence and authorizes corrected
Stocks/Supplies discovery, Supplies scale and full-history engineering. This
document records a qualified candidate, not a deployed-state assertion or GO.

The replacement of stale PR252 uses current main 55b8bcba4e85b581076c4e75f43935d0ec3e8577.
Stocks uses a positive retained current-Moscow-day Catalog SKU; missing bounded
input is UNKNOWN without a call. Supplies uses the actual runtime request with
CPC_STATES, ORDER_CREATION DESC, cursor and limit1. No Seller surface or IAM
expansion. Existing FBO split logic is retained; the owner coordinator now admits
the existing bounded 1..30-day FBO window contract. Default plan identity is unchanged.

Runtime source 06679f59edf975ae58451b4c746ff628cc387fd8 is built by
9eb17604-e162-4b87-8131-f4f48199b231 and independently qualified by
3d15e969-597a-40ff-a7f8-ba59b396ef3c. The immutable runtime release record
`infra/tenant/releases/ozon/06679f5.json` retains source/archive/config/image proof.
Controller source 9a4cafed54a42b3520efa7c4a4564ae58a687069 is built and qualified
by a53f3b23-ad91-480e-b860-b8ee07efdfa3. Its source CI has five successful jobs.
The controller proof is recorded separately under docs/tenancy/evidence.

After reconciled SKU completion, Performance quota WAIT must not block remaining
Seller Supplies traversal. The new source-scoped decision grants zero Performance
exports, reads the unchanged reservations and still rejects unknown exports.
Incomplete SKU and ambiguous report outcomes preserve their previous gates.

Intermediate artifact-assembly revisions omitted controller opt-in to avoid
circular release registration. They MUST NOT be deployed. Final integration
restores the original immutable qualification root and ENABLED dedicated schedule
with both independently qualified artifacts. Required final CI and reviewed
deployment/readback remain separate gates; ordinary schedules remain PAUSED.

Deployment must quiesce the dedicated historical tick before replacing images,
wait for terminal executions, review exact tenant-only deltas, publish immutable
PAUSED/ENABLED deployment descriptors for the same root, and read back the exact
artifacts/bindings before activation. No IAM, Secret, schema or ordinary Scheduler
change is authorized. Rollback pauses the dedicated tick and preserves durable
intents, receipts, reservations and loaded data; it never erases an uncertain POST.

Corrected discovery must be performed through canonical tenant-control. A genuine
unavailable required capability stops FULL_HISTORY GO. Supplies scale/state safety
and a qualified full-history adapter/manifest remain mandatory: this candidate
retains qualification-only fail-closed behavior, not a full-history release.
