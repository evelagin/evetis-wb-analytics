# Local implementation evidence — 2026-10-01

Owner-authorized local implementation only. This is an implementation report,
**not an independent final review or deploy-readiness verdict**.

Changed implementation scope:

* apps-script/ingestion/WbAdsDayRepair.gs — pinned pure incident execution/guards.
* apps-script/ingestion/WbAdsDayRepairRuntime.gs — explicit OFF entrypoint and narrow I/O.
* apps-script/ingestion/WbAdsRawLoader.gs — optional fetch/capture and suppression of implicit
  single-ID status fallback when incident I/O is supplied. Existing ordinary caller unchanged.
* tools/step5a_appsscript_tests/day-repair.test.js — focused offline source/runtime fixtures.
* This incident documentation/package directory only.

No old resume source, tests, schema.sql, CONTRACT.md, REVIEW.md or old review package
is modified. The four old tracked hook files were already dirty at startup; only
WbAdsRawLoader.gs acquires this task's additional collector hook. Unrelated work is
preserved. HEAD/branch/index are unchanged; no stage/commit/fetch/push/cleanup.

Baseline: branch recovery/chz-automation-2026-10-01,
HEAD b94ca335b98ce8146f925295f45d8789db5ed46c.
Dirty-tree exception: owner explicitly authorizes this narrow local change in the
existing workspace; no clean/fresh remote/deployment gate is claimed.

## Previous narrow booster-grain correction

Owner ACK accepted the independent-review FAIL for the previous candidate patch
6c724b804617f2519e16bea797bfe9151c05c9006332b0d6fc2befceae955bb5.
That revision changed only WbAdsDayRepair.gs (one booster-key validator plus its
call inside the existing complete-payload guard), appends twelve focused tests,
and updates incident documentation/review artifacts. Runtime adapter, collector,
stat grain, pinned date, population/order, evidence reuse, controls, heartbeat,
write scope, billing semantics and history checks retain their prior bytes/behavior.
No permanent daily-loader or generalized recovery change is implemented.

The grain counterexample fails before both mock RAW writers with
BOOSTER_GRAIN_DUPLICATE; the real narrow adapter fixture produces ERROR and
journal/status error evidence, with zero RAW rows and no COMPLETE. The prior
review is not retroactively relabeled PASS. A fresh independent review is required.

## Previous narrow booster-source-shape correction

Owner ACK accepts the fresh independent-review FAIL for candidate patch
11b692b5408fc6731182361e552ff07aadb7cbeac5e47265ef040b8ea898dff5.
This correction changes only WbAdsDayRepairRuntime.gs, appends 26 tests to the
existing 74-test byte prefix, and updates incident documentation/package artifacts.
The helper adsDayBoosterSource_ checks original own-property presence and array type
before control evidence, before legacy flatten and before either RAW publication.
Absent aliases preserve existing optional booster handling. One present array is
accepted intact; any present non-array is HTTP_BOOSTER_SCHEMA. Both aliases present
always produce HTTP_BOOSTER_ALIASES, without winner selection or merging.

The exact review counterexample (37563883, valid stats, boosterStats:false) now has
zero stat/booster writer calls, local PARTIAL/ADS_PARTIAL, new journal ERROR and
inspectable HTTP_BOOSTER_SCHEMA in failures/status evidence. A final-batch malformed
response also blocks the already prepared 400 stat and 400 booster rows. Valid arrays,
empty arrays and absent fields retain normal completion. Tests use real adapter,
collector and flatten with exclusively mocked services. No production request is made.

Core incident state machine, stat/booster grain guards, pinned day, full population
and ordering, controls, reused campaign/cost evidence, RAW writers, heartbeat rules,
billing semantics and 24–29 checks are unchanged. Legacy collector/daily source and
generalized resume source/tests/package retain their previous bytes. This is local
implementation evidence, not a fresh independent review or deploy approval.

## Previous complete consumed-source-structure correction

