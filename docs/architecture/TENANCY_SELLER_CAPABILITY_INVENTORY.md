# Seller capability inventory / strict runtime authorization

Owner decision, 2026-10-04. Supersedes only the credential-inventory blocker rule
for the explicitly opted-in dedicated tenant client_001. Previous V2 observations,
policy classifications and review provenance remain historical evidence. Deployment
is a separate release record and live verification; this document is not proof of it.

## Authority and accepted risk

The intended Heldi/client_001 credential is the existing Admin read only credential.
The role name is observational, never an authorization rule. Broad reported capability
presence does not prove successful execution of that capability. Owner accepts that
inventory risk; it cannot authorize mutation or unknown endpoints. No rotation,
permissions change, Secret Manager change or Ozon dangerous-method probe is part of this change.

Registry marketplaces.ozon.seller_inventory_model=BROAD_READ_INVENTORY_V1 opts in.
The default STRICT_CAPABILITY_V2 remains unchanged. Legacy EVETIS cannot opt in via
the registry. Only tenant-control receives SELLER_INVENTORY_MODEL and
SELLER_IDENTITY_VERSION=ozon-seller-core-v2. Ingestion keeps the same exact profiles,
TENANT_BINDING_REQUIRED=1 and STRICT_PAGE_CAPS=1. No new runtime route is granted.

## Trust boundary and security-first prerequisite

Before relaxing inventory verdicts, source boundary gaps were repaired separately:
controlled key/header getters and opaque cache, direct SDK/HTTP sink and parent import
AST controls, external dispatch/reflection controls. PR #239 and #240 passed CI and
immutable-image offline qualification before this model implementation.
CPython is not an adversarial-code sandbox. The boundary is reviewed immutable source,
AST gate, independently qualified image and restricted deployment identities. Arbitrary
hostile introspection or monkeypatching is not claimed safe. Ordinary runtime modules
cannot read keys/cache/headers or use direct HTTP sinks through the reviewed source gate.

Seller transport validates exact HTTPS origin, HTTP method, path and execution profile
before credentials/network, revalidates at dispatch, refuses redirects and ambiguous
query/fragment/encoding, seals dynamic route templates and external promo. External
ingestion cannot omit binding. All eight dangerous paths and representative unresolved
paths are offline negative controls; never invoke them against Ozon.

runtime_authorization_evidence is LOCAL RELEASE CONSISTENCY, not an independent security
attestation. It checks selected runtime/control routes against reviewed ACTIVE READ/NONE
policy, external binding/promo contract and records exact source/profile/policy hashes.
Deployment additionally requires AST CI and adversarial image qualification for those
exact bytes. A self-reported PASS is insufficient: evaluator recomputes consistency and
compares supplied evidence. The observation's hashes permit independent release linkage.

## Machine dimensions and hard blockers

Authentication PASS/FAIL/UNPROVEN; identity PASS/FAIL; full selected runtime/control
coverage PASS/FAIL_MISSING_REQUIRED; inventory risk CLEAN or explicit warnings;
runtime authorization and binding-gate integrity PASS/FAIL; inspection integrity;
expiry integrity PASS/FAIL/WARN_UNKNOWN. Absent expiry is disclosed, not invented.

Broad Seller PASS requires successful Seller Info, valid V2 identity, complete selected
profile reads, valid roles inspection/expiry and qualified consistent runtime/binding
integrity. Missing input/proof, malformed policy, failed inspection/auth, expired/invalid
expiry, missing reads, identity invalidity or bypass block. Mutation/unresolved inventory
warns only; its lifecycle/semantics and per-capability treatment remain unchanged.
An inventory warning is not a read-safety finding. Dangerous capability execution remains
UNPROVEN and is not tested. Performance association occurs only after Seller eligibility.

## Durable capability discovery

Full role/method matrix and per-method records are stored once in existing
SELLER_IDENTITY_OBSERVATIONS.evidence_json.credential.capability_discovery. CAPABILITY_PROFILE
stores verdict dimensions/summary and policy provenance; no new physical table or SQL
object is needed. Records contain path, documented HTTP when known, roles, observation
UTC timestamp, lifecycle/semantics/confidence/review, retirement/alias evidence, policy
version/spec/canonical hash, shared supported profiles and actual external callable profiles.
Identifiers and secrets are not duplicated into discovery.

Product states: SUPPORTED_AND_USED, SUPPORTED_NOT_USED, REVIEWED_READ_CANDIDATE,
ARTIFACT_CANDIDATE, RETIRED, DANGEROUS_NEVER_CALL, UNREVIEWED. USED means an implemented
dependency selected in this configuration, NOT observed production traffic. Shared promo
support cannot become external callable authority. ALIAS retains its evidence and does
not replace required exact paths. Unknown remains unresolved, not safely classified by
name. Compare successive observation method sets/hashes for drift; do not infer vendor
changes solely from changes in policy review strictness.

## Future capability workflow

Discover -> authoritative documentation -> semantics review -> establish read safety ->
design probe -> owner-approved bounded probe -> RAW contract -> ingestion -> DQ ->
mart/analytics -> explicit reviewed execution-profile expansion. No auto-expansion from
roles, subscription or discovery. Artifact generation needs its own reviewed profile/ACK.

## Release, deployment and binding

Use dedicated worktree/PR/CI, immutable source/digest qualification, release record/pointer
via PR, canonical frozen Terraform workflow restricted to client_001. Expected change:
four job images plus two tenant-control env fields; no IAM, credentials, dataset, SQL or
Scheduler changes. Schedulers stay PAUSED. If actual plan differs, stop.
Bounded validation appends fresh observations/capabilities; owner confirmation separately
appends canonical markers only for eligible fresh machine observations. Preserve V1,
require independent sampled Seller/Performance SKU association for V2, stop on mismatch.
Owner attestation is not machine evidence. BOUND does not mean lifecycle READY or DQ PASS.
No lifecycle transition, ingestion, backfill, historical trial cleanup or activation is
included. trial-c001-20261001-01 remains untouched. Next phase is separate backfill engineering.
