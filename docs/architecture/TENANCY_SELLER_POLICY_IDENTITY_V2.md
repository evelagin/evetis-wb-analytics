# Tenancy Seller policy / identity v2 — local implementation contract

Date: 2026-10-04. Authority: OWNER ACK — LOCAL IMPLEMENTATION + OFFLINE VALIDATION ONLY.
Status: local implementation/offline evidence. NOT deployment, validation, binding or READY evidence.
This supersedes relevant D2 capability classification and Seller identity requirements in
TENANCY_T5_DESIGN.md; prior observations and decisions retain their historical meaning.

## Independent evidence boundaries

1. `/v1/roles` reports credential capability inventory; role names grant nothing.
2. Reviewed endpoint semantics are distinct from inventory and actual effective permissions.
3. Exact runtime callable routes are enforced independently of capability treatment.
4. Machine identity is computed from observed fields, not owner assertion.
5. Owner confirmation is an independent event referencing a fresh OBSERVED observation.
6. Seller/Performance binding requires machine evidence plus owner confirmation.
7. Lifecycle remains the existing state machine; identity v2 does not advance a tenant.

A listed mutation path is conservatively blocking; it does not prove a successful write is possible.
A role named `Admin read only` does not override that rule. No exception is authorized.

## Policy v2

Dimensions: lifecycle ACTIVE / RETIRED / ALIAS / UNRESOLVED; semantics READ /
ARTIFACT_GENERATION / VALIDATION_OR_CHECK / BUSINESS_STATE_MUTATION /
EXTERNAL_SIDE_EFFECT / UNPROVEN. Provenance includes review source/authority/date/reason,
confidence, spec hash, retirement evidence, alias evidence and preserved legacy_record.

| Reviewed condition | Capability treatment | Runtime authorization |
|---|---|---|
| ACTIVE READ, no side effects | PASS | Only existing exact profile routes |
| RETIRED, no known dangerous semantic classification | WARN | None implied |
| ARTIFACT_GENERATION, artifact effects only | WARN | None implied |
| VALIDATION_OR_CHECK, effects UNPROVEN | FAIL | None |
| BUSINESS_STATE_MUTATION / EXTERNAL_SIDE_EFFECT | FAIL | None |
| UNRESOLVED / absent inventory record | FAIL | None |
| ALIAS | Needs proven exact target equivalence and reviewed semantics | No alias/template automatically callable |

7 of the reviewed 14 paths are business mutations; 5 artifact generation; 1 validation/check
with unresolved side effects; 1 external side effect. No mutation exception exists.
The reviewed 51 additions are 22 retirement notices, 1 unresolved wildcard alias candidate,
28 unresolved methods. Retirement notices do not prove technical endpoint disablement.

The 481 legacy records are preserved. With 51 additions the inventory contains 532 entries.
20 existing runtime/control/promo read paths are reviewed. Other legacy regex classifications
remain suggestions/UNRESOLVED, including those previously called READ. This conservative
migration can create additional blockers beyond the original 28; no silent promotion is allowed.
A spec-hash-matched review manifest can supply reviewed decisions. Changed spec hash does not
inherit old approvals. Version 1 policy cannot authorize validation in the v2 evaluator.
Malformed/duplicate JSON, unsupported versions/enums, inconsistent provenance or ambiguous
aliases fail closed. Policy PASS is not effective-permission or tenant readiness proof.

## Controlled transport and entrypoints

Only common.seller_call constructs credentialed Seller requests. It validates before credentials
and again before dispatch: exact HTTPS origin api-seller.ozon.ru, no explicit port/userinfo,
exact method/path and permitted execution profile. Queries, fragments, percent encoding,
backslashes, non-ASCII/control/whitespace URLs and ambiguous paths are rejected. No dynamic
route/template is currently callable. Redirects 301/302/303/307/308 are denied without forwarding.
Denied calls never reach credential construction/network where the preflight can decide.

Runtime and control each have the existing 15 exact POST paths. Promo has a separate 6-route
profile (GET actions, five POST routes); it cannot opt into client_001 ingestion. Capability WARN
cannot add a callable route. Profile scope comes from the actual main/lifecycle entrypoint.

runtime_execution_contract.json names the sole existing legacy EVETIS project; it is covered by
canonical naming parity tests. Every other ingestion project requires TENANT_BINDING_REQUIRED=1.
Omission/false/unknown flag, unknown/duplicate entity or external promo fails before data writes.
Control validation is not forced through the ingestion binding gate. Legacy ingestion behavior
is retained; unknown entities now fail instead of being silently skipped.

Offline AST gate: tools/tenancy/seller_transport_scan.py checks normal runtime modules, including
new functions in common.py. Only reviewed transport/Google/Performance functions may use raw
sinks. Python is not a sandbox against arbitrary hostile code: review and offline gates remain
mandatory. Historical API/backfill scripts under docs/ozon/audit2026-08-30/raw/api are outside
the runtime image/normal entrypoints. They are not authorization to run a standalone credentialed
script; migrating or executing them requires separate scope. No historical script was executed.