Owner ACK accepts the fast independent-review FAIL for candidate patch
b72f64cf0bb043098fd582c3b54e5279e4496197e7350064356d2f3206bcda17.
The bounded audit traced every object/array and alias consumed by the existing
incident HTTP parser, collector and wbAdvFlattenFullstats_: response/data, campaign,
days/day, apps/app, nm/nms item objects, booster aliases/item objects, plus the
scalar leaf aliases/fields actually selected by flatten. CONTRACT.md records the
exact hierarchy and absence/type rules. Unknown opaque raw_json metadata is excluded.
The validator returns original arrays/objects and never filters, normalizes or
deduplicates source. Both aliases fail closed, including equal or empty values.

Implementation changes are limited to WbAdsDayRepairRuntime.gs: small source shape
helpers, whole-response validation before collector acceptance/control evidence,
and removal of structural evidence fallbacks. The ordinary API client/collector,
flatten, core state machine, date/grain/population/control/evidence/heartbeat guards
and publication paths keep their prior bytes. No new recovery architecture exists.

131 additional table-driven/positive tests are appended after the intact previous
100-test byte prefix. They exercise real adapter/collector/flatten with fixture
services: top-level containers, every consumed object and array, missing required
arrays, array/scalar aliases, consumed scalar containers, valid alternate names,
optional boosters, observational no_stats, immutable source and opaque metadata.
The exact nm:false,nms:[] response with another valid app fails before both writers,
with HTTP_NM_ALIASES in failure/status evidence, new journal ERROR and no COMPLETE.
A malformed campaign later in the same response blocks the entire response; a
malformed final batch blocks earlier prepared 400 stat and 400 booster rows.
This is implementation validation only; ONE separate practical safety review is next.

## Validation commands and scope

* `node --test tools/step5a_appsscript_tests/day-repair.test.js`
* `node --test tools/step5a_appsscript_tests/resume.test.js`
* `node tools/step5a_appsscript_tests/run.js`
* `npm test -- --reporter=default` in cloud (includes all 43 mart tests).
* Existing temporary venv Python `-m pytest tools/tests -q -k
  'not test_gcloud_is_unauthenticated_inside_agent_env'`.
* Same existing venv Python `tools/validate_current_sql.py` (repository-only C1–C18).
* `python3 tools/impact_analysis.py --files` restricted to the four incident files,
  captured in IMPACT.json. Prefix-based framework conservatively maps all ingestion
  producers (95 objects/TIER0); this is not the actual mutation allowlist. Actual
  allowlist is the four objects in CONTRACT.md. No production data gates executed.
* `git diff --check`, syntax execution through VM, SHA-256 preservation/package proof.

The gcloud token command test is **deselected**, never called. Other credential/native
services in focused tests are fixture objects; no real Google client or WB request.
System Python initially lacked pytest/sqlglot (validation environment errors);
rerun uses the already existing venv, no install or global environment change.
Exact completed test counts are recorded in TEST_RESULTS.json.

Focused tests cover pinned range/current-date drift, population/order/control priority,
status7 preservation, full coverage, controls-only PARTIAL, response shape, date/booster
scope, nested/conflicting/exact repeated grain before both RAW writers, no_stats,
old parent immutability, new attempt lifecycle, publication jobs/readback/effective
history, costs/campaign reuse, disabled/lock/sink/schema/Sheet surprise, context drift,
truncated SELECT, deadline and inspectable partial control evidence. Tests exercise
real collector/flatten and real narrow adapter with all Google/WB services substituted.
No test PASS is represented as independent review or live compatibility proof.

## Package boundary

OWN_CODE_DIFF.patch is the authoritative **incident delta against the startup dirty
WbAdsRawLoader baseline**, plus new incident source/tests/CONTRACT/report/evidence.
It intentionally excludes pre-existing generalized resume changes. The reconstructed
startup loader bytes are included as baseline evidence, and their SHA matches the
startup snapshot. This permits exact patch replay without touching Git/index.

READ_ONLY_CONTEXT contains unchanged dependencies WbAdsProbe.gs, Wbadsbigquery.gs,
IngestRunLog.gs and WbBigQuery.gs. Their bytes/hashes are pinned in the manifest,
explicitly evidence/deployment dependencies, not incident code changes. Existing
schema definitions are reused; there is no new schema.sql or DDL. The old resume
package is historical evidence and is not authoritative for this incident scope.

