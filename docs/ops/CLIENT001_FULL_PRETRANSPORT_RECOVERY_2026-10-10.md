# FULL dispatch before transport

The FULL coordinator committed an intent containing `BACKFILL_FULL_AUTHORITY`,
but the Cloud Run boundary compared it with an environment that omitted that
field. The exact registered image rejects the saved intent before token or HTTP
transport. This packaged proof does not reconstruct the historical stacktrace.

The boundary now accepts this field only with the coordinator's committed intent
context. Root, leaf index, shard, plan, receipt sequence, run ID, lease generation,
ACK, Job and the complete environment must agree. Unknown or changed context is
rejected before obtaining a token. Existing receipt fences and the one-dispatch
wake limit remain authoritative.

`EXACT_FULL_AUTHORITY_OVERRIDE_PRETRANSPORT_REJECTION_V1` is an owner-only
source-free recovery for the exact saved incident in `full_pretransport_recovery`.
It requires every closed predicate, fresh frozen preflight, a packaged old-image
proof, no child or source effect, no successor, and the exact original lease and
RUNNING checkpoint. It preserves original evidence, appends a FAILED checkpoint
and a `PRETRANSPORT_REJECTED` lease closure, then commits
`DISPATCH_PRETRANSPORT_REJECTED` and `FULL_PRETRANSPORT_STOP_RECOVERED`.
It creates no receipt, SUCCESS, COMPLETE or RECON. The next normal cloud wake
uses a new dispatch sequence and run ID. Missing or uncertain evidence blocks.

This contract grants no automatic recovery for other incidents, quota exemption,
IAM change or source replay. Terminal-success class-C recovery is unchanged.
The FULL root, historical cutoff, recent selection and source/runtime contracts
remain unchanged. Cloud observers validate the exact owner certificate before
discounting its retained STOP or treating its rejected intent as closed.

Checkpoint evidence is compared as strict JSON content using the existing
duplicate-key/NaN-rejecting parser. The canonical writer's JSON spacing must not
block the exact original RUNNING checkpoint. Unknown content and extra evidence
versions still block. Original serialized evidence is never rewritten.
