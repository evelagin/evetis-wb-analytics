# Client_001 SKU qualification acceleration

Owner authority: 2026-10-07, evidence-based quota calibration ACK. This document describes
candidate code; code/PR/image/schedule existence does not establish live completion.

## Source contract (official documentation read 2026-10-07)

- https://docs.ozon.ru/api/performance/#operation/SubmitRequest
- https://docs.ozon.ru/api/performance/#tag/Limits
- https://docs.ozon.ru/api/performance/#operation/StatisticsCheck
- https://docs.ozon.ru/api/performance/#operation/DownloadStatistics

POST `/api/client/statistics` accepts `campaigns` as uint64 strings, up to ten per report.
Use `dateFrom`, `dateTo`, `groupBy=DATE`. A single campaign returns CSV; multiple campaigns
return ZIP, one `<campaign-id>.csv` per campaign. The maximum period is62days. Each campaign
counts as an export even within one request. Documented account allowance is the smaller of
active campaigns times240 and2000 per24hours; organization cap2000, account concurrency1,
organization concurrency5. These documented bounds do not prove this account's actual allowance.
The runtime already batches campaigns; batching reduces submissions, not charged export units.

## Exact continuity boundary

Preserve root `4f4387b2bdaa39c8942735a3b9b386e665b20c61006c8e8a467832a7542b1779`,
SKU plan `d54a84904bba901fbf2788a60bff74eed178a62fd4f56eb41fbc19c8dbc5f701`,
and original Supplies plan, generation, origin, execution budgets and acknowledgements.
The packaged `qualification_resume.json` is the immutable accepted manifest, with no credentials
or seller identifiers. The exact bytes are included in the new runtime implementation hash.
`BACKFILL_RESUME_PLAN_ID` accepts only a packaged plan with identical semantic inputs. The old
plan's implementation hash identifies historical evidence; it never claims the new image contains
old code. Current-image qualification must explicitly attest this exact compatible root and actual
new implementation hash. New execution receipts must prove the current canonical image.
No active execution or unreconciled receipt may cross deployment; prior receipts remain immutable.

At baseline:30/90 completed,60 pending,sequence14,no async report. No completed campaign is
refetched. Calibration applies only to this accepted SKU plan, with the proven30-campaign prefix.
Start rolling reservation cap25; after each fully reconciled ten-campaign increment allow ten
more (35,45,55,65,75), maximum90. Ordinary scopes retain15. Unknown ordinary export accounting
fails closed. Reservations remain charged even after rejection; account concurrency stays1.
Before every controller dispatch recheck bindings, credentials, owner hold, active executions,
qualified images/identities, PAUSED ordinary Schedulers and current-day Catalog certification.
Before the report POST runtime rechecks owner signs, actual identity and latest credential verdicts.

Each intent records campaign cohort and deterministic hash before POST. A missing ACK prevents
POST. Submission transport never automatically retries; lost responses/5xx/missing UUID preserve
ambiguous intent and stop. Known UUID polling/download resumes without another POST. Unexpected
state, missing campaign file, conflicting natural keys or failed Catalog linkage cannot be COMPLETE.
Known HTTP429 persists safe status/time/numeric quota headers and a cooldown; it never persists raw
API body/auth headers. Preserve rejected reservation, return to15-export guard, wait at least1hour
(or longer Retry-After), and do not busy-retry. More than three renewed throttles or retry instruction
beyond24hours stops for review. Poll/download429 keeps UUID. Network-free adversarial tests cover
continuity, six batches, lost ACK, binding revocation, ambiguous POST, cooldown and header whitelist.

## Deployment and rollback

Only client_001 canonical runtime/control images and dedicated controller release/cadence may
change after exact-image qualification and reviewed Terraform plan. Dedicated cadence10minutes;
ordinary daily/fast/weekly remain PAUSED and unchanged. A wake performs at most one terminal
reconciliation followed by one new dispatch through a freshly reconstructed Tick (within300seconds);
WAIT/MONITOR/STOP/DISPATCH never busy-loop. No IAM, credentials, secrets, policy,
identity algorithm, schema, lifecycle, historical trial data, EVETIS or business semantics change.
Use a clean current-main source commit and pinned digests. Do not update old build configs/releases.

Rollback must pause source progression safely through reviewed controller changes, preserve all
source checkpoints/receipts and new throttling/cohort evidence, and qualify a compatible artifact.
Do not blindly restore the old runtime while new-state fields/intents/receipts exist. Never replay
ambiguous POST, erase reservations, regenerate the accepted root or reset completed campaigns.

SKU90 requires source proof and persisted natural-key/Catalog reconciliation, then stops SKU
source calls. It does not imply full-history GO or READY. Corrected probes, rediscovery, Supplies
scale and canonical full-history manifest/gates remain separate owner-authorized requirements.
Original global DoD external gates remain BLOCKED until independently proven; tenant applicability
exception preserves exact-image, CI, isolation, source/DQ/reconciliation and reviewed-deployment gates.

## Artifact staging (not deployed)

The runtime candidate is built from the exact reviewed PR269 source commit, not represented
as an already merged main commit. The accepted source and actual image hashes are explicit.
Intermediate branch revision `8b00577f1cecd0853952bd6cfeb8fbf419e66577` omitted the controller opt-in only to assemble/qualify
its next immutable artifact without a circular digest dependency; it was never applied.
Final integration restores the same root with independently qualified controller metadata.
The unchanged packaged-controller CI passed for exact source `8b00577f1cecd0853952bd6cfeb8fbf419e66577` (run37581482726).
Final integration still must pass CI and merge before reviewed deployment; candidate builds
are not falsely labeled as main-at-build or live deployment.