Manifest lists full current file bytes, roles, Git tracked/untracked status, hashes,
patch hash and baseline hash. Package reconstruction verifies each patched file's
exact bytes and all context files' current SHA. New tests/contract are included.
REVIEW_PACKAGE completeness refers to this narrow local scope only.

## Unproven before execution

Actual deployed source/container/runtime permissions, exclusion of concurrent writers,
approved live campaign/view hashes, current persisted evidence, WB response/control
outcomes, source finality, and production BQ/Sheet readback remain UNPROVEN. No
production probe is used to close these gates in this task. Hash arguments are not
filled from synthetic test fixtures.

Old heartbeat INS_ADS_20261001050734_5253f5ba is referenced only in SELECT/evidence;
mutation wrappers forbid this ID. No production state was changed in this task.

Next owner ACK: **independent final review only** of this exact incident package,
including source/runtime, all guards, effective-view grain, journal semantics,
write scope, source-contract/no_stats limits and deployment completeness. No deploy,
activation or recovery execution is included. Mart/Unitka remain separate ACKs.


## Current complete scalar-contract correction — 2026-10-02

Owner ACK accepts the independent-review FAIL for exact candidate
49140997d5c12668022ce8ef06b2bc78eb41ec53a02580c565d7bdc6d77ceb18.
The reviewed defect was boolean sum serialized into RAW STRING "false", with
byte readback proving persistence but not business parseability. COMPLETE was
reachable although mart NUMERIC parse-QC would reject it.

Current source changes are bounded to WbAdsDayRepair.gs (two explicit complete
field contracts, primitive/INT64/NUMERIC/FLOAT/timestamp predicates and whole prepared
payload validation) and adsDaySourceScalars_ in WbAdsDayRepairRuntime.gs (same
contract on ORIGINAL consumed leaves before flatten). The former source shape,
alias, pinned date, population/order, grains, controls, reuse, publication, readback,
heartbeat and financial/history checks remain intact. Date guard error codes are
preserved. Names/opaque raw_json accept their existing optional text semantics;
raw_json may be truncated, therefore JSON parsing is not invented as a gate.

The focused suite now includes table-driven real-adapter adversarial mutation of
EVERY one of the 24 stat and 11 booster persisted fields. Boolean/object/array are
rejected for all; required fields also reject null/undefined/empty. Numeric fields
reject nonnumeric strings/NaN/Infinity, integer fractions and range overflow;
NUMERIC tests include overflow caused by consumer rounding. Invalid prepared rows
prove zero stat writer calls, zero booster writer calls, no new COMPLETE, preserved
old parent and an inspectable new journal error. Source-leaf sweeps additionally
cover malformed numeric text and malformed scalar in otherwise empty branches.
The exact original sum:false counterexample uses actual incident adapter, collector,
flatten and mocked production service boundaries. A separate malformed prepared
sum:false injection proves the final whole-payload guard independently of HTTP.
All four historical source/grain counterexamples remain blocked with zero RAW and
no COMPLETE; their results are recorded in TEST_RESULTS.json.

Valid-representation tests retain number/numeric string/NULL/absence/empty semantics,
signed and fractional values where permitted, INT64 boundaries/hex strings, comma
NUMERIC/exponents, exact NUMERIC boundaries, scale rounding/underflow, label aliases,
name text and opaque/truncated raw_json. Entire valid payload bytes are unchanged.
Two previous fixture constructors gain generated timestamp/raw_json values matching
the actual flatten shape; all existing 231 test bodies remain byte-preserved.
These are offline implementation tests, not independent review evidence.

All validation commands above are rerun for this candidate; exact completed counts
and exclusions are authoritative in TEST_RESULTS.json. Python token command test
remains DESELECTED, never PASS. No real WB/Google/auth probe is executed.

Package additionally contains byte-identical read-only copies of three SQL consumer
contracts and the historical ADS3 scalar type evidence. No original SQL/schema or
consumer is modified. Manifest hashes cover these dependencies; every patched
file reconstructs byte-for-byte in memory. Preservation evidence compares all
protected source/tests/old-resume/SQL files and HEAD/branch/index to a fresh startup
snapshot. Previous preservation records are explicitly historical, not current
claims for files modified in this bounded ACK.

STOP after local validation. Fresh independent review of this exact updated package
requires a separate owner ACK; no deployment/activation/recovery is included.