## Identity versions and binding evidence

V1 remains the original SHA256 hex of canonical api/client_id/inn/ogrn JSON, all required.
V2 `ozon-seller-core-v2` hashes canonical sorted compact UTF-8 JSON containing api, version,
trim-normalized string client_id and inn. Mandatory core cannot be empty, non-string, contain
controls or internal whitespace; leading zeros are preserved. No numeric coercion/substitution.
Storage: `ozon-seller-core-v2:sha256:<64 lowercase hex>`. Missing/empty OGRN is valid optional
structural evidence, not a substitute identifier. Malformed legal evidence fails.

Versions never fall back or compare equal. Unknown representation fails closed. Observations
select V2 only via explicit SELLER_IDENTITY_VERSION; default V1 preserves compatibility.
Runtime computes the version explicitly represented by the owner binding marker.
Existing notes STRING stores V2 owner confirmation, identity_version and legal_evidence.ogrn;
no new physical column is required. Previously nonempty OGRN changing/disappearing blocks with
LEGAL_EVIDENCE_CHANGED even when the core fingerprint remains equal. Empty baseline does not
prove future legal evidence: any subsequent legal enrichment remains visible in observations.
SQL binding-status source uses version/owner/legal/link checks; runtime markers remain the
consistent authorization source. The SQL mirror's historical visibility lag remains unchanged.
Identifiers and fingerprints are not normal log output. V2 log/error redaction is separate from
stored observations so it never destroys required evidence. Owner CLI show remains an explicit
inspection interface; do not paste its unmasked output into chat/artifacts.

Performance fingerprint remains V1. With a V2 Seller binding its confirmation must reference
that Seller fingerprint and SAMPLED SKU/catalog evidence. Runtime/control require that link.
Current sampling is at most 20 campaigns, first 100 products; it is not exhaustive cabinet
ownership proof. Owner confirmation remains necessary and cannot convert NO_EVIDENCE,
FOREIGN_SKUS, INCOMPLETE or CREDENTIAL_REJECTED into OBSERVED.

## Migration, validation and rollback boundaries

No historical observation, marker, trial data, schema, lifecycle or binding is migrated by this
local change. client_001 is not validated/bound/production-ready. Historical trial attribution
and completeness remain separate forensic questions; future binding does not retroactively
establish machine binding at execution time.

Before any deployment: review exact diff, fulfill clean/fresh-base gates or authorized isolation,
resolve relevant policy blockers with reviewed evidence (never blanket role exceptions), obtain
ACK for exact image digest and SQL definition scope, preserve prior digest/config/view definition
and paused schedulers. Deploying code and V2 observation configuration are separate from validation
and owner marker creation. New validation needs its own bounded API/cloud-write ACK. V2 confirmation
needs new eligible observations and independent owner verification of Seller and Performance.
No old V1 marker is rewritten: revoke/reconfirm via authorized append-only protocol if migration
is chosen. Unknown marker/protocol or mixed version remains denied. No ingestion, backfill or
scheduler activation is implied. Rollback never re-enables optional external binding or restores
unsafe dispatch. Incompatibility requires STOP; no destructive marker/data cleanup.

Offline acceptance: complete applicable Ozon/Tenancy suites, transport AST, legacy trace/golden
vectors, malformed policies and identity/binding/SQL synthetic cases. Cloud parity, runtime IAM,
effective permissions, sampled binding and production readiness remain UNPROVEN until separately
authorized live verification. Offline PASS does not satisfy deployment Definition of Done.

Candidate build source: infra/tenant/releases/ozon-runtime.v3.cloudbuild.yaml includes the new
exact image contents and an external binding-flag omission gate with --network=none. Historical
V1/V2 build files, approved runtime release pointer and release records stay unchanged. The new
config has not been executed; published digest, image qualification and release record remain
future authorized work. The initial candidate was tested with Python 3.14. Integration
qualification below adds Python 3.12 source/tests evidence; it does not qualify a container.


## Isolated integration qualification — 2026-10-04

Selected committed base: `main` at `f1eb89d02666a4973f1052373f8cff494c87270e`.
Local `origin/main` and native GitHub remote main metadata agreed with that SHA at inspection.
No fetch/pull was performed. Integration branch: `codex/tenancy-policy-identity-v2-integration`.
The primary checkout is outside the write scope.

Candidate patch SHA256: `f741f10ff0ab422e424060ef14ab891ed561636bf482c0f67069a975430ada29`.
Original candidate base `491b9d96fe776e908a8421a633ff7daae0f729b6` diverged from main;
the common ancestor is `437536f49c9454ca4b1492d7b27727a7c06d75cd`.
All 34 candidate paths are identical between the old candidate base and selected main.
No overlapping source conflict exists. New Communications commits on main are retained.
Other Tenancy implementations on separate branches are not silently merged.

Integration-specific runtime/security scope is unchanged. Policy and review-manifest nested JSON
is serialized more compactly; parsed JSON equals the accepted candidate exactly. All 532
records, legacy_record and review/retirement/alias provenance remain. The generator uses
this stable serializer; a roundtrip/stability test covers it.

Python qualification uses isolated CPython 3.12.15 (macOS arm64), not the system Python 3.14.
The standalone runtime archive SHA256 is
`ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2`.
Repository CI dependencies were installed into a task-local venv using the committed
hash-checked `tools/requirements-sql-ci.txt`. No global installation was performed.

Observed offline checks (exit 0 unless explicitly marked):

| Gate | Evidence |
|---|---|
| C5 Python compile | runtime, bootstrap, Ozon tests and tools compile under 3.12 |
| Ozon suite | 701 passed, 1 skipped (external finance audit archive unavailable) |
| Initial C7 full tools suite | 2345 passed before fixture correction; see rerun evidence below |
| Initial Tenancy subset | 884 passed; subset of C7, not additional distinct tests |
| Seller transport AST | PASS on integrated runtime |
| Policy check | strict v2 parsing/check PASS; generator and v1/v2 vectors covered by suites |
| Registry | 2 documents, 0 violations |
| Ozon schema parity | 22 tables / 15 Git DDL, 0 violations |
| Current SQL | C1–C18 PASS; this is source integrity, not live parity |
| C6 Terraform | fmt PASS; tenant/bootstrap/terraform validate PASS in temporary copies, backend disabled |
| Terraform tests | 56 passed using mock Google provider; no real plan/apply |
| Terraform security mutation gate | all 32 weakened-rule mutations caught; 0 survivors |

Docker container build/qualification is BLOCKED because the local daemon is unavailable.
No exact Linux production-image/Python microversion qualification is claimed. This is a
local source candidate for owner review; deployment readiness remains UNPROVEN.

Impact analysis selects TIER0 and these required data gates: ozon_unit,
control_tower_phase1, control_tower_owner_screens, promotion_l3, ubr012_revenue_source,
promo_economics, inventory_context, ads_intel. They require cloud query jobs and were not
executed under the no-production ACK. Full DoD cannot be PASS: live SQL parity,
data contracts, pipeline health and cross-marketplace data regressions remain BLOCKED.
The framework's offline verdict must retain BLOCKED, not be renamed PASS.

Before release, obtain a separate ACK for publication/CI/container qualification; after a
reviewed immutable image digest exists, obtain target-scoped cloud ACK for live preflight,
SQL definition deployment/readback and runtime promotion. Preserve prior config/digest/view
and PAUSED schedules. A rollback that restores binding bypass is prohibited; STOP instead.
Fresh v2 observation, owner confirmation, lifecycle transitions and ingestion each require
separate scope. No client_001 readiness or historical trial-data repair is implied.


### DoD fixture stability correction

The first post-commit `verify_task --offline` reported FAIL: 2344 tools tests passed and
`test_16_unexpected_delete_change_replace_forget` failed. The prior complete run passed.
An offline controlled reproduction showed that `_tfplan()` embedded current ZIP timestamps;
identical contents changed hash across the two-second ZIP clock grain. The test therefore
failed at expected-plan-hash comparison before its intended `show -json` rejection.
`tools/tests/test_tenancy_deploy.py` now uses its existing fixed NOW for both synthetic ZIP
builders, with a wall-clock-change regression. Production hash/provenance/apply guards are
unchanged. This adds one test file to the 34-path candidate. Earlier failed evidence is
retained; final qualification must reference the successful rerun, not hide the first FAIL.

After the fixture correction, all 69 adversarial deployment tests passed (exit 0) under
Python 3.12.15. The complete offline DoD is rerun on the resulting clean commit.


## Security re-audit before broad inventory acceptance — 2026-10-04

Owner decision requires security repair/CI/immutable-image qualification BEFORE any credential
verdict relaxation. Deployed V2 has no observed bypass usage, but its previous AST gate failed
to reject common._secrets / common.urllib / exported SDK references in a new normal module.
This is a source-enforcement gap, not evidence of a live forbidden request.

The repair limits normal module access to an explicit common public interface; cache, raw HTTP,
Secret Manager SDK and reflective common access are rejected. Credential loader/header getter
also enforce approved caller code identities before Secret Manager access; ordinary cache reads
are denied. Tests inject only synthetic state. All eight reported dangerous paths and selected
unresolved paths are denied before credential construction/dispatch in every profile.

Trust boundary remains reviewed immutable source plus AST/CI gates. CPython is not a sandbox
against hostile introspection, monkeypatching or changing the transport itself. No such code may
be qualified/published as normal runtime. Public identifier getters remain identity interfaces,
not API-key access. No execution profile or semantic record was broadened. Seller blocker logic
remains unchanged in this security repair. No tenant validation or cloud deployment is implied.
